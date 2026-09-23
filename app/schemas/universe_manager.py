"""Universe manager agent I/O — maintain horizon-grouped watchlist."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, field_validator, model_validator

from app.schemas.common import StrictModel, TraceMetadata
from app.universe.horizons import UniverseHorizon


def _proposal_row(raw: object, *, symbol_fallback: str = "") -> object:
    """14B copies brief keys (`s`/`h`) or omits ticker when focus_symbols is set."""
    if not isinstance(raw, dict):
        return raw
    row = dict(raw)
    if not row.get("symbol"):
        for key in ("s", "ticker", "sym", "name"):
            val = row.get(key)
            if val:
                row["symbol"] = val
                break
    if not row.get("symbol") and symbol_fallback:
        row["symbol"] = symbol_fallback
    if not row.get("horizon"):
        for key in ("h", "hz", "book"):
            val = row.get(key)
            if val:
                row["horizon"] = val
                break
    if row.get("symbol") and not row.get("horizon"):
        row["horizon"] = UniverseHorizon.SHORT.value
    for alias in ("s", "ticker", "sym", "name", "h", "hz", "book"):
        row.pop(alias, None)
    return row


class WatchlistProposal(StrictModel):
    symbol: str
    horizon: UniverseHorizon
    action: str = Field(description="add | keep | pause | remove | rehorizon")
    priority: int = Field(ge=0, le=100, default=50)
    thesis: str = ""
    invalidation: str = ""
    rationale: str = ""


class UniverseManagerInput(StrictModel):
    as_of: datetime
    current_watchlist: list[dict] = Field(default_factory=list)
    holdings: list[str] = Field(default_factory=list)
    seed_pool: list[str] = Field(default_factory=list)
    seed_pool_by_venue: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Allowlist seeds keyed by venue (US / AU)",
    )
    enabled_venues: list[str] = Field(
        default_factory=list,
        description="Active books this firm runs (e.g. US, AU)",
    )
    candidate_pool: list[str] = Field(
        default_factory=list,
        description="Bounded liquid names beyond seed the manager may add",
    )
    market_regime: str | None = None
    themes: list[str] = Field(default_factory=list)
    horizon_policies: list[dict] = Field(default_factory=list)
    watchlist_limit: int = 40
    focus_limit: int = 10
    objective: str = (
        "Maximize expected return while minimizing loss via horizon-appropriate "
        "selection across enabled venues; never review the entire market each session."
    )
    recent_outcomes: dict[str, object] = Field(
        default_factory=dict,
        description="Closed-trade outcome stats by symbol/horizon/source (observational)",
    )
    trace: TraceMetadata = Field(default_factory=TraceMetadata)


class UniverseManagerOutput(StrictModel):
    timestamp: datetime
    proposals: list[WatchlistProposal] = Field(default_factory=list)
    focus_symbols: list[str] = Field(default_factory=list)
    industries: list[str] = Field(
        default_factory=list,
        description="Sectors overweight in this week's membership review",
    )
    focus_rationale: str = ""
    notes: list[str] = Field(default_factory=list)
    data_quality_score: float = Field(ge=0.0, le=1.0, default=0.8)
    trace: TraceMetadata = Field(default_factory=TraceMetadata)

    @field_validator("focus_symbols", mode="before")
    @classmethod
    def _none_to_list(cls, value: object) -> object:
        return [] if value is None else value

    @field_validator("proposals", mode="before")
    @classmethod
    def _coerce_proposals(cls, value: object) -> object:
        """14B often emits a dict (symbol-keyed or a nested whole object)."""
        if value is None:
            return []
        if isinstance(value, list):
            return [_proposal_row(item) for item in value]
        if not isinstance(value, dict):
            return []
        nested = value.get("proposals")
        if isinstance(nested, list):
            return [_proposal_row(item) for item in nested]
        skip = {
            "industries",
            "focus_symbols",
            "notes",
            "focus_rationale",
            "timestamp",
            "data_quality_score",
            "trace",
            "proposals",
        }
        out: list[dict] = []
        for key, raw in value.items():
            if key in skip or not isinstance(raw, dict):
                continue
            row = _proposal_row(raw, symbol_fallback=str(key))
            if isinstance(row, dict):
                out.append(row)
        return out

    @model_validator(mode="before")
    @classmethod
    def _fill_proposal_symbols_from_focus(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        props = data.get("proposals")
        focus = data.get("focus_symbols") or []
        if not isinstance(props, list) or not isinstance(focus, list):
            return data
        filled: list[object] = []
        for i, raw in enumerate(props):
            fallback = str(focus[i]).upper() if i < len(focus) else ""
            filled.append(_proposal_row(raw, symbol_fallback=fallback) if isinstance(raw, dict) else raw)
        data["proposals"] = filled
        return data

    @field_validator("industries", mode="before")
    @classmethod
    def _coerce_industries(cls, value: object) -> object:
        if value is None:
            return []
        if isinstance(value, str):
            return [p.strip() for p in value.replace(";", ",").split(",") if p.strip()]
        return value

    @field_validator("notes", mode="before")
    @classmethod
    def _coerce_notes(cls, value: object) -> object:
        if value is None:
            return []
        # Models often emit a single prose string for notes — wrap rather than retry.
        if isinstance(value, str):
            text = value.strip()
            return [text] if text else []
        return value
