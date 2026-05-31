"""Local file-based cache for Wayback Machine snapshot HTML.

Snapshots in the Internet Archive are immutable — once a capture exists at a
given ``(timestamp, url)`` pair, the served bytes don't change. That makes
them ideal cache material: rerunning a scan (to tweak extractor regions,
re-render output, debug a contact filter) does not need to hit IA again.

**Layout** — one HTML file per snapshot under
``$XDG_CACHE_HOME/kronikier/snapshots/``::

    <cache_dir>/
        example.com/
            20200315120000__contact__a3f9d4e1.html
            20180101000000__index.html__b2c01f9a.html
        another.example/
            20151204093015__about-us__7e2d1a08.html

The filename encodes ``{timestamp}__{sanitized-path}__{url-hash}.html`` so
each entry is browsable on disk (open in a browser, grep, diff with
``code -d``) without an opaque database. The ``url-hash`` (first 8 hex of
sha1) disambiguates same-timestamp+same-pathfragment collisions across
different full URLs.

**What we cache.** Only ``200 OK`` responses with non-empty bodies. Errors,
redirects, and 404s could be transient and should be re-checked on the next
run, so they're intentionally not persisted.

**Best-effort.** Any filesystem error is logged and treated as a cache miss
— a broken cache must never break a scan.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import threading
from pathlib import Path
from urllib.parse import urlparse

from kronikier.cdx import Snapshot
from kronikier.fetcher import FetchedPage

log = logging.getLogger(__name__)


def default_cache_dir() -> Path:
    """Resolve the on-disk cache root.

    Precedence: ``KRONIEKER_CACHE_DIR`` env > ``XDG_CACHE_HOME/kronikier/snapshots``
    > ``~/.cache/kronikier/snapshots``.
    """
    override = os.environ.get("KRONIEKER_CACHE_DIR")
    if override:
        return Path(override)
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".cache"
    return base / "kronikier" / "snapshots"


# Anything outside this set is replaced with ``_`` when building a filename.
_FS_SAFE = re.compile(r"[^a-zA-Z0-9._-]")
# Cap the path segment so we don't blow past common filesystem limits
# (ext4: 255 bytes; APFS: 255 UTF-16 codepoints).
_MAX_PATH_SEGMENT = 80


def _sanitize_for_fs(s: str, fallback: str) -> str:
    cleaned = _FS_SAFE.sub("_", s).strip("_") or fallback
    return cleaned[:_MAX_PATH_SEGMENT]


class SnapshotCache:
    """File-per-snapshot cache. Thread-safe across the fetcher pool.

    All public methods are best-effort: filesystem errors are logged and
    swallowed. The scan continues with cache-miss behavior on any failure.
    """

    def __init__(self, cache_dir: Path):
        self.path = cache_dir
        self.path.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()  # protects hit/miss counters only
        self.hits = 0
        self.misses = 0

    # ------------------------------------------------------------------
    # Filename layout
    # ------------------------------------------------------------------

    def _file_for(self, snap: Snapshot) -> Path:
        """Deterministic cache path for a snapshot.

        Stable across runs and across machines — derived purely from the
        snapshot's ``timestamp`` and ``original`` URL.
        """
        parsed = urlparse(snap.original)
        domain = _sanitize_for_fs(parsed.netloc, "no-domain")
        path_seg = parsed.path or "/"
        safe_path = _sanitize_for_fs(path_seg, "root")
        # Short hash of the full URL avoids collisions when two distinct URLs
        # share the same domain/path-fragment after sanitization (e.g. query
        # strings, fragment-only differences, %-encoded variants).
        url_hash = hashlib.sha1(snap.original.encode("utf-8")).hexdigest()[:8]
        fname = f"{snap.timestamp}__{safe_path}__{url_hash}.html"
        return self.path / domain / fname

    # ------------------------------------------------------------------
    # Public API: get / put / clear / size / close
    # ------------------------------------------------------------------

    def get(self, snap: Snapshot) -> FetchedPage | None:
        """Look up a cached page. Returns ``None`` on miss or any error."""
        path = self._file_for(snap)
        if not path.exists():
            with self._lock:
                self.misses += 1
            return None
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            log.debug("cache read failed for %s: %s", path, e)
            with self._lock:
                self.misses += 1
            return None
        with self._lock:
            self.hits += 1
        return FetchedPage(snap, 200, content)

    def put(self, page: FetchedPage) -> None:
        """Store a successful fetch. No-op for errors / non-200 / empty bodies."""
        if page.error or page.status != 200 or not page.content:
            return
        path = self._file_for(page.snapshot)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Atomic write: write to ``.tmp`` then rename. POSIX rename is
            # atomic on the same filesystem, so concurrent fetcher workers
            # never see a half-written file.
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(page.content, encoding="utf-8")
            tmp.replace(path)
        except OSError as e:
            log.debug("cache write failed for %s: %s", path, e)
            try:
                tmp.unlink()  # type: ignore[possibly-undefined]
            except OSError:
                pass

    def clear(self) -> int:
        """Delete every cached file. Returns the count removed."""
        if not self.path.exists():
            return 0
        count = self.size()
        try:
            shutil.rmtree(self.path)
            self.path.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            log.debug("cache clear failed: %s", e)
            return 0
        return count

    def size(self) -> int:
        """Number of cached snapshot files."""
        if not self.path.exists():
            return 0
        try:
            return sum(1 for _ in self.path.rglob("*.html"))
        except OSError:
            return 0

    def close(self) -> None:
        """No-op for the filesystem backend (kept for API symmetry)."""
        return
