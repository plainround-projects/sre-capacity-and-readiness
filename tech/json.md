# JSON

## What it is

**JSON** is a plain-text format for data. It has objects `{"name": value}`, arrays `[1, 2]`, text in double quotes, numbers, `true`, `false` and `null`. Almost every language can read and write it, so it is the common language of web services and of files that tools exchange.

## Why this repository uses it

JSON is the project's memory of what was measured. Every experiment writes its result as a JSON document under `docs/evidence/`: the knee test's steps, the soak, the plan, the fleet runs, the dependency test, the restore. The capacity plan, the runbooks and the readiness review are generated from those files, and tests hold the documents to them. JSON is also the body of every HTTP request and response between the instances and the ledger, and the format of the dashboard file.

## Where it appears

| File | Use |
| --- | --- |
| `docs/evidence/*.json` | One measured document per experiment (written by the make targets). |
| `infrastructure/project_files.py` | `JsonEvidence`: loads and saves the evidence files. |
| `interfaces/loadtest_cli.py` | Prints one JSON document on standard output; the Makefile redirects it to a file. |
| `application/loadtest.py` | `to_dict()` methods build the report documents. |
| `dashboards/fleet.json` | A Grafana dashboard (see the Grafana dashboard note). |
| `interfaces/instance_app.py`, `ledger_app.py` | JSON request and response bodies. |
| `application/readiness.py` | Parses the dashboard JSON to check its queries. |

## How it works underneath

`json.dumps(obj, indent=2)` turns a Python object into JSON text with two-space indentation; `json.loads(text)` turns text back into dictionaries, lists, strings and numbers. JSON has no date type and only one kind of number, so the code stores numbers explicitly (a float for a rate, an int for a count) and times as numbers of seconds. A file that holds one JSON document per experiment is easy to diff, review and commit.

## Walkthrough of this repository's use

### Saving and loading evidence

```python
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
```

- `load` returns `None` if the file does not exist: "the experiment has not been run". The review treats a missing document as a failure, so a check cannot pass on the strength of a file that is not there.
- `save` creates the folder (`mkdir(parents=True, exist_ok=True)`) and writes `json.dumps(document, indent=2) + "\\n"`, the trailing newline making a clean text file.

### What a report looks like

```python
@dataclass(frozen=True, slots=True)
class KneeReport:
    """The steps, the knee found in them (None when the load never reached it) and the load chosen below it."""

    steps: tuple[StepResult, ...]
    knee: Knee | None
    safe_rps: int | None
    margin: float
    generator_lag_p99_ms: float

    @property
    def problem(self) -> str | None:
        """Why these numbers cannot be trusted, or None. An untrustworthy knee is never saved as evidence."""
        if self.generator_lag_p99_ms > GENERATOR_LAG_LIMIT_MS:
            return f"the load generator itself was late by {self.generator_lag_p99_ms:.1f} ms at p99"
        if self.knee is None:
            return "the load never reached a knee"
        if self.knee.saturated_resource != "cpu":
            used = self.knee.cpu_utilization_at_knee
            share = "an unmeasured share" if used is None else f"{used * 100:.0f}%"
            return (
                f"p99 rose at {self.knee.knee_rps:g} rps with {share} of the core in use, so the host, not the "
                "instance, ran short of something"
            )
        return None

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready, with the resource named."""
        return {
            "steps": [_step_dict(step) for step in self.steps],
            "knee": None
            if self.knee is None
            else {
                "knee_rps": self.knee.knee_rps,
                "last_good_rps": self.knee.last_good_rps,
                "baseline_p99_ms": self.knee.baseline_p99_ms,
                "knee_p99_ms": self.knee.knee_p99_ms,
                "cpu_utilization_at_knee": self.knee.cpu_utilization_at_knee,
                "saturated_resource": self.knee.saturated_resource,
            },
            "safe_rps": self.safe_rps,
            "margin_below_knee": self.margin,
            "generator_lag_p99_ms": round(self.generator_lag_p99_ms, 2),
        }


def _step_dict(step: StepResult) -> dict[str, Any]:
    return {
        "target_rps": step.target_rps,
        "achieved_rps": round(step.achieved_rps, 1),
        "requests": step.requests,
        "errors": step.errors,
        "p50_ms": step.p50_ms,
        "p99_ms": step.p99_ms,
        "max_ms": step.max_ms,
        "attempts": step.attempts,
        "cpu_utilization": step.cpu_utilization,
    }
```

`KneeReport.to_dict` builds the document for `make knee`: a list of `steps` (each with `target_rps`, `achieved_rps`, `requests`, `errors`, `p50_ms`, `p99_ms`, `max_ms`, `attempts`, `cpu_utilization`), the `knee` (with `knee_rps`, `last_good_rps`, `baseline_p99_ms`, `knee_p99_ms`, `cpu_utilization_at_knee` and the `saturated_resource`), `safe_rps`, `margin_below_knee` and `generator_lag_p99_ms`. A conditional expression writes `null` when there is no knee.

### Standard output is the document

```python
def main(argv: Sequence[str] | None = None) -> None:
    """Entry point of `capacity-loadtest`."""
    configure_logging("WARNING")
    args = build_parser().parse_args(argv)
    result, problem = asyncio.run(_knee(args) if args.command == "knee" else _timed(args))
    if problem is not None:
        sys.exit(f"not a valid measurement, nothing written: {problem}. Run again when the machine is quiet.")
    print(json.dumps(result, indent=2))

```

The program prints its warning on standard error and `json.dumps(result, indent=2)` on standard output. Because the Makefile redirects only standard output (`> docs/evidence/knee.json`), the file contains clean JSON even when a warning was printed.

### JSON in HTTP

The instance posts `{"account": 17, "amount_cents": 4210}` to the ledger (`session.post(url, json=payload)`) and the ledger answers `{"id": 5001}`. The instance's own answer is `{"zone": "a", "instance": "a1", "digest": "..."}`. See the aiohttp note.

### The dashboard is read as data

```python
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
```

The readiness review loads `dashboards/fleet.json` and walks the whole structure recursively: `_expressions` returns every string stored under a key named `expr`. It then extracts the metric names from each query and checks that every one is a metric the system really exports (or a recorded `slo:` rule). A dashboard that queries a metric nothing produces fails the review.

## Try it yourself

1. `python3 -c "import json; print(json.dumps({'a': 1, 'b': [1, 2]}, indent=2))"` prints a two-space-indented document.
2. After a run (`make knee`): `python3 -m json.tool docs/evidence/knee.json | head -20` pretty-prints the evidence. (`json.tool` is a built-in command-line formatter.)
3. `.venv/bin/python -c "import json; d=json.load(open('dashboards/fleet.json')); print([p['title'] for p in d['panels']])"` lists the eight panel titles.
4. `.venv/bin/python -c "from capacity.application.readiness import _expressions; import json; print(len(_expressions(json.load(open('dashboards/fleet.json')))))"` prints `11`: the number of queries in the dashboard.
5. `docker compose run --rm -T loadtest soak --target http://app-a1:8080 --rps 20 --seconds 3 2>/dev/null | python3 -m json.tool | head` shows that standard output alone is valid JSON (the warning, if any, went to standard error and was discarded with `2>/dev/null`).

## Mistakes people make

- Mixing log lines into the same stream as the JSON document.
- Treating a missing evidence file as a pass.
- Storing derived numbers by hand. The documents are generated; the tests re-derive them.
- Expecting JSON to preserve a number's type: `1` and `1.0` look different to humans but not always to every reader.

## Check yourself

1. Why does the load test print JSON on standard output and warnings on standard error?
   *Answer: the Makefile redirects standard output to a file; a warning in the same stream would corrupt the JSON.*
2. What does `JsonEvidence.load` return for an experiment that has not run?
   *Answer: `None`.*
3. What does `indent=2` do?
   *Answer: writes the JSON on several lines with two-space indentation so people can read it.*
4. How does the review find all PromQL queries in the dashboard?
   *Answer: it walks the JSON recursively and collects every string stored under the key `expr`.*
5. Where do the capacity plan numbers come from?
   *Answer: from the evidence JSON files written by the experiments.*

## Further reading

- JSON (RFC 8259): <https://www.rfc-editor.org/rfc/rfc8259>
- `json` (Python 3.12): <https://docs.python.org/3.12/library/json.html>
