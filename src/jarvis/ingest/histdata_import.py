"""HistData.com bulk import orchestration: source discovery (a zip of
monthly zips, a directory of monthly zips, or a directory of already-
extracted CSVs), tick Parquet writing with merge semantics, and per-month
provenance logging.

Writes into the tick Parquet layer at
data/tick/instrument={instrument}/year={YYYY}/month={MM}/data.parquet,
using the column names/dtypes jarvis.ingest.parse.parse_bi5_arrays'
TickArrays establishes (ts_utc_ns, bid, ask, bid_volume, ask_volume) PLUS
one column TickArrays does not carry: row_sequence (WP-009g / D-058). The
two schemas are no longer byte-identical, and that is deliberate, not
drift: TickArrays is an ephemeral in-memory array that never persists to
Parquet (resample_range reads .bi5 blobs directly -- see the note below),
so its own tie-breaker (Tick.seq, jarvis.ingest.parse) never needs to
survive a round trip and is never stored. write_ticks DOES persist to
Parquet and merges across repeated calls, so its tie-breaker must be an
actual column or it cannot survive being written, read back, and merged
again. See write_ticks for why one is needed at all.

NOTE ON DOWNSTREAM WIRING (flagged, not silently worked around): as of
this package, jarvis.bars.resample_range reads raw .bi5 blobs directly
via jarvis.ingest.urls.raw_blob_path / jarvis.ingest.parse.parse_bi5_arrays
-- it does not read from data/tick/ at all. bars/resample.py is on this
package's forbidden-file list, so this package cannot wire the two
together; a follow-up change to bars/resample.py is required before
resample_range will actually consume HistData-imported ticks. See this
package's closing notes.
"""

import io
import json
import os
import re
import tempfile
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import polars as pl

from jarvis.core.errors import IntegrityError, UserError
from jarvis.core.hashing import canonical_json
from jarvis.core.types import Nanos

from jarvis.ingest.histdata import HistDataMonth, parse_histdata_csv

TICK_SCHEMA: dict[str, pl.DataType] = {
    "ts_utc_ns": pl.Int64,
    "bid": pl.Float64,
    "ask": pl.Float64,
    "bid_volume": pl.Float64,
    "ask_volume": pl.Float64,
    # WP-009g / D-058: the tick's 0-indexed position within its SOURCE
    # FILE for the month, i.e. exactly what jarvis.ingest.parse.Tick.seq
    # already means for the Dukascopy path -- an honest record of file
    # order, never a timestamp and never implying precision finer than
    # ts_utc_ns's real millisecond resolution. See write_ticks.
    "row_sequence": pl.Int64,
}

# A tick file predating WP-009g has no row_sequence column at all. Merging
# it against a fresh frame would either crash on a polars schema mismatch
# or (worse) silently coerce nulls into the compound dedup key below --
# see write_ticks's guard.
_LEGACY_SCHEMA_ERROR = (
    "predates the row_sequence tiebreaker (WP-009g / D-058) and cannot be "
    "safely merged against -- data written under the OLD ts_utc_ns-only dedup "
    "key may already have silently discarded genuine same-millisecond quotes "
    "it has no way to recover now. Delete this file and re-import the month "
    "from scratch rather than merging fresh data onto data that might "
    "already be lossy."
)

_WRITE_PARQUET_KWARGS = {
    "compression": "zstd",
    "compression_level": 3,
    "statistics": True,
    "row_group_size": 1_000_000,
}

_CSV_NAME_RE = re.compile(r"^DAT_ASCII_(?P<instrument>[A-Z]+)_T_(?P<year>\d{4})(?P<month>\d{2})\.csv$")
_ZIP_NAME_RE = re.compile(
    r"^HISTDATA_COM_ASCII_(?P<instrument>[A-Z]+)_T(?P<year>\d{4})(?P<month>\d{2})\.zip$"
)
_GAP_LINE_RE = re.compile(r"Gap of \d+s found between")


@dataclass(frozen=True, slots=True)
class ImportReport:
    instrument: str
    months_found: int
    months_imported: int
    months_skipped: tuple[str, ...]
    total_ticks: int
    stamp_clocks: Mapping[str, str]  # "2017-05" -> "us_dst" | "not_required"
    gap_reports: Mapping[str, int]  # month -> gaps declared in the .txt
    range_start_ns: Nanos
    range_end_ns: Nanos
    started_utc: str
    completed_utc: str


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def tick_path(repo_root: Path, instrument: str, year: int, month: int) -> Path:
    return (
        repo_root
        / "data"
        / "tick"
        / f"instrument={instrument}"
        / f"year={year:04d}"
        / f"month={month:02d}"
        / "data.parquet"
    )


def import_log_path(repo_root: Path, instrument: str, year: int, month: int) -> Path:
    return (
        repo_root
        / "data"
        / "tick"
        / f"instrument={instrument}"
        / "_import_log"
        / f"{year:04d}-{month:02d}.json"
    )


def _month_key(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def write_ticks(repo_root: Path, instrument: str, year: int, month: int, frame: pl.DataFrame) -> Path:
    """Write one month of ticks, atomically, MERGING with any existing
    month file rather than replacing it wholesale (the same merge-not-
    replace shape D-045 established for write_bars, in a genuinely
    different function in a different module -- see this function's
    history note below for why that distinction matters).

    THE DEDUP KEY IS (ts_utc_ns, row_sequence), NOT ts_utc_ns ALONE
    (WP-009g / D-058). A HistData millisecond stamp is not always unique:
    WP-009f measured up to 38% of a month's rows genuinely sharing a
    stamp with a DIFFERENT quote in specific 2006-2011 windows (unrelated
    to volatility, confirmed independent of the DST-calendar and column-
    order regimes), and confirmed the mechanism is a plain integer
    collision on millisecond-resolution source stamps -- not a permanent
    property of the market. Deduping on ts_utc_ns alone (the ORIGINAL
    version of this function) silently discarded every genuine quote but
    the last at each colliding stamp. row_sequence is that tick's real,
    honest position in its source file -- not a fabricated timestamp, and
    it never implies precision finer than ts_utc_ns's real millisecond
    resolution. WP-009f confirmed file order behaves as a real
    chronological signal (within-group and between-group price-step
    statistics were indistinguishable), so this recovers real information
    rather than inventing an arbitrary tiebreak.

    A TRUE full duplicate -- identical ts_utc_ns AND bid AND ask, a
    literally repeated row rather than a distinct quote -- is still
    collapsed to one row, exactly as before: it carries no information
    the compound key would need to preserve. Only genuinely DIFFERING
    same-millisecond rows are now kept as separate rows. On a re-import
    of the same month, the newer frame's row still wins at any shared
    (ts_utc_ns, row_sequence) identity, preserving the original "a
    re-import must win" guarantee.

    Raises IntegrityError if an existing on-disk file predates
    row_sequence (see _LEGACY_SCHEMA_ERROR) -- merging fresh, honest data
    onto data written under the lossy old key must never happen silently;
    the file must be rebuilt from source, not patched.

    HISTORY: write_ticks and write_bars (jarvis.bars.store) share this
    merge-then-dedup-then-sort SHAPE because write_ticks was written by
    following write_bars' established pattern -- but they are separate
    functions in separate modules, storing structurally different things.
    D-045 fixed write_bars' replace-instead-of-merge defect; it never
    reviewed, and says nothing about, EITHER function's dedup KEY choice.
    For bars, keying on ts_utc_ns is simply correct: a 1-minute bar has
    exactly one instance per minute by construction. For raw ticks that
    invariant never held -- multiple genuinely distinct quotes can share
    a millisecond -- and nothing caught it because write_ticks's dedup
    key had never been independently reviewed until WP-009f found the
    gap it was causing. Do not cite D-045 as having examined this key."""
    path = tick_path(repo_root, instrument, year, month)
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.is_file():
        existing = pl.read_parquet(path)
        if "row_sequence" not in existing.columns:
            raise IntegrityError(f"{path}: {_LEGACY_SCHEMA_ERROR}")
        combined = pl.concat([existing, frame])
    else:
        combined = frame

    # Step 1: collapse TRUE full duplicates -- identical ts_utc_ns, bid,
    # AND ask -- to a single row. These carry zero additional information
    # regardless of how many times they repeat or where, so this loses
    # nothing; it is exactly what "as today" meant for this case. Sorting
    # by row_sequence first makes keep="last" deterministically prefer
    # the highest row_sequence at each (ts_utc_ns, bid, ask) triple --
    # the newer import, on a re-import of the same month.
    combined = combined.sort("row_sequence")
    combined = combined.unique(subset=["ts_utc_ns", "bid", "ask"], keep="last", maintain_order=True)

    # Step 2: resolve the real identity key. After step 1, two rows can
    # still legitimately share ts_utc_ns (a genuine same-millisecond
    # collision with a different quote) -- that is no longer an error to
    # collapse away. row_sequence disambiguates them, and is ALSO what
    # lets a re-import of the same month replace stale existing rows at
    # matching (ts_utc_ns, row_sequence) identity, keeping "a re-import
    # must win" intact.
    merged = combined.unique(
        subset=["ts_utc_ns", "row_sequence"], keep="last", maintain_order=True
    ).sort(["ts_utc_ns", "row_sequence"])

    tmp_path = path.with_name(path.name + ".tmp")
    try:
        merged.write_parquet(tmp_path, **_WRITE_PARQUET_KWARGS)
        os.replace(tmp_path, path)
    except OSError as exc:
        tmp_path.unlink(missing_ok=True)
        raise IntegrityError(f"failed to write tick parquet {path}: {exc}") from exc
    return path


def _histdata_month_to_frame(hist_month: HistDataMonth) -> pl.DataFrame:
    n = hist_month.row_count
    return pl.DataFrame(
        {
            "ts_utc_ns": hist_month.ts_utc_ns,
            "bid": hist_month.bid,
            "ask": hist_month.ask,
            "bid_volume": [None] * n,
            "ask_volume": [None] * n,
            # 0-indexed position in THIS parse of the source file.
            # parse_histdata_csv never reorders rows (WP-009's own
            # invariant), so index i here IS row i of the CSV -- an
            # honest record of file order, not a guess and not a
            # timestamp. See write_ticks (WP-009g / D-058).
            "row_sequence": range(n),
        },
        schema=TICK_SCHEMA,
    )


def write_import_log(
    repo_root: Path,
    instrument: str,
    hist_month: HistDataMonth,
    *,
    declared_gaps: int,
) -> Path:
    """Records which daylight-saving calendar this month's stamps were
    read as following, and how its column order was resolved, and why.

    `stamp_clock` is `"us_dst"`, `"eu_dst"`, or null. Null is a normal
    outcome, not a gap in the record: it means the file has no ticks on
    any date where the US and EU calendars disagree, so both clocks assign
    the same UTC instant to every stamp in it and no determination was
    needed. `stamp_clock_determination` says which of those happened, so
    a reader never has to infer it from a null.

    `column_order` is `"bid_ask"`, `"ask_bid"`, or `"mixed_per_day"` (the
    file switches order once, cleanly, at a weekend boundary -- D-055h /
    2009-05's shape; every column-order value now traces to this file's
    own content, since the convention-carrying hint D-055c removed never
    applied to column order in the first place). `column_order_determination`
    says whether the whole-file >=99%/<=1% check alone was decisive, or
    whether the per-day fallback was needed to resolve it."""
    path = import_log_path(repo_root, instrument, hist_month.year, hist_month.month)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "instrument": instrument,
        "year": hist_month.year,
        "month": hist_month.month,
        "source_filename": hist_month.source_path.name,
        "source_sha256": hist_month.source_sha256,
        "stamp_clock": hist_month.stamp_clock,
        "stamp_clock_evidence": hist_month.stamp_clock_evidence,
        "stamp_clock_determination": (
            "detected_from_file_content"
            if hist_month.stamp_clock is not None
            else "not_required_both_clocks_agree"
        ),
        "column_order": hist_month.column_order,
        "column_order_evidence": hist_month.column_order_evidence,
        "column_order_determination": (
            "detected_per_day_at_weekend_boundary"
            if hist_month.column_order == "mixed_per_day"
            else "detected_whole_file"
        ),
        "row_count": hist_month.row_count,
        "declared_gaps": declared_gaps,
        "recorded_utc": _utc_now_iso(),
    }
    tmp_path = path.with_name(path.name + ".tmp")
    try:
        tmp_path.write_text(canonical_json(record), encoding="utf-8")
        os.replace(tmp_path, path)
    except OSError as exc:
        tmp_path.unlink(missing_ok=True)
        raise IntegrityError(f"failed to write import log {path}: {exc}") from exc
    return path


def read_import_log(repo_root: Path, instrument: str, year: int, month: int) -> dict | None:
    path = import_log_path(repo_root, instrument, year, month)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IntegrityError(f"import log {path} is malformed: {exc}") from exc


def parse_gap_report(text: str) -> int:
    """Count HistData's own declared gaps from a .txt status report --
    each is one line of the form 'Gap of {N}s found between {a} and {b}.'
    There is no summary total line in HistData's own format; the count IS
    the number of matching lines."""
    return len(_GAP_LINE_RE.findall(text))


# ---------------------------------------------------------------------------
# Source discovery
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _MonthCandidate:
    """A month discovered from filenames alone -- no extraction has
    happened yet. Exactly one of csv_path / standalone_zip_path /
    (outer_zip_path, outer_zip_member) is set. Kept deliberately cheap:
    discovering all 196 months of a real source must not cost more than
    reading directory/zip-index listings, so date-range and already-
    imported filtering can happen BEFORE anything is extracted."""

    instrument: str
    year: int
    month: int
    csv_path: Path | None = None
    standalone_zip_path: Path | None = None
    outer_zip_path: Path | None = None
    outer_zip_member: str | None = None


@dataclass(frozen=True, slots=True)
class _MonthSource:
    instrument: str
    year: int
    month: int
    csv_path: Path
    gap_text: str | None


def _candidates_from_directory(directory: Path, instrument_filter: str | None) -> list[_MonthCandidate]:
    """A directory of already-extracted CSVs (with optional sibling .txt
    files)."""
    candidates = []
    for csv_path in sorted(directory.glob("DAT_ASCII_*_T_*.csv")):
        m = _CSV_NAME_RE.match(csv_path.name)
        if not m:
            continue
        instrument = m.group("instrument")
        if instrument_filter is not None and instrument != instrument_filter:
            continue
        candidates.append(
            _MonthCandidate(
                instrument=instrument,
                year=int(m.group("year")),
                month=int(m.group("month")),
                csv_path=csv_path,
            )
        )
    return candidates


def _candidates_from_zip_directory(directory: Path, instrument_filter: str | None) -> list[_MonthCandidate]:
    candidates = []
    for zip_path in sorted(directory.glob("HISTDATA_COM_ASCII_*.zip")):
        m = _ZIP_NAME_RE.match(zip_path.name)
        if not m:
            continue
        instrument = m.group("instrument")
        if instrument_filter is not None and instrument != instrument_filter:
            continue
        candidates.append(
            _MonthCandidate(
                instrument=instrument,
                year=int(m.group("year")),
                month=int(m.group("month")),
                standalone_zip_path=zip_path,
            )
        )
    return candidates


def _candidates_from_outer_zip(outer_zip_path: Path, instrument_filter: str | None) -> list[_MonthCandidate]:
    """Reads only the outer zip's directory listing (namelist()) -- does
    not decompress any member."""
    candidates = []
    with zipfile.ZipFile(outer_zip_path) as outer:
        for name in sorted(outer.namelist()):
            m = _ZIP_NAME_RE.match(Path(name).name)
            if not m:
                continue
            instrument = m.group("instrument")
            if instrument_filter is not None and instrument != instrument_filter:
                continue
            candidates.append(
                _MonthCandidate(
                    instrument=instrument,
                    year=int(m.group("year")),
                    month=int(m.group("month")),
                    outer_zip_path=outer_zip_path,
                    outer_zip_member=name,
                )
            )
    if not candidates:
        raise UserError(f"{outer_zip_path}: no monthly HISTDATA_COM_ASCII_*.zip entries found inside")
    return candidates


def _discover_candidates(source: Path, instrument_filter: str | None) -> list[_MonthCandidate]:
    """Detect one of: a single zip containing monthly zips, a directory
    of monthly zips, or a directory of already-extracted CSVs."""
    if source.is_file() and source.suffix.lower() == ".zip":
        return _candidates_from_outer_zip(source, instrument_filter)
    if source.is_dir():
        zip_candidates = _candidates_from_zip_directory(source, instrument_filter)
        if zip_candidates:
            return zip_candidates
        return _candidates_from_directory(source, instrument_filter)
    raise UserError(f"{source}: not a zip file or directory")


def _materialize(candidate: _MonthCandidate, work_dir: Path) -> _MonthSource:
    """Extract (if needed) and return the CSV path + gap-report text for
    one month. Only called for months that survive date-range/force/
    already-imported filtering -- this is where the actual decompression
    cost is paid, deliberately deferred until here."""
    if candidate.csv_path is not None:
        csv_path = candidate.csv_path
        txt_path = csv_path.with_suffix(".txt")
        gap_text = txt_path.read_text(encoding="utf-8", errors="replace") if txt_path.is_file() else None
        return _MonthSource(candidate.instrument, candidate.year, candidate.month, csv_path, gap_text)

    month_dir = work_dir / f"{candidate.year:04d}-{candidate.month:02d}"
    month_dir.mkdir(parents=True, exist_ok=True)

    if candidate.standalone_zip_path is not None:
        source_desc = candidate.standalone_zip_path
        with zipfile.ZipFile(candidate.standalone_zip_path) as zf:
            zf.extractall(month_dir)
    else:
        if candidate.outer_zip_path is None or candidate.outer_zip_member is None:
            raise IntegrityError(f"internal error: incomplete month candidate for {candidate.year:04d}-{candidate.month:02d}")
        source_desc = candidate.outer_zip_path
        with zipfile.ZipFile(candidate.outer_zip_path) as outer, outer.open(candidate.outer_zip_member) as member:
            inner_bytes = member.read()
        with zipfile.ZipFile(io.BytesIO(inner_bytes)) as inner:
            inner.extractall(month_dir)

    csv_path = month_dir / f"DAT_ASCII_{candidate.instrument}_T_{candidate.year:04d}{candidate.month:02d}.csv"
    txt_path = month_dir / f"DAT_ASCII_{candidate.instrument}_T_{candidate.year:04d}{candidate.month:02d}.txt"
    if not csv_path.is_file():
        raise IntegrityError(f"{source_desc}: expected {csv_path.name} not found after extraction")
    gap_text = txt_path.read_text(encoding="utf-8", errors="replace") if txt_path.is_file() else None
    return _MonthSource(candidate.instrument, candidate.year, candidate.month, csv_path, gap_text)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def import_histdata(
    repo_root: Path,
    source_dir: Path,
    instrument: str,
    *,
    start: tuple[int, int] | None = None,
    end: tuple[int, int] | None = None,
    force: bool = False,
) -> ImportReport:
    """Import every HistData monthly CSV found under `source_dir` for
    `instrument` into the tick Parquet layer, month by month.

    `start`/`end` are inclusive (year, month) bounds; a month outside
    them is skipped. `force` re-imports a month even if
    data/tick/.../data.parquet already exists for it (merge semantics
    still apply -- this is about whether to redo the work, not about
    overwrite safety, which write_ticks always provides)."""
    started_utc = _utc_now_iso()

    with tempfile.TemporaryDirectory(prefix="histdata_import_") as tmp:
        work_dir = Path(tmp)
        candidates = _discover_candidates(source_dir, instrument)
        candidates.sort(key=lambda c: (c.year, c.month))

        months_found = len(candidates)
        months_imported = 0
        months_skipped: list[str] = []
        total_ticks = 0
        stamp_clocks: dict[str, str] = {}
        gap_reports: dict[str, int] = {}
        range_start_ns: int | None = None
        range_end_ns: int | None = None

        # NOTE: no convention is ever carried between months. An earlier
        # version of this loop threaded the most recently verified
        # convention forward as a hint, because detection was blind on
        # months with no EDT Friday (most Decembers, Januaries and
        # Februaries) and would otherwise hard-fail on them. That crutch
        # is gone, and deliberately so: those months are blind precisely
        # because BOTH clocks convert them identically, so there is
        # nothing there to be wrong about and nothing to inherit. Only
        # March, October and November files contain a date where the two
        # calendars disagree, and every one of those carries its own
        # weekend-boundary evidence. Carrying a convention across the
        # 2018/2019 changeover is exactly how that changeover stayed
        # hidden, so a month now either proves its own clock or does not
        # need one.

        for candidate in candidates:
            key = _month_key(candidate.year, candidate.month)
            if start is not None and (candidate.year, candidate.month) < start:
                months_skipped.append(key)
                continue
            if end is not None and (candidate.year, candidate.month) > end:
                months_skipped.append(key)
                continue

            existing_path = tick_path(repo_root, instrument, candidate.year, candidate.month)
            if existing_path.is_file() and not force:
                months_skipped.append(key)
                continue

            # Extraction (if any) happens here, deferred until this month
            # has survived every cheap filter above.
            src = _materialize(candidate, work_dir)
            hist_month = parse_histdata_csv(src.csv_path, instrument, src.year, src.month)

            frame = _histdata_month_to_frame(hist_month)
            write_ticks(repo_root, instrument, src.year, src.month, frame)

            declared_gaps = parse_gap_report(src.gap_text) if src.gap_text is not None else 0
            write_import_log(
                repo_root,
                instrument,
                hist_month,
                declared_gaps=declared_gaps,
            )

            months_imported += 1
            total_ticks += hist_month.row_count
            stamp_clocks[key] = (
                hist_month.stamp_clock
                if hist_month.stamp_clock is not None
                else "not_required"
            )
            gap_reports[key] = declared_gaps

            month_start_ns = int(hist_month.ts_utc_ns[0])
            month_end_ns = int(hist_month.ts_utc_ns[-1]) + 1
            range_start_ns = month_start_ns if range_start_ns is None else min(range_start_ns, month_start_ns)
            range_end_ns = month_end_ns if range_end_ns is None else max(range_end_ns, month_end_ns)

    completed_utc = _utc_now_iso()

    return ImportReport(
        instrument=instrument,
        months_found=months_found,
        months_imported=months_imported,
        months_skipped=tuple(months_skipped),
        total_ticks=total_ticks,
        stamp_clocks=stamp_clocks,
        gap_reports=gap_reports,
        range_start_ns=Nanos(range_start_ns if range_start_ns is not None else 0),
        range_end_ns=Nanos(range_end_ns if range_end_ns is not None else 0),
        started_utc=started_utc,
        completed_utc=completed_utc,
    )
