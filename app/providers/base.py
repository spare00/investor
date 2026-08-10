"""Provider adapter base: metadata, retry, circuit breaker."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Awaitable, Callable, TypeVar
from uuid import uuid4

from app.core.config import Settings, get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)
T = TypeVar("T")


class ProviderStatus(StrEnum):
    OK = "ok"
    PARTIAL = "partial"
    ERROR = "error"
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    CIRCUIT_OPEN = "circuit_open"
    DISABLED = "disabled"


@dataclass(slots=True)
class ProviderRequestMeta:
    provider_name: str
    provider_version: str
    request_id: str
    request_started_at: datetime
    request_completed_at: datetime | None = None
    source_timestamp: datetime | None = None
    collection_timestamp: datetime | None = None
    status: ProviderStatus = ProviderStatus.OK
    latency_ms: float | None = None
    rate_limit_state: str | None = None
    raw_payload_reference: str | None = None
    error_code: str | None = None
    error_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_name": self.provider_name,
            "provider_version": self.provider_version,
            "request_id": self.request_id,
            "request_started_at": self.request_started_at.isoformat(),
            "request_completed_at": self.request_completed_at.isoformat()
            if self.request_completed_at
            else None,
            "source_timestamp": self.source_timestamp.isoformat() if self.source_timestamp else None,
            "collection_timestamp": self.collection_timestamp.isoformat()
            if self.collection_timestamp
            else None,
            "status": self.status.value,
            "latency_ms": self.latency_ms,
            "rate_limit_state": self.rate_limit_state,
            "raw_payload_reference": self.raw_payload_reference,
            "error_code": self.error_code,
            "error_message": self.error_message,
        }


@dataclass
class CircuitBreaker:
    failure_threshold: int
    reset_seconds: int
    failures: int = 0
    opened_at: float | None = None

    def allow(self) -> bool:
        if self.opened_at is None:
            return True
        if time.monotonic() - self.opened_at >= self.reset_seconds:
            self.opened_at = None
            self.failures = 0
            return True
        return False

    def record_success(self) -> None:
        self.failures = 0
        self.opened_at = None

    def record_failure(self) -> None:
        self.failures += 1
        if self.failures >= self.failure_threshold:
            self.opened_at = time.monotonic()


_BREAKERS: dict[str, CircuitBreaker] = {}


def get_breaker(name: str, settings: Settings | None = None) -> CircuitBreaker:
    cfg = settings or get_settings()
    if name not in _BREAKERS:
        _BREAKERS[name] = CircuitBreaker(
            failure_threshold=cfg.provider_circuit_breaker_failures,
            reset_seconds=cfg.provider_circuit_breaker_reset_seconds,
        )
    return _BREAKERS[name]


def reset_breakers() -> None:
    _BREAKERS.clear()


def redact_secrets(payload: Any) -> Any:
    """Recursively redact credential-like keys for logs/storage metadata."""
    if isinstance(payload, dict):
        out = {}
        for k, v in payload.items():
            key = str(k).lower()
            if any(s in key for s in ("secret", "token", "password", "api_key", "apikey", "auth")):
                out[k] = "***REDACTED***"
            else:
                out[k] = redact_secrets(v)
        return out
    if isinstance(payload, list):
        return [redact_secrets(x) for x in payload]
    return payload


@dataclass
class ProviderCapabilities:
    name: str
    version: str
    supports_quotes: bool = False
    supports_bars: bool = False
    supports_premarket: bool = False
    supports_news: bool = False
    supports_sec: bool = False
    supports_macro: bool = False
    supports_economic_calendar: bool = False
    requires_credentials: bool = False
    is_fixture: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "supports_quotes": self.supports_quotes,
            "supports_bars": self.supports_bars,
            "supports_premarket": self.supports_premarket,
            "supports_news": self.supports_news,
            "supports_sec": self.supports_sec,
            "supports_macro": self.supports_macro,
            "supports_economic_calendar": self.supports_economic_calendar,
            "requires_credentials": self.requires_credentials,
            "is_fixture": self.is_fixture,
        }


async def run_with_retry(
    *,
    provider_name: str,
    provider_version: str,
    settings: Settings,
    fn: Callable[[], Awaitable[T]],
    timeout_seconds: float | None = None,
) -> tuple[T | None, ProviderRequestMeta]:
    started = datetime.now(UTC)
    meta = ProviderRequestMeta(
        provider_name=provider_name,
        provider_version=provider_version,
        request_id=str(uuid4()),
        request_started_at=started,
        collection_timestamp=started,
    )
    breaker = get_breaker(provider_name, settings)
    if not breaker.allow():
        meta.status = ProviderStatus.CIRCUIT_OPEN
        meta.error_code = "circuit_open"
        meta.error_message = "circuit breaker open"
        meta.request_completed_at = datetime.now(UTC)
        meta.latency_ms = (meta.request_completed_at - started).total_seconds() * 1000
        return None, meta

    last_exc: Exception | None = None
    attempts = settings.provider_max_retries + 1
    timeout = float(
        timeout_seconds
        if timeout_seconds is not None
        else settings.provider_request_timeout_seconds
    )
    for attempt in range(attempts):
        try:
            result = await asyncio.wait_for(fn(), timeout=timeout)
            breaker.record_success()
            meta.status = ProviderStatus.OK
            meta.request_completed_at = datetime.now(UTC)
            meta.latency_ms = (meta.request_completed_at - started).total_seconds() * 1000
            return result, meta
        except TimeoutError as exc:
            last_exc = exc
            meta.status = ProviderStatus.TIMEOUT
            meta.error_code = "timeout"
            breaker.record_failure()
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            msg = str(exc).lower()
            if "429" in msg or "rate" in msg:
                meta.status = ProviderStatus.RATE_LIMITED
                meta.error_code = "rate_limited"
                meta.rate_limit_state = "hit"
            else:
                meta.status = ProviderStatus.ERROR
                meta.error_code = "provider_error"
            meta.error_message = str(exc)[:500]
            breaker.record_failure()
            logger.warning(
                "provider_call_failed",
                provider=provider_name,
                attempt=attempt + 1,
                error=str(exc)[:200],
            )
        if attempt + 1 < attempts:
            await asyncio.sleep(settings.provider_retry_backoff_seconds * (attempt + 1))

    meta.request_completed_at = datetime.now(UTC)
    meta.latency_ms = (meta.request_completed_at - started).total_seconds() * 1000
    if last_exc and not meta.error_message:
        meta.error_message = str(last_exc)[:500]
    return None, meta
