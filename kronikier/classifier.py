"""Heuristics for spotting contact-bearing URLs.

A domain can have tens of thousands of archived snapshots — too many to fetch
naively. We rank URLs by how likely the *path* is to carry contact info
(``/contacts``, ``/about``, ``/impressum``, ``/о-нас``, ...) and prefer those.

A score of ``0`` does NOT mean "skip" — homepages are still worth fetching, the
footer almost always carries phone/email. The pipeline uses the score for
ordering and as a cap on how many low-signal pages to bother with.
"""

from __future__ import annotations

import re
from importlib.resources import files
from urllib.parse import unquote, urlparse

# Slugs that strongly indicate a contact / about / imprint page, in any
# language. Match against URL path segments (case-insensitive).
_HIGH_VALUE_SLUGS = {
    # English
    "contact",
    "contacts",
    "contact-us",
    "contactus",
    "get-in-touch",
    "reach-us",
    "about",
    "about-us",
    "aboutus",
    "company",
    "team",
    "staff",
    "people",
    "leadership",
    "management",
    "imprint",
    "impressum",
    "legal",
    "footer",
    "support",
    "help",
    # Russian (transliterated + cyrillic — we URL-decode before matching)
    "kontakt",
    "kontakty",
    "kontakti",
    "svyaz",
    "obratnaya-svyaz",
    "obratnaya_svyaz",
    "o-nas",
    "o_nas",
    "onas",
    "o-kompanii",
    "o_kompanii",
    "rekvizity",
    "контакты",
    "контакт",
    "о-нас",
    "о_нас",
    "о-компании",
    "о_компании",
    "реквизиты",
    "обратная-связь",
    "связь",
    # German / French / Spanish / Italian
    "kontakt",
    "ueber-uns",
    "über-uns",
    "ueberuns",
    "nous-contacter",
    "a-propos",
    "à-propos",
    "contacto",
    "contatti",
    "chi-siamo",
    "quienes-somos",
}

# Slugs that hint a page is non-contact (we deprioritize, not skip).
_LOW_VALUE_SLUGS = {
    "tag",
    "tags",
    "category",
    "categories",
    "search",
    "feed",
    "rss",
    "comments",
    "trackback",
    "page",
    "wp-content",
    "wp-admin",
    "wp-includes",
    "static",
    "assets",
    "media",
    "img",
    "images",
    "css",
    "js",
}

_HIGH_VALUE_RE = re.compile(
    r"(?:^|[/_\-])(" + "|".join(re.escape(s) for s in _HIGH_VALUE_SLUGS) + r")(?:$|[/_.\-])",
    re.IGNORECASE,
)

# Well-known relative paths to probe when the CDX scan misses a contact page.
# The list lives in ``data/well_known_paths.txt`` (bundled with the package)
# so analysts can tweak / extend it without re-deploying. Format: one path
# per line, ``#`` for comments, blank lines ignored, leading/trailing
# whitespace trimmed, duplicates silently dropped.
_WELL_KNOWN_PATHS_RESOURCE = "well_known_paths.txt"


def _load_well_known_paths() -> tuple[str, ...]:
    raw = (files("kronikier") / "data" / _WELL_KNOWN_PATHS_RESOURCE).read_text(
        encoding="utf-8"
    )
    seen: set[str] = set()
    out: list[str] = []
    for lineno, raw_line in enumerate(raw.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if not line.startswith("/"):
            raise ValueError(
                f"{_WELL_KNOWN_PATHS_RESOURCE}:{lineno}: entry must start "
                f"with '/', got {line!r}"
            )
        if line in seen:
            continue
        seen.add(line)
        out.append(line)
    return tuple(out)


WELL_KNOWN_PATHS: tuple[str, ...] = _load_well_known_paths()


def score_url(url: str) -> int:
    """Return a heuristic score; higher means more likely a contact page.

    - +10 per high-value slug match.
    - -5 if the path contains a low-value slug.
    - +3 for a short path (homepages / top-level "about" style URLs).
    - 0 for query-only / asset URLs.
    """
    parsed = urlparse(url)
    path = unquote(parsed.path or "/")
    if not path or path == "/":
        return 5  # homepage — footer often has contacts

    lower_segments = [seg.lower() for seg in path.strip("/").split("/") if seg]
    if not lower_segments:
        return 5

    # Cheap asset filter
    last = lower_segments[-1]
    if "." in last:
        ext = "." + last.rsplit(".", 1)[-1]
        if ext in {".png", ".jpg", ".jpeg", ".gif", ".css", ".js", ".ico", ".woff", ".pdf", ".zip", ".xml"}:
            return 0

    score = 0
    if _HIGH_VALUE_RE.search(path):
        score += 10
    if any(seg in _LOW_VALUE_SLUGS for seg in lower_segments):
        score -= 5
    if len(lower_segments) <= 2:
        score += 3
    return score


def is_probably_contact_page(url: str) -> bool:
    return score_url(url) >= 8


# Server-side CDX ``filter=urlkey:`` regex built from the ASCII subset of
# high-value slugs. We deliberately exclude cyrillic / non-ASCII slugs because
# those appear in CDX urlkeys as %-encoded bytes; the well-known probing
# stage already covers those paths directly via the availability API.
def _build_cdx_urlkey_filter() -> str:
    ascii_slugs = sorted({s for s in _HIGH_VALUE_SLUGS if s.isascii()})
    return ".*(?:" + "|".join(re.escape(s) for s in ascii_slugs) + ").*"


CDX_URLKEY_FILTER: str = _build_cdx_urlkey_filter()
