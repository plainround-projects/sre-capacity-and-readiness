"""Configuration from the environment, validated once at start-up (twelve-factor)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from capacity.domain.cpu import DEFAULT_ROUNDS


class SettingsError(ValueError):
    """An environment variable is missing, malformed or out of range."""


def _text(env: Mapping[str, str], name: str, default: str) -> str:
    value = env.get(name, default).strip()
    if not value:
        raise SettingsError(f"{name} must not be empty")
    return value


def _number(env: Mapping[str, str], name: str, default: float, low: float, high: float) -> float:
    raw = env.get(name)
    try:
        value = default if raw is None else float(raw)
    except ValueError as error:
        raise SettingsError(f"{name} must be a number, got {raw!r}") from error
    if not low <= value <= high:
        raise SettingsError(f"{name} must be between {low:g} and {high:g}, got {value:g}")
    return value


def _port(env: Mapping[str, str], name: str, default: int) -> int:
    return int(_number(env, name, default, 1, 65535))


@dataclass(frozen=True, slots=True)
class InstanceSettings:
    """Settings of one service instance."""

    zone: str
    instance: str
    version: str
    work_rounds: int
    ledger_url: str | None
    ledger_timeout_s: float
    port: int
    ops_port: int
    log_level: str

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> InstanceSettings:
        """Read and validate. An empty LEDGER_URL means the instance has no shared dependency."""
        ledger = env.get("LEDGER_URL", "").strip().rstrip("/")
        return cls(
            zone=_text(env, "ZONE", "a"),
            instance=_text(env, "INSTANCE", "a1"),
            version=_text(env, "VERSION", "v1"),
            work_rounds=int(_number(env, "WORK_ROUNDS", DEFAULT_ROUNDS, 1, 10_000_000)),
            ledger_url=ledger or None,
            ledger_timeout_s=_number(env, "LEDGER_TIMEOUT_S", 0.5, 0.05, 30.0),
            port=_port(env, "PORT", 8080),
            ops_port=_port(env, "OPS_PORT", 9101),
            log_level=_text(env, "LOG_LEVEL", "INFO").upper(),
        )


@dataclass(frozen=True, slots=True)
class LedgerSettings:
    """Settings of the shared ledger service and its connection pool, the dependency every instance calls."""

    database_url: str
    pool_size: int
    pool_timeout_s: float
    hold_ms: float
    port: int
    ops_port: int
    log_level: str

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> LedgerSettings:
        """Read and validate. DATABASE_URL has no default."""
        url = env.get("DATABASE_URL", "").strip()
        if not url.startswith("postgresql://"):
            raise SettingsError("DATABASE_URL must be set to a postgresql:// URL")
        return cls(
            database_url=url,
            pool_size=int(_number(env, "POOL_SIZE", 20, 1, 90)),
            pool_timeout_s=_number(env, "POOL_TIMEOUT_S", 0.4, 0.01, 30.0),
            hold_ms=_number(env, "HOLD_MS", 10.0, 0.0, 1000.0),
            port=_port(env, "PORT", 8082),
            ops_port=_port(env, "OPS_PORT", 9102),
            log_level=_text(env, "LOG_LEVEL", "INFO").upper(),
        )
