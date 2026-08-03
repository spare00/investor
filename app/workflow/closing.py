"""Closing policy interface (no broker calls)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.workflow.states import ClosingPolicy


@dataclass(slots=True)
class PositionClosePlan:
    symbol: str
    quantity: float
    is_intraday_only: bool
    action: str  # keep | reduce | close
    rationale: str


@dataclass(slots=True)
class ClosingDecision:
    policy: ClosingPolicy
    as_of: datetime
    plans: list[PositionClosePlan] = field(default_factory=list)
    broker_orders_allowed: bool = False
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy": self.policy.value,
            "as_of": self.as_of.isoformat(),
            "broker_orders_allowed": False,
            "plans": [
                {
                    "symbol": p.symbol,
                    "quantity": p.quantity,
                    "is_intraday_only": p.is_intraday_only,
                    "action": p.action,
                    "rationale": p.rationale,
                }
                for p in self.plans
            ],
            "notes": self.notes,
        }


class ClosingPolicyEngine:
    """Decide end-of-day handling without submitting broker orders."""

    def decide(
        self,
        *,
        as_of: datetime,
        positions: list[dict[str, Any]],
        policy: ClosingPolicy = ClosingPolicy.CLOSE_INTRADAY_ONLY,
        intraday_symbols: set[str] | None = None,
    ) -> ClosingDecision:
        intraday_symbols = {s.upper() for s in (intraday_symbols or set())}
        plans: list[PositionClosePlan] = []
        for raw in positions:
            symbol = str(raw.get("symbol", "")).upper()
            qty = float(raw.get("quantity") or 0)
            if not symbol or qty == 0:
                continue
            is_intra = symbol in intraday_symbols or bool(raw.get("is_intraday_only"))
            if policy == ClosingPolicy.KEEP_OVERNIGHT:
                action, rationale = "keep", "policy KEEP_OVERNIGHT"
            elif policy == ClosingPolicy.CLOSE_ALL:
                action, rationale = "close", "policy CLOSE_ALL (not executed in Phase 3)"
            elif policy == ClosingPolicy.CLOSE_INTRADAY_ONLY:
                if is_intra:
                    action, rationale = "close", "intraday-only position"
                else:
                    action, rationale = "keep", "swing/position holding"
            elif policy == ClosingPolicy.REDUCE_RISK:
                action, rationale = "reduce", "policy REDUCE_RISK (not executed in Phase 3)"
            else:
                action, rationale = "keep", "MANUAL_REVIEW"
            plans.append(
                PositionClosePlan(
                    symbol=symbol,
                    quantity=qty,
                    is_intraday_only=is_intra,
                    action=action,
                    rationale=rationale,
                )
            )
        return ClosingDecision(
            policy=policy,
            as_of=as_of,
            plans=plans,
            broker_orders_allowed=False,
            notes=["Phase 3: closing plans are advisory only; broker orders disabled"],
        )
