# Shell scripts (sh/Bash) and shell commands

## What it is

A **shell** is the program that reads command lines you type in a terminal and runs them. `sh` and `bash` are shells. A line such as `ls -l` is a **command** (`ls`) with **arguments** (`-l`). Each command ends with an **exit code**: 0 means success, anything else means failure. A **shell script** is a file of such commands. This repository has one script, `sql/000_roles.sh`, and many shell commands inside the Makefile, the Compose file and the CI workflow.

## Why this repository uses it

The database must have a least-privilege user for the ledger (it may read the accounts table and append to the entries table, and nothing else). PostgreSQL's official Docker image runs any `.sh` or `.sql` file placed in a special folder when it starts the first time. A short script is the right tool for creating that user with a password that comes from the environment, so the password is never written into a file in the repository.

## Where it appears

| File | Shell content |
| --- | --- |
| `sql/000_roles.sh` | The script that creates the `ledger_app` role. |
| `docker-compose.yml` | The `db` health check `pg_isready ...` (run through `CMD-SHELL`), the Prometheus health check `wget -qO- ...`, the Envoy health check (a `bash` one-liner that talks to a TCP port), and the mount that places the script in the image's start-up folder. |
| `Makefile` | Every recipe line is a shell command: `mkdir -p`, `echo`, `grep | sed | sort`, `touch`, `rm -rf`, `command -v`, output redirection `> file`, command substitution `$$(...)`, environment assignments in front of a command, `docker compose exec -T db psql ...`. |
| `Dockerfile` | `RUN useradd ... && mkdir ... && chown ...`. |
| `.github/workflows/ci.yml` | `run:` lines. |

## How it works underneath

- Every command is a **process**. The shell starts it, waits for it to end, and records its exit code in the special variable `$?`.
- Each process has three standard streams: **standard input** (stdin), **standard output** (stdout) and **standard error** (stderr).
- A **pipe** `a | b` connects a's output to b's input. `a && b` runs `b` only if `a` succeeded. `a || b` runs `b` only if `a` failed.
- A **variable** is written `NAME=value` and read as `$NAME`. An **environment variable** is a variable that child processes inherit. Docker passes the `environment:` entries of a service into the container's environment.
- `${NAME:-default}` means "the value of `NAME`, or `default` if it is unset or empty".
- **Quotes**: single quotes keep text exactly as written; double quotes allow `$` substitution.
- A **here-document** (`<<'SQL'` ... `SQL`) feeds several lines of text as standard input to a command. Quoting the marker (`'SQL'`) turns off `$` substitution inside the text.
- The first line `#!/bin/sh` is a **shebang**: it tells the operating system which program runs the file.

## Walkthrough of this repository's use

### The roles script

```bash
#!/bin/sh
# Create the ledger's database role with the least privilege it needs. The password comes from the environment.
set -eu
psql -v ON_ERROR_STOP=1 -v app_password="$LEDGER_DB_PASSWORD" --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
CREATE ROLE ledger_app LOGIN PASSWORD :'app_password' NOSUPERUSER NOCREATEDB NOCREATEROLE;
SQL
```

- **Line 1 `#!/bin/sh`**: run this file with `sh`.
- **Line 2**: a comment, saying what the script does and where the password comes from.
- **`set -eu`**: `set` changes the shell's behaviour. `-e` means "stop at the first command that fails" (instead of carrying on with half the work done). `-u` means "treat an unset variable as an error" (instead of silently using an empty string).
- **`psql ...`**: `psql` is PostgreSQL's command-line client. The flags:
  - `-v ON_ERROR_STOP=1` sets a psql variable so psql stops and exits non-zero at the first SQL error (by default it keeps going).
  - `-v app_password="$LEDGER_DB_PASSWORD"` sets a psql variable named `app_password` to the value of the environment variable `LEDGER_DB_PASSWORD`. Compose passes that variable to the `db` container. The double quotes keep the value in one piece even if it contains spaces.
  - `--username "$POSTGRES_USER"` and `--dbname "$POSTGRES_DB"` choose who connects and to which database. The image sets both from its own configuration.
- **`<<'SQL'`**: everything up to the line that says exactly `SQL` is sent to psql as input. The quotes around `SQL` mean the shell does not touch `$` or backticks in the text.
- **The SQL line**: `CREATE ROLE ledger_app LOGIN PASSWORD :'app_password' NOSUPERUSER NOCREATEDB NOCREATEROLE;`. A **role** is a database user. `LOGIN` lets it connect. `:'app_password'` is psql's way to insert the variable as a correctly quoted SQL string literal, which avoids the shell and SQL quoting traps. The three `NO...` words remove powers the role does not need. The role can do nothing yet; `sql/001_schema.sql` grants it what it needs.
- **Why a `.sh` and not a `.sql`**: a `.sql` file cannot read an environment variable. The script can.

### How the script gets run

```yaml
db:
  image: postgres:18
  cpuset: "9-11"
  # The ledger writes an entry per request, 2,000 times a second at the projected peak. Waiting for a disk flush on
  # every commit stalled requests for 100 to 400 ms on a laptop's virtual disk and hid the pool; the entries are
  # test data, so a commit may return before its flush.
  command: ["postgres", "-c", "synchronous_commit=off"]
  restart: unless-stopped
  environment:
    POSTGRES_DB: ledger
    POSTGRES_USER: ${POSTGRES_USER:-postgres}
    POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-local-dev-only}
    LEDGER_DB_PASSWORD: ${LEDGER_DB_PASSWORD:-local-dev-only-app}
  volumes:
    - ./sql/000_roles.sh:/docker-entrypoint-initdb.d/000_roles.sh:ro
    - ./sql/001_schema.sql:/docker-entrypoint-initdb.d/001_schema.sql:ro
    - ./sql/002_seed.sql:/docker-entrypoint-initdb.d/002_seed.sql:ro
  ports:
    - "127.0.0.1:${DB_PORT:-47232}:5432"
  healthcheck:
    test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER:-postgres} -d ledger"]
    interval: 3s
    timeout: 5s
    retries: 20

```

The three `volumes` lines mount the three files into `/docker-entrypoint-initdb.d/`, read-only (`:ro`). The official PostgreSQL image runs the files in that folder, in alphabetical order, **only the first time the database is created**: `000_roles.sh` (create the role), `001_schema.sql` (create the tables, grant access), `002_seed.sql` (fill them). The health check `pg_isready -U ... -d ledger` asks the server whether it accepts connections; `CMD-SHELL` runs the text through a shell so `${POSTGRES_USER:-postgres}` is expanded.

### The Envoy health check: a tiny HTTP client in Bash

```yaml
healthcheck:
  test: ["CMD", "bash", "-c", "exec 3<>/dev/tcp/127.0.0.1/9901 && printf 'GET /ready HTTP/1.1\\r\\nHost: lb\\r\\nConnection: close\\r\\n\\r\\n' >&3 && grep -q LIVE <&3"]
  interval: 5s
  timeout: 15s
  retries: 40
  start_period: 5s
```

The Envoy image has no `curl` or `wget`, but it has `bash`. The test is a Bash one-liner passed to `bash -c`:

```bash
exec 3<>/dev/tcp/127.0.0.1/9901 && printf 'GET /ready HTTP/1.1\r\nHost: lb\r\nConnection: close\r\n\r\n' >&3 && grep -q LIVE <&3
```

- `exec 3<>/dev/tcp/127.0.0.1/9901` opens a TCP connection to port 9901 on this machine. `/dev/tcp/HOST/PORT` is not a real file: Bash itself treats it as a request to connect. `3<>` means "open file descriptor 3 for reading and writing". A **file descriptor** is a small number a process uses to refer to an open file or connection; 0, 1 and 2 are stdin, stdout and stderr, so 3 is the first free one. `exec` with only redirections keeps the descriptor open for the following commands.
- `printf '...' >&3` writes an HTTP request into that connection. `>&3` means "send standard output to descriptor 3". `\r\n` is carriage return plus line feed, the line ending HTTP requires; the `\\r\\n` in the YAML file is YAML's way to write a backslash (YAML double quotes would otherwise turn `\r` into a character itself).
- `grep -q LIVE <&3` reads Envoy's answer from descriptor 3 (`<&3`: take standard input from 3) and succeeds if it contains `LIVE` (`-q` means print nothing, just set the exit code). Envoy's `/ready` page says `LIVE` when it is ready.
- Each `&&` means the next step runs only if the previous succeeded, so a refused connection ends the check as unhealthy.

### Shell in the Makefile

```make
help: ## list the targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | sed -E 's/:.*## /\t/' | sort
```

A pipeline: `grep` finds the matching lines, `sed` rewrites each, `sort` sorts. `-E` means extended regular expressions.

```make
up: $(STAMP) ## build and start the fleet: 9 instances in 3 zones (the plan says how many run), Envoy, the ledger, PostgreSQL, Prometheus
	docker compose up -d --build --wait
	@echo "edge http://localhost:$${EDGE_PORT:-47280}/work   Envoy admin http://localhost:$${ENVOY_ADMIN_PORT:-47281}   Prometheus http://localhost:$${PROMETHEUS_PORT:-47290}"
```

`docker compose up -d --build --wait` starts the fleet. `$${EDGE_PORT:-47280}` reaches the shell as `${EDGE_PORT:-47280}`: the value of `EDGE_PORT`, or 47280 if unset.

```make
reset-fleet: ## recreate every instance without the shared dependency, at version v1
	LEDGER_URL= APP_VERSION=v1 docker compose up -d --no-deps --force-recreate --wait app-a1 app-a2 app-a3 app-b1 app-b2 app-b3 app-c1 app-c2
```

`LEDGER_URL= APP_VERSION=v1 docker compose up ...` puts two **variable assignments in front of a command**. They apply only to that one command's environment. `LEDGER_URL=` sets the variable to the empty string, which means "no shared dependency" for the instances. The Compose file reads both with `${LEDGER_URL:-}` and `${APP_VERSION:-v1}`.

```make
knee: $(STAMP) ## step the load up on one instance until p99 climbs; names the saturated resource (STEP_SECONDS per step)
	mkdir -p $(EVIDENCE)
	$(LOADTEST) knee --target http://app-a1:8080 --ops http://app-a1:9101 --step-seconds $(STEP_SECONDS) > $(EVIDENCE)/knee.json.tmp \
	  || { rm -f $(EVIDENCE)/knee.json.tmp; exit 1; }
	mv $(EVIDENCE)/knee.json.tmp $(EVIDENCE)/knee.json
	@$(PY) -c "import json; d=json.load(open('$(EVIDENCE)/knee.json')); print(json.dumps({k: d[k] for k in ('knee','safe_rps','margin_below_knee')}, indent=2)); [print(s) for s in d['steps']]"
```

`> $(EVIDENCE)/knee.json` is **output redirection**: the command's standard output is written to that file instead of the screen. The load test prints one JSON document on stdout and the logs on stderr, so the file holds clean JSON. `mkdir -p` first creates the folder. The last line runs a short `python -c "..."` to print a summary of the file.

```make
soak: $(STAMP) ## hold one instance at the safe load for SOAK_SECONDS and check p99 in every window
	$(LOADTEST) soak --target http://app-a1:8080 --rps $$($(PY) -c "import json; print(json.load(open('$(EVIDENCE)/knee.json'))['safe_rps'])") \
	  --seconds $(SOAK_SECONDS) --label soak --ops http://app-a1:9101 > $(EVIDENCE)/soak.json.tmp \
	  || { rm -f $(EVIDENCE)/soak.json.tmp; exit 1; }
	mv $(EVIDENCE)/soak.json.tmp $(EVIDENCE)/soak.json
	@cat $(EVIDENCE)/soak.json
```

`--rps $$($(PY) -c "...")` is **command substitution**: `$(...)` runs a command and puts its output where it stands. In a Makefile each `$` that the shell must see is doubled, hence `$$(`. Here the command reads the safe load from `knee.json`, so the soak runs at the load the knee test chose. A backslash at the end of a line continues the command on the next line. `cat file` prints the file.

```make
psql: ## open psql on the ledger database (reads from stdin when piped)
	docker compose exec -T db psql -U $${POSTGRES_USER:-postgres} -d ledger
```

`docker compose exec -T db psql -U $${POSTGRES_USER:-postgres} -d ledger` runs `psql` inside the running `db` container. `-T` turns off the terminal so input can be piped in.

```make
PYTHON ?= $(shell command -v python3.13 || command -v python3.12 || command -v python3)
```

`command -v python3.13 || command -v python3.12 || command -v python3`: `command -v` prints a program's path if it exists. `||` tries the next on failure.

```make
clean: down
	rm -rf $(VENV) logs data .pytest_cache .mypy_cache .ruff_cache .import_linter_cache .coverage
```

`rm -rf` removes files and folders: `-r` recursively, `-f` without asking and without complaining about missing ones.

### Shell in the Dockerfile

```dockerfile
RUN useradd --system --uid 10001 --create-home app && mkdir -p /app/data /app/logs && chown -R app /app/data /app/logs
```

`useradd --system --uid 10001 --create-home app && mkdir -p /app/data /app/logs && chown -R app /app/data /app/logs`: create a user, create two folders, give the user ownership. `&&` chains the three so the build fails if any step fails.

## Try it yourself

1. `sh -n sql/000_roles.sh && echo syntax ok` checks the script's syntax without running it.
2. `echo ${NOPE:-fallback}` prints `fallback`.
3. `false && echo yes; echo $?` prints `1`.
4. With the fleet up: `echo "SELECT rolsuper, rolcreatedb, rolcreaterole FROM pg_roles WHERE rolname = 'ledger_app';" | make psql` prints `f | f | f`.
5. `docker compose exec lb bash -c "exec 3<>/dev/tcp/127.0.0.1/9901 && printf 'GET /ready HTTP/1.1\r\nHost: lb\r\nConnection: close\r\n\r\n' >&3 && cat <&3"` prints Envoy's answer, including a line `LIVE`.

## Mistakes people make

- A script that carries on after an error. `set -eu` prevents it.
- Putting a password in a script file. This script reads it from the environment.
- Unquoted variables with spaces.
- Forgetting that init scripts run only the first time. To run them again you must delete the volume (`make down`).
- Using `bash` features (like `/dev/tcp`) in a `#!/bin/sh` script. The roles script is plain `sh`; only the Envoy health check explicitly calls `bash`.

## Check yourself

1. What do `-e` and `-u` do in `set -eu`?
   *Answer: `-e` stops at the first failing command; `-u` makes an unset variable an error.*
2. Why does the heredoc marker have quotes (`<<'SQL'`)?
   *Answer: so the shell does not substitute `$` and backticks inside the text.*
3. What does `exec 3<>/dev/tcp/127.0.0.1/9901` do?
   *Answer: opens a TCP connection to that port and keeps it as file descriptor 3 for reading and writing.*
4. What does `$$(...)` do in a Makefile recipe?
   *Answer: Make passes `$(...)` to the shell, which runs the command and substitutes its output.*
5. What does `> file` do?
   *Answer: writes the command's standard output to the file.*

## Further reading

- POSIX shell command language: <https://pubs.opengroup.org/onlinepubs/9699919799/utilities/V3_chap02.html>
- GNU Bash manual (redirections, including `/dev/tcp`): <https://www.gnu.org/software/bash/manual/bash.html#Redirections>
- Initialization scripts of the PostgreSQL image: <https://hub.docker.com/_/postgres> (image tag `postgres:18`).
- psql variables and `-v`: <https://www.postgresql.org/docs/18/app-psql.html>
