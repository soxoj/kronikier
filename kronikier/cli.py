"""Command-line entry point: ``kronikier example.com``."""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path

import requests

from kronikier.calibration import (
    Calibration,
    ensure_calibration,
)
from kronikier.cdx import DEFAULT_USER_AGENT
from kronikier.pipeline import scan_domain
from kronikier.report import (
    _aggregate_rows,
    _default_csv_path,
    _render_json_report,
    _render_text_report,
    _write_csv,
)


# ---------------------------------------------------------------------------
# Targets-file parsing
# ---------------------------------------------------------------------------


@dataclass
class Target:
    """One scan target: a domain plus any extra path hints from URL entries.

    ``single_url`` is set only by ``--url URL`` (single-URL mode). When
    non-None, the scan asks CDX for snapshots of that exact URL
    (matchType=exact), skips the well-known probe, and ignores the
    contact-URL filter.
    """

    domain: str
    extra_paths: tuple[str, ...] = field(default_factory=tuple)
    single_url: str | None = None


def _host_for_single_url(url: str) -> str | None:
    """Pull the bare host out of a full URL, for the per-target progress
    header. Returns ``None`` if the input doesn't parse as an absolute
    http(s) URL with a host.
    """
    from urllib.parse import urlparse

    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https"):
        return None
    host = (parsed.netloc or "").split(":", 1)[0].lower()
    return host or None


def _parse_target_line(line: str) -> tuple[str, str | None]:
    """Parse one target line into ``(domain, extra_path_or_None)``.

    Accepts bare domains (``example.com``), schemed URLs
    (``https://example.com/o-nas``), and ``host/path`` shorthand
    (``example.com/contact-form``). Strips scheme, port, and ``www`` left
    alone (subdomain semantics belong to CDX's ``matchType=domain``). Path
    is normalised with a leading ``/``; a bare or empty path returns ``None``.
    """
    s = line.strip()
    if "://" in s:
        s = s.split("://", 1)[1]
    host, sep, path = s.partition("/")
    host = host.split(":", 1)[0].lower()
    if not host:
        raise ValueError(f"empty domain in target line: {line!r}")
    if not sep:
        return host, None
    norm_path = "/" + path
    if norm_path in ("", "/"):
        return host, None
    # Drop trailing slash for path-stability (probe URL builder doesn't care).
    if norm_path != "/" and norm_path.endswith("/"):
        norm_path = norm_path.rstrip("/")
    return host, norm_path


def parse_targets_file(path: Path) -> list[Target]:
    """Read a targets file → list of unique ``Target``\\ s.

    Format: one entry per line, ``#`` comments, blank lines ignored.
    Multiple URLs for the same host merge into a single ``Target`` whose
    ``extra_paths`` preserves first-seen order.
    """
    by_domain: dict[str, list[str]] = {}
    order: list[str] = []
    text = path.read_text(encoding="utf-8")
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            domain, extra = _parse_target_line(line)
        except ValueError as e:
            raise ValueError(f"{path}:{lineno}: {e}") from None
        if domain not in by_domain:
            by_domain[domain] = []
            order.append(domain)
        if extra and extra not in by_domain[domain]:
            by_domain[domain].append(extra)
    return [Target(domain=d, extra_paths=tuple(by_domain[d])) for d in order]


#: Named-mode aliases over ``--timeout``. Documented and shown in ``--help``;
#: the user can use either form interchangeably. Mutually exclusive with
#: ``--timeout``.
MODE_ALIASES: dict[str, tuple[float, bool]] = {
    "auto":       (300.0, False),
    "default":    (300.0, False),
    "deep":       (900.0, False),
    "exhaustive": (0.0,   True),
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="kronikier",
        description=(
            "Mine emails and phone numbers from a domain's web.archive.org "
            "history. Scans are driven by a wall-clock timeout — pass "
            "--timeout to control how long the tool runs, or use the "
            "named-mode aliases."
        ),
    )
    p.add_argument(
        "domain",
        nargs="?",
        help="Target domain, e.g. example.com (no scheme). Omit when "
        "running --calibrate or --targets-file.",
    )
    p.add_argument(
        "--targets-file",
        type=Path,
        default=None,
        metavar="PATH",
        help="Read a list of targets from PATH — one per line, '#' for "
        "comments, blanks ignored. Each entry can be a bare domain "
        "('example.com') or a URL ('https://example.com/team'); URL paths "
        "are added to the well-known probe list for that domain's scan. "
        "Multiple URLs sharing a domain are merged. Mutually exclusive "
        "with the positional `domain` argument.",
    )
    p.add_argument(
        "--single-url",
        dest="single_url",
        default=None,
        metavar="URL",
        help="Single-URL mode: scan only the archived snapshots of the exact "
        "URL passed here (CDX matchType=exact). Disables the well-known "
        "probe and the contact-URL filter — we already know what we're "
        "looking at. Useful for inspecting how one page evolved over "
        "time. Mutually exclusive with the positional `domain` and "
        "`--targets-file`.",
    )

    # ----- Timeout & mode -----
    timeout_group = p.add_mutually_exclusive_group()
    timeout_group.add_argument(
        "--timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help="Wall-clock timeout for the scan, in seconds. 0 = unlimited. "
        "Default 300 sec. (Not to be confused with --cdx-timeout, which is "
        "the per-request CDX read-timeout.)",
    )
    timeout_group.add_argument(
        "--auto",
        action="store_const", dest="mode_alias", const="auto",
        help="Alias for --timeout 300 (the default).",
    )
    timeout_group.add_argument(
        "--default",
        action="store_const", dest="mode_alias", const="default",
        help="Alias for --timeout 300 — fast, contact-URL filter on.",
    )
    timeout_group.add_argument(
        "--deep",
        action="store_const", dest="mode_alias", const="deep",
        help="Alias for --timeout 900 — broader scan, contact-URL filter on.",
    )
    timeout_group.add_argument(
        "--exhaustive",
        action="store_const", dest="mode_alias", const="exhaustive",
        help="Alias for --timeout 0 --all — unlimited time, every URL.",
    )

    p.add_argument(
        "--all",
        action="store_true",
        dest="force_all",
        help="Disable the contact-URL CDX filter — scan every URL the site "
        "has, not just typical contact pages. Independent of --timeout.",
    )

    # ----- Calibration -----
    p.add_argument(
        "--calibrate",
        action="store_true",
        help="Run the latency calibration and exit. Refreshes the persistent "
        "cache; safe to run any time.",
    )
    p.add_argument(
        "--recalibrate",
        action="store_true",
        help="Refresh the latency calibration before running the scan.",
    )
    p.add_argument(
        "--no-escalate",
        action="store_true",
        help="Disable the one-shot zero-result escalation (broaden URL filter "
        "or extend timeout when the first pass finds nothing).",
    )

    # ----- Scope -----
    p.add_argument("--max-snapshots", type=int, default=None,
                   help="Optional hard ceiling on snapshots to fetch — layered "
                   "on top of the timeout-derived capacity. Default: timeout decides.")
    p.add_argument("--from-year", type=int, default=None, help="Limit CDX query to >= this year")
    p.add_argument("--to-year", type=int, default=None, help="Limit CDX query to <= this year")
    p.add_argument("--no-subdomains", action="store_true",
                   help="Restrict to the exact domain (default includes subdomains)")
    p.add_argument("--no-probe", action="store_true",
                   help="Skip the typical-contact-pages availability probe.")
    p.add_argument("--min-score", type=int, default=0, help="Drop URLs scoring below this")

    # ----- Performance -----
    p.add_argument("--workers", type=int, default=4, help="Concurrent fetches (default 4)")
    p.add_argument("--rate", type=float, default=4.0,
                   help="Max requests/sec to wayback (default 4)")
    p.add_argument("--cdx-timeout", type=int, default=300,
                   help="Per-request CDX read-timeout, in seconds (default 300). "
                   "Different from --timeout, which is the whole-scan deadline.")

    # ----- Locale / output -----
    p.add_argument("--regions", default="RU,BY,UA,KZ,US,GB,DE,FR",
                   help="Comma-separated phone regions for bare local numbers. "
                   "The domain's ccTLD is automatically prepended.")
    p.add_argument("--json", action="store_true",
                   help="Emit JSON instead of human-readable text")
    p.add_argument("--csv", dest="csv_path", default=None,
                   help="Path to write the CSV report (default: "
                   "<domain>_<timestamp>.csv in CWD)")
    p.add_argument("--no-csv", action="store_true", help="Do not write a CSV report")

    # ----- Cache -----
    p.add_argument("--no-cache", action="store_true",
                   help="Disable the on-disk snapshot cache for this run "
                   "(every fetch goes to web.archive.org). Default: cache is "
                   "ON at ~/.cache/kronikier/snapshots/.")
    p.add_argument("--clear-cache", action="store_true",
                   help="Delete every cached snapshot and exit. Use to free disk "
                   "space; does not run a scan.")

    p.add_argument("--no-progress", action="store_true",
                   help="Disable progress bars (auto-disabled if stderr is not a TTY)")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="Verbose contact feed: print the capture date and the "
                   "full snapshot URL beneath each discovered contact in the "
                   "live feed. Does NOT enable DEBUG logs — use -d for that.")
    p.add_argument("-d", "--debug", action="store_true",
                   help="Enable DEBUG-level logging (probe / fetch / cache "
                   "internals). Independent of -v.")
    return p


def resolve_timeout(args: argparse.Namespace) -> tuple[float, bool]:
    """Translate ``args`` (after argparse) into ``(timeout_seconds, force_all)``.

    Precedence: named alias > explicit ``--timeout`` > default (300 s).
    ``--all`` is sticky on top of any alias.
    """
    mode_alias = getattr(args, "mode_alias", None)
    force_all = bool(args.force_all)

    if mode_alias is not None:
        timeout, alias_all = MODE_ALIASES[mode_alias]
        return timeout, force_all or alias_all
    if args.timeout is not None:
        return float(args.timeout), force_all
    return 300.0, force_all


def _run_calibrate_only() -> int:
    """Handle the ``--calibrate`` subcommand: refresh cache and exit."""
    sess = requests.Session()
    sess.headers["User-Agent"] = DEFAULT_USER_AGENT
    cal = ensure_calibration(session=sess, force=True, announce=True)
    print(
        f"avg_latency = {cal.avg_latency_s:.3f}s "
        f"(p50={cal.samples_p50:.3f}, p95={cal.samples_p95:.3f}, "
        f"samples={cal.sample_count})"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return _main_impl(argv)
    except KeyboardInterrupt:
        # Backstop only: Ctrl+C *during* a scan is caught inside ``_scan`` so
        # we can finalize the partial CSV first. This handler covers SIGINT
        # delivered between scans (e.g. mid-calibration, between batch items,
        # or during teardown) — swallow the stack trace and exit cleanly.
        print("\nInterrupted.", file=sys.stderr)
        return 130  # 128 + SIGINT


def _main_impl(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # -d / --debug controls log level; -v / --verbose only affects the
    # contact feed (see ProgressUI(verbose=…) below).
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.calibrate:
        return _run_calibrate_only()

    if args.clear_cache:
        from kronikier.cache import SnapshotCache, default_cache_dir
        cache_dir = default_cache_dir()
        cache = SnapshotCache(cache_dir)
        removed = cache.clear()
        cache.close()
        print(
            f"Cleared {removed} cached snapshot{'s' if removed != 1 else ''} "
            f"from {cache_dir}",
            file=sys.stderr,
        )
        return 0

    # --url is the third input mode (alongside positional <domain> and
    # --targets-file); the three are mutually exclusive — combining them
    # is ambiguous, not additive.
    input_modes = sum(
        bool(x) for x in (args.domain, args.targets_file, args.single_url)
    )
    if input_modes > 1:
        print(
            "error: pick exactly one input — a positional <domain>, "
            "--targets-file, or --single-url",
            file=sys.stderr,
        )
        return 2

    if args.single_url:
        # Single-URL mode: scan only that exact URL's archived snapshots.
        # The CDX query uses matchType=exact and the well-known probe is
        # skipped — we already know what we're looking at.
        host = _host_for_single_url(args.single_url)
        if host is None:
            print(
                f"error: --single-url must be an absolute http(s) URL with "
                f"a host; got {args.single_url!r}",
                file=sys.stderr,
            )
            return 2
        targets = [Target(domain=host, single_url=args.single_url)]
    elif args.targets_file:
        try:
            targets = parse_targets_file(args.targets_file)
        except (OSError, ValueError) as e:
            print(f"error reading targets file: {e}", file=sys.stderr)
            return 2
        if not targets:
            print(f"error: no targets found in {args.targets_file}", file=sys.stderr)
            return 2
    elif args.domain:
        targets = [Target(domain=args.domain)]
    else:
        print(
            "error: an input is required — pass a positional <domain>, "
            "--single-url URL, --targets-file PATH, or --calibrate",
            file=sys.stderr,
        )
        return 2

    if args.csv_path and len(targets) > 1:
        print(
            "error: --csv PATH can't be used with multiple targets — drop it "
            "to use the default per-domain naming",
            file=sys.stderr,
        )
        return 2

    timeout_seconds, force_all = resolve_timeout(args)
    show_progress = not args.no_progress and sys.stderr.isatty()

    sess = requests.Session()
    sess.headers["User-Agent"] = DEFAULT_USER_AGENT
    calibration: Calibration | None = ensure_calibration(
        session=sess, force=args.recalibrate, announce=True,
    )

    regions = tuple(r.strip() for r in args.regions.split(",") if r.strip())
    batch = len(targets) > 1

    cache = None
    if not args.no_cache:
        from kronikier.cache import SnapshotCache, default_cache_dir
        cache_dir = default_cache_dir()
        try:
            cache = SnapshotCache(cache_dir)
            existing = cache.size()
            if existing:
                print(
                    f"[*] Snapshot cache: {existing:,} entries at {cache_dir}",
                    file=sys.stderr,
                )
            else:
                print(
                    f"[*] Snapshot cache: empty, will populate at {cache_dir}",
                    file=sys.stderr,
                )
        except OSError as e:
            print(
                f"warning: cache disabled — could not open {cache_dir}: {e}",
                file=sys.stderr,
            )
            cache = None

    for idx, target in enumerate(targets, start=1):
        if batch:
            print(
                f"\n=== [{idx}/{len(targets)}] {target.domain} "
                + (
                    f"(+{len(target.extra_paths)} extra probe path"
                    f"{'s' if len(target.extra_paths) != 1 else ''})"
                    if target.extra_paths else ""
                )
                + " ===",
                file=sys.stderr,
            )

        result = scan_domain(
            target.domain,
            timeout_seconds=timeout_seconds,
            force_all=force_all,
            no_escalate=args.no_escalate,
            calibration=calibration,
            max_snapshots=args.max_snapshots,
            from_year=args.from_year,
            to_year=args.to_year,
            include_subdomains=not args.no_subdomains,
            probe_well_known=not args.no_probe,
            min_score=args.min_score,
            session=sess,
            max_workers=args.workers,
            rate_limit_per_sec=args.rate,
            default_phone_regions=regions,
            extra_well_known_paths=target.extra_paths,
            single_url=target.single_url,
            cache=cache,
            progress=show_progress,
            verbose=args.verbose,
            cdx_timeout=args.cdx_timeout,
        )

        rows = _aggregate_rows(result)

        csv_path: Path | None = None
        if not args.no_csv and rows:
            csv_path = (
                Path(args.csv_path) if (args.csv_path and not batch)
                else _default_csv_path(target.domain)
            )
            _write_csv(csv_path, rows)

        if args.json:
            _render_json_report(result, rows, csv_path)
        else:
            _render_text_report(result, rows, csv_path)

        if result.interrupted:
            # Partial results are already saved (CSV above + report rendered).
            # Stop the batch loop — analyst pressed Ctrl+C, don't roll into
            # the next target.
            saved = f" saved to {csv_path}" if csv_path else ""
            print(
                f"\nInterrupted — {len(rows)} contact(s){saved}.",
                file=sys.stderr,
            )
            if cache is not None and (cache.hits or cache.misses):
                print(
                    f"[*] Cache: {cache.hits} hit / {cache.misses} miss "
                    f"(saved {cache.hits} fetch{'es' if cache.hits != 1 else ''} "
                    f"on web.archive.org)",
                    file=sys.stderr,
                )
            return 130

    if cache is not None and (cache.hits or cache.misses):
        print(
            f"[*] Cache: {cache.hits} hit / {cache.misses} miss "
            f"(saved {cache.hits} fetch{'es' if cache.hits != 1 else ''} "
            f"on web.archive.org)",
            file=sys.stderr,
        )

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
