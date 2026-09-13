"""Tick -> 1-minute bar resampler (WP-005 items 2 and 4; retargeted to
`data/tick/` by WP-010, superseding the original Dukascopy-blob source).

The single most consequential rule in this module: a bar exists if and
only if at least one tick falls in its minute. A minute with no ticks
produces NO ROW -- never zero-filled, never forward-filled, never
interpolated. Get this wrong and quiet periods silently become
flat-price periods, which looks like real data and is not.

SOURCE (WP-010 / D-059): reads `data/tick/` -- the HistData-backed store
built by `jarvis.ingest.histdata_import.write_ticks` and corrected by
D-055 (timezone), D-055h/D-056 (column order), and D-058 (tick-collision
dedup). The Dukascopy raw-.bi5-blob path this module used before WP-010
is gone from here: D-059 established that Dukascopy is no longer needed
for forward-testing (HistData's own weekly update cadence covers it), so
there is no live consumer left requiring this module to read blobs. The
Dukascopy fetcher and its own tests are untouched and left in the
repository, dormant, per D-059 -- only this module's SOURCE changed.

Unit of iteration is now a MONTH (one `data/tick/.../data.parquet` file),
not an hour (one `.bi5` blob): HistData ticks are already consolidated
per month, and a whole month's ticks (worst case ~1.3M rows, confirmed by
WP-009's real full import) comfortably fits in memory at once -- the same
per-month granularity `write_ticks` itself already uses. A month with no
tick Parquet file on disk is a HOLE (never imported, or not yet); a
present month simply contributing zero ticks in the requested sub-range
is not -- these are genuinely different pieces of information and must
not be conflated (see `resample_range`'s missing-months handling).

Ticks sharing an identical `ts_utc_ns` are a real, expected possibility
now (D-058's `row_sequence` tie-break) -- see `_resample_ticks_into` for
exactly how bar open/close handle this.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import polars as pl

from jarvis.core.errors import IntegrityError, UserError
from jarvis.core.types import Nanos
from jarvis.ingest.histdata_import import tick_path
from jarvis.timeengine import NS_PER_HOUR

from jarvis.bars.store import BAR_SCHEMA, write_bars

NS_PER_MINUTE = 60_000_000_000

_HOLE_PREVIEW_LIMIT = 5


@dataclass(frozen=True, slots=True)
class ResampleReport:
    instrument: str
    range_start_ns: Nanos
    range_end_ns: Nanos
    months_expected: int
    months_with_data: int
    months_missing: int  # no tick Parquet file at all: a HOLE
    missing_months: tuple[str, ...]  # "YYYY-MM" labels
    ticks_read: int
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


def _months_between(start_ns: Nanos, end_ns: Nanos) -> list[tuple[int, int]]:
    """Every (year, month) whose Parquet file could hold a tick in
    [start_ns, end_ns) -- end_ns is exclusive, so the last relevant
    instant is end_ns - 1."""
    start_dt = datetime.fromtimestamp(start_ns // 1_000_000_000, tz=timezone.utc)
    end_dt = datetime.fromtimestamp((end_ns - 1) // 1_000_000_000, tz=timezone.utc)
    months: list[tuple[int, int]] = []
    y, m = start_dt.year, start_dt.month
    while (y, m) <= (end_dt.year, end_dt.month):
        months.append((y, m))
        m += 1
        if m == 13:
            m = 1
            y += 1
    return months


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

    # Provisional: threaded only through THIS run's own bars. store.write_bars
    # recomputes prev_gap_ns as a pure function of the full stored (merged,
    # sorted) frame before writing, so this value is only ever correct on
    # its own when this run happens to be the only data ever written for
    # the month -- it is not relied upon to survive a merge with another
    # run's bars (WP-005-CORRECTION).
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


def _resample_ticks_into(
    acc: dict[str, list], ts: np.ndarray, bid: np.ndarray, ask: np.ndarray, prev_last_tick_ns: int | None
) -> int | None:
    """Aggregate a sorted run of ticks into minute bars, appending into
    `acc`. Ticks are assumed sorted ascending by ts_utc_ns -- true of
    `data/tick/`'s on-disk order (`write_ticks` sorts by
    `(ts_utc_ns, row_sequence)`, D-058) -- so minute index is
    non-decreasing and `np.unique` yields correctly-ordered run boundaries
    without an explicit sort.

    Two ticks sharing an identical ts_utc_ns (a real, expected case since
    D-058 -- distinguished on disk only by row_sequence) land in the same
    minute bucket and are handled correctly with no special-casing here:
    `sub_bid[0]`/`sub_bid[-1]` (a bar's open/close) reflect FILE ORDER
    among same-timestamp ticks, which IS row_sequence order, since the
    frame was read off disk in that order and never re-sorted by this
    function. This is the same tie-break principle the old Dukascopy path
    relied on via Tick.seq -- see test_resample_preserves_same_millisecond_
    ticks_in_row_sequence_order."""
    n = len(ts)
    if n == 0:
        return prev_last_tick_ns

    minute_idx = ts // NS_PER_MINUTE
    unique_minutes, start_indices = np.unique(minute_idx, return_index=True)
    end_indices = np.append(start_indices[1:], n)

    for minute, start, end in zip(unique_minutes, start_indices, end_indices):
        _append_bar(
            acc,
            minute_start_ns=int(minute) * NS_PER_MINUTE,
            sub_ts=ts[start:end],
            sub_bid=bid[start:end],
            sub_ask=ask[start:end],
            prev_last_tick_ns=prev_last_tick_ns,
        )
        prev_last_tick_ns = int(ts[end - 1])

    return prev_last_tick_ns


def resample_range(
    repo_root: Path,
    instrument: str,
    start_ns: Nanos,
    end_ns: Nanos,
    *,
    allow_incomplete: bool = False,
) -> ResampleReport:
    """Resample every tick in [start_ns, end_ns) from `data/tick/` to
    1-minute bars.

    start_ns and end_ns must both be hour-aligned; raises UserError
    otherwise (kept as a general sanity bound, though the underlying
    source is now month-granular). Classifies each covered month purely
    from the filesystem: no `data/tick/.../data.parquet` file at all is a
    HOLE. By default (allow_incomplete=False) raises IntegrityError if any
    hole exists in the range, rather than silently producing a
    partial-looking result -- pass allow_incomplete=True to proceed
    anyway (holes are then recorded in the report, not resampled; no bars
    file is written for a missing month, unlike a present-but-quiet one,
    which still writes an explicit empty-schema file -- these are
    deliberately not the same thing: one means "checked, nothing there",
    the other means "never checked at all").

    Processes month by month, holding at most one month of ticks in
    memory at a time (data/tick/'s own storage granularity), writing and
    releasing on every month boundary."""
    if start_ns % NS_PER_HOUR != 0 or end_ns % NS_PER_HOUR != 0:
        raise UserError(
            f"start_ns ({start_ns}) and end_ns ({end_ns}) must both be hour-aligned"
        )
    if start_ns >= end_ns:
        raise UserError(f"start_ns ({start_ns}) must be strictly before end_ns ({end_ns})")

    started_utc = _utc_now_iso()

    months = _months_between(start_ns, end_ns)

    present_months: list[tuple[int, int]] = []
    missing_months: list[tuple[int, int]] = []
    for year, month in months:
        if tick_path(repo_root, instrument, year, month).is_file():
            present_months.append((year, month))
        else:
            missing_months.append((year, month))

    if missing_months and not allow_incomplete:
        labels = [f"{y:04d}-{m:02d}" for y, m in missing_months]
        preview = labels[:_HOLE_PREVIEW_LIMIT]
        more = len(labels) - len(preview)
        suffix = f", and {more} more" if more > 0 else ""
        raise IntegrityError(
            f"{len(missing_months)} of {len(months)} months in "
            f"[{_iso(start_ns)}, {_iso(end_ns)}) have no tick data on disk at "
            f"data/tick/ -- a HOLE, not an imported-and-empty month: "
            f"{', '.join(preview)}{suffix}. "
            "Pass allow_incomplete=True to resample the rest anyway."
        )

    bars_written = 0
    ticks_read = 0
    months_written: list[str] = []
    prev_last_tick_ns: int | None = None
    acc = _new_accumulator()

    for year, month in present_months:
        path = tick_path(repo_root, instrument, year, month)
        df = pl.read_parquet(path)
        df = df.filter((pl.col("ts_utc_ns") >= start_ns) & (pl.col("ts_utc_ns") < end_ns))
        ticks_read += df.height

        if df.height > 0:
            ts = df["ts_utc_ns"].to_numpy()
            bid = df["bid"].to_numpy()
            ask = df["ask"].to_numpy()
            prev_last_tick_ns = _resample_ticks_into(acc, ts, bid, ask, prev_last_tick_ns)

        # Written even when acc is empty (e.g. the requested slice of this
        # month has zero ticks): a schema-less empty frame would roundtrip
        # through Parquet with no columns at all, and a later read_bars
        # caller selecting e.g. bid_c would hit a column error on data
        # that is perfectly valid -- zero bars is a legitimate result for
        # a PRESENT month, not an absent one, and must carry BAR_SCHEMA's
        # dtypes either way.
        frame = pl.DataFrame(acc, schema=BAR_SCHEMA)
        write_bars(repo_root, instrument, year, month, frame)
        bars_written += len(acc["ts_utc_ns"])
        months_written.append(f"{year:04d}-{month:02d}")
        acc = _new_accumulator()

    completed_utc = _utc_now_iso()
    total_minutes = (end_ns - start_ns) // NS_PER_MINUTE

    return ResampleReport(
        instrument=instrument,
        range_start_ns=start_ns,
        range_end_ns=end_ns,
        months_expected=len(months),
        months_with_data=len(present_months),
        months_missing=len(missing_months),
        missing_months=tuple(f"{y:04d}-{m:02d}" for y, m in missing_months),
        ticks_read=ticks_read,
        bars_written=bars_written,
        minutes_absent=int(total_minutes - bars_written),
        months_written=tuple(months_written),
        started_utc=started_utc,
        completed_utc=completed_utc,
    )
