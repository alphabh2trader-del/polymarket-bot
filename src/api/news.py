"""
Multi-source news aggregator.

Sources:
  1. TheNewsAPI   — keyword search, structured JSON (primary)
  2. NewsAPI.org  — keyword search, structured JSON
  3. GNews API    — fallback keyword search
  4. RSS feeds    — Reuters, AP, BBC via feedparser (no key needed)
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

# Boilerplate words common to prediction-market questions. Stripped when building
# a news search query so the search keys on the distinctive entities, not "will",
# "before", etc. (a raw question sent verbatim is treated as an AND of every word
# and usually returns nothing).
QUERY_STOPWORDS = {
    "will", "the", "a", "an", "and", "or", "of", "to", "in", "on", "at", "by",
    "for", "be", "is", "are", "was", "were", "been", "being", "this", "that",
    "these", "those", "before", "after", "during", "until", "than", "then",
    "there", "their", "it", "its", "as", "with", "from", "into", "over", "under",
    "any", "all", "more", "less", "most", "least", "have", "has", "had", "do",
    "does", "did", "get", "gets", "make", "makes", "who", "what", "when", "where",
    "which", "whether", "how", "many", "much", "another",
}


class NewsAggregator:
    def __init__(self, newsapi_key: str = "", gnews_key: str = "", thenewsapi_key: str = ""):
        self.newsapi_key = newsapi_key
        self.gnews_key = gnews_key
        self.thenewsapi_key = thenewsapi_key
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
            self._search_thenewsapi,
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
    # TheNewsAPI (primary)                                                 #
    # ------------------------------------------------------------------ #

    @retry(
        retry=retry_if_exception_type(requests.RequestException),
        stop=stop_after_attempt(2),
        wait=wait_exponential(min=1, max=10),
        reraise=False,
    )
    def _search_thenewsapi(self, query: str, days_back: int) -> list[Article]:
        if not self.thenewsapi_key:
            return []

        published_after = (datetime.utcnow() - timedelta(days=days_back)).strftime("%Y-%m-%d")
        resp = self._session.get(
            "https://api.thenewsapi.com/v1/news/all",
            params={
                "api_token": self.thenewsapi_key,
                "search": query,
                "language": "en",
                "published_after": published_after,
                "sort": "relevance_score",
                "limit": 10,   # free tier caps this lower; the API clamps it.
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()

        articles = []
        for item in data.get("data", []):
            try:
                raw = (item.get("published_at") or "").replace("Z", "+00:00")
                published = datetime.fromisoformat(raw)
                articles.append(Article(
                    title=item.get("title", ""),
                    url=item.get("url", ""),
                    source=item.get("source", "TheNewsAPI"),
                    published_at=published,
                    snippet=item.get("description", "") or item.get("snippet", ""),
                ))
            except Exception:
                continue
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
        import re
        cutoff = datetime.utcnow() - timedelta(days=days_back)
        # Only meaningful words count toward relevance. Requiring >=2 distinct
        # whole-word matches stops generic headlines from matching on a single
        # common word like "win" or "before" and crowding out real articles.
        meaningful = {
            w for w in query.lower().split()
            if len(w) >= 4 and w not in QUERY_STOPWORDS
        }
        if not meaningful:
            meaningful = {w for w in query.lower().split() if len(w) >= 4}
        need = min(2, len(meaningful)) if meaningful else 1
        patterns = [re.compile(rf"\b{re.escape(w)}\b") for w in meaningful]
        articles = []

        for source_name, feed_url in RSS_FEEDS.items():
            try:
                feed = feedparser.parse(feed_url)
                for entry in feed.entries[:30]:
                    title = entry.get("title", "")
                    summary = entry.get("summary", "")
                    combined = (title + " " + summary).lower()

                    # Relevance: at least `need` distinct meaningful words present
                    # as whole words (avoids "win" matching "winter").
                    if sum(1 for p in patterns if p.search(combined)) < need:
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
    def build_search_query(question: str, max_terms: int = 6) -> str:
        """
        Turn a market question into a focused news search query.

        Keeps the distinctive terms — proper nouns (capitalized mid-sentence),
        substantive words (len >= 5), and 4-digit years — and drops boilerplate
        stopwords. A raw truncated question is a poor query: news APIs treat it as
        an AND of every word and usually return nothing. Falls back to the trimmed
        question if nothing distinctive survives.
        """
        import re
        cleaned = question.replace("?", " ").strip()
        # [^\W_] keeps unicode word chars (accented letters like é) but drops
        # underscores, so names like "Québécois" survive intact.
        tokens = re.findall(r"[^\W_]+", cleaned, re.UNICODE)
        terms: list[str] = []
        for i, tok in enumerate(tokens):
            if tok.lower() in QUERY_STOPWORDS:
                continue
            is_proper = i > 0 and tok[0].isupper() and len(tok) >= 2
            is_year = tok.isdigit() and len(tok) == 4
            if is_proper or is_year or len(tok) >= 5:
                terms.append(tok)
            if len(terms) >= max_terms:
                break
        if not terms:
            return cleaned[:80]
        return " ".join(terms)

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
