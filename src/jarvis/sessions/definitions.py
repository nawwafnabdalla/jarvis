"""Versioned session-set schema, YAML loading, and validation."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, time, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml

from jarvis.core.config import repo_root
from jarvis.core.errors import SessionError, UserError
from jarvis.core.types import Nanos
from jarvis.timeengine import (
    NS_PER_DAY,
    FoldPolicy,
    is_ambiguous,
    is_nonexistent,
    local_to_utc_ns,
    trading_day_bounds,
)

_FIRST_SAMPLE_YEAR = 2007
_LAST_SAMPLE_YEAR = 2026


@dataclass(frozen=True, slots=True)
class SessionDef:
    name: str
    tz: str | None  # None for derived sessions
    start: time | None
    end: time | None
    derived: Literal["intersection"] | None
    of: tuple[str, ...]  # empty for concrete sessions
    note: str | None


@dataclass(frozen=True, slots=True)
class FoldPolicySpec:
    ambiguous: FoldPolicy
    nonexistent: FoldPolicy


@dataclass(frozen=True, slots=True)
class SessionSetDef:
    session_set_id: str
    version: int
    tzdata_version_at_authoring: str
    fold_policy: FoldPolicySpec
    exclude_partial: bool
    thin_day_threshold: float
    sessions: Mapping[str, SessionDef]


def resolve_boundary_ns(d: date, t: time, tz: str, fold_spec: FoldPolicySpec) -> Nanos:
    """Resolve one local wall-clock boundary to UTC ns, applying whichever
    of fold_spec's two policy values actually applies to this instant. Never
    passes "raise" through to local_to_utc_ns for an ambiguous/non-existent
    instant -- a session boundary must resolve, not crash."""
    if is_ambiguous(d, t, tz):
        return local_to_utc_ns(d, t, tz, fold_spec.ambiguous)
    if is_nonexistent(d, t, tz):
        return local_to_utc_ns(d, t, tz, fold_spec.nonexistent)
    return local_to_utc_ns(d, t, tz, "later")


def _parse_hhmm(raw: object, *, field: str, session_name: str) -> time:
    if not isinstance(raw, str):
        raise SessionError(f"session {session_name!r}: {field} must be a string like '08:00'")
    try:
        hh, mm = raw.split(":")
        return time(int(hh), int(mm))
    except (ValueError, TypeError) as exc:
        raise SessionError(
            f"session {session_name!r}: {field} {raw!r} is not a valid HH:MM time"
        ) from exc


def _validate_timezone(tz: str, session_name: str) -> None:
    """The only way to validate a zone name without touching zoneinfo
    directly is to ask timeengine to resolve something in it; an unknown
    zone surfaces as UserError from timeengine, which we translate to
    SessionError so a mis-specified session fails at config load under this
    module's own error type."""
    try:
        local_to_utc_ns(date(2023, 1, 2), time(12, 0), tz, "later")
    except UserError as exc:
        raise SessionError(f"session {session_name!r}: unknown timezone {tz!r}") from exc


def _month_start_samples(start_year: int, end_year: int) -> list[date]:
    return [date(y, m, 1) for y in range(start_year, end_year + 1) for m in range(1, 13)]


@lru_cache(maxsize=None)
def _find_transition_dates(tz: str, start_year: int, end_year: int) -> tuple[date, ...]:
    """Every date in [start_year, end_year] whose local midnight-to-midnight
    duration in `tz` is not exactly 24h. Uses only timeengine.local_to_utc_ns
    -- no direct zoneinfo access, no hardcoded transition rule or date.

    Cached: a full-range daily scan costs a noticeable fraction of a second
    per zone, and this is a pure function of its arguments (the installed
    tzdata version does not change within a process), so repeated calls
    with the same arguments -- routine when load_session_set_def is called
    many times across a test run -- would otherwise redo the same scan."""
    transitions: list[date] = []
    current = date(start_year, 1, 1)
    end = date(end_year, 12, 31)
    one_day = timedelta(days=1)

    prev_ns = local_to_utc_ns(current, time(0, 0), tz, "later")
    current += one_day
    while current <= end:
        midnight_ns = local_to_utc_ns(current, time(0, 0), tz, "later")
        if midnight_ns - prev_ns != NS_PER_DAY:
            transitions.append(current)
        prev_ns = midnight_ns
        current += one_day

    return tuple(transitions)


def _validate_anchoring(
    session_name: str, session: SessionDef, fold_spec: FoldPolicySpec, sample_days: list[date]
) -> None:
    for d in sample_days:
        start_ns = resolve_boundary_ns(d, session.start, session.tz, fold_spec)
        end_ns = resolve_boundary_ns(d, session.end, session.tz, fold_spec)
        bounds_start, bounds_end = trading_day_bounds(d)
        if not (bounds_start <= start_ns and end_ns <= bounds_end):
            raise SessionError(
                f"session {session_name!r}: window on trading day {d} "
                f"[{start_ns}, {end_ns}) falls outside trading_day_bounds "
                f"[{bounds_start}, {bounds_end}) -- this session's local "
                "anchoring is not compatible with the 17:00 NY rollover"
            )


def _parse_session(name: str, raw: Mapping) -> SessionDef:
    is_derived = "derived" in raw
    has_concrete_fields = any(k in raw for k in ("tz", "start", "end"))

    if is_derived and has_concrete_fields:
        raise SessionError(
            f"session {name!r}: 'derived' and concrete fields (tz/start/end) "
            "are mutually exclusive"
        )

    if is_derived:
        derived = raw["derived"]
        if derived != "intersection":
            raise SessionError(
                f"session {name!r}: unsupported derived type {derived!r}; "
                "only 'intersection' is supported"
            )
        of_raw = raw.get("of")
        if not isinstance(of_raw, list) or len(of_raw) < 2:
            raise SessionError(
                f"session {name!r}: derived session requires 'of' naming at "
                "least 2 sessions"
            )
        return SessionDef(
            name=name,
            tz=None,
            start=None,
            end=None,
            derived="intersection",
            of=tuple(of_raw),
            note=raw.get("note"),
        )

    if not has_concrete_fields or not all(k in raw for k in ("tz", "start", "end")):
        raise SessionError(
            f"session {name!r}: a concrete session requires tz, start, and end"
        )

    tz = raw["tz"]
    if not isinstance(tz, str):
        raise SessionError(f"session {name!r}: tz must be a string")
    _validate_timezone(tz, name)

    start = _parse_hhmm(raw["start"], field="start", session_name=name)
    end = _parse_hhmm(raw["end"], field="end", session_name=name)
    if end <= start:
        raise SessionError(
            f"session {name!r}: end ({end}) must be after start ({start}); "
            "midnight-crossing sessions are not supported"
        )

    return SessionDef(
        name=name, tz=tz, start=start, end=end, derived=None, of=(), note=raw.get("note")
    )


def _validate_derived_references(sessions: Mapping[str, SessionDef]) -> None:
    for name, session in sessions.items():
        if session.derived is None:
            continue
        for parent_name in session.of:
            parent = sessions.get(parent_name)
            if parent is None:
                raise SessionError(
                    f"session {name!r}: 'of' names unknown session {parent_name!r}"
                )
            if parent.derived is not None:
                raise SessionError(
                    f"session {name!r}: 'of' names {parent_name!r}, which is "
                    "itself derived -- nested derived sessions are not "
                    "supported in V1 (one level only)"
                )


def load_session_set_def(session_set_id: str, version: int) -> SessionSetDef:
    """Load config/sessions/{id}.v{version}.yaml, validate, and return.
    Raises SessionError on any validation failure (see below)."""
    path = repo_root() / "config" / "sessions" / f"{session_set_id}.v{version}.yaml"
    if not path.is_file():
        raise SessionError(f"session set file not found: {path}")

    stat = path.stat()
    return _load_session_set_def_cached(session_set_id, version, path, stat.st_mtime_ns, stat.st_size)


@lru_cache(maxsize=None)
def _load_session_set_def_cached(
    session_set_id: str, version: int, path: Path, mtime_ns: int, size: int
) -> SessionSetDef:
    """Cache key includes the file's mtime and size, not just
    (session_set_id, version): different test cases routinely reuse the
    same logical id/version against different tmp-path files (or the same
    path with rewritten content), and a cache keyed on id/version alone
    would silently serve a stale result from an earlier call -- a
    validation guard that appears to run but never actually does, the same
    failure class WP-001-CORRECTION fixed for worker-thread exceptions."""
    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)

    if not isinstance(raw, dict):
        raise SessionError(f"{path}: does not contain a mapping at top level")

    if raw.get("session_set_id") != session_set_id:
        raise SessionError(
            f"{path}: session_set_id {raw.get('session_set_id')!r} does not "
            f"match requested {session_set_id!r}"
        )
    if raw.get("version") != version:
        raise SessionError(
            f"{path}: version {raw.get('version')!r} does not match requested {version!r}"
        )

    fold_raw = raw.get("fold_policy")
    if not isinstance(fold_raw, dict) or "ambiguous" not in fold_raw or "nonexistent" not in fold_raw:
        raise SessionError(f"{path}: fold_policy must specify 'ambiguous' and 'nonexistent'")
    if fold_raw["ambiguous"] == "raise" or fold_raw["nonexistent"] == "raise":
        raise SessionError(
            f"{path}: fold_policy must not be 'raise' -- a session boundary "
            "must resolve, not crash"
        )
    fold_policy = FoldPolicySpec(ambiguous=fold_raw["ambiguous"], nonexistent=fold_raw["nonexistent"])

    thin_day_threshold = raw.get("thin_day_threshold")
    if not isinstance(thin_day_threshold, (int, float)) or not (0 < thin_day_threshold <= 1):
        raise SessionError(f"{path}: thin_day_threshold must be in (0, 1], got {thin_day_threshold!r}")

    sessions_raw = raw.get("sessions")
    if not isinstance(sessions_raw, dict) or not sessions_raw:
        raise SessionError(f"{path}: sessions must be a non-empty mapping")

    sessions = {name: _parse_session(name, body) for name, body in sessions_raw.items()}
    _validate_derived_references(sessions)

    month_samples = _month_start_samples(_FIRST_SAMPLE_YEAR, _LAST_SAMPLE_YEAR)
    transition_cache: dict[str, list[date]] = {}
    for name, session in sessions.items():
        if session.derived is not None:
            continue
        if session.tz not in transition_cache:
            transition_cache[session.tz] = _find_transition_dates(
                session.tz, _FIRST_SAMPLE_YEAR, _LAST_SAMPLE_YEAR
            )
        sample_days = sorted(set(month_samples) | set(transition_cache[session.tz]))
        _validate_anchoring(name, session, fold_policy, sample_days)

    return SessionSetDef(
        session_set_id=session_set_id,
        version=version,
        tzdata_version_at_authoring=str(raw.get("tzdata_version_at_authoring", "")),
        fold_policy=fold_policy,
        exclude_partial=bool(raw.get("exclude_partial", False)),
        thin_day_threshold=float(thin_day_threshold),
        sessions=sessions,
    )


load_session_set_def.cache_clear = _load_session_set_def_cached.cache_clear
