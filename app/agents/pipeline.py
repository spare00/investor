"""Bottom-up agent analysis pipeline (no order execution)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from app.agents.cio import CIOAgent
from app.agents.devils_advocate import DevilsAdvocateAgent
from app.agents.macro_strategist import MacroStrategistAgent
from app.agents.market_intelligence import MarketIntelligenceAgent
from app.agents.quant_strategist import QuantStrategistAgent
from app.agents.risk_manager import RiskManagerAgent
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.schemas import (
    CIODecision,
    DevilsAdvocateOutput,
    MacroStrategistOutput,
    MarketIntelligenceOutput,
    QuantStrategistOutput,
    RiskManagerOutput,
)
from app.schemas.common import TraceMetadata
from app.schemas.devils_advocate import DevilsAdvocateInput, ProposedThesis
from app.schemas.macro_strategist import MacroSnapshotInput, MacroStrategistInput, MacroStrategistOutput
from app.schemas.market_intelligence import MarketIntelligenceInput, NewsItemInput
from app.schemas.quant_strategist import BarSnapshot, QuantStrategistInput, QuantStrategistOutput
from app.schemas.risk_manager import (
    PortfolioStateInput,
    ProposedTrade,
    RiskManagerInput,
)
from app.schemas.cio import CIOInput
from app.services.collection import CollectionBundle
from app.services.llm import LLMClient
from app.universe.horizons import align_cio_horizons

logger = get_logger(__name__)


def theses_from_quant(
    quant: QuantStrategistOutput,
    *,
    entry_universe: list[str] | None,
    regime: str | None,
    limit: int = 5,
) -> list[ProposedThesis]:
    """Build Devil/CIO challenge targets from actionable Quant views.

    Premarket/intraday often pass no explicit ProposedTrade; without theses Devil
    only sees "No explicit trade proposal" and soft-blocks a flat RISK_ON book.
    """
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
        prob = float(view.probability_estimate or 0.0)
        if prob < 0.45:
            continue
        trend = getattr(view.trend_state, "value", str(view.trend_state or ""))
        direction = "short" if trend == "down" else "long"
        ez = view.entry_zone
        ranked.append(
            (
                prob,
                ProposedThesis(
                    symbol=sym,
                    direction=direction,
                    summary=(
                        f"Quant {direction} {sym} p={prob:.2f} trend={trend} "
                        f"entry={ez.min}-{ez.max}"
                    ),
                    supporting_points=[
                        p
                        for p in [
                            regime or "",
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
    """Runs the mandated bottom-up agent order with Macro∥Quant parallelism."""

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
    ) -> AnalysisBundle:
        wf = workflow_id or collection.workflow_id or uuid4()
        as_of = collection.collected_at
        entry_list = list(entry_universe) if entry_universe is not None else list(self.settings.trade_allowlist)
        watch_ctx = list(watchlist_context or [])

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
            portfolio_symbols=[p.symbol for p in portfolio.positions],
            allowlist=entry_list,
            trace=TraceMetadata(source_data_timestamp=as_of),
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
            market_intelligence_summary=mi_out.model_dump(mode="json"),
            trace=TraceMetadata(source_data_timestamp=as_of),
        )

        index_syms = {"SPY", "QQQ", "IWM", "DIA"}
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
            market_intelligence_summary={
                "themes": mi_out.top_market_themes,
                "quality": mi_out.data_quality_score,
            },
            trace=TraceMetadata(source_data_timestamp=as_of),
        )

        macro_out, quant_out = await asyncio.gather(
            self.macro.run(macro_in),
            self.quant.run(quant_in),
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
            trace=TraceMetadata(source_data_timestamp=as_of),
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
                trace=TraceMetadata(source_data_timestamp=as_of),
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
                trace=TraceMetadata(source_data_timestamp=as_of),
            )
        )
        cio_out = align_cio_horizons(cio_out, watch_ctx)

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
