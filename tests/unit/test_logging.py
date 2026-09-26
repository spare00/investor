"""Rotating file handler and log-file prep."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from app.core.logging import setup_logging


@pytest.fixture
def restore_logging() -> Iterator[None]:
    root = logging.getLogger()
    previous = list(root.handlers)
    level = root.level
    yield
    for handler in list(root.handlers):
        handler.close()
        root.removeHandler(handler)
    for handler in previous:
        root.addHandler(handler)
    root.setLevel(level)


def test_empty_log_file_skips_handler(restore_logging: None) -> None:
    setup_logging("INFO", "json", log_file="", max_bytes=1024, backup_count=2)
    root = logging.getLogger()
    assert not any(isinstance(h, RotatingFileHandler) for h in root.handlers)
    assert any(isinstance(h, logging.StreamHandler) for h in root.handlers)


def test_rotating_handler_rolls_over(tmp_path: Path, restore_logging: None) -> None:
    log_path = tmp_path / "investor.log"
    setup_logging(
        "INFO",
        "json",
        log_file=str(log_path),
        max_bytes=1024,
        backup_count=3,
    )
    probe = logging.getLogger("rotation_probe")
    for i in range(50):
        probe.info("payload-%s-%s", i, "x" * 80)
    for handler in logging.getLogger().handlers:
        handler.flush()
    names = {p.name for p in tmp_path.iterdir()}
    assert "investor.log" in names
    assert any(name.startswith("investor.log.") for name in names)
    assert log_path.stat().st_size < 8_000


def test_oversized_existing_log_is_dropped(tmp_path: Path, restore_logging: None) -> None:
    log_path = tmp_path / "investor.log"
    log_path.write_bytes(b"z" * 8_000)
    setup_logging(
        "INFO",
        "json",
        log_file=str(log_path),
        max_bytes=1024,
        backup_count=2,
    )
    assert log_path.stat().st_size < 1_024
    for leftover in tmp_path.iterdir():
        assert leftover.stat().st_size < 2_000
