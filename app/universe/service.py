"""Universe service — seed, AI refresh, entry gate, focus set for collection."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.universe_manager import UniverseManagerAgent
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.core.timeutils import utc_now
from app.models.entities import FocusSetSnapshot, WatchlistSymbol
from app.schemas.common import TraceMetadata
from app.schemas.universe_manager import UniverseManagerInput, UniverseManagerOutput
from app.universe.horizons import UniverseHorizon, all_horizon_summaries, policy_for

logger = get_logger(__name__)


class UniverseService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        settings: Settings | None = None,
        agent: UniverseManagerAgent | None = None,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.agent = agent or UniverseManagerAgent(settings=self.settings)

    def is_dynamic(self) -> bool:
        return (self.settings.universe_mode or "dynamic").lower() == "dynamic"

    async def ensure_seeded(self) -> int:
        """Bootstrap missing watchlist rows from US/AU allowlists."""
        from app.market.venues import Venue, enabled_venues

        rows = list((await self.session.execute(select(WatchlistSymbol))).scalars().all())
        existing = {r.symbol.upper() for r in rows}
        now = utc_now()
        n = 0

        def _horizon_for(sym: str, venue: Venue, index: int) -> str:
            if venue == Venue.AU:
                if sym == "NDQ":
                    return UniverseHorizon.SCALP.value
                if sym in {"VAS", "JPEQ"}:
                    return UniverseHorizon.DAY.value
                return UniverseHorizon.SHORT.value
            indexes = {"SPY", "QQQ", "IWM", "DIA"}
            if sym in indexes:
                return UniverseHorizon.SCALP.value if index < 2 else UniverseHorizon.DAY.value
            if sym in {"NVDA", "TSLA", "AMD", "META", "AAPL"}:
                return UniverseHorizon.DAY.value
            if sym in {"MSFT", "AMZN", "GOOGL", "AVGO"}:
                return UniverseHorizon.SHORT.value
            return UniverseHorizon.SHORT.value

        seed_books: list[tuple[Venue, list[str]]] = [(Venue.US, list(self.settings.trade_allowlist))]
        if Venue.AU in enabled_venues(self.settings) or self.settings.trade_allowlist_au:
            # Always seed AU allowlist rows when configured so JPEQ etc. are entry-eligible
            # once ENABLED_VENUES includes AU (or allowlist is non-empty for manual books).
            if Venue.AU in enabled_venues(self.settings):
                seed_books.append((Venue.AU, list(self.settings.trade_allowlist_au)))

        for venue, symbols in seed_books:
            for i, raw in enumerate(symbols):
                sym = raw.upper().strip()
                if not sym or sym in existing:
                    continue
                horizon = _horizon_for(sym, venue, i)
                self.session.add(
                    WatchlistSymbol(
                        id=uuid4(),
                        symbol=sym,
                        horizon=horizon,
                        status="active",
                        priority=max(10, 85 - i * 3),
                        thesis=f"Seeded into {horizon} book from {venue.value} allowlist",
                        invalidation="Liquidity failure or thesis break",
                        source="seed",
                        last_reviewed_at=now,
                        payload={"seed_index": i, "venue": venue.value},
                    )
                )
                existing.add(sym)
                n += 1
        n += await self._repair_seed_horizons(rows)
        if n:
            await self.session.flush()
            logger.info("universe_seeded", count=n)
        return n

    async def _repair_seed_horizons(self, rows: list[WatchlistSymbol]) -> int:
        """Keep AU tape (NDQ) on scalp even if it was seeded as short/day earlier."""
        from app.models.entities import Position

        desired = {"NDQ": UniverseHorizon.SCALP.value}
        held = {
            str(p.symbol or "").upper()
            for p in (await self.session.execute(select(Position))).scalars().all()
            if abs(float(p.quantity or 0)) > 1e-9
        }
        changed = 0
        for row in rows:
            want = desired.get(str(row.symbol or "").upper())
            if not want or row.horizon == want:
                continue
            if str(row.symbol or "").upper() in held:
                continue
            if str(row.source or "") not in {"seed", "repair", ""}:
                continue
            row.horizon = want
            row.thesis = f"Seeded into {want} book from AU allowlist"
            row.source = "seed"
            changed += 1
        return changed

    async def list_active(self) -> list[WatchlistSymbol]:
        result = await self.session.execute(
            select(WatchlistSymbol)
            .where(WatchlistSymbol.status == "active")
            .order_by(WatchlistSymbol.priority.desc(), WatchlistSymbol.symbol.asc())
        )
        return list(result.scalars().all())

    async def entry_universe(self, *, venue: str | None = None) -> set[str]:
        """Symbols allowed for NEW entries (optionally scoped to a venue)."""
        from app.market.venues import combined_entry_allowlist, parse_venue

        want = parse_venue(venue)
        allow = (
            self.settings.allowlist_for_venue(want)
            if want is not None
            else combined_entry_allowlist(self.settings)
        )
        if not self.is_dynamic():
            return set(allow)
        await self.ensure_seeded()
        active = await self.list_active()
        if not active:
            return set(allow)
        from app.universe.book_strategy import is_active_strategy_horizon

        active_syms = {
            row.symbol.upper()
            for row in active
            if is_active_strategy_horizon(row.horizon)
        }
        return active_syms & set(allow)

    async def horizon_by_symbol(self) -> dict[str, str]:
        await self.ensure_seeded()
        rows = list((await self.session.execute(select(WatchlistSymbol))).scalars().all())
        return {r.symbol.upper(): r.horizon for r in rows if r.status == "active"}

    async def collection_universe(
        self,
        holdings: list[str] | None = None,
        *,
        venue: str | None = None,
    ) -> list[str]:
        """Symbols to collect/analyze this cycle: holdings ∪ focus (or watchlist capped)."""
        from app.market.venues import Venue, parse_venue

        want = parse_venue(venue)
        held = sorted({h.upper() for h in (holdings or []) if h})
        if want == Venue.AU:
            bench = (self.settings.primary_benchmark_au or "VAS").upper()
            allow = self.settings.allowlist_for_venue(Venue.AU)
        elif want == Venue.US:
            bench = (self.settings.primary_benchmark or "SPY").upper()
            allow = self.settings.allowlist_for_venue(Venue.US)
        else:
            bench = (self.settings.primary_benchmark or "SPY").upper()
            allow = None

        if not self.is_dynamic():
            base = set(allow) if allow is not None else set(self.settings.trade_allowlist)
            return self._with_index_symbols(sorted({*base, *held, bench}), want)

        await self.ensure_seeded()
        active_set = {r.symbol.upper() for r in await self.list_active()}
        if allow is not None:
            active_set &= set(allow)
            held_scoped = [h for h in held if h in allow or h == bench]
        else:
            held_scoped = held
        allowed = active_set | set(held_scoped) | {bench}
        latest = await self._latest_focus()
        if latest and latest.symbols:
            # Drop sold / paused names that lingered in an older focus snapshot.
            focus = [str(s).upper() for s in latest.symbols if str(s).upper() in allowed]
            if focus or held_scoped:
                return self._with_index_symbols(
                    await self._filter_collection_symbols(
                        sorted({*focus, *held_scoped, bench}),
                        held=set(held_scoped),
                        bench=bench,
                    ),
                    want,
                )

        active = await self.list_active()
        if allow is not None:
            active = [r for r in active if r.symbol.upper() in allow]
        ranked = sorted(active, key=lambda r: (-r.priority, r.symbol))
        focus = [r.symbol.upper() for r in ranked[: self.settings.universe_focus_limit]]
        return self._with_index_symbols(
            await self._filter_collection_symbols(
                sorted({*focus, *held_scoped, bench}),
                held=set(held_scoped),
                bench=bench,
            ),
            want,
        )

    def _with_index_symbols(self, symbols: list[str], venue: Any) -> list[str]:
        if venue is None:
            return list(symbols)
        from app.market.book_context import index_symbols_for_venue

        return sorted({str(s).upper() for s in symbols} | set(index_symbols_for_venue(venue, self.settings)))

    async def _filter_collection_symbols(
        self,
        symbols: list[str],
        *,
        held: set[str],
        bench: str,
    ) -> list[str]:
        """Keep holdings/benchmark always; drop medium from research/entry sets."""
        from app.universe.book_strategy import is_active_strategy_horizon

        hz = await self.horizon_by_symbol()
        out: list[str] = []
        for sym in symbols:
            if sym in held or sym == bench:
                out.append(sym)
                continue
            if is_active_strategy_horizon(hz.get(sym)):
                out.append(sym)
        return out

    async def snapshot(self) -> dict[str, Any]:
        await self.ensure_seeded()
        rows = list(
            (
                await self.session.execute(
                    select(WatchlistSymbol).order_by(
                        WatchlistSymbol.status.asc(),
                        WatchlistSymbol.priority.desc(),
                        WatchlistSymbol.symbol.asc(),
                    )
                )
            )
            .scalars()
            .all()
        )
        by_horizon: dict[str, list[dict[str, Any]]] = {h.value: [] for h in UniverseHorizon}
        for r in rows:
            item = self._row_dict(r)
            by_horizon.setdefault(r.horizon, []).append(item)
        focus = await self._latest_focus()
        screened, screen_meta = await self._screened_candidate_pool()
        return {
            "mode": self.settings.universe_mode,
            "watchlist": [self._row_dict(r) for r in rows],
            "by_horizon": by_horizon,
            "horizon_policies": all_horizon_summaries(),
            "focus": None
            if focus is None
            else {
                "as_of": focus.as_of.isoformat(),
                "session_date": focus.session_date,
                "symbols": focus.symbols,
                "holdings": focus.holdings,
                "rationale": focus.rationale,
                "hygiene": (focus.payload or {}).get("hygiene")
                if isinstance(focus.payload, dict)
                else None,
            },
            "limits": {
                "watchlist": self.settings.universe_watchlist_limit,
                "focus": self.settings.universe_focus_limit,
            },
            "candidate_pool": screened,
            "screener": screen_meta,
            "allow_candidate_adds": self.settings.universe_allow_candidate_adds,
            "book_usage": await self._book_usage(),
            "recent_outcomes": await self._outcome_snapshot_summary(),
        }

    async def _outcome_snapshot_summary(self) -> dict[str, Any]:
        from app.universe.outcomes import recent_outcome_stats

        try:
            full = await recent_outcome_stats(self.session, lookback_days=90)
        except Exception as exc:  # noqa: BLE001 — snapshot must not fail closed on analytics
            logger.warning("universe_outcome_snapshot_failed", error=str(exc)[:200])
            return {"error": str(exc)[:200]}
        return {
            "lookback_days": full.get("lookback_days"),
            "by_horizon": full.get("by_horizon"),
            "by_source": full.get("by_source"),
            "symbols_with_trades": len(full.get("by_symbol") or []),
            "negative_signals": [
                s
                for s in (full.get("by_symbol") or [])
                if s.get("signal") == "negative"
            ][:12],
        }

    async def _stamp_outcome_stats(self, outcomes: dict[str, Any]) -> None:
        """Persist observational stats onto watchlist payload (no priority mutation)."""
        by_sym = {
            str(s.get("symbol") or "").upper(): s
            for s in (outcomes.get("by_symbol") or [])
            if s.get("symbol")
        }
        if not by_sym:
            return
        rows = list(
            (
                await self.session.execute(
                    select(WatchlistSymbol).where(WatchlistSymbol.status != "removed")
                )
            )
            .scalars()
            .all()
        )
        stamped = 0
        for row in rows:
            pack = by_sym.get(row.symbol.upper())
            if not pack:
                continue
            payload = dict(row.payload or {})
            payload["last_outcome_stats"] = {
                "trade_count": pack.get("trade_count"),
                "win_rate": pack.get("win_rate"),
                "expectancy": pack.get("expectancy"),
                "total_pnl": pack.get("total_pnl"),
                "signal": pack.get("signal"),
                "horizon": pack.get("horizon"),
                "as_of": outcomes.get("period_end"),
                "lookback_days": outcomes.get("lookback_days"),
            }
            row.payload = payload
            stamped += 1
        if stamped:
            await self.session.flush()
            logger.info("universe_outcome_stats_stamped", symbols=stamped)

    async def _book_usage(self) -> dict[str, Any]:
        from app.models import Position
        from app.universe.caps import count_open_by_horizon
        from app.universe.horizons import HORIZON_POLICIES, UniverseHorizon

        held = [
            p.symbol.upper()
            for p in (await self.session.execute(select(Position))).scalars().all()
            if p.quantity
        ]
        horizons = await self.horizon_by_symbol()
        counts = count_open_by_horizon(held, horizons)
        out: dict[str, Any] = {}
        for h in UniverseHorizon:
            pol = HORIZON_POLICIES[h]
            cur = int(counts.get(h.value, 0))
            out[h.value] = {
                "open": cur,
                "max_positions": pol.max_positions,
                "label_ko": pol.label_ko,
                "full": cur >= pol.max_positions,
            }
        return out

    def _candidate_pool(
        self,
        *,
        themes: list[str] | None = None,
        market_regime: str | None = None,
    ) -> list[str]:
        from app.universe.candidates import ranked_candidate_pool

        return ranked_candidate_pool(
            self.settings, themes=themes, market_regime=market_regime
        )

    async def _screened_candidate_pool(
        self,
        *,
        themes: list[str] | None = None,
        market_regime: str | None = None,
    ) -> tuple[list[str], dict[str, Any]]:
        ranked = self._candidate_pool(themes=themes, market_regime=market_regime)
        from app.universe.screener import screen_candidates

        result = await screen_candidates(self.session, self.settings, ranked)
        return result.passed, result.to_dict()

    async def last_llm_refresh_at(self) -> datetime | None:
        """Most recent focus snapshot produced by Universe Manager (LLM)."""
        row = (
            await self.session.execute(
                select(FocusSetSnapshot)
                .where(FocusSetSnapshot.source == "universe_manager")
                .order_by(FocusSetSnapshot.as_of.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return row.as_of if row is not None else None

    def llm_refresh_interval(self) -> timedelta:
        days = max(1, int(self.settings.universe_refresh_min_interval_days))
        return timedelta(days=days)

    async def llm_refresh_due(self, *, now: datetime | None = None) -> bool:
        """True when Universe Manager LLM is allowed (weekly by default)."""
        last = await self.last_llm_refresh_at()
        if last is None:
            return True
        stamp = last if last.tzinfo is not None else last.replace(tzinfo=UTC)
        return (now or utc_now()) - stamp >= self.llm_refresh_interval()

    async def refresh(
        self,
        *,
        holdings: list[str] | None = None,
        market_regime: str | None = None,
        themes: list[str] | None = None,
        session_date: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """Run Universe Manager and persist watchlist + focus.

        LLM runs at most every ``universe_refresh_min_interval_days`` (default 7)
        and, by default, only on operator-timezone weekends unless ``force=True``.
        Between LLM runs, rebuilds focus without the model so premarket/scheduler
        do not burn weekday trading budget.
        """
        await self.ensure_seeded()
        if not self.settings.universe_manager_enabled:
            focus = await self.build_focus_without_llm(holdings=holdings or [], session_date=session_date)
            return {"skipped": True, "reason": "universe_manager_disabled", "focus": focus}

        if not force and bool(self.settings.universe_refresh_weekend_only):
            from app.universe.schedule import is_operator_weekend

            if not is_operator_weekend(self.settings):
                focus = await self.build_focus_without_llm(
                    holdings=holdings or [], session_date=session_date
                )
                hygiene = await self.hygiene_active_watchlist(holdings=holdings or [])
                logger.info("universe_refresh_deferred_weekend")
                return {
                    "skipped": True,
                    "reason": "weekend_only",
                    "focus": focus,
                    "hygiene": hygiene,
                }

        if not force and not await self.llm_refresh_due():
            last = await self.last_llm_refresh_at()
            focus = await self.build_focus_without_llm(
                holdings=holdings or [], session_date=session_date
            )
            hygiene = await self.hygiene_active_watchlist(holdings=holdings or [])
            logger.info(
                "universe_refresh_deferred_weekly",
                last_llm_at=last.isoformat() if last else None,
                min_interval_days=int(self.settings.universe_refresh_min_interval_days),
            )
            return {
                "skipped": True,
                "reason": "min_interval",
                "last_llm_at": last.isoformat() if last else None,
                "min_interval_days": int(self.settings.universe_refresh_min_interval_days),
                "focus": focus,
                "hygiene": hygiene,
            }

        paused = list(
            (
                await self.session.execute(
                    select(WatchlistSymbol).where(WatchlistSymbol.status != "removed")
                )
            )
            .scalars()
            .all()
        )
        screened, screen_meta = await self._screened_candidate_pool(
            themes=themes, market_regime=market_regime
        )
        from app.market.venues import Venue, enabled_venues
        from app.universe.candidates import combined_seed_pool
        from app.universe.outcomes import recent_outcome_stats

        outcomes = await recent_outcome_stats(self.session, lookback_days=90)
        venues = [v.value for v in enabled_venues(self.settings)]
        seed_by_venue: dict[str, list[str]] = {
            Venue.US.value: [s.upper() for s in self.settings.trade_allowlist if s.strip()],
        }
        if Venue.AU in enabled_venues(self.settings):
            seed_by_venue[Venue.AU.value] = [
                s.upper() for s in self.settings.trade_allowlist_au if s.strip()
            ]
        payload = UniverseManagerInput(
            as_of=utc_now(),
            current_watchlist=[self._row_dict(r) for r in paused],
            holdings=[h.upper() for h in (holdings or [])],
            seed_pool=combined_seed_pool(self.settings),
            seed_pool_by_venue=seed_by_venue,
            enabled_venues=venues,
            candidate_pool=screened,
            market_regime=market_regime,
            themes=themes or [],
            horizon_policies=all_horizon_summaries(),
            watchlist_limit=self.settings.universe_watchlist_limit,
            focus_limit=self.settings.universe_focus_limit,
            recent_outcomes=outcomes,
            trace=TraceMetadata(source_data_timestamp=utc_now()),
        )
        out = await self.agent.run(payload)
        await self._apply_proposals(out, candidate_symbols=set(screened))
        await self._stamp_outcome_stats(outcomes)
        hygiene = await self.hygiene_active_watchlist(holdings=holdings or [])
        focus_doc = await self._persist_focus(
            symbols=out.focus_symbols,
            holdings=holdings or [],
            rationale=out.focus_rationale,
            session_date=session_date,
            source="universe_manager",
            extra={
                "notes": out.notes,
                "quality": out.data_quality_score,
                "screener": screen_meta,
                "hygiene": hygiene,
                "enabled_venues": venues,
                "outcomes_summary": {
                    "by_horizon": outcomes.get("by_horizon"),
                    "by_source": outcomes.get("by_source"),
                    "symbol_count": len(outcomes.get("by_symbol") or []),
                },
            },
        )
        return {
            "skipped": False,
            "proposals": len(out.proposals),
            "focus": focus_doc,
            "notes": out.notes,
            "screener": screen_meta,
            "hygiene": hygiene,
            "outcomes": {
                "by_horizon": outcomes.get("by_horizon"),
                "by_source": outcomes.get("by_source"),
                "symbol_count": len(outcomes.get("by_symbol") or []),
            },
        }

    async def hygiene_active_watchlist(
        self,
        *,
        holdings: list[str] | None = None,
    ) -> dict[str, Any]:
        """Pause active names that fail liquidity screen (holdings exempt)."""
        from app.universe.screener import screen_candidates

        if not self.settings.universe_screener_enabled:
            return {"skipped": True, "reason": "screener_disabled", "paused": []}
        if not self.settings.universe_screener_pause_illiquid:
            return {"skipped": True, "reason": "pause_illiquid_disabled", "paused": []}

        held = {h.upper() for h in (holdings or [])}
        active = await self.list_active()
        symbols = [r.symbol.upper() for r in active if r.symbol.upper() not in held]
        if not symbols:
            return {"skipped": False, "paused": [], "checked": 0}

        result = await screen_candidates(self.session, self.settings, symbols)
        reject_map = {h.symbol: h for h in result.rejected}
        now = utc_now()
        paused: list[dict[str, Any]] = []
        for row in active:
            sym = row.symbol.upper()
            if sym in held or sym not in reject_map:
                continue
            hit = reject_map[sym]
            row.status = "paused"
            row.last_reviewed_at = now
            row.payload = {
                **(row.payload or {}),
                "paused_by": "liquidity_screener",
                "reasons": list(hit.reasons),
                "avg_volume_20d": hit.avg_volume_20d,
                "spread_bps": hit.spread_bps,
            }
            paused.append({"symbol": sym, "reasons": list(hit.reasons)})
        if paused:
            await self.session.flush()
            logger.info("universe_hygiene_paused", count=len(paused), symbols=[p["symbol"] for p in paused])
        return {
            "skipped": False,
            "checked": len(symbols),
            "paused": paused,
            "screener_source": result.source,
        }

    async def build_focus_without_llm(
        self,
        *,
        holdings: list[str],
        session_date: str | None = None,
    ) -> dict[str, Any]:
        await self.ensure_seeded()
        active = await self.list_active()
        ranked = sorted(active, key=lambda r: (-r.priority, r.symbol))
        held = [h.upper() for h in holdings]
        held_set = set(held)
        focus: list[str] = []
        for h in held:
            if h not in focus:
                focus.append(h)
        from app.universe.book_strategy import is_active_strategy_horizon

        for r in ranked:
            if r.symbol.upper() not in focus:
                if not is_active_strategy_horizon(r.horizon) and r.symbol.upper() not in held_set:
                    continue
                focus.append(r.symbol.upper())
            if len(focus) >= self.settings.universe_focus_limit:
                break
        return await self._persist_focus(
            symbols=focus,
            holdings=held,
            rationale="Priority focus without LLM refresh",
            session_date=session_date,
            source="universe_service",
        )

    async def apply_session_context(
        self,
        *,
        holdings: list[str] | None = None,
        market_regime: str | None = None,
        themes: list[str] | None = None,
        session_date: str | None = None,
    ) -> dict[str, Any]:
        """Boost theme-aligned watchlist priorities and rebuild focus (no LLM)."""
        from app.universe.candidates import THEME_SYMBOLS, themes_for_regime

        await self.ensure_seeded()
        tags = [t.strip().lower() for t in (themes or []) if t and str(t).strip()]
        tags.extend(themes_for_regime(market_regime))
        tags = list(dict.fromkeys(tags))
        boosted_syms: set[str] = set()
        for tag in tags:
            boosted_syms.update(s.upper() for s in THEME_SYMBOLS.get(tag, ()))

        now = utc_now()
        boosted = 0
        if boosted_syms:
            active = await self.list_active()
            for row in active:
                if row.symbol.upper() in boosted_syms:
                    before = int(row.priority)
                    row.priority = min(100, before + 8)
                    if row.priority != before:
                        boosted += 1
                    row.last_reviewed_at = now
                    row.payload = {
                        **(row.payload or {}),
                        "regime_boost": market_regime,
                        "themes": tags,
                    }
            await self.session.flush()

        focus = await self.build_focus_without_llm(
            holdings=holdings or [],
            session_date=session_date,
        )
        # Annotate latest focus with context
        latest = await self._latest_focus()
        if latest is not None:
            latest.payload = {
                **(latest.payload or {}),
                "market_regime": market_regime,
                "themes": tags,
                "boosted_count": boosted,
            }
            latest.rationale = (
                f"{latest.rationale} · regime={market_regime or 'n/a'} themes={','.join(tags[:4]) or 'none'}"
            )
            await self.session.flush()
        return {
            "boosted": boosted,
            "themes": tags,
            "market_regime": market_regime,
            "focus": focus,
        }

    async def _apply_proposals(
        self,
        out: UniverseManagerOutput,
        *,
        candidate_symbols: set[str] | None = None,
    ) -> None:
        now = utc_now()
        by_sym = {
            r.symbol.upper(): r
            for r in (
                await self.session.execute(select(WatchlistSymbol))
            ).scalars().all()
        }
        # Cap active count after apply
        for prop in out.proposals:
            sym = prop.symbol.upper().strip()
            if not sym:
                continue
            # Soft guard: seed ∪ screened candidates ∪ already-known watchlist
            from app.universe.candidates import addable_universe, venue_for_universe_symbol

            allowed_new = addable_universe(
                self.settings,
                known_symbols=set(by_sym),
                candidate_symbols=candidate_symbols,
            )
            if sym not in allowed_new and prop.action == "add":
                logger.info("universe_reject_unknown_add", symbol=sym)
                continue
            try:
                horizon = UniverseHorizon(prop.horizon).value
            except ValueError:
                continue
            row = by_sym.get(sym)
            action = prop.action.lower()
            if action == "remove":
                if row:
                    row.status = "removed"
                    row.last_reviewed_at = now
                continue
            if action == "pause":
                if row:
                    row.status = "paused"
                    row.last_reviewed_at = now
                    row.thesis = prop.thesis or row.thesis
                    row.invalidation = prop.invalidation or row.invalidation
                continue
            venue_tag = venue_for_universe_symbol(self.settings, sym)
            if row is None:
                row = WatchlistSymbol(
                    id=uuid4(),
                    symbol=sym,
                    horizon=horizon,
                    status="active",
                    priority=prop.priority,
                    thesis=prop.thesis,
                    invalidation=prop.invalidation,
                    source="universe_manager",
                    last_reviewed_at=now,
                    payload={"rationale": prop.rationale, "venue": venue_tag},
                )
                self.session.add(row)
                by_sym[sym] = row
            else:
                row.horizon = horizon if action in {"add", "keep", "rehorizon"} else row.horizon
                row.status = "active"
                row.priority = prop.priority
                if prop.thesis:
                    row.thesis = prop.thesis
                if prop.invalidation:
                    row.invalidation = prop.invalidation
                row.source = "universe_manager"
                row.last_reviewed_at = now
                row.payload = {
                    **(row.payload or {}),
                    "rationale": prop.rationale,
                    "venue": (row.payload or {}).get("venue") or venue_tag,
                }

        # Enforce watchlist limit by pausing lowest priority actives
        actives = sorted(
            [r for r in by_sym.values() if r.status == "active"],
            key=lambda r: (-r.priority, r.symbol),
        )
        limit = self.settings.universe_watchlist_limit
        for extra in actives[limit:]:
            extra.status = "paused"
            extra.last_reviewed_at = now
        await self.session.flush()

    async def _persist_focus(
        self,
        *,
        symbols: list[str],
        holdings: list[str],
        rationale: str,
        session_date: str | None,
        source: str,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        day = session_date or now.astimezone(UTC).date().isoformat()
        # Prefer ET session date when possible — keep simple UTC date if unknown
        held = [h.upper() for h in holdings]
        active_set = {r.symbol.upper() for r in await self.list_active()}
        allowed = active_set | set(held)
        cleaned = []
        for s in symbols:
            u = str(s).upper().strip()
            if u and u in allowed and u not in cleaned:
                cleaned.append(u)
        for h in held:
            if h not in cleaned:
                cleaned.insert(0, h)
        # If LLM/focus listed only stale names, fall back to priority actives.
        if not cleaned:
            ranked = sorted(
                [r for r in (await self.list_active())],
                key=lambda r: (-r.priority, r.symbol),
            )
            cleaned = [r.symbol.upper() for r in ranked[: self.settings.universe_focus_limit]]
        cleaned = cleaned[: max(self.settings.universe_focus_limit, len(held))]
        row = FocusSetSnapshot(
            id=uuid4(),
            as_of=now,
            session_date=day,
            symbols=cleaned,
            holdings=held,
            rationale=rationale,
            source=source,
            payload=extra or {},
        )
        self.session.add(row)
        await self.session.flush()
        return {
            "as_of": now.isoformat(),
            "session_date": day,
            "symbols": cleaned,
            "holdings": held,
            "rationale": rationale,
            "source": source,
        }

    async def _latest_focus(self) -> FocusSetSnapshot | None:
        return (
            await self.session.execute(
                select(FocusSetSnapshot).order_by(FocusSetSnapshot.as_of.desc()).limit(1)
            )
        ).scalar_one_or_none()

    @staticmethod
    def _row_dict(r: WatchlistSymbol) -> dict[str, Any]:
        pol = None
        try:
            pol = policy_for(r.horizon)
        except Exception:  # noqa: BLE001
            pol = None
        return {
            "symbol": r.symbol,
            "horizon": r.horizon,
            "horizon_label_ko": pol.label_ko if pol else None,
            "status": r.status,
            "priority": r.priority,
            "thesis": r.thesis,
            "invalidation": r.invalidation,
            "source": r.source,
            "last_reviewed_at": r.last_reviewed_at.isoformat() if r.last_reviewed_at else None,
            "last_outcome_stats": (r.payload or {}).get("last_outcome_stats")
            if isinstance(r.payload, dict)
            else None,
        }
