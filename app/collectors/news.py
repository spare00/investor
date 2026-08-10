"""News collectors — stub and HTTP-ready provider adapters."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.collectors.base import NewsProvider, RawNewsItem
from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class StubNewsProvider:
    """Deterministic offline news for tests and paper dry-runs."""

    name = "stub"

    def __init__(self, items: list[RawNewsItem] | None = None) -> None:
        self._items = items

    async def fetch_news(
        self,
        *,
        symbols: list[str] | None = None,
        since: datetime | None = None,
        limit: int = 50,
    ) -> list[RawNewsItem]:
        now = datetime.now(UTC)
        default = self._items or [
            RawNewsItem(
                headline="Fed officials signal patience on rate cuts",
                source="Reuters",
                published_at=now - timedelta(minutes=45),
                provider=self.name,
                external_id="stub-fed-1",
                symbols=["SPY", "QQQ"],
                category="fed",
                url="https://example.com/fed-patience",
            ),
            RawNewsItem(
                headline="Megacap tech leads premarket gains",
                source="Bloomberg",
                published_at=now - timedelta(minutes=30),
                provider=self.name,
                external_id="stub-tech-1",
                symbols=["NVDA", "MSFT", "AAPL", "QQQ"],
                category="corporate",
            ),
            RawNewsItem(
                headline="Fed officials signal patience on rate cuts",
                source="AP",
                published_at=now - timedelta(minutes=40),
                provider=self.name,
                external_id="stub-fed-dup",
                symbols=["SPY"],
                category="fed",
            ),
            RawNewsItem(
                headline="BHP lifts iron ore guidance on China demand",
                source="AFR",
                published_at=now - timedelta(minutes=25),
                provider=self.name,
                external_id="stub-asx-bhp-1",
                symbols=["BHP", "VAS"],
                category="guidance",
                url="https://example.com/bhp-guidance",
            ),
            RawNewsItem(
                headline="ASX banks steady ahead of RBA decision",
                source="AAP",
                published_at=now - timedelta(minutes=20),
                provider=self.name,
                external_id="stub-asx-banks-1",
                symbols=["CBA", "VAS", "IOZ"],
                category="macro",
            ),
        ]
        filtered = default
        if symbols:
            symset = {s.upper() for s in symbols}
            # AU-only universes should not inherit US megacap stubs.
            filtered = [
                i
                for i in filtered
                if not i.symbols or symset.intersection(s.upper() for s in i.symbols)
            ]
        if since:
            filtered = [i for i in filtered if i.published_at >= since]
        return filtered[:limit]


class NewsAPIProvider:
    """
    NewsAPI.org adapter scaffold.

    Requires NEWS_API_KEY. Fail-closed: returns empty list and logs on error
    rather than inventing headlines.
    """

    name = "newsapi"

    def __init__(self, api_key: str | None = None) -> None:
        settings = get_settings()
        secret = settings.news_api_key
        self.api_key = api_key or (secret.get_secret_value() if secret else None)

    async def fetch_news(
        self,
        *,
        symbols: list[str] | None = None,
        since: datetime | None = None,
        limit: int = 50,
    ) -> list[RawNewsItem]:
        if not self.api_key:
            logger.warning("newsapi_missing_key", action="fail_closed_empty")
            return []
        # Real HTTP wiring lands when keys are available; keep fail-closed.
        logger.warning(
            "newsapi_not_wired",
            message="HTTP client deferred; use stub or inject provider in tests",
        )
        return []


def get_news_provider(name: str | None = None) -> NewsProvider:
    settings = get_settings()
    provider_name = (name or settings.news_provider).lower()
    if provider_name == "newsapi":
        return NewsAPIProvider()
    return StubNewsProvider()
