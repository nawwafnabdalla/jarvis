# JARVIS — PROJECT HANDOVER

Paste this at the start of a new chat to restore full context. This document lives outside the repo by design — it's a context-priming doc, not a build deliverable. (First confirmed check of the actual repo found no `HANDOVER.md` anywhere in `docs/` or git history — if you want it committed there too going forward so a fresh Claude Code session can read it directly, that's a reasonable change, just say so.)

---

## What Jarvis is

A personal, local, deterministic FX research system for GBP/USD. **A research instrument, not a trading bot.** The standard it's held to: *"I can trust what it tells me, reproduce the result, and become more competent by using it."* A beautiful wrong backtest is a failed product.

Built for one user (Nawwaf, 20, degree apprentice at J.P. Morgan, ~1hr/day). Genuine blank slate — no candidate setups. Core discipline: **market description before strategy testing**.

**Frozen exclusions (V1):** no AI layer, no live trading, no ML, no funded-challenge simulation, no web UI, no auto-optimisation, no multi-pair. Windows-native Python 3.12, fully local.

---

## Current state

**Stage 0 is complete.** **The full HistData ingest (WP-009 and its entire correction thread) is complete, verified, and merged to `main`. WP-010 (wiring resample/QA to actually read the corrected dataset) is also complete and merged.** This is the first point at which the dataset is genuinely ready for a real Stage 1A run — everything before this was either Stage 0 scaffolding or ingest work still being corrected.

**Repo:** `https://github.com/nawwafnabdalla/jarvis` (public — review is done by cloning it, not reading summaries)

**Latest commit:** `3028844` (WP-010, on `main`). 380 tests passing under both invocations, 11/11 architecture contracts (dependency count 116→111 after WP-010's Dukascopy-path simplification).

**Docs confirmed to actually exist in `docs/`** (checked directly as of WP-010): `DECISION_LOG.md`, `PRODUCT_BIBLE.md`, `TECHNICAL_BIBLE_1-4.md`, `WP-009-TZ-FINDING.md`. **A previous version of this handover assumed `AUDIT_stage0_codebase_and_spec.md` and `STAGE_1A_RUNBOOK.md` also existed there — a direct check did not find them.** Worth confirming before relying on the runbook for the next step: does it exist somewhere else, or does it need writing/rewriting now that the ingest pipeline it describes has changed substantially?

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
| `probe` | Stage 0 contexts, two-tier gate, report — not yet run against the corrected dataset |
| `cli` | `jarvis` console script |

### What's NOT built

- Stage 1B: dataset manifests, sealing, dataset versions
- Stage 2: market description engine
- Stages 3–6: strategy machinery, backtester, statistics, forward testing

---

## The one thing that must happen next

**Run the real Resample → Validate → Features → Probe pass** — the operational run, not a work package — against the now-fully-corrected dataset, and produce **D-042**: `PROCEED GBP/USD`, `WIDEN CONTEXT`, or `CONSIDER EUR/USD FALLBACK`.

Nothing code-side is blocking this anymore. This is genuinely the first time that's been true.

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
| D-045a | **Every item a coding agent raises in closing notes gets an explicit disposition** — acted on, deferred with a log entry, or dismissed with a reason |
| D-054 | A data-convention claim requires discriminating evidence spanning the **full** ingest range, not a sample |
| D-055 | **The corrected timezone model** — no HistData file is ever on a fixed clock; two DST calendars (US era 2006-2018, EU era 2019-2022), switching once, at 00:00 UTC the Monday after the EU transition |
| D-058 | Tick storage dedup keys on **`(ts_utc_ns, row_sequence)`**, never `ts_utc_ns` alone — genuinely distinct same-millisecond quotes must not collapse |
| D-059 | **Dukascopy retired from the forward-testing role.** HistData's own ~weekly update cadence for GBP/USD is sufficient, and using one verified source throughout avoids a vendor-inconsistency between backtest and forward-test data. Fetcher code retained, dormant, not deleted |
| D-060 | Resample/QA retargeted to read `data/tick/` directly (WP-010) |

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

---

## Immediate next steps

1. **Confirm whether `STAGE_1A_RUNBOOK.md` (or equivalent) exists anywhere, or needs writing/rewriting** — the ingest pipeline it would describe has changed substantially since any earlier version.
2. **Run Resample → Validate → Features → Probe for real**, against the corrected dataset.
3. **D-042** — the gate decision.

Everything downstream of the probe (Stage 1B onward) remains untouched and unstarted.
