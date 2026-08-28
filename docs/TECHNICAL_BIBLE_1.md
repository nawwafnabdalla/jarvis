# JARVIS TECHNICAL BIBLE — Part 1 of 4
## Parts A–E: Frozen Contract · System Architecture · Repository · Data · Time & Sessions

Companion documents:
- Part 2 — Features, Market Description, Strategy, Opportunity, Experiment Ledger, Lifecycle, Vault (F–L)
- Part 3 — Backtesting, Statistics, Robustness, Forward Test (M–P)
- Part 4 — CLI, Reporting, Testing, Integrity, Roadmap, Credit Plan, Work Package Template, Amendments, Open Questions, Ready Check (Q–Z)

Source of truth: `PRODUCT_BIBLE.md` (Doc 2, frozen 2026-08-27). This document elaborates; it does not override. Six proposed amendments are recorded in Part 4 §X. **PDLA-01 and PDLA-06 must be ratified before Stage 0 can begin. PDLA-02, 03, 04 and 05 must be ratified before the stages they affect (2, 4 and 5) but do not block Stage 0.**

---

# PART A — FROZEN CONTRACT

## A.1 Binding decisions restated

These are extracted verbatim in intent from the Product Bible. Every one is a constraint on implementation, not a suggestion.

| ID | Binding decision | Enforcement point in this spec |
|---|---|---|
| F-01 | GBP/USD only | `config/instruments.yaml` contains exactly one entry; CLI rejects any other symbol |
| F-02 | Stage 0 feasibility gate precedes strategy machinery | Roadmap Part 4 §U; Stage 0 produces a decision artefact |
| F-03 | Tick archive retained on disk and hashed | `data/raw/` + `dataset_manifests` table |
| F-04 | 1-minute OHLC derived from ticks at ingest | `bars` module; no external bar source permitted |
| F-05 | Bid and ask stored separately | Bar schema has 8 OHLC columns, not 4 |
| F-06 | Dukascopy is the research dataset | `ingest.dukascopy` is the only fetcher in V1 |
| F-07 | Broker execution dataset is separate and separately versioned | `dataset_versions.role ∈ {research, execution}`; schema present, no importer in V1 |
| F-08 | Development 2007–2018, Validation 2019–2022, Vault 2023–present | `vault.PERIODS`, single source of the boundaries |
| F-09 | Descriptive-only vault access before unlock | `vault.GatedReader` with query-class tagging |
| F-10 | One irreversible unlock per strategy lineage | `holdout_unlocks` table, UNIQUE on `hypothesis_family_id`, append-only trigger |
| F-11 | Opportunity scanning is pure | `Strategy.scan()` signature admits no account/position state; purity test in Part 4 §S |
| F-12 | Opportunities materialised before trades | `opportunities` table written in full before `theoretical_trades` is touched |
| F-13 | YAML manifest + Python function hybrid | Part 2 §H |
| F-14 | Git, code and data hashing on every experiment | `experiment_runs` carries `code_sha`, `manifest_sha`, `dataset_version_id` |
| F-15 | Dirty git tree prohibits execution | `provenance.require_clean_tree()` called by every run command |
| F-16 | Buys at ask, sells at bid, always | Part 3 §M |
| F-17 | Stop = first tick beyond level + slippage draw; limit = trade-through, not touch | Part 3 §M |
| F-18 | Ambiguous bars resolved from ticks | Part 3 §M.6 |
| F-19 | Pessimistic (stop-first) fallback when ticks unavailable | Part 3 §M.6 |
| F-20 | UTC nanosecond integer storage | Part E |
| F-21 | IANA timezone conversion, never hardcoded offsets | Part E; lint rule bans numeric offset literals in `sessions/` and `timeengine/` |
| F-22 | Versioned session definitions | `config/sessions/*.yaml` with `session_set_id` + `version` |
| F-23 | 17:00 America/New_York rollover | Part E.4 |
| F-24 | Windows-native Python 3.12, `pathlib` everywhere | Part 4 §T; lint rule bans `os.path.join` and string path concatenation |
| F-25 | DuckDB + Parquet for market data | Part D |
| F-26 | SQLite for the experiment ledger | Part 2 §J |
| F-27 | Local-only; no server, container or web framework | Dependency allowlist, Part 4 §T.5 |
| F-28 | Multiple-testing accounting by hypothesis family | Part 2 §K.5 |
| F-29 | Pre-registration required before any validation run | Part 2 §K.6 |
| F-30 | No AI in V1 | No LLM dependency in the allowlist; no API-key handling code |
| F-31 | No live trading, no broker write API | No order-placement module exists |
| F-32 | No ML | scikit-learn and equivalents excluded from the dependency allowlist |
| F-33 | No funded-challenge simulator | Not in the module tree |
| F-34 | No replay trainer | Not in the module tree |
| F-35 | No web application | Reports are files, not services |
| F-36 | No automatic parameter optimisation | No search/optimiser module; sweeps are explicit, enumerated and counted |

## A.2 Contradiction audit

I checked every frozen decision against every other. **Four genuine conflicts and two under-specifications were found.** They are stated here and carried into Part 4 §X as formal amendments. Nothing else in the Product Bible is internally inconsistent.

**Conflict 1 — Stage 0 requires Stage 1's deliverables.**
The Product Bible sequences Stage 0 (feasibility probe, "ingest GBP/USD, count contexts") before Stage 1 (data layer: ingest, hashing, manifests, resampling, sessions). Stage 0 cannot count context occurrences without ingested, resampled, session-aware data — which is precisely Stage 1's output. As written, Stage 0 is not buildable. → **PDLA-01.**

**Conflict 2 — the edge definition double-counts costs.**
The Product Bible defines an edge as "positive expectancy in R whose bootstrap 95% lower bound exceeds modelled costs." But F-16 puts spread inside the fill prices and the cost model puts commission and slippage inside the trade result. Expectancy is therefore already net of costs, and requiring the lower bound to exceed costs a second time subtracts them twice. → **PDLA-02.**

**Conflict 3 — descriptive vault access can leak hypotheses.**
F-09 permits descriptive queries on the vault to close the recent-regime blind spot. But the Stage 2 market description engine produces exactly the conditional distributions from which hypotheses are generated. If those run over vault years, the user's hypothesis is fitted to the holdout by the most reliable mechanism available: a human brain. The safeguard and the feature collide. → **PDLA-03.**

**Conflict 4 — spread is specified twice.**
F-05 stores real bid/ask, so spread is an observed property of the data. Doc 1 §I5 also specifies a modelled empirical spread distribution in the cost model. Applying both charges spread twice. → **PDLA-04.**

**Under-specification 1 — the R denominator.**
"R = entry-to-stop distance" does not say whether the distance is measured at the intended entry price or the actual fill. If the actual fill is used, entry slippage silently widens the denominator and *shrinks* reported R loss, flattering the strategy. → **PDLA-05** (resolved by specification, not by changing intent).

**Under-specification 2 — the ~40 events/year gate.**
The gate is stated against an unspecified object. Context frequency and setup frequency differ by a factor of two to four once a trigger and a direction filter are applied. Applying the gate to context frequency will pass strategies that cannot possibly reach a usable holdout sample. → **PDLA-06.**

No other conflicts exist. The remaining thirty frozen decisions are mutually consistent and implementable as written.

---

# PART B — SYSTEM ARCHITECTURE

## B.1 Shape

Five layers, strictly downward-depending. A module may import from its own layer and any layer below it, never above.

```
┌──────────────────────────────────────────────────────────────┐
│  L5  PRESENTATION      cli · reporting · notebooks           │
├──────────────────────────────────────────────────────────────┤
│  L4  RESEARCH          describe · backtest · statistics ·    │
│                        robustness · forward                  │
├──────────────────────────────────────────────────────────────┤
│  L3  RESEARCH CONTROL  experiments · vault                   │
├──────────────────────────────────────────────────────────────┤
│  L2  DOMAIN            features · strategies · opportunities │
│                        · execution                           │
├──────────────────────────────────────────────────────────────┤
│  L1  FOUNDATION        core · provenance · timeengine ·      │
│                        sessions · ingest · bars · qa         │
└──────────────────────────────────────────────────────────────┘
```

Two rules do most of the integrity work and both are testable:

**The single-reader rule.** No module outside `bars` and `vault` may open a Parquet file or issue a DuckDB query against `data/`. Every read of market data by anything at L2 or above goes through `vault.GatedReader`, which knows the period boundaries and the caller's query class. This makes vault leakage a structural impossibility rather than a discipline. Enforced by an import-linter contract and a filesystem-access test.

**The purity rule.** `features`, `strategies` and `opportunities` may not import `experiments`, `statistics`, `robustness`, `backtest` or `reporting`. A strategy therefore cannot see its own results, cannot see how many times it has been run, and cannot branch on account state. Enforced by import-linter.

## B.2 Data flow, end to end

```
Dukascopy .bi5 ──┐
                 │ ingest.fetch          (network, retry, byte-hash)
      data/raw/ ─┤
                 │ ingest.parse          (LZMA, 20-byte records)
   ticks.parquet ┤
                 │ bars.resample         (deterministic, UTC-aligned)
  bars_1m.parquet┤
                 │ qa.run                (ERROR / WARNING / INFO)
                 │ provenance.seal       (manifest + SHA-256 + dataset_version)
                 ▼
          ┌──────────────┐
          │ GatedReader  │◄── vault.PERIODS, query class, unlock records
          └──────┬───────┘
                 │  bars (period-restricted)
                 ▼
          features.compute ──► FeatureFrame (immutable, hashed)
                 │
                 ├──► describe.*          (Stage 2 reports)
                 │
                 └──► strategies.scan ──► opportunities table
                                              │
                                              ▼
                                     execution.simulate
                                     (fills, slippage, ambiguity)
                                              │
                                              ▼
                                     theoretical_trades table
                                              │
                            ┌─────────────────┼─────────────────┐
                            ▼                 ▼                 ▼
                      statistics.*     robustness.*      forward.reconcile
                            │                 │                 │
                            └────────► reporting.* ◄────────────┘
                                              │
                                              ▼
                                    reports/*.md + *.parquet
```

Everything that writes to the ledger routes through `experiments.ledger`, which is the only module holding a SQLite connection.

## B.3 Module contracts

Format: **responsibility · public interface · depends on · must not depend on · failure modes.**

### L1 — Foundation

**`core`**
Types, IDs, canonical JSON, hashing, error hierarchy, config loading. No I/O beyond reading `config/`.
Interface: `sha256_canonical(obj) -> str`, `new_id(prefix) -> str`, `JarvisError` tree, `Instrument`, `Price`, `Nanos`.
Depends on: nothing.
Failure: malformed config → `ConfigError` at import of the CLI, never mid-run.

**`provenance`**
Git state, code hashing, dataset manifests, dataset version registration.
Interface: `require_clean_tree() -> GitState`, `code_sha() -> str`, `write_manifest(m: DatasetManifest) -> Path`, `register_dataset_version(...) -> DatasetVersionId`, `verify_dataset(dvid) -> VerifyResult`.
Depends on: `core`.
Must not depend on: anything else. It is called first by everything.
Failure: dirty tree → `DirtyTreeError` (HALT). Hash mismatch → `ProvenanceError` (HALT).

**`timeengine`**
UTC nanosecond arithmetic, IANA conversion, trading day and week assignment.
Interface: `to_utc_ns(dt) -> int`, `local(ns, tz) -> datetime`, `trading_day(ns) -> date`, `trading_week(ns) -> WeekId`, `is_weekend_gap(ns) -> bool`.
Depends on: `core`.
Prohibited: any numeric UTC offset literal; `datetime.utcnow`; naive datetimes crossing a module boundary.
Failure: ambiguous or non-existent local time during a DST transition → explicit `AmbiguousTimeError` unless a fold policy is supplied.

**`sessions`**
Versioned session definitions and membership queries.
Interface: `load_session_set(id, version) -> SessionSet`, `SessionSet.membership(ns) -> frozenset[str]`, `SessionSet.window(name, trading_day) -> (start_ns, end_ns)`.
Depends on: `core`, `timeengine`.
Failure: undefined session name → `SessionError` (HALT). Partial session (Sunday open, Friday close, holiday) → returns window with `partial=True`; callers must decide.

**`ingest`**
Dukascopy fetch, decompress, parse, normalise to tick Parquet.
Interface: `fetch_hour(instrument, ns_hour) -> RawBlob`, `parse_blob(blob) -> TickBatch`, `ingest_range(start, end) -> IngestReport`.
Depends on: `core`, `provenance`, `timeengine`.
Must not depend on: `bars`, `qa`, or anything above.
Failure: HTTP failure after retries → recorded as `missing_hour`, run continues, summarised at end. Malformed record length → `ParseError` (HALT for that file, quarantine, continue).

**`bars`**
Tick → 1-minute resampling and the only read path to market Parquet.
Interface: `resample(tick_batch) -> BarBatch`, `read_bars(start_ns, end_ns, columns) -> pl.DataFrame`, `read_ticks(start_ns, end_ns) -> pl.DataFrame`.
Depends on: `core`, `timeengine`, `provenance`.
Must not depend on: `vault` (would be circular — `vault` wraps `bars`).
Failure: requested range not covered by any sealed dataset version → `DataCoverageError` (HALT).

**`qa`**
Data quality checks with severity classification.
Interface: `run_checks(dataset_version_id) -> QAReport`.
Depends on: `core`, `bars`, `timeengine`, `sessions`.
Failure: any ERROR finding → dataset version is not sealed; it cannot be used by research.

### L2 — Domain

**`features`**
Deterministic feature library. Pure functions over bar frames.
Interface: `FEATURES: dict[str, FeatureDef]`, `compute(names, bars, session_set) -> FeatureFrame`.
Depends on: `core`, `timeengine`, `sessions`.
Must not depend on: `bars` directly (receives frames), `vault`, or anything at L3+.
Failure: insufficient lookback → leading rows are null, never zero, never forward-filled.

**`strategies`**
Manifest schema, loader, registry, `Strategy` protocol.
Interface: `load(path) -> StrategyVersion`, `validate(manifest) -> ValidationReport`, `REGISTRY: dict[str, ScanFn]`.
Depends on: `core`, `features`, `sessions`.
Must not depend on: `execution`, `experiments`, `statistics`, `vault`.

**`opportunities`**
Runs `scan` over every bar and materialises candidates.
Interface: `scan_all(strategy_version, feature_frame) -> OpportunityBatch`.
Depends on: `core`, `strategies`, `features`.

**`execution`**
Fill semantics and slippage. Knows nothing about strategies or statistics.
Interface: `simulate(orders, bars, ticks, exec_config) -> FillResult`, `SlippageModel`.
Depends on: `core`, `timeengine`.
Must not depend on: `strategies`, `opportunities`.

### L3 — Research control

**`vault`**
Period boundaries, query classification, gated reads, unlock records.
Interface: `PERIODS`, `GatedReader(query_class, family_id)`, `unlock(family_id, justification) -> UnlockRecord`, `status() -> VaultStatus`.
Depends on: `core`, `bars`, `experiments.ledger` (for unlock records only).
Failure: forbidden read → `VaultViolation` (HALT, logged to audit).

**`experiments`**
Ledger, state machine, pre-registration, multiple-testing counters, audit chain.
Interface: `ledger.connect()`, `lifecycle.transition(...)`, `prereg.create(...)`, `counters.family_run_count(family_id)`.
Depends on: `core`, `provenance`.
Sole holder of the SQLite connection.

### L4 — Research

**`describe`** (Stage 2 reports), **`backtest`** (orchestrates opportunities → execution → trades), **`statistics`**, **`robustness`**, **`forward`**. Each depends downward only, and each writes results through `experiments.ledger`.

### L5 — Presentation

**`reporting`** (Markdown/Parquet/CSV emitters, watermarking), **`cli`** (Typer app, argument parsing, exit codes). Nothing imports these.

---

# PART C — REPOSITORY ARCHITECTURE

## C.1 Tree

```
jarvis/
├── pyproject.toml
├── README.md
├── .gitignore
├── .importlinter                    # layer contracts, enforced in CI
├── config/
│   ├── instruments.yaml             # GBP/USD only
│   ├── sessions/
│   │   └── fx_core.v1.yaml
│   ├── periods.yaml                 # dev/validation/vault boundaries
│   └── execution/
│       └── default.v1.yaml          # commission, slippage model params
├── src/jarvis/
│   ├── core/ provenance/ timeengine/ sessions/ ingest/ bars/ qa/
│   ├── features/ strategies/ opportunities/ execution/
│   ├── vault/ experiments/
│   ├── describe/ backtest/ statistics/ robustness/ forward/
│   ├── reporting/ cli/
│   └── strategy_impls/              # user Python scan functions, hashed
├── data/                            # gitignored except manifests
│   ├── raw/ticks/GBPUSD/YYYY/MM/DD/HHh_ticks.bi5
│   ├── tick/instrument=GBPUSD/year=YYYY/month=MM/part-*.parquet
│   ├── bars_1m/instrument=GBPUSD/year=YYYY/part-*.parquet
│   └── manifests/                   # TRACKED — small JSON, provenance
├── ledger/
│   └── jarvis.sqlite                # gitignored, backed up separately
├── hypotheses/                      # tracked Markdown, one per hypothesis
├── strategies/                      # tracked YAML manifests
├── notebooks/
├── reports/                         # gitignored, regenerable
├── tests/
│   ├── unit/ synthetic/ golden/ property/ integration/ architecture/
│   └── fixtures/                    # tracked, tiny, deterministic
├── scripts/
└── docs/
```

## C.2 Directory contracts

| Directory | Owns | Must NOT contain | Git | Immutable | Generated |
|---|---|---|---|---|---|
| `config/` | Canonical constants: instrument, sessions, periods, execution defaults | Strategy parameters; anything experiment-specific | Yes | Versioned — new version = new file, old files never edited | No |
| `src/jarvis/` | All application code | Data, results, notebooks, secrets | Yes | No | No |
| `src/jarvis/strategy_impls/` | User `scan` functions | Manifests; parameters; any I/O | Yes | Once referenced by a sealed experiment, the file must not be edited — edit creates a new file with a new version suffix | No |
| `data/raw/` | Untouched Dukascopy blobs | Anything derived | No | Yes, byte-for-byte | Downloaded |
| `data/tick/`, `data/bars_1m/` | Normalised Parquet | Hand-edited rows | No | Yes after sealing | Yes |
| `data/manifests/` | Provenance JSON | Large payloads | **Yes** | Yes — append-only | Yes |
| `ledger/` | The experiment ledger | Anything else | No | Append-only by trigger | Yes |
| `hypotheses/` | Human-written hypothesis records | Results | Yes | Append-only by convention; superseding creates a new file | No |
| `strategies/` | YAML manifests | Python logic | Yes | New version = new file | No |
| `reports/` | Rendered outputs | Anything not reproducible from ledger + data | No | No | Yes |
| `tests/fixtures/` | Tiny deterministic datasets with known answers | Real market data over ~1 MB | Yes | Yes | No |

The rule behind the table: **anything that constitutes evidence is tracked or ledgered; anything regenerable is not; anything raw is immutable.**

## C.3 Naming conventions

| Object | Convention | Example |
|---|---|---|
| Session set file | `{set_id}.v{n}.yaml` | `fx_core.v1.yaml` |
| Strategy manifest | `{family}__{name}.v{n}.yaml` | `pre_london_break__baseline.v3.yaml` |
| Strategy impl module | `src/jarvis/strategy_impls/{family}/{name}_v{n}.py` | `.../pre_london_break/baseline_v3.py` |
| Hypothesis file | `hypotheses/HYP-{nnn}-{slug}.md` | `HYP-004-asian-compression.md` |
| Family ID | `FAM-{nnn}` | `FAM-002` |
| Experiment run ID | `RUN-{ULID}` | `RUN-01JB3K...` |
| Dataset version ID | `DSV-{nnn}` | `DSV-007` |
| Report file | `reports/{kind}/{id}__{utc_ts}.md` | `reports/experiment/RUN-01JB3K__20260901T101500Z.md` |
| Parquet partition | Hive style, `key=value` | `year=2019` |

All identifiers are ULIDs or zero-padded integers — never natural keys, never derived from user text.

---

# PART D — DATA SPECIFICATION

## D.1 Raw tick blob

Dukascopy serves one file per instrument-hour:

```
https://datafeed.dukascopy.com/datafeed/GBPUSD/{YYYY}/{MM0:02d}/{DD:02d}/{HH:02d}h_ticks.bi5
```

`MM0` is **zero-indexed** (January = `00`). This is the single most common ingest bug; it is called out in the Stage 1A work package acceptance criteria.

Format: LZMA-compressed stream of fixed 20-byte big-endian records.

| Offset | Type | Meaning |
|---|---|---|
| 0 | `uint32` | milliseconds since the start of the file's hour |
| 4 | `uint32` | ask, in points |
| 8 | `uint32` | bid, in points |
| 12 | `float32` | ask volume |
| 16 | `float32` | bid volume |

Points are converted with the instrument's `point_scale` (GBP/USD: `1e-5`). A zero-byte response is **valid** and means no ticks in that hour (weekend, holiday, or a genuinely dead hour); it is recorded as `empty`, not `missing`.

## D.2 Normalised tick record

Stored at `data/tick/instrument=GBPUSD/year=YYYY/month=MM/part-*.parquet`, sorted by `ts_utc_ns`, ZSTD level 3.

| Column | Type | Nullable | Semantics |
|---|---|---|---|
| `ts_utc_ns` | `int64` | No | Nanoseconds since Unix epoch, UTC. Dukascopy gives millisecond resolution; the lower six digits are zero. |
| `bid` | `float64` | No | Best bid |
| `ask` | `float64` | No | Best ask |
| `bid_volume` | `float32` | Yes | As supplied; null if source reports 0.0 for all ticks in the hour |
| `ask_volume` | `float32` | Yes | As supplied |
| `source_file_id` | `int32` | No | FK to `source_files` in the manifest, giving exact blob provenance |
| `seq` | `int32` | No | Ordinal within the source hour, preserving original order when timestamps tie |

Ties are common at millisecond resolution. `(ts_utc_ns, seq)` is the total order. **Never sort ticks by timestamp alone** — it makes fill simulation non-deterministic.

## D.3 One-minute bar record

Stored at `data/bars_1m/instrument=GBPUSD/year=YYYY/part-*.parquet`.

| Column | Type | Nullable | Semantics |
|---|---|---|---|
| `ts_utc_ns` | `int64` | No | **Start** of the minute. Interval is half-open `[t, t+60e9)`. |
| `bid_o`,`bid_h`,`bid_l`,`bid_c` | `float64` | No | OHLC of the bid series within the interval |
| `ask_o`,`ask_h`,`ask_l`,`ask_c` | `float64` | No | OHLC of the ask series within the interval |
| `tick_count` | `int32` | No | Number of ticks in the interval; always ≥ 1 for a present row |
| `first_tick_ns` | `int64` | No | Timestamp of the first tick |
| `last_tick_ns` | `int64` | No | Timestamp of the last tick |
| `spread_open` | `float64` | No | `ask_o - bid_o` |
| `spread_max` | `float64` | No | Max of `ask - bid` over ticks in the interval |
| `spread_twa` | `float64` | No | Time-weighted average spread over the interval |
| `prev_gap_ns` | `int64` | No | Nanoseconds since the last tick of the previous present bar |
| `source_file_ids` | `list[int32]` | No | Provenance; usually length 1, length 2 at hour boundaries |

### D.3.1 Deterministic aggregation rules

1. A bar exists **if and only if** at least one tick falls in `[t, t+60e9)`. Missing minutes produce **no row**. They are never zero-filled, never forward-filled, never interpolated.
2. `bid_o` is the bid of the first tick by `(ts_utc_ns, seq)`; `bid_c` is the bid of the last tick by the same order. Identically for ask.
3. `bid_h`/`bid_l` are the max/min of the **bid series only**; `ask_h`/`ask_l` of the **ask series only**. A bar's `bid_h` and `ask_h` may not come from the same tick. This is correct and intentional.
4. `spread_twa` weights each tick's spread by the interval to the next tick, with the final tick weighted to the interval end.
5. Aggregation is a pure function of the sorted tick batch. The same ticks in the same order always produce byte-identical Parquet given the same writer settings (`compression=zstd, level=3, statistics=True, row_group_size=1_000_000`, no timestamp metadata).

### D.3.2 Absent-bar semantics for consumers

Absence means "no quote activity," which during liquid hours is itself information and during the weekend is expected. Consumers must handle it explicitly:
- **Features** compute over a bar index that includes only present bars; any rolling window that spans an absence records `window_gap_ns` and nulls the feature if `window_gap_ns` exceeds the feature's tolerance.
- **Strategies** are never called on an absent minute.
- **Execution** treats an absence between two present bars as a potential gap and applies the gap rules in Part 3 §M.7.

## D.4 Dataset manifest

One JSON file per ingest operation at `data/manifests/DSV-{nnn}.json`, git-tracked.

```json
{
  "dataset_version_id": "DSV-007",
  "role": "research",
  "instrument": "GBPUSD",
  "source": "dukascopy",
  "source_url_template": "https://datafeed.dukascopy.com/datafeed/{sym}/{y}/{m0:02d}/{d:02d}/{h:02d}h_ticks.bi5",
  "coverage": { "start_utc": "2007-01-01T00:00:00Z", "end_utc": "2022-12-31T23:59:59Z" },
  "fetch": {
    "started_utc": "2026-09-01T09:00:00Z",
    "completed_utc": "2026-09-02T04:31:12Z",
    "hours_expected": 140256,
    "hours_fetched": 140251,
    "hours_empty": 39204,
    "hours_missing": 5,
    "missing_hours": ["2011-05-14T03:00:00Z", "..."]
  },
  "raw": { "total_bytes": 6821334912, "file_count": 140251, "manifest_sha256": "…" },
  "tick_parquet":  { "row_count": 998_412_331, "total_bytes": 12_004_112_004, "sha256_by_partition": { "year=2007": "…" } },
  "bars_1m_parquet": { "row_count": 5_912_004, "total_bytes": 318_004_112, "sha256_by_partition": { "year=2007": "…" } },
  "code": { "ingest_version": "1.0.0", "resample_version": "1.0.0", "git_sha": "…", "tree_clean": true },
  "timezone_assumptions": { "source_timezone": "UTC", "conversion": "none", "session_set": "fx_core.v1" },
  "qa": { "report_path": "reports/qa/DSV-007.md", "errors": 0, "warnings": 14, "info": 231 },
  "sealed": true,
  "sealed_utc": "2026-09-02T05:10:44Z",
  "manifest_sha256": "…"
}
```

`manifest_sha256` is computed over the canonical JSON of every field except itself. `provenance.verify_dataset` recomputes partition hashes and the manifest hash; any mismatch is a HALT.

A dataset version is **sealed** only when: coverage is contiguous within tolerance, QA reports zero ERRORs, all partition hashes are recorded, and the git tree was clean. Only sealed versions are readable by research code.

## D.5 Dataset roles and the execution dataset

`role ∈ {research, execution}`. F-07 requires the broker dataset to exist as a separate version. The schema is identical for bars; the manifest's `source` becomes the broker name and `timezone_assumptions.source_timezone` becomes the broker's server timezone (frequently UTC+2/UTC+3 with its own DST rules — a notorious source of error, so it is a required field, not an inferred one). No importer is built in V1. Every experiment row records exactly one `dataset_version_id`, so a strategy can later be re-run against the execution dataset and the two results compared without any schema change.

## D.6 Data QA checks

`qa.run_checks` produces findings classified ERROR (blocks sealing), WARNING (recorded, sealing allowed), INFO (recorded only).

| Check | Condition | Severity |
|---|---|---|
| Inverted quote | `bid > ask` on any tick | ERROR |
| Zero/negative spread | `ask - bid <= 0` | ERROR |
| Non-positive price | `bid <= 0 or ask <= 0` | ERROR |
| Timestamp reversal | `ts_utc_ns` decreases within a source file | ERROR |
| Malformed record | blob length not a multiple of 20 | ERROR |
| Missing hour inside a trading week | Hour absent (not empty) between Sun 17:00 NY and Fri 17:00 NY | ERROR if > 0.5% of a year's hours, else WARNING |
| Duplicate tick | Identical `(ts, bid, ask, seq)` | WARNING |
| Extreme spread | `spread_twa > 20 × ` the hour-of-week median | WARNING |
| Unrealistic jump | Mid-price move > 10 × trailing 1000-tick stdev in one tick | WARNING |
| Weekend activity | Ticks between Fri 17:05 NY and Sun 16:55 NY | WARNING (real but thin; must not enter features by default) |
| Bar coverage | Present-bar count for a trading day < 60% of the trailing 20-day median | WARNING |
| DST boundary sanity | Bar count in the hour spanning each DST transition ≠ expected | INFO |
| Volume all-zero | Every tick in a partition reports 0.0 volume | INFO |

Rationale for the missing-hour threshold: Dukascopy has known small holes in the 2007–2010 range. Treating every one as fatal makes the dataset unusable; treating none as fatal lets silent holes distort session ranges. The 0.5%-of-year threshold with per-year reporting is the compromise, and the QA report lists every missing hour so a human can decide.

---

# PART E — TIME AND SESSION SPECIFICATION

This module is the highest-consequence low-glamour component in the system. An error here does not crash; it silently attributes a two-week-per-year misalignment to a market regime.

## E.1 Representation

- Canonical instant: `int64` nanoseconds since Unix epoch, UTC. Named type `Nanos`.
- No naive `datetime` crosses a module boundary. Ever.
- No numeric UTC offset appears anywhere in `timeengine` or `sessions`. Enforced by a lint rule matching offset-shaped literals (`timedelta(hours=…)` applied to a timezone conversion, `+0100`, `UTC+`).
- Timezone data from Python's `zoneinfo` with the `tzdata` package pinned to an explicit version recorded in every dataset manifest. **A tzdata upgrade changes historical session boundaries for some zones and must therefore create a new dataset version.**

## E.2 Conversion API

```python
def to_utc_ns(dt: datetime) -> Nanos: ...          # requires tz-aware input
def from_utc_ns(ns: Nanos, tz: str) -> datetime: ...
def local_wall(ns: Nanos, tz: str) -> tuple[date, time]: ...

def local_to_utc_ns(
    d: date, t: time, tz: str,
    fold_policy: Literal["raise", "earlier", "later"] = "raise",
) -> Nanos: ...
```

`local_to_utc_ns` is where DST bites. A local wall time may be **ambiguous** (occurs twice, autumn) or **non-existent** (skipped, spring). The default is `raise`. Session definitions must declare their policy explicitly; `fx_core.v1` uses `later` for ambiguous times and `later` for non-existent times, meaning a session boundary that falls in a skipped hour moves forward to the first valid instant. This choice is recorded in the session YAML, not hardcoded.

## E.3 Trading day

```
trading_day(ns):
    local = from_utc_ns(ns, "America/New_York")
    if local.time() >= 17:00:  return local.date() + 1 day
    else:                      return local.date()
```

Consequences, all intentional:
- The trading day boundary sits at 21:00 UTC during US EDT and 22:00 UTC during US EST. It moves. This is why the offset must never be hardcoded.
- A trading day labelled Monday begins Sunday evening New York time.
- "Previous day's high" means the high over `trading_day = D-1` restricted to present bars, **not** the previous calendar date.

## E.4 Trading week

Begins at the first present bar at or after Sunday 17:00 America/New_York; ends at the last present bar before Friday 17:00 America/New_York. `WeekId` is the ISO year-week of the week's Wednesday, which avoids the year-boundary ambiguity of ISO weeks anchored on Monday.

Partial sessions:
- **Sunday open**: the trading day beginning Sunday 17:00 NY has no Asian or European history before it. Any feature with a lookback crossing the weekend gap sets `weekend_crossed=True`.
- **Friday close**: liquidity thins from roughly 16:00 NY. The last trading day of the week is flagged `partial_close=True`.
- Both are **excluded by default** from Stage 2 descriptive statistics and from opportunity scanning, via a session-set flag `exclude_partial: true`. A strategy may opt in explicitly; the manifest must say so.
- Holidays are not enumerated. They are detected empirically: a trading day whose present-bar count is below 60% of the trailing 20-day median is flagged `thin_day=True` and excluded by the same switch. This avoids maintaining a holiday calendar for two countries and catches half-days, which a calendar usually misses.

## E.5 Session definitions

`config/sessions/fx_core.v1.yaml`:

```yaml
session_set_id: fx_core
version: 1
tzdata_version: "2026a"
fold_policy:
  ambiguous: later
  nonexistent: later
exclude_partial: true
thin_day_threshold: 0.60

sessions:
  tokyo:
    tz: Asia/Tokyo
    start: "09:00"
    end: "15:00"
    note: "Tokyo cash hours. Asia/Tokyo has observed no DST since 1951."

  pre_london:
    tz: Europe/London
    start: "00:00"
    end: "08:00"
    note: >
      The 'Asian range' window as used for London-open work, expressed in
      London local time so that its relationship to the London open is
      DST-stable. This is deliberately NOT the Tokyo session.

  london:
    tz: Europe/London
    start: "08:00"
    end: "16:30"

  london_open_window:
    tz: Europe/London
    start: "08:00"
    end: "11:00"

  new_york:
    tz: America/New_York
    start: "08:00"
    end: "17:00"

  overlap_london_ny:
    derived: intersection
    of: [london, new_york]
```

**Why `pre_london` is defined in London local time.** If the Asian range were defined in Tokyo local time, the gap between its end and the London open would swing by an hour twice a year in each of two zones, producing four distinct regimes per year for a purely clerical reason. Defining it in `Europe/London` fixes its relationship to the thing it is actually measured against. `tokyo` remains available for genuinely Tokyo-anchored questions. This is a modelling decision, recorded here, and any strategy is free to use either.

Session sets are immutable. Changing any value requires `fx_core.v2.yaml`; experiments record which set and version they used.

## E.6 Membership and windows

```python
SessionSet.membership(ns) -> frozenset[str]     # e.g. {"london", "london_open_window"}
SessionSet.window(name, trading_day) -> Window  # (start_ns, end_ns, partial: bool)
```

Sessions may overlap; membership returns all matches. `window` resolves the named session for a given trading day, applying the fold policy, and marks `partial=True` when the window is truncated by the week boundary or falls on a thin day.

## E.7 Edge cases and acceptance tests

Every case below is a required test with a hardcoded expected value.

| # | Case | Expected |
|---|---|---|
| T1 | 2023-03-12, US spring forward; 2023-03-26, EU spring forward | Between these dates London−NY = 4h, not the usual 5h. `london` 08:00 London maps to 03:00 New York, not 04:00. |
| T2 | 2023-10-29 EU fall back; 2023-11-05 US fall back | Between these dates London−NY = 4h again. One week, opposite direction. |
| T3 | 2021-03-14 02:30 America/New_York | Non-existent local time → policy `later` → resolves to 03:00 EDT. |
| T4 | 2021-11-07 01:30 America/New_York | Ambiguous → policy `later` → the EST occurrence. |
| T5 | 2023-03-26 01:30 Europe/London | Non-existent → resolves to 02:00 BST. |
| T6 | Trading-day boundary on 2023-03-11 vs 2023-03-13 | 22:00 UTC before, 21:00 UTC after. |
| T7 | Asia/Tokyo across any 2023 date | Offset constant +09:00; no transition. |
| T8 | 2007-01-01 (pre-2007 US DST rule change baseline) | US DST began the first Sunday of April before 2007 and the second Sunday of March from 2007. Test 2006-04-02 and 2007-03-11 to confirm `zoneinfo` historical rules are being used, not current rules projected backward. |
| T9 | Sunday 2023-06-04 17:00 NY | First instant of trading day 2023-06-05; `partial=True`. |
| T10 | Friday 2023-06-09 17:00 NY | Excluded (half-open interval); last included instant is 16:59:59.999999999. |
| T11 | 2023-12-25 | Present-bar count far below trailing median → `thin_day=True`. |
| T12 | `pre_london` window on 2023-06-15 | 23:00 UTC 2023-06-14 → 07:00 UTC 2023-06-15 (BST). On 2023-01-15: 00:00 → 08:00 UTC (GMT). |
| T13 | Round-trip property | For 10⁶ random instants over 2007–2026 and all three zones, `to_utc_ns(from_utc_ns(ns, tz)) == ns`. |
| T14 | Monotonicity property | `trading_day` is non-decreasing in `ns` across the full dataset. |
| T15 | No-offset-literal lint | Static scan of `timeengine/` and `sessions/` finds zero numeric UTC offsets. |

T8 is the one people miss. If a library is projecting today's DST rules onto 2006, every session boundary in the first year of the development set is wrong by an hour for four weeks, and it will look like a regime.

---

*Part 1 ends. Part 2 covers Features, Market Description, Strategy Definition, the Opportunity ontology, the Experiment Ledger, Lifecycle and the Vault.*
