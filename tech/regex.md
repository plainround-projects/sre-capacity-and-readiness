# Regular expressions (`re`)

## What it is

A **regular expression** (regex) is a small pattern language for matching text. `a+` matches one or more `a`; `[0-9]` matches one digit; `\b` matches the edge of a word; `.` matches any character; `*` means "any number of"; `^` and `$` are the start and end of a line; `(...)` groups. Python's `re` module searches and rewrites text with these patterns.

## Why this repository uses it

Three places need to find structure in text. The load test reads one number out of Prometheus text format (the instance's CPU seconds). The readiness review finds the metric names inside PromQL queries in the dashboard, to prove each one is exported. The report generator rewrites the *measured* blocks inside the runbooks with fresh numbers.

## Where it appears

| File | Use |
| --- | --- |
| `infrastructure/openloop.py` | `_CPU_LINE`: finds `process_cpu_seconds_total <number>` in a `/metrics` page. |
| `application/readiness.py` | `_metric_names`: strips selectors, ranges and strings from a PromQL query, then finds identifiers. Also checks runbook headings. |
| `interfaces/capacity_cli.py` | `_MEASURED`: finds `<!-- measured --> ... <!-- /measured -->` blocks in the runbooks. |

## How it works underneath

`re.compile(pattern, flags)` turns a pattern into a matcher. `.search(text)` finds the first match anywhere; `.findall(text)` returns all matches; `re.sub(pattern, replacement, text)` replaces matches. **Flags** change behaviour: `MULTILINE` makes `^` and `$` match at each line; `DOTALL` lets `.` match a newline. A **group** `(...)` captures part of a match (`match.group(1)`). `*?` is **lazy**: it matches as little as possible. A `r"..."` string (**raw string**) keeps backslashes as typed.

## Walkthrough of this repository's use

### Reading the CPU counter

```python
_CPU_LINE = re.compile(r"^process_cpu_seconds_total\s+([0-9.eE+-]+)\s*$", re.MULTILINE)
```

`^process_cpu_seconds_total\s+([0-9.eE+-]+)\s*$` with `re.MULTILINE`: a line that starts with the metric name, then whitespace (`\s+`), then a number captured as group 1, then optional whitespace until the end of the line. The character class `[0-9.eE+-]` allows digits, a decimal point, `e`/`E` and signs, which covers numbers like `12.5` and `1.2e-05`. `MetricsCpuProbe.cpu_seconds` then does `float(match.group(1))`.

```python
class MetricsCpuProbe:
    """Reads `process_cpu_seconds_total` from an instance's ops port: how much CPU the process has used so far."""

    def __init__(self, host: str, port: int) -> None:
        """`port` is the instance's ops port."""
        self._host = host
        self._port = port

    async def cpu_seconds(self) -> float:
        """Cumulative CPU seconds of the instance."""
        reader, writer = await asyncio.open_connection(self._host, self._port)
        try:
            writer.write(f"GET /metrics HTTP/1.0\r\nHost: {self._host}\r\n\r\n".encode())
            await writer.drain()
            text = (await reader.read()).decode("utf-8", "replace")
        finally:
            writer.close()
        match = _CPU_LINE.search(text)
        if match is None:
            raise ValueError("the instance exports no process_cpu_seconds_total")
        return float(match.group(1))
```

The probe opens a TCP connection, requests `/metrics`, reads the whole page, and searches it. If there is no such line it raises `ValueError`: the instance does not export CPU, and the knee test cannot name the resource.

### Metric names in a query

```python
def _metric_names(expression: str) -> set[str]:
    """Identifiers in a PromQL expression, with selectors, ranges, strings and grouping clauses taken out."""
    bare = re.sub(r'"[^"]*"', "", re.sub(r"\{[^}]*\}", "", re.sub(r"\[[^\]]*\]", "", expression)))
    bare = re.sub(r"\b(?:by|without|on|ignoring)\s*\([^)]*\)", "", bare)
    names = set(re.findall(r"[a-zA-Z_:][a-zA-Z0-9_:]*", bare))
    return {name for name in names if name not in _PROMQL_WORDS}
```

The function removes, step by step, everything that is not a metric name, and then reads identifiers:

1. `re.sub(r"\[[^\]]*\]", "", expression)` deletes range selectors such as `[30s]`.
2. `re.sub(r"\{[^}]*\}", "", ...)` deletes label selectors such as `{job="instances"}`.
3. `re.sub(r'"[^"]*"', "", ...)` deletes strings.
4. `re.sub(r"\b(?:by|without|on|ignoring)\s*\([^)]*\)", "", bare)` deletes grouping clauses such as `by (le)`.
5. `re.findall(r"[a-zA-Z_:][a-zA-Z0-9_:]*", bare)` finds every identifier (letters, digits, underscores and colons, not starting with a digit).
6. Names that are PromQL functions (`sum`, `rate`, `histogram_quantile`, ...) are dropped using the `_PROMQL_WORDS` set.

What remains are metric names (and recorded rules such as `slo:sli_error:ratio_rate1h`, whose colons are part of the name).

### Rewriting the measured blocks

```python
_MEASURED = re.compile(r"<!-- measured -->.*?<!-- /measured -->", re.DOTALL)
```

`<!-- measured -->.*?<!-- /measured -->` with `re.DOTALL`: from the opening marker to the nearest closing marker, across lines (`.*?` is lazy so it stops at the first closing marker).

```python
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
```

For each runbook, `cmd_report` builds the block `<!-- measured -->` + the facts + `<!-- /measured -->` and substitutes it with `_MEASURED.sub(...)`. The replacement string has its backslashes doubled (`block.replace("\\\\", "\\\\\\\\")`) because in a replacement string `\\` means something special; this keeps text exactly as written. The effect: running `make plan-doc` writes the measured knee, safe load, pool size and fleet size into the three runbooks, so they can never disagree with the evidence.

## Try it yourself

1. `.venv/bin/python -c "import re; print(re.search(r'^process_cpu_seconds_total\\s+([0-9.eE+-]+)\\s*$', 'process_cpu_seconds_total 12.5\\n', re.M).group(1))"` prints `12.5`.
2. `.venv/bin/python -c "from capacity.application.readiness import _metric_names; print(sorted(_metric_names('sum(rate(app_requests_total{code=\"200\"}[30s])) by (instance)')))"` prints `['app_requests_total']`.
3. `grep -n "measured" docs/runbooks/zone-loss.md` shows the two marker lines.
4. `.venv/bin/python -c "import re; print(re.sub(r'<!-- measured -->.*?<!-- /measured -->', 'X', 'a <!-- measured -->old<!-- /measured --> b', flags=re.S))"` prints `a X b`.
5. `.venv/bin/python -m pytest tests/unit/test_capacity_report.py -q` runs the tests of the runbook facts and the rewriting.

## Mistakes people make

- Greedy `.*` where lazy `.*?` is needed: it would swallow everything up to the *last* closing marker.
- Forgetting `re.MULTILINE` when using `^` and `$` on a many-line page.
- Forgetting `re.DOTALL` when the match crosses lines.
- Putting a backslash in a replacement string without escaping.
- Writing patterns without a raw string, so `\b` becomes a backspace character.

## Check yourself

1. What does `[0-9.eE+-]+` match?
   *Answer: one or more characters from digits, a point, e/E, plus and minus: a number such as `1.2e-05`.*
2. Why is the measured-block pattern lazy?
   *Answer: so each block ends at its own closing marker, not at the last one in the file.*
3. Why does the review remove `{...}` and `[...]` before looking for names?
   *Answer: label names, label values and durations are not metric names.*
4. What does group 1 hold in the CPU pattern?
   *Answer: the number.*
5. What does `re.sub` return?
   *Answer: a new string with every match replaced.*

## Further reading

- `re` (Python 3.12): <https://docs.python.org/3.12/library/re.html>
- Regular Expression HOWTO: <https://docs.python.org/3.12/howto/regex.html>
