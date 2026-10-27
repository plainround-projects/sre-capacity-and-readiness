"""`capacity-experiments`: the experiments that need containers stopped, paused, recreated and loaded.

Each experiment prints what it measured and writes it to `docs/evidence/<name>.json`, which the readiness review and
the tests read. Everything here drives the running stack from the host.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from capacity.application.experiments import choose_pool_size, naive_pool_size, sweep_sizes
from capacity.infrastructure.compose import Compose, versions_seen, wait_for_members
from capacity.infrastructure.logging_setup import configure_logging
from capacity.infrastructure.project_files import JsonEvidence

ZONE_A = ["app-a1", "app-a2", "app-a3"]
INSTANCES = ["app-a1", "app-a2", "app-a3", "app-b1", "app-b2", "app-b3", "app-c1", "app-c2"]
LEDGER_URL = "http://ledger:8082"
SLO_P99_MS = "50"
FLEET = "http://lb:8080"
PEAK_RPS = 2000


def _say(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


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


def zone_loss(compose: Compose, evidence: JsonEvidence, seconds: float) -> dict[str, Any]:
    """Lose the largest zone, wait until the balancer has noticed, and run the projected peak on the five survivors."""
    _say("stopping zone a (the largest zone, three instances)")
    compose.stop(ZONE_A)
    try:
        wait_for_members(5)
        report = _fleet(compose, "zone-a-down", PEAK_RPS, seconds)
    finally:
        compose.start(ZONE_A)
        wait_for_members(8)
    report["healthy_instances"] = 5
    evidence.save("fleet-zone-loss", report)
    return report


def full_fleet(compose: Compose, evidence: JsonEvidence, seconds: float) -> dict[str, Any]:
    """The full fleet of eight at the projected peak with no shared dependency: the CPU-only baseline."""
    report = _fleet(compose, "fleet-8", PEAK_RPS, seconds)
    report["healthy_instances"] = 8
    evidence.save("fleet-full", report)
    return report


def pool_sweep(compose: Compose, evidence: JsonEvidence, seconds: float, hold_ms: float) -> dict[str, Any]:
    """Reintroduce the shared ledger and step its connection pool up from the naive size until the SLO holds."""
    _say(f"linking all {len(INSTANCES)} instances to the ledger")
    compose.recreate(INSTANCES, env={"LEDGER_URL": LEDGER_URL})
    wait_for_members(8)
    naive = naive_pool_size(PEAK_RPS, hold_ms)
    results: list[dict[str, Any]] = []
    for size in sweep_sizes(naive):
        _say(f"ledger pool of {size}")
        compose.recreate(["ledger"], env={"POOL_SIZE": str(size), "LEDGER_URL": LEDGER_URL})
        report = _fleet(compose, f"pool-{size}", PEAK_RPS, seconds)
        report["pool_size"] = size
        results.append(report)
    chosen = choose_pool_size(results)
    if chosen is None:
        sys.exit("no pool size in the sweep held the SLO; extend the sweep")
    _say(f"adopting a pool of {chosen}: the smallest that held, plus 25 percent headroom")
    compose.recreate(["ledger"], env={"POOL_SIZE": str(chosen), "LEDGER_URL": LEDGER_URL})
    resized = _fleet(compose, f"pool-{chosen}", PEAK_RPS, seconds)
    resized["pool_size"] = chosen
    evidence.save("fleet-pool-naive", results[0])
    evidence.save("fleet-pool-resized", resized)
    summary = {
        "naive_pool_size": naive,
        "hold_ms": hold_ms,
        "peak_rps": PEAK_RPS,
        "chosen_pool_size": chosen,
        "sweep": results,
    }
    evidence.save("pool-sweep", summary)
    return summary


def dependency(compose: Compose, evidence: JsonEvidence, ledger_timeout_s: float) -> dict[str, Any]:
    """Freeze the ledger (it accepts connections and never answers) and check that requests fail within a bound."""
    compose.recreate(INSTANCES[:1], env={"LEDGER_URL": LEDGER_URL})
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
    compose.recreate(INSTANCES, env={"APP_VERSION": "v1", "LEDGER_URL": LEDGER_URL})
    wait_for_members(8)
    seconds = 40 + 2 * len(INSTANCES) * 12
    load = compose.start_loadtest(
        "fleet", "--target", FLEET, "--rps", str(rps), "--seconds", str(seconds), "--label", "rollout"
    )
    time.sleep(3)
    restarted = 0
    for version in ("v2", "v1"):
        _say(f"rolling to {version}, one instance at a time")
        for service in INSTANCES:
            compose.recreate([service], env={"APP_VERSION": version, "LEDGER_URL": LEDGER_URL})
            wait_for_members(8)
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
    if args.command == "zone-loss":
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
