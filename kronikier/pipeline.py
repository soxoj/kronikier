"""End-to-end pipeline: domain → ranked snapshots → fetched pages → contacts.

The pipeline is driven by a :class:`~kronikier.planner.ScanPlan` which
encodes the user's wall-clock timeout, the calibrated per-snapshot latency,
and the resulting "how many snapshots can we fetch / should we filter by URL"
decisions. Mode-name flags (``--default/--deep/--exhaustive/--auto``) are
preserved as documented aliases that lower into timeout presets.
"""

from __future__ import annotations

import logging
import math
import queue
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Iterable

import requests

if TYPE_CHECKING:
    from kronikier.cache import SnapshotCache

from kronikier.calibration import Calibration, DEFAULT_AVG_LATENCY_S
from kronikier.cdx import (
    DEFAULT_USER_AGENT,
    Snapshot,
    closest_snapshot,
    query_domain,
)
from kronikier.classifier import WELL_KNOWN_PATHS, score_url
from kronikier.extractors import Contact, extract_contacts
from kronikier.fetcher import FetchedPage, fetch_snapshots
from kronikier.planner import ScanPlan, broaden_plan, extend_plan, make_plan
from kronikier.progress_ui import ProgressUI

log = logging.getLogger(__name__)

#: Legacy mode names → (timeout_seconds, force_all). Kept as documented aliases
#: over ``--timeout``/``--all``. ``auto`` is a no-op (timeout is auto by design
#: now). ``exhaustive`` maps to timeout=0 (unlimited) + force_all=True.
_MODE_TO_TIMEOUT: dict[str, tuple[float, bool]] = {
    "auto": (300.0, False),
    "default": (300.0, False),
    "deep": (900.0, False),
    "exhaustive": (0.0, True),
}

#: Kept exported so other code (and tests) can introspect the legal mode set.
SCAN_MODES = tuple(_MODE_TO_TIMEOUT.keys())


@dataclass
class ContactSighting:
    """Where and when a particular contact value was seen."""

    contact: Contact
    snapshot_url: str
    timestamp: str
    source_url: str


@dataclass
class ScanResult:
    domain: str
    snapshots_considered: int
    snapshots_fetched: int
    sightings: list[ContactSighting] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    #: Legacy: which mode name (or alias) was effectively run. Now derived
    #: from the plan — kept for backward compat with older renderers.
    resolved_mode: str = "default"
    #: True if the scan stopped because the wall-clock timeout expired.
    timeout_exhausted: bool = False
    #: The timeout the run targeted (0 means unlimited).
    timeout_seconds: float = 0.0
    #: Actual wall-clock time the scan stage took (sec).
    elapsed_seconds: float = 0.0
    #: One-line human-readable explanation of the plan that ran.
    plan_rationale: str = ""
    #: Whether the contact-URL CDX filter was active during this scan.
    url_filter_active: bool = True
    #: True if the user hit Ctrl+C mid-scan. ``sightings`` still holds
    #: everything we collected before the interrupt; the CLI saves them
    #: to CSV before exiting with code 130.
    interrupted: bool = False
    #: Set to the URL when the scan was a single-URL run (--single-url). The
    #: text/JSON renderer uses it to label the report with the URL instead of
    #: the host and to suppress hints about ``--all`` / the contact-URL
    #: filter (neither applies to single-URL mode).
    single_url: str | None = None

    def by_value(self) -> dict[str, list[ContactSighting]]:
        out: dict[str, list[ContactSighting]] = defaultdict(list)
        for s in self.sightings:
            out[s.contact.value].append(s)
        return dict(out)

    def timeline(self) -> list[tuple[str, str, str, str]]:
        rows = [
            (s.timestamp, s.contact.kind, s.contact.value, s.source_url)
            for s in self.sightings
        ]
        rows.sort()
        return rows


def _pick_best_per_url_year(snapshots: Iterable[Snapshot]) -> list[Snapshot]:
    """Keep one snapshot per ``(urlkey, year)`` pair.

    Year-level granularity gives ~10 snapshots per URL over a typical archive
    lifespan — enough to surface timeline changes (e.g. /kontakty in 2018 vs
    2022) without exploding fetch count.
    """
    seen: set[tuple[str, str]] = set()
    out: list[Snapshot] = []
    for snap in snapshots:
        url_key = snap.urlkey or snap.original
        year = snap.timestamp[:4] if snap.timestamp else ""
        key = (url_key, year)
        if key in seen:
            continue
        seen.add(key)
        out.append(snap)
    return out


def _dedup_exact(snapshots: Iterable[Snapshot]) -> list[Snapshot]:
    """Strip exact (URL, timestamp) duplicates only — keep every distinct snapshot.

    Used when the plan says "preserve all timestamps" (i.e. unlimited timeout),
    so multiple snapshots of /kontakty within the same year all reach fetch.
    """
    seen: set[tuple[str, str]] = set()
    out: list[Snapshot] = []
    for s in snapshots:
        key = (s.urlkey or s.original, s.timestamp)
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def _ts_to_int(ts: str) -> int:
    """Normalize an 8 or 14 digit IA timestamp string to a comparable int.

    Our query timestamps look like ``"19990101"`` (8 digits, day precision),
    while IA's availability API returns 14-digit timestamps like
    ``"19990405120000"``. Padding with zeros on the right lets us compare
    them on the same axis: ``19990101000000 < 19990405120000``.
    """
    return int(ts.ljust(14, "0"))


def _iter_well_known(
    domain: str,
    session: requests.Session,
    probe_timestamps: Iterable[str] = ("19990101", "20050101", "20100101", "20150101", "20200101"),
    ui: ProgressUI | None = None,
    task=None,
    desc: str = "Searching typical contact pages",
    deadline: float | None = None,
    paths: Iterable[str] = WELL_KNOWN_PATHS,
) -> Iterable[Snapshot]:
    """Generator: yield each discovered well-known snapshot as soon as found.

    ``paths`` is the list of relative URL paths to probe; the bar is sized to
    ``len(paths) × len(probe_timestamps)`` slots so the analyst sees a fixed
    "questions to resolve" count. The actual HTTP call count is usually much
    lower thanks to two pieces of inference (see comments inline):

    1. **First-call-None ⇒ no captures.** If the very first availability
       call on a path returns ``None``, IA has zero snapshots for that URL.
       All later timestamps would also return ``None`` — skip them.
    2. **Covered-range inference.** When a call ``(Q, S)`` returns snapshot
       ``S`` for query ``Q``, IA's "closest" semantics guarantee no other
       snapshot exists in the interval ``[min(Q, S), max(Q, S)]`` — anything
       there would have been closer to ``Q`` than ``S``. So any remaining
       query whose timestamp falls inside a known covered range will return
       the same snapshot we already have. Skip the HTTP call.
    """
    seen_keys: set[tuple[str, str]] = set()
    found = 0
    timestamps = list(probe_timestamps)
    path_list = list(paths)
    total = len(path_list) * len(timestamps)

    own_task = False
    if ui is not None and task is None:
        task = ui.add_task(desc, total=total)
        own_task = True

    try:
        for path in path_list:
            url = f"http://{domain}{path}"
            # Per-path inference state. ``covered_ranges`` accumulates
            # ``(lo, hi)`` 14-digit timestamp pairs we've already proven
            # captureless (so future queries inside them are redundant).
            # ``had_any_hit`` distinguishes "first call returned None"
            # (whole path is dead → break) from "later call returned None"
            # (transient API blip → just skip this ts).
            covered_ranges: list[tuple[int, int]] = []
            had_any_hit = False
            dead_path = False

            for ts in timestamps:
                if dead_path:
                    # The first probe on this path returned None → URL has
                    # zero archived snapshots. Advance the bar for the
                    # remaining slots so the visual count stays aligned
                    # with `total`, but make no HTTP calls.
                    if ui is not None:
                        ui.advance(task)
                    continue

                if deadline is not None and time.monotonic() >= deadline:
                    return

                ts_int = _ts_to_int(ts)
                if any(lo <= ts_int <= hi for lo, hi in covered_ranges):
                    # We've already proven no new snapshot can come back for
                    # any timestamp in this interval. Skip the HTTP call but
                    # still tick the bar — same "slot resolved" semantics.
                    if ui is not None:
                        ui.advance(task)
                    continue

                try:
                    snap = closest_snapshot(url, timestamp=ts, session=session)
                except requests.RequestException as e:
                    log.debug("probe failed for %s @ %s: %s", url, ts, e)
                    snap = None
                if ui is not None:
                    ui.advance(task)

                if snap is None:
                    if not had_any_hit:
                        # First call on this path returned None → URL has
                        # no archived snapshots at all. Mark the path dead
                        # so the bar still ticks through remaining slots.
                        dead_path = True
                    # Later None: probably a transient API glitch. Move on
                    # to the next timestamp; covered_ranges still protects
                    # us from wasted retries on the same path.
                    continue

                had_any_hit = True
                key = (snap.timestamp, snap.original)
                if key not in seen_keys:
                    seen_keys.add(key)
                    found += 1
                    if own_task and ui is not None:
                        ui.update(task, postfix=f"found={found}")
                    yield snap

                # Mark the interval [min(Q, S), max(Q, S)] as covered. IA
                # returned ``snap`` as *globally* closest to ``ts``, so no
                # other snapshot exists strictly between them. Any remaining
                # query inside this interval would return the same snap.
                snap_ts_int = _ts_to_int(snap.timestamp)
                lo, hi = sorted((ts_int, snap_ts_int))
                covered_ranges.append((lo, hi))
    finally:
        if own_task and ui is not None:
            ui.stop_task(task)


# ---------------------------------------------------------------------------
# Phone-region prioritisation by ccTLD
# ---------------------------------------------------------------------------

_TLD_TO_REGION = {
    # ccTLDs — country code maps directly to libphonenumber region.
    # CIS / Eastern Europe.
    "ru": "RU", "by": "BY", "ua": "UA", "kz": "KZ", "md": "MD",
    "uz": "UZ", "tj": "TJ", "kg": "KG", "tm": "TM", "am": "AM",
    "az": "AZ", "ge": "GE",
    # Western & Central Europe.
    "uk": "GB", "gb": "GB", "ie": "IE", "de": "DE", "at": "AT",
    "ch": "CH", "fr": "FR", "be": "BE", "lu": "LU", "nl": "NL",
    "it": "IT", "es": "ES", "pt": "PT", "gr": "GR", "pl": "PL",
    "cz": "CZ", "sk": "SK", "hu": "HU", "ro": "RO", "bg": "BG",
    "hr": "HR", "si": "SI", "rs": "RS", "ba": "BA", "mk": "MK",
    "al": "AL", "ee": "EE", "lv": "LV", "lt": "LT",
    # Nordics.
    "se": "SE", "no": "NO", "fi": "FI", "dk": "DK", "is": "IS",
    # Anglosphere.
    "us": "US", "ca": "CA", "au": "AU", "nz": "NZ", "za": "ZA",
    # Middle East.
    "tr": "TR", "il": "IL", "ae": "AE", "sa": "SA", "qa": "QA",
    "kw": "KW", "bh": "BH", "om": "OM", "jo": "JO", "lb": "LB",
    "eg": "EG",
    # Asia-Pacific.
    "jp": "JP", "cn": "CN", "hk": "HK", "tw": "TW", "kr": "KR",
    "sg": "SG", "my": "MY", "th": "TH", "vn": "VN", "id": "ID",
    "ph": "PH", "in": "IN", "pk": "PK", "bd": "BD", "lk": "LK",
    # Americas (outside US/CA).
    "mx": "MX", "br": "BR", "ar": "AR", "cl": "CL", "pe": "PE",
    "ve": "VE", "uy": "UY", "py": "PY", "bo": "BO", "ec": "EC",
    # Africa.
    "ng": "NG", "ke": "KE", "gh": "GH", "ma": "MA", "tn": "TN",
    "dz": "DZ",
    # Generic TLDs — overwhelmingly US-anchored in practice. Without this
    # mapping the analyst's RU-first default order claims any 10-digit
    # ``(855) 843-7200``-style US number as ``+7 8558437200`` because
    # libphonenumber stops at the first region whose VALID leniency parses
    # the digits. Putting US in front lets the more-likely answer win;
    # actually-Russian companies on ``.com`` still parse via the second
    # pass once US misses. Override per-scan with ``--regions``.
    "com": "US", "org": "US", "net": "US", "io": "US",
    "co": "US", "app": "US", "ai": "US", "dev": "US",
    "tech": "US", "info": "US", "biz": "US", "xyz": "US",
}


def _regions_for_domain(domain: str, defaults: Iterable[str]) -> tuple[str, ...]:
    """Prepend the ccTLD-implied region to ``defaults`` (deduped)."""
    tld = domain.lower().rsplit(".", 1)[-1] if "." in domain else ""
    region = _TLD_TO_REGION.get(tld)
    seen: set[str] = set()
    out: list[str] = []
    if region:
        out.append(region)
        seen.add(region)
    for r in defaults:
        if r not in seen:
            out.append(r)
            seen.add(r)
    return tuple(out)


# ---------------------------------------------------------------------------
# Streaming helpers
# ---------------------------------------------------------------------------


def _make_announcer(ui: ProgressUI, seen_contact_keys: set[tuple[str, str]]):
    def announce(contact: Contact, snapshot_url: str, timestamp: str) -> None:
        key = (contact.kind, contact.value)
        if key in seen_contact_keys:
            return
        seen_contact_keys.add(key)
        date = f"{timestamp[:4]}-{timestamp[4:6]}-{timestamp[6:8]}"
        ui.announce_contact(contact.kind, contact.value, date, snapshot_url)

    return announce


def _absorb_contacts(
    page: FetchedPage,
    *,
    default_phone_regions: Iterable[str],
    sightings: list[ContactSighting],
    announce,
) -> int:
    """Run extraction on a fetched page; append every contact to ``sightings``.

    Email domain is **never** used as a rejection signal — small businesses
    routinely list ``their-name@mail.ru`` as the official contact. Obvious
    third-party widget emails are filtered earlier in :mod:`extractors`.
    """
    kept = 0
    for contact in extract_contacts(page.content, default_regions=default_phone_regions):
        sightings.append(
            ContactSighting(
                contact=contact,
                snapshot_url=page.snapshot.archive_url(raw=False),
                timestamp=page.snapshot.timestamp,
                source_url=page.snapshot.original,
            )
        )
        announce(
            contact,
            page.snapshot.archive_url(raw=False),
            page.snapshot.timestamp,
        )
        kept += 1
    return kept


# ---------------------------------------------------------------------------
# CDX streaming with a wall-clock sub-budget
# ---------------------------------------------------------------------------


def _stream_cdx_with_budget(
    domain: str,
    *,
    plan: ScanPlan,
    sess: requests.Session,
    from_year: int | None,
    to_year: int | None,
    include_subdomains: bool,
    cdx_timeout: int,
    sub_deadline: float,
    budget_s: float,
) -> tuple[list[Snapshot], bool, str | None]:
    """Iterate ``query_domain`` in a daemon thread; enforce ``sub_deadline``
    from the main thread.

    On huge sites the CDX server can spend minutes scanning the index before
    yielding a single byte — long enough that the user's whole timeout is
    gone by the time ``requests.get`` returns. Running the iteration in a
    background thread lets us walk away after ``budget_s`` seconds and still
    have time left for fetching. The worker keeps running until its own
    request finally times out (or the process exits); we simply stop reading
    from its queue.

    Returns ``(snapshots, truncated, error_message)``. ``truncated`` is
    ``True`` when the sub-deadline expired with the worker still busy.
    """
    out_q: queue.Queue = queue.Queue()
    DONE = object()
    ERR = object()

    def worker() -> None:
        try:
            for snap in query_domain(
                domain,
                from_year=from_year,
                to_year=to_year,
                include_subdomains=include_subdomains,
                urlkey_filter=plan.cdx_urlkey_filter,
                limit=plan.cdx_limit,
                # collapse=None: preserve multiple timestamps per URL — year
                # collapse is a client-side concern (_pick_best_per_url_year).
                collapse=None,
                session=sess,
                timeout=cdx_timeout,
            ):
                out_q.put(snap)
            out_q.put(DONE)
        except requests.RequestException as e:  # network / HTTP failure
            out_q.put((ERR, str(e)))
        except Exception as e:  # pragma: no cover — defensive
            out_q.put((ERR, f"{type(e).__name__}: {e}"))

    t = threading.Thread(target=worker, name="cdx-stream", daemon=True)
    t.start()

    snaps: list[Snapshot] = []
    truncated = False
    err_msg: str | None = None
    finished_via_done = False
    while True:
        if not math.isinf(sub_deadline):
            remaining = sub_deadline - time.monotonic()
            if remaining <= 0:
                truncated = True
                break
            poll = min(remaining, 1.0)
        else:
            poll = 1.0
        try:
            item = out_q.get(timeout=poll)
        except queue.Empty:
            continue
        if item is DONE:
            finished_via_done = True
            break
        if isinstance(item, tuple) and item and item[0] is ERR:
            err_msg = (
                f"CDX query failed: {item[1]} — try a longer "
                f"`--cdx-timeout` (current: {cdx_timeout} s)."
            )
            break
        snaps.append(item)

    # If we abandoned the worker (truncated=True), drain whatever extra rows
    # it already produced — they're free, the bytes are already on the wire.
    if truncated and not finished_via_done:
        deadline_after_drain = time.monotonic() + 0.2
        while time.monotonic() < deadline_after_drain:
            try:
                item = out_q.get(timeout=0.05)
            except queue.Empty:
                break
            if item is DONE or (isinstance(item, tuple) and item and item[0] is ERR):
                break
            snaps.append(item)

    return snaps, truncated, err_msg


# ---------------------------------------------------------------------------
# Plan-driven scanner
# ---------------------------------------------------------------------------


def _scan(
    plan: ScanPlan,
    *,
    domain: str,
    sess: requests.Session,
    from_year: int | None,
    to_year: int | None,
    include_subdomains: bool,
    probe_well_known: bool,
    min_score: int,
    max_workers: int,
    rate_limit_per_sec: float,
    default_phone_regions: Iterable[str],
    ui: ProgressUI,
    cdx_timeout: int,
    well_known_paths: tuple[str, ...] = WELL_KNOWN_PATHS,
    single_url: str | None = None,
    cache: "SnapshotCache | None" = None,
) -> ScanResult:
    """Run one scan according to ``plan``.

    The scanner is unified across all (filter, timeout, --all) combinations.
    Differences between former modes are now data on the plan:

    - ``plan.use_url_filter`` / ``plan.cdx_urlkey_filter`` / ``plan.cdx_limit``
      control the CDX query.
    - ``plan.capacity`` caps how many snapshots reach the fetch pool.
    - ``plan.deadline_monotonic`` is the absolute wall-clock cutoff.
    - ``plan.unlimited`` (timeout=0) flips us to "preserve every snapshot"
      dedup — keeping multi-year /kontakty pages distinct.
    """
    started_at = time.monotonic()
    deadline = plan.deadline_monotonic
    unlimited = plan.unlimited
    capacity = plan.capacity

    sightings: list[ContactSighting] = []
    errors: list[str] = []
    seen_contact_keys: set[tuple[str, str]] = set()
    announce = _make_announcer(ui, seen_contact_keys)

    fetched_count = 0
    found_snaps = 0
    # cdx_total is the count of candidates CDX returned (after dedup, before
    # ranking/capping). It feeds the user-facing "Considered: N snapshots"
    # stat — what the analyst wants to know is *how much was available*,
    # not just what we picked.
    cdx_total = 0
    probe_total = 0

    probe_timestamps = ("19990101", "20050101", "20100101", "20150101", "20200101")
    total_probes = len(well_known_paths) * len(probe_timestamps) if probe_well_known else 0
    # Total starts at total_probes but gets bumped to ``len(cdx_top) +
    # total_probes`` once the CDX query result is in. We don't know cdx_top's
    # size yet, so use total_probes (or None) as a placeholder so the bar has
    # *some* total to render — heartbeat/countdown rely on the task existing.
    bar_task = ui.add_task("Searching", total=total_probes or None)

    # _set_postfix is the single place the live bar's counters & countdown
    # are refreshed. Defined up-front so the heartbeat (below) and the CDX
    # query stage can keep the countdown ticking even before the producer
    # and consumer threads start.
    def _set_postfix() -> None:
        wip = max(0, found_snaps - fetched_count - len(errors))
        ui.update(
            bar_task,
            postfix=(
                f"pages={found_snaps}, fetched={fetched_count}, "
                f"err={len(errors)}, wip={wip}, "
                f"contacts={len(sightings)}"
            ),
        )
        ui.set_timeout_left(
            bar_task,
            deadline=None if math.isinf(deadline) else deadline,
            timeout_seconds=plan.timeout_seconds,
        )

    # Heartbeat — the producer/consumer call _set_postfix only on events
    # (snap accepted / page fetched / fetch error). Between events there can
    # be multi-second gaps: the initial CDX query, slow Wayback responses,
    # rate-limit sleeps, fetcher retry back-offs. Without a ticker those
    # gaps look like a frozen progress bar. A daemon thread keeps the
    # countdown column live at ~2 Hz regardless of pipeline activity, and
    # warns the user once if no fetch has completed in STALL_THRESHOLD_S
    # seconds (almost always one specific URL is stuck on a slow Wayback
    # response — not a global rate-limit).
    heartbeat_stop = threading.Event()
    STALL_THRESHOLD_S = 20.0
    stall_state = {"last_count": -1, "last_change": time.monotonic(), "warned": False}

    def _heartbeat() -> None:
        while not heartbeat_stop.wait(0.5):
            try:
                _set_postfix()
            except Exception:
                # Bar may already have been stopped — silently quit.
                return
            # Stall detection — drives off the consumer's progress events.
            now_count = fetched_count + len(errors)
            if now_count != stall_state["last_count"]:
                stall_state["last_count"] = now_count
                stall_state["last_change"] = time.monotonic()
                stall_state["warned"] = False
                continue
            if stall_state["warned"]:
                continue
            stalled_for = time.monotonic() - stall_state["last_change"]
            wip = max(0, found_snaps - fetched_count - len(errors))
            if stalled_for >= STALL_THRESHOLD_S and wip > 0:
                ui.status(
                    f"[!] No fetch completed in {stalled_for:.0f}s — "
                    f"{wip} request(s) likely stuck on a slow Wayback "
                    f"response (per-request timeout 30s × up to 4 attempts). "
                    f"Rerun with `-v --no-progress` to see which URL is "
                    f"stalled, or pass `--workers {max_workers * 2}` to "
                    f"add parallelism."
                )
                stall_state["warned"] = True

    heartbeat_thread = threading.Thread(
        target=_heartbeat, name="ui-heartbeat", daemon=True,
    )
    heartbeat_thread.start()

    # ----- 1) CDX query (always — drives ranked top-N feeding) -----
    cdx_top: list[Snapshot] = []
    if single_url is not None:
        cdx_target_label = single_url
        descr = "exact-URL"
    else:
        cdx_target_label = domain
        descr_parts = []
        if plan.use_url_filter:
            descr_parts.append("filtered")
        if plan.cdx_limit is not None:
            descr_parts.append("capped")
        if not descr_parts:
            descr_parts.append("full")
        descr = "+".join(descr_parts)
    ui.status(f"[*] Querying CDX index ({descr}) for {cdx_target_label}…")

    # Sub-deadline for the CDX phase. On marketplace-scale sites
    # (270 M+ captures) the server-side scan can eat the *entire* user
    # timeout before yielding the first byte — and our previous "check
    # after each row" guard never fired because the row never arrived.
    # We now cap the CDX phase at ~30 % of total timeout (≥ 30 s) and
    # enforce that cap *from outside* the request: the iteration runs in
    # a daemon thread and the main thread polls a queue with the
    # sub-deadline. If the worker is still inside ``requests.get`` when
    # the budget expires we simply stop reading from the queue and move
    # on — the thread dies with the process.
    if math.isinf(deadline):
        cdx_sub_deadline = math.inf
        cdx_budget_s = math.inf
    else:
        cdx_budget_s = max(30.0, plan.timeout_seconds * 0.3)
        cdx_sub_deadline = min(deadline, time.monotonic() + cdx_budget_s)

    if single_url is not None:
        # Single-URL mode: strip the plan's urlkey filter and limit (which
        # were computed for the whole host), and ask CDX for the exact URL.
        from dataclasses import replace as _dc_replace
        cdx_plan = _dc_replace(plan, cdx_urlkey_filter=None, cdx_limit=None)
        cdx_target = single_url
        cdx_include_subdomains = False  # → CDX matchType=exact
    else:
        cdx_plan = plan
        cdx_target = domain
        cdx_include_subdomains = include_subdomains

    cdx_snaps, cdx_truncated, cdx_err = _stream_cdx_with_budget(
        cdx_target,
        plan=cdx_plan,
        sess=sess,
        from_year=from_year,
        to_year=to_year,
        include_subdomains=cdx_include_subdomains,
        cdx_timeout=cdx_timeout,
        sub_deadline=cdx_sub_deadline,
        budget_s=cdx_budget_s,
    )
    if cdx_err is not None:
        errors.append(cdx_err)
        ui.status(f"        {cdx_err}")
        ui.status("        Falling back to probing only.")

    if cdx_truncated:
        ui.status(
            f"        CDX scan hit sub-budget at {len(cdx_snaps):,} "
            f"candidates — proceeding so fetcher gets its share of the "
            f"timeout."
        )

    if unlimited:
        cdx_snaps = _dedup_exact(cdx_snaps)
    else:
        cdx_snaps = _pick_best_per_url_year(cdx_snaps)

    cdx_total = len(cdx_snaps)
    if single_url is not None:
        # Every snapshot is the same URL — ranking by URL score is meaningless.
        # Order by timestamp so the timeline reads chronologically and ignore
        # the user's min_score (the URL was chosen on purpose).
        cdx_ranked = sorted(cdx_snaps, key=lambda s: s.timestamp)
    else:
        cdx_ranked = sorted(
            cdx_snaps,
            key=lambda s: (score_url(s.original), s.timestamp),
            reverse=True,
        )
        cdx_ranked = [s for s in cdx_ranked if score_url(s.original) >= min_score]
    cdx_top = cdx_ranked if unlimited else cdx_ranked[:capacity]

    if cdx_snaps:
        ui.status(
            f"        CDX returned {len(cdx_snaps):,} candidates, "
            f"keeping {len(cdx_top):,} by relevance."
        )

    # Bump the bar's total to reflect *all* work: every CDX fetch the consumer
    # will do (= len(cdx_top)) plus every probe call the producer will make
    # (= total_probes). Both stream into the same bar — see ``ui.advance``
    # below (consumer) and inside ``_iter_well_known`` (probe).
    ui.update(bar_task, total=(len(cdx_top) + total_probes) or None)

    # Short-circuit the well-known probe when we know it can't add anything
    # the analyst would care about. Two cases, both gated on
    # ``total_is_precise`` (the loose 50k-per-page ceiling can't tell us how
    # much we're missing):
    #
    # (A) CDX returned 0 candidates → all N captures are non-HTML or non-200.
    #     ``/wayback/available`` would just resurface those same assets
    #     (robots.txt, favicon, redirects) — no contact pages.
    #
    # (B) CDX returned ≥1 candidate AND the URL filter was off → we already
    #     ran the *broad* HTML/200 scan. Any remaining captures are
    #     non-HTML / non-200 assets, same as case (A). Probing 150 paths to
    #     surface them is wasted IA load. (When the URL filter IS on, probe
    #     stays useful — it can catch non-ASCII contact paths like
    #     ``/контакты`` that CDX's urlkey regex can't match.)
    if (
        probe_well_known
        and plan.total_is_precise
        and (plan.estimated_total_snapshots or 0) > 0
    ):
        if not cdx_top:
            ui.status(
                f"[*] All {plan.estimated_total_snapshots} archived captures "
                f"are non-HTML or non-200 — skipping well-known probe "
                f"(would take ~{(len(well_known_paths) * 5 * 2) // 60} min "
                f"and can't surface anything new)."
            )
            probe_well_known = False
            ui.update(bar_task, total=None)
        elif not plan.use_url_filter:
            ui.status(
                f"[*] Precise count: {plan.estimated_total_snapshots} captures, "
                f"CDX returned {len(cdx_top)} HTML page"
                f"{'s' if len(cdx_top) != 1 else ''} — skipping well-known "
                f"probe (the broad scan already covered every HTML page; "
                f"remaining captures are non-HTML / non-200 assets)."
            )
            probe_well_known = False
            ui.update(bar_task, total=None)

    # ----- 2) Producer-consumer with deadline -----
    snap_queue: queue.Queue = queue.Queue(maxsize=max(max_workers * 4, 16))
    SENTINEL = object()
    stop_event = threading.Event()
    found_lock = threading.Lock()
    seen_keys: set[tuple[str, str]] = set()
    timeout_was_exhausted = threading.Event()

    def _deadline_passed() -> bool:
        return not math.isinf(deadline) and time.monotonic() >= deadline

    def _capacity_reached() -> bool:
        return not unlimited and found_snaps >= capacity

    # In single-URL mode the planner's rationale (computed for the whole
    # host) is misleading — replace with one that names the actual URL,
    # and force ``url_filter_active=False`` so the report doesn't suggest
    # ``--all`` (the filter was bypassed for this scan, recommending it
    # makes no sense).
    if single_url is not None:
        _effective_rationale = (
            f"single-URL mode — {len(cdx_top)} archived snapshot"
            f"{'s' if len(cdx_top) != 1 else ''} of {single_url}"
        )
        _effective_url_filter_active = False
    else:
        _effective_rationale = plan.rationale
        _effective_url_filter_active = plan.use_url_filter

    if not probe_well_known and not cdx_top:
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=1.0)
        ui.stop_task(bar_task)
        return ScanResult(
            domain=domain, snapshots_considered=cdx_total, snapshots_fetched=0,
            sightings=[], errors=errors,
            timeout_seconds=plan.timeout_seconds,
            elapsed_seconds=time.monotonic() - started_at,
            plan_rationale=_effective_rationale,
            url_filter_active=_effective_url_filter_active,
            single_url=single_url,
        )

    def _safe_put(item) -> bool:
        """Put ``item`` onto the snap queue, polling ``stop_event`` so we
        never block forever after the consumer has exited. Returns False
        if we gave up because stop_event fired."""
        while not stop_event.is_set():
            try:
                snap_queue.put(item, timeout=0.1)
                return True
            except queue.Full:
                continue
        return False

    def _producer() -> None:
        nonlocal found_snaps, probe_total
        try:
            # Phase 1: CDX top-N
            for snap in cdx_top:
                if stop_event.is_set():
                    return
                if _deadline_passed():
                    timeout_was_exhausted.set()
                    return
                key = (snap.timestamp, snap.original)
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                with found_lock:
                    if _capacity_reached():
                        return
                    found_snaps += 1
                _set_postfix()
                if not _safe_put(snap):
                    return
            # Phase 2: well-known probes
            if probe_well_known and not stop_event.is_set() and not _deadline_passed():
                for snap in _iter_well_known(
                    domain, sess,
                    probe_timestamps=probe_timestamps,
                    ui=ui, task=bar_task,
                    deadline=deadline if not math.isinf(deadline) else None,
                    paths=well_known_paths,
                ):
                    if stop_event.is_set():
                        return
                    if _deadline_passed():
                        timeout_was_exhausted.set()
                        return
                    probe_total += 1
                    key = (snap.timestamp, snap.original)
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    with found_lock:
                        if _capacity_reached():
                            return
                        found_snaps += 1
                    _set_postfix()
                    if not _safe_put(snap):
                        return
        finally:
            # Best-effort SENTINEL — if the queue is full and stop_event is
            # set, the consumer is already gone, so this is harmless to skip.
            try:
                snap_queue.put(SENTINEL, timeout=0.5)
            except queue.Full:
                pass

    producer_thread = threading.Thread(target=_producer, name="probe-producer", daemon=True)
    producer_thread.start()

    def _snap_consumer() -> Iterable[Snapshot]:
        while True:
            item = snap_queue.get()
            if item is SENTINEL:
                return
            yield item

    fetcher_deadline = None if math.isinf(deadline) else deadline

    interrupted = False
    try:
        for page in fetch_snapshots(
            _snap_consumer(),
            session=sess,
            max_workers=max_workers,
            rate_limit_per_sec=rate_limit_per_sec,
            deadline=fetcher_deadline,
            cache=cache,
        ):
            if page.error:
                errors.append(
                    f"{page.snapshot.original} @ {page.snapshot.timestamp}: {page.error}"
                )
                ui.advance(bar_task)
                _set_postfix()
                continue
            fetched_count += 1
            _absorb_contacts(
                page,
                default_phone_regions=default_phone_regions,
                sightings=sightings,
                announce=announce,
            )
            ui.advance(bar_task)
            _set_postfix()
    except KeyboardInterrupt:
        # Ctrl+C — preserve whatever ``sightings`` we accumulated so far.
        # The CLI catches ``result.interrupted`` and writes the partial CSV
        # before exiting. Threads are stopped via the finally block.
        interrupted = True
    finally:
        stop_event.set()
        heartbeat_stop.set()
        # A second Ctrl+C during cleanup must not leak a stack trace. The
        # threads are daemons, so even if join() times out they die with
        # the process.
        try:
            producer_thread.join(timeout=2.0)
        except KeyboardInterrupt:
            pass
        try:
            heartbeat_thread.join(timeout=1.0)
        except KeyboardInterrupt:
            pass
        ui.stop_task(bar_task)

    if _deadline_passed():
        timeout_was_exhausted.set()

    elapsed = time.monotonic() - started_at

    return ScanResult(
        domain=domain,
        # "Considered" = total candidates we knew about before capping. The
        # analyst wants to see how big the haystack was, not just what we
        # picked. ``fetched_count`` reflects what was actually extracted.
        snapshots_considered=cdx_total + probe_total,
        snapshots_fetched=fetched_count,
        sightings=sightings,
        errors=errors,
        timeout_exhausted=timeout_was_exhausted.is_set(),
        timeout_seconds=plan.timeout_seconds,
        elapsed_seconds=elapsed,
        plan_rationale=_effective_rationale,
        url_filter_active=_effective_url_filter_active,
        interrupted=interrupted,
        single_url=single_url,
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def _resolve_legacy_mode(mode: str) -> tuple[float, bool]:
    """Map a legacy ``--mode`` name onto (timeout_seconds, force_all)."""
    if mode not in _MODE_TO_TIMEOUT:
        raise ValueError(f"mode must be one of {SCAN_MODES}, got {mode!r}")
    return _MODE_TO_TIMEOUT[mode]


def scan_domain(
    domain: str,
    *,
    # Primary timeout-first controls
    timeout_seconds: float | None = None,
    force_all: bool = False,
    no_escalate: bool = False,
    calibration: Calibration | None = None,
    # Legacy aliases (translate to timeout+force_all)
    mode: str | None = None,
    # Snapshot count controls
    max_snapshots: int | None = None,
    # Scope
    from_year: int | None = None,
    to_year: int | None = None,
    include_subdomains: bool = True,
    probe_well_known: bool = True,
    min_score: int = 0,
    # Performance
    session: requests.Session | None = None,
    max_workers: int = 4,
    rate_limit_per_sec: float = 4.0,
    cdx_timeout: int = 300,
    # Locale
    default_phone_regions: Iterable[str] = ("RU", "BY", "UA", "KZ", "US", "GB", "DE", "FR"),
    # Probe customization (typically populated from --targets-file URL paths)
    extra_well_known_paths: Iterable[str] = (),
    # Single-URL mode (--single-url): scan only the captures of this exact
    # URL via CDX matchType=exact. Disables probe, urlkey filter, and
    # min_score gating — analyst already knows the page they care about.
    single_url: str | None = None,
    # Cache
    cache: "SnapshotCache | None" = None,
    # UI
    progress: bool = False,
    verbose: bool = False,
) -> ScanResult:
    """Scan ``domain`` for historical contacts under a wall-clock timeout.

    Parameters
    ----------
    timeout_seconds:
        Wall-clock timeout in seconds. ``0`` (or ``None`` combined with the
        ``exhaustive`` alias) means unlimited. Default ``300``.
    force_all:
        Disable the contact-URL CDX filter — scan every URL the site has.
        Independent of timeout.
    no_escalate:
        Disable one-shot zero-result escalation.
    calibration:
        Optional pre-computed :class:`Calibration`. When ``None``, a
        conservative default latency is used (the CLI runs/loads real
        calibration before calling here).
    mode:
        Legacy alias: maps to timeout presets.

        - ``"auto"``/``"default"`` → ``timeout_seconds=300``
        - ``"deep"`` → ``timeout_seconds=900``
        - ``"exhaustive"`` → ``timeout_seconds=0, force_all=True``
    max_snapshots:
        Optional hard ceiling on top of the timeout-derived capacity. ``None``
        means "let the timeout decide".
    """
    if mode is not None:
        legacy_timeout, legacy_all = _resolve_legacy_mode(mode)
        if timeout_seconds is None:
            timeout_seconds = legacy_timeout
        # --all is sticky: a mode can turn it on, but the user can also force it.
        force_all = force_all or legacy_all
    if timeout_seconds is None:
        timeout_seconds = 300.0

    sess = session or requests.Session()
    sess.headers.setdefault("User-Agent", DEFAULT_USER_AGENT)

    default_phone_regions = _regions_for_domain(domain, default_phone_regions)

    avg_latency = (
        calibration.avg_latency_s if calibration is not None else DEFAULT_AVG_LATENCY_S
    )

    with ProgressUI(enabled=progress, verbose=verbose) as ui:
        plan = make_plan(
            domain,
            timeout_seconds=float(timeout_seconds),
            workers=max_workers,
            rate_limit_per_sec=rate_limit_per_sec,
            force_all=force_all,
            user_max_snapshots=max_snapshots,
            avg_latency_s=avg_latency,
            session=sess,
            ui=ui,
        )

        # In single-URL mode the planner's status line is host-wide info
        # ("contact-URL filter on; top N of ~M snapshots") that doesn't
        # apply to what we're actually about to do. Spell out the real
        # strategy on the very next line so the analyst isn't confused.
        if single_url is not None:
            ui.status(
                f"[*] Single-URL mode: will fetch every archived snapshot "
                f"of {single_url} (urlkey filter and well-known probe "
                f"disabled)."
            )

        # Fast exit: preflight already confirmed the IA has zero captures for
        # this domain. Running CDX + 145 well-known probes here would just
        # burn 300s for guaranteed-nothing. Skip in single-URL mode — the
        # ``no_captures`` flag is about the whole host, not the URL we're
        # actually asking for.
        if plan.no_captures and single_url is None:
            ui.status(
                f"[*] No snapshots available in the Internet Archive for {domain}."
            )
            return ScanResult(
                domain=domain,
                snapshots_considered=0,
                snapshots_fetched=0,
                sightings=[],
                errors=[],
                resolved_mode=_back_compat_mode_name(plan, mode),
                timeout_seconds=plan.timeout_seconds,
                elapsed_seconds=0.0,
                plan_rationale=plan.rationale,
                url_filter_active=plan.use_url_filter,
            )

        # Build the effective probe path list — defaults plus any caller-
        # supplied extras (e.g. URL paths from --targets-file), deduped while
        # preserving the defaults' order so familiar paths probe first.
        merged_paths = list(WELL_KNOWN_PATHS)
        seen_paths = set(WELL_KNOWN_PATHS)
        for p in extra_well_known_paths:
            if p and p not in seen_paths:
                seen_paths.add(p)
                merged_paths.append(p)

        # Single-URL mode disables the well-known probe — we already know
        # the exact URL the analyst wants. Probe paths and the urlkey
        # filter on CDX would all be wasted work.
        if single_url is not None:
            probe_well_known = False

        common = dict(
            domain=domain,
            sess=sess,
            from_year=from_year,
            to_year=to_year,
            include_subdomains=include_subdomains,
            probe_well_known=probe_well_known,
            min_score=min_score,
            max_workers=max_workers,
            rate_limit_per_sec=rate_limit_per_sec,
            default_phone_regions=default_phone_regions,
            ui=ui,
            cdx_timeout=cdx_timeout,
            single_url=single_url,
            well_known_paths=tuple(merged_paths),
            cache=cache,
        )

        result = _scan(plan, **common)
        result.resolved_mode = _back_compat_mode_name(plan, mode)

        # Zero-result escalation. Trigger: nothing found and CDX didn't break.
        # Two-step ladder (one shot only):
        #   1. If the URL filter was on → retry with filter off (same timeout).
        #   2. Else, if we have a finite timeout AND it was exhausted → extend it.
        #   3. Else (filter off, timeout unlimited or unspent) → give up.
        cdx_broken = any("CDX query failed" in e for e in result.errors)
        if (
            not no_escalate
            and not result.sightings
            and not cdx_broken
            and not result.interrupted  # don't escalate after Ctrl+C
        ):
            if plan.use_url_filter:
                ui.status(
                    f"[*] No contacts in first pass — broadening URL filter "
                    f"for {domain}…"
                )
                next_plan = broaden_plan(
                    plan,
                    new_deadline=time.monotonic() + timeout_seconds
                    if timeout_seconds > 0 else math.inf,
                )
                result = _scan(next_plan, **common)
                result.resolved_mode = _back_compat_mode_name(next_plan, mode)
            elif timeout_seconds > 0 and result.timeout_exhausted:
                ui.status(
                    f"[*] No contacts in first pass — extending timeout to "
                    f"{2 * timeout_seconds:.0f}s for {domain}…"
                )
                next_plan = extend_plan(plan, extra_seconds=timeout_seconds)
                result = _scan(next_plan, **common)
                result.resolved_mode = _back_compat_mode_name(next_plan, mode)

    return result


def _back_compat_mode_name(plan: ScanPlan, original_mode: str | None) -> str:
    """Synthesize a legacy ``resolved_mode`` string for older renderers/tests.

    Preference order:
    1. The user's original ``mode=`` keyword (preserves "auto" → "exhaustive"
       semantics callers may inspect).
    2. Derived from plan flags.
    """
    if original_mode is not None and original_mode != "auto":
        return original_mode
    if plan.timeout_seconds == 0:
        return "exhaustive"
    if plan.use_url_filter and plan.timeout_seconds >= 900:
        return "deep"
    if plan.use_url_filter:
        return "default"
    return "all"
