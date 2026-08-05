"""Universe service — seed, AI refresh, entry gate, focus set for collection."""

from __future__ import annotations

from datetime import UTC, datetime
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
        """Bootstrap watchlist from TRADE_ALLOWLIST when empty."""
        existing = (
            await self.session.execute(select(WatchlistSymbol).limit(1))
        ).scalar_one_or_none()
        if existing is not None:
            return 0
        now = utc_now()
        indexes = {"SPY", "QQQ", "IWM", "DIA"}
        n = 0
        for i, raw in enumerate(self.settings.trade_allowlist):
            sym = raw.upper().strip()
            if not sym:
                continue
            if sym in indexes:
                horizon = UniverseHorizon.SCALP if i < 2 else UniverseHorizon.DAY
            elif sym in {"NVDA", "TSLA", "AMD", "META", "AAPL"}:
                horizon = UniverseHorizon.DAY
            elif sym in {"MSFT", "AMZN", "GOOGL", "AVGO"}:
                horizon = UniverseHorizon.SHORT
            else:
                horizon = UniverseHorizon.MEDIUM
            self.session.add(
                WatchlistSymbol(
                    id=uuid4(),
                    symbol=sym,
                    horizon=horizon.value,
                    status="active",
                    priority=max(10, 85 - i * 3),
                    thesis=f"Seeded into {horizon.value} book from TRADE_ALLOWLIST",
                    invalidation="Liquidity failure or thesis break",
                    source="seed",
                    last_reviewed_at=now,
                    payload={"seed_index": i},
                )
            )
            n += 1
        await self.session.flush()
        logger.info("universe_seeded", count=n)
        return n

    async def list_active(self) -> list[WatchlistSymbol]:
        result = await self.session.execute(
            select(WatchlistSymbol)
            .where(WatchlistSymbol.status == "active")
            .order_by(WatchlistSymbol.priority.desc(), WatchlistSymbol.symbol.asc())
        )
        return list(result.scalars().all())

    async def entry_universe(self) -> set[str]:
        """Symbols allowed for NEW entries."""
        if not self.is_dynamic():
            return set(self.settings.allowlist_set())
        await self.ensure_seeded()
        active = await self.list_active()
        if not active:
            return set(self.settings.allowlist_set())
        return {row.symbol.upper() for row in active}

    async def horizon_by_symbol(self) -> dict[str, str]:
        await self.ensure_seeded()
        rows = list((await self.session.execute(select(WatchlistSymbol))).scalars().all())
        return {r.symbol.upper(): r.horizon for r in rows if r.status == "active"}

    async def collection_universe(self, holdings: list[str] | None = None) -> list[str]:
        """Symbols to collect/analyze this cycle: holdings ∪ focus (or watchlist capped)."""
        held = sorted({h.upper() for h in (holdings or []) if h})
        if not self.is_dynamic():
            return sorted({*self.settings.trade_allowlist, *held})

        await self.ensure_seeded()
        latest = await self._latest_focus()
        if latest and latest.symbols:
            focus = [str(s).upper() for s in latest.symbols]
            return sorted({*focus, *held})

        active = await self.list_active()
        ranked = sorted(active, key=lambda r: (-r.priority, r.symbol))
        focus = [r.symbol.upper() for r in ranked[: self.settings.universe_focus_limit]]
        return sorted({*focus, *held})

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
            },
            "limits": {
                "watchlist": self.settings.universe_watchlist_limit,
                "focus": self.settings.universe_focus_limit,
            },
            "candidate_pool": self._candidate_pool(),
            "allow_candidate_adds": self.settings.universe_allow_candidate_adds,
        }

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

    async def refresh(
        self,
        *,
        holdings: list[str] | None = None,
        market_regime: str | None = None,
        themes: list[str] | None = None,
        session_date: str | None = None,
    ) -> dict[str, Any]:
        """Run Universe Manager and persist watchlist + focus."""
        await self.ensure_seeded()
        if not self.settings.universe_manager_enabled:
            focus = await self.build_focus_without_llm(holdings=holdings or [], session_date=session_date)
            return {"skipped": True, "reason": "universe_manager_disabled", "focus": focus}

        current = await self.list_active()
        paused = list(
            (
                await self.session.execute(
                    select(WatchlistSymbol).where(WatchlistSymbol.status != "removed")
                )
            )
            .scalars()
            .all()
        )
        payload = UniverseManagerInput(
            as_of=utc_now(),
            current_watchlist=[self._row_dict(r) for r in paused],
            holdings=[h.upper() for h in (holdings or [])],
            seed_pool=list(self.settings.trade_allowlist),
            candidate_pool=self._candidate_pool(
                themes=themes, market_regime=market_regime
            ),
            market_regime=market_regime,
            themes=themes or [],
            horizon_policies=all_horizon_summaries(),
            watchlist_limit=self.settings.universe_watchlist_limit,
            focus_limit=self.settings.universe_focus_limit,
            trace=TraceMetadata(source_data_timestamp=utc_now()),
        )
        out = await self.agent.run(payload)
        await self._apply_proposals(out)
        focus_doc = await self._persist_focus(
            symbols=out.focus_symbols,
            holdings=holdings or [],
            rationale=out.focus_rationale,
            session_date=session_date,
            source="universe_manager",
            extra={"notes": out.notes, "quality": out.data_quality_score},
        )
        return {
            "skipped": False,
            "proposals": len(out.proposals),
            "focus": focus_doc,
            "notes": out.notes,
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
        focus: list[str] = []
        for h in held:
            if h not in focus:
                focus.append(h)
        for r in ranked:
            if r.symbol.upper() not in focus:
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

    async def _apply_proposals(self, out: UniverseManagerOutput) -> None:
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
            # Soft guard: seed ∪ curated candidates ∪ already-known watchlist
            from app.universe.candidates import addable_universe

            allowed_new = addable_universe(self.settings, known_symbols=set(by_sym))
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
                    payload={"rationale": prop.rationale},
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
                row.payload = {**(row.payload or {}), "rationale": prop.rationale}

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
        cleaned = []
        for s in symbols:
            u = str(s).upper().strip()
            if u and u not in cleaned:
                cleaned.append(u)
        held = [h.upper() for h in holdings]
        for h in held:
            if h not in cleaned:
                cleaned.insert(0, h)
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
        }
