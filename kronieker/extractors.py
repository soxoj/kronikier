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


def _normalize_html(content: str) -> str:
    """Decode entities and surface the text content of common deobfuscation tricks.

    - HTML entity decode (``&#64;`` → ``@``).
    - Cloudflare ``data-cfemail`` attribute decoding.
    - Fullwidth ``＠`` → ``@``, ``．`` → ``.``.
    - ``mailto:`` and ``tel:`` href values are pulled out as bare strings.
    """
    soup = BeautifulSoup(content, "html.parser")

    # Pull out mailto: / tel: hrefs into the visible text so the regexes see them
    # even if the visible link text is obfuscated.
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

    text = soup.get_text(" ", strip=False)
    text = html.unescape(text)

    # Fullwidth normalization (cheap subset, just the common offenders).
    text = text.replace("＠", "@").replace("．", ".")

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
    """Syntax-only filter — domain is *never* used as a reason to reject.

    Rejected:
      * asset paths matching email syntax (``logo@2x.png``)
      * JSON/JS escape sequences left over from minified source (``u003e``)

    Everything else is kept. The OSINT analyst decides what's noise.
    """
    addr_l = addr.lower()
    local, _, domain = addr_l.partition("@")
    if not local or not domain:
        return False
    if any(addr_l.endswith(ext) for ext in _EMAIL_FP_EXTENSIONS):
        return False
    if any(bad in addr_l for bad in ("u003e", "u003c", "\\x")):
        return False
    return True


def extract_emails(content: str) -> Iterator[Contact]:
    """Yield unique email Contacts from an HTML (or plain-text) page."""
    text = _normalize_html(content)
    seen: set[str] = set()
    for pass_text in (text, _deobfuscate_at_dot(text)):
        for match in _EMAIL_RE.finditer(pass_text):
            raw = match.group(1)
            canonical = raw.lower().rstrip(".,;:")
            if canonical in seen:
                continue
            if not _looks_like_real_email(canonical):
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


# ---------------------------------------------------------------------------
# Combined entrypoint
# ---------------------------------------------------------------------------


def extract_contacts(
    content: str,
    default_regions: Iterable[str] = ("RU", "US", "GB", "DE", "FR"),
) -> list[Contact]:
    """Extract emails + phones from a single HTML/text blob."""
    return [*extract_emails(content), *extract_phones(content, default_regions)]
