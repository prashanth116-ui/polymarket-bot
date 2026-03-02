"""Economic data feed — FRED API wrapper."""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests

from config.loader import get_env

logger = logging.getLogger(__name__)

FRED_BASE_URL = "https://api.stlouisfed.org/fred"


@dataclass
class EconomicSeries:
    series_id: str
    title: str
    value: float
    date: str
    units: str = ""
    frequency: str = ""


# Common FRED series for prediction markets
COMMON_SERIES = {
    "UNRATE": "Unemployment Rate",
    "CPIAUCSL": "Consumer Price Index",
    "GDP": "Gross Domestic Product",
    "FEDFUNDS": "Federal Funds Rate",
    "T10Y2Y": "10Y-2Y Treasury Spread",
    "DGS10": "10-Year Treasury Rate",
    "VIXCLS": "VIX Volatility Index",
    "DEXUSEU": "USD/EUR Exchange Rate",
    "PAYEMS": "Total Nonfarm Payrolls",
    "UMCSENT": "Consumer Sentiment",
}


class EconomicDataFeed:
    """FRED API client for economic indicators."""

    def __init__(self, api_key: str = None, timeout: int = 10):
        self.api_key = api_key or get_env("FRED_API_KEY")
        self.timeout = timeout

    def _get(self, endpoint: str, params: dict = None) -> Optional[dict]:
        if not self.api_key:
            logger.debug("FRED API key not configured — skipping")
            return None

        params = params or {}
        params["api_key"] = self.api_key
        params["file_type"] = "json"

        try:
            resp = requests.get(
                f"{FRED_BASE_URL}/{endpoint}",
                params=params,
                timeout=self.timeout,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"FRED API error ({endpoint}): {e}")
            return None

    def get_latest(self, series_id: str) -> Optional[EconomicSeries]:
        """Get the latest observation for a FRED series."""
        data = self._get("series/observations", {
            "series_id": series_id,
            "sort_order": "desc",
            "limit": 1,
        })
        if not data or "observations" not in data:
            return None

        obs = data["observations"]
        if not obs:
            return None

        latest = obs[0]
        try:
            value = float(latest["value"])
        except (ValueError, TypeError):
            return None

        return EconomicSeries(
            series_id=series_id,
            title=COMMON_SERIES.get(series_id, series_id),
            value=value,
            date=latest.get("date", ""),
        )

    def get_series(self, series_id: str, days: int = 365) -> list[EconomicSeries]:
        """Get historical observations for a series."""
        start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        data = self._get("series/observations", {
            "series_id": series_id,
            "observation_start": start,
            "sort_order": "asc",
        })
        if not data or "observations" not in data:
            return []

        results = []
        for obs in data["observations"]:
            try:
                value = float(obs["value"])
            except (ValueError, TypeError):
                continue
            results.append(EconomicSeries(
                series_id=series_id,
                title=COMMON_SERIES.get(series_id, series_id),
                value=value,
                date=obs.get("date", ""),
            ))
        return results

    def get_key_indicators(self) -> dict[str, Optional[EconomicSeries]]:
        """Fetch latest values for all common economic indicators."""
        indicators = {}
        for series_id in COMMON_SERIES:
            indicators[series_id] = self.get_latest(series_id)
        return indicators

    def search_series(self, query: str, limit: int = 5) -> list[dict]:
        """Search for FRED series by keyword."""
        data = self._get("series/search", {
            "search_text": query,
            "limit": limit,
        })
        if not data or "seriess" not in data:
            return []

        return [
            {
                "id": s["id"],
                "title": s.get("title", ""),
                "frequency": s.get("frequency", ""),
                "units": s.get("units", ""),
                "popularity": s.get("popularity", 0),
            }
            for s in data["seriess"]
        ]
