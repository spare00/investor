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
from app.universe.book_strategy import (
    horizon_for_symbol,
    notional_pct_for_risk,
    playbook_for,
    portfolio_action_from_symbol_actions,
    should_propose_entry,
    tape_from_view,
)
from app.universe.caps import horizon_cap_violation

_ENTRY_ACTIONS = {
    SymbolAction.STRONG_BUY,
    SymbolAction.BUY,
    SymbolAction.SCALE_IN,
}

# Points of cash above the floor that still count as drag (paper learning).
_CASH_DRAG_BUFFER_PCT = 10.0


def cash_target_after_plans(
    *,
    current_cash_pct: float,
    min_cash_pct: float,
    plans: list,
) -> float:
    """Cash target falls when we buy. Never below the floor; never freeze at today's pile."""
    deployed = 0.0
    for plan in plans or []:
        action = getattr(plan, "action", None)
        if action in _ENTRY_ACTIONS:
            deployed += float(getattr(plan, "target_position_pct", 0) or 0)
    return round(
        max(float(min_cash_pct), min(100.0, float(current_cash_pct) - deployed)),
        2,
    )


def quant_entry_plans(
    *,
    views: list,
    watchlist: list[dict],
    held_symbols: list[str],
    regime: MarketRegime,
    max_position_pct: float,
    allowlist: list[str] | None = None,
    new_counts: dict[str, int] | None = None,
) -> list[SymbolActionPlan]:
    """Turn Quant views into SCALE_IN plans (playbook + book caps)."""
    allow = {s.upper() for s in (allowlist or []) if s} or None
    held = [s.upper() for s in held_symbols]
    hz_map = {s: horizon_for_symbol(s, watchlist) for s in held}
    new_by_book: dict[str, int] = dict(new_counts or {})
    plans: list[SymbolActionPlan] = []
    ranked = sorted(
        views,
        key=lambda v: float(getattr(v, "probability_estimate", 0) or 0),
        reverse=True,
    )
    for view in ranked:
        sym = str(getattr(view, "symbol", "") or "").upper()
        if not sym or sym in held:
            continue
        if allow is not None and sym not in allow:
            continue
        hz = horizon_for_symbol(sym, watchlist)
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
            **tape_from_view(view),
        ):
            continue
        if view.entry_zone is None or view.stop_or_invalidation is None:
            continue
        if new_by_book.get(hz, 0) >= book.max_new_per_cycle:
            continue
        cap = horizon_cap_violation(
            symbol=sym,
            horizon_by_symbol={**hz_map, sym: hz},
            held_symbols=held,
            is_new_symbol=True,
        )
        if cap:
            continue
        size = notional_pct_for_risk(
            horizon=hz,
            entry=float(view.entry_zone.max + view.entry_zone.min) / 2.0,
            stop=float(view.stop_or_invalidation),
            max_position_pct=max_position_pct,
        )
        plans.append(
            SymbolActionPlan(
                symbol=sym,
                action=SymbolAction.SCALE_IN,
                confidence=int(float(view.probability_estimate or 0) * 100),
                target_position_pct=size,
                order_type=OrderType.LIMIT,
                entry_zone=PriceZone(min=view.entry_zone.min, max=view.entry_zone.max),
                stop_loss=view.stop_or_invalidation,
                take_profit=[],
                time_horizon=book.cio_time_horizon,
                thesis=f"{book.label_ko}: {book.summary}"[:80],
                invalidation="Break below stop_or_invalidation",
                max_holding_time_minutes=None,
            )
        )
        new_by_book[hz] = new_by_book.get(hz, 0) + 1
        held.append(sym)
        hz_map[sym] = hz
    return plans


def ensure_cio_takes_setups(
    decision: CIODecision,
    *,
    quant,
    watchlist: list[dict] | None,
    positions: list,
    allowlist: list[str] | None,
    risk_ok: bool,
    regime: MarketRegime,
    max_position_pct: float,
    enabled: bool,
    cash_pct: float = 100.0,
    min_cash_pct: float = 30.0,
) -> CIODecision:
    """If the book sat idle — or stayed cash-heavy after a token buy — take Quant setups."""
    if not enabled or not risk_ok or not decision.risk_approval:
        return decision
    entering = [p for p in decision.symbol_actions if p.action in _ENTRY_ACTIONS]
    cash_heavy = float(cash_pct) >= float(min_cash_pct) + _CASH_DRAG_BUFFER_PCT
    if entering and not cash_heavy:
        return decision
    watch = watchlist or []
    held = [
        str(p.symbol).upper()
        for p in (positions or [])
        if abs(getattr(p, "quantity", 0) or 0) > 1e-9
    ]
    already = {p.symbol.upper() for p in entering}
    new_counts: dict[str, int] = {}
    for plan in entering:
        hz = horizon_for_symbol(plan.symbol, watch)
        new_counts[hz] = new_counts.get(hz, 0) + 1
    extras = quant_entry_plans(
        views=list(getattr(quant, "symbol_views", None) or []),
        watchlist=watch,
        held_symbols=held + list(already),
        regime=regime,
        max_position_pct=max_position_pct,
        allowlist=allowlist,
        new_counts=new_counts,
    )
    if not extras:
        return decision
    taken = {p.symbol.upper() for p in extras}
    kept = [p for p in decision.symbol_actions if p.symbol.upper() not in taken]
    merged = kept + extras
    return decision.model_copy(
        update={
            "symbol_actions": merged,
            "portfolio_action": portfolio_action_from_symbol_actions(merged),
            "reason_not_to_trade": None,
            "cash_target_pct": cash_target_after_plans(
                current_cash_pct=cash_pct,
                min_cash_pct=min_cash_pct,
                plans=merged,
            ),
        }
    )


def reconcile_nameless_entry(decision: CIODecision, *, has_positions: bool) -> CIODecision:
    """Do not advertise SCALE_IN/BUY when no named entry survived."""
    entering = [p for p in decision.symbol_actions if p.action in _ENTRY_ACTIONS]
    if entering:
        return decision
    if decision.portfolio_action not in {
        PortfolioAction.SCALE_IN,
        PortfolioAction.BUY,
        PortfolioAction.STRONG_BUY,
    }:
        return decision
    fallback = PortfolioAction.HOLD if has_positions else PortfolioAction.NO_TRADE
    return decision.model_copy(
        update={
            "portfolio_action": fallback,
            "reason_not_to_trade": decision.reason_not_to_trade or "no named entry after setups",
        }
    )


class CIOAgent(BaseAgent[CIOInput, CIODecision]):
    name = AgentName.CIO
    prompt_file = "system_v1.md"
    prompt_version = "2.7.0"

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
            symbol_action_for_exit,
        )

        risk_ok = payload.risk.overall_verdict in {
            RiskVerdict.APPROVED,
            RiskVerdict.CONDITIONAL,
            RiskVerdict.SIZE_REDUCED,
        } and not payload.risk.halt_new_trades

        regime = payload.macro.market_regime
        positions = self._scoped_positions(payload)
        # Devil is advisory. Only Hard Veto / halt blocks new risk.
        prefer_no = not risk_ok

        views = {str(v.symbol).upper(): v for v in payload.quant.symbol_views if v.symbol}
        watch = payload.watchlist or []

        symbol_actions: list[SymbolActionPlan] = []
        reason_not = None

        if prefer_no:
            portfolio_action = PortfolioAction.STAY_CASH
            symbol_actions = self._close_plans(
                payload, thesis="Fallback CIO: risk blocked — flatten existing positions"
            )
            reason_not = "Risk blocked — no new entries"
        else:
            held_syms = [p.symbol.upper() for p in positions if abs(p.quantity or 0) > 1e-9]

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
                if action == SymbolAction.SELL:
                    target = 0.0
                elif action == SymbolAction.REDUCE:
                    target = abs(pos.weight_pct) * 0.5
                else:
                    target = abs(pos.weight_pct)
                symbol_actions.append(
                    self._plan_for_position(
                        pos,
                        action=action,
                        thesis=(
                            f"{label}: {decision.value} on "
                            f"{view.trend_state.value}/{view.momentum_state.value}"
                        ),
                        horizon=hz,
                        target_pct=target,
                        stop=view.stop_or_invalidation if action != SymbolAction.HOLD else None,
                        confidence=int(view.probability_estimate * 100),
                    )
                )

            if risk_ok:
                symbol_actions.extend(
                    quant_entry_plans(
                        views=list(views.values()),
                        watchlist=watch,
                        held_symbols=held_syms,
                        regime=regime,
                        max_position_pct=float(self.settings.max_position_pct),
                        allowlist=payload.allowlist,
                    )
                )

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
            cash_target_pct=(
                100.0
                if prefer_no
                else cash_target_after_plans(
                    current_cash_pct=payload.portfolio_cash_pct,
                    min_cash_pct=float(self.settings.min_cash_pct),
                    plans=symbol_actions,
                )
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
