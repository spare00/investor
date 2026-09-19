"""Stamp and read entry/exit attribution on CIO plans and lifecycles.

Public entry-reason labels (operator language):
  uptrend_dip | oversold_bounce | chase | aggressive_injected

Internal timing from classify_timing:
  dip_buy | bounce | chase | continuation | falling_knife | blowoff

`aggressive_injected` is a source, not a timing. A trade can be both
oversold_bounce and aggressive_injected; report buckets are independent gates.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.universe.book_strategy import classify_timing, tape_from_view

TIMING_TO_REASON: dict[str, str] = {
    "dip_buy": "uptrend_dip",
    "bounce": "oversold_bounce",
    "chase": "chase",
}

ENTRY_REASONS: tuple[str, ...] = (
    "uptrend_dip",
    "oversold_bounce",
    "chase",
    "aggressive_injected",
)

SOURCE_CIO = "cio"
SOURCE_INJECTED = "aggressive_injected"

EXIT_TAKE_PROFIT = "take_profit"
EXIT_STOP = "stop"
EXIT_GIVEBACK = "giveback_exit"
EXIT_SESSION_FLATTEN = "session_flatten"
EXIT_MAX_HOLDING = "max_holding"
EXIT_UNKNOWN = "unknown"

COHORT_INJECTED_SIDEWAYS = "aggressive_injected_sideways"
COHORT_SHORT_BOUNCE = "short_oversold_bounce"
COHORT_SCALP_FLATTEN = "scalp_session_flatten"

_ATTR_KEYS = ("entry_timing", "entry_source", "trend_at_entry")


def public_entry_reason(timing: str | None) -> str | None:
    if not timing:
        return None
    return TIMING_TO_REASON.get(str(timing).strip().lower())


def _timing_from_notes(notes: list[Any] | None) -> str | None:
    for raw in notes or []:
        text = str(raw)
        if text.startswith("timing="):
            label = text.split("=", 1)[1].strip().lower()
            return label or None
    return None


def trend_value(view: Any) -> str | None:
    trend = view.get("trend_state") if isinstance(view, dict) else getattr(view, "trend_state", None)
    if trend is None:
        return None
    return str(getattr(trend, "value", trend) or "") or None


def timing_from_view(view: Any) -> str | None:
    """Prefer Quant notes (`timing=`), else recompute from tape + trend."""
    notes = view.get("notes") if isinstance(view, dict) else getattr(view, "notes", None)
    stamped = _timing_from_notes(list(notes or []))
    if stamped:
        return stamped
    explicit = view.get("entry_timing") if isinstance(view, dict) else getattr(view, "entry_timing", None)
    if explicit:
        return str(explicit).strip().lower() or None
    trend = view.get("trend_state") if isinstance(view, dict) else getattr(view, "trend_state", None)
    momentum = (
        view.get("momentum_state") if isinstance(view, dict) else getattr(view, "momentum_state", None)
    )
    if trend is None or momentum is None:
        return None
    tape = tape_from_view(view)
    return classify_timing(trend=trend, momentum=momentum, rsi=None, **tape)


def attribution_from_view(view: Any, *, source: str = SOURCE_CIO) -> dict[str, str]:
    out: dict[str, str] = {"entry_source": source}
    timing = timing_from_view(view)
    if timing:
        out["entry_timing"] = timing
    trend = trend_value(view)
    if trend:
        out["trend_at_entry"] = trend
    return out


def merge_attribution(base: dict[str, Any] | None, extra: dict[str, Any] | None) -> dict[str, Any]:
    merged = dict(base or {})
    for key in _ATTR_KEYS:
        val = (extra or {}).get(key)
        if val and not merged.get(key):
            merged[key] = val
    return merged


def stamp_plan(plan: Any, *, view: Any | None = None, source: str | None = None) -> Any:
    """Copy timing/trend/source onto a SymbolActionPlan. Does not overwrite source."""
    patch: dict[str, Any] = {}
    current_source = getattr(plan, "entry_source", None)
    if source and not current_source:
        patch["entry_source"] = source
    elif not current_source:
        patch["entry_source"] = SOURCE_CIO
    if view is not None:
        attr = attribution_from_view(view, source=patch.get("entry_source") or current_source or SOURCE_CIO)
        if not getattr(plan, "entry_timing", None) and attr.get("entry_timing"):
            patch["entry_timing"] = attr["entry_timing"]
        if not getattr(plan, "trend_at_entry", None) and attr.get("trend_at_entry"):
            patch["trend_at_entry"] = attr["trend_at_entry"]
    if not patch:
        return plan
    return plan.model_copy(update=patch)


def stamp_cio_entry_attribution(decision: Any, quant: Any) -> Any:
    """Fill missing entry tags on surviving CIO entry plans from Quant views."""
    from app.schemas.common import SymbolAction

    entry_actions = {SymbolAction.STRONG_BUY, SymbolAction.BUY, SymbolAction.SCALE_IN}
    views = list(getattr(quant, "symbol_views", None) or [])
    by_sym = {str(getattr(v, "symbol", "") or "").upper(): v for v in views if getattr(v, "symbol", None)}
    updated = []
    changed = False
    for plan in getattr(decision, "symbol_actions", None) or []:
        if plan.action not in entry_actions:
            updated.append(plan)
            continue
        view = by_sym.get(str(plan.symbol or "").upper())
        stamped = stamp_plan(plan, view=view, source=getattr(plan, "entry_source", None) or SOURCE_CIO)
        changed = changed or stamped is not plan
        updated.append(stamped)
    if not changed:
        return decision
    return decision.model_copy(update={"symbol_actions": updated})


def attribution_from_plan(plan: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    if isinstance(plan, dict):
        for key in _ATTR_KEYS:
            val = plan.get(key)
            if val:
                out[key] = str(val)
        return out
    for key in _ATTR_KEYS:
        val = getattr(plan, key, None)
        if val:
            out[key] = str(val)
    return out


def apply_attribution(meta: dict[str, Any] | None, attr: dict[str, Any] | None) -> dict[str, Any]:
    return merge_attribution(meta, attr)


def read_attribution(lc: Any) -> dict[str, str | None]:
    meta = dict(getattr(lc, "metadata_json", None) or {})
    policy = dict(getattr(lc, "exit_policy", None) or {})
    out: dict[str, str | None] = {}
    for key in _ATTR_KEYS:
        val = meta.get(key) or policy.get(key)
        out[key] = str(val) if val else None
    return out


_CANONICAL_EXITS = {
    EXIT_TAKE_PROFIT,
    EXIT_STOP,
    EXIT_GIVEBACK,
    EXIT_SESSION_FLATTEN,
    EXIT_MAX_HOLDING,
}


def classify_exit_reason(
    *,
    thesis: str | None = None,
    reason: str | None = None,
    metadata: dict[str, Any] | None = None,
    horizon: str | None = None,
) -> str:
    meta = dict(metadata or {})
    existing = str(meta.get("exit_reason") or "").strip().lower()
    if existing in _CANONICAL_EXITS:
        return existing
    if str(reason or "").strip().lower() in _CANONICAL_EXITS:
        return str(reason).strip().lower()
    draft = meta.get("exit_draft") if isinstance(meta.get("exit_draft"), dict) else {}
    parts = [
        str(reason or ""),
        str(thesis or ""),
        str(draft.get("reason") or ""),
        str(meta.get("exit_reason") or ""),
        str(meta.get("closed_by") or ""),
    ]
    blob = " ".join(parts).lower()
    if "giveback" in blob:
        return EXIT_GIVEBACK
    if "take_profit" in blob:
        return EXIT_TAKE_PROFIT
    if "hard_stop" in blob or "stop_triggered" in blob or "protection_stop" in blob:
        return EXIT_STOP
    if (
        "force_close" in blob
        or "closing:" in blob
        or "intraday-only" in blob
        or "session_flatten" in blob
    ):
        return EXIT_SESSION_FLATTEN
    if "max_holding" in blob:
        return EXIT_MAX_HOLDING
    _ = horizon  # reserved: callers may fold scalp max-hold into session later
    return EXIT_UNKNOWN


def stamp_lifecycle_exit_reason(lc: Any, *, raw: str | None = None, thesis: str | None = None) -> str:
    meta = dict(getattr(lc, "metadata_json", None) or {})
    existing = str(meta.get("exit_reason") or "")
    if existing and existing != EXIT_UNKNOWN:
        return existing
    policy = dict(getattr(lc, "exit_policy", None) or {})
    classified = classify_exit_reason(
        thesis=thesis,
        reason=raw,
        metadata=meta,
        horizon=str(policy.get("horizon") or "") or None,
    )
    if classified == EXIT_UNKNOWN and existing:
        return existing
    meta["exit_reason"] = classified
    if raw and not meta.get("exit_reason_raw"):
        meta["exit_reason_raw"] = str(raw)
    lc.metadata_json = meta
    return classified


async def copy_entry_attribution_to_lifecycle(session: Any, lc: Any) -> None:
    """Pull CIO/intent tags onto a broker-synced lifecycle. Idempotent."""
    meta = dict(getattr(lc, "metadata_json", None) or {})
    qty = abs(float(getattr(lc, "quantity", 0) or 0))
    if qty > 0 and not meta.get("opened_quantity"):
        meta["opened_quantity"] = qty

    have = any(meta.get(k) for k in _ATTR_KEYS)
    if have:
        lc.metadata_json = meta
        return

    attr: dict[str, Any] = {}
    decision_id = getattr(lc, "decision_id", None)
    symbol = str(getattr(lc, "symbol", "") or "").upper()

    if decision_id is not None:
        from app.models import CIODecisionRecord

        row = (
            await session.execute(
                select(CIODecisionRecord).where(CIODecisionRecord.decision_id == decision_id)
            )
        ).scalar_one_or_none()
        if row is not None:
            payload = dict(row.payload or {})
            for plan in payload.get("symbol_actions") or []:
                if not isinstance(plan, dict):
                    continue
                if str(plan.get("symbol") or "").upper() != symbol:
                    continue
                attr = attribution_from_plan(plan)
                if attr:
                    break

    if not attr and symbol:
        from app.brokers.models import IntentType
        from app.models import OrderIntent

        intents = list(
            (
                await session.execute(
                    select(OrderIntent)
                    .where(OrderIntent.symbol == symbol)
                    .where(
                        OrderIntent.intent_type.in_(
                            [IntentType.OPEN_LONG.value, IntentType.ADD_LONG.value]
                        )
                    )
                    .order_by(OrderIntent.created_at.desc())
                    .limit(8)
                )
            )
            .scalars()
            .all()
        )
        for intent in intents:
            intent_meta = dict(intent.metadata_json or {})
            pulled = {k: intent_meta[k] for k in _ATTR_KEYS if intent_meta.get(k)}
            if pulled:
                attr = pulled
                if not decision_id and intent.decision_id:
                    lc.decision_id = intent.decision_id
                break

    if attr:
        meta = apply_attribution(meta, attr)
        policy = dict(getattr(lc, "exit_policy", None) or {})
        for key in _ATTR_KEYS:
            if attr.get(key) and not policy.get(key):
                policy[key] = attr[key]
        lc.exit_policy = policy
    lc.metadata_json = meta
