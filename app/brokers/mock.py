"""MockBroker — deterministic paper broker for offline tests."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from app.brokers.base import OrderRequest, OrderResult, OrderSide, OrderStatus
from app.brokers.errors import BrokerError
from app.brokers.models import (
    BrokerAccount,
    BrokerAsset,
    BrokerCapabilities,
    BrokerClock,
    BrokerEnvironment,
    BrokerHealth,
    BrokerOrder,
    BrokerOrderRequest,
    BrokerPosition,
    redact_account_id,
)
from app.core.logging import get_logger

logger = get_logger(__name__)


class MockBroker:
    """In-memory deterministic broker (seeded). Implements BrokerClient + Phase 5 APIs."""

    name = "mock"
    version = "1.0.0"

    def __init__(
        self,
        *,
        seed: int = 42,
        starting_cash: float = 25_000.0,
        market_open: bool = True,
        allow_short: bool = False,
        fractionable: bool = True,
    ) -> None:
        self.seed = seed
        self.orders: dict[str, OrderResult] = {}
        self._order_meta: dict[str, dict[str, Any]] = {}
        self.positions: dict[str, dict[str, object]] = {}
        self.account: dict[str, object] = {
            "id": f"mock-{seed}",
            "equity": str(starting_cash),
            "cash": str(starting_cash),
            "buying_power": str(starting_cash),
            "portfolio_value": str(starting_cash),
            "status": "ACTIVE",
            "trading_blocked": False,
            "account_blocked": False,
            "pattern_day_trader": False,
            "daytrade_count": 0,
        }
        self._seq = 0
        self.fail_next = False
        self.timeout_next = False
        self.rate_limit_next = False
        self.market_open = market_open
        self.allow_short = allow_short
        self.fractionable = fractionable
        self.halted_symbols: set[str] = set()
        self.prices: dict[str, float] = {}
        self.partial_fill_fraction: float | None = None  # e.g. 0.5 for tests

    def capabilities(self) -> BrokerCapabilities:
        return BrokerCapabilities(
            provider=self.name,
            supports_replace=True,
            supports_fractional=self.fractionable,
            supports_short=self.allow_short,
            is_mock=True,
        )

    async def health_check(self) -> BrokerHealth:
        return BrokerHealth(
            healthy=True,
            provider=self.name,
            environment=BrokerEnvironment.MOCK.value,
            connected=True,
            message="ok",
            as_of=datetime.now(UTC),
        )

    async def get_clock(self) -> BrokerClock:
        now = datetime.now(UTC)
        return BrokerClock(
            is_open=self.market_open,
            timestamp=now,
            next_open=now if self.market_open else now + timedelta(hours=1),
            next_close=now + timedelta(hours=6) if self.market_open else None,
            source=self.name,
        )

    async def get_asset(self, symbol: str) -> BrokerAsset:
        sym = symbol.upper()
        return BrokerAsset(
            symbol=sym,
            tradable=sym not in self.halted_symbols,
            fractionable=self.fractionable,
            shortable=self.allow_short,
            exchange="MOCK",
        )

    async def is_asset_tradable(self, symbol: str) -> bool:
        asset = await self.get_asset(symbol)
        return asset.tradable and self.market_open

    def _price(self, symbol: str, fallback: float | None = None) -> float:
        if symbol in self.prices:
            return self.prices[symbol]
        # Deterministic synthetic price from seed+symbol
        h = int(hashlib.sha256(f"{self.seed}:{symbol}".encode()).hexdigest()[:8], 16)
        return fallback if fallback is not None else 50.0 + (h % 500) / 10.0

    async def submit_order(self, request: OrderRequest) -> OrderResult:
        if self.timeout_next:
            self.timeout_next = False
            raise TimeoutError("mock_broker_timeout")
        if self.rate_limit_next:
            self.rate_limit_next = False
            raise BrokerError("429 rate limited")
        if self.fail_next:
            self.fail_next = False
            raise BrokerError("simulated broker failure")
        if not self.market_open:
            raise BrokerError("market_closed")
        sym = request.symbol.upper()
        if sym in self.halted_symbols:
            raise BrokerError("trading_halt")
        if request.idempotency_key:
            for existing in self.orders.values():
                raw = existing.raw or {}
                if raw.get("client_order_id") == request.idempotency_key:
                    return existing
        if request.side == OrderSide.SELL and not self.allow_short:
            pos_qty = float(self.positions.get(sym, {}).get("qty", 0) or 0)
            if request.qty > pos_qty + 1e-9:
                raise BrokerError("shorting_not_allowed")
        cash = float(str(self.account["cash"]))
        fill_price = request.limit_price or self._price(sym, 100.0)
        # Slippage: +1bp buy / -1bp sell deterministic
        slip = fill_price * 0.0001
        fill_price = fill_price + slip if request.side == OrderSide.BUY else fill_price - slip
        notional = request.qty * fill_price
        if request.side == OrderSide.BUY and notional > cash + 1e-6:
            raise BrokerError("insufficient_buying_power")
        if not self.fractionable and abs(request.qty - int(request.qty)) > 1e-9:
            raise BrokerError("fractional_shares_unsupported")

        self._seq += 1
        oid = f"mock-{self.seed}-{self._seq}"
        fill_qty = request.qty
        status = OrderStatus.FILLED
        if self.partial_fill_fraction is not None:
            fill_qty = round(request.qty * self.partial_fill_fraction, 6)
            status = OrderStatus.PARTIAL if fill_qty < request.qty else OrderStatus.FILLED

        result = OrderResult(
            broker_order_id=oid,
            status=status,
            submitted_at=datetime.now(UTC),
            filled_qty=fill_qty,
            avg_fill_price=fill_price,
            raw={
                "client_order_id": request.idempotency_key,
                "symbol": sym,
                "side": request.side.value,
                "qty": request.qty,
                "type": request.order_type,
            },
        )
        self.orders[oid] = result
        self._order_meta[oid] = {"request": request, "replaced_by": None}
        self._apply_fill(sym, request.side, fill_qty, fill_price)
        return result

    def _apply_fill(self, sym: str, side: OrderSide, qty: float, price: float) -> None:
        pos = self.positions.get(sym, {"symbol": sym, "qty": "0", "avg_entry_price": "0"})
        cur = float(pos["qty"])  # type: ignore[arg-type]
        if side == OrderSide.BUY:
            new_qty = cur + qty
            if new_qty != 0:
                prev_cost = abs(cur) * float(pos.get("avg_entry_price") or price)
                pos["avg_entry_price"] = str((prev_cost + qty * price) / abs(new_qty))
            cur = new_qty
        else:
            cur -= qty
        pos["qty"] = str(cur)
        pos["market_value"] = str(cur * price)
        pos["current_price"] = str(price)
        pos["unrealized_pl"] = "0"
        pos["side"] = "long" if cur >= 0 else "short"
        if abs(cur) < 1e-9:
            self.positions.pop(sym, None)
        else:
            self.positions[sym] = pos
        cash = float(str(self.account["cash"]))
        delta = qty * price
        self.account["cash"] = str(cash - delta if side == OrderSide.BUY else cash + delta)
        equity = float(str(self.account["cash"])) + sum(
            float(p.get("market_value") or 0) for p in self.positions.values()
        )
        self.account["equity"] = str(equity)
        self.account["portfolio_value"] = str(equity)
        self.account["buying_power"] = str(self.account["cash"])

    async def cancel_order(self, broker_order_id: str) -> OrderResult:
        order = self.orders.get(broker_order_id)
        if order is None:
            raise BrokerError("order not found")
        if order.status in {OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED}:
            return order
        canceled = OrderResult(
            broker_order_id=broker_order_id,
            status=OrderStatus.CANCELED,
            submitted_at=order.submitted_at,
            filled_qty=order.filled_qty,
            avg_fill_price=order.avg_fill_price,
            raw=order.raw,
        )
        self.orders[broker_order_id] = canceled
        return canceled

    async def cancel_all_orders(self) -> int:
        n = 0
        for oid, order in list(self.orders.items()):
            if order.status not in {OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED}:
                await self.cancel_order(oid)
                n += 1
        return n

    async def replace_order(self, broker_order_id: str, replacement: OrderRequest) -> OrderResult:
        existing = await self.get_order(broker_order_id)
        if existing.filled_qty > (replacement.qty or 0) + 1e-9:
            raise BrokerError("cannot_replace_below_filled_qty")
        await self.cancel_order(broker_order_id)
        new = await self.submit_order(replacement)
        self._order_meta[broker_order_id]["replaced_by"] = new.broker_order_id
        return new

    async def close_position(self, symbol: str, *, qty: float | None = None) -> OrderResult | None:
        sym = symbol.upper()
        pos = self.positions.get(sym)
        if not pos:
            return None
        cur = float(pos["qty"])  # type: ignore[arg-type]
        close_qty = abs(cur) if qty is None else min(abs(cur), qty)
        side = OrderSide.SELL if cur > 0 else OrderSide.BUY
        return await self.submit_order(
            OrderRequest(
                symbol=sym,
                side=side,
                qty=close_qty,
                order_type="market",
                idempotency_key=f"close-{sym}-{uuid4()}",
            )
        )

    async def close_all_positions(self) -> int:
        n = 0
        for sym in list(self.positions.keys()):
            if await self.close_position(sym):
                n += 1
        return n

    async def get_order(self, broker_order_id: str) -> OrderResult:
        if broker_order_id not in self.orders:
            raise BrokerError("order not found")
        return self.orders[broker_order_id]

    async def get_order_by_client_id(self, client_order_id: str) -> OrderResult | None:
        for o in self.orders.values():
            if (o.raw or {}).get("client_order_id") == client_order_id:
                return o
        return None

    async def get_open_orders(self) -> list[OrderResult]:
        return [
            o
            for o in self.orders.values()
            if o.status in {OrderStatus.NEW, OrderStatus.ACCEPTED, OrderStatus.PARTIAL}
        ]

    async def get_positions(self) -> list[dict[str, object]]:
        return list(self.positions.values())

    async def get_account(self) -> dict[str, object]:
        return dict(self.account)

    async def get_account_canonical(self) -> BrokerAccount:
        raw = await self.get_account()
        now = datetime.now(UTC)
        return BrokerAccount(
            account_id_reference=redact_account_id(str(raw.get("id"))),
            environment=BrokerEnvironment.MOCK,
            equity=float(str(raw.get("equity", 0))),
            cash=float(str(raw.get("cash", 0))),
            buying_power=float(str(raw.get("buying_power", 0))),
            portfolio_value=float(str(raw.get("portfolio_value", raw.get("equity", 0)))),
            trading_blocked=bool(raw.get("trading_blocked")),
            account_blocked=bool(raw.get("account_blocked")),
            pattern_day_trader=bool(raw.get("pattern_day_trader")),
            daytrade_count=int(raw.get("daytrade_count") or 0),
            as_of=now,
            source=self.name,
        )

    async def get_positions_canonical(self) -> list[BrokerPosition]:
        now = datetime.now(UTC)
        out: list[BrokerPosition] = []
        for p in await self.get_positions():
            qty = float(p.get("qty") or 0)
            out.append(
                BrokerPosition(
                    symbol=str(p.get("symbol", "")).upper(),
                    quantity=qty,
                    available_quantity=qty,
                    side=str(p.get("side") or ("long" if qty >= 0 else "short")),
                    market_value=float(p.get("market_value") or 0),
                    cost_basis=None,
                    average_entry_price=float(p.get("avg_entry_price") or 0) or None,
                    current_price=float(p.get("current_price") or 0) or None,
                    unrealized_pl=float(p.get("unrealized_pl") or 0),
                    as_of=now,
                    source=self.name,
                )
            )
        return out

    async def get_calendar(self, start: str | None = None, end: str | None = None) -> list[dict[str, object]]:
        now = datetime.now(UTC).date()
        return [{"date": now.isoformat(), "open": "09:30", "close": "16:00", "session_open": self.market_open}]

    async def get_activities(self) -> list[dict[str, object]]:
        return [
            {
                "id": o.broker_order_id,
                "activity_type": "FILL",
                "symbol": (o.raw or {}).get("symbol"),
                "qty": o.filled_qty,
                "price": o.avg_fill_price,
            }
            for o in self.orders.values()
            if o.filled_qty > 0
        ]


# Backward-compatible alias used by older tests/imports
SimulatedBroker = MockBroker
