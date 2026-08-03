"""Collectors package."""

from app.collectors.earnings import get_earnings_provider
from app.collectors.macro_data import get_macro_data_provider
from app.collectors.market_data import get_market_data_provider
from app.collectors.news import get_news_provider
from app.collectors.sec_filings import get_sec_filings_provider

__all__ = [
    "get_earnings_provider",
    "get_macro_data_provider",
    "get_market_data_provider",
    "get_news_provider",
    "get_sec_filings_provider",
]
