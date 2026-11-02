# pytest 9, pytest-asyncio and coverage

## What it is

**pytest** is the most used Python testing tool. A **test** is a function whose name starts with `test_`; it runs some code and uses `assert` to state what must be true. pytest finds the tests, runs them, and reports which passed. A **fixture** is a function that prepares something a test needs (a temporary folder, a running server) and hands it over by name.

**pytest-asyncio** lets `async def` tests run on an event loop. **pytest-cov** (built on **coverage**) records which lines of the program ran during the tests, and so which lines no test touches.

## Why this repository uses it

The knee search, the percentile function, the plan formula and the readiness decision are small pieces of arithmetic where a mistake silently produces a wrong fleet size. Tests pin them: for example, the lesson's numbers must give five instances for the peak and eight in all; losing the largest zone must always leave enough for the peak whatever the load; a spike that goes away is noise, not a knee. A second kind of test holds the *measurements* and the documents built from them to the lesson's Done when lines.

## Where it appears

| Path | What it holds |
| --- | --- |
| `tests/unit/` | Fast tests of the domain and application code with fakes: knee and plan, latency and load test, readiness, SLO arithmetic, settings, alerting, the capacity report, and the committed evidence. |
| `tests/integration/` | An instance, a stand-in ledger and the open-loop generator over real local sockets, and the command-line programs. Marker `integration`. |
| `tests/e2e/` | Smoke tests of the running fleet. Marker `e2e`. |
| `tests/measurements/` and `tests/unit/test_evidence.py` | The lesson's Done when lines held against `docs/evidence`. Marker `measurements`. |
| `tests/conftest.py` | Shared fixtures: `root`, `spec` and `evidence`. |
| `pyproject.toml` | `[tool.pytest.ini_options]` and `[tool.coverage.*]`. |
| `Makefile` | `test-unit`, `test-integration`, `test-e2e`, `test-measurements`. |

## How it works underneath

1. pytest **collects**: it walks `testpaths`, imports each `test_*.py` file and gathers functions named `test_*` and methods of classes named `Test*`.
2. For each test it builds the fixtures named in the function's parameters. A `scope="session"` fixture is built once for the whole run; the default is per test.
3. It runs the test. An `assert` that fails raises `AssertionError`; pytest rewrites asserts to show the values on both sides.
4. **Markers** such as `@pytest.mark.integration` (or `pytestmark = pytest.mark.integration` for a whole file) label tests; `-m integration` selects them.
5. With `asyncio_mode = "auto"`, every `async def test_...` and async fixture is run on an event loop automatically.
6. **Coverage** hooks into Python's tracing and counts executed lines (and, with `branch = true`, both sides of each `if`).

## Walkthrough of this repository's use

### Configuration

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-ra --strict-markers"
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "function"
markers = [
    "integration: real sockets in this process, no containers",
    "e2e: needs the Docker Compose stack (make up)",
    "measurements: the lesson's Done when lines held against the committed measurements (make test-measurements)",
]

[tool.coverage.run]
source = ["capacity"]
branch = true

[tool.coverage.report]
show_missing = true
skip_covered = false

```

- `testpaths = ["tests"]`: where to look.
- `addopts = "-ra --strict-markers"`: extra flags every run gets. `-ra` prints a short summary of everything that did not simply pass. `--strict-markers` makes a misspelt marker an error.
- `asyncio_mode = "auto"`, `asyncio_default_fixture_loop_scope = "function"`: async tests run on a fresh loop per test.
- `markers`: registers `integration`, `e2e` and `measurements` with a description.
- Coverage: `source = ["capacity"]` limits what is measured; `branch = true`; `show_missing = true` lists the line numbers that were not run.

### Shared fixtures

```python
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
```

`@pytest.fixture(scope="session")` builds `root` (the project folder, found from this file's location), `spec` (the parsed `slos.yaml`) and `evidence` (a function that loads `docs/evidence/<name>.json`) once per run.

### Unit tests of the knee and the plan

```python
def test_the_knee_is_the_first_step_where_p99_jumps_and_the_last_good_step_is_before_it(self):
    knee = find_knee(LESSON)
    assert (knee.knee_rps, knee.last_good_rps) == (550, 500)

def test_the_resource_is_named_from_cpu_evidence_not_from_throughput(self):
    assert find_knee(LESSON).saturated_resource == "cpu"

def test_without_cpu_evidence_the_resource_is_not_guessed(self):
    flat = [step(rps, 10.0 if rps < 500 else 400.0) for rps in (100, 200, 300, 400, 500)]
    assert find_knee(flat).saturated_resource.startswith("unknown")

def test_a_curve_that_never_rises_has_no_knee(self):
    with pytest.raises(NoKneeError):
        find_knee([step(rps, 12.0) for rps in (100, 200, 300, 400, 500)])

def test_too_few_steps_cannot_show_a_flat_part_and_a_knee(self):
    with pytest.raises(NoKneeError):
        find_knee([step(100, 12.0), step(200, 500.0)])

def test_errors_alone_also_mark_the_knee(self):
    steps = [step(100, 12.0), step(200, 12.0), step(300, 12.0), step(400, 14.0, errors=50)]
    assert find_knee(steps).knee_rps == 400

def test_a_spike_that_goes_away_again_is_noise_not_a_knee(self):
    noisy = [step(100, 12.0), step(200, 12.0), step(300, 12.0), step(400, 90.0), step(500, 13.0), step(600, 14.0)]
    with pytest.raises(NoKneeError):
        find_knee(noisy)

def test_a_rise_that_persists_is_the_knee_even_if_it_dips_later(self):
    steps = [step(100, 12.0), step(200, 12.0), step(300, 12.0), step(400, 90.0), step(500, 400.0), step(600, 30.0)]
    assert find_knee(steps).knee_rps == 400

def test_steps_given_out_of_order_are_sorted(self):
    assert find_knee(list(reversed(LESSON))).knee_rps == 550
```

Tests are plain functions or methods of a class with sentence names. They state the behaviours the lesson relies on: the knee is the first step where p99 jumps and persists; the resource is named from CPU evidence, not from throughput; without CPU evidence the resource is *not guessed*; a spike that goes away is noise; steps given out of order are sorted. `assert` states each expectation; pytest rewrites it to show both sides on failure.

```python
    @pytest.mark.parametrize("margin", [0, 1, -0.2, 1.5])
    def test_the_margin_must_be_a_fraction(self, margin):
        with pytest.raises(InvalidPlanError):
            safe_load(find_knee(LESSON), margin=margin)


class TestPlan:
    def test_the_lessons_numbers_give_five_instances_for_peak_and_eight_in_all(self):
        plan = plan_capacity(1000, 2, 400, zones=3)
        assert (plan.future_peak_rps, plan.instances_for_peak, plan.total_instances) == (2000, 5, 8)
        assert plan.distribution == (3, 3, 2)
        assert plan.surviving_after_zone_loss == 5

    def test_the_surviving_fleet_carries_the_whole_projected_peak(self):
        plan = plan_capacity(1000, 2, 400, zones=3)
        assert plan.surviving_capacity_rps >= plan.future_peak_rps

    def test_headroom_comes_from_the_projected_peak_not_todays(self):
        today = plan_capacity(1000, 1, 400)
        future = plan_capacity(1000, 2, 400)
        assert future.total_instances > today.total_instances

    @pytest.mark.parametrize("peak", [500, 1000, 1777, 3000, 10_000])
    def test_whatever_the_load_losing_the_largest_zone_still_leaves_enough(self, peak):
        plan = plan_capacity(peak, 2, 313, zones=3)
        assert plan.total_instances - max(plan.distribution) >= plan.instances_for_peak

    def test_more_zones_need_less_headroom(self):
        assert (
            plan_capacity(1000, 2, 400, zones=4).total_instances <= plan_capacity(1000, 2, 400, zones=3).total_instances
        )

    def test_spread_gives_larger_zones_first(self):
        assert spread(8, 3) == (3, 3, 2)
        assert spread(9, 3) == (3, 3, 3)

    def test_the_formula_is_written_out_with_every_input(self):
        text = plan_capacity(1000, 2, 400).formula()
```

The plan tests include a **parametrized** test: `@pytest.mark.parametrize("peak", [...])` runs the same test function once for each listed value, here checking that for any peak, losing the largest zone still leaves enough instances. One test body, many cases.

### Integration tests with a real instance

```python
def settings(ledger_url=None, rounds=200, timeout=0.3):
    return InstanceSettings("b", "b2", "v7", rounds, ledger_url, timeout, 0, 0, "INFO")


async def start(app):
    server = TestServer(app)
    await server.start_server()
    return server


@pytest.fixture
async def instance():
    metrics = InstanceMetrics("b", "b2")
    server = await start(instance_app.build_app(settings(), metrics))
    ops = await start(build_ops_app(metrics.render))
    yield server, ops, metrics
    await server.close()
    await ops.close()


async def get(url):
    async with aiohttp.ClientSession() as session, session.get(url) as response:
        return response.status, dict(response.headers), await response.text()


```

`pytestmark = pytest.mark.integration` marks the whole file. `settings(...)` builds instance settings for a test (port 0 means "pick any free port"). The `instance` fixture starts the real instance app and its ops app on free ports with `TestServer`, yields them, and closes them afterwards (`yield` splits setup from teardown). The tests then run the project's own open-loop generator against the real server.

### End-to-end smoke tests

```python
"""Smoke tests of the running fleet. Needs `make up` first."""

import json
import time
import urllib.request

import pytest

from capacity.domain.telemetry import EXPORTED_METRICS

pytestmark = pytest.mark.e2e
EDGE = "http://localhost:47280"
ENVOY_ADMIN = "http://localhost:47281"
INSTANCE_OPS = "http://localhost:47283"
LEDGER_OPS = "http://localhost:47284"
PROMETHEUS = "http://localhost:47290"


def fetch(url: str) -> tuple[int, dict[str, str], str]:
    """GET a URL; header names come back lower-cased (Envoy lower-cases them)."""
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status, {k.lower(): v for k, v in response.headers.items()}, response.read().decode()
    except OSError as error:
        pytest.fail(f"{url} is not reachable ({error}); run `make up` first")


def test_requests_through_the_edge_reach_every_instance_in_every_zone():
    seen = set()
    zones = set()
    for _ in range(120):
        status, headers, _ = fetch(f"{EDGE}/work")
        assert status == 200
        seen.add(headers["x-instance"])
        zones.add(headers["x-zone"])
```

`fetch` makes a request and fails the test with a helpful message if the URL is not reachable ("run `make up` first"). The first test sends 120 requests through Envoy and checks that every instance in every zone answered, using the `x-instance` and `x-zone` headers (lower-cased by Envoy). Another test checks that every metric name in `EXPORTED_METRICS` exists on the running system.

### Measurement tests

```python
"""The lesson's Done when lines, held against the measurements `make run` left in docs/evidence.

These depend on the machine that took the measurements: a contended host shows a late load generator, a knee that is
noise, or a fleet that cannot hold the projected peak, and these tests then fail and say so. Run them with
`make test-measurements` after `make run`; `make run` ends with them.
"""

import pytest

SLO_P99_MS = 50.0


pytestmark = pytest.mark.measurements


def test_step_one_the_knee_was_found_and_cpu_was_the_resource_that_saturated(evidence):
    knee = evidence("knee")
    assert knee["knee"] is not None
    assert knee["knee"]["saturated_resource"] == "cpu"
    assert knee["knee"]["cpu_utilization_at_knee"] >= 0.9
    steps = knee["steps"]
    flat = [step["p99_ms"] for step in steps if step["target_rps"] < knee["knee"]["knee_rps"]]
    assert max(flat) < SLO_P99_MS, "p99 was flat and inside the SLO on the good side of the knee"
    assert knee["knee"]["knee_p99_ms"] > 3 * knee["knee"]["baseline_p99_ms"], "and jumped on the other side"


def test_step_two_the_safe_load_is_a_stated_margin_below_the_knee_and_held_for_the_whole_soak(evidence):
    knee, soak = evidence("knee"), evidence("soak")
    margin = 1 - knee["safe_rps"] / knee["knee"]["knee_rps"]
    assert 0.2 <= margin <= 0.4
    assert knee["margin_below_knee"] == 0.25
    assert soak["target_rps"] == knee["safe_rps"]
    assert soak["errors"] == 0
    assert soak["within_slo"] is True
```

These tests read the committed evidence and assert the lesson's Done when lines: the knee was found and CPU saturated first; the safe load is a stated margin below the knee and held for the whole soak; the planned fleet minus a zone held the projected peak; the next bottleneck is the shared connection pool and the plan names its size; a rolling restart and a rollback under load cost no request; and a frozen dependency fails requests fast while the instance stays healthy. They are marked `measurements` and excluded from `make test` (`-m 'not measurements'`) because they fail when the evidence is missing or was taken on a contended machine; run them with `make test-measurements` after `make run`. The docstring says so.

### Coverage gate

```make
test-unit: $(STAMP)
	$(PY) -m pytest tests/unit tests/integration -m 'not measurements' --cov=capacity --cov-report=term
	$(PY) -m coverage report --include='src/capacity/domain/*,src/capacity/application/*' --fail-under=95
```

`--cov=capacity --cov-report=term` prints a table of coverage per file. The second command, `coverage report --include='src/capacity/domain/*,src/capacity/application/*' --fail-under=95`, fails the build if domain and application lines are less than 95% covered. Infrastructure and interfaces are checked by the integration and end-to-end tests instead.

## Try it yourself

1. `make test-unit` runs the unit and integration tests (not the measurement tests) with coverage; the last lines show the coverage table.
2. `.venv/bin/python -m pytest tests/unit/test_knee_and_plan.py -q` runs one file; `-q` is quiet.
3. `.venv/bin/python -m pytest tests -m integration -q` selects by marker.
4. `.venv/bin/python -m pytest tests/unit -k knee -v` runs tests whose name contains `knee`, verbosely (`-v`).
5. In `src/capacity/domain/knee.py` change `RISE_FACTOR = 3.0` to `30.0` and run `tests/unit/test_knee_and_plan.py`: the knee tests fail, because no step now rises enough. (Revert after.)

## Mistakes people make

- Tests that depend on the real clock or on timing. The knee and plan logic is pure and tested without a clock.
- Mocks everywhere. The integration tests use real servers and real sockets (with a stand-in for the ledger); the end-to-end tests use the real fleet.
- Forgetting `await` or the async marker; `asyncio_mode = "auto"` removes that risk.
- Chasing 100% coverage. The gate is on the logic layers only.

## Check yourself

1. What is a fixture?
   *Answer: a function that prepares something a test needs and is requested by parameter name.*
2. What does a plain `assert` do in pytest?
   *Answer: it checks a condition; pytest rewrites it to show the values on both sides when it fails.*
3. What does `yield` do in a fixture?
   *Answer: splits setup (before) from teardown (after).*
4. What do markers allow?
   *Answer: selecting groups of tests, for example `-m e2e`.*
5. Why are the measurement tests excluded from `make test`?
   *Answer: they depend on evidence `make run` produced on a quiet machine, and fail when it is missing or was taken under load.*

## Further reading

- pytest 9.1: <https://docs.pytest.org/en/9.1.x/>
- pytest-asyncio 1.4: <https://pytest-asyncio.readthedocs.io/en/stable/>
- coverage.py 7.17: <https://coverage.readthedocs.io/en/7.17.0/>
- pytest-cov 7.1: <https://pytest-cov.readthedocs.io/en/stable/>
