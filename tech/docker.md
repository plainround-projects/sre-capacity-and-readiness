# Docker

## What it is

Docker is a tool that runs programs inside **containers**. A container is a normal process on your computer, but the operating system gives it its own private view: its own file system, its own network address and its own list of processes. It cannot see your files unless you share them in on purpose.

A container starts from an **image**. An image is a read-only bundle: an operating system's files, plus the language runtime, plus your program, frozen at one moment. You describe how to build an image in a text file called a **Dockerfile**. `docker build` reads the Dockerfile and produces the image.

## Why this repository uses it

The capacity exercise needs a fleet: nine identical service instances, a shared ledger, a load balancer, a database, Prometheus and a load generator. Containers make each instance a separate, resource-limited box with a fixed share of one CPU core, which is what makes "the capacity of one instance" a stable number to measure. One Dockerfile builds the image used by the instances, the ledger and the load generator. Envoy, PostgreSQL and Prometheus use ready-made images.

## Where it appears

| File | What it does with Docker |
| --- | --- |
| `Dockerfile` | Builds the one image `sre-capacity-and-readiness:dev` that runs `capacity-instance`, `capacity-ledger` and `capacity-loadtest`. |
| `.dockerignore` | Lists what must not be sent to Docker when building. |
| `docker-compose.yml` | Starts the containers (see the Docker Compose note). |
| `Makefile` | `docker-build`, `up`, the load-test targets (`docker compose run`), `psql`, `test-rules` and `down` call `docker`. |
| `.github/workflows/ci.yml` | The `rules` and `e2e` jobs use Docker on the CI machine. |

## How it works underneath

- Docker has a background program, the **daemon**, that does the real work. The `docker` command you type is a **client** that sends requests to the daemon over a local socket.
- An image is made of **layers**. Each instruction in the Dockerfile that changes files adds one layer. Docker **caches** layers: if an instruction and everything before it are unchanged, Docker reuses the stored layer instead of running it again. That is why the order of instructions matters.
- A **tag** such as `python:3.12.8-slim-bookworm` names an image. A **digest** (`@sha256:...`) is a checksum of the exact image content. A tag can later point at a different image; a digest cannot.
- A container is an image plus a thin writable layer plus a running process. When the container is removed, the writable layer is gone. Data you want to keep goes in a **volume** (a folder Docker manages) or a **bind mount** (a folder from your computer).
- A container's process runs as a **user**. Unless you say otherwise it is `root`, the all-powerful user inside the container. Good practice is to switch to an ordinary user.

## Walkthrough of this repository's use

### The Dockerfile

```dockerfile
# One image for every process in the stack: capacity-instance, capacity-ledger and capacity-loadtest are all entry points.
FROM python:3.12.8-slim-bookworm@sha256:2199a62885a12290dc9c5be3ca0681d367576ab7bf037da120e564723292a2f0

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.lock ./
RUN pip install --require-hashes -r requirements.lock

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-deps .

RUN useradd --system --uid 10001 --create-home app && mkdir -p /app/data /app/logs && chown -R app /app/data /app/logs
USER app
EXPOSE 8080 8081 9100 9101
CMD ["capacity-instance"]
```

Line by line:

- **Line 1** is a comment. It says one image serves three programs: `capacity-instance`, `capacity-ledger` and `capacity-loadtest`. Each program is just a different command run from the same image.
- **`FROM python:3.12.8-slim-bookworm@sha256:...`** chooses the starting image. `python` is the official image, `3.12.8` the exact Python version, `slim` means a smaller image without compilers and extra tools, and `bookworm` is the name of the Debian Linux release it is built on. The `@sha256:...` pins the exact bytes, so the build does not change if the tag is later moved.
- **`ENV ...`** sets environment variables inside the image. An **environment variable** is a named text value that every process can read. `PYTHONDONTWRITEBYTECODE=1` stops Python from writing `.pyc` cache files (useless in a throwaway container). `PYTHONUNBUFFERED=1` makes Python print immediately instead of waiting to fill a buffer, so `docker logs` shows lines at once. `PIP_NO_CACHE_DIR=1` stops pip from keeping downloaded files (smaller image). `PIP_DISABLE_PIP_VERSION_CHECK=1` stops pip contacting the internet to ask whether it is out of date. The trailing `\` joins the lines into one instruction, which makes one layer.
- **`WORKDIR /app`** sets the folder where the following instructions run and where the container starts. Docker creates it if it does not exist.
- **`COPY requirements.lock ./`** copies one file from your computer into `/app`. It is copied alone, before the code, on purpose.
- **`RUN pip install --require-hashes -r requirements.lock`** installs the third-party packages. `--require-hashes` refuses any package whose checksum is not in the lock file. Because this layer depends only on `requirements.lock`, Docker reuses it from cache on every build where the lock file did not change, even if the source code changed.
- **`COPY pyproject.toml README.md ./`**, **`COPY src ./src`**, **`RUN pip install --no-deps .`** copy the project's own files and install the project itself. `--no-deps` is safe here because all dependencies are already installed from the lock file. The installation creates the commands `capacity-instance`, `capacity-ledger`, `capacity-loadtest` (and the three tool commands; see the pyproject note). This image does not need the `sql/` folder: PostgreSQL gets its SQL files through the `db` service (see the Docker Compose note).
- **`RUN useradd --system --uid 10001 --create-home app && mkdir -p /app/data /app/logs && chown -R app /app/data /app/logs`** creates an ordinary user called `app` with the numeric id 10001, creates the two folders the program writes to, and gives that user ownership of them. `&&` chains commands so the instruction fails if any step fails.
- **`USER app`** makes every later instruction and the running container use that user instead of `root`. If someone breaks into the program, they hold an ordinary user's rights, not root's.
- **`EXPOSE 8080 8081 9100 9101`** is documentation: it lists the ports the programs listen on (the ports differ per program: an instance serves 8080 and its ops page on 9101, the ledger 8082 and 9102). It does not open anything by itself.
- **`CMD ["capacity-instance"]`** is the default command (an instance). Compose sets `command:` for the ledger and an `entrypoint:` for the load generator.

### The .dockerignore file

```text
.git
.venv
logs
data
tests
docs
**/__pycache__
.mypy_cache
.ruff_cache
.pytest_cache
.import_linter_cache
.coverage
.env
```

When you run `docker build .`, Docker first sends the whole folder (the **build context**) to the daemon. This file lists names to leave out: the git history, the local virtual environment, logs and data, tests and docs (not needed at run time), Python cache folders, tool caches and the `.env` file (which may hold a local token and must never be baked into an image). `**/__pycache__` means a folder of that name at any depth. A smaller context builds faster and cannot leak files.

### Docker in the Makefile

```make
# Replays series against the alert rules with promtool (needs Docker).
test-rules: ## promtool: check the rule files and replay series against them
	docker run --rm --entrypoint promtool -v $(CURDIR)/slo:/slo:ro $(PROMTOOL_IMAGE) check rules /slo/generated/rules.yml /slo/generated/rules.demo.yml /slo/alerts/fleet.rules.yml
	docker run --rm --entrypoint promtool -v $(CURDIR)/slo:/slo:ro $(PROMTOOL_IMAGE) test rules /slo/tests/burn_rate.test.yml /slo/tests/fleet.test.yml
```

`docker run --rm --entrypoint promtool -v $(CURDIR)/slo:/slo:ro prom/prometheus:v3.5.0 check rules ...` runs one command in a throwaway container. `--rm` deletes the container when it ends. `--entrypoint promtool` replaces the image's normal start program (the Prometheus server) with the `promtool` checker. `-v host:container:ro` shares a folder in read-only. Everything after the image name is passed to `promtool`.

## Try it yourself

1. `docker build -t sre-capacity-and-readiness:dev .` builds the image. The first time it downloads the base image; the second time every step prints `CACHED`.
2. `docker run --rm sre-capacity-and-readiness:dev id` prints something like `uid=10001(app) gid=10001(app)`. That proves the container does not run as root.
3. `docker run --rm sre-capacity-and-readiness:dev python --version` prints `Python 3.12.8`.
4. `docker image ls sre-capacity-and-readiness` shows the image and its size.
5. `docker history sre-capacity-and-readiness:dev` lists the layers, one per instruction. You can see the `pip install` layers.

## Mistakes people make

- Copying the whole project before installing dependencies, so every code change reinstalls everything. The Dockerfile copies the lock file first.
- Running as root. Here `USER app` switches.
- Baking secrets into the image. `.dockerignore` excludes `.env`, and the token arrives only as an environment variable at run time.
- Using a floating tag such as `python:3.12`. The digest pin here makes builds repeatable.
- Expecting `EXPOSE` to publish a port. Publishing is done with `ports:` in Compose.

## Check yourself

1. What is the difference between an image and a container?
   *Answer: an image is the read-only bundle; a container is a running (or stopped) instance made from it with a writable layer on top.*
2. Why does the Dockerfile copy `requirements.lock` before `src`?
   *Answer: so the slow dependency install layer is cached and only rebuilds when the lock file changes.*
3. What does the `@sha256:` part of the `FROM` line add?
   *Answer: it pins the exact image content, so a moved tag cannot change the build.*
4. Why does the image create a user with `useradd`?
   *Answer: so the programs do not run as root.*
5. What does `.dockerignore` protect?
   *Answer: the build context stays small, and local files such as `.env` and `.git` never enter the image.*

## Further reading

- Dockerfile reference: <https://docs.docker.com/reference/dockerfile/>
- Docker build best practices: <https://docs.docker.com/build/building/best-practices/>
- The pinned base image: <https://hub.docker.com/_/python> (tag `3.12.8-slim-bookworm`).
- `prom/prometheus:v3.5.0`: <https://hub.docker.com/r/prom/prometheus>
