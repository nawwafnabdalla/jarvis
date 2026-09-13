"""Stage 2's date scope. AR-1 (Technical Bible Part 4 §Z.1, "already
incorporated") restricts Stage 2's initial report suite (R1, R2, R5) to
2007-2014 -- narrower than the development period (2007-2018) and the
DESCRIPTIVE_PRE_VAULT range C4b would otherwise permit (2007-2022) --
specifically so 2015-2018 stays unseen during hypothesis generation (D-067
corrects the conflicting "2007-2022" language C4b and G.1.1 originally
carried). No `vault.PERIODS`/`QueryClass` entry exists for this narrower
range (only development/validation/holdout do, none matching), and
`vault.GatedReader` itself does not exist yet -- so this is a minimal,
Stage-2-local mechanism, not a new general-purpose period class: a single
fixed constant every Stage 2 descriptive report computation uses
unconditionally, so the range is never a parameter a caller could forget
or override, not a type-level guarantee equivalent to what GatedReader
will eventually provide.
"""

from datetime import datetime, timezone

from jarvis.core.types import Nanos

STAGE2_DESCRIPTIVE_START_NS = Nanos(
    int(datetime(2007, 1, 1, tzinfo=timezone.utc).timestamp()) * 1_000_000_000
)
STAGE2_DESCRIPTIVE_END_NS = Nanos(
    int(datetime(2015, 1, 1, tzinfo=timezone.utc).timestamp()) * 1_000_000_000
)


def stage2_descriptive_range() -> tuple[Nanos, Nanos]:
    """The fixed, half-open [start, end) range every Stage 2 descriptive
    report (R1, R2, R5) computes over: 2007-01-01T00:00:00Z through
    2015-01-01T00:00:00Z exclusive, i.e. 2007-2014 inclusive. Not a
    parameter -- there is no way to compute a Stage 2 descriptive report
    over any other range."""
    return STAGE2_DESCRIPTIVE_START_NS, STAGE2_DESCRIPTIVE_END_NS
