"""IBKR contract cache / resolve helpers (no Gateway required)."""

from __future__ import annotations

from types import SimpleNamespace

from app.brokers.ibkr_contracts import cache_contract, clear_contract_cache, contract_from_con_id


def test_cache_and_lookup_by_con_id() -> None:
    clear_contract_cache()
    contract = SimpleNamespace(conId=265598, symbol="AAPL", currency="USD", primaryExchange="NASDAQ")
    cache_contract(contract)
    assert contract_from_con_id(265598) is contract
    clear_contract_cache()
    bare = contract_from_con_id(265598)
    assert bare is not None
    assert int(getattr(bare, "conId", 0) or 0) == 265598
