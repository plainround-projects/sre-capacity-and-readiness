"""Docker Compose and the stack's HTTP surfaces, driven from the host: the experiments' hands."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from pathlib import Path
from typing import Any

_ATTEMPTS = 3
_RETRY_PAUSE_SECONDS = 2.0
_TRANSIENT = ("Bad Gateway", "request returned", "connection reset", "EOF", "i/o timeout")
CLUSTERS_URL = "http://localhost:47281/clusters"
# One line per host: `app::192.168.32.5:8080::health_flags::healthy`. A host Envoy has dropped from DNS but still
# remembers shows `/pending_dynamic_removal` instead, so it does not match.
_HEALTHY_HOST = re.compile(r"^app::\S+::health_flags::healthy$", re.MULTILINE)


class ComposeError(RuntimeError):
    """A docker compose command failed."""


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
