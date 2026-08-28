# JARVIS TECHNICAL BIBLE — Part 3 of 4
## Parts M–P: Backtesting Execution · Risk & R · Statistics · Robustness · Edge Definition · Forward Test

---

# PART M — BACKTESTING SPECIFICATION

This is the part of the system where a plausible-looking mistake produces a plausible-looking profit. Every rule below is stated so that an implementer has no latitude.

## M.1 The pipeline

```
opportunities (all, already written)
   │
   ├─ filter: accepted == 1
   ▼
for each opportunity, in ts order:
   │
   ├─ 1. concurrency check   → skip if restrictions violated at this instant
   ├─ 2. construct orders    → entry order + protective stop + target
   ├─ 3. simulate entry      → fill or expiry (max_bars_to_fill)
   ├─ 4. simulate management → stop / target / time stop / session end
   ├─ 5. compute R           → r_gross, r_net, mae_r, mfe_r
   └─ 6. write theoretical_trade (immutable)
```

Opportunities are processed in strict `ts_utc_ns` order. The concurrency check is the only place where state crosses between opportunities, and it reads only positions opened by earlier opportunities in the same run — never any account balance, never any equity curve. There is no capital constraint in V1; accounting is in R and position sizing is fixed at 1R (Part M.9).

## M.2 Price side convention (F-16)

| Action | Price series used |
|---|---|
| Open long (buy) | **ask** |
| Close long (sell) | **bid** |
| Open short (sell) | **bid** |
| Close short (buy) | **ask** |

A long's stop-loss is a sell → triggered and filled on the **bid**. A long's take-profit is a sell → **bid**. A short's stop is a buy → **ask**. A short's target → **ask**.

**This means a long entered on the ask and stopped on the bid pays the spread twice, once at each end.** That is correct and it is where most retail backtests lose their edge. Any implementation that evaluates a long's stop against the ask series is wrong and the synthetic test suite contains a case that catches it.

## M.3 Order types and trigger semantics

Let `bid(τ)`, `ask(τ)` be the tick series. Define the **relevant series** per the table above.

### M.3.1 Market buy
Fills at the first tick at or after the decision instant, at `ask(τ) + slip`, where `slip ≥ 0`.

### M.3.2 Market sell
Fills at the first tick at or after the decision instant, at `bid(τ) − slip`.

### M.3.3 Buy stop (entry, long breakout) at level `L`
- **Trigger:** the first tick with `ask(τ) >= L`.
- **Fill price:** `max(L, ask(τ)) + slip`. If the market gapped through, `ask(τ) > L` and the fill is at the worse price. It is never `L` when the tick is worse than `L`.
- **Never** triggered by the bid touching `L`.

### M.3.4 Sell stop (entry, short breakout) at level `L`
- **Trigger:** first tick with `bid(τ) <= L`.
- **Fill:** `min(L, bid(τ)) − slip`.

### M.3.5 Buy limit (entry) at level `L`
- **Trigger:** requires **trade-through**, not touch (F-17). The first tick with `ask(τ) < L`. Strictly less than.
- **Fill:** `min(L, ask(τ)) + slip`, capped so the fill is never better than `L`... in fact for a buy limit the fill is `min(L, ask(τ))`, then `+ slip` may make it worse than `L`; if `fill > L` after slippage, the order is treated as **not filled** at that tick and continues to wait. A limit order never fills worse than its limit price.
- Rationale for trade-through: an ask that merely touches `L` means there was quoted interest at `L`, not that a resting order at `L` was executed. Touch-fills on limits are the second-largest source of phantom backtest profit after intrabar ambiguity.

### M.3.6 Sell limit (entry) at level `L`
- **Trigger:** first tick with `bid(τ) > L`. Strictly greater.
- **Fill:** `max(L, bid(τ)) − slip`, and never worse than `L`; if slippage would push it below `L`, no fill at that tick.

### M.3.7 Stop loss (exit)
Same trigger and fill mechanics as the corresponding entry stop (M.3.3 / M.3.4), on the correct side. Stop-loss fills **may** be worse than the level; that is the whole point of a stop.

### M.3.8 Take profit (exit)
Same mechanics as the corresponding limit (M.3.5 / M.3.6). Take-profit fills are **never better than** the level and require trade-through.

### M.3.9 Time stop and session end
Executed as a market order at the first tick at or after the trigger instant. If the trigger instant falls inside a data gap, the fill is at the first tick after the gap, and the trade is flagged `gap_exit=True`.

## M.4 Slippage model

```yaml
# config/execution/default.v1.yaml
exec_config_id: default
version: 1
slippage:
  model: spread_scaled_halfnormal
  # slip = k * spread_twa(bar) * |Z|, Z ~ N(0,1), truncated at 3 sigma
  k_market: 0.25
  k_stop:   0.50          # stops fill in thinner conditions, by construction
  k_limit:  0.00          # a filled limit does not slip; it simply may not fill
  gap_multiplier: 3.0     # applied when the fill follows a data gap > 60s
  seed_source: run_rng_seed
commission:
  per_side_price_units: 0.0
```

- Slippage is drawn from a seeded RNG stream derived deterministically from `(run_rng_seed, opportunity_id, order_role)`. Two runs with the same seed produce identical slippage; changing the seed does not change which opportunities exist, only their fills. This makes slippage stress testing (Part O) meaningful and reproducible.
- The model is **fitted, not guessed**, at Stage 4B: `k_stop` is calibrated so that the distribution of `fill − level` on stop orders in the backtest matches the distribution of observed tick-to-tick jumps beyond a level in the actual data. The fitting procedure is versioned; the fitted constants become `default.v2` and every experiment records which version it used.
- `k_limit = 0` is deliberate. A limit either fills at its price or does not fill. Modelling positive slippage on limits would be a gift to the strategy.

## M.5 Costs and the spread double-count (PDLA-04)

**Spread is charged exactly once, by the bid/ask price convention in M.2.** There is no additive spread cost. The manifest validator rejects any `costs.spread_*` field. Commission is additive per side and defaults to zero for a retail FX spread-only account. Swap is `none` in V1 because every V1 strategy is intraday by construction (`cancel_if_session_ends` and `time_stop_bars` bound holding time); a manifest that permits holding past 17:00 New York is rejected until a swap model exists.

## M.6 Intrabar ambiguity — the central rule (F-18, F-19)

A trade is **ambiguous** at a given minute if, within that bar, both the stop level and the target level were reachable on their respective relevant series:

```
long:  bid_l(bar) <= stop_level   AND  bid_h(bar) >= target_level
short: ask_h(bar) >= stop_level   AND  ask_l(bar) <= target_level
```

Resolution, in strict order:

1. **Tick resolution.** Load the ticks for that minute from the tick archive. Walk them in `(ts_utc_ns, seq)` order. The first tick that satisfies a trigger condition determines the outcome. Record `ambiguity_resolved_by = 'tick'`.
2. **Pessimistic fallback.** If the tick archive has no data for that minute — which can only happen for a minute reconstructed from a source hour that is present in bars but absent in ticks, i.e. essentially never, but the branch must exist — assume the **stop** filled first, at the stop's normal fill mechanics. Record `ambiguity_resolved_by = 'pessimistic'`.
3. If neither level was reachable in the bar, the bar is unambiguous. Record `ambiguity_resolved_by = 'unambiguous'`.

Two further requirements:

- **Every run reports the ambiguity fraction.** `ambiguous_trade_fraction = count(resolved_by != 'unambiguous') / count(trades)`. Reports display it in the header. If it exceeds 25%, the report prints a prominent caution: a result that depends heavily on intrabar sequencing is a result about the data's resolution, not about the market.
- **Ambiguity sensitivity is a mandatory robustness test** (Part O, RT-8): re-run resolving every ambiguous bar pessimistically, and report the delta in expectancy. If the delta is large, the strategy's edge lives inside single minutes and is not tradeable.

## M.7 Gaps

A **gap** exists between two consecutive present bars when `prev_gap_ns > 60e9` (more than one minute of no ticks), or across the weekend boundary.

- An order pending across a gap is evaluated at the **first tick after the gap**, not at the gap's theoretical level.
- A stop level jumped over by a gap fills at the first post-gap tick on the relevant series, with `slip × gap_multiplier`. Losses larger than 1R are therefore possible and are recorded as `r_net < -1`. Any backtester that clamps stop losses at exactly −1R is lying, and the golden test suite contains a weekend-gap case that asserts `r_net < -1`.
- The weekend gap is the largest of these. Strategies with `cancel_if_session_ends: true` are unaffected; the code path is still tested.

## M.8 Simultaneous conditions and edge cases

| Situation | Rule |
|---|---|
| Entry stop and stop-loss triggered in the same bar | Ambiguity resolution (M.6) applies to entry-then-stop as a two-step sequence within the tick walk. |
| Target and stop at the same price | Manifest validation error; refused before the run. |
| Stop distance below `min_distance_atr` | Opportunity is rejected at scan time with `rejected_by='stop_too_tight'`; it is still written to `opportunities`. |
| Entry never fills within `max_bars_to_fill` | `entry_filled=0`, `exit_reason='no_fill'`, no R recorded. The opportunity is still counted in the denominator of "opportunities generated." |
| Session ends with position open | Market exit at the session boundary tick; `exit_reason='session_end'`. |
| Zero ticks in the minute of an expected trigger | The bar does not exist; the order waits. |
| Two opportunities in the same direction on the same day | Governed by `max_opportunities_per_trading_day`; the second is written with `rejected_by='daily_cap'`. |
| Concurrent opposing signals | `max_concurrent_positions: 1` means the second is rejected. No hedging in V1. |

## M.9 Determinism

Given identical `(dataset_version, manifest_sha, impl_sha, exec_config_sha, rng_seed)`, the run must produce a byte-identical `result_sha256`. Sources of non-determinism to eliminate explicitly: dict iteration order in serialisation (use canonical JSON with sorted keys), floating-point summation order (fixed left-to-right over the sorted trade sequence), Parquet writer metadata (timestamps disabled), and any use of `set` in output construction.

---

# PART M.10 — RISK AND R

**Definition (resolves PDLA-05).**

```
r_denominator = |entry_reference_price − stop_reference_price|
```

captured at **signal time**, stored on the `opportunity` row, and copied verbatim onto the trade. It is never recomputed from actual fills.

```
long:   r_gross = (exit_price − entry_price) / r_denominator
short:  r_gross = (entry_price − exit_price) / r_denominator

r_net   = r_gross − (total_commission / r_denominator)
```

Spread does not appear in this formula because it is already inside `entry_price` and `exit_price` via M.2.

Consequences, all intended:
- Entry slippage makes `entry_price` worse, so `r_gross` falls. It does not widen the denominator, so it cannot flatter the result.
- Exit slippage on a stop produces `r_net` slightly worse than −1.
- A weekend gap through a stop produces `r_net` substantially worse than −1.
- A target hit exactly produces `r_net` slightly below the manifest's `r_multiple`, because the entry paid the ask and the exit received the bid.

**MAE and MFE** are computed in R units over the holding period using the **relevant exit series** (bid for a long, ask for a short), so they represent what the position was actually worth, not the mid:
```
long:  mae_r = (min bid_l over holding − entry_price) / r_denominator
       mfe_r = (max bid_h over holding − entry_price) / r_denominator
```

**Break-even and stop movement:** V1 has **no stop movement**. No trailing stops, no move-to-break-even, no partial exits. The manifest schema has no fields for them and the validator rejects them. This is not because they are bad; it is because each one adds a parameter and a decision point, and the project has no evidence yet that would justify either. They enter, if at all, as an explicit decision-log amendment after Stage 5.

**Missing stop:** a `Candidate` without a `stop_reference_price` is a validation error at scan time. There are no undefined-risk trades.

**Currency:** never used internally. `reporting` may render R multiples as currency for illustration, given a stated account size, clearly marked as illustrative.

---

# PART N — STATISTICS SPECIFICATION

## N.1 Metric definitions

Let `r = (r₁ … r_n)` be the sequence of `r_net` for filled trades in a run, in time order. Let `k` = number of opportunities generated and `f` = number filled.

| Metric | Formula | Edge cases | Sample warning |
|---|---|---|---|
| `n_opportunities` | `k` | — | — |
| `fill_rate` | `f / k` | `k = 0` → undefined, report `n/a` | `k < 30` |
| `expectancy_r` | `mean(r)` | `n = 0` → `n/a` | `n < 100`: "insufficient for inference" |
| `r_distribution` | Full empirical quantiles at 1, 5, 25, 50, 75, 95, 99 | — | `n < 40`: quantiles beyond 25/75 suppressed |
| `win_rate` | `#{rᵢ > 0} / n` | Exactly-zero outcomes counted as losses, stated in the footnote | Wilson interval always shown |
| `win_rate_ci` | Wilson score interval, α = 0.05 | `n = 0` → `n/a` | — |
| `profit_factor` | `Σ rᵢ⁺ / |Σ rᵢ⁻|` | No losses → `∞`, rendered as `undefined (no losing trades)` | Unstable below `n = 200`; flagged |
| `max_drawdown_r` | Max peak-to-trough of the cumulative `r` curve | `n = 0` → `n/a` | Point estimate is **never** shown alone; see N.3 |
| `mae_r`, `mfe_r` | Distribution, quantiles as above | — | — |
| `expectancy_ci` | Stationary bootstrap, N.2 | `n < 40` → suppressed with an explicit refusal | — |
| `ambiguous_fraction` | Part M.6 | — | > 0.25 → caution banner |
| `trades_per_year` | `n / admissible_years` | — | < 25 → "below vault viability" |

## N.2 Stationary bootstrap

Politis–Romano stationary bootstrap on the **trade sequence**, not on bars.

- Block lengths drawn from a geometric distribution with mean `L`.
- `L` is selected by the Politis–White automatic rule applied to the `r` series, floored at 1 and capped at `n/10`. The selected `L` is **reported**. If `L ≈ 1` the trades show no detectable serial dependence and the bootstrap reduces to the ordinary bootstrap; if `L` is large, the report says so, because that means outcomes cluster and the effective sample is much smaller than `n`.
- 10,000 resamples, seeded from `run_rng_seed`, so intervals are reproducible.
- Interval method: BCa (bias-corrected and accelerated) where computable, percentile otherwise; which one was used is reported.
- **Refusal:** below `n = 40` the bootstrap is not computed at all. An interval from 25 trades is a number, not information, and printing it invites belief.

Why a *stationary* bootstrap on trades rather than an ordinary one: session strategies cluster — a volatile fortnight produces a run of similar outcomes. Ordinary i.i.d. resampling would ignore that and produce intervals that are too narrow. The block bootstrap preserves local dependence.

## N.3 Metrics I am flagging as questionable

Per the brief's instruction to surface rather than silently change:

**Maximum drawdown as a point estimate — flagged, handled by specification.**
Max drawdown on a finite trade sequence is an extremum statistic. Its sampling distribution is wide and right-skewed, and it grows with `n` even for a strategy with fixed expectancy. Reporting "max drawdown was 8.3R" invites the reader to plan around 8.3R, when a second sample from the same process would routinely produce 14R. **Specification:** `max_drawdown_r` is never reported alone. It is always reported alongside the **bootstrap distribution of max drawdown** over 10,000 stationary-bootstrap resamples, with the median, 95th percentile and observed value shown together. The 95th percentile is the number a person should actually plan around. This is a strengthening, not a change, so it needs no amendment.

**Profit factor — flagged, retained with a caveat.**
Profit factor is bounded below by 0, unbounded above, undefined with no losses, and a monotone transform of the win/loss ratio for fixed R multiples. For a fixed-R-multiple strategy it carries almost no information beyond win rate. **Specification:** retained because it is a lingua franca, always shown with `n`, and footnoted as redundant with win rate for fixed-target strategies.

**Win rate as a headline — flagged, demoted.**
For a 2R-target strategy, win rate and expectancy are nearly the same statistic, and win rate invites the wrong intuition ("I'm right more than half the time"). **Specification:** `expectancy_r` is the only permitted `primary_metric` for a preregistration in V1. Win rate may be reported; it may not be pre-registered as the primary metric. The preregistration CLI enumerates permitted metrics and win rate is not among them.

**Sharpe ratio — confirmed excluded.**
Doc 1 §I3 excluded it. Correct: Sharpe on trade-level returns has no meaningful time normalisation and depends on trade frequency, so it is not comparable across strategies with different opportunity counts. Not implemented.

## N.4 The edge definition (PDLA-02) and evidence tiers

The Product Bible's criterion double-counts costs. The corrected criterion, proposed as an amendment:

> An **edge** is a positive expectancy in R, **already net of modelled costs**, whose stationary-bootstrap 95% lower bound exceeds zero, which survives year-by-year segmentation, which exceeds its placebo baseline, and whose lower bound remains above zero under 1.5× cost and slippage stress.

The cost-stress clause replaces the "exceeds modelled costs" clause and does the work the original was reaching for: it demands margin above the cost model rather than subtracting costs twice.

### N.4.1 Evidence tiers

The system labels every result with exactly one tier. Tiers are computed mechanically; they are never chosen by the user.

| Tier | Requirements |
|---|---|
| **NO EVIDENCE** | `n < 40`, or bootstrap 95% LB ≤ 0, or the placebo baseline's interval overlaps the strategy's |
| **EXPLORATORY** | LB > 0 on development data only. **Every development-only result is at most EXPLORATORY, regardless of magnitude.** |
| **PROMISING** | Development LB > 0; all mandatory robustness tests pass; LB remains > 0 at 1.5× cost stress; positive in ≥ 70% of individual years; **Šidák-adjusted** LB > 0 given the family run count |
| **VALIDATION-SUPPORTED** | PROMISING, plus a pre-registered validation run meeting its pre-registered threshold in the pre-registered direction, on data not used in development |
| **HOLDOUT-SUPPORTED** | VALIDATION-SUPPORTED, plus a pre-registered holdout run after a recorded unlock, meeting its threshold |

Three deliberate properties:
1. There is no tier called "proven," "validated edge," or anything implying certainty. The top tier is "holdout-supported," which is a statement about evidence, not truth.
2. PROMISING requires the *adjusted* lower bound, so a strategy that only clears the bar because it is attempt number 47 cannot reach it.
3. Every tier label is rendered with its `n`, its family run count, and its bootstrap `L`. A reader who sees HOLDOUT-SUPPORTED with `n = 63` and `L = 9` can see immediately that the effective sample is small.

Every report also carries a fixed footer: *"These tiers describe the strength of evidence gathered under a specified procedure. They do not establish that a relationship exists, and they say nothing about whether it will persist."*

---

# PART O — ROBUSTNESS SPECIFICATION

Robustness tests **attack** a strategy. None of them may be used to select parameters, and none of their outputs may be fed back into a manifest. The `robustness` module has no write path to `strategies/`.

| ID | Test | Method | Output | Pass interpretation | Limitation |
|---|---|---|---|---|---|
| RT-1 | Year-by-year segmentation | Recompute expectancy and Wilson/bootstrap intervals per admissible year | Table, n per year | Positive point estimate in ≥ 70% of years, and no single year contributing > 40% of total R | Small annual n makes annual intervals wide by construction |
| RT-2 | Cost stress | Re-run with commission scaled ×1.0, ×1.5, ×2.0, ×3.0 | Expectancy and LB vs multiplier | LB > 0 at ×1.5 | Does not capture broker spread markup; that needs the execution dataset |
| RT-3 | Spread stress | Re-run with all spreads inflated ×1.25, ×1.5, ×2.0 by widening ask and lowering bid symmetrically around the mid | Same | LB > 0 at ×1.5 | Symmetric widening is an approximation of real spread behaviour |
| RT-4 | Slippage stress | Re-run with `k_market`, `k_stop` scaled ×2, ×3 | Same | LB > 0 at ×2 | Slippage model is fitted from the same data; stress is a sensitivity, not a validation |
| RT-5 | Parameter perturbation | Each numeric parameter varied ±10% and ±20%, one at a time | Expectancy surface, one row per perturbation | Expectancy remains positive across the ±10% band, and the surface is not a spike | **These runs count toward the family total.** Perturbation is not free. |
| RT-6 | Timing perturbation | Shift the scan window ±5, ±15 minutes | Expectancy per shift | Result does not collapse under a 5-minute shift | A strategy legitimately keyed to a specific event may fail this and still be sound; interpret with the mechanism in mind |
| RT-7 | Best-trade removal | Drop the top 1%, 5%, 10% of trades by `r_net` | Expectancy after removal | Positive after removing the top 5% | Aggressive; a genuinely fat-tailed strategy may fail this legitimately |
| RT-8 | Ambiguity sensitivity | Re-resolve every ambiguous bar pessimistically | Delta in expectancy, ambiguous fraction | Delta < 20% of expectancy | Directly measures dependence on intrabar sequencing |
| RT-9 | Placebo baseline | Same opportunity timestamps, same stop and target distances, **random direction** with a seeded stream; 200 replications | Distribution of placebo expectancy vs actual | Actual expectancy outside the placebo distribution's 95% envelope | Tests directional edge only; a strategy whose edge is in *timing* needs RT-10 |
| RT-10 | Random-timing baseline | Same count of trades per day, same direction distribution, timestamps drawn uniformly within the scan window; 200 replications | Distribution vs actual | Actual outside the 95% envelope | Tests whether the entry timing matters at all |
| RT-11 | Regime segmentation | Split by `rv_ratio` terciles from the regime-check aggregates | Expectancy per regime | No regime with a strongly negative expectancy that would dominate a future period | Terciles are computed on pre-vault data |
| RT-12 | Reproducibility | Re-run with identical inputs | `result_sha256` comparison | Byte-identical | — |

RT-1, RT-2, RT-3, RT-8, RT-9 and RT-12 are **mandatory** before a strategy version may transition to VALIDATION_READY. The remainder are recommended and their absence is recorded and displayed.

**The optimisation firewall.** RT-5 produces a surface across parameter values. It is presented sorted by parameter value, never by expectancy, and the report contains no "best" row. The CLI has no command that selects a parameter set from a robustness result. If a user reads the surface and picks the peak, that is a new strategy version whose lineage records that its parent was a perturbation run — visible forever.

---

# PART P — FORWARD-TEST SPECIFICATION

Stage 6, manual in V1. The schema exists from Stage 3 so no retrofit is needed.

## P.1 Workflow

1. A strategy version in FORWARD_TESTING is scanned nightly against the latest ingested data: `jarvis forward opportunities --strategy X --day YYYY-MM-DD`. This writes the day's opportunities to the ledger exactly as a backtest would, using the same `scan` function. **The theoretical trade is computed too**, so the model's answer exists before the human's is entered.
2. The user logs their own record: `jarvis forward log --day YYYY-MM-DD`. The CLI presents each opportunity the scanner found and asks: noticed? taken? actual entry, stop, target, risk, times, notes.
3. The CLI then asks whether any trade was taken that the scanner did **not** produce. Those are recorded with `opportunity_id = NULL` and are automatically classified `error_class='strategy'` — a trade with no corresponding opportunity is by definition off-plan.
4. `jarvis forward reconcile --strategy X --from --to` produces the reconciliation report.

**Ordering safeguard.** Step 1 must complete before step 2 is permitted for that day, and step 2 must complete before the reconciliation shows the theoretical outcome. The CLI refuses to display a day's theoretical results until that day's forward log exists. This is the "prediction before revelation" mechanism from the Product Bible applied to live trading, and it is enforced by a check on `forward_records` existence, not by discipline.

## P.2 The three-way comparison

| Layer | Source | Meaning |
|---|---|---|
| **Theoretical** | `theoretical_trades` | What the rules would have produced under the execution model |
| **Intended** | `forward_records.intended_*` | What the user meant to do |
| **Actual** | `forward_records.actual_*` | What happened |

## P.3 Error decomposition

Computed deterministically, in this precedence order:

```
if opportunity_id is NULL                     → strategy        (traded off-plan)
elif noticed = 0                              → behavioural     (missed setup)
elif noticed = 1 and taken = 0                → behavioural     (declined a valid setup)
elif |intended_risk_r − 1.0| > 0.10           → risk            (sizing deviation)
elif |actual_entry − intended_entry| > tol_e  → execution       (entry deviation)
elif |actual_stop  − intended_stop|  > 0      → risk            (stop moved)
elif rule_violations non-empty                → behavioural
elif |actual_r − theoretical_r| > tol_r       → execution       (slippage/delay)
else                                          → variance
```

`tol_e` is `0.10 × spread_at_fill`; `tol_r` is 0.05R. Both are configurable and versioned.

**The distinction the whole layer exists for:** a losing month decomposed into `variance` is a normal losing month. The same month decomposed into `behavioural` and `risk` is a discipline failure wearing a market's clothing. The reconciliation report leads with this decomposition, not with PnL.

## P.4 Reconciliation report contents

- Opportunity funnel: generated → accepted by rules → noticed → taken → filled.
- Theoretical R vs actual R, cumulative, side by side.
- Error decomposition counts and R attribution per class.
- Execution delay distribution (`fill_ts − signal_ts`).
- Realised slippage vs modelled slippage — the single most valuable output, because a systematic gap between them means the backtest's execution model is wrong and every historical result needs re-reading.
- Missed-setup analysis: were the missed ones systematically different (time of day, day of week, spread state) from the taken ones?
- Rule violation log.

## P.5 Broker export (deferred, schema-ready)

Not implemented in V1. When it arrives, a broker export becomes a `dataset_versions` row with `role='execution'` for price data, and a separate importer populates `forward_records.actual_*` fields that are typed manually in V1. No schema change is anticipated. No broker write API, ever, in this project's V1 scope.

---

*Part 3 ends. Part 4 covers CLI, Reporting, Testing, Failure behaviour, Logging, Backup, Performance, Security, Documentation, the Roadmap, the Premium Credit Plan, the Work Package Template, Decision-Log Amendments, Open Questions, the Adversarial Review and the Ready-to-Build determination.*
