"""Closing window + overnight risk review (intent-producing, not direct broker)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.intraday.events import IntradayEventBus
from app.intraday.modes import ModeCapabilities, resolve_mode
from app.models import ClosingReview, OvernightReview, PositionLifecycle
from app.workflow.closing import ClosingPolicyEngine
from app.workflow.states import ClosingPolicy
from sqlalchemy import select


class ClosingService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        settings: Settings | None = None,
        venue: str | None = None,
    ) -> None:
        from app.market.venues import resolve_venue

        self.session = session
        self.settings = settings or get_settings()
        self.venue = resolve_venue(self.settings, venue=venue)
        self.bus = IntradayEventBus(session, settings=self.settings)
        self.engine = ClosingPolicyEngine()

    def _open_lifecycle_clause(self) -> tuple[Any, ...]:
        return (
            PositionLifecycle.status.in_(["OPEN", "ADDING", "REDUCING", "PENDING_CLOSE"]),
            PositionLifecycle.venue == self.venue.value,
        )

    async def run_closing(self, *, in_closing_window: bool = True) -> dict[str, Any]:
        mode = resolve_mode(self.settings)
        caps = ModeCapabilities(mode)
        policy = ClosingPolicy(self.settings.default_closing_policy)
        lifecycles = list(
            (
                await self.session.execute(
                    select(PositionLifecycle).where(*self._open_lifecycle_clause())
                )
            )
            .scalars()
            .all()
        )
        horizons = await self._horizons_for_lifecycles(lifecycles)
        positions = []
        intraday_symbols: set[str] = set()
        for p in lifecycles:
            hz = horizons.get(p.symbol.upper())
            # Scalp/day books flatten near close even if overnight_allowed was set incorrectly.
            force_intra = (not p.overnight_allowed) or (hz in {"scalp", "day"})
            if force_intra:
                intraday_symbols.add(p.symbol.upper())
            positions.append(
                {
                    "symbol": p.symbol,
                    "quantity": p.quantity,
                    "is_intraday_only": force_intra,
                    "horizon": hz,
                }
            )
        decision = self.engine.decide(
            as_of=datetime.now(UTC),
            positions=positions,
            policy=policy,
            intraday_symbols=intraday_symbols,
        )
        notes = list(decision.notes)
        if in_closing_window:
            session_day = datetime.now(UTC).date().isoformat()
            await self.bus.publish(
                event_type="CLOSING_WINDOW_ENTERED",
                source="closing_service",
                deduplication_key=f"{self.venue.value}:closing:{session_day}",
                requires_risk_review=True,
                importance="high",
                bypass_cooldown=True,
                payload={"venue": self.venue.value},
            )
            notes.append("new_entries_blocked" if not self.settings.allow_new_positions_in_closing_window else "entries_allowed")
            if self.settings.cancel_entry_orders_at_closing_window:
                notes.append("entry_orders_should_cancel")

        # Materialize exit intents (and optionally paper-submit force closes).
        intent_drafts: list[dict[str, Any]] = []
        intent_ids: list[str] = []
        retry_force_submit = False
        for plan in decision.plans:
            if plan.action not in {"close", "reduce"} or not caps.can_create_intent:
                continue
            # Signed short qty must not fail the <=0 guard in _create_exit_intent.
            raw_qty = plan.quantity if plan.action == "close" else plan.quantity * 0.5
            qty = abs(float(raw_qty or 0))
            draft = {
                "symbol": plan.symbol,
                "action": plan.action,
                "quantity": qty,
                "rationale": plan.rationale,
                "draft_only": caps.intents_are_draft_only or not caps.can_submit,
            }
            intent_drafts.append(draft)
            lc = next((x for x in lifecycles if x.symbol.upper() == plan.symbol.upper()), None)
            if lc is None:
                continue
            # Idempotent: do not stack exit intents on every force-close tick.
            if plan.action == "close" and lc.status == "PENDING_CLOSE":
                draft["skipped"] = "already_pending_close"
                notes.append(f"skip_duplicate_close:{plan.symbol}")
                # Prior ticks may have marked PENDING_CLOSE without creating an intent
                # (e.g. negative short qty). Still allow armed force-close submit.
                if qty > 0 and self._should_auto_submit_force_close(caps):
                    retry_force_submit = True
                continue
            if plan.action == "reduce" and lc.status == "REDUCING":
                draft["skipped"] = "already_reducing"
                notes.append(f"skip_duplicate_reduce:{plan.symbol}")
                if qty > 0 and self._should_auto_submit_force_close(caps):
                    retry_force_submit = True
                continue
            from app.brokers.models import IntentStatus as _IntentStatus
            from app.models import OrderIntent as _OrderIntent

            prior = (
                await self.session.execute(
                    select(_OrderIntent).where(
                        _OrderIntent.symbol == lc.symbol,
                        _OrderIntent.status == _IntentStatus.CREATED.value,
                    )
                )
            ).scalars().first()
            if prior is not None and str(prior.thesis or "").startswith("closing:"):
                draft["skipped"] = "already_created"
                draft["intent_id"] = str(prior.id)
                intent_ids.append(str(prior.id))
                notes.append(f"skip_duplicate_intent:{plan.symbol}")
                if qty > 0 and self._should_auto_submit_force_close(caps):
                    retry_force_submit = True
                continue
            if caps.intents_are_draft_only:
                meta = dict(lc.metadata_json or {})
                meta["exit_draft"] = {
                    "reason": f"closing:{plan.rationale}",
                    "qty": qty,
                    "at": datetime.now(UTC).isoformat(),
                }
                lc.metadata_json = meta
                if plan.action == "close":
                    lc.status = "PENDING_CLOSE"
                elif plan.action == "reduce":
                    lc.status = "REDUCING"
                continue
            intent = await self._create_exit_intent(
                lc, action=plan.action, qty=qty, rationale=plan.rationale
            )
            if intent is not None:
                intent_ids.append(str(intent.id))
                draft["intent_id"] = str(intent.id)
                if plan.action == "close":
                    lc.status = "PENDING_CLOSE"
                elif plan.action == "reduce":
                    lc.status = "REDUCING"
            else:
                notes.append(f"exit_intent_failed:{plan.symbol}")

        submitted = 0
        if (intent_ids or retry_force_submit) and self._should_auto_submit_force_close(caps):
            submitted = await self._submit_close_intents(lifecycles, decision.plans)
            notes.append(f"force_close_orders_submitted={submitted}")
            if submitted:
                from app.brokers.models import IntentStatus as _IntentStatus
                from app.models import OrderIntent as _OrderIntent
                from uuid import UUID as _UUID

                for raw_id in intent_ids:
                    try:
                        row = await self.session.get(_OrderIntent, _UUID(str(raw_id)))
                    except (ValueError, TypeError):
                        row = None
                    if row is not None:
                        row.status = _IntentStatus.SUBMITTED.value
            if retry_force_submit and not intent_ids:
                notes.append("force_close_retry_pending_close")
        elif intent_ids:
            notes.append("force_close_intents_pending_submit")

        review = ClosingReview(
            id=uuid4(),
            policy=policy.value,
            payload=decision.to_dict(),
            intent_drafts=intent_drafts,
            notes=notes,
        )
        self.session.add(review)
        await self.session.flush()
        return {
            "review_id": str(review.id),
            "policy": policy.value,
            "plans": decision.to_dict()["plans"],
            "intent_drafts": intent_drafts,
            "intent_ids": intent_ids,
            "broker_orders_submitted": submitted > 0,
            "orders_submitted": submitted,
            "notes": notes,
        }

    def _should_auto_submit_force_close(self, caps: ModeCapabilities) -> bool:
        from app.execution.firm_execution import paper_auto_submit_allowed

        if not self.settings.effective_auto_execute_force_close():
            return False
        if not caps.can_submit:
            return False
        return paper_auto_submit_allowed(self.settings)

    async def _create_exit_intent(
        self,
        lc: PositionLifecycle,
        *,
        action: str,
        qty: float,
        rationale: str,
    ) -> Any:
        from app.brokers.models import IntentStatus, IntentType
        from app.models import OrderIntent

        if qty <= 0:
            return None
        is_short = float(lc.quantity or 0) < 0
        if action == "close":
            intent_type = (
                IntentType.CLOSE_SHORT.value if is_short else IntentType.CLOSE_LONG.value
            )
        else:
            intent_type = (
                IntentType.REDUCE_SHORT.value if is_short else IntentType.REDUCE_LONG.value
            )
        intent = OrderIntent(
            id=uuid4(),
            decision_id=lc.decision_id,
            symbol=lc.symbol,
            intent_type=intent_type,
            side="buy" if is_short else "sell",
            quantity=abs(float(qty)),
            entry_price=lc.current_price,
            stop_price=lc.stop_price,
            status=IntentStatus.CREATED.value,
            thesis=f"closing:{rationale}",
            exit_policy=dict(lc.exit_policy or {}),
            metadata_json={
                "source": "closing_service",
                "reason": rationale,
                "lifecycle_id": str(lc.id),
                "action": action,
                "venue": getattr(lc, "venue", None) or self.venue.value,
                "con_id": int(getattr(lc, "con_id", 0) or 0) or None,
            },
        )
        self.session.add(intent)
        await self.session.flush()
        return intent

    async def _submit_close_intents(
        self, lifecycles: list[PositionLifecycle], plans: list[Any]
    ) -> int:
        """Submit market exits for close/reduce plans (paper path only)."""
        from app.execution.order_manager import OrderManager
        from app.execution.safety_controls import trading_controls
        from app.execution.validation import ExecutionValidationResult, ValidatedOrderIntent

        if not trading_controls.is_new_order_allowed():
            return 0
        by_sym = {p.symbol.upper(): p for p in lifecycles}
        intents: list[ValidatedOrderIntent] = []
        for plan in plans:
            if plan.action not in {"close", "reduce"}:
                continue
            lc = by_sym.get(plan.symbol.upper())
            if lc is None:
                continue
            qty = abs(float(plan.quantity if plan.action == "close" else plan.quantity * 0.5))
            if qty <= 0:
                continue
            side = "sell" if float(lc.quantity or 0) >= 0 else "buy"
            key = f"force-close:{lc.id}:{plan.action}:{datetime.now(UTC).date().isoformat()}"
            intents.append(
                ValidatedOrderIntent(
                    symbol=lc.symbol.upper(),
                    side=side,
                    quantity=qty,
                    order_type="market",
                    limit_price=None,
                    stop_price=lc.stop_price,
                    idempotency_key=key,
                    decision_id=str(lc.decision_id) if lc.decision_id else str(uuid4()),
                    thesis=f"force_close:{plan.rationale}",
                    venue=getattr(lc, "venue", None) or self.venue.value,
                    con_id=int(getattr(lc, "con_id", 0) or 0) or None,
                )
            )
        if not intents:
            return 0
        validation = ExecutionValidationResult(approved=True, intents=intents)
        orders = await OrderManager(self.session, settings=self.settings).submit_validated_intents(
            validation
        )
        return len(orders)

    async def _horizons_for_lifecycles(
        self, lifecycles: list[PositionLifecycle]
    ) -> dict[str, str]:
        """Prefer lifecycle exit_policy.horizon; fall back to watchlist."""
        from app.models import WatchlistSymbol

        out: dict[str, str] = {}
        need_watch: set[str] = set()
        for lc in lifecycles:
            sym = str(lc.symbol or "").upper()
            if not sym:
                continue
            policy = lc.exit_policy if isinstance(lc.exit_policy, dict) else {}
            hz = str(policy.get("horizon") or "").strip().lower()
            if hz:
                out[sym] = hz
            else:
                need_watch.add(sym)
        if need_watch:
            rows = (
                await self.session.execute(
                    select(WatchlistSymbol).where(WatchlistSymbol.symbol.in_(need_watch))
                )
            ).scalars().all()
            for r in rows:
                out.setdefault(r.symbol.upper(), str(r.horizon))
        return out

    async def overnight_review(
        self,
        *,
        earnings: bool = False,
        economic_event: bool = False,
        gap_risk_high: bool = False,
        next_session_holiday: bool = False,
    ) -> dict[str, Any]:
        lifecycles = list(
            (
                await self.session.execute(
                    select(PositionLifecycle).where(*self._open_lifecycle_clause())
                )
            )
            .scalars()
            .all()
        )
        results: list[dict[str, Any]] = []
        horizons = await self._horizons_for_lifecycles(lifecycles)
        for lc in lifecycles:
            status = "OVERNIGHT_APPROVED"
            reasons: list[str] = []
            hz = horizons.get(lc.symbol.upper())
            event_strict = False
            if hz:
                try:
                    from app.universe.horizons import policy_for

                    event_strict = bool(policy_for(hz).overnight_event_strict)
                except ValueError:
                    event_strict = False
            if not lc.overnight_allowed or hz in {"scalp", "day"}:
                status = "CLOSE_BEFORE_MARKET_CLOSE"
                reasons.append("overnight_not_allowed" if not lc.overnight_allowed else f"horizon_{hz}")
            # Short book: overnight ok in quiet tape, but events/holidays → flatten preference.
            # Medium book: review/reduce rather than automatic flatten.
            if earnings or economic_event:
                reasons.append("event_risk")
                if status.startswith("OVERNIGHT"):
                    status = (
                        "CLOSE_BEFORE_MARKET_CLOSE"
                        if event_strict or hz == "short"
                        else "MANUAL_REVIEW_REQUIRED"
                    )
            if gap_risk_high:
                reasons.append("gap_risk")
                if status.startswith("OVERNIGHT"):
                    status = (
                        "CLOSE_BEFORE_MARKET_CLOSE"
                        if event_strict and hz == "short"
                        else "OVERNIGHT_APPROVED_WITH_REDUCTION"
                    )
            if next_session_holiday:
                reasons.append("holiday_gap")
                if status.startswith("OVERNIGHT") or status == "OVERNIGHT_APPROVED_WITH_REDUCTION":
                    status = (
                        "CLOSE_BEFORE_MARKET_CLOSE"
                        if event_strict or hz == "short"
                        else "MANUAL_REVIEW_REQUIRED"
                    )
            if not self.settings.overnight_review_required:
                status = "NO_DATA"
            valid_for = datetime.now(UTC).date().isoformat()
            row = OvernightReview(
                id=uuid4(),
                position_lifecycle_id=lc.id,
                symbol=lc.symbol,
                status=status,
                reasons=reasons,
                valid_for_session_date=valid_for,
                payload={},
            )
            self.session.add(row)
            results.append({"symbol": lc.symbol, "status": status, "reasons": reasons, "valid_for": valid_for})
        await self.session.flush()
        return {"reviews": results}
