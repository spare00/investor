"""CIO / Final Decision Maker agent."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from app.agents.base import BaseAgent
from app.agents.briefs import cio_brief
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
    prompt_version = "2.2.0"

    def output_model(self) -> type[CIODecision]:
        return CIODecision

    def build_user_prompt(self, payload: CIOInput) -> str:
        return cio_brief(payload)

    def _scoped_positions(self, payload: CIOInput) -> list:
        allow = {str(s).upper() for s in payload.allowlist if s}
        watch = {
            str(row.get("symbol") or "").upper()
            for row in (payload.watchlist or [])
            if isinstance(row, dict) and row.get("symbol")
        }
        scoped = allow | watch
        if not scoped:
            return list(payload.positions or [])
        return [p for p in payload.positions if str(p.symbol).upper() in scoped]

    def _close_plans(self, payload: CIOInput, *, thesis: str) -> list[SymbolActionPlan]:
        plans: list[SymbolActionPlan] = []
        for pos in self._scoped_positions(payload):
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

    def _plan_for_position(
        self,
        pos,
        *,
        action: SymbolAction,
        thesis: str,
        horizon: str,
        target_pct: float | None = None,
        stop: float | None = None,
        confidence: int = 55,
    ) -> SymbolActionPlan:
        from app.universe.book_strategy import policy_time_horizon

        return SymbolActionPlan(
            symbol=pos.symbol.upper(),
            action=action,
            confidence=confidence,
            target_position_pct=abs(pos.weight_pct) if target_pct is None else target_pct,
            order_type=OrderType.MARKET if action == SymbolAction.SELL else OrderType.LIMIT,
            stop_loss=stop,
            thesis=thesis,
            invalidation="n/a" if stop is None else f"Stop {stop}",
            time_horizon=policy_time_horizon(horizon),
        )

    def fallback_output(self, payload: CIOInput, *, reason: str) -> CIODecision:
        from app.universe.book_strategy import (
            exit_action,
            horizon_for_symbol,
            playbook_for,
            portfolio_action_from_symbol_actions,
            should_propose_entry,
            symbol_action_for_exit,
        )
        from app.universe.caps import horizon_cap_violation

        risk_ok = payload.risk.overall_verdict in {
            RiskVerdict.APPROVED,
            RiskVerdict.CONDITIONAL,
            RiskVerdict.SIZE_REDUCED,
        } and not payload.risk.halt_new_trades

        regime = payload.macro.market_regime
        positions = self._scoped_positions(payload)
        flat = not positions or all(abs(p.quantity or 0) < 1e-9 for p in positions)
        soft_prefer_no = bool(payload.devil.prefer_no_trade)
        if soft_prefer_no and risk_ok and flat and regime in {
            MarketRegime.RISK_ON,
            MarketRegime.STRONG_RISK_ON,
        }:
            soft_prefer_no = False
        prefer_no = soft_prefer_no or not risk_ok

        views = {str(v.symbol).upper(): v for v in payload.quant.symbol_views if v.symbol}
        watch = payload.watchlist or []

        symbol_actions: list[SymbolActionPlan] = []
        reason_not = None

        if prefer_no:
            if not risk_ok:
                portfolio_action = PortfolioAction.STAY_CASH
                symbol_actions = self._close_plans(
                    payload, thesis="Fallback CIO: risk blocked — flatten existing positions"
                )
            else:
                for pos in positions:
                    if abs(pos.quantity or 0) < 1e-9:
                        continue
                    hz = horizon_for_symbol(pos.symbol, watch)
                    book = playbook_for(hz)
                    label = book.label_ko if book else hz
                    symbol_actions.append(
                        self._plan_for_position(
                            pos,
                            action=SymbolAction.HOLD,
                            thesis=f"{label}: hold — devil/risk prefers no new risk",
                            horizon=hz,
                        )
                    )
                portfolio_action = PortfolioAction.HOLD if symbol_actions else PortfolioAction.NO_TRADE
            reason_not = payload.devil.prefer_no_trade_rationale or "Risk or Devil prefers no trade"
        else:
            held_syms = [p.symbol.upper() for p in positions if abs(p.quantity or 0) > 1e-9]
            hz_map = {s: horizon_for_symbol(s, watch) for s in held_syms}
            new_by_book: dict[str, int] = {}

            for pos in positions:
                if abs(pos.quantity or 0) < 1e-9:
                    continue
                sym = pos.symbol.upper()
                hz = horizon_for_symbol(sym, watch)
                book = playbook_for(hz)
                label = book.label_ko if book else hz
                view = views.get(sym)
                if view is None or book is None:
                    symbol_actions.append(
                        self._plan_for_position(
                            pos,
                            action=SymbolAction.HOLD,
                            thesis=f"{label}: maintain existing (no book tape)",
                            horizon=hz,
                        )
                    )
                    continue
                decision = exit_action(
                    horizon=hz,
                    trend=view.trend_state,
                    momentum=view.momentum_state,
                    liquidity=view.liquidity_state,
                )
                action = symbol_action_for_exit(decision)
                target = 0.0 if action == SymbolAction.SELL else (
                    abs(pos.weight_pct) * 0.5 if action == SymbolAction.REDUCE else abs(pos.weight_pct)
                )
                symbol_actions.append(
                    self._plan_for_position(
                        pos,
                        action=action,
                        thesis=f"{label}: {decision.value} on {view.trend_state.value}/{view.momentum_state.value}",
                        horizon=hz,
                        target_pct=target,
                        stop=view.stop_or_invalidation if action != SymbolAction.HOLD else None,
                        confidence=int(view.probability_estimate * 100),
                    )
                )

            if risk_ok:
                ranked = sorted(
                    views.values(),
                    key=lambda v: float(v.probability_estimate or 0),
                    reverse=True,
                )
                for view in ranked:
                    sym = view.symbol.upper()
                    if sym in held_syms:
                        continue
                    hz = horizon_for_symbol(sym, watch)
                    book = playbook_for(hz)
                    if book is None:
                        continue
                    if not should_propose_entry(
                        horizon=hz,
                        probability=float(view.probability_estimate or 0),
                        trend=view.trend_state,
                        momentum=view.momentum_state,
                        liquidity=view.liquidity_state,
                        volatility=view.volatility_state,
                        rsi=None,
                        regime=regime,
                    ):
                        continue
                    if view.entry_zone is None or view.stop_or_invalidation is None:
                        continue
                    if new_by_book.get(hz, 0) >= book.max_new_per_cycle:
                        continue
                    cap = horizon_cap_violation(
                        symbol=sym,
                        horizon_by_symbol={**hz_map, sym: hz},
                        held_symbols=held_syms,
                        is_new_symbol=True,
                    )
                    if cap:
                        continue
                    from app.universe.book_strategy import notional_pct_for_risk

                    size = notional_pct_for_risk(
                        horizon=hz,
                        entry=float(view.entry_zone.max + view.entry_zone.min) / 2.0
                        if view.entry_zone
                        else float(view.stop_or_invalidation or 0) * 1.02,
                        stop=float(view.stop_or_invalidation),
                        max_position_pct=float(self.settings.max_position_pct),
                    )
                    symbol_actions.append(
                        SymbolActionPlan(
                            symbol=sym,
                            action=SymbolAction.SCALE_IN,
                            confidence=int(view.probability_estimate * 100),
                            target_position_pct=size,
                            order_type=OrderType.LIMIT,
                            entry_zone=PriceZone(
                                min=view.entry_zone.min, max=view.entry_zone.max
                            ),
                            stop_loss=view.stop_or_invalidation,
                            take_profit=[],
                            time_horizon=book.cio_time_horizon,
                            thesis=f"{book.label_ko}: {book.summary}"[:80],
                            invalidation="Break below stop_or_invalidation",
                            max_holding_time_minutes=None,
                        )
                    )
                    new_by_book[hz] = new_by_book.get(hz, 0) + 1
                    held_syms.append(sym)
                    hz_map[sym] = hz

            portfolio_action = portfolio_action_from_symbol_actions(symbol_actions)
            if not symbol_actions:
                portfolio_action = PortfolioAction.NO_TRADE
                reason_not = "No actionable book setup (scalp/day/short)"
            elif not any(
                a.action in {SymbolAction.BUY, SymbolAction.STRONG_BUY, SymbolAction.SCALE_IN}
                for a in symbol_actions
            ) and not any(
                a.action in {SymbolAction.SELL, SymbolAction.REDUCE, SymbolAction.PARTIAL_SELL}
                for a in symbol_actions
            ):
                reason_not = "No actionable quant entry zone"

        return CIODecision(
            decision_id=uuid4(),
            timestamp=datetime.now(UTC),
            market_regime=regime,
            portfolio_action=portfolio_action,
            symbol_actions=symbol_actions,
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
