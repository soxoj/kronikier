"""Unit tests for the URL classifier heuristics."""

from __future__ import annotations

import pytest

from kronikier.classifier import (
    WELL_KNOWN_PATHS,
    is_probably_contact_page,
    score_url,
)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/contact",
        "https://example.com/contacts",
        "https://example.com/contact-us",
        "https://example.com/contact.html",
        "https://example.com/contact.php",
        "https://example.com/contacts/",
        "https://example.com/about",
        "https://example.com/about-us.html",
        "https://example.com/company/team",
        "https://example.com/impressum",
        "https://example.com/kontakty",
        "https://example.com/o-nas",
        "https://example.com/о-нас",
        "https://example.com/контакты",
        "https://example.com/о_компании",
    ],
)
def test_high_value_paths_classified(url):
    assert is_probably_contact_page(url), url


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/tag/python",
        "https://example.com/wp-content/uploads/x.jpg",
        "https://example.com/feed",
        "https://example.com/search?q=hi",
        "https://example.com/blog/2007/01/hello",
    ],
)
def test_low_signal_paths(url):
    assert score_url(url) < 8, url


def test_homepage_gets_modest_score():
    # Homepage isn't an "about" page but its footer often has contacts —
    # we want to fetch it even at min_score=5.
    assert score_url("https://example.com/") >= 5
    assert score_url("https://example.com") >= 5


def test_assets_get_zero():
    assert score_url("https://example.com/static/logo.png") == 0
    assert score_url("https://example.com/assets/app.js") == 0


def test_score_higher_for_high_value_than_blog():
    assert score_url("https://example.com/contact") > score_url(
        "https://example.com/blog/post"
    )


def test_well_known_paths_nonempty():
    assert "/contacts" in WELL_KNOWN_PATHS
    assert "/about" in WELL_KNOWN_PATHS
    assert "/" in WELL_KNOWN_PATHS


def test_well_known_paths_loaded_from_data_file():
    """``WELL_KNOWN_PATHS`` is built from ``data/well_known_paths.txt``.

    Every entry must start with ``/`` (the loader rejects others), comments
    and blanks are dropped, and cyrillic paths survive verbatim.
    """
    assert all(p.startswith("/") for p in WELL_KNOWN_PATHS), WELL_KNOWN_PATHS
    # Cyrillic — these would never come back from the ASCII-only CDX urlkey
    # filter, so they're load-bearing for non-English contact pages.
    assert "/контакты" in WELL_KNOWN_PATHS
    assert "/о-нас" in WELL_KNOWN_PATHS
    # No duplicates.
    assert len(WELL_KNOWN_PATHS) == len(set(WELL_KNOWN_PATHS))


def _stage_fixture(tmp_path, monkeypatch, filename: str, body: str) -> None:
    """Stage ``data/<filename>`` under ``tmp_path`` and point the loader at it.

    The real loader joins ``files('kronikier') / 'data' / FNAME``,
    so we monkeypatch ``files`` to return ``tmp_path`` — pathlib's
    ``__truediv__`` then chains naturally.
    """
    from kronikier import classifier as cls

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / filename).write_text(body, encoding="utf-8")

    monkeypatch.setattr(cls, "files", lambda pkg: tmp_path)
    monkeypatch.setattr(cls, "_WELL_KNOWN_PATHS_RESOURCE", filename)


def test_well_known_paths_loader_rejects_bare_word(tmp_path, monkeypatch):
    """A malformed file (path without leading ``/``) raises with line number."""
    from kronikier import classifier as cls

    _stage_fixture(tmp_path, monkeypatch, "bad.txt", "/contact\nabout\n/team\n")

    with pytest.raises(ValueError, match=r":2:.*must start with '/'"):
        cls._load_well_known_paths()


def test_well_known_paths_loader_ignores_comments_and_blanks(tmp_path, monkeypatch):
    from kronikier import classifier as cls

    _stage_fixture(
        tmp_path, monkeypatch, "paths.txt",
        "# header comment\n"
        "\n"
        "/contact\n"
        "  /about  \n"   # whitespace trimmed
        "/contact\n"     # duplicate dropped
        "# inline section\n"
        "/team\n",
    )

    paths = cls._load_well_known_paths()
    assert paths == ("/contact", "/about", "/team")
