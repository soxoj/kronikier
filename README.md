# kronieker

OSINT tool that mines **historical** contacts (email, phone numbers) for a
domain out of **web.archive.org** snapshots. Built for investigations
where the current site no longer shows contact details (or shows
different ones) but earlier versions are preserved in the archive.

[![asciicast](https://github.com/user-attachments/assets/bc24bdd0-f483-41f5-82ae-cb0418f72389)](https://asciinema.org/a/kZErdPENZJlnSEjA)

**Full docs:** [docs/USAGE.md](docs/USAGE.md) ·
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## Quick start

```bash
pip install -e .
```

```bash
# Default scan: 5-minute timeout, contact-URL filter on.
kronieker theranos.com

# How did one specific page evolve across snapshots?
kronieker --single-url https://www.theranos.com/contact-us

# Long-running thorough scan, every URL the host has.
kronieker wirecard.com --exhaustive

# Batch a list of targets.
kronieker --targets-file targets.txt
```

The first ever invocation runs a one-time latency calibration (~3-5 s,
cached for 14 days). Every run writes a CSV next to the current
directory and prints a summary table to the terminal:

```
Contact                       First seen   Last seen
─────────────────────────     ──────────   ──────────
email   info@theranos.com     2014-02-08   2015-02-24
phone   +1 855-843-7200       2014-09-02   2015-11-18

CSV saved: ./theranos.com_20260529_200437.csv
[*] Cache: 3 hit / 0 miss (saved 3 fetches on web.archive.org)
```

That's it for getting started. See [docs/USAGE.md](docs/USAGE.md) for the
full CLI reference, batch-input format, snapshot-cache controls, and
troubleshooting.

---

## What you get

Every run produces:

- **A monospace summary table** on the terminal — first/last seen
  timestamps per contact, ordered chronologically.
- **A CSV file** with nine columns:
  `kind, value, value_human, value_raw, first_seen, last_seen,
  sightings_count, first_archive_url, last_archive_url`. UTF-8 with BOM
  so Excel opens non-ASCII content cleanly.
- **A live contact feed** in the terminal (`+ email …`, `+ phone …`) as
  results stream in. `-v` adds the snapshot date + URL beneath each one.

> **Phone numbers are reconstructed, not copied.** The `value` column is
> a normalised E.164 form produced by libphonenumber from whatever
> digits and punctuation appeared on the page — there's a guess about
> country code, trunk prefix, and grouping involved. Most of the time
> it's right, but on weird formats (truncated numbers, ambiguous
> regions, OCR-style typos) it can land on a plausible-but-wrong number.
> **If a phone in the `value` column looks off, always cross-check
> `value_raw` to see exactly how it appeared on the page** — that's the
> ground truth.

`--json` switches the table to a machine-readable JSON object on stdout;
CSV is still written and the path is reported on stderr.

Empty results print `No contacts found.` and skip the CSV entirely.

---

## When this is the right tool

The Wayback Machine is often the only source of authoritative owner
information when a site:

- is dead;
- was re-registered and the contacts scrubbed;
- changed editorial team and removed the previous people in charge;
- surfaced in a dubious story and the phone numbers vanished overnight.

For everything else (a live site whose contacts page works), just open
the live site.

---

## Snapshot cache

Wayback snapshots are immutable, so rerunning the same scan can answer
without spending more IA bytes. The CLI keeps an on-disk cache, **on by
default**, at `~/.cache/kronieker/snapshots/`. One HTML file per
snapshot, browsable on disk, grouped by host.

Disable with `--no-cache`. Wipe with `--clear-cache`.

Full layout, env-var override, and best-effort semantics are in
[docs/USAGE.md](docs/USAGE.md#snapshot-cache).

---

## Rate limits and ethics

- Default `--rate 4` (4 req/sec to wayback) is polite. Don't raise it
  without a reason — the archive serves everyone.
- All data extracted is already public. The tool doesn't bypass
  paywalls, crawler blocks, or any access control.
- Contacts in the archive may belong to people no longer associated
  with the domain. The timeline columns (`first_seen` / `last_seen`)
  are there so you can judge what was current when.
- Use only within the scope of legal investigations.

---

## OSINT precedents

The "recover contacts from archives" technique is documented across
many published investigations. A few:

- **John Carreyrou / WSJ — Theranos.** Material from archived
  `theranos.com` used as evidence after the company shut down.
- **Brian Krebs (KrebsOnSecurity).** Multiple investigations of malware
  and fraud site operators identified via archived contact emails on
  early versions of their landing pages.
- **OCCRP shell-company investigations.** "Contact" pages of shell
  sites routinely name the human beneficiary before being scrubbed.
- **Bellingcat.** Applied the same technique across multiple
  investigations.

The end-to-end test suite (`pytest -m e2e`) hits the live wayback for
`theranos.com` and `enron.com` and asserts that the canonical contacts
of both still surface — the OSINT use case is the regression test.

---

## License

[MIT](LICENSE).
