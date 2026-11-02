# HTTP and curl

## What it is

HTTP is the language web programs use to talk. A **client** sends a **request**; a **server** sends back a **response**.

A request has a **method** (what to do: `GET` to read, `POST` to create, `PUT` to replace, `DELETE` to remove), a **path** (`/work`), optional **headers** (extra `Name: value` lines) and optionally a **body**. A response has a **status code**, headers and a body.

**Status codes** are three digits in families:

- 2xx success (`200 OK`).
- 4xx the client made a mistake (`400 Bad Request`, `404 Not Found`).
- 5xx the server failed (`500`, `502 Bad Gateway`, `503 Service Unavailable`, `504 Gateway Timeout`).

`curl` is a command-line tool that sends HTTP requests and prints the answer.

## Why this repository uses it

Capacity is about requests: how many per second the service answers, and how fast. The load generator speaks HTTP/1.1 itself, byte by byte, so you can see exactly what a request is. The SLOs are defined on HTTP outcomes at Envoy: any answer that is not 5xx is good, and 99% must finish within 50 ms.

## Where it appears

| File | Use |
| --- | --- |
| `infrastructure/openloop.py` | Writes `GET /work HTTP/1.1` by hand and parses the response status, headers and body; reads `/metrics` with HTTP/1.0. |
| `interfaces/instance_app.py` | Returns 200 or a fast 503; sets `X-Zone`, `X-Instance`, `X-Version`. |
| `interfaces/ledger_app.py` | 200, 400, 503 (pool exhausted) and 500. |
| `deploy/envoy.yaml` | Routing, a 2-second route timeout, retries and HTTP health checks. |
| `src/capacity/infrastructure/compose.py` | Polls Envoy's admin `/stats` page and counts the versions answering. |
| `slo/slos.yaml` | "Good" is any response that is not a 5xx. |

## How it works underneath

- HTTP/1.1 runs over **TCP**. A client connects to an address and a **port**, sends text lines, and reads text lines back.
- A request is: a **request line** (`GET /work HTTP/1.1`), **headers** (`Name: value`, one per line), a **blank line**, and an optional body. Every line ends with `\r\n`.
- A response is: a **status line** (`HTTP/1.1 200 OK`), headers, a blank line, and the body. The header `Content-Length: N` says how many bytes of body follow, so the client knows where the response ends.
- **Keep-alive**: in HTTP/1.1 the connection stays open after a response unless a side sends `Connection: close`. The client can send the next request on the same connection, which saves the cost of opening a new one. The generator keeps a pool of such connections.
- A **proxy** (Envoy) is both a server (to the caller) and a client (to the instance). A request that waits longer than the route timeout (2 s) is answered by Envoy with `504`.
- **503 Service Unavailable** means "I cannot answer right now". Here the instance answers it quickly when the ledger is unavailable.

## Walkthrough of this repository's use

### The request, byte by byte

```python
class RawOpenLoopRunner:
    """Sends `GET path` at a fixed rate over a pool of keep-alive connections."""

    def __init__(
        self, host: str, port: int, path: str = "/work", max_connections: int = 512, timeout_s: float = 5.0
    ) -> None:
        """`max_connections` bounds requests in flight; a request that waits for a connection still counts that wait.

        Twice that many requests may be outstanding; beyond it a scheduled request is counted as failed at once.
        """
        self._host = host
        self._port = port
        self._request = f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUser-Agent: capacity-loadtest\r\n\r\n".encode()
        self._limit = asyncio.Semaphore(max_connections)
        self._backlog = 2 * max_connections
        self._timeout_s = timeout_s
        self._idle: deque[_Connection] = deque()
```

`self._request = f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUser-Agent: capacity-loadtest\r\n\r\n".encode()` builds the whole request once: the request line, a `Host` header (HTTP/1.1 requires it), a `User-Agent`, and an empty line that ends the headers. `.encode()` turns the text into bytes. A `GET` has no body.

### Reading the response

```python
async def _exchange(self, connection: _Connection) -> tuple[int, bool]:
    connection.writer.write(self._request)
    await connection.writer.drain()
    status = int((await connection.reader.readline()).split()[1])
    length, keep_alive = 0, True
    while True:
        line = await connection.reader.readline()
        if line in (b"\r\n", b"\n", b""):
            break
        name, _, value = line.decode("latin-1").partition(":")
        if name.lower() == "content-length":
            length = int(value.strip())
        elif name.lower() == "connection" and value.strip().lower() == "close":
            keep_alive = False
    if length:
        await connection.reader.readexactly(length)
    return status, keep_alive
```

- `status = int((await connection.reader.readline()).split()[1])`: read one line (`HTTP/1.1 200 OK`), split on spaces, take the second word (`200`) as a number.
- The `while True` loop reads header lines until an empty one (`b"\r\n"`). For each line `line.decode("latin-1").partition(":")` splits at the first colon into name and value. It remembers `content-length` and whether `Connection: close` was sent.
- `readexactly(length)` reads the body so the connection is clean for the next request. A reply is OK only when the status is exactly 200.

### Status codes the services use

```python
async def _record(
    app: web.Application, ledger_url: str, status: int, body: dict[str, str]
) -> tuple[int, dict[str, str]]:
    """One bounded call to the ledger. A refusal, a timeout or a non-200 answer is a fast 503, never a hang."""
    payload = {"account": app[RNG].randint(1, 100), "amount_cents": app[RNG].randint(1, 10_000)}
    try:
        async with app[SESSION].post(f"{ledger_url}/entries", json=payload) as response:
            if response.status == 200:
                return status, body
            return 503, {**body, "error": f"ledger answered {response.status}"}
    except (aiohttp.ClientError, TimeoutError):
        return 503, {**body, "error": "ledger unavailable"}
```

An instance returns 200 normally. If the ledger is configured and unavailable, 503 with `ledger unavailable` or `ledger answered N`. The ledger itself answers 400 for a malformed body, 503 `ledger busy: pool exhausted` when no pooled connection is free within the timeout, 500 for anything unexpected.

### Envoy's part

```yaml
routes:
  - match: {prefix: "/"}
    route:
      cluster: app
      timeout: 2s
      retry_policy:
        retry_on: connect-failure,refused-stream,reset
        num_retries: 1
```

The route sends every path to the cluster `app`, with `timeout: 2s` and one retry on `connect-failure,refused-stream,reset`. A retry on *connection* failures is safe for a request that has not reached the application yet. The lesson uses this to make a restarting instance invisible to users.

### Reading Envoy's membership count

```python
def healthy_members() -> int:
    """Hosts the load balancer lists as healthy and not pending removal, or 0 when it cannot be reached."""
    try:
        with urllib.request.urlopen(CLUSTERS_URL, timeout=3) as response:
            return len(_HEALTHY_HOST.findall(response.read().decode()))
    except (urllib.error.URLError, TimeoutError, ValueError):
        return 0


def wait_for_members(expected: int, timeout_s: float = 90.0) -> None:
    """Block until the load balancer sees exactly `expected` healthy instances."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if healthy_members() == expected:
            return
        time.sleep(0.5)
    raise TimeoutError(f"the load balancer did not reach {expected} healthy instances within {timeout_s:g}s")


def versions_seen(url: str, samples: int) -> set[str]:
    """The `X-Version` of `samples` responses through the load balancer."""
    seen: set[str] = set()
    for _ in range(samples):
        with urllib.request.urlopen(f"{url}/work", timeout=5) as response:
            seen.add(response.headers.get("X-Version", "?"))
    return seen
```

`urllib.request.urlopen(CLUSTERS_URL, timeout=3)` fetches `http://localhost:47281/clusters`, a page that prints one line per host and per statistic. The code counts the lines that match the regular expression `^app::\S+::health_flags::healthy$`. `versions_seen` sends `samples` requests and collects the `X-Version` header of each: the rollout test uses it to confirm which release is serving.

## Try it yourself

1. `printf 'GET /work HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n' | nc localhost 47280` sends a hand-written request to Envoy (if you have `nc`); you see the status line, the headers (including `content-length`) and the JSON body.
2. `curl -i localhost:47280/work` prints the same through curl.
3. `curl -s -o /dev/null -w '%{http_code} %{time_total}\n' localhost:47280/work` prints the status code and the time in seconds (about a millisecond or two of CPU plus a few milliseconds of network).
4. `curl -s localhost:47281/clusters | grep -c 'health_flags::healthy$'` prints `9` right after `make up`.
5. `.venv/bin/python -m pytest tests/integration/test_instance_and_loadgen.py -q` exercises the hand-written client against a real instance.

## Mistakes people make

- Forgetting the blank line after the headers: the server waits forever for the rest of the request.
- Forgetting `Content-Length` handling, so leftover body bytes corrupt the next response on a kept-alive connection.
- Counting health probes as user traffic. Envoy probes the instances' ops port, not `/work`.
- Retrying requests that already reached the application. Envoy's retry is limited to connection-level failures.

## Check yourself

1. What ends the headers of a request?
   *Answer: an empty line (`\r\n\r\n` after the last header).*
2. What does `Content-Length` tell the reader?
   *Answer: how many body bytes follow, so it knows where the response ends.*
3. Why does the generator reuse connections?
   *Answer: keep-alive saves the cost of opening a connection for every request.*
4. When does Envoy answer 504?
   *Answer: when the instance does not finish within the route's 2-second timeout.*
5. Which statuses count as good for the availability SLO?
   *Answer: anything that is not 5xx.*

## Further reading

- HTTP/1.1 (RFC 9112): <https://www.rfc-editor.org/rfc/rfc9112>
- HTTP Semantics (RFC 9110): <https://www.rfc-editor.org/rfc/rfc9110>
- curl man page: <https://curl.se/docs/manpage.html>
