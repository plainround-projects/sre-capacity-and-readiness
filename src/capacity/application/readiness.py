"""The readiness review: run every check against the repository and the measured evidence, then decide.

A check that cannot find its evidence fails. The review is only worth running if it can fail something, so no check
passes on the strength of a file merely existing when the lesson asks for a measurement.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from capacity.application.ports import ChecklistConfig, EvidenceStore, ProjectFiles, SloCatalog
from capacity.domain.readiness import Decision, ReviewItem, Verdict, decide
from capacity.domain.telemetry import EXPORTED_METRICS, RECORDED_RULE_PREFIX

REQUIRED_RUNBOOKS = {
    "CPU saturation past the knee": "docs/runbooks/cpu-knee.md",
    "Shared pool exhaustion": "docs/runbooks/pool-exhaustion.md",
    "Loss of a zone": "docs/runbooks/zone-loss.md",
}
RUNBOOK_HEADINGS = ("Symptoms", "Mitigate", "Diagnose")
_PROMQL_WORDS = {
    "sum", "rate", "increase", "irate", "avg", "max", "min", "count", "vector", "time", "abs", "ceil", "floor",
    "histogram_quantile", "clamp_min", "clamp_max", "label_replace", "by", "without", "on", "ignoring",
}  # fmt: skip


@dataclass(frozen=True, slots=True)
class Check:
    """One checklist line: an id (for the owner and date in the checklist file), its area, what it asserts, and how."""

    item_id: str
    area: str
    asserts: str
    run: Callable[[], tuple[bool, str]]


class Reviewer:
    """Runs the checklist."""

    def __init__(self, files: ProjectFiles, evidence: EvidenceStore, slos: SloCatalog, config: ChecklistConfig) -> None:
        """Wire the ports."""
        self._files = files
        self._evidence = evidence
        self._slos = slos
        self._config = config

    def checks(self) -> list[Check]:
        """The checklist, in the order a reviewer would walk it."""
        return [
            Check(
                "slos",
                "SLOs and burn-rate alerts",
                "Page and ticket alerts are generated from the SLO file, in sync, and replayed with promtool",
                self._slo_check,
            ),
            Check("dashboards", "Dashboards", "Every panel queries a metric the system exports", self._dashboard_check),
            Check(
                "runbooks",
                "Runbooks for the top three failures",
                "CPU knee, pool exhaustion and zone loss each have symptoms, mitigation and diagnosis",
                self._runbook_check,
            ),
            Check(
                "rollout",
                "Safe rollout and rollback",
                "A rolling restart and a rollback ran under load with no failed request and p99 inside the SLO",
                self._rollout_check,
            ),
            Check(
                "dependencies",
                "Dependency failure behavior",
                "With the ledger frozen, requests fail fast with a 503 and the instance stays healthy",
                self._dependency_check,
            ),
            Check(
                "restore",
                "Backup and restore",
                "A backup was restored into a scratch database and matches the source row for row",
                self._restore_check,
            ),
            Check(
                "ownership",
                "On-call ownership",
                "An owner, a primary and secondary on-call role, an escalation path and the runbooks are written down",
                self._ownership_check,
            ),
            Check(
                "capacity",
                "Capacity",
                "The knee, the soak, the lost-zone run and the resized pool all held the SLO",
                self._capacity_check,
            ),
        ]

    def review(self) -> tuple[list[ReviewItem], Decision]:
        """Every checklist item with its verdict, then the decision."""
        items = [self._item(check) for check in self.checks()]
        for risk in self._config.accepted_risks():
            items.append(
                ReviewItem(
                    risk["area"],
                    risk["check"],
                    Verdict.ACCEPTED_RISK,
                    risk["evidence"],
                    risk["owner"],
                    revisit_when=risk["revisit_when"],
                )
            )
        return items, decide(items)

    def _item(self, check: Check) -> ReviewItem:
        passed, evidence = check.run()
        owner = self._config.owner(check.item_id)
        if passed:
            return ReviewItem(check.area, check.asserts, Verdict.PASS, evidence, owner)
        return ReviewItem(
            check.area,
            check.asserts,
            Verdict.FAIL,
            evidence,
            owner,
            due=self._config.due(check.item_id),
            blocking=self._config.blocking(check.item_id),
        )

    def _slo_check(self) -> tuple[bool, str]:
        slos, alerts = self._slos.slos(), self._slos.alerts()
        problems = []
        if not slos:
            problems.append("no SLO defined")
        if {alert.severity for alert in alerts} != {"page", "ticket"}:
            problems.append("the alert table needs both a page and a ticket")
        if not self._slos.rules_in_sync():
            problems.append("slo/generated is out of date with slo/slos.yaml")
        problems += [
            f"{test} is missing"
            for test in ("slo/tests/burn_rate.test.yml", "slo/tests/fleet.test.yml")
            if not self._files.exists(test)
        ]
        if problems:
            return False, "; ".join(problems)
        return (
            True,
            f"{len(slos)} SLOs with {len(alerts)} burn alerts each, generated rules in sync, promtool replays present",
        )

    def _dashboard_check(self) -> tuple[bool, str]:
        paths = self._files.glob("dashboards/*.json")
        if not paths:
            return False, "no dashboard under dashboards/"
        unknown: set[str] = set()
        queries = 0
        for path in paths:
            for expression in _expressions(json.loads(self._files.read(path))):
                queries += 1
                unknown |= {
                    name
                    for name in _metric_names(expression)
                    if name not in EXPORTED_METRICS and not name.startswith(RECORDED_RULE_PREFIX)
                }
        if unknown:
            return False, f"panels query metrics nothing exports: {sorted(unknown)}"
        return (
            True,
            f"{len(paths)} dashboard, {queries} queries, every metric is one the edge, an "
            "instance or the ledger exports",
        )

    def _runbook_check(self) -> tuple[bool, str]:
        problems = []
        for failure, path in REQUIRED_RUNBOOKS.items():
            if not self._files.exists(path):
                problems.append(f"{failure}: {path} is missing")
                continue
            text = self._files.read(path).lower()
            missing = [heading for heading in RUNBOOK_HEADINGS if heading.lower() not in text]
            if missing:
                problems.append(f"{failure}: no {', '.join(missing)} section")
        knee = self._evidence.load("knee")
        knee_runbook = "docs/runbooks/cpu-knee.md"
        if knee is not None and self._files.exists(knee_runbook):
            measured = (_knee_rps(knee), str(knee.get("safe_rps")))
            if not all(number in self._files.read(knee_runbook) for number in measured):
                problems.append(
                    f"cpu-knee.md does not quote the measured knee ({measured[0]}) and safe load ({measured[1]})"
                )
        if problems:
            return False, "; ".join(problems)
        return (
            True,
            "three runbooks with symptoms, mitigation and diagnosis; "
            "the CPU one quotes the measured knee and safe load",
        )

    def _rollout_check(self) -> tuple[bool, str]:
        data = self._evidence.load("rollout")
        if data is None:
            return False, "no rollout evidence: run `make rollout-test`"
        passed = data.get("within_slo") is True and data.get("rollback_ok") is True
        return passed, (
            f"{data.get('restarted')} instance restarts (roll to v2, roll back to v1) under {data.get('rps')} rps: "
            f"{data.get('errors')} failed requests, p99 {data.get('p99_ms')} ms"
        )

    def _dependency_check(self) -> tuple[bool, str]:
        data = self._evidence.load("dependency")
        if data is None:
            return False, "no dependency evidence: run `make dependency-test`"
        passed = data.get("fail_fast") is True and data.get("instance_healthy") is True
        return passed, (
            f"ledger frozen: slowest 503 after {data.get('slowest_503_ms')} ms (bound {data.get('bound_ms')} ms), "
            f"instance healthy: {data.get('instance_healthy')}"
        )

    def _restore_check(self) -> tuple[bool, str]:
        data = self._evidence.load("restore")
        if data is None:
            return False, "restore not yet tested: run `make restore-test`"
        within_target = float(str(data.get("restore_seconds"))) <= float(str(data.get("rto_target_seconds")))
        return data.get("matched") is True and within_target, (
            f"{data.get('rows')} rows restored in {data.get('restore_seconds')} s "
            f"(target {data.get('rto_target_seconds')} s), "
            f"row count and checksum match the source: {data.get('matched')}"
        )

    def _ownership_check(self) -> tuple[bool, str]:
        path = "docs/ownership.md"
        if not self._files.exists(path):
            return False, f"{path} is missing"
        text = self._files.read(path).lower()
        missing = [word for word in ("owner", "primary", "secondary", "escalation", "runbooks") if word not in text]
        if missing:
            return False, f"{path} does not say: {', '.join(missing)}"
        return True, "owner, primary and secondary on-call roles, escalation path and runbook index are written down"

    def _capacity_check(self) -> tuple[bool, str]:
        names = ("knee", "soak", "fleet-zone-loss", "fleet-pool-naive", "fleet-pool-resized")
        loaded = {name: self._evidence.load(name) for name in names}
        missing = [name for name, document in loaded.items() if document is None]
        if missing:
            return False, f"missing capacity evidence ({', '.join(missing)}): run `make run`"
        knee, soak, zone, naive, resized = (loaded[name] or {} for name in names)
        problems = []
        if knee.get("knee") is None:
            problems.append("the load test never found a knee")
        if soak.get("within_slo") is not True:
            problems.append("the soak at the safe load left the SLO")
        if zone.get("within_slo") is not True:
            problems.append("the fleet minus a zone left the SLO at projected peak")
        if resized.get("within_slo") is not True:
            problems.append("the resized pool still left the SLO")
        if naive.get("within_slo") is True:
            problems.append("the naive pool did not fail, so the next bottleneck was not found")
        if problems:
            return False, "; ".join(problems)
        return True, (
            f"knee {_knee_rps(knee)} rps, safe {knee.get('safe_rps')} rps held {soak.get('seconds')} s "
            f"(p99 {soak.get('p99_ms')} ms); "
            f"lost-zone fleet at {zone.get('target_rps')} rps p99 {zone.get('p99_ms')} ms; "
            f"naive pool of {naive.get('pool_size')} failed "
            f"(p99 {naive.get('p99_ms')} ms, error rate {naive.get('error_rate')}), "
            f"pool of {resized.get('pool_size')} held (p99 {resized.get('p99_ms')} ms)"
        )


def _knee_rps(knee: dict[str, Any]) -> str:
    detail = knee.get("knee")
    return "?" if not isinstance(detail, dict) else f"{detail.get('knee_rps'):g}"


def _expressions(node: Any) -> list[str]:
    """Every PromQL expression in a dashboard JSON document."""
    found: list[str] = []
    if isinstance(node, dict):
        if isinstance(node.get("expr"), str):
            found.append(node["expr"])
        for value in node.values():
            found.extend(_expressions(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(_expressions(value))
    return found


def _metric_names(expression: str) -> set[str]:
    """Identifiers in a PromQL expression, with selectors, ranges, strings and grouping clauses taken out."""
    bare = re.sub(r'"[^"]*"', "", re.sub(r"\{[^}]*\}", "", re.sub(r"\[[^\]]*\]", "", expression)))
    bare = re.sub(r"\b(?:by|without|on|ignoring)\s*\([^)]*\)", "", bare)
    names = set(re.findall(r"[a-zA-Z_:][a-zA-Z0-9_:]*", bare))
    return {name for name in names if name not in _PROMQL_WORDS}
