"""Shared fixtures: the project root, the real SLO spec, and the committed evidence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from capacity.infrastructure.slo_files import SloSpec, load_spec

ROOT = Path(__file__).resolve().parent.parent
EVIDENCE = ROOT / "docs" / "evidence"


@pytest.fixture(scope="session")
def root() -> Path:
    return ROOT


@pytest.fixture(scope="session")
def spec() -> SloSpec:
    return load_spec(ROOT / "slo" / "slos.yaml")


@pytest.fixture(scope="session")
def evidence() -> Any:
    def load(name: str) -> dict[str, Any]:
        document: dict[str, Any] = json.loads((EVIDENCE / f"{name}.json").read_text())
        return document

    return load
