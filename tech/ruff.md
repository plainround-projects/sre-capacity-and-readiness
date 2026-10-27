# Ruff 0.17

## What it is

Ruff is a very fast Python **linter** and **formatter**. A linter reads code without running it and reports likely mistakes and style problems (an unused import, a missing docstring, a risky pattern). A formatter rewrites code into one consistent layout (spacing, line breaks, quote style). Ruff does both in one program.

## Why this repository uses it

Consistent code is easier to read and review, and a linter catches whole classes of bugs before a test runs. `make lint` runs ruff first so style problems cannot slip into a change.

## Where it appears

| File | Use |
| --- | --- |
| `pyproject.toml` | `[tool.ruff]` and `[tool.ruff.lint]` settings. |
| `Makefile` | `lint` runs `ruff check .` and `ruff format --check .`; `format` runs `ruff check --fix .` and `ruff format .`. |
| `.github/workflows/ci.yml` | `make lint` in the `check` job. |
| `.gitignore`, `.dockerignore` | Exclude `.ruff_cache/`. |

## How it works underneath

- `ruff check` parses each file into a tree, applies the selected **rules** (each has a code such as `E501` or `B008`), and prints each violation with file, line and rule code. Some violations have an automatic fix; `--fix` applies the safe ones.
- `ruff format` parses the file and prints it back in a fixed style (Black-compatible). `--check` does not change files; it only reports files that would change and exits with failure.
- The rule families are named by letter prefixes.

## Walkthrough of this repository's use

```toml
[tool.ruff]
line-length = 120
target-version = "py312"
src = ["src", "tests"]

[tool.ruff.lint]
select = ["E", "F", "I", "B", "UP", "SIM", "N", "C4", "RUF", "PT", "ASYNC", "D100", "D101", "D103", "D104"]

[tool.ruff.lint.per-file-ignores]
"tests/**" = ["D"]

```

- `line-length = 120`: lines may be 120 characters.
- `target-version = "py312"`: use Python 3.12 syntax rules.
- `src = ["src", "tests"]`: where first-party code lives, so import sorting knows which imports are local.
- `select = [...]` chooses rule families:

| Prefix | What it checks |
| --- | --- |
| `E` | pycodestyle errors (spacing, line length). |
| `F` | pyflakes: unused imports and variables, undefined names. |
| `I` | import sorting. |
| `B` | flake8-bugbear: likely bugs, such as mutable default arguments. |
| `UP` | pyupgrade: use newer syntax (for example `X | None` instead of `Optional[X]`). |
| `SIM` | simplifications. |
| `N` | naming conventions. |
| `C4` | better comprehensions. |
| `RUF` | ruff's own rules. |
| `PT` | pytest style. |
| `ASYNC` | async mistakes, such as blocking calls inside `async def`. |
| `D100`, `D101`, `D103`, `D104` | a docstring is required for public modules, classes, functions and packages. |

```toml
[tool.ruff.lint.per-file-ignores]
"tests/**" = ["D"]
```

`per-file-ignores`: tests (`tests/**`) are exempt from the docstring rules (`D`); test names already read as sentences.

```make
lint: $(STAMP) ## ruff check, ruff format check, mypy, import-linter, generated rules in sync
	$(VENV)/bin/ruff check .
	$(VENV)/bin/ruff format --check .
	$(VENV)/bin/mypy
	$(VENV)/bin/lint-imports
	$(MAKE) --no-print-directory rules-check

format: $(STAMP) ## apply ruff's formatting and safe fixes
	$(VENV)/bin/ruff check --fix .
	$(VENV)/bin/ruff format .
```

`ruff check .` and `ruff format --check .` are the checks; `format` uses `--fix` and plain `format` to change files.

## Try it yourself

1. `.venv/bin/ruff check .` prints `All checks passed!`.
2. Add `import os` at the top of a source file and run it again: `F401 'os' imported but unused`. Run `.venv/bin/ruff check --fix .` and the line disappears.
3. Add extra spaces inside a function and run `.venv/bin/ruff format --check .`: it names the file; `.venv/bin/ruff format .` fixes it.
4. `.venv/bin/ruff rule B008` prints the explanation of one rule.
5. `.venv/bin/ruff check --select D --statistics src` counts docstring violations (none).

## Mistakes people make

- Fighting the formatter. Let it decide.
- Silencing rules with `# noqa` instead of fixing.
- Running the formatter and the checker with different line lengths.
- Treating lint success as proof of correctness.

## Check yourself

1. What is the difference between `check` and `format`?
   *Answer: check finds problems; format rewrites layout.*
2. What does `--check` on format do?
   *Answer: reports without changing, and fails if changes would be made.*
3. What does `I` select?
   *Answer: import sorting.*
4. Why are docstring rules off in tests?
   *Answer: test names already describe behaviour.*
5. What does `UP` do?
   *Answer: flags older syntax that has a modern replacement.*

## Further reading

- Ruff 0.17 docs: <https://docs.astral.sh/ruff/>
- Rule list: <https://docs.astral.sh/ruff/rules/>
