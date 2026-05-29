"""Rendering layer: turn :class:`ScanResult` into rows, CSV, text, JSON.

Kept separate from :mod:`cli` so the orchestration (argparse, session,
calibration, scan invocation) stays under ~250 LOC and presentation logic
is independently testable.
"""

from __future__ import annotations

import csv
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Sequence

import phonenumbers

from kronieker.pipeline import ScanResult


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------


def _human_date(ts: str) -> str:
    """Wayback ``YYYYMMDDhhmmss`` → ``YYYY-MM-DD``.

    The snapshot time (HHMMSS) is the IA crawler's clock, not the site
    operator's, so we drop it as noise for human readers.
    """
    if not ts or len(ts) < 8 or not ts[:8].isdigit():
        return ts or ""
    return f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}"


def _human_phone(e164: str) -> str:
    """E.164 (``+498944241400``) → INTERNATIONAL (``+49 89 44241400``)."""
    try:
        num = phonenumbers.parse(e164, None)
        return phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.INTERNATIONAL)
    except phonenumbers.NumberParseException:
        return e164


def _format_table(rows: Sequence[Sequence[str]], headers: Sequence[str]) -> str:
    """Render a minimal monospace table without an external dep."""
    if not rows:
        cols = len(headers)
    else:
        cols = max(len(headers), max(len(r) for r in rows))

    widths = [len(h) for h in headers] + [0] * (cols - len(headers))
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    out = [fmt.format(*headers, *([""] * (cols - len(headers))))]
    out.append("  ".join("─" * w for w in widths))
    for row in rows:
        padded = list(row) + [""] * (cols - len(row))
        out.append(fmt.format(*padded))
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Row aggregation
# ---------------------------------------------------------------------------


@dataclass
class _Row:
    kind: str
    value: str  # canonical (E.164 / lowercased email)
    value_human: str  # nicely formatted phone or email as-is
    value_raw: str  # distinct as-seen forms on the page(s), joined by " | "
    first_ts: str  # raw YYYYMMDDhhmmss
    last_ts: str
    sightings: int
    first_url: str  # web.archive.org snapshot URL
    last_url: str


def _aggregate_rows(result: ScanResult) -> list[_Row]:
    rows: list[_Row] = []
    for value, sightings in result.by_value().items():
        ordered = sorted(sightings, key=lambda s: s.timestamp)
        first, last = ordered[0], ordered[-1]
        kind = first.contact.kind
        human = _human_phone(value) if kind == "phone" else value
        # Distinct as-seen forms, preserving order of first appearance — gives
        # the analyst every literal rendering the site actually used (e.g.
        # "8-0162-51-12-54" alongside "+375 162 51-12-54").
        raw_variants: list[str] = []
        for s in ordered:
            r = (s.contact.raw or "").strip()
            if r and r not in raw_variants:
                raw_variants.append(r)
        rows.append(
            _Row(
                kind=kind,
                value=value,
                value_human=human,
                value_raw=" | ".join(raw_variants),
                first_ts=first.timestamp,
                last_ts=last.timestamp,
                sightings=len(ordered),
                first_url=first.snapshot_url,
                last_url=last.snapshot_url,
            )
        )
    rows.sort(key=lambda r: (r.first_ts, r.kind, r.value))
    return rows


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------


def _sanitize_filename(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", s).strip("_") or "domain"


def _default_csv_path(domain: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Path.cwd() / f"{_sanitize_filename(domain)}_{stamp}.csv"


def _write_csv(path: Path, rows: list[_Row]) -> None:
    # UTF-8 with BOM so Excel opens non-ASCII content without garbled glyphs.
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "kind",
                "value",
                "value_human",
                "value_raw",
                "first_seen",
                "last_seen",
                "sightings_count",
                "first_archive_url",
                "last_archive_url",
            ]
        )
        for r in rows:
            w.writerow(
                [
                    r.kind,
                    r.value,
                    r.value_human,
                    r.value_raw,
                    _human_date(r.first_ts),
                    _human_date(r.last_ts),
                    r.sightings,
                    r.first_url,
                    r.last_url,
                ]
            )


# ---------------------------------------------------------------------------
# Text / JSON renderers
# ---------------------------------------------------------------------------


def _timeout_hint(result: ScanResult) -> str | None:
    """Return a one-line stderr hint based on whether the scan hit its timeout."""
    # Single-URL mode never benefits from ``--all`` (the filter was already
    # off) or from "scan another path" suggestions, so suppress those
    # hints entirely. Timeout-exhaustion is still worth flagging — but only
    # the "extend timeout" half, not the "try --all" half.
    if result.single_url:
        if result.timeout_exhausted and result.timeout_seconds > 0:
            return (
                f"Timeout exhausted after {result.elapsed_seconds:.0f}s — "
                f"try `--timeout {int(result.timeout_seconds * 2)}` to fetch "
                f"more snapshots of this URL."
            )
        return None
    if result.timeout_exhausted and result.timeout_seconds > 0:
        return (
            f"Timeout exhausted after {result.elapsed_seconds:.0f}s. "
            f"Try `--timeout {int(result.timeout_seconds * 2)}` or `--all` "
            f"for deeper coverage."
        )
    if result.url_filter_active and result.timeout_seconds > 0:
        return (
            "Contact-URL filter was on. For obscure custom contact pages, "
            "try `--all` (scan every URL) or `--exhaustive`."
        )
    return None


def _render_text_report(result: ScanResult, rows: list[_Row], csv_path: Path | None) -> None:
    timeout_str = (
        f"{result.timeout_seconds:.0f}s" if result.timeout_seconds > 0 else "unlimited"
    )
    # Header line names the URL in single-URL mode (we're scanning one page,
    # not the whole host) and the bare domain otherwise.
    if result.single_url:
        print(f"URL: {result.single_url}")
    else:
        print(f"Domain: {result.domain}")
    print(
        f"Timeout: {timeout_str} | "
        f"Elapsed: {result.elapsed_seconds:.1f}s | "
        f"Considered: {result.snapshots_considered:,} snapshots | "
        f"Fetched: {result.snapshots_fetched} | "
        f"Distinct contacts: {len(rows)}"
    )
    if result.plan_rationale:
        print(f"Plan: {result.plan_rationale}")
    if result.timeout_exhausted:
        if result.single_url:
            print("(stopped at timeout — pass --timeout N for more)")
        else:
            print("(stopped at timeout — pass --timeout N or --all for more)")
    print()

    if rows:
        table_rows = [
            [f"{r.kind:5}  {r.value_human}", _human_date(r.first_ts), _human_date(r.last_ts)]
            for r in rows
        ]
        print(_format_table(table_rows, headers=["Contact", "First seen", "Last seen"]))
    else:
        print("No contacts found.")

    if csv_path is not None:
        print(f"\nCSV saved: {csv_path}", file=sys.stderr)

    hint = _timeout_hint(result)
    if hint:
        print(f"\nHint: {hint}", file=sys.stderr)

    if result.errors:
        print("\nErrors (first 10):", file=sys.stderr)
        for err in result.errors[:10]:
            print(f"  - {err}", file=sys.stderr)


def _render_json_report(result: ScanResult, rows: list[_Row], csv_path: Path | None) -> None:
    json.dump(
        {
            "domain": result.domain,
            "single_url": result.single_url,
            "timeout_seconds": result.timeout_seconds,
            "elapsed_seconds": round(result.elapsed_seconds, 2),
            "timeout_exhausted": result.timeout_exhausted,
            "plan_rationale": result.plan_rationale,
            "url_filter_active": result.url_filter_active,
            "snapshots_considered": result.snapshots_considered,
            "snapshots_fetched": result.snapshots_fetched,
            "hint": _timeout_hint(result),
            "csv_path": str(csv_path) if csv_path else None,
            "contacts": [
                {
                    "kind": r.kind,
                    "value": r.value,
                    "value_human": r.value_human,
                    "value_raw": r.value_raw,
                    "first_seen": _human_date(r.first_ts),
                    "last_seen": _human_date(r.last_ts),
                    "sightings": r.sightings,
                    "first_archive_url": r.first_url,
                    "last_archive_url": r.last_url,
                }
                for r in rows
            ],
            "errors": result.errors,
        },
        sys.stdout,
        indent=2,
        ensure_ascii=False,
    )
    sys.stdout.write("\n")
    if csv_path is not None:
        print(f"CSV saved: {csv_path}", file=sys.stderr)
