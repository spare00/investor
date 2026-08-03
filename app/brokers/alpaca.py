"""Alpaca Paper Trading broker adapter (HTTP)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from app.brokers.base import BrokerClient, OrderRequest, OrderResult, OrderSide, OrderStatus
from app.brokers.errors import BrokerError
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.core.security import require_execution_allowed

logger = get_logger(__name__)


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
        self._ensure_paper_mode()

    def _ensure_paper_mode(self) -> None:
        """Refuse live endpoints and live trading flags in Phase 5."""
        if self.settings.enable_live_trading:
            raise BrokerError("enable_live_trading_must_be_false")
        if self.settings.broker_environment.lower() != "paper":
            raise BrokerError("broker_environment_must_be_paper")
        paper_hint = self.settings.alpaca_paper_base_url.rstrip("/")
        if "paper-api" not in self.base_url:
            raise BrokerError(
                f"Refusing non-paper Alpaca URL: {self.base_url} (expected paper endpoint)"
            )
        # Prefer configured paper base URL
        if self.base_url != paper_hint and "paper-api" not in self.base_url:
            raise BrokerError("alpaca_base_url_not_paper")
        mode = require_execution_allowed(self.settings)
        if mode.value == "live":
            raise BrokerError("live_execution_mode_blocked")

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
                body = _redact_secrets(response.text[:500], self.settings)
                logger.error(
                    "alpaca_http_error",
                    status=response.status_code,
                    path=path,
                    body=body,
                )
                raise BrokerError(
                    f"Alpaca HTTP {response.status_code}: {_redact_secrets(response.text[:200], self.settings)}"
                )
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
        # Never return raw account id to upper layers via this helper's callers —
        # prefer get_account_canonical for API surfaces.
        return dict(data)

    async def get_clock(self) -> Any:
        from app.brokers.models import BrokerClock

        data = await self._request("GET", "/v2/clock")
        assert isinstance(data, dict)
        ts = data.get("timestamp")
        if isinstance(ts, str):
            timestamp = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        else:
            timestamp = datetime.now(UTC)
        return BrokerClock(
            is_open=bool(data.get("is_open")),
            timestamp=timestamp,
            next_open=_parse_dt(data.get("next_open")),
            next_close=_parse_dt(data.get("next_close")),
            source="alpaca",
        )

    async def get_calendar(self, start: str | None = None, end: str | None = None) -> list[dict[str, object]]:
        params: dict[str, str] = {}
        if start:
            params["start"] = start
        if end:
            params["end"] = end
        self._ensure_paper_mode()
        owns = self._client is None
        client = await self._http()
        try:
            response = await client.get("/v2/calendar", params=params or None)
            if response.status_code >= 400:
                raise BrokerError(f"Alpaca HTTP {response.status_code}")
            rows = response.json()
        finally:
            if owns:
                await client.aclose()
        assert isinstance(rows, list)
        return [dict(r) for r in rows if isinstance(r, dict)]

    async def get_asset(self, symbol: str) -> Any:
        from app.brokers.models import BrokerAsset

        data = await self._request("GET", f"/v2/assets/{symbol.upper()}")
        assert isinstance(data, dict)
        return BrokerAsset(
            symbol=str(data.get("symbol", symbol)).upper(),
            tradable=bool(data.get("tradable")),
            fractionable=bool(data.get("fractionable")),
            shortable=bool(data.get("shortable")),
            easy_to_borrow=bool(data.get("easy_to_borrow")),
            exchange=str(data.get("exchange") or "") or None,
            asset_class=str(data.get("class") or "us_equity"),
            status=str(data.get("status") or "active"),
        )

    async def is_asset_tradable(self, symbol: str) -> bool:
        asset = await self.get_asset(symbol)
        return bool(asset.tradable and asset.status == "active")

    async def get_order_by_client_id(self, client_order_id: str) -> OrderResult | None:
        self._ensure_paper_mode()
        owns = self._client is None
        client = await self._http()
        try:
            response = await client.get(
                "/v2/orders:by_client_order_id",
                params={"client_order_id": client_order_id[:48]},
            )
            if response.status_code == 404:
                return None
            if response.status_code >= 400:
                raise BrokerError(f"Alpaca HTTP {response.status_code}")
            data = response.json()
        finally:
            if owns:
                await client.aclose()
        if not isinstance(data, dict):
            return None
        return self._to_result(data)

    async def replace_order(self, broker_order_id: str, replacement: OrderRequest) -> OrderResult:
        payload: dict[str, Any] = {"qty": str(replacement.qty)}
        if replacement.limit_price is not None:
            payload["limit_price"] = str(replacement.limit_price)
        if replacement.stop_price is not None:
            payload["stop_price"] = str(replacement.stop_price)
        if replacement.time_in_force:
            payload["time_in_force"] = replacement.time_in_force
        data = await self._request("PATCH", f"/v2/orders/{broker_order_id}", json=payload)
        assert isinstance(data, dict)
        return self._to_result(data)

    async def close_position(self, symbol: str, *, qty: float | None = None) -> OrderResult | None:
        payload: dict[str, Any] | None = None
        if qty is not None:
            payload = {"qty": str(qty)}
        data = await self._request("DELETE", f"/v2/positions/{symbol.upper()}", json=payload)
        if not data:
            return None
        assert isinstance(data, dict)
        return self._to_result(data)

    async def close_all_positions(self) -> int:
        data = await self._request("DELETE", "/v2/positions")
        if isinstance(data, list):
            return len(data)
        return 0

    async def get_activities(self) -> list[dict[str, object]]:
        data = await self._request("GET", "/v2/account/activities")
        if isinstance(data, list):
            return [dict(r) for r in data if isinstance(r, dict)]
        return []

    async def health_check(self) -> Any:
        from app.brokers.models import BrokerEnvironment, BrokerHealth

        try:
            await self.get_clock()
            return BrokerHealth(
                healthy=True,
                provider="alpaca",
                environment=BrokerEnvironment.PAPER.value,
                connected=True,
                message="ok",
                as_of=datetime.now(UTC),
            )
        except Exception as exc:  # noqa: BLE001
            return BrokerHealth(
                healthy=False,
                provider="alpaca",
                environment=BrokerEnvironment.PAPER.value,
                connected=False,
                message=str(exc)[:200],
                as_of=datetime.now(UTC),
            )

    def capabilities(self) -> Any:
        from app.brokers.models import BrokerCapabilities

        return BrokerCapabilities(
            provider="alpaca",
            supports_replace=True,
            supports_bracket=True,
            supports_fractional=True,
            supports_short=False,
            supports_extended_hours=True,
            supports_streaming=False,
            is_mock=False,
        )

    async def get_account_canonical(self) -> Any:
        from app.brokers.models import BrokerAccount, BrokerEnvironment, redact_account_id

        raw = await self.get_account()
        return BrokerAccount(
            account_id_reference=redact_account_id(str(raw.get("id"))),
            environment=BrokerEnvironment.PAPER,
            currency=str(raw.get("currency") or "USD"),
            equity=float(raw.get("equity") or 0),
            cash=float(raw.get("cash") or 0),
            buying_power=float(raw.get("buying_power") or 0),
            portfolio_value=float(raw.get("portfolio_value") or raw.get("equity") or 0),
            long_market_value=float(raw.get("long_market_value") or 0),
            short_market_value=float(raw.get("short_market_value") or 0),
            initial_margin=float(raw["initial_margin"]) if raw.get("initial_margin") not in (None, "") else None,
            maintenance_margin=float(raw["maintenance_margin"])
            if raw.get("maintenance_margin") not in (None, "")
            else None,
            daytrade_count=int(raw.get("daytrade_count") or 0),
            pattern_day_trader=bool(raw.get("pattern_day_trader")),
            trading_blocked=bool(raw.get("trading_blocked")),
            transfers_blocked=bool(raw.get("transfers_blocked")),
            account_blocked=bool(raw.get("account_blocked")),
            as_of=datetime.now(UTC),
            source="alpaca",
        )

    async def get_positions_canonical(self) -> list[Any]:
        from app.brokers.models import BrokerPosition

        now = datetime.now(UTC)
        out: list[Any] = []
        for p in await self.get_positions():
            qty = float(p.get("qty") or 0)
            out.append(
                BrokerPosition(
                    symbol=str(p.get("symbol", "")).upper(),
                    quantity=qty,
                    available_quantity=float(p.get("qty_available") or qty),
                    side=str(p.get("side") or ("long" if qty >= 0 else "short")),
                    market_value=float(p.get("market_value") or 0),
                    cost_basis=float(p["cost_basis"]) if p.get("cost_basis") not in (None, "") else None,
                    average_entry_price=float(p["avg_entry_price"])
                    if p.get("avg_entry_price") not in (None, "")
                    else None,
                    current_price=float(p["current_price"]) if p.get("current_price") not in (None, "") else None,
                    unrealized_pl=float(p["unrealized_pl"]) if p.get("unrealized_pl") not in (None, "") else None,
                    unrealized_pl_pct=float(p["unrealized_plpc"]) if p.get("unrealized_plpc") not in (None, "") else None,
                    exchange=str(p.get("exchange") or "") or None,
                    as_of=now,
                    source="alpaca",
                )
            )
        return out


def _parse_dt(value: object) -> datetime | None:
    if isinstance(value, str) and value:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    return None


def _redact_secrets(text: str, settings: Settings) -> str:
    out = text
    for secret in (settings.alpaca_api_key, settings.alpaca_api_secret):
        if secret is not None:
            val = secret.get_secret_value()
            if val:
                out = out.replace(val, "***REDACTED***")
    return out


class SimulatedBroker:  # pragma: no cover - compatibility shim
    """Deprecated alias — use app.brokers.mock.MockBroker."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        from app.brokers.mock import MockBroker

        object.__setattr__(self, "_inner", MockBroker(*args, **kwargs))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "_inner":
            object.__setattr__(self, name, value)
        elif hasattr(self, "_inner"):
            setattr(self._inner, name, value)
        else:
            object.__setattr__(self, name, value)


def get_broker(settings: Settings | None = None) -> BrokerClient:
    from app.brokers.factory import get_broker as _factory_get

    return _factory_get(settings)
