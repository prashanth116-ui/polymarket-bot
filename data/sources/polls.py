"""Polling data — RealClearPolitics scraper + 538 data."""

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import requests

logger = logging.getLogger(__name__)


@dataclass
class PollResult:
    question: str
    source: str
    candidate_a: str
    candidate_a_pct: float
    candidate_b: str
    candidate_b_pct: float
    sample_size: Optional[int] = None
    poll_date: Optional[str] = None
    margin_of_error: Optional[float] = None
    url: str = ""


@dataclass
class PollAverage:
    question: str
    source: str
    candidate_a: str
    candidate_a_avg: float
    candidate_b: str
    candidate_b_avg: float
    spread: float = 0.0
    num_polls: int = 0
    as_of: Optional[str] = None


class PollsFeed:
    """Aggregates polling data from public sources."""

    def __init__(self, timeout: int = 15):
        self.timeout = timeout

    def get_rcp_average(self, slug: str) -> Optional[PollAverage]:
        """Fetch RealClearPolitics polling average via their public JSON.

        Args:
            slug: RCP slug (e.g., "president/2028-generic-ballot")
        """
        url = f"https://www.realclearpolling.com/polls/{slug}"
        try:
            resp = requests.get(url, timeout=self.timeout, headers={
                "User-Agent": "Mozilla/5.0 (research bot)"
            })
            if resp.status_code != 200:
                logger.debug(f"RCP returned {resp.status_code} for {slug}")
                return None

            # Extract polling data from page
            # RCP embeds JSON data in script tags
            text = resp.text

            # Look for RCP average in the HTML
            avg_match = re.search(r'"rcp_avg":\s*\{([^}]+)\}', text)
            if avg_match:
                logger.debug(f"Found RCP average data for {slug}")

            return None  # Parsing depends on current RCP page structure

        except Exception as e:
            logger.error(f"RCP fetch error: {e}")
            return None

    def get_538_forecast(self, model: str = "president") -> Optional[dict]:
        """Fetch 538/ABC forecast data.

        Args:
            model: Forecast model type
        """
        # 538 moved to ABC News — check current API
        url = f"https://projects.fivethirtyeight.com/polls/{model}/"
        try:
            resp = requests.get(url, timeout=self.timeout, headers={
                "User-Agent": "Mozilla/5.0 (research bot)"
            })
            if resp.status_code != 200:
                return None

            # 538 CSV endpoints (if available)
            return {"status": "page_loaded", "url": url}

        except Exception as e:
            logger.error(f"538 fetch error: {e}")
            return None

    def estimate_probability_from_polls(
        self,
        candidate_pct: float,
        opponent_pct: float,
        margin_of_error: float = 3.0,
        days_to_event: int = 30,
    ) -> float:
        """Convert polling percentage to win probability estimate.

        Uses a simple normal distribution approximation.
        Lead / MOE gives z-score, converted to probability.

        Args:
            candidate_pct: Candidate's polling average
            opponent_pct: Opponent's polling average
            margin_of_error: Typical poll MOE
            days_to_event: Days until the event

        Returns:
            Estimated win probability (0-1)
        """
        import math

        lead = candidate_pct - opponent_pct

        # Adjust MOE for time — uncertainty grows with time to event
        time_factor = 1.0 + (days_to_event / 365) * 0.5
        adjusted_moe = margin_of_error * time_factor

        if adjusted_moe <= 0:
            return 0.5

        z = lead / adjusted_moe

        # Normal CDF approximation
        prob = 0.5 * (1 + math.erf(z / math.sqrt(2)))

        return max(0.01, min(0.99, prob))

    def search_polls(self, query: str) -> list[dict]:
        """Search for relevant polls across sources."""
        results = []

        # Try RCP
        rcp_data = self.get_rcp_average(query)
        if rcp_data:
            results.append({
                "source": "RealClearPolitics",
                "data": rcp_data,
            })

        return results
