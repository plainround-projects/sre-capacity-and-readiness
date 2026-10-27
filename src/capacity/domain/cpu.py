"""The service's real work: CPU burned per request, not a sleep standing in for it."""

from __future__ import annotations

import hashlib

DEFAULT_ROUNDS = 3000


def cpu_work(rounds: int = DEFAULT_ROUNDS) -> str:
    """Chain SHA-256 `rounds` times and return the hex digest.

    The time this takes is real CPU time on the machine it runs on, so the knee a load test finds is an actual
    resource limit. Cost scales linearly with `rounds` (about a third of a microsecond each on a current laptop).
    """
    digest = hashlib.sha256(b"x").digest()
    for _ in range(rounds):
        digest = hashlib.sha256(digest).digest()
    return digest.hex()
