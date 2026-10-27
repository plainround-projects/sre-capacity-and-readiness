"""The capacity plan: how many instances serve the projected peak, and survive losing a zone."""

from __future__ import annotations

import math
from dataclasses import dataclass

from capacity.domain.errors import InvalidPlanError


@dataclass(frozen=True, slots=True)
class CapacityPlan:
    """The inputs, the formula's intermediate results and the answer, so a reviewer can check every step."""

    current_peak_rps: float
    growth_factor: float
    safe_rps_per_instance: float
    zones: int
    future_peak_rps: float
    instances_for_peak: int
    total_instances: int
    distribution: tuple[int, ...]
    surviving_after_zone_loss: int

    @property
    def surviving_capacity_rps(self) -> float:
        """What the instances left after losing the largest zone can carry at their safe load."""
        return self.surviving_after_zone_loss * self.safe_rps_per_instance

    def formula(self) -> str:
        """The calculation, written out."""
        return (
            f"future_peak = {self.current_peak_rps:g} x {self.growth_factor:g} = {self.future_peak_rps:g} rps; "
            f"instances_for_peak = ceil({self.future_peak_rps:g} / {self.safe_rps_per_instance:g}) "
            f"= {self.instances_for_peak}; "
            f"total = smallest fleet over {self.zones} zones whose largest zone can be lost and still leave "
            f"{self.instances_for_peak} = {self.total_instances} ({'/'.join(map(str, self.distribution))}); "
            f"survivors after losing a zone of {max(self.distribution)} = {self.surviving_after_zone_loss}"
        )


def spread(total: int, zones: int) -> tuple[int, ...]:
    """Spread instances over zones as evenly as possible, larger zones first (8 over 3 zones is 3, 3, 2)."""
    base, extra = divmod(total, zones)
    return tuple(base + (1 if index < extra else 0) for index in range(zones))


def plan_capacity(
    current_peak_rps: float, growth_factor: float, safe_rps_per_instance: float, zones: int = 3
) -> CapacityPlan:
    """Size the fleet from the projected peak, then add instances until the surviving fleet still carries the full peak.

    Headroom is computed from the projected peak (today's peak times growth), never from today's, because the
    fleet has to survive a zone loss and the growth at the same time. The check uses the actual distribution:
    losing the largest zone must leave at least `instances_for_peak` instances.
    """
    if min(current_peak_rps, growth_factor, safe_rps_per_instance) <= 0 or zones < 2:
        raise InvalidPlanError("peak, growth and safe load must be positive and there must be at least two zones")
    future_peak = current_peak_rps * growth_factor
    needed = math.ceil(future_peak / safe_rps_per_instance)
    total = needed
    while True:
        distribution = spread(total, zones)
        survivors = total - max(distribution)
        if survivors >= needed:
            return CapacityPlan(
                current_peak_rps,
                growth_factor,
                safe_rps_per_instance,
                zones,
                future_peak,
                needed,
                total,
                distribution,
                survivors,
            )
        total += 1
