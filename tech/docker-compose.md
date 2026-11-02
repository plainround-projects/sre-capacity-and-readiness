# Docker Compose

## What it is

Docker Compose is a tool, built into the `docker` command as `docker compose`, that starts several containers together from one YAML file. The file lists the **services** (one per container), how they connect, which ports they publish, how much CPU and memory each may use, and in what order they start.

## Why this repository uses it

A capacity test needs a stable thing to measure. Compose gives each of the nine service instances a fixed **1.0 CPU** and **256 MiB** of memory, so "the capacity of one instance" is a number that does not depend on what else runs on the machine. Compose also wires the whole fleet: Envoy in front, a shared ledger and PostgreSQL behind, Prometheus watching, and a load generator that runs inside the same network so a laptop's port forwarding is not part of the measurement. The experiments also use Compose commands to stop a zone, freeze the ledger and replace instances one by one.

## Where it appears

| File | What it does with Compose |
| --- | --- |
| `docker-compose.yml` | Defines `app-a1` ... `app-c3` (nine instances, of which the plan says how many run), `lb` (Envoy), `db`, `ledger`, `prometheus` and `loadtest` (a tool service). |
| `Makefile` | `up`, `reset-fleet`, `LOADTEST := docker compose run --rm -T loadtest`, `psql`, `down`. |
| `src/capacity/infrastructure/compose.py` | Python wrapper that runs `docker compose` for the experiments: recreate, stop, start, pause, unpause, exec, run. |
| `.env.example`, `.env` | Provide values for the `${NAME:-default}` placeholders. |
| `.github/workflows/ci.yml` | The `e2e` job runs `make up`, the smoke tests and `make down`. |

## How it works underneath

- Compose reads the file, fills `${NAME:-default}` from the shell or `.env`, and asks the Docker daemon to create what is described.
- Compose creates a private **network** for the project. Each service gets a DNS name equal to its service name (`app-a1`, `lb`, `ledger`, `db`). **Published** ports (`ports:`) are an extra door from your computer into one container.
- `cpus: 1.0` limits a container to the equivalent of one CPU core, enforced by the Linux kernel: a process that wants more is slowed down (**throttled**). `mem_limit: 256m` kills a container that exceeds 256 MiB. `cpu_shares: 4096` is a *relative weight* when CPUs are contended (the default is 1024): this container gets four times the default share.
- A **profile** (`profiles: ["tools"]`) hides a service unless asked for; `docker compose run --rm loadtest ...` starts it for one command and removes it afterwards.
- `docker compose up -d --no-deps --force-recreate --wait app-a1` replaces one container with a fresh one (stopping the old one gracefully) and waits until it is healthy.
- `docker compose stop` / `start` stop and start existing containers; `pause` freezes a container's processes without closing its connections (the nastiest dependency failure: it accepts connections and never answers).

## Walkthrough of this repository's use

### The shared instance block

```yaml
# The fleet: up to 9 instances in 3 zones (3/3/3), of which the capacity plan says how many run, behind an Envoy load balancer, a shared ledger with a bounded pool, and
# PostgreSQL behind it. Every instance is a container with a fixed 1.0 CPU and 256 MiB limit, so one instance's
# capacity is a stable number. Prometheus scrapes Envoy (the edge) and every instance.
name: sre-capacity-and-readiness

x-instance: &instance
  build: .
  image: sre-capacity-and-readiness:dev
  command: ["capacity-instance"]
  restart: unless-stopped
  read_only: true
  security_opt: ["no-new-privileges:true"]
  cap_drop: ["ALL"]
  tmpfs: ["/tmp"]
  cpus: 1.0
  mem_limit: 256m
  stop_grace_period: 15s
  healthcheck:
    test: ["CMD", "python", "-c", "import urllib.request as u; u.urlopen('http://localhost:9101/healthz', timeout=2)"]
    interval: 5s
    timeout: 15s
    retries: 40
    start_period: 10s

x-instance-env: &instance-env
  WORK_ROUNDS: ${WORK_ROUNDS:-6000}
  # Empty means no shared dependency: the first tests find the CPU knee on its own. `make run` sets it for the pool tests.
  LEDGER_URL: ${LEDGER_URL:-}
  LEDGER_TIMEOUT_S: ${LEDGER_TIMEOUT_S:-0.5}
  VERSION: ${APP_VERSION:-v1}
  LOG_LEVEL: ${LOG_LEVEL:-INFO}
```

- The leading comment states the design: up to nine instances in three zones (3/3/3), of which the capacity plan says how many run, Envoy in front, a shared ledger with a bounded pool, PostgreSQL, fixed CPU and memory limits so capacity is a stable number.
- `x-instance: &instance` is an **extension field** (ignored by Compose; a place to write settings once) with a YAML **anchor** `&instance`. `<<: *instance` in each service merges it in (see the YAML note).
- `build: .` and `image: sre-capacity-and-readiness:dev` build the one image; `command: ["capacity-instance"]` starts an instance.
- `restart: unless-stopped`, `read_only: true`, `security_opt: ["no-new-privileges:true"]`, `cap_drop: ["ALL"]` and `tmpfs: ["/tmp"]` are the hardening settings explained in the Docker notes of this track: restart after a crash, a read-only root file system with a memory-backed `/tmp`, no privilege gain, no Linux capabilities.
- `cpus: 1.0` and `mem_limit: 256m`: the fixed budget per instance.
- `stop_grace_period: 15s`: when told to stop, give the process up to 15 seconds to finish in-flight requests before it is killed. The rollout test relies on that.
- `healthcheck` runs a Python one-liner that opens `http://localhost:9101/healthz` and fails if it cannot (`interval: 5s`, `timeout: 15s`, `retries: 40`, `start_period: 10s`; generous because the build machine was often busy).
- `x-instance-env: &instance-env` holds the environment shared by every instance: `WORK_ROUNDS` (how many SHA-256 rounds each request burns, default 3000), `LEDGER_URL` (empty means no shared dependency), `LEDGER_TIMEOUT_S`, `VERSION` (`${APP_VERSION:-v1}`, how the rollout test tells releases apart) and `LOG_LEVEL`.

### The nine instances

```yaml
services:
  app-a1:
    <<: *instance
    environment:
      <<: *instance-env
      ZONE: a
      INSTANCE: a1
    ports:
      - "127.0.0.1:${APP_A1_PORT:-47282}:8080"
      - "127.0.0.1:${APP_A1_OPS_PORT:-47283}:9101"
  app-a2:
    <<: *instance
    environment:
      <<: *instance-env
      ZONE: a
      INSTANCE: a2
  app-a3:
    <<: *instance
    environment:
      <<: *instance-env
      ZONE: a
      INSTANCE: a3
```

Each instance merges the block and then sets `ZONE` and `INSTANCE` (`a`, `a1`...). Only `app-a1` publishes ports (`47282` for traffic, `47283` for its ops page) because the knee test is aimed at that one instance. The others are reached only through Envoy. The same pattern repeats for `app-a3`, `app-b1` to `app-b3`, `app-c1` and `app-c2`: three, three and two.

### Envoy

```yaml
lb:
  image: envoyproxy/envoy:v1.32.3
  restart: unless-stopped
  command: ["-c", "/etc/envoy/envoy.yaml", "--log-level", "warn"]
  volumes:
    - ./deploy/envoy.yaml:/etc/envoy/envoy.yaml:ro
  ports:
    - "127.0.0.1:${EDGE_PORT:-47280}:8080"
    - "127.0.0.1:${ENVOY_ADMIN_PORT:-47281}:9901"
  depends_on:
    app-a1:
      condition: service_healthy
    app-a2:
      condition: service_healthy
    app-a3:
      condition: service_healthy
    app-b1:
      condition: service_healthy
    app-b2:
      condition: service_healthy
    app-b3:
      condition: service_healthy
    app-c1:
      condition: service_healthy
    app-c2:
      condition: service_healthy
    app-c3:
      condition: service_healthy
  healthcheck:
    test: ["CMD", "bash", "-c", "exec 3<>/dev/tcp/127.0.0.1/9901 && printf 'GET /ready HTTP/1.1\\r\\nHost: lb\\r\\nConnection: close\\r\\n\\r\\n' >&3 && grep -q LIVE <&3"]
    interval: 5s
    timeout: 15s
    retries: 40
    start_period: 5s
```

`image: envoyproxy/envoy:v1.32.3`; `command: ["-c", "/etc/envoy/envoy.yaml", "--log-level", "warn"]` passes Envoy's arguments; the config is a read-only bind mount; the edge (`8080`) and admin (`9901`) ports are published on `127.0.0.1` only. `depends_on` lists all eight instances with `condition: service_healthy`: Envoy starts last. The health check is a Bash one-liner (see the Bash note).

### PostgreSQL and the ledger

```yaml
db:
  image: postgres:18
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

ledger:
  build: .
  image: sre-capacity-and-readiness:dev
  command: ["capacity-ledger"]
  restart: unless-stopped
  read_only: true
  security_opt: ["no-new-privileges:true"]
  cap_drop: ["ALL"]
  tmpfs: ["/tmp"]
  cpus: 2.0
  mem_limit: 256m
  depends_on:
    db:
      condition: service_healthy
  environment:
    DATABASE_URL: postgresql://ledger_app:${LEDGER_DB_PASSWORD:-local-dev-only-app}@db:5432/ledger
    POOL_SIZE: ${POOL_SIZE:-20}
    POOL_TIMEOUT_S: ${POOL_TIMEOUT_S:-0.4}
    HOLD_MS: ${HOLD_MS:-10}
    LOG_LEVEL: ${LOG_LEVEL:-INFO}
  ports:
    - "127.0.0.1:${LEDGER_OPS_PORT:-47284}:9102"
  healthcheck:
    test: ["CMD", "python", "-c", "import urllib.request as u; u.urlopen('http://localhost:9102/readyz', timeout=2)"]
    interval: 5s
    timeout: 15s
    retries: 40
    start_period: 10s
```

`db` is `postgres:18` with the three init files mounted (see the Bash and SQL notes) and a health check `pg_isready`. `ledger` is built from the same image with `command: ["capacity-ledger"]`, `cpus: 2.0` (the ledger must not be the CPU bottleneck), `depends_on` the database being healthy, and environment: `DATABASE_URL` with the limited `ledger_app` role, `POOL_SIZE` (20 by default: the number of connections it may open), `POOL_TIMEOUT_S` (0.4 s: how long a request waits for a free connection), `HOLD_MS` (10 ms: how long each request holds its connection, standing in for a real query). The lesson's second bottleneck is this pool.

### Prometheus

```yaml
prometheus:
  image: prom/prometheus:v3.5.0
  restart: unless-stopped
  cpus: 0.5
  command:
    - --config.file=/etc/prometheus/prometheus.yml
    - --storage.tsdb.retention.time=1d
  ports:
    - "127.0.0.1:${PROMETHEUS_PORT:-47290}:9090"
  volumes:
    - ./slo/prometheus.yml:/etc/prometheus/prometheus.yml:ro
    - ./slo/generated:/etc/prometheus/rules/generated:ro
    - ./slo/alerts:/etc/prometheus/rules/alerts:ro
  depends_on:
    lb:
      condition: service_healthy
  healthcheck:
    test: ["CMD", "wget", "-qO-", "http://localhost:9090/-/ready"]
    interval: 5s
    timeout: 15s
    retries: 40
```

`cpus: 0.5` caps Prometheus so it cannot compete with the load tests for CPU. The two command flags, the three read-only mounts (config, generated rules, hand-written alerts), and a `wget` health check follow the same pattern as the other projects. It starts after Envoy is healthy.

### The load generator

```yaml
# The load generator runs inside the compose network, so a laptop's port forwarding is not in the measurement.
# Run it with `docker compose run --rm -T loadtest ...` (the Makefile does).
loadtest:
  build: .
  image: sre-capacity-and-readiness:dev
  profiles: ["tools"]
  entrypoint: ["capacity-loadtest"]
  # The generator must never be the slow party: give it the largest CPU share so its own lag stays out of the numbers.
  cpu_shares: 4096
  read_only: true
  security_opt: ["no-new-privileges:true"]
  cap_drop: ["ALL"]
  tmpfs: ["/tmp"]

```

`profiles: ["tools"]` keeps it out of `docker compose up`. `entrypoint: ["capacity-loadtest"]` makes the container run the load test program directly, with the arguments you give after the service name. `cpu_shares: 4096` gives it the largest CPU weight so the generator is never the slow party. It is started with `docker compose run --rm -T loadtest knee ...` (the Makefile's `LOADTEST` variable): `run` starts a one-off container, `--rm` removes it afterwards, `-T` turns off the terminal so the JSON report can be redirected to a file.

### Driving Compose from Python

```python
class Compose:
    """Thin wrapper over the `docker compose` command for this project."""

    def __init__(self, project_dir: Path) -> None:
        """`project_dir` holds docker-compose.yml."""
        self._dir = project_dir

    def run(
        self, *args: str, env: dict[str, str] | None = None, stdin: bytes | None = None, timeout: float = 600
    ) -> bytes:
        """Run `docker compose <args>` and return standard output; raise ComposeError on a non-zero exit.

        A busy Docker daemon sometimes answers with a transient error ("Bad Gateway", a reset connection); those are
        retried, anything else fails at once.
        """
        for attempt in range(1, _ATTEMPTS + 1):
            result = subprocess.run(
                ["docker", "compose", *args],
                cwd=self._dir,
                env={**os.environ, **(env or {})},
                input=stdin,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
            if result.returncode == 0:
                return result.stdout
            stderr = result.stderr.decode(errors="replace")
            if attempt == _ATTEMPTS or not any(marker in stderr for marker in _TRANSIENT):
                raise ComposeError(f"docker compose {' '.join(args)} failed: {stderr[-800:]}")
            time.sleep(attempt * _RETRY_PAUSE_SECONDS)
        raise AssertionError("unreachable")  # pragma: no cover

    def recreate(self, services: Sequence[str], env: dict[str, str] | None = None) -> None:
        """Recreate the services (stop the old container gracefully, start a new one) and wait until healthy."""
        self.run("up", "-d", "--no-deps", "--force-recreate", "--wait", *services, env=env)

    def stop(self, services: Sequence[str]) -> None:
        """Stop the services."""
        self.run("stop", *services)

    def start(self, services: Sequence[str]) -> None:
        """Start stopped services and wait until healthy."""
        self.run("up", "-d", "--no-deps", "--wait", *services)

    def pause(self, service: str) -> None:
        """Freeze a service: it still accepts connections but never answers (the worst dependency failure)."""
        self.run("pause", service)

    def unpause(self, service: str) -> None:
        """Resume a paused service."""
        self.run("unpause", service)

    def db(self, *args: str, stdin: bytes | None = None) -> bytes:
        """Run a command inside the database container as the superuser."""
        return self.run("exec", "-T", "db", *args, stdin=stdin)

    def loadtest(self, *args: str, timeout: float = 1800) -> dict[str, Any]:
        """Run the load generator inside the compose network and parse its JSON report."""
        output = self.run("run", "--rm", "-T", "loadtest", *args, timeout=timeout)
        document: dict[str, Any] = json.loads(output)
        return document

    def start_loadtest(self, *args: str) -> subprocess.Popen[bytes]:
        """Start the load generator in the background; read its report with `communicate()`."""
        return subprocess.Popen(
            ["docker", "compose", "run", "--rm", "-T", "loadtest", *args],
            cwd=self._dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

```

`Compose.run` calls `subprocess.run(["docker", "compose", *args], ...)` and retries a few times when Docker's daemon answers with a transient error ("Bad Gateway", a reset connection), which happens when the machine is overloaded; any other failure raises `ComposeError` at once. `recreate`, `stop`, `start`, `pause`, `unpause`, `db` and `loadtest` are named wrappers for the Compose commands the experiments need. `start_loadtest` uses `subprocess.Popen` to run the generator in the background while the experiment rolls instances.

### The remaining instances: zones b and c

Zone A's instances `app-a2` and `app-a3` are written exactly like `app-a1` without the published host ports. The other six instances (`app-b1` to `app-b3` and `app-c1` to `app-c3`) repeat the same pattern:

```yaml
app-b1:
  <<: *instance
  environment:
    <<: *instance-env
    ZONE: b
    INSTANCE: b1
app-b2:
  <<: *instance
  environment:
    <<: *instance-env
    ZONE: b
    INSTANCE: b2
app-b3:
  <<: *instance
  environment:
    <<: *instance-env
    ZONE: b
    INSTANCE: b3
app-c1:
  <<: *instance
  environment:
    <<: *instance-env
    ZONE: c
    INSTANCE: c1
app-c2:
  <<: *instance
  environment:
    <<: *instance-env
    ZONE: c
    INSTANCE: c2
app-c3:
  <<: *instance
  environment:
    <<: *instance-env
    ZONE: c
    INSTANCE: c3
```

`<<: *instance` copies the shared service definition (the anchor `instance`) and `<<: *instance-env` copies the shared environment, then the two lines `ZONE` and `INSTANCE` say which zone and which instance this container is. That makes nine instances in all, three in each zone. The capacity plan decides how many of them run (`make set-fleet` stops the spares): for example eight as 3/3/2, where losing zone a removes three and leaves the five that carry the projected peak.

## Try it yourself

1. `docker compose config --services` lists the services (the `loadtest` one only with `--profile tools`).
2. `make up`, then `docker compose ps` lists `lb`, `db`, `ledger`, `prometheus` and nine `app-*` containers as healthy.
3. `docker stats --no-stream` shows each instance with a CPU percentage and the `256MiB` memory limit.
4. `docker compose stop app-a1 app-a2 app-a3` stops zone a; `curl -s localhost:47281/stats?filter=cluster.app.membership_healthy` prints `cluster.app.membership_healthy: 6` (nine minus three). Then `docker compose start app-a1 app-a2 app-a3`.
5. `make down` removes the containers and the database data.

## Mistakes people make

- Measuring capacity on containers with no CPU limit: the number then depends on whatever else is running.
- Running the load generator on the same limited CPU as the service. Here it has a large share and runs inside the network.
- Publishing every instance's port. Only `app-a1` is published, for the knee test.
- Forgetting `--wait` or health checks, then testing a service that is not ready.
- Expecting `docker compose stop` to be instant: `stop_grace_period` is 15 s.

## Check yourself

1. What does `cpus: 1.0` do?
   *Answer: limits the container to one CPU core's worth of time; it is throttled beyond that.*
2. What does a profile do?
   *Answer: hides a service unless you name the profile or run the service explicitly.*
3. Why does the load generator run in the Compose network?
   *Answer: so published-port forwarding on the host is not part of the measurement.*
4. What does `depends_on` with `condition: service_healthy` do for Envoy?
   *Answer: Envoy starts only after all nine instances pass their health checks.*
5. What does `docker compose pause` do?
   *Answer: freezes the container's processes; it still holds its connections but never answers.*

## Further reading

- Compose file reference: <https://docs.docker.com/reference/compose-file/>
- Resource constraints (`cpus`, `mem_limit`, `cpu_shares`): <https://docs.docker.com/engine/containers/resource_constraints/>
- `docker compose run`: <https://docs.docker.com/reference/cli/docker/compose/run/>
- `docker compose pause`: <https://docs.docker.com/reference/cli/docker/compose/pause/>
