import numpy as np
import pytest

from jarvis.core.errors import IntegrityError
from jarvis.ingest.histdata import NS_PER_HOUR, _naive_ns, detect_column_order
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
    result = parse_histdata_csv(csv_path, "GBPUSD", 2017, 5)

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
    result = parse_histdata_csv(csv_path, "GBPUSD", 2017, 5)
    # last 3 digits (001) must survive as exactly 1ms = 1_000_000ns
    remainder_ns = int(result.ts_utc_ns[0]) % 1_000_000_000
    assert remainder_ns == 1_000_000


# Volume -> null ------------------------------------------------------------


def test_volume_column_mapped_to_null_not_zero(tmp_path):
    csv_path = tmp_path / "DAT_ASCII_GBPUSD_T_201705.csv"
    _write_csv(csv_path, [_row("20170501 120000000", 1.30000, 1.30010, volume="0")])
    result = parse_histdata_csv(csv_path, "GBPUSD", 2017, 5)
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
        parse_histdata_csv(csv_path, "GBPUSD", 2017, 5)
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
        parse_histdata_csv(csv_path, "GBPUSD", 2017, 5)
    assert "line 2" in str(excinfo.value)


def test_bid_gte_ask_raises(tmp_path):
    # 200 consistent bid<ask rows plus 1 isolated inversion (0.5%, well
    # under detect_column_order's 1% tolerance) -- this exercises the
    # sanity-net check for a genuine isolated corruption, distinct from
    # test_mixed_column_order_raises' ~75/25 case, which is ambiguous
    # column order, not an isolated bad row.
    csv_path = tmp_path / "DAT_ASCII_GBPUSD_T_201705.csv"
    rows = [_row(f"201705{(1 + i // 1440):02d} {(i % 1440) // 60:02d}{i % 60:02d}00000", 1.29000, 1.29010) for i in range(200)]
    rows.append(_row("20170501 235959000", 1.29020, 1.29010))  # isolated bid > ask
    _write_csv(csv_path, rows)
    with pytest.raises(IntegrityError) as excinfo:
        parse_histdata_csv(csv_path, "GBPUSD", 2017, 5)
    assert "line 201" in str(excinfo.value)
    assert "bid" in str(excinfo.value).lower()


def test_bid_equal_ask_is_accepted(tmp_path):
    """A zero spread is unusual but real, and this is not a hypothetical:
    the genuine 2009-11 file contains exactly five rows where bid == ask
    (all on 2009-11-13, around 15:30) and none at all where bid > ask.
    Rejecting equality made that month unimportable while contradicting
    detect_column_order's own 1% tolerance, which exists to absorb these
    quotes. A true inversion (bid > ask) is still corruption -- see
    test_bid_gte_ask_raises."""
    csv_path = tmp_path / "DAT_ASCII_GBPUSD_T_200911.csv"
    _write_csv(
        csv_path,
        [
            _row("20091113 153007000", 1.66910, 1.66920),
            _row("20091113 153008000", 1.66910, 1.66910),  # zero spread
            _row("20091113 153009000", 1.66900, 1.66920),
        ],
    )
    result = parse_histdata_csv(csv_path, "GBPUSD", 2009, 11)
    assert result.row_count == 3
    assert result.bid[1] == result.ask[1] == pytest.approx(1.66910)


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
        parse_histdata_csv(csv_path, "GBPUSD", 2017, 5)
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
        parse_histdata_csv(csv_path, "GBPUSD", 2017, 5)
    assert "line 2" in str(excinfo.value)


def test_empty_file_raises(tmp_path):
    csv_path = tmp_path / "DAT_ASCII_GBPUSD_T_201705.csv"
    csv_path.write_text("", encoding="utf-8")
    with pytest.raises(IntegrityError):
        parse_histdata_csv(csv_path, "GBPUSD", 2017, 5)


# detect_column_order (WP-009-CORRECTION finding 2) -------------------------


def test_detects_bid_ask_order():
    # col2 (bid) < col3 (ask) throughout -- the documented order.
    col2 = np.array([1.29000, 1.29010, 1.29020, 1.29030], dtype=np.float64)
    col3 = np.array([1.29010, 1.29020, 1.29030, 1.29040], dtype=np.float64)
    order, evidence = detect_column_order(col2, col3)
    assert order == "bid_ask"
    assert "0.0000" in evidence


def test_detects_ask_bid_order():
    # col2 > col3 throughout -- col2 is actually ask, col3 is actually bid.
    col2 = np.array([1.29010, 1.29020, 1.29030, 1.29040], dtype=np.float64)
    col3 = np.array([1.29000, 1.29010, 1.29020, 1.29030], dtype=np.float64)
    order, evidence = detect_column_order(col2, col3)
    assert order == "ask_bid"
    assert "1.0000" in evidence


def test_mixed_column_order_raises():
    # ~75% col2 > col3, ~25% col2 < col3 -- neither threshold met.
    n = 400
    col2 = np.empty(n, dtype=np.float64)
    col3 = np.empty(n, dtype=np.float64)
    for i in range(n):
        if i < 300:  # 75%: col2 > col3
            col2[i], col3[i] = 1.29010, 1.29000
        else:  # 25%: col2 < col3
            col2[i], col3[i] = 1.29000, 1.29010
    with pytest.raises(IntegrityError) as excinfo:
        detect_column_order(col2, col3)
    assert "0.7500" in str(excinfo.value)


def test_zero_spread_rows_tolerated():
    # 0.5% exact-equal (zero-spread) rows among an otherwise clean
    # bid_ask file -- equal rows count toward neither numerator, so they
    # must not push the fraction into the ambiguous middle range.
    n = 1000
    col2 = np.full(n, 1.29000, dtype=np.float64)
    col3 = np.full(n, 1.29010, dtype=np.float64)
    equal_rows = 5  # 0.5%
    col3[:equal_rows] = col2[:equal_rows]  # zero spread on these rows
    order, evidence = detect_column_order(col2, col3)
    assert order == "bid_ask"
