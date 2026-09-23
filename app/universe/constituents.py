"""Index-style membership snapshots (S&P 500 / ASX 50).

Not a live reconstitution feed. Python loads the bundled snapshot so membership
is an index book, not a 40-name Mag7 tuple. Refresh the files when constituents
change; liquidity screening still happens at watch reconstitution.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

_DATA_DIR = Path(__file__).resolve().parent / "snapshots"

_GICS_TO_BUCKET: dict[str, str] = {
    "information technology": "tech",
    "communication services": "communication",
    "consumer discretionary": "consumer",
    "consumer staples": "consumer",
    "energy": "energy",
    "financials": "finance",
    "health care": "healthcare",
    "industrials": "industrials",
    "materials": "materials",
    "real estate": "property",
    "utilities": "utilities",
}


def _read_csv_pairs(path: Path) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    if not path.is_file():
        return rows
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.lower().startswith("symbol,"):
            continue
        parts = line.split(",", 1)
        sym = parts[0].strip().upper()
        sector = parts[1].strip() if len(parts) > 1 else ""
        if sym:
            rows.append((sym, sector))
    return rows


def _read_symbols(path: Path) -> list[str]:
    out: list[str] = []
    if not path.is_file():
        return out
    for raw in path.read_text().splitlines():
        line = raw.strip().upper()
        if not line or line.startswith("#"):
            continue
        out.append(line.split(",", 1)[0].strip())
    return [s for s in out if s]


@lru_cache(maxsize=1)
def sp500_rows() -> tuple[tuple[str, str], ...]:
    return tuple(_read_csv_pairs(_DATA_DIR / "us_sp500.csv"))


@lru_cache(maxsize=1)
def asx50_symbols() -> tuple[str, ...]:
    return tuple(_read_symbols(_DATA_DIR / "asx50.txt"))


def sp500_symbols() -> list[str]:
    return [sym for sym, _ in sp500_rows()]


def gics_sector_buckets() -> dict[str, str]:
    """Symbol → internal sector bucket from the S&P 500 snapshot."""
    out: dict[str, str] = {}
    for sym, gics in sp500_rows():
        bucket = _GICS_TO_BUCKET.get(gics.strip().lower(), "other")
        out[sym] = bucket
    return out
