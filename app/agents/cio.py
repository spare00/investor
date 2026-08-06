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
    prompt_file = "system_v1.md"
    prompt_version = "1.0.0"

    def output_model(self) -> type[CIODecision]:
        return CIODecision

    def build_user_prompt(self, payload: CIOInput) -> str:
        held = ", ".join(f"{p.symbol}:{p.quantity}" for p in payload.positions) or "none"
        return (
            "Produce final CIODecision JSON. Honor Hard Vetoes. "
            "If risk_approval is false, do not emit risk-increasing actions. "
            "Review EVERY open position and emit HOLD/REDUCE/PARTIAL_SELL/SELL as needed "
            "(including symbols not on watchlist). New entries only from allowlist/watchlist. "
            "Match time_horizon to watchlist horizon when present "
            "(scalp/day→intraday, short→swing, medium→position). "
            "Respect per-horizon book capacity — prefer highest-conviction names.\n"
            f"Open positions: {held}\n\n"
            f"{dump_for_prompt(payload)}"
        )

    def _close_plans(self, payload: CIOInput, *, thesis: str) -> list[SymbolActionPlan]:
        plans: list[SymbolActionPlan] = []
        for pos in payload.positions:
            if pos.quantity == 0:
                continue
            plans.append(
                SymbolActionPlan(
                    symbol=pos.symbol.upper(),
                    action=SymbolAction.SELL,
                    confidence=65,
                    target_position_pct=0.0,
                    order_type=OrderType.MARKET,
                    thesis=thesis,
                    invalidation="n/a",
                    time_horizon=TimeHorizon.INTRADAY,
                )
            )
        return plans

    def _hold_plans(self, payload: CIOInput) -> list[SymbolActionPlan]:
        plans: list[SymbolActionPlan] = []
        for pos in payload.positions:
            if pos.quantity == 0:
                continue
            plans.append(
                SymbolActionPlan(
                    symbol=pos.symbol.upper(),
                    action=SymbolAction.HOLD,
                    confidence=55,
                    target_position_pct=abs(pos.weight_pct),
                    order_type=OrderType.LIMIT,
                    thesis="Fallback CIO: maintain existing position under review",
                    invalidation="n/a",
                    time_horizon=TimeHorizon.INTRADAY,
                )
            )
        return plans

    def fallback_output(self, payload: CIOInput, *, reason: str) -> CIODecision:
        risk_ok = payload.risk.overall_verdict in {
            RiskVerdict.APPROVED,
            RiskVerdict.CONDITIONAL,
            RiskVerdict.SIZE_REDUCED,
        } and not payload.risk.halt_new_trades

        regime = payload.macro.market_regime
        flat = not payload.positions or all(abs(p.quantity or 0) < 1e-9 for p in payload.positions)
        # Soft Devil prefer_no must not permanently park a flat book in RISK_ON —
        # opportunity cost dominates when risk already approved.
        soft_prefer_no = bool(payload.devil.prefer_no_trade)
        if soft_prefer_no and risk_ok and flat and regime in {
            MarketRegime.RISK_ON,
            MarketRegime.STRONG_RISK_ON,
        }:
            soft_prefer_no = False
        prefer_no = soft_prefer_no or not risk_ok

        symbol_actions: list[SymbolActionPlan] = []
        portfolio_action = PortfolioAction.NO_TRADE
        reason_not = None

        if prefer_no:
            if not risk_ok:
                portfolio_action = PortfolioAction.STAY_CASH
                symbol_actions = self._close_plans(
                    payload, thesis="Fallback CIO: risk blocked — flatten existing positions"
                )
            else:
                portfolio_action = PortfolioAction.HOLD
                symbol_actions = self._hold_plans(payload)
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
                    symbol_actions = self._hold_plans(payload)
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
                    symbol_actions = self._hold_plans(payload)
                    reason_not = "No actionable quant entry zone"
            else:
                portfolio_action = PortfolioAction.HOLD
                symbol_actions = self._hold_plans(payload)
                reason_not = f"Regime {regime.value} not conducive to new risk"

        return CIODecision(
            decision_id=uuid4(),
            timestamp=datetime.now(UTC),
            market_regime=regime,
            portfolio_action=portfolio_action,
            symbol_actions=symbol_actions,
            # Paper accounts with shorts can report cash_pct > 100; clamp to schema.
            cash_target_pct=min(
                100.0, max(payload.portfolio_cash_pct, self.settings.min_cash_pct)
            ),
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
