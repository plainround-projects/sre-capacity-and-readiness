# Technology notes

This folder teaches every technology used in `sre-capacity-and-readiness`, from zero, using this repository's own files. Each note has the same nine parts: what it is, why the repository uses it, where it appears, how it works underneath, a walkthrough of the repository's own use, things to try, common mistakes, five questions with answers, and further reading.

## The files

| File | Its role in this repository |
| --- | --- |
| [python.md](python.md) | The language: dataclasses, enums, the CPU work, the knee search, the plan formula, `subprocess`, `argparse`. |
| [asyncio.md](asyncio.md) | The open-loop load generator, the instance that does CPU work on the event loop, and the async ledger. |
| [uvloop.md](uvloop.md) | The faster event loop under the instance, the ledger and the load generator. |
| [python-domain-layer.md](python-domain-layer.md) | Every file of `domain/`: CPU work, latency, knee, plan, readiness, telemetry, SLO formulas. |
| [python-application-layer.md](python-application-layer.md) | Every file of `application/`: ports, load tests, SLI counting, alert replay, rule generation, the report and the review. |
| [python-infrastructure-layer.md](python-infrastructure-layer.md) | Every file of `infrastructure/`: settings, logging, metrics, the open-loop generator, Docker Compose, files. |
| [python-interfaces-layer.md](python-interfaces-layer.md) | Every file of `interfaces/`: the instance, the ledger and the command-line programs. |
| [python-tests.md](python-tests.md) | Every test file, with what each test proves. |
| [promql-generated-rules.md](promql-generated-rules.md) | Every line of the two generated rule files, group by group. |
| [aiohttp.md](aiohttp.md) | The instance and the ledger as web services, and the instance's bounded call to the ledger. |
| [http.md](http.md) | HTTP/1.1 byte by byte: the hand-written client, keep-alive, status codes, `curl`. |
| [json.md](json.md) | The evidence files, the report documents and JSON over HTTP. |
| [grafana-dashboard.md](grafana-dashboard.md) | `dashboards/fleet.json`: panels and queries, and how the review reads them. |
| [python-logging.md](python-logging.md) | Structured logfmt log lines. |
| [regex.md](regex.md) | Patterns that read the CPU counter, find metric names in queries and rewrite runbook blocks. |
| [markdown.md](markdown.md) | The generated capacity plan and launch decision, and the runbooks. |
| [prometheus-client.md](prometheus-client.md) | The CPU counter, the instance and ledger metrics, `EXPORTED_METRICS`. |
| [pyyaml.md](pyyaml.md) | Reading the SLO file and the checklist; writing the generated rules. |
| [sql.md](sql.md) | The schema with grants, the deterministic seed, the queries and the restore checksum. |
| [postgresql.md](postgresql.md) | The database server, backup and restore, the connection limit. |
| [psycopg.md](psycopg.md) | The driver and the connection pool that is the second bottleneck. |
| [bash.md](bash.md) | `sql/000_roles.sh`, the Envoy health check in Bash, and the shell in the Makefile. |
| [envoy.md](envoy.md) | The edge load balancer: listener, route, retries, health checks, the admin page. |
| [prometheus.md](prometheus.md) | The monitoring server that scrapes Envoy, the instances and the ledger. |
| [promql-and-alert-rules.md](promql-and-alert-rules.md) | PromQL, the generated burn-rate alerts on Envoy's metrics, and the fleet alert. |
| [promtool.md](promtool.md) | Replay tests for the fleet alert and the burn-rate alerts. |
| [yaml.md](yaml.md) | The format of the SLO file, the checklist, Envoy, Compose and CI files. |
| [pyproject-toml.md](pyproject-toml.md) | `pyproject.toml`: package metadata, the six command-line programs, tool settings. |
| [pip-and-lock-files.md](pip-and-lock-files.md) | Virtual environments, hash-pinned lock files and the vulnerability audit. |
| [make.md](make.md) | The Makefile: every experiment as a target, and `make run` as the whole story. |
| [docker.md](docker.md) | The Dockerfile and `.dockerignore`. |
| [docker-compose.md](docker-compose.md) | The fleet with fixed CPU and memory, Envoy, the ledger, the database, Prometheus and the load generator. |
| [pytest.md](pytest.md) | Tests, fixtures, markers, parametrized tests, and the measurement tests. |
| [ruff.md](ruff.md) | Linting and formatting. |
| [mypy.md](mypy.md) | Static type checking. |
| [import-linter.md](import-linter.md) | Enforcing the four-layer architecture. |
| [github-actions.md](github-actions.md) | The CI workflow. |
| [git.md](git.md) | Version control and the ignore files. |
| [editorconfig.md](editorconfig.md) | Editor formatting rules. |

## Suggested reading order

1. Language and concurrency: `python.md`, `asyncio.md`, `uvloop.md`, then the code itself layer by layer: `python-domain-layer.md`, `python-application-layer.md`, `python-infrastructure-layer.md`, `python-interfaces-layer.md`.
2. Web pieces: `http.md`, `aiohttp.md`, `json.md`, `python-logging.md`, `regex.md`.
3. Data: `sql.md`, `postgresql.md`, `psycopg.md`, `bash.md`.
4. The fleet and its edge: `docker.md`, `docker-compose.md`, `envoy.md`.
5. Measuring and alerting: `prometheus-client.md`, `prometheus.md`, `promql-and-alert-rules.md`, `promql-generated-rules.md`, `promtool.md`, `grafana-dashboard.md`, `yaml.md`, `pyyaml.md`.
6. Build and documents: `make.md`, `pyproject-toml.md`, `pip-and-lock-files.md`, `markdown.md`.
7. Checks: `pytest.md`, `python-tests.md`, `ruff.md`, `mypy.md`, `import-linter.md`, `github-actions.md`.
8. Housekeeping: `git.md`, `editorconfig.md`.

## If you have ten minutes

Read these three: `asyncio.md` (why the load generator is open loop and why the instance burns CPU on the event loop), `envoy.md` (how the edge balances, retries and measures) and `make.md` (what `make run` does, step by step).
