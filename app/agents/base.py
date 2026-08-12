"""Shared agent base: prompt loading, LLM call, schema validation, retries."""

from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import Any, Generic, TypeVar
from uuid import uuid4

from pydantic import BaseModel, ValidationError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_fixed

from app.agents.llm_sanitize import sanitize_for_model, schema_enum_hint
from app.agents.prompts import LoadedPrompt, load_agent_prompt
from app.agents.activity import mark_agent_finished, mark_agent_started
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.schemas.common import AgentName, TraceMetadata
from app.services.llm import LLMClient, LLMError, get_llm_client

logger = get_logger(__name__)

InputT = TypeVar("InputT", bound=BaseModel)
OutputT = TypeVar("OutputT", bound=BaseModel)


class AgentExecutionError(Exception):
    """Raised when an agent cannot produce a validated output."""


class BaseAgent(ABC, Generic[InputT, OutputT]):
    name: AgentName
    agent_version: str = "0.1.0"
    prompt_version: str = "1.0.0"
    prompt_file: str = "system_v1.md"
    schema_version: str = "1.0.0"

    def __init__(
        self,
        *,
        llm: LLMClient | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.llm = llm or get_llm_client(self.settings)
        self._loaded_prompt: LoadedPrompt | None = None

    def load_prompt(self) -> LoadedPrompt:
        if self._loaded_prompt is None:
            self._loaded_prompt = load_agent_prompt(self.name.value, filename=self.prompt_file)
            self.prompt_version = self._loaded_prompt.version
        return self._loaded_prompt

    def load_system_prompt(self) -> str:
        loaded = self.load_prompt()
        return (
            f"{loaded.system_prompt}\n\n{schema_enum_hint()}\n\n"
            f"Prompt-Version: {loaded.version}\n"
            f"Prompt-SHA256: {loaded.sha256}\n"
        )

    def default_system_prompt(self) -> str:
        return f"You are the {self.name.value} agent for an internal trading system."

    @abstractmethod
    def output_model(self) -> type[OutputT]: ...

    @abstractmethod
    def build_user_prompt(self, payload: InputT) -> str: ...

    def fallback_output(self, payload: InputT, *, reason: str) -> OutputT | None:
        """Optional deterministic fallback when LLM is unavailable. Default: none."""
        return None

    async def run(self, payload: InputT) -> OutputT:
        """Execute agent with schema validation and isolated error handling."""
        run_id = uuid4()
        started = time.perf_counter()
        mark_agent_started(self.name.value, run_id=str(run_id))
        logger.info("agent_start", agent=self.name.value, run_id=str(run_id))
        try:
            result = await self._run_validated(payload, run_id=run_id, started=started)
            mark_agent_finished(self.name.value, outcome="completed")
            return result
        except Exception as exc:  # noqa: BLE001 — isolate failures at agent boundary
            logger.exception("agent_failed", agent=self.name.value, run_id=str(run_id))
            fallback = self.fallback_output(payload, reason=str(exc))
            if fallback is not None:
                logger.warning(
                    "agent_fallback_used",
                    agent=self.name.value,
                    run_id=str(run_id),
                    reason=str(exc),
                )
                mark_agent_finished(self.name.value, outcome="fallback", error=str(exc))
                return fallback
            mark_agent_finished(self.name.value, outcome="failed", error=str(exc))
            raise AgentExecutionError(f"{self.name.value} failed: {exc}") from exc

    async def _run_validated(
        self, payload: InputT, *, run_id: Any, started: float
    ) -> OutputT:
        loaded = self.load_prompt()
        system_prompt = self.load_system_prompt()
        base_user_prompt = self.build_user_prompt(payload)
        book_block = ""
        try:
            trace = getattr(payload, "trace", None)
            book = getattr(trace, "book", None) if trace is not None else None
            if isinstance(book, dict) and book.get("venue"):
                from app.market.book_context import book_from_mapping

                ctx = book_from_mapping(book)
                if ctx is not None:
                    book_block = ctx.prompt_block() + "\n\n"
        except Exception:  # noqa: BLE001
            book_block = ""
        if book_block:
            base_user_prompt = f"{book_block}{base_user_prompt}"
        validation_feedback: list[str] = []

        # Phase 2 policy: one validation repair attempt, then fail (fallback may still apply).
        @retry(
            reraise=True,
            stop=stop_after_attempt(2),
            wait=wait_fixed(0.2),
            retry=retry_if_exception_type(
                (ValidationError, LLMError, json.JSONDecodeError, ValueError)
            ),
        )
        async def _once() -> OutputT:
            user_prompt = base_user_prompt
            if validation_feedback:
                user_prompt = (
                    f"{base_user_prompt}\n\nPrevious output failed validation. "
                    f"Fix these errors and resubmit valid JSON only:\n{validation_feedback[-1]}"
                )
            response = await self.llm.complete_json(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )
            data = json.loads(response.content)
            if not isinstance(data, dict):
                raise ValueError("LLM content must be a JSON object")
            model = self.output_model()
            data = sanitize_for_model(data, model)
            latency_ms = (time.perf_counter() - started) * 1000.0
            trace = data.get("trace") or {}
            if isinstance(trace, dict):
                trace.setdefault("agent_version", self.agent_version)
                trace["prompt_version"] = loaded.version
                trace["prompt_sha256"] = loaded.sha256
                trace["schema_version"] = self.schema_version
                trace.setdefault("model_name", response.model)
                trace.setdefault(
                    "model_parameters",
                    {
                        "temperature": self.settings.llm_temperature,
                        "max_tokens": self.settings.llm_max_tokens,
                    },
                )
                usage = {}
                if isinstance(response.raw, dict):
                    usage = response.raw.get("usage") or {}
                trace.setdefault("token_usage", usage if isinstance(usage, dict) else {})
                trace["latency_ms"] = latency_ms
                trace.setdefault("decision_timestamp", datetime.now(UTC).isoformat())
                trace.setdefault("run_id", str(run_id))
                input_trace = getattr(payload, "trace", None)
                input_book = getattr(input_trace, "book", None) if input_trace is not None else None
                if input_book and not trace.get("book"):
                    if isinstance(input_book, dict):
                        trace["book"] = input_book
                    elif hasattr(input_book, "model_dump"):
                        trace["book"] = input_book.model_dump(mode="json")
                data["trace"] = trace
            data.setdefault("timestamp", datetime.now(UTC).isoformat())
            try:
                return model.model_validate(data)
            except ValidationError as exc:
                validation_feedback.append(str(exc)[:2500])
                raise

        return await _once()


def dump_for_prompt(model: BaseModel | dict[str, Any] | list[Any] | None) -> str:
    if model is None:
        return "null"
    if isinstance(model, BaseModel):
        return model.model_dump_json(indent=2)
    return json.dumps(model, indent=2, default=str)


def ensure_trace(existing: TraceMetadata | None = None) -> TraceMetadata:
    return existing or TraceMetadata()
