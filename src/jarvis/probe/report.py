"""Stage 0 report writing, run orchestration, and run-lineage tracking.

EXPLORATORY -- FREQUENCY ONLY -- NOT EVIDENCE OF PREDICTIVE VALUE.
"""

import json
import os
import subprocess
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

import polars as pl

from jarvis.bars import read_bars
from jarvis.core.errors import IntegrityError, UserError
from jarvis.core.hashing import canonical_json
from jarvis.core.types import Nanos
from jarvis.features import FEATURE_SET_VERSION, compute
from jarvis.ingest.histdata_import import tick_path
from jarvis.qa import run_checks
from jarvis.sessions import SessionSet, load_session_set
from jarvis.timeengine import NS_PER_HOUR, trading_day, trading_day_bounds

from jarvis.probe.contexts import ContextEvent, ProbeParams, context_eligible_days, detect_events
from jarvis.probe.gate import INTERSECTION_KEYS, GateResult, YearCounts, evaluate_gate

WATERMARK = "EXPLORATORY -- FREQUENCY ONLY -- NOT EVIDENCE OF PREDICTIVE VALUE"

D024A_QUALIFICATION = (
    "D-024a: the gate thresholds are a conservative research-design choice, "
    "not a statistical law. They encode an assumption that a directional "
    "filter and entry trigger remove 50-75% of context instances, and a "
    "judgement that ~150 holdout events is the minimum at which a small "
    "effect is distinguishable from zero. Both are defensible; neither is "
    "proven. They may be changed only through the decision log and only on "
    "evidence -- never because a probe failed to clear them."
)

CC_FORWARD_WINDOW_CAVEAT = (
    "C-C looks forward 60 bars from the break. For counting occurrences "
    "this is legitimate -- the probe measures how often a pattern "
    "completes, not what is tradeable at the break instant. But a C-C "
    "count is therefore NOT a count of tradeable opportunities, because at "
    "the break you do not yet know whether re-entry will occur."
)

# PDLA-03 / D-021: Stage 0 runs on 2007-2022 only. The vault (2023 onward)
# is untouched, including for descriptive purposes -- instrument selection
# informed by holdout years is contamination at the highest level.
VAULT_BOUNDARY_NS = Nanos(int(datetime(2023, 1, 1, tzinfo=timezone.utc).timestamp()) * 1_000_000_000)


def reject_vault_range(end_ns: Nanos, *, caller: str) -> None:
    """Raise UserError if end_ns reaches into the sealed vault (2023-01-01
    onward) -- PDLA-03/D-021: the vault is untouched by anything Stage 0
    or Stage 1A related, including for descriptive purposes.

    Strict >, not >=: end_ns is an EXCLUSIVE upper bound (this project's
    [start, end) convention since WP-001), so end_ns == VAULT_BOUNDARY_NS
    (2023-01-01T00:00:00Z) reads through 2022-12-31T23:59:59.999999999Z
    and touches no vault data -- it must be ACCEPTED, not refused
    (D-047a/WP-008-CORRECTION: an earlier `>=` here silently dropped
    2022-12-31 from the gate, on the P10 leg deliberately most sensitive
    to the worst year, to satisfy a literally-read acceptance criterion
    that was itself in error).

    WP-012: extracted from run_probe's own inline check (which was the
    only one of the four Stage 1A commands enforcing this in code) so
    every command that accepts a user-supplied end date can enforce the
    vault boundary from one implementation instead of each carrying its
    own copy that could drift. `caller` names the command in the error
    message only."""
    if end_ns > VAULT_BOUNDARY_NS:
        raise UserError(
            f"{caller}: --to exceeds the vault boundary (2023-01-01T00:00:00Z, "
            f"end_ns={end_ns}) -- --to must not exceed 2023-01-01T00:00:00Z "
            "(exclusive). The vault (2023 onward) is untouched, including for "
            "descriptive purposes (PDLA-03/D-021)"
        )


_MIN_ADMISSIBLE_MONTHS_RATIO = 0.95
_REQUIRED_FEATURES = ("pre_london_high", "pre_london_low", "pre_london_range_pct", "atr_bars")

_WIDEN_TARGETS = {
    "range_pct_max": 0.40,
    "break_buffer_atr": 0.05,
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def widened_params(widen: str | None) -> ProbeParams:
    """ProbeParams for a baseline (widen=None) or single-widened run.
    Raises UserError for an unrecognised --widen target -- the CLI passes
    this straight through as a bad-argument error, not a silent no-op."""
    if widen is None:
        return ProbeParams()
    if widen not in _WIDEN_TARGETS:
        raise UserError(
            f"unknown --widen target {widen!r}; must be one of {sorted(_WIDEN_TARGETS)}"
        )
    return ProbeParams(**{widen: _WIDEN_TARGETS[widen]})


def _code_sha(repo_root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except OSError:
        return "unknown"
    if result.returncode != 0:
        return "unknown"
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# Run lineage
# ---------------------------------------------------------------------------


def lineage_path(repo_root: Path, instrument: str, start_ns: Nanos, end_ns: Nanos) -> Path:
    return repo_root / "reports" / "stage0" / "_lineage" / f"{instrument}__{start_ns}__{end_ns}.json"


def read_lineage(repo_root: Path, instrument: str, start_ns: Nanos, end_ns: Nanos) -> list[dict]:
    """Prior runs recorded for this exact (instrument, start_ns, end_ns)
    lineage. An absent file means no prior runs -- a valid state, not an
    error."""
    path = lineage_path(repo_root, instrument, start_ns, end_ns)
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IntegrityError(f"lineage file {path} is malformed: {exc}") from exc
    return raw.get("runs", [])


def has_prior_widening(runs: list[dict]) -> bool:
    return any(r.get("widened_from") is not None for r in runs)


def record_lineage_run(
    repo_root: Path,
    instrument: str,
    start_ns: Nanos,
    end_ns: Nanos,
    *,
    widened_from: str | None,
    decision: str,
) -> int:
    """Append one run record, atomically. Returns this run's 1-based
    run_number. Every probe run -- including widenings -- is recorded
    here so the number of attempts before a gate decision is never lost."""
    path = lineage_path(repo_root, instrument, start_ns, end_ns)
    path.parent.mkdir(parents=True, exist_ok=True)
    runs = read_lineage(repo_root, instrument, start_ns, end_ns)
    run_number = len(runs) + 1
    runs.append(
        {
            "run_number": run_number,
            "timestamp": _utc_now_iso(),
            "widened_from": widened_from,
            "decision": decision,
        }
    )
    tmp_path = path.with_name(path.name + ".tmp")
    try:
        tmp_path.write_text(canonical_json({"runs": runs}), encoding="utf-8")
        os.replace(tmp_path, path)
    except OSError as exc:
        tmp_path.unlink(missing_ok=True)
        raise IntegrityError(f"failed to write lineage file {path}: {exc}") from exc
    return run_number


# ---------------------------------------------------------------------------
# Year admissibility
# ---------------------------------------------------------------------------


def _year_bounds(year: int) -> tuple[Nanos, Nanos]:
    start = int(datetime(year, 1, 1, tzinfo=timezone.utc).timestamp()) * 1_000_000_000
    end = int(datetime(year + 1, 1, 1, tzinfo=timezone.utc).timestamp()) * 1_000_000_000
    return Nanos(start), Nanos(end)


def _months_present_ratio(repo_root: Path, instrument: str, year: int) -> float:
    """Cheap (filesystem-only) data-completeness ratio: fraction of the
    year's 12 calendar months with a data/tick/ Parquet file on disk.

    WP-012: retargeted from Dukascopy raw-blob (hour-granular) presence to
    the HistData-backed store (month-granular) -- reusing the same "a hole
    is a wholly-missing month" model WP-010 already established for
    resample_range and run_checks, rather than inventing a new signal.
    This function predates WP-009/WP-010 and was out of scope for both
    (neither package touched probe/); it still checked
    jarvis.ingest.urls.raw_blob_path until this fix, which meant every
    year measured 0% presence against data/raw/ticks/ (204 leftover hours
    from an unrelated 2024-01 diagnostic, zero real 2006-2022 coverage)
    and year_admissibility failed every year unconditionally --
    see docs/STAGE_1A_RUNBOOK.md Section 0.

    Deliberately NOT the expensive tick-level jarvis.qa.run_checks pass --
    that is still required separately for the "zero QA ERRORs" half of
    admissibility, but file presence alone answers "months present"
    without re-parsing a single tick."""
    present = sum(
        1 for month in range(1, 13) if tick_path(repo_root, instrument, year, month).is_file()
    )
    return present / 12


def _year_admissible_days(year: int) -> int:
    """Count of weekday trading-day labels in the calendar year --
    reported as YearCounts.admissible_days, a denominator for the reader,
    independent of whether the year itself passed the admissibility
    test."""
    start_ns, end_ns = _year_bounds(year)
    day = trading_day(start_ns)
    count = 0
    while True:
        bounds_start, bounds_end = trading_day_bounds(day)
        if bounds_start >= end_ns:
            break
        if bounds_start >= start_ns and day.isoweekday() <= 5:
            count += 1
        day = date.fromordinal(day.toordinal() + 1)
    return count


def year_admissibility(repo_root: Path, instrument: str, year: int) -> bool:
    """A year is admissible iff >= 95% of its 12 calendar months have a
    data/tick/ Parquet file present AND jarvis.qa reports zero ERROR
    findings for it (WP-012: months check retargeted from Dukascopy
    raw-blob hours -- see _months_present_ratio). The (cheap) months
    check runs first and short-circuits the (expensive, full tick-level
    re-parse) QA check when it already fails.

    A partial year like 2006 (data/tick/ only holds 2006-09 through
    2006-12, D-036's warmup range) correctly fails this on months alone
    (4/12 = 33%) -- this is the mechanism D-036a relies on to exclude
    warmup-only years from the gate without a separate truncated read
    range; see docs/STAGE_1A_RUNBOOK.md Section 2."""
    ratio = _months_present_ratio(repo_root, instrument, year)
    if ratio < _MIN_ADMISSIBLE_MONTHS_RATIO:
        return False
    year_start_ns, year_end_ns = _year_bounds(year)
    qa_report = run_checks(repo_root, instrument, year_start_ns, year_end_ns)
    return qa_report.errors == 0


# ---------------------------------------------------------------------------
# Per-year event aggregation (D-039)
# ---------------------------------------------------------------------------


def _per_context_counts(events: list[ContextEvent]) -> dict[str, int]:
    """D-039: C-B/C-C count per (trading_day, direction); C-A/C-D count
    per trading_day and BROADCAST across whichever C-B/C-C directions
    occurred that same day when intersected -- a day with C-A plus both a
    long and a short C-B yields 2 events for C-A∩C-B, not 1."""
    by_key: dict[tuple[date, str, str], ContextEvent] = {}
    for e in events:
        by_key[(e.trading_day, e.context, e.direction)] = e

    counts = {"C-A": 0, "C-B": 0, "C-C": 0, "C-D": 0}
    for key in INTERSECTION_KEYS:
        counts[key] = 0

    days_with_ca = {k[0] for k in by_key if k[1] == "C-A"}
    days_with_cd = {k[0] for k in by_key if k[1] == "C-D"}

    for day, ctx, _direction in by_key:
        if ctx in counts:
            counts[ctx] += 1
        if ctx == "C-B":
            if day in days_with_ca:
                counts["C-A∩C-B"] += 1
            if day in days_with_cd:
                counts["C-D∩C-B"] += 1
        elif ctx == "C-C":
            if day in days_with_ca:
                counts["C-A∩C-C"] += 1
            if day in days_with_cd:
                counts["C-D∩C-C"] += 1

    return counts


def _year_of_day(d: date) -> int:
    return d.year


def build_year_counts(
    repo_root: Path,
    instrument: str,
    years: list[int],
    events: tuple[ContextEvent, ...],
    eligible_days: frozenset[date],
) -> tuple[YearCounts, ...]:
    events_by_year: dict[int, list[ContextEvent]] = defaultdict(list)
    for e in events:
        events_by_year[_year_of_day(e.trading_day)].append(e)

    eligible_by_year: dict[int, int] = defaultdict(int)
    for d in eligible_days:
        eligible_by_year[d.year] += 1

    result = []
    for year in years:
        admissible = year_admissibility(repo_root, instrument, year)
        result.append(
            YearCounts(
                year=year,
                admissible=admissible,
                admissible_days=_year_admissible_days(year),
                context_eligible_days=eligible_by_year.get(year, 0),
                per_context=_per_context_counts(events_by_year.get(year, [])),
            )
        )
    return tuple(result)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run_probe(
    repo_root: Path,
    instrument: str,
    start_ns: Nanos,
    end_ns: Nanos,
    *,
    session_set_id: str = "fx_core",
    session_set_version: int = 1,
    widen: str | None = None,
) -> tuple[GateResult, tuple[ContextEvent, ...]]:
    """Load bars and compute features ONCE for the whole requested range
    (never per-year -- computing features per year would reintroduce an
    ATR/rv_60m warmup artefact at every year boundary, exactly the D-036
    problem the whole-range approach avoids), detect every context event
    once, then aggregate into per-year counts and evaluate the gate.

    PDLA-03/D-021: refuses any range touching 2023-01-01 or later --
    Stage 0 must never read vault years, even for its own admissibility
    accounting."""
    if start_ns % NS_PER_HOUR != 0 or end_ns % NS_PER_HOUR != 0:
        raise UserError(f"start_ns ({start_ns}) and end_ns ({end_ns}) must both be hour-aligned")
    if start_ns >= end_ns:
        raise UserError(f"start_ns ({start_ns}) must be strictly before end_ns ({end_ns})")
    # See reject_vault_range's own docstring for the strict->-not->= reasoning.
    reject_vault_range(end_ns, caller="stage0 probe")

    params = widened_params(widen)
    session_set = load_session_set(session_set_id, session_set_version)

    bars = read_bars(repo_root, instrument, start_ns, end_ns)
    if bars.height == 0:
        raise UserError(f"no bars in [{start_ns}, {end_ns}) -- nothing to probe")

    features = compute(list(_REQUIRED_FEATURES), bars, session_set).frame

    events = detect_events(bars, features, session_set, params)
    eligible_days = context_eligible_days(bars, features, session_set)

    start_year = datetime.fromtimestamp(start_ns // 1_000_000_000, tz=timezone.utc).year
    end_year = datetime.fromtimestamp((end_ns - 1) // 1_000_000_000, tz=timezone.utc).year
    years = list(range(start_year, end_year + 1))

    year_counts = build_year_counts(repo_root, instrument, years, events, eligible_days)
    gate_result = evaluate_gate(year_counts, params, widen)

    return gate_result, events


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------


def stage0_report_path(repo_root: Path, generated: datetime) -> Path:
    ts_str = generated.strftime("%Y%m%dT%H%M%SZ")
    return repo_root / "reports" / "stage0" / f"STAGE0__{ts_str}.md"


def _render_markdown(
    gate_result: GateResult,
    instrument: str,
    start_ns: Nanos,
    end_ns: Nanos,
    session_set: SessionSet,
    code_sha: str,
    generated: datetime,
    prior_run_count: int,
) -> str:
    # Section order follows the WP's required list exactly: 1 watermark,
    # 2 decision+arithmetic, 3 D-024a qualification, 4 per-year table,
    # 5 median/P10, 6 C-C caveat, 7 parameters+dataset range+session set
    # version+feature set version+code SHA, 8 prior run count. Metadata
    # (range, session set, code SHA) deliberately does NOT sit right after
    # the watermark -- it belongs in section 7, not section 1.
    lines: list[str] = []

    # 1. Watermark.
    lines.append(f"# {WATERMARK}")
    lines.append("")
    lines.append(f"# Stage 0 Probe -- {instrument}")
    lines.append("")

    # 2. Decision and arithmetic, step by step.
    lines.append("## Decision")
    lines.append("")
    lines.append(f"**{gate_result.decision}**")
    lines.append("")
    lines.append("Arithmetic:")
    lines.append("```")
    lines.append(gate_result.arithmetic)
    lines.append("```")
    lines.append("")

    # 3. D-024a qualification, verbatim.
    lines.append("## Qualification")
    lines.append("")
    lines.append(D024A_QUALIFICATION)
    lines.append("")

    # 4. Per-year table.
    lines.append("## Per-year counts")
    lines.append("")
    lines.append(
        "\"Admissible days\" is a calendar denominator -- the count of weekday "
        "trading-day labels in that calendar year -- reported for every year "
        "shown here regardless of whether the year itself passed admissibility; "
        "it is not \"days that passed admissibility\" and must not be read as one."
    )
    lines.append("")
    header = (
        "| Year | Admissible | Admissible days | Context-eligible days | "
        + " | ".join(["C-A", "C-B", "C-C", "C-D", *INTERSECTION_KEYS])
        + " |"
    )
    lines.append(header)
    lines.append("|" + "---|" * (4 + 4 + len(INTERSECTION_KEYS)))
    for yc in gate_result.per_year:
        counts = [str(yc.per_context.get(k, 0)) for k in ("C-A", "C-B", "C-C", "C-D", *INTERSECTION_KEYS)]
        lines.append(
            f"| {yc.year} | {yc.admissible} | {yc.admissible_days} | "
            f"{yc.context_eligible_days} | " + " | ".join(counts) + " |"
        )
    lines.append("")

    # 5. Median and P10, with the percentile method named.
    lines.append("## Median and P10")
    lines.append("")
    lines.append(f"- Narrowest intersection: {gate_result.narrowest_intersection}")
    lines.append(f"- Percentile method: {gate_result.percentile_method}")
    lines.append(f"- Median annual (M): {gate_result.median_annual}")
    lines.append(f"- P10 annual: {gate_result.p10_annual}")
    lines.append(f"- Years used: {gate_result.years_used}")
    lines.append("")

    # 6. The C-C forward-window caveat.
    lines.append("## C-C forward-window caveat")
    lines.append("")
    lines.append(CC_FORWARD_WINDOW_CAVEAT)
    lines.append("")

    # 7. Every parameter used, plus dataset range, session set version,
    # feature set version, code SHA.
    lines.append("## Parameters and provenance")
    lines.append("")
    p = gate_result.params
    lines.append(f"- range_pct_max: {p.range_pct_max}")
    lines.append(f"- range_pct_min: {p.range_pct_min}")
    lines.append(f"- break_buffer_atr: {p.break_buffer_atr}")
    lines.append(f"- reentry_buffer_atr: {p.reentry_buffer_atr}")
    lines.append(f"- reentry_window_bars: {p.reentry_window_bars}")
    lines.append(f"- atr_period_bars: {p.atr_period_bars}")
    lines.append(f"- widened_from: {gate_result.widened_from}")
    lines.append(f"- Dataset range: {start_ns} - {end_ns} (UTC ns, half-open)")
    lines.append(f"- Session set: {session_set.definition.session_set_id} v{session_set.definition.version}")
    lines.append(f"- Feature set: v{FEATURE_SET_VERSION}")
    lines.append(f"- Code SHA: {code_sha}")
    lines.append(f"- Generated: {generated.isoformat().replace('+00:00', 'Z')}")
    lines.append("")

    # 8. Prior probe run count for this lineage.
    lines.append("## Run lineage")
    lines.append("")
    lines.append(f"- Prior probe runs for this lineage: {prior_run_count}")
    lines.append("")

    return "\n".join(lines)


def write_report(
    repo_root: Path,
    instrument: str,
    start_ns: Nanos,
    end_ns: Nanos,
    session_set: SessionSet,
    gate_result: GateResult,
    events: tuple[ContextEvent, ...],
    *,
    prior_run_count: int = 0,
) -> tuple[Path, Path]:
    """Write the markdown report and its Parquet events sidecar (one row
    per detected ContextEvent). Returns (md_path, parquet_path)."""
    generated = datetime.now(timezone.utc)
    code_sha = _code_sha(repo_root)

    md_path = stage0_report_path(repo_root, generated)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(
        _render_markdown(
            gate_result, instrument, start_ns, end_ns, session_set, code_sha, generated, prior_run_count
        ),
        encoding="utf-8",
    )

    parquet_path = md_path.with_suffix(".parquet")
    events_df = pl.DataFrame(
        {
            "context": [e.context for e in events],
            "trading_day": [e.trading_day.isoformat() for e in events],
            "direction": [e.direction for e in events],
            "ts_utc_ns": [int(e.ts_utc_ns) for e in events],
            "detail": [dict(e.detail) for e in events],
        }
    )
    events_df.write_parquet(parquet_path)

    return md_path, parquet_path
