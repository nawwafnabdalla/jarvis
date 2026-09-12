import pytest

from jarvis.core.errors import IntegrityError
from jarvis.ingest.histdata import NS_PER_HOUR, _naive_ns
from jarvis.ingest.histdata_import import _histdata_month_to_frame
from jarvis.ingest.histdata import parse_histdata_csv


def _write_csv(path, lines: list[str]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for line in lines:
            f.write(line + "\n")


def _row(ts: str, bid: float, ask: float, volume: str = "0") -> str:
    return f"{ts},{bid:.6f},{ask:.6f},{volume}"


# Golden fixture ----------------------------------------------------------


def test_golden_fixture_exact_values(tmp_path):
    csv_path = tmp_path / "DAT_ASCII_GBPUSD_T_201705.csv"
    _write_csv(
        csv_path,
        [
            _row("20170501 000001457", 1.291150, 1.291290),
            _row("20170501 000001999", 1.291160, 1.291300),
            _row("20170501 000002001", 1.291170, 1.291310),
        ],
    )
    result = parse_histdata_csv(csv_path, "GBPUSD", 2017, 5, convention_hint="ny_local")

    assert result.row_count == 3
    assert result.instrument == "GBPUSD"
    assert result.year == 2017
    assert result.month == 5

    base = _naive_ns(2017, 5, 1, 0, 0, 1) + 4 * NS_PER_HOUR  # EDT, +4h from local-as-utc
    assert int(result.ts_utc_ns[0]) == base + 457_000_000
    assert int(result.ts_utc_ns[1]) == base + 999_000_000
    assert int(result.ts_utc_ns[2]) == base + 1_000_000_000 + 1_000_000

    assert result.bid[0] == pytest.approx(1.291150, abs=1e-9)
    assert result.ask[0] == pytest.approx(1.291290, abs=1e-9)
    assert result.bid[2] == pytest.approx(1.291170, abs=1e-9)
    assert result.ask[2] == pytest.approx(1.291310, abs=1e-9)


def test_millisecond_precision_preserved(tmp_path):
    csv_path = tmp_path / "DAT_ASCII_GBPUSD_T_201705.csv"
    _write_csv(csv_path, [_row("20170501 120000001", 1.30000, 1.30010)])
    result = parse_histdata_csv(csv_path, "GBPUSD", 2017, 5, convention_hint="ny_local")
    # last 3 digits (001) must survive as exactly 1ms = 1_000_000ns
    remainder_ns = int(result.ts_utc_ns[0]) % 1_000_000_000
    assert remainder_ns == 1_000_000


# Volume -> null ------------------------------------------------------------


def test_volume_column_mapped_to_null_not_zero(tmp_path):
    csv_path = tmp_path / "DAT_ASCII_GBPUSD_T_201705.csv"
    _write_csv(csv_path, [_row("20170501 120000000", 1.30000, 1.30010, volume="0")])
    result = parse_histdata_csv(csv_path, "GBPUSD", 2017, 5, convention_hint="ny_local")
    frame = _histdata_month_to_frame(result)
    assert frame["bid_volume"].null_count() == frame.height
    assert frame["ask_volume"].null_count() == frame.height
    # not merely "falsy" -- genuinely null, not 0.0
    assert frame["bid_volume"].to_list() == [None]
    assert frame["ask_volume"].to_list() == [None]


# Malformed input -----------------------------------------------------------


def test_malformed_row_raises_with_line_number(tmp_path):
    csv_path = tmp_path / "DAT_ASCII_GBPUSD_T_201705.csv"
    _write_csv(
        csv_path,
        [
            _row("20170501 000001000", 1.29000, 1.29010),
            "not a valid row at all",
            _row("20170501 000003000", 1.29002, 1.29012),
        ],
    )
    with pytest.raises(IntegrityError) as excinfo:
        parse_histdata_csv(csv_path, "GBPUSD", 2017, 5, convention_hint="ny_local")
    assert "line 2" in str(excinfo.value)


def test_malformed_timestamp_field_raises_with_line_number(tmp_path):
    csv_path = tmp_path / "DAT_ASCII_GBPUSD_T_201705.csv"
    _write_csv(
        csv_path,
        [
            _row("20170501 000001000", 1.29000, 1.29010),
            _row("2017050X 000002000", 1.29001, 1.29011),  # non-digit in date
        ],
    )
    with pytest.raises(IntegrityError) as excinfo:
        parse_histdata_csv(csv_path, "GBPUSD", 2017, 5, convention_hint="ny_local")
    assert "line 2" in str(excinfo.value)


def test_bid_gte_ask_raises(tmp_path):
    csv_path = tmp_path / "DAT_ASCII_GBPUSD_T_201705.csv"
    _write_csv(
        csv_path,
        [
            _row("20170501 000001000", 1.29000, 1.29010),
            _row("20170501 000002000", 1.29020, 1.29010),  # bid > ask
        ],
    )
    with pytest.raises(IntegrityError) as excinfo:
        parse_histdata_csv(csv_path, "GBPUSD", 2017, 5, convention_hint="ny_local")
    assert "line 2" in str(excinfo.value)
    assert "bid" in str(excinfo.value).lower()


def test_bid_equal_ask_raises(tmp_path):
    csv_path = tmp_path / "DAT_ASCII_GBPUSD_T_201705.csv"
    _write_csv(csv_path, [_row("20170501 000001000", 1.29000, 1.29000)])
    with pytest.raises(IntegrityError):
        parse_histdata_csv(csv_path, "GBPUSD", 2017, 5, convention_hint="ny_local")


def test_non_ascending_timestamps_raises(tmp_path):
    csv_path = tmp_path / "DAT_ASCII_GBPUSD_T_201705.csv"
    _write_csv(
        csv_path,
        [
            _row("20170501 000002000", 1.29000, 1.29010),
            _row("20170501 000001000", 1.29001, 1.29011),  # goes backwards
        ],
    )
    with pytest.raises(IntegrityError) as excinfo:
        parse_histdata_csv(csv_path, "GBPUSD", 2017, 5, convention_hint="ny_local")
    assert "line 2" in str(excinfo.value)
    assert "ascending" in str(excinfo.value).lower()


def test_wrong_month_for_filename_raises(tmp_path):
    csv_path = tmp_path / "DAT_ASCII_GBPUSD_T_201705.csv"
    _write_csv(
        csv_path,
        [
            _row("20170501 000001000", 1.29000, 1.29010),
            _row("20170601 000001000", 1.29001, 1.29011),  # wrong month
        ],
    )
    with pytest.raises(IntegrityError) as excinfo:
        parse_histdata_csv(csv_path, "GBPUSD", 2017, 5, convention_hint="ny_local")
    assert "line 2" in str(excinfo.value)


def test_empty_file_raises(tmp_path):
    csv_path = tmp_path / "DAT_ASCII_GBPUSD_T_201705.csv"
    csv_path.write_text("", encoding="utf-8")
    with pytest.raises(IntegrityError):
        parse_histdata_csv(csv_path, "GBPUSD", 2017, 5, convention_hint="ny_local")
