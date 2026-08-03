"""Deterministic Risk Engine implementation."""

from __future__ import annotations

from datetime import UTC, datetime

from app.core.config import Settings, get_settings
from app.risk.types import (
    CheckResult,
    PortfolioRiskView,
    PositionRiskView,
    PreTradeRiskResult,
    RiskLimits,
    SizingResult,
    TradeIntent,
    VetoCode,
)


def limits_from_settings(settings: Settings | None = None) -> RiskLimits:
    cfg = settings or get_settings()
    return RiskLimits(
        starting_cash=cfg.starting_cash,
        max_position_pct=cfg.max_position_pct,
        max_sector_pct=cfg.max_sector_pct,
        max_gross_exposure_pct=cfg.max_gross_exposure_pct,
        min_cash_pct=cfg.min_cash_pct,
        risk_per_trade_pct=cfg.risk_per_trade_pct,
        daily_max_loss_pct=cfg.daily_max_loss_pct,
        max_drawdown_pct=cfg.max_drawdown_pct,
        max_open_positions=cfg.max_open_positions,
        max_consecutive_losses=cfg.max_consecutive_losses,
        cooldown_after_loss_minutes=cfg.cooldown_after_loss_minutes,
        max_consecutive_losses_halt_day=cfg.max_consecutive_losses_halt_day,
        min_avg_daily_volume=cfg.min_avg_daily_volume,
        max_bid_ask_spread_bps=cfg.max_bid_ask_spread_bps,
        min_data_quality_score=cfg.min_data_quality_score,
        max_slippage_bps=cfg.max_slippage_bps,
        penny_stock_max_price=cfg.penny_stock_max_price,
        allow_leveraged_etfs=cfg.allow_leveraged_etfs,
    )


class DeterministicRiskEngine:
    """Pure-Python risk math and Hard Veto evaluation."""

    def __init__(self, limits: RiskLimits | None = None) -> None:
        self.limits = limits or limits_from_settings()

    def stop_distance(self, entry_price: float, stop_price: float) -> float:
        if entry_price <= 0:
            raise ValueError("entry_price must be positive")
        distance = abs(entry_price - stop_price)
        if distance <= 0:
            raise ValueError("stop_distance must be positive")
        return distance

    def drawdown_pct(self, peak_equity: float, equity: float) -> float:
        if peak_equity <= 0:
            return 0.0
        dd = (peak_equity - equity) / peak_equity * 100.0
        return max(0.0, dd)

    def sector_exposure_pct(
        self,
        positions: list[PositionRiskView],
        equity: float,
        sector: str,
        additional_notional: float = 0.0,
    ) -> float:
        if equity <= 0:
            return 0.0
        current = sum(p.market_value for p in positions if p.sector == sector)
        return (current + additional_notional) / equity * 100.0

    def position_size(
        self,
        *,
        equity: float,
        entry_price: float,
        stop_price: float,
        atr: float | None = None,
        existing_position_value: float = 0.0,
    ) -> SizingResult:
        """
        Size by dollar risk = equity * risk_per_trade_pct / 100.

        Shares = floor(dollar_risk / stop_distance), then capped by max position %.
        Optional ATR is used only to widen stop distance if stop is tighter than 1*ATR
        (conservative: never size up on ATR alone).
        """
        if equity <= 0 or entry_price <= 0:
            return SizingResult(shares=0, dollar_risk=0.0, stop_distance=0.0, position_notional=0.0)

        distance = self.stop_distance(entry_price, stop_price)
        if atr is not None and atr > distance:
            distance = atr

        dollar_risk = equity * (self.limits.risk_per_trade_pct / 100.0)
        raw_shares = int(dollar_risk // distance) if distance > 0 else 0
        capped_by: list[str] = []

        max_position_value = equity * (self.limits.max_position_pct / 100.0)
        remaining_room = max(0.0, max_position_value - existing_position_value)
        max_shares_by_position = int(remaining_room // entry_price) if entry_price > 0 else 0

        shares = min(raw_shares, max_shares_by_position)
        if shares < raw_shares:
            capped_by.append("max_position_pct")

        notional = shares * entry_price
        return SizingResult(
            shares=shares,
            dollar_risk=dollar_risk,
            stop_distance=distance,
            position_notional=notional,
            capped_by=capped_by,
        )

    def evaluate_pretrade(
        self,
        portfolio: PortfolioRiskView,
        trade: TradeIntent,
        *,
        allowlist: set[str],
        data_quality_score: float,
        market_session_clear: bool,
        broker_data_consistent: bool,
        now: datetime | None = None,
        seen_idempotency_keys: set[str] | None = None,
    ) -> PreTradeRiskResult:
        now = now or datetime.now(UTC)
        checks: list[CheckResult] = []
        halt_day = False

        def add(
            code: VetoCode,
            passed: bool,
            message: str,
            *,
            hard: bool = True,
            **details: float | str | bool | None,
        ) -> None:
            checks.append(
                CheckResult(code=code, passed=passed, message=message, hard=hard, details=details)
            )

        # --- Portfolio-level hard gates ---
        if portfolio.trading_halted:
            add(VetoCode.TRADING_HALTED, False, "Trading is halted")
            halt_day = True

        if abs(portfolio.daily_pnl_pct) >= self.limits.daily_max_loss_pct and portfolio.daily_pnl_pct < 0:
            add(
                VetoCode.DAILY_LOSS_LIMIT,
                False,
                f"Daily loss {portfolio.daily_pnl_pct:.2f}% exceeds limit "
                f"{self.limits.daily_max_loss_pct:.2f}%",
                daily_pnl_pct=portfolio.daily_pnl_pct,
            )
            halt_day = True
        else:
            add(VetoCode.DAILY_LOSS_LIMIT, True, "Daily loss within limit")

        dd = portfolio.drawdown_pct
        if portfolio.peak_equity is not None:
            dd = self.drawdown_pct(portfolio.peak_equity, portfolio.equity)
        if dd >= self.limits.max_drawdown_pct:
            add(
                VetoCode.MAX_DRAWDOWN,
                False,
                f"Drawdown {dd:.2f}% exceeds max {self.limits.max_drawdown_pct:.2f}%",
                drawdown_pct=dd,
            )
            halt_day = True
        else:
            add(VetoCode.MAX_DRAWDOWN, True, "Drawdown within limit", drawdown_pct=dd)

        if portfolio.consecutive_losses >= self.limits.max_consecutive_losses_halt_day:
            add(
                VetoCode.CONSECUTIVE_LOSSES_HALT,
                False,
                f"Consecutive losses {portfolio.consecutive_losses} halt day trading",
            )
            halt_day = True
        elif portfolio.consecutive_losses >= self.limits.max_consecutive_losses:
            in_cooldown = (
                portfolio.cooldown_until is not None and now < portfolio.cooldown_until
            )
            if in_cooldown and trade.side == "buy":
                add(
                    VetoCode.CONSECUTIVE_LOSSES_COOLDOWN,
                    False,
                    "New buys blocked during loss cooldown",
                )
            else:
                add(
                    VetoCode.CONSECUTIVE_LOSSES_COOLDOWN,
                    True,
                    "Cooldown cleared or non-buy side",
                )
        else:
            add(VetoCode.CONSECUTIVE_LOSSES_COOLDOWN, True, "Consecutive losses OK")

        if data_quality_score < self.limits.min_data_quality_score:
            add(
                VetoCode.LOW_DATA_QUALITY,
                False,
                f"Data quality {data_quality_score:.2f} below "
                f"{self.limits.min_data_quality_score:.2f}",
            )
        else:
            add(VetoCode.LOW_DATA_QUALITY, True, "Data quality OK")

        add(
            VetoCode.BROKER_DATA_MISMATCH,
            broker_data_consistent,
            "Broker data consistent" if broker_data_consistent else "Broker data mismatch",
        )
        add(
            VetoCode.MARKET_SESSION_UNCLEAR,
            market_session_clear,
            "Market session clear" if market_session_clear else "Market session unclear",
        )

        symbol = trade.symbol.upper()
        add(
            VetoCode.NOT_IN_ALLOWLIST,
            symbol in {s.upper() for s in allowlist},
            f"{symbol} allowlist check",
        )

        if trade.entry_price < self.limits.penny_stock_max_price:
            add(VetoCode.PENNY_STOCK, False, f"{symbol} below penny stock threshold")
        else:
            add(VetoCode.PENNY_STOCK, True, "Not a penny stock")

        if trade.is_leveraged_etf and not self.limits.allow_leveraged_etfs:
            add(VetoCode.LEVERAGED_ETF, False, f"{symbol} leveraged ETF blocked")
        else:
            add(VetoCode.LEVERAGED_ETF, True, "Leveraged ETF policy OK")

        has_stop = trade.stop_loss is not None or bool(
            trade.invalidation and trade.invalidation.strip()
        )
        if trade.side == "buy":
            add(
                VetoCode.MISSING_STOP,
                has_stop,
                "Stop/invalidation present" if has_stop else "Missing stop or invalidation",
            )
        else:
            add(VetoCode.MISSING_STOP, True, "Stop not required for non-buy")

        if trade.expected_slippage_bps is not None:
            add(
                VetoCode.EXCESSIVE_SLIPPAGE,
                trade.expected_slippage_bps <= self.limits.max_slippage_bps,
                f"Slippage {trade.expected_slippage_bps} bps",
            )
        if trade.avg_daily_volume is not None:
            add(
                VetoCode.INSUFFICIENT_VOLUME,
                trade.avg_daily_volume >= self.limits.min_avg_daily_volume,
                f"ADV {trade.avg_daily_volume}",
            )
        if trade.bid_ask_spread_bps is not None:
            add(
                VetoCode.EXCESSIVE_SPREAD,
                trade.bid_ask_spread_bps <= self.limits.max_bid_ask_spread_bps,
                f"Spread {trade.bid_ask_spread_bps} bps",
            )

        # Duplicate order prevention
        if trade.idempotency_key and seen_idempotency_keys is not None:
            if trade.idempotency_key in seen_idempotency_keys:
                checks.append(
                    CheckResult(
                        code="duplicate_idempotency_key",
                        passed=False,
                        message="Duplicate idempotency key",
                        hard=True,
                    )
                )

        # --- Sizing & concentration (buy path) ---
        sizing: SizingResult | None = None
        adjusted_qty: float | None = trade.quantity

        if trade.side == "buy" and trade.stop_loss is not None:
            existing = next(
                (p.market_value for p in portfolio.positions if p.symbol.upper() == symbol),
                0.0,
            )
            sizing = self.position_size(
                equity=portfolio.equity,
                entry_price=trade.entry_price,
                stop_price=trade.stop_loss,
                atr=trade.atr,
                existing_position_value=existing,
            )
            adjusted_qty = float(min(trade.quantity, sizing.shares))
            if sizing.shares <= 0:
                checks.append(
                    CheckResult(
                        code=VetoCode.RISK_PER_TRADE,
                        passed=False,
                        message="Position size rounded to zero under risk limits",
                        hard=True,
                    )
                )
            else:
                add(VetoCode.RISK_PER_TRADE, True, "Risk per trade sizing OK")

            notional = (adjusted_qty or 0.0) * trade.entry_price
            projected_weight = (existing + notional) / portfolio.equity * 100.0 if portfolio.equity else 0.0
            add(
                VetoCode.MAX_POSITION_PCT,
                projected_weight <= self.limits.max_position_pct + 1e-9,
                f"Projected position weight {projected_weight:.2f}%",
                projected_weight=projected_weight,
            )

            sector_pct = self.sector_exposure_pct(
                portfolio.positions, portfolio.equity, trade.sector, additional_notional=notional
            )
            add(
                VetoCode.MAX_SECTOR_PCT,
                sector_pct <= self.limits.max_sector_pct + 1e-9,
                f"Projected sector {trade.sector} {sector_pct:.2f}%",
                sector_pct=sector_pct,
            )

            projected_gross = portfolio.gross_exposure_pct + (
                notional / portfolio.equity * 100.0 if portfolio.equity else 0.0
            )
            add(
                VetoCode.MAX_GROSS_EXPOSURE,
                projected_gross <= self.limits.max_gross_exposure_pct + 1e-9,
                f"Projected gross {projected_gross:.2f}%",
            )

            cash_after = portfolio.cash - notional
            cash_pct_after = cash_after / portfolio.equity * 100.0 if portfolio.equity else 0.0
            add(
                VetoCode.MIN_CASH_PCT,
                cash_pct_after >= self.limits.min_cash_pct - 1e-9,
                f"Projected cash {cash_pct_after:.2f}%",
            )

            is_new = not any(p.symbol.upper() == symbol for p in portfolio.positions)
            open_count = len(portfolio.positions) + (1 if is_new else 0)
            add(
                VetoCode.MAX_OPEN_POSITIONS,
                open_count <= self.limits.max_open_positions,
                f"Open positions would be {open_count}",
            )

        hard_vetoes = [
            (c.code if isinstance(c.code, str) else c.code.value)
            for c in checks
            if not c.passed and c.hard
        ]
        approved = not hard_vetoes and not halt_day

        return PreTradeRiskResult(
            approved=approved,
            halt_day=halt_day,
            checks=checks,
            hard_vetoes=hard_vetoes,
            sizing=sizing,
            adjusted_quantity=adjusted_qty if approved else 0.0,
        )
