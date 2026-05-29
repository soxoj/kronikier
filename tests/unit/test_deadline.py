"""Tests for timeout/deadline enforcement in the scan pipeline.

We deliberately avoid ``responses`` here — its global lock serialises mocked
HTTP, which would also serialise the producer and the fetcher and defeat the
purpose of testing deadline cutoffs. Instead we monkeypatch the real
functions the pipeline calls (``closest_snapshot``, ``_fetch_one``,
``show_num_pages``, ``query_domain``) so the threading actually runs.
"""

from __future__ import annotations

import time

from kronieker import pipeline as pipeline_mod
from kronieker.calibration import Calibration, CALIBRATION_VERSION
from kronieker.cdx import Snapshot
from kronieker.fetcher import FetchedPage


def _calibration(avg: float = 0.001) -> Calibration:
    """Default avg=1ms so capacity is huge — deadline becomes the real cutoff."""
    return Calibration(
        version=CALIBRATION_VERSION, avg_latency_s=avg, sample_count=8,
        last_calibrated_at="2026-01-01T00:00:00+00:00",
        samples_p50=avg, samples_p95=avg, user_agent="test",
    )


def _fast_snapshot(i: int) -> Snapshot:
    return Snapshot(
        timestamp=f"2020010100000{i % 10}",
        original=f"http://big.example/contact-{i}",
        mimetype="text/html",
        status="200",
        urlkey=f"com,big)/contact-{i}",
    )


def test_scan_stops_producing_when_deadline_passed(monkeypatch):
    """A short timeout + slow fetches → fetched count must stay well under
    the candidate count because the deadline cuts off producer dispatch."""
    candidates = [_fast_snapshot(i) for i in range(200)]
    monkeypatch.setattr("kronieker.planner.show_num_pages", lambda *a, **kw: 350)

    def fake_query_domain(domain, **kwargs):
        return iter(candidates)
    monkeypatch.setattr(pipeline_mod, "query_domain", fake_query_domain)

    def slow_fetch(snap, session, timeout, retries):
        time.sleep(0.05)  # 50ms per fetch
        return FetchedPage(snap, 200, f"<html>hit-{snap.original}</html>")
    monkeypatch.setattr("kronieker.fetcher._fetch_one", slow_fetch)

    # Timeout 0.2s, workers=2 → capacity nominal would be huge but real wall
    # time only allows a handful of fetches before deadline.
    result = pipeline_mod.scan_domain(
        "big.example",
        timeout_seconds=0.2,
        max_workers=2,
        rate_limit_per_sec=0,  # no rate throttle on top of slow_fetch
        probe_well_known=False,
        calibration=_calibration(),
        no_escalate=True,
    )

    assert result.snapshots_fetched < len(candidates), (
        f"deadline didn't cut off — fetched {result.snapshots_fetched} of "
        f"{len(candidates)} candidates"
    )


def test_in_flight_fetches_complete_after_deadline_hit(monkeypatch):
    """When the deadline hits mid-scan, the futures already submitted finish.

    We verify by counting fetch invocations: at least the primed-pool size of
    workers worth of fetches must complete, even though many more were queued.
    """
    candidates = [_fast_snapshot(i) for i in range(50)]
    monkeypatch.setattr("kronieker.planner.show_num_pages", lambda *a, **kw: 350)
    monkeypatch.setattr(pipeline_mod, "query_domain",
                        lambda d, **kw: iter(candidates))

    started = {"n": 0}
    completed = {"n": 0}

    def measured_fetch(snap, session, timeout, retries):
        started["n"] += 1
        time.sleep(0.05)
        completed["n"] += 1
        return FetchedPage(snap, 200, "<html>ok</html>")
    monkeypatch.setattr("kronieker.fetcher._fetch_one", measured_fetch)

    pipeline_mod.scan_domain(
        "big.example",
        timeout_seconds=0.15,
        max_workers=4,
        rate_limit_per_sec=0,
        probe_well_known=False,
        calibration=_calibration(),
        no_escalate=True,
    )

    # Every started fetch must have completed — we don't abandon in-flight work.
    assert completed["n"] == started["n"], (
        f"some in-flight fetches were abandoned: started={started['n']}, "
        f"completed={completed['n']}"
    )
    # And we must have completed at least *some* fetches.
    assert completed["n"] >= 1


def test_scan_result_marks_timeout_exhausted_true(monkeypatch):
    """When the scan stops because of the deadline, ScanResult.timeout_exhausted=True."""
    candidates = [_fast_snapshot(i) for i in range(100)]
    monkeypatch.setattr("kronieker.planner.show_num_pages", lambda *a, **kw: 350)
    monkeypatch.setattr(pipeline_mod, "query_domain",
                        lambda d, **kw: iter(candidates))

    def slow_fetch(snap, session, timeout, retries):
        time.sleep(0.05)
        return FetchedPage(snap, 200, "<html>x</html>")
    monkeypatch.setattr("kronieker.fetcher._fetch_one", slow_fetch)

    result = pipeline_mod.scan_domain(
        "big.example",
        timeout_seconds=0.15,
        max_workers=2,
        rate_limit_per_sec=0,
        probe_well_known=False,
        calibration=_calibration(),
        no_escalate=True,
    )

    assert result.timeout_exhausted is True
    assert result.timeout_seconds == 0.15
    assert result.elapsed_seconds >= 0.15 * 0.9  # rough lower bound


def test_unlimited_timeout_does_not_mark_exhausted(monkeypatch):
    """timeout=0 → scan completes naturally, timeout_exhausted stays False."""
    candidates = [_fast_snapshot(i) for i in range(3)]
    monkeypatch.setattr("kronieker.planner.show_num_pages", lambda *a, **kw: 1)
    monkeypatch.setattr(pipeline_mod, "query_domain",
                        lambda d, **kw: iter(candidates))
    monkeypatch.setattr(
        "kronieker.fetcher._fetch_one",
        lambda snap, sess, t, r: FetchedPage(snap, 200, "<html>ok</html>"),
    )

    result = pipeline_mod.scan_domain(
        "small.example",
        timeout_seconds=0,         # unlimited
        force_all=True,
        max_workers=2,
        rate_limit_per_sec=0,
        probe_well_known=False,
        calibration=_calibration(),
        no_escalate=True,
    )

    assert result.timeout_exhausted is False
    assert result.timeout_seconds == 0.0
