# JARVIS — PROJECT HANDOVER

Paste this at the start of a new chat to restore full context. It is a context-priming doc, not a build deliverable, but it is committed to `docs/` in this repo (since commit `3d6e850`) so a fresh Claude Code session can read it directly.

---

## What Jarvis is

A personal, local, deterministic FX research system for GBP/USD. **A research instrument, not a trading bot.** The standard it's held to: *"I can trust what it tells me, reproduce the result, and become more competent by using it."* A beautiful wrong backtest is a failed product.

Built for one user (Nawwaf, 20, degree apprentice at J.P. Morgan, ~1hr/day). Genuine blank slate — no candidate setups. Core discipline: **market description before strategy testing**.

**Frozen exclusions (V1):** no AI layer, no live trading, no ML, no funded-challenge simulation, no web UI, no auto-optimisation, no multi-pair. Windows-native Python 3.12, fully local.

---

## Current state

**Stage 0 is closed** (as originally recorded below — GBP/USD confirmed, D-042 final). **Stage 1A's operational run is complete and its corrections have propagated all the way through: the on-disk `data/tick/` and `data/bars_1m/` stores were rebuilt AFTER the DST/dedup/column-order fixes landed** (verified by file mtimes and an independent overnight audit — `data/tick/` rebuilt 2026-09-12, `data/bars_1m/` resampled from it a full day later on 2026-09-13, matching `STAGE_1A_RUNBOOK.md`'s execution record). **Stage 2 (market description) is no longer "next" — it has produced its first three reports.** R1 (session range anatomy), R2 (London range conditional on pre-London range percentile), and R5 (spread and cost climate) are all built, tested, and have each been run for real against the full 2007–2014 dataset (2,802,563 bars). This closes Stage 2 milestone 2C. **The project is now at Stage 2.5 — human hypothesis generation — which is explicitly "no engineering" per the roadmap.** No hypothesis has yet been registered; no Stage 3 work has begun.

R2 in particular produced a registered null: under the confirmed quintile-bucketing formulation, pre-London range compression showed no monotonic relationship with subsequent London range, pooled or by year (D-075/D-076). This is a legitimate, first-class research result, not a setback — see `docs/DECISION_LOG.md` D-075/D-076 and the overnight audit's findings for the full picture, including two real but modest gaps found during that audit: **AR-4's event calendar (Stage 2 milestone 2D) was never built** (still logged Active, no resolving entry — no scheduled-event exclusion flag exists anywhere R1/R2/R5 read), and **Stage 2 milestone 2A ("remaining features to complete the 19") is only partially done** — 6 of the Bible's 19 feature-catalog rows are registered (the ones R1/R2/R5 actually needed); the other 13 (ret_*, rv_ratio, dist_to, session_state, break_state, reentry_state, spread_now/pct, the three calendar features, prev_day/week extremes) remain unbuilt. Neither gap affects R1/R2/R5's own correctness (verified directly), but both are undisclosed in the reports themselves and worth the user's awareness before any Stage 3 planning.

**Repo:** `https://github.com/nawwafnabdalla/jarvis` (public — review is done by cloning it, not reading summaries)

**Latest commit:** `4e2e18b` (WP-021, D-075/D-076, R2 built) plus this overnight audit's documentation-only follow-up, on `main`. 554 tests passing under both invocations, 11/11 architecture contracts held.

**Docs confirmed to actually exist in `docs/`** (checked directly): `DECISION_LOG.md`, `PRODUCT_BIBLE.md`, `TECHNICAL_BIBLE_1-4.md`, `WP-009-TZ-FINDING.md`, `STAGE_1A_RUNBOOK.md`, `HANDOVER.md` (this file), `AUDIT_stage2_independent_20260913.md`, `STAGE_2_EVIDENCE_INDEX.md` (new — the human inspection route into R1/R2/R5).

### What's built

| Module | Does |
|---|---|
| `core` | IDs, canonical JSON hashing, error hierarchy, config |
| `provenance` | git state, code hashing (stub until Stage 1B) |
| `timeengine` | UTC nanoseconds, IANA conversion, DST fold policy, trading day/week |
| `sessions` | Versioned session sets, membership, windows, derived intersections |
| `ingest` | HistData CSV ingest (primary, fully corrected) + Dukascopy fetcher (retained, dormant — see D-059) |
| `bars` | Tick → 1-minute resampler, reading from `data/tick/` (the HistData store) |
| `qa` | Data-integrity checks, retargeted to the HistData store; Dukascopy-only checks dormant, not deleted |
| `features` | 12 of 19 catalog rows registered (session ranges, `atr_bars`, `pre_london_range_pct`, `rv_60m`) + auto-generated leakage harness (L-1…L-5, redesigned under D-073) |
| `probe` | Stage 0 contexts, two-tier gate, report — D-042 final |
| `describe` | `periods.py` (fixed 2007-2014 Stage 2 range), `r1.py`, `r2.py`, `r5.py` — all three built, tested, run for real |
| `reporting` | Shared furniture (watermark, sample-size suppression), `boxplot.py`, per-report renderers for R1/R2/R5 |
| `cli` | `jarvis` console script; `describe run --report {R1,R2,R5}` wired |
| `vault`, `strategies`, `strategy_impls`, `opportunities`, `execution`, `experiments`, `backtest`, `statistics`, `robustness`, `forward` | **Still empty skeletons** — Stage 3 onward, not started |

### What's NOT built

- Stage 1B: dataset manifests, sealing, dataset versions (R1/R2/R5 already disclose this — "Dataset version: not yet available")
- AR-4's event calendar (Stage 2 milestone 2D) — Active in the decision log, no code, no resolving entry
- The remaining 13 of 19 feature-catalog rows (Stage 2 milestone 2A, partially done)
- AR-5's `GatedReader` capability-token vault boundary (Stage 1E) — the 2007-2014 restriction on R1/R2/R5 is enforced only by `describe_run` calling a fixed-constant function before loading bars, not structurally (D-061a, D-071 — confirmed still accurate by this audit)
- Stages 3–6: strategy machinery, backtester, statistics, forward testing — genuinely untouched
- R3, R4, R6 — explicitly gated by AR-6 on R1/R2/R5 being "used and found wanting" first; that use has not happened yet

---

## The one thing that must happen next

**Nothing engineering-shaped.** Stage 2.5 is explicitly "no engineering" (Technical Bible Part 4 §U). R1, R2 and R5 are built, tested, reproducible, and have been run for real. The next legitimate action is Nawwaf's own inspection of the evidence — see `docs/STAGE_2_EVIDENCE_INDEX.md` for the route in. Nothing should be built, characterized, or hypothesized ahead of that review; in particular, R3/R4/R6 remain out of scope until AR-6's "used and found wanting" gate is actually satisfied by a human, not inferred by an agent.

---

## Critical frozen decisions

| ID | Decision |
|---|---|
| C1 | GBP/USD only |
| C2 | 1-minute OHLC with **separate bid and ask**, derived from ticks; tick archive retained |
| C4 | Development 2007–2018, Validation 2019–2022, **Vault 2023–present sealed** |
| C4b | Vault access = coarse annual aggregates only; Stage 0 and Stage 2 run on 2007–2022 |
| C7 | Buys at ask, sells at bid. Stops fill at first tick beyond level + slippage. Same-minute stop/target resolved from ticks, pessimistic fallback |
| C7b | **Spread charged once**, via bid/ask prices |
| C8 | UTC nanoseconds, IANA zones, never hardcoded offsets |
| C10 | Every dev run counted; validation requires pre-registration; sweeps count individually |
| C11 | No AI in V1 |
| D-024a | Gate thresholds (median ≥100, P10 ≥60) are a **research-design choice**, changeable only via decision log on evidence, **never because a probe failed to clear them** |
| D-024b | **Gap in D-024 (what happens when the one permitted widening still lands in the 40–100 band), ruled on with real evidence.** EUR/USD fallback deferred (not triggered by this result, not abandoned — available later on Stage 3 attrition evidence); multi-pair-simultaneous rejected outright (undermines C10/D-007). Stage 0 concludes for GBP/USD |
| D-042 | **Stage 0 gate decision, final.** Baseline `WIDEN_CONTEXT` (M=61.5, P10=51.0); one sanctioned widening (`break_buffer_atr`) moved it only marginally, still `WIDEN_CONTEXT`. Per D-024b: **GBP/USD confirmed as the instrument going forward, not provisionally** |
| D-045a | **Every item a coding agent raises in closing notes gets an explicit disposition** — acted on, deferred with a log entry, or dismissed with a reason |
| D-054 | A data-convention claim requires discriminating evidence spanning the **full** ingest range, not a sample |
| D-055 | **The corrected timezone model** — no HistData file is ever on a fixed clock; two DST calendars (US era 2006-2018, EU era 2019-2022), switching once, at 00:00 UTC the Monday after the EU transition |
| D-058 | Tick storage dedup keys on **`(ts_utc_ns, row_sequence)`**, never `ts_utc_ns` alone — genuinely distinct same-millisecond quotes must not collapse |
| D-059 | **Dukascopy retired from the forward-testing role.** HistData's own ~weekly update cadence for GBP/USD is sufficient, and using one verified source throughout avoids a vendor-inconsistency between backtest and forward-test data. Fetcher code retained, dormant, not deleted |
| D-060 | Resample/QA retargeted to read `data/tick/` directly (WP-010) |
| D-063/D-064/D-065 | Three real defects found by actually executing the Stage 1A run: a QA zero-spread false-positive and sample-cap bug (WP-013), an INFO-check count-capping bug and design flaw (WP-014), a CLI `UnicodeEncodeError` crash on the probe's own `∩` output (WP-015) |

---

## THE HISTDATA STORY (the single biggest piece of work since Stage 0)

**HistData.com is the sole data source now** — free, no account, no throttling, GBP/USD available from May 2000. Dukascopy is retained only as dormant, unused code (D-059); it is no longer part of any live path.

**The timestamp convention is not what it first looked like.** Their FAQ claims "fixed EST, no DST" — flatly wrong. Early sample-based verification (NFP timing, weekend-close timing, the 16:00 London WMR fix) established 2006–2018 as `America/New_York`, DST-aware. Extending that sample further turned up what looked like an anomaly: some March and November files in 2019–2022 reading as a "fixed" convention. **A full 196-month investigation replaced that framing entirely (D-055):** no file is ever truly fixed. There are two DST calendars in play — 2006-09 to 2018-12 changes on US dates, 2019-01 to 2022-12 changes on EU dates — and they agree for ~11 months a year, diverging only in two short annual windows. That's exactly why the anomaly looked seasonal. The EU-era switch instant itself lands at 00:00 UTC the *Monday after* the EU transition, not at the transition itself — a real, confirmed detail, not an artifact. Verified three independent ways (weekend boundaries, the clock's own discontinuities, the London fix), zero exceptions across the full archive.

**Two "unresolved" questions from the sample-based investigation turned out to be detector artefacts, not properties of the data** — one detector couldn't read Fridays/Sundays at all (exactly where one of the "weak" months' evidence sat), and the "noisy" March files turned out to genuinely change clock mid-file at a verified weekend boundary, which no single per-file label could ever have described correctly.

**Column order has the same underlying lesson, different convention:** 2009-05 switches cleanly from ask-bid to bid-ask mid-month at a weekend boundary (D-055h/D-056) — not corruption, just another per-file-constant assumption that didn't hold for one file.

**A separate, unrelated defect, found only by actually running the full corrected import:** `write_ticks`'s dedup was silently collapsing genuinely distinct same-millisecond quotes — not a market phenomenon (ruled out via density comparisons and a specifically-tested-and-rejected Flash Crash hypothesis), but four sharp, calendar-bounded vendor-side regimes from 2006-09 to 2011-04, up to 38% of rows missing in the worst month. Root cause is unknowable (vendor-internal); the consequence was fixable and was fixed (D-057/D-058) — dedup now keys on `(ts_utc_ns, row_sequence)`, a real column recording file position, never a fabricated timestamp implying false precision.

**Current state: 196/196 months on disk, fully reconciled tick counts, resample and QA both reading from `data/tick/` directly (D-060).** This is the first point the dataset has been actually trustworthy end-to-end rather than provisionally accepted.

---

## Working process (this is load-bearing)

**Review reads the actual repository**, never a narrative summary. Clone it, run the real test suite, reproduce defects independently. The HistData investigation is the strongest demonstration of this yet: multiple plausible-sounding hypotheses were proposed and then correctly killed by a decisive test rather than argued away — a detector-aggregation-bug theory, a diagnostic-tooling-arithmetic-bug theory, and a Flash Crash theory all turned out wrong, each time because someone actually ran the check rather than accepting the plausible story.

**Work packages** have: objective, files allowed/forbidden, required behaviour, edge cases, acceptance criteria, named tests, explicit "must NOT do" list, definition of done.

**Verdicts:** APPROVED / APPROVED WITH NON-BLOCKING ISSUES / REJECTED — REQUIRES CORRECTION.

### Error tally (worth preserving — the pattern matters more than any one bug)

**Stage 0** (see prior handover / decision log D-001–D-048 for full detail): six PM specification errors, three coding defects, all caught before reaching a research result.

**The HistData investigation** (D-052 through D-060 — see the decision log for full detail): a wrong FAQ claim caught early; a genuine, structural, dated timezone anomaly found, then correctly *not* accepted at face value — three separate wrong hypotheses about its cause were proposed and killed by direct testing before the real two-calendar mechanism was found; two real code defects found and fixed outside the original scope (a false-positive spread-corruption check rejecting genuine zero-spread quotes, a field-rename that would have crashed the CLI on first real use, caught only because no test exercised that path); and one substantial, independently-discovered data-integrity fix (the tick-collision dedup gap) that had nothing to do with the investigation's original question and would have gone unnoticed without actually running the full real import.

**The Stage 1A operational run itself (WP-012 through WP-016 — see the decision log for full detail):** four more real defects, none hypothetical, all found only by actually executing the runbook rather than by reviewing it — a QA check's stale Dukascopy-era admissibility path (WP-012); a zero-spread false-positive plus a sample-display cap that was silently also capping a reported count (WP-013); a second, independent count-capping bug plus a check redesigned on investigated reasoning after its original design turned out to test nothing real (WP-014); a console-encoding crash in the CLI's own output layer that bypassed the entire error-handling framework (WP-015). Then a genuine gap in the frozen gate specification itself, D-024 — found by hitting it on the one permitted widening attempt, not by inspection — resolved by a deliberate ruling (D-024b) rather than worked around.

**Stage 2's build (WP-018 through WP-021 — D-067 through D-076):** a date-scope conflict between C4b and AR-1 caught before any report ran (D-067); `n_resamples` tuned down for R5 on measured evidence, not intuition (D-068b); a missing `new_york_range` catalog row (D-069); an independent external audit's three findings (contract-vitality overstatement, a CLI-enforcement pattern, a skipped decision number) verified and logged rather than dismissed (D-070–D-072); a foundational masking-semantic defect found only by building R1 against a session (`new_york`) nothing had used before, which cascaded into a real day-attribution bug in `pre_london_range_pct` and a stale assumption in the leakage harness's own L-3 check — all three found, diagnosed with a genuine control (not just re-passing tests), and fixed rather than patched around (D-073); an ATR-warmup-driven day-count discrepancy traced to its actual mechanism rather than adjusted until the test passed (D-074); a quintile-bucketing method resolved from the literal meaning of "quintile" plus a real discreteness discovery (61 possible values, not a continuous score) that made tie-handling the load-bearing design decision, confirmed against real data afterward rather than assumed (D-075); and, found by an overnight independent audit explicitly tasked with distrusting prior summaries, a transposed pair of digits in D-075's own prose, resolved against three independent primary sources (D-076).

**That overnight audit itself (2026-09-15, no WP number — explicitly "no engineering" per Stage 2.5) is worth naming as its own data point:** tasked with reconstructing Jarvis's state from primary evidence rather than inherited narrative, it found the D-075 transcription error above, confirmed (not merely trusted) that the DST/dedup/column-order fixes actually reached the on-disk bars R1/R2/R5 read (via file mtimes, not just code review), found AR-4's event calendar and Stage 2 milestone 2A silently incomplete with no decision-log entry disclosing either gap, found this very document (HANDOVER.md) had gone stale describing Stage 0 as current, and found a real (if practically negligible on the actual dataset) hidden-denominator possibility in R5's hour-of-week table between its displayed `n` and the range-CI's own separately-null-dropped sample size. See `docs/DECISION_LOG.md` D-076 and `docs/STAGE_2_EVIDENCE_INDEX.md` for the full account.

---

## Immediate next steps

**Nothing engineering-shaped is next.** Stage 2.5 is explicitly "no engineering" (Technical Bible Part 4 §U) — the next legitimate action belongs to Nawwaf: inspect R1/R2/R5 via `docs/STAGE_2_EVIDENCE_INDEX.md` and decide, on the evidence, whether any of them is "used and found wanting" enough to justify AR-6 releasing R3/R4/R6, or whether a Stage 3 hypothesis is warranted at all. Carry forward the same working-process discipline that has found real defects at every stage so far, including this audit itself: verify against the real repository and real run output, never against a narrative summary — including this one.

Everything from Stage 1B onward beyond what's listed as built above remains untouched and unstarted.
