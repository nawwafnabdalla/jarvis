"""Stage 0 feasibility probe (Technical Bible Part 2 SS G.0).

EXPLORATORY -- FREQUENCY ONLY -- NOT EVIDENCE OF PREDICTIVE VALUE.

Measures whether GBP/USD, specialised to the four candidate contexts
below, can produce enough observations to ever be evaluated. It does not
look for an edge and must never be read as evidence of one. No function
in this package computes PnL, R, expectancy, win rate, or any other
profitability proxy -- and none ever will, by construction: the
`.importlinter` contract "probe must not compute profitability" forbids
importing jarvis.backtest, jarvis.statistics, jarvis.robustness, or
jarvis.execution from this package.
"""

from jarvis.probe.contexts import ContextEvent, ContextId, Direction, ProbeParams, detect_events
from jarvis.probe.gate import GateDecision, GateResult, YearCounts, evaluate_gate
from jarvis.probe.report import WATERMARK, run_probe, write_report

__all__ = [
    "WATERMARK",
    "ContextEvent",
    "ContextId",
    "Direction",
    "GateDecision",
    "GateResult",
    "ProbeParams",
    "YearCounts",
    "detect_events",
    "evaluate_gate",
    "run_probe",
    "write_report",
]
