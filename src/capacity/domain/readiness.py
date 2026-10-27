"""The readiness review: every checklist item gets a verdict, an owner and evidence; the review ends in one decision."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from capacity.domain.errors import InvalidReviewError


class Verdict(StrEnum):
    """The only three answers. "Probably fine" is not one of them."""

    PASS = "PASS"
    FAIL = "FAIL"
    ACCEPTED_RISK = "ACCEPTED RISK"


class Decision(StrEnum):
    """The only three outcomes of a review."""

    LAUNCH = "LAUNCH"
    LAUNCH_WITH_CONDITIONS = "LAUNCH WITH CONDITIONS"
    DONT_LAUNCH = "DON'T LAUNCH YET"


@dataclass(frozen=True, slots=True)
class ReviewItem:
    """One line of the checklist.

    A FAIL names the date by which it is fixed. `blocking` marks a failure that must be fixed before launch rather
    than after. An ACCEPTED RISK names the condition that reopens it. Both need an owner, which is a role.
    """

    area: str
    check: str
    verdict: Verdict
    evidence: str
    owner: str
    due: date | None = None
    revisit_when: str = ""
    blocking: bool = False

    def __post_init__(self) -> None:
        """A verdict without its evidence, owner, date or revisit condition is a complaint, not a finding."""
        if not self.evidence.strip():
            raise InvalidReviewError(f"{self.area}: a verdict needs evidence")
        if self.verdict is not Verdict.PASS and not self.owner.strip():
            raise InvalidReviewError(f"{self.area}: a {self.verdict.value} needs an owner")
        if self.verdict is Verdict.FAIL and self.due is None:
            raise InvalidReviewError(f"{self.area}: a FAIL needs a date")
        if self.verdict is Verdict.ACCEPTED_RISK and not self.revisit_when.strip():
            raise InvalidReviewError(f"{self.area}: an ACCEPTED RISK needs the condition that reopens it")


def decide(items: list[ReviewItem]) -> Decision:
    """DON'T LAUNCH if a failure blocks, LAUNCH WITH CONDITIONS if failures are due by a date, else LAUNCH."""
    failures = [item for item in items if item.verdict is Verdict.FAIL]
    if any(item.blocking for item in failures):
        return Decision.DONT_LAUNCH
    return Decision.LAUNCH_WITH_CONDITIONS if failures else Decision.LAUNCH
