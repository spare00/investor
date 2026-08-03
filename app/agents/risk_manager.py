"""Portfolio & Risk Manager agent — LLM narrative + deterministic engine."""

from __future__ import annotations

from datetime import UTC, datetime

from app.agents.base import BaseAgent, dump_for_prompt
from app.core.config import Settings
from app.risk import (
    DeterministicRiskEngine,
    PortfolioRiskView,
    PositionRiskView,
    TradeIntent,
    limits_from_settings,
)
from app.schemas.common import AgentName, RiskVerdict, TraceMetadata
from app.schemas.risk_manager import (
    RiskManagerInput,
    RiskManagerOutput,
    TradeRiskAdjustment,
)
from app.services.llm import LLMClient


class RiskManagerAgent(BaseAgent[RiskManagerInput, RiskManagerOutput]):
    name = AgentName.RISK_MANAGER
    prompt_file = "risk_manager_v0.1.0.txt"
    prompt_version = "0.1.0"

    def __init__(
        self,
        *,
        llm: LLMClient | None = None,
        settings: Settings | None = None,
        engine: DeterministicRiskEngine | None = None,
    ) -> None:
        super().__init__(llm=llm, settings=settings)
        self.engine = engine or DeterministicRiskEngine(limits_from_settings(self.settings))

    def output_model(self) -> type[RiskManagerOutput]:
        return RiskManagerOutput

    def build_user_prompt(self, payload: RiskManagerInput) -> str:
        engine_preview = self._run_engine(payload)
        return (
            "Review portfolio risk. Hard Vetoes from the deterministic engine are authoritative.\n"
            f"ENGINE_RESULT:\n{dump_for_prompt(engine_preview)}\n\n"
            f"INPUT:\n{dump_for_prompt(payload)}"
        )

    def _portfolio_view(self, payload: RiskManagerInput) -> PortfolioRiskView:
        p = payload.portfolio
        return PortfolioRiskView(
            equity=p.equity,
            cash=p.cash,
            cash_pct=p.cash_pct,
            gross_exposure_pct=p.gross_exposure_pct,
            positions=[
                PositionRiskView(
                    symbol=x.symbol,
                    quantity=x.quantity,
                    market_value=x.market_value,
                    sector=x.sector,
                    weight_pct=x.weight_pct,
                )
                for x in p.positions
            ],
            daily_pnl_pct=p.daily_pnl_pct,
            drawdown_pct=p.drawdown_pct,
            consecutive_losses=p.consecutive_losses,
            trading_halted=p.trading_halted,
            cooldown_until=p.cooldown_until,
        )

    def _run_engine(self, payload: RiskManagerInput) -> dict[str, object]:
        portfolio = self._portfolio_view(payload)
        allowlist = set(self.settings.trade_allowlist)
        results = []
        for trade in payload.proposed_trades:
            intent = TradeIntent(
                symbol=trade.symbol,
                side=trade.side,
                quantity=trade.quantity or 0.0,
                entry_price=trade.entry_price or 0.0,
                stop_loss=trade.stop_loss,
                invalidation=trade.invalidation,
                expected_slippage_bps=trade.expected_slippage_bps,
                avg_daily_volume=trade.avg_daily_volume,
                bid_ask_spread_bps=trade.bid_ask_spread_bps,
                atr=trade.atr,
                sector=trade.sector or "Unknown",
                idempotency_key=trade.idempotency_key,
            )
            result = self.engine.evaluate_pretrade(
                portfolio,
                intent,
                allowlist=allowlist,
                data_quality_score=payload.data_quality_score,
                market_session_clear=payload.market_session_clear,
                broker_data_consistent=payload.broker_data_consistent,
                now=payload.as_of,
            )
            results.append(
                {
                    "symbol": trade.symbol,
                    "approved": result.approved,
                    "halt_day": result.halt_day,
                    "hard_vetoes": result.hard_vetoes,
                    "adjusted_quantity": result.adjusted_quantity,
                    "checks": [
                        {
                            "code": c.code if isinstance(c.code, str) else c.code.value,
                            "passed": c.passed,
                            "message": c.message,
                            "hard": c.hard,
                        }
                        for c in result.checks
                    ],
                }
            )
        return {"trades": results}

    async def run(self, payload: RiskManagerInput) -> RiskManagerOutput:
        """Always apply deterministic engine; LLM may only annotate soft warnings."""
        engine_data = self._run_engine(payload)
        trades = engine_data.get("trades", [])
        assert isinstance(trades, list)

        hard_vetoes: list[str] = []
        adjustments: list[TradeRiskAdjustment] = []
        halt_day = False
        for t in trades:
            assert isinstance(t, dict)
            vetoes = list(t.get("hard_vetoes") or [])
            hard_vetoes.extend(str(v) for v in vetoes)
            halt_day = halt_day or bool(t.get("halt_day"))
            if t.get("approved"):
                verdict = RiskVerdict.APPROVED
            elif halt_day:
                verdict = RiskVerdict.HALT_DAY
            else:
                verdict = RiskVerdict.REJECTED
            adjustments.append(
                TradeRiskAdjustment(
                    symbol=str(t["symbol"]),
                    original_quantity=None,
                    approved_quantity=float(t["adjusted_quantity"] or 0) if t.get("approved") else 0.0,
                    verdict=verdict,
                    reasons=[str(v) for v in vetoes] or ["ok"],
                )
            )

        if halt_day:
            overall = RiskVerdict.HALT_DAY
        elif hard_vetoes:
            overall = RiskVerdict.REJECTED
        elif not payload.proposed_trades:
            overall = RiskVerdict.APPROVED
        else:
            overall = (
                RiskVerdict.APPROVED
                if all(a.verdict == RiskVerdict.APPROVED for a in adjustments)
                else RiskVerdict.REJECTED
            )

        soft_warnings: list[str] = []
        # Optional LLM soft commentary — never overrides hard vetoes.
        try:
            llm_out = await super().run(payload)
            soft_warnings = list(llm_out.soft_warnings)
            # Keep engine hard fields authoritative.
        except Exception:  # noqa: BLE001
            soft_warnings = ["LLM soft review unavailable; engine-only decision"]

        return RiskManagerOutput(
            timestamp=datetime.now(UTC),
            overall_verdict=overall,
            hard_vetoes=sorted(set(hard_vetoes)),
            soft_warnings=soft_warnings,
            trade_adjustments=adjustments,
            halt_new_trades=halt_day or overall in {RiskVerdict.REJECTED, RiskVerdict.HALT_DAY},
            cash_pct=payload.portfolio.cash_pct,
            gross_exposure_pct=payload.portfolio.gross_exposure_pct,
            notes=["Deterministic Risk Engine is authoritative for Hard Vetoes"],
            engine_checks=trades,  # type: ignore[arg-type]
            trace=TraceMetadata(
                agent_version=self.agent_version,
                prompt_version=self.prompt_version,
                model_name="risk-engine+optional-llm",
                source_data_timestamp=payload.as_of,
            ),
        )

    def fallback_output(
        self, payload: RiskManagerInput, *, reason: str
    ) -> RiskManagerOutput:
        # run() already engine-first; fallback mirrors engine-only path.
        engine_data = self._run_engine(payload)
        trades = engine_data["trades"]
        assert isinstance(trades, list)
        hard = []
        for t in trades:
            assert isinstance(t, dict)
            hard.extend(str(v) for v in (t.get("hard_vetoes") or []))
        halt = any(bool(t.get("halt_day")) for t in trades if isinstance(t, dict))
        return RiskManagerOutput(
            timestamp=datetime.now(UTC),
            overall_verdict=RiskVerdict.HALT_DAY if halt else (
                RiskVerdict.REJECTED if hard else RiskVerdict.APPROVED
            ),
            hard_vetoes=sorted(set(hard)),
            soft_warnings=[reason],
            trade_adjustments=[],
            halt_new_trades=bool(hard or halt),
            cash_pct=payload.portfolio.cash_pct,
            gross_exposure_pct=payload.portfolio.gross_exposure_pct,
            notes=["fallback engine-only"],
            engine_checks=trades,  # type: ignore[arg-type]
            trace=TraceMetadata(model_name="fallback-engine"),
        )
