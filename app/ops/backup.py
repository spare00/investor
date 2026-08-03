"""Filesystem backup and restore for operational snapshots."""

from __future__ import annotations

import hashlib
import json
import shutil
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.prompts import PROMPTS_ROOT
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.models import ConfigurationHistory, DailyPerformance, PortfolioSnapshot

logger = get_logger(__name__)

SCHEMA_VERSION = "backup_v1"
SECRET_PATTERNS = (".env", "secret", "credential", "api_key", "token", "password")


@dataclass(slots=True)
class BackupResult:
    backup_id: str
    path: str
    manifest_path: str
    file_count: int
    created_at: str


@dataclass(slots=True)
class VerifyResult:
    valid: bool
    backup_id: str | None
    errors: list[str]


class BackupService:
    """Create zip backups under ``backups/`` with manifest and sha256 hashes."""

    EXPORT_TABLES = (
        ("daily_performance", DailyPerformance),
        ("portfolio_snapshots", PortfolioSnapshot),
        ("configuration_history", ConfigurationHistory),
    )

    def __init__(
        self,
        session: AsyncSession | None = None,
        settings: Settings | None = None,
        root: Path | None = None,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.root = root or Path.cwd()
        self.backup_dir = self.root / "backups"

    def _is_secret_path(self, path: Path) -> bool:
        name = path.name.lower()
        return any(p in name for p in SECRET_PATTERNS)

    def _sha256_file(self, path: Path) -> str:
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    async def _export_table_json(self, label: str, model: type) -> dict[str, Any]:
        if self.session is None:
            return {"table": label, "rows": [], "note": "no_session"}
        rows = (await self.session.execute(select(model))).scalars().all()
        exported: list[dict[str, Any]] = []
        for row in rows:
            data: dict[str, Any] = {}
            for col in row.__table__.columns:
                val = getattr(row, col.name)
                if isinstance(val, datetime):
                    val = val.isoformat()
                elif hasattr(val, "hex"):
                    val = str(val)
                data[col.name] = val
            exported.append(data)
        return {"table": label, "rows": exported, "count": len(exported)}

    async def create(self, *, as_zip: bool = True) -> BackupResult:
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        backup_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        staging = self.backup_dir / f"{backup_id}_staging"
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)

        manifest: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "backup_id": backup_id,
            "created_at": datetime.now(UTC).isoformat(),
            "database_url_redacted": self._redact_database_url(self.settings.database_url),
            "files": [],
        }

        # Config snapshot (non-secret fields only)
        config_snapshot = {
            k: v
            for k, v in self.settings.model_dump(mode="json").items()
            if "secret" not in k.lower() and "password" not in k.lower() and "token" not in k.lower()
        }
        config_path = staging / "config.json"
        config_path.write_text(json.dumps(config_snapshot, indent=2), encoding="utf-8")

        # Table exports
        exports_dir = staging / "exports"
        exports_dir.mkdir()
        for label, model in self.EXPORT_TABLES:
            payload = await self._export_table_json(label, model)
            out = exports_dir / f"{label}.json"
            out.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

        # Prompt files
        prompts_dir = staging / "prompts"
        if PROMPTS_ROOT.exists():
            shutil.copytree(
                PROMPTS_ROOT,
                prompts_dir,
                ignore=shutil.ignore_patterns("*.pyc", "__pycache__"),
            )

        # Build manifest with hashes
        for path in sorted(staging.rglob("*")):
            if not path.is_file() or self._is_secret_path(path):
                continue
            rel = path.relative_to(staging).as_posix()
            manifest["files"].append(
                {"path": rel, "sha256": self._sha256_file(path), "size_bytes": path.stat().st_size}
            )

        manifest_path = staging / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        if as_zip:
            archive = self.backup_dir / f"{backup_id}.zip"
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                for path in staging.rglob("*"):
                    if path.is_file() and not self._is_secret_path(path):
                        zf.write(path, arcname=path.relative_to(staging).as_posix())
            shutil.rmtree(staging)
            final_path = archive
        else:
            final_dir = self.backup_dir / backup_id
            if final_dir.exists():
                shutil.rmtree(final_dir)
            staging.rename(final_dir)
            final_path = final_dir

        logger.info("backup_created", backup_id=backup_id, path=str(final_path))
        return BackupResult(
            backup_id=backup_id,
            path=str(final_path),
            manifest_path=str(final_path / "manifest.json") if not as_zip else str(final_path),
            file_count=len(manifest["files"]),
            created_at=manifest["created_at"],
        )

    def verify(self, backup_path: str | Path) -> VerifyResult:
        path = Path(backup_path)
        errors: list[str] = []
        if not path.exists():
            return VerifyResult(valid=False, backup_id=None, errors=["backup_not_found"])

        staging: Path | None = None
        try:
            if path.suffix == ".zip":
                staging = self.backup_dir / f"_verify_{path.stem}"
                if staging.exists():
                    shutil.rmtree(staging)
                staging.mkdir()
                with zipfile.ZipFile(path) as zf:
                    zf.extractall(staging)
                root = staging
            else:
                root = path

            manifest_file = root / "manifest.json"
            if not manifest_file.exists():
                return VerifyResult(valid=False, backup_id=None, errors=["missing_manifest"])
            manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
            backup_id = manifest.get("backup_id")
            if manifest.get("schema_version") != SCHEMA_VERSION:
                errors.append("schema_version_mismatch")

            for entry in manifest.get("files", []):
                rel = entry["path"]
                if rel == "manifest.json":
                    continue
                file_path = root / rel
                if not file_path.exists():
                    errors.append(f"missing_file:{rel}")
                    continue
                digest = self._sha256_file(file_path)
                if digest != entry.get("sha256"):
                    errors.append(f"hash_mismatch:{rel}")

            return VerifyResult(valid=len(errors) == 0, backup_id=backup_id, errors=errors)
        finally:
            if staging and staging.exists():
                shutil.rmtree(staging)

    def restore(
        self,
        backup_path: str | Path,
        *,
        target_path: str | Path,
        confirm: bool = False,
    ) -> dict[str, Any]:
        if not confirm:
            return {"restored": False, "reason": "confirm_required"}

        path = Path(backup_path)
        target = Path(target_path)
        if not path.exists():
            return {"restored": False, "reason": "backup_not_found"}

        verify = self.verify(path)
        if not verify.valid:
            return {"restored": False, "reason": "verify_failed", "errors": verify.errors}

        # Refuse restore to same DATABASE_URL without explicit confirm (already required)
        db_url = self.settings.database_url
        if str(target.resolve()) == str(Path(db_url.replace("sqlite+aiosqlite:///", "")).resolve()):
            if not confirm:
                return {"restored": False, "reason": "same_database_refused"}

        target.mkdir(parents=True, exist_ok=True)
        staging: Path | None = None
        try:
            if path.suffix == ".zip":
                staging = target / "_restore_staging"
                if staging.exists():
                    shutil.rmtree(staging)
                staging.mkdir()
                with zipfile.ZipFile(path) as zf:
                    zf.extractall(staging)
                source_root = staging
            else:
                source_root = path

            for item in source_root.iterdir():
                if item.name == "manifest.json":
                    continue
                dest = target / item.name
                if item.is_dir():
                    if dest.exists():
                        shutil.rmtree(dest)
                    shutil.copytree(item, dest)
                else:
                    shutil.copy2(item, dest)

            logger.info("backup_restored", backup_id=verify.backup_id, target=str(target))
            return {
                "restored": True,
                "backup_id": verify.backup_id,
                "target": str(target),
            }
        finally:
            if staging and staging.exists():
                shutil.rmtree(staging)

    @staticmethod
    def _redact_database_url(url: str) -> str:
        if "@" in url:
            prefix, rest = url.split("@", 1)
            if "://" in prefix:
                scheme, _ = prefix.split("://", 1)
                return f"{scheme}://***@{rest}"
        return url
