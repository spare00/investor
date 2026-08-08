"""Agent-firm execution bridge: CIO Decision → Order Intents → optional paper submit.

Identity: the six agents run the firm. The CIO makes the final trade judgment after
bottom-up analysis. Risk Hard Veto cannot be overridden. Humans are operators and
optional emergency brakes — not the default order approvers.

Live trading remains hard-blocked here.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.core.metrics import ORDERS_BLOCKED
from app.execution.order_manager import OrderManager
from app.execution.service import ExecutionService
from app.execution.validation import ExecutionValidator
from app.market.live_prices import requires_live_market_prices, resolve_execution_prices
from app.risk import PortfolioRiskView, PositionRiskView
from app.schemas.cio import CIODecision
from app.schemas.risk_manager import PortfolioStateInput

logger = get_logger(__name__)


def portfolio_to_risk_view(portfolio: PortfolioStateInput) -> PortfolioRiskView:
    return PortfolioRiskView(
        equity=portfolio.equity,
        cash=portfolio.cash,
        cash_pct=portfolio.cash_pct,
        gross_exposure_pct=portfolio.gross_exposure_pct,
        positions=[
            PositionRiskView(
                symbol=p.symbol,
                quantity=p.quantity,
                market_value=p.market_value,
                sector=p.sector,
                weight_pct=p.weight_pct,
            )
            for p in portfolio.positions
        ],
        daily_pnl_pct=portfolio.daily_pnl_pct,
        drawdown_pct=portfolio.drawdown_pct,
        consecutive_losses=portfolio.consecutive_losses,
        trading_halted=portfolio.trading_halted,
        cooldown_until=portfolio.cooldown_until,
    )


def paper_auto_submit_allowed(settings: Settings) -> bool:
    """True when the firm may submit paper orders without a human click."""
    if settings.enable_live_trading:
        return False
    if settings.broker_environment.lower() == "live":
        return False
    if settings.trading_mode.value == "live" and not settings.is_live_trading_allowed():
        return False
    if settings.trading_mode.value not in {"paper", "simulation"}:
        # Live mode must never auto-submit from this bridge
        return False
    if not settings.enable_broker_orders:
        return False
    if not settings.enable_automated_execution:
        return False
    if settings.require_manual_order_approval:
        return False
    return True


async def materialize_cio_decision(
    session: AsyncSession,
    decision: CIODecision,
    *,
    portfolio: PortfolioStateInput | PortfolioRiskView,
    latest_prices: dict[str, float],
    data_quality_score: float = 1.0,
    workflow_id: UUID | None = None,
    settings: Settings | None = None,
    create_intents: bool = True,
    allow_submit: bool = True,
    entry_universe: set[str] | None = None,
    horizon_by_symbol: dict[str, str] | None = None,
    block_new_entries: bool = False,
    market_session_clear: bool = True,
) -> dict[str, Any]:
    """
    Materialize a CIO decision into order intents and optionally auto-submit paper.

    Always fail-closed on Live. Intent creation is the firm acting; submit is gated
    by paper automation flags. Manual approval (when enabled) parks intents for an
    operator brake — it is not the primary trading model.

    When the live/broker path is enabled, prices are always refreshed from Alpaca at
    materialize time — collection leftovers and stub quotes are never used to size
    or submit orders.
    """
    cfg = settings or get_settings()
    notes: list[str] = []
    risk_view = (
        portfolio
        if isinstance(portfolio, PortfolioRiskView)
        else portfolio_to_risk_view(portfolio)
    )

    needed = {str(a.symbol).upper() for a in (decision.symbol_actions or []) if a.symbol}
    needed |= {str(k).upper() for k in (latest_prices or {})}
    prices, price_notes = await resolve_execution_prices(
        needed, candidate_prices=latest_prices, settings=cfg
    )
    notes.extend(price_notes)
    if requires_live_market_prices(cfg) and not prices:
        notes.append("orders_blocked_live_prices_unavailable")
        ORDERS_BLOCKED.labels(reason="live_prices_unavailable").inc()
        logger.error(
            "materialize_blocked_no_live_prices",
            workflow_id=str(workflow_id) if workflow_id else None,
            symbols=sorted(needed)[:20],
        )
        return {
            "validation_approved": False,
            "validation_rejections": ["live_prices_unavailable"],
            "intent_ids": [],
            "intent_count": 0,
            "broker_orders_submitted": False,
            "orders_submitted": 0,
            "paper_auto_submit_allowed": paper_auto_submit_allowed(cfg),
            "notes": notes,
            "actor": "cio_bottom_up",
            "live_trading_blocked": True,
            "prices_used": {},
        }

    seen_keys = await OrderManager(session, settings=cfg).seen_idempotency_keys()
    validation = ExecutionValidator(settings=cfg).validate(
        decision,
        portfolio=risk_view,
        latest_prices=prices,
        data_quality_score=data_quality_score,
        market_session_clear=market_session_clear,
        broker_data_consistent=True,
        workflow_id=str(workflow_id) if workflow_id else None,
        seen_idempotency_keys=seen_keys,
        entry_universe=entry_universe,
        horizon_by_symbol=horizon_by_symbol,
        block_new_entries=block_new_entries,
    )

    intent_ids: list[str] = []
    if create_intents and validation.intents:
        try:
            intents = await ExecutionService(session, settings=cfg).build_intents_from_decision(
                decision,
                portfolio=risk_view,
                latest_prices=prices,
                data_quality_score=data_quality_score,
                workflow_id=workflow_id,
                horizon_by_symbol=horizon_by_symbol,
                entry_universe=entry_universe,
                block_new_entries=block_new_entries,
                market_session_clear=market_session_clear,
            )
            intent_ids = [str(i.id) for i in intents]
            notes.append(f"order_intents_created={len(intents)}")
        except Exception as exc:  # noqa: BLE001
            notes.append(f"intent_build_failed:{exc}")
            logger.exception("firm_intent_build_failed", workflow_id=str(workflow_id))

    orders_submitted = 0
    auto = (
        allow_submit
        and paper_auto_submit_allowed(cfg)
        and validation.approved
        and bool(validation.intents)
    )
    if auto:
        try:
            orders = await OrderManager(session, settings=cfg).submit_validated_intents(
                validation,
                decision_id=decision.decision_id,
                workflow_id=workflow_id,
            )
            orders_submitted = len(orders)
            notes.append(f"orders_submitted={orders_submitted}")
            try:
                from app.execution.position_manager import PositionManager

                await PositionManager(session, settings=cfg).sync_from_broker()
                notes.append("positions_synced")
            except Exception as exc:  # noqa: BLE001
                notes.append(f"position_sync_failed:{exc}")
        except Exception as exc:  # noqa: BLE001
            notes.append(f"order_submit_failed:{exc}")
            logger.exception("firm_order_submit_failed", workflow_id=str(workflow_id))
    elif validation.approved and validation.intents:
        if not cfg.enable_broker_orders:
            notes.append("orders_skipped_enable_broker_orders_false")
            ORDERS_BLOCKED.labels(reason="broker_orders_disabled").inc()
        elif cfg.require_manual_order_approval:
            notes.append("intents_awaiting_optional_manual_brake")
            ORDERS_BLOCKED.labels(reason="manual_approval_brake").inc()
        elif not cfg.enable_automated_execution:
            notes.append("orders_skipped_enable_automated_execution_false")
            ORDERS_BLOCKED.labels(reason="automated_execution_disabled").inc()
        elif not allow_submit:
            notes.append("submit_not_allowed_in_context")
        else:
            notes.append("submit_skipped_mode")
    elif validation.rejections:
        notes.append(f"validation_rejected:{','.join(validation.rejections[:5])}")
        for reason in validation.rejections[:10]:
            ORDERS_BLOCKED.labels(reason=reason.split(":")[0][:64]).inc()

    return {
        "validation_approved": validation.approved,
        "validation_rejections": list(validation.rejections),
        "intent_ids": intent_ids,
        "intent_count": len(intent_ids),
        "broker_orders_submitted": orders_submitted > 0,
        "orders_submitted": orders_submitted,
        "paper_auto_submit_allowed": paper_auto_submit_allowed(cfg),
        "notes": notes,
        "actor": "cio_bottom_up",
        "live_trading_blocked": True,
        "prices_used": prices,
    }
