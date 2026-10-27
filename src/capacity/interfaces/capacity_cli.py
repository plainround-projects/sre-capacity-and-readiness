"""`capacity`: the capacity plan from the measured safe load, and the readiness review."""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Sequence
from pathlib import Path

from capacity.application import capacity_report, launch_decision
from capacity.application.readiness import Reviewer
from capacity.domain.plan import plan_capacity
from capacity.infrastructure.logging_setup import configure_logging
from capacity.infrastructure.project_files import FileSystemProject, JsonEvidence, SloFilesCatalog, YamlChecklist

_MEASURED = re.compile(r"<!-- measured -->.*?<!-- /measured -->", re.DOTALL)
PEAK_RPS = 1000.0
GROWTH = 2.0
ZONES = 3
COMPOSE_FLEET = 8  # instances defined in docker-compose.yml (3/3/2 over three zones)


def cmd_plan(args: argparse.Namespace) -> int:
    """Size the fleet from the measured safe load, save it as evidence, and say if the lost-zone run confirmed it."""
    evidence = JsonEvidence(Path(args.evidence))
    knee = evidence.load("knee")
    safe = (
        float(args.safe_rps)
        if args.safe_rps
        else (float(str(knee["safe_rps"])) if knee and knee.get("safe_rps") else 0.0)
    )
    if safe <= 0:
        sys.exit("no safe load: run `make knee` first or pass --safe-rps")
    plan = plan_capacity(args.peak, args.growth, safe, ZONES)
    print(plan.formula())
    zone_run = evidence.load("fleet-zone-loss")
    verified = bool(zone_run and zone_run.get("within_slo") is True)
    evidence.save(
        "plan",
        {
            "current_peak_rps": plan.current_peak_rps,
            "growth_factor": plan.growth_factor,
            "safe_rps_per_instance": plan.safe_rps_per_instance,
            "future_peak_rps": plan.future_peak_rps,
            "instances_for_peak": plan.instances_for_peak,
            "total_instances": plan.total_instances,
            "distribution": list(plan.distribution),
            "surviving_after_zone_loss": plan.surviving_after_zone_loss,
            "formula": plan.formula(),
            "verified_by_lost_zone_run": verified,
        },
    )
    print(f"verified by running {plan.surviving_after_zone_loss} instances at {plan.future_peak_rps:g} rps: {verified}")
    if plan.total_instances > COMPOSE_FLEET:
        print(
            f"the plan needs {plan.total_instances} instances but docker-compose.yml defines {COMPOSE_FLEET}: add "
            "instances there (and to deploy/envoy.yaml and the fleet alert) before trusting the fleet runs",
            file=sys.stderr,
        )
        return 1
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    """Write docs/capacity-plan.md from the evidence."""
    root = Path(args.root)
    evidence = JsonEvidence(root / "docs" / "evidence")
    absent = capacity_report.missing(evidence)
    if absent:
        sys.exit(f"missing evidence: {', '.join(absent)}; run `make run`")
    (root / "docs" / "capacity-plan.md").write_text(capacity_report.render(evidence), encoding="utf-8")
    for name, facts in capacity_report.runbook_facts(evidence).items():
        path = root / "docs" / "runbooks" / name
        block = f"<!-- measured -->\n{facts}\n<!-- /measured -->"
        path.write_text(_MEASURED.sub(block.replace("\\", "\\\\"), path.read_text(encoding="utf-8")), encoding="utf-8")
    print("wrote docs/capacity-plan.md and the measured facts in the runbooks")
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    """Run the readiness review; with --write, write docs/launch-decision.md."""
    root = Path(args.root)
    reviewer = Reviewer(
        FileSystemProject(root),
        JsonEvidence(root / "docs" / "evidence"),
        SloFilesCatalog(root),
        YamlChecklist(root / "readiness" / "checklist.yaml"),
    )
    items, decision = reviewer.review()
    print(launch_decision.console(items, decision))
    if args.write:
        (root / "docs" / "launch-decision.md").write_text(launch_decision.markdown(items, decision), encoding="utf-8")
        print("wrote docs/launch-decision.md")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """The command-line interface."""
    parser = argparse.ArgumentParser(prog="capacity", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan", help="size the fleet from the measured safe load")
    plan.add_argument("--evidence", default="docs/evidence")
    plan.add_argument("--peak", type=float, default=PEAK_RPS, help="today's peak requests per second")
    plan.add_argument("--growth", type=float, default=GROWTH, help="growth factor over six months")
    plan.add_argument("--safe-rps", type=float, default=0.0, help="override the measured safe load per instance")
    plan.set_defaults(handler=cmd_plan)
    report = sub.add_parser("report", help="write docs/capacity-plan.md from the evidence")
    report.add_argument("--root", default=".")
    report.set_defaults(handler=cmd_report)
    review = sub.add_parser("review", help="run the readiness review")
    review.add_argument("--root", default=".")
    review.add_argument("--write", action="store_true", help="write docs/launch-decision.md")
    review.set_defaults(handler=cmd_review)
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    """Entry point of `capacity`."""
    configure_logging("WARNING")
    args = build_parser().parse_args(argv)
    sys.exit(args.handler(args))
