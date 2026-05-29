"""Unit tests for the CDX API client (network mocked with `responses`)."""

from __future__ import annotations

import json

import responses

from kronieker.cdx import (
    CDX_ENDPOINT,
    Snapshot,
    closest_snapshot,
    query_domain,
    show_num_pages,
)


@responses.activate
def test_query_domain_parses_streaming_json():
    body = json.dumps(
        [
            ["urlkey", "timestamp", "original", "mimetype", "statuscode", "digest", "length"],
            ["ru,romashka)/", "20070101120000", "http://romashka.ru/", "text/html", "200", "X", "100"],
            ["ru,romashka)/contacts", "20081231120000", "http://romashka.ru/contacts.html", "text/html", "200", "Y", "200"],
        ]
    )
    responses.add(responses.GET, CDX_ENDPOINT, body=body, status=200, content_type="application/json")

    snaps = list(query_domain("romashka.ru"))
    assert len(snaps) == 2
    assert snaps[0].timestamp == "20070101120000"
    assert snaps[0].original == "http://romashka.ru/"
    assert snaps[1].original == "http://romashka.ru/contacts.html"
    assert snaps[1].year == 2008


@responses.activate
def test_query_domain_includes_filters_in_request():
    body = json.dumps([["urlkey", "timestamp", "original", "mimetype", "statuscode", "digest", "length"]])
    responses.add(responses.GET, CDX_ENDPOINT, body=body, status=200)

    list(query_domain("example.com", from_year=2005, to_year=2010))

    sent = responses.calls[0].request
    assert "matchType=domain" in sent.url
    assert "filter=statuscode%3A200" in sent.url or "filter=statuscode:200" in sent.url
    assert "filter=mimetype%3Atext%2Fhtml" in sent.url or "filter=mimetype:text/html" in sent.url
    assert "from=20050101000000" in sent.url
    assert "to=20101231235959" in sent.url
    assert "collapse=urlkey" in sent.url


@responses.activate
def test_query_domain_passes_urlkey_filter():
    body = json.dumps([["urlkey", "timestamp", "original", "mimetype", "statuscode", "digest", "length"]])
    responses.add(responses.GET, CDX_ENDPOINT, body=body, status=200)

    list(query_domain("example.com", urlkey_filter=".*(contact|about).*"))

    sent_url = responses.calls[0].request.url
    assert "filter=urlkey%3A" in sent_url or "filter=urlkey:" in sent_url
    assert "contact" in sent_url


def test_snapshot_archive_url_raw_uses_id_modifier():
    s = Snapshot(
        timestamp="20070101120000",
        original="http://example.com/contact.html",
        mimetype="text/html",
        status="200",
        urlkey="com,example)/contact.html",
    )
    assert (
        s.archive_url(raw=True)
        == "https://web.archive.org/web/20070101120000id_/http://example.com/contact.html"
    )
    assert (
        s.archive_url(raw=False)
        == "https://web.archive.org/web/20070101120000/http://example.com/contact.html"
    )


@responses.activate
def test_closest_snapshot_hits_availability_api():
    responses.add(
        responses.GET,
        "https://archive.org/wayback/available",
        json={
            "url": "http://example.com/contact",
            "archived_snapshots": {
                "closest": {
                    "available": True,
                    "url": "http://web.archive.org/web/20080101120000/http://example.com/contact",
                    "timestamp": "20080101120000",
                    "status": "200",
                }
            },
        },
        status=200,
    )

    snap = closest_snapshot("http://example.com/contact", timestamp="20080101")
    assert snap is not None
    assert snap.timestamp == "20080101120000"
    assert snap.status == "200"
    # snap.original must be the SOURCE URL, not the playback URL
    assert snap.original == "http://example.com/contact"


@responses.activate
def test_closest_snapshot_extracts_actual_archived_url_from_playback():
    """If IA redirected http → https on capture, the playback URL embeds the
    https variant; we must use *that* as ``original`` so id_ playback succeeds.
    """
    responses.add(
        responses.GET,
        "https://archive.org/wayback/available",
        json={
            "url": "http://tomhunter.ru/contacts",
            "archived_snapshots": {
                "closest": {
                    "available": True,
                    "url": "http://web.archive.org/web/20220625004541/https://tomhunter.ru/contacts",
                    "timestamp": "20220625004541",
                    "status": "200",
                }
            },
        },
        status=200,
    )

    snap = closest_snapshot("http://tomhunter.ru/contacts", timestamp="20220101")
    assert snap is not None
    # Must use the https variant from the playback URL — not the http we queried
    assert snap.original == "https://tomhunter.ru/contacts"
    # id_ playback URL is built from that source
    assert snap.archive_url(raw=True) == (
        "https://web.archive.org/web/20220625004541id_/https://tomhunter.ru/contacts"
    )


@responses.activate
def test_closest_snapshot_returns_none_when_no_capture():
    responses.add(
        responses.GET,
        "https://archive.org/wayback/available",
        json={"url": "http://example.com/", "archived_snapshots": {}},
        status=200,
    )
    assert closest_snapshot("http://example.com/") is None


@responses.activate
def test_show_num_pages_parses_integer():
    responses.add(responses.GET, CDX_ENDPOINT, body="42\n", status=200)
    assert show_num_pages("example.com") == 42


@responses.activate
def test_show_num_pages_returns_none_on_http_error():
    responses.add(responses.GET, CDX_ENDPOINT, status=500)
    assert show_num_pages("example.com") is None


@responses.activate
def test_show_num_pages_returns_none_on_non_integer_body():
    responses.add(responses.GET, CDX_ENDPOINT, body="not a number", status=200)
    assert show_num_pages("example.com") is None


@responses.activate
def test_show_num_pages_sends_correct_params():
    responses.add(responses.GET, CDX_ENDPOINT, body="3", status=200)
    show_num_pages("example.com", include_subdomains=True)
    sent = responses.calls[0].request.url
    assert "matchType=domain" in sent
    assert "showNumPages=true" in sent
    assert "url=example.com" in sent


@responses.activate
def test_query_domain_skips_malformed_rows():
    body = (
        "[\n"
        '["urlkey","timestamp","original","mimetype","statuscode","digest","length"],\n'
        "garbage line that is not json,\n"
        '["ru,r)/","20100101000000","http://r.ru/","text/html","200","Z","1"]\n'
        "]"
    )
    responses.add(responses.GET, CDX_ENDPOINT, body=body, status=200)
    snaps = list(query_domain("r.ru"))
    assert len(snaps) == 1
    assert snaps[0].original == "http://r.ru/"
