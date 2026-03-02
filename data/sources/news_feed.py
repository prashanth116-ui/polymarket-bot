"""News data feed — NewsAPI + RSS polling."""

import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests

from config.loader import get_env

logger = logging.getLogger(__name__)


@dataclass
class NewsArticle:
    title: str
    description: str
    source: str
    url: str
    published_at: Optional[datetime] = None
    category: str = ""
    relevance: float = 0.0


class NewsFeed:
    """Aggregates news from NewsAPI and RSS feeds."""

    def __init__(self, api_key: str = None, timeout: int = 10):
        self.api_key = api_key or get_env("NEWS_API_KEY")
        self.timeout = timeout
        self.base_url = "https://newsapi.org/v2"

        # Default RSS feeds for prediction market categories
        self.rss_feeds = {
            "politics": [
                "https://rss.nytimes.com/services/xml/rss/nyt/Politics.xml",
                "https://feeds.bbci.co.uk/news/politics/rss.xml",
            ],
            "economics": [
                "https://feeds.bbci.co.uk/news/business/rss.xml",
                "https://rss.nytimes.com/services/xml/rss/nyt/Economy.xml",
            ],
            "crypto": [
                "https://cointelegraph.com/rss",
            ],
            "science": [
                "https://rss.nytimes.com/services/xml/rss/nyt/Science.xml",
            ],
        }

    def search_newsapi(self, query: str, days: int = 7, page_size: int = 10) -> list[NewsArticle]:
        """Search NewsAPI for articles matching a query."""
        if not self.api_key:
            logger.debug("NewsAPI key not configured — skipping")
            return []

        from_date = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        try:
            resp = requests.get(
                f"{self.base_url}/everything",
                params={
                    "q": query,
                    "from": from_date,
                    "sortBy": "relevancy",
                    "pageSize": page_size,
                    "language": "en",
                    "apiKey": self.api_key,
                },
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()

            articles = []
            for article in data.get("articles", []):
                pub_at = None
                if article.get("publishedAt"):
                    try:
                        pub_at = datetime.fromisoformat(
                            article["publishedAt"].replace("Z", "+00:00")
                        )
                    except (ValueError, TypeError):
                        pass

                articles.append(NewsArticle(
                    title=article.get("title", ""),
                    description=article.get("description", "") or "",
                    source=article.get("source", {}).get("name", ""),
                    url=article.get("url", ""),
                    published_at=pub_at,
                ))

            logger.info(f"NewsAPI: {len(articles)} articles for '{query}'")
            return articles

        except Exception as e:
            logger.error(f"NewsAPI error: {e}")
            return []

    def get_headlines(self, category: str = "general", count: int = 10) -> list[NewsArticle]:
        """Get top headlines from NewsAPI."""
        if not self.api_key:
            return []

        try:
            resp = requests.get(
                f"{self.base_url}/top-headlines",
                params={
                    "category": category,
                    "country": "us",
                    "pageSize": count,
                    "apiKey": self.api_key,
                },
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()

            articles = []
            for article in data.get("articles", []):
                articles.append(NewsArticle(
                    title=article.get("title", ""),
                    description=article.get("description", "") or "",
                    source=article.get("source", {}).get("name", ""),
                    url=article.get("url", ""),
                    category=category,
                ))
            return articles

        except Exception as e:
            logger.error(f"NewsAPI headlines error: {e}")
            return []

    def fetch_rss(self, url: str, max_items: int = 10) -> list[NewsArticle]:
        """Fetch articles from an RSS feed."""
        try:
            resp = requests.get(url, timeout=self.timeout)
            resp.raise_for_status()
            root = ET.fromstring(resp.text)

            articles = []
            # Handle both RSS 2.0 and Atom feeds
            items = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")

            for item in items[:max_items]:
                # RSS 2.0
                title = item.findtext("title", "")
                desc = item.findtext("description", "")
                link = item.findtext("link", "")
                pub_date = item.findtext("pubDate")

                # Atom fallback
                if not title:
                    title = item.findtext("{http://www.w3.org/2005/Atom}title", "")
                if not link:
                    link_el = item.find("{http://www.w3.org/2005/Atom}link")
                    if link_el is not None:
                        link = link_el.get("href", "")

                articles.append(NewsArticle(
                    title=title,
                    description=desc[:500] if desc else "",
                    source=url.split("/")[2],
                    url=link,
                ))

            logger.debug(f"RSS: {len(articles)} articles from {url.split('/')[2]}")
            return articles

        except Exception as e:
            logger.error(f"RSS fetch error ({url}): {e}")
            return []

    def get_category_news(self, category: str, max_items: int = 20) -> list[NewsArticle]:
        """Get news for a specific category from RSS feeds + NewsAPI."""
        articles = []

        # RSS feeds
        feeds = self.rss_feeds.get(category, [])
        for feed_url in feeds:
            articles.extend(self.fetch_rss(feed_url, max_items=max_items // max(1, len(feeds))))

        # NewsAPI supplement
        if self.api_key:
            api_articles = self.search_newsapi(category, days=3, page_size=max_items)
            articles.extend(api_articles)

        # Deduplicate by title
        seen = set()
        unique = []
        for a in articles:
            key = a.title.lower().strip()
            if key and key not in seen:
                seen.add(key)
                a.category = category
                unique.append(a)

        return unique[:max_items]

    def search_market_news(self, question: str, max_items: int = 10) -> list[NewsArticle]:
        """Search for news relevant to a specific market question."""
        # Extract key terms from the question
        stop_words = {"will", "the", "be", "in", "a", "an", "by", "to", "of", "and", "or", "is", "it", "on"}
        words = question.replace("?", "").split()
        key_terms = [w for w in words if w.lower() not in stop_words and len(w) > 2]
        query = " ".join(key_terms[:5])

        if not query:
            return []

        articles = []
        if self.api_key:
            articles = self.search_newsapi(query, days=7, page_size=max_items)

        return articles
