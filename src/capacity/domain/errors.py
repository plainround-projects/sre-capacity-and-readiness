"""Named errors of the domain."""

from __future__ import annotations


class DomainError(Exception):
    """Base class for every error this project raises on purpose."""


class InvalidSloError(DomainError):
    """An SLO definition is incomplete or inconsistent."""


class NoKneeError(DomainError):
    """The load test never reached a step where latency rose sharply, so there is no knee to plan from."""


class InvalidPlanError(DomainError):
    """A capacity plan input is out of range or the plan cannot meet its own requirement."""


class InvalidReviewError(DomainError):
    """A readiness review item is missing its owner, its evidence or its date."""
