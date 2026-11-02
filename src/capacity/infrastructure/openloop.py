"""An open-loop HTTP/1.1 load generator over raw asyncio streams.

Open loop: each request is scheduled to arrive at a fixed time whether or not earlier ones have finished, and its
latency runs from that scheduled time to the end of its response. A closed-loop generator (send the next request
after the last one returns) can never show queuing delay, because it never lets a queue form.
"""

from __future__ import annotations

import asyncio
import re
from collections import deque
from dataclasses import dataclass

from capacity.domain.latency import Sample, percentile

_CPU_LINE = re.compile(r"^process_cpu_seconds_total\s+([0-9.eE+-]+)\s*$", re.MULTILINE)


@dataclass(slots=True)
class _Connection:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter


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

    async def run(self, rps: float, duration_s: float) -> tuple[list[Sample], float]:
        """Send `rps * duration_s` requests on schedule; return all samples and the generator's own p99 lag in ms."""
        loop = asyncio.get_running_loop()
        total = int(rps * duration_s)
        start = loop.time()
        samples: list[Sample] = []
        lags: list[float] = []
        tasks: set[asyncio.Task[None]] = set()

        async def one(scheduled: float) -> None:
            ok = await self._request_once()
            samples.append(Sample(scheduled - start, loop.time() - scheduled, ok))

        index = 0
        while index < total:
            now = loop.time()
            due = min(total, int((now - start) * rps) + 1)
            while index < due:
                scheduled = start + index / rps
                lags.append(now - scheduled)
                if len(tasks) >= self._backlog:
                    # The service is so far behind that requests are piling up unsent. Count the request as failed
                    # now instead of queueing a task for it: thousands of waiting tasks would slow the generator itself.
                    samples.append(Sample(scheduled - start, loop.time() - scheduled, False))
                else:
                    task = loop.create_task(one(scheduled))
                    tasks.add(task)
                    task.add_done_callback(tasks.discard)
                index += 1
            await asyncio.sleep(max(0.0005, start + index / rps - loop.time()))
        if tasks:
            await asyncio.gather(*tasks)
        return samples, percentile([lag * 1000.0 for lag in lags], 0.99)

    async def close(self) -> None:
        """Close every idle connection."""
        while self._idle:
            self._idle.popleft().writer.close()

    async def _request_once(self) -> bool:
        async with self._limit:
            connection: _Connection | None = None
            try:
                connection = self._idle.pop() if self._idle else await self._connect()
                status, keep_alive = await asyncio.wait_for(self._exchange(connection), self._timeout_s)
            except (OSError, asyncio.IncompleteReadError, TimeoutError, ValueError):
                if connection is not None:
                    connection.writer.close()
                return False
            if keep_alive:
                self._idle.append(connection)
            else:
                connection.writer.close()
            return status == 200

    async def _connect(self) -> _Connection:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(self._host, self._port), self._timeout_s)
        return _Connection(reader, writer)

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


class MetricsCpuProbe:
    """Reads `process_cpu_seconds_total` from an instance's ops port: how much CPU the process has used so far."""

    def __init__(self, host: str, port: int) -> None:
        """`port` is the instance's ops port."""
        self._host = host
        self._port = port

    async def cpu_seconds(self) -> float:
        """Cumulative CPU seconds of the instance."""
        reader, writer = await asyncio.open_connection(self._host, self._port)
        try:
            writer.write(f"GET /metrics HTTP/1.0\r\nHost: {self._host}\r\n\r\n".encode())
            await writer.drain()
            text = (await reader.read()).decode("utf-8", "replace")
        finally:
            writer.close()
        match = _CPU_LINE.search(text)
        if match is None:
            raise ValueError("the instance exports no process_cpu_seconds_total")
        return float(match.group(1))
