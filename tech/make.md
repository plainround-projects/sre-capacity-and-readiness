# Make

## What it is

Make is a command-line tool that runs named recipes. A **recipe** is a list of shell commands. You put the recipes in a file called `Makefile`, and you run one with `make <name>`. Each named recipe is called a **target**. Make was written in 1976 to rebuild programs, so a target can also say "I need these other things first", and Make runs those first. These "other things" are called **prerequisites**.

Here, `make` means GNU Make or the Apple build of it. Both read the same file format for everything this repository uses.

## Why this repository uses it

A capacity exercise has many ordered steps: start the fleet, find the knee, soak at the safe load, compute the plan, run the fleet and lose a zone, sweep the pool, test the failures, write the plan document, run the review. The lesson's rule is that a reader needs only a few commands. `make run` is the whole story in the lesson's order; every step is also its own target so one experiment can be repeated alone. The Makefile is also the record of exactly which flags each experiment uses.

## Where it appears

| File | What it does with Make |
| --- | --- |
| `Makefile` | Defines `help`, `install`, `up`, `reset-fleet`, `run`, `knee`, `soak`, `plan`, `full-fleet`, `zone-loss`, `pool-sweep`, `dependency-test`, `rollout-test`, `restore-test`, `plan-doc`, `review`, `psql`, `rules`, `rules-check`, `lint`, `format`, `test-unit`, `test-rules`, `test`, `test-measurements`, `test-integration`, `test-e2e`, `audit`, `check`, `lock`, `docker-build`, `down`, `clean`. |
| `.github/workflows/ci.yml` | Calls `make lint`, `make test-unit`, `make audit`, `make test-rules`, `make up`, `make test-e2e`, `make down`. |
| `.editorconfig` | Forces tab indentation for the `Makefile`. |
| `README.md` | Tells the reader which targets to run. |

## How it works underneath

When you type `make up`, this happens in order:

1. Make opens the file named `Makefile` in the current folder and reads all of it. Nothing runs yet.
2. It finds the target called `up`.
3. It looks at the prerequisites of `up` (here `$(STAMP)`). For each prerequisite it checks whether a **file** with that name exists and is newer than what it depends on. If not, it runs that prerequisite's recipe first.
4. Then it runs the recipe lines of `up`, top to bottom. Each line goes to a new shell process (`/bin/sh`). If one line fails (non-zero exit code), Make stops and reports the error.

A target normally names a **file** Make should produce. Targets that do not produce a file (like `up` or `knee`) are **phony**: the `.PHONY` line says "these names are not files, always run them".

When a target lists several prerequisites and has no recipe, as `run` does, Make runs the prerequisites **in the order listed** and then stops. That is how one command runs twelve steps.

Variables are written `NAME := value` (set once, now), `NAME ?= value` (set only if unset) or `export NAME ?= value` (also pass it to every command Make starts), and used as `$(NAME)`. In a recipe, `$$` means "a literal dollar sign for the shell". Recipe lines must start with a **tab character**.

## Walkthrough of this repository's use

### The variables at the top

```make
# make up | run | knee | soak | review | test | lint | down      (make help lists everything)
PYTHON ?= $(shell command -v python3.13 || command -v python3.12 || command -v python3)
VENV := .venv
PY := $(VENV)/bin/python
STAMP := $(VENV)/.installed
PROMTOOL_IMAGE := prom/prometheus:v3.5.0
EVIDENCE := docs/evidence
LOADTEST := docker compose run --rm -T loadtest
EXPERIMENTS := $(VENV)/bin/capacity-experiments

-include .env
export STEP_SECONDS ?= 10
export SOAK_SECONDS ?= 120
CHECK_SECONDS ?= 40

.PHONY: help install preflight clean-evidence set-fleet confirm-plan up reset-fleet run knee soak plan plan-doc full-fleet zone-loss pool-sweep dependency-test rollout-test restore-test review test-measurements \
        psql rules rules-check lint format test test-unit test-rules test-integration test-e2e audit check lock docker-build down clean
```

- `PYTHON ?= $(shell command -v python3.13 || command -v python3.12 || command -v python3)` picks a Python (`command -v` prints a program's path if it exists; `||` tries the next).
- `VENV`, `PY`, `STAMP`: the private environment folder, the Python in it and the marker file that says "installed".
- `PROMTOOL_IMAGE`: the Docker image holding `promtool`.
- `EVIDENCE := docs/evidence` is where measured results go as JSON files.
- `LOADTEST := docker compose run --rm -T loadtest` is the command that starts the load generator container (see the Docker Compose note). Defining it once keeps the recipes short.
- `EXPERIMENTS := $(VENV)/bin/capacity-experiments` is the command-line program that runs the fleet experiments.
- `-include .env` reads `.env` if it exists.
- `export STEP_SECONDS ?= 10` and `export SOAK_SECONDS ?= 120`: how long each load step and the soak last. The lesson's real values are 120 s and 1800 s; the defaults are shorter so the exercise fits in about twenty minutes. You can override them: `make knee STEP_SECONDS=120`.
- `CHECK_SECONDS ?= 40` is the length of each fleet run (not exported; used only in this Makefile).
- The `.PHONY` list (continued with a trailing `\`) names all targets that are not files.

### `help` and the install stamp

```make
help: ## list the targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | sed -E 's/:.*## /\t/' | sort

$(STAMP): requirements-dev.lock pyproject.toml
	$(PYTHON) -m venv $(VENV)
	$(VENV)/bin/pip install -q --require-hashes -r requirements-dev.lock
	$(VENV)/bin/pip install -q --no-deps -e .
	touch $@

install: $(STAMP) ## create .venv from the lock file
```

`help` is a pipeline of `grep`, `sed` and `sort` that turns every `target: ... ## description` line into a help line. The stamp rule installs the pinned dependencies with hash checking and the project in editable mode (see the pip note), then `touch $@` marks success.

### `up`, `reset-fleet` and `run`

```make
up: $(STAMP) ## build and start the fleet: 9 instances in 3 zones (the plan says how many run), Envoy, the ledger, PostgreSQL, Prometheus
	docker compose up -d --build --wait
	@echo "edge http://localhost:$${EDGE_PORT:-47280}/work   Envoy admin http://localhost:$${ENVOY_ADMIN_PORT:-47281}   Prometheus http://localhost:$${PROMETHEUS_PORT:-47290}"

preflight: $(STAMP) ## refuse to measure when the machine is busy (one-minute load per CPU above 0.6, or MAX_LOAD)
	$(EXPERIMENTS) preflight

clean-evidence: ## remove the load-dependent evidence, so a run never mixes old and new measurements
	rm -f $(EVIDENCE)/*.json

set-fleet: $(STAMP) ## run exactly the instances the plan says and stop the spares
	$(EXPERIMENTS) set-fleet

reset-fleet: ## recreate every instance without the shared dependency, at version v1
	LEDGER_URL= APP_VERSION=v1 docker compose up -d --no-deps --force-recreate --wait app-a1 app-a2 app-a3 app-b1 app-b2 app-b3 app-c1 app-c2 app-c3

# The whole story, in the lesson's order: the knee, the safe load, the plan, the fleet and its lost zone, the next
# bottleneck, the failures the review needs evidence for, and the review.
run: preflight clean-evidence up reset-fleet knee soak plan set-fleet full-fleet zone-loss confirm-plan pool-sweep dependency-test rollout-test restore-test plan-doc review test-measurements ## the whole capacity and readiness exercise (about 20 minutes on a quiet machine)
```

- `up` builds and starts the fleet and waits until every health check passes (`--wait`).
- `reset-fleet` recreates all nine instances with no shared dependency (`LEDGER_URL=` empty) at release `v1`. The two variable assignments in front of the command apply only to it (see the Bash note). It is the starting state for the knee test, because an earlier run may have left the fleet pointing at the ledger or at `v2`.
- `run` lists `up reset-fleet knee soak plan full-fleet zone-loss pool-sweep dependency-test rollout-test restore-test plan-doc review test-measurements` and has no commands of its own: Make runs them left to right. The `##` description notes it takes about twenty minutes on a quiet machine.

### The load tests

```make
knee: $(STAMP) ## step the load up on one instance until p99 climbs; names the saturated resource (STEP_SECONDS per step)
	mkdir -p $(EVIDENCE)
	$(LOADTEST) knee --target http://app-a1:8080 --ops http://app-a1:9101 --step-seconds $(STEP_SECONDS) > $(EVIDENCE)/knee.json.tmp \
	  || { rm -f $(EVIDENCE)/knee.json.tmp; exit 1; }
	mv $(EVIDENCE)/knee.json.tmp $(EVIDENCE)/knee.json
	@$(PY) -c "import json; d=json.load(open('$(EVIDENCE)/knee.json')); print(json.dumps({k: d[k] for k in ('knee','safe_rps','margin_below_knee')}, indent=2)); [print(s) for s in d['steps']]"

soak: $(STAMP) ## hold one instance at the safe load for SOAK_SECONDS and check p99 in every window
	$(LOADTEST) soak --target http://app-a1:8080 --rps $$($(PY) -c "import json; print(json.load(open('$(EVIDENCE)/knee.json'))['safe_rps'])") \
	  --seconds $(SOAK_SECONDS) --label soak --ops http://app-a1:9101 > $(EVIDENCE)/soak.json.tmp \
	  || { rm -f $(EVIDENCE)/soak.json.tmp; exit 1; }
	mv $(EVIDENCE)/soak.json.tmp $(EVIDENCE)/soak.json
	@cat $(EVIDENCE)/soak.json
```

- `knee` runs the load generator against `http://app-a1:8080` (service names work inside the Compose network) with `--ops http://app-a1:9101` so it can read the instance's CPU use, steps of `$(STEP_SECONDS)` seconds, and writes the JSON report to `docs/evidence/knee.json` (`>` redirects standard output to the file). The `@$(PY) -c "..."` line prints a summary: the knee, the safe load and the margin, then one line per step.
- `soak` runs the same generator at the safe load found by `knee` (`$$($(PY) -c "...")` reads `safe_rps` from `knee.json`) for `$(SOAK_SECONDS)` seconds and writes `soak.json`. It depends on `knee.json` existing.

### The fleet experiments

```make
plan: $(STAMP) ## size the fleet from the measured safe load: the formula, the inputs, the result
	$(VENV)/bin/capacity plan

confirm-plan: $(STAMP) ## size the fleet again now that the lost-zone run exists, so the plan records that it was confirmed
	$(VENV)/bin/capacity plan

full-fleet: $(STAMP) ## run the projected peak (2,000 rps) on the planned fleet, no shared dependency yet
	$(EXPERIMENTS) full-fleet --seconds $(CHECK_SECONDS)

zone-loss: $(STAMP) ## stop the largest zone and run the projected peak on the survivors
	$(EXPERIMENTS) zone-loss --seconds $(CHECK_SECONDS)

pool-sweep: $(STAMP) ## reintroduce the shared ledger and step its connection pool up until the SLO holds
	$(EXPERIMENTS) pool-sweep --seconds $(CHECK_SECONDS)

dependency-test: $(STAMP) ## freeze the ledger and check requests fail fast instead of hanging
	$(EXPERIMENTS) dependency

rollout-test: $(STAMP) ## roll every instance to v2 and back to v1 under load, one at a time
	$(EXPERIMENTS) rollout

restore-test: $(STAMP) ## back the ledger up, restore into a scratch database, compare row for row
	$(EXPERIMENTS) restore

plan-doc: $(STAMP) ## write docs/capacity-plan.md from the measured evidence
	$(VENV)/bin/capacity report

review: $(STAMP) ## the readiness review: every item PASS, FAIL or ACCEPTED RISK, then the decision (docs/launch-decision.md)
	$(VENV)/bin/capacity review --write
```

- `plan` computes the fleet size from the measured safe load (`capacity plan`) and saves `plan.json`.
- `full-fleet`, `zone-loss`, `pool-sweep` each call `capacity-experiments` with `--seconds $(CHECK_SECONDS)`: run the projected peak (2,000 requests a second) on the instances the plan says; stop the largest zone and run it on the survivors; step the shared ledger's connection pool up until the SLO holds.
- `dependency-test` freezes the ledger and checks requests fail fast; `rollout-test` rolls every instance to v2 and back under load; `restore-test` backs the ledger up, restores it into a scratch database and compares.
- `plan-doc` writes `docs/capacity-plan.md` (and the measured numbers in the runbooks) from the evidence; `review` runs the readiness review and writes `docs/launch-decision.md`.

### Everything else

```make
psql: ## open psql on the ledger database (reads from stdin when piped)
	docker compose exec -T db psql -U $${POSTGRES_USER:-postgres} -d ledger

rules: $(STAMP) ## regenerate the Prometheus SLO rules from slo/slos.yaml
	$(VENV)/bin/capacity-slo rules

rules-check: $(STAMP) ## fail when the committed rules differ from what slo/slos.yaml generates
	$(VENV)/bin/capacity-slo rules --check

lint: $(STAMP) ## ruff check, ruff format check, mypy, import-linter, generated rules in sync
	$(VENV)/bin/ruff check .
	$(VENV)/bin/ruff format --check .
	$(VENV)/bin/mypy
	$(VENV)/bin/lint-imports
	$(MAKE) --no-print-directory rules-check

format: $(STAMP) ## apply ruff's formatting and safe fixes
	$(VENV)/bin/ruff check --fix .
	$(VENV)/bin/ruff format .

test-unit: $(STAMP)
	$(PY) -m pytest tests/unit tests/integration -m 'not measurements' --cov=capacity --cov-report=term
	$(PY) -m coverage report --include='src/capacity/domain/*,src/capacity/application/*' --fail-under=95

# Replays series against the alert rules with promtool (needs Docker).
test-rules: ## promtool: check the rule files and replay series against them
	docker run --rm --entrypoint promtool -v $(CURDIR)/slo:/slo:ro $(PROMTOOL_IMAGE) check rules /slo/generated/rules.yml /slo/generated/rules.demo.yml /slo/alerts/fleet.rules.yml
	docker run --rm --entrypoint promtool -v $(CURDIR)/slo:/slo:ro $(PROMTOOL_IMAGE) test rules /slo/tests/burn_rate.test.yml /slo/tests/fleet.test.yml

test: test-unit test-rules ## unit and integration tests with coverage, then the promtool rule tests

# The measurements are the files `make run` writes; these tests hold the documents and the lesson's Done when lines to them.
test-measurements: $(STAMP) ## the committed measurements against the plan, the alerts, the runbooks and the lesson's Done when lines (after make run)
	$(PY) -m pytest tests/unit/test_evidence.py tests/measurements -m measurements

test-integration: $(STAMP) ## the instance and the ledger client over real sockets, in process
	$(PY) -m pytest tests/integration -m integration

test-e2e: $(STAMP) ## smoke tests of the running fleet (make up first)
	$(PY) -m pytest tests/e2e -m e2e

audit: $(STAMP) ## check the locked dependencies for known vulnerabilities
	$(VENV)/bin/pip-audit --no-deps --disable-pip -r requirements-dev.lock

check: lint test ## lint, types, import rules, tests with coverage, promtool tests

lock: ## regenerate the hash-pinned lock files (needs uv)
	uv pip compile pyproject.toml --generate-hashes --python-version 3.12 --no-header -o requirements.lock
	uv pip compile pyproject.toml --extra dev --generate-hashes --python-version 3.12 --no-header -o requirements-dev.lock

docker-build: ## build the image used by every service
	docker build -t sre-capacity-and-readiness:dev .

down: ## stop the fleet and delete its data
	docker compose down -v

clean: down
	rm -rf $(VENV) logs data .pytest_cache .mypy_cache .ruff_cache .import_linter_cache .coverage
```

`psql` opens the database client; `rules` and `rules-check` regenerate and verify the Prometheus rules; `lint` runs ruff, mypy and import-linter and the rules check; `format` fixes style; `test-unit` runs pytest **excluding** the `measurements` marker and enforces 95% coverage on the logic layers; `test-rules` runs `promtool` in Docker; `test-measurements` runs only the tests that hold the committed measurements to the lesson's Done when lines (they need `make run` first); `test-integration` and `test-e2e` select by marker; `audit`, `check`, `lock`, `docker-build`, `down` and `clean` are housekeeping as in the other projects.

### The targets that make a run trustworthy

```python
preflight: $(STAMP) ## refuse to measure when the machine is busy (one-minute load per CPU above 0.6, or MAX_LOAD)
	$(EXPERIMENTS) preflight

clean-evidence: ## remove the load-dependent evidence, so a run never mixes old and new measurements
	rm -f $(EVIDENCE)/*.json

set-fleet: $(STAMP) ## run exactly the instances the plan says and stop the spares
	$(EXPERIMENTS) set-fleet
```

- `preflight` runs `capacity-experiments preflight`, which reads the machine's one-minute load average and stops the run when it is above 0.6 per CPU (or above `MAX_LOAD` if that variable is set). Measuring on a busy machine produces numbers that describe the machine; refusing at the start costs seconds, finding out at the end costs twenty minutes.
- `clean-evidence` deletes every `docs/evidence/*.json` first, so a run never mixes old and new measurements.
- `set-fleet` runs `capacity-experiments set-fleet`: it starts the instances `plan.json` names and stops the spares, then waits until Envoy counts exactly that many healthy members.

The `run` line now begins with `preflight clean-evidence up reset-fleet`, and `plan set-fleet` sit between `soak` and `full-fleet`.

```python
knee: $(STAMP) ## step the load up on one instance until p99 climbs; names the saturated resource (STEP_SECONDS per step)
	mkdir -p $(EVIDENCE)
	$(LOADTEST) knee --target http://app-a1:8080 --ops http://app-a1:9101 --step-seconds $(STEP_SECONDS) > $(EVIDENCE)/knee.json.tmp \
	  || { rm -f $(EVIDENCE)/knee.json.tmp; exit 1; }
	mv $(EVIDENCE)/knee.json.tmp $(EVIDENCE)/knee.json
```

The knee test writes to `knee.json.tmp` and renames it to `knee.json` only when the command succeeded; when the load generator decides the measurement is not valid (see the interfaces note) it exits with an error and the `||` branch deletes the temporary file. `mv` is atomic on one file system, so a reader never sees half a file. The soak recipe does the same with `soak.json.tmp`, and also passes `--ops http://app-a1:9101` so the soak can read the instance's CPU use.

### Two more targets and a stricter soak

```python
confirm-plan: $(STAMP) ## size the fleet again now that the lost-zone run exists, so the plan records that it was confirmed
	$(VENV)/bin/capacity plan
```

`confirm-plan` runs `capacity plan` a second time, after the lost-zone run exists: the first plan could not know the result, so `verified_by_lost_zone_run` was always false. It sits between `zone-loss` and `pool-sweep` in `run`.

```python
@$(PY) -c "import json,sys; d=json.load(open('$(EVIDENCE)/soak.json')); sys.exit(0 if d['within_slo'] else 'the soak at the safe load left the SLO: the safe load is not safe')"
```

After writing `soak.json`, the soak recipe exits with an error, and so stops `make run`, when the soak left the SLO: a safe load that is not safe makes the rest of the plan meaningless. (`sys.exit` with a string prints it and exits 1.)

## Try it yourself

1. `make help` lists every target with its description, sorted.
2. `make -n run` prints every command `run` would execute, in order, without running any (a long list).
3. `make -n knee` shows the full `docker compose run` command with `10` as the step length; `make -n knee STEP_SECONDS=120` shows `120`.
4. `make install` the first time creates `.venv`; run it again and it prints nothing.
5. `make -n lint` shows the five checks in order.

## Mistakes people make

- Spaces instead of a tab at the start of a recipe line: "missing separator".
- Writing `$VAR` when the shell should see it; in a recipe it must be `$$VAR`.
- Running `make soak` before `make knee`: the soak reads the safe load from `knee.json`.
- Running the long experiments on a busy machine: the numbers then measure the machine. The README explains how to recognise it (the generator reports its own lag).

## Check yourself

1. What does `run` do when it has no recipe?
   *Answer: Make runs its prerequisites in the listed order, so one command runs all the steps.*
2. What does `LOADTEST := docker compose run --rm -T loadtest` give you?
   *Answer: a short name for the command that starts the load generator container for one command and removes it.*
3. Why do `knee` and `soak` redirect output to a file?
   *Answer: the load generator prints a JSON report on standard output; the file becomes the committed evidence.*
4. What does `export STEP_SECONDS ?= 10` mean?
   *Answer: set it to 10 unless it is already set, and pass it to every command Make starts.*
5. Why does `reset-fleet` exist?
   *Answer: to return all nine instances to v1 with no shared dependency before the knee test.*

## Further reading

- GNU Make manual: <https://www.gnu.org/software/make/manual/make.html>
