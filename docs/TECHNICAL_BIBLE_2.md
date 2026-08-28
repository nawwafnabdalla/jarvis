# JARVIS TECHNICAL BIBLE — Part 2 of 4
## Parts F–L: Features · Market Description & Stage 0 · Strategy · Opportunity · Ledger · Lifecycle · Vault

---

# PART F — FEATURE SPECIFICATION

## F.1 Principles

1. A feature is a pure function of bars at or before time `t`, the session set, and its own parameters. Nothing else.
2. **No feature may read a bar with `ts_utc_ns > t`.** This is not a convention; it is enforced by a mechanical test (Part 4 §S.6).
3. Insufficient history yields `null`, never `0`, never a forward-fill, never a partial-window estimate.
4. Every feature declares its lookback in **bars, sessions or trading days**, and its tolerance for gaps.
5. Features are computed once per `(dataset_version, session_set, feature_set_version)` and cached to Parquet with a content hash. Recomputation must be byte-identical.

## F.2 Feature definition object

```python
@dataclass(frozen=True)
class FeatureDef:
    name: str
    version: int
    dtype: Literal["f64", "i64", "bool", "cat"]
    lookback: Lookback                  # bars=N | sessions=N | trading_days=N
    gap_tolerance_ns: int | None        # null the output if a window gap exceeds this
    requires: tuple[str, ...]           # other feature names
    params: Mapping[str, float | int | str]
    compute: Callable[[FeatureContext], pl.Series]
    leakage_class: Literal["causal", "session_terminal"]
```

`session_terminal` marks features that are only defined once a session has **closed** (for example `pre_london_range`). These are the highest-risk features in the library: a naive implementation makes the completed Asian range available at 03:00 London time, which is a time machine. The framework enforces it: a `session_terminal` feature's value is null for every bar with `ts_utc_ns < session_window.end_ns` and constant thereafter for the remainder of the trading day.

## F.3 The V1 library

Nineteen features. No RSI, MACD, Bollinger Bands or ADX — they enter only when a feature-lab experiment demands one, per the Product Bible.

Notation: `m(t)` = mid price = `(bid_c + ask_c)/2` at bar `t`. All prices in price units; all ranges convertible to ATR units by the distance features.

| # | Name | Definition | Lookback | Class | Leakage risk |
|---|---|---|---|---|---|
| 1 | `ret_1m` | `ln(m(t)/m(t-1))` | 1 bar | causal | Low. Null if `prev_gap_ns > 5 min`. |
| 2 | `ret_5m`, `ret_15m`, `ret_60m` | `ln(m(t)/m(t-k))` | k bars | causal | Low |
| 3 | `true_range_1m` | `max(bid_h,·) − min(bid_l,·)` vs previous close, standard TR on the **bid** series | 2 bars | causal | Low |
| 4 | `atr_bars(n=1440)` | Wilder EMA of `true_range_1m` over n present bars | 1440 bars | causal | Medium — must count *present* bars, not clock minutes |
| 5 | `rv_60m` | `sqrt(Σ ret_1m² )` over trailing 60 present bars, annualisation **not** applied | 60 bars | causal | Low |
| 6 | `rv_ratio` | `rv_60m / median(rv_60m)` over trailing 20 trading days at the same hour-of-day | 20 days | causal | **High** — the hour-of-day conditioning must use only prior days |
| 7 | `pre_london_high`, `pre_london_low`, `pre_london_range` | Extremes of the bid/ask mid over the `pre_london` window | 1 session | session_terminal | **High** |
| 8 | `pre_london_range_pct(n=60)` | Percentile rank of today's `pre_london_range` within the trailing n trading days' values, **excluding today** | 60 days | session_terminal | **High** — off-by-one inclusion of today is the classic bug |
| 9 | `pre_london_close_position` | `(m(session_end) − pre_london_low) / pre_london_range` | 1 session | session_terminal | High |
| 10 | `london_high`, `london_low`, `london_range` | Same over `london` | 1 session | session_terminal | High |
| 11 | `prev_day_high`, `prev_day_low`, `prev_day_close` | Over `trading_day = D−1`, present bars only | 1 day | causal from 00:00 of D | Medium |
| 12 | `prev_week_high`, `prev_week_low` | Over the prior trading week | 1 week | causal | Medium |
| 13 | `dist_to(level)` | `(m(t) − level) / atr_bars(1440)`, signed, in ATR units | — | inherits | Inherits the level's class |
| 14 | `time_since_session_start_ns` | `t − session_window.start_ns` for each active session | — | causal | Low |
| 15 | `session_state` | Categorical: which sessions contain `t`, plus `pre_open`/`post_close` | — | causal | Low |
| 16 | `break_state(level, buffer_atr)` | `none` \| `above` \| `below`; first bar whose mid exceeds `level ± buffer × atr` | — | inherits | Medium |
| 17 | `reentry_state(level, buffer_atr, window)` | `true` if `break_state` fired and mid returned inside the level within `window` present bars | — | inherits | **High** — must not peek past `t` |
| 18 | `spread_now`, `spread_pct` | `spread_twa` of bar `t`; percentile within trailing 20 trading days at the same hour-of-week | 20 days | causal | Medium |
| 19 | `hour_of_day_london`, `day_of_week_trading`, `minutes_into_trading_day` | Calendar features from `timeengine` | — | causal | Low — but wrong if DST is mishandled |

### F.3.1 Notes that matter

- **`atr_bars` counts present bars.** Using clock minutes silently shortens the window across data gaps and makes ATR jump after every quiet period.
- **`rv_ratio` and `spread_pct` condition on hour-of-day.** The conditioning sample must be strictly prior trading days. Including today makes every quiet day look quiet relative to itself.
- **`pre_london_range_pct` excludes today by construction.** If today is included, the top and bottom of the distribution are structurally unreachable and every percentile is biased toward the centre.
- **`reentry_state` has a forward window.** It is therefore only defined at `t + window`. The framework enforces this by shifting the output series and nulling the tail. A feature with a forward window that is not shifted is the single most productive source of false edges in retail backtesting.

## F.4 Per-feature unit test requirement

Every feature ships with at minimum:
1. A hand-computed value on a 10-bar synthetic fixture where the answer is checkable by eye.
2. A null-propagation test: with `lookback − 1` bars available, the output is null.
3. A gap test: with a `gap_tolerance_ns`-exceeding hole in the window, the output is null.
4. A **leakage test**: compute over `bars[0:n]`, then over `bars[0:n+50]`, and assert the first `n` values are identical. Any feature that fails this reads the future. This test is generated automatically for every registered feature; adding a feature to the registry adds the test.
5. A determinism test: two computations produce identical bytes.

---

# PART G — MARKET DESCRIPTION SPECIFICATION (STAGE 2) AND STAGE 0

## G.0 Stage 0 — Feasibility probe

### G.0.1 Purpose

Stage 0 answers one question: **can a specialisation this narrow produce enough observations to ever be evaluated?** It is not looking for an edge, and its outputs must never be used as evidence of one. Every Stage 0 artefact carries the watermark `EXPLORATORY — FREQUENCY ONLY — NOT EVIDENCE OF PREDICTIVE VALUE`.

### G.0.2 Data scope

Stage 0 runs on **2007–2022 only** (development + validation). The vault is untouched, including for descriptive purposes. Rationale: Stage 0's output can change the choice of instrument, and instrument selection informed by holdout years is holdout contamination at the highest level of the project. This is stricter than F-09 permits and is stated here as a deliberate tightening.

A year is admissible only if ≥ 95% of its expected trading hours are present and it has zero QA ERRORs. Inadmissible years are reported and excluded from the counts; if fewer than 12 years are admissible, Stage 0 returns `INSUFFICIENT DATA` rather than a gate decision.

### G.0.3 Candidate context definitions

Four contexts, chosen to be descriptive, mechanically unambiguous, and free of any directional or predictive claim. None is asserted to have value.

**C-A — Pre-London compression.**
`pre_london_range_pct(60) <= 0.33`, evaluated at the close of the `pre_london` window. One event per trading day, at most.

**C-B — London interaction with the pre-London extreme.**
Within `london_open_window` (08:00–11:00 London), the mid trades beyond `pre_london_high + 0.10 × atr_bars(1440)` or below `pre_london_low − 0.10 × atr_bars(1440)`. One event per trading day **per direction**, timestamped at first occurrence. A day where both occur counts as two events for C-B and one day for the day-count.

**C-C — Break and re-entry.**
A C-B event occurs, and within 60 present bars the mid returns inside the pre-London range by at least `0.05 × atr`. One event per trading day per direction.

**C-D — Elevated pre-London volatility.**
`pre_london_range_pct(60) >= 0.67`. One event per trading day. Deliberately the complement of C-A so the two together sample both tails.

**Intersections reported:** C-A∩C-B, C-A∩C-C, C-D∩C-B, C-D∩C-C.

### G.0.4 Counting rules

- Events are attributed to the **trading day** of their trigger timestamp.
- Partial Sunday and Friday sessions and thin days are excluded (`exclude_partial: true`).
- Deduplication: within a `(trading_day, context, direction)` tuple, only the first qualifying instant counts. This prevents a single choppy morning generating forty "events."
- Overlaps between contexts are not deduplicated — each context is counted independently, and intersections are counted separately.
- Reported per year: count, and the count normalised by admissible trading days in that year.

### G.0.5 The decision procedure

This is the resolution of PDLA-06. The Product Bible's "~40 events/year" is the requirement for the **final tradeable setup**, not for a context. A context becomes a setup only after a directional filter and an entry trigger are applied, which historically removes 50–75% of instances. Stage 0 therefore applies a two-tier gate.

Let `M` = median annual count across admissible years of the **narrowest intersection** among {C-A∩C-B, C-A∩C-C, C-D∩C-B, C-D∩C-C}, and `P10` = the 10th-percentile annual count.

```
if M >= 100 and P10 >= 60:
        PROCEED GBP/USD
elif M >= 100 and P10 < 60:
        PROCEED GBP/USD (WITH INSTABILITY WARNING)
elif 40 <= M < 100:
        WIDEN CONTEXT
elif M < 40:
        if widening already attempted once:
                CONSIDER EUR/USD FALLBACK
        else:
                WIDEN CONTEXT
```

- **`M ≥ 100`** targets ≥ 40 surviving setup instances per year after a typical 60% attrition, which over the 3.7-year vault yields roughly 150 holdout events — the minimum at which a 0.15R effect is distinguishable from zero at conventional power.
- **`P10 ≥ 60`** guards against a context that only exists in high-volatility years. A setup that vanishes in quiet years cannot be traded consistently, whatever its average frequency.
- **WIDEN CONTEXT** means relaxing exactly one parameter (the percentile threshold from 0.33 to 0.40, or the ATR buffer from 0.10 to 0.05) and re-running. One widening only. Repeated widening until the gate passes is parameter mining, and the run counter records every attempt.
- **CONSIDER EUR/USD FALLBACK** means running the byte-identical probe on EUR/USD and comparing. It does not automatically switch instruments; it produces the comparison and a recommendation for a human decision that becomes a decision-log entry.

Every Stage 0 run — including widenings and the EUR/USD comparison — writes an `experiment_runs` row with `run_class='stage0_probe'`. These do not count toward any hypothesis family's confirmatory budget (no hypothesis exists yet), but they are permanently visible, so the number of attempts made before the gate passed is never lost.

### G.0.6 Stage 0 output

`reports/stage0/STAGE0__{ts}.md` containing: admissible-year table; per-context annual counts; intersection annual counts; median and P10; the gate decision with the arithmetic shown; every parameter used; dataset version; code SHA; and the watermark. Plus `reports/stage0/STAGE0__{ts}.parquet` with one row per event for inspection.

## G.1 Stage 2 — Market description engine

### G.1.1 What this stage is and is not

It computes conditional descriptive statistics about GBP/USD. It does not simulate trades, compute PnL, evaluate entries or exits, or rank anything by profitability. Its purpose is to give a human enough understanding of the market to generate hypotheses worth testing.

**Data scope: 2007–2022 by default.** Vault years are reachable only through the separate coarse regime-check command (Part L.4), never through these reports. This is the resolution of PDLA-03.

### G.1.2 The initial report suite

Six reports. Each has a fixed research question, a deterministic calculation, and mandated uncertainty reporting.

**R1 — Session range anatomy.**
*Question:* how large are the `pre_london`, `london` and `new_york` ranges, and how has that changed?
*Calculation:* distribution of each session's range in price and in ATR units; by year; by day of week.
*Uncertainty:* median with bootstrap 95% CI; interquartile range; n per cell.
*Output:* table + box plot by year.

**R2 — London range conditional on pre-London range percentile.**
*Question:* does a compressed Asian range associate with a larger or smaller London range?
*Calculation:* bucket trading days by `pre_london_range_pct(60)` into quintiles; report the distribution of `london_range / atr_bars(1440)` per bucket; per year and pooled.
*Uncertainty:* bootstrap CI on each bucket median; explicit statement of the pooled n and per-year n.
*Restriction:* the report must display the per-year table adjacent to the pooled table. A relationship visible only in the pooled data and absent in most individual years is an artefact, and the layout is designed to make that obvious.

**R3 — Break and hold frequencies.**
*Question:* when London trades beyond the pre-London extreme, how often does it stay beyond it?
*Calculation:* conditional on a C-B event, the empirical probability that the mid is still beyond the level at +30, +60, +120 present bars; by direction; by `pre_london_range_pct` quintile; by year.
*Uncertainty:* Wilson 95% intervals on every proportion.
*Restriction:* "still beyond the level" is not a trade outcome. The report may not express results in R, pips of profit, or any risk-adjusted form.

**R4 — Where daily extremes form.**
*Question:* at what hour do the trading day's high and low occur?
*Calculation:* histogram of the London-local hour of the daily extreme; by year; by day of week; split by whether the day closed above or below its open.
*Uncertainty:* multinomial CIs per hour bin.

**R5 — Spread and cost climate.**
*Question:* what does it actually cost to transact, by hour and day?
*Calculation:* `spread_twa` distribution by hour-of-week; by year; ratio of median spread to median 60-minute range for the same hour.
*Uncertainty:* quantiles with bootstrap CIs.
*Why it exists:* this report frequently ends projects early, in a good way. If the median spread in the target window is a meaningful fraction of the median move, no strategy in that window survives costs and no further work is warranted.

**R6 — Volatility regimes by year.**
*Question:* how different are the years from one another?
*Calculation:* `rv_60m` distribution by year and by session; range distributions by year; year-over-year ratio table.
*Uncertainty:* CIs on annual medians.
*Why it exists:* it calibrates expectations about how much year-to-year variation is normal, which is what makes later year-by-year robustness results interpretable.

### G.1.3 Mandatory report furniture

Every Stage 2 report carries, without exception:
- The header watermark `DESCRIPTIVE — EXPLORATORY — NOT EVIDENCE`.
- Dataset version, session set version, feature set version, code SHA, generation timestamp.
- The data period covered, stated explicitly, with confirmation that vault years are excluded.
- **Sample-size warnings**: any cell with n < 30 is rendered with a warning marker; any cell with n < 10 is suppressed and shown as `n<10 suppressed`.
- A fixed closing paragraph stating that patterns visible in descriptive statistics are the *starting point* for a hypothesis, not support for one, and that any relationship worth acting on must be pre-registered and tested independently.

### G.1.4 Interpretation restrictions, enforced

- No Stage 2 report may compute PnL, R, expectancy, win rate, profit factor, or drawdown. The `describe` module has no import path to `statistics` or `execution` (Part 1 §B.1), so this is structural.
- No Stage 2 report may rank conditions by any profitability proxy.
- No Stage 2 report accepts a user-supplied threshold scan. If a user wants to see twenty percentile cutoffs, that is a parameter sweep and it belongs in Stage 3 with a run counter attached.

---

# PART H — STRATEGY SPECIFICATION

## H.1 The manifest

`strategies/{family}__{name}.v{n}.yaml`:

```yaml
schema_version: 1
strategy_id: "pre_london_break__baseline"
version: 3
strategy_name: "Pre-London range break, baseline"
hypothesis_family_id: "FAM-002"
hypothesis_id: "HYP-004"

instrument: GBPUSD
timeframe: "1m"
dataset_role: research

session_set: { id: fx_core, version: 1 }
sessions:
  scan_window: london_open_window
  exclude_partial: true

impl:
  module: "jarvis.strategy_impls.pre_london_break.baseline_v3"
  scan_fn: "scan"
  impl_sha256: "a3f1…"          # written by `jarvis strategy validate`, verified at run

features:
  required:
    - pre_london_high
    - pre_london_low
    - pre_london_range_pct
    - atr_bars
    - spread_now
  feature_set_version: 1

parameters:
  range_pct_max: 0.33
  break_buffer_atr: 0.10
  atr_period_bars: 1440

entry:
  type: stop                     # market | stop | limit
  reference: break_level         # named by the scan function's Candidate
  offset_atr: 0.0

stop:
  type: level
  reference: pre_london_opposite_extreme
  min_distance_atr: 0.15
  max_distance_atr: 1.50

target:
  type: r_multiple
  r_multiple: 2.0

invalidation:
  max_bars_to_fill: 30
  time_stop_bars: 240
  cancel_if_session_ends: true

sizing:
  risk_per_trade_r: 1.0          # always 1.0 in V1; accounting is in R

costs:
  commission_per_side_price_units: 0.0
  swap_model: none               # V1: no overnight holding by construction
  slippage_model: { id: default, version: 1 }

restrictions:
  max_concurrent_positions: 1
  max_opportunities_per_trading_day: 2
  no_entry_if_spread_pct_above: 0.90

metadata:
  author: "user"
  created_utc: "2026-10-14T09:12:00Z"
  notes: "Baseline. No filter beyond compression + buffer."
```

### H.1.1 Rules

- **Every parameter lives here, never in Python.** A sweep changes the manifest and therefore `manifest_sha`, while `impl_sha256` stays constant. This is what makes forty sweep runs comparable and countable.
- `impl_sha256` is computed over the implementation file's bytes. At run time it is recomputed and compared; a mismatch is a HALT. Editing a `scan` function after a sealed experiment used it is therefore detected, not merely discouraged.
- **`costs.commission_per_side` does not include spread.** Spread is already in the bid/ask data (PDLA-04). The validator rejects any manifest containing a `spread` cost field.
- `dataset_role` allows the same manifest to be re-run against the future broker dataset without editing.
- Manifests are immutable once referenced by a sealed experiment run. `jarvis strategy validate` refuses to overwrite such a file and instructs the user to create `v{n+1}`.

## H.2 The Python interface

```python
from typing import Protocol

@dataclass(frozen=True)
class Candidate:
    ts_utc_ns: int              # the bar at which the setup qualified
    direction: Literal["long", "short"]
    entry_reference_price: float
    stop_reference_price: float
    levels: Mapping[str, float] # named levels the manifest may reference
    tags: Mapping[str, str]     # free-form, for later segmentation
    # NOTE: no size, no PnL, no order — those are downstream concerns

class ScanFn(Protocol):
    def __call__(self, ctx: ScanContext) -> Candidate | None: ...

@dataclass(frozen=True)
class ScanContext:
    i: int                          # index of the current bar
    ts_utc_ns: int
    bars: BarView                   # read-only, truncated at i
    features: FeatureView           # read-only, truncated at i
    sessions: frozenset[str]
    params: Mapping[str, float]
    trading_day: date
```

### H.2.1 Purity requirements

`scan` must be a pure function of `ScanContext`. Specifically it may not:
- access account balance, equity, open positions, or prior trade outcomes;
- access whether the user was available, awake, or trading;
- access experiment results, run counts, or any ledger state;
- read any bar with index > `i` — `BarView` and `FeatureView` are hard-truncated and raise `LookaheadError` on out-of-range access, so this is enforced rather than trusted;
- perform I/O of any kind (no file, network, clock, or random access);
- carry mutable state between calls — the scanner passes a fresh context each time and asserts that identical contexts produce identical outputs.

Randomness: forbidden in `scan`. If a strategy genuinely needs a random element, it must be supplied deterministically from a seeded stream in `params`.

Logging: `scan` may call `ctx.note(str)`, which attaches a diagnostic string to the resulting `Candidate`. It may not use the logging module directly.

Errors: an exception inside `scan` aborts the entire run. It is never swallowed and the run is never partially recorded. A strategy that raises on 3 bars out of 6 million produces no experiment at all until fixed.

---

# PART I — OPPORTUNITY SPECIFICATION

## I.1 The ontology

This vocabulary is fixed for the life of the project. Four terms, four meanings, no synonyms.

| Term | Definition | Produced by | Table |
|---|---|---|---|
| **Candidate** | The return value of `scan` at one bar. A setup qualified, before any restriction is applied. Ephemeral — exists in memory. | `strategies` | — |
| **Opportunity** | A candidate that survived the manifest's `restrictions` and has been assigned an entry, stop and target specification. **Every opportunity is recorded, whether or not it produces a trade.** | `opportunities.scan_all` | `opportunities` |
| **Theoretical trade** | The simulated result of an opportunity under the execution model: fills, exit, R outcome. An opportunity may produce no theoretical trade (entry never filled, session ended, invalidated). | `backtest` + `execution` | `theoretical_trades` |
| **Forward record** | A human-logged real-world observation matched to an opportunity: noticed or not, taken or not, and the actual prices. | `forward` | `forward_records` |

The cardinality is deliberate: **1 candidate → 0..1 opportunity → 0..1 theoretical trade → 0..1 forward record.** Because every candidate that fails a restriction is still written to `opportunities` with `rejected_by` populated, the counts of "setups that existed," "setups the rules allowed," "setups that filled," "setups I noticed," and "setups I took" are all recoverable from the same lineage. That is the whole point of F-12, and every downstream metric depends on it.

## I.2 The `opportunities` table

Written in full before any execution simulation begins. Immutable.

```sql
CREATE TABLE opportunities (
    opportunity_id      TEXT PRIMARY KEY,           -- ULID
    run_id              TEXT NOT NULL REFERENCES experiment_runs(run_id),
    strategy_version_id TEXT NOT NULL REFERENCES strategy_versions(strategy_version_id),
    dataset_version_id  TEXT NOT NULL REFERENCES dataset_versions(dataset_version_id),

    ts_utc_ns           INTEGER NOT NULL,
    trading_day         TEXT    NOT NULL,           -- ISO date
    trading_week        TEXT    NOT NULL,
    session_tags        TEXT    NOT NULL,           -- JSON array

    direction           TEXT    NOT NULL CHECK (direction IN ('long','short')),
    entry_ref_price     REAL    NOT NULL,
    stop_ref_price      REAL    NOT NULL,
    target_ref_price    REAL,
    r_distance_price    REAL    NOT NULL,           -- |entry_ref - stop_ref|, the R denominator
    levels_json         TEXT    NOT NULL,
    tags_json           TEXT    NOT NULL,
    scan_notes          TEXT,

    accepted            INTEGER NOT NULL CHECK (accepted IN (0,1)),
    rejected_by         TEXT,                       -- restriction name, null if accepted
    spread_at_signal    REAL    NOT NULL,
    atr_at_signal       REAL,

    created_utc         TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX ix_opp_run          ON opportunities(run_id);
CREATE INDEX ix_opp_day          ON opportunities(strategy_version_id, trading_day);
CREATE INDEX ix_opp_ts           ON opportunities(ts_utc_ns);
CREATE UNIQUE INDEX ux_opp_dedup ON opportunities(run_id, ts_utc_ns, direction);
```

`r_distance_price` is captured **here**, at signal time, from the reference prices. This is the resolution of PDLA-05: the R denominator is fixed before any fill occurs, so entry slippage can only make outcomes worse, never flatter them.

---

# PART J — EXPERIMENT LEDGER

SQLite at `ledger/jarvis.sqlite`. `PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON;`

## J.1 Immutability mechanism

Three layers, because one is not enough:

1. **No UPDATE or DELETE path in application code.** The ledger module exposes `insert_*` functions only. There is no generic `execute`.
2. **Database triggers.** Every append-only table carries:
   ```sql
   CREATE TRIGGER trg_no_update_opportunities BEFORE UPDATE ON opportunities
   BEGIN SELECT RAISE(ABORT, 'opportunities is append-only'); END;
   CREATE TRIGGER trg_no_delete_opportunities BEFORE DELETE ON opportunities
   BEGIN SELECT RAISE(ABORT, 'opportunities is append-only'); END;
   ```
   Applied to: `dataset_versions`, `code_versions`, `hypothesis_families`, `hypotheses`, `strategy_versions`, `experiment_runs`, `experiment_parameters`, `opportunities`, `theoretical_trades`, `robustness_runs`, `preregistrations`, `holdout_unlocks`, `state_transitions`, `audit_log`.
3. **Hash-chained audit log.** Every insert into any of the above also appends to `audit_log` with `prev_hash` and `row_hash = sha256(prev_hash ‖ canonical_json(event))`. `jarvis audit verify` walks the chain from genesis. Direct `sqlite3` tampering that bypasses the triggers breaks the chain and is detected.

**Mutable state is modelled as appended transitions, not as updated columns.** There is no `status` column anywhere. Current state is `SELECT to_state FROM state_transitions WHERE entity_id=? ORDER BY seq DESC LIMIT 1`.

## J.2 DDL

```sql
-- ---------- provenance ----------
CREATE TABLE dataset_versions (
    dataset_version_id TEXT PRIMARY KEY,
    role            TEXT NOT NULL CHECK (role IN ('research','execution')),
    instrument      TEXT NOT NULL CHECK (instrument = 'GBPUSD'),
    source          TEXT NOT NULL,
    coverage_start_ns INTEGER NOT NULL,
    coverage_end_ns   INTEGER NOT NULL,
    manifest_path   TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL UNIQUE,
    tzdata_version  TEXT NOT NULL,
    qa_errors       INTEGER NOT NULL,
    qa_warnings     INTEGER NOT NULL,
    sealed          INTEGER NOT NULL CHECK (sealed IN (0,1)),
    sealed_utc      TEXT,
    created_utc     TEXT NOT NULL
);

CREATE TABLE code_versions (
    code_version_id TEXT PRIMARY KEY,
    git_sha         TEXT NOT NULL,
    tree_clean      INTEGER NOT NULL CHECK (tree_clean = 1),   -- dirty trees cannot be recorded
    python_version  TEXT NOT NULL,
    deps_lock_sha256 TEXT NOT NULL,
    created_utc     TEXT NOT NULL,
    UNIQUE (git_sha, deps_lock_sha256)
);

-- ---------- research objects ----------
CREATE TABLE hypothesis_families (
    hypothesis_family_id TEXT PRIMARY KEY,          -- FAM-002
    title           TEXT NOT NULL,
    core_claim      TEXT NOT NULL,
    created_utc     TEXT NOT NULL
);

CREATE TABLE hypotheses (
    hypothesis_id   TEXT PRIMARY KEY,               -- HYP-004
    hypothesis_family_id TEXT NOT NULL REFERENCES hypothesis_families(hypothesis_family_id),
    file_path       TEXT NOT NULL,                  -- hypotheses/HYP-004-*.md
    file_sha256     TEXT NOT NULL,
    observation     TEXT NOT NULL,
    proposed_relationship TEXT NOT NULL,
    scope           TEXT NOT NULL,
    mechanism       TEXT,
    expected_direction TEXT NOT NULL,
    falsifier       TEXT NOT NULL,
    confounders     TEXT,
    post_hoc        INTEGER NOT NULL CHECK (post_hoc IN (0,1)),
    created_utc     TEXT NOT NULL
);

CREATE TABLE strategy_versions (
    strategy_version_id TEXT PRIMARY KEY,
    strategy_id     TEXT NOT NULL,
    version         INTEGER NOT NULL,
    hypothesis_family_id TEXT NOT NULL REFERENCES hypothesis_families(hypothesis_family_id),
    hypothesis_id   TEXT REFERENCES hypotheses(hypothesis_id),
    parent_strategy_version_id TEXT REFERENCES strategy_versions(strategy_version_id),
    manifest_path   TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL UNIQUE,
    impl_module     TEXT NOT NULL,
    impl_sha256     TEXT NOT NULL,
    session_set_id  TEXT NOT NULL,
    session_set_version INTEGER NOT NULL,
    feature_set_version INTEGER NOT NULL,
    created_utc     TEXT NOT NULL,
    UNIQUE (strategy_id, version)
);

-- ---------- runs ----------
CREATE TABLE experiment_runs (
    run_id          TEXT PRIMARY KEY,
    run_class       TEXT NOT NULL CHECK (run_class IN
                      ('stage0_probe','describe','development','robustness','validation','holdout','placebo')),
    strategy_version_id TEXT REFERENCES strategy_versions(strategy_version_id),
    hypothesis_family_id TEXT REFERENCES hypothesis_families(hypothesis_family_id),
    preregistration_id TEXT REFERENCES preregistrations(preregistration_id),
    dataset_version_id TEXT NOT NULL REFERENCES dataset_versions(dataset_version_id),
    code_version_id TEXT NOT NULL REFERENCES code_versions(code_version_id),
    period          TEXT NOT NULL CHECK (period IN ('development','validation','holdout','full_pre_vault')),
    period_start_ns INTEGER NOT NULL,
    period_end_ns   INTEGER NOT NULL,
    sweep_id        TEXT,                            -- groups a manual sweep
    sweep_index     INTEGER,
    rng_seed        INTEGER NOT NULL,
    started_utc     TEXT NOT NULL,
    completed_utc   TEXT,
    outcome         TEXT CHECK (outcome IN ('completed','failed','halted')),
    halt_reason     TEXT,
    result_sha256   TEXT,                            -- hash of the canonical result payload
    CHECK (run_class <> 'validation' OR preregistration_id IS NOT NULL),
    CHECK (run_class <> 'holdout'    OR preregistration_id IS NOT NULL)
);
CREATE INDEX ix_runs_family ON experiment_runs(hypothesis_family_id, run_class);
CREATE INDEX ix_runs_sv     ON experiment_runs(strategy_version_id);

CREATE TABLE experiment_parameters (
    run_id          TEXT NOT NULL REFERENCES experiment_runs(run_id),
    name            TEXT NOT NULL,
    value_json      TEXT NOT NULL,
    PRIMARY KEY (run_id, name)
);

CREATE TABLE theoretical_trades (
    trade_id        TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL REFERENCES experiment_runs(run_id),
    opportunity_id  TEXT NOT NULL REFERENCES opportunities(opportunity_id),
    direction       TEXT NOT NULL CHECK (direction IN ('long','short')),

    entry_order_type TEXT NOT NULL,
    entry_filled    INTEGER NOT NULL CHECK (entry_filled IN (0,1)),
    entry_ts_ns     INTEGER,
    entry_price     REAL,
    entry_slippage_price REAL,

    exit_reason     TEXT CHECK (exit_reason IN ('stop','target','time_stop','session_end','no_fill')),
    exit_ts_ns      INTEGER,
    exit_price      REAL,
    exit_slippage_price REAL,

    r_denominator   REAL NOT NULL,                   -- copied from opportunity, never recomputed
    r_gross         REAL,
    r_net           REAL,
    mae_r           REAL,
    mfe_r           REAL,
    bars_held       INTEGER,

    ambiguity_resolved_by TEXT NOT NULL
        CHECK (ambiguity_resolved_by IN ('unambiguous','tick','pessimistic')),
    exec_config_id  TEXT NOT NULL,
    exec_config_sha256 TEXT NOT NULL,
    created_utc     TEXT NOT NULL
);
CREATE INDEX ix_trades_run ON theoretical_trades(run_id);
CREATE UNIQUE INDEX ux_trades_opp ON theoretical_trades(run_id, opportunity_id);

CREATE TABLE robustness_runs (
    robustness_run_id TEXT PRIMARY KEY,
    parent_run_id   TEXT NOT NULL REFERENCES experiment_runs(run_id),
    test_name       TEXT NOT NULL,
    config_json     TEXT NOT NULL,
    result_json     TEXT NOT NULL,
    verdict         TEXT NOT NULL CHECK (verdict IN ('pass','fail','inconclusive')),
    created_utc     TEXT NOT NULL
);

-- ---------- integrity ----------
CREATE TABLE preregistrations (
    preregistration_id TEXT PRIMARY KEY,
    hypothesis_family_id TEXT NOT NULL REFERENCES hypothesis_families(hypothesis_family_id),
    hypothesis_id   TEXT NOT NULL REFERENCES hypotheses(hypothesis_id),
    strategy_version_id TEXT NOT NULL REFERENCES strategy_versions(strategy_version_id),
    target_period   TEXT NOT NULL CHECK (target_period IN ('validation','holdout')),
    primary_metric  TEXT NOT NULL,
    expected_direction TEXT NOT NULL CHECK (expected_direction IN ('positive','negative')),
    threshold_value REAL NOT NULL,
    abandonment_criterion TEXT NOT NULL,
    family_runs_at_prereg INTEGER NOT NULL,          -- multiple-testing snapshot
    manifest_sha256 TEXT NOT NULL,
    impl_sha256     TEXT NOT NULL,
    dataset_version_id TEXT NOT NULL REFERENCES dataset_versions(dataset_version_id),
    code_version_id TEXT NOT NULL REFERENCES code_versions(code_version_id),
    consumed_by_run_id TEXT,                          -- set once, by trigger-guarded insert into a separate table
    created_utc     TEXT NOT NULL
);

CREATE TABLE preregistration_consumption (
    preregistration_id TEXT PRIMARY KEY REFERENCES preregistrations(preregistration_id),
    run_id          TEXT NOT NULL REFERENCES experiment_runs(run_id),
    consumed_utc    TEXT NOT NULL
);

CREATE TABLE holdout_unlocks (
    unlock_id       TEXT PRIMARY KEY,
    hypothesis_family_id TEXT NOT NULL UNIQUE
        REFERENCES hypothesis_families(hypothesis_family_id),
    strategy_version_id TEXT NOT NULL REFERENCES strategy_versions(strategy_version_id),
    preregistration_id TEXT NOT NULL REFERENCES preregistrations(preregistration_id),
    justification   TEXT NOT NULL,
    family_runs_at_unlock INTEGER NOT NULL,
    unlocked_utc    TEXT NOT NULL,
    unlocked_by     TEXT NOT NULL
);

CREATE TABLE state_transitions (
    seq             INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type     TEXT NOT NULL CHECK (entity_type IN ('strategy_version','hypothesis_family')),
    entity_id       TEXT NOT NULL,
    from_state      TEXT,
    to_state        TEXT NOT NULL,
    reason          TEXT NOT NULL,
    evidence_run_id TEXT REFERENCES experiment_runs(run_id),
    created_utc     TEXT NOT NULL
);
CREATE INDEX ix_transitions_entity ON state_transitions(entity_type, entity_id, seq);

CREATE TABLE audit_log (
    seq             INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type      TEXT NOT NULL,
    payload_json    TEXT NOT NULL,
    prev_hash       TEXT NOT NULL,
    row_hash        TEXT NOT NULL,
    created_utc     TEXT NOT NULL
);

-- ---------- forward (Stage 6, schema present in V1) ----------
CREATE TABLE forward_records (
    forward_record_id TEXT PRIMARY KEY,
    opportunity_id  TEXT REFERENCES opportunities(opportunity_id),
    strategy_version_id TEXT NOT NULL REFERENCES strategy_versions(strategy_version_id),
    trading_day     TEXT NOT NULL,
    noticed         INTEGER NOT NULL CHECK (noticed IN (0,1)),
    taken           INTEGER NOT NULL CHECK (taken IN (0,1)),
    intended_entry  REAL, actual_entry  REAL,
    intended_stop   REAL, actual_stop   REAL,
    intended_target REAL, actual_target REAL,
    intended_risk_r REAL, actual_risk_r REAL,
    signal_ts_ns    INTEGER,
    order_ts_ns     INTEGER,
    fill_ts_ns      INTEGER,
    execution_delay_ns INTEGER,
    spread_at_fill  REAL,
    slippage_price  REAL,
    rule_violations TEXT,                             -- JSON array
    error_class     TEXT CHECK (error_class IN
                      ('none','strategy','execution','risk','behavioural','variance')),
    notes           TEXT,
    created_utc     TEXT NOT NULL
);
CREATE INDEX ix_forward_opp ON forward_records(opportunity_id);
```

---

# PART K — EXPERIMENT LIFECYCLE

## K.1 The minimal correct state machine

The Product Bible's illustrative list has eleven states; several are not states but properties of runs. The minimal correct machine has **seven** states for a `strategy_version`.

```
        DRAFT
          │ (manifest validates, impl hash recorded)
          ▼
     DEVELOPMENT ──────────────────► RETIRED
          │  (dev runs, sweeps, robustness — repeatable, all counted)
          │  robustness_complete AND dev criteria met
          ▼
   VALIDATION_READY ────────────────► RETIRED
          │  (preregistration created for target_period=validation)
          ▼
    VALIDATION_RUN ─────────────────► RETIRED   (failed → abandonment criterion met)
          │  validation criteria met
          ▼
    HOLDOUT_READY ──────────────────► RETIRED
          │  (preregistration for holdout + unlock issued — IRREVERSIBLE)
          ▼
     HOLDOUT_RUN ───────────────────► RETIRED
          │  holdout criteria met
          ▼
   FORWARD_TESTING ────────────────► RETIRED
```

## K.2 Transition rules

| From → To | Prerequisites | Irreversible |
|---|---|---|
| DRAFT → DEVELOPMENT | Manifest valid; `impl_sha256` recorded; clean git tree; sealed dataset version exists | No |
| DEVELOPMENT → VALIDATION_READY | ≥1 completed development run; all mandatory robustness tests present with a verdict; a written justification | No |
| VALIDATION_READY → VALIDATION_RUN | An unconsumed preregistration exists for `target_period='validation'` matching this exact `manifest_sha256` and `impl_sha256` | No |
| VALIDATION_RUN → HOLDOUT_READY | Validation run completed; primary metric met the pre-registered threshold in the pre-registered direction | No |
| HOLDOUT_READY → HOLDOUT_RUN | A holdout preregistration **and** a `holdout_unlocks` row for the family. The unlock insert is irreversible and the family UNIQUE constraint means it can never happen twice. | **Yes** |
| HOLDOUT_RUN → FORWARD_TESTING | Holdout run completed and met its criterion | No |
| any → RETIRED | A stated reason | **Yes** |

**Forbidden transitions, rejected with a specific error:**
- Any skip forward (DRAFT → VALIDATION_RUN, DEVELOPMENT → HOLDOUT_RUN, and so on).
- Any backward transition. If a strategy needs changing after a validation run, that is a **new strategy version** with a new manifest and a `parent_strategy_version_id`. The lineage is preserved; the evidence is not inherited.
- RETIRED → anything.
- A second HOLDOUT_UNLOCKED for the same family — blocked at the database level, not in application logic.

The critical design consequence: **you cannot fix a strategy after seeing its holdout result and keep the result.** The new version starts at DRAFT, and its family has already spent its single unlock. That is the intended cost.

## K.3 Hypothesis template (Stage 2.5)

`hypotheses/HYP-{nnn}-{slug}.md`. Nine fields, target completion time under two minutes.

```markdown
# HYP-004 — Asian compression precedes larger London range
family: FAM-002
created_utc: 2026-10-14T08:55:00Z
post_hoc: true

## Observation
R2 pooled shows the bottom pre-London quintile has a London range median
about 0.15 ATR above the top quintile. Present in 11 of 16 years.

## Proposed relationship
Lower pre-London range percentile → higher subsequent London range.

## Scope
GBP/USD, 08:00–11:00 London, trading days only, 2007–2022.

## Possible mechanism
Suppressed overnight participation leaves resting orders unfilled; the
London auction clears them. Speculative — not required to be true.

## Expected direction
Positive: London range increases as pre-London percentile decreases.

## Falsifier
If the bottom-quintile London range median is not above the top quintile
in validation (2019–2022), with a bootstrap 95% CI excluding zero
difference, the hypothesis is abandoned.

## Confounders
Volatility regime clustering across years; scheduled events at 09:30 London;
the 2020 regime; seasonality in the ATR normaliser.

## Variables required
pre_london_range_pct, london_range, atr_bars, session_state

## Post-hoc?
Yes — generated from R2, which I had already looked at.
```

`post_hoc: true` is not a confession; it is a required epistemic tag and it will be true for nearly every early hypothesis. It appears on every downstream report for that family. A post-hoc hypothesis needs a stricter validation threshold, and the report says so.

## K.4 Pre-registration ritual

Resolution of the carried-forward open question. Target: under two minutes.

```
> jarvis preregister --strategy pre_london_break__baseline.v3 --period validation

  Strategy      pre_london_break__baseline v3   (manifest a3f1… impl 7c02…)
  Family        FAM-002  "Pre-London compression effects"
  Hypothesis    HYP-004
  Dataset       DSV-007 (sealed)   Code  9d41… (clean)
  Family runs to date: 41 development, 6 robustness, 0 validation

  ? Primary metric                    [expectancy_r]      ⏎
  ? Expected direction                (positive/negative) positive ⏎
  ? Threshold (bootstrap 95% lower bound must exceed)     0.05 ⏎
  ? Abandonment criterion (one line)
    > LB <= 0 or fewer than 80 trades in validation ⏎
  ? Confirm. This is immutable and consumes nothing until you run.  (y/N) y

  PREREG-0007 created 2026-11-02T14:08:31Z
  Frozen: manifest a3f1…  impl 7c02…  dataset DSV-007  code 9d41…
  Validation is now permitted for this exact strategy version.
  Note: 41 prior development runs in this family. With Šidák adjustment at
  alpha=0.05 across 41 tests, the equivalent per-test alpha is 0.00125.
  This is reported alongside your result.
```

Five prompts, four of them defaulted or single-token. Everything else is inferred from the strategy version and the ledger.

Semantics:
- The preregistration freezes four hashes. If any differs at run time, the validation run is refused.
- A preregistration is consumed by exactly one run (enforced by `preregistration_consumption`'s primary key). A failed or halted run consumes it too — you cannot retry a validation with the same registration after seeing a partial result.
- Multiple preregistrations for the same strategy version are permitted but every one is permanently visible, and the count appears on every report. Registering five times and reporting the best is possible, and will be conspicuous.
- `family_runs_at_prereg` snapshots the multiple-testing state at registration, so the adjustment cannot be recalculated later on a smaller denominator.

## K.5 Multiple-testing accounting

**What counts as one test:** one row in `experiment_runs` with `run_class IN ('development','validation','holdout')`. Describe runs, Stage 0 probes and robustness runs do not count toward the confirmatory budget; robustness is an attack on a strategy, not a search for one.

**What constitutes a family:** a `hypothesis_family_id`. A family is the set of strategy versions sharing a core claim. The rule for whether a new idea joins an existing family or starts a new one: *if the new version would be abandoned by the same falsifier as the existing family, it is the same family.* Starting a fresh family to reset the counter is possible and is visible in the ledger, because families record their creation timestamp and their first strategy version's parent.

**Sweep counting:** a sweep of 40 parameter combinations writes 40 runs sharing a `sweep_id`, and increments the family count by 40. Not by 1. The CLI prints the resulting family total before executing a sweep and requires confirmation above 20 combinations.

**Reporting:** every experiment report shows, in the header:
```
Family FAM-002 · confirmatory runs to date: 41 · this run: #42
Unadjusted 95% CI on expectancy_r: [0.02, 0.31]
Šidák-adjusted 95% CI (m=42):      [-0.07, 0.40]
```
Both intervals, always, adjacent. The adjusted interval uses `alpha_adj = 1 − (1 − 0.05)^(1/m)`. This is conservative when runs are correlated (parameter sweeps are highly correlated), and that conservatism is stated in the report footnote rather than corrected away — an accurate correction for correlated tests requires assumptions this project has no basis to make.

**The anti-amnesia rule:** no report may display a result without its family run count. `reporting` refuses to render an experiment report if `family_runs` is unavailable.

---

# PART L — VAULT SPECIFICATION

## L.1 Periods

`config/periods.yaml`, the single definition, read by `vault.PERIODS` and by nothing else:

```yaml
periods:
  development: { start: "2007-01-01", end: "2018-12-31" }
  validation:  { start: "2019-01-01", end: "2022-12-31" }
  holdout:     { start: "2023-01-01", end: null }      # null = present
timezone: UTC
boundary_semantics: "half-open on the end date at 00:00 UTC of the following day"
```

## L.2 Query classification

Every read of market data passes through `vault.GatedReader`, constructed with an explicit `QueryClass`:

| QueryClass | May read | Description |
|---|---|---|
| `DESCRIPTIVE_PRE_VAULT` | dev + validation | Stage 2 reports, feature computation for description |
| `REGIME_CHECK` | vault, **coarse aggregates only** | The narrow carve-out, Part L.4 |
| `DEVELOPMENT` | dev only | Development experiment runs |
| `VALIDATION` | dev + validation | Validation runs; requires a live preregistration |
| `HOLDOUT` | all | Requires an unlock record for the family |

Mechanically:
- `GatedReader.read_bars(start, end)` intersects the requested range with the class's permitted range. **If the intersection is smaller than the request, it raises `VaultViolation` rather than silently truncating.** Silent truncation would produce a plausible, wrong answer, which is worse than a crash.
- The reader is constructed with `family_id` where relevant, and checks `holdout_unlocks` at construction, not at read time.
- Every construction and every violation writes to `audit_log`.

## L.3 Permitted vs forbidden — the mechanical rule

A query is **descriptive** if it references none of: entry price, exit price, order, fill, position, stop, target, R, PnL, expectancy, win rate, profit factor, drawdown, MAE, MFE, or any ordering of outcomes by profitability.

A query is a **strategy evaluation** if it references any of them.

This is enforced structurally rather than by string inspection: the modules that compute those quantities (`execution`, `backtest`, `statistics`, `robustness`) have no import path to a `REGIME_CHECK` or `DESCRIPTIVE_PRE_VAULT` reader. The reader factory refuses to hand a vault-capable reader to any caller whose module is in the forbidden set, checked via the call stack at construction. Indirect leakage — computing features on vault data and passing the frame to `statistics` — is blocked because `FeatureFrame` carries the `QueryClass` and permitted period of the reader that produced it, and `statistics` rejects frames whose provenance does not match its run's period.

## L.4 The regime-check carve-out

This is the resolution of PDLA-03: it preserves the recent-regime visibility the Product Bible wanted while removing the hypothesis-generation channel.

`jarvis vault regime-check` produces **only** the following, and nothing else:
- Annual median and IQR of `london_range`, `pre_london_range`, `new_york_range`, in ATR units and price units.
- Annual median and IQR of `rv_60m` by session.
- Annual median `spread_twa` by session.
- Annual count of admissible trading days.

**One row per year per session. No conditioning on any other variable. No cross-tabs. No distributions beyond median and IQR. No per-day, per-week or per-hour breakdown.**

The distinction is exactly this: *"is the market currently more or less volatile than the period I researched"* is a calibration question that does not suggest a strategy. *"what is the London range conditional on the pre-London percentile in 2024"* is a hypothesis generator. The first is permitted; the second is not.

Every regime check writes an `audit_log` entry. The regime-check report itself is watermarked and states that it is not evidence about any strategy.

## L.5 Unlock

```
> jarvis vault unlock --family FAM-002 --prereg PREREG-0011

  ┌──────────────────────────────────────────────────────────────┐
  │  IRREVERSIBLE                                                │
  │                                                              │
  │  Family FAM-002 has never unlocked the holdout.              │
  │  This is the only unlock this family will ever receive.      │
  │                                                              │
  │  Strategy version   pre_london_break__baseline v3            │
  │  Confirmatory runs  47 development, 1 validation             │
  │  Preregistration    PREREG-0011 (holdout)                    │
  │  Primary metric     expectancy_r, positive, LB > 0.05        │
  │  Abandonment        LB <= 0 or n < 80                        │
  │                                                              │
  │  After this, every report for FAM-002 will permanently       │
  │  display that the holdout has been consumed. Any revision    │
  │  to this strategy will start a new version whose holdout     │
  │  evidence cannot be obtained.                                │
  └──────────────────────────────────────────────────────────────┘

  Type the family id to confirm: FAM-002
  Justification (recorded permanently): ____
```

The record is written before any holdout data is read. If the subsequent run crashes, the unlock still stands.

## L.6 Watermarking

Once `holdout_unlocks` contains a row for a family, `reporting` prepends to **every** subsequent report for that family:

```
╔════════════════════════════════════════════════════════════════╗
║  HOLDOUT CONSUMED — FAM-002 unlocked 2026-12-03T11:22:04Z      ║
║  Results below for this family are no longer holdout-naive.    ║
║  Strategy version at unlock: pre_london_break__baseline v3     ║
╚════════════════════════════════════════════════════════════════╝
```

This applies to development reports for new versions in the same family, robustness reports, and forward-test reports. There is no way to render a report for that family without it.

---

*Part 2 ends. Part 3 covers Backtesting execution semantics, R and risk, Statistics, Robustness, the Edge definition and Forward-test reconciliation.*
