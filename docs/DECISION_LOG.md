# JARVIS — DECISION LOG

The canonical record of every product and technical decision, chronological, append-only. Entries are never edited; a superseded decision gets a new entry marking the old one as superseded and naming the entry that replaced it.

Status key: **Active** · **Superseded** · **Deferred** · **Rejected**

---

## 2026-08-27 — Product discovery

| # | Decision | Rationale | Status |
|---|---|---|---|
| D-001 | Product Bible opened; no code written before product definition is frozen | Brief §17, §20. Premium coding credits are a scarce budget and must not be spent on architecture discovery | Active |
| D-002 | Instrument: GBP/USD | The project's framing is session-structural, and GBP's London character is the most pronounced among the majors. Range-to-cost ratio comparable to EUR/USD despite wider spreads. EUR/USD retained as the fallback if the Stage 0 gate fails | Active |
| D-003 | Environment: Windows, native Python 3.12, local only, no cloud | Single user, ~150 MB working analytical dataset. Cloud adds ops burden and weakens the vault's credibility. Native over WSL to avoid a filesystem boundary during multi-gigabyte ingest | Active |
| D-004 | AI layer excluded from V1 entirely | An intelligence layer has nothing to say until hundreds of experiments exist. It is the highest-cost, least-evidenced component, and it creates exactly the dependence the human-outcome objective forbids. A chat interface covers teaching and criticism at zero engineering cost | Active |
| D-005 | Holdout: 2023–present sealed, one irreversible unlock per hypothesis family, descriptive access permitted | A shorter seal yields too few events to be meaningful. Descriptive access removes the recent-regime blind spot at near-zero cost to test integrity | **Superseded in part by D-014** (scope of descriptive access narrowed) |
| D-006 | V1 exclusion list ratified: no live trading, ML, funded modelling, multi-pair, replay trainer, web UI, auto-optimisation | Each excluded item either costs weeks with no research value or actively increases the probability of forming a false belief | Active |
| D-007 | Roadmap reordered: market description precedes strategy machinery | The user is a blank slate. A backtester with no hypotheses to test is premature, and imported hypotheses are someone else's noise | Active |
| D-008 | Product definition frozen (Product Bible v1.0) | All twelve critical decisions resolved; the pre-registration ritual deliberately deferred to Stage 3 | Superseded by D-019 (v1.1) |

---

## 2026-08-28 — Technical specification

| # | Decision | Rationale | Status |
|---|---|---|---|
| D-009 | Technical Bible produced in four parts (A–Z) plus WP-000 | Implementation-grade specification so the coding model never invents architecture, infers semantics, or makes research-integrity decisions while coding | Active |
| D-010 | Architecture: five strict layers, enforced by `import-linter` in CI | Two rules carry the integrity load — the single-reader rule (only `bars` and `vault` touch market Parquet) and the purity rule (`features`/`strategies`/`opportunities` cannot import results modules). Both must be machine-enforced or they decay within months | Active |
| D-011 | `pre_london` session defined in `Europe/London` local time, not Tokyo local | Defining the Asian range in Tokyo time makes its distance from the London open swing by an hour twice a year in each of two zones, creating four clerical regimes per year. Defining it in London time fixes its relationship to the thing it is measured against. `tokyo` remains available separately | Active |
| D-012 | Vault not encrypted at rest | The only adversary is the user. Encryption adds a key-loss failure mode that could destroy the holdout permanently, without adding protection. The vault's force is the `GatedReader` and the permanent unlock record | Active |
| D-013 | Reports: Markdown plus Parquet/CSV sidecars. No HTML generator, in V1 or likely ever | Reports are read by one person, in a terminal or notebook, need to be diffable, and need to be pasteable into a chat window — which is now the primary AI route since D-004. Markdown satisfies all four; HTML satisfies none better and costs a rendering pipeline | Active |
| D-014 | Event calendar: static curated CSV, versioned as a dataset, used as an exclusion flag and robustness segmentation only — never as a predictive feature in V1 | Preserves the offline-by-default property. No scraping, no API dependency | Active |
| D-015 | Scan kept as a per-bar Python callable rather than vectorised | 10–30 minutes for a full-history run is acceptable for a tool run a few times a day. Vectorising would trade the project's core value — the user understanding their own strategy — for speed nobody needs. Escape hatch: restrict scanning to the session window, a 10-line change | Active |
| D-016 | Primary metric for pre-registration restricted to `expectancy_r`; win rate may be reported but not pre-registered | For a fixed-R-multiple strategy, win rate and expectancy are nearly the same statistic, and win rate invites the wrong intuition | Active |
| D-017 | Maximum drawdown never reported as a point estimate | It is an extremum statistic with a wide right-skewed sampling distribution that grows with `n`. Always reported alongside its bootstrap distribution, with the 95th percentile as the plannable number | Active |
| D-018 | Sharpe ratio excluded | Trade-level Sharpe has no meaningful time normalisation and depends on trade frequency, so it is not comparable across strategies | Active |

### Adversarial review changes (within PM authority; recorded for visibility)

| # | Decision | Rationale | Status |
|---|---|---|---|
| AR-1 | Stage 2 descriptive reports run on 2007–2014. Every development report must show 2015–2018 as a separate segment labelled "period not seen during hypothesis generation." An effect absent in that segment cannot reach PROMISING | The development set is otherwise used twice — to generate the hypothesis and to test it. This is the largest single source of false evidence in the design and pre-registration cannot fix it, because pre-registration happens after the observation | Active |
| AR-2 | Report three intervals, not two: unadjusted, Šidák over distinct sweep IDs, and Šidák over all family runs. The evidence tier uses the conservative bound | Correcting properly for correlated tests requires assumptions this project cannot justify. Showing the bracket is more honest than picking one point inside it | Active |
| AR-3 | The 1.5× spread-stressed result is a co-equal headline figure on every experiment report, labelled "retail-realistic estimate" beside the base case labelled "institutional feed" | Dukascopy is an aggregated institutional feed. Retail spreads are materially wider, especially in the first minutes of the London open — precisely the target window | Active |
| AR-4 | Event calendar promoted from deferred to a Stage 2 deliverable | Without it, strategies may be harvesting scheduled-release moves that are untradeable in practice because spreads blow out | Active |
| AR-5 | Vault gating enforced by capability token, not call-stack inspection. `GatedReader` constructors are private; readers are minted only by `RunContext.reader(query_class)` | Stack inspection is fragile, hard to test, and breaks under any refactor adding a wrapper. A capability token makes the vault boundary a type-level property | Active |
| AR-6 | Stage 2 ships three reports (R1, R2, R5). R3, R4, R6 deferred to Stage 2F, built only after the first three have been used and found wanting. Robustness ships six mandatory tests at 5E, the rest at 5G | Six reports and twelve robustness tests before anything has been read or any strategy exists is speculative build on the critical path | Active |

---

## 2026-08-28 — Amendment ratification

All six proposed amendments ratified by the user without modification.

| # | Amendment | Decision | Rationale | Status |
|---|---|---|---|---|
| D-019 | **PDLA-01 — Stage 0 sequencing** | **RATIFIED.** Stage 0 redefined as a vertical slice (milestones 0A–0I): scaffold, fetcher, parser, time engine, session engine, resampler, QA, five features, probe. Stage 1 redefined as *completing* the data layer rather than building it | Stage 0 as originally sequenced required Stage 1's deliverables and could not be built. Any shortcut avoiding the dependency makes the probe untrustworthy — counting Asian-range events without a DST-correct session engine gives wrong counts for roughly four weeks a year, and the probe's entire output is a count. **User ruling: correctness of the feasibility gate takes priority over preserving the original two-day estimate.** Accepted cost: three premium milestones and two to three weeks | Active |
| D-020 | **PDLA-02 — Edge definition** | **RATIFIED.** An edge is a positive expectancy in R, already net of modelled costs, whose stationary-bootstrap 95% lower bound exceeds zero, surviving year-by-year segmentation, exceeding its placebo baseline, and remaining above zero under 1.5× cost and slippage stress | The original wording subtracted costs twice, since spread is inside the fill prices and commission and slippage are inside `r_net`. A stress requirement serves the original intent — margin above costs — without the double subtraction | Active |
| D-021 | **PDLA-03 — Vault access scope** | **RATIFIED.** Vault reads restricted to a fixed enumerated set of coarse annual aggregates: annual median and IQR of session ranges, realised volatility and spread, plus admissible day counts. One row per year per session. No conditioning, no cross-tabs, no sub-daily breakdown. Stage 2 conditional reports and Stage 0 both run on 2007–2022 only | Conditional distributions over vault years fit the hypothesis to the holdout through the user's own reasoning — the one contamination path no software control can detect afterwards. Coarse aggregates answer the calibration question ("is the market now like the market I studied") without suggesting a strategy | Active; supersedes part of D-005 |
| D-022 | **PDLA-04 — Spread charged once** | **RATIFIED.** Spread is charged solely by the bid/ask price convention. No additive spread cost. The manifest validator rejects any spread cost field. The empirical spread distribution is retained as a descriptive output and as the scaling basis for slippage | Storing real bid/ask and additionally modelling a spread cost charges it twice, roughly doubling cost drag and rejecting real effects | Active |
| D-023 | **PDLA-05 — R denominator** | **RATIFIED.** `r_denominator = \|entry_reference_price − stop_reference_price\|`, captured at signal time on the opportunity row, copied to the trade, never recomputed from actual fills | Using the actual fill lets entry slippage widen the denominator, shrinking reported R losses — an error that makes a strategy look better precisely when execution was worst | Active |
| D-024 | **PDLA-06 — Two-tier frequency gate** | **RATIFIED.** `PROCEED GBP/USD` requires the narrowest measured context intersection to have a median annual count ≥ 100 **and** a 10th-percentile annual count ≥ 60 over admissible years in 2007–2022. Counts 40–100 trigger one widening attempt. Below 40 after widening, the EUR/USD comparison runs | The original gate did not say what was being counted. Context frequency and final setup frequency differ by two to four times, so a 40/year gate on contexts would pass specialisations capable of only 10–20 tradeable setups a year — never enough for a usable holdout sample | Active |
| D-024a | **Qualification on D-024, recorded at the user's explicit instruction** | The thresholds of 100 (median) and 60 (P10) are a **conservative research-design choice for V1, not a universal statistical law.** They encode an assumption that a directional filter and entry trigger remove roughly 50–75% of context instances, and a judgement that ~150 holdout events is the minimum at which a small effect is distinguishable from zero. Both are defensible; neither is proven. **They may be changed only through this decision log and only on the basis of evidence** — for example measured context-to-setup attrition once Stage 3 exists, or a power calculation against an observed effect size. **They may not be changed because a probe failed to clear them.** | Prevents the gate from being quietly relaxed at the moment it becomes inconvenient, which is the moment it matters | Active |
| D-025 | **Product Bible v1.1 issued; status set to READY TO BUILD STAGE 0** | Supersedes v1.0. All six amendments incorporated; all other v1.0 decisions carried forward unchanged | Both blockers resolved | Active |
| D-026 | **WP-000 released for implementation. WP-001 onward deliberately not written** | User ruling: implement and review WP-000 first so the work-package format and the review gate are tested on one small package before the pattern is multiplied across forty-three more. A defect in the template would otherwise be replicated forty-four times | Active |

---

## Pending entries

Reserved for outcomes not yet known.

| # | Awaiting |
|---|---|
| D-027 | WP-000 review verdict (APPROVED / APPROVED WITH NON-BLOCKING ISSUES / REJECTED) |
| D-028 | Stage 0 gate decision: PROCEED GBP/USD, WIDEN CONTEXT, or CONSIDER EUR/USD FALLBACK, with arithmetic |
