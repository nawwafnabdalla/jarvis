"""Trading-day and trading-week assignment against the 17:00 America/New_York rollover."""

from dataclasses import dataclass
from datetime import date, time, timedelta

from jarvis.core.types import Nanos
from jarvis.timeengine.convert import from_utc_ns, local_to_utc_ns

TRADING_DAY_ROLLOVER_HOUR = 17  # 17:00 America/New_York
TRADING_DAY_TZ = "America/New_York"

_ROLLOVER_TIME = time(TRADING_DAY_ROLLOVER_HOUR, 0, 0)


def trading_day(ns: Nanos) -> date:
    """The trading day containing this instant.

    local = from_utc_ns(ns, "America/New_York")
    if local.time() >= 17:00 -> local.date() + 1 day
    else                     -> local.date()

    Consequence: a trading day labelled Monday BEGINS Sunday evening NY time.
    The boundary is 21:00 UTC during US EDT and 22:00 UTC during US EST — it
    moves, which is precisely why no offset may be hardcoded."""
    local = from_utc_ns(ns, TRADING_DAY_TZ)
    if local.time() >= _ROLLOVER_TIME:
        return local.date() + timedelta(days=1)
    return local.date()


def trading_day_bounds(d: date) -> tuple[Nanos, Nanos]:
    """Half-open [start_ns, end_ns) for the named trading day. start is
    17:00 NY on the previous calendar day; end is 17:00 NY on day d.
    Both resolved through local_to_utc_ns with fold_policy="later"."""
    start_ns = local_to_utc_ns(d - timedelta(days=1), _ROLLOVER_TIME, TRADING_DAY_TZ, "later")
    end_ns = local_to_utc_ns(d, _ROLLOVER_TIME, TRADING_DAY_TZ, "later")
    return Nanos(start_ns), Nanos(end_ns)


@dataclass(frozen=True, slots=True)
class WeekId:
    iso_year: int
    iso_week: int

    def __str__(self) -> str:
        return f"{self.iso_year}-W{self.iso_week:02d}"


def trading_week(ns: Nanos) -> WeekId:
    """ISO year-week of the WEDNESDAY of the trading week containing ns.

    Anchoring on Wednesday (rather than the week's first day) avoids the
    year-boundary ambiguity that arises when a trading week straddles
    1 January: the Wednesday is always unambiguously inside one ISO year.
    Derive the week from the trading day, not the raw UTC calendar date."""
    day = trading_day(ns)
    weekday = day.isoweekday()  # Mon=1 .. Sun=7

    if weekday <= 5:  # Mon-Fri: the normal case
        wednesday = day + timedelta(days=3 - weekday)
    else:
        # trading_day() can, in principle, return a raw Saturday/Sunday date
        # for a pre-17:00-Sunday instant (thin/edge weekend data); such a
        # date belongs to the UPCOMING trading week, not the ISO week its
        # raw calendar date would fall in.
        days_to_next_monday = 8 - weekday  # Sat(6)->2, Sun(7)->1
        wednesday = day + timedelta(days=days_to_next_monday + 2)

    iso_year, iso_week, _ = wednesday.isocalendar()
    return WeekId(iso_year=iso_year, iso_week=iso_week)


def is_weekend_gap(ns: Nanos) -> bool:
    """True if this instant falls in the market's weekend closure:
    at or after 17:00 Friday NY, and before 17:00 Sunday NY."""
    local = from_utc_ns(ns, TRADING_DAY_TZ)
    weekday = local.isoweekday()  # Mon=1 .. Sun=7
    t = local.time()

    if weekday == 5:  # Friday
        return t >= _ROLLOVER_TIME
    if weekday == 6:  # Saturday
        return True
    if weekday == 7:  # Sunday
        return t < _ROLLOVER_TIME
    return False
