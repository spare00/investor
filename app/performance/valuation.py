"""Portfolio valuation builders."""

from __future__ import annotations

from datetime import datetime
from typing import Any, TypedDict


class PositionValuation(TypedDict, total=False):
    symbol: str
    quantity: float
    side: str
    market_value: float
    cost_basis: float


class PortfolioValuation(TypedDict):
    portfolio_id: str
    as_of: datetime
    valuation_kind: str
    cash: float
    long_market_value: float
    short_market_value: float
    gross_exposure: float
    net_exposure: float
    equity: float
    fees: float
    slippage: float
    benchmarks: dict[str, float]
    source_snapshot_ids: list[str]
    positions: list[PositionValuation]


def valuation_dedup_key(
    portfolio_id: str,
    as_of: datetime,
    valuation_kind: str,
) -> str:
    return f"{portfolio_id}|{as_of.isoformat()}|{valuation_kind}"


def build_portfolio_valuation(
    *,
    portfolio_id: str,
    as_of: datetime,
    valuation_kind: str,
    cash: float,
    positions: list[dict[str, Any]],
    fees: float = 0.0,
    slippage: float = 0.0,
    benchmarks: dict[str, float] | None = None,
    source_snapshot_ids: list[str] | None = None,
) -> PortfolioValuation:
    long_mv = 0.0
    short_mv = 0.0
    pos_out: list[PositionValuation] = []
    for p in positions:
        qty = float(p.get("quantity", 0))
        side = str(p.get("side", "long")).lower()
        price = float(p.get("price", p.get("current_price", 0)))
        mv = abs(qty * price)
        if side == "short" or qty < 0:
            short_mv += mv
            side = "short"
        else:
            long_mv += mv
            side = "long"
        pos_out.append(
            PositionValuation(
                symbol=str(p.get("symbol", "")),
                quantity=abs(qty),
                side=side,
                market_value=mv,
                cost_basis=float(p.get("cost_basis", mv)),
            )
        )
    gross = long_mv + short_mv
    net = long_mv - short_mv
    equity = cash + net
    return PortfolioValuation(
        portfolio_id=portfolio_id,
        as_of=as_of,
        valuation_kind=valuation_kind,
        cash=cash,
        long_market_value=long_mv,
        short_market_value=short_mv,
        gross_exposure=gross,
        net_exposure=net,
        equity=equity,
        fees=fees,
        slippage=slippage,
        benchmarks=benchmarks or {},
        source_snapshot_ids=source_snapshot_ids or [],
        positions=pos_out,
    )
