# JARVIS — PROJECT HANDOVER

Paste this at the start of a new chat to restore full context. It is a context-priming doc, not a build deliverable, but it is committed to `docs/` in this repo (since commit `3d6e850`) so a fresh Claude Code session can read it directly.

---

## What Jarvis is

A personal, local, deterministic FX research system for GBP/USD. **A research instrument, not a trading bot.** The standard it's held to: *"I can trust what it tells me, reproduce the result, and become more competent by using it."* A beautiful wrong backtest is a failed product.

Built for one user (Nawwaf, 20, degree apprentice at J.P. Morgan, ~1hr/day). Genuine blank slate — no candidate setups. Core discipline: **market description before strategy testing**.

**Frozen exclusions (V1):** no AI layer, no live trading, no ML, no funded-challenge simulation, no web UI, no auto-optimisation, no multi-pair. Windows-native Python 3.12, fully local.

---

## Current state

**Stage 0 is genuinely, fully complete — including the real operational run, not just the scaffolding.** The full HistData ingest (WP-009 and its correction thread), WP-010 (resample/QA wired to the corrected dataset), and WP-012 (vault-boundary enforcement, `year_admissibility` fixed) were all prerequisites. On 2026-09-13, `docs/STAGE_1A_RUNBOOK.md` was executed for real — Resample → Validate → Features → Probe, against the full corrected 2006–2022 dataset — surfacing and fixing two genuine defects along the way (WP-013/D-063: a QA zero-spread false-positive and a sample-cap bug; WP-014/D-064: an INFO-check count-capping bug and a design flaw) plus one CLI output bug found by the run itself (WP-015/D-065: a Windows-console `UnicodeEncodeError` crash on the probe's own `∩` output character). The baseline probe returned `WIDEN_CONTEXT`; the one permitted widening attempt (`break_buffer_atr`, chosen and justified in writing before its result was known) moved the result only marginally and it stayed `WIDEN_CONTEXT` — a real gap in the original gate specification (D-024), since it never said what to do in that case. **D-024b rules on that gap** (EUR/USD fallback deferred not triggered, multi-pair rejected outright) and **D-042 is now finalized**: **GBP/USD is confirmed as the instrument going forward, not provisionally.**

**Stage 0 is closed. Stage 2 (market description) is the next stage and has not been started.**

**Repo:** `https://github.com/nawwafnabdalla/jarvis` (public — review is done by cloning it, not reading summaries)

**Latest commit:** `93f60b4` (WP-016, D-024b/D-042, Stage 0's conclusion), on `main`. 399 tests passing under both invocations, 11/11 architecture contracts.

**Docs confirmed to actually exist in `docs/`** (checked directly): `DECISION_LOG.md`, `PRODUCT_BIBLE.md`, `TECHNICAL_BIBLE_1-4.md`, `WP-009-TZ-FINDING.md`, `STAGE_1A_RUNBOOK.md`, `HANDOVER.md` (this file, now committed to the repo rather than living outside it). `AUDIT_stage0_codebase_and_spec.md` still does not exist — no longer relevant now that Stage 0 has concluded.

### What's built

| Module | Does |
|---|---|
| `core` | IDs, canonical JSON hashing, error hierarchy, config |
| `provenance` | git state, code hashing (stub until Stage 1B) |
| `timeengine` | UTC nanoseconds, IANA conversion, DST fold policy, trading day/week |
| `sessions` | Versioned session sets, membership, windows, derived intersections |
| `ingest` | HistData CSV ingest (primary, fully corrected) + Dukascopy fetcher (retained, dormant — see D-059) |
| `bars` | Tick → 1-minute resampler, **now reading from `data/tick/` (the HistData store), not Dukascopy blobs** |
| `qa` | Data-integrity checks, **retargeted to the HistData store**; Dukascopy-only fetch-log checks (E-04/W-06/E-05/E-06) dormant, not deleted |
| `features` | 5 features + auto-generated leakage harness |
| `probe` | Stage 0 contexts, two-tier gate, report — **run for real against the corrected dataset (2026-09-13); D-042 is final** |
| `cli` | `jarvis` console script — output crash (WP-015) fixed, `OutputError`/exit 4 added |

### What's NOT built

- Stage 1B: dataset manifests, sealing, dataset versions
- **Stage 2: market description engine — the next stage, not started**
- Stages 3–6: strategy machinery, backtester, statistics, forward testing

---

## The one thing that must happen next

**Stage 0 is closed.** The gate decision (D-042) is final: GBP/USD confirmed, per D-024b's ruling on the gap D-024 didn't originally cover (see the decision log). There is no outstanding Stage 0 work.

**Stage 2 (market description) is next.** It has not been started — no scaffolding, no design work, nothing beyond the fact that it's the next stage on the roadmap (D-007). Carry `C-D∩C-C`'s narrowness (a wide pre-London range genuinely followed by a further breakout-continuation less often than the gate's 100/60 design assumed) forward as a known, evidenced characteristic of GBP/USD worth explaining there — not as an unresolved question.

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

---

## Immediate next steps

Stage 0 has no outstanding work. **Stage 2 (market description) is next** and has not been started — no scaffolding exists yet. When that work begins, carry forward: `C-D∩C-C`'s narrowness as a known, evidenced characteristic of GBP/USD (D-024b/D-042), and the same working-process discipline that found four real defects during Stage 1A's execution alone — verify against the real repository and real run output, never against a narrative summary.

Everything from Stage 1B onward remains untouched and unstarted.
