"""
Multi-source news aggregator.

Sources:
  1. NewsAPI.org  — keyword search, structured JSON
  2. GNews API    — fallback keyword search
  3. RSS feeds    — Reuters, AP, BBC via feedparser (no key needed)
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import feedparser
import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from src.utils.logger import get_logger

log = get_logger(__name__)


@dataclass
class Article:
    title: str
    url: str
    source: str
    published_at: datetime
    snippet: str = ""

    @property
    def url_hash(self) -> str:
        return hashlib.md5(self.url.encode()).hexdigest()


RSS_FEEDS = {
    "Reuters": "https://feeds.reuters.com/reuters/topNews",
    "AP News": "https://feeds.apnews.com/apnews/topnews",
    "BBC": "http://feeds.bbci.co.uk/news/rss.xml",
    "BBC World": "http://feeds.bbci.co.uk/news/world/rss.xml",
    "Reuters Politics": "https://feeds.reuters.com/Reuters/PoliticsNews",
    "Reuters Business": "https://feeds.reuters.com/reuters/businessNews",
}


class NewsAggregator:
    def __init__(self, newsapi_key: str = "", gnews_key: str = ""):
        self.newsapi_key = newsapi_key
        self.gnews_key = gnews_key
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "PolymarketBot/1.0"})

    # ------------------------------------------------------------------ #
    # Main search interface                                                #
    # ------------------------------------------------------------------ #

    def search_news(self, query: str, days_back: int = 7) -> list[Article]:
        """
        Search all configured sources for articles matching query.
        Returns deduplicated list sorted by publication date descending.
        """
        articles: list[Article] = []
        seen_hashes: set[str] = set()

        sources = [
            self._search_newsapi,
            self._search_gnews,
            self._search_rss,
        ]

        for source_fn in sources:
            try:
                batch = source_fn(query, days_back)
                for a in batch:
                    if a.url_hash not in seen_hashes:
                        seen_hashes.add(a.url_hash)
                        articles.append(a)
            except Exception as exc:
                log.warning(f"News source {source_fn.__name__} failed: {exc}")

        articles.sort(key=lambda a: a.published_at.replace(tzinfo=None), reverse=True)
        log.debug(f"Found {len(articles)} articles for query '{query}'")
        return articles

    # ------------------------------------------------------------------ #
    # NewsAPI                                                              #
    # ------------------------------------------------------------------ #

    @retry(
        retry=retry_if_exception_type(requests.RequestException),
        stop=stop_after_attempt(2),
        wait=wait_exponential(min=1, max=10),
        reraise=False,
    )
    def _search_newsapi(self, query: str, days_back: int) -> list[Article]:
        if not self.newsapi_key:
            return []

        from_date = (datetime.utcnow() - timedelta(days=days_back)).strftime("%Y-%m-%d")
        resp = self._session.get(
            "https://newsapi.org/v2/everything",
            params={
                "q": query,
                "from": from_date,
                "sortBy": "relevancy",
                "pageSize": 20,
                "language": "en",
                "apiKey": self.newsapi_key,
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()

        articles = []
        for item in data.get("articles", []):
            try:
                published = datetime.fromisoformat(
                    item["publishedAt"].replace("Z", "+00:00")
                )
                articles.append(Article(
                    title=item.get("title", ""),
                    url=item.get("url", ""),
                    source=item.get("source", {}).get("name", "NewsAPI"),
                    published_at=published,
                    snippet=item.get("description", "") or item.get("content", ""),
                ))
            except Exception:
                continue
        return articles

    # ------------------------------------------------------------------ #
    # GNews                                                                #
    # ------------------------------------------------------------------ #

    def _search_gnews(self, query: str, days_back: int) -> list[Article]:
        if not self.gnews_key:
            return []

        resp = self._session.get(
            "https://gnews.io/api/v4/search",
            params={
                "q": query,
                "lang": "en",
                "max": 10,
                "token": self.gnews_key,
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()

        cutoff = datetime.utcnow() - timedelta(days=days_back)
        articles = []
        for item in data.get("articles", []):
            try:
                published = datetime.fromisoformat(
                    item["publishedAt"].replace("Z", "+00:00")
                )
                if published.replace(tzinfo=None) < cutoff:
                    continue
                articles.append(Article(
                    title=item.get("title", ""),
                    url=item.get("url", ""),
                    source=item.get("source", {}).get("name", "GNews"),
                    published_at=published,
                    snippet=item.get("description", ""),
                ))
            except Exception:
                continue
        return articles

    # ------------------------------------------------------------------ #
    # RSS feeds                                                            #
    # ------------------------------------------------------------------ #

    def _search_rss(self, query: str, days_back: int) -> list[Article]:
        cutoff = datetime.utcnow() - timedelta(days=days_back)
        query_words = set(query.lower().split())
        articles = []

        for source_name, feed_url in RSS_FEEDS.items():
            try:
                feed = feedparser.parse(feed_url)
                for entry in feed.entries[:30]:
                    title = entry.get("title", "")
                    summary = entry.get("summary", "")
                    combined = (title + " " + summary).lower()

                    # Simple keyword relevance check
                    if not any(w in combined for w in query_words if len(w) > 3):
                        continue

                    # Parse date
                    pub = entry.get("published_parsed") or entry.get("updated_parsed")
                    if pub:
                        published = datetime(*pub[:6])
                    else:
                        published = datetime.utcnow()

                    if published < cutoff:
                        continue

                    articles.append(Article(
                        title=title,
                        url=entry.get("link", ""),
                        source=source_name,
                        published_at=published,
                        snippet=summary[:300],
                    ))
            except Exception as exc:
                log.debug(f"RSS feed {source_name} error: {exc}")
                continue

        return articles

    # ------------------------------------------------------------------ #
    # Utility                                                              #
    # ------------------------------------------------------------------ #

    @staticmethod
    def format_for_prompt(articles: list[Article], max_articles: int = 8) -> str:
        """Format articles as a text block suitable for an LLM prompt."""
        if not articles:
            return "No recent news articles found."

        lines = []
        for i, a in enumerate(articles[:max_articles], 1):
            date_str = a.published_at.strftime("%Y-%m-%d")
            lines.append(f"{i}. [{date_str}] {a.source}: {a.title}")
            if a.snippet:
                snippet = a.snippet[:200].replace("\n", " ")
                lines.append(f"   {snippet}")
        return "\n".join(lines)
