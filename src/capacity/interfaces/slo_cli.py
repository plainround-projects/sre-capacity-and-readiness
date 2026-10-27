"""`capacity-slo`: generate the Prometheus SLO rules from slo/slos.yaml, or check the committed files."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from capacity.infrastructure.slo_files import load_spec, render_rule_files


def main(argv: Sequence[str] | None = None) -> None:
    """Entry point of `capacity-slo`."""
    parser = argparse.ArgumentParser(prog="capacity-slo", description=__doc__)
    parser.add_argument("--slos", default="slo/slos.yaml")
    parser.add_argument("command", choices=["rules"])
    parser.add_argument("--check", action="store_true", help="fail instead of writing when a file is out of date")
    args = parser.parse_args(argv)
    drift = 0
    for path, text in render_rule_files(load_spec(Path(args.slos))).items():
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                print(f"{path} is out of date; run `make rules`", file=sys.stderr)
                drift = 1
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            print(f"wrote {path}")
    sys.exit(drift)
