# WP-009-TZ — The HistData timestamp anomaly, resolved on the full archive

**Scope:** HistData.com GBP/USD ASCII tick archive, 2006-09 … 2022-12, 196 monthly
files (`data/ALL_DATA_GBPUSD.zip`).
**Status:** resolved. The earlier characterisation was wrong and is superseded below.

---

## 1. What the anomaly actually is

Previous work, from sample months only, described the archive as *mostly*
`America/New_York` with *some* March and November months in 2019–2022 reading as a
fixed UTC−5. That framing does not survive the full archive. **No file anywhere in
the archive is on a fixed UTC−5 clock.** Every file is DST-aware. What changes, once,
is *which daylight-saving calendar the stamps follow*:

| Era | Files | Stamp clock |
|---|---|---|
| **A** | 2006-09 … 2018-12 | `America/New_York` wall clock — UTC−5/−4, changing on the **US** dates (2nd Sun Mar / 1st Sun Nov) |
| **B** | 2019-01 … 2022-12 | UTC−5/−4, changing on the **EU** dates (last Sun Mar / last Sun Oct) |

The two calendars agree for about eleven months of the year. They differ only inside
two windows:

- **spring** — US change → EU change (roughly two to three weeks in March)
- **autumn** — EU change → US change (roughly one week, late Oct into early Nov)

Inside those windows New York is UTC−4 while London is UTC+0, so the two clocks place
every stamp exactly one hour apart. Everywhere else they are not merely
indistinguishable but *interchangeable*: a file containing no divergent date converts
identically under either.

That is why the anomaly looked like "some Marches and some Novembers." Those are
simply the months that contain a divergence window.

### The exact Era B switch instant

Era B's clock does not change at the EU transition instant (01:00 UTC on the Sunday).
It changes at **00:00 UTC on the Monday following** — about 23 hours later. This is not
inferred; it is directly visible, and it is why the discontinuity appears mid-session
on Sunday evening rather than buried inside the weekend closure.

---

## 2. Evidence

### 2.1 Weekend boundaries on divergent weeks — 94 of 94, no exceptions

The market closes 17:00 New York on Friday and reopens 17:00 New York on Sunday. Both
edges are structural, not statistical: either there are ticks or there are not. On a
divergent day the two clocks predict close/reopen stamps an hour apart
(`16:5x`/`17:00` under us_dst, `15:5x`/`16:00` under eu_dst).

| Era | Divergent-week boundaries | Read us_dst | Read eu_dst |
|---|---|---|---|
| 2007–2018 | 70 | **70** | 0 |
| 2019–2022 | 24 | 0 | **24** |

Zero ambiguous, zero contradictory. (Excluded from the count: the four Europe/London
change Sundays in Era B — see §4.3.)

### 2.2 The stamp clock's own discontinuities — exactly 8, all in Era B

A DST-aware clock leaves a mark when it changes: a one-hour hole when it springs
forward, a backward step when it falls back. Scanning **every consecutive stamp pair of
all 196 files** finds exactly eight, and they are all on EU change dates in 2019–2022:

| Date | Last stamp before | First stamp after | Rule boundary (00:00 UTC Monday) |
|---|---|---|---|
| 2019-03-31 | 18:59:56 | 20:00:00 | 2019-04-01 — 3.2s / 0.4s |
| 2019-10-27 | 19:59:59 | 19:00:13 | 2019-10-28 — 0.0s / 13.6s |
| 2020-03-29 | 18:59:57 | 20:00:00 | 2020-03-30 — 2.2s / 0.0s |
| 2020-10-25 | 19:59:59 | 19:00:00 | 2020-10-26 — 0.9s / 0.1s |
| 2021-03-28 | 18:59:28 | 20:00:00 | 2021-03-29 — 31.8s / 0.6s |
| 2021-10-31 | 19:59:55 | 19:00:00 | 2021-11-01 — 4.8s / 0.1s |
| 2022-03-27 | 18:59:42 | 20:00:00 | 2022-03-28 — 17.6s / 0.4s |
| 2022-10-30 | 19:59:57 | 19:00:00 | 2022-10-31 — 2.3s / 0.4s |

Every one brackets the predicted boundary to within seconds. **2006-09 … 2018-12
contains none at all** — its US-calendar changes fall at 02:00 New York on a Sunday,
inside the weekend closure, where no clock change can be seen.

(Three non-clock one-hour artefacts also exist and are distinguishable: 2019-12-24
16:59→17:59 and two on 2022-12-13 — a Christmas Eve early close and a feed outage.)

### 2.3 End-to-end, against a third city

The 16:00 London fix is the sharpest repeatable feature in GBP/USD and is pinned to
**London's** clock, which is not the conversion basis in either era. Converting every
divergent weekday under a rule and expressing the result in London local time:

| Era | Converted as | Fix lands at |
|---|---|---|
| 2007–2018 (non-divergent days, unambiguous) | — | 15:59 |
| 2007–2018 divergent | **us_dst** (the rule) | **15:59** ✓ |
| 2007–2018 divergent | eu_dst (control) | 16:59 ✗ |
| 2019–2022 (non-divergent days, unambiguous) | — | 16:00 |
| 2019–2022 divergent | us_dst (control) | 15:00 ✗ |
| 2019–2022 divergent | **eu_dst** (the rule) | **16:00** ✓ |

The rule places the fix correctly in both eras; the wrong rule displaces it by exactly
an hour in both. Three independent anchors (weekend boundary → New York, clock
discontinuity → the clock itself, London fix → London) agree.

### 2.4 Whole-archive parse

The implementation was run over all 196 real files:

| Outcome | Files |
|---|---|
| detected `us_dst` | 33 — all 2006-10 … 2018-11, none later |
| detected `eu_dst` | 11 — all 2019-03 … 2022-11, none earlier |
| no determination needed | 151 |
| failed | 1 — 2009-05, column order, unrelated to timezones (§5.2) |

Zero files cross the era boundary in either direction. After conversion, **every Friday
weekend close in all 195 parsed files lands at 17:00 New York**, with two exceptions,
both holidays rather than timezone errors: 2010-12-24 (Christmas Eve early close) and
2020-12-25 (Christmas Day).

The 11 `eu_dst` files are the twelve March/October/November files of 2019–2022 minus
2020-11, which needs no determination — see §4.1.

---

## 3. The two open questions from the brief

### Job #1 — why 2022-11 showed only 1 of 4 readable days

**Answer: an artefact of the detector, not a property of the data.** Of the four
weekdays before the 2022-11-06 US change, Nov 1–3 are Tue/Wed/Thu and Nov 4 is a
**Friday**. Two separate things were going on:

- The daily-rollover anchor cannot read a Friday at all. The 17:00 New York rollover
  quiet period is exactly where the weekend closure begins, so on a Friday the "trough"
  is the weekend, not the rollover. The same structural blindness applies to Sundays,
  where the 16:00 side is empty because the market has not opened. Any per-day detector
  keyed on the rollover must discard Fridays and Sundays or it reads the weekend and
  calls it evidence.
- On the weekend-boundary anchor — which *is* readable on a Friday — 2022-11-04 reads
  cleanly: last tick `15:59`, i.e. 16:59 New York under eu_dst.

So all four days read, and all four agree on `eu_dst`. There was never any ambiguity
here to explain.

### Job #2 — why ~30% of days "disagreed" in 2019-03 and 2022-03

**Answer: the disagreement was not random, and in 2022-03 it is not disagreement at
all — it is the file changing clock partway through the month, exactly on schedule.**

2022-03 splits cleanly:

| Days | Regime | Reads |
|---|---|---|
| Mar 1–11 | US EST, London GMT — not divergent | non-discriminating |
| Mar 13–24 | US EDT, London GMT — **divergent** | `eu_dst` (rollover at stamp 16:xx) |
| Mar 28–31 | US EDT, London BST — not divergent | offset −4, as both clocks agree |

The switch between the second and third block is the 2022-03-27 clock discontinuity in
§2.2. A month-level convention cannot represent this file: **the file genuinely holds
two different offsets**, and any single per-file constant is wrong for part of it. The
previous detector, forced to pick one label for the month, was being asked an
unanswerable question — and the "~30% of days disagree" figure was that question's
residue, not a property of the data.

2019-03 is the same picture with a later EU change (Mar 31): its divergence window runs
from Mar 11 to the end of the month, so there are no post-switch days in the file at
all. Its readings are uniformly `eu_dst`; the apparent noise came from the soft
rollover anchor being read on Fridays and Sundays (see Job #1) and on thin days with no
real signal.

### The "April bleed" — dismissed, with evidence

April contains **no divergent dates in any year** (the EU change is always in March), so
there is nothing in an April file for a convention to be wrong about: both clocks agree
on every tick. Checked directly anyway — every April Friday in 2019–2022 closes at
`16:59`, the London open sits at stamp 03:00 and the rollover at 17:00, all exactly as
both clocks predict. The reported 2021-04-01/02 and 2022-04-01/02 anomaly does not exist
in the data.

---

## 4. What is genuinely unknowable, and why it no longer matters

The old framing needed a "quarantine" bucket for months whose convention could not be
determined. Under the corrected model that bucket is **empty**, because the question
changed shape:

### 4.1 Blindness and risk are the same thing

A file can only be blind where the two clocks agree — and where they agree, both give
the identical UTC instant for every tick. December, the EST season, 2020-11: these were
never at risk. They are unreadable *because* nothing depends on reading them.

### 4.2 The 2018/2019 changeover date cannot be narrowed

The last pre-switch evidence is the 2018 autumn window (Oct 29 – Nov 2, reads `us_dst`);
the first post-switch evidence is the 2019 spring window (Mar 11 onward, reads
`eu_dst`). Between them lies a stretch in which the two calendars agree, so nothing in
the data can say where in that gap the change happened. **This is a permanent limit, and
a harmless one** — every file in the gap converts identically either way.

### 4.3 The four Europe/London change Sundays in Era B

On 2019-03-31, 2020-03-29, 2021-03-28, 2022-03-27 and their autumn counterparts, the
week opens roughly an hour away from its usual 17:00 New York — later in spring, earlier
in autumn. The reason is not determinable from our side and is not guessed at here. It
is recorded as an observation, and those Sundays are excluded from detection evidence.
The cost is nil: every affected window still carries an unambiguous Friday close.

---

## 5. Two findings outside the timezone question

Both surfaced only by running the real archive, and both are flagged rather than
quietly absorbed.

### 5.1 `bid >= ask` rejected genuine zero-spread quotes — **fixed**

The spread sanity check rejected `bid >= ask`, which made **2009-11 unimportable**. The
real file has exactly five rows where `bid == ask` (all on 2009-11-13 around 15:30) and
**zero** rows where `bid > ask`. This directly contradicted `detect_column_order`'s own
comment, which cites those same five rows as *genuine zero-spread thin quotes* and sizes
its 1% tolerance around them. Changed to reject only `bid > ask`; a true inversion is
still corruption.

### 5.2 2009-05 changes column order mid-month — **not fixed; needs a decision**

`detect_column_order` refuses 2009-05 (76% of rows have col2 > col3, neither ≥99% nor
≤1%) and says a human must decide. It is right to refuse, but the file is not ambiguous
— it switches cleanly, at a weekend boundary:

| Days | Rows | Fraction col2 > col3 |
|---|---|---|
| May 1 – 22 | 491,616 | **1.0000** — (ask, bid) |
| May 24 – 31 | 143,922 | **0.0000** — (bid, ask) |

This also corrects the existing module comment, which describes 2009-05 as "a clean
transition month" — it is a transition month, but the transition is *inside* it.

Structurally this is the same lesson as the timezone finding: a per-file constant cannot
describe a file whose convention changes partway through. The fix would be to detect
column order per day under the same ≥99%/≤1% rule and require any switch to fall on a
weekend boundary. **Left unimplemented**: column order is a different data convention
from the one this investigation was asked to resolve, one file in 196 is affected, and
the current behaviour fails loudly rather than guessing.

---

## 6. Consequences for the ingest

1. **The stamp clock is not a per-file constant.** An Era B March or October file holds
   two offsets. `parse_histdata_csv` now assigns offsets per row, splitting at the
   verified switch.
2. **October 2019–2022 are not monotonic.** The clock falls back and re-uses an hour it
   has already stamped. The previous strict ascending check rejected all four files
   outright. Exactly one backward step is now permitted, only at the instant the rule
   predicts; more than one is corruption and still fails.
3. **The repeated hour is resolved by position in the file, not by wall clock.** A fold
   policy cannot help — both passes have identical wall-clock stamps and only file order
   separates them.
4. **The convention hint is gone.** The import loop used to carry the last verified
   convention forward into months that could not discriminate. That crutch existed to
   get past months that were blind, and it is exactly the mechanism that would carry a
   convention across a changeover and hide it. It is removed: a month now either proves
   its own clock from its own content, or does not need one.
5. **October files have no usable weekend boundary** — their only divergent weekend day
   is the Europe/London change Sunday, which is excluded. They are read from the clock
   discontinuity instead, which is decisive in both directions (present → `eu_dst`;
   absent, in a file spanning the instant → `us_dst`).
6. **The "does this file need a determination" test keys on ticks, not the calendar.**
   2008-11 and 2014-11 each contain exactly one divergent date, and it falls on a
   Saturday with zero ticks. Keying off the calendar month hard-failed both for no
   reason.

## 7. Open item

The decision log in this repository ends at D-026 (plus pending D-027/D-028) and does
not contain D-042, D-045a or D-054, which the working brief cites as frozen. This
finding needs a decision-log entry, and the numbering should come from whichever copy
of the log is current.
