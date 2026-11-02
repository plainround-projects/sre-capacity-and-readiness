"""`capacity-loadtest`: the knee test, the soak and the fleet run. Prints one JSON document on standard output."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlsplit

import uvloop

from capacity.application.loadtest import knee_test, timed_run
from capacity.infrastructure.logging_setup import configure_logging
from capacity.infrastructure.openloop import MetricsCpuProbe, RawOpenLoopRunner

DEFAULT_STEPS = "100,200,300,400,450,500,550,600,650,700,750,800,900,1000,1200,1400,1600"


def _runner(target: str) -> RawOpenLoopRunner:
    parts = urlsplit(target)
    return RawOpenLoopRunner(parts.hostname or "localhost", parts.port or 80)


def _probe(ops_url: str) -> MetricsCpuProbe:
    ops = urlsplit(ops_url)
    return MetricsCpuProbe(ops.hostname or "localhost", ops.port or 9101)


async def _knee(args: argparse.Namespace) -> tuple[dict[str, Any], str | None]:
    """The knee test, repeated up to `--attempts` times until it is trustworthy; the last result and its problem."""
    document: dict[str, Any] = {}
    problem: str | None = None
    for attempt in range(1, args.attempts + 1):
        runner = _runner(args.target)
        steps = [float(value) for value in args.steps.split(",")]
        report = await knee_test(runner, _probe(args.ops), steps, args.step_seconds, args.margin)
        await runner.close()
        document = {
            "experiment": "knee",
            "step_seconds": args.step_seconds,
            "test_attempts": attempt,
            **report.to_dict(),
        }
        problem = report.problem
        if problem is None:
            break
        print(f"knee attempt {attempt} discarded: {problem}", file=sys.stderr)
    return document, problem


async def _timed(args: argparse.Namespace) -> tuple[dict[str, Any], str | None]:
    runner = _runner(args.target)
    probe = _probe(args.ops) if args.ops else None
    report = await timed_run(
        runner, args.label, args.rps, args.seconds, args.slo_p99_ms, args.window, probe, args.attempts
    )
    await runner.close()
    return {"experiment": args.command, **report.to_dict()}, report.problem


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
    knee.add_argument(
        "--attempts", type=int, default=3, help="repeat the whole test this often until it is trustworthy"
    )
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
        run.add_argument("--ops", default="", help="the instance's ops port, to tell a saturated core from a busy host")
        run.add_argument(
            "--attempts", type=int, default=3, help="run again this often when a window spiked with no errors"
        )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    """Entry point of `capacity-loadtest`."""
    configure_logging("WARNING")
    args = build_parser().parse_args(argv)
    # uvloop keeps the generator's own scheduling delay small at 2,000 requests a second.
    result, problem = asyncio.run(
        _knee(args) if args.command == "knee" else _timed(args), loop_factory=uvloop.new_event_loop
    )
    if problem is not None:
        sys.exit(f"not a valid measurement, nothing written: {problem}. Run again when the machine is quiet.")
    print(json.dumps(result, indent=2))
