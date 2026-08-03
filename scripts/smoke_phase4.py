"""Phase 4 smoke runner (pytest-free)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from app.execution.safety_controls import TradingControls, TradingState
from app.execution.validation import ExecutionValidator
from app.risk import PortfolioRiskView
from app.schemas.cio import CIODecision, SymbolActionPlan
from app.schemas.common import (
    MarketRegime,
    OrderType,
    PortfolioAction,
    PriceZone,
    SymbolAction,
)

NOW = datetime(2026, 8, 3, 14, 0, tzinfo=UTC)
errors: list[str] = []


def check(name: str, cond: bool) -> None:
    if not cond:
        errors.append(name)
        print("FAIL", name)
    else:
        print("PASS", name)


def main() -> int:
    controls = TradingControls()
    check("active", controls.is_new_order_allowed())
    controls.emergency_stop("panic")
    check("blocked", not controls.is_new_order_allowed())
    controls.resume()
    check("resume_noop", controls.snapshot().state == TradingState.EMERGENCY_STOP)
    controls.clear_emergency()
    check("paused", controls.snapshot().state == TradingState.PAUSED)
    controls.resume()
    check("resumed", controls.is_new_order_allowed())

    decision = CIODecision(
        decision_id=uuid4(),
        timestamp=NOW,
        market_regime=MarketRegime.RISK_ON,
        portfolio_action=PortfolioAction.BUY,
        symbol_actions=[
            SymbolActionPlan(
                symbol="QQQ",
                action=SymbolAction.BUY,
                confidence=70,
                target_position_pct=5,
                order_type=OrderType.LIMIT,
                entry_zone=PriceZone(min=99, max=101),
                stop_loss=95,
                thesis="test",
                invalidation="break 95",
            )
        ],
        cash_target_pct=50,
        risk_approval=True,
        hard_veto_honored=True,
    )
    portfolio = PortfolioRiskView(equity=25000, cash=25000, cash_pct=100, gross_exposure_pct=0)
    ok = ExecutionValidator(controls=TradingControls()).validate(
        decision, portfolio=portfolio, latest_prices={"QQQ": 100}, data_quality_score=0.95
    )
    check("buy_approved", bool(ok.approved and ok.intents and ok.intents[0].quantity > 0))

    paused = TradingControls()
    paused.pause()
    blocked = ExecutionValidator(controls=paused).validate(
        decision, portfolio=portfolio, latest_prices={"QQQ": 100}, data_quality_score=0.95
    )
    check("pause_blocks", not blocked.approved)

    bypass = CIODecision.model_construct(
        decision_id=uuid4(),
        timestamp=NOW,
        market_regime=MarketRegime.RISK_ON,
        portfolio_action=PortfolioAction.BUY,
        symbol_actions=[
            SymbolActionPlan(
                symbol="QQQ",
                action=SymbolAction.BUY,
                confidence=80,
                target_position_pct=5,
                stop_loss=95,
                thesis="bypass",
                invalidation="x",
                entry_zone=PriceZone(min=99, max=101),
            )
        ],
        cash_target_pct=40,
        hedge_required=False,
        risk_approval=False,
        risk_conditions=[],
        reason_not_to_trade=None,
        hard_veto_honored=False,
    )
    rej = ExecutionValidator(controls=TradingControls()).validate(
        bypass, portfolio=portfolio, latest_prices={"QQQ": 100}, data_quality_score=0.9
    )
    check(
        "no_risk_approval",
        (not rej.approved) and ("cio_risk_approval_false" in rej.rejections),
    )

    print("RESULT", "OK" if not errors else errors)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
