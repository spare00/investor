"""Data retention dry-run planning."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.config import Settings, get_settings


@dataclass(slots=True)
class RetentionTarget:
    category: str
    retention_days: int
    cutoff: str
    estimated_records: int = 0
    action: str = "plan_only"
    note: str = ""


@dataclass(slots=True)
class RetentionPlan:
    generated_at: str
    dry_run: bool
    targets: list[RetentionTarget] = field(default_factory=list)
    would_delete: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "dry_run": self.dry_run,
            "would_delete": self.would_delete,
            "targets": [
                {
                    "category": t.category,
                    "retention_days": t.retention_days,
                    "cutoff": t.cutoff,
                    "estimated_records": t.estimated_records,
                    "action": t.action,
                    "note": t.note,
                }
                for t in self.targets
            ],
        }


class RetentionPolicy:
    """
    Compute purge plans from settings retention days.

    Default is dry-run only — audit logs and orders are never deleted unless explicitly
    requested via ``execute=True`` (not implemented in Phase 7).
    """

    PROTECTED_CATEGORIES = frozenset({"audit_log", "orders", "executions"})

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def _cutoff(self, days: int) -> datetime:
        return datetime.now(UTC) - timedelta(days=days)

    def plan(self, *, dry_run: bool = True, record_counts: dict[str, int] | None = None) -> RetentionPlan:
        cfg = self.settings
        counts = record_counts or {}
        now = datetime.now(UTC)

        categories: list[tuple[str, int]] = [
            ("raw_provider_payload", cfg.raw_provider_payload_retention_days),
            ("canonical_market_data", cfg.canonical_market_data_retention_days),
            ("audit_log", cfg.audit_log_retention_days),
            ("metrics", cfg.metric_retention_days),
        ]

        targets: list[RetentionTarget] = []
        for category, days in categories:
            cutoff = self._cutoff(days)
            protected = category in self.PROTECTED_CATEGORIES
            targets.append(
                RetentionTarget(
                    category=category,
                    retention_days=days,
                    cutoff=cutoff.isoformat(),
                    estimated_records=counts.get(category, 0),
                    action="plan_only" if dry_run or protected else "purge",
                    note=(
                        "protected — dry-run only"
                        if protected
                        else ("dry-run — no deletion" if dry_run else "eligible for purge")
                    ),
                )
            )

        return RetentionPlan(
            generated_at=now.isoformat(),
            dry_run=dry_run,
            targets=targets,
            would_delete=False if dry_run else False,
        )
