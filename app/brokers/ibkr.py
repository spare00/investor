"""Interactive Brokers paper adapter via TWS API (IB Gateway).

Requires a running IB Gateway / TWS paper session (default host 127.0.0.1:4002).
Passwords stay in Gateway — this process only uses host/port/clientId/account.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from app.brokers.base import OrderRequest, OrderResult, OrderStatus
from app.brokers.errors import BrokerError
from app.brokers.pricing import round_equity_price
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.core.security import require_execution_allowed
from app.market.venues import venue_for_symbol

logger = get_logger(__name__)

_TERMINAL = frozenset(
    {
        "Filled",
        "Cancelled",
        "ApiCancelled",
        "Inactive",
        "Rejected",
    }
)


def _map_status(raw: str | None) -> OrderStatus:
    value = (raw or "").strip()
    mapping = {
        "PendingSubmit": OrderStatus.NEW,
        "PendingCancel": OrderStatus.ACCEPTED,
        "PreSubmitted": OrderStatus.ACCEPTED,
        "Submitted": OrderStatus.ACCEPTED,
        "ApiPending": OrderStatus.NEW,
        "ValidationError": OrderStatus.ACCEPTED,  # often outside-RTH hold (warn 399), not a hard reject
        "Filled": OrderStatus.FILLED,
        "PartiallyFilled": OrderStatus.PARTIAL,
        "Cancelled": OrderStatus.CANCELED,
        "ApiCancelled": OrderStatus.CANCELED,
        "Inactive": OrderStatus.CANCELED,
        "Rejected": OrderStatus.REJECTED,
    }
    return mapping.get(value, OrderStatus.NEW)


class IbkrBroker:
    """TWS API broker (paper). Uses ``ib_async`` against local Gateway."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._ib: Any | None = None
        self._lock = asyncio.Lock()
        self._ensure_paper_mode()

    def _ensure_paper_mode(self) -> None:
        if self.settings.enable_live_trading:
            raise BrokerError("enable_live_trading_must_be_false")
        if self.settings.broker_environment.lower() != "paper":
            raise BrokerError("broker_environment_must_be_paper")
        mode = require_execution_allowed(self.settings)
        if mode.value == "live":
            raise BrokerError("live_execution_mode_blocked")
        port = int(self.settings.ibkr_port)
        # Soft guard: common live ports — still allow override for custom setups.
        if port in {7496, 4001} and not self.settings.ibkr_allow_live_ports:
            raise BrokerError(f"ibkr_port_looks_live:{port}")

    async def _ensure_connected(self) -> Any:
        self._ensure_paper_mode()
        async with self._lock:
            if self._ib is not None and self._ib.isConnected():
                return self._ib
            try:
                from ib_async import IB
            except ImportError as exc:  # pragma: no cover
                raise BrokerError("ib_async_not_installed") from exc

            ib = IB()
            host = self.settings.ibkr_host
            port = int(self.settings.ibkr_port)
            client_id = int(self.settings.ibkr_client_id)
            timeout = max(5, int(self.settings.broker_request_timeout_seconds))
            try:
                await ib.connectAsync(
                    host,
                    port,
                    clientId=client_id,
                    readonly=False,
                    timeout=timeout,
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("ibkr_connect_failed", host=host, port=port)
                raise BrokerError(f"ibkr_connect_failed:{exc}") from exc
            if not ib.isConnected():
                raise BrokerError("ibkr_not_connected")
            # Prefer configured account when multiple are present.
            accounts = list(ib.managedAccounts() or [])
            wanted = (self.settings.ibkr_account or "").strip()
            if wanted and accounts and wanted not in accounts:
                logger.warning(
                    "ibkr_account_not_in_managed",
                    wanted=wanted,
                    managed=accounts,
                )
            self._ib = ib
            logger.info(
                "ibkr_connected",
                host=host,
                port=port,
                client_id=client_id,
                accounts=accounts,
            )
            return ib

    async def disconnect(self) -> None:
        async with self._lock:
            if self._ib is not None and self._ib.isConnected():
                self._ib.disconnect()
            self._ib = None

    def _account_id(self, ib: Any) -> str:
        wanted = (self.settings.ibkr_account or "").strip()
        accounts = list(ib.managedAccounts() or [])
        if wanted:
            return wanted
        if accounts:
            return str(accounts[0])
        raise BrokerError("ibkr_no_managed_accounts")

    async def _qualify_stock(
        self,
        ib: Any,
        symbol: str,
        *,
        currency: str | None = None,
        venue: str | None = None,
    ) -> Any:
        from ib_async import Stock

        from app.market.venues import ib_qualify_candidates

        sym = symbol.upper().strip()
        candidates = ib_qualify_candidates(self.settings, venue=venue)
        if currency:
            # Prefer an explicit currency override first.
            ccy = currency.upper()
            preferred = [(ex, c) for ex, c in candidates if c == ccy]
            rest = [(ex, c) for ex, c in candidates if c != ccy]
            candidates = preferred + rest

        last_exc: Exception | None = None
        for exchange, ccy in candidates:
            contract = Stock(sym, exchange, ccy)
            try:
                qualified = await ib.qualifyContractsAsync(contract)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                continue
            hit = next((c for c in (qualified or []) if getattr(c, "conId", 0)), None)
            if hit is not None:
                return hit
        if last_exc is not None:
            raise BrokerError(f"ibkr_qualify_failed:{sym}:{last_exc}") from last_exc
        raise BrokerError(f"ibkr_contract_not_found:{sym}")

    def _build_order(self, request: OrderRequest) -> Any:
        from ib_async import LimitOrder, MarketOrder, StopLimitOrder, StopOrder

        side = "BUY" if request.side.value.lower() == "buy" else "SELL"
        tif = (request.time_in_force or "day").upper()
        if tif == "DAY":
            tif = "DAY"
        elif tif in {"GTC", "IOC", "FOK"}:
            pass
        else:
            tif = "DAY"

        qty = float(request.qty)
        otype = (request.order_type or "market").lower()
        if otype == "market":
            order = MarketOrder(side, qty)
        elif otype == "limit":
            if request.limit_price is None:
                raise BrokerError("ibkr_limit_requires_price")
            order = LimitOrder(side, qty, round_equity_price(float(request.limit_price)))
        elif otype == "stop":
            if request.stop_price is None:
                raise BrokerError("ibkr_stop_requires_price")
            order = StopOrder(side, qty, round_equity_price(float(request.stop_price)))
        elif otype == "stop_limit":
            if request.stop_price is None or request.limit_price is None:
                raise BrokerError("ibkr_stop_limit_requires_prices")
            order = StopLimitOrder(
                side,
                qty,
                round_equity_price(float(request.limit_price)),
                round_equity_price(float(request.stop_price)),
            )
        else:
            raise BrokerError(f"ibkr_unsupported_order_type:{otype}")

        order.tif = tif
        order.outsideRth = bool(self.settings.enable_extended_hours_orders)
        if request.idempotency_key:
            # IB orderRef is free-form; keep short for gateway UIs.
            order.orderRef = request.idempotency_key[:32]
        account = (self.settings.ibkr_account or "").strip()
        if account:
            order.account = account
        return order

    def _trade_to_result(self, trade: Any) -> OrderResult:
        status = trade.orderStatus
        filled = float(status.filled or 0)
        avg = float(status.avgFillPrice or 0) or None
        order_id = status.orderId or getattr(trade.order, "orderId", 0) or 0
        perm_id = getattr(status, "permId", None) or getattr(trade.order, "permId", None)
        broker_id = str(perm_id or order_id)
        raw = {
            "order_id": order_id,
            "perm_id": perm_id,
            "status": status.status,
            "filled": filled,
            "remaining": float(status.remaining or 0),
            "avg_fill_price": avg,
            "why_held": getattr(status, "whyHeld", "") or "",
            "order_ref": getattr(trade.order, "orderRef", "") or "",
            "symbol": getattr(getattr(trade, "contract", None), "symbol", None),
        }
        return OrderResult(
            broker_order_id=broker_id,
            status=_map_status(str(status.status)),
            submitted_at=datetime.now(UTC),
            filled_qty=filled,
            avg_fill_price=avg,
            raw=raw,
        )

    async def _wait_trade(self, ib: Any, trade: Any, *, seconds: float = 3.0) -> OrderResult:
        deadline = asyncio.get_event_loop().time() + seconds
        while asyncio.get_event_loop().time() < deadline:
            st = str(trade.orderStatus.status or "")
            if st in _TERMINAL or float(trade.orderStatus.filled or 0) > 0:
                break
            await asyncio.sleep(0.15)
        return self._trade_to_result(trade)

    async def submit_order(self, request: OrderRequest) -> OrderResult:
        ib = await self._ensure_connected()
        contract = await self._qualify_stock(ib, request.symbol, venue=request.venue)
        order = self._build_order(request)
        try:
            trade = ib.placeOrder(contract, order)
            result = await self._wait_trade(ib, trade)
        except BrokerError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("ibkr_submit_failed", symbol=request.symbol)
            raise BrokerError(f"ibkr_submit_failed:{exc}") from exc
        logger.info(
            "ibkr_order_submitted",
            symbol=request.symbol,
            side=request.side.value,
            qty=request.qty,
            broker_order_id=result.broker_order_id,
            status=result.status.value,
        )
        return result

    def _find_trade(self, ib: Any, broker_order_id: str) -> Any | None:
        needle = str(broker_order_id)
        for trade in list(ib.trades()) + list(ib.openTrades()):
            status = trade.orderStatus
            oid = str(status.orderId or getattr(trade.order, "orderId", "") or "")
            pid = str(getattr(status, "permId", None) or getattr(trade.order, "permId", "") or "")
            if needle in {oid, pid}:
                return trade
        return None

    async def cancel_order(self, broker_order_id: str) -> OrderResult:
        ib = await self._ensure_connected()
        trade = self._find_trade(ib, broker_order_id)
        if trade is None:
            raise BrokerError(f"ibkr_order_not_found:{broker_order_id}")
        try:
            ib.cancelOrder(trade.order)
            return await self._wait_trade(ib, trade, seconds=2.0)
        except Exception as exc:  # noqa: BLE001
            raise BrokerError(f"ibkr_cancel_failed:{exc}") from exc

    async def cancel_all_orders(self) -> int:
        ib = await self._ensure_connected()
        open_trades = list(ib.openTrades())
        for trade in open_trades:
            try:
                ib.cancelOrder(trade.order)
            except Exception:  # noqa: BLE001
                logger.exception("ibkr_cancel_all_item_failed")
        await asyncio.sleep(0.5)
        return len(open_trades)

    async def get_order(self, broker_order_id: str) -> OrderResult:
        ib = await self._ensure_connected()
        trade = self._find_trade(ib, broker_order_id)
        if trade is None:
            raise BrokerError(f"ibkr_order_not_found:{broker_order_id}")
        return self._trade_to_result(trade)

    async def get_order_by_client_id(self, client_order_id: str) -> OrderResult | None:
        ib = await self._ensure_connected()
        key = (client_order_id or "")[:32]
        for trade in list(ib.trades()) + list(ib.openTrades()):
            ref = getattr(trade.order, "orderRef", "") or ""
            if ref == key:
                return self._trade_to_result(trade)
        return None

    async def get_open_orders(self) -> list[OrderResult]:
        ib = await self._ensure_connected()
        try:
            await ib.reqOpenOrdersAsync()
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(0.2)
        return [self._trade_to_result(t) for t in ib.openTrades()]

    async def get_positions(self) -> list[dict[str, object]]:
        ib = await self._ensure_connected()
        account = self._account_id(ib)
        out: list[dict[str, object]] = []
        # Request fresh positions, then read cache (avoid sync _run helpers).
        try:
            await ib.reqPositionsAsync()
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(0.2)

        portfolio = [p for p in ib.portfolio() if str(p.account) == account]
        if portfolio:
            for item in portfolio:
                qty = float(item.position or 0)
                if abs(qty) < 1e-12:
                    continue
                avg = float(item.averageCost or 0)
                out.append(
                    {
                        "symbol": str(item.contract.symbol).upper(),
                        "qty": qty,
                        "avg_entry_price": avg,
                        "market_value": float(item.marketValue or 0),
                        "cost_basis": abs(avg * qty),
                        "unrealized_pl": float(item.unrealizedPNL or 0),
                        "side": "long" if qty > 0 else "short",
                        "exchange": getattr(item.contract, "primaryExchange", None)
                        or getattr(item.contract, "exchange", None),
                        "currency": getattr(item.contract, "currency", None),
                        "account": account,
                    }
                )
            return out

        for pos in ib.positions(account):
            qty = float(pos.position or 0)
            if abs(qty) < 1e-12:
                continue
            avg = float(pos.avgCost or 0)
            out.append(
                {
                    "symbol": str(pos.contract.symbol).upper(),
                    "qty": qty,
                    "avg_entry_price": avg,
                    "market_value": 0.0,
                    "cost_basis": abs(avg * qty),
                    "unrealized_pl": 0.0,
                    "side": "long" if qty > 0 else "short",
                    "exchange": getattr(pos.contract, "exchange", None),
                    "currency": getattr(pos.contract, "currency", None),
                    "account": account,
                }
            )
        return out

    async def _summary_map(self, ib: Any, account: str) -> dict[str, tuple[str, str]]:
        rows = await ib.accountSummaryAsync(account)
        # tag -> (value, currency)
        return {str(r.tag): (str(r.value), str(r.currency or "")) for r in rows}

    async def get_account(self) -> dict[str, object]:
        ib = await self._ensure_connected()
        account = self._account_id(ib)
        summary = await self._summary_map(ib, account)

        def _val(tag: str, default: float = 0.0) -> float:
            raw = summary.get(tag)
            if not raw:
                return default
            try:
                return float(raw[0])
            except (TypeError, ValueError):
                return default

        equity = _val("NetLiquidation") or _val("EquityWithLoanValue")
        cash = _val("TotalCashValue") or _val("CashBalance")
        buying_power = _val("BuyingPower") or _val("AvailableFunds")
        currency = self.settings.ibkr_default_currency or "USD"
        for tag in ("NetLiquidation", "TotalCashValue"):
            if tag in summary and summary[tag][1]:
                currency = summary[tag][1]
                break
        return {
            "id": account,
            "account_number": account,
            "equity": equity,
            "cash": cash,
            "buying_power": buying_power,
            "portfolio_value": equity,
            "currency": currency,
            "long_market_value": _val("GrossPositionValue"),
            "trading_blocked": False,
            "account_blocked": False,
            "pattern_day_trader": False,
            "daytrade_count": 0,
            "source": "ibkr",
        }

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
            short_market_value=0.0,
            daytrade_count=0,
            pattern_day_trader=False,
            trading_blocked=False,
            transfers_blocked=False,
            account_blocked=False,
            as_of=datetime.now(UTC),
            source="ibkr",
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
                    available_quantity=qty,
                    side=str(p.get("side") or ("long" if qty >= 0 else "short")),
                    average_entry_price=float(p.get("avg_entry_price") or 0),
                    market_value=float(p.get("market_value") or 0),
                    cost_basis=float(p.get("cost_basis") or 0),
                    unrealized_pl=float(p.get("unrealized_pl") or 0),
                    current_price=None,
                    exchange=str(p.get("exchange") or "") or None,
                    as_of=now,
                    source="ibkr",
                )
            )
        return out

    async def replace_order(self, broker_order_id: str, replacement: OrderRequest) -> OrderResult:
        # IB modify = cancel + new, or orderModify — keep simple: cancel then submit.
        await self.cancel_order(broker_order_id)
        return await self.submit_order(replacement)

    async def close_position(self, symbol: str, *, qty: float | None = None) -> OrderResult | None:
        positions = await self.get_positions()
        row = next((p for p in positions if str(p.get("symbol")).upper() == symbol.upper()), None)
        if row is None:
            return None
        held = float(row.get("qty") or 0)
        if abs(held) < 1e-12:
            return None
        close_qty = abs(float(qty)) if qty is not None else abs(held)
        close_qty = min(close_qty, abs(held))
        from app.brokers.base import OrderSide

        side = OrderSide.SELL if held > 0 else OrderSide.BUY
        return await self.submit_order(
            OrderRequest(
                symbol=symbol.upper(),
                side=side,
                qty=close_qty,
                order_type="market",
                idempotency_key=f"close-{symbol.upper()}-{int(datetime.now(UTC).timestamp())}",
                venue=venue_for_symbol(
                    symbol,
                    self.settings,
                    exchange=str(row.get("exchange") or "") or None,
                    currency=str(row.get("currency") or "") or None,
                ).value,
            )
        )

    async def close_all_positions(self) -> int:
        n = 0
        for p in await self.get_positions():
            sym = str(p.get("symbol") or "")
            if not sym:
                continue
            result = await self.close_position(sym)
            if result is not None:
                n += 1
        return n

    async def health_check(self) -> Any:
        from app.brokers.models import BrokerEnvironment, BrokerHealth

        try:
            ib = await self._ensure_connected()
            ok = bool(ib.isConnected())
            return BrokerHealth(
                healthy=ok,
                provider="ibkr",
                environment=BrokerEnvironment.PAPER.value,
                connected=ok,
                message="ok" if ok else "disconnected",
                as_of=datetime.now(UTC),
            )
        except Exception as exc:  # noqa: BLE001
            return BrokerHealth(
                healthy=False,
                provider="ibkr",
                environment=BrokerEnvironment.PAPER.value,
                connected=False,
                message=str(exc)[:200],
                as_of=datetime.now(UTC),
            )

    def capabilities(self) -> Any:
        from app.brokers.models import BrokerCapabilities

        return BrokerCapabilities(
            provider="ibkr",
            supports_replace=True,
            supports_bracket=False,
            supports_fractional=False,
            supports_short=False,
            supports_extended_hours=True,
            supports_streaming=True,
            is_mock=False,
        )

    async def ping(self) -> dict[str, Any]:
        """Connectivity probe for CLI / ops."""
        ib = await self._ensure_connected()
        account = await self.get_account()
        positions = await self.get_positions()
        aapl = None
        try:
            c = await self._qualify_stock(ib, "AAPL", currency="USD")
            aapl = {
                "con_id": getattr(c, "conId", None),
                "symbol": c.symbol,
                "primary_exchange": getattr(c, "primaryExchange", None),
                "currency": c.currency,
            }
        except BrokerError as exc:
            aapl = {"error": str(exc)}
        return {
            "connected": ib.isConnected(),
            "managed_accounts": list(ib.managedAccounts() or []),
            "account": {
                "id": account.get("id"),
                "equity": account.get("equity"),
                "cash": account.get("cash"),
                "currency": account.get("currency"),
            },
            "positions": len(positions),
            "sample_position": positions[0] if positions else None,
            "aapl": aapl,
            "host": self.settings.ibkr_host,
            "port": self.settings.ibkr_port,
            "client_id": self.settings.ibkr_client_id,
        }
