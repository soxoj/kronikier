"""Email and phone number extraction from archived HTML.

The wayback machine preserves all kinds of late-90s / 2000s contact obfuscation
tricks (``user [at] domain [dot] ru``, HTML entities, Cloudflare email
protection, fullwidth ``＠``, ``mailto:`` with junk parameters, etc.). The
extractors here normalize an input page through several passes before running
the matchers, so a single deobfuscation rule helps both email and phone
recognition downstream.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Iterable, Iterator

import phonenumbers
from bs4 import BeautifulSoup
from phonenumbers import Leniency

# ---------------------------------------------------------------------------
# Public data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Contact:
    """A single contact datum extracted from a page."""

    kind: str  # "email" or "phone"
    value: str  # canonical form (lowercased email / E.164 phone)
    raw: str  # the substring that triggered the match


# ---------------------------------------------------------------------------
# Email extraction
# ---------------------------------------------------------------------------

# RFC 5322 is overkill — this is a pragmatic match for what shows up on
# real-world pages. We intentionally keep it permissive and filter known
# false positives later.
_EMAIL_RE = re.compile(
    r"""
    (?<![A-Za-z0-9._%+\-/])           # left boundary: not part of a path/email
    (
        [A-Za-z0-9._%+\-]{1,64}
        @
        [A-Za-z0-9.\-]{1,255}
        \.
        [A-Za-z]{2,24}
    )
    (?![A-Za-z0-9])                    # right boundary
    """,
    re.VERBOSE,
)

# [at] / (at) / {at} / " at " — but only when there's a domain-ish thing after.
_AT_OBFUSCATION_RE = re.compile(
    r"\s*[\[\(\{]\s*(?:at|@|собака|sobaka)\s*[\]\)\}]\s*"
    r"|\s+(?:at|@|собака|sobaka)\s+",
    re.IGNORECASE,
)
_DOT_OBFUSCATION_RE = re.compile(
    r"\s*[\[\(\{]\s*(?:dot|\.|точка|tochka)\s*[\]\)\}]\s*"
    r"|\s+(?:dot|точка|tochka)\s+",
    re.IGNORECASE,
)

# File-extension endings that look like emails but are asset paths
# (``logo@2x.png``, ``hero@3x.jpg``). This is the ONLY remaining email
# false-positive filter — we deliberately do NOT filter by domain. For OSINT
# every mailbox is potentially useful regardless of the host. Junk that's
# truly an asset path or JS escape leftover (``u003e``) is rejected below.
_EMAIL_FP_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".webp",
    ".css",
    ".js",
    ".woff",
    ".woff2",
    ".ttf",
    ".ico",
}


# Google tracking IDs like ``UA-36441709-1`` (Universal Analytics),
# ``AW-1234567890`` (Google Ads) and ``DC-1234567`` (DoubleClick legacy)
# embed long digit runs that ``PhoneNumberMatcher`` happily claims as
# Ukrainian phones (``UA`` is libphonenumber's region code for Ukraine and
# the digits inside the ID parse as a valid local 10-digit number under
# ``POSSIBLE`` leniency). They're never phone numbers — replacing them with
# same-length whitespace before the matcher runs neutralises the false
# positive without disturbing surrounding match spans.
_TRACKING_ID_RE = re.compile(r"\b(?:UA|AW|DC)-\d{2,}(?:-\d+)?\b")

# Decimal numbers with ≥4 fractional digits — the canonical geo-coordinate
# shape (``37.476600``, ``-122.144000``, ``51.5074``). The lookbehind /
# lookahead block dot-formatted US phones like ``555.123.4567`` from being
# clipped: those have at most 4 trailing digits BUT another dot bordering
# the run, which the assertions reject.
_COORD_RE = re.compile(r"(?<![\d.])-?\d{1,3}\.\d{4,}(?![\d.])")

# Calendar date with an optional clock suffix (``19.06.2020 / 12:48``,
# ``2020-06-19 12:48:30``, ``19/06/2020,12:48``) — libphonenumber sometimes
# claims fragments of these as phones, particularly when the time portion
# fuses with adjacent digits. Standalone clock patterns (``12:48``) are
# also blanked because they're never phone numbers on their own.
#
# The ``\s*`` slots between digit groups and separators tolerate dates that
# the HTML chopped across inline elements: BS4 (and the JS DOM walker) emit
# them as ``2020 / 06 / 19`` after separator-injection, which the strict
# form ``\d{4}/\d{2}/\d{2}`` would miss.
_DATETIME_RE = re.compile(
    r"""
    \b
    (?:
        \d{1,2}\s*[./\-]\s*\d{1,2}\s*[./\-]\s*(?:19|20)\d{2}   # DD.MM.YYYY
      | (?:19|20)\d{2}\s*[./\-]\s*\d{1,2}\s*[./\-]\s*\d{1,2}   # YYYY-MM-DD
    )
    (?:\s*[T,;/\-]?\s*\d{1,2}\s*[:.]\s*\d{2}(?:\s*[:.]\s*\d{2})?)?  # HH:MM[:SS]
    \b
    |
    \b\d{1,2}:\d{2}(?::\d{2})?\b        # standalone clock (colon-only —
                                          # ``19.45`` etc. is too ambiguous
                                          # with phone groups to blank).
    """,
    re.VERBOSE,
)

# ``(75) 2018`` — parenthesised 1-3 digit token immediately followed by a
# 19xx/20xx year. Common on press-release archive lists (``2018 (75 items)``
# or ``Press releases (75) 2018``). libphonenumber claims the 6-7 digit
# cluster as a German landline.
_PAREN_YEAR_RE = re.compile(r"\(\s*\d{1,3}\s*\)\s*(?:19|20)\d{2}\b")

# ``2012 2012`` — a 4-digit year repeated. Appears on dated archive listings
# (``© 2012 2012``) and gets claimed as an 8-digit phone.
_REPEATED_YEAR_RE = re.compile(r"\b((?:19|20)\d{2})\s+\1\b")

# ``596/2014`` — case-number / Aktenzeichen / ordinance citation style:
# 1-6 digit identifier, slash, 19xx/20xx year. MUST be blanked before the
# slash-in-phone bridge below so the bridge doesn't accidentally normalize
# the case-number to a phone-shaped ``596 2014``.
_CASE_NUMBER_RE = re.compile(r"\b\d{1,6}\s*/\s*(?:19|20)\d{2}\b")

# ``2020 19`` / ``2020 06 19`` — a 19xx/20xx year followed by 1-3 short
# numeric tokens (each ≤2 digits). Catches the URL-path date that survived
# tag-stripping when the path was tokenised onto separate lines, plus
# German-style date renderings like ``2020 06 19``. Pinned to year-leading
# and to ≤2-digit follow-ups so it doesn't eat phone groups.
_YEAR_NUM_CLUSTER_RE = re.compile(r"\b(?:19|20)\d{2}(?:\s+\d{1,2}){1,3}\b")

# Phone numbers written with ``/`` as a digit-group separator (a real
# German convention, e.g. ``+49175/5604673``). libphonenumber's matcher
# (Python VALID-leniency / libphonenumber-js default) does not treat
# ``/`` as a separator and stops at the slash. We bridge by substituting
# ``/`` → ``" "`` inside ``+``-anchored substrings (always a phone) AND
# inside bare digit clusters with a phone-shaped split (``2-4 digits /
# 6+ digits``). Case-numbers (``596/2014``) and dates (``2020/06/19``)
# are blanked upstream so the bare bridge can't accidentally capture them.
_PHONE_SLASH_BRIDGE_RE = re.compile(r"\+\d[\d\s\-()/]{6,}\d")
_PHONE_SLASH_BRIDGE_BARE_RE = re.compile(r"(?<!\d)\d{2,4}\s*/\s*\d{6,}(?!\d)")

# Phone-shaped bare digit cluster (``175 5604673``, post-bridge or native).
# Pass 3 walks these and tries ``phonenumbers.is_possible_number`` per
# region — VALID leniency in Pass 2 is too strict to catch local-only
# forms even when the surrounding context implies a phone.
_PHONE_BARE_CANDIDATE_RE = re.compile(r"(?<!\d)\d{2,4}\s+\d{6,}(?!\d)")

# Business-registration / tax / VAT identifiers carry long digit runs that
# look indistinguishable from phone numbers when they sit naked in text
# (``200604351`` is a Singapore UEN, ``7707083893`` a Russian INN, etc.).
# We blank a marker-anchored shape: a recognised label, optional
# separator (``:``, ``№``, ``-``, …), then a digit run of ≥6.  The label
# whitelist is intentionally broad — false positives here cost very little
# (text becomes spaces) and the alternative is leaking IDs as phones.
_REG_NUM_RE = re.compile(
    r"""
    \b(?:
        UEN | ACRA                                            # Singapore
      | (?:Co(?:mpany)?[.\s]*)? Reg(?:istration)?
            [.\s]* (?:No|Number)?                             # Reg./Co. Reg./Reg. No./...
      | Co(?:mpany)? [.\s]+ (?:No|Number)                     # "Company Number" / "Co. No."
      | HR[BA] | Handelsregister                              # Germany
      | VAT (?:[.\s]* (?:No|Number|Id))?                      # VAT (No|Number|Id)
      | USt [\s.\-]? Id (?:Nr|N)?                             # German VAT
      | INN | ИНН | ОГРН | КПП | ОКПО | БИК                   # Russia
      | Tax\s*ID                                              # generic tax
      | EIN                                                   # US IRS
      | CIN                                                   # India
      | BRN                                                   # generic BR No.
      | ISIN | WKN | CUSIP | SEDOL | FIGI                     # securities IDs
      | NIF | CIF                                             # ES/PT
      | CNPJ | CPF                                            # Brazil
      | I[ČC]O | DI[ČC]                                       # CZ/SK
      | NIP | REGON                                           # Poland
      | KvK                                                   # Netherlands
      | СНИЛС                                                 # Russia personal
    )
    [\s.:№#\-]*
    (?:[A-Z]{2,3})?           # optional 2-3 letter country prefix (DE259...)
    [\s.:№#\-]*
    \d{6,}
    [A-Z]?                    # optional trailing letter (Singapore UEN ``…-D``, …)
    \b
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Bare ISIN — 2 ISO country letters + 9 alphanumerics + 1 check digit.
# Wirecard's investor-relations pages embed ``ISIN DE0007472060`` and the
# digit run gets claimed as a German phone. Blanking the whole token kills
# the FP regardless of marker presence.
_ISIN_RE = re.compile(r"\b[A-Z]{2}[A-Z0-9]{9}\d\b")

# German postal-address fragment: a short house-number (``35``) followed by
# a 5-digit Deutsche Post PLZ (``85609``) and a capitalised city name
# (``Aschheim``). libphonenumber's DE matcher claims ``35 85609`` as a
# 7-digit landline; the only reliable distinguisher is the trailing
# capitalised word. Length-preserving blank of the digit pair leaves the
# city name visible (no extractor cares about it).
_GERMAN_POSTAL_ADDR_RE = re.compile(r"\b\d{1,4}\s+\d{5}\b(?=\s+[A-ZÄÖÜ])")

# HTML elements whose textContent must NOT be fed to the phone matcher.
# - ``<script>``: serialised JSON / SVG strings / coordinate arrays.
# - ``<style>``: numeric CSS values.
# - ``<svg>``: ``<polygon points="…">`` and ``<path d="…">`` runs of digits.
# - ``<noscript>``: usually a tracking/analytics fallback with IDs.
_DROP_TAGS = ("script", "style", "svg", "noscript")


def _normalize_html(content: str) -> str:
    """Decode entities and surface the text content of common deobfuscation tricks.

    - HTML entity decode (``&#64;`` → ``@``).
    - Cloudflare ``data-cfemail`` attribute decoding.
    - Fullwidth ``＠`` → ``@``, ``．`` → ``.``.
    - ``mailto:`` and ``tel:`` href values are pulled out as bare strings.
    - Google tracking IDs (``UA-…``, ``AW-…``, ``DC-…``) are blanked out so
      the phone matcher doesn't grab their digit runs.
    """
    soup = BeautifulSoup(content, "html.parser")

    # Pull out mailto: / tel: hrefs into the visible text so the regexes see them
    # even if the visible link text is obfuscated. (Done BEFORE dropping <script>
    # etc. since those tags don't carry hrefs anyway, but order is explicit.)
    extra_bits: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.lower().startswith("mailto:"):
            extra_bits.append(href[7:].split("?", 1)[0])
        elif href.lower().startswith("tel:"):
            extra_bits.append(href[4:])

    # Cloudflare email protection: <span class="__cf_email__" data-cfemail="...">
    for node in soup.find_all(attrs={"data-cfemail": True}):
        decoded = _decode_cfemail(node["data-cfemail"])
        if decoded:
            extra_bits.append(decoded)

    # Drop subtrees whose textContent is pure noise for contact extraction.
    for tag in soup.find_all(_DROP_TAGS):
        tag.decompose()

    text = soup.get_text(" ", strip=False)
    text = html.unescape(text)

    # Fullwidth normalization (cheap subset, just the common offenders).
    text = text.replace("＠", "@").replace("．", ".")

    # Collapse runs of whitespace (newlines, tabs, NBSP) to a single space.
    # Real-world breakage that drove this: phones split across block-level
    # elements (``+49<br>(0)89-4424<br>1400``) leave their digit groups on
    # separate lines, and libphonenumber treats a newline as a stronger
    # boundary than a space, so it stops at the first run and misses the
    # trailing digits. A single-space normalization bridges the gap and
    # leaves email matching unaffected.
    text = re.sub(r"\s+", " ", text)

    # Blank out tracking IDs, datetime stamps, geo coordinates and the
    # additional year-cluster / case-number patterns. Same length-preserving
    # substitution keeps phone span-dedup valid. Order matters:
    # ``_CASE_NUMBER_RE`` runs BEFORE the slash bridge so that case-numbers
    # have already been removed when the bridge swaps ``/`` for spaces.
    blank = lambda m: " " * len(m.group(0))  # noqa: E731
    text = _TRACKING_ID_RE.sub(blank, text)
    text = _DATETIME_RE.sub(blank, text)
    text = _PAREN_YEAR_RE.sub(blank, text)
    text = _REPEATED_YEAR_RE.sub(blank, text)
    text = _CASE_NUMBER_RE.sub(blank, text)
    text = _YEAR_NUM_CLUSTER_RE.sub(blank, text)
    text = _COORD_RE.sub(blank, text)
    text = _REG_NUM_RE.sub(blank, text)
    text = _ISIN_RE.sub(blank, text)
    text = _GERMAN_POSTAL_ADDR_RE.sub(blank, text)

    # Bridge ``/`` separators in phone-shaped substrings. Two passes:
    # the ``+``-anchored one for international forms, then the bare one
    # for plain-digit phones written with ``/`` (e.g. ``175/5604673``).
    # Both run AFTER reg-number / case-number / date blanking so they
    # can't capture identifiers that happen to use ``/``.
    slash_to_space = lambda m: m.group(0).replace("/", " ")  # noqa: E731
    text = _PHONE_SLASH_BRIDGE_RE.sub(slash_to_space, text)
    text = _PHONE_SLASH_BRIDGE_BARE_RE.sub(slash_to_space, text)

    if extra_bits:
        text = text + "\n" + "\n".join(extra_bits)
    return text


def _decode_cfemail(token: str) -> str | None:
    """Decode a Cloudflare ``data-cfemail`` token to a plain email."""
    try:
        key = int(token[:2], 16)
        decoded = "".join(
            chr(int(token[i : i + 2], 16) ^ key) for i in range(2, len(token), 2)
        )
        return decoded if "@" in decoded else None
    except (ValueError, IndexError):
        return None


def _deobfuscate_at_dot(text: str) -> str:
    """Replace [at]/[dot] style tokens with ``@``/``.``.

    This is applied as a second pass so that pages mixing real emails with
    obfuscated ones still surface both.
    """
    text = _AT_OBFUSCATION_RE.sub("@", text)
    text = _DOT_OBFUSCATION_RE.sub(".", text)
    return text


def _looks_like_real_email(addr: str) -> bool:
    """Syntax-only filter — domain is *never* used as a reason to reject
    a real mailbox, but a few unambiguous false-positive shapes are.

    Rejected:
      * asset paths matching email syntax (``logo@2x.png``)
      * JSON/JS escape sequences left over from minified source (``u003e``)
      * domains starting with ``www.`` (always an artifact of the at-
        obfuscation deobfuscator turning prose like ``"Archive at
        www.dgap.de"`` or ``"found at www.example.com"`` into a pseudo-
        email — real mail servers don't accept mailboxes on a ``www.``
        subdomain).

    Everything else is kept. The OSINT analyst decides what's noise.
    """
    addr_l = addr.lower()
    local, _, domain = addr_l.partition("@")
    if not local or not domain:
        return False
    if domain.startswith("www."):
        return False
    if any(addr_l.endswith(ext) for ext in _EMAIL_FP_EXTENSIONS):
        return False
    if any(bad in addr_l for bad in ("u003e", "u003c", "\\x")):
        return False
    return True


# Local-part words that are almost never real email mailboxes but are
# extremely common in English/Russian prose right before " at " / " на "
# (e.g. "Commercial support is available at nginx.com" gets the
# at-deobfuscator turning it into "available@nginx.com"). When a candidate
# email surfaces ONLY in the deobfuscated pass — i.e. it required ``at`` →
# ``@`` substitution to be visible — and its local part is one of these
# words, it's a deobfuscation artifact and we drop it. A real
# ``available@example.com`` written plainly is caught by Pass 1 and kept.
_PROSE_AT_STOPWORDS = frozenset({
    "available", "archived", "based", "found", "headquartered",
    "hosted", "listed", "located", "registered", "stationed",
    "displayed", "offered", "presented", "showcased", "stored",
    "published",
})


def extract_emails(content: str) -> Iterator[Contact]:
    """Yield unique email Contacts from an HTML (or plain-text) page.

    Two passes — original normalized text, then a deobfuscated copy with
    ``[at]`` / ``[dot]`` / bare-``at``/``dot`` rewrites applied. Pass 2 is
    stricter: candidates whose local part is a prose stopword (and that
    didn't already appear in Pass 1) are rejected as deobfuscation
    artifacts rather than real mailboxes.
    """
    text = _normalize_html(content)
    seen: set[str] = set()

    # Pass 1: text as-is.
    for match in _EMAIL_RE.finditer(text):
        raw = match.group(1)
        canonical = raw.lower().rstrip(".,;:")
        if canonical in seen:
            continue
        if not _looks_like_real_email(canonical):
            continue
        seen.add(canonical)
        yield Contact(kind="email", value=canonical, raw=raw)

    # Pass 2: with at-/dot-deobfuscation. Stop-word filter kicks in only
    # for matches that weren't already produced by Pass 1 — anything that
    # came in cleanly is a real mailbox regardless of local-part text.
    pass1_emails = set(seen)
    for match in _EMAIL_RE.finditer(_deobfuscate_at_dot(text)):
        raw = match.group(1)
        canonical = raw.lower().rstrip(".,;:")
        if canonical in seen:
            continue
        if not _looks_like_real_email(canonical):
            continue
        local = canonical.split("@", 1)[0]
        if canonical not in pass1_emails and local in _PROSE_AT_STOPWORDS:
            continue
        seen.add(canonical)
        yield Contact(kind="email", value=canonical, raw=raw)


# ---------------------------------------------------------------------------
# Phone extraction
# ---------------------------------------------------------------------------


# Calendar dates with a 4-digit year (``02.09.2008``, ``2008-09-02``,
# ``9/2/2008``) have phone-shaped digit runs and slip through
# libphonenumber's matcher under either leniency setting. The 4-digit-year
# anchor is the safest "this is a date, not a phone" signal — we deliberately
# don't filter 2-digit-year cases (``02.09.08``) since those are genuinely
# ambiguous and the project rule is to surface, not over-filter.
_DATE_LIKE_RE = re.compile(
    r"""
    ^\s*
    (?:
        \d{1,2}[./\-]\d{1,2}[./\-]\d{4}   # DD.MM.YYYY / D-M-YYYY / etc.
      | \d{4}[./\-]\d{1,2}[./\-]\d{1,2}   # YYYY.MM.DD / YYYY-M-D
    )
    \s*$
    """,
    re.VERBOSE,
)


def _looks_like_date(raw: str) -> bool:
    """True if ``raw`` is a clear calendar date with a 4-digit year."""
    return bool(_DATE_LIKE_RE.match(raw))


def extract_phones(
    content: str,
    default_regions: Iterable[str] = ("RU", "US", "GB", "DE", "FR"),
) -> Iterator[Contact]:
    """Yield unique phone Contacts using libphonenumber.

    ``default_regions`` is tried in order for numbers written without a leading
    ``+``. Most archived sites we care about are Russian small businesses
    from the 2000s, so RU is the default first hop.

    Two-pass leniency strategy:

    1. **Pass 1 — international form (with ``+``):** ``Leniency.POSSIBLE``.
       A leading ``+`` is a strong intent signal — postal codes, tax IDs and
       order numbers don't carry one. So even site-side typos like
       ``+375-33-354518`` (one digit short of a real BY mobile) are surfaced
       and E.164-normalized.

    2. **Pass 2 — bare local (region-prefixed):** default ``Leniency.VALID``.
       Without a leading ``+`` we can't tell intent from form, and POSSIBLE
       starts matching 6-digit Belarusian postal codes (``225006``), 9-digit
       UNPs (``290506581``) and other identifiers. STRICT here keeps Pass 2
       clean.
    """
    text = _normalize_html(content)
    seen: set[str] = set()

    # Pass 1: numbers in *explicit* international format.
    #
    # The leading ``+`` is the only reliable signal that a digit string was
    # written as an international number — without it, libphonenumber's
    # POSSIBLE leniency will treat ``8`` (Russian IDD) or ``00`` (European
    # IDD) as international-call prefixes and reinterpret an obvious local
    # number as a foreign one. Real-world breakage that drove this filter:
    #
    #   ``(855) 843-7200`` (US toll-free)   → +7 8558437200  (wrong)
    #   ``8(863)-218-22-22`` (RU Rostov)    → +1 8632182222 (wrong)
    #
    # In both, the trunk/IDD ambiguity bit Pass 1, which then claimed the
    # seen-set so Pass 2 (region-aware) never got to try. Requiring a
    # literal ``+`` defers all unprefixed digit runs to Pass 2 where the
    # ccTLD-prioritised region order makes the right call.
    for match in phonenumbers.PhoneNumberMatcher(text, None, leniency=Leniency.POSSIBLE):
        if "+" not in match.raw_string:
            continue
        if _looks_like_date(match.raw_string):
            continue
        canonical = phonenumbers.format_number(
            match.number, phonenumbers.PhoneNumberFormat.E164
        )
        if canonical in seen:
            continue
        seen.add(canonical)
        yield Contact(kind="phone", value=canonical, raw=match.raw_string)

    # Pass 2: try each default region for any remaining bare local numbers.
    # Default (VALID) leniency — avoids postcode/INN/UNP false positives.
    #
    # Position-based dedup: ``8(863)-218-22-22`` is a valid number in both
    # RU (`+7…`, trunk 8) and US (`+1…`, IDD 8), so iterating regions in
    # order would emit both interpretations. We track the spans we've
    # already emitted from and skip later regions whose match overlaps —
    # the first region in ``default_regions`` wins, which is exactly the
    # ccTLD-prioritised order ``_regions_for_domain`` set up.
    emitted_spans: list[tuple[int, int]] = []

    def _overlaps_existing(start: int, end: int) -> bool:
        return any(s < end and start < e for s, e in emitted_spans)

    for region in default_regions:
        for match in phonenumbers.PhoneNumberMatcher(text, region):
            if _looks_like_date(match.raw_string):
                continue
            span_start = match.start
            span_end = span_start + len(match.raw_string)
            if _overlaps_existing(span_start, span_end):
                continue
            canonical = phonenumbers.format_number(
                match.number, phonenumbers.PhoneNumberFormat.E164
            )
            if canonical in seen:
                emitted_spans.append((span_start, span_end))
                continue
            seen.add(canonical)
            emitted_spans.append((span_start, span_end))
            yield Contact(kind="phone", value=canonical, raw=match.raw_string)

    # Pass 3 — phone-shaped bare digit clusters that VALID leniency rejects
    # but ``is_possible_number`` accepts. Targets the ``\d{2,4} \d{6,}`` shape
    # (which the slash bridge in ``_normalize_html`` produces for inputs like
    # ``175/5604673``, but which also catches natively-written ``Tel.: 175
    # 5604673`` shapes that Pass 2 misses). All upstream blanking (datetime,
    # case-number, year-cluster, reg-num) has already run, so these clusters
    # are genuine phone candidates — not identifiers in disguise.
    for match in _PHONE_BARE_CANDIDATE_RE.finditer(text):
        span_start, span_end = match.span()
        if _overlaps_existing(span_start, span_end):
            continue
        candidate = match.group(0)
        if _looks_like_date(candidate):
            continue
        for region in default_regions:
            try:
                num = phonenumbers.parse(candidate, region)
            except phonenumbers.NumberParseException:
                continue
            if not phonenumbers.is_possible_number(num):
                continue
            canonical = phonenumbers.format_number(
                num, phonenumbers.PhoneNumberFormat.E164
            )
            emitted_spans.append((span_start, span_end))
            if canonical in seen:
                break
            seen.add(canonical)
            yield Contact(kind="phone", value=canonical, raw=candidate)
            break


# ---------------------------------------------------------------------------
# Combined entrypoint
# ---------------------------------------------------------------------------


def extract_contacts(
    content: str,
    default_regions: Iterable[str] = ("RU", "US", "GB", "DE", "FR"),
) -> list[Contact]:
    """Extract emails + phones from a single HTML/text blob."""
    return [*extract_emails(content), *extract_phones(content, default_regions)]
