"""SessionSet: window resolution, membership queries, derived sessions."""

from dataclasses import dataclass
from datetime import date

from jarvis.core.errors import SessionError
from jarvis.core.types import Nanos
from jarvis.sessions.definitions import SessionSetDef, load_session_set_def, resolve_boundary_ns
from jarvis.timeengine import is_weekend_gap
from jarvis.timeengine import trading_day as _trading_day_of


@dataclass(frozen=True, slots=True)
class Window:
    name: str
    trading_day: date
    start_ns: Nanos
    end_ns: Nanos  # half-open: [start_ns, end_ns)
    partial: bool  # overlaps the weekend gap
    empty: bool  # start_ns == end_ns (possible for derived sessions)

    @property
    def duration_ns(self) -> int:
        return self.end_ns - self.start_ns


def _is_partial(start_ns: Nanos, end_ns: Nanos) -> bool:
    # A window is at most ~9 hours (the longest concrete session in
    # fx_core.v1 spans 8h; a derived intersection can only be narrower than
    # its parents). is_weekend_gap has no interior "island" within a single
    # short window -- the gap is one contiguous span (Fri 17:00 NY to Sun
    # 17:00 NY), so a window that dips into the gap and back out again
    # within its own short span is impossible: once a probe inside [start,
    # end) is in the gap, every instant between that probe and whichever
    # boundary is also inside the gap is in the gap too, and vice versa.
    # Endpoint-plus-midpoint therefore can only miss a gap-entry/exit that
    # falls strictly between two probes with no probe on either side of it
    # -- which cannot happen here because the three probes (start, last
    # included instant, midpoint) are spaced no more than ~4.5h apart, and
    # the gap boundary itself is a session boundary only if a session were
    # anchored exactly at 17:00 NY on a Friday or Sunday, which none of
    # fx_core.v1's sessions are.
    if is_weekend_gap(start_ns) or is_weekend_gap(Nanos(end_ns - 1)):
        return True
    midpoint = Nanos((start_ns + end_ns) // 2)
    return is_weekend_gap(midpoint)


class SessionSet:
    def __init__(self, definition: SessionSetDef) -> None:
        self.definition = definition
        # window() is a pure function of (definition, name, trading_day):
        # self.definition never changes after construction (SessionSetDef is
        # frozen), and load_session_set_def's content-aware cache (WP-004
        # addendum) means a changed YAML produces a new SessionSetDef and
        # therefore a new SessionSet with a fresh, empty cache here -- there
        # is no path by which this dict can go stale against a live
        # instance. An instance-level dict (not functools.lru_cache on the
        # method) is deliberate: an lru_cache on a bound method keeps `self`
        # alive for the cache's lifetime and leaks across instances; this
        # dict's lifetime is exactly the SessionSet's own.
        self._window_cache: dict[tuple[str, date], Window] = {}

    def session_names(self) -> tuple[str, ...]:
        return tuple(self.definition.sessions)

    def window(self, name: str, trading_day: date) -> Window:
        """Resolve a named session's window for the given trading day.
        Raises SessionError for an unknown session name."""
        cache_key = (name, trading_day)
        cached = self._window_cache.get(cache_key)
        if cached is not None:
            return cached

        session = self.definition.sessions.get(name)
        if session is None:
            raise SessionError(f"unknown session: {name!r}")

        if session.derived is not None:
            parent_windows = [self.window(parent, trading_day) for parent in session.of]
            start_ns = Nanos(max(w.start_ns for w in parent_windows))
            end_ns = Nanos(min(w.end_ns for w in parent_windows))
            if end_ns <= start_ns:
                end_ns = start_ns
        else:
            start_ns = resolve_boundary_ns(
                trading_day, session.start, session.tz, self.definition.fold_policy
            )
            end_ns = resolve_boundary_ns(
                trading_day, session.end, session.tz, self.definition.fold_policy
            )

        empty = start_ns == end_ns
        result = Window(
            name=name,
            trading_day=trading_day,
            start_ns=start_ns,
            end_ns=end_ns,
            partial=False if empty else _is_partial(start_ns, end_ns),
            empty=empty,
        )
        self._window_cache[cache_key] = result
        return result

    def membership(self, ns: Nanos) -> frozenset[str]:
        """Every session name whose window contains this instant.
        Sessions may overlap; all matches are returned. Uses
        timeengine.trading_day(ns) to determine which day's windows apply."""
        day = _trading_day_of(ns)
        matches = set()
        for name in self.definition.sessions:
            window = self.window(name, day)
            if not window.empty and window.start_ns <= ns < window.end_ns:
                matches.add(name)
        return frozenset(matches)


def load_session_set(session_set_id: str, version: int) -> SessionSet:
    return SessionSet(load_session_set_def(session_set_id, version))
