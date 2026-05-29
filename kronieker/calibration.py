"""Per-snapshot latency calibration.

Measures how fast the running machine can fetch a wayback snapshot end-to-end
(network + IA serving + local parse-prep), and caches the answer on disk so
the timeout-driven planner can size scans without re-measuring on every invocation.

Design notes:

- The calibration fixture is a **fixed set** of canonical wayback snapshots
  pinned to historical timestamps. We never use "latest" — re-running the
  calibration two years from now should hit the same bytes, so the avg is
  stable across time and machines.
- Cache file lives in ``$XDG_CACHE_HOME/kronieker/calibration.json``
  (or ``~/.cache/...`` on macOS / Linux without XDG). Cache is regeneratable
  — that's the XDG distinction between cache vs config.
- Real-run timings from production scans are deliberately *not* folded back
  in: a single slow domain would skew the fixture-based average. Use
  ``--recalibrate`` if you suspect drift.
"""

from __future__ import annotations

import json
import logging
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests

from kronieker.cdx import DEFAULT_USER_AGENT, Snapshot
from kronieker.fetcher import _fetch_one

log = logging.getLogger(__name__)

#: Cache schema version. Bump when fields change incompatibly.
CALIBRATION_VERSION = 1

#: Persistent cache TTL. Beyond this we auto-recalibrate on next invocation.
#: 14 days hits the sweet spot between staleness and bothering the user — IA
#: latency changes on the scale of infrastructure deploys, not days.
TTL_SECONDS = 14 * 24 * 3600

#: Fallback when calibration fails (too few successful samples). Conservative
#: — modern IA serves snapshots in 0.3-0.5 s; 0.6 leaves timeout headroom.
DEFAULT_AVG_LATENCY_S = 0.6

#: We need at least this many successful fetches out of the fixture set for
#: the result to be statistically meaningful.
MIN_SUCCESSFUL_SAMPLES = 4

#: Fixed-set fixture: 8 canonical wayback snapshots of stable, small HTML
#: pages, pinned to historical timestamps. Re-running calibration always hits
#: the same captures. **Audit this list annually** — if IA ever deletes one
#: of these specific timestamps, that fixture entry will silently fail and
#: degrade calibration to 7/8 samples. The ``MIN_SUCCESSFUL_SAMPLES`` floor
#: protects against catastrophic failure but not slow drift.
CALIBRATION_SNAPSHOTS: tuple[Snapshot, ...] = (
    Snapshot(timestamp="20200101000000", original="http://example.com/",
             mimetype="text/html", status="200", urlkey=""),
    Snapshot(timestamp="20180101000000", original="http://example.com/",
             mimetype="text/html", status="200", urlkey=""),
    Snapshot(timestamp="20200101000000", original="http://www.iana.org/",
             mimetype="text/html", status="200", urlkey=""),
    Snapshot(timestamp="20180101000000", original="http://www.iana.org/",
             mimetype="text/html", status="200", urlkey=""),
    Snapshot(timestamp="20200101000000", original="http://www.w3.org/",
             mimetype="text/html", status="200", urlkey=""),
    Snapshot(timestamp="20180101000000", original="http://www.python.org/",
             mimetype="text/html", status="200", urlkey=""),
    Snapshot(timestamp="20200101000000", original="http://www.gnu.org/",
             mimetype="text/html", status="200", urlkey=""),
    Snapshot(timestamp="20180101000000", original="http://www.wikipedia.org/",
             mimetype="text/html", status="200", urlkey=""),
)


@dataclass(frozen=True)
class Calibration:
    version: int
    avg_latency_s: float
    sample_count: int
    last_calibrated_at: str          # ISO-8601 UTC
    samples_p50: float
    samples_p95: float
    user_agent: str


def cache_path() -> Path:
    """Resolved location of the persistent calibration JSON.

    Honors ``XDG_CACHE_HOME`` if set; otherwise falls back to ``~/.cache/``.
    """
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "kronieker" / "calibration.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _parse_iso(s: str) -> datetime | None:
    try:
        # Python 3.11+: fromisoformat handles "+00:00" suffix.
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def is_stale(cal: Calibration | None, *, ttl_seconds: int = TTL_SECONDS) -> bool:
    """Return True if ``cal`` is missing, wrong version, or older than TTL."""
    if cal is None:
        return True
    if cal.version != CALIBRATION_VERSION:
        return True
    ts = _parse_iso(cal.last_calibrated_at)
    if ts is None:
        return True
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - ts).total_seconds()
    return age > ttl_seconds


def load(path: Path | None = None) -> Calibration | None:
    """Read the cache from disk. Returns ``None`` on any failure or staleness.

    Callers should treat ``None`` as "need to recalibrate".
    """
    p = path or cache_path()
    try:
        raw = p.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return None
    try:
        data = json.loads(raw)
        cal = Calibration(
            version=int(data["version"]),
            avg_latency_s=float(data["avg_latency_s"]),
            sample_count=int(data["sample_count"]),
            last_calibrated_at=str(data["last_calibrated_at"]),
            samples_p50=float(data.get("samples_p50", data["avg_latency_s"])),
            samples_p95=float(data.get("samples_p95", data["avg_latency_s"])),
            user_agent=str(data.get("user_agent", "")),
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None
    if is_stale(cal):
        return None
    return cal


def save(cal: Calibration, path: Path | None = None) -> Path:
    """Persist ``cal`` to disk, creating parent directories."""
    p = path or cache_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(asdict(cal), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return p


def _fallback(reason: str) -> Calibration:
    """Produce a Calibration using the hardcoded default latency."""
    return Calibration(
        version=CALIBRATION_VERSION,
        avg_latency_s=DEFAULT_AVG_LATENCY_S,
        sample_count=0,
        last_calibrated_at=_now_iso(),
        samples_p50=DEFAULT_AVG_LATENCY_S,
        samples_p95=DEFAULT_AVG_LATENCY_S,
        user_agent=DEFAULT_USER_AGENT + f" [fallback: {reason}]",
    )


def run_calibration(
    *,
    session: requests.Session | None = None,
    fixture: tuple[Snapshot, ...] = CALIBRATION_SNAPSHOTS,
    workers: int = 4,
    timeout: int = 30,
) -> Calibration:
    """Fetch the fixture snapshots in parallel, time each, return Calibration.

    Falls back to ``DEFAULT_AVG_LATENCY_S`` when fewer than
    :data:`MIN_SUCCESSFUL_SAMPLES` fixture snapshots return HTTP 200.
    """
    sess = session or requests.Session()
    sess.headers.setdefault("User-Agent", DEFAULT_USER_AGENT)

    durations: list[float] = []

    def _timed(snap: Snapshot) -> float | None:
        t0 = time.monotonic()
        page = _fetch_one(snap, sess, timeout=timeout, retries=1)
        dt = time.monotonic() - t0
        if page.error or page.status != 200:
            log.debug(
                "calibration fixture failed: %s @ %s — %s",
                snap.original, snap.timestamp, page.error or page.status,
            )
            return None
        return dt

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(_timed, snap) for snap in fixture]
        for fut in as_completed(futures):
            try:
                result = fut.result()
            except Exception as e:  # pragma: no cover — defensive
                log.warning("calibration future crashed: %s", e)
                continue
            if result is not None:
                durations.append(result)

    if len(durations) < MIN_SUCCESSFUL_SAMPLES:
        return _fallback(f"only {len(durations)}/{len(fixture)} fixture fetches succeeded")

    durations.sort()
    avg = statistics.fmean(durations)
    p50 = statistics.median(durations)
    p95_idx = max(0, int(0.95 * len(durations)) - 1)
    p95 = durations[p95_idx]

    return Calibration(
        version=CALIBRATION_VERSION,
        avg_latency_s=avg,
        sample_count=len(durations),
        last_calibrated_at=_now_iso(),
        samples_p50=p50,
        samples_p95=p95,
        user_agent=DEFAULT_USER_AGENT,
    )


def ensure_calibration(
    *,
    session: requests.Session | None = None,
    force: bool = False,
    path: Path | None = None,
    announce: bool = True,
) -> Calibration:
    """Return a usable :class:`Calibration`, running it if needed.

    Order of precedence:
    1. ``force=True`` (``--recalibrate``) — always re-run + persist.
    2. Fresh cached calibration on disk — use it.
    3. Run calibration, persist, return.
    """
    if not force:
        cached = load(path)
        if cached is not None:
            return cached

    if announce:
        if force:
            msg = "[*] Recalibrating wayback latency…"
        else:
            msg = "[*] First-run calibration of wayback latency (≈3-5 s)…"
        print(msg, file=sys.stderr, flush=True)

    cal = run_calibration(session=session)
    try:
        saved_to = save(cal, path)
        if announce:
            print(
                f"[*] Calibration: {cal.avg_latency_s:.2f} s/snapshot "
                f"({cal.sample_count} samples) → cached to {saved_to}",
                file=sys.stderr, flush=True,
            )
    except OSError as e:
        log.warning("could not persist calibration to %s: %s", path or cache_path(), e)
    return cal
