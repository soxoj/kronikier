# Usage

`kronikier` recovers emails and phone numbers from the
web.archive.org history of a domain. The tool is driven by a **time
timeout** — you tell it how long you're willing to wait, and it picks
how many snapshots to scan so the run fits.

For the rationale behind the design choices, see
[ARCHITECTURE.md](ARCHITECTURE.md).

---

## Install

```bash
pip install -e .             # runtime only
pip install -e ".[dev]"      # plus pytest, responses
```

Runtime dependencies: `requests`, `beautifulsoup4`, `phonenumbers`, `rich`.
Python 3.10+.

---

## Quick start

```bash
# Default 5-minute timeout — fast, contact-URL filter on.
kronikier theranos.com

# Longer timeout on a harder case:
kronikier mysite.ru --timeout 900

# Scan every URL (not just contact pages), still inside the default timeout:
kronikier wirecard.com --all

# Unlimited time, every URL — for small defunct sites where you want everything:
kronikier old-defunct-corp.com --exhaustive

# Pipe machine-readable output to another tool:
kronikier mysite.ru --json > mysite.json

# Batch: scan a list of domains/URLs from a file (one per line):
kronikier --targets-file targets.txt

# Single-URL mode: examine one specific page's history across snapshots.
kronikier --single-url https://www.theranos.com/contact-us
```

The first ever invocation runs a one-time **calibration** (~3-5 sec) to
measure your machine's archive-fetch latency, cached at
`~/.cache/kronikier/calibration.json` and reused for 14 days.

---

## How the timeout works

When you run `kronikier example.com --timeout 300`, the tool:

1. Pulls a cheap **size signal** from CDX (`showNumPages`, ~1 sec).
2. Reads the cached **calibration** (e.g. `0.42 s/snapshot`).
3. Computes how many snapshots fit in your timeout:
   `capacity = timeout × concurrency / avg_latency`.
4. If the **whole site** fits in that capacity → scans every URL (no
   contact-URL filter — you can see everything).
5. If it doesn't fit → keeps the contact-URL filter on and fetches the
   top-`capacity` ranked snapshots.
6. Stops when the deadline hits. **In-flight fetches finish** so no
   data is lost mid-extraction.
7. If the first pass found **nothing**, automatically retries with a
   broader strategy (drops the URL filter, or doubles the timeout) —
   one shot only.

---

## Modes (named-alias shortcuts over `--timeout`)

The named flags are exact shortcuts. They show the same `Plan: …` line
when they run.

| Flag           | Equivalent to            | Use when…                                                            | Typical time     |
| -------------- | ------------------------ | -------------------------------------------------------------------- | ---------------- |
| `--default`    | `--timeout 300`           | Quick triage on an unknown domain. The default.                      | up to ~5 min     |
| `--auto`       | `--timeout 300`           | Same as `--default` — kept for muscle memory.                        | up to ~5 min     |
| `--deep`       | `--timeout 900`           | Filtered scan didn't find enough; or known to be a hard target.      | up to ~15 min    |
| `--exhaustive` | `--timeout 0 --all`       | Defunct site, you want every snapshot of every URL ever archived.    | minutes to hours |
| `--all`        | (modifier, not a timeout) | Scan every URL the site has — drops the contact-URL filter.          | depends on timeout |

`--all` is **additive**: `--deep --all` means "15-min timeout, no URL filter".

If a scan finishes with **zero** contacts and timeout time was left, the
tool auto-escalates one step (drop the filter, then extend the timeout).
Pass `--no-escalate` to disable that.

---

## Calibration

The tool runs a one-time latency calibration on its first ever invocation
(8 canonical wayback snapshots, picks an average). Cached for 14 days.

```bash
# Refresh the calibration without running a scan:
kronikier --calibrate

# Run a scan *and* refresh the calibration first:
kronikier example.com --recalibrate
```

A stale or wrong calibration just makes the timeout→capacity estimate
slightly off; the deadline check is the real cutoff. The diagnostic
line at the end of every scan shows the observed vs. cached latency —
recalibrate if they drift significantly.

---

## CLI reference

```
kronikier <domain> [options]
kronikier --targets-file PATH [options]     # batch
kronikier --single-url URL [options]        # one specific page across time
kronikier --calibrate                       # one-off, no scan
kronikier --clear-cache                     # wipe the snapshot cache
```

### Timeout and mode (mutually exclusive)
| Flag                | Effect                                                                       |
| ------------------- | ---------------------------------------------------------------------------- |
| `--timeout SECONDS`  | Wall-clock timeout for the scan. `0` = unlimited. Default `300`.              |
| `--auto`            | Alias for `--timeout 300`. **Default when no flag is given.**                 |
| `--default`         | Alias for `--timeout 300`.                                                    |
| `--deep`            | Alias for `--timeout 900`.                                                    |
| `--exhaustive`      | Alias for `--timeout 0 --all`.                                                |

### Coverage modifiers
| Flag              | Effect                                                                       |
| ----------------- | ---------------------------------------------------------------------------- |
| `--all`           | Disable the contact-URL CDX filter. Independent of `--timeout`; combines.     |
| `--no-escalate`   | Disable the one-shot zero-result escalation (broaden filter / extend timeout).|

### Calibration
| Flag              | Effect                                                                       |
| ----------------- | ---------------------------------------------------------------------------- |
| `--calibrate`     | Refresh the latency cache and exit (no scan).                                |
| `--recalibrate`   | Refresh the calibration before this run.                                     |

### Input modes (mutually exclusive)
| Flag                  | Effect                                                                                                          |
| --------------------- | --------------------------------------------------------------------------------------------------------------- |
| `<domain>` (positional) | Default: scan all the contact-bearing pages of a host.                                                        |
| `--targets-file PATH` | Read a list of targets from PATH — one per line, `#` for comments, blanks ignored. Each entry is a bare domain or a full URL; URL paths extend that domain's well-known probe list. Multiple entries sharing a host are merged. |
| `--single-url URL`    | Scan only the captures of one exact URL (CDX `matchType=exact`). Probe and contact-URL filter are skipped — we already know what we're looking at. Useful for "how did this one page change?" timeline questions. |

### Scope
| Flag                  | Effect                                                                                          |
| --------------------- | ----------------------------------------------------------------------------------------------- |
| `--max-snapshots N`   | Optional hard ceiling on fetched pages (layered on top of timeout-derived capacity).             |
| `--from-year YYYY`    | Limit CDX query to >= year.                                                                     |
| `--to-year YYYY`      | Limit CDX query to <= year.                                                                     |
| `--no-subdomains`     | Restrict to exact host instead of `domain` + subdomains.                                        |
| `--no-probe`          | Skip the well-known-paths availability probe. The path list itself lives in `kronikier/data/well_known_paths.txt` — one path per line, `#` for comments — edit it to add domain-specific guesses without touching the code.|
| `--min-score N`       | Drop URLs whose path-classifier score is below N.                                               |

### Performance and resilience
| Flag                  | Effect                                                                                                            |
| --------------------- | ----------------------------------------------------------------------------------------------------------------- |
| `--workers N`         | Concurrent fetches (default 4).                                                                                   |
| `--rate F`            | Max requests/sec to wayback (default 4). Be polite to IA.                                                         |
| `--cdx-timeout N`     | Read-timeout for the CDX query in seconds (default 300). Raise for very large domains where IA's filter scan is slow. |

### Parsing
| Flag                | Effect                                                                                                       |
| ------------------- | ------------------------------------------------------------------------------------------------------------ |
| `--regions LIST`    | Phone regions to try for *bare local* numbers without `+` (default `RU,BY,UA,KZ,US,GB,DE,FR`). The domain's TLD is automatically prepended to this list with the right country in front: 70+ ccTLDs are mapped (`.by` → `BY`, `.fr` → `FR`, `.au` → `AU`, …), and generic TLDs (`.com / .org / .net / .io / .co / .app / .ai`) default to `US` since those domains are overwhelmingly US-anchored in practice. Override `--regions` when investigating a `.com` site that isn't. Numbers in international (`+`-prefixed) format don't need this. |

### Output
| Flag           | Effect                                                                                                          |
| -------------- | --------------------------------------------------------------------------------------------------------------- |
| `--csv PATH`   | Write the CSV report to PATH (default `<domain>_<YYYYMMDD_HHMMSS>.csv` in CWD).                                 |
| `--no-csv`     | Don't write a CSV.                                                                                              |
| `--json`       | Print a JSON object on stdout instead of the human-readable table. CSV (if not `--no-csv`) is still written and the path is reported on stderr. |
| `--no-progress`| Disable progress bars and the live contact feed. Auto-disabled when stderr is not a TTY.                        |
| `-v`           | Verbose contact feed: date + full snapshot URL under each found contact.                                        |
| `-d`           | DEBUG-level logs (probe / fetch / cache internals). Independent of `-v`.                                        |

### Snapshot cache
| Flag            | Effect                                                                                                            |
| --------------- | ----------------------------------------------------------------------------------------------------------------- |
| `--no-cache`    | Don't read from or write to the local snapshot cache during this run — every fetch goes to wayback.               |
| `--clear-cache` | Delete every cached snapshot file and exit. Use to free disk space; doesn't run a scan.                           |

The cache root is `$XDG_CACHE_HOME/kronikier/snapshots/` (default
`~/.cache/kronikier/snapshots/`), overridable with the `KRONIEKER_CACHE_DIR`
env var. See the dedicated section below for how cache files are laid out.

---

## Output formats

### Text (default)

```
Domain: theranos.com
Timeout: 300s | Elapsed: 88.4s | Considered: 4,201 snapshots | Fetched: 60 | Distinct contacts: 18
Plan: contact-URL filter on; will fetch top 800 of ~12,350,000 snapshots

Contact                              First seen   Last seen
───────────────────────────────────  ──────────   ──────────
email   info@theranos.com            2007-11-22   2016-04-03
phone   +1 650 838 9292              2014-09-02   2016-04-03
phone   +1 855 843 7200              2014-09-02   2015-11-18
…

CSV saved: ./theranos.com_20260520_134522.csv

Hint: Contact-URL filter was on. For obscure custom contact pages, try
`--all` (scan every URL) or `--exhaustive`.
```

Empty results don't print an empty table — they print `No contacts found.`
and skip the CSV file entirely.

### CSV (always written unless `--no-csv` AND there's at least one contact)

Columns:

```
kind, value, value_human, value_raw, first_seen, last_seen, sightings_count,
first_archive_url, last_archive_url
```

- `value` is the canonical form (E.164 phone, lowercased email).
- `value_human` is the INTERNATIONAL-formatted phone or the email as-is.
- `value_raw` is the *as-seen* literal text from the page(s), with
  distinct renderings joined by ` | `. E.g. a Belarusian landline shown
  as `8-0162-51-12-54` on one snapshot and `+375 162 51-12-54` on
  another lands in CSV as `8-0162-51-12-54 | +375 162 51-12-54`.

> **Phone reconstruction caveat.** The `value` column is a normalised
> E.164 form produced by libphonenumber — there's a guess about country
> code, trunk prefix, and grouping involved. Most of the time it's
> right, but on weird formats (truncated numbers, ambiguous regions,
> OCR typos) it can land on a plausible-but-wrong number. If a phone in
> `value` looks off, cross-check `value_raw` against the original page
> via `first_archive_url`. That's the ground truth.
- `first_archive_url` / `last_archive_url` link to the playback page on
  web.archive.org so a reviewer can verify the source by clicking.
- UTF-8 with BOM, so Excel opens it without garbled Cyrillic.

### JSON (`--json`)

```json
{
  "domain": "theranos.com",
  "timeout_seconds": 300.0,
  "elapsed_seconds": 88.4,
  "timeout_exhausted": false,
  "plan_rationale": "contact-URL filter on; will fetch top 800 of ~12,350,000 snapshots",
  "url_filter_active": true,
  "snapshots_considered": 4201,
  "snapshots_fetched": 60,
  "hint": "Contact-URL filter was on. For obscure custom contact pages, try `--all` (scan every URL) or `--exhaustive`.",
  "csv_path": "./theranos.com_20260520_134522.csv",
  "contacts": [
    {
      "kind": "email",
      "value": "info@theranos.com",
      "value_human": "info@theranos.com",
      "value_raw": "info@theranos.com",
      "first_seen": "2007-11-22",
      "last_seen": "2016-04-03",
      "sightings": 12,
      "first_archive_url": "https://web.archive.org/web/20071122…",
      "last_archive_url":  "https://web.archive.org/web/20160403…"
    }
  ],
  "errors": []
}
```

---

## Snapshot cache

Wayback snapshots are immutable, so rerunning the same scan can answer
without spending more IA bytes. The CLI keeps an on-disk cache of fetched
HTML, **enabled by default**.

Layout: one HTML file per `(timestamp, url)` pair, grouped by host:

```
~/.cache/kronikier/snapshots/
├── theranos.com/
│   ├── 20140902120000__contact-us__a3f9d4e1.html
│   └── 20120101000000__index.html__b2c01f9a.html
└── wirecard.com/
    └── 20151204093015__about-us__7e2d1a08.html
```

The filename is `{timestamp}__{sanitized-path}__{url-hash}.html` so each
file is browsable on disk — open it in a browser, grep it, diff
historical versions with `code -d`. The 8-char hash disambiguates URLs
that collapse to the same path after fs-sanitisation.

Only successful 200-OK HTML responses are cached. Errors, redirects, and
404s are intentionally not persisted (they could be transient). A scan
prints a one-line summary at the end:

```
[*] Cache: 152 hit / 47 miss (saved 152 fetches on web.archive.org)
```

Overrides:

- `--no-cache` — disable read+write for this run.
- `--clear-cache` — wipe and exit.
- `KRONIEKER_CACHE_DIR=/some/path` — use a different root.

The cache itself is best-effort; any filesystem error is treated as a
miss, never blocks the scan.

---

## Single-URL mode

`--single-url URL` switches the scanner from "find contacts on this host"
to "show me every archived snapshot of this exact URL". The CDX query
becomes `matchType=exact`, the well-known-paths probe is skipped, and
the contact-URL filter is bypassed — the analyst already chose the
page.

```bash
# Every archived version of theranos.com's leadership page:
kronikier --single-url https://www.theranos.com/leadership

# How did one specific subdomain page change over time?
kronikier --single-url https://news.theranos.com/2018/05/...
```

When to reach for it:

- A specific URL surfaced in another investigation and you want its
  archive timeline.
- The site has unusual structure and you want to inspect *one* page
  without paying for a host-wide scan.
- Rebuilding the chain of contact changes on a single contact page.

The URL must be an absolute `http://` or `https://` URL with a host.
`--single-url` is mutually exclusive with both the positional `<domain>`
and `--targets-file`.

---

## Common workflows

### Defunct site, all contacts ever

```bash
kronikier theranos.com --exhaustive --max-snapshots 500
```

Use `--exhaustive` (= `--timeout 0 --all`) when you want every snapshot
of every page (e.g. a contact page that changed numbers every quarter)
and you don't mind waiting. `--max-snapshots` caps the upper end if
the site is unexpectedly huge.

### Live site, missing contact info

```bash
kronikier mysite.ru
```

Default timeout (300 s) with the contact-URL filter on. If the first
pass finds nothing, the tool auto-broadens to `--all` semantics for
the same timeout. A final `Hint:` line suggests `--exhaustive` if
that's still not enough.

### Bulk OSINT pipeline

```bash
kronikier --targets-file targets.txt --no-progress
```

`targets.txt` is plain text, one entry per line — bare domains or full
URLs. `#` starts a comment; blank lines are ignored. Example:

```
# Suspects from case 2026-04
theranos.com
https://theranos.com/leadership                # extra probe path
https://theranos.com/contact-us
enron.com
https://www.theranos.com/leadership            # subdomain → separate scan
```

Mechanics:

- Multiple lines sharing a host **merge** into one scan. Their URL
  *paths* are added to the well-known probe list for that scan — so
  `/uslugi/excavator` and `/o-nas` become bonus probe targets in
  addition to the bundled `WELL_KNOWN_PATHS`.
- Different hosts (e.g. `theranos.com` and `www.theranos.com`) run as
  **separate** scans — subdomain semantics belong to CDX's
  `matchType=domain` and we don't second-guess.
- Calibration and the HTTPS connection pool are shared across the
  whole batch.
- Each scan writes its own CSV (default per-domain naming); `--csv PATH`
  is rejected in batch mode to avoid clobbering.
- `--targets-file` is mutually exclusive with the positional domain.

Old-school shell loop still works if you prefer per-target JSON files
in a directory:

```bash
for d in $(cat domains.txt); do
  kronikier "$d" --json --no-progress > "results/$d.json"
done
```

`--no-progress` + `--json` gives clean machine-readable output; CSV
files still land in the working directory.

### Restricting to a specific era

```bash
kronikier old-corp.com --timeout 900 --from-year 2008 --to-year 2012
```

Useful when a known event (a sale, a rebrand, a scandal) gives you a
specific window of interest. Affects the CDX query — well-known-paths
probing still samples five decades.

---

## Troubleshooting

### `CDX query failed: Read timed out`

Some giant sites (marketplace-scale, 5 000+ CDX pages) need longer than the
default 300 s timeout. Bump it:

```bash
kronikier huge-marketplace.example --cdx-timeout 900
```

The error message also prints this hint.

### "I see fewer contacts than I expect"

Three things to check, in order:

1. **Did the timeout cap the scan?** If `Timeout exhausted` shows in the
   header, raise it: `--timeout 900` or `--exhaustive`.
2. **Is the missing contact in a non-typical URL?** The default scan
   filters CDX by a regex of contact-y slugs (`/contact`, `/about`,
   etc.). Custom paths like `/get-in-touch` slip through the filter —
   add `--all` to scan every URL.
3. **Is the calibration stale?** Run `kronikier --calibrate` to
   refresh. A wrong calibration leads to a wrong capacity estimate
   (slower or faster than your machine really is).

### "A clearly-typo'd phone number is missing"

The tool keeps numbers that fail libphonenumber's strict validation — as
long as they're written with a leading `+`. Without `+` (bare local
form) the matcher stays strict; it's the only way to keep postal codes,
tax IDs, and order numbers out of the result.

### "A phone number's country code looks wrong"

For numbers written without a `+`, the parser tries libphonenumber
region-by-region and the first valid interpretation wins. The list is
prioritised by the domain's TLD (see `--regions` above), so:

- `theranos.com` → `US` first → `(855) 843-7200` parses as
  `+1 855-843-7200`.
- `avito.ru` → `RU` first → `8(863)-218-22-22` parses as
  `+7 863 218-22-22`.

If your target's site is on a TLD that doesn't match its real country
(e.g. a Russian SMB hosted on `.com`), override the order:

```bash
kronikier russian-smb.example.com --regions RU,US,GB
```

The CSV `value_raw` column always holds the as-seen original — pivot off
that when the canonical `value` is suspicious.

### "DEBUG details about what the fetcher / probe is doing"

```bash
kronikier example.com -d --no-progress
```

`-d` (or `--debug`) raises the log level to DEBUG. Useful when a fetch is
stalled and you want to see the URL it's stuck on, or when the cache
hits/misses don't match your mental model. Independent from `-v`, which
only controls the live contact feed verbosity.

### "An email on `mail.ru` / `gmail.com` is in my data — is that real?"

Yes. The tool does *no* domain-based filtering of emails. For OSINT a
contact email on a free provider is usually the strongest signal —
small-business sites use them everywhere. Cross-reference the
`first_archive_url` to verify on the actual snapshot.

---

## Programmatic use

The CLI is a thin wrapper around `kronikier.scan_domain`. Library
callers don't have to go through the CLI:

```python
from kronikier import scan_domain

result = scan_domain(
    "theranos.com",
    timeout_seconds=300,        # or 900, or 0 (= unlimited)
    force_all=False,            # equivalent of --all
    from_year=2007,
    to_year=2016,
    default_phone_regions=("US",),
)

for timestamp, kind, value, source_url in result.timeline():
    print(timestamp, kind, value, source_url)

print(
    f"Timeout: {result.timeout_seconds}s, elapsed: {result.elapsed_seconds:.1f}s, "
    f"exhausted={result.timeout_exhausted}"
)
```

Calibration is not auto-run from library code (it's a CLI-side UX
feature). If you want an accurate capacity estimate, pass your own
`Calibration`:

```python
from kronikier.calibration import ensure_calibration
cal = ensure_calibration(announce=False)
result = scan_domain("theranos.com", timeout_seconds=300, calibration=cal)
```

Other `scan_domain` keyword arguments map directly to CLI flags:

| Library kwarg              | CLI equivalent          |
| -------------------------- | ----------------------- |
| `timeout_seconds`          | `--timeout`             |
| `force_all`                | `--all`                 |
| `no_escalate`              | `--no-escalate`         |
| `max_snapshots`            | `--max-snapshots`       |
| `from_year` / `to_year`    | `--from-year` / `--to-year` |
| `include_subdomains`       | inverse of `--no-subdomains` |
| `probe_well_known`         | inverse of `--no-probe` |
| `min_score`                | `--min-score`           |
| `max_workers`              | `--workers`             |
| `rate_limit_per_sec`       | `--rate`                |
| `cdx_timeout`              | `--cdx-timeout`         |
| `default_phone_regions`    | `--regions`             |
| `extra_well_known_paths`   | URLs in `--targets-file` |
| `single_url`               | `--single-url`          |
| `cache`                    | (off if `--no-cache`)   |

The on-disk snapshot cache is *not* attached automatically when
`scan_domain` is called from a library — instantiate it yourself if you
want it:

```python
from kronikier.cache import SnapshotCache, default_cache_dir
cache = SnapshotCache(default_cache_dir())
result = scan_domain("theranos.com", cache=cache)
```

---

## Rate limits and ethics

- Default `--rate 4` (4 RPS to wayback) is polite. Don't raise it for
  routine work; IA serves the archive for everyone.
- All data extracted is already public — IA published it. The tool
  doesn't bypass paywalls, crawler-blocks, or any access control.
- Contacts in archives may belong to people no longer associated with
  the domain. The timeline (`first_seen` / `last_seen`) is there so you
  can judge what was current when.
- Use only within the scope of legal investigations.

---

## OSINT precedents

Wayback-for-contact-recovery is a well-documented technique. Publicly
referenced uses:

- **Bellingcat — Skripal / GRU (2018-2019).** Recovering archived
  versions of Russian military unit pages to obtain personnel contacts
  scrubbed after Salisbury.
- **Brian Krebs (KrebsOnSecurity).** Multiple investigations of malware
  and fraud site operators identified via archived contact emails.
- **OCCRP shell-company investigations.** "Contact" pages of shell sites
  routinely name the human beneficiary before being scrubbed.
- **John Carreyrou / WSJ — Theranos.** Material from archived
  `theranos.com` used as evidence after the company shut down.

The end-to-end test suite (`pytest -m e2e`) hits the live wayback for
`theranos.com` and `enron.com` and asserts that the canonical contacts
of both still surface — the OSINT use case is the regression test.
