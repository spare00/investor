"""Deterministic multi-day simulation without real broker or LLM calls."""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.brokers.mock import MockBroker
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.services.llm import FakeLLMProvider

logger = get_logger(__name__)

CODE_VERSION = "0.12.0"

try:
    from app.models.entities import SimulationRunRecord as SimulationRunModel  # type: ignore[attr-defined]
except ImportError:
    SimulationRunModel = None  # type: ignore[misc, assignment]


class SimulationScenario(StrEnum):
    BULL_MARKET = "bull-market"
    BEAR_MARKET = "bear-market"
    SIDEWAYS = "sideways"
    VOLATILITY_SHOCK = "volatility-shock"
    PROVIDER_OUTAGE = "provider-outage"
    BROKER_OUTAGE = "broker-outage"
    EMERGENCY_STOP = "emergency-stop"
    DRAWDOWN = "drawdown"
    EARLY_CLOSE = "early-close"


@dataclass(slots=True)
class SimulationSummary:
    simulation_id: UUID
    scenario: str
    seed: int
    days: int
    code_version: str
    prompt_version: str
    model_name: str
    config_hash: str
    starting_equity: float
    ending_equity: float
    total_return_pct: float
    max_drawdown_pct: float
    trades_count: int
    win_rate: float
    metrics: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "simulation_id": str(self.simulation_id),
            "scenario": self.scenario,
            "seed": self.seed,
            "days": self.days,
            "code_version": self.code_version,
            "prompt_version": self.prompt_version,
            "model_name": self.model_name,
            "config_hash": self.config_hash,
            "starting_equity": self.starting_equity,
            "ending_equity": self.ending_equity,
            "total_return_pct": self.total_return_pct,
            "max_drawdown_pct": self.max_drawdown_pct,
            "trades_count": self.trades_count,
            "win_rate": self.win_rate,
            "metrics": self.metrics,
        }


class MultiDaySimulationRunner:
    """
    Run deterministic multi-day scenarios using MockBroker and FakeLLM.

    Does not call real brokers or external LLMs.
    """

    SCENARIO_DRIFT: dict[SimulationScenario, float] = {
        SimulationScenario.BULL_MARKET: 0.004,
        SimulationScenario.BEAR_MARKET: -0.003,
        SimulationScenario.SIDEWAYS: 0.0,
        SimulationScenario.VOLATILITY_SHOCK: 0.0,
        SimulationScenario.PROVIDER_OUTAGE: 0.001,
        SimulationScenario.BROKER_OUTAGE: 0.0,
        SimulationScenario.EMERGENCY_STOP: -0.002,
        SimulationScenario.DRAWDOWN: -0.005,
        SimulationScenario.EARLY_CLOSE: 0.001,
    }

    SCENARIO_VOLATILITY: dict[SimulationScenario, float] = {
        SimulationScenario.BULL_MARKET: 0.008,
        SimulationScenario.BEAR_MARKET: 0.012,
        SimulationScenario.SIDEWAYS: 0.003,
        SimulationScenario.VOLATILITY_SHOCK: 0.035,
        SimulationScenario.PROVIDER_OUTAGE: 0.006,
        SimulationScenario.BROKER_OUTAGE: 0.005,
        SimulationScenario.EMERGENCY_STOP: 0.015,
        SimulationScenario.DRAWDOWN: 0.018,
        SimulationScenario.EARLY_CLOSE: 0.004,
    }

    def __init__(
        self,
        session: AsyncSession | None = None,
        settings: Settings | None = None,
        *,
        seed: int | None = None,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.seed = seed if seed is not None else self.settings.mock_broker_seed
        self._rng = random.Random(self.seed)

    def _config_hash(self) -> str:
        payload = self.settings.model_dump(mode="json")
        # Exclude secrets from hash stability checks in tests
        for key in list(payload.keys()):
            if "secret" in key.lower() or "token" in key.lower() or "password" in key.lower():
                payload.pop(key, None)
        raw = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def _prompt_version(self) -> str:
        return getattr(self.settings, "calculation_version", "indicators_v1")

    async def run(
        self,
        scenario: SimulationScenario | str,
        *,
        days: int = 5,
        symbols: list[str] | None = None,
    ) -> SimulationSummary | dict[str, Any]:
        if not self.settings.enable_long_running_simulation and days > 30:
            days = min(days, 30)

        sc = SimulationScenario(scenario)
        sym_list = symbols or ["SPY", "QQQ"]
        simulation_id = uuid4()
        broker = MockBroker(seed=self.seed, starting_cash=self.settings.starting_cash)
        llm = FakeLLMProvider({"simulation": True, "scenario": sc.value})
        _ = llm  # reserved for future agent replay hooks

        equity = float(self.settings.starting_cash)
        peak = equity
        max_dd = 0.0
        trades = 0
        wins = 0
        drift = self.SCENARIO_DRIFT[sc]
        vol = self.SCENARIO_VOLATILITY[sc]

        outage_day: int | None = None
        if sc in {SimulationScenario.PROVIDER_OUTAGE, SimulationScenario.BROKER_OUTAGE}:
            outage_day = self._rng.randint(1, max(1, days - 1))

        for day in range(1, days + 1):
            if sc == SimulationScenario.EARLY_CLOSE and day == days:
                broker.market_open = False

            if outage_day == day:
                broker.fail_next = sc == SimulationScenario.BROKER_OUTAGE

            daily_return = drift + self._rng.gauss(0, vol)
            if sc == SimulationScenario.VOLATILITY_SHOCK and day == max(1, days // 2):
                daily_return -= abs(self._rng.gauss(0.03, 0.01))

            if sc == SimulationScenario.EMERGENCY_STOP and day >= max(1, days // 2):
                daily_return = min(daily_return, -0.01)

            equity *= 1.0 + daily_return
            peak = max(peak, equity)
            dd = (peak - equity) / peak if peak > 0 else 0.0
            max_dd = max(max_dd, dd)

            # Synthetic trade activity
            if self._rng.random() < 0.4 and broker.market_open:
                trades += 1
                if daily_return > 0:
                    wins += 1
                for symbol in sym_list:
                    broker.prices[symbol] = broker.prices.get(symbol, 100.0) * (1.0 + daily_return)

        ending_equity = round(equity, 2)
        starting_equity = float(self.settings.starting_cash)
        total_return_pct = round((ending_equity / starting_equity - 1.0) * 100, 4)
        win_rate = round(wins / trades, 4) if trades else 0.0

        summary = SimulationSummary(
            simulation_id=simulation_id,
            scenario=sc.value,
            seed=self.seed,
            days=days,
            code_version=CODE_VERSION,
            prompt_version=self._prompt_version(),
            model_name=self.settings.llm_model,
            config_hash=self._config_hash(),
            starting_equity=starting_equity,
            ending_equity=ending_equity,
            total_return_pct=total_return_pct,
            max_drawdown_pct=round(max_dd * 100, 4),
            trades_count=trades,
            win_rate=win_rate,
            metrics={
                "sharpe_synthetic": round(drift / vol if vol else 0.0, 4),
                "llm_calls": float(len(llm.calls)),
                "broker_orders": float(len(broker.orders)),
            },
        )

        persisted = await self._persist(summary)
        if persisted is None:
            return summary.to_dict()
        return summary

    async def _persist(self, summary: SimulationSummary) -> SimulationSummary | None:
        if self.session is None or SimulationRunModel is None:
            logger.debug("simulation_persist_skipped", reason="no_model_or_session")
            return None
        try:
            now = datetime.now(UTC)
            row = SimulationRunModel(
                id=summary.simulation_id,
                scenario=summary.scenario,
                period_start=now,
                period_end=now,
                trading_days=summary.days,
                trade_count=summary.trades_count,
                return_pct=summary.total_return_pct,
                max_drawdown=summary.max_drawdown_pct,
                win_rate=summary.win_rate,
                sharpe=summary.metrics.get("sharpe_synthetic"),
                code_version=summary.code_version,
                prompt_version=summary.prompt_version,
                model_version=summary.model_name,
                configuration_hash=summary.config_hash,
                status="COMPLETED",
                payload=summary.to_dict(),
            )
            self.session.add(row)
            await self.session.flush()
            return summary
        except Exception:
            logger.exception("simulation_persist_failed")
            try:
                await self.session.rollback()
            except Exception:  # noqa: BLE001
                pass
            return None
