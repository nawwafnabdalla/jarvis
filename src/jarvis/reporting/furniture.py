"""Shared Stage 2 report furniture (Technical Bible Part 2 §G.1.3): the
mandatory header/watermark, sample-size warning rendering, and the fixed
closing paragraph every Stage 2 report carries without exception. Minimum
viable slice, built for R5 (WP-018) -- extend for R1/R2's own needs as
they arise, not speculatively now.

Layer note: `reporting` sits above `describe` (`.importlinter`'s layer
contract: `jarvis.cli : jarvis.reporting` is the top layer, `jarvis.
describe` the one below) -- this module renders a report's ALREADY-
COMPUTED result into markdown; it never computes statistics itself.
"""

import subprocess
from datetime import datetime, timezone
from pathlib import Path

WATERMARK = "DESCRIPTIVE -- EXPLORATORY -- NOT EVIDENCE"

CLOSING_PARAGRAPH = (
    "Patterns visible in descriptive statistics are the starting point for "
    "a hypothesis, not support for one. Any relationship worth acting on "
    "must be pre-registered and tested independently."
)

# G.1.3: "any cell with n < 30 is rendered with a warning marker; any cell
# with n < 10 is suppressed and shown as 'n<10 suppressed'."
_SAMPLE_WARN_THRESHOLD = 30
_SAMPLE_SUPPRESS_THRESHOLD = 10


def is_suppressed(n: int) -> bool:
    return n < _SAMPLE_SUPPRESS_THRESHOLD


def sample_size_note(n: int) -> str:
    """G.1.3's exact suppression/warning text for a cell with `n`
    observations. Empty string means no note is needed (n >= 30)."""
    if n < _SAMPLE_SUPPRESS_THRESHOLD:
        return "n<10 suppressed"
    if n < _SAMPLE_WARN_THRESHOLD:
        return f"n={n} (small sample)"
    return ""


def code_sha(repo_root: Path) -> str:
    """Mirrors `jarvis.probe.report._code_sha` -- duplicated rather than
    imported, since `probe/` is outside this package's file scope (WP-018)
    and this is a small enough, self-contained lookup that cross-module
    reaching for it is not worth the coupling."""
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


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def header_lines(
    *,
    title: str,
    data_period: str,
    code_sha_value: str,
    generated_at: str,
    dataset_version: str | None = None,
    session_set: str | None = None,
    feature_set: str | None = None,
) -> list[str]:
    """G.1.3's mandatory header block. `dataset_version`/`session_set`/
    `feature_set` are Optional because the infrastructure they'd name
    (Stage 1B's dataset manifests; a report that genuinely uses no
    session set or feature set, as R5 does not) does not exist or does
    not apply yet -- rendered as an explicit "not yet available" note
    rather than silently omitted, so a reader sees the gap instead of
    assuming it was forgotten."""
    lines = [
        f"# {WATERMARK}",
        "",
        f"# {title}",
        "",
        f"- Data period: {data_period}",
        "- Vault years (2023-present): excluded -- this report never reads them",
        f"- Dataset version: {dataset_version or 'not yet available (Stage 1B not built)'}",
        f"- Session set: {session_set or 'not used by this report'}",
        f"- Feature set: {feature_set or 'not used by this report'}",
        f"- Code SHA: {code_sha_value}",
        f"- Generated: {generated_at}",
    ]
    return lines


def closing_lines() -> list[str]:
    return ["", "## Interpretation", "", CLOSING_PARAGRAPH]
