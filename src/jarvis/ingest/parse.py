"""Dukascopy .bi5 binary parsing: LZMA decompress, 20-byte record unpack."""

import lzma
import struct
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from jarvis.core.errors import IntegrityError
from jarvis.core.types import Nanos

_RECORD_STRUCT = struct.Struct(">IIIff")
_RECORD_SIZE = _RECORD_STRUCT.size  # 20
_NS_PER_MS = 1_000_000

# Field order matches the wire format exactly (Technical Bible Part 1 SS D.2):
# ask precedes bid. Swapping these two fields would silently invert every
# spread while still producing plausible-looking output -- see
# test_parse_bi5_arrays_matches_parse_bi5_on_golden_fixture.
_ARRAY_DTYPE = np.dtype(
    [
        ("ms", ">u4"),
        ("ask", ">u4"),
        ("bid", ">u4"),
        ("ask_vol", ">f4"),
        ("bid_vol", ">f4"),
    ]
)


@dataclass(frozen=True, slots=True)
class Tick:
    ts_utc_ns: int  # millisecond resolution from the source; lower 6 digits always zero
    bid: float
    ask: float
    bid_volume: float | None
    ask_volume: float | None
    seq: int  # ordinal within the source hour, per Part 1 §D.2 — the tie-breaker


@dataclass(frozen=True, slots=True)
class ParsedHour:
    instrument: str
    hour_utc_ns: Nanos
    ticks: tuple[Tick, ...]  # sorted by (ts_utc_ns, seq); empty tuple is valid
    record_count: int
    source_path: Path


def _empty_hour(instrument: str, hour_utc_ns: Nanos, source_path: Path) -> ParsedHour:
    return ParsedHour(
        instrument=instrument,
        hour_utc_ns=hour_utc_ns,
        ticks=(),
        record_count=0,
        source_path=source_path,
    )


def _read_and_validate(path: Path) -> bytes:
    """Read a .bi5 file, LZMA-decompress it, and validate the decompressed
    length is a multiple of the record size. Returns b"" for a 0-byte file
    or an LZMA stream that decompresses to zero bytes -- both are valid
    "no ticks" inputs, not errors. Shared by parse_bi5 and
    parse_bi5_arrays so the two paths cannot drift on error handling."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise IntegrityError(f"cannot read raw tick blob at {path}: {exc}") from exc

    if len(raw) == 0:
        return b""

    try:
        decompressed = lzma.decompress(raw)
    except lzma.LZMAError as exc:
        raise IntegrityError(f"{path}: not valid LZMA data: {exc}") from exc

    if len(decompressed) == 0:
        return b""

    remainder = len(decompressed) % _RECORD_SIZE
    if remainder != 0:
        raise IntegrityError(
            f"{path}: decompressed length {len(decompressed)} is not a multiple of "
            f"{_RECORD_SIZE} (remainder {remainder})"
        )

    return decompressed


def parse_bi5(
    path: Path,
    instrument: str,
    hour_utc_ns: Nanos,
    point_scale: float,
) -> ParsedHour:
    """Decompress and parse one raw .bi5 file. A 0-byte file (the recorded
    form of an `empty`-classified hour) is valid input and returns a
    ParsedHour with zero ticks — not an error. A non-empty file that is not
    valid LZMA, or whose decompressed length is not a multiple of 20 bytes,
    is a parse failure — see "Required behaviour" for exactly how to fail."""
    decompressed = _read_and_validate(path)
    if len(decompressed) == 0:
        return _empty_hour(instrument, hour_utc_ns, path)

    ticks: list[Tick] = []
    try:
        for seq, offset in enumerate(range(0, len(decompressed), _RECORD_SIZE)):
            ms, ask_points, bid_points, ask_volume, bid_volume = _RECORD_STRUCT.unpack_from(
                decompressed, offset
            )
            ticks.append(
                Tick(
                    ts_utc_ns=hour_utc_ns + ms * _NS_PER_MS,
                    bid=bid_points * point_scale,
                    ask=ask_points * point_scale,
                    bid_volume=bid_volume,
                    ask_volume=ask_volume,
                    seq=seq,
                )
            )
    except struct.error as exc:
        raise IntegrityError(f"{path}: failed to unpack a tick record: {exc}") from exc

    return ParsedHour(
        instrument=instrument,
        hour_utc_ns=hour_utc_ns,
        ticks=tuple(ticks),
        record_count=len(ticks),
        source_path=path,
    )


@dataclass(frozen=True, slots=True)
class TickArrays:
    """Column-oriented equivalent of ParsedHour.ticks, for bulk processing.
    Arrays are parallel and already in (ts_utc_ns, seq) order -- which is
    simply file order, since seq IS the file ordinal. seq itself is not
    stored: it is the array index by construction."""

    instrument: str
    hour_utc_ns: Nanos
    ts_utc_ns: np.ndarray  # int64
    bid: np.ndarray  # float64
    ask: np.ndarray  # float64
    bid_volume: np.ndarray  # float64
    ask_volume: np.ndarray  # float64
    record_count: int
    source_path: Path


def _empty_tick_arrays(instrument: str, hour_utc_ns: Nanos, source_path: Path) -> TickArrays:
    return TickArrays(
        instrument=instrument,
        hour_utc_ns=hour_utc_ns,
        ts_utc_ns=np.empty(0, dtype=np.int64),
        bid=np.empty(0, dtype=np.float64),
        ask=np.empty(0, dtype=np.float64),
        bid_volume=np.empty(0, dtype=np.float64),
        ask_volume=np.empty(0, dtype=np.float64),
        record_count=0,
        source_path=source_path,
    )


def parse_bi5_arrays(
    path: Path,
    instrument: str,
    hour_utc_ns: Nanos,
    point_scale: float,
) -> TickArrays:
    """Vectorised equivalent of parse_bi5, returning column arrays instead
    of a tuple of Tick dataclasses. Decodes the whole record block in one
    `np.frombuffer` call instead of constructing one Python object per
    tick: measured, this collapses the Tick-construction cost from ~2us/
    tick to a small fraction of that, roughly two orders of magnitude on
    that specific cost. The overall wall-clock improvement per hour is far
    more modest (~3x measured), because LZMA decompression -- shared by
    both functions via `_read_and_validate` and unaffected by this change
    -- dominates the remaining time. See WP-005 closing notes for measured
    throughput.

    Shares validation with parse_bi5 via `_read_and_validate`, so error
    behaviour (empty file, invalid LZMA, misaligned length) is identical."""
    decompressed = _read_and_validate(path)
    if len(decompressed) == 0:
        return _empty_tick_arrays(instrument, hour_utc_ns, path)

    records = np.frombuffer(decompressed, dtype=_ARRAY_DTYPE)

    ts_utc_ns = hour_utc_ns + records["ms"].astype(np.int64) * _NS_PER_MS
    bid = records["bid"].astype(np.float64) * point_scale
    ask = records["ask"].astype(np.float64) * point_scale
    bid_volume = records["bid_vol"].astype(np.float64)
    ask_volume = records["ask_vol"].astype(np.float64)

    return TickArrays(
        instrument=instrument,
        hour_utc_ns=hour_utc_ns,
        ts_utc_ns=ts_utc_ns,
        bid=bid,
        ask=ask,
        bid_volume=bid_volume,
        ask_volume=ask_volume,
        record_count=int(len(records)),
        source_path=path,
    )
