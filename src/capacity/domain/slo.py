"""SLIs, SLOs, error budgets and burn rates.

Every SLI is a fraction of good events over valid events. The error budget is what the target allows to
be bad, and the burn rate says how fast an interval spends it relative to a budget that would last the
whole window exactly.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from capacity.domain.errors import InvalidSloError

WINDOW_DAYS = 28
MINUTES_PER_DAY = 1440


class SliKind(StrEnum):
    """What an SLI counts as good."""

    AVAILABILITY = "availability"
    LATENCY = "latency"


@dataclass(frozen=True, slots=True)
class Slo:
    """A target for one SLI of one journey over a rolling window.

    `valid` and `good` are the human-readable definitions that go into the SLO document; the arithmetic is
    fixed by `kind`: availability counts a request good when its status is below 500 (a redirect, or a
    correct 404 for an unknown code); latency looks only at successful requests and counts one good when
    it was served under `threshold_ms`.
    """

    name: str
    journey: str
    kind: SliKind
    target: float
    valid: str
    good: str
    why: str
    threshold_ms: float | None = None
    window_days: int = WINDOW_DAYS

    def __post_init__(self) -> None:
        """Validate the definition so a broken SLO file fails when loaded, not when evaluated."""
        if not 0.0 < self.target < 1.0:
            raise InvalidSloError(f"{self.name}: the target must be strictly between 0 and 1")
        if self.kind is SliKind.LATENCY and (self.threshold_ms is None or self.threshold_ms <= 0):
            raise InvalidSloError(f"{self.name}: a latency SLO needs a positive threshold_ms")
        if self.kind is SliKind.AVAILABILITY and self.threshold_ms is not None:
            raise InvalidSloError(f"{self.name}: an availability SLO has no threshold_ms")
        if not (self.valid.strip() and self.good.strip() and self.why.strip()):
            raise InvalidSloError(f"{self.name}: valid, good and why must each say something")

    @property
    def allowed_bad_fraction(self) -> float:
        """The share of valid events the target lets be bad: 1 minus the target."""
        return round(1.0 - self.target, 12)

    @property
    def window_minutes(self) -> int:
        """The window length in minutes."""
        return self.window_days * MINUTES_PER_DAY

    @property
    def budget_minutes(self) -> float:
        """The budget as minutes of full outage over the window."""
        return self.window_minutes * self.allowed_bad_fraction

    def budget_requests(self, valid_events: int) -> float:
        """The budget as a count of bad requests, for the number of valid requests in the window."""
        return valid_events * self.allowed_bad_fraction

    def judge(self, status: int, latency_ms: float) -> tuple[bool, bool]:
        """Return (valid, good) for one request."""
        if self.kind is SliKind.AVAILABILITY:
            return True, status < 500
        succeeded = status < 500
        assert self.threshold_ms is not None
        return succeeded, succeeded and latency_ms < self.threshold_ms


@dataclass(frozen=True, slots=True)
class BurnAlert:
    """A multiwindow burn-rate alert: it fires only when both windows burn faster than `factor`.

    `min_bad_events` is the low-traffic guard: the long window must also hold at least that many bad events.
    A journey with a couple of requests a minute would otherwise page on two unlucky failures in an hour.
    """

    name: str
    severity: str
    factor: float
    long_minutes: int
    short_minutes: int
    min_bad_events: int = 10

    def __post_init__(self) -> None:
        """The short window must be shorter, and severity is page or ticket."""
        if self.severity not in {"page", "ticket"}:
            raise InvalidSloError(f"{self.name}: severity is page or ticket")
        if not 0 < self.short_minutes < self.long_minutes or self.factor <= 0:
            raise InvalidSloError(f"{self.name}: need 0 < short window < long window and a positive factor")
        if self.min_bad_events < 1:
            raise InvalidSloError(f"{self.name}: min_bad_events must be at least 1")

    def budget_consumed_fraction(self, window_days: int = WINDOW_DAYS) -> float:
        """The share of the whole window's budget the long window spends at exactly this burn rate."""
        return self.factor * self.long_minutes / (window_days * MINUTES_PER_DAY)


# The starting point from the track's SLO alerting lesson, for a 28-day window.
DEFAULT_ALERTS: tuple[BurnAlert, ...] = (
    BurnAlert("fast_burn", "page", 14.4, 60, 5),
    BurnAlert("slow_burn", "page", 6.0, 360, 30),
    BurnAlert("budget_leak", "ticket", 1.0, 4320, 360),
)


def burn_rate(bad: int, valid: int, allowed_bad_fraction: float) -> float:
    """Observed bad fraction over the allowed bad fraction; 1.0 spends the budget exactly over the window."""
    if valid == 0:
        return 0.0
    return (bad / valid) / allowed_bad_fraction


def budget_remaining_pct(bad: int, valid: int, allowed_bad_fraction: float) -> float:
    """Percent of the window's error budget still unspent given the bad and valid counts so far."""
    return 100.0 * (1.0 - burn_rate(bad, valid, allowed_bad_fraction))
