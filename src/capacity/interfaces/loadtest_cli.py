"""`capacity-loadtest`: the knee test, the soak and the fleet run. Prints one JSON document on standard output."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlsplit

from capacity.application.loadtest import GENERATOR_LAG_LIMIT_MS, knee_test, timed_run
from capacity.infrastructure.logging_setup import configure_logging
from capacity.infrastructure.openloop import MetricsCpuProbe, RawOpenLoopRunner

DEFAULT_STEPS = "100,200,300,400,450,500,550,600,650,700,750,800,900,1000,1200,1400,1600"


def _runner(target: str) -> RawOpenLoopRunner:
    parts = urlsplit(target)
    return RawOpenLoopRunner(parts.hostname or "localhost", parts.port or 80)


async def _knee(args: argparse.Namespace) -> dict[str, Any]:
    runner = _runner(args.target)
    ops = urlsplit(args.ops)
    probe = MetricsCpuProbe(ops.hostname or "localhost", ops.port or 9101)
    steps = [float(value) for value in args.steps.split(",")]
    report = await knee_test(runner, probe, steps, args.step_seconds, args.margin)
    await runner.close()
    return {"experiment": "knee", "step_seconds": args.step_seconds, **report.to_dict()}


async def _timed(args: argparse.Namespace) -> dict[str, Any]:
    runner = _runner(args.target)
    report = await timed_run(runner, args.label, args.rps, args.seconds, args.slo_p99_ms, args.window)
    await runner.close()
    return {"experiment": args.command, **report.to_dict()}


def build_parser() -> argparse.ArgumentParser:
    """The command-line interface."""
    parser = argparse.ArgumentParser(prog="capacity-loadtest", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    knee = sub.add_parser("knee", help="step the load up against one instance until p99 climbs sharply")
    knee.add_argument("--target", required=True, help="the instance, for example http://app-a1:8080")
    knee.add_argument("--ops", required=True, help="the instance's ops port, for CPU, for example http://app-a1:9101")
    knee.add_argument("--steps", default=DEFAULT_STEPS, help="comma-separated requests per second")
    knee.add_argument("--step-seconds", type=float, default=10.0, help="the lesson uses 120 for the real thing")
    knee.add_argument("--margin", type=float, default=0.25, help="how far below the knee to set the safe load")
    for name, helptext in (
        ("soak", "hold one instance at the safe load"),
        ("fleet", "load the fleet through the load balancer"),
    ):
        run = sub.add_parser(name, help=helptext)
        run.add_argument("--target", required=True)
        run.add_argument("--rps", type=float, required=True)
        run.add_argument("--seconds", type=float, required=True)
        run.add_argument("--label", default=name)
        run.add_argument("--slo-p99-ms", type=float, default=50.0)
        run.add_argument("--window", type=float, default=10.0, help="seconds per p99 window")
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    """Entry point of `capacity-loadtest`."""
    configure_logging("WARNING")
    args = build_parser().parse_args(argv)
    result = asyncio.run(_knee(args) if args.command == "knee" else _timed(args))
    if result["generator_lag_p99_ms"] > GENERATOR_LAG_LIMIT_MS:
        print(
            f"warning: the load generator itself was late by {result['generator_lag_p99_ms']} ms at p99; "
            "the numbers include its own queueing",
            file=sys.stderr,
        )
    print(json.dumps(result, indent=2))
