# mypy 2.4

## What it is

mypy is a **static type checker** for Python. Python does not require you to declare types, but you can write **type hints** (`def f(x: int) -> str`). mypy reads the whole program without running it and checks that the hints agree: that you do not pass text where a number is expected, that you handle `None`, and that a function returns what it promised.

## Why this repository uses it

The domain code makes strong promises (a `Slo` always has a valid target, a `ShortCode` is always seven characters). Types make those promises checkable across layers, and catch mistakes such as forgetting that a lookup can return `None`.

## Where it appears

| File | Use |
| --- | --- |
| `pyproject.toml` | `[tool.mypy]` and two `[[tool.mypy.overrides]]` entries. |
| `Makefile` | `lint` runs `mypy` (no arguments: the config says what to check). |
| `requirements-dev.lock` | Pins `mypy` and `types-PyYAML`. |
| All of `src/` and `tests/` | The type hints themselves. |

## How it works underneath

mypy builds a model of every module's types, then checks each expression. Where a library has no hints (`prometheus_client`) it cannot check calls; the config tells it to accept that. **Stub packages** such as `types-PyYAML` supply hints for libraries that have none. **Strict mode** switches on all optional checks, including "every function needs annotations" and "no implicit `Any`".

## Walkthrough of this repository's use

```toml
[tool.mypy]
python_version = "3.12"
strict = true
files = ["src", "tests"]

[[tool.mypy.overrides]]
module = ["prometheus_client", "prometheus_client.*"]
ignore_missing_imports = true

# Test functions are named for the behavior they check; annotating every one adds noise, not safety.
[[tool.mypy.overrides]]
module = ["tests.*"]
disallow_untyped_defs = false
disallow_incomplete_defs = false
disallow_untyped_calls = false
```

- `python_version = "3.12"` and `strict = true`.
- `files = ["src", "tests"]` makes plain `mypy` check both folders.
- The first override: `module = ["prometheus_client", "prometheus_client.*"]` with `ignore_missing_imports = true` accepts the missing hints for that library.
- The second override: for `tests.*`, three strict checks are off (`disallow_untyped_defs` and friends), because annotating every test function adds noise and no safety. A comment in the file says so.

### Type hints in the code

```python
class OpenLoopRunner(Protocol):
    """Sends requests on a fixed schedule, whether or not earlier ones finished."""

    async def run(self, rps: float, duration_s: float) -> tuple[list[Sample], float]:
        """Return every request's sample and the generator's own p99 lag in milliseconds (how late it sent)."""
        ...


class CpuProbe(Protocol):
    """Reads how much CPU time the process under test has used."""

    async def cpu_seconds(self) -> float:
        """Cumulative CPU seconds of the instance."""
        ...
```

`Protocol` classes describe the shape of a port. `async def run(...) -> tuple[list[Sample], float]` says exactly what the load generator returns: the samples and its own p99 lag. `float` in `cpu_seconds` and `dict[str, object] | None` in `EvidenceStore.load` force callers to handle the missing-experiment case. mypy complains if code uses a result without checking.

```python
def judge(self, status: int, latency_ms: float) -> tuple[bool, bool]:
    """Return (valid, good) for one request."""
    if self.kind is SliKind.AVAILABILITY:
        return True, status < 500
    succeeded = status < 500
    assert self.threshold_ms is not None
    return succeeded, succeeded and latency_ms < self.threshold_ms
```

`assert self.threshold_ms is not None` narrows the type from `float | None` to `float`, so the next line type-checks.

This repository has no `# type: ignore` comments: every error mypy raised was fixed in the code (for example, the instance's cleanup context is annotated `AsyncIterator[None]`).

## Try it yourself

1. `.venv/bin/mypy` prints `Success: no issues found in N source files`.
2. In `domain/slo.py` change `-> float` of `burn_rate` to `-> str` and run `mypy`: it reports `Incompatible return value type`.
3. `.venv/bin/mypy --strict src/capacity/domain/requests.py` checks one file.
4. `.venv/bin/mypy --show-error-codes` shows codes like `[arg-type]`.
5. Remove the `assert` in `Slo.judge` and run mypy: `Unsupported operand types for < ("float" and "None")`. (Revert.)

## Mistakes people make

- Using `Any` everywhere, which turns checking off.
- Blanket `# type: ignore` without an error code.
- Thinking hints are enforced at run time. They are not.
- Skipping `None` handling.

## Check yourself

1. Does Python enforce type hints?
   *Answer: no; mypy checks them before running.*
2. What does `X | None` force?
   *Answer: callers must handle None.*
3. What is a stub package?
   *Answer: a package providing type hints for a library that lacks them.*
4. What does `strict = true` do?
   *Answer: enables all optional strict checks.*
5. Why relax rules for tests?
   *Answer: annotating each test function adds noise without safety.*

## Further reading

- mypy 2.4 docs: <https://mypy.readthedocs.io/en/stable/>
- Type hints cheat sheet: <https://mypy.readthedocs.io/en/stable/cheat_sheet_py3.html>
