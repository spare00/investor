"""Deterministic Execution Validator — last gate before broker submit."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.core.config import Settings, get_settings
from app.execution.safety_controls import TradingControls, trading_controls
from app.risk import DeterministicRiskEngine, PortfolioRiskView, TradeIntent, limits_from_settings
from app.schemas.cio import CIODecision, SymbolActionPlan
from app.schemas.common import PortfolioAction, SymbolAction


ENTRY_ACTIONS = {
    SymbolAction.STRONG_BUY,
    SymbolAction.BUY,
    SymbolAction.SCALE_IN,
    SymbolAction.HEDGE,
}

RISK_INCREASING_PORTFOLIO = {
    PortfolioAction.STRONG_BUY,
    PortfolioAction.BUY,
    PortfolioAction.SCALE_IN,
    PortfolioAction.HEDGE,
}


@dataclass(slots=True)
class ValidatedOrderIntent:
    symbol: str
    side: str
    quantity: float
    order_type: str
    limit_price: float | None
    stop_price: float | None
    idempotency_key: str
    decision_id: str
    thesis: str


@dataclass(slots=True)
class ExecutionValidationResult:
    approved: bool
    intents: list[ValidatedOrderIntent] = field(default_factory=list)
    rejections: list[str] = field(default_factory=list)
    validated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class ExecutionValidator:
    """
    CIO output → order intents, only if:
    - trading controls allow new orders
    - risk_approval is true for risk-increasing actions
    - Hard Veto re-check passes on latest portfolio/prices
    - allowlist required for new entries only (exits allowed for any held symbol)
    - stops required for entries
    """

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        engine: DeterministicRiskEngine | None = None,
        controls: TradingControls | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.engine = engine or DeterministicRiskEngine(limits_from_settings(self.settings))
        self.controls = controls or trading_controls

    def validate(
        self,
        decision: CIODecision,
        *,
        portfolio: PortfolioRiskView,
        latest_prices: dict[str, float],
        data_quality_score: float,
        market_session_clear: bool = True,
        broker_data_consistent: bool = True,
        seen_idempotency_keys: set[str] | None = None,
        workflow_id: str | None = None,
        entry_universe: set[str] | None = None,
        horizon_by_symbol: dict[str, str] | None = None,
        block_new_entries: bool = False,
    ) -> ExecutionValidationResult:
        rejections: list[str] = []

        if not self.controls.is_new_order_allowed():
            snap = self.controls.snapshot()
            return ExecutionValidationResult(
                approved=False,
                rejections=[f"trading_controls:{snap.state.value}:{snap.reason}"],
            )

        if decision.portfolio_action in RISK_INCREASING_PORTFOLIO and not decision.risk_approval:
            return ExecutionValidationResult(
                approved=False,
                rejections=["cio_risk_approval_false"],
            )

        if decision.portfolio_action in {
            PortfolioAction.NO_TRADE,
            PortfolioAction.HOLD,
        }:
            # Portfolio-level hold / no-trade means no broker submits, even if
            # symbol_actions contain review notes.
            return ExecutionValidationResult(approved=True, intents=[], rejections=[])

        if decision.portfolio_action == PortfolioAction.STAY_CASH and not decision.symbol_actions:
            return ExecutionValidationResult(approved=True, intents=[], rejections=[])

        intents: list[ValidatedOrderIntent] = []
        allowlist = entry_universe if entry_universe is not None else self.settings.allowlist_set()
        horizons = horizon_by_symbol or {}
        seen = seen_idempotency_keys or set()
        held_syms = [p.symbol for p in portfolio.positions if p.quantity]

        for plan in decision.symbol_actions:
            if plan.action in {SymbolAction.HOLD, SymbolAction.NO_TRADE, SymbolAction.STAY_CASH}:
                continue  # informational only — not an order, not a rejection
            result = self._validate_plan(
                decision,
                plan,
                portfolio=portfolio,
                latest_prices=latest_prices,
                data_quality_score=data_quality_score,
                market_session_clear=market_session_clear,
                broker_data_consistent=broker_data_consistent,
                seen=seen,
                workflow_id=workflow_id,
                allowlist=allowlist,
                horizon_by_symbol=horizons,
                held_symbols=held_syms,
                block_new_entries=block_new_entries,
            )
            if result is None:
                continue  # skipped (e.g. new entry in closing window)
            if isinstance(result, str):
                rejections.append(result)
            else:
                intents.append(result)
                seen.add(result.idempotency_key)

        # Fail closed: any rejection blocks the whole batch.
        if rejections:
            return ExecutionValidationResult(approved=False, intents=[], rejections=rejections)
        return ExecutionValidationResult(approved=True, intents=intents, rejections=[])

    def _validate_plan(
        self,
        decision: CIODecision,
        plan: SymbolActionPlan,
        *,
        portfolio: PortfolioRiskView,
        latest_prices: dict[str, float],
        data_quality_score: float,
        market_session_clear: bool,
        broker_data_consistent: bool,
        seen: set[str],
        workflow_id: str | None,
        allowlist: set[str],
        horizon_by_symbol: dict[str, str] | None = None,
        held_symbols: list[str] | None = None,
        block_new_entries: bool = False,
    ) -> ValidatedOrderIntent | str | None:
        from app.universe.caps import horizon_cap_violation
        from app.universe.horizons import policy_for

        symbol = plan.symbol.upper()
        if plan.action in ENTRY_ACTIONS and symbol not in allowlist:
            return f"{symbol}:not_in_allowlist"

        if plan.action in ENTRY_ACTIONS and block_new_entries:
            # Skip entries so exit intents in the same batch can still approve.
            return None

        if plan.action in ENTRY_ACTIONS and not decision.risk_approval:
            return f"{symbol}:entry_without_risk_approval"

        if plan.action in ENTRY_ACTIONS and plan.stop_loss is None and not plan.invalidation.strip():
            return f"{symbol}:missing_stop_or_invalidation"

        horizons = horizon_by_symbol or {}
        held = held_symbols or [p.symbol for p in portfolio.positions if p.quantity]
        if plan.action in ENTRY_ACTIONS:
            cap = horizon_cap_violation(
                symbol=symbol,
                horizon_by_symbol=horizons,
                held_symbols=held,
                is_new_symbol=True,
            )
            if cap:
                return cap

        price = latest_prices.get(symbol)
        if price is None or price <= 0:
            return f"{symbol}:missing_latest_price"

        if plan.action in {SymbolAction.HOLD, SymbolAction.NO_TRADE, SymbolAction.STAY_CASH}:
            return f"{symbol}:non_executable_action"

        existing = next((p for p in portfolio.positions if p.symbol.upper() == symbol), None)
        sector = existing.sector if existing else "Unknown"

        # Size from risk engine using stop distance when opening/adding risk.
        qty = 0.0
        if plan.action in ENTRY_ACTIONS:
            side = "buy"
            if plan.stop_loss is None:
                return f"{symbol}:buy_requires_numeric_stop_for_sizing"
            risk_mult = 1.0
            hz = horizons.get(symbol)
            if hz:
                try:
                    risk_mult = float(policy_for(hz).risk_per_trade_mult)
                except ValueError:
                    risk_mult = 1.0
            sizing = self.engine.position_size(
                equity=portfolio.equity,
                entry_price=price,
                stop_price=plan.stop_loss,
                risk_mult=risk_mult,
            )
            qty = float(sizing.shares)
            if qty <= 0:
                return f"{symbol}:sized_to_zero"
            intent = TradeIntent(
                symbol=symbol,
                side="buy",
                quantity=qty,
                entry_price=price,
                stop_loss=plan.stop_loss,
                invalidation=plan.invalidation,
                sector=sector,
                idempotency_key=f"{decision.decision_id}:{symbol}:buy",
            )
            pre = self.engine.evaluate_pretrade(
                portfolio,
                intent,
                allowlist=allowlist,
                data_quality_score=data_quality_score,
                market_session_clear=market_session_clear,
                broker_data_consistent=broker_data_consistent,
                seen_idempotency_keys=seen,
            )
            if not pre.approved:
                return f"{symbol}:hard_veto:{','.join(pre.hard_vetoes)}"
            qty = float(pre.adjusted_quantity or 0)
        else:
            # Reduce / flatten existing exposure (long → sell, short → buy to cover).
            if existing is None or existing.quantity == 0:
                return f"{symbol}:no_position_to_exit"
            held = abs(float(existing.quantity))
            if plan.action in {SymbolAction.PARTIAL_SELL, SymbolAction.REDUCE}:
                qty = max(1.0, held * 0.5)
            else:
                qty = held
            side = "sell" if existing.quantity > 0 else "buy"

        limit_price = None
        if plan.entry_zone is not None:
            limit_price = (plan.entry_zone.min + plan.entry_zone.max) / 2.0

        order_type = plan.order_type.value
        # Exits: prefer market so stub/offline quotes cannot park unfillable limits.
        if plan.action not in ENTRY_ACTIONS:
            order_type = "market"
            limit_price = None
        elif order_type in {"limit", "stop_limit"} and limit_price is None:
            if price and price > 0:
                limit_price = float(price)
            else:
                return f"{symbol}:limit_order_missing_price"

        key = f"{workflow_id or decision.decision_id}:{symbol}:{side}:{plan.action.value}"
        if key in seen:
            return f"{symbol}:duplicate_idempotency_key"

        return ValidatedOrderIntent(
            symbol=symbol,
            side=side,
            quantity=qty,
            order_type=order_type,
            limit_price=limit_price,
            stop_price=plan.stop_loss,
            idempotency_key=key,
            decision_id=str(decision.decision_id),
            thesis=plan.thesis,
        )
