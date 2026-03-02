"""Tests for market scanner and CLOB client."""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
from datetime import datetime, timezone
from data.market_scanner import MarketScanner, _infer_category
from core.types import Market


def test_parse_market():
    """Test parsing a raw Gamma API market dict."""
    scanner = MarketScanner()
    raw = {
        "conditionId": "0xabc123",
        "question": "Will Bitcoin exceed $100k?",
        "description": "Test market about bitcoin price",
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["0.65", "0.35"]',
        "clobTokenIds": '["token_yes_123", "token_no_456"]',
        "volumeNum": 50000,
        "liquidityNum": 10000,
        "active": True,
        "endDate": "2026-12-31T23:59:59Z",
        "acceptingOrders": True,
        "enableOrderBook": True,
        "spread": 0.03,
        "bestBid": 0.63,
        "bestAsk": 0.66,
        "events": [{"slug": "bitcoin-100k"}],
    }
    market = scanner._parse_market(raw)
    assert market is not None
    assert market.condition_id == "0xabc123"
    assert market.question == "Will Bitcoin exceed $100k?"
    assert market.last_price_yes == 0.65
    assert market.last_price_no == 0.35
    assert market.yes_token_id == "token_yes_123"
    assert market.no_token_id == "token_no_456"
    assert market.volume == 50000
    assert market.liquidity == 10000
    assert market.active is True
    assert market.end_date is not None


def test_parse_market_missing_fields():
    """Gracefully handle missing fields."""
    scanner = MarketScanner()
    raw = {
        "conditionId": "0xdef",
        "question": "Test?",
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["0.5", "0.5"]',
        "clobTokenIds": '["t1", "t2"]',
        "active": True,
    }
    market = scanner._parse_market(raw)
    assert market is not None
    assert market.last_price_yes == 0.5


def test_parse_market_no_condition_id():
    """Should return None if no condition_id."""
    scanner = MarketScanner()
    raw = {"question": "No ID?"}
    assert scanner._parse_market(raw) is None


def test_infer_category():
    """Test category inference from keywords."""
    assert _infer_category({"question": "Will Trump win?", "description": ""}) == "politics"
    assert _infer_category({"question": "Bitcoin price?", "description": ""}) == "crypto"
    assert _infer_category({"question": "NBA finals?", "description": ""}) == "sports"
    assert _infer_category({"question": "Will GDP grow?", "description": ""}) == "economics"
    assert _infer_category({"question": "Random question?", "description": ""}) == "other"


def test_market_midpoint():
    """Test Market.midpoint_yes property."""
    m = Market(
        condition_id="test",
        question="Test?",
        description="",
        category="",
        end_date=None,
        tokens={"YES": "t1", "NO": "t2"},
        best_bid_yes=0.60,
        best_ask_yes=0.64,
    )
    assert abs(m.midpoint_yes - 0.62) < 0.001


def test_market_hours_to_resolution():
    """Test hours_to_resolution calculation."""
    from datetime import timedelta
    future = datetime.now(timezone.utc) + timedelta(hours=48)
    m = Market(
        condition_id="test",
        question="Test?",
        description="",
        category="",
        end_date=future,
        tokens={},
    )
    hrs = m.hours_to_resolution
    assert hrs is not None
    assert 47 < hrs < 49


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
