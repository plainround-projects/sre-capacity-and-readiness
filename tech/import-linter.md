# import-linter 2.15

## What it is

import-linter is a tool that checks the **import rules** of a Python project. In Python, a file uses code from another by writing `import` or `from x import y`. Over time these imports can tangle. import-linter reads all imports and fails if they break rules you wrote down, called **contracts**.

## Why this repository uses it

`ARCHITECTURE.md` says the code has four layers: `interfaces` on top of `infrastructure` on top of `application` on top of `domain`, and each may only import downward. The domain must know nothing about web servers or databases. Without a checker, one careless import can break that quietly.

## Where it appears

| File | Use |
| --- | --- |
| `pyproject.toml` | `[tool.importlinter]` and two contracts. |
| `Makefile` | `lint` runs `lint-imports`. |
| `.gitignore` | Excludes `.import_linter_cache/`. |

## How it works underneath

import-linter builds a graph: each module is a node, each import an arrow. A **layers** contract says modules in a higher layer may import lower ones but not the reverse. A **forbidden** contract says given source modules must not (even indirectly) import given external modules. With `include_external_packages = true` the graph includes third-party packages such as `aiohttp`.

## Walkthrough of this repository's use

```toml
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

- `root_package = "capacity"` is the package to analyse.
- Contract 1 (`type = "layers"`): the list is ordered top to bottom: interfaces, infrastructure, application, domain. Any import that goes up (for example `domain` importing `application`) fails.
- Contract 2 (`type = "forbidden"`): `source_modules` are `capacity.domain` and `capacity.application`; `forbidden_modules` are `aiohttp`, `prometheus_client`, `yaml`, `psycopg`, `psycopg_pool` and `subprocess`. Neither inner layer may use them, directly or through another module. That is why ports (Protocols) exist: the load-test use case asks for an `OpenLoopRunner` and a `CpuProbe`, and only infrastructure implements them with sockets; the experiments ask infrastructure to run `docker compose` (`subprocess`). The contract's name says it: "know nothing about the web, the metrics library, YAML, PostgreSQL or subprocesses".

## Try it yourself

1. `.venv/bin/lint-imports` prints both contracts as `KEPT` and `Contracts: 2 kept, 0 broken.`
2. Add `import subprocess` to `src/capacity/domain/slo.py` and run it again: contract 2 breaks and the chain is shown. (Revert.)
3. Add `from capacity.infrastructure.clock import SystemClock` to a domain file: contract 1 breaks.
4. `.venv/bin/lint-imports --verbose` lists the modules checked.
5. Read the layer descriptions in `ARCHITECTURE.md` and match them to the contract.

## Mistakes people make

- Putting a convenient helper in the wrong layer and importing upward.
- Importing a third-party library in the domain "just once".
- Not including external packages in the graph, so forbidden imports are not seen.

## Check yourself

1. What does a layers contract forbid?
   *Answer: imports from a lower layer back up to a higher layer.*
2. Why must domain not import `psycopg` or `subprocess`?
   *Answer: the domain must stay pure; storage and running programs belong in infrastructure.*
3. What does `include_external_packages` do?
   *Answer: puts third-party packages in the graph so they can be forbidden.*
4. How do the use cases still use storage?
   *Answer: through ports (Protocols) implemented in infrastructure.*
5. Which command runs the check?
   *Answer: `lint-imports` (part of `make lint`).*

## Further reading

- import-linter 2.15: <https://import-linter.readthedocs.io/en/stable/>
