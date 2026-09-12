import numpy as np
import pytest

from jarvis.core.errors import IntegrityError
from jarvis.ingest.histdata import NS_PER_HOUR, _naive_ns, detect_tz_convention, parse_histdata_csv

_RECORD = "{ts},{bid:.5f},{ask:.5f},0\n"


def _write_csv(path, rows: list[tuple[str, float, float]]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for ts, bid, ask in rows:
            f.write(_RECORD.format(ts=ts, bid=bid, ask=ask))


def _stamp(y: int, mo: int, d: int, h: int, mi: int = 0, s: int = 0, ms: int = 0) -> str:
    return f"{y:04d}{mo:02d}{d:02d} {h:02d}{mi:02d}{s:02d}{ms:03d}"


# detect_tz_convention -------------------------------------------------


def test_detects_ny_local_from_edt_friday_close():
    # 2017-05-05 is an EDT Friday. Close at local 16:59 -> ny_local.
    stamps = np.array(
        [
            _naive_ns(2017, 5, 5, 16, 0),
            _naive_ns(2017, 5, 5, 16, 59),
            _naive_ns(2017, 5, 7, 17, 1),  # reopen Sunday, >1h gap
        ],
        dtype=np.int64,
    )
    convention, evidence = detect_tz_convention(stamps, 2017, 5)
    assert convention == "ny_local"
    assert "2017-05-05" in evidence
    assert "16:59" in evidence


def test_detects_fixed_offset_from_edt_friday_close():
    # Same EDT Friday, but the file's own close stamp reads 15:59 --
    # revealing a fixed EST offset applied even during EDT.
    stamps = np.array(
        [
            _naive_ns(2017, 5, 5, 15, 0),
            _naive_ns(2017, 5, 5, 15, 59),
            _naive_ns(2017, 5, 7, 16, 1),
        ],
        dtype=np.int64,
    )
    convention, evidence = detect_tz_convention(stamps, 2017, 5)
    assert convention == "fixed_utc_minus_5"
    assert "15:59" in evidence


def test_est_fridays_are_not_discriminating():
    # 2017-01-06 is an EST Friday -- both conventions read 16:59, so this
    # is not evidence of anything. With no convention_hint, detection
    # must raise rather than guess.
    stamps = np.array(
        [
            _naive_ns(2017, 1, 6, 16, 0),
            _naive_ns(2017, 1, 6, 16, 59),
            _naive_ns(2017, 1, 8, 17, 1),
        ],
        dtype=np.int64,
    )
    with pytest.raises(IntegrityError):
        detect_tz_convention(stamps, 2017, 1)


def test_est_fridays_fall_back_to_convention_hint():
    stamps = np.array(
        [
            _naive_ns(2017, 1, 6, 16, 0),
            _naive_ns(2017, 1, 6, 16, 59),
            _naive_ns(2017, 1, 8, 17, 1),
        ],
        dtype=np.int64,
    )
    convention, evidence = detect_tz_convention(stamps, 2017, 1, convention_hint="ny_local")
    assert convention == "ny_local"
    assert "convention_hint" in evidence


def test_internally_mixed_file_raises():
    # Two EDT Fridays in the same file disagreeing on close hour.
    stamps = np.array(
        [
            _naive_ns(2017, 5, 5, 16, 0),
            _naive_ns(2017, 5, 5, 16, 59),  # ny_local evidence
            _naive_ns(2017, 5, 8, 8, 0),
            _naive_ns(2017, 5, 12, 15, 0),
            _naive_ns(2017, 5, 12, 15, 59),  # fixed_utc_minus_5 evidence
            _naive_ns(2017, 5, 14, 17, 1),
        ],
        dtype=np.int64,
    )
    with pytest.raises(IntegrityError):
        detect_tz_convention(stamps, 2017, 5)


# Conversion -------------------------------------------------------------


def test_ny_local_conversion_summer(tmp_path):
    # May (EDT, UTC-4): 12:00 local -> 16:00 UTC.
    csv_path = tmp_path / "DAT_ASCII_GBPUSD_T_201705.csv"
    _write_csv(
        csv_path,
        [
            (_stamp(2017, 5, 5, 16, 59), 1.29000, 1.29010),  # discriminating Friday close
            (_stamp(2017, 5, 15, 12, 0), 1.30000, 1.30010),
            (_stamp(2017, 5, 21, 17, 1), 1.31000, 1.31010),  # reopen Sunday (contiguity, not required to discriminate)
        ],
    )
    result = parse_histdata_csv(csv_path, "GBPUSD", 2017, 5)
    assert result.convention == "ny_local"
    idx = 1  # the 12:00 local row
    expected_utc_ns = _naive_ns(2017, 5, 15, 12, 0) + 4 * NS_PER_HOUR
    assert int(result.ts_utc_ns[idx]) == expected_utc_ns


def test_ny_local_conversion_winter(tmp_path):
    # January (EST, UTC-5): 12:00 local -> 17:00 UTC.
    csv_path = tmp_path / "DAT_ASCII_GBPUSD_T_201701.csv"
    _write_csv(
        csv_path,
        [
            (_stamp(2017, 1, 15, 12, 0), 1.30000, 1.30010),
            (_stamp(2017, 1, 21, 17, 1), 1.31000, 1.31010),
        ],
    )
    result = parse_histdata_csv(csv_path, "GBPUSD", 2017, 1, convention_hint="ny_local")
    expected_utc_ns = _naive_ns(2017, 1, 15, 12, 0) + 5 * NS_PER_HOUR
    assert int(result.ts_utc_ns[0]) == expected_utc_ns


def test_fixed_convention_conversion(tmp_path):
    # fixed_utc_minus_5: always +5h, regardless of month (May, normally EDT).
    csv_path = tmp_path / "DAT_ASCII_GBPUSD_T_201705.csv"
    _write_csv(
        csv_path,
        [
            (_stamp(2017, 5, 15, 12, 0), 1.30000, 1.30010),
            (_stamp(2017, 5, 21, 17, 1), 1.31000, 1.31010),
        ],
    )
    result = parse_histdata_csv(csv_path, "GBPUSD", 2017, 5, convention_hint="fixed_utc_minus_5")
    expected_utc_ns = _naive_ns(2017, 5, 15, 12, 0) + 5 * NS_PER_HOUR
    assert int(result.ts_utc_ns[0]) == expected_utc_ns


def test_nfp_anchor_ny_local(tmp_path):
    # NFP releases at 08:30 NY local, first Friday of the month. In an
    # EDT month, that is 12:30 UTC.
    csv_path = tmp_path / "DAT_ASCII_GBPUSD_T_201705.csv"
    _write_csv(
        csv_path,
        [
            (_stamp(2017, 5, 5, 8, 30), 1.29500, 1.29510),  # NFP burst tick
            (_stamp(2017, 5, 5, 16, 59), 1.29000, 1.29010),  # discriminating Friday close
            (_stamp(2017, 5, 7, 17, 1), 1.31000, 1.31010),
        ],
    )
    result = parse_histdata_csv(csv_path, "GBPUSD", 2017, 5)
    assert result.convention == "ny_local"
    expected_utc_ns = _naive_ns(2017, 5, 5, 8, 30) + 4 * NS_PER_HOUR
    assert int(result.ts_utc_ns[0]) == expected_utc_ns
    from datetime import datetime, timezone

    dt = datetime.fromtimestamp(result.ts_utc_ns[0] / 1e9, tz=timezone.utc)
    assert (dt.hour, dt.minute) == (12, 30)


def test_pre_2007_dst_rules(tmp_path):
    """November 2004 falls under the pre-2007 US DST regime (DST ended
    the LAST Sunday of October, not the first Sunday of November). A
    November 2, 2004 stamp must convert using EST (-5) -- if jarvis.
    timeengine were projecting modern rules backward, this date would be
    incorrectly treated as still-DST (EDT, -4) since the modern rule's
    November transition (first Sunday) had not yet occurred."""
    csv_path = tmp_path / "DAT_ASCII_GBPUSD_T_200411.csv"
    _write_csv(
        csv_path,
        [
            (_stamp(2004, 11, 2, 12, 0), 1.80000, 1.80010),
        ],
    )
    result = parse_histdata_csv(csv_path, "GBPUSD", 2004, 11, convention_hint="ny_local")
    expected_utc_ns = _naive_ns(2004, 11, 2, 12, 0) + 5 * NS_PER_HOUR  # EST, not EDT
    assert int(result.ts_utc_ns[0]) == expected_utc_ns
