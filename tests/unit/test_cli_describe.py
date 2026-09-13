from datetime import datetime, timezone

import numpy as np
import polars as pl
import pytest
from typer.testing import CliRunner

from jarvis.bars import write_bars
from jarvis.cli.main import app

runner = CliRunner()


def _synthetic_bars(n: int, *, start_utc: datetime, seed: int = 0) -> pl.DataFrame:
    rng = np.random.default_rng(seed)
    start_ns = int(start_utc.timestamp()) * 1_000_000_000
    ts = start_ns + np.arange(n) * 60_000_000_000
    bid_c = 1.3 + np.cumsum(rng.normal(0, 0.0001, n))
    ask_c = bid_c + rng.uniform(0.00005, 0.0003, n)
    spread_twa = ask_c - bid_c
    return pl.DataFrame(
        {
            "ts_utc_ns": ts,
            "bid_o": bid_c,
            "bid_h": bid_c,
            "bid_l": bid_c,
            "bid_c": bid_c,
            "ask_o": ask_c,
            "ask_h": ask_c,
            "ask_l": ask_c,
            "ask_c": ask_c,
            "tick_count": np.ones(n, dtype=np.int32),
            "first_tick_ns": ts,
            "last_tick_ns": ts,
            "spread_open": spread_twa,
            "spread_max": spread_twa,
            "spread_twa": spread_twa,
            "prev_gap_ns": np.zeros(n, dtype=np.int64),
        }
    )


def test_report_r1_refuses_not_yet_implemented():
    result = runner.invoke(app, ["describe", "run", "--report", "R1"])
    assert result.exit_code == 1
    assert "not yet implemented" in result.output


def test_report_r2_refuses_not_yet_implemented():
    result = runner.invoke(app, ["describe", "run", "--report", "R2"])
    assert result.exit_code == 1
    assert "not yet implemented" in result.output


def test_year_flag_is_refused_not_silently_accepted():
    result = runner.invoke(app, ["describe", "run", "--report", "R5", "--year", "2010"])
    assert result.exit_code == 1
    assert "--year" in result.output
    assert "not yet supported" in result.output


def test_describe_run_r5_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setattr("jarvis.cli.main.repo_root", lambda: tmp_path)

    bars = _synthetic_bars(60 * 24 * 10, start_utc=datetime(2010, 1, 4, tzinfo=timezone.utc))
    write_bars(tmp_path, "GBPUSD", 2010, 1, bars)

    result = runner.invoke(app, ["describe", "run", "--report", "R5"])

    assert result.exit_code == 0, result.output
    assert "Bars examined" in result.output
    assert "Report" in result.output
    assert "Sidecar" in result.output

    describe_dir = tmp_path / "reports" / "describe"
    md_files = list(describe_dir.glob("R5__*.md"))
    parquet_files = list(describe_dir.glob("R5__*.parquet"))
    assert len(md_files) == 1
    assert len(parquet_files) == 1

    md_text = md_files[0].read_text(encoding="utf-8")
    assert md_text.startswith("# DESCRIPTIVE -- EXPLORATORY -- NOT EVIDENCE")
    assert "R5 -- Spread and cost climate" in md_text

    sidecar = pl.read_parquet(parquet_files[0])
    assert sidecar.height > 0
    assert "weekday" in sidecar.columns


def test_describe_run_r5_with_no_bars_still_writes_a_report(tmp_path, monkeypatch):
    monkeypatch.setattr("jarvis.cli.main.repo_root", lambda: tmp_path)

    result = runner.invoke(app, ["describe", "run", "--report", "R5"])

    assert result.exit_code == 0, result.output
    describe_dir = tmp_path / "reports" / "describe"
    md_files = list(describe_dir.glob("R5__*.md"))
    assert len(md_files) == 1
    md_text = md_files[0].read_text(encoding="utf-8")
    assert "Bars examined: 0" in md_text
