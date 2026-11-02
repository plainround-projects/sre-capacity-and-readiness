# GitHub Actions

## What it is

GitHub Actions is GitHub's built-in automation. A **workflow** is a YAML file in `.github/workflows/`. It says "when this event happens (a push, a pull request), start these **jobs**". Each job runs on a fresh virtual machine (a **runner**) and is a list of **steps**. A step either runs a shell command (`run:`) or uses a ready-made **action** (`uses:`), a reusable package of steps.

## Why this repository uses it

The lesson's rule is that checks must run the same way for everyone and never be skipped. CI (continuous integration) runs the same `make` targets a person runs by hand, on every push, so a change that breaks lint, tests, alert-rule replays or the running fleet is caught.

## Where it appears

| File | Role |
| --- | --- |
| `.github/workflows/ci.yml` | The only workflow: three jobs, `check`, `rules` and `e2e`. |

(The repository is not pushed anywhere yet; the file is ready for when it is.)

## How it works underneath

- GitHub reads the YAML when events occur. For `on: [push, pull_request]`, every push and every pull request starts the workflow.
- Jobs run in parallel unless one `needs:` another. Each job gets its own clean machine, so nothing carries over between jobs.
- `ubuntu-latest` is a GitHub-hosted Linux runner with Docker, Git, `make` and some Python versions installed.
- `actions/checkout@v4` downloads the repository's files onto the runner. `actions/setup-python@v5` installs the requested Python.
- A step fails if its command returns a non-zero exit code; the job stops and shows red.
- `if: always()` makes a step run even when earlier steps failed. That is how cleanup is guaranteed.

## Walkthrough of this repository's use

```yaml
name: ci

on: [push, pull_request]

jobs:
  check:
    name: Format, lint, types, import rules, generated files, unit and integration tests with coverage, audit
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: make lint
      - run: make test-unit
      - run: make audit

  rules:
    name: Alert rules replayed with promtool
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: make test-rules

  e2e:
    name: Smoke tests of the fleet on the Docker Compose stack
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: make up
      - run: make test-e2e
      - if: always()
        run: make down
```

### Top of the file

- `name: ci` is the name shown on GitHub.
- `on: [push, pull_request]` is the trigger list.

### Job `check`

- `name:` is a long label that says what the job covers.
- `runs-on: ubuntu-latest` picks the machine.
- `actions/checkout@v4` gets the code.
- `actions/setup-python@v5` with `python-version: "3.12"` installs Python 3.12 (quoted so YAML does not read it as a number; an unquoted `3.10` would become `3.1`).
- `run: make lint`, `make test-unit`, `make audit` run the project's own targets. `make lint` creates the `.venv` first (through the `$(STAMP)` prerequisite), so no separate install step is needed.

### Job `rules`

No Python is needed: `make test-rules` only runs Docker containers, and Docker is already on the runner. This job replays the alert rules with `promtool`.

### Job `e2e`

`make up` builds and starts the Compose stack (nine instances, Envoy, the ledger, PostgreSQL, Prometheus) and waits until it is healthy; `make test-e2e` runs smoke tests against the running fleet (Envoy answers, the instances answer, Prometheus scrapes its targets, the metric names the dashboard and alerts use really exist); the last step, `make down`, has `if: always()` so containers are removed even if the tests failed. The long experiments (`make run`) are not part of CI: they need several free CPU cores and about twenty minutes.

## Try it yourself

1. Run the same three commands locally: `make lint && make test-unit && make audit`. If they pass, the `check` job passes.
2. `make test-rules` is the `rules` job.
3. `make up && make test-e2e && make down` is the `e2e` job.
4. `.venv/bin/python -c "import yaml; print(list(yaml.safe_load(open('.github/workflows/ci.yml'))['jobs']))"` prints `['check', 'rules', 'e2e']`.
5. After pushing the repository to GitHub, look under the Actions tab.

## Mistakes people make

- Writing CI commands that differ from the local ones. Here both use `make`.
- Not pinning action versions (`@v4`).
- Unquoted Python versions.
- Forgetting cleanup (`if: always()`).
- Putting secrets in the workflow file.

## Check yourself

1. What is a job and what is a step?
   *Answer: a job runs on one runner; a step is one command or action in it.*
2. What does `actions/checkout@v4` do?
   *Answer: downloads the repository onto the runner.*
3. What does `if: always()` do?
   *Answer: runs the step even if previous steps failed.*
4. Why does CI call `make` targets?
   *Answer: the same commands run locally and in CI.*
5. Why does the `rules` job skip Python setup?
   *Answer: `make test-rules` only needs Docker.*

## Further reading

- Workflow syntax: <https://docs.github.com/en/actions/using-workflows/workflow-syntax-for-github-actions>
- `actions/checkout`: <https://github.com/actions/checkout>
- `actions/setup-python`: <https://github.com/actions/setup-python>
