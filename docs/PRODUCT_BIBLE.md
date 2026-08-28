# JARVIS — PRODUCT BIBLE v1.1

**Status: READY TO BUILD STAGE 0**
Amended and re-frozen: 2026-08-28
Supersedes: Product Bible v1.0 (Doc 2, frozen 2026-08-27)
Companion: `DECISION_LOG.md` · Technical Bible Parts 1–4 · `WP-000`

Changes in v1.1 are confined to the six ratified amendments PDLA-01 through PDLA-06 and the consequential roadmap revision. Every other decision from v1.0 carries forward unchanged.

---

## Part 1 — Frozen decisions

### Product

**What V1 is:** a local, single-user, deterministic FX research engine for GBP/USD. It ingests price data, describes market behaviour statistically, lets you define strategies as machine-readable objects, backtests them with honest cost and fill modelling, and records every experiment immutably.

**What V1 is not:** it contains no AI, no live trading, and no user interface beyond a command line and Jupyter notebooks.

**The quality bar:** you can trust what it tells you, reproduce the result, and understand why. A beautiful wrong backtest is a failed product.

### The twelve critical decisions

| # | Decision | Frozen as | v1.1 |
|---|---|---|---|
| C1 | Instrument | **GBP/USD only.** Fallback to EUR/USD only via the Stage 0 gate procedure. | unchanged |
| C2 | Data granularity | **1-minute OHLC, bid and ask stored separately**, derived at ingest from tick data. Raw tick archive retained and hashed, used to resolve ambiguous bars. | unchanged |
| C3 | Data source | **Dukascopy** for research history. Broker export as a second, separately versioned dataset for execution reality. Every ingest writes a manifest with source, URL, fetch timestamp, byte count, SHA-256. | unchanged |
| C4 | History & holdout | Development **2007–2018**. Validation **2019–2022**. **Sealed vault 2023–present.** Vault access limited to coarse annual aggregates (see C4b). Strategy evaluation on vault data requires an irreversible unlock, one per hypothesis family, permanently recorded and displayed. | **amended — PDLA-03** |
| **C4b** | **Vault access scope** | **Vault reads are restricted to a fixed enumerated set of coarse annual aggregates: annual median and IQR of session ranges, realised volatility and spread, plus admissible day counts. One row per year per session. No conditioning on any other variable, no cross-tabs, no sub-daily breakdown.** All Stage 2 conditional reports run on 2007–2022 only. Stage 0 runs on 2007–2022 only. | **new — PDLA-03** |
| C5 | "Opportunity" | A strategy exposes a pure `scan(ctx) -> Candidate \| None`, evaluated on every bar, with no reference to account state, position state, or user availability. Candidates are materialised into an `opportunities` table before any filtering. Trades are a subset with a link back. | unchanged |
| C6 | Strategy language | **Hybrid.** YAML manifest holds identity, parameters, costs, sizing, sessions, restrictions. Logic lives in named Python functions in a versioned package. Manifest hash and implementation SHA-256 recorded on every run. Engine refuses to run with a dirty git tree. Parameters live only in the manifest. | unchanged |
| C7 | Execution semantics | Buys at ask, sells at bid, always. Stops fill at first tick beyond the level plus slippage drawn from a distribution. Limits require trade-through, not touch. Same-minute stop/target resolved from the tick archive; pessimistic (stop-first) fallback otherwise. Every trade carries `ambiguity_resolved_by`; every report states the ambiguity fraction. | unchanged |
| **C7b** | **Cost accounting** | **Spread is charged exactly once, by the bid/ask price convention. There is no additive spread cost.** The cost model contains commission and slippage only; the manifest validator rejects any spread cost field. The empirical spread distribution is retained as a descriptive output and as the scaling basis for the slippage model. | **new — PDLA-04** |
| **C7c** | **R denominator** | **`r_denominator = \|entry_reference_price − stop_reference_price\|`, captured at signal time on the opportunity row, copied to the trade, never recomputed from actual fills.** All slippage therefore appears in the numerator only and can never flatter a result. | **new — PDLA-05** |
| C8 | Time & sessions | UTC nanosecond integers. All session logic in IANA zones, converted at query time — never hardcoded offsets. Sessions are named, versioned config objects. Trading week bounded by 17:00 America/New_York. Sunday-open, Friday-close and thin days flagged and excluded by default. | unchanged |
| C9 | Environment | **Windows, local, single machine.** Native Python 3.12 in a venv, not WSL. DuckDB + Parquet for market data, SQLite for the ledger. All paths via `pathlib`. No server, containers or web framework. | unchanged |
| C10 | Multiple-testing | Every backtest touching development data writes an immutable run row tagged with a hypothesis family. A 40-combination sweep counts as 40. Validation runs require a prior pre-registration containing hypothesis, metric, threshold, direction and abandonment criterion, enforced by the engine, capped at ~2 minutes of friction. Reports show unadjusted and adjusted intervals side by side. | unchanged; ritual specified in Technical Bible Part 2 §K.4 |
| C11 | AI layer | **Excluded from V1 entirely.** No LLM calls, no API keys, no budget controls. Every experiment record must be summarisable to a paste-sized block. | unchanged |
| C12 | Exclusions | No live trading or broker write API. No ML. No funded-challenge modelling. No multi-pair or multi-asset. No replay trainer. No web UI. No auto-optimisation or automated parameter search. No portfolio or correlation analysis. No knowledge-state tracker, Socratic mode, or mistake classifier. | unchanged |

### User profile

Age 20. Fundamentals known, has traded FX and crypto, held a funded account at 18 without a system. **No candidate setups — genuine blank slate.** A-level maths, comfortable with distributions and Poisson from gambling contexts. Roughly one hour a day, variable, more when engaged.

Two binding consequences: market description precedes strategy machinery, and any ritual heavier than ~2 minutes will be routed around and therefore protects nothing.

---

## Part 2 — Roadmap (revised under PDLA-01)

**Stage 0 — Feasibility slice.** No longer a script. A vertical slice through the data layer: repository scaffold, Dukascopy fetcher, tick parser, time engine, session engine, resampler, QA checks, five features, and the probe with its gate. Nine milestones (0A–0I), three of them premium. **Estimated two to three weeks.**
*Exit:* a gate decision — `PROCEED GBP/USD`, `WIDEN CONTEXT`, or `CONSIDER EUR/USD FALLBACK` — with its arithmetic shown, recorded in the decision log.

**Stage 1 — Data layer completion.** Full 2007–2022 ingest, manifests, hashing, sealing, provenance, ledger bootstrap with append-only triggers and hash-chained audit, vault gating.

**Stage 2 — Market description.** Remaining features, bootstrap utilities, reports R1/R2/R5 first, event calendar as an exclusion flag, reporting framework. R3/R4/R6 only after the first three have been used.

**Stage 2.5 — Human hypothesis generation.** No engineering.

**Stage 3 — Strategy and experiment infrastructure.** Manifest validation, scan context with lookahead guards, opportunity scanner, lifecycle state machine, pre-registration, multiple-testing counters, leakage test harness.

**Stage 4 — Backtester.** Order semantics, slippage model and its fitting, ambiguity resolver, gap handling, orchestration, synthetic fixtures, golden tests. **Highest-value premium spend in the project.**

**Stage 5 — Statistics and robustness.** Core metrics, stationary bootstrap, evidence tiers, robustness battery.

**Stage 6 — Forward reconciliation.** Manual journal, ordering safeguard, error decomposition, backup and doctor completion.

**Premium allocation:** 16 of 44 milestones. Stage 0 14%, Stage 1 9%, Stage 2 4%, Stage 3 24%, Stage 4 34%, Stage 5 12%, reserve 3%.
**The test for premium:** if this component is subtly wrong, does the system crash or does it lie? Crash → standard. Lie → premium.

---

## Part 3 — Standing risks

1. **Sample size may kill the premise.** The Stage 0 gate exists to find this out in week three rather than month twelve.
2. **Costs may consume any edge found.** "No reliable edge found" remains a valid and expected outcome.
3. **Research feed ≠ execution feed.** Dukascopy is an institutional feed; retail spreads are wider, especially at the London open. Every experiment report therefore shows a 1.5× spread-stressed figure as a co-equal headline number.
4. **Data revisions.** Dukascopy silently corrects history. Hashing and dataset versioning make this visible; a revision creates a new dataset version and marks prior experiments unreproducible rather than overwriting them.
5. **The vault is a commitment device only.** The user holds the key. Its force comes from the permanence and prominence of the unlock record, not from access control. The vault is therefore deliberately **not** encrypted at rest — encryption would add a key-loss failure mode without adding protection against the only adversary.
6. **Attention risk.** One hour a day over twelve months is the real constraint. Every stage must produce something readable, or the project dies of boredom before it dies of statistics.
7. **Hypothesis generation from data the hypothesis is then tested on.** Unavoidable in year one. Mitigated by running Stage 2 descriptive reports on 2007–2014 and requiring every development report to show 2015–2018 as a separate segment labelled *"period not seen during hypothesis generation."* Mitigated, not eliminated.

---

## Part 4 — Definitions

**Edge (amended, PDLA-02).** A positive expectancy in R, **already net of modelled costs**, whose stationary-bootstrap 95% lower bound exceeds zero, which survives year-by-year segmentation, exceeds its placebo baseline, and whose lower bound remains above zero under 1.5× cost and slippage stress.

**R.** One unit of risk, defined per strategy as the entry-reference-to-stop-reference distance, fixed at signal time (C7c). All internal accounting in R; currency only at the reporting edge.

**Hypothesis family.** A lineage of related strategy variants sharing a core claim. The unit of multiple-testing accounting and of vault unlocking. Two versions belong to the same family if the same falsifier would abandon both.

**Evidence tiers.** NO EVIDENCE · EXPLORATORY · PROMISING · VALIDATION-SUPPORTED · HOLDOUT-SUPPORTED. Computed mechanically, never chosen. No tier implies truth or persistence.

**Stage 0 frequency gate (amended, PDLA-06).** `PROCEED GBP/USD` requires the narrowest measured context intersection to have a median annual count ≥ 100 **and** a 10th-percentile annual count ≥ 60, measured over admissible years in 2007–2022. Counts between 40 and 100 trigger one widening attempt. Below 40 after widening, the EUR/USD comparison runs.

> **Recorded qualification, at the user's instruction.** These thresholds are a **conservative research-design choice for V1, not a universal statistical law.** They encode an assumption that a directional filter and entry trigger remove roughly 50–75% of context instances, and a judgement that ~150 holdout events is the minimum at which a small effect is distinguishable from zero. Both are defensible and neither is proven. The thresholds **may only be changed through the decision log, on the basis of evidence** — for example, measured attrition from context to setup once Stage 3 exists, or a power calculation against an observed effect size. They may not be changed because a probe failed to clear them.

---

## Part 5 — Open questions

| # | Question | Status |
|---|---|---|
| Y1 | Pre-registration CLI ritual | Resolved — Technical Bible Part 2 §K.4 |
| Y2 | HTML reports after Stage 5 | Resolved: no. Markdown + Parquet sidecars + notebooks |
| Y3 | Event-calendar source | Resolved: static curated CSV, versioned as a dataset, exclusion flag only |
| Y4 | Broker export schema | Deferred to Stage 6+; schema present, no importer |
| Y5 | Slippage calibration constants | Deferred to Stage 4B, fitted from data |
| Y6 | Widening the instrument set | Deferred to post-Stage 5; requires a new Product Bible version |
| Y7 | Stop movement and partial exits | Deliberately excluded from V1; revisit only with evidence |
| Y8 | Vault encryption at rest | Resolved: no (see risk 5) |

---

## Part 6 — Immediate next action

**WP-000 (Stage 0A — repository scaffold, `core` module, architecture enforcement) is released for implementation.**

WP-001 onward are deliberately **not** written. WP-000 is implemented and reviewed first, so the work-package format and the review gate are tested on one small package before the pattern is multiplied across forty-three more.

Review gate returns one of: **APPROVED**, **APPROVED WITH NON-BLOCKING ISSUES**, or **REJECTED — REQUIRES CORRECTION**. A program that merely runs is not approved.
