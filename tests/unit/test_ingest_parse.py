import lzma
import random
import struct
from datetime import datetime, timezone
from pathlib import Path

import pytest

from jarvis.core.errors import IntegrityError
from jarvis.core.types import Nanos
from jarvis.ingest.parse import Tick, parse_bi5

_RECORD_STRUCT = struct.Struct(">IIIff")
_POINT_SCALE = 1.0e-5  # GBPUSD

_GOLDEN_FIXTURE_PATH = (
    Path(__file__).resolve().parents[1] / "fixtures" / "dukascopy" / "2024-01-15-03h_sample.bi5"
)
_GOLDEN_HOUR_NS = Nanos(int(datetime(2024, 1, 15, 3, tzinfo=timezone.utc).timestamp() * 1_000_000_000))


def _build_bi5(records: list[tuple[int, int, int, float, float]]) -> bytes:
    """Pack (ms, ask_points, bid_points, ask_volume, bid_volume) tuples into a
    compressed .bi5 blob, purely as test input construction — not used for
    the golden fixture's expected-value assertions, which are hand-written
    literals independent of this helper."""
    raw = b"".join(_RECORD_STRUCT.pack(*r) for r in records)
    return lzma.compress(raw)


def test_golden_fixture_produces_exact_known_ticks():
    parsed = parse_bi5(_GOLDEN_FIXTURE_PATH, "GBPUSD", _GOLDEN_HOUR_NS, _POINT_SCALE)

    assert parsed.record_count == 5
    assert len(parsed.ticks) == 5

    # Hand-verified expected values, computed independently of the fixture
    # builder: ts_utc_ns = hour_start_ns + ms * 1_000_000; price = points * 1e-5.
    hour_ns = 1_705_287_600_000_000_000
    assert hour_ns == _GOLDEN_HOUR_NS  # sanity check the hour boundary itself

    expected = (
        Tick(ts_utc_ns=hour_ns + 0, bid=0.99900, ask=1.00000, bid_volume=2.0, ask_volume=1.0, seq=0),
        Tick(ts_utc_ns=hour_ns + 1_000_000_000, bid=0.99910, ask=1.00010, bid_volume=0.5, ask_volume=0.5, seq=1),
        Tick(ts_utc_ns=hour_ns + 1_000_000_000, bid=0.99920, ask=1.00020, bid_volume=1.5, ask_volume=1.5, seq=2),
        Tick(ts_utc_ns=hour_ns + 60_000_000_000, bid=0.99930, ask=1.00030, bid_volume=2.5, ask_volume=2.5, seq=3),
        Tick(ts_utc_ns=hour_ns + 3_599_000_000_000, bid=0.99940, ask=1.00040, bid_volume=0.0, ask_volume=0.0, seq=4),
    )

    for actual, exp in zip(parsed.ticks, expected):
        assert actual.ts_utc_ns == exp.ts_utc_ns
        assert actual.seq == exp.seq
        assert actual.bid == pytest.approx(exp.bid, abs=1e-9)
        assert actual.ask == pytest.approx(exp.ask, abs=1e-9)
        assert actual.bid_volume == pytest.approx(exp.bid_volume, abs=1e-6)
        assert actual.ask_volume == pytest.approx(exp.ask_volume, abs=1e-6)

    # The tie-broken pair (records 1 and 2, same millisecond) must appear in
    # file order, distinguished only by seq.
    assert parsed.ticks[1].ts_utc_ns == parsed.ticks[2].ts_utc_ns
    assert parsed.ticks[1].seq == 1
    assert parsed.ticks[2].seq == 2
    assert parsed.ticks[1].ask < parsed.ticks[2].ask


def test_zero_byte_file_produces_zero_ticks(tmp_path: Path):
    path = tmp_path / "00h_ticks.bi5"
    path.write_bytes(b"")

    parsed = parse_bi5(path, "GBPUSD", Nanos(0), _POINT_SCALE)

    assert parsed.ticks == ()
    assert parsed.record_count == 0


def test_valid_lzma_empty_payload_produces_zero_ticks(tmp_path: Path):
    path = tmp_path / "00h_ticks.bi5"
    path.write_bytes(lzma.compress(b""))

    parsed = parse_bi5(path, "GBPUSD", Nanos(0), _POINT_SCALE)

    assert parsed.ticks == ()
    assert parsed.record_count == 0


def test_invalid_lzma_raises_integrity_error(tmp_path: Path):
    path = tmp_path / "00h_ticks.bi5"
    path.write_bytes(b"this is definitely not an lzma stream, just garbage bytes")

    with pytest.raises(IntegrityError):
        parse_bi5(path, "GBPUSD", Nanos(0), _POINT_SCALE)


def test_non_multiple_of_20_length_raises_integrity_error_with_counts(tmp_path: Path):
    # One full 20-byte record plus 5 trailing garbage bytes -> 25 bytes, not
    # a multiple of 20.
    raw = _RECORD_STRUCT.pack(0, 100000, 99900, 1.0, 1.0) + b"\x00\x01\x02\x03\x04"
    assert len(raw) == 25
    path = tmp_path / "00h_ticks.bi5"
    path.write_bytes(lzma.compress(raw))

    with pytest.raises(IntegrityError) as excinfo:
        parse_bi5(path, "GBPUSD", Nanos(0), _POINT_SCALE)

    message = str(excinfo.value)
    assert "25" in message
    assert "5" in message  # remainder: 25 % 20 == 5


def test_missing_file_raises_integrity_error(tmp_path: Path):
    path = tmp_path / "does_not_exist.bi5"

    with pytest.raises(IntegrityError):
        parse_bi5(path, "GBPUSD", Nanos(0), _POINT_SCALE)


def test_point_scale_conversion_accuracy(tmp_path: Path):
    # 123456 points at point_scale=1e-5 -> 1.23456, hand-computed.
    raw = _build_bi5([(0, 123456, 123400, 1.0, 1.0)])
    path = tmp_path / "00h_ticks.bi5"
    path.write_bytes(raw)

    parsed = parse_bi5(path, "GBPUSD", Nanos(0), 1.0e-5)

    assert parsed.ticks[0].ask == pytest.approx(1.23456, abs=1e-9)
    assert parsed.ticks[0].bid == pytest.approx(1.23400, abs=1e-9)


def test_duplicate_millisecond_timestamps_preserved_in_seq_order(tmp_path: Path):
    raw = _build_bi5(
        [
            (500, 100000, 99900, 1.0, 1.0),
            (500, 100005, 99905, 2.0, 2.0),
        ]
    )
    path = tmp_path / "00h_ticks.bi5"
    path.write_bytes(raw)

    parsed = parse_bi5(path, "GBPUSD", Nanos(0), _POINT_SCALE)

    assert len(parsed.ticks) == 2
    assert parsed.ticks[0].ts_utc_ns == parsed.ticks[1].ts_utc_ns == 500_000_000
    assert parsed.ticks[0].seq == 0
    assert parsed.ticks[1].seq == 1
    assert parsed.ticks[0].ask == pytest.approx(1.00000, abs=1e-9)
    assert parsed.ticks[1].ask == pytest.approx(1.00005, abs=1e-9)


def test_ms_offset_outside_hour_is_preserved_not_clamped(tmp_path: Path):
    """A record whose ms offset places it at or beyond the next hour
    (>= 3_600_000) must still be parsed and included as-is, per the explicit
    instruction not to clamp, drop, or "correct" it — the parser reports
    what the file contains, it does not enforce Dukascopy's hour-boundary
    assumption itself."""
    raw = _build_bi5([(4_000_000, 100000, 99900, 1.0, 1.0)])  # 400,000ms past the hour boundary
    path = tmp_path / "00h_ticks.bi5"
    path.write_bytes(raw)

    hour_ns = Nanos(0)
    parsed = parse_bi5(path, "GBPUSD", hour_ns, _POINT_SCALE)

    assert parsed.record_count == 1
    assert parsed.ticks[0].ts_utc_ns == hour_ns + 4_000_000 * 1_000_000


@pytest.mark.parametrize("n", [1, 2, 17, 200, 500])
def test_property_n_records_round_trip(tmp_path: Path, n: int):
    """Property test: for a synthetically constructed valid .bi5 blob of N
    random records, parse_bi5 returns exactly N ticks in input record order.

    Uses stdlib `random` with a fixed seed rather than the `hypothesis`
    package: this work package explicitly forbids adding a dependency and
    does not grant pyproject.toml as an editable file, so an actual
    property-testing library isn't available here even though it's the
    project's eventual intended tool for this test class (Technical Bible
    Part 4 §S.2, §T.5). See the closing notes for this flagged as a
    discrepancy rather than silently resolved either way.
    """
    rng = random.Random(1000 + n)
    records = [
        (
            rng.randrange(0, 3_600_000),
            rng.randrange(0, 2**32),
            rng.randrange(0, 2**32),
            rng.uniform(0, 1000),
            rng.uniform(0, 1000),
        )
        for _ in range(n)
    ]

    raw = _build_bi5(records)
    path = tmp_path / "00h_ticks.bi5"
    path.write_bytes(raw)

    hour_ns = Nanos(0)
    parsed = parse_bi5(path, "GBPUSD", hour_ns, _POINT_SCALE)

    assert parsed.record_count == n
    assert len(parsed.ticks) == n

    for seq, (tick, (ms, ask_pts, bid_pts, ask_vol, bid_vol)) in enumerate(
        zip(parsed.ticks, records)
    ):
        assert tick.seq == seq
        assert tick.ts_utc_ns == hour_ns + ms * 1_000_000
        assert tick.ask == pytest.approx(ask_pts * _POINT_SCALE, rel=1e-9)
        assert tick.bid == pytest.approx(bid_pts * _POINT_SCALE, rel=1e-9)
        assert tick.ask_volume == pytest.approx(ask_vol, rel=1e-5)
        assert tick.bid_volume == pytest.approx(bid_vol, rel=1e-5)
