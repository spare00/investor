"""Shared agent base: prompt loading, LLM call, schema validation, retries."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Generic, TypeVar
from uuid import uuid4

from pydantic import BaseModel, ValidationError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_fixed

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.schemas.common import AgentName, TraceMetadata
from app.services.llm import LLMClient, LLMError, get_llm_client

logger = get_logger(__name__)

InputT = TypeVar("InputT", bound=BaseModel)
OutputT = TypeVar("OutputT", bound=BaseModel)

PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"

COMMON_RULES = """
COMMON RULES (mandatory):
- Use only the provided data. Do not invent missing facts.
- If data is insufficient, say so and lower data_quality_score; do not speculate.
- Always check timestamps of the latest data.
- Separate facts from interpretation.
- Write both supporting and opposing evidence.
- Do not overstate confidence.
- Do not use unsourced news.
- Explicitly note conflicting data.
- If important information is stale, lower data_quality_score.
- Respond as internal system analysis JSON, never as investment advice.
- Follow the specified JSON schema exactly.
""".strip()


class AgentExecutionError(Exception):
    """Raised when an agent cannot produce a validated output."""


class BaseAgent(ABC, Generic[InputT, OutputT]):
    name: AgentName
    agent_version: str = "0.1.0"
    prompt_version: str = "0.1.0"
    prompt_file: str

    def __init__(
        self,
        *,
        llm: LLMClient | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.llm = llm or get_llm_client(self.settings)

    def load_system_prompt(self) -> str:
        path = PROMPTS_DIR / self.prompt_file
        body = path.read_text(encoding="utf-8") if path.exists() else self.default_system_prompt()
        return f"{body.strip()}\n\n{COMMON_RULES}\n\nPrompt-Version: {self.prompt_version}\n"

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
        logger.info("agent_start", agent=self.name.value, run_id=str(run_id))
        try:
            return await self._run_validated(payload, run_id=run_id)
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
                return fallback
            raise AgentExecutionError(f"{self.name.value} failed: {exc}") from exc

    async def _run_validated(self, payload: InputT, *, run_id: Any) -> OutputT:
        system_prompt = self.load_system_prompt()
        user_prompt = self.build_user_prompt(payload)

        @retry(
            reraise=True,
            stop=stop_after_attempt(self.settings.llm_max_retries + 1),
            wait=wait_fixed(0.2),
            retry=retry_if_exception_type((ValidationError, LLMError, json.JSONDecodeError, ValueError)),
        )
        async def _once() -> OutputT:
            response = await self.llm.complete_json(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )
            data = json.loads(response.content)
            if not isinstance(data, dict):
                raise ValueError("LLM content must be a JSON object")
            model = self.output_model()
            # Inject/refresh audit metadata when absent
            trace = data.get("trace") or {}
            if isinstance(trace, dict):
                trace.setdefault("agent_version", self.agent_version)
                trace.setdefault("prompt_version", self.prompt_version)
                trace.setdefault("model_name", response.model)
                trace.setdefault(
                    "model_parameters",
                    {
                        "temperature": self.settings.llm_temperature,
                        "max_tokens": self.settings.llm_max_tokens,
                    },
                )
                trace.setdefault("decision_timestamp", datetime.now(UTC).isoformat())
                trace.setdefault("run_id", str(run_id))
                data["trace"] = trace
            return model.model_validate(data)

        return await _once()


def dump_for_prompt(model: BaseModel | dict[str, Any] | list[Any] | None) -> str:
    if model is None:
        return "null"
    if isinstance(model, BaseModel):
        return model.model_dump_json(indent=2)
    return json.dumps(model, indent=2, default=str)


def ensure_trace(existing: TraceMetadata | None = None) -> TraceMetadata:
    return existing or TraceMetadata()
