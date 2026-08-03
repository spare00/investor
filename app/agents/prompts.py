"""Prompt file loading, versioning, and hashing."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

PROMPTS_ROOT = Path(__file__).resolve().parents[2] / "prompts"
SHARED_DIR = PROMPTS_ROOT / "shared"

_VERSION_RE = re.compile(r"^Prompt-Version:\s*(\S+)", re.MULTILINE | re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class LoadedPrompt:
    agent_key: str
    body: str
    version: str
    sha256: str
    path: Path
    common_rules: str
    output_contract: str

    @property
    def system_prompt(self) -> str:
        return (
            f"{self.body.strip()}\n\n"
            f"---\n# Shared Common Rules\n{self.common_rules.strip()}\n\n"
            f"---\n# Shared Output Contract\n{self.output_contract.strip()}\n"
        )


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def load_shared() -> tuple[str, str]:
    rules = _read(SHARED_DIR / "common_rules.md")
    contract = _read(SHARED_DIR / "output_contract.md")
    return rules, contract


def extract_prompt_version(text: str, default: str = "1.0.0") -> str:
    match = _VERSION_RE.search(text)
    return match.group(1) if match else default


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_agent_prompt(agent_key: str, *, filename: str = "system_v1.md") -> LoadedPrompt:
    """
    Load prompts/{agent_key}/system_v1.md combined with shared rules.

    Falls back to legacy prompts/{agent_key}_v0.1.0.txt if v1 missing.
    """
    primary = PROMPTS_ROOT / agent_key / filename
    legacy = PROMPTS_ROOT / f"{agent_key}_v0.1.0.txt"
    if primary.exists():
        path = primary
        body = _read(primary)
    elif legacy.exists():
        path = legacy
        body = _read(legacy)
    else:
        raise FileNotFoundError(f"No prompt file for agent {agent_key}: tried {primary} and {legacy}")

    rules, contract = load_shared()
    version = extract_prompt_version(body)
    full_for_hash = f"{body}\n{rules}\n{contract}"
    return LoadedPrompt(
        agent_key=agent_key,
        body=body,
        version=version,
        sha256=sha256_text(full_for_hash),
        path=path,
        common_rules=rules,
        output_contract=contract,
    )


REQUIRED_PROMPT_SECTIONS = (
    "Identity",
    "Mission",
    "Inputs",
    "Permitted Reasoning Scope",
    "Required Analysis Procedure",
    "Output Requirements",
    "Abstention and Failure Conditions",
    "Forbidden Actions",
    "Quality Checklist",
)

AGENT_PROMPT_KEYS = (
    "market_intelligence",
    "macro_strategist",
    "quant_strategist",
    "risk_manager",
    "devils_advocate",
    "cio",
)
