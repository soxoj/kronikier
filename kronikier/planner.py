"""Pre-flight scan planner.

Picks a concrete scan strategy from the user's wall-clock timeout, the
calibrated average per-snapshot latency, and a cheap CDX size probe.
Replaces the old mode-name routing (``--auto/--default/--deep/--exhaustive``).

The planner only computes numbers — no HTTP I/O of its own beyond the single
``show_num_pages`` meta-call. All side effects (status prints) flow through
the :class:`~kronikier.progress_ui.ProgressUI` passed in.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

import requests

from kronikier.cdx import count_captures, show_num_pages
from kronikier.classifier import CDX_URLKEY_FILTER
from kronikier.progress_ui import ProgressUI

#: Empirical CDX convention — each "index page" returned by ``showNumPages``
#: corresponds to roughly this many records. Used as a *worst-case ceiling*
#: when we can't afford a precise count.
SNAPS_PER_PAGE = 50_000

#: When ``show_num_pages`` reports ≤ this many CDX pages, the worst-case
#: ``pages × 50 000`` ceiling is too loose to plan against — a 1-page site
#: can hold anywhere from a few to 50 k snapshots. We splurge on one extra
#: cheap CDX call (``fl=urlkey``) to learn the true count, which often
#: flips the plan from "filter on" to "scan everything".
PRECISE_COUNT_MAX_PAGES = 2

#: Floor for the ranked-pool size we ask CDX for in the filtered branch.
#: We overshoot capacity by 5× so the ranker has options without bloating the
#: server-side scan time on huge sites (the previous 20× factor turned the
#: marketplace-scale case — 270 M+ captures — into a multi-minute CDX call
#: that ate the whole user timeout before yielding a single row).
_CDX_LIMIT_OVERSAMPLE = 5
_CDX_LIMIT_FLOOR = 2_000


@dataclass(frozen=True)
class ScanPlan:
    """Concrete plan computed for one scan invocation.

    Carries the deadline (monotonic seconds) — *not* a relative timeout — so
    downstream code never has to decide "when did the clock start".
    """

    deadline_monotonic: float
    timeout_seconds: float           # 0.0 means unlimited
    avg_latency_s: float
    effective_concurrency: int
    capacity: int                    # snapshots expected to fit in timeout
    cdx_num_pages: int | None
    estimated_total_snapshots: int | None
    #: ``True`` when ``estimated_total_snapshots`` came from
    #: :func:`count_captures` (exact); ``False`` when it's the loose
    #: ``pages × 50 000`` ceiling.
    total_is_precise: bool
    #: ``True`` when the precise-count preflight confirmed **zero** captures
    #: for this domain. Tells :func:`scan_domain` to skip the CDX query and
    #: well-known probe entirely and report "no snapshots" immediately.
    no_captures: bool
    use_url_filter: bool
    cdx_urlkey_filter: str | None    # the regex string when filter is on
    cdx_limit: int | None
    user_forced_all: bool
    rationale: str

    @property
    def unlimited(self) -> bool:
        return math.isinf(self.deadline_monotonic) or self.timeout_seconds == 0


def _effective_concurrency(workers: int, rate_limit_per_sec: float) -> int:
    """Real-world concurrency ceiling: workers, clamped by the rate limit.

    With ``workers=4`` and ``rate=4/s`` the actual ceiling is 4 req/sec, not
    16 — extra workers just sleep in the throttle. Capacity would be wildly
    inflated without this clamp.
    """
    if rate_limit_per_sec is None or rate_limit_per_sec <= 0:
        return max(1, workers)
    return min(max(1, workers), max(1, int(rate_limit_per_sec)))


def make_plan(
    domain: str,
    *,
    timeout_seconds: float,
    workers: int,
    rate_limit_per_sec: float,
    force_all: bool,
    user_max_snapshots: int | None,
    avg_latency_s: float,
    session: requests.Session,
    ui: ProgressUI | None = None,
) -> ScanPlan:
    """Compute a :class:`ScanPlan` for ``domain``.

    Parameters
    ----------
    timeout_seconds:
        Wall-clock timeout for the scan. ``0`` means unlimited (used by the
        ``--exhaustive`` alias and ``--timeout 0``).
    workers:
        ``--workers`` value from the CLI.
    rate_limit_per_sec:
        ``--rate`` value from the CLI; clamps effective concurrency.
    force_all:
        ``--all`` was passed — never apply the contact-URL filter.
    user_max_snapshots:
        Optional hard ceiling layered on top of the timeout-derived capacity.
        ``None`` means "let the timeout decide".
    avg_latency_s:
        Calibration value (sec/snapshot). Drives the capacity formula.
    session:
        Reused for the single ``show_num_pages`` meta-call.
    ui:
        Optional progress UI for the one-line plan status.
    """
    eff_conc = _effective_concurrency(workers, rate_limit_per_sec)

    # ----- Preflight: size the domain BEFORE the timeout clock starts -----
    # show_num_pages and (optionally) count_captures are HTTP calls; if we
    # set the deadline before them, a multi-second precise count would eat
    # into the user's `--timeout`. Deadline computation is therefore deferred
    # to after the preflight block below.
    pages = show_num_pages(domain, session=session)

    estimated_total: int | None
    total_is_precise = False
    no_captures = False
    if pages is None:
        estimated_total = None
    elif pages <= PRECISE_COUNT_MAX_PAGES:
        if ui is not None:
            ui.status(
                f"[*] Small site detected ({pages} CDX page"
                f"{'s' if pages != 1 else ''}) — counting exact captures…"
            )
        exact = count_captures(domain, session=session)
        if exact is None:
            estimated_total = pages * SNAPS_PER_PAGE
        else:
            estimated_total = exact
            total_is_precise = True
            if exact == 0:
                no_captures = True
    else:
        estimated_total = pages * SNAPS_PER_PAGE

    # ----- Now anchor the deadline (preflight done) -----
    if timeout_seconds <= 0:
        capacity = user_max_snapshots if user_max_snapshots is not None else 10**9
        deadline = math.inf
    else:
        raw_capacity = max(1, int(timeout_seconds * eff_conc / max(avg_latency_s, 1e-3)))
        capacity = (
            min(raw_capacity, user_max_snapshots)
            if user_max_snapshots is not None
            else raw_capacity
        )
        deadline = time.monotonic() + timeout_seconds

    # Format helper: "1,234" for exact counts, "~1,234" for the loose
    # 50K-per-page ceiling — so the analyst can tell at a glance.
    def _fmt_total(n: int) -> str:
        return f"{n:,}" if total_is_precise else f"~{n:,}"

    if force_all:
        rationale = (
            f"--all forced: broad CDX scan (all HTML pages, no URL filter) "
            f"up to {capacity:,} snapshots"
        )
        plan = ScanPlan(
            deadline_monotonic=deadline,
            timeout_seconds=float(timeout_seconds),
            avg_latency_s=avg_latency_s,
            effective_concurrency=eff_conc,
            capacity=capacity,
            cdx_num_pages=pages,
            estimated_total_snapshots=estimated_total,
            total_is_precise=total_is_precise,
            no_captures=no_captures,
            use_url_filter=False,
            cdx_urlkey_filter=None,
            cdx_limit=None,
            user_forced_all=True,
            rationale=rationale,
        )
    elif estimated_total is not None and estimated_total <= capacity:
        # The whole site fits in timeout — no need to filter.
        if no_captures:
            rationale = "Internet Archive has no captures for this domain"
        else:
            # Naming note: we drop the contact-URL filter, but CDX still
            # restricts to ``mimetype:text/html`` and ``statuscode:200``.
            # Saying "every URL" misled users into thinking we'd fetch
            # robots.txt / favicons / redirects — we don't.
            rationale = (
                f"site fits timeout ({_fmt_total(estimated_total)} ≤ "
                f"{capacity:,}) — broad CDX scan (all HTML pages, no URL filter)"
            )
        plan = ScanPlan(
            deadline_monotonic=deadline,
            timeout_seconds=float(timeout_seconds),
            avg_latency_s=avg_latency_s,
            effective_concurrency=eff_conc,
            capacity=capacity,
            cdx_num_pages=pages,
            estimated_total_snapshots=estimated_total,
            total_is_precise=total_is_precise,
            no_captures=no_captures,
            use_url_filter=False,
            cdx_urlkey_filter=None,
            cdx_limit=None,
            user_forced_all=False,
            rationale=rationale,
        )
    else:
        # Big or unknown-sized site: keep the contact-URL filter, oversample
        # by 20× for the ranker.
        cdx_limit = max(capacity * _CDX_LIMIT_OVERSAMPLE, _CDX_LIMIT_FLOOR)
        if estimated_total:
            rationale = (
                f"contact-URL filter on; will fetch top {capacity:,} "
                f"of {_fmt_total(estimated_total)} snapshots"
            )
        else:
            rationale = (
                f"contact-URL filter on; will fetch top {capacity:,} snapshots"
            )
        plan = ScanPlan(
            deadline_monotonic=deadline,
            timeout_seconds=float(timeout_seconds),
            avg_latency_s=avg_latency_s,
            effective_concurrency=eff_conc,
            capacity=capacity,
            cdx_num_pages=pages,
            estimated_total_snapshots=estimated_total,
            total_is_precise=total_is_precise,
            no_captures=no_captures,
            use_url_filter=True,
            cdx_urlkey_filter=CDX_URLKEY_FILTER,
            cdx_limit=cdx_limit,
            user_forced_all=False,
            rationale=rationale,
        )

    # Suppress the "Plan:" status line for the no-captures case — scan_domain
    # prints a dedicated "No snapshots available…" message instead, and the
    # planner's capacity numbers would be misleading (0 snapshots ≤ 295 etc).
    if ui is not None and not plan.no_captures:
        timeout_part = (
            f"timeout {timeout_seconds:.0f}s" if timeout_seconds > 0 else "no timeout"
        )
        ui.status(
            f"[*] Plan: {plan.rationale} | {timeout_part}, "
            f"avg latency {avg_latency_s:.2f}s/snapshot"
        )

    return plan


def broaden_plan(plan: ScanPlan, *, new_deadline: float | None = None) -> ScanPlan:
    """Return a copy of ``plan`` with the URL filter disabled.

    Used by zero-result escalation: if the first scan found nothing with the
    filter on, retry without it (same or extended deadline).
    """
    # Keep a bounded ``cdx_limit`` even when broadening — passing ``None``
    # turns an escalation on a huge site (e.g. a marketplace) into an
    # unbounded CDX scan of millions of rows that always blows the timeout.
    broadened_limit = max(plan.capacity * _CDX_LIMIT_OVERSAMPLE, _CDX_LIMIT_FLOOR)
    return ScanPlan(
        deadline_monotonic=new_deadline if new_deadline is not None else plan.deadline_monotonic,
        timeout_seconds=plan.timeout_seconds,
        avg_latency_s=plan.avg_latency_s,
        effective_concurrency=plan.effective_concurrency,
        capacity=plan.capacity,
        cdx_num_pages=plan.cdx_num_pages,
        estimated_total_snapshots=plan.estimated_total_snapshots,
        total_is_precise=plan.total_is_precise,
        no_captures=plan.no_captures,
        use_url_filter=False,
        cdx_urlkey_filter=None,
        cdx_limit=broadened_limit,
        user_forced_all=plan.user_forced_all,
        rationale="broadened: URL filter dropped after zero-result first pass",
    )


def extend_plan(plan: ScanPlan, *, extra_seconds: float) -> ScanPlan:
    """Return a copy of ``plan`` with the deadline pushed out by ``extra_seconds``.

    Used by zero-result escalation when the filter is already off — we double
    the timeout for one more try.
    """
    if math.isinf(plan.deadline_monotonic) or plan.timeout_seconds <= 0:
        # Already unlimited; nothing to extend.
        return plan
    return ScanPlan(
        deadline_monotonic=time.monotonic() + extra_seconds,
        timeout_seconds=plan.timeout_seconds + extra_seconds,
        avg_latency_s=plan.avg_latency_s,
        effective_concurrency=plan.effective_concurrency,
        capacity=max(
            plan.capacity,
            int(extra_seconds * plan.effective_concurrency / max(plan.avg_latency_s, 1e-3)),
        ),
        cdx_num_pages=plan.cdx_num_pages,
        estimated_total_snapshots=plan.estimated_total_snapshots,
        total_is_precise=plan.total_is_precise,
        no_captures=plan.no_captures,
        use_url_filter=plan.use_url_filter,
        cdx_urlkey_filter=plan.cdx_urlkey_filter,
        cdx_limit=plan.cdx_limit,
        user_forced_all=plan.user_forced_all,
        rationale=f"extended: timeout +{extra_seconds:.0f}s after zero-result pass",
    )
