"""Ports: what the load-test use cases and the readiness review ask the outside world for."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Protocol

from capacity.domain.latency import Sample
from capacity.domain.slo import BurnAlert, Slo


class OpenLoopRunner(Protocol):
    """Sends requests on a fixed schedule, whether or not earlier ones finished."""

    async def run(self, rps: float, duration_s: float) -> tuple[list[Sample], float]:
        """Return every request's sample and the generator's own p99 lag in milliseconds (how late it sent)."""
        ...


class CpuProbe(Protocol):
    """Reads how much CPU time the process under test has used."""

    async def cpu_seconds(self) -> float:
        """Cumulative CPU seconds of the instance."""
        ...


class Clock(Protocol):
    """Time, injected so tests are deterministic."""

    def monotonic(self) -> float:
        """Seconds on a monotonic clock."""
        ...

    def today(self) -> date:
        """Today's date."""
        ...


class ProjectFiles(Protocol):
    """Read-only access to the repository, for checks that look at files."""

    def exists(self, relative: str) -> bool:
        """True when the file or folder exists."""
        ...

    def read(self, relative: str) -> str:
        """The file's text."""
        ...

    def glob(self, pattern: str) -> list[str]:
        """Relative paths matching a glob."""
        ...

    @property
    def root(self) -> Path:
        """The repository root."""
        ...


class EvidenceStore(Protocol):
    """Measured results written by the make targets, one JSON document per experiment."""

    def load(self, name: str) -> dict[str, object] | None:
        """The evidence called `name`, or None when the experiment has not been run."""
        ...


class SloCatalog(Protocol):
    """The SLO definitions and their generated artifacts, as the readiness review sees them."""

    def slos(self) -> list[Slo]:
        """Every SLO defined in `slo/slos.yaml`."""
        ...

    def alerts(self) -> list[BurnAlert]:
        """The burn-rate alert table."""
        ...

    def rules_in_sync(self) -> bool:
        """True when the committed Prometheus rule files are exactly what the definitions generate."""
        ...


class ChecklistConfig(Protocol):
    """Who owns each checklist item and by when a failure of it is fixed."""

    def owner(self, item_id: str) -> str:
        """The role that owns the item."""
        ...

    def due(self, item_id: str) -> date:
        """The date by which a failure of the item is fixed."""
        ...

    def blocking(self, item_id: str) -> bool:
        """True when a failure of the item must be fixed before launch."""
        ...

    def accepted_risks(self) -> list[dict[str, str]]:
        """Risks the team has accepted: area, check, evidence, owner, revisit_when."""
        ...
