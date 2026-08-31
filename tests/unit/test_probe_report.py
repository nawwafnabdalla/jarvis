from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from jarvis.core.errors import UserError
from jarvis.core.types import Nanos
from jarvis.probe.contexts import ContextEvent, ProbeParams
from jarvis.probe.gate import evaluate_gate
from jarvis.probe.report import (
    CC_FORWARD_WINDOW_CAVEAT,
    D024A_QUALIFICATION,
    VAULT_BOUNDARY_NS,
    WATERMARK,
    _per_context_counts,
    has_prior_widening,
    lineage_path,
    read_lineage,
    record_lineage_run,
    stage0_report_path,
    widened_params,
    write_report,
)
from jarvis.sessions import load_session_set

SESSION_SET = load_session_set("fx_core", 1)


def _ns(y: int, mo: int, d: int, h: int = 0) -> Nanos:
    return Nanos(int(datetime(y, mo, d, h, tzinfo=timezone.utc).timestamp()) * 1_000_000_000)


def _year_counts_for_report():
    from jarvis.probe.gate import YearCounts

    years = []
    for i in range(16):
        per_context = {
            "C-A": 500,
            "C-B": 500,
            "C-C": 500,
            "C-D": 500,
            "C-A∩C-B": 150,
            "C-A∩C-C": 150,
            "C-D∩C-B": 150,
            "C-D∩C-C": 150,
        }
        years.append(
            YearCounts(
                year=2007 + i,
                admissible=True,
                admissible_days=260,
                context_eligible_days=250,
                per_context=per_context,
            )
        )
    return years


# D-039: direction broadcasting -------------------------------------------


def test_direction_broadcast_yields_two_intersection_events():
    """Acceptance criterion 3: a day with C-A plus both a long and a
    short C-B yields 2 intersection events for C-A∩C-B, not 1."""
    day = date(2024, 1, 16)
    events = [
        ContextEvent(context="C-A", trading_day=day, direction="none", ts_utc_ns=Nanos(1), detail={}),
        ContextEvent(context="C-B", trading_day=day, direction="long", ts_utc_ns=Nanos(2), detail={}),
        ContextEvent(context="C-B", trading_day=day, direction="short", ts_utc_ns=Nanos(3), detail={}),
    ]
    counts = _per_context_counts(events)
    assert counts["C-A∩C-B"] == 2
    assert counts["C-A"] == 1
    assert counts["C-B"] == 2


def test_intersection_only_counted_when_both_sides_present():
    day = date(2024, 1, 16)
    events = [
        ContextEvent(context="C-B", trading_day=day, direction="long", ts_utc_ns=Nanos(2), detail={}),
    ]
    counts = _per_context_counts(events)
    assert counts["C-A∩C-B"] == 0
    assert counts["C-B"] == 1


def test_intersection_across_different_days_not_counted():
    events = [
        ContextEvent(
            context="C-A", trading_day=date(2024, 1, 16), direction="none", ts_utc_ns=Nanos(1), detail={}
        ),
        ContextEvent(
            context="C-B", trading_day=date(2024, 1, 17), direction="long", ts_utc_ns=Nanos(2), detail={}
        ),
    ]
    counts = _per_context_counts(events)
    assert counts["C-A∩C-B"] == 0


# Report content (acceptance criterion 8) ------------------------------------


def test_report_contains_watermark_qualification_and_cc_caveat(tmp_path: Path):
    year_counts = _year_counts_for_report()
    gate_result = evaluate_gate(year_counts, ProbeParams(), None)
    events = ()

    md_path, parquet_path = write_report(
        tmp_path,
        "GBPUSD",
        _ns(2007, 1, 1),
        _ns(2023, 1, 1),
        SESSION_SET,
        gate_result,
        events,
        prior_run_count=0,
    )

    assert md_path.is_file()
    assert parquet_path.is_file()
    text = md_path.read_text(encoding="utf-8")

    assert WATERMARK in text
    assert D024A_QUALIFICATION in text
    assert CC_FORWARD_WINDOW_CAVEAT in text
    assert gate_result.decision in text
    assert "linear" in text


def test_report_sections_appear_in_the_required_order(tmp_path: Path):
    """The WP specifies an exact section order: watermark, decision+
    arithmetic, D-024a qualification, per-year table, median/P10, C-C
    caveat, parameters+provenance, prior run count. Metadata (dataset
    range, session set, code SHA) belongs in section 7, not bundled in
    right after the watermark."""
    year_counts = _year_counts_for_report()
    gate_result = evaluate_gate(year_counts, ProbeParams(), None)

    md_path, _parquet_path = write_report(
        tmp_path, "GBPUSD", _ns(2007, 1, 1), _ns(2023, 1, 1), SESSION_SET, gate_result, (), prior_run_count=2
    )
    text = md_path.read_text(encoding="utf-8")

    positions = {
        "watermark": text.index(WATERMARK),
        "decision": text.index("## Decision"),
        "qualification": text.index(D024A_QUALIFICATION),
        "per_year": text.index("## Per-year counts"),
        "median_p10": text.index("## Median and P10"),
        "cc_caveat": text.index(CC_FORWARD_WINDOW_CAVEAT),
        "parameters": text.index("## Parameters"),
        "lineage": text.index("Prior probe runs for this lineage"),
    }
    ordered_keys = sorted(positions, key=positions.get)
    assert ordered_keys == [
        "watermark",
        "decision",
        "qualification",
        "per_year",
        "median_p10",
        "cc_caveat",
        "parameters",
        "lineage",
    ]


def test_report_path_naming_convention(tmp_path: Path):
    generated = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    path = stage0_report_path(tmp_path, generated)
    assert path.name == "STAGE0__20240601T120000Z.md"
    assert path.parent == tmp_path / "reports" / "stage0"


def test_report_parquet_has_one_row_per_event(tmp_path: Path):
    year_counts = _year_counts_for_report()
    gate_result = evaluate_gate(year_counts, ProbeParams(), None)
    events = (
        ContextEvent(
            context="C-A", trading_day=date(2024, 1, 16), direction="none", ts_utc_ns=Nanos(1), detail={"x": 1.0}
        ),
        ContextEvent(
            context="C-B", trading_day=date(2024, 1, 16), direction="long", ts_utc_ns=Nanos(2), detail={"y": 2.0}
        ),
    )
    import polars as pl

    _md_path, parquet_path = write_report(
        tmp_path, "GBPUSD", _ns(2007, 1, 1), _ns(2023, 1, 1), SESSION_SET, gate_result, events
    )
    df = pl.read_parquet(parquet_path)
    assert df.height == 2


# widened_params ---------------------------------------------------------


def test_widened_params_none_returns_defaults():
    params = widened_params(None)
    assert params == ProbeParams()


def test_widened_params_range_pct_max():
    params = widened_params("range_pct_max")
    assert params.range_pct_max == pytest.approx(0.40)
    assert params.break_buffer_atr == pytest.approx(0.10)  # unchanged


def test_widened_params_break_buffer_atr():
    params = widened_params("break_buffer_atr")
    assert params.break_buffer_atr == pytest.approx(0.05)
    assert params.range_pct_max == pytest.approx(0.33)  # unchanged


def test_widened_params_unknown_raises():
    with pytest.raises(UserError):
        widened_params("not_a_real_param")


# Lineage tracking ---------------------------------------------------------


def test_lineage_starts_empty(tmp_path: Path):
    runs = read_lineage(tmp_path, "GBPUSD", _ns(2007, 1, 1), _ns(2023, 1, 1))
    assert runs == []
    assert has_prior_widening(runs) is False


def test_record_lineage_run_appends_and_persists(tmp_path: Path):
    start, end = _ns(2007, 1, 1), _ns(2023, 1, 1)
    n1 = record_lineage_run(tmp_path, "GBPUSD", start, end, widened_from=None, decision="WIDEN_CONTEXT")
    assert n1 == 1
    n2 = record_lineage_run(
        tmp_path, "GBPUSD", start, end, widened_from="range_pct_max", decision="PROCEED_GBPUSD"
    )
    assert n2 == 2

    runs = read_lineage(tmp_path, "GBPUSD", start, end)
    assert len(runs) == 2
    assert runs[0]["widened_from"] is None
    assert runs[1]["widened_from"] == "range_pct_max"
    assert has_prior_widening(runs) is True


def test_lineage_is_scoped_to_exact_range(tmp_path: Path):
    record_lineage_run(
        tmp_path, "GBPUSD", _ns(2007, 1, 1), _ns(2023, 1, 1), widened_from=None, decision="WIDEN_CONTEXT"
    )
    other_runs = read_lineage(tmp_path, "GBPUSD", _ns(2008, 1, 1), _ns(2023, 1, 1))
    assert other_runs == []


def test_lineage_path_distinguishes_instrument_and_range(tmp_path: Path):
    p1 = lineage_path(tmp_path, "GBPUSD", _ns(2007, 1, 1), _ns(2023, 1, 1))
    p2 = lineage_path(tmp_path, "EURUSD", _ns(2007, 1, 1), _ns(2023, 1, 1))
    assert p1 != p2


# Vault boundary constant --------------------------------------------------


def test_vault_boundary_is_2023_01_01():
    assert VAULT_BOUNDARY_NS == _ns(2023, 1, 1)


# CLI vault-boundary refusal (acceptance criterion 7, corrected) ------------
#
# WP-008-CORRECTION: end_ns is an EXCLUSIVE upper bound (this project's
# [start, end) convention since WP-001). --to 2023-01-01T00:00:00Z is
# therefore the boundary ITSELF and must be ACCEPTED -- it reads through
# 2022-12-31T23:59:59.999999999Z and touches no vault data, exactly
# PDLA-03/D-021's intent. An earlier `>=` check refused this value,
# silently making 2022-12-31 permanently unreachable.


def test_vault_boundary_is_exclusive_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from typer.testing import CliRunner

    from jarvis.cli.main import app

    monkeypatch.setattr("jarvis.cli.main.repo_root", lambda: tmp_path)
    runner = CliRunner()

    def run(to: str):
        return runner.invoke(
            app, ["stage0", "probe", "--from", "2007-01-01T00:00:00+00:00", "--to", to]
        )

    # The boundary itself: ACCEPTED by the vault-boundary check. tmp_path
    # has no data, so the run still fails overall -- but for "no bars",
    # never for "vault boundary".
    at_boundary = run("2023-01-01T00:00:00+00:00")
    assert "vault boundary" not in at_boundary.output
    assert at_boundary.exit_code != 0
    assert "no bars" in at_boundary.output

    # One hour past the boundary: REFUSED.
    past_boundary = run("2023-01-01T01:00:00+00:00")
    assert past_boundary.exit_code != 0
    assert "vault boundary" in past_boundary.output

    # Well past the boundary: REFUSED.
    far_past_boundary = run("2023-06-01T00:00:00+00:00")
    assert far_past_boundary.exit_code != 0
    assert "vault boundary" in far_past_boundary.output


def test_last_readable_instant_is_2022_12_31():
    """With --to 2023-01-01T00:00:00Z, the last representable instant in
    the half-open range (end_ns - 1) must fall on 2022-12-31 -- so the
    2022-12-31 exclusion bug (WP-008-CORRECTION) can never silently
    reappear."""
    last_instant_ns = VAULT_BOUNDARY_NS - 1
    last_instant = datetime.fromtimestamp(last_instant_ns // 1_000_000_000, tz=timezone.utc)
    assert last_instant.date() == date(2022, 12, 31)
