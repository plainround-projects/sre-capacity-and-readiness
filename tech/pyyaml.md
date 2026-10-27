# PyYAML 6.0.2

## What it is

PyYAML is a Python library that reads YAML text into Python data (`yaml.safe_load`) and writes Python data back to YAML text (`yaml.safe_dump` / `yaml.dump`). See the YAML note for the format itself.

## Why this repository uses it

Two YAML files are *read* by the code: the SLO definition (`slo/slos.yaml`) and the readiness checklist configuration (`readiness/checklist.yaml`: owners, fix dates, blocking flags, accepted risks). The code also *writes* YAML: the generated Prometheus rule files. PyYAML does both.

## Where it appears

| File | Use |
| --- | --- |
| `infrastructure/slo_files.py` | `load_spec` reads `slo/slos.yaml`; `dump_rules` writes the Prometheus rule files. |
| `infrastructure/project_files.py` | `YamlChecklist` reads `readiness/checklist.yaml`. |
| `tests/unit/test_rules_and_files.py` | Check loading errors and that generated files are in sync. |
| `pyproject.toml`, lock files | Pin `PyYAML==6.0.2` and the stub package `types-PyYAML` for mypy. |

## How it works underneath

`yaml.safe_load(stream)` parses the text and builds only basic Python types: `dict`, `list`, `str`, `int`, `float`, `bool`, `None` (and dates). It refuses YAML tags that could make arbitrary Python objects. That is why `safe_load` is the right call for any file that could be edited by someone else. `yaml.dump` converts data to text. Keys are sorted alphabetically by default; the code turns that off to keep a meaningful order.

## Walkthrough of this repository's use

### Loading with checks

```python
def _load(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        document = yaml.safe_load(handle)
    if not isinstance(document, dict):
        raise InvalidSloError(f"{path} must contain a mapping")
    return document


def load_spec(path: Path) -> SloSpec:
    """Parse `slos.yaml`; any missing or inconsistent field raises InvalidSloError."""
    document = _load(path)
    try:
        window_days = int(document["window_days"])
        journeys = tuple(
            JourneyDoc(name, int(body["rank"]), str(body["working"]).strip())
            for name, body in document["journeys"].items()
        )
        slos = tuple(
            Slo(
                name=entry["name"],
                journey=entry["journey"],
                kind=SliKind(entry["kind"]),
                target=float(entry["target"]),
                valid=str(entry["valid"]).strip(),
                good=str(entry["good"]).strip(),
                why=str(entry["why"]).strip(),
                threshold_ms=float(entry["threshold_ms"]) if "threshold_ms" in entry else None,
                window_days=window_days,
            )
            for entry in document["slos"]
        )
        default_guard = int(document.get("min_bad_events", 10))
        alerts = tuple(
            BurnAlert(
                e["name"],
                e["severity"],
                float(e["factor"]),
                int(e["long_minutes"]),
                int(e["short_minutes"]),
                int(e.get("min_bad_events", default_guard)),
            )
            for e in document["alerts"]
        )
    except (KeyError, TypeError, ValueError) as error:
        raise InvalidSloError(f"{path}: {error!r}") from error
    known = {journey.name for journey in journeys}
    unknown = {slo.journey for slo in slos} - known
    if unknown:
        raise InvalidSloError(f"{path}: SLOs reference undefined journeys {sorted(unknown)}")
    return SloSpec(window_days, journeys, slos, alerts)
```

- `_load` opens the file as UTF-8 text, calls `yaml.safe_load`, and insists the result is a `dict` (a mapping). Anything else raises the project's own `InvalidSloError`.
- `load_spec` pulls each field with `document["key"]` and converts it with `int(...)`, `float(...)`, `SliKind(...)`. A missing key is `KeyError`; a wrong type `ValueError`/`TypeError`. All three are caught and re-raised as `InvalidSloError` with the file name, so the message tells the reader which file is wrong.
- `Slo(...)` and `BurnAlert(...)` validate their own fields when constructed (see the Python note), so a bad target such as `1.5` fails here.
- The last lines check that every SLO refers to a journey that exists.

### The readiness checklist

```python
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

```

`YamlChecklist` loads the file once with `yaml.safe_load` and answers questions: `owner(item_id)` (a role such as `on-call lead`), `due(item_id)` (the date a failure of that item is fixed by), `blocking(item_id)` (whether a failure must be fixed before launch) and `accepted_risks()` (a list of the risks the team knowingly launches with, each with area, check, evidence, owner and the condition that reopens it). `due` handles a quirk: PyYAML turns an unquoted `2026-11-03` into a Python `date` already, so the code accepts a `date` or parses a string with `date.fromisoformat`.

```yaml
# Who owns each checklist item and by when a failure of it is fixed. A failing item without an owner and a date is
# refused by the review. `blocking: true` means a failure must be fixed before launch, not after.
items:
  slos:
    owner: service owner
    fix_by: 2026-11-03
  dashboards:
    owner: service owner
    fix_by: 2026-11-03
  runbooks:
    owner: on-call lead
    fix_by: 2026-11-03
    blocking: true
  rollout:
    owner: release owner
    fix_by: 2026-11-03
    blocking: true
  dependencies:
    owner: service owner
    fix_by: 2026-11-03
  restore:
    owner: database on-call
    fix_by: 2026-11-03
  ownership:
    owner: engineering manager
    fix_by: 2026-11-03
    blocking: true
  capacity:
    owner: capacity owner
    fix_by: 2026-11-03
    blocking: true

# A risk the team knowingly launches with. Each has an owner and the condition that reopens the question.
accepted_risks:
  - area: Autoscaling
    check: The fleet is a fixed size, planned for the six-month peak
    evidence: Load in the lesson's facts is steady and grows by a known factor, and a fixed fleet of eight carries twice today's peak with a zone lost; autoscaling is the lesson's stretch goal and is not built
    owner: capacity owner
    revisit_when: Traffic passes 1.5 times today's peak, or a launch event is expected to double traffic within minutes
  - area: Failure domains of the teaching build
    check: The three zones are labels on containers of one Docker host
    evidence: The zone-loss test removes three instances at once, which models the capacity effect of a zone failure but not a shared network, power or control-plane failure
    owner: service owner
    revisit_when: The service is deployed to real zones, at which point the same test is repeated by shutting down a real zone's nodes
```

The file maps each checklist item to its owner and `fix_by` date (`runbooks`, `rollout`, `ownership` and `capacity` are `blocking: true`), then lists `accepted_risks`. A failing item without an owner and a date is refused by the review.

### Dumping the generated rules


```python
def _literal_for_multiline(dumper: yaml.SafeDumper, value: str) -> yaml.ScalarNode:
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


class _RuleDumper(yaml.SafeDumper):
    """Dump multi-line expressions as literal blocks so the generated file reads like hand-written rules."""


_RuleDumper.add_representer(str, _literal_for_multiline)


def dump_rules(document: dict[str, Any], header: str) -> str:
    """Serialize a rule document with a generated-file banner."""
    body = yaml.dump(document, Dumper=_RuleDumper, sort_keys=False, width=10_000, allow_unicode=True)
    return "".join(f"# {line}\n" for line in header.splitlines()) + body
```

`_RuleDumper` is a small subclass of `yaml.SafeDumper`. `add_representer(str, _literal_for_multiline)` tells it how to write text values: if the text contains a newline it is written as a **literal block** (`|-`), which makes multi-line PromQL expressions readable; otherwise the default style. `dump_rules` calls `yaml.dump` with this dumper and three options: `sort_keys=False` keeps the order the code built (so `record`/`expr`/`labels` stay in a natural order instead of alphabetical), `width=10_000` stops PyYAML from folding long expressions across lines, and `allow_unicode=True` writes characters as themselves. A banner like "Generated by ... Do not edit" is added as `#` comment lines at the top.

### Where the YAML goes

The result is `slo/generated/rules.yml`, which Prometheus loads; `make rules-check` regenerates in memory and compares.

## Try it yourself

1. `.venv/bin/python -c "from pathlib import Path; from capacity.infrastructure.slo_files import load_spec; s=load_spec(Path('slo/slos.yaml')); print([x.name for x in s.slos])"` prints `['api_availability', 'api_latency']`.
2. `.venv/bin/python -c "import yaml; print(yaml.safe_load('a: [1, 2]\nb: {c: true}'))"` prints `{'a': [1, 2], 'b': {'c': True}}`.
3. `.venv/bin/python -c "import yaml; yaml.safe_load('!!python/object:os.system {}')"` raises a `ConstructorError`: safe mode refuses it.
4. `make rules` then `git diff --stat slo/generated` shows no change.
5. Break `slos.yaml` (delete a `target:` line) and run `make rules`; the message names the file.

## Mistakes people make

- Using `yaml.load` with untrusted input.
- Trusting parsed values without validation. Here every value is converted and checked.
- Forgetting that YAML guesses types (see the YAML note).

## Check yourself

1. What does `safe_load` return for a YAML mapping?
   *Answer: a Python `dict`.*
2. Why wrap errors in `InvalidSloError`?
   *Answer: to give one clear error type and a message naming the file.*
3. Which two things does the code do with YAML files?
   *Answer: reads the SLO definition; writes generated Prometheus rules.*
4. Why is `types-PyYAML` in the dev dependencies?
   *Answer: it gives mypy type information for PyYAML.*
5. What does `safe_load` refuse that `load` can accept?
   *Answer: tags that construct arbitrary Python objects.*

## Further reading

- PyYAML 6.0.2: <https://pyyaml.org/wiki/PyYAMLDocumentation>
