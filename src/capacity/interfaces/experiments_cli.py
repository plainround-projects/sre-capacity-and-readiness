"""`capacity-experiments`: the experiments that need containers stopped, paused, recreated and loaded.

Each experiment prints what it measured and writes it to `docs/evidence/<name>.json`, which the readiness review and
the tests read. Everything here drives the running stack from the host.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from capacity.application.experiments import (
    choose_pool_size,
    fleet_instances,
    naive_pool_size,
    spare_instances,
    sweep_sizes,
)
from capacity.infrastructure.compose import Compose, versions_seen, wait_for_members
from capacity.infrastructure.logging_setup import configure_logging
from capacity.infrastructure.project_files import JsonEvidence

MAX_LOAD_PER_CPU = 0.6  # the one-minute load average per CPU above which the host is too busy to measure on
LEDGER_URL = "http://ledger:8082"
SLO_P99_MS = "50"
FLEET = "http://lb:8080"


def _say(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


class PlannedFleet:
    """The fleet `make plan` decided on, read from its evidence: which instances run and the peak they must carry."""

    def __init__(self, evidence: JsonEvidence) -> None:
        """Read `plan.json`; exit with a message when the plan has not been made."""
        plan = evidence.load("plan")
        if plan is None:
            sys.exit("no plan yet: run `make plan` (after `make knee`) first")
        distribution = [int(str(size)) for size in plan["distribution"]]  # type: ignore[attr-defined]
        self.instances = fleet_instances(distribution)
        self.zone_a = fleet_instances(distribution[:1])
        self.spares = spare_instances(distribution)
        self.peak_rps = float(str(plan["future_peak_rps"]))

    @property
    def size(self) -> int:
        """Instances in the fleet."""
        return len(self.instances)


def preflight() -> dict[str, Any]:
    """Refuse to measure on a busy machine: the numbers would describe the machine, not the service."""
    load = os.getloadavg()[0]
    limit = MAX_LOAD_PER_CPU * (os.cpu_count() or 1)
    if load > float(os.environ.get("MAX_LOAD", limit)):
        sys.exit(
            f"the machine is busy: one-minute load {load:.1f} is above {limit:.1f}. Measurements taken now would "
            "describe the machine, not the service. Stop other work and run again (or set MAX_LOAD to accept it)."
        )
    return {"experiment": "preflight", "load_1min": round(load, 2), "limit": round(limit, 2)}


def _fleet(compose: Compose, label: str, rps: float, seconds: float) -> dict[str, Any]:
    return compose.loadtest(
        "fleet",
        "--target",
        FLEET,
        "--rps",
        str(rps),
        "--seconds",
        str(seconds),
        "--label",
        label,
        "--slo-p99-ms",
        SLO_P99_MS,
    )


def set_fleet(compose: Compose, evidence: JsonEvidence) -> dict[str, Any]:
    """Run exactly the instances the plan says: start those it names, stop the spares, wait for the balancer."""
    fleet = PlannedFleet(evidence)
    _say(f"running {fleet.size} instances; spares: {', '.join(fleet.spares) or 'none'}")
    if fleet.spares:
        compose.stop(fleet.spares)
    compose.start(fleet.instances)
    wait_for_members(fleet.size)
    return {"experiment": "set-fleet", "instances": fleet.instances, "spares": fleet.spares}


def zone_loss(compose: Compose, evidence: JsonEvidence, seconds: float) -> dict[str, Any]:
    """Lose the largest zone, wait until the balancer has noticed, and run the projected peak on the survivors."""
    fleet = PlannedFleet(evidence)
    survivors = fleet.size - len(fleet.zone_a)
    _say(f"stopping zone a (the largest zone, {len(fleet.zone_a)} instances)")
    compose.stop(fleet.zone_a)
    try:
        wait_for_members(survivors)
        report = _fleet(compose, "zone-a-down", fleet.peak_rps, seconds)
    finally:
        compose.start(fleet.zone_a)
        wait_for_members(fleet.size)
    report["healthy_instances"] = survivors
    evidence.save("fleet-zone-loss", report)
    return report


def full_fleet(compose: Compose, evidence: JsonEvidence, seconds: float) -> dict[str, Any]:
    """The whole planned fleet at the projected peak with no shared dependency: the CPU-only baseline."""
    fleet = PlannedFleet(evidence)
    report = _fleet(compose, f"fleet-{fleet.size}", fleet.peak_rps, seconds)
    report["healthy_instances"] = fleet.size
    evidence.save("fleet-full", report)
    return report


def pool_sweep(compose: Compose, evidence: JsonEvidence, seconds: float, hold_ms: float) -> dict[str, Any]:
    """Reintroduce the shared ledger and step its connection pool up from the naive size until the SLO holds."""
    fleet = PlannedFleet(evidence)
    _say(f"linking all {fleet.size} instances to the ledger")
    compose.recreate(fleet.instances, env={"LEDGER_URL": LEDGER_URL})
    wait_for_members(fleet.size)
    naive = naive_pool_size(fleet.peak_rps, hold_ms)
    results: list[dict[str, Any]] = []
    for size in sweep_sizes(naive):
        _say(f"ledger pool of {size}")
        compose.recreate(["ledger"], env={"POOL_SIZE": str(size), "LEDGER_URL": LEDGER_URL})
        report = _fleet(compose, f"pool-{size}", fleet.peak_rps, seconds)
        report["pool_size"] = size
        results.append(report)
    chosen = choose_pool_size(results)
    if chosen is None:
        sys.exit("no pool size in the sweep held the SLO; extend the sweep")
    _say(f"adopting a pool of {chosen}: the smallest that held, plus 25 percent headroom")
    compose.recreate(["ledger"], env={"POOL_SIZE": str(chosen), "LEDGER_URL": LEDGER_URL})
    resized = _fleet(compose, f"pool-{chosen}", fleet.peak_rps, seconds)
    resized["pool_size"] = chosen
    evidence.save("fleet-pool-naive", results[0])
    evidence.save("fleet-pool-resized", resized)
    summary = {
        "naive_pool_size": naive,
        "hold_ms": hold_ms,
        "peak_rps": fleet.peak_rps,
        "chosen_pool_size": chosen,
        "sweep": results,
    }
    evidence.save("pool-sweep", summary)
    return summary


def dependency(compose: Compose, evidence: JsonEvidence, ledger_timeout_s: float) -> dict[str, Any]:
    """Freeze the ledger (it accepts connections and never answers) and check that requests fail within a bound."""
    compose.recreate(PlannedFleet(evidence).instances[:1], env={"LEDGER_URL": LEDGER_URL})
    compose.pause("ledger")
    try:
        report = compose.loadtest(
            "fleet",
            "--target",
            "http://app-a1:8080",
            "--rps",
            "20",
            "--seconds",
            "6",
            "--label",
            "ledger-frozen",
            "--window",
            "2",
        )
        healthy = _get("http://localhost:47283/healthz")
    finally:
        compose.unpause("ledger")
    bound_ms = ledger_timeout_s * 1000 + 250
    document = {
        "experiment": "dependency",
        "requests": report["requests"],
        "errors": report["errors"],
        "slowest_503_ms": report["max_ms"],
        "bound_ms": bound_ms,
        "fail_fast": report["errors"] == report["requests"] and report["max_ms"] <= bound_ms,
        "instance_healthy": healthy,
    }
    evidence.save("dependency", document)
    return document


def rollout(compose: Compose, evidence: JsonEvidence, rps: float) -> dict[str, Any]:
    """Roll every instance to v2 one at a time under load, then roll back to v1 the same way, and count failures."""
    fleet = PlannedFleet(evidence)
    compose.recreate(fleet.instances, env={"APP_VERSION": "v1", "LEDGER_URL": LEDGER_URL})
    wait_for_members(fleet.size)
    seconds = 40 + 2 * fleet.size * 12
    load = compose.start_loadtest(
        "fleet", "--target", FLEET, "--rps", str(rps), "--seconds", str(seconds), "--label", "rollout"
    )
    time.sleep(3)
    restarted = 0
    for version in ("v2", "v1"):
        _say(f"rolling to {version}, one instance at a time")
        for service in fleet.instances:
            compose.recreate([service], env={"APP_VERSION": version, "LEDGER_URL": LEDGER_URL})
            wait_for_members(fleet.size)
            restarted += 1
        seen = versions_seen("http://localhost:47280", 60)
        _say(f"versions answering after the roll to {version}: {sorted(seen)}")
        if seen != {version}:
            load.kill()
            sys.exit(f"after rolling to {version} the fleet still answered with {sorted(seen)}")
    stdout, stderr = load.communicate(timeout=seconds + 120)
    if load.returncode != 0:
        sys.exit(f"the load generator failed: {stderr.decode(errors='replace')[-500:]}")
    report: dict[str, Any] = json.loads(stdout)
    document = {
        "experiment": "rollout",
        "restarted": restarted,
        "rps": rps,
        "seconds": seconds,
        "requests": report["requests"],
        "errors": report["errors"],
        "p99_ms": report["p99_ms"],
        "within_slo": report["within_slo"],
        "rollback_ok": True,
    }
    evidence.save("rollout", document)
    return document


def restore(compose: Compose, evidence: JsonEvidence, rto_target_s: float) -> dict[str, Any]:
    """Back the ledger up, restore the copy into a scratch database and compare it with the source row by row."""
    query = (
        "SELECT count(*) || ' ' || md5(string_agg(id || ':' || account_id || ':' || amount_cents, ',' ORDER BY id)) "
        "FROM entries"
    )

    def fingerprint(database: str) -> tuple[int, str]:
        rows, checksum = compose.db("psql", "-U", "postgres", "-d", database, "-At", "-c", query).decode().split()
        return int(rows), checksum

    source = fingerprint("ledger")
    started = time.perf_counter()
    dump = compose.db("pg_dump", "-U", "postgres", "-Fc", "ledger")
    backup_s = time.perf_counter() - started
    compose.db("psql", "-U", "postgres", "-c", "DROP DATABASE IF EXISTS ledger_restore")
    compose.db("psql", "-U", "postgres", "-c", "CREATE DATABASE ledger_restore")
    started = time.perf_counter()
    compose.db("pg_restore", "-U", "postgres", "-d", "ledger_restore", "--no-owner", "--no-privileges", stdin=dump)
    restore_s = time.perf_counter() - started
    restored = fingerprint("ledger_restore")
    compose.db("psql", "-U", "postgres", "-c", "DROP DATABASE ledger_restore")
    document = {
        "experiment": "restore",
        "rows": restored[0],
        "source_rows": source[0],
        "checksum": restored[1],
        "matched": restored == source,
        "dump_bytes": len(dump),
        "backup_seconds": round(backup_s, 2),
        "restore_seconds": round(restore_s, 2),
        "rto_target_seconds": rto_target_s,
    }
    evidence.save("restore", document)
    return document


def _get(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            return bool(response.status == 200)
    except (urllib.error.URLError, TimeoutError):
        return False


def main(argv: Sequence[str] | None = None) -> None:
    """Entry point of `capacity-experiments`."""
    configure_logging("WARNING")
    parser = argparse.ArgumentParser(prog="capacity-experiments", description=__doc__)
    parser.add_argument("--project", default=".")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("preflight")
    sub.add_parser("set-fleet")
    for name in ("zone-loss", "full-fleet"):
        sub.add_parser(name).add_argument("--seconds", type=float, default=40.0)
    pool = sub.add_parser("pool-sweep")
    pool.add_argument("--seconds", type=float, default=30.0)
    pool.add_argument("--hold-ms", type=float, default=10.0)
    sub.add_parser("dependency").add_argument("--ledger-timeout-s", type=float, default=0.5)
    sub.add_parser("rollout").add_argument("--rps", type=float, default=1000.0)
    sub.add_parser("restore").add_argument("--rto-target-s", type=float, default=300.0)
    args = parser.parse_args(argv)
    project = Path(args.project).resolve()
    compose, evidence = Compose(project), JsonEvidence(project / "docs" / "evidence")
    if args.command == "preflight":
        result = preflight()
    elif args.command == "set-fleet":
        result = set_fleet(compose, evidence)
    elif args.command == "zone-loss":
        result = zone_loss(compose, evidence, args.seconds)
    elif args.command == "full-fleet":
        result = full_fleet(compose, evidence, args.seconds)
    elif args.command == "pool-sweep":
        result = pool_sweep(compose, evidence, args.seconds, args.hold_ms)
    elif args.command == "dependency":
        result = dependency(compose, evidence, args.ledger_timeout_s)
    elif args.command == "rollout":
        result = rollout(compose, evidence, args.rps)
    else:
        result = restore(compose, evidence, args.rto_target_s)
    print(json.dumps(result, indent=2))
