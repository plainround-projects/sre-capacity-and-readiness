# Envoy v1.32.3

## What it is

Envoy is a **proxy**: a program that sits between callers and a group of servers, accepts each request, chooses one server and forwards the request to it. Used this way it is also a **load balancer**. Envoy is also very good at measuring: it counts every request and records how long each took, and it publishes those numbers.

Words you will meet:

- A **listener** is a port Envoy accepts connections on.
- A **cluster** is a named group of servers ("upstream" servers) Envoy can send requests to. Each server in it is an **endpoint**.
- A **route** says which cluster a request goes to.
- A **health check** is a request Envoy sends to each endpoint on a schedule to decide if it should receive traffic.
- A **filter** is a processing step in Envoy's pipeline; the **router** filter is the one that forwards.
- Envoy is configured with a YAML file. Everything here is **static**: written in the file, not fetched from a control plane.

## Why this repository uses it

Two reasons. First, the fleet needs a load balancer to spread requests over eight instances and to stop sending requests to an instance that is down (the zone-loss test stops three of them). Second, the lesson says SLIs belong at the edge: Envoy counts every response and times it, so its numbers are what a user experiences, including the effect of retries and of failed health checks. The SLO rules read Envoy's metrics, not the instances'.

## Where it appears

| File | Use |
| --- | --- |
| `deploy/envoy.yaml` | The whole configuration. |
| `docker-compose.yml` | The `lb` service, image `envoyproxy/envoy:v1.32.3`, with the config mounted read-only and a Bash health check. |
| `slo/prometheus.yml` | Scrapes Envoy at `lb:9901`, path `/stats/prometheus`. |
| `slo/generated/rules.yml`, `slo/alerts/fleet.rules.yml` | Read Envoy's `envoy_http_downstream_rq_*` and `envoy_cluster_membership_healthy`. |
| `src/capacity/infrastructure/compose.py` | `healthy_members()` asks Envoy's admin page how many members are healthy. |
| `docs/adr/0002-envoy-as-the-edge.md` | The decision. |

## How it works underneath

1. A caller connects to Envoy's listener on port 8080.
2. Envoy's **HTTP connection manager** parses each HTTP request, finds the route, and asks the **cluster** for an endpoint using the load-balancing policy (here round robin: each healthy endpoint in turn).
3. The router filter forwards the request and streams the answer back. Envoy records the status class (2xx, 5xx) and the time.
4. In the background, every second, Envoy sends a health-check request to each endpoint. An endpoint that fails is removed from rotation; when it passes again it is added back.
5. Envoy resolves host names through DNS. With `STRICT_DNS` it looks them up regularly, which is needed here because Docker gives a recreated container a new address.
6. The **admin interface** (port 9901) shows statistics, cluster state and a `/ready` page, and `/stats/prometheus` prints the statistics in Prometheus format.

## Walkthrough of this repository's use

### The admin port

```yaml
# The edge: every request enters here, so Envoy's own counts and latency histogram are the SLIs. Eight instances in
# three zones, active health checks against each instance's ops port, one retry on a refused connection so a
# restarting instance never reaches a user.
admin:
  address:
    socket_address: {address: 0.0.0.0, port_value: 9901}

```

The comment says what the file is for. `admin.address.socket_address` makes the admin interface listen on `0.0.0.0` (all network interfaces inside the container) port 9901. Compose publishes it on `127.0.0.1:47281` only.

### The listener and the route

```yaml
static_resources:
  listeners:
    - name: ingress
      address:
        socket_address: {address: 0.0.0.0, port_value: 8080}
      filter_chains:
        - filters:
            - name: envoy.filters.network.http_connection_manager
              typed_config:
                "@type": type.googleapis.com/envoy.extensions.filters.network.http_connection_manager.v3.HttpConnectionManager
                stat_prefix: ingress_http
                route_config:
                  name: local
                  virtual_hosts:
                    - name: app
                      domains: ["*"]
                      routes:
                        - match: {prefix: "/"}
                          route:
                            cluster: app
                            timeout: 2s
                            retry_policy:
                              retry_on: connect-failure,refused-stream,reset
                              num_retries: 1
                http_filters:
                  - name: envoy.filters.http.router
                    typed_config:
                      "@type": type.googleapis.com/envoy.extensions.filters.http.router.v3.Router

```

- `static_resources` holds the listeners and clusters written in the file.
- The listener `ingress` listens on port 8080. Its `filter_chains` contain the HTTP connection manager (`envoy.filters.network.http_connection_manager`). The `"@type"` line tells Envoy which kind of configuration object follows; YAML keys that start with `@` must be quoted.
- `stat_prefix: ingress_http` names the statistics; it becomes the label `envoy_http_conn_manager_prefix="ingress_http"` that every SLO query selects on.
- `route_config` has one virtual host that accepts any `domains: ["*"]`, and one route: every path (`prefix: "/"`) goes to `cluster: app`.
- `timeout: 2s` is the route timeout: a request that takes longer than two seconds is answered with 504 by Envoy.
- `retry_policy`: `retry_on: connect-failure,refused-stream,reset` and `num_retries: 1` retry once when the connection could not be made, was refused or reset. This hides a restarting instance from users.
- `http_filters` lists one filter, the **router**, which does the forwarding.

### The cluster

```yaml
clusters:
  - name: app
    # Instances are addressed by name; Docker gives a recreated container a new address, so names are re-resolved
    # every second (STRICT_DNS) and the health checks keep a not-yet-resolved instance out of rotation.
    type: STRICT_DNS
    dns_refresh_rate: 1s
    dns_lookup_family: V4_ONLY
    connect_timeout: 0.5s
    lb_policy: ROUND_ROBIN
    circuit_breakers:
      thresholds:
        - max_connections: 4096
          max_pending_requests: 4096
          max_requests: 4096
    health_checks:
      - timeout: 1s
        interval: 1s
        interval_jitter: 0.2s
        unhealthy_threshold: 1
        healthy_threshold: 1
        http_health_check: {path: /healthz}
```

- `type: STRICT_DNS` with `dns_refresh_rate: 1s` and `dns_lookup_family: V4_ONLY`: look names up again every second and use IPv4 only. The comment explains the reason: a recreated container gets a new address.
- `connect_timeout: 0.5s`: give up connecting after half a second.
- `lb_policy: ROUND_ROBIN`: each healthy endpoint in turn.
- `circuit_breakers` set limits (4096) on connections, pending requests and requests, so Envoy itself never becomes the bottleneck during a load test. A **circuit breaker** is a safety limit that rejects work beyond a threshold.
- `health_checks`: every `1s` (`interval`, plus up to `0.2s` random `interval_jitter` so the checks do not all fire together) with a `timeout` of 1 s, one failure marks an endpoint unhealthy and one success marks it healthy again. `http_health_check: {path: /healthz}` sends `GET /healthz`.

### The endpoints

```yaml
load_assignment:
  cluster_name: app
  endpoints:
    - locality: {zone: a}
      lb_endpoints:
        - endpoint: {address: {socket_address: {address: app-a1, port_value: 8080}}, health_check_config: {port_value: 9101}}
        - endpoint: {address: {socket_address: {address: app-a2, port_value: 8080}}, health_check_config: {port_value: 9101}}
        - endpoint: {address: {socket_address: {address: app-a3, port_value: 8080}}, health_check_config: {port_value: 9101}}
    - locality: {zone: b}
      lb_endpoints:
        - endpoint: {address: {socket_address: {address: app-b1, port_value: 8080}}, health_check_config: {port_value: 9101}}
        - endpoint: {address: {socket_address: {address: app-b2, port_value: 8080}}, health_check_config: {port_value: 9101}}
        - endpoint: {address: {socket_address: {address: app-b3, port_value: 8080}}, health_check_config: {port_value: 9101}}
    - locality: {zone: c}
      lb_endpoints:
        - endpoint: {address: {socket_address: {address: app-c1, port_value: 8080}}, health_check_config: {port_value: 9101}}
        - endpoint: {address: {socket_address: {address: app-c2, port_value: 8080}}, health_check_config: {port_value: 9101}}
```

`load_assignment` lists the endpoints. Each `locality: {zone: a}` groups instances by zone, and each `lb_endpoints` entry gives an address (`app-a1`, a Compose service name) and `port_value: 8080`. `health_check_config: {port_value: 9101}` tells Envoy to send the health check to port 9101 (the instance's ops port), not the traffic port. The eight endpoints are the 3/3/2 fleet.

### How the numbers are read

```python
"""Metric names the generated rules read: the Envoy edge's, and the instances' own."""

from __future__ import annotations

# Envoy's HTTP connection manager is the edge: every request passes through it, so its counts and latency
# histogram are what users experience. Its latency buckets are in milliseconds.
EDGE_PREFIX = 'envoy_http_conn_manager_prefix="ingress_http"'
EDGE_RESPONSES = "envoy_http_downstream_rq_xx"
EDGE_DURATION_BUCKET = "envoy_http_downstream_rq_time_bucket"
EDGE_DURATION_COUNT = "envoy_http_downstream_rq_time_count"
EDGE_LATENCY_BUCKETS_MS: tuple[float, ...] = (
    0.5,
```

Envoy's request-time histogram is in milliseconds (`EDGE_LATENCY_BUCKETS_MS`), unlike the instances' own histogram in seconds. The generated SLO rules use `envoy_http_downstream_rq_xx` (responses by class) and `envoy_http_downstream_rq_time_bucket`. `envoy_cluster_membership_healthy{envoy_cluster_name="app"}` is the number of healthy endpoints, which the fleet alerts watch.

```python
def healthy_members() -> int:
    """Healthy instances according to the load balancer, or 0 when it cannot be reached."""
    try:
        with urllib.request.urlopen(HEALTHY_MEMBERS_URL, timeout=3) as response:
            return int(response.read().decode().strip().rsplit(":", 1)[1])
    except (urllib.error.URLError, TimeoutError, ValueError, IndexError):
        return 0


def wait_for_members(expected: int, timeout_s: float = 90.0) -> None:
    """Block until the load balancer sees exactly `expected` healthy instances."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if healthy_members() == expected:
            return
        time.sleep(0.5)
    raise TimeoutError(f"the load balancer did not reach {expected} healthy instances within {timeout_s:g}s")
```

`healthy_members()` fetches `http://localhost:47281/stats?filter=cluster.app.membership_healthy` and reads the number after the colon. `wait_for_members` polls until Envoy agrees that the expected number of instances is healthy; the zone-loss test waits for 5 before starting the load.

## Try it yourself

1. `make up`, then open <http://localhost:47281> to see Envoy's admin page. `/clusters` lists the eight endpoints and their health.
2. `curl -s localhost:47281/stats?filter=cluster.app.membership_healthy` prints `cluster.app.membership_healthy: 8`.
3. `curl -s localhost:47280/work` prints a small JSON answer and the response headers (`curl -i`) include `x-zone` and `x-instance`; repeat a few times and the instance changes (round robin).
4. `docker compose stop app-a1 app-a2 app-a3`, wait a few seconds, and run step 2 again: it prints `5`. Then `docker compose start app-a1 app-a2 app-a3` restores 8.
5. `curl -s localhost:47281/stats/prometheus | grep envoy_http_downstream_rq_xx | head` shows the counters the SLO rules use.

## Mistakes people make

- Counting health probes as user traffic. Here the check goes to the instance's ops port, so probes never appear in the request counts or the latency numbers of the service port. (The ops page runs in the same process as the service, so an instance that is completely saturated can fail a health check too; the one-second check timeout is a trade-off.)
- Static addresses for containers that get new addresses when recreated. Here `STRICT_DNS` re-resolves every second.
- Retrying requests that are not safe to repeat. Envoy retries only on connection-level failures here.
- Measuring latency only in the instances and missing queueing in front of them.
- Forgetting that the route timeout (2 s) also bounds the worst latency a user can see.

## Check yourself

1. What is a cluster in Envoy?
   *Answer: a named group of upstream servers (endpoints) that Envoy can forward to.*
2. Why does `app` use `STRICT_DNS`?
   *Answer: Docker gives a recreated container a new address, so names must be looked up again regularly.*
3. What does `retry_on: connect-failure,refused-stream,reset` with `num_retries: 1` do?
   *Answer: retries once when the connection failed, was refused or was reset, so a restarting instance does not reach a user.*
4. Where is `envoy_http_conn_manager_prefix="ingress_http"` set?
   *Answer: by `stat_prefix: ingress_http` in the connection manager configuration.*
5. Why does the health check use port 9101?
   *Answer: 9101 is the instance's ops port, where `/healthz` lives; health probes go there so they are not counted as user requests.*

## Further reading

- Envoy v1.32 documentation: <https://www.envoyproxy.io/docs/envoy/v1.32.3/>
- Cluster configuration: <https://www.envoyproxy.io/docs/envoy/v1.32.3/api-v3/config/cluster/v3/cluster.proto>
- HTTP connection manager: <https://www.envoyproxy.io/docs/envoy/v1.32.3/api-v3/extensions/filters/network/http_connection_manager/v3/http_connection_manager.proto>
- Statistics: <https://www.envoyproxy.io/docs/envoy/v1.32.3/configuration/upstream/cluster_manager/cluster_stats>
