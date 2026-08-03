"""CIO / Final Decision Maker agent."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from app.agents.base import BaseAgent, dump_for_prompt
from app.schemas.cio import CIODecision, CIOInput, SymbolActionPlan
from app.schemas.common import (
    AgentName,
    MarketRegime,
    OrderType,
    PortfolioAction,
    PriceZone,
    RiskVerdict,
    SymbolAction,
    TimeHorizon,
    TraceMetadata,
)


class CIOAgent(BaseAgent[CIOInput, CIODecision]):
    name = AgentName.CIO
    prompt_file = "cio_v0.1.0.txt"
    prompt_version = "0.1.0"

    def output_model(self) -> type[CIODecision]:
        return CIODecision

    def build_user_prompt(self, payload: CIOInput) -> str:
        return (
            "Produce final CIODecision JSON. Honor Hard Vetoes. "
            "If risk_approval is false, do not emit risk-increasing actions.\n\n"
            f"{dump_for_prompt(payload)}"
        )

    def fallback_output(self, payload: CIOInput, *, reason: str) -> CIODecision:
        risk_ok = payload.risk.overall_verdict in {
            RiskVerdict.APPROVED,
            RiskVerdict.CONDITIONAL,
            RiskVerdict.SIZE_REDUCED,
        } and not payload.risk.halt_new_trades

        regime = payload.macro.market_regime
        prefer_no = payload.devil.prefer_no_trade or not risk_ok

        symbol_actions: list[SymbolActionPlan] = []
        portfolio_action = PortfolioAction.NO_TRADE
        reason_not = None

        if prefer_no:
            portfolio_action = (
                PortfolioAction.STAY_CASH if payload.portfolio_cash_pct >= 50 else PortfolioAction.HOLD
            )
            reason_not = payload.devil.prefer_no_trade_rationale or "Risk or Devil prefers no trade"
        else:
            # Mild scale-in only on RISK_ON with a liquid index ETF view.
            if regime in {MarketRegime.RISK_ON, MarketRegime.STRONG_RISK_ON}:
                qqq = next(
                    (v for v in payload.quant.symbol_views if v.symbol == "QQQ"),
                    None,
                )
                if qqq and qqq.entry_zone and qqq.stop_or_invalidation:
                    portfolio_action = PortfolioAction.SCALE_IN
                    symbol_actions.append(
                        SymbolActionPlan(
                            symbol="QQQ",
                            action=SymbolAction.SCALE_IN,
                            confidence=int(qqq.probability_estimate * 100),
                            target_position_pct=min(8.0, self.settings.max_position_pct),
                            order_type=OrderType.LIMIT,
                            entry_zone=PriceZone(
                                min=qqq.entry_zone.min, max=qqq.entry_zone.max
                            ),
                            stop_loss=qqq.stop_or_invalidation,
                            take_profit=[],
                            time_horizon=TimeHorizon.INTRADAY,
                            thesis="Fallback CIO: risk-on regime with QQQ trend support",
                            invalidation="Break below stop_or_invalidation",
                            max_holding_time_minutes=180,
                        )
                    )
                else:
                    portfolio_action = PortfolioAction.HOLD
                    reason_not = "No actionable quant entry zone"
            else:
                portfolio_action = PortfolioAction.STAY_CASH
                reason_not = f"Regime {regime.value} not conducive to new risk"

        return CIODecision(
            decision_id=uuid4(),
            timestamp=datetime.now(UTC),
            market_regime=regime,
            portfolio_action=portfolio_action,
            symbol_actions=symbol_actions if risk_ok else [],
            cash_target_pct=max(payload.portfolio_cash_pct, self.settings.min_cash_pct),
            hedge_required=regime in {MarketRegime.RISK_OFF, MarketRegime.STRONG_RISK_OFF},
            risk_approval=risk_ok,
            risk_conditions=list(payload.risk.hard_vetoes),
            reason_not_to_trade=reason_not,
            hard_veto_honored=True,
            trace=TraceMetadata(
                agent_version=self.agent_version,
                prompt_version=self.prompt_version,
                model_name="fallback-rules",
                source_data_timestamp=payload.as_of,
            ),
        )
