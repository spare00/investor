"""Structured logging setup using structlog."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, cast

import structlog

_DEFAULT_MAX_BYTES = 50 * 1024 * 1024
_DEFAULT_BACKUP_COUNT = 5


def _prepare_log_file(path: Path, *, max_bytes: int, backup_count: int) -> None:
    """Drop already-oversized logs so a 500MB leftover is not kept as .1."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > max_bytes:
        leftover = Path(f"{path}.1")
        leftover.unlink(missing_ok=True)
        path.rename(leftover)
        if leftover.stat().st_size > max_bytes:
            leftover.unlink()
    for i in range(1, backup_count + 1):
        bak = Path(f"{path}.{i}")
        if bak.exists() and bak.stat().st_size > max_bytes:
            bak.unlink()


def setup_logging(
    level: str = "INFO",
    log_format: str = "json",
    *,
    log_file: str | None = None,
    max_bytes: int = _DEFAULT_MAX_BYTES,
    backup_count: int = _DEFAULT_BACKUP_COUNT,
) -> None:
    """Configure stdlib + structlog. Call once at process start.

    When ``log_file`` is set and stdout is not a TTY (``scripts/start.sh`` /
    nohup), skip the stream handler so the nohup stdio capture cannot grow
    without bound. Interactive ``uvicorn`` still logs to the terminal and file.
    """
    log_level = getattr(logging, level.upper(), logging.INFO)
    file_path = (log_file or "").strip()
    max_bytes = max(int(max_bytes), 1024)
    backup_count = max(int(backup_count), 1)

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if log_format == "console":
        renderer: Any = structlog.dev.ConsoleRenderer()
    else:
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(log_level)

    to_stdout = sys.stdout.isatty() or not file_path
    if to_stdout:
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(formatter)
        root.addHandler(stream)

    if file_path:
        path = Path(file_path)
        _prepare_log_file(path, max_bytes=max_bytes, backup_count=backup_count)
        rotating = RotatingFileHandler(
            path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        rotating.setFormatter(formatter)
        root.addHandler(rotating)
        try:
            path.chmod(0o600)
        except OSError:
            pass

    if not root.handlers:
        fallback = logging.StreamHandler(sys.stdout)
        fallback.setFormatter(formatter)
        root.addHandler(fallback)

    # Quiet noisy libraries
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return cast(structlog.stdlib.BoundLogger, structlog.get_logger(name))
