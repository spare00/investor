"""Closing window + overnight risk review (intent-producing, not direct broker)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.intraday.events import IntradayEventBus
from app.intraday.modes import ModeCapabilities, resolve_mode
from app.models import ClosingReview, OvernightReview, PositionLifecycle
from app.workflow.closing import ClosingPolicyEngine
from app.workflow.states import ClosingPolicy
from sqlalchemy import select


class ClosingService:
    def __init__(self, session: AsyncSession, *, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.bus = IntradayEventBus(session, settings=self.settings)
        self.engine = ClosingPolicyEngine()

    async def run_closing(self, *, in_closing_window: bool = True) -> dict[str, Any]:
        mode = resolve_mode(self.settings)
        caps = ModeCapabilities(mode)
        policy = ClosingPolicy(self.settings.default_closing_policy)
        lifecycles = list(
            (
                await self.session.execute(
                    select(PositionLifecycle).where(
                        PositionLifecycle.status.in_(["OPEN", "ADDING", "REDUCING", "PENDING_CLOSE"])
                    )
                )
            )
            .scalars()
            .all()
        )
        positions = [
            {
                "symbol": p.symbol,
                "quantity": p.quantity,
                "is_intraday_only": not p.overnight_allowed,
            }
            for p in lifecycles
        ]
        decision = self.engine.decide(
            as_of=datetime.now(UTC),
            positions=positions,
            policy=policy,
            intraday_symbols={p.symbol for p in lifecycles if not p.overnight_allowed},
        )
        notes = list(decision.notes)
        if in_closing_window:
            await self.bus.publish(
                event_type="CLOSING_WINDOW_ENTERED",
                source="closing_service",
                deduplication_key=f"closing:{datetime.now(UTC).date().isoformat()}",
                requires_risk_review=True,
                importance="high",
                bypass_cooldown=True,
            )
            notes.append("new_entries_blocked" if not self.settings.allow_new_positions_in_closing_window else "entries_allowed")
            if self.settings.cancel_entry_orders_at_closing_window:
                notes.append("entry_orders_should_cancel")

        # Produce exit intent drafts (not broker submits) when mode allows
        intent_drafts: list[dict[str, Any]] = []
        for plan in decision.plans:
            if plan.action in {"close", "reduce"} and caps.can_create_intent:
                intent_drafts.append(
                    {
                        "symbol": plan.symbol,
                        "action": plan.action,
                        "quantity": plan.quantity if plan.action == "close" else plan.quantity * 0.5,
                        "rationale": plan.rationale,
                        "draft_only": caps.intents_are_draft_only or not caps.can_submit,
                    }
                )
                for lc in lifecycles:
                    if lc.symbol == plan.symbol and plan.action == "close":
                        lc.status = "PENDING_CLOSE"

        review = ClosingReview(
            id=uuid4(),
            policy=policy.value,
            payload=decision.to_dict(),
            intent_drafts=intent_drafts,
            notes=notes,
        )
        self.session.add(review)
        await self.session.flush()
        return {
            "review_id": str(review.id),
            "policy": policy.value,
            "plans": decision.to_dict()["plans"],
            "intent_drafts": intent_drafts,
            "broker_orders_submitted": False,
            "notes": notes,
        }

    async def overnight_review(
        self,
        *,
        earnings: bool = False,
        economic_event: bool = False,
        gap_risk_high: bool = False,
        next_session_holiday: bool = False,
    ) -> dict[str, Any]:
        lifecycles = list(
            (
                await self.session.execute(
                    select(PositionLifecycle).where(
                        PositionLifecycle.status.in_(["OPEN", "ADDING", "REDUCING", "PENDING_CLOSE"])
                    )
                )
            )
            .scalars()
            .all()
        )
        results: list[dict[str, Any]] = []
        for lc in lifecycles:
            status = "OVERNIGHT_APPROVED"
            reasons: list[str] = []
            if not lc.overnight_allowed:
                status = "CLOSE_BEFORE_MARKET_CLOSE"
                reasons.append("overnight_not_allowed")
            if earnings or economic_event:
                status = "MANUAL_REVIEW_REQUIRED" if status == "OVERNIGHT_APPROVED" else status
                reasons.append("event_risk")
            if gap_risk_high:
                status = "OVERNIGHT_APPROVED_WITH_REDUCTION" if status.startswith("OVERNIGHT") else status
                reasons.append("gap_risk")
            if next_session_holiday:
                status = "MANUAL_REVIEW_REQUIRED"
                reasons.append("holiday_gap")
            if not self.settings.overnight_review_required:
                status = "NO_DATA"
            valid_for = datetime.now(UTC).date().isoformat()
            row = OvernightReview(
                id=uuid4(),
                position_lifecycle_id=lc.id,
                symbol=lc.symbol,
                status=status,
                reasons=reasons,
                valid_for_session_date=valid_for,
                payload={},
            )
            self.session.add(row)
            results.append({"symbol": lc.symbol, "status": status, "reasons": reasons, "valid_for": valid_for})
        await self.session.flush()
        return {"reviews": results}
