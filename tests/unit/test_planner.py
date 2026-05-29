"""Unit tests for the scan planner.

Pure-logic tests — we mock ``show_num_pages`` to control the sizing signal
and never make real HTTP calls.
"""

from __future__ import annotations

import math

import pytest
import requests

from kronieker import planner as planner_mod
from kronieker.classifier import CDX_URLKEY_FILTER
from kronieker.planner import (
    SNAPS_PER_PAGE,
    ScanPlan,
    _effective_concurrency,
    broaden_plan,
    extend_plan,
    make_plan,
)


@pytest.fixture
def sess():
    return requests.Session()


def _make_plan(monkeypatch, *, num_pages, exact_captures=None, **kwargs):
    """Helper: mock show_num_pages + count_captures and call make_plan.

    ``exact_captures=None`` (default) makes ``count_captures`` return
    ``None`` so the planner falls back to the ``pages × 50_000`` estimate
    — preserving the pre-precise-count test expectations. Pass an int to
    exercise the new precise-count branch.
    """
    monkeypatch.setattr(planner_mod, "show_num_pages", lambda *a, **kw: num_pages)
    monkeypatch.setattr(planner_mod, "count_captures", lambda *a, **kw: exact_captures)
    defaults = dict(
        timeout_seconds=120.0,
        workers=4,
        rate_limit_per_sec=4.0,
        force_all=False,
        user_max_snapshots=None,
        avg_latency_s=0.5,
        session=requests.Session(),
        ui=None,
    )
    defaults.update(kwargs)
    return make_plan("example.com", **defaults)


# ---------------------------------------------------------------------------
# Capacity formula
# ---------------------------------------------------------------------------


def test_capacity_formula_basic(monkeypatch):
    """timeout=120, conc=clamp(min(workers=4, rate=4))=4, avg=0.5 → 960."""
    plan = _make_plan(monkeypatch, num_pages=10_000)  # huge site, filter on
    assert plan.capacity == 960
    assert plan.effective_concurrency == 4
    assert plan.avg_latency_s == 0.5


def test_capacity_respects_user_max_snapshots(monkeypatch):
    plan = _make_plan(monkeypatch, num_pages=10_000, user_max_snapshots=100)
    assert plan.capacity == 100


def test_capacity_clamps_concurrency_to_rate_limit(monkeypatch):
    """workers=8 but rate=2/sec → effective concurrency = 2, not 8."""
    plan = _make_plan(
        monkeypatch, num_pages=10_000, workers=8, rate_limit_per_sec=2.0
    )
    assert plan.effective_concurrency == 2
    assert plan.capacity == int(120 * 2 / 0.5)  # 480


def test_effective_concurrency_no_rate_limit_uses_workers():
    """When rate_limit_per_sec=0 (no throttle), workers is the real ceiling."""
    assert _effective_concurrency(workers=8, rate_limit_per_sec=0) == 8
    assert _effective_concurrency(workers=8, rate_limit_per_sec=-1) == 8


def test_effective_concurrency_clamps_to_at_least_one():
    assert _effective_concurrency(workers=0, rate_limit_per_sec=0.5) == 1


# ---------------------------------------------------------------------------
# URL-filter decision branches
# ---------------------------------------------------------------------------


def test_plan_drops_filter_when_estimated_total_fits_timeout(monkeypatch):
    """num_pages × SNAPS_PER_PAGE ≤ capacity → filter off, broad scan."""
    # capacity at default settings = 960. We need estimated_total ≤ 960.
    # SNAPS_PER_PAGE=50000, so num_pages=0 (=> estimated_total=None) wouldn't
    # trigger; we want num_pages such that pages*SNAPS_PER_PAGE ≤ 960.
    # That requires num_pages=0 — but 0 means "unknown" in our convention.
    # Instead, use a tiny capacity by shrinking timeout.
    plan = _make_plan(
        monkeypatch, num_pages=1,                # 1 * 50_000 = 50_000 estimated
        timeout_seconds=600.0, workers=100, rate_limit_per_sec=200.0,
        avg_latency_s=0.5,
    )
    # capacity = 600 * min(100, 200) / 0.5 = 120_000 > 50_000 → fits
    assert plan.use_url_filter is False
    assert plan.cdx_urlkey_filter is None
    assert plan.cdx_limit is None
    assert "broad CDX scan" in plan.rationale


def test_plan_keeps_filter_when_site_too_big(monkeypatch):
    plan = _make_plan(monkeypatch, num_pages=350)  # 350 * 50_000 = 17.5M
    assert plan.use_url_filter is True
    assert plan.cdx_urlkey_filter == CDX_URLKEY_FILTER
    # cdx_limit = max(capacity * 5, 2000); capacity=960 → 4800
    assert plan.cdx_limit == 4_800


def test_plan_force_all_drops_filter_regardless_of_size(monkeypatch):
    plan = _make_plan(monkeypatch, num_pages=10_000, force_all=True)
    assert plan.use_url_filter is False
    assert plan.user_forced_all is True
    assert plan.cdx_limit is None
    assert "--all forced" in plan.rationale


def test_plan_when_show_num_pages_returns_none(monkeypatch):
    """If the sizing probe fails, fall back to filtered scan (the safe choice)."""
    plan = _make_plan(monkeypatch, num_pages=None)
    assert plan.cdx_num_pages is None
    assert plan.estimated_total_snapshots is None
    assert plan.use_url_filter is True  # default to filter on when unknown
    assert plan.cdx_limit is not None


def test_plan_cdx_limit_floor_is_2000(monkeypatch):
    """Tiny capacity must still ask CDX for at least the floor number of
    candidates so the ranker has options to choose from.
    """
    plan = _make_plan(
        monkeypatch, num_pages=10_000, timeout_seconds=1.0, avg_latency_s=10.0
    )
    # capacity = 1 * 4 / 10 → max(1, 0) = 1. Limit = max(1*5, 2000) = 2000.
    assert plan.cdx_limit == 2_000


# ---------------------------------------------------------------------------
# Unlimited / timeout=0 semantics
# ---------------------------------------------------------------------------


def test_plan_timeout_zero_means_unlimited(monkeypatch):
    plan = _make_plan(monkeypatch, num_pages=10_000, timeout_seconds=0)
    assert plan.unlimited is True
    assert math.isinf(plan.deadline_monotonic)
    # capacity falls back to a very large number (unbounded) since user didn't cap
    assert plan.capacity == 10**9


def test_plan_timeout_zero_respects_max_snapshots_when_set(monkeypatch):
    plan = _make_plan(
        monkeypatch, num_pages=10_000, timeout_seconds=0, user_max_snapshots=100
    )
    assert plan.unlimited is True
    assert plan.capacity == 100


# ---------------------------------------------------------------------------
# Plan mutations: broaden / extend (used by escalation)
# ---------------------------------------------------------------------------


def test_broaden_plan_disables_filter_but_keeps_bounded_limit():
    """Broadening drops the urlkey filter, but the CDX call must stay
    bounded — passing limit=None on a marketplace-scale site would re-trigger
    the multi-minute server scan and exhaust the timeout on its own.
    """
    plan = ScanPlan(
        deadline_monotonic=100.0, timeout_seconds=120.0, avg_latency_s=0.5,
        effective_concurrency=4, capacity=960, cdx_num_pages=350,
        estimated_total_snapshots=17_500_000, total_is_precise=False, no_captures=False, use_url_filter=True,
        cdx_urlkey_filter=CDX_URLKEY_FILTER, cdx_limit=4_800,
        user_forced_all=False, rationale="orig",
    )
    broader = broaden_plan(plan)
    assert broader.use_url_filter is False
    assert broader.cdx_urlkey_filter is None
    # capacity=960 → max(960*5, 2000) = 4800.
    assert broader.cdx_limit == 4_800
    assert broader.deadline_monotonic == plan.deadline_monotonic  # unchanged
    assert "broadened" in broader.rationale


def test_broaden_plan_with_new_deadline_uses_it():
    plan = ScanPlan(
        deadline_monotonic=100.0, timeout_seconds=120.0, avg_latency_s=0.5,
        effective_concurrency=4, capacity=960, cdx_num_pages=None,
        estimated_total_snapshots=None, total_is_precise=False, no_captures=False, use_url_filter=True,
        cdx_urlkey_filter=CDX_URLKEY_FILTER, cdx_limit=5_000,
        user_forced_all=False, rationale="orig",
    )
    broader = broaden_plan(plan, new_deadline=200.0)
    assert broader.deadline_monotonic == 200.0


def test_extend_plan_pushes_deadline():
    plan = ScanPlan(
        deadline_monotonic=100.0, timeout_seconds=120.0, avg_latency_s=0.5,
        effective_concurrency=4, capacity=960, cdx_num_pages=None,
        estimated_total_snapshots=None, total_is_precise=False, no_captures=False, use_url_filter=False,
        cdx_urlkey_filter=None, cdx_limit=None,
        user_forced_all=False, rationale="orig",
    )
    extended = extend_plan(plan, extra_seconds=60.0)
    assert extended.timeout_seconds == 180.0
    assert extended.capacity >= plan.capacity  # may bump if extra > previous
    assert "extended" in extended.rationale


def test_extend_plan_is_noop_for_unlimited_timeouts():
    plan = ScanPlan(
        deadline_monotonic=math.inf, timeout_seconds=0.0, avg_latency_s=0.5,
        effective_concurrency=4, capacity=10**9, cdx_num_pages=None,
        estimated_total_snapshots=None, total_is_precise=False, no_captures=False, use_url_filter=False,
        cdx_urlkey_filter=None, cdx_limit=None,
        user_forced_all=True, rationale="orig",
    )
    assert extend_plan(plan, extra_seconds=60.0) is plan


# ---------------------------------------------------------------------------
# Estimated total uses SNAPS_PER_PAGE
# ---------------------------------------------------------------------------


def test_estimated_total_uses_snaps_per_page_constant(monkeypatch):
    plan = _make_plan(monkeypatch, num_pages=7)
    assert plan.estimated_total_snapshots == 7 * SNAPS_PER_PAGE
    assert plan.total_is_precise is False


# ---------------------------------------------------------------------------
# Precise capture-count for small sites
# ---------------------------------------------------------------------------


def test_precise_count_used_when_pages_le_threshold(monkeypatch):
    """pages=1 → count_captures is called and its exact value lands in the plan."""
    plan = _make_plan(monkeypatch, num_pages=1, exact_captures=80)
    assert plan.estimated_total_snapshots == 80
    assert plan.total_is_precise is True


def test_precise_count_flips_plan_to_unfiltered_for_small_site(monkeypatch):
    """80 actual captures ≤ capacity=960 → filter dropped, scan everything.

    With the old loose ceiling (pages=1 → 50_000 snapshots) the planner would
    have stayed filtered. This is exactly the yookasa-style regression we
    introduced PRECISE_COUNT_MAX_PAGES to fix.
    """
    plan = _make_plan(monkeypatch, num_pages=1, exact_captures=80)
    assert plan.use_url_filter is False
    assert plan.cdx_urlkey_filter is None
    assert plan.cdx_limit is None
    assert "80 ≤" in plan.rationale  # no "~", exact number


def test_precise_count_skipped_when_pages_above_threshold(monkeypatch):
    """pages=3 > PRECISE_COUNT_MAX_PAGES=2 → no precise call, loose estimate."""
    count_called = {"n": 0}

    def fake_count(*a, **kw):
        count_called["n"] += 1
        return 0  # would be wrong if used

    monkeypatch.setattr(planner_mod, "count_captures", fake_count)
    monkeypatch.setattr(planner_mod, "show_num_pages", lambda *a, **kw: 3)

    plan = make_plan(
        "big.example",
        timeout_seconds=120.0, workers=4, rate_limit_per_sec=4.0,
        force_all=False, user_max_snapshots=None, avg_latency_s=0.5,
        session=requests.Session(), ui=None,
    )
    assert count_called["n"] == 0, "precise count should not run on big sites"
    assert plan.estimated_total_snapshots == 3 * SNAPS_PER_PAGE
    assert plan.total_is_precise is False


def test_precise_count_falls_back_to_estimate_on_error(monkeypatch):
    """If count_captures returns None (HTTP error), fall back to pages×50K
    and keep ``total_is_precise=False``."""
    plan = _make_plan(monkeypatch, num_pages=1, exact_captures=None)
    assert plan.estimated_total_snapshots == 1 * SNAPS_PER_PAGE
    assert plan.total_is_precise is False
    # Rationale carries the "~" prefix when imprecise.
    if plan.use_url_filter:
        assert "~50,000" in plan.rationale
    else:
        assert "~50,000" in plan.rationale


def test_preflight_does_not_eat_into_timeout(monkeypatch):
    """Deadline is anchored AFTER show_num_pages / count_captures.

    If we set deadline first and then the count call took N seconds, the
    user's timeout budget would silently shrink by N. We avoid that by
    anchoring the monotonic clock once preflight is done.
    """
    import time as _time

    # Pretend show_num_pages / count_captures together took 3 seconds.
    real_monotonic = _time.monotonic

    def slow_count(*a, **kw):
        # Advance the fake clock by 3 s by sleeping in real time? No — that
        # would actually take 3 s. Instead, patch monotonic to step forward.
        return 80

    monkeypatch.setattr(planner_mod, "count_captures", slow_count)
    monkeypatch.setattr(planner_mod, "show_num_pages", lambda *a, **kw: 1)

    fake_clock = [real_monotonic()]

    def fake_monotonic():
        return fake_clock[0]

    monkeypatch.setattr(planner_mod.time, "monotonic", fake_monotonic)

    # Simulate 3-second preflight by advancing the clock between the two
    # monotonic() reads the planner does (one inside, one for deadline).
    original_show_num_pages = planner_mod.show_num_pages

    def advancing_show_num_pages(*a, **kw):
        fake_clock[0] += 3.0
        return 1
    monkeypatch.setattr(planner_mod, "show_num_pages", advancing_show_num_pages)

    plan = make_plan(
        "small.example",
        timeout_seconds=120.0, workers=4, rate_limit_per_sec=4.0,
        force_all=False, user_max_snapshots=None, avg_latency_s=0.5,
        session=requests.Session(), ui=None,
    )

    # Deadline should be anchored at *current* clock + 120s, NOT
    # original_clock + 120s. So deadline - now ≈ 120s, not 117s.
    remaining = plan.deadline_monotonic - fake_clock[0]
    assert abs(remaining - 120.0) < 0.5, (
        f"timeout budget eroded by preflight — remaining {remaining:.2f}s "
        f"instead of 120s"
    )
