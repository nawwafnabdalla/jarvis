"""Feature computation orchestration: resolves dependency order, applies
session_terminal masking uniformly (defence in depth -- independent of
whatever each feature's own compute() function does), assembles the
output FeatureFrame, and writes/reads month-partitioned Parquet with the
same merge semantics as jarvis.bars.store.write_bars (D-045: a sub-range
write must never destroy the rest of an already-written month).
"""

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import polars as pl

from jarvis.core.errors import IntegrityError, UserError
from jarvis.sessions import SessionSet

from jarvis.features.base import REGISTRY, FeatureContext, apply_session_terminal_mask, resolve_order

# Importing the library module populates REGISTRY as a side effect of its
# module-level register() calls -- this import exists purely for that
# effect and is otherwise unused here.
import jarvis.features.library  # noqa: F401

FEATURE_SET_VERSION = 1

_WRITE_PARQUET_KWARGS = {
    "compression": "zstd",
    "compression_level": 3,
    "statistics": True,
    "row_group_size": 1_000_000,
}


@dataclass(frozen=True, slots=True)
class FeatureFrame:
    frame: pl.DataFrame  # ts_utc_ns + one column per requested feature
    feature_set_version: int
    feature_names: tuple[str, ...]
    session_set_id: str
    session_set_version: int
    null_counts: Mapping[str, int]


def compute(names: Sequence[str], bars: pl.DataFrame, session_set: SessionSet) -> FeatureFrame:
    """Compute every feature in `names` (plus their transitive
    dependencies, via resolve_order) over `bars`, in ascending ts_utc_ns.

    Every session_terminal feature's raw output is masked here,
    uniformly, via apply_session_terminal_mask -- this is the mechanical
    enforcement Technical Bible Part F §F.2 describes ("the framework
    enforces it"), applied identically regardless of what any individual
    feature's own compute() function returns. The masked series (not the
    raw one) is what gets stored into `computed` for any feature that
    depends on a session_terminal one, so a downstream feature can never
    see a value that hasn't actually closed yet.

    The output frame carries only the REQUESTED names as columns -- a
    dependency pulled in transitively (e.g. pre_london_high when only
    pre_london_range_pct was requested) is computed and used internally
    but not exposed unless it was itself requested."""
    if bars.height == 0:
        raise UserError("compute: bars frame is empty")

    order = resolve_order(names)

    computed: dict[str, pl.Series] = {}
    for name in order:
        defn = REGISTRY[name]
        ctx = FeatureContext(bars=bars, computed=computed, session_set=session_set, params=defn.params)
        raw = defn.compute(ctx)
        if len(raw) != bars.height:
            raise IntegrityError(
                f"feature {name!r} returned {len(raw)} values for {bars.height} bars"
            )

        if defn.leakage_class == "session_terminal":
            session_name = defn.params.get("session")
            if session_name is None:
                raise UserError(
                    f"feature {name!r} is session_terminal but declares no 'session' param"
                )
            value = apply_session_terminal_mask(raw, bars, session_set, str(session_name))
        else:
            value = raw

        computed[name] = value.cast(defn.dtype).alias(name)

    requested = tuple(names)
    columns: dict[str, pl.Series] = {"ts_utc_ns": bars["ts_utc_ns"]}
    for name in requested:
        columns[name] = computed[name]
    frame = pl.DataFrame(columns)

    null_counts = {name: int(computed[name].null_count()) for name in requested}

    return FeatureFrame(
        frame=frame,
        feature_set_version=FEATURE_SET_VERSION,
        feature_names=requested,
        session_set_id=session_set.definition.session_set_id,
        session_set_version=session_set.definition.version,
        null_counts=null_counts,
    )


def features_path(repo_root: Path, instrument: str, year: int, month: int) -> Path:
    return (
        repo_root
        / "data"
        / "features"
        / f"instrument={instrument}"
        / f"v{FEATURE_SET_VERSION}"
        / f"{year:04d}-{month:02d}.parquet"
    )


def write_features(
    repo_root: Path, instrument: str, year: int, month: int, frame: pl.DataFrame
) -> Path:
    """Write one month of features, atomically, MERGING with any existing
    month file rather than replacing it (D-045, same reasoning and same
    pattern as jarvis.bars.store.write_bars): existing rows and `frame`'s
    rows are concatenated, deduplicated on ts_utc_ns keeping `frame`'s row
    for any collision (a recomputed bar's features must win), and sorted
    ascending. Unlike bars, there is no derived per-row field like
    prev_gap_ns that depends on row order, so no post-merge recomputation
    is needed beyond the sort itself."""
    path = features_path(repo_root, instrument, year, month)
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.is_file():
        existing = pl.read_parquet(path)
        combined = pl.concat([existing, frame])
    else:
        combined = frame

    merged = combined.unique(subset=["ts_utc_ns"], keep="last", maintain_order=True).sort(
        "ts_utc_ns"
    )

    tmp_path = path.with_name(path.name + ".tmp")
    try:
        merged.write_parquet(tmp_path, **_WRITE_PARQUET_KWARGS)
        os.replace(tmp_path, path)
    except OSError as exc:
        tmp_path.unlink(missing_ok=True)
        raise IntegrityError(f"failed to write features parquet {path}: {exc}") from exc
    return path
