"""Client for the web.archive.org CDX (Capture inDeX) API.

Docs: https://github.com/internetarchive/wayback/tree/master/wayback-cdx-server

We use it to enumerate every snapshot URL the IA has for a given domain. The
two practically important knobs are:

- ``matchType=domain`` — include subdomains.
- ``collapse=urlkey`` — at most one row per URL (we don't need every snapshot
  of the same page; the pipeline picks one representative snapshot per URL).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterator

import requests

# Playback URL shapes:
#   http(s)://web.archive.org/web/<14-digit-ts>[<id_>]/<source-url>
_PLAYBACK_URL_RE = re.compile(
    r"^https?://web\.archive\.org/web/\d{14}[a-z_]*/(.+)$",
    re.IGNORECASE,
)


def _source_from_playback(playback_url: str) -> str | None:
    """Extract the underlying source URL from an IA playback URL, or None."""
    if not playback_url:
        return None
    m = _PLAYBACK_URL_RE.match(playback_url)
    return m.group(1) if m else None

CDX_ENDPOINT = "https://web.archive.org/cdx/search/cdx"

#: Timeout for *cheap* CDX meta-calls (showNumPages, availability lookup).
DEFAULT_TIMEOUT = 30

#: Timeout for the main CDX index query. On giant domains (marketplace-class with
#: 5000+ index pages) IA's server-side filter+limit scan can take minutes
#: before the first byte arrives. 5 minutes covers all real-world cases seen
#: so far; can be overridden via ``query_domain(timeout=…)``.
CDX_QUERY_TIMEOUT = 300

DEFAULT_USER_AGENT = (
    "kronikier/0.1 (+https://github.com/soxoj/kronikier)"
)


@dataclass(frozen=True)
class Snapshot:
    """One archived capture row from the CDX index."""

    timestamp: str  # YYYYMMDDhhmmss
    original: str  # original URL as captured
    mimetype: str
    status: str
    urlkey: str

    @property
    def year(self) -> int:
        return int(self.timestamp[:4])

    def archive_url(self, raw: bool = True) -> str:
        """Build the wayback playback URL.

        ``raw=True`` uses the ``id_`` modifier which serves the captured bytes
        unmodified — no rewriting, no IA banner, no JS injection. Required for
        clean parsing.
        """
        modifier = "id_" if raw else ""
        return f"https://web.archive.org/web/{self.timestamp}{modifier}/{self.original}"


def query_domain(
    domain: str,
    *,
    limit: int | None = None,
    from_year: int | None = None,
    to_year: int | None = None,
    include_subdomains: bool = True,
    only_html: bool = True,
    only_ok: bool = True,
    collapse: str | None = "urlkey",
    urlkey_filter: str | None = None,
    session: requests.Session | None = None,
    timeout: int = CDX_QUERY_TIMEOUT,
) -> Iterator[Snapshot]:
    """Stream Snapshot rows for ``domain`` from the CDX API.

    The API can return tens of thousands of rows; we stream them line-by-line
    so the caller can early-exit (e.g. after collecting the top-N best URLs).
    """
    params: dict[str, str | int] = {
        "url": domain,
        "output": "json",
        "matchType": "domain" if include_subdomains else "exact",
    }
    if only_ok:
        params["filter"] = "statuscode:200"
    if collapse:
        params["collapse"] = collapse
    if from_year:
        params["from"] = f"{from_year}0101000000"
    if to_year:
        params["to"] = f"{to_year}1231235959"
    if limit:
        params["limit"] = limit

    # CDX accepts repeated `filter` params. requests' params dict won't repeat
    # a key, so build a list of tuples when we need multiple filters.
    params_list: list[tuple[str, str | int]] = list(params.items())
    if only_html:
        params_list.append(("filter", "mimetype:text/html"))
    if urlkey_filter:
        # Server-side regex on the ``urlkey`` field — collapses CDX response
        # from millions of rows (marketplace-scale) to a few hundred.
        params_list.append(("filter", f"urlkey:{urlkey_filter}"))

    sess = session or requests.Session()
    sess.headers.setdefault("User-Agent", DEFAULT_USER_AGENT)

    import json

    with sess.get(CDX_ENDPOINT, params=params_list, timeout=timeout, stream=True) as resp:
        resp.raise_for_status()
        rows = _parse_cdx_rows(resp.iter_lines(decode_unicode=True))
        header: list[str] | None = None
        for row in rows:
            if header is None:
                header = row
                continue
            if len(row) != len(header):
                continue
            data = dict(zip(header, row))
            try:
                yield Snapshot(
                    timestamp=data["timestamp"],
                    original=data["original"],
                    mimetype=data.get("mimetype", ""),
                    status=data.get("statuscode", ""),
                    urlkey=data.get("urlkey", ""),
                )
            except KeyError:
                continue


def _parse_cdx_rows(line_iter):
    """Yield row-lists from CDX JSON output.

    Real CDX serves the response as one row per line, but the first line
    starts with ``[[`` (opening array + first row) and the last line ends with
    ``]]``. We try two strategies:

    1. Buffer all lines (separated by newlines so they remain parseable as a
       single JSON document) and ``json.loads`` the whole thing. This is the
       happy path against the real CDX server.
    2. If full-body parse fails (truncated response, embedded garbage), fall
       back to line-by-line peeling of outer brackets.
    """
    import json

    lines = [part for part in line_iter if part is not None]
    body = "\n".join(lines).strip()
    if not body:
        return

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        for raw_line in lines:
            stripped = raw_line.strip().rstrip(",")
            # Peel a leading "[" if it doesn't have a matching "]" inside the
            # line (handles the "[[…]," opener).
            if stripped.startswith("[[") and not stripped.endswith("]]"):
                stripped = stripped[1:]
            # Peel a trailing "]" if balanced is off the other way (handles
            # the "…]]" closer).
            if stripped.endswith("]]") and not stripped.startswith("[["):
                stripped = stripped[:-1]
            if stripped in ("[", "]", ""):
                continue
            try:
                row = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(row, list):
                yield row
        return

    if isinstance(parsed, list):
        for row in parsed:
            if isinstance(row, list):
                yield row


def count_captures(
    domain: str,
    *,
    include_subdomains: bool = True,
    session: requests.Session | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> int | None:
    """Return the **exact** number of captures CDX has for ``domain``.

    Uses ``fl=urlkey`` so each row carries only one field — minimal payload.
    Still O(N) HTTP bytes in the total capture count, so callers must gate
    by :func:`show_num_pages` first (only worth running for ``pages ≤ 2``
    sites, where ``~50 000 × pages`` is a wildly loose ceiling and a precise
    count flips the planner from "filter on" to "scan everything").

    Returns ``None`` on any HTTP / JSON failure — callers should treat that
    as "fall back to the page-count estimate".
    """
    sess = session or requests.Session()
    sess.headers.setdefault("User-Agent", DEFAULT_USER_AGENT)
    try:
        resp = sess.get(
            CDX_ENDPOINT,
            params={
                "url": domain,
                "matchType": "domain" if include_subdomains else "exact",
                "output": "json",
                "fl": "urlkey",
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        import json

        payload = json.loads(resp.text)
    except (requests.RequestException, ValueError):
        return None
    if not isinstance(payload, list) or not payload:
        return 0
    # CDX prefixes the array with a header row (``[["urlkey"], ...]``).
    return max(0, len(payload) - 1)


def show_num_pages(
    domain: str,
    *,
    include_subdomains: bool = True,
    session: requests.Session | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> int | None:
    """Ask CDX how many index pages the domain would produce.

    CDX serves results in "pages" of roughly 500k records each. This is a
    cheap meta-query (a single HTTP call returning a single integer) that
    lets us size the domain *before* deciding which scan mode to run.
    Returns ``None`` if the call fails — callers should treat that as
    "unknown, pick a safe default".
    """
    sess = session or requests.Session()
    sess.headers.setdefault("User-Agent", DEFAULT_USER_AGENT)
    try:
        resp = sess.get(
            CDX_ENDPOINT,
            params={
                "url": domain,
                "matchType": "domain" if include_subdomains else "exact",
                "showNumPages": "true",
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        return int(resp.text.strip())
    except (requests.RequestException, ValueError):
        return None


def closest_snapshot(
    url: str,
    *,
    timestamp: str = "20100101",
    session: requests.Session | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> Snapshot | None:
    """Ask the wayback availability API for the snapshot of ``url`` closest to ``timestamp``.

    Used for "probe well-known paths" when CDX domain scan misses them.
    """
    sess = session or requests.Session()
    sess.headers.setdefault("User-Agent", DEFAULT_USER_AGENT)
    resp = sess.get(
        "https://archive.org/wayback/available",
        params={"url": url, "timestamp": timestamp},
        timeout=timeout,
    )
    resp.raise_for_status()
    payload = resp.json()
    snap = (payload.get("archived_snapshots") or {}).get("closest")
    if not snap or not snap.get("available"):
        return None
    # ``snap["url"]`` is the *playback* URL (``http://web.archive.org/web/<ts>/<src>``),
    # not the source URL. Extract the embedded source URL — it may differ from
    # what we queried (e.g. IA redirected http → https on capture), and using
    # the *actual* archived URL is what makes ``id_`` playback succeed.
    actual_source = _source_from_playback(snap.get("url", "")) or url
    return Snapshot(
        timestamp=snap["timestamp"],
        original=actual_source,
        mimetype="text/html",
        status=str(snap.get("status", "200")),
        urlkey="",
    )
