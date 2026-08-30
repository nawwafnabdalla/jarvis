"""Tick -> 1-minute bar resampler (WP-005 items 2 and 4).

The single most consequential rule in this module: a bar exists if and
only if at least one tick falls in its minute. A minute with no ticks
produces NO ROW -- never zero-filled, never forward-filled, never
interpolated. Get this wrong and quiet periods silently become
flat-price periods, which looks like real data and is not.

Hole detection (item 4) is why WP-004c (the fetch log) had to land first:
the filesystem alone decides what gets resampled -- a 0-byte blob is a
legitimate zero-bar hour (market closed), and no blob at all is a hole.
The fetch log is consulted only to explain a hole in an error message; it
never influences the resample decision itself.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import polars as pl

from jarvis.core.config import load_instruments
from jarvis.core.errors import IntegrityError, UserError
from jarvis.core.types import Nanos
from jarvis.ingest.fetch_log import read_fetch_log
from jarvis.ingest.parse import TickArrays, parse_bi5_arrays
from jarvis.ingest.urls import NS_PER_HOUR, raw_blob_path

from jarvis.bars.store import BAR_SCHEMA, write_bars

NS_PER_MINUTE = 60_000_000_000

_HOLE_PREVIEW_LIMIT = 5


@dataclass(frozen=True, slots=True)
class ResampleReport:
    instrument: str
    range_start_ns: Nanos
    range_end_ns: Nanos
    hours_expected: int
    hours_with_data: int
    hours_empty: int  # 0-byte blob: market closed, legitimately no bars
    hours_unfetched: int  # no blob at all: a HOLE
    unfetched_hours: tuple[Nanos, ...]
    bars_written: int
    minutes_absent: int  # minutes in range with no bar (informational)
    months_written: tuple[str, ...]
    started_utc: str
    completed_utc: str


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _iso(ns: Nanos) -> str:
    return datetime.fromtimestamp(ns // 1_000_000_000, tz=timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def _month_of(hour_utc_ns: Nanos) -> tuple[int, int]:
    dt = datetime.fromtimestamp(hour_utc_ns // 1_000_000_000, tz=timezone.utc)
    return dt.year, dt.month


def _hole_reasons(repo_root: Path, instrument: str, hours: list[Nanos]) -> list[str]:
    """Best-effort explanation for the first few holes, drawn from the
    fetch log -- informational only, per the module docstring. Never used
    to decide what gets resampled."""
    reasons = []
    cache: dict[tuple[int, int], dict] = {}
    for hour_ns in hours[:_HOLE_PREVIEW_LIMIT]:
        key = _month_of(hour_ns)
        if key not in cache:
            cache[key] = read_fetch_log(repo_root, instrument, *key)
        entry = cache[key].get(hour_ns)
        reason = "never attempted" if entry is None else entry.status
        reasons.append(f"{_iso(hour_ns)} ({reason})")
    return reasons


def _new_accumulator() -> dict[str, list]:
    return {column: [] for column in BAR_SCHEMA}


def _append_bar(
    acc: dict[str, list],
    *,
    minute_start_ns: int,
    sub_ts: np.ndarray,
    sub_bid: np.ndarray,
    sub_ask: np.ndarray,
    prev_last_tick_ns: int | None,
) -> None:
    minute_end_ns = minute_start_ns + NS_PER_MINUTE
    first_tick_ns = int(sub_ts[0])
    last_tick_ns = int(sub_ts[-1])

    bid_o, bid_c = float(sub_bid[0]), float(sub_bid[-1])
    ask_o, ask_c = float(sub_ask[0]), float(sub_ask[-1])
    # bid_h/bid_l and ask_h/ask_l are independently the max/min of their own
    # series -- deliberately NOT derived from a mid price, so a bar's
    # bid_h and ask_h may come from different ticks. This is correct.
    bid_h, bid_l = float(sub_bid.max()), float(sub_bid.min())
    ask_h, ask_l = float(sub_ask.max()), float(sub_ask.min())

    spreads = sub_ask - sub_bid
    spread_open = ask_o - bid_o
    spread_max = float(spreads.max())

    # Time-weighted average spread, normalised by (minute_end - first_tick)
    # rather than a fixed 60e9 (resolved specification gap, Technical Bible
    # SS D.3.1 is silent on the sub-interval before the first tick): this is
    # the only quantity the bar's own data supports, and it correctly does
    # not shrink the reported spread of a bar whose first tick arrives late.
    if len(sub_ts) == 1:
        weights = np.array([minute_end_ns - first_tick_ns], dtype=np.float64)
    else:
        weights = np.empty(len(sub_ts), dtype=np.float64)
        weights[:-1] = np.diff(sub_ts)
        weights[-1] = minute_end_ns - last_tick_ns
    spread_twa = float(np.sum(spreads * weights) / (minute_end_ns - first_tick_ns))

    prev_gap_ns = None if prev_last_tick_ns is None else first_tick_ns - prev_last_tick_ns

    acc["ts_utc_ns"].append(minute_start_ns)
    acc["bid_o"].append(bid_o)
    acc["bid_h"].append(bid_h)
    acc["bid_l"].append(bid_l)
    acc["bid_c"].append(bid_c)
    acc["ask_o"].append(ask_o)
    acc["ask_h"].append(ask_h)
    acc["ask_l"].append(ask_l)
    acc["ask_c"].append(ask_c)
    acc["tick_count"].append(int(len(sub_ts)))
    acc["first_tick_ns"].append(first_tick_ns)
    acc["last_tick_ns"].append(last_tick_ns)
    acc["spread_open"].append(spread_open)
    acc["spread_max"].append(spread_max)
    acc["spread_twa"].append(spread_twa)
    acc["prev_gap_ns"].append(prev_gap_ns)


def _resample_hour_into(
    acc: dict[str, list], ticks: TickArrays, prev_last_tick_ns: int | None
) -> int | None:
    """Aggregate one hour's ticks into minute bars, appending into `acc`.
    Ticks are assumed sorted by (ts_utc_ns, seq) -- ParsedHour/TickArrays's
    documented invariant -- so their minute index is non-decreasing and
    `np.unique` on it yields correctly-ordered run boundaries without an
    explicit sort."""
    n = len(ticks.ts_utc_ns)
    if n == 0:
        return prev_last_tick_ns

    minute_idx = ticks.ts_utc_ns // NS_PER_MINUTE
    unique_minutes, start_indices = np.unique(minute_idx, return_index=True)
    end_indices = np.append(start_indices[1:], n)

    for minute, start, end in zip(unique_minutes, start_indices, end_indices):
        _append_bar(
            acc,
            minute_start_ns=int(minute) * NS_PER_MINUTE,
            sub_ts=ticks.ts_utc_ns[start:end],
            sub_bid=ticks.bid[start:end],
            sub_ask=ticks.ask[start:end],
            prev_last_tick_ns=prev_last_tick_ns,
        )
        prev_last_tick_ns = int(ticks.ts_utc_ns[end - 1])

    return prev_last_tick_ns


def resample_range(
    repo_root: Path,
    instrument: str,
    start_ns: Nanos,
    end_ns: Nanos,
    *,
    allow_incomplete: bool = False,
) -> ResampleReport:
    """Resample every UTC hour in [start_ns, end_ns) to 1-minute bars.

    start_ns and end_ns must both be hour-aligned; raises UserError
    otherwise. Classifies every hour purely from the filesystem: a 0-byte
    raw blob is a legitimate zero-bar hour (market closed); no blob at all
    is a HOLE. By default (allow_incomplete=False) raises IntegrityError
    if any hole exists in the range, rather than silently producing a
    partial-looking result -- pass allow_incomplete=True to proceed
    anyway (holes are then recorded in the report, not resampled).

    Processes hour by hour, holding at most one hour of ticks in memory at
    a time, accumulating bars (not ticks) for the current month, and
    writing+releasing on every month boundary."""
    if start_ns % NS_PER_HOUR != 0 or end_ns % NS_PER_HOUR != 0:
        raise UserError(
            f"start_ns ({start_ns}) and end_ns ({end_ns}) must both be hour-aligned"
        )
    if start_ns >= end_ns:
        raise UserError(f"start_ns ({start_ns}) must be strictly before end_ns ({end_ns})")

    started_utc = _utc_now_iso()
    point_scale = load_instruments()[instrument].point_scale

    hours = [Nanos(ns) for ns in range(start_ns, end_ns, NS_PER_HOUR)]

    hours_with_data = 0
    hours_empty = 0
    unfetched_hours: list[Nanos] = []

    classified: list[tuple[Nanos, str]] = []
    for hour_ns in hours:
        path = raw_blob_path(repo_root, instrument, hour_ns)
        if not path.is_file():
            unfetched_hours.append(hour_ns)
            classified.append((hour_ns, "unfetched"))
            continue
        if path.stat().st_size == 0:
            hours_empty += 1
            classified.append((hour_ns, "empty"))
            continue
        hours_with_data += 1
        classified.append((hour_ns, "data"))

    if unfetched_hours and not allow_incomplete:
        reasons = _hole_reasons(repo_root, instrument, unfetched_hours)
        more = len(unfetched_hours) - len(reasons)
        suffix = f", and {more} more" if more > 0 else ""
        raise IntegrityError(
            f"{len(unfetched_hours)} of {len(hours)} hours in "
            f"[{_iso(start_ns)}, {_iso(end_ns)}) have no raw blob at all -- "
            f"a HOLE, not a market-closed empty hour: {', '.join(reasons)}{suffix}. "
            "Pass allow_incomplete=True to resample the rest anyway."
        )

    bars_written = 0
    months_written: list[str] = []
    prev_last_tick_ns: int | None = None
    current_month: tuple[int, int] | None = None
    acc = _new_accumulator()

    def flush() -> None:
        nonlocal bars_written, acc
        if current_month is None or not acc["ts_utc_ns"]:
            return
        frame = pl.DataFrame(acc, schema=BAR_SCHEMA)
        write_bars(repo_root, instrument, current_month[0], current_month[1], frame)
        bars_written += len(acc["ts_utc_ns"])
        months_written.append(f"{current_month[0]:04d}-{current_month[1]:02d}")
        acc = _new_accumulator()

    for hour_ns, kind in classified:
        month = _month_of(hour_ns)
        if current_month is None:
            current_month = month
        elif month != current_month:
            flush()
            current_month = month

        if kind != "data":
            continue

        path = raw_blob_path(repo_root, instrument, hour_ns)
        ticks = parse_bi5_arrays(path, instrument, hour_ns, point_scale)
        prev_last_tick_ns = _resample_hour_into(acc, ticks, prev_last_tick_ns)

    flush()

    completed_utc = _utc_now_iso()
    total_minutes = (end_ns - start_ns) // NS_PER_MINUTE

    return ResampleReport(
        instrument=instrument,
        range_start_ns=start_ns,
        range_end_ns=end_ns,
        hours_expected=len(hours),
        hours_with_data=hours_with_data,
        hours_empty=hours_empty,
        hours_unfetched=len(unfetched_hours),
        unfetched_hours=tuple(unfetched_hours),
        bars_written=bars_written,
        minutes_absent=int(total_minutes - bars_written),
        months_written=tuple(months_written),
        started_utc=started_utc,
        completed_utc=completed_utc,
    )
