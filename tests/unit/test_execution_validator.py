"""Phase 4 tests — trading controls and execution validator."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.execution.safety_controls import TradingControls, TradingState
from app.execution.validation import ExecutionValidator
from app.risk import PortfolioRiskView, PositionRiskView
from app.schemas.cio import CIODecision, SymbolActionPlan
from app.schemas.common import (
    MarketRegime,
    OrderType,
    PortfolioAction,
    PriceZone,
    SymbolAction,
)


NOW = datetime(2026, 8, 3, 14, 0, tzinfo=UTC)


def _buy_decision(*, risk_approval: bool = True) -> CIODecision:
    return CIODecision(
        decision_id=uuid4(),
        timestamp=NOW,
        market_regime=MarketRegime.RISK_ON,
        portfolio_action=PortfolioAction.BUY if risk_approval else PortfolioAction.NO_TRADE,
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
        ]
        if risk_approval
        else [],
        cash_target_pct=50,
        risk_approval=risk_approval,
        hard_veto_honored=True,
    )


def test_emergency_stop_blocks_and_needs_clear() -> None:
    controls = TradingControls()
    assert controls.is_new_order_allowed() is True
    controls.pause("test")
    assert controls.snapshot().state == TradingState.PAUSED
    controls.resume()
    assert controls.is_new_order_allowed() is True

    controls.emergency_stop("panic")
    assert controls.is_new_order_allowed() is False
    # resume must not clear emergency
    controls.resume()
    assert controls.snapshot().state == TradingState.EMERGENCY_STOP
    controls.clear_emergency()
    assert controls.snapshot().state == TradingState.PAUSED
    controls.resume()
    assert controls.is_new_order_allowed() is True


def test_validator_blocks_when_paused() -> None:
    controls = TradingControls()
    controls.pause()
    validator = ExecutionValidator(controls=controls)
    result = validator.validate(
        _buy_decision(),
        portfolio=PortfolioRiskView(
            equity=25_000, cash=25_000, cash_pct=100, gross_exposure_pct=0
        ),
        latest_prices={"QQQ": 100},
        data_quality_score=0.9,
    )
    assert result.approved is False
    assert any("trading_controls" in r for r in result.rejections)


def test_validator_blocks_cio_buy_without_risk_approval() -> None:
    # Construct invalid combo by model_construct to bypass CIO schema guard for attacker sim
    decision = CIODecision.model_construct(
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
    result = ExecutionValidator(controls=TradingControls()).validate(
        decision,
        portfolio=PortfolioRiskView(
            equity=25_000, cash=25_000, cash_pct=100, gross_exposure_pct=0
        ),
        latest_prices={"QQQ": 100},
        data_quality_score=0.9,
    )
    assert result.approved is False
    assert "cio_risk_approval_false" in result.rejections


def test_validator_approves_sized_buy() -> None:
    result = ExecutionValidator(controls=TradingControls()).validate(
        _buy_decision(risk_approval=True),
        portfolio=PortfolioRiskView(
            equity=25_000, cash=25_000, cash_pct=100, gross_exposure_pct=0
        ),
        latest_prices={"QQQ": 100},
        data_quality_score=0.95,
    )
    assert result.approved is True
    assert len(result.intents) == 1
    assert result.intents[0].symbol == "QQQ"
    assert result.intents[0].side == "buy"
    assert result.intents[0].quantity > 0


def test_validator_blocks_duplicate_idempotency() -> None:
    decision = _buy_decision()
    key = f"{decision.decision_id}:QQQ:buy:BUY"
    result = ExecutionValidator(controls=TradingControls()).validate(
        decision,
        portfolio=PortfolioRiskView(
            equity=25_000, cash=25_000, cash_pct=100, gross_exposure_pct=0
        ),
        latest_prices={"QQQ": 100},
        data_quality_score=0.95,
        seen_idempotency_keys={key},
        workflow_id=str(decision.decision_id),
    )
    assert result.approved is False
    assert any("duplicate" in r for r in result.rejections)


def test_validator_sell_requires_position() -> None:
    decision = CIODecision(
        decision_id=uuid4(),
        timestamp=NOW,
        market_regime=MarketRegime.RISK_OFF,
        portfolio_action=PortfolioAction.SELL,
        symbol_actions=[
            SymbolActionPlan(
                symbol="QQQ",
                action=SymbolAction.SELL,
                confidence=60,
                target_position_pct=0,
                thesis="exit",
                invalidation="n/a",
            )
        ],
        cash_target_pct=100,
        risk_approval=True,
    )
    empty = ExecutionValidator(controls=TradingControls()).validate(
        decision,
        portfolio=PortfolioRiskView(
            equity=25_000, cash=25_000, cash_pct=100, gross_exposure_pct=0
        ),
        latest_prices={"QQQ": 100},
        data_quality_score=0.9,
    )
    assert empty.approved is False

    with_pos = ExecutionValidator(controls=TradingControls()).validate(
        decision,
        portfolio=PortfolioRiskView(
            equity=25_000,
            cash=20_000,
            cash_pct=80,
            gross_exposure_pct=20,
            positions=[
                PositionRiskView(
                    symbol="QQQ", quantity=10, market_value=1000, sector="Index", weight_pct=4
                )
            ],
        ),
        latest_prices={"QQQ": 100},
        data_quality_score=0.9,
    )
    assert with_pos.approved is True
    assert with_pos.intents[0].quantity == 10
