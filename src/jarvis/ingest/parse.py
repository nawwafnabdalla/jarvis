"""Dukascopy .bi5 binary parsing: LZMA decompress, 20-byte record unpack."""

import lzma
import struct
from dataclasses import dataclass
from pathlib import Path

from jarvis.core.errors import IntegrityError
from jarvis.core.types import Nanos

_RECORD_STRUCT = struct.Struct(">IIIff")
_RECORD_SIZE = _RECORD_STRUCT.size  # 20
_NS_PER_MS = 1_000_000


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
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise IntegrityError(f"cannot read raw tick blob at {path}: {exc}") from exc

    if len(raw) == 0:
        return _empty_hour(instrument, hour_utc_ns, path)

    try:
        decompressed = lzma.decompress(raw)
    except lzma.LZMAError as exc:
        raise IntegrityError(f"{path}: not valid LZMA data: {exc}") from exc

    if len(decompressed) == 0:
        return _empty_hour(instrument, hour_utc_ns, path)

    remainder = len(decompressed) % _RECORD_SIZE
    if remainder != 0:
        raise IntegrityError(
            f"{path}: decompressed length {len(decompressed)} is not a multiple of "
            f"{_RECORD_SIZE} (remainder {remainder})"
        )

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
