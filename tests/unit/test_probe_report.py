from datetime import date, datetime, timezone
from pathlib import Path

import polars as pl
import pytest
import yaml

from jarvis.core.errors import UserError
from jarvis.core.types import Nanos
from jarvis.ingest.histdata_import import TICK_SCHEMA, write_ticks
from jarvis.probe.contexts import ContextEvent, ProbeParams
from jarvis.probe.gate import evaluate_gate
from jarvis.probe.gate import _MIN_ADMISSIBLE_YEARS as _MIN_ADMISSIBLE_GATE_YEARS
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
    reject_vault_range,
    stage0_report_path,
    widened_params,
    write_report,
    year_admissibility,
)
from jarvis.sessions import load_session_set

SESSION_SET = load_session_set("fx_core", 1)


def _ns(y: int, mo: int, d: int, h: int = 0) -> Nanos:
    return Nanos(int(datetime(y, mo, d, h, tzinfo=timezone.utc).timestamp()) * 1_000_000_000)


# year_admissibility fixtures -----------------------------------------------

_DEFAULT_SESSIONS = {
    "tokyo": {"tz": "Asia/Tokyo", "start": "09:00", "end": "15:00"},
}


def _write_session_set(repo_root: Path, *, thin_day_threshold: float = 0.60) -> None:
    config_dir = repo_root / "config" / "sessions"
    config_dir.mkdir(parents=True, exist_ok=True)
    content = {
        "session_set_id": "fx_core",
        "version": 1,
        "tzdata_version_at_authoring": "2026.3",
        "fold_policy": {"ambiguous": "later", "nonexistent": "later"},
        "exclude_partial": True,
        "thin_day_threshold": thin_day_threshold,
        "sessions": _DEFAULT_SESSIONS,
    }
    (config_dir / "fx_core.v1.yaml").write_text(yaml.dump(content), encoding="utf-8")


@pytest.fixture
def probe_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway repo root for year_admissibility/run_checks, which needs
    both jarvis.core.config.repo_root and jarvis.sessions.definitions.
    repo_root patched (two separate bindings -- see test_qa_report.py's
    identical fixture for why one patch alone is not enough)."""
    monkeypatch.setattr("jarvis.core.config.repo_root", lambda: tmp_path)
    monkeypatch.setattr("jarvis.sessions.definitions.repo_root", lambda: tmp_path)
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    (tmp_path / "config").mkdir()
    _write_session_set(tmp_path)
    return tmp_path


def _write_clean_month(repo_root: Path, instrument: str, year: int, month: int) -> None:
    """One clean, QA-inoffensive tick, sufficient to make tick_path(...)
    exist and to survive run_checks with zero ERROR findings (positive
    spread, single row so no reversal/duplicate possible)."""
    ts = int(datetime(year, month, 1, 12, tzinfo=timezone.utc).timestamp()) * 1_000_000_000
    frame = pl.DataFrame(
        {
            "ts_utc_ns": [ts],
            "bid": [1.30000],
            "ask": [1.30010],
            "bid_volume": [1.0],
            "ask_volume": [1.0],
            "row_sequence": [0],
        },
        schema=TICK_SCHEMA,
    )
    write_ticks(repo_root, instrument, year, month, frame)


def _write_dirty_month(repo_root: Path, instrument: str, year: int, month: int) -> None:
    """A tick that trips E-01 (non-positive spread) -- ask < bid."""
    ts = int(datetime(year, month, 1, 12, tzinfo=timezone.utc).timestamp()) * 1_000_000_000
    frame = pl.DataFrame(
        {
            "ts_utc_ns": [ts],
            "bid": [1.30010],
            "ask": [1.30000],
            "bid_volume": [1.0],
            "ask_volume": [1.0],
            "row_sequence": [0],
        },
        schema=TICK_SCHEMA,
    )
    write_ticks(repo_root, instrument, year, month, frame)


# year_admissibility (WP-012) -------------------------------------------


def test_year_admissibility_reads_data_tick_not_dukascopy_blobs(probe_repo: Path):
    """The retargeted signal must be driven by data/tick/ presence alone --
    confirmed here by never creating a data/raw/ directory at all (the old
    Dukascopy-blob check would have measured 0% and failed every year)."""
    assert not (probe_repo / "data" / "raw").exists()
    for month in range(1, 13):
        _write_clean_month(probe_repo, "GBPUSD", 2015, month)
    assert year_admissibility(probe_repo, "GBPUSD", 2015) is True
    assert not (probe_repo / "data" / "raw").exists()  # still never created


def test_year_admissibility_false_for_partial_year(probe_repo: Path):
    """Mirrors 2006's real on-disk shape (data/tick/ only holds 2006-09
    through 2006-12) -- must fail on months-present alone, the mechanism
    D-036a relies on to exclude warmup-only years without a separate
    truncated read range."""
    for month in (9, 10, 11, 12):
        _write_clean_month(probe_repo, "GBPUSD", 2006, month)
    assert year_admissibility(probe_repo, "GBPUSD", 2006) is False


def test_year_admissibility_false_when_qa_errors_present(probe_repo: Path):
    """All 12 months present (passes the months check) but one month has
    a genuine QA ERROR -- the "zero QA ERRORs" half must still be
    enforced, not bypassed by the retarget."""
    for month in range(1, 13):
        if month == 6:
            _write_dirty_month(probe_repo, "GBPUSD", 2015, month)
        else:
            _write_clean_month(probe_repo, "GBPUSD", 2015, month)
    assert year_admissibility(probe_repo, "GBPUSD", 2015) is False


def test_year_admissibility_no_tick_data_at_all_is_inadmissible(probe_repo: Path):
    assert year_admissibility(probe_repo, "GBPUSD", 2015) is False


# Regression: the gate must not return INSUFFICIENT_DATA unconditionally ----


def test_gate_not_unconditionally_insufficient_data_against_corrected_store(probe_repo: Path):
    """Direct regression test for the bug WP-011's runbook process found:
    with realistic data/tick/ coverage (every month of every year present,
    zero QA errors -- the real repo's actual shape for 2007-2022) every
    year must be admissible, giving evaluate_gate the
    _MIN_ADMISSIBLE_YEARS it needs. Before this fix, year_admissibility
    checked Dukascopy raw-blob presence and returned False unconditionally
    regardless of how complete data/tick/ actually was, forcing
    INSUFFICIENT_DATA no matter what. This test would have failed against
    the pre-fix code even with a perfect synthetic dataset."""
    from jarvis.probe.gate import YearCounts

    years = list(range(2007, 2007 + _MIN_ADMISSIBLE_GATE_YEARS + 4))  # comfortably over the floor
    year_counts = []
    for year in years:
        for month in range(1, 13):
            _write_clean_month(probe_repo, "GBPUSD", year, month)
        admissible = year_admissibility(probe_repo, "GBPUSD", year)
        year_counts.append(
            YearCounts(
                year=year,
                admissible=admissible,
                admissible_days=260,
                context_eligible_days=250,
                per_context={
                    "C-A": 150, "C-B": 150, "C-C": 150, "C-D": 150,
                    "C-A∩C-B": 80, "C-A∩C-C": 80, "C-D∩C-B": 80, "C-D∩C-C": 80,
                },
            )
        )

    assert all(yc.admissible for yc in year_counts), "every synthetic year must be admissible"

    result = evaluate_gate(year_counts, ProbeParams(), None)
    assert result.decision != "INSUFFICIENT_DATA"
    assert len(result.years_used) == len(years)


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


# reject_vault_range direct unit tests (WP-012) ------------------------------


def test_reject_vault_range_accepts_the_boundary_itself():
    reject_vault_range(VAULT_BOUNDARY_NS, caller="test")  # must not raise


def test_reject_vault_range_refuses_past_boundary():
    with pytest.raises(UserError, match="vault boundary"):
        reject_vault_range(Nanos(VAULT_BOUNDARY_NS + 1), caller="test")


# WP-012: vault-boundary enforcement extended to the other three Stage 1A
# commands, which previously had no code-level check at all -- each gets
# the same accept-the-boundary / refuse-past-it pair stage0 probe already
# had verified above.


def test_data_resample_refuses_past_vault_boundary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from typer.testing import CliRunner

    from jarvis.cli.main import app

    monkeypatch.setattr("jarvis.cli.main.repo_root", lambda: tmp_path)
    runner = CliRunner()

    at_boundary = runner.invoke(
        app,
        ["data", "resample", "--from", "2022-01-01T00:00:00+00:00", "--to", "2023-01-01T00:00:00+00:00"],
    )
    assert "vault boundary" not in at_boundary.output

    past_boundary = runner.invoke(
        app,
        ["data", "resample", "--from", "2022-01-01T00:00:00+00:00", "--to", "2023-01-01T01:00:00+00:00"],
    )
    assert past_boundary.exit_code != 0
    assert "vault boundary" in past_boundary.output


def test_data_validate_refuses_past_vault_boundary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from typer.testing import CliRunner

    from jarvis.cli.main import app

    monkeypatch.setattr("jarvis.cli.main.repo_root", lambda: tmp_path)
    runner = CliRunner()

    at_boundary = runner.invoke(
        app,
        ["data", "validate", "--from", "2022-01-01T00:00:00+00:00", "--to", "2023-01-01T00:00:00+00:00"],
    )
    assert "vault boundary" not in at_boundary.output

    past_boundary = runner.invoke(
        app,
        ["data", "validate", "--from", "2022-01-01T00:00:00+00:00", "--to", "2023-01-01T01:00:00+00:00"],
    )
    assert past_boundary.exit_code != 0
    assert "vault boundary" in past_boundary.output


def test_features_build_refuses_past_vault_boundary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from typer.testing import CliRunner

    from jarvis.cli.main import app

    monkeypatch.setattr("jarvis.cli.main.repo_root", lambda: tmp_path)
    runner = CliRunner()

    at_boundary = runner.invoke(
        app,
        ["features", "build", "--from", "2022-01-01T00:00:00+00:00", "--to", "2023-01-01T00:00:00+00:00"],
    )
    assert "vault boundary" not in at_boundary.output

    past_boundary = runner.invoke(
        app,
        ["features", "build", "--from", "2022-01-01T00:00:00+00:00", "--to", "2023-01-01T01:00:00+00:00"],
    )
    assert past_boundary.exit_code != 0
    assert "vault boundary" in past_boundary.output
