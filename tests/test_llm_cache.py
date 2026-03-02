"""Tests for LLM forecaster cache price bucketing and per-tier TTL."""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import time
from unittest.mock import patch, MagicMock
from models.llm_forecaster import LLMForecaster


def test_cache_key_includes_price_bucket():
    """Cache key should change when price moves to a different 5% bucket."""
    forecaster = LLMForecaster()
    key1 = forecaster._cache_key("market1", "YES", "model", 0.50)
    key2 = forecaster._cache_key("market1", "YES", "model", 0.52)
    key3 = forecaster._cache_key("market1", "YES", "model", 0.56)

    # 0.50 and 0.52 round to same 0.05 bucket (0.50)
    assert key1 == key2
    # 0.56 rounds to 0.55 — different bucket
    assert key1 != key3


def test_cache_key_different_outcomes():
    """Different outcomes should produce different cache keys."""
    forecaster = LLMForecaster()
    key_yes = forecaster._cache_key("market1", "YES", "model", 0.50)
    key_no = forecaster._cache_key("market1", "NO", "model", 0.50)
    assert key_yes != key_no


def test_cache_key_boundary_buckets():
    """Price at exact boundary should round correctly."""
    forecaster = LLMForecaster()
    key_25 = forecaster._cache_key("m", "YES", "mod", 0.25)
    key_27 = forecaster._cache_key("m", "YES", "mod", 0.27)
    key_28 = forecaster._cache_key("m", "YES", "mod", 0.28)

    # 0.25 -> bucket 0.25, 0.27 -> bucket 0.25, 0.28 -> bucket 0.30
    assert key_25 == key_27
    assert key_27 != key_28


def test_screening_cache_ttl_longer():
    """Screening tier cache should last longer than final tier."""
    forecaster = LLMForecaster(cache_ttl=3600, screening_cache_ttl=7200)

    # Insert a cached result at a known time
    result = {"probability": 0.65, "confidence": 0.8, "reasoning": "test"}
    key = "test-key"

    # Set cache with old timestamp (4000 seconds ago)
    forecaster._cache[key] = (time.time() - 4000, result)

    # Final tier (3600s TTL) should miss
    assert forecaster._get_cached(key, tier="final") is None

    # Re-insert for screening test
    forecaster._cache[key] = (time.time() - 4000, result)

    # Screening tier (7200s TTL) should hit
    cached = forecaster._get_cached(key, tier="screening")
    assert cached is not None
    assert cached["probability"] == 0.65


def test_cache_expired():
    """Expired cache entries should return None and be removed."""
    forecaster = LLMForecaster(cache_ttl=100, screening_cache_ttl=200)
    result = {"probability": 0.5, "confidence": 0.5, "reasoning": "old"}
    key = "expire-test"

    # Set with very old timestamp
    forecaster._cache[key] = (time.time() - 300, result)

    assert forecaster._get_cached(key, tier="screening") is None
    assert key not in forecaster._cache


def test_cache_fresh():
    """Fresh cache entries should be returned."""
    forecaster = LLMForecaster(cache_ttl=3600)
    result = {"probability": 0.72, "confidence": 0.9, "reasoning": "fresh"}
    key = "fresh-test"

    forecaster._set_cache(key, result)
    cached = forecaster._get_cached(key, tier="final")
    assert cached is not None
    assert cached["probability"] == 0.72


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
