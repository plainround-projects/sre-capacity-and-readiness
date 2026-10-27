"""Adapters over the repository on disk: its files, its measured evidence and its checklist configuration."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from capacity.application.ports import SloCatalog
from capacity.domain.slo import BurnAlert, Slo
from capacity.infrastructure.slo_files import load_spec, render_rule_files


class FileSystemProject:
    """Read-only access to the repository root."""

    def __init__(self, root: Path) -> None:
        """`root` is the repository root."""
        self._root = root

    @property
    def root(self) -> Path:
        """The repository root."""
        return self._root

    def exists(self, relative: str) -> bool:
        """True when the file or folder exists."""
        return (self._root / relative).exists()

    def read(self, relative: str) -> str:
        """The file's text."""
        return (self._root / relative).read_text(encoding="utf-8")

    def glob(self, pattern: str) -> list[str]:
        """Relative paths matching a glob, sorted."""
        return sorted(str(path.relative_to(self._root)) for path in self._root.glob(pattern))


class JsonEvidence:
    """Evidence documents under `docs/evidence/<name>.json`, written by the make targets."""

    def __init__(self, directory: Path) -> None:
        """`directory` holds the JSON files."""
        self._directory = directory

    def load(self, name: str) -> dict[str, object] | None:
        """The evidence called `name`, or None when the experiment has not been run."""
        path = self._directory / f"{name}.json"
        if not path.exists():
            return None
        document: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
        return document

    def save(self, name: str, document: dict[str, Any]) -> Path:
        """Write the evidence, creating the folder."""
        self._directory.mkdir(parents=True, exist_ok=True)
        path = self._directory / f"{name}.json"
        path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        return path


class YamlChecklist:
    """`readiness/checklist.yaml`: owners, fix dates and blocking flags per item, and the accepted risks."""

    def __init__(self, path: Path) -> None:
        """`path` is the YAML file."""
        self._document: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))

    def owner(self, item_id: str) -> str:
        """The role that owns the item."""
        return str(self._document["items"][item_id]["owner"])

    def due(self, item_id: str) -> date:
        """The date by which a failure of the item is fixed."""
        value = self._document["items"][item_id]["fix_by"]
        return value if isinstance(value, date) else date.fromisoformat(str(value))

    def blocking(self, item_id: str) -> bool:
        """True when a failure of the item must be fixed before launch."""
        return bool(self._document["items"][item_id].get("blocking", False))

    def accepted_risks(self) -> list[dict[str, str]]:
        """The accepted risks, each with area, check, evidence, owner and revisit_when."""
        return [{key: str(value) for key, value in risk.items()} for risk in self._document.get("accepted_risks", [])]


class SloFilesCatalog(SloCatalog):
    """The SLO definitions and the generated rules, read from `slo/`."""

    def __init__(self, root: Path) -> None:
        """`root` is the repository root."""
        self._root = root
        self._spec = load_spec(root / "slo" / "slos.yaml")

    def slos(self) -> list[Slo]:
        """Every SLO."""
        return list(self._spec.slos)

    def alerts(self) -> list[BurnAlert]:
        """The burn-rate alert table."""
        return list(self._spec.alerts)

    def rules_in_sync(self) -> bool:
        """True when the committed rule files equal what the generator produces from the definitions."""
        return all(
            (self._root / path).exists() and (self._root / path).read_text(encoding="utf-8") == text
            for path, text in render_rule_files(self._spec).items()
        )
