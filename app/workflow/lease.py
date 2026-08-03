"""DB-backed workflow leases."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.models import WorkflowLease

logger = get_logger(__name__)


def _aware_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


class LeaseError(Exception):
    """Raised when a lease cannot be acquired or renewed."""


class LeaseService:
    def __init__(self, session: AsyncSession, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()

    async def acquire(self, lease_key: str, owner: str) -> WorkflowLease:
        now = datetime.now(UTC)
        ttl = timedelta(seconds=self.settings.workflow_lease_seconds)
        existing = (
            await self.session.execute(
                select(WorkflowLease).where(WorkflowLease.lease_key == lease_key)
            )
        ).scalar_one_or_none()
        if existing is not None:
            if _aware_utc(existing.expires_at) > now and existing.owner != owner:
                raise LeaseError(f"lease_held_by:{existing.owner}")
            # Expired or same owner — take over
            existing.owner = owner
            existing.acquired_at = now
            existing.expires_at = now + ttl
            existing.heartbeat_at = now
            await self.session.flush()
            logger.info("lease_reacquired", key=lease_key, owner=owner)
            return existing

        row = WorkflowLease(
            id=uuid4(),
            lease_key=lease_key,
            owner=owner,
            acquired_at=now,
            expires_at=now + ttl,
            heartbeat_at=now,
            metadata_json={},
        )
        self.session.add(row)
        await self.session.flush()
        logger.info("lease_acquired", key=lease_key, owner=owner)
        return row

    async def heartbeat(self, lease_key: str, owner: str) -> WorkflowLease:
        now = datetime.now(UTC)
        row = (
            await self.session.execute(
                select(WorkflowLease).where(WorkflowLease.lease_key == lease_key)
            )
        ).scalar_one_or_none()
        if row is None:
            raise LeaseError("lease_missing")
        if row.owner != owner:
            raise LeaseError("lease_owner_mismatch")
        row.heartbeat_at = now
        row.expires_at = now + timedelta(seconds=self.settings.workflow_lease_seconds)
        await self.session.flush()
        return row

    async def release(self, lease_key: str, owner: str) -> None:
        row = (
            await self.session.execute(
                select(WorkflowLease).where(WorkflowLease.lease_key == lease_key)
            )
        ).scalar_one_or_none()
        if row is None:
            return
        if row.owner != owner:
            raise LeaseError("lease_owner_mismatch")
        await self.session.delete(row)
        await self.session.flush()

    async def reclaim_expired(self) -> int:
        now = datetime.now(UTC)
        rows = list((await self.session.execute(select(WorkflowLease))).scalars().all())
        expired = [row for row in rows if _aware_utc(row.expires_at) <= now]
        for row in expired:
            await self.session.delete(row)
        await self.session.flush()
        return len(expired)
