# JARVIS TECHNICAL BIBLE — Part 4 of 4
## Parts Q–Z: CLI · Reporting · Testing · Integrity · Roadmap · Credit Plan · Work Packages · Amendments · Open Questions · Adversarial Review · Ready Check

---

# PART Q — CLI SPECIFICATION

One entry point: `jarvis`. Typer-based. Exit codes: `0` success, `1` user error, `2` HALT (integrity), `3` gate not met.

Every command that reads market data or writes to the ledger begins with the same preamble: check git tree cleanliness, resolve the code version, resolve the dataset version, construct the appropriate `GatedReader`. Failure at any step is a HALT before work begins, never mid-run.

| Command | Arguments | Output | Side effects | Errors |
|---|---|---|---|---|
| `jarvis data fetch` | `--from --to [--force-refetch]` | Progress, missing-hour summary | Writes `data/raw/` | Network failure after retries → recorded, not fatal |
| `jarvis data parse` | `--from --to` | Row counts per partition | Writes `data/tick/` | Malformed blob → quarantine + HALT for that file |
| `jarvis data resample` | `--from --to` | Bar counts, absent-minute counts | Writes `data/bars_1m/` | Missing tick partition → HALT |
| `jarvis data validate` | `--dataset DSV-nnn` | QA report path, ERROR/WARN/INFO counts | Writes `reports/qa/` | — |
| `jarvis data seal` | `--dataset DSV-nnn` | Manifest path, hashes | Writes manifest, inserts `dataset_versions` | QA errors > 0 → refused |
| `jarvis data status` | — | Coverage table, sealed versions, gaps | None | — |
| `jarvis features build` | `--dataset --feature-set --from --to` | Feature frame path, null counts | Writes feature cache | Leakage self-check failure → HALT |
| `jarvis stage0 probe` | `--instrument --contexts --widen` | Gate decision + report path | Writes run rows, report | Insufficient admissible years → exit 3 |
| `jarvis describe run` | `--report R1\|R2\|R5 [--year]` | Report path | Writes `reports/describe/`, a `describe` run row | Vault range requested → HALT |
| `jarvis hypothesis new` | `--family` | Template path | Creates `hypotheses/HYP-nnn-*.md` | — |
| `jarvis hypothesis register` | `--file` | HYP id | Inserts `hypotheses` row + file hash | Missing required section → error |
| `jarvis strategy validate` | `--manifest` | Validation report; writes `impl_sha256` back | Updates manifest's hash field only if unreferenced | Referenced-and-changed → refused |
| `jarvis strategy scan` | `--strategy --period` | Opportunity counts by day/direction/rejection | Writes `opportunities` | Lookahead detected → HALT |
| `jarvis experiment run` | `--strategy --period [--sweep FILE]` | Result summary + report path | Writes run, opportunities, trades | Dirty tree, unsealed data, vault violation → HALT |
| `jarvis experiment compare` | `--runs RUN-a,RUN-b` | Side-by-side diff of manifests, parameters, metrics | None | — |
| `jarvis experiment list` | `--family --class` | Table with family run counts | None | — |
| `jarvis robustness run` | `--run RUN-x --tests RT-1,…` | Verdicts table | Writes `robustness_runs` | — |
| `jarvis preregister` | `--strategy --period` | PREREG id, frozen hashes | Inserts `preregistrations` | Uncommitted changes → refused |
| `jarvis lifecycle transition` | `--strategy --to STATE --reason` | New state | Inserts `state_transitions` | Illegal transition → error with the legal set |
| `jarvis vault status` | — | Periods, unlocks by family, regime-check history | None | — |
| `jarvis vault regime-check` | `--from-year` | Coarse aggregates report (Part 2 §L.4) | Writes audit entry + report | — |
| `jarvis vault unlock` | `--family --prereg` | Unlock record | Inserts `holdout_unlocks`, irreversible | Second unlock for family → refused at DB level |
| `jarvis forward opportunities` | `--strategy --day` | Day's opportunities | Writes opportunities + theoretical trades | — |
| `jarvis forward log` | `--day` | Interactive prompts | Writes `forward_records` | Day not yet scanned → refused |
| `jarvis forward reconcile` | `--strategy --from --to` | Reconciliation report | Writes report | Days with missing logs → listed, excluded |
| `jarvis audit verify` | — | Chain status, first broken seq if any | None | Broken chain → exit 2 |
| `jarvis backup run` | — | Archive path, sizes | Writes archive | — |
| `jarvis doctor` | — | Environment, deps lock, tzdata version, disk, dataset integrity | None | — |

Deliberately absent: any `optimise`, `search`, `tune`, `best`, `auto` command. There is no code path from a result to a parameter choice.

---

# PART R — REPORTING SPECIFICATION

## R.1 Format decision (resolves a carried-forward open question)

**Markdown plus Parquet/CSV sidecars. No HTML generator in V1, and probably never.**

Reasoning: reports are read by one person on one machine, are consumed in a terminal or a notebook, need to be diffable in git-adjacent workflows, and need to be pasteable into a chat window — which is now the primary route to AI assistance since F-30 removed the in-product AI layer. Markdown satisfies all four. HTML satisfies none of them better and costs a rendering pipeline, a template system and a styling decision. Charts are produced in notebooks from the Parquet sidecars, where they can be iterated on interactively.

Every report emits three artefacts with a shared stem: `.md` (human), `.parquet` (the numbers), `.json` (the full provenance block).

## R.2 Universal report header

Every report, without exception, opens with:

```
# {Report title}
{WATERMARK}
Generated   2026-11-02T14:22:09Z
Dataset     DSV-007 (research, sealed, GBP/USD, 2007-01-01 → 2022-12-31)
Code        9d41f2c (clean)   tzdata 2026a   deps a71c…
Sessions    fx_core v1        Features v1    Exec default v1
Period      development (2007-01-01 → 2018-12-31)
```

Plus, where applicable: family run count, evidence tier, ambiguity fraction, holdout-consumed banner.

## R.3 Per-stage report contents

| Report | Watermark | Leads with |
|---|---|---|
| Stage 0 probe | `EXPLORATORY — FREQUENCY ONLY — NOT EVIDENCE OF PREDICTIVE VALUE` | The gate decision and its arithmetic |
| Data QA | `DATA INTEGRITY` | ERROR count, then WARNINGs by class, then the missing-hour list |
| Market description | `DESCRIPTIVE — EXPLORATORY — NOT EVIDENCE` | The question the report answers, then the table, with per-year adjacent to pooled |
| Experiment | Evidence tier | Tier, `n`, family run count; then expectancy with both raw and adjusted intervals; then the funnel; then distributions |
| Strategy comparison | Evidence tier of each | Manifest diff first, metric diff second |
| Robustness | `ATTACK RESULTS` | Verdict table, then each test |
| Validation | Evidence tier + preregistration id | The pre-registered threshold, the pre-registered direction, then the result. In that order, so the bar is visible before the number. |
| Holdout | Evidence tier + unlock banner | Same ordering |
| Forward reconciliation | `FORWARD` | The opportunity funnel and the error decomposition, **before** any PnL |
| Regime check | `CALIBRATION ONLY — NOT EVIDENCE ABOUT ANY STRATEGY` | Annual aggregates only |

## R.4 Prohibitions

- No report may display a metric without `n`.
- No report may display an experiment result without its family run count.
- No report may display a bootstrap interval computed on `n < 40`.
- No report may rank strategies by expectancy without displaying each one's evidence tier and family run count adjacent to the number.
- No report may use the words "proven," "confirmed edge," "validated strategy," or "profitable system." Enforced by a vocabulary lint over report templates.

---

# PART S — TESTING SPECIFICATION

Six tiers. A milestone is not done until its tier-appropriate tests pass.

## S.1 Unit tests
Standard coverage of pure functions: timestamp conversion, session membership, each feature, each order type's trigger and fill, R arithmetic, each statistic. Target: every public function in L1 and L2 has at least one.

## S.2 Property tests (Hypothesis)
- Timestamp round-trip across all three zones over 2007–2026 (Part 1 §E.7 T13).
- `trading_day` monotone in `ns` (T14).
- Resampling: for any tick batch, `bid_l ≤ bid_o,bid_c ≤ bid_h` and the same for ask; `tick_count ≥ 1`.
- R arithmetic: for any fill prices, `r_gross` sign matches the direction of price movement relative to entry.
- Fill monotonicity: increasing slippage never improves `r_net`.

## S.3 Synthetic-market tests
Hand-built tick sequences, a few dozen ticks each, where the answer is derivable by hand. Each is a named fixture with an asserted exact outcome.

| Fixture | Construction | Asserted |
|---|---|---|
| `SM-01 stop_before_target` | Price drifts down through the stop, then up through the target | `exit_reason='stop'`, `r_net ≈ −1`, `ambiguity_resolved_by='unambiguous'` |
| `SM-02 target_before_stop` | Reverse | `exit_reason='target'`, `r_net ≈ +2 − spread effect` |
| `SM-03 both_in_same_minute_stop_first` | Within one minute, ticks hit stop at seq 4 and target at seq 9 | `exit_reason='stop'`, `resolved_by='tick'` |
| `SM-04 both_in_same_minute_target_first` | Target at seq 3, stop at seq 8 | `exit_reason='target'`, `resolved_by='tick'` |
| `SM-05 ambiguous_no_ticks` | Bar shows both reachable; tick partition absent | `exit_reason='stop'`, `resolved_by='pessimistic'` |
| `SM-06 spread_side_check` | Long entry; bid touches the stop but ask does not; then price recovers | Long stop **is** triggered (stop is a sell, evaluated on bid). Catches the side-of-book bug. |
| `SM-07 limit_touch_no_trade_through` | Ask exactly equals the buy-limit level and never goes below | **No fill** |
| `SM-08 limit_trade_through` | Ask goes one point below | Fill at the limit price |
| `SM-09 gap_through_stop` | 90-minute data gap; first post-gap tick is 3R beyond the stop | `r_net < −2.5`, `gap_exit=True`, slippage × `gap_multiplier` |
| `SM-10 weekend_gap` | Friday close to Sunday open, position open | `exit_reason='session_end'` if configured, else gap fill |
| `SM-11 spread_widening` | Spread widens 10× at the fill instant | Fill reflects the wide spread; `spread_at_signal` recorded |
| `SM-12 dst_spring_forward` | Session spanning 2023-03-26 02:00 Europe/London | Window resolves per fold policy; bar count matches expectation |
| `SM-13 dst_us_eu_offset_window` | 2023-03-15, inside the two-week US/EU divergence | London 08:00 maps to NY 03:00 |
| `SM-14 missing_minute` | Bar absent between two present bars | Feature windows null out; order waits |
| `SM-15 entry_never_fills` | Entry stop never reached within `max_bars_to_fill` | `entry_filled=0`, `exit_reason='no_fill'`, opportunity still counted |
| `SM-16 stop_and_target_equal` | Manifest sets them equal | Rejected at validation, before any run |

## S.4 Golden tests
A tracked fixture dataset — one month of real GBP/USD ticks, roughly 40 MB, committed to `tests/fixtures/` via Git LFS or, preferably, a downsampled 5 MB slice — plus two frozen strategy manifests, plus their expected output hashes. Any change to execution semantics that alters a golden result must be an explicit, reviewed change.

## S.5 Reproducibility tests
Same dataset, manifest, impl, exec config and seed → identical `result_sha256`. Run twice in the same process and once in a fresh process, to catch state leakage.

## S.6 Leakage tests
The most important tier.

1. **Feature truncation test (auto-generated for every registered feature).** Compute over `bars[0:n]` and over `bars[0:n+50]`; assert the first `n` values are identical. Registering a feature registers this test.
2. **Session-terminal test.** For every `session_terminal` feature, assert it is null at every bar before the session window's end.
3. **Scan truncation test.** Instrument `BarView`/`FeatureView` to raise on any index > `i`; run every registered strategy over the golden fixture; assert zero raises.
4. **Shuffled-future test.** Randomise all bars after index `i`; assert every `scan` output at indices ≤ `i` is unchanged. This catches leakage through any path the truncation guard misses.
5. **Percentile-window test.** For `pre_london_range_pct`, assert today's value is unchanged when today's own range is altered — proving today is excluded from its own reference distribution.

## S.7 Vault tests
1. `DEVELOPMENT` reader raises `VaultViolation` on any request touching 2019 or later.
2. `VALIDATION` reader raises on 2023 or later.
3. `HOLDOUT` reader without an unlock row raises.
4. A partially-overlapping request raises rather than truncating.
5. `statistics` rejects a `FeatureFrame` whose provenance period does not match the run's period.
6. `describe` cannot obtain a vault-capable reader (constructor capability test).
7. `regime-check` output contains no column outside the permitted whitelist.

## S.8 Integrity tests
1. `UPDATE` and `DELETE` on every append-only table raise `SQLITE_CONSTRAINT`.
2. Direct row modification via a raw connection breaks `audit verify` at the expected sequence number.
3. A second `holdout_unlocks` insert for a family fails on the UNIQUE constraint.
4. A preregistration cannot be consumed twice.
5. An illegal lifecycle transition is rejected and the rejection is audited.
6. A dirty git tree causes `experiment run` to exit 2 before touching data.
7. Editing a referenced `impl` file causes the next run to HALT on hash mismatch.

## S.9 Architecture tests
`import-linter` contracts asserting every layer rule and every prohibited dependency from Part 1 §B.3, run in CI. A violation fails the build. This is what keeps the purity and single-reader rules real over a year of changes.

---

# PART T — INTEGRITY, FAILURE, LOGGING, BACKUP, PERFORMANCE, SECURITY, DOCS

## T.1 HALT conditions

The system refuses to run rather than produce a questionable result. HALT (exit 2), no partial write, audited:

- Dirty git working tree on any command that writes a run.
- Dataset hash mismatch against its manifest.
- Unsealed or QA-ERROR dataset version requested by research code.
- `impl_sha256` mismatch against the manifest.
- Manifest fails schema validation, or contains a prohibited field (`spread` cost, stop movement).
- Lookahead detected by the runtime guard.
- Any `VaultViolation`.
- Missing provenance: no `code_versions` row obtainable.
- Illegal lifecycle transition.
- Unknown strategy version or session set version.
- `bid > ask` encountered at read time.
- No execution config resolvable for a backtest.
- Audit chain broken (any command; verified lazily at startup by comparing the last row hash).

## T.2 WARNING conditions

Recorded and displayed prominently; the run proceeds:

- QA warnings on the dataset in use.
- `n` below a metric's threshold.
- Ambiguity fraction above 25%.
- Family run count above 50 at preregistration time.
- Feature nulls exceeding 5% of rows in the scan window.
- Missing hours within the requested period below the ERROR threshold.
- Bootstrap block length `L` above 5 (heavy clustering; effective sample much smaller than `n`).

## T.3 Logging and audit

- **Operational log:** `logs/jarvis-{date}.log`, structured JSON lines, rotating, 90-day retention. Not evidence.
- **Audit log:** the hash-chained `audit_log` table. Never rotated, never truncated, retained for the life of the project. Events: dataset sealed, run started/completed/halted, preregistration created, preregistration consumed, vault reader constructed, vault violation, regime check performed, unlock issued, lifecycle transition, manifest validated, backup completed.
- Every audit payload includes the code version and the wall-clock UTC timestamp from the OS, with the understanding that a single-user system cannot have a trusted timestamp. This is stated in the docs rather than papered over.

## T.4 Backup and recovery

Research history is irreplaceable; market data is re-downloadable. The backup strategy reflects that asymmetry.

**Tier 1 — irreplaceable, backed up nightly, three copies.**
`ledger/jarvis.sqlite` (via `VACUUM INTO` to get a consistent snapshot, never a file copy of a live WAL database), `data/manifests/`, `strategies/`, `hypotheses/`, `config/`, the git repository including all history.
Destination: local external drive + one cloud object store, both encrypted with age or GPG. The key lives in a password manager, not in the repo.

**Tier 2 — expensive but reproducible, backed up weekly if space allows.**
`data/bars_1m/` (small, ~300 MB — worth backing up).

**Tier 3 — not backed up.**
`data/raw/`, `data/tick/`, `reports/`, feature caches. All regenerable, and `data/raw/` is 6–8 GB.

**Recovery procedure, tested monthly by `jarvis doctor --restore-test`:**
1. Restore git repo → code, config, manifests, strategies, hypotheses.
2. Restore ledger snapshot → `jarvis audit verify` must pass from genesis.
3. `jarvis data fetch` + `parse` + `resample` from manifest coverage.
4. `jarvis data validate` and compare partition hashes against the restored manifests. Any mismatch means Dukascopy revised history since the original ingest — which is expected occasionally, and is why this check exists. The correct response is a **new dataset version**, not overwriting the old one, and any experiment referencing the old version is thereafter unreproducible and marked as such.

That last point is the honest limit of the reproducibility guarantee, and it should be documented rather than hidden.

## T.5 Performance and dependencies

**Size estimates (GBP/USD, 2007–2022, 16 years):**

| Artefact | Estimate |
|---|---|
| Raw `.bi5` | 6–8 GB |
| Tick Parquet (ZSTD-3) | 10–14 GB |
| 1-minute bars Parquet | 250–350 MB |
| Feature cache per feature set | 300–600 MB |
| Ledger after ~2,000 experiments | < 2 GB |
| **Total working set** | **20–25 GB**, comfortably inside the available 50–200 GB |

**Runtime estimates on a mid-range Windows laptop:**

| Operation | Estimate |
|---|---|
| Full fetch, 16 years | 8–20 hours, network-bound, resumable |
| Parse + resample, 16 years | 30–90 minutes |
| Feature build, 19 features | 2–5 minutes |
| Opportunity scan, 16 years | 30 seconds – 3 minutes if vectorised; 10–30 minutes if `scan` is called per bar in pure Python |
| Backtest, ~600 opportunities | seconds, plus tick loads for ambiguous bars |
| Stationary bootstrap, 10k resamples | seconds |

The scan is the one real performance question. **Decision: keep `scan` as a per-bar Python callable.** It is 10–30 minutes for a full-history run, which is acceptable for a research tool run a few times a day, and it keeps strategy code simple, readable and easy to reason about for a user who is learning. Vectorising the scan interface would trade the project's core value — the user understanding their own strategy — for speed nobody needs. If it becomes painful, the fix is to restrict scanning to the session window (a 3-hour window is 12% of the data), which is a 10-line change.

**Dependency allowlist.** Anything not on this list requires a decision-log entry.

| Package | Purpose | Why not something else |
|---|---|---|
| `polars` | Ingest, resample, feature computation | Faster than pandas, stricter null semantics, no index confusion |
| `duckdb` | Ad-hoc analytical queries over Parquet, notebooks | SQL over Parquet without a server |
| `pyarrow` | Parquet I/O | Required by both above |
| `numpy` | Statistics kernels | — |
| `scipy` | Wilson intervals, distributions | — |
| `pydantic` v2 | Manifest and config validation | Schema errors become clear messages, not tracebacks |
| `typer` + `rich` | CLI | — |
| `pyyaml` | Manifests, session sets | — |
| `tzdata` | IANA database, pinned | Windows has no system zoneinfo |
| `pytest`, `hypothesis` | Testing | — |
| `import-linter` | Architecture tests | — |
| `matplotlib` | Notebook charts only | Not imported by any `src/jarvis` module outside `reporting` |
| `requests` | Dukascopy fetch | — |

**Excluded and why:** pandas (only at the notebook edge, never in `src/`), scikit-learn and all ML (F-32), any web framework (F-27, F-35), any LLM SDK (F-30), any broker SDK (F-31), TA-Lib (features are hand-written so their leakage properties are known).

## T.6 Security

Single-user, local, no secrets in V1 — so no security theatre.

- The vault encryption key (if the vault is stored encrypted at rest) lives outside the repo, in the user's password manager. **Recommendation: do not encrypt the vault Parquet files.** Encryption gives no protection against the only adversary — the user — and adds a failure mode where the key is lost and the holdout is destroyed. The vault's protection is the `GatedReader` and the permanent unlock record, not cryptography. This is an explicit, arguable choice and it is in the decision log.
- `data/raw/` and sealed Parquet directories set read-only after sealing (`attrib +R` on Windows) as accidental-deletion protection, not as security.
- The ledger is backed up before every schema migration, and migrations are forward-only with a recorded version.
- No API keys exist in V1. If any are added later, they go in an OS keyring, never a file, and `jarvis doctor` checks for accidental key-shaped strings in the repo.

## T.7 Documentation set

Seven documents, each the single home of its truth. Duplication is the failure mode.

| File | Owns | Never contains |
|---|---|---|
| `PRODUCT_BIBLE.md` | Frozen product decisions, objectives, exclusions, roadmap shape, decision log | Technical detail |
| `TECHNICAL_SPEC.md` | Parts A–C: architecture, modules, repository (this document, Part 1) | Product rationale |
| `DATA_CONTRACT.md` | Parts D–E: schemas, manifests, time and sessions | Strategy semantics |
| `STRATEGY_SPEC.md` | Parts F, H, I: features, manifest, scan interface, opportunity ontology | Execution rules |
| `EXECUTION_SEMANTICS.md` | Part M: fills, ambiguity, gaps, R | Statistics |
| `RESEARCH_PROCESS.md` | Parts G, J, K, L, N, O: description, ledger, lifecycle, vault, statistics, robustness, evidence tiers | Implementation detail |
| `TESTING.md` | Part S | — |
| `DECISION_LOG.md` | Every decision and amendment, chronological | Anything not a decision |

`PRODUCT_BIBLE.md` and `DECISION_LOG.md` are the only two a future reader must read to understand *why*. The rest explain *what*.

---

# PART U — IMPLEMENTATION ROADMAP

Because of PDLA-01, "Stage 0" is not a single script; it is a vertical slice through the data layer plus a probe. The milestones below reflect that.

Legend for the credit column: **P** = premium coding model, **S** = standard model, **M** = manual/deterministic (no AI needed).

## Stage 0 — Feasibility slice

| ID | Objective | Prereq | Deliverable | Acceptance | Complexity | Credit |
|---|---|---|---|---|---|---|
| 0A | Repo scaffold, `core`, config loading, hashing, error tree, `pyproject`, import-linter contracts | — | Package skeleton that imports and passes an empty test suite | `pytest` green; `lint-imports` green; `jarvis doctor` runs | Low | **S** |
| 0B | Dukascopy fetcher: URL construction, retry with backoff, raw archive, resumability | 0A | `jarvis data fetch` | Fetches one known week; zero-byte hours recorded as `empty`; re-run is a no-op; **month index is zero-based** (explicit test on a known January file) | Low-Med | **S** |
| 0C | `.bi5` parser → tick Parquet | 0B | `jarvis data parse` | Golden test: a known hour decodes to a known row count and known first/last prices; `(ts, seq)` total order preserved | Med | **S** |
| 0D | Time engine | 0A | `timeengine` module | **All 15 acceptance tests in Part 1 §E.7 pass**, including T8 (historical US DST rules) and T13 (round-trip property over 10⁶ instants) | High | **P** |
| 0E | Session engine, versioned session sets | 0D | `sessions` module, `fx_core.v1.yaml` | T9–T12 pass; membership and window queries correct across both DST divergence windows; zero numeric offset literals (T15) | High | **P** |
| 0F | Tick → 1-minute resampler | 0C, 0D | `jarvis data resample` | Deterministic byte-identical output on repeat; absent minutes produce no row; property tests in §S.2 pass | Med | **S** |
| 0G | QA check suite | 0F | `jarvis data validate` | Every check in Part 1 §D.6 implemented with a synthetic positive case | Med | **S** |
| 0H | Minimal feature subset: `atr_bars`, `pre_london_high/low/range`, `pre_london_range_pct`, `rv_60m` | 0E, 0F | `features` module (partial) | Auto-generated leakage test passes for each; session-terminal nulling verified; percentile-window test (§S.6.5) passes | High | **P** |
| 0I | Stage 0 probe: four contexts, counting rules, gate procedure, report | 0G, 0H | `jarvis stage0 probe` | Reproduces hand-checked counts on a one-month fixture; gate arithmetic matches Part 2 §G.0.5; report carries the watermark | Med | **S** |

**Stage 0 exit:** a gate decision artefact — `PROCEED GBP/USD`, `WIDEN CONTEXT`, or `CONSIDER EUR/USD FALLBACK` — with its arithmetic shown. This becomes a decision-log entry.

## Stage 1 — Data layer completion

| ID | Objective | Credit |
|---|---|---|
| 1A | Full 2007–2022 ingest run (operational, not code) | **M** |
| 1B | Dataset manifests, hashing, sealing, `dataset_versions` | **S** |
| 1C | `provenance`: git state, code versions, dirty-tree enforcement | **S** |
| 1D | Ledger bootstrap: DDL, append-only triggers, hash-chained audit log | **P** |
| 1E | `vault`: periods, `GatedReader`, capability-based construction, regime-check command | **P** |

## Stage 2 — Market description

| ID | Objective | Credit |
|---|---|---|
| 2A | Remaining features to complete the 19 | **S** |
| 2B | Bootstrap and interval utilities (shared with Stage 5) | **P** |
| 2C | Reports R1, R2, R5 (per AR-6) | **S** |
| 2D | Event calendar as a static curated CSV + exclusion flag (per AR-4) | **S** |
| 2E | Reporting framework: header, watermarks, sidecars, vocabulary lint | **S** |
| 2F | Reports R3, R4, R6 — **only after the user has used R1/R2/R5** | **S** |

## Stage 2.5 — Human hypothesis generation. No engineering.

## Stage 3 — Strategy and experiment infrastructure

| ID | Objective | Credit |
|---|---|---|
| 3A | Manifest schema, pydantic validation, prohibited-field rejection | **S** |
| 3B | `ScanContext`, truncated views, `LookaheadError` guard, purity enforcement | **P** |
| 3C | Opportunity scanner, restrictions, `opportunities` materialisation | **P** |
| 3D | Lifecycle state machine, `state_transitions`, illegal-transition rejection | **P** |
| 3E | Preregistration CLI and semantics, consumption guard | **S** |
| 3F | Multiple-testing counters and report integration | **S** |
| 3G | Leakage test harness (auto-generation, shuffled-future test) | **P** |

## Stage 4 — Backtester

| ID | Objective | Credit |
|---|---|---|
| 4A | Order types, trigger and fill semantics, side-of-book correctness | **P** |
| 4B | Slippage model, fitting procedure, versioning | **P** |
| 4C | Ambiguity resolver with tick fallback and pessimistic branch | **P** |
| 4D | Gap handling, weekend semantics | **P** |
| 4E | Backtest orchestration, R computation, MAE/MFE | **S** |
| 4F | Synthetic-market fixtures SM-01…SM-16 | **P** |
| 4G | Golden tests and reproducibility harness | **S** |

## Stage 5 — Statistics and robustness

| ID | Objective | Credit |
|---|---|---|
| 5A | Core metrics, Wilson, drawdown distribution | **S** |
| 5B | Stationary bootstrap, Politis–White block selection, BCa | **P** |
| 5C | Evidence tier computation | **P** |
| 5D | Robustness harness | **S** |
| 5E | RT-1…RT-8 | **S** |
| 5F | RT-9, RT-10 placebo and random-timing baselines | **P** |
| 5G | RT-11, RT-12 | **S** |

## Stage 6 — Forward reconciliation

| ID | Objective | Credit |
|---|---|---|
| 6A | `forward opportunities`, ordering safeguard | **S** |
| 6B | `forward log` interactive CLI | **S** |
| 6C | Error decomposition and reconciliation report | **S** |
| 6D | Backup, restore test, `jarvis doctor` completion | **S** |

---

# PART V — PREMIUM CREDIT PLAN

**Premium milestones: 16 of 44.** Every one of them is premium for the same reason — a subtle error produces a plausible wrong number rather than a crash.

| Stage | Premium milestones | Share of premium effort |
|---|---|---|
| 0 | 0D, 0E, 0H | 14% |
| 1 | 1D, 1E | 9% |
| 2 | 2B | 4% |
| 3 | 3B, 3C, 3D, 3G | 24% |
| 4 | 4A, 4B, 4C, 4D, 4F | 34% |
| 5 | 5B, 5C, 5F | 12% |
| Reserve | Debugging across stages | 3% |

Stages 3 and 4 together take 58%, close to the Product Bible's 55% target, with the time engine and leakage work taking the 14% it allocated to Stage 1 correctness.

**Never premium:** report templating, CLI argument plumbing, chart code, CSV and Parquet writing, the fetcher's retry loop, manifest serialisation, notebook scaffolding, documentation, the forward-log prompts, the backup script. These are roughly 60% of the total line count and none of them can produce a wrong research answer.

**The test for premium allocation:** *if this component is subtly wrong, does the system crash or does it lie?* Crash → standard. Lie → premium.

---

# PART W — CODING WORK PACKAGE TEMPLATE

```markdown
# WP-{nnn} — {Title}

## Task ID
WP-{nnn}   Stage {n}{X}   Credit class: {PREMIUM | STANDARD}

## Objective
{One paragraph. What exists at the end that does not exist now.}

## Product Bible references
{Frozen decision IDs, e.g. F-16, F-18, F-19}

## Technical Spec references
{Exact part and section numbers, e.g. Part 3 §M.3, §M.6}

## Existing repository state
{What is already built and can be relied on. What is stubbed. What does not exist yet.}

## Files allowed to change
{Explicit list. Anything not listed is out of bounds.}

## Files forbidden to change
{Explicit list, especially: config/, existing schemas, other modules' public interfaces,
 anything under data/ or ledger/}

## Required interfaces
{Exact signatures, dataclasses, and types. Copy them from the spec verbatim.}

## Required behaviour
{Numbered, testable statements. Each maps to at least one acceptance test.}

## Edge cases that must be handled
{Enumerated, with the required behaviour for each.}

## Acceptance criteria
{Objective, checkable. "Test X passes" not "works correctly".}

## Tests that MUST pass
{Named tests, including pre-existing ones that must not regress.}

## Commands to run
```
pytest tests/unit/test_{x}.py -v
pytest tests/synthetic/ -v
lint-imports
jarvis {command} {args}
```

## Expected output
{Literal expected stdout or file contents where feasible.}

## Things the coding model must NOT do
- Must not add dependencies outside the allowlist in Part 4 §T.5
- Must not change any public interface outside this package
- Must not modify config/ or any versioned schema
- Must not "improve" behaviour specified here — if the spec looks wrong, stop and report
- Must not add error suppression, broad `except`, or default fallbacks not specified here
- Must not add caching, parallelism or optimisation unless this package asks for it
- Must not write to data/, ledger/ or reports/ outside the paths named here

## Definition of done
All acceptance tests pass, `lint-imports` passes, no new dependencies,
no changes outside the allowed file list, and a one-paragraph note stating
every assumption made where the spec was silent.
```

That last clause is the one that catches architecture drift. A coding model that had to guess will say so if asked directly, and those notes are the review's starting point.

---

# PART X — DECISION-LOG AMENDMENTS

Six proposed amendments. **Each needs a yes or no.** I have not applied any of them to the Product Bible; the spec above is written assuming the recommended resolution, and if any is rejected the corresponding sections need revision.

---

### PDLA-01 — Stage 0 sequencing

**Existing frozen decision.** Roadmap: Stage 0 is the feasibility probe ("ingest GBP/USD, count context occurrences per year"), preceding Stage 1 (the data layer: ingest, hashing, manifests, resampling, session engine).

**Problem.** Stage 0's deliverable requires ingest, tick parsing, resampling, a timezone-correct session engine and several features — which are Stage 1's and Stage 2's deliverables. As sequenced, Stage 0 cannot be built.

**Reasoning.** Any shortcut that avoids the dependency makes the probe untrustworthy. Counting Asian-range compression events without a correct DST-aware session engine gives a wrong count for roughly four weeks a year, in the specific weeks where London and New York diverge — and the whole purpose of the probe is a count.

**Proposed replacement.** Stage 0 is redefined as a **vertical slice** comprising milestones 0A–0I (Part U): the fetcher, parser, resampler, time and session engines, five features, and the probe. Stage 1 is redefined as *completing* the data layer — full-history ingest, manifests, sealing, provenance, ledger bootstrap and the vault — rather than building it from nothing.

**What it invalidates.** The Product Bible's roadmap section, which characterises Stage 0 as "cheap, routine work, no premium credits." Stage 0 now contains three premium milestones (0D, 0E, 0H) and is roughly two to three weeks of work rather than two days. This is a real cost increase and should be accepted knowingly.

**Blocks engineering?** **Yes — Stage 0 cannot begin without this.**

---

### PDLA-02 — The edge definition double-counts costs

**Existing frozen decision.** "Edge: positive expectancy in R whose bootstrap 95% lower bound exceeds modelled costs, surviving year-by-year segmentation and a placebo/random-entry baseline."

**Problem.** F-16 puts spread inside the fill prices and the cost model puts commission and slippage inside `r_net`. Expectancy is therefore already net of costs. Requiring the lower bound to additionally exceed costs subtracts them twice and would reject genuine effects.

**Reasoning.** The intent behind the original wording is clearly "require margin above costs, not merely survival of them." That intent is better served by a stress requirement than by a second subtraction.

**Proposed replacement.** *An edge is a positive expectancy in R, already net of modelled costs, whose stationary-bootstrap 95% lower bound exceeds zero, which survives year-by-year segmentation, exceeds its placebo baseline, and whose lower bound remains above zero under 1.5× cost and slippage stress.*

**What it invalidates.** The `Definitions` section of the Product Bible. Nothing built.

**Blocks engineering?** No — blocks Stage 5C (evidence tiers).

---

### PDLA-03 — Descriptive vault access is a hypothesis-generation channel

**Existing frozen decision.** "Vault permits descriptive queries only... You can ask 'what's the distribution of London range in 2024' or 'has Asian-session volatility shifted.'"

**Problem.** The Stage 2 description engine produces precisely the conditional distributions from which hypotheses are generated. Running those over vault years fits the hypothesis to the holdout through the user's own reasoning — the one contamination path no software control can detect afterwards.

**Reasoning.** The Product Bible's stated purpose for the carve-out is closing the recent-regime blind spot: *is the market now like the market I studied?* That question is answered by coarse annual aggregates. Conditional distributions answer a different question and carry the risk.

**Proposed replacement.** Vault access is restricted to a fixed, enumerated set of **coarse annual aggregates** (Part 2 §L.4): annual median and IQR of session ranges, realised volatility and spread, plus admissible day counts. One row per year per session. No conditioning on any other variable, no cross-tabs, no sub-daily breakdown. All Stage 2 conditional reports run on 2007–2022 only. Additionally, Stage 0 runs on 2007–2022 only, since its output can change the instrument choice.

**What it invalidates.** The illustrative example in the Product Bible's holdout discussion ("distribution of London range in 2024" is now too fine-grained; the annual median and IQR remain available).

**Blocks engineering?** No — blocks Stage 1E (vault) and Stage 2.

---

### PDLA-04 — Spread is specified twice

**Existing frozen decision.** F-05 stores real bid and ask (so spread is observed), and Doc 1 §I5 specifies a modelled empirical spread distribution in the cost model.

**Problem.** Applying both charges spread twice, roughly doubling the cost drag and rejecting real effects.

**Proposed replacement.** Spread is charged exactly once, by the bid/ask price convention. The cost model contains commission and slippage only, and the manifest validator rejects any spread cost field. The empirical spread distribution is retained but repurposed: it is a **descriptive** output (report R5) and the basis for the **slippage** scaling factor, not an additive cost.

**What it invalidates.** Doc 1 §I5's characterisation of the cost model. Nothing built.

**Blocks engineering?** No — blocks Stage 4.

---

### PDLA-05 — The R denominator is under-specified

**Existing frozen decision.** "R = entry-to-stop distance, defined per-strategy."

**Problem.** Silent on whether the distance is measured at the intended entry or the actual fill. Using the actual fill lets entry slippage widen the denominator, which shrinks reported R losses and flatters every result — an error that makes a strategy look better precisely when execution was worst.

**Proposed replacement.** `r_denominator = |entry_reference_price − stop_reference_price|`, captured at signal time on the `opportunity` row, copied to the trade, never recomputed. All slippage therefore appears in the numerator only.

**What it invalidates.** Nothing. This is a specification of intent, but it is consequential enough to warrant an explicit ruling rather than a silent choice.

**Blocks engineering?** No — blocks Stage 3C and Stage 4.

---

### PDLA-06 — The ~40 events/year gate lacks an object

**Existing frozen decision.** "Gate: if under ~40/year, widen the specialisation or switch pair."

**Problem.** The gate does not say what is being counted. Context frequency and final setup frequency differ by a factor of two to four, because a directional filter and an entry trigger typically remove 50–75% of context instances. Applying a 40/year gate to *context* frequency would pass specialisations that can only ever produce 10–20 tradeable setups a year, which cannot reach a usable holdout sample.

**Proposed replacement.** The gate is stated against the final setup and translated into a two-tier context gate: `PROCEED` requires the narrowest measured context intersection to have a median annual count ≥ 100 **and** a 10th-percentile annual count ≥ 60. Counts between 40 and 100 trigger one widening attempt. Below 40 after widening, the EUR/USD comparison runs. Full procedure in Part 2 §G.0.5.

**What it invalidates.** The numeric gate in the Product Bible's Stage 0 description. Note the practical consequence: this gate is **harder to pass**, and there is a real chance Stage 0 returns `WIDEN CONTEXT` and forces a less narrow specialisation than originally imagined. That is the gate doing its job.

**Blocks engineering?** **Yes — Stage 0's decision procedure depends on it.**

---

# PART Y — REMAINING OPEN QUESTIONS

| # | Question | Status | When |
|---|---|---|---|
| Y1 | Pre-registration CLI ritual | **Resolved** — Part 2 §K.4 | — |
| Y2 | HTML reports after Stage 5 | **Resolved: no.** Markdown + Parquet sidecars + notebooks. Part 4 §R.1 | — |
| Y3 | Event-calendar source | **Resolved: static curated CSV**, hand-assembled once for BoE, Fed, NFP, CPI, GDP releases, versioned as a dataset. No scraping, preserving the offline-by-default property. Used as an exclusion flag and a robustness segmentation, never as a predictive feature in V1. | Stage 2D |
| Y4 | Broker export schema | **Deferred.** `forward_records` and `dataset_versions.role='execution'` exist; the importer waits until a broker account exists. No schema change anticipated. | Stage 6+ |
| Y5 | Slippage model calibration constants | Deferred to 4B, where they are fitted from data rather than chosen | Stage 4B |
| Y6 | Whether to widen the instrument set after Stage 5 | Deferred. Requires a decision-log entry and probably a new Product Bible version | Post-Stage 5 |
| Y7 | Stop movement and partial exits | Deliberately excluded from V1 (Part 3 §M.10). Revisit only with evidence | Post-Stage 5 |
| Y8 | Vault encryption at rest | **Recommended: no.** The adversary is the user; encryption adds a key-loss failure mode without adding protection. Part 4 §T.6 | Ratify now |

---

# PART Z — ADVERSARIAL REVIEW AND READY CHECK

## Z.1 Five-perspective attack, and the resulting changes

**Quant researcher: "how does this generate false evidence?"**
The dev set is used twice — once to generate the hypothesis (Stage 2 reports) and again to test it (Stage 3 development runs). Every hypothesis in this project's first year will be post-hoc from data it is then tested on. That is the largest single source of false evidence in the design and no amount of pre-registration fixes it, because pre-registration happens after the observation.
→ **Change AR-1:** development is reported in two segments. Stage 2 descriptive reports run on **2007–2014**. Development experiment runs still use 2007–2018, but every development report must show 2015–2018 as a separate segment alongside the pooled result, labelled *"period not seen during hypothesis generation."* An effect present in 2007–2014 and absent in 2015–2018 is flagged and cannot reach PROMISING. This costs nothing and catches the most likely failure.

**Statistician: "how does repeated testing fool us?"**
Šidák correction over all family runs treats 40 correlated sweep runs as 40 independent tests, which is far too strict and will cause real effects to be discarded. But correcting properly for correlated tests requires assumptions this project cannot justify.
→ **Change AR-2:** report three intervals, not two: unadjusted, Šidák over distinct `sweep_id`s (a lower bound on strictness), and Šidák over all runs (an upper bound). The truth lies between the two adjusted bounds, and showing the bracket is more honest than picking one. The evidence tier uses the **conservative** bound, so the tier is never generous, but the reader sees the range.

**FX execution specialist: "how will historical fills differ from reality?"**
Dukascopy is an aggregated institutional feed. A retail broker's spread is materially wider, especially in the first minutes of the London open — precisely the window this project intends to trade. A backtest on Dukascopy spreads is systematically optimistic for a retail account. Separately, no event calendar means strategies may be harvesting scheduled-release moves that are untradeable in practice because spreads blow out.
→ **Change AR-3:** every experiment report shows the ×1.5 spread-stress result as a **co-equal headline figure** beside the base case, not buried in the robustness section. The base case is labelled *"institutional feed"* and the stressed case *"retail-realistic estimate."*
→ **Change AR-4:** the event calendar is promoted from deferred to a Stage 2 deliverable (2D), used as an exclusion flag and a mandatory robustness segmentation ("does the edge survive removing scheduled-event days?").

**Software engineer: "where do bugs silently corrupt results?"**
The single-reader rule was specified as enforced by call-stack inspection at reader construction. Stack inspection is fragile, hard to test, and breaks under any refactor that adds a wrapper.
→ **Change AR-5:** replace it with a capability token. `GatedReader.__init__` is private; readers are minted only by `experiments.RunContext.reader(query_class)`, which derives the permitted period from the run's own `period` column. Modules receive a reader as an argument and cannot construct one. This is testable, refactor-proof, and makes the vault boundary a type-level property rather than a runtime heuristic.

**Product manager: "where are we overengineering?"**
Six market description reports before the user has read any of them is speculative. Twelve robustness tests before a single strategy exists is speculative. Both risk building things that are never used.
→ **Change AR-6:** Stage 2 ships **three** reports (R1 range anatomy, R2 conditional London range, R5 spread climate). R3, R4 and R6 move to Stage 2F, built only after the first three have been used and found wanting. Robustness ships as six mandatory tests at Stage 5E and the remainder at 5G. This removes roughly a week of speculative build from the critical path.

All six changes are incorporated into the specification above.

## Z.2 What this design still cannot protect against

Stated plainly, because a research instrument that overstates its own guarantees is the failure it exists to prevent:

1. **Hypothesis generation from data the hypothesis is then tested on.** AR-1 mitigates; nothing eliminates it.
2. **The user unsealing the vault by reading Parquet directly.** The vault is a commitment device. Its force is the permanent unlock record, not access control.
3. **Feed divergence.** Until the execution dataset exists, every result is about Dukascopy's market, not a broker's.
4. **Regime change.** Sixteen years of history says nothing certain about 2027.
5. **Survivorship in the user's own attention.** The system counts experiment runs, not the ideas abandoned before they became runs.
6. **A small effective sample.** If the bootstrap block length comes back large, `n = 200` trades may carry the information of 30. The system reports this; it cannot fix it.

## Z.3 Ready-to-build determination

**Status: NOT READY — two blockers, both requiring a one-word answer from you.**

| Blocker | What is needed |
|---|---|
| **PDLA-01** | Ratify redefining Stage 0 as a vertical slice (0A–0I), accepting that it now contains three premium milestones and is roughly two to three weeks rather than two days. Without this, Stage 0 has no buildable definition. |
| **PDLA-06** | Ratify the two-tier gate (median ≥ 100 and P10 ≥ 60 on the narrowest context intersection). Without this, Stage 0 has no decision procedure, and the number chosen changes whether the project proceeds on GBP/USD at all. |

PDLA-02, 03, 04 and 05 should also be ratified, but they block Stages 2, 4 and 5 rather than Stage 0 and can be settled while Stage 0 is being built. AR-1 through AR-6 are within my authority as elaborations of frozen decisions and are already incorporated; they are recorded here for visibility, not for approval.

**On ratification of PDLA-01 and PDLA-06, the status becomes READY TO BUILD STAGE 0**, and work package WP-000 (attached separately) is the first task. WP-001 through WP-008 covering milestones 0B–0I follow the same template and will be written once WP-000 is reviewed — deliberately, so that the first package's review can correct the template before eight more are written against it.

---

*End of Technical Bible.*
