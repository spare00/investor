"""Universe manager agent I/O — maintain horizon-grouped watchlist."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, field_validator

from app.schemas.common import StrictModel, TraceMetadata
from app.universe.horizons import UniverseHorizon


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
    candidate_pool: list[str] = Field(
        default_factory=list,
        description="Bounded liquid names beyond seed the manager may add",
    )
    market_regime: str | None = None
    themes: list[str] = Field(default_factory=list)
    horizon_policies: list[dict] = Field(default_factory=list)
    watchlist_limit: int = 40
    focus_limit: int = 12
    objective: str = (
        "Maximize expected return while minimizing loss via horizon-appropriate "
        "selection; never review the entire market each session."
    )
    trace: TraceMetadata = Field(default_factory=TraceMetadata)


class UniverseManagerOutput(StrictModel):
    timestamp: datetime
    proposals: list[WatchlistProposal] = Field(default_factory=list)
    focus_symbols: list[str] = Field(default_factory=list)
    focus_rationale: str = ""
    notes: list[str] = Field(default_factory=list)
    data_quality_score: float = Field(ge=0.0, le=1.0, default=0.8)
    trace: TraceMetadata = Field(default_factory=TraceMetadata)

    @field_validator("proposals", "focus_symbols", "notes", mode="before")
    @classmethod
    def _none_to_list(cls, value: object) -> object:
        return [] if value is None else value
