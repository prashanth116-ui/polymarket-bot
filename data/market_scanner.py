"""Gamma API market scanner — discover, filter, and rank active markets."""

import json
import logging
from datetime import datetime, timezone
from typing import Optional

import requests

from core.constants import GAMMA_API_URL, MIN_VOLUME_24H, MIN_HOURS_TO_RESOLUTION
from core.types import Market

logger = logging.getLogger(__name__)

# Gamma API pagination limit
MAX_PER_PAGE = 100


class MarketScanner:
    """Discovers and filters Polymarket prediction markets via the Gamma API."""

    def __init__(self, timeout: int = 15):
        self.timeout = timeout
        self.base_url = GAMMA_API_URL

    def _get(self, path: str, params: dict = None) -> list | dict:
        url = f"{self.base_url}{path}"
        try:
            resp = requests.get(url, params=params, timeout=self.timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"Gamma API error: {e}")
            return []

    def _parse_market(self, raw: dict) -> Optional[Market]:
        """Parse a raw Gamma API market dict into a Market object."""
        try:
            condition_id = raw.get("conditionId", "")
            if not condition_id:
                return None

            # Parse token IDs from JSON string
            clob_ids_raw = raw.get("clobTokenIds", "[]")
            if isinstance(clob_ids_raw, str):
                clob_ids = json.loads(clob_ids_raw)
            else:
                clob_ids = clob_ids_raw or []

            # Parse outcomes
            outcomes_raw = raw.get("outcomes", "[]")
            if isinstance(outcomes_raw, str):
                outcomes = json.loads(outcomes_raw)
            else:
                outcomes = outcomes_raw or []

            # Build token map
            tokens = {}
            for i, outcome in enumerate(outcomes):
                key = outcome.upper() if outcome.lower() in ("yes", "no") else outcome
                if i < len(clob_ids):
                    tokens[key] = clob_ids[i]

            # Parse outcome prices
            prices_raw = raw.get("outcomePrices", "[]")
            if isinstance(prices_raw, str):
                prices = json.loads(prices_raw)
            else:
                prices = prices_raw or []

            yes_price = float(prices[0]) if len(prices) > 0 else 0.5
            no_price = float(prices[1]) if len(prices) > 1 else 1 - yes_price

            # Parse end date
            end_date = None
            end_str = raw.get("endDate")
            if end_str:
                try:
                    end_date = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    pass

            # Parse tags from events
            tags = []
            events = raw.get("events", [])
            if events and isinstance(events, list):
                for event in events:
                    slug = event.get("slug", "")
                    if slug:
                        tags.append(slug)

            # Parse updated_at
            updated_at = None
            updated_str = raw.get("updatedAt")
            if updated_str:
                try:
                    updated_at = datetime.fromisoformat(updated_str.replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    pass

            return Market(
                condition_id=condition_id,
                question=raw.get("question", ""),
                description=raw.get("description", ""),
                category=raw.get("groupItemTitle", "") or _infer_category(raw),
                end_date=end_date,
                tokens=tokens,
                active=raw.get("active", False),
                volume=float(raw.get("volumeNum", 0) or 0),
                liquidity=float(raw.get("liquidityNum", 0) or 0),
                last_price_yes=yes_price,
                last_price_no=no_price,
                best_bid_yes=float(raw.get("bestBid", 0) or 0),
                best_ask_yes=float(raw.get("bestAsk", 1) or 1),
                spread_yes=float(raw.get("spread", 1) or 1),
                tags=tags,
                updated_at=updated_at,
            )
        except Exception as e:
            logger.warning(f"Failed to parse market: {e}")
            return None

    def scan(
        self,
        limit: int = 200,
        min_volume_24h: float = MIN_VOLUME_24H,
        min_liquidity: float = 0,
        categories: list[str] = None,
        sort_by: str = "volume24hr",
    ) -> list[Market]:
        """Scan for active, tradable markets.

        Args:
            limit: Max markets to return
            min_volume_24h: Minimum 24h volume in USDC
            min_liquidity: Minimum liquidity in USDC
            categories: Filter to these category slugs (optional)
            sort_by: Sort field (volume24hr, liquidity, spread, competitive)

        Returns:
            List of Market objects, sorted by sort_by descending
        """
        all_markets = []
        offset = 0

        while len(all_markets) < limit:
            batch_size = min(MAX_PER_PAGE, limit - len(all_markets))
            params = {
                "limit": batch_size,
                "offset": offset,
                "active": "true",
                "closed": "false",
                "order": sort_by,
                "ascending": "false",
            }

            raw_markets = self._get("/markets", params)
            if not raw_markets or not isinstance(raw_markets, list):
                break

            for raw in raw_markets:
                market = self._parse_market(raw)
                if market is None:
                    continue

                # Apply filters
                vol_24h = float(raw.get("volume24hr", 0) or 0)
                if vol_24h < min_volume_24h:
                    continue
                if market.liquidity < min_liquidity:
                    continue
                if not raw.get("acceptingOrders", False):
                    continue
                if not raw.get("enableOrderBook", False):
                    continue

                all_markets.append(market)

            if len(raw_markets) < batch_size:
                break  # No more pages
            offset += batch_size

        logger.info(f"Scanner found {len(all_markets)} markets (min_vol=${min_volume_24h}, min_liq=${min_liquidity})")
        return all_markets

    def get_market(self, condition_id: str) -> Optional[Market]:
        """Fetch a single market by condition ID."""
        raw_markets = self._get("/markets", {"conditionId": condition_id})
        if raw_markets and isinstance(raw_markets, list) and len(raw_markets) > 0:
            return self._parse_market(raw_markets[0])
        return None

    def search(self, query: str, limit: int = 20) -> list[Market]:
        """Search markets by keyword."""
        raw_markets = self._get("/markets", {
            "limit": limit,
            "active": "true",
            "closed": "false",
            "q": query,
        })
        markets = []
        if isinstance(raw_markets, list):
            for raw in raw_markets:
                m = self._parse_market(raw)
                if m:
                    markets.append(m)
        return markets

    def get_trending(self, limit: int = 20) -> list[Market]:
        """Get trending markets by 24h volume."""
        return self.scan(limit=limit, sort_by="volume24hr")

    def get_high_liquidity(self, limit: int = 20, min_liquidity: float = 5000) -> list[Market]:
        """Get markets with high liquidity (best for market making)."""
        return self.scan(limit=limit, min_liquidity=min_liquidity, sort_by="liquidity")

    def get_wide_spread(self, limit: int = 50, min_spread: float = 0.05) -> list[Market]:
        """Get markets with wide spreads (opportunity for market making)."""
        markets = self.scan(limit=200, sort_by="liquidity")
        wide = [m for m in markets if m.spread_yes >= min_spread]
        wide.sort(key=lambda m: m.spread_yes, reverse=True)
        return wide[:limit]

    def get_near_resolution(self, hours: int = 72, limit: int = 20) -> list[Market]:
        """Get markets resolving within N hours."""
        markets = self.scan(limit=200, sort_by="volume24hr")
        near = []
        for m in markets:
            h = m.hours_to_resolution
            if h is not None and 0 < h <= hours:
                near.append(m)
        near.sort(key=lambda m: m.hours_to_resolution or 999)
        return near[:limit]


def _infer_category(raw: dict) -> str:
    """Infer category from question/description keywords."""
    text = (raw.get("question", "") + " " + raw.get("description", "")).lower()
    categories = {
        "politics": ["president", "election", "congress", "senate", "trump", "biden", "democrat", "republican", "vote"],
        "crypto": ["bitcoin", "ethereum", "btc", "eth", "crypto", "token", "blockchain", "solana"],
        "sports": ["nba", "nfl", "mlb", "nhl", "soccer", "football", "basketball", "championship", "super bowl"],
        "economics": ["gdp", "inflation", "fed", "interest rate", "unemployment", "recession", "cpi"],
        "science": ["nasa", "spacex", "climate", "ai ", "artificial intelligence", "fda"],
        "entertainment": ["oscar", "grammy", "movie", "show", "netflix", "celebrity"],
    }
    for cat, keywords in categories.items():
        if any(kw in text for kw in keywords):
            return cat
    return "other"
