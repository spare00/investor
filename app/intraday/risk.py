"""Dynamic risk revalidation for open positions."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.intraday.events import IntradayEventBus
from app.models import PositionLifecycle, PositionRiskReview


@dataclass(slots=True)
class DynamicRiskResult:
    status: str
    reasons: list[str] = field(default_factory=list)
    review_id: str | None = None


class DynamicRiskRevalidator:
    def __init__(self, session: AsyncSession, *, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.bus = IntradayEventBus(session, settings=self.settings)

    async def evaluate(
        self,
        lifecycle: PositionLifecycle,
        *,
        equity: float,
        daily_pnl_pct: float,
        drawdown_pct: float,
        sector_weight_pct: float = 0.0,
        quote_age_seconds: float | None = 0.0,
        spread_bps: float | None = 10.0,
        economic_event_imminent: bool = False,
        overnight_risk: bool = False,
        broker_drift: bool = False,
        price: float | None = None,
    ) -> DynamicRiskResult:
        reasons: list[str] = []
        status = "RISK_OK"
        qty = abs(float(lifecycle.quantity or 0))
        px = float(price or lifecycle.current_price or lifecycle.average_entry_price or 0)
        weight = (qty * px / equity * 100.0) if equity and px else 0.0

        if broker_drift:
            status = "TRADING_PAUSE_REQUIRED"
            reasons.append("broker_drift")
        if quote_age_seconds is not None and quote_age_seconds > self.settings.latest_quote_max_age_seconds * 20:
            status = "TRADING_PAUSE_REQUIRED"
            reasons.append("data_stale")
        if daily_pnl_pct <= -self.settings.daily_max_loss_pct:
            status = "EMERGENCY_STOP_REQUIRED"
            reasons.append("daily_loss_limit")
        if drawdown_pct >= self.settings.max_drawdown_pct:
            status = "EMERGENCY_STOP_REQUIRED"
            reasons.append("drawdown_limit")
        if weight > self.settings.max_position_pct:
            status = "REDUCE_REQUIRED" if status == "RISK_OK" else status
            reasons.append("symbol_concentration")
        if sector_weight_pct > self.settings.max_sector_pct:
            status = "REDUCE_REQUIRED" if status in {"RISK_OK", "RISK_WARNING"} else status
            reasons.append("sector_concentration")
        if spread_bps is not None and spread_bps > self.settings.max_order_spread_bps:
            status = "RISK_WARNING" if status == "RISK_OK" else status
            reasons.append("spread")
        if economic_event_imminent or overnight_risk:
            status = "RISK_WARNING" if status == "RISK_OK" else status
            reasons.append("event_or_overnight_risk")

        # Stop already breached
        if lifecycle.stop_price and px and px <= float(lifecycle.stop_price):
            status = "EXIT_REQUIRED"
            reasons.append("stop_breached")

        if lifecycle.invalidation_state == "CONFIRMED":
            status = "EXIT_REQUIRED"
            reasons.append("invalidation_confirmed")

        review = PositionRiskReview(
            id=uuid4(),
            position_lifecycle_id=lifecycle.id,
            status=status,
            reasons=reasons,
            payload={
                "weight": weight,
                "daily_pnl_pct": daily_pnl_pct,
                "drawdown_pct": drawdown_pct,
                "as_of": datetime.now(UTC).isoformat(),
            },
        )
        self.session.add(review)
        await self.session.flush()

        if status in {"EXIT_REQUIRED", "EMERGENCY_STOP_REQUIRED", "TRADING_PAUSE_REQUIRED"}:
            critical = status in {"EXIT_REQUIRED", "EMERGENCY_STOP_REQUIRED", "TRADING_PAUSE_REQUIRED"}
            etype = "RISK_LIMIT_BREACH" if critical else "RISK_LIMIT_WARNING"
            await self.bus.publish(
                event_type=etype,
                source="dynamic_risk",
                symbols=[lifecycle.symbol],
                deduplication_key=f"risk:{lifecycle.id}:{status}:{datetime.now(UTC).strftime('%Y%m%d%H%M')}",
                position_id=lifecycle.id,
                requires_risk_review=True,
                bypass_cooldown=critical,
                importance="critical" if critical else "high",
                payload={"status": status, "reasons": reasons},
            )
        return DynamicRiskResult(status=status, reasons=reasons, review_id=str(review.id))
