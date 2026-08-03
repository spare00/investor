"""Intraday operation modes and capability gates."""

from __future__ import annotations

from enum import StrEnum

from app.core.config import Settings


class IntradayOperationMode(StrEnum):
    OBSERVE_ONLY = "OBSERVE_ONLY"
    ANALYZE_ONLY = "ANALYZE_ONLY"
    MANUAL_APPROVAL = "MANUAL_APPROVAL"
    PAPER_AUTOMATED = "PAPER_AUTOMATED"
    PAUSED = "PAUSED"
    EMERGENCY_STOP = "EMERGENCY_STOP"


class ModeCapabilities:
    def __init__(self, mode: IntradayOperationMode) -> None:
        self.mode = mode

    @property
    def can_collect(self) -> bool:
        return self.mode != IntradayOperationMode.EMERGENCY_STOP

    @property
    def can_analyze(self) -> bool:
        return self.mode in {
            IntradayOperationMode.OBSERVE_ONLY,
            IntradayOperationMode.ANALYZE_ONLY,
            IntradayOperationMode.MANUAL_APPROVAL,
            IntradayOperationMode.PAPER_AUTOMATED,
        }

    @property
    def can_create_intent(self) -> bool:
        return self.mode in {
            IntradayOperationMode.ANALYZE_ONLY,  # draft only — submit still gated
            IntradayOperationMode.MANUAL_APPROVAL,
            IntradayOperationMode.PAPER_AUTOMATED,
        }

    @property
    def can_approve(self) -> bool:
        return self.mode in {
            IntradayOperationMode.MANUAL_APPROVAL,
            IntradayOperationMode.PAPER_AUTOMATED,
        }

    @property
    def can_submit(self) -> bool:
        return self.mode in {
            IntradayOperationMode.MANUAL_APPROVAL,
            IntradayOperationMode.PAPER_AUTOMATED,
        }

    @property
    def intents_are_draft_only(self) -> bool:
        return self.mode in {
            IntradayOperationMode.OBSERVE_ONLY,
            IntradayOperationMode.ANALYZE_ONLY,
        }


def resolve_mode(settings: Settings, *, emergency: bool = False, paused: bool = False) -> IntradayOperationMode:
    if emergency:
        return IntradayOperationMode.EMERGENCY_STOP
    if paused:
        return IntradayOperationMode.PAUSED
    raw = (settings.intraday_operation_mode or "OBSERVE_ONLY").upper()
    try:
        return IntradayOperationMode(raw)
    except ValueError:
        return IntradayOperationMode.OBSERVE_ONLY
