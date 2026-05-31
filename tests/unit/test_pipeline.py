"""End-to-end test of the pipeline with a fully mocked wayback machine.

This is the most important unit test — it wires CDX → fetcher → extractors
together and asserts that for an archived page that has now lost its
contacts, the tool still surfaces what was there historically.
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path

import responses

from kronikier.cdx import CDX_ENDPOINT
from kronikier.pipeline import scan_domain

FIXTURES = Path(__file__).parent.parent / "fixtures"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _cdx_body(rows: list[list[str]]) -> str:
    header = ["urlkey", "timestamp", "original", "mimetype", "statuscode", "digest", "length"]
    return json.dumps([header, *rows])


@responses.activate
def test_pipeline_recovers_contacts_from_archived_snapshot():
    """The "current" site has nothing, but a 2007 archive has 5 emails + 3 phones."""
    domain = "romashka-llc.ru"

    # CDX returns three snapshots for the domain.
    responses.add(
        responses.GET,
        CDX_ENDPOINT,
        body=_cdx_body(
            [
                # urlkey, timestamp, original, mimetype, status, digest, length
                ["ru,romashka-llc)/", "20240101000000", f"http://{domain}/", "text/html", "200", "X1", "10"],
                ["ru,romashka-llc)/contacts.html", "20070815120000", f"http://{domain}/contacts.html", "text/html", "200", "X2", "20"],
                ["ru,romashka-llc)/about", "20030101000000", f"http://{domain}/about", "text/html", "200", "X3", "30"],
            ]
        ),
        status=200,
    )

    # Availability API (used by probe_well_known) — return None for everything
    # so we only fetch what CDX gave us.
    responses.add(
        responses.GET,
        "https://archive.org/wayback/available",
        json={"archived_snapshots": {}},
        status=200,
    )

    # Snapshot bodies
    responses.add(
        responses.GET,
        f"https://web.archive.org/web/20240101000000id_/http://{domain}/",
        body=_read("empty_footer_2024.html"),
        status=200,
        content_type="text/html; charset=utf-8",
    )
    responses.add(
        responses.GET,
        f"https://web.archive.org/web/20070815120000id_/http://{domain}/contacts.html",
        body=_read("contacts_ru_2007.html"),
        status=200,
        content_type="text/html; charset=utf-8",
    )
    responses.add(
        responses.GET,
        f"https://web.archive.org/web/20030101000000id_/http://{domain}/about",
        body="<html><body><p>About us. Email: hello@romashka-llc.ru</p></body></html>",
        status=200,
        content_type="text/html; charset=utf-8",
    )

    result = scan_domain(
        domain,
        mode="exhaustive",  # exercise the CDX path against our mock
        max_snapshots=10,
        probe_well_known=False,  # skip well-known probing to keep mock setup tight
        rate_limit_per_sec=0,
        default_phone_regions=("RU",),
    )

    assert result.snapshots_considered == 3
    assert result.snapshots_fetched == 3

    emails = {s.contact.value for s in result.sightings if s.contact.kind == "email"}
    phones = {s.contact.value for s in result.sightings if s.contact.kind == "phone"}

    assert "info@romashka-llc.ru" in emails
    assert "buh@romashka-llc.ru" in emails
    assert "director@romashka-llc.ru" in emails  # deobfuscated собака/точка
    assert "hello@romashka-llc.ru" in emails

    assert "+74951234567" in phones
    assert "+79165551234" in phones


@responses.activate
def test_pipeline_handles_cdx_error_gracefully():
    responses.add(responses.GET, CDX_ENDPOINT, status=500)
    result = scan_domain(
        "broken.example",
        mode="exhaustive",
        probe_well_known=False,
        rate_limit_per_sec=0,
    )
    assert result.snapshots_considered == 0
    assert result.snapshots_fetched == 0
    assert result.errors and "CDX query failed" in result.errors[0]
    assert result.sightings == []


@responses.activate
def test_pipeline_keeps_free_provider_business_email():
    """OSINT case: the company's real contact email is on a free provider
    (mail.ru / yandex.ru / gmail.com). It MUST NOT be filtered out just
    because the email-domain doesn't match the website-domain.
    """
    domain = "small-ru.example"
    responses.add(
        responses.GET,
        CDX_ENDPOINT,
        body=_cdx_body(
            [["ex,small-ru)/", "20220520162612", f"http://{domain}/kontakty",
              "text/html", "200", "X", "1"]]
        ),
        status=200,
    )
    responses.add(
        responses.GET,
        f"https://web.archive.org/web/20220520162612id_/http://{domain}/kontakty",
        body=(
            "<html><body>"
            "<p>e-mail: <a href='mailto:forcing-technic@mail.ru'>forcing-technic@mail.ru</a></p>"
            "<p>Заместитель директора +375-29-722-84-40</p>"
            "<p>ПТО 8-0162-51-12-54</p>"
            "<p><img src='hero@2x.png'></p>"
            "</body></html>"
        ),
        status=200,
        content_type="text/html",
    )
    result = scan_domain(
        domain,
        mode="exhaustive",
        probe_well_known=False,
        rate_limit_per_sec=0,
    )
    emails = {s.contact.value for s in result.sightings if s.contact.kind == "email"}
    phones = {s.contact.value for s in result.sightings if s.contact.kind == "phone"}

    # The free-provider contact email — primary OSINT signal — must be kept.
    assert "forcing-technic@mail.ru" in emails
    # Image-filename false-positive still filtered by extractor.
    assert not any(".png" in e for e in emails)

    # BY phones in both international and 8-trunk format, picked up via
    # the .by TLD → BY-first region prioritisation.
    assert "+375297228440" in phones
    assert "+375162511254" in phones


@responses.activate
def test_default_mode_keeps_multiple_years_of_same_url():
    """Default-mode dedup must be (URL, year) — not (URL only).

    For /kontakty across 2018/2020/2022 we expect all three timestamps to
    reach the fetch stage, so multi-year contact changes are observable.
    """
    domain = "multi-year.example"
    url = f"http://{domain}/kontakty"
    rows = [
        ["ex,multi-year)/kontakty", "20180101000000", url, "text/html", "200", "A", "1"],
        ["ex,multi-year)/kontakty", "20200101000000", url, "text/html", "200", "B", "1"],
        ["ex,multi-year)/kontakty", "20220101000000", url, "text/html", "200", "C", "1"],
        # Two snapshots in the SAME year — these MUST collapse to one (per
        # year dedup), so we expect 3 total, not 4.
        ["ex,multi-year)/kontakty", "20220601000000", url, "text/html", "200", "D", "1"],
    ]

    def cdx_cb(request):
        if "showNumPages" in request.url:
            return (200, {}, "1")
        return (200, {}, _cdx_body(rows))

    responses.add_callback(responses.GET, CDX_ENDPOINT, callback=cdx_cb)
    responses.add(
        responses.GET, "https://archive.org/wayback/available",
        json={"archived_snapshots": {}}, status=200,
    )
    # Mock all 3 fetched snapshots (one per year)
    for ts in ("20180101000000", "20200101000000", "20220101000000"):
        responses.add(
            responses.GET,
            f"https://web.archive.org/web/{ts}id_/{url}",
            body=f"<html>info-{ts}@{domain}</html>",
            status=200, content_type="text/html",
        )

    result = scan_domain(domain, mode="default", probe_well_known=False,
                        rate_limit_per_sec=0)
    fetched = {s.timestamp for s in result.sightings}
    assert fetched == {"20180101000000", "20200101000000", "20220101000000"}


@responses.activate
def test_exhaustive_mode_does_not_dedup_within_year():
    """Exhaustive mode must fetch *every* snapshot, including multiple
    snapshots of the same URL within the same year.
    """
    domain = "every-snap.example"
    url = f"http://{domain}/kontakty"
    rows = [
        # Three snapshots all in 2022 — exhaustive must keep all three.
        ["ex,every-snap)/kontakty", "20220101000000", url, "text/html", "200", "A", "1"],
        ["ex,every-snap)/kontakty", "20220601000000", url, "text/html", "200", "B", "1"],
        ["ex,every-snap)/kontakty", "20221201000000", url, "text/html", "200", "C", "1"],
    ]
    responses.add(responses.GET, CDX_ENDPOINT, body=_cdx_body(rows), status=200)
    responses.add(
        responses.GET, "https://archive.org/wayback/available",
        json={"archived_snapshots": {}}, status=200,
    )
    for ts in ("20220101000000", "20220601000000", "20221201000000"):
        responses.add(
            responses.GET,
            f"https://web.archive.org/web/{ts}id_/{url}",
            body=f"<html>info-{ts}@{domain}</html>",
            status=200, content_type="text/html",
        )

    result = scan_domain(domain, mode="exhaustive", probe_well_known=False,
                        rate_limit_per_sec=0, max_snapshots=10)
    fetched = {s.timestamp for s in result.sightings}
    assert fetched == {"20220101000000", "20220601000000", "20221201000000"}, (
        f"exhaustive must not dedup within a year; got {fetched}"
    )


class TestProbeSmartSkip:
    """Smart-skip inference inside ``_iter_well_known`` — see docstring there
    for the math. These tests pin the two skip rules.
    """

    def _run(self, monkeypatch, responses_fn, paths=("/",), timestamps=None):
        from kronikier.pipeline import _iter_well_known
        import requests as _requests

        timestamps = timestamps or ("19990101", "20050101", "20100101", "20150101", "20200101")
        calls: list[tuple[str, str]] = []

        def fake_closest(url, *, timestamp, session=None, timeout=30):
            calls.append((url, timestamp))
            return responses_fn(url, timestamp)

        monkeypatch.setattr("kronikier.pipeline.closest_snapshot", fake_closest)
        sess = _requests.Session()
        snaps = list(_iter_well_known(
            "ex.example", sess, probe_timestamps=timestamps, paths=paths,
        ))
        return calls, snaps

    def test_first_none_kills_remaining_timestamps_for_path(self, monkeypatch):
        """When the first probe returns None, the URL has zero archived
        snapshots — skip the remaining 4 timestamps for that path entirely.
        """
        calls, snaps = self._run(monkeypatch, lambda u, t: None)
        assert calls == [("http://ex.example/", "19990101")], (
            f"expected exactly 1 HTTP call for a dead path, got {len(calls)}: {calls}"
        )
        assert snaps == []

    def test_wide_gap_covers_entire_timeline(self, monkeypatch):
        """Query 1999 → snap from 2020 ⇒ no captures in [1999, 2020] except
        S, so the remaining 2005/2010/2015/2020 queries can be skipped.
        """
        from kronikier.cdx import Snapshot

        def fake(url, ts):
            return Snapshot(
                timestamp="20200315120000",
                original=url,
                mimetype="text/html",
                status="200",
                urlkey="",
            )

        calls, snaps = self._run(monkeypatch, fake)
        assert len(calls) == 1, (
            f"after a wide-gap hit, all 4 remaining queries must be skipped — "
            f"got {len(calls)} HTTP calls: {calls}"
        )
        assert len(snaps) == 1

    def test_narrow_gap_keeps_probing(self, monkeypatch):
        """Query 1999 → snap from 2003 covers only [1999, 2003]. The remaining
        2005/2010/2015/2020 timestamps are all outside that interval — they
        must still trigger HTTP calls (we can't prove what's at those years).
        """
        from kronikier.cdx import Snapshot

        # Return distinct snapshots for distinct queries to also exercise
        # the dedup path.
        def fake(url, ts):
            return Snapshot(
                timestamp={
                    "19990101": "20030615000000",
                    "20050101": "20070615000000",
                    "20100101": "20120615000000",
                    "20150101": "20170615000000",
                    "20200101": "20220615000000",
                }[ts],
                original=url, mimetype="text/html", status="200", urlkey="",
            )

        calls, snaps = self._run(monkeypatch, fake)
        # All 5 timestamps span gaps too narrow to cover each other, so all
        # 5 HTTP calls should happen.
        assert len(calls) == 5
        assert len(snaps) == 5

    def test_second_query_hits_same_cluster(self, monkeypatch):
        """Query 1999 → snap 2003 (interval [1999, 2003], 2005 outside).
        Query 2005 → snap 2003 again. The second response widens the covered
        interval to [1999, 2005], and 2010+ are still outside but 2005 itself
        is now covered — no impact since we already processed it. The point
        of this test: make sure the same snapshot returned twice is just
        deduped, not crashed.
        """
        from kronikier.cdx import Snapshot

        def fake(url, ts):
            if ts in ("19990101", "20050101"):
                return Snapshot(
                    timestamp="20030615000000", original=url,
                    mimetype="text/html", status="200", urlkey="",
                )
            return None  # later timestamps return None — transient

        calls, snaps = self._run(monkeypatch, fake)
        assert len(calls) >= 2
        assert len(snaps) == 1  # deduped


class TestStreamCdxWithBudget:
    """Direct tests of the threaded CDX iterator. The point of moving CDX
    off the main thread is to enforce the sub-budget *even when the first
    byte never arrives* — verify all three exit paths.
    """

    def _plan(self):
        from kronikier.planner import ScanPlan
        return ScanPlan(
            deadline_monotonic=math.inf, timeout_seconds=0.0, avg_latency_s=0.5,
            effective_concurrency=4, capacity=100, cdx_num_pages=None,
            estimated_total_snapshots=None, total_is_precise=False,
            no_captures=False, use_url_filter=False, cdx_urlkey_filter=None,
            cdx_limit=None, user_forced_all=False, rationale="",
        )

    def test_abandons_after_sub_deadline_when_first_byte_never_arrives(self, monkeypatch):
        """The whole point of the rewrite: ``requests.get`` hanging on first
        byte must NOT stall the scan. After ``sub_deadline`` we walk away
        with whatever (possibly nothing) we got.
        """
        import requests as _requests
        from kronikier.pipeline import _stream_cdx_with_budget

        def never_yields(*args, **kwargs):
            # Mimic IA's CDX server doing a long scan with no output.
            time.sleep(10.0)
            return
            yield  # pragma: no cover — generator marker

        monkeypatch.setattr("kronikier.pipeline.query_domain", never_yields)

        start = time.monotonic()
        snaps, truncated, err = _stream_cdx_with_budget(
            "huge.example",
            plan=self._plan(),
            sess=_requests.Session(),
            from_year=None, to_year=None, include_subdomains=True,
            cdx_timeout=30,
            sub_deadline=time.monotonic() + 0.3,  # 300ms budget
            budget_s=0.3,
        )
        elapsed = time.monotonic() - start

        assert truncated is True
        assert snaps == []
        assert err is None
        # Critical: we walked away around the budget, NOT after 10s.
        assert elapsed < 2.0, f"main thread blocked for {elapsed:.2f}s — abandon failed"

    def test_returns_all_rows_when_iteration_finishes_within_budget(self, monkeypatch):
        from kronikier.cdx import Snapshot
        from kronikier.pipeline import _stream_cdx_with_budget
        import requests as _requests

        def fast_stream(*args, **kwargs):
            for i in range(3):
                yield Snapshot(
                    timestamp="20200101000000",
                    original=f"http://fast.example/p{i}",
                    mimetype="text/html", status="200", urlkey="",
                )

        monkeypatch.setattr("kronikier.pipeline.query_domain", fast_stream)

        snaps, truncated, err = _stream_cdx_with_budget(
            "fast.example",
            plan=self._plan(),
            sess=_requests.Session(),
            from_year=None, to_year=None, include_subdomains=True,
            cdx_timeout=30,
            sub_deadline=time.monotonic() + 5.0,
            budget_s=5.0,
        )
        assert len(snaps) == 3
        assert truncated is False
        assert err is None

    def test_propagates_request_exception_as_error_message(self, monkeypatch):
        from kronikier.pipeline import _stream_cdx_with_budget
        import requests as _requests

        def crashing(*args, **kwargs):
            raise _requests.ConnectionError("simulated network drop")
            yield  # pragma: no cover

        monkeypatch.setattr("kronikier.pipeline.query_domain", crashing)

        snaps, truncated, err = _stream_cdx_with_budget(
            "ex.example",
            plan=self._plan(),
            sess=_requests.Session(),
            from_year=None, to_year=None, include_subdomains=True,
            cdx_timeout=30,
            sub_deadline=time.monotonic() + 5.0,
            budget_s=5.0,
        )
        assert snaps == []
        assert truncated is False
        assert err is not None
        assert "simulated network drop" in err


@responses.activate
def test_single_url_mode_runs_exact_cdx_and_skips_probe():
    """``scan_domain(single_url=...)`` must:

    1. Query CDX with ``matchType=exact`` against the full URL (not the host).
    2. Skip the urlkey filter — we know what URL we're after.
    3. Skip the well-known probe — no other paths to discover.
    4. Fetch every snapshot the URL has (within capacity).
    """
    url = "https://www.example.com/about-us"
    host = "www.example.com"

    def cdx_cb(request):
        # Only the planner preflight should hit showNumPages / fl=urlkey on
        # the *host*; the main scan query must target the exact URL.
        if "showNumPages" in request.url:
            return (200, {}, "1")
        if "fl=urlkey" in request.url:
            return (200, {}, json.dumps([["urlkey"], ["a"], ["b"]]))
        # Main scan query: assert it has matchType=exact and our URL.
        assert "matchType=exact" in request.url, request.url
        assert "about-us" in request.url, request.url
        # urlkey filter must NOT be present.
        assert "filter=urlkey" not in request.url, request.url
        return (200, {}, _cdx_body([
            ["ex,example)/about-us", "20200101000000", url, "text/html", "200", "A", "1"],
            ["ex,example)/about-us", "20210101000000", url, "text/html", "200", "B", "1"],
        ]))

    responses.add_callback(responses.GET, CDX_ENDPOINT, callback=cdx_cb)
    for ts in ("20200101000000", "20210101000000"):
        responses.add(
            responses.GET,
            f"https://web.archive.org/web/{ts}id_/{url}",
            body=f"<html>info-{ts}@example.com</html>",
            status=200, content_type="text/html",
        )
    # No /wayback/available mock: probe must NOT run, or the test fails on a
    # ConnectionError for the unmocked URL.

    result = scan_domain(
        host,
        timeout_seconds=120,
        single_url=url,
        rate_limit_per_sec=0,
    )

    emails = {s.contact.value for s in result.sightings if s.contact.kind == "email"}
    timestamps = {s.timestamp for s in result.sightings}
    # Both archived versions of the URL must have been fetched.
    assert "info-20200101000000@example.com" in emails
    assert "info-20210101000000@example.com" in emails
    assert timestamps == {"20200101000000", "20210101000000"}


@responses.activate
def test_no_captures_short_circuits_before_cdx_query_and_probe():
    """When ``count_captures`` reports zero, the scanner must not run the main
    CDX query or the well-known probe — it should return an empty result fast.

    Reproduces no-captures.example behavior: IA has 0 captures for the
    domain, planner detects this via the precise-count preflight, scan_domain
    short-circuits with a "No snapshots available" status line. Previously the
    tool would spend the full timeout probing 29 paths × 5 timestamps.
    """
    domain = "no-snapshots.example"

    def cdx_cb(request):
        if "showNumPages" in request.url:
            return (200, {}, "1")
        if "fl=urlkey" in request.url:
            # Header row only ⇒ count_captures returns 0.
            return (200, {}, json.dumps([["urlkey"]]))
        # Any other CDX hit would be a regression — the scanner must skip
        # the main CDX index query in the no-captures branch.
        raise AssertionError(f"unexpected CDX call: {request.url}")

    responses.add_callback(responses.GET, CDX_ENDPOINT, callback=cdx_cb)
    # No /wayback/available mock — if the probe phase ran, responses would
    # raise ConnectionError for the unmocked URL and the test would fail.

    result = scan_domain(domain, timeout_seconds=300, rate_limit_per_sec=0)

    assert result.sightings == []
    assert result.snapshots_considered == 0
    assert result.snapshots_fetched == 0
    assert result.errors == []
    # The only CDX hits must be the two preflight calls (showNumPages + fl=urlkey).
    cdx_hits = [c for c in responses.calls if c.request.url.startswith(CDX_ENDPOINT)]
    assert len(cdx_hits) == 2
    assert any("showNumPages" in c.request.url for c in cdx_hits)
    assert any("fl=urlkey" in c.request.url for c in cdx_hits)


@responses.activate
def test_ctrl_c_mid_scan_preserves_partial_sightings_and_flags_interrupted(monkeypatch):
    """KeyboardInterrupt during the fetch loop must surface as
    ``ScanResult.interrupted=True`` with every sighting collected before the
    keystroke still in ``sightings`` (so the CLI can save partial CSV).
    """
    from kronikier.cdx import Snapshot
    from kronikier.fetcher import FetchedPage

    domain = "interrupt-mid-scan.example"
    url = f"http://{domain}/contacts"

    def cdx_cb(request):
        if "showNumPages" in request.url:
            return (200, {}, "300")
        if "fl=urlkey" in request.url:
            return (200, {}, json.dumps([["urlkey"], ["x"]]))
        return (200, {}, _cdx_body([
            ["ex,im)/contacts", "20200101000000", url, "text/html", "200", "A", "1"],
            ["ex,im)/contacts", "20210101000000", url, "text/html", "200", "B", "1"],
            ["ex,im)/contacts", "20220101000000", url, "text/html", "200", "C", "1"],
        ]))

    responses.add_callback(responses.GET, CDX_ENDPOINT, callback=cdx_cb)
    responses.add(
        responses.GET, "https://archive.org/wayback/available",
        json={"archived_snapshots": {}}, status=200,
    )

    call_count = {"n": 0}

    def flaky_fetch(snap, session, timeout, retries):
        call_count["n"] += 1
        # First call: deliver a contact. Second call: raise KeyboardInterrupt
        # to simulate Ctrl+C arriving in the consumer thread mid-loop.
        if call_count["n"] == 1:
            return FetchedPage(snap, 200, f"<html>info@{domain}</html>")
        raise KeyboardInterrupt

    monkeypatch.setattr("kronikier.fetcher._fetch_one", flaky_fetch)

    result = scan_domain(
        domain, timeout_seconds=120, probe_well_known=False,
        rate_limit_per_sec=0, max_workers=1,
    )

    assert result.interrupted is True
    assert any(s.contact.value == f"info@{domain}" for s in result.sightings), (
        f"partial sighting was lost on Ctrl+C: {[s.contact.value for s in result.sightings]}"
    )


@responses.activate
def test_skips_probe_when_broad_scan_already_covered_html_pages():
    """When CDX runs without the URL filter (the "broad CDX scan" branch),
    a precise positive count + at least one CDX hit means the probe phase
    would only resurface non-HTML assets — skip it.

    Reproduces ``broad-scan-tiny.example`` (precise count = 10, CDX HTML/200
    returns 1). Without this guard the scanner spends ~5 min probing 30
    well-known paths × 5 timestamps for nothing.
    """
    domain = "broad-scan-tiny.example"
    contact_url = f"http://{domain}/contacts"

    def cdx_cb(request):
        if "showNumPages" in request.url:
            return (200, {}, "1")
        if "fl=urlkey" in request.url:
            # Precise count = 10 captures total.
            return (200, {}, json.dumps([["urlkey"]] + [
                ["ex,broad-scan-tiny)/x" + str(i)] for i in range(10)
            ]))
        # The main CDX query (HTML/200 + no urlkey filter, since the planner
        # picks the broad branch on a 10-capture site) returns just one row.
        return (200, {}, _cdx_body([
            ["ex,broad-scan-tiny)/contacts", "20200101000000", contact_url,
             "text/html", "200", "A", "1"],
        ]))

    responses.add_callback(responses.GET, CDX_ENDPOINT, callback=cdx_cb)
    responses.add(
        responses.GET,
        f"https://web.archive.org/web/20200101000000id_/{contact_url}",
        body="<html>info@broad-scan-tiny.example</html>",
        status=200, content_type="text/html",
    )
    # No /wayback/available mock — if the probe runs, a connection error
    # bubbles up and the test fails.

    result = scan_domain(
        domain, timeout_seconds=300, rate_limit_per_sec=0,
    )

    # The single CDX-returned page should still be fetched and its contact
    # extracted; only the well-known probe is skipped.
    emails = {s.contact.value for s in result.sightings if s.contact.kind == "email"}
    assert "info@broad-scan-tiny.example" in emails


@responses.activate
def test_skips_probe_when_precise_count_positive_but_cdx_filter_empties_it():
    """When the precise count says N>0 captures exist but the main CDX query
    (HTML+200 filter) returns zero, the well-known probe must be skipped.

    Reproduces non-html-only.example: 2 archived captures total, both
    non-HTML/non-200 — the pipeline previously spent 265s probing 150 paths
    × timestamps for guaranteed-nothing. Probe must be skipped and the scan
    must exit fast.
    """
    domain = "non-html-only.example"

    def cdx_cb(request):
        if "showNumPages" in request.url:
            return (200, {}, "1")
        if "fl=urlkey" in request.url:
            # Two captures total — precise count > 0, not the no_captures case.
            return (200, {}, json.dumps([
                ["urlkey"], ["ex,non-html-only)/robots.txt"],
                ["ex,non-html-only)/favicon.ico"],
            ]))
        # Main CDX query with HTML+200 filter — returns 0 rows.
        return (200, {}, _cdx_body([]))

    responses.add_callback(responses.GET, CDX_ENDPOINT, callback=cdx_cb)
    # No /wayback/available mock — any probe call would raise ConnectionError
    # and fail the test.

    result = scan_domain(domain, timeout_seconds=300, rate_limit_per_sec=0)

    assert result.sightings == []
    assert result.snapshots_fetched == 0
    # The scan must complete in well under the 300s budget.
    assert result.elapsed_seconds < 10.0, (
        f"probe was not short-circuited: elapsed={result.elapsed_seconds}s"
    )


def test_regions_for_domain_prepends_cctld_region():
    from kronikier.pipeline import _regions_for_domain

    # Need real-looking TLDs (the function keys off the last DNS label) but
    # the second-level label is a placeholder. None of these resolve.
    assert _regions_for_domain("placeholder.by", ("RU", "US")) == ("BY", "RU", "US")
    # Generic TLDs (.com etc.) map to US — overwhelmingly US-anchored in
    # practice; analyst can override with --regions when investigating a
    # .com domain that isn't.
    assert _regions_for_domain("example.com", ("RU", "US")) == ("US", "RU")
    assert _regions_for_domain("placeholder.kz", ("RU", "US")) == ("KZ", "RU", "US")
    # Dedupes when TLD region is already present
    assert _regions_for_domain("placeholder.ru", ("RU", "US")) == ("RU", "US")


def test_regions_for_domain_handles_popular_zones():
    """Spot-check the extended TLD map: a real-world domain in each zone
    should put the right country first.
    """
    from kronikier.pipeline import _regions_for_domain

    defaults = ("RU", "BY", "UA", "KZ", "US", "GB", "DE", "FR")
    cases = {
        # Anglosphere & Western Europe.
        "x.uk": "GB", "x.au": "AU", "x.ie": "IE",
        "x.de": "DE", "x.fr": "FR", "x.it": "IT", "x.es": "ES",
        "x.nl": "NL", "x.be": "BE", "x.ch": "CH", "x.at": "AT",
        # Nordics.
        "x.se": "SE", "x.fi": "FI", "x.no": "NO", "x.dk": "DK",
        # CIS.
        "x.kz": "KZ", "x.uz": "UZ", "x.am": "AM", "x.ge": "GE",
        # Asia.
        "x.jp": "JP", "x.cn": "CN", "x.kr": "KR", "x.sg": "SG",
        "x.in": "IN", "x.th": "TH",
        # Americas.
        "x.mx": "MX", "x.br": "BR", "x.ar": "AR",
        # Middle East.
        "x.tr": "TR", "x.ae": "AE", "x.il": "IL",
        # Generic — all anchored to US.
        "x.com": "US", "x.org": "US", "x.net": "US",
        "x.io": "US", "x.app": "US",
    }
    for domain, expected_first in cases.items():
        regions = _regions_for_domain(domain, defaults)
        assert regions[0] == expected_first, (
            f"{domain}: expected {expected_first} first, got {regions[0]} "
            f"in {regions}"
        )


@responses.activate
def test_timeline_is_sorted_by_timestamp():
    domain = "histcorp.com"
    responses.add(
        responses.GET,
        CDX_ENDPOINT,
        body=_cdx_body(
            [
                ["com,histcorp)/", "20200101000000", f"http://{domain}/", "text/html", "200", "A", "1"],
                ["com,histcorp)/contact", "20050101000000", f"http://{domain}/contact", "text/html", "200", "B", "1"],
            ]
        ),
        status=200,
    )
    responses.add(
        responses.GET,
        f"https://web.archive.org/web/20200101000000id_/http://{domain}/",
        body="<html>new@histcorp.com</html>",
        status=200,
        content_type="text/html",
    )
    responses.add(
        responses.GET,
        f"https://web.archive.org/web/20050101000000id_/http://{domain}/contact",
        body="<html>old@histcorp.com</html>",
        status=200,
        content_type="text/html",
    )

    result = scan_domain(
        domain,
        mode="exhaustive",
        probe_well_known=False,
        rate_limit_per_sec=0,
    )
    tl = result.timeline()
    # Old contact should appear before new contact
    assert tl[0][0] < tl[-1][0]
    values_in_order = [row[2] for row in tl]
    assert values_in_order.index("old@histcorp.com") < values_in_order.index("new@histcorp.com")


# ---------------------------------------------------------------------------
# Mode behavior
# ---------------------------------------------------------------------------


@responses.activate
def test_default_mode_passes_filter_and_limit_to_cdx():
    """Default timeout on a big site → contact-URL filter on, oversample limit.

    The capacity-based limit replaces the old fixed 5000 — we just assert
    that *some* limit and the filter both reached CDX.
    """
    # Mock showNumPages so the planner doesn't try to call it for real
    def cdx_cb(request):
        if "showNumPages" in request.url:
            return (200, {}, "350")  # big site → filter stays on
        return (200, {}, _cdx_body([]))

    responses.add_callback(responses.GET, CDX_ENDPOINT, callback=cdx_cb)
    responses.add(
        responses.GET,
        "https://archive.org/wayback/available",
        json={"archived_snapshots": {}},
        status=200,
    )

    scan_domain(
        "example.com",
        timeout_seconds=120.0,
        probe_well_known=False,
        rate_limit_per_sec=0,
        no_escalate=True,
    )

    scan_calls = [
        c for c in responses.calls
        if c.request.url.startswith(CDX_ENDPOINT)
        and "showNumPages" not in c.request.url
        and "fl=urlkey" not in c.request.url
    ]
    assert len(scan_calls) == 1
    sent_url = scan_calls[0].request.url
    assert "filter=urlkey%3A" in sent_url or "filter=urlkey:" in sent_url
    assert "limit=" in sent_url


@responses.activate
def test_cdx_timeout_is_passed_through(monkeypatch):
    """Verify the --cdx-timeout flag actually reaches query_domain.

    We monkeypatch query_domain to capture the timeout argument.
    """
    captured: dict[str, int] = {}

    def fake_query_domain(domain, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        return iter([])  # empty result

    import kronikier.pipeline as pipeline
    monkeypatch.setattr(pipeline, "query_domain", fake_query_domain)
    responses.add(
        responses.GET, "https://archive.org/wayback/available",
        json={"archived_snapshots": {}}, status=200,
    )

    scan_domain(
        "ex.com", mode="default", probe_well_known=False,
        rate_limit_per_sec=0, cdx_timeout=600,
    )
    assert captured["timeout"] == 600


@responses.activate
def test_longer_timeout_yields_larger_capacity():
    """A larger timeout computes a larger capacity → larger CDX limit.

    The exact number is capacity-dependent (capacity*20 oversample), but it
    must scale with the timeout.
    """
    def cdx_cb(request):
        if "showNumPages" in request.url:
            return (200, {}, "350")
        return (200, {}, _cdx_body([]))

    responses.add_callback(responses.GET, CDX_ENDPOINT, callback=cdx_cb)
    responses.add(
        responses.GET,
        "https://archive.org/wayback/available",
        json={"archived_snapshots": {}},
        status=200,
    )

    import re

    def _limit_for_timeout(b):
        responses.calls.reset()
        scan_domain("example.com", timeout_seconds=b, probe_well_known=False,
                    rate_limit_per_sec=0, no_escalate=True)
        scan_calls = [
            c for c in responses.calls
            if c.request.url.startswith(CDX_ENDPOINT)
            and "showNumPages" not in c.request.url
        and "fl=urlkey" not in c.request.url
        ]
        m = re.search(r"[?&]limit=(\d+)", scan_calls[0].request.url)
        return int(m.group(1)) if m else 0

    small = _limit_for_timeout(120.0)
    big = _limit_for_timeout(600.0)
    assert big >= small  # 600s timeout → at least as much CDX reach


@responses.activate
def test_scan_result_records_plan_metadata():
    """Plan rationale, url_filter_active, timeout_seconds, elapsed_seconds present."""
    def cdx_cb(request):
        if "showNumPages" in request.url:
            return (200, {}, "350")
        return (200, {}, _cdx_body([]))

    responses.add_callback(responses.GET, CDX_ENDPOINT, callback=cdx_cb)
    responses.add(
        responses.GET, "https://archive.org/wayback/available",
        json={"archived_snapshots": {}}, status=200,
    )

    r = scan_domain("ex.com", timeout_seconds=120, probe_well_known=False,
                    rate_limit_per_sec=0, no_escalate=True)
    assert r.timeout_seconds == 120.0
    assert r.elapsed_seconds >= 0
    assert r.url_filter_active is True
    assert "filter" in r.plan_rationale.lower()

    r = scan_domain("ex.com", timeout_seconds=0, force_all=True,
                    probe_well_known=False, rate_limit_per_sec=0,
                    no_escalate=True)
    assert r.timeout_seconds == 0.0
    assert r.url_filter_active is False
    # timeout=0 → back-compat name resolves to "exhaustive" (the named alias)
    assert r.resolved_mode == "exhaustive"


@responses.activate
def test_filtered_scan_pushes_urlkey_regex_to_cdx():
    """A filtered plan must push the contact-slug regex server-side."""
    def cdx_cb(request):
        if "showNumPages" in request.url:
            return (200, {}, "350")
        return (200, {}, _cdx_body([]))

    responses.add_callback(responses.GET, CDX_ENDPOINT, callback=cdx_cb)
    responses.add(
        responses.GET,
        "https://archive.org/wayback/available",
        json={"archived_snapshots": {}},
        status=200,
    )

    scan_domain("example.com", timeout_seconds=600, probe_well_known=False,
                rate_limit_per_sec=0, no_escalate=True)

    scan_calls = [
        c for c in responses.calls
        if c.request.url.startswith(CDX_ENDPOINT)
        and "showNumPages" not in c.request.url
        and "fl=urlkey" not in c.request.url
    ]
    assert len(scan_calls) == 1
    sent_url = scan_calls[0].request.url
    assert "filter=urlkey%3A" in sent_url or "filter=urlkey:" in sent_url
    assert "contact" in sent_url


@responses.activate
def test_exhaustive_alias_disables_server_side_collapse():
    """`--exhaustive` (and the equivalent `timeout=0, force_all=True`) must NOT
    send ``collapse=`` to CDX, otherwise IA returns one snapshot per URL.
    This was a real bug found on small-ru.example.
    """
    def cdx_cb(request):
        if "showNumPages" in request.url:
            return (200, {}, "1")
        if "fl=urlkey" in request.url:
            # Non-empty so count_captures doesn't trigger the "no captures"
            # short-circuit before the main scan call we're asserting on.
            return (200, {}, json.dumps([["urlkey"], ["ex,com/)/"]]))
        return (200, {}, _cdx_body([]))

    responses.add_callback(responses.GET, CDX_ENDPOINT, callback=cdx_cb)
    responses.add(
        responses.GET, "https://archive.org/wayback/available",
        json={"archived_snapshots": {}}, status=200,
    )
    scan_domain(
        "ex.com", mode="exhaustive",
        probe_well_known=False, rate_limit_per_sec=0,
    )
    scan_calls = [
        c for c in responses.calls
        if c.request.url.startswith(CDX_ENDPOINT)
        and "showNumPages" not in c.request.url
        and "fl=urlkey" not in c.request.url
    ]
    assert len(scan_calls) == 1
    sent = scan_calls[0].request.url
    assert "collapse=" not in sent, f"Unexpected collapse in CDX URL: {sent}"


@responses.activate
def test_all_flag_disables_url_filter():
    """`force_all=True` (CLI: --all) → no urlkey filter on the CDX call."""
    def cdx_cb(request):
        if "showNumPages" in request.url:
            return (200, {}, "350")
        return (200, {}, _cdx_body([]))

    responses.add_callback(responses.GET, CDX_ENDPOINT, callback=cdx_cb)
    responses.add(
        responses.GET, "https://archive.org/wayback/available",
        json={"archived_snapshots": {}}, status=200,
    )

    scan_domain(
        "example.com", timeout_seconds=120, force_all=True,
        probe_well_known=False, rate_limit_per_sec=0,
    )

    scan_calls = [
        c for c in responses.calls
        if c.request.url.startswith(CDX_ENDPOINT)
        and "showNumPages" not in c.request.url
        and "fl=urlkey" not in c.request.url
    ]
    assert len(scan_calls) == 1
    sent_url = scan_calls[0].request.url
    assert "filter=urlkey%3A" not in sent_url
    assert "filter=urlkey:" not in sent_url


def test_unknown_mode_raises():
    import pytest

    with pytest.raises(ValueError, match="mode must be one of"):
        scan_domain("example.com", mode="banana")


@responses.activate
def test_default_mode_streams_probed_pages_into_extractor():
    """In default mode, snapshots discovered via probing must be fetched and
    parsed *inline* — without the caller having to opt into a second stage.
    """
    import json as _json
    from urllib.parse import parse_qs, urlparse

    domain = "stream-demo.example"
    # CDX returns nothing — we want to verify probe → fetch → extract still works.
    responses.add(responses.GET, CDX_ENDPOINT, body=_cdx_body([]), status=200)
    # We respond "archived" for one specific path (/contacts) and "empty" for
    # everything else. The first matching probe yields a snapshot which the
    # streaming pipeline must fetch and parse.
    contact_path = f"http://{domain}/contacts"

    def availability_cb(request):
        qs = parse_qs(urlparse(request.url).query)
        queried = qs.get("url", [""])[0]
        if queried == contact_path:
            body = {
                "url": contact_path,
                "archived_snapshots": {
                    "closest": {
                        "available": True,
                        "url": f"http://web.archive.org/web/20100101000000/{contact_path}",
                        "timestamp": "20100101000000",
                        "status": "200",
                    }
                },
            }
        else:
            body = {"archived_snapshots": {}}
        return (200, {}, _json.dumps(body))

    responses.add_callback(
        responses.GET,
        "https://archive.org/wayback/available",
        callback=availability_cb,
    )

    # Fetch mock for the discovered snapshot.
    responses.add(
        responses.GET,
        f"https://web.archive.org/web/20100101000000id_/{contact_path}",
        body=f"<html>Email: info@{domain}</html>",
        status=200,
        content_type="text/html",
    )

    result = scan_domain(
        domain,
        mode="default",
        max_snapshots=5,
        max_workers=1,
        rate_limit_per_sec=0,
    )

    emails = {s.contact.value for s in result.sightings if s.contact.kind == "email"}
    assert f"info@{domain}" in emails, (
        f"streaming probe→fetch→extract failed; got {emails}"
    )
    # Exactly one probe matched, so exactly one fetch should have happened
    # against multiple probe timestamps (one per `probe_timestamps` value).
    assert result.snapshots_fetched >= 1


# ---------------------------------------------------------------------------
# Producer-consumer interleaving: probe and fetch must overlap in time
# ---------------------------------------------------------------------------


def test_probe_and_fetch_run_concurrently_in_default_mode(monkeypatch):
    """Strong test: the fetch+extract step must complete BEFORE probing finishes.

    Bypasses ``responses`` (which serializes mocked HTTP calls behind a single
    lock and would deadlock this test) — instead we monkeypatch the two real
    functions that the streaming pipeline calls (``closest_snapshot`` and
    ``_fetch_one``), so the threads in the producer/fetcher run truly
    independently.

    Setup:
      - First probe call returns a snapshot.
      - All later probe calls block on a threading.Event until a fetch fires.
      - The fetch sets that event.

    If probing and fetching are NOT interleaved, the fetch never gets a chance
    to run (it's behind the still-running probe), the event never fires, and
    the later probes time out. With true interleaving, the first snapshot
    flows through the queue → into the fetch pool → fires the event → later
    probes unblock.
    """
    import threading

    from kronikier.cdx import Snapshot
    from kronikier.fetcher import FetchedPage

    domain = "interleave-demo.example"

    fetch_happened = threading.Event()
    later_probes_saw_fetch = threading.Event()
    probe_calls = {"n": 0}
    probe_lock = threading.Lock()

    def fake_closest_snapshot(url, *, timestamp="20100101", session=None, timeout=30):
        with probe_lock:
            probe_calls["n"] += 1
            n = probe_calls["n"]
        if n == 1:
            return Snapshot(
                timestamp="20100101000000",
                original=url,
                mimetype="text/html",
                status="200",
                urlkey="",
            )
        # Wait briefly for the fetch to start running concurrently.
        if fetch_happened.wait(timeout=3.0):
            later_probes_saw_fetch.set()
        return None

    def fake_fetch_one(snap, session, timeout, retries):
        fetch_happened.set()
        return FetchedPage(snap, 200, f"<html>Email: info@{domain}</html>")

    monkeypatch.setattr("kronikier.pipeline.closest_snapshot", fake_closest_snapshot)
    monkeypatch.setattr("kronikier.fetcher._fetch_one", fake_fetch_one)
    # Pin planner preflight to "unknown size" so it doesn't short-circuit on
    # the (real) zero-capture response for ``interleave-demo.example``.
    monkeypatch.setattr("kronikier.planner.show_num_pages", lambda *a, **k: None)

    result = scan_domain(
        domain,
        mode="default",
        max_snapshots=2,
        max_workers=2,
        rate_limit_per_sec=0,
    )

    assert fetch_happened.is_set(), "fetch never ran — pipeline didn't reach fetch stage"
    assert later_probes_saw_fetch.is_set(), (
        "probe and fetch are not interleaving — later probes finished before fetch fired"
    )
    emails = {s.contact.value for s in result.sightings if s.contact.kind == "email"}
    assert f"info@{domain}" in emails


# ---------------------------------------------------------------------------
# Auto mode
# ---------------------------------------------------------------------------


@responses.activate
def test_planner_keeps_filter_on_giant_site():
    """Default timeout + giant site → planner keeps URL filter on the CDX call."""
    domain = "avito.example"
    contact_url = f"http://{domain}/contact"

    def cdx_cb(request):
        if "showNumPages" in request.url:
            return (200, {}, "350")  # avito-class → estimated 17.5M snapshots
        return (200, {}, _cdx_body([[
            "com,avito)/contact", "20180101120000", contact_url,
            "text/html", "200", "X", "1",
        ]]))

    responses.add_callback(responses.GET, CDX_ENDPOINT, callback=cdx_cb)
    responses.add(
        responses.GET, "https://archive.org/wayback/available",
        json={"archived_snapshots": {}}, status=200,
    )
    responses.add(
        responses.GET,
        f"https://web.archive.org/web/20180101120000id_/{contact_url}",
        body=f"<html>info@{domain}</html>",
        status=200, content_type="text/html",
    )

    result = scan_domain(
        domain, timeout_seconds=120, probe_well_known=False, rate_limit_per_sec=0,
    )
    assert result.url_filter_active is True

    scan_calls = [
        c for c in responses.calls
        if c.request.url.startswith(CDX_ENDPOINT)
        and "showNumPages" not in c.request.url
        and "fl=urlkey" not in c.request.url
    ]
    assert len(scan_calls) == 1
    sent = scan_calls[0].request.url
    assert "filter=urlkey%3A" in sent or "filter=urlkey:" in sent


@responses.activate
def test_escalation_broadens_filter_when_first_pass_empty():
    """Zero contacts + filter on → second pass drops the filter and re-runs.

    Escalation fires on empty sightings (not on timeout exhaustion), so we
    use a normal timeout — the first pass returns an empty filtered CDX,
    then the broadened pass fetches the seeded snapshot.
    """
    domain = "needs-all.example"
    contact_url = f"http://{domain}/random-page"

    def cdx_cb(request):
        url = request.url
        if "showNumPages" in url:
            return (200, {}, "350")
        if "filter=urlkey" in url:
            # Filtered scan returns nothing → escalation should drop the filter
            return (200, {}, _cdx_body([]))
        # Second pass (no filter) returns a contact
        return (200, {}, _cdx_body([[
            "com,needs-all)/random-page", "20180101120000",
            contact_url, "text/html", "200", "X", "1",
        ]]))

    responses.add_callback(responses.GET, CDX_ENDPOINT, callback=cdx_cb)
    responses.add(
        responses.GET, "https://archive.org/wayback/available",
        json={"archived_snapshots": {}}, status=200,
    )
    responses.add(
        responses.GET,
        f"https://web.archive.org/web/20180101120000id_/{contact_url}",
        body=f"<html>Email: hello@{domain}</html>",
        status=200, content_type="text/html",
    )

    result = scan_domain(
        domain, timeout_seconds=5.0,
        probe_well_known=False, rate_limit_per_sec=0,
    )

    emails = {s.contact.value for s in result.sightings if s.contact.kind == "email"}
    assert f"hello@{domain}" in emails, (
        f"escalation didn't broaden — sightings: {result.sightings}"
    )
    # After escalation, filter should be off
    assert result.url_filter_active is False


@responses.activate
def test_no_escalate_flag_disables_retry():
    """`no_escalate=True` (--no-escalate) suppresses zero-result broadening."""
    def cdx_cb(request):
        if "showNumPages" in request.url:
            return (200, {}, "350")
        return (200, {}, _cdx_body([]))

    responses.add_callback(responses.GET, CDX_ENDPOINT, callback=cdx_cb)
    responses.add(
        responses.GET, "https://archive.org/wayback/available",
        json={"archived_snapshots": {}}, status=200,
    )

    result = scan_domain(
        "no-retry.example", timeout_seconds=0.001, no_escalate=True,
        probe_well_known=False, rate_limit_per_sec=0,
    )

    assert result.sightings == []
    scan_calls = [
        c for c in responses.calls
        if c.request.url.startswith(CDX_ENDPOINT)
        and "showNumPages" not in c.request.url
        and "fl=urlkey" not in c.request.url
    ]
    assert len(scan_calls) == 1, "escalation should be suppressed by no_escalate"


@responses.activate
def test_auto_mode_handles_failed_sizing_probe():
    """If show_num_pages fails (or returns 500), we still proceed safely."""
    responses.add(responses.GET, CDX_ENDPOINT, status=500)
    responses.add(
        responses.GET, "https://archive.org/wayback/available",
        json={"archived_snapshots": {}}, status=200,
    )

    result = scan_domain("broken.example", timeout_seconds=120, rate_limit_per_sec=0)
    # CDX failure surfaces in errors[] (not raised). No crash.
    assert any("CDX query failed" in e for e in result.errors)
