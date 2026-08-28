"""Shared value types."""

from dataclasses import dataclass
from typing import NewType

Nanos = NewType("Nanos", int)  # nanoseconds since Unix epoch, UTC


@dataclass(frozen=True, slots=True)
class Instrument:
    symbol: str  # "GBPUSD"
    point_scale: float  # 1e-5
    digits: int  # 5
