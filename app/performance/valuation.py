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
        price = float(p.get("price") or p.get("current_price") or 0)
        if "market_value" in p and p.get("market_value") is not None:
            mv = abs(float(p.get("market_value") or 0))
        else:
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


def positions_from_snapshot_payload(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Recover positions for Performance display.

    Older snapshots stored holdings only in fingerprint tuples
    ``[symbol, venue, qty, market_value]``, not a ``positions`` list. IBKR
    marks often have market_value without a last price.
    """
    data = payload or {}
    raw = data.get("positions")
    if isinstance(raw, list) and raw and isinstance(raw[0], dict) and raw[0].get("symbol"):
        return list(raw)
    out: list[dict[str, Any]] = []
    fingerprint = data.get("fingerprint") if isinstance(data.get("fingerprint"), dict) else {}
    for row in fingerprint.get("positions") or []:
        if not isinstance(row, (list, tuple)) or len(row) < 4:
            continue
        try:
            qty = float(row[2] or 0)
            mv = float(row[3] or 0)
        except (TypeError, ValueError):
            continue
        if qty == 0:
            continue
        out.append(
            {
                "symbol": str(row[0]),
                "quantity": qty,
                "side": "long" if qty > 0 else "short",
                "price": abs(mv / qty),
                "market_value": abs(mv),
            }
        )
    return out
