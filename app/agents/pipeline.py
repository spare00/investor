"""Bottom-up agent analysis pipeline (no order execution)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from app.agents.briefs import mi_summary_for_downstream
from app.agents.cio import CIOAgent
from app.agents.devils_advocate import DevilsAdvocateAgent
from app.agents.macro_strategist import MacroStrategistAgent
from app.agents.market_intelligence import MarketIntelligenceAgent
from app.agents.quant_strategist import QuantStrategistAgent
from app.agents.risk_manager import RiskManagerAgent
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.market.book_context import VenueBookContext, build_venue_book_context, index_symbols_for_venue
from app.schemas import (
    DevilsAdvocateOutput,
    MacroStrategistOutput,
    MarketIntelligenceOutput,
    QuantStrategistOutput,
    RiskManagerOutput,
)
from app.schemas.cio import CIODecision, CIOInput, SymbolActionPlan
from app.schemas.common import SymbolAction, TraceMetadata
from app.schemas.devils_advocate import DevilsAdvocateInput, ProposedThesis
from app.schemas.macro_strategist import MacroSnapshotInput, MacroStrategistInput
from app.schemas.market_intelligence import MarketIntelligenceInput, NewsItemInput
from app.schemas.quant_strategist import BarSnapshot, QuantStrategistInput, QuantStrategistOutput
from app.schemas.risk_manager import (
    PortfolioStateInput,
    ProposedTrade,
    RiskManagerInput,
)
from app.services.collection import CollectionBundle
from app.services.llm import LLMClient
from app.market.live_prices import assess_collection_price_integrity
from app.universe.horizons import (
    align_cio_horizons,
    enrich_watchlist_context,
    policy_by_symbol,
    suggested_long_stop,
    widen_long_stop_if_too_tight,
)

logger = get_logger(__name__)

_ENTRY_ACTIONS = {
    SymbolAction.STRONG_BUY,
    SymbolAction.BUY,
    SymbolAction.SCALE_IN,
}


def theses_from_quant(
    quant: QuantStrategistOutput,
    *,
    entry_universe: list[str] | None,
    regime: str | None,
    watchlist: list[dict] | None = None,
    limit: int = 5,
) -> list[ProposedThesis]:
    """Build Devil/CIO challenge targets from book-aware Quant views.

    Premarket/intraday often pass no explicit ProposedTrade; without theses Devil
    only sees "No explicit trade proposal" and soft-blocks a flat RISK_ON book.
    """
    from app.universe.book_strategy import horizon_for_symbol, should_propose_entry

    allow = {s.upper() for s in (entry_universe or []) if s} or None
    ranked: list[tuple[float, ProposedThesis]] = []
    for view in quant.symbol_views or []:
        sym = str(view.symbol or "").upper()
        if not sym:
            continue
        if allow is not None and sym not in allow:
            continue
        if view.entry_zone is None:
            continue
        hz = horizon_for_symbol(sym, watchlist)
        if not should_propose_entry(
            horizon=hz,
            probability=float(view.probability_estimate or 0.0),
            trend=view.trend_state,
            momentum=view.momentum_state,
            liquidity=view.liquidity_state,
            volatility=view.volatility_state,
            rsi=None,
            regime=regime,
        ):
            continue
        trend = getattr(view.trend_state, "value", str(view.trend_state or ""))
        direction = "short" if trend == "down" else "long"
        ez = view.entry_zone
        ranked.append(
            (
                float(view.probability_estimate or 0.0),
                ProposedThesis(
                    symbol=sym,
                    direction=direction,
                    summary=(
                        f"Quant {hz} {direction} {sym} p={float(view.probability_estimate):.2f} "
                        f"trend={trend} entry={ez.min}-{ez.max}"
                    ),
                    supporting_points=[
                        p
                        for p in [
                            regime or "",
                            hz,
                            view.probability_basis or "",
                            f"stop={view.stop_or_invalidation}"
                            if view.stop_or_invalidation is not None
                            else "",
                        ]
                        if p
                    ],
                ),
            )
        )
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [thesis for _, thesis in ranked[:limit]]


def enrich_cio_entry_stops(
    decision: CIODecision,
    quant: QuantStrategistOutput,
    *,
    latest_prices: dict[str, float] | None = None,
    watchlist_context: list[dict] | None = None,
    atr_by_symbol: dict[str, float] | None = None,
) -> CIODecision:
    """Fill missing stops/entry zones with horizon-aware widths when possible."""
    prices = {k.upper(): float(v) for k, v in (latest_prices or {}).items() if v}
    atrs = {k.upper(): float(v) for k, v in (atr_by_symbol or {}).items() if v and float(v) > 0}
    by_sym = {str(v.symbol).upper(): v for v in (quant.symbol_views or []) if v.symbol}
    by_pol = policy_by_symbol(watchlist_context)
    updated: list[SymbolActionPlan] = []
    changed = False
    for plan in decision.symbol_actions:
        if plan.action not in _ENTRY_ACTIONS:
            updated.append(plan)
            continue
        sym = plan.symbol.upper()
        view = by_sym.get(sym)
        pol = by_pol.get(sym)
        patch: dict = {}
        if plan.entry_zone is None and view and view.entry_zone is not None:
            patch["entry_zone"] = view.entry_zone
        zone = patch.get("entry_zone") or plan.entry_zone
        ref: float | None = None
        if zone is not None:
            try:
                ref = float(zone.min)
            except (TypeError, ValueError):
                ref = None
        if ref is None and sym in prices:
            ref = prices[sym]
        atr = atrs.get(sym)

        stop: float | None = None
        if plan.stop_loss is not None:
            stop = float(plan.stop_loss)
        elif view and view.stop_or_invalidation:
            stop = float(view.stop_or_invalidation)
        if stop is None and ref is not None:
            stop = suggested_long_stop(reference=ref, atr=atr, policy=pol)
        if stop is not None and ref is not None and pol is not None:
            stop = widen_long_stop_if_too_tight(stop=stop, reference=ref, policy=pol)
        if plan.stop_loss is None and stop is not None:
            patch["stop_loss"] = stop
            if not plan.invalidation or plan.invalidation.strip() in {"", "n/a"}:
                book = pol.horizon.value if pol else "default"
                patch["invalidation"] = f"Stop {stop} ({book} book)"
        elif (
            plan.stop_loss is not None
            and stop is not None
            and pol is not None
            and ref is not None
            and float(plan.stop_loss) != stop
        ):
            # Widen too-tight LLM/quant stops to book minimum distance.
            patch["stop_loss"] = stop
        if patch:
            changed = True
            updated.append(plan.model_copy(update=patch))
        else:
            updated.append(plan)
    if not changed:
        return decision
    filled_stops = sum(1 for p in updated if p.stop_loss is not None) - sum(
        1 for p in decision.symbol_actions if p.stop_loss is not None
    )
    logger.info("cio_entry_stops_enriched", filled=filled_stops)
    return decision.model_copy(update={"symbol_actions": updated})


@dataclass(slots=True)
class AnalysisBundle:
    workflow_id: UUID
    market_intelligence: MarketIntelligenceOutput
    macro: MacroStrategistOutput
    quant: QuantStrategistOutput
    risk: RiskManagerOutput
    devil: DevilsAdvocateOutput
    cio: CIODecision
    completed_at: datetime


class AgentPipeline:
    """Runs the mandated bottom-up agent order.

    Cloud: Macro ∥ Quant. Local: sequential so one GPU is not dual-loaded;
    Quant and Risk skip chat and use Python engines.
    """

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        llm: LLMClient | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        common = {"settings": self.settings, "llm": llm}
        self.mi = MarketIntelligenceAgent(**common)
        self.macro = MacroStrategistAgent(**common)
        self.quant = QuantStrategistAgent(**common)
        self.risk = RiskManagerAgent(**common)
        self.devil = DevilsAdvocateAgent(**common)
        self.cio = CIOAgent(**common)

    async def run_from_collection(
        self,
        collection: CollectionBundle,
        *,
        portfolio: PortfolioStateInput,
        proposed_trades: list[ProposedTrade] | None = None,
        workflow_id: UUID | None = None,
        entry_universe: list[str] | None = None,
        watchlist_context: list[dict] | None = None,
        book: VenueBookContext | None = None,
        venue: str | None = None,
    ) -> AnalysisBundle:
        wf = workflow_id or collection.workflow_id or uuid4()
        as_of = collection.collected_at
        active_book = book or build_venue_book_context(
            self.settings,
            venue=venue,
            allowlist=entry_universe,
        )
        if entry_universe is not None:
            entry_list = list(entry_universe)
        else:
            entry_list = list(active_book.allowlist) or list(self.settings.trade_allowlist)
        watch_ctx = enrich_watchlist_context(watchlist_context)
        book_payload = active_book.to_dict()

        def _trace() -> TraceMetadata:
            return TraceMetadata(source_data_timestamp=as_of, book=book_payload)

        mi_in = MarketIntelligenceInput(
            as_of=as_of,
            news_items=[
                NewsItemInput(
                    headline=n.headline,
                    source=n.source,
                    published_at=n.published_at,
                    url=n.url,
                    symbols=n.symbols,
                    provider=n.provider,
                )
                for n in collection.news
                if not n.is_duplicate
            ],
            earnings_summaries=collection.earnings,
            sec_filings=collection.filings,
            portfolio_symbols=[
                p.symbol
                for p in portfolio.positions
                if (getattr(p, "venue", None) or "US").upper() == active_book.venue
            ],
            allowlist=entry_list,
            watchlist=watch_ctx,
            trace=_trace(),
        )
        mi_out = await self.mi.run(mi_in)

        macro_in = MacroStrategistInput(
            as_of=as_of,
            macro=MacroSnapshotInput(
                as_of=collection.macro.as_of if collection.macro else as_of,
                fed_funds_rate=collection.macro.fed_funds_rate if collection.macro else None,
                cpi_yoy=collection.macro.cpi_yoy if collection.macro else None,
                pce_yoy=collection.macro.pce_yoy if collection.macro else None,
                unemployment_rate=collection.macro.unemployment_rate if collection.macro else None,
                gdp_growth_q_o_q=collection.macro.gdp_growth_q_o_q if collection.macro else None,
                us_10y_yield=collection.macro.us_10y_yield if collection.macro else None,
                us_2y_yield=collection.macro.us_2y_yield if collection.macro else None,
                dxy=collection.macro.dxy if collection.macro else None,
                wti_oil=collection.macro.wti_oil if collection.macro else None,
                gold=collection.macro.gold if collection.macro else None,
                hy_credit_spread_bps=collection.macro.hy_credit_spread_bps if collection.macro else None,
                notes=list(collection.macro.notes) if collection.macro else [],
            ),
            market_intelligence_summary=mi_summary_for_downstream(mi_out),
            trace=_trace(),
        )

        index_syms = set(active_book.index_symbols) or set(
            index_symbols_for_venue(active_book.venue, self.settings)
        )
        index_bars = []
        symbol_bars = []
        vix = None
        for m in collection.markets:
            bar = BarSnapshot(
                symbol=m.symbol,
                last=m.last,
                open=m.open,
                high=m.high,
                low=m.low,
                volume=m.volume,
                avg_volume_20d=m.avg_volume_20d,
                atr_14=m.atr_14,
                rsi_14=m.rsi_14,
                sma_20=m.sma_20,
                sma_50=m.sma_50,
                sma_200=m.sma_200,
                bid=m.bid,
                ask=m.ask,
                premarket_change_pct=m.premarket_change_pct,
                gap_pct=m.gap_pct,
            )
            if m.symbol in index_syms:
                index_bars.append(bar)
            else:
                symbol_bars.append(bar)
            if m.vix is not None:
                vix = m.vix

        quant_in = QuantStrategistInput(
            as_of=as_of,
            index_bars=index_bars,
            symbol_bars=symbol_bars,
            vix=vix,
            market_intelligence_summary=mi_summary_for_downstream(mi_out),
            watchlist=watch_ctx,
            trace=_trace(),
        )

        if self.settings.llm_is_local():
            # One on-box model: overlapping chat completions just queue and timeout.
            macro_out = await self.macro.run(macro_in)
            quant_out = await self.quant.run(quant_in)
        else:
            macro_out, quant_out = await asyncio.gather(
                self.macro.run(macro_in),
                self.quant.run(quant_in),
            )

        live_req, feed_live, price_providers, price_notes = assess_collection_price_integrity(
            providers=[m.provider for m in collection.markets],
            market_count=len(collection.markets),
            settings=self.settings,
        )

        risk_in = RiskManagerInput(
            as_of=as_of,
            portfolio=portfolio,
            proposed_trades=proposed_trades or [],
            market_intelligence=mi_out,
            macro=macro_out,
            quant=quant_out,
            data_quality_score=collection.aggregate_quality,
            market_session_clear=not collection.fail_closed,
            broker_data_consistent=True,
            live_prices_required=live_req,
            price_feed_live=feed_live,
            price_providers=price_providers,
            price_integrity_notes=price_notes,
            watchlist=watch_ctx,
            trace=_trace(),
        )
        risk_out = await self.risk.run(risk_in)

        theses = [
            ProposedThesis(
                symbol=t.symbol,
                direction="long" if t.side == "buy" else "flat",
                summary=f"Proposed {t.side} {t.symbol}",
                supporting_points=[macro_out.market_regime.value],
            )
            for t in (proposed_trades or [])
        ]
        if not theses:
            theses = theses_from_quant(
                quant_out,
                entry_universe=entry_list,
                regime=macro_out.market_regime.value,
                watchlist=watch_ctx,
            )
        if not theses:
            theses = [
                ProposedThesis(
                    symbol=None,
                    direction="flat",
                    summary="No explicit trade proposal",
                    supporting_points=[],
                )
            ]

        devil_out = await self.devil.run(
            DevilsAdvocateInput(
                as_of=as_of,
                proposed_theses=theses,
                market_intelligence=mi_out,
                macro=macro_out,
                quant=quant_out,
                risk=risk_out,
                consensus_lean=macro_out.market_regime.value,
                watchlist=watch_ctx,
                trace=_trace(),
            )
        )

        cio_out = await self.cio.run(
            CIOInput(
                as_of=as_of,
                market_intelligence=mi_out,
                macro=macro_out,
                quant=quant_out,
                risk=risk_out,
                devil=devil_out,
                portfolio_cash_pct=portfolio.cash_pct,
                positions=list(portfolio.positions),
                allowlist=entry_list,
                watchlist=watch_ctx,
                trace=_trace(),
            )
        )
        prices = {m.symbol.upper(): float(m.last) for m in collection.markets if m.last}
        atrs = {
            m.symbol.upper(): float(m.atr_14)
            for m in collection.markets
            if m.atr_14 is not None and float(m.atr_14) > 0
        }
        cio_out = align_cio_horizons(cio_out, watch_ctx)
        from app.universe.book_strategy import align_cio_playbook_exits

        cio_out = align_cio_playbook_exits(
            cio_out,
            quant_out,
            watch_ctx,
            held_symbols=[p.symbol for p in portfolio.positions if abs(p.quantity or 0) > 1e-9],
        )
        cio_out = enrich_cio_entry_stops(
            cio_out,
            quant_out,
            latest_prices=prices,
            watchlist_context=watch_ctx,
            atr_by_symbol=atrs,
        )

        logger.info(
            "agent_pipeline_complete",
            workflow_id=str(wf),
            regime=macro_out.market_regime.value,
            risk=risk_out.overall_verdict.value,
            cio=cio_out.portfolio_action.value,
        )
        return AnalysisBundle(
            workflow_id=wf,
            market_intelligence=mi_out,
            macro=macro_out,
            quant=quant_out,
            risk=risk_out,
            devil=devil_out,
            cio=cio_out,
            completed_at=datetime.now(UTC),
        )
