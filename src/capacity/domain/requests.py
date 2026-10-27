"""One request as the edge saw it: the unit every SLI is counted in."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RequestEvent:
    """A single line of the request log.

    `minute` counts minutes from the start of the measurement window (fractional), which is what
    the 28-day budget arithmetic indexes by.
    """

    journey: str
    status: int
    latency_ms: float
    minute: float
