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

.PHONY: help install preflight clean-evidence set-fleet up reset-fleet run knee soak plan plan-doc full-fleet zone-loss pool-sweep dependency-test rollout-test restore-test review test-measurements \
        psql rules rules-check lint format test test-unit test-rules test-integration test-e2e audit check lock docker-build down clean

help: ## list the targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | sed -E 's/:.*## /\t/' | sort

$(STAMP): requirements-dev.lock pyproject.toml
	$(PYTHON) -m venv $(VENV)
	$(VENV)/bin/pip install -q --require-hashes -r requirements-dev.lock
	$(VENV)/bin/pip install -q --no-deps -e .
	touch $@

install: $(STAMP) ## create .venv from the lock file

up: $(STAMP) ## build and start the fleet: 8 instances in 3 zones, Envoy, the ledger, PostgreSQL, Prometheus
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
run: preflight clean-evidence up reset-fleet knee soak plan set-fleet full-fleet zone-loss pool-sweep dependency-test rollout-test restore-test plan-doc review test-measurements ## the whole capacity and readiness exercise (about 20 minutes on a quiet machine)

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

plan: $(STAMP) ## size the fleet from the measured safe load: the formula, the inputs, the result
	$(VENV)/bin/capacity plan

full-fleet: $(STAMP) ## run the projected peak (2,000 rps) on all 8 instances, no shared dependency yet
	$(EXPERIMENTS) full-fleet --seconds $(CHECK_SECONDS)

zone-loss: $(STAMP) ## stop the largest zone and run the projected peak on the 5 survivors
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
