# Architecture

`kronieker` is an OSINT tool that mines the Internet Archive's
historical snapshots of a given domain for contact data (emails and phone
numbers). It exists for one asymmetric scenario: **the current site has
nothing useful, but the wayback machine still holds what was there
earlier** — a defunct fraud company, a scrubbed about-us page, a renamed
legal entity, a redacted personnel listing.

This document describes the runtime architecture. For usage instructions
see [USAGE.md](USAGE.md).

---

## Pipeline at a glance

```
                ┌──────────────────────────────────────────────────────────┐
                │ scan_domain(domain, timeout_seconds=300, force_all=…,    │
                │             single_url=None, cache=None, …)              │
                └────────────────────────────┬─────────────────────────────┘
                                             │
                          ┌──────────────────▼───────────────┐
                          │ Calibration (load or run)        │
                          │  • ~/.cache/kronieker/cal.json   │
                          │  • avg_latency_s                 │
                          └──────────────────┬───────────────┘
                                             │
                          ┌──────────────────▼──────────────────────────────┐
                          │ planner.make_plan                               │
                          │  • show_num_pages (cheap meta-call)             │
                          │  • count_captures if pages ≤ 2 (precise count)  │
                          │  • capacity = timeout × conc / avg              │
                          │  • use_url_filter = !(force_all or all-fits)    │
                          │  • returns ScanPlan(deadline, cdx_limit, …)     │
                          └──────────────────┬──────────────────────────────┘
                                             │
                          ┌──────────────────▼──────────────────────────────┐
                          │ _scan(plan, single_url=…, cache=…)              │
                          │                                                 │
                          │  ┌─────────────────────────────────────────┐    │
                          │  │ CDX iteration in a daemon thread        │    │
                          │  │  • main polls a queue with sub-budget   │    │
                          │  │    (≤ 30 % of timeout) — abandon if     │    │
                          │  │    first byte never arrives             │    │
                          │  │  • single-URL ⇒ matchType=exact, no     │    │
                          │  │    urlkey filter, no ranker             │    │
                          │  └────────────────┬────────────────────────┘    │
                          │                   ▼                              │
                          │  ┌─────────────────────────────────────────┐    │
                          │  │ Probe-skip inference (saves IA load)    │    │
                          │  │  • precise count = 0 → skip all         │    │
                          │  │  • all captures non-HTML → skip probe   │    │
                          │  │  • broad-scan covered HTML pages →      │    │
                          │  │    skip probe                           │    │
                          │  │  • covered-range inference: 1 HTTP per  │    │
                          │  │    path × timeline cluster, not 5×      │    │
                          │  └────────────────┬────────────────────────┘    │
                          │                   ▼                              │
                          │  ┌─────────────────────────────────────────┐    │
                          │  │ Producer thread                         │    │
                          │  │  • ranked CDX top-N                     │    │
                          │  │  • well-known probes (when not skipped) │    │
                          │  │  • deadline + capacity stop checks      │    │
                          │  └────────────────┬────────────────────────┘    │
                          │                   ▼ bounded Queue                │
                          │  ┌─────────────────────────────────────────┐    │
                          │  │ Fetch pool (deadline-aware)             │    │
                          │  │  • cache.get → hit yielded synchronously│    │
                          │  │  • miss → ThreadPool + rate-limit token │    │
                          │  │  • successful miss → cache.put          │    │
                          │  │  • Ctrl+C → preserve sightings so far   │    │
                          │  └────────────────┬────────────────────────┘    │
                          │                   ▼                              │
                          │  ┌─────────────────────────────────────────┐    │
                          │  │ Extractor + live announce               │    │
                          │  │  • Pass 1: +-prefixed numbers           │    │
                          │  │  • Pass 2: bare locals, region-priority │    │
                          │  │  • 4-digit-year date filter             │    │
                          │  └─────────────────────────────────────────┘    │
                          └──────────────────┬──────────────────────────────┘
                                             │
                          ┌──────────────────▼───────────────┐
                          │ Zero-result escalation           │
                          │  • filter on → drop filter       │
                          │  • else → double timeout         │
                          │  • one shot, then stop           │
                          │  • suppressed when interrupted   │
                          └──────────────────┬───────────────┘
                                             ▼
                                        ScanResult
```

---

## Components

| Module                   | Responsibility                                                                                                  |
| ------------------------ | --------------------------------------------------------------------------------------------------------------- |
| `cdx.py`                 | Thin client for the IA CDX API (`query_domain`), the availability API (`closest_snapshot`), `show_num_pages`, and `count_captures` (cheap exact-count for small sites). Parses both line-delimited and compact JSON outputs. Extracts the real source URL from IA playback URLs (http↔https). |
| `classifier.py`          | Scores URL paths by likelihood of carrying contacts. Loads the multilingual `WELL_KNOWN_PATHS` list at import time from `data/well_known_paths.txt` (bundled package resource — analysts can edit/extend without redeploy), and builds the regex used for server-side CDX filtering (`CDX_URLKEY_FILTER`). |
| `fetcher.py`             | Concurrent snapshot downloader with a global rate limit and an optional **deadline** kwarg. Streams input lazily; once the deadline passes no new fetches are submitted, but in-flight requests are allowed to finish. Retries 404/429/5xx. Cache-aware: hits are yielded synchronously without touching the executor or the rate-limit token, misses write back through worker threads. |
| `cache.py`               | File-per-snapshot disk cache. SHA-1-prefixed filename layout `{ts}__{path}__{hash}.html` makes entries browsable on disk and survives URL-sanitisation collisions. Snapshots are immutable so there's no TTL. Only `200 OK` HTML responses persist — errors and 4xx are intentionally re-checked next run. Best-effort: any IO error logs and reads as a miss, never blocks the scan. |
| `extractors.py`          | Email + phone extraction. HTML-entity decode, Cloudflare cfemail unwrap, `mailto:` / `tel:` href harvest, `[at]/[dot]` deobfuscation (English + Russian), libphonenumber via hybrid leniency. Pass 1 (`+`-required, POSSIBLE) catches international-format numbers including site-side typos; Pass 2 (region-by-region, VALID) catches bare locals with position-based dedup so each substring is parsed once by the highest-priority region. 4-digit-year dates (`02.09.2008`) are filtered out — they have phone-shaped digit runs but obviously aren't phones. |
| `calibration.py`         | Per-machine latency calibration. Fetches 8 canonical wayback snapshots, measures avg/p50/p95, persists to `$XDG_CACHE_HOME/kronieker/calibration.json` (14-day TTL). Falls back to a conservative `DEFAULT_AVG_LATENCY_S = 0.6` when too few fixture fetches succeed. |
| `planner.py`             | Computes a `ScanPlan` from `(timeout, workers, rate, calibration, --all)`. Decides whether to apply the contact-URL filter (drops it when the whole site fits the timeout), what `cdx_limit` to ask for, and what the absolute monotonic deadline is. Preflight uses `show_num_pages` and (for `pages ≤ 2`) `count_captures` to pin the actual capture count, which often flips small sites from "filter on" to "scan everything". |
| `pipeline.py`            | The orchestrator. Runs the plan via a unified producer-consumer `_scan` with deadline plumbing, threaded CDX iteration with a wall-clock sub-budget, smart probe-skip inference, and zero-result escalation (one shot). Accepts `extra_well_known_paths` to merge per-target probe hints from `--targets-file`, and `single_url` to switch to one-URL-across-time mode. Carries plan metadata into `ScanResult` for the CLI/JSON layer. |
| `progress_ui.py`         | A thin wrapper around `rich.progress.Progress`. Adds a timeout-countdown column (`Ns left / Ms`), status lines, the live per-contact feed (`-v` adds date + URL beneath), and a daemon heartbeat thread that keeps the countdown ticking even during silent CDX waits. No-op when `enabled=False`. |
| `cli.py`                 | argparse entrypoint. Resolves named-mode aliases (`--default/--deep/--exhaustive/--auto`) into `(timeout, force_all)`, runs `ensure_calibration`, dispatches the three input modes (positional `<domain>`, `--targets-file`, `--single-url`) to a list of `Target(domain, extra_paths, single_url)` entries, and calls `scan_domain` per target. Catches `KeyboardInterrupt` so partial CSVs save before exit (exit code 130). Renders text + CSV + JSON. |
| `report.py`              | Pure rendering layer: `_aggregate_rows` (canonical contact → first/last sighting + distinct raw forms), CSV writer (UTF-8 BOM, `value_raw` column), text/JSON renderers. No I/O on the network side. |

---

## Timeout-driven planning

The tool's primary control is **wall-clock timeout**, not a named mode.
The planner translates `(timeout, workers, rate, calibration)` into a
concrete strategy:

```
effective_concurrency = min(workers, ceil(rate_limit))
capacity             = timeout × effective_concurrency / avg_latency_s

if force_all:
    use_url_filter = False
    cdx_limit      = None
elif estimated_total_snapshots ≤ capacity:
    use_url_filter = False              # whole site fits, no need to filter
    cdx_limit      = None
else:
    use_url_filter = True
    cdx_limit      = max(capacity × 5, 2_000)   # oversample for the ranker
```

`_CDX_LIMIT_OVERSAMPLE = 5` (was 20) keeps the ranker's pool large
enough without bloating the server-side CDX scan on marketplace-scale
domains, where the server's regex pass is the dominant cost. Floor is
`2_000` for sites where `capacity` is single-digit.

`estimated_total_snapshots` is `show_num_pages × SNAPS_PER_PAGE`. The
`50_000`-per-page convention is an IA approximation, accurate to ±2-3× —
the runtime deadline check absorbs the slack.

### Named-mode aliases

The CLI keeps `--auto/--default/--deep/--exhaustive` as **documented
aliases** mapping to timeout presets, so muscle memory still works:

| Alias          | Timeout (s) | `force_all` |
| -------------- | ----------- | ----------- |
| `--auto`       | 300         | false       |
| `--default`    | 300         | false       |
| `--deep`       | 900         | false       |
| `--exhaustive` | 0 (∞)       | true        |

`--all` is independent of any alias — it forces `force_all=True` without
changing the timeout.

### Zero-result escalation

If the first pass returns **zero sightings** and CDX didn't fail:

1. If the URL filter was on → drop the filter, same timeout reset.
2. Else, if the timeout was finite and exhausted → double the timeout.
3. Else → give up.

One shot only. Suppressed by `--no-escalate`.

---

## Calibration

The first ever invocation runs a one-time latency calibration: 8 canonical
wayback snapshots (`example.com`, `iana.org`, `www.w3.org`, etc. at pinned
historical timestamps) are fetched in parallel through the production
`_fetch_one` path. The mean is persisted as JSON to
`$XDG_CACHE_HOME/kronieker/calibration.json` with a 14-day TTL.

Real-run timings from production scans are **not** folded back into the
cache — a single slow domain would skew the fixture-based average. Each
scan still logs a one-line "this run averaged X s/snapshot vs calibration
Y" diagnostic so the user can decide whether to recalibrate.

If fewer than `MIN_SUCCESSFUL_SAMPLES = 4` of the 8 fixture fetches
succeed, the calibration falls back to a hardcoded
`DEFAULT_AVG_LATENCY_S = 0.6` and marks the file accordingly.

---

## Concurrency model

`_scan` is a single, unified producer-consumer pipeline (no separate
batched code path). It runs whether the timeout is 30 s or unlimited.
Four threads are involved at any given moment:

1. **CDX worker thread** (`_stream_cdx_with_budget`) — runs the
   `query_domain` iteration. The main thread polls its output queue
   with a `min(remaining_budget, 1.0)` timeout. The CDX sub-budget is
   `max(30 s, plan.timeout_seconds × 0.3)` from the moment `_scan`
   starts; if it expires we abandon the worker (it stays daemon until
   `requests.get` times out and the process exits). This is the only
   way to enforce a wall-clock cap on a CDX call where IA's first byte
   may take minutes — `requests`' own `timeout` parameter is per-byte,
   not per-call. See "Key design decision: threaded CDX iteration"
   below.

2. **Producer thread** emits in two phases:
   - the ranked CDX top-N first (deduped — per-year for bounded scans,
     exact `(URL, ts)`-only when `timeout == 0` so the exhaustive use
     case keeps multi-year `/kontakty` pages distinct);
   - then the well-known-paths probes (~150 path × epoch). The path
     list is `WELL_KNOWN_PATHS ∪ extra_well_known_paths`, where the
     extras come from `--targets-file` URL entries for that domain.
     Probes use smart skip inference (see decision point below) to
     avoid wasting HTTP on captureless paths.

   Each item passes through three stop signals: the consumer-side
   `stop_event`, the wall-clock `deadline`, and the `capacity` cap.
   `Queue.put` uses a 100 ms timeout polling `stop_event` so the
   producer never blocks forever when the consumer has exited.

3. **Consumer in the main thread** — `fetch_snapshots(deadline=…,
   cache=…)` pulls from the queue lazily. Cache hits are yielded
   synchronously, without touching the executor or burning a
   rate-limit token. Misses go through the `ThreadPoolExecutor`;
   results are surfaced via `wait(FIRST_COMPLETED)`. Once the deadline
   passes `_refill` stops submitting new fetches; in-flight requests
   are allowed to finish (cancelling mid-`requests.get` would leave
   half-parsed responses and trigger retries).

4. **UI heartbeat thread** — keeps the progress bar's countdown column
   ticking at ~2 Hz even during silent CDX waits, and emits a stall
   warning when 20 s elapse with no fetch completing.

A regression test
(`test_probe_and_fetch_run_concurrently_in_default_mode`) asserts that
probe call N+1 observes the fetch from probe call N having already
started — i.e. probing and fetching genuinely overlap, not just
nominally.

---

## Key design decisions

### 1. Year-level dedup, except when timeout is unlimited

CDX's default `collapse=urlkey` returns the **earliest** snapshot per URL,
losing the timeline this tool exists to surface. We always disable
server-side collapse and instead dedupe client-side:

- **Bounded timeout** (`timeout > 0`): one snapshot per `(urlkey, year)`,
  giving ~10 snapshots per long-archived URL.
- **Unlimited timeout** (`timeout == 0`, e.g. `--exhaustive`): strip only
  exact `(URL, timestamp)` duplicates — every distinct snapshot reaches
  fetch. The analyst asked for everything; we honour that.

This is what lets a single multi-year `/kontakty` page contribute its
2018, 2020, and 2022 snapshots — phone numbers may differ across years
and each version is independently interesting.

### 2. No domain-based email filtering

For OSINT, the most useful business email on a Russian or Belarusian
SMB site is on a free provider — `forcing-technic@mail.ru`,
`ivan@yandex.ru`, `business@gmail.com`. Early versions rejected those
because their domain didn't match the target. That was wrong: the
analyst judges relevance, the tool's job is to surface signal.

`extractors._looks_like_real_email` is now syntax-only: it rejects
file-extension paths (`logo@2x.png`) and JSON/JS escape leftovers
(`u003e`). Nothing else.

### 3. Two-pass phone extraction with leniency and span dedup

libphonenumber has two practical leniency levels and we use both,
gated to avoid an interaction that previously misclassified obvious
numbers across regions:

- **Pass 1 — `Leniency.POSSIBLE`, no region, ``+``-required.** The
  leading `+` is the only reliable "this is international" signal —
  without it, POSSIBLE leniency interprets `8` (Russian IDD) or `00`
  (European IDD) as international-call prefixes and reinterprets an
  obvious local number as a foreign one. Real breakage that drove this
  filter: `(855) 843-7200` parsed as `+7 8558437200`, and
  `8(863)-218-22-22` parsed as `+1 8632182222`. Requiring a literal
  `+` defers all unprefixed digit runs to Pass 2 where the region order
  makes the right call. Pass 1 still catches typos like
  `+375-33-354518` (one digit short of a real BY mobile).
- **Pass 2 — `Leniency.VALID`, region-by-region.** Without a `+` we
  iterate the prioritised region list and the first valid match wins.
  Span-based dedup: once a region emits a match for substring
  `[start:end]`, later regions whose match overlaps are skipped — same
  number can't be claimed by two regions. Strict VALID leniency here
  keeps postcodes (`225006`), tax IDs (`290506581`), and order
  numbers out of the result.

A 4-digit-year date filter (`^\s*\d{1,2}[./\-]\d{1,2}[./\-]\d{4}\s*$`
plus `YYYY-MM-DD`) rejects calendar dates whose digit runs would
otherwise look phone-shaped. The 2-digit-year case (`02.09.08`) is
genuinely ambiguous and is left alone — analyst sorts it.

### 4. TLD-aware phone regions

`_regions_for_domain` prepends a country region to the user's
`default_regions` list based on the domain's TLD. The map covers 70+
ccTLDs (Anglosphere, Western & Eastern Europe, Nordics, CIS,
Middle East, Asia-Pacific, Latin America, Africa) plus the generic
`.com / .org / .net / .io / .co / .app / .ai / .dev / .tech / .info /
.biz / .xyz`, which all default to `US` since those domains are
overwhelmingly US-anchored in practice.

Without the generic-TLD mapping, the analyst's RU-first default order
claimed every `(855) 843-7200`-shaped US number as `+7 8558437200`.
With the mapping, `theranos.com → US` first, the right answer wins.
Russian companies on `.com` are still parseable via `--regions
RU,US,GB,…`.

The `.by → BY` priority is what lets the Belarusian landline
`8-0162-51-12-54` parse as `+375162511254` without the user touching
anything.

### 5. IA playback URL extraction

The availability API returns playback URLs like
`http://web.archive.org/web/<ts>/https://example.com/contacts`. Naively
storing that as `Snapshot.original` would later get wrapped *again* into
an `id_` playback URL — a double-nested mess that IA returns 404 for.
`cdx._source_from_playback` extracts the underlying source URL, which
also transparently handles http↔https mismatches when IA captured the
redirect target.

### 6. 404 retry

IA's playback layer transiently 404s slow snapshots (the storage is
deep, the snapshot exists but rendering takes seconds). `_fetch_one`
retries on 404 with a 3-second-per-attempt backoff. This was confirmed
empirically on `tomhunter.ru` where browser load was slow but the
content was present.

### 7. Precise capture-count for small sites

CDX's cheap `showNumPages` meta-call returns the count of *index pages*
(IA convention: up to ~50 000 captures per page). For sites with
`pages ≤ PRECISE_COUNT_MAX_PAGES = 2` the loose `pages × 50 000` ceiling
is too generous — a 1-page domain can hold anywhere from a handful to
50 000 snapshots. We pay for one extra cheap CDX call (`fl=urlkey` to
minimise per-row bytes) to learn the exact count. For larger sites the
ceiling stays — counting them exactly would mean materialising hundreds
of thousands of rows.

The exact count often flips the planner from "filter on, fetch top-N"
to "filter off, scan everything" for small targets — which is what an
analyst typically wants on a niche site that gets passed via
`--targets-file`. The preflight (`show_num_pages` + optional
`count_captures`) is now anchored **before** the timeout deadline, so a
multi-second precise call doesn't silently eat into the user's
`--timeout` budget.

`ScanPlan.total_is_precise` carries this provenance through to the
rationale string: `"site fits timeout (80 ≤ 960)"` for an exact count
vs `"…of ~50,000 snapshots"` (note the `~`) for the loose ceiling.

### 8. Threaded CDX iteration with a wall-clock sub-budget

Server-side CDX scans with a urlkey-regex filter on giant domains
(marketplace-scale: ~5 300 CDX pages, millions of rows) can take
**minutes** to return the first byte. `requests`' `timeout=N` parameter
is a per-byte read timeout, not a total cap — if IA emits keep-alive
bytes during its scan, the timer never fires.

`_stream_cdx_with_budget` solves this by running `query_domain` on a
daemon thread and having the main thread poll its output queue with a
per-iteration `poll = min(remaining_budget, 1.0)`. The sub-budget is
`max(30 s, plan.timeout_seconds × 0.3)`. When it expires we stop
reading from the queue and proceed with whatever we've got — the
worker thread keeps running until its own `requests.get` finally
times out (`--cdx-timeout`, default 300 s) and dies with the process.

Without this, the marketplace-scale case would spend the entire user
timeout inside CDX, the producer would see `_deadline_passed()` on its
first iteration, and the fetcher would never run.

### 9. Smart probe-skip inference

The well-known-paths probe makes one HTTP call per `(path, timestamp)`
pair. With ~30 paths × 5 probe timestamps that's 150 calls per host.
Three skip rules cut the wasted ones:

- **No captures.** If the planner's precise-count preflight reports
  zero captures for the host, we skip CDX *and* probe — there's nothing
  to find.
- **All non-HTML.** When precise count > 0 but the main CDX query
  (HTML + status 200) returns zero rows, every capture is a
  redirect / robots.txt / image, etc. Probe would just resurface those;
  skip it.
- **Broad scan already covered HTML.** When precise count > 0, CDX
  returned ≥ 1 row, and the planner had the URL filter off, we ran the
  comprehensive scan already. Probe doesn't add anything; skip it. The
  filter-on branch keeps probing because the urlkey filter is
  ASCII-only and misses `/контакты`-style paths.

Inside `_iter_well_known`, per-path covered-range inference cuts the
remaining cost further. After a successful `(query Q, returned snap S)`,
no other snapshot can exist in `[min(Q,S), max(Q,S)]` (otherwise IA
would have returned that closer one). Any remaining timestamp falling
in a covered range is resolved without an HTTP call — usually a 5×
saving per path. First-call-`None` means the URL has zero captures
domain-wide, so the remaining 4 timestamps for that path are skipped
entirely.

### 10. Snapshot cache

Wayback captures are immutable, so the on-disk cache has no TTL and no
invalidation. Layout: one file per `(timestamp, original_url)`, grouped
by host:

```
~/.cache/kronieker/snapshots/
└── theranos.com/
    └── 20140902120000__contact-us__a3f9d4e1.html
```

Filename = `{timestamp}__{sanitized-path}__{sha1[:8]}.html`. The hash
suffix disambiguates URLs whose path slug collapses to the same
fs-safe form after sanitisation (different query strings, fragments,
%-encoded variants).

The cache is best-effort end-to-end:

- Only `200 OK` HTML responses are written. Errors, redirects, 404s
  are not persisted — they could be transient and would poison reruns.
- Any filesystem error is swallowed and counted as a miss; a broken
  cache must never break a scan.
- Cache hits short-circuit the fetcher: yielded synchronously, no
  executor touch, no rate-limit token spent. A fully cached scan
  runs at memory speed.
- Writes happen on the worker thread (atomic `tmp + rename`), in
  parallel with other fetches.

CLI controls: `--no-cache` (read/write off for this run),
`--clear-cache` (wipe and exit), `KRONIEKER_CACHE_DIR=…` for an
alternate root.

### 11. Single-URL mode

`--single-url URL` flips the scanner from "find contacts on this host"
to "show me every archived snapshot of one exact URL". When
`single_url` is set, `_scan`:

- Asks CDX with `matchType=exact` and the URL as `url=…`, not the
  host.
- Strips `cdx_urlkey_filter` and `cdx_limit` from the plan it passes
  to the streamer.
- Skips the well-known-paths probe entirely.
- Skips score-based ranking — every snapshot is the same URL, ordered
  chronologically.
- Skips the `min_score` cutoff (analyst already picked the URL).

The planner still runs on the host for capacity computation, but its
filter and limit decisions are overridden in `_scan`. Probe-skip and
no-captures inferences (which are about the *host*, not the URL) are
suppressed.

### 12. Soft-fail on CDX

A CDX timeout or 5xx is non-fatal: the probing thread still produces
snapshots and contacts surface from them. The error is recorded in
`ScanResult.errors`; the user sees a hint to bump `--cdx-timeout`. The
zero-result escalation is suppressed if CDX errored (no point retrying
against a broken endpoint).

### 13. Deadline carries the absolute time, not a relative timeout

Everywhere in the pipeline the timeout is represented as `deadline:
float` (monotonic). The planner computes it once at the start of the
run; downstream code only ever compares `time.monotonic() >= deadline`.
This avoids the "where did the clock start" class of bugs that
relative timeouts invite when handed off across producer, consumer, and
fetcher threads.

### 14. CSV write happens outside the timeout

The timeout covers scanning. Writing the CSV (small, local I/O) runs
after the scan completes, even when the timeout was exhausted — we never
discard found data because the clock ran out at the wrong moment. This
is documented in `--help`.

### 15. Batch input as text + per-target URL hints

`--targets-file PATH` reads a plain-text list of targets, one per line,
with `#` comments. Each line is normalised by `_parse_target_line` into
a `(host, optional_path)` pair: scheme/port are stripped, host is
lowercased, trailing slashes dropped, and a non-trivial path (anything
other than `""` or `"/"`) is kept.

`parse_targets_file` then groups entries by host into a
`Target(domain, extra_paths)` list, preserving first-seen order both
across hosts and within a host's `extra_paths`. Duplicates within a
host's paths are dropped. Subdomains stay separate (`theranos.com` and
`www.theranos.com` run independently — CDX `matchType=domain` handles
the subtree semantics on its own).

In `main()`, calibration and the HTTPS session are created **once**
outside the per-target loop, then each `Target` becomes one call to
`scan_domain(..., extra_well_known_paths=target.extra_paths)`. Inside
`scan_domain`, the extras are unioned with `WELL_KNOWN_PATHS`
(defaults-first, dedup, order-preserving) before being passed into
`_scan`'s producer. Result: an analyst can hint domain-specific contact
URLs (`/uslugi/excavator`, `/o-nas/team`) without editing the bundled
`data/well_known_paths.txt`.

`--csv PATH` is rejected in batch mode to prevent file clobbering; each
scan uses the default `<domain>_<timestamp>.csv` naming.

---

## Outputs

`ScanResult` carries everything callers care about:

- `sightings: list[ContactSighting]` — every appearance of every contact,
  including the snapshot's wayback URL and timestamp.
- `timeout_seconds, elapsed_seconds, timeout_exhausted` — what was asked,
  what we actually spent, and whether we stopped because of the deadline.
- `plan_rationale: str` — one-line explanation of the strategy chosen
  by the planner (echoed in the text header for the analyst).
- `url_filter_active: bool` — was the contact-URL CDX filter applied?
  Drives the trailing hint ("try `--all` for more coverage").
- `resolved_mode: str` — legacy back-compat name ("default" / "deep" /
  "exhaustive" / "all") derived from the plan; the timeout/elapsed/
  plan_rationale fields are the canonical metadata going forward.
- `errors: list[str]` — non-fatal soft-fails.

The CLI aggregates sightings by canonical value (`ScanResult.by_value()`),
sorts by first-seen timestamp, and emits:

- A monospace table on stdout (`Contact | First seen | Last seen`,
  phones rendered in INTERNATIONAL format, dates `YYYY-MM-DD`).
- A CSV file (`<domain>_<scan-ts>.csv`, UTF-8 BOM, eight columns
  including E.164 canonical phone and INTERNATIONAL human-readable
  phone, plus first/last wayback URLs).
- A timeout-aware hint on stderr — suggests `--timeout N×2` when the
  timeout was exhausted, or `--all` when the filter was active.
- A JSON dump on stdout when `--json` is passed, including all the
  timeout metadata fields.

Empty results never produce a CSV file or a blank table — the CLI prints
`No contacts found.` and exits.

---

## Concurrency / safety properties

- All wayback requests share a single rate limit (`--rate`, default 4 RPS).
- `--workers` (default 4) caps concurrency in the fetch pool.
- All non-main threads (CDX streamer, probe producer, UI heartbeat) are
  daemons and shut down with the process. The producer additionally
  observes a `stop_event` and a wall-clock `deadline`, so Ctrl+C never
  leaves them spinning.
- Ctrl+C anywhere inside the fetch loop is caught at the `_scan`
  boundary: partial sightings are preserved on the returned
  `ScanResult` (`interrupted=True`), the CLI writes the CSV before
  exit, and `main()` returns exit code 130.
- The IA User-Agent (`kronieker/0.1 (+https://github.com/soxoj/kronieker)`)
  is sent on every request for transparency.
- No data is sent anywhere except web.archive.org / archive.org.

---

## Test taxonomy

- **Unit tests** (`tests/unit/`, ~240, run in <10 s, offline via
  `responses` and `monkeypatch`):
  - `test_planner.py` — capacity formula, filter-on/off branches,
    user-override ceiling, unlimited timeout, precise-count preflight.
  - `test_calibration.py` — roundtrip, TTL/version staleness, XDG
    path, fallback when too few fixture fetches succeed.
  - `test_deadline.py` — deadline cutoff, in-flight completion,
    `timeout_exhausted=True` semantics.
  - `test_pipeline.py` — full producer-consumer pipeline against
    mocked CDX/availability, including: escalation paths, smart
    probe-skip, threaded CDX sub-budget, single-URL mode,
    Ctrl+C-preserves-partials.
  - `test_cache.py` — file-based snapshot cache roundtrip, negative
    results not persisted, fetcher cache-hit short-circuit,
    `--no-cache` / `--clear-cache` integration.
  - `test_extractors.py` — every obfuscation variant, asset-path
    filter, Russian / US phone formats with span dedup, 4-digit-year
    date filter, region-priority edge cases.
  - `test_cli.py` — flag resolution, named-alias mapping,
    `--calibrate` / `--single-url` / `--no-cache` exit paths,
    Ctrl+C partial-CSV save, `-v` vs `-d` independence,
    timeout-aware hint rendering.
- **End-to-end tests** (`tests/e2e/test_live_archive.py`, marked
  `pytest -m e2e`, ~5 min, hits the real wayback). Covers
  `theranos.com` and `enron.com` — both defunct in real life,
  archives still hold their contacts. Wired to the two documented
  OSINT precedents this tool was designed for.
