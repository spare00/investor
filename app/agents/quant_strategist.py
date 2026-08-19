"""Quant & Technical Strategist agent."""

from __future__ import annotations

from datetime import UTC, datetime

from app.agents.base import BaseAgent
from app.agents.briefs import quant_brief
from app.schemas.common import (
    AgentName,
    BreadthState,
    LiquidityState,
    MomentumState,
    PriceZone,
    Scenario,
    TraceMetadata,
    TrendState,
    VolatilityState,
)
from app.schemas.quant_strategist import (
    BarSnapshot,
    QuantStrategistInput,
    QuantStrategistOutput,
    SymbolQuantView,
)


def _trend(bar: BarSnapshot, horizon: str = "short") -> TrendState:
    hz = str(horizon or "short").lower()
    if hz == "scalp":
        if bar.sma_20 and bar.last:
            if bar.last > bar.sma_20:
                return TrendState.UP
            if bar.last < bar.sma_20:
                return TrendState.DOWN
        if bar.open and bar.last:
            if bar.last > bar.open:
                return TrendState.UP
            if bar.last < bar.open:
                return TrendState.DOWN
        return TrendState.SIDEWAYS
    if hz == "day":
        if bar.high and bar.low and bar.last:
            typical = (bar.high + bar.low + bar.last) / 3.0
            above = bar.last >= typical
            from_open = bar.open is None or bar.last >= bar.open
            if above and from_open:
                return TrendState.UP
            if (not above) and bar.open is not None and bar.last < bar.open:
                return TrendState.DOWN
        if bar.sma_20 and bar.last:
            if bar.last > bar.sma_20:
                return TrendState.UP
            if bar.last < bar.sma_20:
                return TrendState.DOWN
        return TrendState.SIDEWAYS
    if bar.sma_50 and bar.sma_200 and bar.last:
        if bar.last > bar.sma_50 > bar.sma_200:
            return TrendState.UP
        if bar.last < bar.sma_50 < bar.sma_200:
            return TrendState.DOWN
    return TrendState.SIDEWAYS


def _volume_accelerating(bar: BarSnapshot, *, mult: float = 1.15) -> bool:
    if bar.volume is None or bar.avg_volume_20d is None or bar.avg_volume_20d <= 0:
        return False
    return bar.volume >= mult * bar.avg_volume_20d


def _momentum(bar: BarSnapshot, horizon: str = "short") -> MomentumState:
    hz = str(horizon or "short").lower()
    if hz == "scalp":
        if bar.rsi_14 is not None and bar.rsi_14 >= 80:
            return MomentumState.EXHAUSTED
        if _volume_accelerating(bar) and (
            (bar.open is not None and bar.last > bar.open)
            or (bar.sma_20 is not None and bar.last > bar.sma_20)
        ):
            return MomentumState.ACCELERATING
        if bar.rsi_14 is not None and bar.rsi_14 <= 35:
            return MomentumState.DECELERATING
        return MomentumState.STEADY
    if hz == "day":
        if bar.rsi_14 is not None and bar.rsi_14 >= 80:
            return MomentumState.EXHAUSTED
        if bar.high and bar.low and bar.high > bar.low:
            loc = (bar.last - bar.low) / (bar.high - bar.low)
            if loc >= 0.55 and (bar.open is None or bar.last >= bar.open):
                return MomentumState.ACCELERATING
            if loc <= 0.35:
                return MomentumState.DECELERATING
        return MomentumState.STEADY
    if bar.rsi_14 is None:
        return MomentumState.STEADY
    if bar.rsi_14 >= 70:
        return MomentumState.EXHAUSTED
    if bar.rsi_14 >= 55:
        return MomentumState.ACCELERATING
    if bar.rsi_14 <= 30:
        return MomentumState.EXHAUSTED
    if bar.rsi_14 <= 45:
        return MomentumState.DECELERATING
    return MomentumState.STEADY


def _volatility(bar: BarSnapshot, vix: float | None) -> VolatilityState:
    if vix is not None:
        if vix >= 30:
            return VolatilityState.EXTREME
        if vix >= 20:
            return VolatilityState.ELEVATED
        if vix <= 14:
            return VolatilityState.LOW
    if bar.atr_14 and bar.last:
        atr_pct = bar.atr_14 / bar.last
        if atr_pct >= 0.03:
            return VolatilityState.ELEVATED
        if atr_pct <= 0.01:
            return VolatilityState.LOW
    return VolatilityState.NORMAL


def _liquidity(bar: BarSnapshot) -> LiquidityState:
    if bar.bid and bar.ask and bar.last:
        spread = (bar.ask - bar.bid) / bar.last * 10_000
        if spread > 25:
            return LiquidityState.STRESSED
        if spread > 15:
            return LiquidityState.TIGHT
    if bar.avg_volume_20d and bar.avg_volume_20d < 1_000_000:
        return LiquidityState.TIGHT
    return LiquidityState.NORMAL


def _probability(trend: TrendState, momentum: MomentumState) -> tuple[float, str]:
    score = 0.5
    basis = ["base=0.50"]
    if trend in {TrendState.UP, TrendState.STRONG_UP}:
        score += 0.15
        basis.append("trend_up=+0.15")
    elif trend in {TrendState.DOWN, TrendState.STRONG_DOWN}:
        score -= 0.15
        basis.append("trend_down=-0.15")
    if momentum == MomentumState.ACCELERATING:
        score += 0.1
        basis.append("mom_acc=+0.10")
    elif momentum in {MomentumState.DECELERATING, MomentumState.EXHAUSTED}:
        score -= 0.1
        basis.append("mom_weak=-0.10")
    score = max(0.05, min(0.95, round(score, 2)))
    return score, "rule: " + ", ".join(basis)


class QuantStrategistAgent(BaseAgent[QuantStrategistInput, QuantStrategistOutput]):
    name = AgentName.QUANT_STRATEGIST
    prompt_file = "system_v1.md"
    prompt_version = "2.2.0"

    def output_model(self) -> type[QuantStrategistOutput]:
        return QuantStrategistOutput

    def build_user_prompt(self, payload: QuantStrategistInput) -> str:
        return quant_brief(payload)

    def fallback_output(
        self, payload: QuantStrategistInput, *, reason: str
    ) -> QuantStrategistOutput:
        from app.universe.book_strategy import (
            adjust_probability,
            horizon_for_symbol,
            playbook_for,
            structure_allows_entry,
        )
        from app.universe.horizons import policy_by_symbol, suggested_long_stop

        by_pol = policy_by_symbol(payload.watchlist)
        bars = payload.symbol_bars or payload.index_bars
        views: list[SymbolQuantView] = []
        for bar in bars:
            horizon = horizon_for_symbol(bar.symbol, payload.watchlist)
            trend = _trend(bar, horizon)
            mom = _momentum(bar, horizon)
            vol = _volatility(bar, payload.vix)
            liq = _liquidity(bar)
            base_prob, basis = _probability(trend, mom)
            prob, book_notes = adjust_probability(
                base=base_prob,
                horizon=horizon,
                liquidity=liq,
                volatility=vol,
                rsi=bar.rsi_14,
                volume=bar.volume,
                avg_volume=bar.avg_volume_20d,
            )
            book = playbook_for(horizon)
            pol = by_pol.get(bar.symbol.upper())
            stop = suggested_long_stop(
                reference=float(bar.last),
                atr=float(bar.atr_14) if bar.atr_14 else None,
                policy=pol,
            )
            zone_pct = book.entry_zone_pct if book else 0.01
            target_pct = book.target_pct if book else 0.02
            ok, why = structure_allows_entry(
                horizon=horizon,
                trend=trend,
                momentum=mom,
                liquidity=liq,
                volatility=vol,
                rsi=bar.rsi_14,
                volume=bar.volume,
                avg_volume=bar.avg_volume_20d,
                last=bar.last,
                open_=bar.open,
                high=bar.high,
                low=bar.low,
                sma_20=bar.sma_20,
            )
            entry_zone = None
            if book is not None and ok:
                entry_zone = PriceZone(
                    min=round(bar.last * (1 - zone_pct), 4),
                    max=round(bar.last * (1 + zone_pct), 4),
                )
            notes = ["python-indicators" if reason == "local_python_owns" else "fallback-rules"]
            notes.extend(book_notes)
            notes.append(f"structure={why}")
            views.append(
                SymbolQuantView(
                    symbol=bar.symbol.upper(),
                    trend_state=trend,
                    momentum_state=mom,
                    volatility_state=vol,
                    liquidity_state=liq,
                    support=bar.low,
                    resistance=bar.high,
                    entry_zone=entry_zone,
                    stop_or_invalidation=stop,
                    upside_scenario=Scenario(
                        name="continuation",
                        description=(book.summary[:80] if book else "Trend holds above short MA"),
                        probability=prob,
                        target_price=round(bar.last * (1 + target_pct), 4),
                    ),
                    downside_scenario=Scenario(
                        name="invalidation",
                        description="Book stop / thesis break",
                        probability=round(1 - prob, 2),
                        target_price=round(bar.last * (1 - target_pct), 4),
                    ),
                    probability_estimate=prob,
                    probability_basis=basis + " | " + ",".join(book_notes),
                    notes=notes,
                )
            )

        spy = next((b for b in payload.index_bars if b.symbol.upper() == "SPY"), None)
        market_trend = _trend(spy, "short") if spy else TrendState.SIDEWAYS
        market_mom = _momentum(spy, "short") if spy else MomentumState.STEADY
        market_vol = _volatility(spy, payload.vix) if spy else VolatilityState.NORMAL
        breadth = BreadthState.MIXED
        if payload.advance_decline is not None:
            if payload.advance_decline >= 1.5:
                breadth = BreadthState.STRONG
            elif payload.advance_decline >= 1.0:
                breadth = BreadthState.HEALTHY
            elif payload.advance_decline >= 0.7:
                breadth = BreadthState.MIXED
            else:
                breadth = BreadthState.WEAK

        quality = 0.8 if bars else 0.3
        python_only = reason == "local_python_owns"
        return QuantStrategistOutput(
            timestamp=datetime.now(UTC),
            market_trend_state=market_trend,
            market_momentum_state=market_mom,
            market_volatility_state=market_vol,
            market_breadth_state=breadth,
            market_liquidity_state=LiquidityState.NORMAL,
            symbol_views=views,
            data_quality_score=quality,
            conflicts=[],
            trace=TraceMetadata(
                agent_version=self.agent_version,
                prompt_version=self.prompt_version,
                model_name="python-rules" if python_only else "fallback-rules",
                source_data_timestamp=payload.as_of,
            ),
        )
