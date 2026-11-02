# uvloop

## What it is

`uvloop` is a Python library that replaces the event loop of `asyncio` with a faster one written in Cython on top of **libuv**, the C library that Node.js uses for its event loop. An **event loop** is the part of an `asyncio` program that waits for sockets and timers and runs the code that was waiting for them. `asyncio` ships with one written in Python; `uvloop` does the same job with far less per-event overhead and the same programming interface, so the program does not change.

## Why this repository uses it

Three processes here are limited by the cost of the loop itself rather than by their own work: the **ledger** (every request is one trip through the loop for the web server, one for the database driver and one for the reply), the **instances** (the call to the ledger and the web server around the SHA-256 work) and the **load generator** (2,000 requests a second from one Python process). On the stock loop the ledger used a whole core at about 1,750 requests a second, below the projected peak of 2,000, so the pool experiment measured the ledger's CPU instead of its connection pool.

## Where it appears

| File | Use |
| --- | --- |
| `pyproject.toml`, `requirements.lock`, `requirements-dev.lock` | The dependency `uvloop==0.21.0`, with hashes. |
| `src/capacity/interfaces/instance_app.py` | `main` runs the instance on uvloop. |
| `src/capacity/interfaces/ledger_app.py` | `main` and each worker run on uvloop. |
| `src/capacity/interfaces/loadtest_cli.py` | The load generator runs on uvloop. |

## How it works underneath

`asyncio.run(coroutine, loop_factory=uvloop.new_event_loop)` creates the loop with the given function instead of the default one (the `loop_factory` argument exists since Python 3.12). uvloop wraps libuv handles for TCP, timers and signals, so each wake-up costs a few microseconds of C instead of tens of Python calls. The CPU-bound work is not faster: `hashlib` runs the same SHA-256 either way.

## Walkthrough of this repository's use

The three call sites, quoted from the interface layer note:

```python
# uvloop cuts the cost of everything around the CPU work (the server, and the call to the ledger). The CPU work
# itself is unchanged, so the instance still saturates its core where the load test finds it.
asyncio.run(run(settings), loop_factory=uvloop.new_event_loop)
```

```python
asyncio.run(run(own), loop_factory=uvloop.new_event_loop)
```

```python
# uvloop keeps the generator's own scheduling delay small at 2,000 requests a second.
result, problem = asyncio.run(
    _knee(args) if args.command == "knee" else _timed(args), loop_factory=uvloop.new_event_loop
```

Each passes the loop factory to `asyncio.run`. The dependency line in the project file:

```python
"uvloop==0.21.0",
```

Pinned to one version like the other runtime dependencies; `requirements.lock` repeats it with the SHA-256 hashes of the wheels, and `pip install --require-hashes` refuses anything else.

## Try it yourself

1. `.venv/bin/python -c "import uvloop; print(uvloop.__version__)"` prints `0.21.0`.
2. `.venv/bin/python -c "import asyncio, uvloop; print(asyncio.run(asyncio.sleep(0, 'ok'), loop_factory=uvloop.new_event_loop))"` prints `ok`.
3. `grep -n -A3 '^uvloop' requirements.lock` shows the pin and its hashes.

## Mistakes people make

- Expecting uvloop to speed up CPU-bound code. It only cuts the cost of waiting and I/O.
- Installing it but not using it: nothing changes unless the loop is created from it.
- Using it on Windows, where it is not available.

## Check yourself

1. What does `loop_factory` do?
   *Answer: tells `asyncio.run` how to create the event loop.*
2. Why does the instance still saturate its core at the same load?
   *Answer: the SHA-256 work is unchanged; uvloop only cuts the cost of the I/O around it.*
3. Why does the ledger run on it?
   *Answer: its work is almost all I/O handling, which one stock loop could not keep up with at 2,000 requests a second.*
4. How does the install stay safe?
   *Answer: the version is pinned and `requirements.lock` carries the hashes, checked by `--require-hashes`.*
5. What stays the same in the program?
   *Answer: every `async def`, `await` and `asyncio` call; only the loop under them changes.*

## Further reading

- uvloop: <https://uvloop.readthedocs.io/>
- `asyncio.run` and `loop_factory`: <https://docs.python.org/3.12/library/asyncio-runner.html#asyncio.run>
- libuv: <https://libuv.org/>
