"""Unit tests for the persistent latency-calibration module."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from kronikier import calibration as cal_mod
from kronikier.calibration import (
    CALIBRATION_VERSION,
    DEFAULT_AVG_LATENCY_S,
    MIN_SUCCESSFUL_SAMPLES,
    TTL_SECONDS,
    Calibration,
    cache_path,
    is_stale,
    load,
    run_calibration,
    save,
)
from kronikier.cdx import Snapshot
from kronikier.fetcher import FetchedPage


def _make_cal(**overrides) -> Calibration:
    defaults = dict(
        version=CALIBRATION_VERSION,
        avg_latency_s=0.42,
        sample_count=8,
        last_calibrated_at=datetime.now(timezone.utc).isoformat(),
        samples_p50=0.4,
        samples_p95=0.5,
        user_agent="test/1.0",
    )
    defaults.update(overrides)
    return Calibration(**defaults)


# ---------------------------------------------------------------------------
# Roundtrip
# ---------------------------------------------------------------------------


def test_calibration_roundtrip(tmp_path: Path):
    path = tmp_path / "cal.json"
    cal = _make_cal(avg_latency_s=0.371, sample_count=8)
    save(cal, path)

    loaded = load(path)
    assert loaded is not None
    assert loaded.avg_latency_s == 0.371
    assert loaded.sample_count == 8
    assert loaded.samples_p50 == 0.4
    assert loaded.samples_p95 == 0.5


def test_load_returns_none_when_file_missing(tmp_path: Path):
    assert load(tmp_path / "missing.json") is None


def test_load_returns_none_on_corrupt_json(tmp_path: Path):
    path = tmp_path / "cal.json"
    path.write_text("{not valid json", encoding="utf-8")
    assert load(path) is None


def test_load_returns_none_on_missing_required_field(tmp_path: Path):
    path = tmp_path / "cal.json"
    path.write_text(json.dumps({"version": 1, "avg_latency_s": 0.5}), encoding="utf-8")
    # Missing other required fields → None (treated as corrupt)
    assert load(path) is None


# ---------------------------------------------------------------------------
# Staleness
# ---------------------------------------------------------------------------


def test_is_stale_for_none():
    assert is_stale(None) is True


def test_is_stale_for_wrong_version():
    cal = _make_cal(version=CALIBRATION_VERSION + 99)
    assert is_stale(cal) is True


def test_calibration_stale_after_ttl():
    long_ago = (datetime.now(timezone.utc) - timedelta(seconds=TTL_SECONDS + 100)).isoformat()
    cal = _make_cal(last_calibrated_at=long_ago)
    assert is_stale(cal) is True


def test_calibration_fresh_within_ttl():
    recent = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
    cal = _make_cal(last_calibrated_at=recent)
    assert is_stale(cal) is False


def test_load_returns_none_when_cached_value_is_stale(tmp_path: Path):
    """The load() helper conflates 'stale' with 'missing' (caller treats same)."""
    path = tmp_path / "cal.json"
    long_ago = (datetime.now(timezone.utc) - timedelta(seconds=TTL_SECONDS + 100)).isoformat()
    save(_make_cal(last_calibrated_at=long_ago), path)
    assert load(path) is None


def test_load_returns_none_on_version_mismatch(tmp_path: Path):
    path = tmp_path / "cal.json"
    save(_make_cal(version=CALIBRATION_VERSION + 99), path)
    assert load(path) is None


# ---------------------------------------------------------------------------
# Cache path
# ---------------------------------------------------------------------------


def test_calibration_cache_path_respects_xdg_cache_home(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "custom-cache"))
    p = cache_path()
    assert str(p).startswith(str(tmp_path / "custom-cache"))
    assert p.name == "calibration.json"


def test_calibration_cache_path_falls_back_to_home(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    p = cache_path()
    assert str(p).startswith(str(tmp_path / ".cache"))


# ---------------------------------------------------------------------------
# run_calibration — mock _fetch_one
# ---------------------------------------------------------------------------


def _ok_page(snap: Snapshot) -> FetchedPage:
    return FetchedPage(snapshot=snap, status=200, content="<html>ok</html>")


def _err_page(snap: Snapshot) -> FetchedPage:
    return FetchedPage(snapshot=snap, status=503, content="", error="HTTP 503")


def test_run_calibration_computes_mean_from_successful_fetches(monkeypatch):
    """All 8 succeed → real avg, real sample count."""
    monkeypatch.setattr(cal_mod, "_fetch_one", lambda snap, sess, timeout, retries: _ok_page(snap))

    cal = run_calibration(workers=4)
    assert cal.version == CALIBRATION_VERSION
    assert cal.sample_count == 8
    assert cal.avg_latency_s > 0  # something measured
    assert cal.avg_latency_s < 5.0  # but not absurd in a unit-test environment


def test_calibration_falls_back_to_default_on_too_few_successes(monkeypatch):
    """6/8 fail → fewer than MIN_SUCCESSFUL_SAMPLES succeed → fallback."""
    call_count = {"n": 0}

    def fake_fetch(snap, sess, timeout, retries):
        call_count["n"] += 1
        # First 2 succeed, rest fail → 2 successful samples (< MIN=4) → fallback
        return _ok_page(snap) if call_count["n"] <= 2 else _err_page(snap)

    monkeypatch.setattr(cal_mod, "_fetch_one", fake_fetch)

    cal = run_calibration(workers=1)  # workers=1 makes call order deterministic
    assert cal.avg_latency_s == DEFAULT_AVG_LATENCY_S
    assert cal.sample_count == 0
    assert "fallback" in cal.user_agent


def test_run_calibration_uses_passed_fixture(monkeypatch):
    """Custom fixture is honored."""
    monkeypatch.setattr(cal_mod, "_fetch_one", lambda snap, sess, timeout, retries: _ok_page(snap))

    custom = tuple(
        Snapshot(timestamp=f"2020010100000{i}", original=f"http://test{i}.example/",
                 mimetype="text/html", status="200", urlkey="")
        for i in range(5)
    )
    cal = run_calibration(fixture=custom, workers=2)
    assert cal.sample_count == 5


# ---------------------------------------------------------------------------
# ensure_calibration: end-to-end glue
# ---------------------------------------------------------------------------


def test_ensure_calibration_uses_cached_when_fresh(monkeypatch, tmp_path: Path):
    """Fresh cache on disk → no fetches happen."""
    path = tmp_path / "cal.json"
    save(_make_cal(avg_latency_s=0.123), path)

    called = {"n": 0}
    monkeypatch.setattr(cal_mod, "_fetch_one", lambda *a, **kw: called.__setitem__("n", called["n"] + 1) or _ok_page(a[0]))

    cal = cal_mod.ensure_calibration(path=path, announce=False)
    assert cal.avg_latency_s == 0.123
    assert called["n"] == 0  # cached, no recalibration


def test_ensure_calibration_runs_when_force(monkeypatch, tmp_path: Path):
    """force=True triggers recalibration even with fresh cache."""
    path = tmp_path / "cal.json"
    save(_make_cal(avg_latency_s=0.999), path)

    monkeypatch.setattr(cal_mod, "_fetch_one", lambda snap, sess, timeout, retries: _ok_page(snap))
    cal = cal_mod.ensure_calibration(force=True, path=path, announce=False)

    # New calibration was run — value will not be the cached 0.999
    assert cal.sample_count == 8
    assert cal.avg_latency_s != 0.999


def test_ensure_calibration_runs_when_no_cache(monkeypatch, tmp_path: Path):
    """No cache file → runs calibration + persists."""
    path = tmp_path / "missing.json"
    monkeypatch.setattr(cal_mod, "_fetch_one", lambda snap, sess, timeout, retries: _ok_page(snap))

    cal = cal_mod.ensure_calibration(path=path, announce=False)
    assert cal.sample_count == 8
    assert path.exists()
