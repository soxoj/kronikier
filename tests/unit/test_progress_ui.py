"""Tests for the ProgressUI's per-contact announcement gating."""

from __future__ import annotations

from kronikier.progress_ui import ProgressUI


def _capture(ui: ProgressUI) -> list[str]:
    """Hook ui.console.print and collect rendered lines as strings."""
    captured: list[str] = []
    ui.console.print = lambda s: captured.append(str(s))  # type: ignore[assignment]
    return captured


def test_announce_disabled_emits_nothing():
    ui = ProgressUI(enabled=False, verbose=True)
    captured = _capture(ui)
    ui.announce_contact("email", "info@x.ru", "2024-02-28", "https://web.archive.org/web/x/")
    assert captured == []


def test_announce_default_hides_date_and_url():
    """Without --verbose, only the contact line is shown."""
    ui = ProgressUI(enabled=True, verbose=False)
    captured = _capture(ui)
    url = "https://web.archive.org/web/20240228000000/https://small-ru.example/kontakty"

    ui.announce_contact("email", "info@x.ru", "2024-02-28", url)

    assert len(captured) == 1
    assert "info@x.ru" in captured[0]
    assert "2024-02-28" not in captured[0]
    assert "web.archive.org" not in captured[0]


def test_announce_verbose_adds_date_and_full_snapshot_url():
    """With --verbose, a second dim line carries date + the snapshot URL."""
    ui = ProgressUI(enabled=True, verbose=True)
    captured = _capture(ui)
    url = "https://web.archive.org/web/20240228000000/https://small-ru.example/kontakty"

    ui.announce_contact("email", "info@x.ru", "2024-02-28", url)

    assert len(captured) == 2
    assert "info@x.ru" in captured[0]
    assert "2024-02-28" in captured[1]
    assert url in captured[1]
