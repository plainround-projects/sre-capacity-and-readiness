# TOML and pyproject.toml

## What it is

TOML is a plain-text configuration format. It looks like an INI file with proper types: `[section]` headers, `key = value` lines, text in quotes, numbers, `true`/`false`, lists in square brackets and nested **tables**. `[tool.ruff]` means "a table called `ruff` inside a table called `tool`".

`pyproject.toml` is the standard file a Python project uses to say what it is, what it needs, how to build it and how its tools are configured. Python tools all look for it in the project's top folder.

## Why this repository uses it

One file describes the package, its pinned runtime and development dependencies, its command-line programs, and the settings of every checking tool (ruff, mypy, pytest, coverage, import-linter). A new reader has one place to look.

## Where it appears

| File | What it does |
| --- | --- |
| `pyproject.toml` | The only TOML file. Sections: `[build-system]`, `[project]`, `[project.optional-dependencies]`, `[project.scripts]`, `[tool.setuptools.packages.find]`, `[tool.ruff]`, `[tool.mypy]`, `[tool.pytest.ini_options]`, `[tool.coverage.*]`, `[tool.importlinter]`. |
| `.editorconfig` | Gives `*.toml` two-space indentation. |
| `Makefile` | The install stamp depends on `pyproject.toml`; `lock` reads it. |
| `Dockerfile` | Copies it so `pip install .` can build the project. |

## How it works underneath

- `pip install .` (or `-e .`) reads `[build-system]` to learn which tool builds the package (here `setuptools`), installs that tool temporarily, and asks it to build a **wheel**: a zip file holding the package that pip then installs.
- `[project.scripts]` creates **command-line programs**. When pip installs the package, it writes a small launcher file into the environment's `bin` folder for each entry. The launcher imports the named function and calls it.
- **Editable install** (`pip install -e .`) installs a link instead of a copy, so changes to files under `src/` take effect immediately.
- Each tool reads only its own `[tool.NAME]` tables. Ruff reads `[tool.ruff]`; pytest reads `[tool.pytest.ini_options]`.

## Walkthrough of this repository's use

### Build system and project metadata

```toml
[build-system]
requires = ["setuptools>=75"]
build-backend = "setuptools.build_meta"

[project]
name = "sre-capacity-and-readiness"
version = "0.1.0"
description = "A capacity plan from a measured knee, a lost-zone fleet test, the next bottleneck, and a readiness review that ends in a launch decision."
readme = "README.md"
requires-python = ">=3.12"
license = { text = "MIT" }
authors = [{ name = "Sumit Srivastava" }]
dependencies = [
    "aiohttp>=3.14.4,<4",
    "prometheus-client==0.22.1",
    "PyYAML==6.0.2",
    "psycopg[binary]==3.2.9",
    "psycopg-pool==3.2.6",
    "uvloop==0.21.0",
]
```

- `requires = ["setuptools>=75"]` and `build-backend = "setuptools.build_meta"` choose the build tool.
- `name`, `version`, `description`, `readme`: the identity of the package. `requires-python = ">=3.12"` forbids older Pythons. `license = { text = "MIT" }` and `authors` are legal metadata. `{ ... }` is an **inline table**.
- `dependencies` lists the runtime libraries: `aiohttp>=3.14.4,<4` (web framework and client; any version from 3.14.4 up to but not including 4), `prometheus-client==0.22.1` (exactly this), `PyYAML==6.0.2`, `psycopg[binary]==3.2.9` (the PostgreSQL driver; the `[binary]` part is an **extra** that also installs a pre-built copy of the C library it needs) and `psycopg-pool==3.2.6` (connection pools for it). The lock files pin the exact version of these and of everything they need.

### Optional dependencies

```toml
[project.optional-dependencies]
dev = [
    "pytest>=8,<10",
    "pytest-asyncio>=0.24",
    "pytest-cov>=5",
    "ruff>=0.6",
    "mypy>=1.11",
    "import-linter>=2.0",
    "pip-audit>=2.7",
    "types-PyYAML",
]
```

`[project.optional-dependencies]` with a group called `dev` lists tools only needed while developing: pytest, plugins, ruff, mypy, import-linter, pip-audit and `types-PyYAML` (type information for PyYAML). `pip install .[dev]` would add them. The Makefile uses the locked list instead.

### Command-line programs

```toml
[project.scripts]
capacity-instance = "capacity.interfaces.instance_app:main"
capacity-ledger = "capacity.interfaces.ledger_app:main"
capacity-loadtest = "capacity.interfaces.loadtest_cli:main"
capacity-experiments = "capacity.interfaces.experiments_cli:main"
capacity-slo = "capacity.interfaces.slo_cli:main"
capacity = "capacity.interfaces.capacity_cli:main"
```

Each line `NAME = "module.path:function"` becomes a program. For example `capacity-instance = "capacity.interfaces.instance_app:main"` creates `.venv/bin/capacity-instance`, which imports `capacity.interfaces.instance_app` and calls `main()`. The six programs are `capacity-instance` (a service instance), `capacity-ledger` (the shared ledger), `capacity-loadtest` (the load generator), `capacity-experiments` (the fleet experiments), `capacity-slo` (rule generation) and `capacity` (the plan, report and review commands).

### Where the code is

```toml
[tool.setuptools.packages.find]
where = ["src"]
```

The code lives in `src/` (the "src layout"), and setuptools is told to look there. The benefit: tests cannot accidentally import code from the project folder without installing it.

### Ruff, mypy, pytest

```toml
[tool.ruff]
line-length = 120
target-version = "py312"
src = ["src", "tests"]
extend-exclude = ["tech"]

[tool.ruff.lint]
select = ["E", "F", "I", "B", "UP", "SIM", "N", "C4", "RUF", "PT", "ASYNC", "D100", "D101", "D103", "D104"]

[tool.ruff.lint.per-file-ignores]
"tests/**" = ["D"]

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

```

- `[tool.ruff]`: `line-length = 120`, `target-version = "py312"`, `src` folders for import sorting. `[tool.ruff.lint] select` chooses rule families; see the ruff note. `per-file-ignores` disables the docstring rules (`D`) in tests.
- `[tool.mypy]`: `strict = true` turns on every strict check; `files` says what to check. The `[[tool.mypy.overrides]]` entries (double brackets mean "an item of a list of tables") relax two things: `prometheus_client` has no type information, and test functions need not have annotations.
- `[tool.pytest.ini_options]`: `testpaths`, `addopts` (`-ra` shows a summary of non-passing tests, `--strict-markers` makes an unknown marker an error), `asyncio_mode = "auto"` (see the pytest note) and the registered `markers`: `integration`, `e2e` and `measurements` (the tests that hold the lesson's Done when lines to the committed measurements; `make test` leaves them out).

### Coverage and import-linter

```toml
[tool.coverage.run]
source = ["capacity"]
branch = true

[tool.coverage.report]
show_missing = true
skip_covered = false

[tool.importlinter]
root_package = "capacity"
include_external_packages = true

[[tool.importlinter.contracts]]
name = "Layers: interfaces -> infrastructure -> application -> domain, never upward"
type = "layers"
layers = [
    "capacity.interfaces",
    "capacity.infrastructure",
    "capacity.application",
    "capacity.domain",
]

[[tool.importlinter.contracts]]
name = "Domain and application know nothing about the web, the metrics library, YAML, PostgreSQL or subprocesses"
type = "forbidden"
source_modules = ["capacity.domain", "capacity.application"]
forbidden_modules = ["aiohttp", "prometheus_client", "yaml", "psycopg", "psycopg_pool", "subprocess"]
```

Coverage measures which lines tests ran: `source` limits it to this package, `branch = true` also tracks if/else both ways, `show_missing` lists missed lines. `[tool.importlinter]` and its `[[tool.importlinter.contracts]]` define the two architecture rules (see the import-linter note).

## Try it yourself

1. `python -c "import tomllib; d=tomllib.load(open('pyproject.toml','rb')); print(d['project']['name'], d['project']['version'])"` prints `sre-capacity-and-readiness 0.1.0`.
2. `ls .venv/bin | grep capacity` lists the six launcher programs created from `[project.scripts]`.
3. `.venv/bin/capacity --help` runs one of them and prints the usage.
4. `cat .venv/bin/capacity-instance` shows the tiny launcher script.
5. `.venv/bin/pip show sre-capacity-and-readiness` shows the installed metadata (and `Editable project location`).

## Mistakes people make

- Reading `[[x]]` (list of tables) as `[x]` (a table).
- Putting upper bounds everywhere or nowhere. Here runtime ranges are wide and the lock files fix the real versions.
- Forgetting that a `.toml` string needs quotes.
- Changing `pyproject.toml` and forgetting to regenerate the lock files (`make lock`).

## Check yourself

1. What does `[project.scripts]` create?
   *Answer: launcher programs in the environment's `bin` folder that call the named function.*
2. What is the difference between `[x]` and `[[x]]`?
   *Answer: the first is one table; the second adds one item to a list of tables.*
3. What does `strict = true` do for mypy?
   *Answer: it turns on all the strict type-checking options.*
4. Why a `src/` folder?
   *Answer: code is only importable once installed, so tests exercise the installed package.*
5. What is an editable install?
   *Answer: an installation that links to the source files so edits apply without reinstalling.*

## Further reading

- TOML 1.0 specification: <https://toml.io/en/v1.0.0>
- Python packaging guide, `pyproject.toml`: <https://packaging.python.org/en/latest/guides/writing-pyproject-toml/>
- setuptools 75: <https://setuptools.pypa.io/en/latest/userguide/pyproject_config.html>
