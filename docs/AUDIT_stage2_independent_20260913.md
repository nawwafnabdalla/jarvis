# Independent audit — Jarvis, at commit `5c63121`

Run from a clean clone of the public repo, in a separate environment from the one that wrote the code. Everything below was executed, not read: the suite was installed and run, contracts were run, and claims were tested directly rather than taken from prior reports.

**Caveat on scope:** this ran on Linux; the project is Windows-native. OS-specific behaviour (the `cp1252` console class of bug, path handling) would not reproduce identically here and is outside what this audit can speak to. `data/` is gitignored, so nothing here re-verifies the tick store, the R5 output, or anything else data-dependent — this is a code, contract, and document audit only.

---

## Verified as claimed

| Claim | Result |
|---|---|
| 453 tests pass | **Confirmed** — 453 passed, 2 deselected, clean |
| 11/11 architecture contracts KEPT | **Confirmed** — `lint-imports` reports 11 kept, 0 broken |
| Bootstrap is deterministic regardless of chunk size (D-068) | **Confirmed independently** — tested across a 400× chunk-size range (25 → 10,000 resamples per chunk); CI bounds bit-identical at every size. Seed control also verified both directions |
| No vacuous contracts remain after WP-017 | **Confirmed** — all 25 modules referenced in `.importlinter` resolve; no repeat of the `bars.storage` defect |
| Decision log integrity | **Confirmed** — 109 entries, zero duplicates, every `Superseded` entry correctly points forward to its replacement |
| Code hygiene | **Clean** — zero TODO/FIXME/HACK/placeholder markers anywhere in `src/`; no skipped or xfail tests |
| Tests that assert nothing | **None genuinely** — 4 flagged by AST scan are all legitimate "does not raise" tests (a bare call that throws if wrong), not vacuous |

---

## Finding 1 — "11/11 contracts KEPT" materially overstates current architectural assurance

**Severity: moderate. Not a defect; a measurement problem.**

WP-017 fixed one contract that passed by matching nothing (`jarvis.bars.storage` never existed). That fix addressed one *mechanism* of vacuity — a missing module. There is a second mechanism nobody has checked for: **a contract whose source module exists but is an empty skeleton cannot fire either.**

Twelve modules in `src/jarvis/` contain only a docstring — zero imports, zero code: `provenance`, `strategies`, `strategy_impls`, `opportunities`, `execution`, `experiments`, `vault`, `backtest`, `statistics`, `robustness`, `forward`. A forbidden-contract sourced only at such a module is structurally incapable of failing, because a module with no imports cannot violate an import restriction.

Classifying all eleven contracts:

| Contract | Status |
|---|---|
| Five-layer architecture | LIVE |
| purity rule | LIVE |
| **provenance depends on nothing but core** | **DORMANT — cannot fire** |
| single-reader rule | LIVE (proven by WP-017) |
| single-reader: ingest cannot read bars | LIVE |
| bars cannot depend on vault | WEAK — only target is an empty skeleton |
| features cannot depend on bars directly | LIVE |
| **strategies cannot depend on execution/experiments/statistics/vault** | **DORMANT — cannot fire** |
| **execution cannot depend on strategies or opportunities** | **DORMANT — cannot fire** |
| **vault depends on nothing beyond core, bars, experiments.ledger** | **DORMANT — cannot fire** |
| probe must not compute profitability | WEAK — all four targets are empty skeletons |

**5 genuinely live, 2 weak, 4 dormant.**

This is not wrong — pre-emptive contracts for unbuilt modules are good practice, and they become live automatically as those modules get built. The problem is purely that the headline number is cited as evidence. It has been cited that way repeatedly, including by me, in review after review.

The one worth singling out is **`probe must not compute profitability`**. Its own comment states the intent explicitly: *"This contract makes that mechanical rather than a matter of discipline."* It does not, today. All four forbidden targets (`backtest`, `statistics`, `robustness`, `execution`) are empty. Probe could compute an expectancy proxy inline with numpy and no contract would notice. Since D-042 was just produced by probe and the frequency-only discipline is exactly what made that result trustworthy, this gap sits directly under a load-bearing claim. It is not evidence of a violation — it is the absence of the mechanical guarantee that was claimed.

**Suggested resolution:** not new contracts. Add dormant/live classification to whatever reports contract status, so "11 kept" reads as "5 live, 2 weak, 4 dormant (pending their modules being built)". A one-line note in the decision log naming this distinction would stop the number being over-read in future reviews. Re-classify automatically rather than by hand, so it self-corrects as modules get built.

---

## Finding 2 — CLI-level-only enforcement is now a pattern, not an isolated gap

**Severity: low now, rising with each new instance.**

D-061a already records that the vault-boundary check lives at the CLI entry point, so direct Python calls to `resample_range`/`run_checks`/`compute` bypass it. That was logged as a known, deliberate, scoped limitation.

`compute_r5` is a second instance of the same shape. Its signature is `compute_r5(bars, *, start_ns, end_ns)` and its docstring states plainly that it *"does not re-filter by range itself... and trusts its caller."* The 2007–2014 restriction (D-067, the AR-1 hypothesis-generation firewall) is enforced only by `cli/main.py:607` calling `stage2_descriptive_range()`. A direct Python call could compute R5 over any range, including 2015–2018 — the exact period AR-1 exists to keep unseen.

Both instances are individually defensible and honestly documented. The concern is the trend: two independent research-integrity boundaries (vault access, hypothesis-generation scope) now rest on CLI discipline rather than structure, and R1/R2 will inherit the same pattern. AR-5's original reasoning — that the vault boundary should be a capability/type-level property rather than convention, because convention decays — applies identically here.

**Suggested resolution:** nothing urgent, but worth a decision-log entry naming the pattern (rather than leaving it as two unrelated per-package footnotes), and worth deciding deliberately whether `vault.GatedReader`, when built, should be the single structural answer for both cases.

---

## Finding 3 — decision log skips D-034 with no explanation

**Severity: trivial.**

Numbering runs D-001 → D-069 with exactly one gap: D-034 does not exist. Every other number is present, unique, and correctly cross-referenced. Most likely a drafting artefact between D-033a and D-035. Worth one line noting it as intentionally unused, so a future reader doesn't hunt for a lost decision.

---

## What this audit did not cover

- Anything data-dependent (tick store, R5's real output, reconciliation figures) — `data/` and `reports/` are gitignored.
- Windows-specific behaviour.
- The substantive correctness of R5's statistics beyond the bootstrap utility itself.
- Whether the Technical Bible's own specifications are internally consistent beyond the areas already covered by D-067/D-069.

---

## Overall

The codebase is in strong shape and the standard of the work is visibly high: the test suite is clean, the decision log is unusually rigorous and well cross-referenced, the newest code (`core/bootstrap.py`, `describe/periods.py`) is careful and well-reasoned, and the one non-obvious correctness claim tested here held bit-identically under independent verification.

The one finding worth acting on is Finding 1 — not because anything is broken, but because the number being used as a proxy for architectural safety currently measures less than it appears to, and this project's own D-028 lesson ("62/62 tests pass" being literally true while a real defect sat one layer beneath) is precisely about that failure mode.
