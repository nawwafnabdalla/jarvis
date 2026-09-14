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


def _weekday_bars(n_weekdays: int, *, start: datetime, seed: int = 0) -> pl.DataFrame:
    """Real weekday-shaped bars (Sat/Sun skipped entirely) -- unlike
    _synthetic_bars above, R1 genuinely needs this shape, since it
    computes real session windows and trading-day day-of-week groupings
    that a naive continuous run (R5 never cared about either) would
    populate wrongly."""
    rng = np.random.default_rng(seed)
    rows: list[dict] = []
    day = start
    price = 1.3000
    added = 0
    while added < n_weekdays:
        if day.weekday() < 5:
            for m in range(24 * 60):
                ts = int(day.timestamp()) * 1_000_000_000 + m * 60_000_000_000
                price += float(rng.normal(0, 0.00003))
                rows.append(
                    {
                        "ts_utc_ns": ts, "bid_o": price, "bid_h": price + 0.0002, "bid_l": price - 0.0002,
                        "bid_c": price, "ask_o": price + 0.0002, "ask_h": price + 0.0004, "ask_l": price,
                        "ask_c": price + 0.0002, "tick_count": 1, "first_tick_ns": ts, "last_tick_ns": ts,
                        "spread_open": 0.0002, "spread_max": 0.0002, "spread_twa": 0.0002, "prev_gap_ns": None,
                    }
                )
            added += 1
        day = datetime.fromtimestamp(day.timestamp() + 86400, tz=timezone.utc)
    return pl.DataFrame(rows)


def test_describe_run_r1_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setattr("jarvis.cli.main.repo_root", lambda: tmp_path)

    bars = _weekday_bars(15, start=datetime(2010, 1, 4, tzinfo=timezone.utc))
    write_bars(tmp_path, "GBPUSD", 2010, 1, bars)

    result = runner.invoke(app, ["describe", "run", "--report", "R1"])

    assert result.exit_code == 0, result.output
    assert "Bars examined" in result.output
    assert "Sessions" in result.output
    assert "Box plot" in result.output

    describe_dir = tmp_path / "reports" / "describe"
    md_files = list(describe_dir.glob("R1__*.md"))
    parquet_files = list(describe_dir.glob("R1__*.parquet"))
    svg_files = list(describe_dir.glob("R1__*__boxplot.svg"))
    assert len(md_files) == 1
    assert len(parquet_files) == 1
    assert len(svg_files) == 1

    md_text = md_files[0].read_text(encoding="utf-8")
    assert md_text.startswith("# DESCRIPTIVE -- EXPLORATORY -- NOT EVIDENCE")
    assert "R1 -- Session range anatomy" in md_text
    for session in ("pre_london", "london", "new_york"):
        assert f"`{session}` -- by year" in md_text

    svg_text = svg_files[0].read_text(encoding="utf-8")
    assert svg_text.startswith("<svg")


def test_describe_run_r1_with_no_bars_still_writes_a_report(tmp_path, monkeypatch):
    monkeypatch.setattr("jarvis.cli.main.repo_root", lambda: tmp_path)

    result = runner.invoke(app, ["describe", "run", "--report", "R1"])

    assert result.exit_code == 0, result.output
    describe_dir = tmp_path / "reports" / "describe"
    md_files = list(describe_dir.glob("R1__*.md"))
    assert len(md_files) == 1
    md_text = md_files[0].read_text(encoding="utf-8")
    assert "Bars examined: 0" in md_text


def test_describe_run_r2_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setattr("jarvis.cli.main.repo_root", lambda: tmp_path)

    bars = _weekday_bars(70, start=datetime(2010, 1, 4, tzinfo=timezone.utc))
    write_bars(tmp_path, "GBPUSD", 2010, 1, bars)

    result = runner.invoke(app, ["describe", "run", "--report", "R2"])

    assert result.exit_code == 0, result.output
    assert "Bars examined" in result.output
    assert "Pooled buckets" in result.output
    assert "Year rows" in result.output

    describe_dir = tmp_path / "reports" / "describe"
    md_files = list(describe_dir.glob("R2__*.md"))
    parquet_files = list(describe_dir.glob("R2__*.parquet"))
    assert len(md_files) == 1
    assert len(parquet_files) == 1

    md_text = md_files[0].read_text(encoding="utf-8")
    assert md_text.startswith("# DESCRIPTIVE -- EXPLORATORY -- NOT EVIDENCE")
    assert "R2 -- London range conditional on pre-London range percentile" in md_text
    assert "## Pooled" in md_text
    assert "## By year" in md_text


def test_describe_run_r2_with_no_bars_still_writes_a_report(tmp_path, monkeypatch):
    monkeypatch.setattr("jarvis.cli.main.repo_root", lambda: tmp_path)

    result = runner.invoke(app, ["describe", "run", "--report", "R2"])

    assert result.exit_code == 0, result.output
    describe_dir = tmp_path / "reports" / "describe"
    md_files = list(describe_dir.glob("R2__*.md"))
    assert len(md_files) == 1
    md_text = md_files[0].read_text(encoding="utf-8")
    assert "Bars examined: 0" in md_text


def test_report_r3_refuses_not_yet_implemented():
    result = runner.invoke(app, ["describe", "run", "--report", "R3"])
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
