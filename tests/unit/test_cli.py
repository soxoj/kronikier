"""Unit tests for CLI formatters and CSV writer."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from kronieker.report import (
    _Row,
    _aggregate_rows,
    _default_csv_path,
    _format_table,
    _human_date,
    _human_phone,
    _sanitize_filename,
    _write_csv,
)
from kronieker.extractors import Contact
from kronieker.pipeline import ContactSighting, ScanResult


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------


class TestHumanDate:
    @pytest.mark.parametrize(
        "ts,expected",
        [
            ("20070815120000", "2007-08-15"),
            ("19990101000000", "1999-01-01"),
            ("20240229", "2024-02-29"),  # 8-char short form
        ],
    )
    def test_valid(self, ts, expected):
        assert _human_date(ts) == expected

    @pytest.mark.parametrize("ts", ["", "abc", "2024", None])
    def test_garbage_passes_through(self, ts):
        # None → "", short/non-digit → unchanged-ish, never raises
        result = _human_date(ts or "")
        assert isinstance(result, str)


class TestHumanPhone:
    def test_e164_to_international(self):
        # Wirecard's real archived number
        assert _human_phone("+498944241400") == "+49 89 44241400"

    def test_invalid_falls_back_to_input(self):
        assert _human_phone("not-a-phone") == "not-a-phone"


class TestFormatTable:
    def test_basic_layout(self):
        out = _format_table(
            rows=[["email  info@x.ru", "2007-08-15", "2019-12-06"]],
            headers=["Contact", "First seen", "Last seen"],
        )
        lines = out.split("\n")
        assert lines[0].startswith("Contact")
        # Header separator line (Unicode dashes)
        assert "─" in lines[1]
        assert "info@x.ru" in lines[2]

    def test_empty_rows_keeps_header(self):
        out = _format_table([], headers=["Contact", "First seen", "Last seen"])
        lines = out.split("\n")
        assert "Contact" in lines[0]
        assert "─" in lines[1]

    def test_columns_align_to_widest_cell(self):
        out = _format_table(
            [["short", "a", "b"], ["a very long contact value", "x", "y"]],
            headers=["C", "F", "L"],
        )
        lines = out.split("\n")
        # Column 1 should be at least as wide as "a very long contact value"
        assert all(len(line) >= len("a very long contact value") for line in lines if line.strip())


# ---------------------------------------------------------------------------
# Filename sanitization
# ---------------------------------------------------------------------------


class TestSanitize:
    @pytest.mark.parametrize(
        "raw,clean_substr",
        [
            ("wirecard.com", "wirecard.com"),
            ("foo/bar?baz", "foo_bar_baz"),
            ("http://example.com/", "http_example.com"),
            ("", "domain"),  # empty fallback
        ],
    )
    def test_sanitize(self, raw, clean_substr):
        assert _sanitize_filename(raw) == clean_substr or clean_substr in _sanitize_filename(raw)

    def test_default_path_contains_domain_and_timestamp(self):
        p = _default_csv_path("wirecard.com")
        assert p.name.startswith("wirecard.com_")
        assert p.name.endswith(".csv")
        # YYYYMMDD_HHMMSS = 15 chars + .csv = 19
        assert len(p.name) >= len("wirecard.com_") + len("20260519_143015") + len(".csv")


# ---------------------------------------------------------------------------
# Row aggregation
# ---------------------------------------------------------------------------


def _mk_sighting(value: str, kind: str, ts: str, raw: str | None = None) -> ContactSighting:
    return ContactSighting(
        contact=Contact(kind=kind, value=value, raw=raw if raw is not None else value),
        snapshot_url=f"https://web.archive.org/web/{ts}/http://x.ru/c",
        timestamp=ts,
        source_url="http://x.ru/c",
    )


def test_aggregate_rows_picks_first_and_last_sightings():
    result = ScanResult(
        domain="x.ru",
        snapshots_considered=3,
        snapshots_fetched=3,
        sightings=[
            _mk_sighting("info@x.ru", "email", "20100101000000"),
            _mk_sighting("info@x.ru", "email", "20070101000000"),  # earliest
            _mk_sighting("info@x.ru", "email", "20190101000000"),  # latest
            _mk_sighting("+74951234567", "phone", "20080101000000"),
        ],
    )
    rows = _aggregate_rows(result)
    by_value = {r.value: r for r in rows}

    assert by_value["info@x.ru"].first_ts == "20070101000000"
    assert by_value["info@x.ru"].last_ts == "20190101000000"
    assert by_value["info@x.ru"].sightings == 3
    # Phone gets human-formatted
    assert by_value["+74951234567"].value_human == "+7 495 123-45-67"


def test_aggregate_rows_sorted_by_first_seen():
    result = ScanResult(
        domain="x.ru",
        snapshots_considered=2,
        snapshots_fetched=2,
        sightings=[
            _mk_sighting("late@x.ru", "email", "20200101000000"),
            _mk_sighting("early@x.ru", "email", "20050101000000"),
        ],
    )
    rows = _aggregate_rows(result)
    assert [r.value for r in rows] == ["early@x.ru", "late@x.ru"]


# ---------------------------------------------------------------------------
# CSV writer
# ---------------------------------------------------------------------------


def test_csv_roundtrip(tmp_path: Path):
    rows = [
        _Row(
            kind="email",
            value="info@x.ru",
            value_human="info@x.ru",
            value_raw="info@x.ru",
            first_ts="20070815120000",
            last_ts="20191206080000",
            sightings=3,
            first_url="https://web.archive.org/web/20070815120000/http://x.ru/c",
            last_url="https://web.archive.org/web/20191206080000/http://x.ru/c",
        ),
        _Row(
            kind="phone",
            value="+74951234567",
            value_human="+7 495 123-45-67",
            value_raw="8 (495) 123-45-67 | +7 495 123-45-67",
            first_ts="20080101000000",
            last_ts="20080101000000",
            sightings=1,
            first_url="https://web.archive.org/web/20080101000000/http://x.ru/c",
            last_url="https://web.archive.org/web/20080101000000/http://x.ru/c",
        ),
    ]
    out = tmp_path / "x.ru_test.csv"
    _write_csv(out, rows)
    assert out.exists()

    # BOM for Excel compat
    raw = out.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")

    with open(out, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        loaded = list(reader)

    assert len(loaded) == 2
    assert loaded[0]["kind"] == "email"
    assert loaded[0]["value"] == "info@x.ru"
    assert loaded[0]["first_seen"] == "2007-08-15"
    assert loaded[0]["last_seen"] == "2019-12-06"
    assert loaded[0]["sightings_count"] == "3"
    assert "web.archive.org" in loaded[0]["first_archive_url"]

    assert loaded[1]["kind"] == "phone"
    assert loaded[1]["value_human"] == "+7 495 123-45-67"
    # Distinct as-seen forms, "|"-joined.
    assert loaded[1]["value_raw"] == "8 (495) 123-45-67 | +7 495 123-45-67"


def test_csv_with_empty_results_writes_header_only(tmp_path: Path):
    out = tmp_path / "empty.csv"
    _write_csv(out, [])
    lines = out.read_text(encoding="utf-8-sig").splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("kind,value,value_human,value_raw,first_seen,last_seen")


def test_aggregate_rows_collects_distinct_raw_forms():
    """A phone may be written ``8-0162-…`` on one page and ``+375 162 …`` on
    another — both literal forms must reach the CSV verbatim."""
    result = ScanResult(
        domain="small-ru.example",
        snapshots_considered=2,
        snapshots_fetched=2,
        sightings=[
            _mk_sighting("+375162511254", "phone", "20180101000000", raw="8-0162-51-12-54"),
            _mk_sighting("+375162511254", "phone", "20210119000000", raw="+375 162 51-12-54"),
            # Dup raw on a third snapshot — must not duplicate inside value_raw.
            _mk_sighting("+375162511254", "phone", "20220101000000", raw="8-0162-51-12-54"),
        ],
    )
    rows = _aggregate_rows(result)
    assert len(rows) == 1
    assert rows[0].value == "+375162511254"
    assert rows[0].value_raw == "8-0162-51-12-54 | +375 162 51-12-54"
    assert rows[0].sightings == 3


# ---------------------------------------------------------------------------
# Mode-hint rendering
# ---------------------------------------------------------------------------


def test_text_report_hint_suggests_timeout_increase_when_exhausted(capsys):
    """When the scan stopped at the timeout, suggest --timeout N×2 or --all."""
    from kronieker.report import _render_text_report
    from kronieker.pipeline import ScanResult

    result = ScanResult(
        domain="example.com",
        snapshots_considered=10,
        snapshots_fetched=10,
        sightings=[],
        errors=[],
        timeout_exhausted=True,
        timeout_seconds=300.0,
        elapsed_seconds=301.0,
        plan_rationale="contact-URL filter on",
        url_filter_active=True,
    )
    _render_text_report(result, rows=[], csv_path=None)
    captured = capsys.readouterr()
    assert "Timeout: 300s" in captured.out
    assert "Timeout exhausted" in captured.err
    assert "--timeout 600" in captured.err or "--all" in captured.err


def test_text_report_hint_suggests_all_when_filter_was_active(capsys):
    """When the scan finished inside the timeout but the filter was on, suggest --all."""
    from kronieker.report import _render_text_report
    from kronieker.pipeline import ScanResult

    result = ScanResult(
        domain="example.com",
        snapshots_considered=10,
        snapshots_fetched=10,
        sightings=[],
        errors=[],
        timeout_exhausted=False,
        timeout_seconds=300.0,
        elapsed_seconds=42.0,
        url_filter_active=True,
    )
    _render_text_report(result, rows=[], csv_path=None)
    err = capsys.readouterr().err
    assert "--all" in err


def test_text_report_no_hint_when_filter_off_and_within_timeout(capsys):
    """Already broadest scan — no hint needed."""
    from kronieker.report import _render_text_report
    from kronieker.pipeline import ScanResult

    result = ScanResult(
        domain="example.com",
        snapshots_considered=10,
        snapshots_fetched=10,
        sightings=[],
        errors=[],
        timeout_exhausted=False,
        timeout_seconds=0.0,
        elapsed_seconds=200.0,
        url_filter_active=False,
    )
    _render_text_report(result, rows=[], csv_path=None)
    captured = capsys.readouterr()
    assert "Timeout: unlimited" in captured.out
    assert "Hint:" not in captured.err


class TestTextReportInSingleUrlMode:
    """Single-URL mode must not bleed host-wide planner cosmetics into the
    report — the header names the URL, the rationale describes single-URL
    mode, and hints about ``--all`` are suppressed (the filter never
    applied in this scan).
    """

    def _result(self, **overrides):
        from kronieker.pipeline import ScanResult
        kw = dict(
            domain="www.example.com",
            snapshots_considered=3, snapshots_fetched=3,
            sightings=[], errors=[],
            timeout_exhausted=False,
            timeout_seconds=90.0, elapsed_seconds=1.5,
            plan_rationale="single-URL mode — 3 archived snapshots of "
                "https://www.example.com/contact",
            url_filter_active=False,
            single_url="https://www.example.com/contact",
        )
        kw.update(overrides)
        return ScanResult(**kw)

    def test_header_names_url_not_domain(self, capsys):
        from kronieker.report import _render_text_report

        _render_text_report(self._result(), rows=[], csv_path=None)
        out = capsys.readouterr().out
        assert "URL: https://www.example.com/contact" in out
        assert "Domain: " not in out

    def test_hint_about_all_is_suppressed(self, capsys):
        """``--all`` makes no sense when the contact-URL filter was never
        active in the first place. The host-wide "try --all" hint must not
        appear under single-URL.
        """
        from kronieker.report import _render_text_report

        _render_text_report(self._result(), rows=[], csv_path=None)
        err = capsys.readouterr().err
        assert "--all" not in err
        assert "Contact-URL filter" not in err

    def test_timeout_exhausted_hint_only_suggests_extending_timeout(self, capsys):
        from kronieker.report import _render_text_report

        _render_text_report(
            self._result(timeout_exhausted=True, elapsed_seconds=90.0),
            rows=[], csv_path=None,
        )
        err = capsys.readouterr().err
        assert "Timeout exhausted" in err
        assert "more snapshots of this URL" in err
        assert "--all" not in err

    def test_json_includes_single_url_field(self, capsys):
        import json as _json
        from kronieker.report import _render_json_report

        _render_json_report(self._result(), rows=[], csv_path=None)
        payload = _json.loads(capsys.readouterr().out)
        assert payload["single_url"] == "https://www.example.com/contact"
        assert payload["url_filter_active"] is False


def test_text_report_prints_no_contacts_message_when_empty(capsys):
    from kronieker.report import _render_text_report
    from kronieker.pipeline import ScanResult

    result = ScanResult(
        domain="empty.example",
        snapshots_considered=5, snapshots_fetched=5,
        sightings=[], errors=[], resolved_mode="default",
    )
    _render_text_report(result, rows=[], csv_path=None)
    out = capsys.readouterr().out
    assert "No contacts found." in out
    # Must NOT print an empty table (header + separator with no data)
    assert "First seen" not in out
    assert "Last seen" not in out


def test_cli_does_not_write_csv_when_no_results(tmp_path, monkeypatch):
    """Empty CSV files are noise — don't write them."""
    from kronieker import cli
    from kronieker.calibration import Calibration, CALIBRATION_VERSION
    from kronieker.pipeline import ScanResult

    empty_result = ScanResult(
        domain="empty.example",
        snapshots_considered=5, snapshots_fetched=5,
        sightings=[], errors=[], resolved_mode="default",
    )
    monkeypatch.setattr(cli, "scan_domain", lambda *a, **kw: empty_result)
    monkeypatch.setattr(
        cli, "ensure_calibration",
        lambda **kw: Calibration(
            version=CALIBRATION_VERSION, avg_latency_s=0.5, sample_count=8,
            last_calibrated_at="2026-01-01T00:00:00+00:00",
            samples_p50=0.5, samples_p95=0.6, user_agent="test",
        ),
    )
    monkeypatch.chdir(tmp_path)

    cli.main(["empty.example", "--no-progress"])

    # No CSV created in the working dir
    csvs = list(tmp_path.glob("*.csv"))
    assert csvs == [], f"unexpected CSV files: {csvs}"


# ---------------------------------------------------------------------------
# Timeout / mode-alias resolution
# ---------------------------------------------------------------------------


def test_named_aliases_map_to_timeout():
    from kronieker.cli import build_parser, resolve_timeout

    p = build_parser()
    assert resolve_timeout(p.parse_args(["example.com"])) == (300.0, False)
    assert resolve_timeout(p.parse_args(["example.com", "--auto"])) == (300.0, False)
    assert resolve_timeout(p.parse_args(["example.com", "--default"])) == (300.0, False)
    assert resolve_timeout(p.parse_args(["example.com", "--deep"])) == (900.0, False)
    assert resolve_timeout(p.parse_args(["example.com", "--exhaustive"])) == (0.0, True)


def test_explicit_timeout_overrides_alias_default():
    from kronieker.cli import build_parser, resolve_timeout

    p = build_parser()
    assert resolve_timeout(p.parse_args(["example.com", "--timeout", "45"])) == (45.0, False)
    assert resolve_timeout(p.parse_args(["example.com", "--timeout", "0"])) == (0.0, False)


def test_all_flag_is_sticky_on_top_of_aliases():
    """--all combined with --deep stays --all."""
    from kronieker.cli import build_parser, resolve_timeout

    p = build_parser()
    assert resolve_timeout(p.parse_args(["example.com", "--deep", "--all"])) == (900.0, True)
    assert resolve_timeout(p.parse_args(["example.com", "--timeout", "60", "--all"])) == (60.0, True)


def test_timeout_and_named_mode_mutex_errors():
    """Passing --timeout and --deep simultaneously is rejected by argparse."""
    from kronieker.cli import build_parser

    p = build_parser()
    with pytest.raises(SystemExit):
        p.parse_args(["example.com", "--timeout", "60", "--deep"])


def test_ctrl_c_returns_130_and_no_traceback(monkeypatch, capsys):
    """KeyboardInterrupt from the scan stage must produce a clean exit.

    Without explicit handling, the long-running fetch loop leaks a stack
    trace through threads' finally-blocks. main() must swallow it and
    return the standard SIGINT exit code.
    """
    from kronieker import cli
    from kronieker.calibration import Calibration, CALIBRATION_VERSION

    def raise_kbd(*a, **kw):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "scan_domain", raise_kbd)
    monkeypatch.setattr(
        cli, "ensure_calibration",
        lambda **kw: Calibration(
            version=CALIBRATION_VERSION, avg_latency_s=0.42, sample_count=8,
            last_calibrated_at="2026-01-01T00:00:00+00:00",
            samples_p50=0.4, samples_p95=0.5, user_agent="test",
        ),
    )

    rc = cli.main(["example.com", "--no-progress"])
    assert rc == 130
    err = capsys.readouterr().err
    assert "Interrupted." in err
    assert "Traceback" not in err


def test_ctrl_c_mid_scan_saves_partial_csv(monkeypatch, tmp_path, capsys):
    """Ctrl+C during a scan must persist whatever contacts were collected.

    The CLI receives a ``ScanResult`` with ``interrupted=True`` from
    ``scan_domain`` (which catches the interrupt inside ``_scan`` so partial
    state survives). Even one phone or email gathered before the keystroke
    should land in CSV and the rendered report.
    """
    from kronieker import cli
    from kronieker.calibration import Calibration, CALIBRATION_VERSION
    from kronieker.extractors import Contact
    from kronieker.pipeline import ContactSighting, ScanResult

    partial = ScanResult(
        domain="example.com",
        snapshots_considered=5,
        snapshots_fetched=2,
        sightings=[
            ContactSighting(
                contact=Contact(kind="email", value="info@example.com", raw="info@example.com"),
                snapshot_url="https://web.archive.org/web/20200101000000/http://example.com/",
                timestamp="20200101000000",
                source_url="http://example.com/",
            ),
        ],
        errors=[],
        timeout_seconds=300.0,
        elapsed_seconds=4.2,
        plan_rationale="contact-URL filter on; ...",
        url_filter_active=True,
        interrupted=True,
    )

    monkeypatch.setattr(cli, "scan_domain", lambda *a, **kw: partial)
    monkeypatch.setattr(
        cli, "ensure_calibration",
        lambda **kw: Calibration(
            version=CALIBRATION_VERSION, avg_latency_s=0.42, sample_count=8,
            last_calibrated_at="2026-01-01T00:00:00+00:00",
            samples_p50=0.4, samples_p95=0.5, user_agent="test",
        ),
    )

    csv_target = tmp_path / "partial.csv"
    rc = cli.main([
        "example.com", "--no-progress", "--csv", str(csv_target),
    ])

    assert rc == 130
    assert csv_target.exists(), "partial CSV must be saved before exit"
    contents = csv_target.read_text()
    assert "info@example.com" in contents
    err = capsys.readouterr().err
    assert "Interrupted" in err
    assert str(csv_target) in err
    assert "Traceback" not in err


def test_ctrl_c_mid_batch_stops_iteration_after_current_target(monkeypatch, tmp_path, capsys):
    """In batch mode, Ctrl+C during target N must save target N's partial
    results and skip target N+1 entirely, not roll into the next scan.
    """
    from kronieker import cli
    from kronieker.calibration import Calibration, CALIBRATION_VERSION

    scans_run: list[str] = []

    def fake_scan(domain, **kw):
        from kronieker.pipeline import ScanResult
        scans_run.append(domain)
        return ScanResult(
            domain=domain,
            snapshots_considered=0,
            snapshots_fetched=0,
            sightings=[],
            errors=[],
            interrupted=True,  # first call: user pressed Ctrl+C
        )

    monkeypatch.setattr(cli, "scan_domain", fake_scan)
    monkeypatch.setattr(
        cli, "ensure_calibration",
        lambda **kw: Calibration(
            version=CALIBRATION_VERSION, avg_latency_s=0.42, sample_count=8,
            last_calibrated_at="2026-01-01T00:00:00+00:00",
            samples_p50=0.4, samples_p95=0.5, user_agent="test",
        ),
    )

    targets_file = tmp_path / "targets.txt"
    targets_file.write_text("first.example\nsecond.example\nthird.example\n")

    rc = cli.main([
        "--targets-file", str(targets_file), "--no-progress", "--no-csv",
    ])

    assert rc == 130
    assert scans_run == ["first.example"], (
        f"batch loop should stop after Ctrl+C, but ran: {scans_run}"
    )


class TestSingleUrlMode:
    """The ``--single-url URL`` flag flips the scan into matchType=exact
    against the given URL, kills the well-known probe, and bypasses both
    the urlkey filter and the URL ranker. Make sure each piece holds.
    """

    def _calibration(self):
        from kronieker.calibration import Calibration, CALIBRATION_VERSION
        return Calibration(
            version=CALIBRATION_VERSION, avg_latency_s=0.42, sample_count=8,
            last_calibrated_at="2026-01-01T00:00:00+00:00",
            samples_p50=0.4, samples_p95=0.5, user_agent="test",
        )

    def _stub_scan(self, captured):
        def fake_scan(domain, **kw):
            captured["domain"] = domain
            captured["single_url"] = kw.get("single_url")
            captured["probe_well_known"] = kw.get("probe_well_known")
            from kronieker.pipeline import ScanResult
            return ScanResult(
                domain=domain, snapshots_considered=0, snapshots_fetched=0,
                sightings=[], errors=[],
            )
        return fake_scan

    def test_passes_url_through_to_scan_domain(self, monkeypatch):
        from kronieker import cli

        captured = {}
        monkeypatch.setattr(cli, "scan_domain", self._stub_scan(captured))
        monkeypatch.setattr(cli, "ensure_calibration", lambda **kw: self._calibration())

        rc = cli.main([
            "--single-url", "https://www.example.com/azovdisk/about",
            "--no-progress", "--no-csv", "--no-cache",
        ])
        assert rc == 0
        # Host derived from the URL becomes the per-target label.
        assert captured["domain"] == "www.example.com"
        assert captured["single_url"] == "https://www.example.com/azovdisk/about"

    def test_mutex_with_positional_domain(self, monkeypatch, capsys):
        from kronieker import cli

        monkeypatch.setattr(cli, "ensure_calibration", lambda **kw: self._calibration())

        rc = cli.main([
            "example.com", "--single-url", "https://other.example/x",
            "--no-progress", "--no-csv", "--no-cache",
        ])
        assert rc == 2
        assert "pick exactly one input" in capsys.readouterr().err

    def test_mutex_with_targets_file(self, monkeypatch, capsys, tmp_path):
        from kronieker import cli

        monkeypatch.setattr(cli, "ensure_calibration", lambda **kw: self._calibration())
        tf = tmp_path / "t.txt"
        tf.write_text("foo.example\n", encoding="utf-8")

        rc = cli.main([
            "--single-url", "https://x.example/p", "--targets-file", str(tf),
            "--no-progress", "--no-csv", "--no-cache",
        ])
        assert rc == 2
        assert "pick exactly one input" in capsys.readouterr().err

    def test_rejects_url_without_scheme(self, monkeypatch, capsys):
        from kronieker import cli

        monkeypatch.setattr(cli, "ensure_calibration", lambda **kw: self._calibration())

        rc = cli.main([
            "--single-url", "example.com/about",  # no scheme
            "--no-progress", "--no-csv", "--no-cache",
        ])
        assert rc == 2
        err = capsys.readouterr().err
        assert "absolute http(s) URL" in err

    def test_disables_probe_implicitly(self, monkeypatch):
        """Single-URL mode must override probe_well_known regardless of CLI."""
        from kronieker import cli

        captured = {}
        monkeypatch.setattr(cli, "scan_domain", self._stub_scan(captured))
        monkeypatch.setattr(cli, "ensure_calibration", lambda **kw: self._calibration())

        # Even without --no-probe, single-URL must not run probe.
        cli.main([
            "--single-url", "https://www.example.com/contact",
            "--no-progress", "--no-csv", "--no-cache",
        ])
        # The CLI plumbs probe_well_known=not args.no_probe (= True by default);
        # scan_domain then forces it off based on single_url. Verify by piggy-
        # backing on a deeper unit test: the CLI value is what gets propagated.
        # (The pipeline override is covered by TestSingleUrlScan in test_pipeline.)
        assert captured["probe_well_known"] is True  # CLI propagates default
        assert captured["single_url"] == "https://www.example.com/contact"


class TestVerboseDebugSplit:
    """Project rule: ``-v`` controls only the contact-feed verbosity (date +
    snapshot URL beneath each contact). ``-d`` controls only DEBUG logging.
    They must NOT cross-trigger.
    """

    def _calibration(self):
        from kronieker.calibration import Calibration, CALIBRATION_VERSION
        return Calibration(
            version=CALIBRATION_VERSION, avg_latency_s=0.42, sample_count=8,
            last_calibrated_at="2026-01-01T00:00:00+00:00",
            samples_p50=0.4, samples_p95=0.5, user_agent="test",
        )

    def _stub_scan(self, captured):
        def fake_scan(domain, **kw):
            captured["verbose"] = kw.get("verbose")
            from kronieker.pipeline import ScanResult
            return ScanResult(
                domain=domain, snapshots_considered=0, snapshots_fetched=0,
                sightings=[], errors=[],
            )
        return fake_scan

    def test_v_does_not_enable_debug_logging(self, monkeypatch):
        """`-v` must NOT turn root logger to DEBUG. Only `-d` does."""
        from kronieker import cli
        import logging

        captured = {}
        monkeypatch.setattr(cli, "scan_domain", self._stub_scan(captured))
        monkeypatch.setattr(cli, "ensure_calibration", lambda **kw: self._calibration())
        # Reset basicConfig state so logging.basicConfig actually takes effect.
        logging.getLogger().handlers.clear()

        cli.main(["example.com", "-v", "--no-progress", "--no-csv", "--no-cache"])

        # -v should leave logging at INFO (default).
        assert logging.getLogger().level == logging.INFO
        # And -v must propagate verbose=True into scan_domain (so the live
        # feed shows date + snapshot URL).
        assert captured["verbose"] is True

    def test_d_enables_debug_without_verbose_feed(self, monkeypatch):
        """`-d` must turn on DEBUG logs, and must NOT turn on the verbose feed."""
        from kronieker import cli
        import logging

        captured = {}
        monkeypatch.setattr(cli, "scan_domain", self._stub_scan(captured))
        monkeypatch.setattr(cli, "ensure_calibration", lambda **kw: self._calibration())
        logging.getLogger().handlers.clear()

        cli.main(["example.com", "-d", "--no-progress", "--no-csv", "--no-cache"])

        assert logging.getLogger().level == logging.DEBUG
        assert captured["verbose"] is False  # -d alone does NOT imply verbose feed

    def test_both_v_and_d_combine(self, monkeypatch):
        """Passing both flags together is allowed and applies both effects."""
        from kronieker import cli
        import logging

        captured = {}
        monkeypatch.setattr(cli, "scan_domain", self._stub_scan(captured))
        monkeypatch.setattr(cli, "ensure_calibration", lambda **kw: self._calibration())
        logging.getLogger().handlers.clear()

        cli.main(["example.com", "-v", "-d", "--no-progress", "--no-csv", "--no-cache"])

        assert logging.getLogger().level == logging.DEBUG
        assert captured["verbose"] is True


def test_calibrate_flag_does_not_scan(monkeypatch, tmp_path, capsys):
    """`kronieker --calibrate` refreshes the cache and exits without scanning."""
    from kronieker import cli
    from kronieker.calibration import Calibration, CALIBRATION_VERSION

    scan_called = {"n": 0}

    def fake_scan(*a, **kw):
        scan_called["n"] += 1

    monkeypatch.setattr(cli, "scan_domain", fake_scan)
    monkeypatch.setattr(
        cli, "ensure_calibration",
        lambda **kw: Calibration(
            version=CALIBRATION_VERSION, avg_latency_s=0.42, sample_count=8,
            last_calibrated_at="2026-01-01T00:00:00+00:00",
            samples_p50=0.4, samples_p95=0.5, user_agent="test",
        ),
    )

    rc = cli.main(["--calibrate"])
    assert rc == 0
    assert scan_called["n"] == 0
    out = capsys.readouterr().out
    assert "avg_latency" in out


# ---------------------------------------------------------------------------
# Targets-file parsing
# ---------------------------------------------------------------------------


class TestParseTargetLine:
    def test_bare_domain(self):
        from kronieker.cli import _parse_target_line
        assert _parse_target_line("example.com") == ("example.com", None)

    def test_bare_domain_with_trailing_slash(self):
        from kronieker.cli import _parse_target_line
        assert _parse_target_line("example.com/") == ("example.com", None)

    def test_domain_with_path_no_scheme(self):
        from kronieker.cli import _parse_target_line
        assert _parse_target_line("example.com/contact") == ("example.com", "/contact")

    def test_full_url_https(self):
        from kronieker.cli import _parse_target_line
        assert _parse_target_line("https://example.com/o-nas/team") == (
            "example.com", "/o-nas/team",
        )

    def test_full_url_http(self):
        from kronieker.cli import _parse_target_line
        assert _parse_target_line("http://example.com/contact") == (
            "example.com", "/contact",
        )

    def test_subdomain_preserved(self):
        from kronieker.cli import _parse_target_line
        assert _parse_target_line("https://news.corp.example/2018/x") == (
            "news.corp.example", "/2018/x",
        )

    def test_port_stripped(self):
        from kronieker.cli import _parse_target_line
        assert _parse_target_line("example.com:8080/page") == (
            "example.com", "/page",
        )

    def test_trailing_slash_in_path_dropped(self):
        from kronieker.cli import _parse_target_line
        assert _parse_target_line("https://x.ru/contacts/") == ("x.ru", "/contacts")

    def test_uppercase_host_lowered(self):
        from kronieker.cli import _parse_target_line
        assert _parse_target_line("HTTPS://Example.COM/Path") == (
            "example.com", "/Path",
        )

    def test_empty_raises(self):
        from kronieker.cli import _parse_target_line
        with pytest.raises(ValueError):
            _parse_target_line("https:///path")


def test_parse_targets_file_merges_same_domain(tmp_path):
    """Multiple URLs for one host collapse into one Target with extras
    in first-seen order; bare-domain entries don't lose their slot."""
    from kronieker.cli import parse_targets_file

    f = tmp_path / "targets.txt"
    f.write_text(
        "# header\n"
        "\n"
        "small-ru.example\n"
        "https://small-ru.example/uslugi\n"
        "  https://small-ru.example/o-nas  \n"
        "https://small-ru.example/uslugi\n"  # dup
        "corp.example\n"
        "https://www.corp.example/leadership\n"
        "# comment line\n",
        encoding="utf-8",
    )

    targets = parse_targets_file(f)
    by_domain = {t.domain: t for t in targets}

    assert [t.domain for t in targets] == [
        "small-ru.example", "corp.example", "www.corp.example",
    ]
    assert by_domain["small-ru.example"].extra_paths == (
        "/uslugi", "/o-nas",
    )
    assert by_domain["corp.example"].extra_paths == ()
    assert by_domain["www.corp.example"].extra_paths == ("/leadership",)


def test_parse_targets_file_reports_lineno_on_bad_entry(tmp_path):
    from kronieker.cli import parse_targets_file

    f = tmp_path / "bad.txt"
    f.write_text("example.com\n\nhttps:///nohost\n", encoding="utf-8")

    with pytest.raises(ValueError, match=r":3:.*empty domain"):
        parse_targets_file(f)


def test_targets_file_dispatches_to_scan_domain(monkeypatch, tmp_path, capsys):
    """`--targets-file` runs scan_domain once per merged target and forwards
    extra_paths through to scan_domain's kwargs."""
    from kronieker import cli
    from kronieker.calibration import Calibration, CALIBRATION_VERSION
    from kronieker.pipeline import ScanResult

    targets_file = tmp_path / "targets.txt"
    targets_file.write_text(
        "example.com\n"
        "https://other.com/o-nas\n"
        "https://other.com/team\n",
        encoding="utf-8",
    )

    invocations: list[tuple[str, tuple[str, ...]]] = []

    def fake_scan(domain, **kwargs):
        invocations.append((domain, tuple(kwargs.get("extra_well_known_paths", ()))))
        return ScanResult(
            domain=domain, snapshots_considered=0, snapshots_fetched=0,
            sightings=[], errors=[],
        )

    monkeypatch.setattr(cli, "scan_domain", fake_scan)
    monkeypatch.setattr(
        cli, "ensure_calibration",
        lambda **kw: Calibration(
            version=CALIBRATION_VERSION, avg_latency_s=0.5, sample_count=8,
            last_calibrated_at="2026-01-01T00:00:00+00:00",
            samples_p50=0.5, samples_p95=0.6, user_agent="test",
        ),
    )
    monkeypatch.chdir(tmp_path)

    rc = cli.main(["--targets-file", str(targets_file), "--no-progress"])

    assert rc == 0
    assert invocations == [
        ("example.com", ()),
        ("other.com", ("/o-nas", "/team")),
    ]


def test_positional_and_targets_file_mutex(tmp_path, capsys):
    from kronieker import cli

    f = tmp_path / "t.txt"
    f.write_text("example.com\n", encoding="utf-8")

    rc = cli.main(["other.com", "--targets-file", str(f)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "pick exactly one input" in err
    assert "--targets-file" in err


def test_targets_file_empty_errors(tmp_path, capsys):
    from kronieker import cli

    f = tmp_path / "empty.txt"
    f.write_text("# only comments\n\n", encoding="utf-8")

    rc = cli.main(["--targets-file", str(f)])
    assert rc == 2
    assert "no targets found" in capsys.readouterr().err
