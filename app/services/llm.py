"""OpenAI-compatible LLM client with timeout, retries, and daily spend budget."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.services.llm_budget import (
    LLMBudgetExceeded,
    assert_llm_budget_allows_call,
    record_llm_usage,
    snapshot_llm_budget,
    usage_from_openai_response,
)

logger = get_logger(__name__)


class LLMError(Exception):
    """Raised when the LLM call fails after retries."""


@dataclass(slots=True)
class LLMResponse:
    content: str
    model: str
    raw: dict[str, Any]


class LLMClient(Protocol):
    async def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse: ...


class OpenAICompatibleClient:
    """Chat Completions against an OpenAI-compatible HTTP API."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    async def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        cfg = self.settings
        if not _llm_api_key_configured(cfg):
            raise LLMError("LLM_API_KEY is not configured")
        try:
            assert_llm_budget_allows_call(cfg)
        except LLMBudgetExceeded as exc:
            try:
                from app.core.metrics import LLM_BUDGET_EXCEEDED

                reason = "tokens" if "token" in exc.reason else "calls"
                LLM_BUDGET_EXCEEDED.labels(reason=reason).inc()
            except Exception:  # noqa: BLE001
                pass
            logger.error("llm_budget_blocked", reason=exc.reason, **(exc.snapshot or {}))
            try:
                from app.alerts.base import AlertSeverity
                from app.alerts.ops import emit_llm_budget_alert

                await emit_llm_budget_alert(
                    settings=cfg,
                    code="llm.budget_exhausted",
                    message=str(exc.reason),
                    severity=AlertSeverity.CRITICAL,
                    context=exc.snapshot or {},
                )
            except Exception:  # noqa: BLE001
                pass
            raise LLMError(str(exc)) from exc
        return await self._complete_json_with_retries(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.TransportError, LLMError)),
    )
    async def _complete_json_with_retries(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        cfg = self.settings
        api_key = cfg.llm_api_key.get_secret_value() if cfg.llm_api_key else ""

        payload = {
            "model": model or cfg.llm_model,
            "temperature": cfg.llm_temperature if temperature is None else temperature,
            "max_tokens": cfg.llm_max_tokens if max_tokens is None else max_tokens,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        url = cfg.llm_base_url.rstrip("/") + "/chat/completions"
        timeout = httpx.Timeout(cfg.llm_timeout_seconds)

        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, headers=headers, json=payload)
            if response.status_code >= 400:
                logger.error(
                    "llm_http_error",
                    status=response.status_code,
                    body=response.text[:500],
                )
                raise LLMError(f"LLM HTTP {response.status_code}")
            data = response.json()

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError("Unexpected LLM response shape") from exc

        prompt_t, completion_t = usage_from_openai_response(data if isinstance(data, dict) else None)
        if prompt_t == 0 and completion_t == 0:
            # Provider omitted usage — count a conservative floor so budgets still bite.
            prompt_t = max(1, (len(system_prompt) + len(user_prompt)) // 4)
            completion_t = max(1, len(content) // 4)
        record_llm_usage(prompt_tokens=prompt_t, completion_tokens=completion_t, settings=cfg)
        snap = snapshot_llm_budget(cfg)
        if snap.soft_warned or snap.month_soft_warned:
            try:
                from app.alerts.base import AlertSeverity
                from app.alerts.ops import emit_llm_budget_alert

                await emit_llm_budget_alert(
                    settings=cfg,
                    code="llm.budget_soft_limit",
                    message="LLM budget soft limit reached",
                    severity=AlertSeverity.WARNING,
                    context=snap.to_dict(),
                )
            except Exception:  # noqa: BLE001
                pass

        return LLMResponse(content=content, model=str(data.get("model") or payload["model"]), raw=data)


class StubLLMClient:
    """
    Deterministic JSON producer for offline tests (Phase 2 FakeLLMProvider).

    Returns a caller-provided payload (already a JSON object / dict).
    """

    def __init__(self, payload: dict[str, Any] | None = None) -> None:
        self.payload = payload or {}
        self.calls: list[dict[str, str]] = []

    async def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        self.calls.append({"system": system_prompt[:80], "user": user_prompt[:200]})
        return LLMResponse(
            content=json.dumps(self.payload),
            model=model or "fake-llm",
            raw={"stub": True, "usage": {"prompt_tokens": 0, "completion_tokens": 0}},
        )


# Phase 2 naming alias
FakeLLMProvider = StubLLMClient
OpenAICompatibleProvider = OpenAICompatibleClient


def _llm_api_key_configured(settings: Settings) -> bool:
    if settings.llm_api_key is None:
        return False
    return bool(settings.llm_api_key.get_secret_value().strip())


def get_llm_client(settings: Settings | None = None) -> LLMClient:
    cfg = settings or get_settings()
    if not _llm_api_key_configured(cfg):
        logger.warning("llm_client_fallback_stub", reason="missing_api_key")
        return StubLLMClient()
    return OpenAICompatibleClient(cfg)
