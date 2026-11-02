"""Pure decisions the experiments make: which pool size to adopt, and whether a run proved what it set out to."""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

POOL_HEADROOM = 1.25
POOL_STEP = 5
ZONES = ("a", "b", "c")
COMPOSE_FLEET = 9  # instances docker-compose.yml defines: three in each zone


def naive_pool_size(peak_rps: float, hold_ms: float) -> int:
    """The textbook sizing: peak requests per second times the average time a request holds a connection."""
    return math.ceil(peak_rps * hold_ms / 1000.0)


def sweep_sizes(naive: int) -> list[int]:
    """Pool sizes to try, from the naive number upward in steps, until well past it."""
    return [naive, naive + 4, naive + 10, naive + 20]


def choose_pool_size(results: list[dict[str, Any]]) -> int | None:
    """The smallest size that held the SLO, plus 25 percent headroom rounded up to a step of five; None if none held.

    The smallest size that holds is the knee of the pool, not a place to operate: a pool exactly big enough has no
    room for a burst, so the adopted size keeps a margin above it.
    """
    holding = sorted(int(str(result["pool_size"])) for result in results if result["within_slo"] is True)
    if not holding:
        return None
    return math.ceil(holding[0] * POOL_HEADROOM / POOL_STEP) * POOL_STEP


def fleet_instances(distribution: Sequence[int]) -> list[str]:
    """The service names of a fleet spread as `distribution` says over zones a, b and c, such as (3, 3, 2)."""
    return [
        f"app-{zone}{number}"
        for zone, count in zip(ZONES, distribution, strict=False)
        for number in range(1, count + 1)
    ]


def spare_instances(distribution: Sequence[int]) -> list[str]:
    """The instances compose defines that the plan does not run."""
    wanted = set(fleet_instances(distribution))
    return [name for name in fleet_instances((COMPOSE_FLEET // len(ZONES),) * len(ZONES)) if name not in wanted]
