"""Alpaca Paper Trading broker adapter (HTTP)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from app.brokers.base import BrokerClient, OrderRequest, OrderResult, OrderSide, OrderStatus
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.core.security import require_execution_allowed

logger = get_logger(__name__)


class BrokerError(Exception):
    """Broker API failure — callers must fail closed."""


def _map_status(raw: str | None) -> OrderStatus:
    value = (raw or "").lower()
    mapping = {
        "new": OrderStatus.NEW,
        "accepted": OrderStatus.ACCEPTED,
        "pending_new": OrderStatus.NEW,
        "accepted_for_bidding": OrderStatus.ACCEPTED,
        "filled": OrderStatus.FILLED,
        "partially_filled": OrderStatus.PARTIAL,
        "canceled": OrderStatus.CANCELED,
        "cancelled": OrderStatus.CANCELED,
        "expired": OrderStatus.CANCELED,
        "rejected": OrderStatus.REJECTED,
        "pending_cancel": OrderStatus.ACCEPTED,
        "pending_replace": OrderStatus.ACCEPTED,
    }
    return mapping.get(value, OrderStatus.NEW)


class AlpacaBroker:
    """Alpaca v2 trading API. Defaults to paper base URL from settings."""

    def __init__(self, settings: Settings | None = None, *, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings or get_settings()
        self._client = client
        key = self.settings.alpaca_api_key
        secret = self.settings.alpaca_api_secret
        if not key or not secret:
            raise BrokerError("Alpaca API key/secret not configured")
        self._headers = {
            "APCA-API-KEY-ID": key.get_secret_value(),
            "APCA-API-SECRET-KEY": secret.get_secret_value(),
            "Content-Type": "application/json",
        }
        self.base_url = self.settings.alpaca_base_url.rstrip("/")

    def _ensure_paper_mode(self) -> None:
        mode = require_execution_allowed(self.settings)
        if mode.value == "live" and self.settings.is_live_trading_allowed():
            # Dual-gate passed — still warn; paper URL preferred.
            if "paper-api" not in self.base_url:
                logger.warning("alpaca_live_url_in_use", base_url=self.base_url)
            return
        if "paper-api" not in self.base_url and mode.value != "live":
            raise BrokerError(
                f"Refusing non-paper Alpaca URL in mode={mode.value}: {self.base_url}"
            )

    async def _http(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        return httpx.AsyncClient(base_url=self.base_url, headers=self._headers, timeout=30.0)

    async def _request(
        self, method: str, path: str, *, json: dict[str, Any] | None = None
    ) -> Any:
        self._ensure_paper_mode()
        owns_client = self._client is None
        client = await self._http()
        try:
            response = await client.request(method, path, json=json)
            if response.status_code >= 400:
                logger.error(
                    "alpaca_http_error",
                    status=response.status_code,
                    path=path,
                    body=response.text[:500],
                )
                raise BrokerError(f"Alpaca HTTP {response.status_code}: {response.text[:200]}")
            if response.status_code == 204 or not response.content:
                return {}
            return response.json()
        finally:
            if owns_client:
                await client.aclose()

    def _to_result(self, data: dict[str, Any]) -> OrderResult:
        submitted = data.get("submitted_at") or data.get("created_at")
        if isinstance(submitted, str):
            submitted_at = datetime.fromisoformat(submitted.replace("Z", "+00:00"))
        else:
            submitted_at = datetime.now(UTC)
        filled = data.get("filled_qty") or data.get("filled_avg_price") and data.get("qty")
        try:
            filled_qty = float(data.get("filled_qty") or 0)
        except (TypeError, ValueError):
            filled_qty = 0.0
        avg = data.get("filled_avg_price")
        return OrderResult(
            broker_order_id=str(data.get("id") or data.get("client_order_id") or ""),
            status=_map_status(str(data.get("status")) if data.get("status") else None),
            submitted_at=submitted_at,
            filled_qty=filled_qty,
            avg_fill_price=float(avg) if avg not in (None, "") else None,
            raw=data,
        )

    async def submit_order(self, request: OrderRequest) -> OrderResult:
        payload: dict[str, Any] = {
            "symbol": request.symbol.upper(),
            "qty": str(request.qty),
            "side": request.side.value,
            "type": request.order_type,
            "time_in_force": request.time_in_force,
        }
        if request.idempotency_key:
            payload["client_order_id"] = request.idempotency_key[:48]
        if request.order_type == "limit" and request.limit_price is not None:
            payload["limit_price"] = str(request.limit_price)
        if request.order_type in {"stop", "stop_limit"} and request.stop_price is not None:
            payload["stop_price"] = str(request.stop_price)
        if request.order_type == "stop_limit" and request.limit_price is not None:
            payload["limit_price"] = str(request.limit_price)

        data = await self._request("POST", "/v2/orders", json=payload)
        assert isinstance(data, dict)
        logger.info(
            "alpaca_order_submitted",
            symbol=request.symbol,
            side=request.side.value,
            qty=request.qty,
            broker_order_id=data.get("id"),
        )
        return self._to_result(data)

    async def cancel_order(self, broker_order_id: str) -> OrderResult:
        try:
            data = await self._request("DELETE", f"/v2/orders/{broker_order_id}")
        except BrokerError:
            # Fetch current status after cancel attempt
            data = await self._request("GET", f"/v2/orders/{broker_order_id}")
        assert isinstance(data, dict) or data == {}
        if not data:
            return OrderResult(
                broker_order_id=broker_order_id,
                status=OrderStatus.CANCELED,
                submitted_at=datetime.now(UTC),
            )
        return self._to_result(data)

    async def cancel_all_orders(self) -> int:
        data = await self._request("DELETE", "/v2/orders")
        if isinstance(data, list):
            return len(data)
        return 0

    async def get_order(self, broker_order_id: str) -> OrderResult:
        data = await self._request("GET", f"/v2/orders/{broker_order_id}")
        assert isinstance(data, dict)
        return self._to_result(data)

    async def get_open_orders(self) -> list[OrderResult]:
        self._ensure_paper_mode()
        owns = self._client is None
        client = await self._http()
        try:
            response = await client.get("/v2/orders", params={"status": "open", "limit": 100})
            if response.status_code >= 400:
                raise BrokerError(f"Alpaca HTTP {response.status_code}")
            rows = response.json()
        finally:
            if owns:
                await client.aclose()
        assert isinstance(rows, list)
        return [self._to_result(r) for r in rows if isinstance(r, dict)]

    async def get_positions(self) -> list[dict[str, object]]:
        data = await self._request("GET", "/v2/positions")
        assert isinstance(data, list)
        return [dict(row) for row in data if isinstance(row, dict)]

    async def get_account(self) -> dict[str, object]:
        data = await self._request("GET", "/v2/account")
        assert isinstance(data, dict)
        return dict(data)


class SimulatedBroker:
    """In-memory paper broker for unit tests (no network)."""

    def __init__(self) -> None:
        self.orders: dict[str, OrderResult] = {}
        self.positions: dict[str, dict[str, object]] = {}
        self.account: dict[str, object] = {
            "equity": "25000",
            "cash": "25000",
            "buying_power": "25000",
            "status": "ACTIVE",
        }
        self._seq = 0
        self.fail_next = False

    async def submit_order(self, request: OrderRequest) -> OrderResult:
        if self.fail_next:
            self.fail_next = False
            raise BrokerError("simulated broker failure")
        self._seq += 1
        oid = f"sim-{self._seq}"
        # Idempotency
        if request.idempotency_key:
            for existing in self.orders.values():
                raw = existing.raw or {}
                if raw.get("client_order_id") == request.idempotency_key:
                    return existing
        fill_price = request.limit_price or 100.0
        result = OrderResult(
            broker_order_id=oid,
            status=OrderStatus.FILLED,
            submitted_at=datetime.now(UTC),
            filled_qty=request.qty,
            avg_fill_price=fill_price,
            raw={"client_order_id": request.idempotency_key, "symbol": request.symbol},
        )
        self.orders[oid] = result
        sym = request.symbol.upper()
        pos = self.positions.get(sym, {"symbol": sym, "qty": "0", "avg_entry_price": "0"})
        qty = float(pos["qty"])  # type: ignore[arg-type]
        if request.side == OrderSide.BUY:
            qty += request.qty
        else:
            qty -= request.qty
        pos["qty"] = str(qty)
        pos["avg_entry_price"] = str(fill_price)
        pos["market_value"] = str(qty * fill_price)
        pos["unrealized_pl"] = "0"
        if qty == 0:
            self.positions.pop(sym, None)
        else:
            self.positions[sym] = pos
        cash = float(str(self.account["cash"]))
        delta = request.qty * fill_price
        self.account["cash"] = str(cash - delta if request.side == OrderSide.BUY else cash + delta)
        return result

    async def cancel_order(self, broker_order_id: str) -> OrderResult:
        order = self.orders.get(broker_order_id)
        if order is None:
            raise BrokerError("order not found")
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

    async def get_order(self, broker_order_id: str) -> OrderResult:
        if broker_order_id not in self.orders:
            raise BrokerError("order not found")
        return self.orders[broker_order_id]

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


def get_broker(settings: Settings | None = None) -> BrokerClient:
    cfg = settings or get_settings()
    if cfg.app_env.value == "test" or not cfg.alpaca_api_key:
        logger.warning("broker_using_simulated", reason="missing_keys_or_test")
        return SimulatedBroker()
    return AlpacaBroker(cfg)
