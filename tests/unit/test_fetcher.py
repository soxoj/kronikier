"""Unit tests for the parallel snapshot fetcher."""

from __future__ import annotations

import responses

from kronikier.cdx import Snapshot
from kronikier.fetcher import fetch_snapshots


def _snap(ts: str, url: str) -> Snapshot:
    return Snapshot(timestamp=ts, original=url, mimetype="text/html", status="200", urlkey=url)


@responses.activate
def test_fetch_snapshot_success():
    snap = _snap("20070101120000", "http://example.com/contact")
    responses.add(
        responses.GET,
        snap.archive_url(raw=True),
        body="<html>hello</html>",
        status=200,
        content_type="text/html; charset=utf-8",
    )
    results = list(fetch_snapshots([snap], max_workers=1, rate_limit_per_sec=0))
    assert len(results) == 1
    assert results[0].status == 200
    assert results[0].content == "<html>hello</html>"
    assert results[0].error is None


@responses.activate
def test_fetch_skips_non_html_content_type():
    snap = _snap("20070101120000", "http://example.com/logo.png")
    responses.add(
        responses.GET,
        snap.archive_url(raw=True),
        body=b"\x89PNG\r\n",
        status=200,
        content_type="image/png",
    )
    results = list(fetch_snapshots([snap], max_workers=1, rate_limit_per_sec=0))
    assert results[0].content == ""
    assert results[0].error and "non-html" in results[0].error


@responses.activate
def test_fetch_retries_on_429_then_succeeds():
    snap = _snap("20070101120000", "http://example.com/")
    url = snap.archive_url(raw=True)
    responses.add(responses.GET, url, status=429)
    responses.add(responses.GET, url, body="<html>ok</html>", status=200, content_type="text/html")

    results = list(fetch_snapshots([snap], max_workers=1, retries=2, rate_limit_per_sec=0))
    assert results[0].status == 200
    assert results[0].content == "<html>ok</html>"


@responses.activate
def test_fetch_retries_transient_404_then_succeeds():
    """IA sometimes 404s a snapshot on first hit and serves it on retry.

    Confirmed empirically against tomhunter.ru — the page loaded slowly in a
    browser, our pipeline saw 404 on the first GET and gave up.
    """
    snap = _snap("20220625004541", "https://tomhunter.ru/contacts")
    url = snap.archive_url(raw=True)
    responses.add(responses.GET, url, status=404)
    responses.add(responses.GET, url, body="<html>contacts</html>", status=200, content_type="text/html")

    # Need retries >= 1 to recover. Use a 0 rate limit so we don't actually
    # wait the 3-sec backoff; instead the test mocks make the second call
    # return immediately.
    import kronikier.fetcher as _fetcher
    orig_sleep = _fetcher.time.sleep
    _fetcher.time.sleep = lambda _s: None
    try:
        results = list(fetch_snapshots([snap], max_workers=1, retries=2, rate_limit_per_sec=0))
    finally:
        _fetcher.time.sleep = orig_sleep
    assert results[0].status == 200
    assert results[0].content == "<html>contacts</html>"


@responses.activate
def test_fetch_empty_input_does_not_explode():
    results = list(fetch_snapshots([], max_workers=2, rate_limit_per_sec=0))
    assert results == []


@responses.activate
def test_fetch_multiple_snapshots():
    snaps = [
        _snap("20070101120000", "http://example.com/a"),
        _snap("20070101120000", "http://example.com/b"),
        _snap("20070101120000", "http://example.com/c"),
    ]
    for s in snaps:
        responses.add(
            responses.GET,
            s.archive_url(raw=True),
            body=f"<html>{s.original}</html>",
            status=200,
            content_type="text/html",
        )
    results = list(fetch_snapshots(snaps, max_workers=3, rate_limit_per_sec=0))
    assert len(results) == 3
    assert {r.snapshot.original for r in results} == {s.original for s in snaps}
