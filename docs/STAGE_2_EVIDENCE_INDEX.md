# STAGE 2.5 — EVIDENCE INDEX

A navigation aid into R1, R2 and R5, written for the human inspection Stage 2.5 exists for (Technical Bible Part 4 §U: "Human hypothesis generation. No engineering."). This document is deliberately a **pointer**, not an analysis: it names where each primary artifact lives, what it measures, and what its own disclosed caveats are — it adds no new conditioning, no comparison across reports, and no interpretation beyond what `docs/DECISION_LOG.md` already records. Reading this file is not a substitute for opening the actual reports.

Companion reading: `docs/HANDOVER.md` (current project state), `docs/DECISION_LOG.md` entries D-067 through D-076 (everything that happened while building these three reports, including two real defects found and fixed mid-build), `docs/AUDIT_stage2_independent_20260913.md` (an independent external audit of the codebase as of commit `5c63121`).

---

## How to get the actual reports

`reports/describe/` is git-ignored — these are regenerable outputs, not source, so nothing about "the current report" is pinned to a commit. To get the real, current artifacts:

```bash
jarvis describe run --report R1
jarvis describe run --report R2
jarvis describe run --report R5
```

Each writes a timestamped Markdown file plus a Parquet sidecar to `reports/describe/` (R1 also writes an SVG box plot). Re-running is deterministic given the same code and data — a fresh run should reproduce the same numbers as any prior run at the same commit, to the last digit (this was directly verified during the overnight audit: two independent R5 runs at the same commit, generated hours apart, are byte-identical apart from the timestamp line).

---

## R1 — Session range anatomy

**File:** `reports/describe/R1__*.md` (+ `.parquet`, `+__boxplot.svg`)
**Computation:** `src/jarvis/describe/r1.py` · **Rendering:** `src/jarvis/reporting/describe_r1.py`, `src/jarvis/reporting/boxplot.py`
**Spec:** Technical Bible Part 2 §G.1.2

- **Question asked:** how large are the `pre_london`, `london` and `new_york` ranges, and how has that changed?
- **What it measures:** the distribution (median, IQR, 95% bootstrap CI) of each session's price range, in both raw price and ATR-normalised units, broken out by year and by day of week. The box plot shows the year axis only, in ATR units.
- **Population:** every trading day in 2007-2014 with bars in that session's own window; a day's ATR-unit figure additionally requires `atr_bars(1440)` to be past its own warmup (costs exactly one day at the very start of the dataset — D-074).
- **Disclosed judgment calls (D-074):** ATR units chosen for the box plot over price units, because GBP/USD's price level drifted materially over the period and a price-unit plot would show that drift rather than range-relative-to-volatility; one shared SVG with three panels, each with its own y-axis scale.

## R2 — London range conditional on pre-London range percentile

**File:** `reports/describe/R2__*.md` (+ `.parquet`)
**Computation:** `src/jarvis/describe/r2.py` · **Rendering:** `src/jarvis/reporting/describe_r2.py`
**Spec:** Technical Bible Part 2 §G.1.2

- **Question asked:** does a compressed Asian (pre-London) range associate with a larger or smaller London range?
- **What it measures:** trading days bucketed into 5 equal-count quintiles by `pre_london_range_pct(60)` (today's pre-London range's percentile rank among the trailing 60 eligible days); the distribution of `london_range / atr_bars(1440)` within each quintile, pooled and by year, with bootstrap CIs on each bucket's median.
- **A load-bearing mechanical detail (D-075):** `pre_london_range_pct(60)` only takes 61 possible values (`k/60`), so many trading days share an identical score. Quintile buckets never split a tied group across a boundary, which means bucket sizes are close to but not exactly 1/5 each — every bucket's realised size and its own realised percentile range are shown in both tables, nothing is smoothed to look even.
- **The result as measured (D-075, corrected D-076):** pooled bucket sizes 413 / 412 / 415 / 411 / 361 (Q1..Q5); pooled median ATR-unit London range 32.20 / 33.65 / 32.98 / 31.53 / 32.65 — no monotonic pattern, and the bootstrap 95% CIs overlap substantially across all five buckets.
- **What this does and does not establish:** this is one specific, pre-specified formulation (a 60-day trailing percentile, quintile buckets, the ATR-normalised London range) over one fixed period (2007-2014). It speaks to that formulation. It does not test — and this report cannot rule out — a different lookback window, a different bucket count, a different normalisation, or a relationship confined to a subset of years or conditions. Reaching for an alternative formulation because this one didn't show a pattern is exactly the kind of search this project's multiple-testing discipline (Product Bible C10) exists to prevent outside a registered, counted run.

## R5 — Spread and cost climate

**File:** `reports/describe/R5__*.md` (+ `.parquet`)
**Computation:** `src/jarvis/describe/r5.py` · **Rendering:** `src/jarvis/reporting/describe_r5.py`
**Spec:** Technical Bible Part 2 §G.1.2

- **Question asked:** what does it actually cost to transact, by hour and day?
- **What it measures:** `spread_twa` (time-weighted spread, a raw ingested bar field, not a derived feature) distribution by Europe/London hour-of-week and by UTC calendar year; the ratio of median spread to median 60-*bar* rolling mid-price range for the same hour-of-week bucket.
- **A real, verified pattern in the by-year table:** median spread on the institutional (Dukascopy-sourced-equivalent, HistData) feed declines from 0.000500 in 2007-2008 to 0.000300 by 2012-2014 — roughly a 40% reduction over the development period. The bootstrap CIs on several years are exactly zero-width (e.g. `[0.000500, 0.000500]`) — this is a genuine property of a heavily quantised variable with a dominant modal value under a 500-resample bootstrap, verified during the overnight audit, not a computation error, but worth knowing before reacting to how precise it looks.
- **A mechanical caveat found during the overnight audit, checked against real data and found practically negligible:** the displayed `n` in the hour-of-week table is the spread sample's own count; the adjacent 60-bar range CI drops its own nulls separately (a 60-bar warmup). Checked directly against the full real dataset: the two counts differ by at most 42 rows out of ~23,000 (0.18%), confined to the first calendar hour of the entire 2007-2014 window. Not zero, not hidden in the underlying data, but not something the rendered table currently discloses as a separate number either.
- **Methodological implication worth carrying forward, not a trading claim:** if transaction costs roughly halved over 2007-2014, pooling that whole period for any future cost-sensitive evaluation blends two different cost regimes. This is a property of the *evaluation methodology* to be aware of, not evidence about any strategy.

---

## Known gaps that apply to all three reports (not a defect in any one of them)

- **No event-calendar exclusion.** AR-4 (Technical Bible Part 4 §Z.1) promoted a scheduled-economic-event exclusion flag to a mandatory Stage 2 deliverable (milestone 2D). It was never built — no code, no data file, and the decision log still shows AR-4 as Active with no resolving entry. R1/R2/R5 include every trading day regardless of scheduled releases.
- **The 2007-2014 boundary is enforced once, by a fixed constant, not structurally.** `describe_run` calls `stage2_descriptive_range()` (a hardcoded 2007-01-01–2015-01-01 pair) before loading bars; `compute_r1`/`compute_r2`/`compute_r5` trust whatever `bars` they're handed and do not independently check the date range themselves. This is a known, disclosed pattern (D-061a, D-071), re-confirmed by this audit with no evidence it has ever been violated — AR-5's intended structural fix (`GatedReader`, a capability token) has not been built (Stage 1E, still an empty `vault/` skeleton).
- **Only 6 of the Bible's 19 feature-catalog rows are built** (the ones these three reports actually needed). The other 13 — including `spread_pct`, `dist_to`, `break_state`, `reentry_state`, `session_state` — do not exist yet. This does not affect R1/R2/R5's own correctness (verified directly: R5 reads `spread_twa` as a raw bar column, not a feature), but it means Stage 2 milestone 2A is not actually complete despite 2C being done.

---

## Suggested inspection sequence

1. Regenerate all three reports fresh (commands above), so you're looking at output from the current code, not a stale file.
2. Open R1 first. It's the closest thing here to a calibration check — do the magnitudes and the year-over-year and weekday patterns look like a plausible description of GBP/USD to you, before you've seen R2 or R5?
3. Open R5 next. Look at the by-year spread table specifically before anything else in it.
4. Open R2 last, since it's conditioned on a feature (`pre_london_range_pct`) whose behaviour R1 already gave you some intuition for.
5. Read the box plot SVG directly, not just the table it summarises.

## Questions worth answering before consulting anything else

- What actually surprised me in each report?
- What did I believe about GBP/USD's session structure before opening these, and did the evidence change it?
- In R1, which axis (year or weekday) looks more stable, and which looks noisier — and is that noise or a real pattern?
- In R5, is the spread decline something I'd have predicted, or does it change how I'd think about testing anything against the earlier years of this dataset?
- In R2, would I have described the quintile relationship as "roughly flat" before seeing the numbers, or am I only calling it flat now because that's what came out?
- If R2 had shown a clean, monotonic pattern instead, would I trust it more or less than I trust this null — and why?
- What do these three reports simply not answer, that I might currently be assuming they do?
- Am I reacting to statistical magnitude or to how a table or chart happens to look?

This document does not answer any of these. That's the point.
