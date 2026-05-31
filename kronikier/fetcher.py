"""Polite, parallel fetcher for archived snapshots.

We use the ``id_`` playback modifier (see :meth:`Snapshot.archive_url`) so the
response is the original captured bytes with no IA rewriting. That keeps the
extractors honest — they see exactly the HTML the site served at capture time.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterable, Iterator

import requests

from kronikier.cdx import DEFAULT_USER_AGENT, Snapshot

if TYPE_CHECKING:
    from kronikier.cache import SnapshotCache

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class FetchedPage:
    snapshot: Snapshot
    status: int
    content: str
    error: str | None = None


#: Status codes worth retrying when talking to web.archive.org. 404 is
#: included because IA's playback endpoint sometimes serves transient 404s
#: under load (the snapshot exists and can be rendered later) — confirmed
#: empirically on real Russian small-business domains.
_RETRYABLE_STATUSES = frozenset({404, 408, 429, 500, 502, 503, 504})


def _fetch_one(
    snap: Snapshot,
    session: requests.Session,
    timeout: int,
    retries: int,
) -> FetchedPage:
    url = snap.archive_url(raw=True)
    log.debug("→ fetch start: %s", url)
    t0 = time.monotonic()
    result: FetchedPage | None = None
    try:
        result = _fetch_one_attempts(snap, url, session, timeout, retries)
        return result
    finally:
        elapsed = time.monotonic() - t0
        if result is None:
            log.debug("← fetch raised after %.2fs: %s", elapsed, url)
        else:
            tail = f" err={result.error}" if result.error else ""
            log.debug(
                "← fetch done in %.2fs (status=%s%s): %s",
                elapsed, result.status, tail, url,
            )


def _fetch_one_attempts(
    snap: Snapshot,
    url: str,
    session: requests.Session,
    timeout: int,
    retries: int,
) -> FetchedPage:
    last_err: str | None = None
    for attempt in range(retries + 1):
        try:
            resp = session.get(url, timeout=timeout, allow_redirects=True)
            if resp.status_code == 200:
                ctype = resp.headers.get("Content-Type", "").lower()
                if ctype and "html" not in ctype and "text" not in ctype:
                    return FetchedPage(snap, resp.status_code, "", error=f"non-html: {ctype}")
                return FetchedPage(snap, resp.status_code, resp.text)
            last_err = f"HTTP {resp.status_code}"
            if resp.status_code in _RETRYABLE_STATUSES and attempt < retries:
                # 404 from IA is often transient — back off a bit longer than
                # for explicit rate-limiting responses.
                backoff = 3.0 if resp.status_code == 404 else 1.5
                time.sleep(backoff * (attempt + 1))
                continue
            return FetchedPage(snap, resp.status_code, "", error=last_err)
        except requests.RequestException as e:
            last_err = f"{type(e).__name__}: {e}"
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
    return FetchedPage(snap, 0, "", error=last_err)


def fetch_snapshots(
    snapshots: Iterable[Snapshot],
    *,
    session: requests.Session | None = None,
    max_workers: int = 4,
    timeout: int = 30,
    retries: int = 3,
    rate_limit_per_sec: float = 4.0,
    deadline: float | None = None,
    cache: "SnapshotCache | None" = None,
) -> Iterator[FetchedPage]:
    """Fetch snapshots in parallel with a global rate limit.

    The input is consumed **lazily** — pass a generator that produces
    snapshots over time (e.g. from a slow probing stage) and they will be
    submitted to the fetch pool as soon as they arrive. Results are yielded
    as each fetch completes (not in input order).

    Parameters
    ----------
    deadline:
        Absolute :func:`time.monotonic` value past which no new fetches are
        submitted. Already-in-flight requests are allowed to finish — we
        don't cancel mid-``requests.get`` because that leaves half-parsed
        responses and triggers spurious retries. ``None`` (default) means
        "no deadline".
    cache:
        Optional :class:`~kronikier.cache.SnapshotCache`. When provided we
        check it before issuing the HTTP request: hits are yielded
        synchronously (no executor, no rate-limit token, no IA bytes spent),
        misses go through the worker pool as usual and their successful
        results are written back to the cache.
    """
    sess = session or requests.Session()
    sess.headers.setdefault("User-Agent", DEFAULT_USER_AGENT)

    min_interval = 1.0 / rate_limit_per_sec if rate_limit_per_sec > 0 else 0.0
    last_dispatch = [0.0]  # mutable closure cell

    def _fetch_and_cache(snap: Snapshot) -> FetchedPage:
        # Worker-side helper: do the real HTTP fetch, then persist to cache
        # if it was a clean success. Persisting from the worker keeps the
        # cache write off the main thread and parallel with other fetches.
        page = _fetch_one(snap, sess, timeout, retries)
        if cache is not None:
            cache.put(page)
        return page

    def submit_with_throttle(executor: ThreadPoolExecutor, snap: Snapshot):
        if min_interval > 0:
            elapsed = time.monotonic() - last_dispatch[0]
            if elapsed < min_interval:
                time.sleep(min_interval - elapsed)
            last_dispatch[0] = time.monotonic()
        return executor.submit(_fetch_and_cache, snap)

    def _deadline_passed() -> bool:
        return deadline is not None and time.monotonic() >= deadline

    snap_iter = iter(snapshots)
    # Prime the pool with up to 2× workers worth of in-flight requests so we
    # always have something completing while the producer feeds new items.
    prime_cap = max(max_workers * 2, 1)
    # Cache hits are yielded out-of-band — they never touch the executor or
    # the throttle, so a 100% cache-hit run finishes at memory speed.
    cached_hits: list[FetchedPage] = []

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        pending: set = set()
        exhausted = False

        def _refill() -> None:
            nonlocal exhausted
            while not exhausted and len(pending) < prime_cap:
                if _deadline_passed():
                    exhausted = True
                    return
                try:
                    snap = next(snap_iter)
                except StopIteration:
                    exhausted = True
                    return
                if cache is not None:
                    hit = cache.get(snap)
                    if hit is not None:
                        cached_hits.append(hit)
                        continue  # skip pool entirely; no rate-limit token
                pending.add(submit_with_throttle(ex, snap))

        _refill()
        # Drain cached hits first so the consumer sees them as fast as
        # possible — this is what makes a fully-cached scan effectively
        # instant.
        while cached_hits:
            yield cached_hits.pop(0)
        while pending:
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for fut in done:
                try:
                    yield fut.result()
                except Exception as e:  # pragma: no cover — defensive
                    log.warning("fetch task crashed: %s", e)
            _refill()
            while cached_hits:
                yield cached_hits.pop(0)
