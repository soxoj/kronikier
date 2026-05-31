"""Unit tests for the file-based snapshot cache."""

from __future__ import annotations

from pathlib import Path

import pytest

from kronikier.cache import SnapshotCache, default_cache_dir
from kronikier.cdx import Snapshot
from kronikier.fetcher import FetchedPage


def _snap(ts="20200101000000", url="http://example.com/contact") -> Snapshot:
    return Snapshot(
        timestamp=ts,
        original=url,
        mimetype="text/html",
        status="200",
        urlkey="",
    )


def _page(snap=None, status=200, content="<html>info@x</html>", error=None) -> FetchedPage:
    return FetchedPage(snap or _snap(), status, content, error=error)


class TestDefaultCacheDir:
    def test_kronikier_cache_dir_env_takes_precedence(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KRONIEKER_CACHE_DIR", str(tmp_path / "custom"))
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
        assert default_cache_dir() == tmp_path / "custom"

    def test_xdg_cache_home_fallback(self, tmp_path, monkeypatch):
        monkeypatch.delenv("KRONIEKER_CACHE_DIR", raising=False)
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
        assert default_cache_dir() == tmp_path / "xdg" / "kronikier" / "snapshots"

    def test_home_cache_default(self, tmp_path, monkeypatch):
        monkeypatch.delenv("KRONIEKER_CACHE_DIR", raising=False)
        monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        assert default_cache_dir() == tmp_path / ".cache" / "kronikier" / "snapshots"


class TestSnapshotCacheRoundtrip:
    def test_put_then_get_returns_same_content(self, tmp_path):
        cache = SnapshotCache(tmp_path)
        snap = _snap()
        cache.put(_page(snap, content="<html>hello</html>"))
        got = cache.get(snap)
        assert got is not None
        assert got.content == "<html>hello</html>"
        assert got.status == 200
        assert cache.hits == 1
        assert cache.misses == 0

    def test_get_miss_returns_none_and_counts(self, tmp_path):
        cache = SnapshotCache(tmp_path)
        assert cache.get(_snap()) is None
        assert cache.misses == 1
        assert cache.hits == 0

    def test_distinct_snapshots_get_distinct_files(self, tmp_path):
        cache = SnapshotCache(tmp_path)
        a = _snap("20200101000000", "http://example.com/a")
        b = _snap("20200101000000", "http://example.com/b")
        cache.put(_page(a, content="A"))
        cache.put(_page(b, content="B"))
        assert cache.get(a).content == "A"
        assert cache.get(b).content == "B"
        assert cache.size() == 2

    def test_put_is_idempotent(self, tmp_path):
        cache = SnapshotCache(tmp_path)
        snap = _snap()
        cache.put(_page(snap, content="<html>v1</html>"))
        cache.put(_page(snap, content="<html>v2</html>"))  # overwrite ok
        assert cache.get(snap).content == "<html>v2</html>"
        assert cache.size() == 1


class TestNegativeResults:
    """Errors / non-200 / empty bodies must never make it onto disk — they
    could be transient and would poison future runs.
    """

    def test_error_page_not_cached(self, tmp_path):
        cache = SnapshotCache(tmp_path)
        snap = _snap()
        cache.put(_page(snap, status=500, content="", error="HTTP 500"))
        assert cache.get(snap) is None
        assert cache.size() == 0

    def test_404_not_cached(self, tmp_path):
        cache = SnapshotCache(tmp_path)
        snap = _snap()
        cache.put(_page(snap, status=404, content="", error="HTTP 404"))
        assert cache.get(snap) is None
        assert cache.size() == 0

    def test_empty_body_not_cached(self, tmp_path):
        cache = SnapshotCache(tmp_path)
        snap = _snap()
        cache.put(_page(snap, status=200, content=""))
        assert cache.get(snap) is None
        assert cache.size() == 0


class TestClear:
    def test_clear_removes_everything_and_returns_count(self, tmp_path):
        cache = SnapshotCache(tmp_path)
        for i in range(5):
            cache.put(_page(_snap(url=f"http://example.com/{i}"), content=f"p{i}"))
        assert cache.size() == 5
        removed = cache.clear()
        assert removed == 5
        assert cache.size() == 0


class TestFilenameLayout:
    """The on-disk filename must be human-browsable: domain-grouped,
    timestamp-leading, with the URL path visible.
    """

    def test_file_path_is_domain_grouped_and_browsable(self, tmp_path):
        cache = SnapshotCache(tmp_path)
        snap = _snap("20200315120000", "http://example.com/contact-us")
        cache.put(_page(snap))
        files = list(tmp_path.rglob("*.html"))
        assert len(files) == 1
        rel = files[0].relative_to(tmp_path)
        assert rel.parts[0] == "example.com"
        # Filename starts with the timestamp and contains the URL path slug.
        assert rel.name.startswith("20200315120000__")
        assert "contact-us" in rel.name

    def test_unsafe_path_chars_are_sanitized(self, tmp_path):
        cache = SnapshotCache(tmp_path)
        snap = _snap(url="http://example.com/foo bar?x=y&z#frag")
        cache.put(_page(snap))
        files = list(tmp_path.rglob("*.html"))
        assert len(files) == 1
        # No spaces, no ? & # in the filename — fs-safe.
        assert " " not in files[0].name
        assert "?" not in files[0].name
        # Lookup still works on the original URL.
        assert cache.get(snap) is not None


class TestFetcherIntegration:
    """Cache must short-circuit ``fetch_snapshots`` — a hit yields the
    page without calling ``_fetch_one`` (i.e. no HTTP, no rate-limit token).
    """

    def test_cache_hit_skips_http(self, tmp_path, monkeypatch):
        from kronikier.fetcher import fetch_snapshots

        cache = SnapshotCache(tmp_path)
        snap = _snap()
        cache.put(_page(snap, content="<html>cached!</html>"))

        http_calls = {"n": 0}

        def fail_if_called(*a, **kw):
            http_calls["n"] += 1
            raise AssertionError("HTTP fetch should never run for a cache hit")

        monkeypatch.setattr("kronikier.fetcher._fetch_one", fail_if_called)

        pages = list(fetch_snapshots([snap], rate_limit_per_sec=0, cache=cache))
        assert len(pages) == 1
        assert pages[0].content == "<html>cached!</html>"
        assert http_calls["n"] == 0
        assert cache.hits == 1

    def test_cache_miss_does_http_and_persists(self, tmp_path, monkeypatch):
        from kronikier.fetcher import fetch_snapshots

        cache = SnapshotCache(tmp_path)
        snap = _snap()

        http_calls = {"n": 0}

        def fake_fetch(snap, session, timeout, retries):
            http_calls["n"] += 1
            return FetchedPage(snap, 200, "<html>fresh</html>")

        monkeypatch.setattr("kronikier.fetcher._fetch_one", fake_fetch)

        pages = list(fetch_snapshots([snap], rate_limit_per_sec=0, cache=cache))
        assert len(pages) == 1
        assert pages[0].content == "<html>fresh</html>"
        assert http_calls["n"] == 1
        # And the page should now be in cache for next time.
        assert cache.size() == 1
        assert cache.get(snap).content == "<html>fresh</html>"

    def test_cache_None_means_no_caching(self, tmp_path, monkeypatch):
        from kronikier.fetcher import fetch_snapshots

        snap = _snap()

        def fake_fetch(snap, session, timeout, retries):
            return FetchedPage(snap, 200, "<html>x</html>")

        monkeypatch.setattr("kronikier.fetcher._fetch_one", fake_fetch)

        pages = list(fetch_snapshots([snap], rate_limit_per_sec=0, cache=None))
        assert len(pages) == 1
        # Nothing should have landed on disk.
        assert not list(tmp_path.rglob("*.html"))


class TestCliIntegration:
    """End-to-end through ``cli.main``."""

    def _calibration(self):
        from kronikier.calibration import Calibration, CALIBRATION_VERSION
        return Calibration(
            version=CALIBRATION_VERSION, avg_latency_s=0.42, sample_count=8,
            last_calibrated_at="2026-01-01T00:00:00+00:00",
            samples_p50=0.4, samples_p95=0.5, user_agent="test",
        )

    def test_clear_cache_flag_wipes_and_exits(self, tmp_path, monkeypatch, capsys):
        from kronikier import cli

        monkeypatch.setenv("KRONIEKER_CACHE_DIR", str(tmp_path / "cache"))
        # Pre-populate.
        cache = SnapshotCache(tmp_path / "cache")
        cache.put(_page())
        assert cache.size() == 1

        scan_calls = {"n": 0}
        monkeypatch.setattr(cli, "scan_domain", lambda *a, **kw: scan_calls.__setitem__("n", scan_calls["n"] + 1))

        rc = cli.main(["--clear-cache"])
        assert rc == 0
        assert scan_calls["n"] == 0
        err = capsys.readouterr().err
        assert "Cleared 1 cached snapshot" in err
        assert SnapshotCache(tmp_path / "cache").size() == 0

    def test_no_cache_flag_disables_cache(self, tmp_path, monkeypatch, capsys):
        from kronikier import cli
        from kronikier.pipeline import ScanResult

        monkeypatch.setenv("KRONIEKER_CACHE_DIR", str(tmp_path / "cache"))

        captured_cache = {"value": "sentinel"}

        def fake_scan(domain, **kw):
            captured_cache["value"] = kw.get("cache")
            return ScanResult(domain=domain, snapshots_considered=0,
                              snapshots_fetched=0, sightings=[], errors=[])

        monkeypatch.setattr(cli, "scan_domain", fake_scan)
        monkeypatch.setattr(cli, "ensure_calibration", lambda **kw: self._calibration())

        rc = cli.main(["example.com", "--no-cache", "--no-progress", "--no-csv"])
        assert rc == 0
        assert captured_cache["value"] is None, (
            f"--no-cache must pass cache=None to scan_domain; got {captured_cache['value']!r}"
        )
        # And no cache dir should have been created.
        assert not (tmp_path / "cache").exists()
