"""Dukascopy fetch, decompress, parse, normalise to tick Parquet."""

from jarvis.ingest.fetch import IngestReport, RawBlob, fetch_hour, ingest_range
from jarvis.ingest.urls import dukascopy_url, raw_blob_path

__all__ = [
    "IngestReport",
    "RawBlob",
    "dukascopy_url",
    "fetch_hour",
    "ingest_range",
    "raw_blob_path",
]
