"""Dukascopy fetch, decompress, parse, normalise to tick Parquet.

Also HistData.com CSV ingest (jarvis.ingest.histdata /
jarvis.ingest.histdata_import) -- the research dataset's bulk acquisition
route since WP-009. The Dukascopy fetcher above is retained for
incremental forward-testing later; it is not replaced."""

from jarvis.ingest.fetch import IngestReport, RawBlob, RawStatus, fetch_hour, ingest_range
from jarvis.ingest.fetch_log import (
    FETCH_LOG_SCHEMA_VERSION,
    FetchLogEntry,
    fetch_log_path,
    merge_fetch_log,
    read_fetch_log,
)
from jarvis.ingest.histdata import (
    HistDataMonth,
    StampClock,
    detect_stamp_clock,
    month_has_divergent_dates,
    parse_histdata_csv,
)
from jarvis.ingest.histdata_import import (
    TICK_SCHEMA,
    ImportReport,
    import_histdata,
    import_log_path,
    parse_gap_report,
    read_import_log,
    tick_path,
    write_import_log,
    write_ticks,
)
from jarvis.ingest.parse import ParsedHour, Tick, TickArrays, parse_bi5, parse_bi5_arrays
from jarvis.ingest.urls import dukascopy_url, raw_blob_path

__all__ = [
    "FETCH_LOG_SCHEMA_VERSION",
    "TICK_SCHEMA",
    "FetchLogEntry",
    "HistDataMonth",
    "ImportReport",
    "IngestReport",
    "ParsedHour",
    "RawBlob",
    "RawStatus",
    "Tick",
    "TickArrays",
    "StampClock",
    "detect_stamp_clock",
    "month_has_divergent_dates",
    "dukascopy_url",
    "fetch_hour",
    "fetch_log_path",
    "import_histdata",
    "import_log_path",
    "ingest_range",
    "merge_fetch_log",
    "parse_bi5",
    "parse_bi5_arrays",
    "parse_gap_report",
    "parse_histdata_csv",
    "raw_blob_path",
    "read_fetch_log",
    "read_import_log",
    "tick_path",
    "write_import_log",
    "write_ticks",
]
