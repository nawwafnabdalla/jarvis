"""UTC nanosecond <-> IANA local time conversion, with explicit DST fold handling."""

import importlib.metadata
from datetime import date, datetime, time, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jarvis.core.errors import AmbiguousTimeError, UserError
from jarvis.core.types import Nanos

NS_PER_US = 1_000
NS_PER_MS = 1_000_000
NS_PER_SECOND = 1_000_000_000
NS_PER_MINUTE = 60 * NS_PER_SECOND
NS_PER_HOUR = 60 * NS_PER_MINUTE
NS_PER_DAY = 24 * NS_PER_HOUR

FoldPolicy = Literal["raise", "earlier", "later"]

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _get_zone(tz: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz)
    except ZoneInfoNotFoundError as exc:
        raise UserError(f"unknown IANA timezone: {tz!r}") from exc


def _trunc_div(a: int, b: int) -> int:
    """Integer division truncating toward zero (Python's `//` floors instead)."""
    q, r = divmod(a, b)
    if r != 0 and (a < 0) != (b < 0):
        q += 1
    return q


def to_utc_ns(dt: datetime) -> Nanos:
    """Convert a timezone-AWARE datetime to UTC nanoseconds.
    Raises UserError if dt is naive (tzinfo is None). A naive datetime is
    never silently assumed to be UTC — that assumption is exactly how an
    hour goes missing without anyone noticing."""
    if dt.tzinfo is None:
        raise UserError(
            "to_utc_ns: naive datetime rejected; a naive datetime is never "
            "assumed to be UTC"
        )
    dt_utc = dt.astimezone(timezone.utc)
    delta = dt_utc - _EPOCH
    ns = delta.days * NS_PER_DAY + delta.seconds * NS_PER_SECOND + delta.microseconds * NS_PER_US
    return Nanos(ns)


def from_utc_ns(ns: Nanos, tz: str) -> datetime:
    """Convert UTC nanoseconds to a timezone-aware datetime in the named
    IANA zone. Raises UserError for an unknown zone name.

    NOTE ON PRECISION: datetime supports microsecond resolution only.
    Nanosecond components below 1000ns are truncated (not rounded) in the
    returned datetime, toward zero. This is lossy and intentional — the
    datetime is for calendar reasoning, never for round-tripping back to
    Nanos. Callers needing exact ns must retain the original integer."""
    zone = _get_zone(tz)
    total_us = _trunc_div(ns, NS_PER_US)
    return (_EPOCH + timedelta(microseconds=total_us)).astimezone(zone)


def local_wall(ns: Nanos, tz: str) -> tuple[date, time]:
    """Return the local calendar date and wall-clock time in the named zone."""
    dt = from_utc_ns(ns, tz)
    return dt.date(), dt.time()


def is_ambiguous(d: date, t: time, tz: str) -> bool:
    """True if this local wall time occurs twice in the named zone."""
    zone = _get_zone(tz)
    dt0 = datetime.combine(d, t, tzinfo=zone).replace(fold=0)
    dt1 = datetime.combine(d, t, tzinfo=zone).replace(fold=1)
    if dt0.utcoffset() == dt1.utcoffset():
        return False
    # A differing fold offset alone doesn't distinguish "occurs twice" from
    # "occurs zero times" -- a non-existent (skipped) wall time also shows
    # different fold=0/fold=1 offsets. Only a time that actually round-trips
    # (i.e. is not non-existent) is genuinely ambiguous.
    return not is_nonexistent(d, t, tz)


def is_nonexistent(d: date, t: time, tz: str) -> bool:
    """True if this local wall time does not occur in the named zone."""
    zone = _get_zone(tz)
    dt = datetime.combine(d, t, tzinfo=zone)
    dt_utc = dt.astimezone(timezone.utc)
    dt_roundtrip = dt_utc.astimezone(zone)
    return (dt_roundtrip.date(), dt_roundtrip.time()) != (d, t)


def _bisect_offset_boundary(
    lo_ns: int, hi_ns: int, zone: ZoneInfo, target_offset: timedelta
) -> int:
    """Binary search in [lo_ns, hi_ns] for the earliest UTC ns whose offset
    in `zone` equals target_offset. Assumes offset(lo_ns) != target_offset
    and offset(hi_ns) == target_offset; makes no assumption about the size
    of the gap being searched."""
    lo, hi = lo_ns, hi_ns
    while hi - lo > 1:
        mid = (lo + hi) // 2
        mid_dt = (_EPOCH + timedelta(microseconds=_trunc_div(mid, NS_PER_US))).astimezone(zone)
        if mid_dt.utcoffset() == target_offset:
            hi = mid
        else:
            lo = mid
    return hi


def _resolve_nonexistent(d: date, t: time, zone: ZoneInfo, fold_policy: FoldPolicy) -> Nanos:
    dt0 = datetime.combine(d, t, tzinfo=zone).replace(fold=0)  # pre-gap offset interpretation
    dt1 = datetime.combine(d, t, tzinfo=zone).replace(fold=1)  # post-gap offset interpretation
    u0_ns = to_utc_ns(dt0)
    u1_ns = to_utc_ns(dt1)
    offset_after = dt1.utcoffset()

    # For any forward (spring) gap, offset_after > offset_before, so the
    # post-gap interpretation's UTC instant is strictly earlier than the
    # pre-gap interpretation's — u1 < transition <= u0, always.
    lo_ns, hi_ns = min(u0_ns, u1_ns), max(u0_ns, u1_ns)
    transition_ns = _bisect_offset_boundary(lo_ns, hi_ns, zone, offset_after)

    if fold_policy == "later":
        return Nanos(transition_ns)
    return Nanos(transition_ns - 1)  # "earlier": last valid instant strictly before the gap


def local_to_utc_ns(
    d: date,
    t: time,
    tz: str,
    fold_policy: FoldPolicy = "raise",
) -> Nanos:
    """Convert a local wall-clock date+time in the named zone to UTC ns.

    A local wall time may be AMBIGUOUS (occurs twice — autumn fall-back) or
    NON-EXISTENT (skipped — spring forward). Behaviour by policy:

      "raise"   : raise AmbiguousTimeError for either case (DEFAULT)
      "earlier" : ambiguous   -> the first (pre-transition) occurrence
                  non-existent -> the last valid instant BEFORE the gap
      "later"   : ambiguous   -> the second (post-transition) occurrence
                  non-existent -> the first valid instant AFTER the gap

    The default is "raise" so that a caller who has not thought about DST
    gets an error rather than a silently-chosen interpretation."""
    zone = _get_zone(tz)

    if is_ambiguous(d, t, tz):
        if fold_policy == "raise":
            raise AmbiguousTimeError(f"{d} {t} in {tz} is ambiguous (occurs twice)")
        fold = 0 if fold_policy == "earlier" else 1
        dt = datetime.combine(d, t, tzinfo=zone).replace(fold=fold)
        return to_utc_ns(dt)

    if is_nonexistent(d, t, tz):
        if fold_policy == "raise":
            raise AmbiguousTimeError(f"{d} {t} in {tz} does not exist (falls in a DST gap)")
        return _resolve_nonexistent(d, t, zone, fold_policy)

    dt = datetime.combine(d, t, tzinfo=zone)
    return to_utc_ns(dt)


def tzdata_version() -> str:
    """Return the installed tzdata package version (importlib.metadata).
    Recorded in dataset manifests: a tzdata upgrade can change historical
    session boundaries, which must therefore create a new dataset version."""
    return importlib.metadata.version("tzdata")
