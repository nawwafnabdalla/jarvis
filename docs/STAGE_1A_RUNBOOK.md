# Stage 1A Operational Runbook — Resample → Validate → Features → Probe

**WP-011. Spec only. Nothing in this document has been executed.** Every command
below was verified against the actual current CLI source
(`src/jarvis/cli/main.py`) and the actual current `probe`/`gate` source as of
commit `3d6e850`, not assumed from memory of earlier work packages. This is a
fresh document — the `STAGE_1A_RUNBOOK.md` referenced early in this project
was written for the abandoned Dukascopy fetch-and-rate-limit pipeline and does
not exist anywhere in this repo or its git history; nothing here is inherited
from it.

Execution is a separate, later step, gated on this document being reviewed
and approved.

---

## 0. Pre-flight issue — RESOLVED (WP-012 / D-061, commit `a98f23e`)

**This blocker is fixed. This section is a historical record of what it was
and how it was closed, kept so the reasoning stays visible — it is not a
live blocker and nothing below requires re-checking it before running.**

Verifying the actual current source when this runbook was first written
(not just the CLI, as required, but the code each CLI command calls)
surfaced a real defect that would have silently invalidated the entire run.
It was reported here rather than worked around, per this project's standing
rule, and fixed in the very next package rather than during the run itself.

**What was wrong:** `jarvis.probe.report.year_admissibility` (called
internally by `run_probe`, which is what `jarvis stage0 probe` invokes)
determines whether a year counts toward the gate's percentile statistics
using two checks: `jarvis.qa.run_checks` reporting zero ERROR findings
(correctly retargeted to `data/tick/` by WP-010/D-060), and a presence
check that — until this fix — still called `jarvis.ingest.urls.raw_blob_path`
and checked for a **Dukascopy `.bi5` blob** on disk for every trading-week
hour, requiring ≥95% presence. `data/raw/ticks/GBPUSD/` held 224 leftover
files from an unrelated 2024-01 diagnostic and zero real 2006–2022 coverage,
so every year measured a 0.0 ratio and failed admissibility before
`run_checks` was ever reached. With `_MIN_ADMISSIBLE_YEARS = 12` and zero
years ever passing, `evaluate_gate` returned `INSUFFICIENT_DATA`
unconditionally, regardless of how complete the real dataset actually was.

**The fix (WP-012 / D-061):** the presence check was retargeted to
`data/tick/`, reusing WP-010's own "a hole is a wholly-missing month" model
(`tick_path(...).is_file()` per calendar month) instead of Dukascopy blob
presence. Verified directly: a regression test builds 20 years of realistic
synthetic `data/tick/` coverage and confirms `evaluate_gate` no longer
returns `INSUFFICIENT_DATA` unconditionally — it would have failed against
that exact test before this fix. Full decision-log entry: **D-061**
(`docs/DECISION_LOG.md`).

**Same package also closed the vault-boundary enforcement gap** referenced
in Section 6.C below — see that section's own updated text, and **D-061a**
for the one residual, deliberately-deferred limitation (enforcement is
CLI-level, not embedded in `resample_range`/`run_checks`/`compute`
themselves — irrelevant to this runbook specifically, since every step below
runs through the CLI, never a direct Python call).

---

## 1. What each step actually consumes — verified from source, not assumed

The pipeline's name suggests a strict chain; the actual code is looser than
that in one place worth being explicit about:

| Step | CLI command | Reads | Writes | Actually depends on |
|---|---|---|---|---|
| 0. Ingest | *(already done)* | HistData source zip | `data/tick/` | — |
| 1. Resample | `jarvis data resample` | `data/tick/` | `data/bars_1m/` | Step 0 |
| 2. Validate | `jarvis data validate` | `data/tick/` (tick-level checks) + `data/bars_1m/` (bar-level checks) | `reports/qa/*.md`, `*.parquet` | Step 0 (tick checks), Step 1 (bar checks) |
| 3. Features | `jarvis features build` | `data/bars_1m/` | `data/features/` | Step 1 |
| 4. Probe | `jarvis stage0 probe` | `data/bars_1m/` **only** | `reports/stage0/*.md`, `*.parquet`, `_lineage/*.json` | Step 1 |

**Probe does not read `data/features/` at all.** `run_probe`
(`src/jarvis/probe/report.py:345-349`) calls `read_bars(...)` then
`compute(list(_REQUIRED_FEATURES), bars, session_set).frame` directly —
it computes its four required features (`pre_london_high`, `pre_london_low`,
`pre_london_range_pct`, `atr_bars`) fresh, in memory, from bars, every time
it runs. It never opens a `data/features/` Parquet file. So Step 3 is not a
code-level prerequisite of Step 4 — Probe would produce an identical result
if Step 3 were skipped entirely. Step 3 is still worth running: it's part of
this pipeline's stated name, it produces the persisted feature store Stage 2+
will actually need later, and skipping it here would leave that store empty
for no benefit. But nobody should be surprised that Probe works before
Features has ever been run, or conclude something is broken if Step 3's
output looks unused downstream right now — it is, by design, until Stage 2.

**Probe also re-runs a form of Step 2 internally, per year, as part of its
own admissibility check** (`year_admissibility` calls `run_checks` again for
each candidate year). Step 2's standalone run is for producing a reviewable
report artifact and applying the ERROR-halts rule (Section 4) before Steps 3
and 4 run against data nobody has looked at — it is not redundant with
Probe's internal check in purpose, even though both call the same function.

---

## 2. Exact date ranges, tied to their governing decisions

| Parameter | Value | Governing decision |
|---|---|---|
| Resample/Validate/Features range start | `2006-09-01T00:00:00Z` | D-036: ingest from 2006-09-01 so the first ~60 trading days of 2007 have the prior data `pre_london_range_pct(60)` and `atr_bars` need |
| Probe range start | `2006-09-01T00:00:00Z` (**same as above, not 2007-01-01**) | D-036a: read range and evaluation range are separate concepts. The read range must include the warmup period or the same deflation D-036 fixed reappears. "Context counting starts 2007-01-01" is achieved **not** by truncating the read range, but because 2006 is a partial calendar year (data exists only Sep–Dec) and therefore fails `year_admissibility`'s ≥95%-hours-present threshold on its own — it is naturally excluded from the gate's percentile statistics without any special-casing. Confirm this happened by checking 2006 shows `Admissible: False` in the per-year table (Section 6) — if it shows `True`, something is wrong and this needs to stop, not be waved through |
| Resample/Validate/Features range end | `2023-01-01T00:00:00Z` | Matches the full on-disk ingest range (2006-09 through 2022-12, confirmed via directory listing — `month=09` through `month=12` under `year=2006`, `month=01` through `month=12` under `year=2022`) |
| Probe range end | `2023-01-01T00:00:00Z` | C4 / C4b / PDLA-03 (D-021): Stage 0 runs on 2007–2022 only, vault (2023 onward) untouched, even for descriptive purposes. `VAULT_BOUNDARY_NS` in `probe/report.py` is exactly this instant |

**On `--to 2023-01-01T00:00:00Z` specifically:** this value must be *accepted*
by `jarvis stage0 probe`, not refused. The check in `run_probe`
(`src/jarvis/probe/report.py:333`) is `if end_ns > VAULT_BOUNDARY_NS: raise
UserError(...)` — strict `>`, not `>=`. Since `end_ns` here equals
`VAULT_BOUNDARY_NS` exactly, the condition is `False` and the call proceeds.
This is D-047a's fix (the SIXTH PM ERROR: an earlier `>=` here silently
dropped 2022-12-31 from the gate, on the P10 leg deliberately most sensitive
to the worst year) and it is **correct, current behaviour** — if
`--to 2023-01-01T00:00:00Z` is ever refused when this runs, that is the fixed
defect reappearing and must stop the run for investigation, not be read as
the boundary "working as intended."

Every date above is midnight UTC and therefore hour-aligned, satisfying
`_parse_iso_utc_ns`'s alignment check (`src/jarvis/cli/main.py:151-162`),
which every one of these commands enforces identically.

---

## 3. Exact command sequence

Verified directly against `src/jarvis/cli/main.py` (line numbers as of commit
`3d6e850`). Run in this order:

### Step 0 — Ingest (already complete; not run by this runbook)

`data/tick/instrument=GBPUSD/` already contains all 196 months, 2006-09
through 2022-12, per D-056/D-058. No action.

### Step 1 — Resample (`data_resample`, `cli/main.py:225-268`)

```bash
jarvis data resample --from 2006-09-01T00:00:00Z --to 2023-01-01T00:00:00Z
```

Do **not** pass `--allow-incomplete`. Every month in this range is present on
disk (verified above); if resample reports any missing month now, that is a
new, unexplained regression and must halt for investigation — the flag exists
to consciously accept a known hole, not to suppress an unexpected one.

Expect the printed report to show `Months missing 0` and `Months with data
196`.

### Step 2 — Validate (`data_validate`, `cli/main.py:271-308`)

```bash
jarvis data validate --from 2006-09-01T00:00:00Z --to 2023-01-01T00:00:00Z
```

Apply the QA-finding-severity rule in Section 4 to the result before
proceeding to Step 3. Exit code 3 means at least one ERROR finding exists
(`report.errors > 0`, `cli/main.py:307-308`) — per Section 4, that halts the
run pending review, it does not mean "retry" or "ignore and continue."

### Step 3 — Features (`features_build`, `cli/main.py:379-433`)

```bash
jarvis features build --from 2006-09-01T00:00:00Z --to 2023-01-01T00:00:00Z
```

No `--features` flag — omitting it computes every registered feature
(`tuple(REGISTRY)`, `cli/main.py:397`), which is what should be persisted to
`data/features/`.

### Step 4 — Probe (`stage0_probe`, `cli/main.py:436-511`)

**Blocked on Section 0 being fixed first.** Once it is:

```bash
jarvis stage0 probe --from 2006-09-01T00:00:00Z --to 2023-01-01T00:00:00Z
```

No `--widen`. This is the baseline run. `run_probe` refuses to widen a
lineage more than once (`has_prior_widening`, enforced in
`cli/main.py:469-475`) — if the baseline decision is `WIDEN_CONTEXT`, the
single permitted follow-up is:

```bash
jarvis stage0 probe --from 2006-09-01T00:00:00Z --to 2023-01-01T00:00:00Z --widen range_pct_max
```

or `--widen break_buffer_atr` — exactly one of the two, exactly once, per
`ProbeParams`'s widen targets (`probe/report.py:56-59`). Which one (if
either) to use is a decision for whoever reviews the baseline result, not
specified here.

Exit code 3 on `WIDEN_CONTEXT`, `CONSIDER_EURUSD_FALLBACK`, or
`INSUFFICIENT_DATA`; exit 0 on `PROCEED_GBPUSD` or
`PROCEED_GBPUSD_WITH_INSTABILITY_WARNING` (`cli/main.py:510-511`).

---

## 4. QA-finding-severity handling rule (a decision this runbook is proposing, not a pre-existing fact)

No rule for this currently exists anywhere in the codebase or decision log.
`run_checks` never raises on an ERROR finding by design (`qa/report.py`'s own
docstring: "it never raises on an ERROR finding — the CLI's exit code is the
gate"), so nothing enforces a stop automatically outside the CLI's own exit
code. This runbook proposes:

- **Any ERROR finding, on any month, halts the run** — proceed to Step 3/4
  only after every ERROR is reviewed and understood. An ERROR at this stage
  plausibly indicates a real problem worth seeing before it feeds a research
  result that everything downstream will trust.
- **WARNING and INFO findings do not block proceeding.** Compile them into a
  summary (count by check ID, across the whole range) attached to the run's
  own record, but continue the sequence.

**This is a decision, not a fact, and is not yet logged.** If adopted when
this runbook is executed, it needs its own decision-log entry at that time —
this document does not assign it a number or mark it Active, since WP-011 has
no authority to adopt a rule on its own behalf.

---

## 5. Known, already-fixed findings — do not mistake these for new surprises

A clean re-run of this pipeline should show the following as either absent,
or present but already understood — none of these are new information if
they appear:

- **2009-05** (D-055h/D-056): mixed ask-bid/bid-ask column order mid-month.
  Already fixed at ingest time. Should import and resample cleanly; no action
  if it does.
- **2009-11** (D-055g): five genuine zero-spread quotes, once misread as
  corruption. Already fixed. Should not appear as an E-01/E-02 finding.
- **2006-09 through 2011-04, tick-collision dedup** (D-057/D-058): up to 38%
  of rows in the worst month were once silently discarded by a too-narrow
  dedup key. Already fixed and the store already rebuilt under the corrected
  key. Tick counts for this window should already reflect the fix — this is
  not something to re-discover or re-fix during this run.
- **I-02 "Volume all-zero" findings, every month, every year** (WP-010/D-060):
  HistData ticks carry no real volume field at all — every value is null,
  filled to `0.0`. This check will now fire routinely across the entire
  range. It is INFO severity, correct and expected, and never affects
  sealability — do not investigate it as an anomaly.
- **D-047b, thin days**: QA's W-05 (thin trading day) is a *reporting* finding
  only — there is no `thin_day` flag threaded into `probe`'s own counting.
  A WARNING/INFO-heavy year (e.g. one with unusual holiday clustering) is not
  itself evidence of a data problem, and probe's counts for that year are
  **not** adjusted for it. If a probe-run count looks implausible for a
  specific year, this is the documented first thing to check, per D-047b's
  own text — but it explains a possible number, it does not indicate the
  pipeline needs a fix.

---

## 6. Final reconciliation, before D-042's output is treated as final

Three checks, all against actual output — logs and files, not assumption:

**A. Total context counts reconcile across two independent reads.** The
probe's own markdown report (`reports/stage0/STAGE0__*.md`, section "Per-year
counts") gives a per-context, per-year table built from `YearCounts.per_context`.
Independently sum each context's count directly from the raw events sidecar
(`reports/stage0/STAGE0__*.parquet`, one row per detected event — group by
`context`, count rows, compare against the sum of that context's column
across every row of the markdown table). These must match exactly — they are
two different code paths over the same underlying `events` tuple
(`write_report`'s markdown renderer vs. a fresh read of what it wrote to
Parquet), so a mismatch means the report itself is wrong, not that the data
is.

**B. Spot-check one year's count against an independently-derived
expectation.** Pick a year comfortably inside Development, away from both the
2018/2019 DST-calendar-era boundary (D-055) and any of the specific known-
finding months in Section 5 — 2015 is a reasonable choice. For **C-A**
specifically (the simplest of the four contexts: evaluated once per day, not
per-direction, per `probe/contexts.py:151`) filter the events Parquet to
`trading_day` in 2015 and `context == "C-A"`, and sanity-check the resulting
count against the year's own reported `context_eligible_days` (also in the
per-year table) — the C-A count must not exceed `context_eligible_days` for
that year, and should be a plausible fraction of it given the context's own
threshold condition, not a number this document pre-computes (that requires
the actual run). If the count exceeds eligible days, or is zero across a
full year with no admissibility failure, stop — one of those is a hard
contradiction and the other is very unlikely to be a genuine result.

**C. Confirm no vault-sealed data was touched — from the actual commands run,
not from assuming the boundary held.** **Updated by WP-012/D-061 (commit
`a98f23e`):** all four commands now have a code-level refusal above
`2023-01-01T00:00:00Z` — `reject_vault_range` (`probe/report.py`), called by
`data resample`, `data validate`, `features build`, and `stage0 probe` alike.
This was previously true only for `stage0 probe`; the other three would have
processed a >2023 range without complaint. The remaining, deliberately-
deferred limitation (**D-061a**) is that this enforcement lives at the CLI
entry point, not inside `resample_range`/`run_checks`/`compute` themselves —
**not a gap for this runbook specifically**, since every command below is
invoked through the `jarvis` CLI, never a direct Python call, so the code-
level check is live for every step here. There is still no `GatedReader` or
vault-access audit log (D-036a defers that to Stage 1E, which does not
exist), so the confirmation below is still a manual cross-check — now
confirming the code-level guard actually fired as expected on every
invocation, not compensating for its total absence on three of four
commands. Cross-check the actual `--to` value passed in every command
actually invoked during the run (the terminal transcript, and each report's
own printed range: resample and validate echo `--from`/`--to` back at the
top of their output; the probe and features reports both record `Dataset
range` in their own metadata section) against `2023-01-01T00:00:00Z`.
Confirm every single one is `<=` that value
before treating D-042 as final. This is a real gap worth naming plainly: the
vault boundary is enforced by code for exactly one of the four steps, and by
runbook discipline alone for the other three.

---

## 7. What this document is not

This package wrote a specification. It did not run `jarvis data resample`,
`jarvis data validate`, `jarvis features build`, or `jarvis stage0 probe`,
and it produced no bars, no features, no QA report, no probe report, and no
D-042 decision. Zero code was changed. Execution — including fixing Section
0's blocking issue first — is a separate, later step, and happens only after
this document has been reviewed and approved.
