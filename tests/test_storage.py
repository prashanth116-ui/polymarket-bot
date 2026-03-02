"""Tests for SQLite storage."""

import sys
import os
import tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.storage import Storage


def _make_storage():
    """Create a temp storage instance."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return Storage(db_path=path), path


def test_upsert_and_get_market():
    store, path = _make_storage()
    try:
        store.upsert_market(
            condition_id="0xabc",
            question="Will X happen?",
            category="politics",
            volume=5000,
            liquidity=1000,
            last_price_yes=0.65,
        )
        m = store.get_market("0xabc")
        assert m is not None
        assert m["question"] == "Will X happen?"
        assert m["category"] == "politics"
        assert m["volume"] == 5000
        assert m["last_price_yes"] == 0.65
    finally:
        store.close()
        os.unlink(path)


def test_upsert_updates_existing():
    store, path = _make_storage()
    try:
        store.upsert_market("0xabc", "Q1", volume=100)
        store.upsert_market("0xabc", "Q1 updated", volume=200)
        m = store.get_market("0xabc")
        assert m["question"] == "Q1 updated"
        assert m["volume"] == 200
    finally:
        store.close()
        os.unlink(path)


def test_record_and_get_trades():
    store, path = _make_storage()
    try:
        store.record_trade("m1", "YES", "BUY", 0.50, 10, 5.0, fee=0.10, strategy="edge")
        store.record_trade("m1", "YES", "SELL", 0.60, 10, 6.0, fee=0.12, strategy="edge")
        trades = store.get_trades("m1")
        assert len(trades) == 2
        assert trades[0]["side"] == "SELL"  # Most recent first
        assert trades[1]["side"] == "BUY"
    finally:
        store.close()
        os.unlink(path)


def test_trade_count():
    store, path = _make_storage()
    try:
        store.record_trade("m1", "YES", "BUY", 0.50, 10, 5.0)
        store.record_trade("m2", "NO", "BUY", 0.30, 20, 6.0)
        assert store.get_trade_count(days=1) == 2
    finally:
        store.close()
        os.unlink(path)


def test_daily_pnl():
    store, path = _make_storage()
    try:
        store.record_daily_pnl("2026-03-01", "edge", 50.0, trades=5, wins=4, losses=1)
        store.record_daily_pnl("2026-02-28", "edge", -10.0, trades=3, wins=1, losses=2)
        rows = store.get_daily_pnl(days=7)
        assert len(rows) == 2
        total = store.get_total_pnl(days=7)
        assert abs(total - 40.0) < 0.01
    finally:
        store.close()
        os.unlink(path)


def test_estimates():
    store, path = _make_storage()
    try:
        store.record_estimate("m1", "YES", 0.70, 0.85, "llm_claude", "Strong evidence")
        store.record_estimate("m1", "YES", 0.72, 0.80, "llm_claude", "Updated")
        latest = store.get_latest_estimate("m1", "llm_claude")
        assert latest is not None
        assert latest["probability"] == 0.72
    finally:
        store.close()
        os.unlink(path)


def test_model_scores():
    store, path = _make_storage()
    try:
        store.record_model_score("llm_claude", "m1", 0.70, 1, 0.09)
        store.record_model_score("llm_claude", "m2", 0.40, 0, 0.16)
        avg = store.get_model_brier("llm_claude")
        assert avg is not None
        assert abs(avg - 0.125) < 0.01
    finally:
        store.close()
        os.unlink(path)


def test_positions():
    store, path = _make_storage()
    try:
        store.save_position("m1", "YES", "t1", 0.50, 10, 5.0, "edge")
        positions = store.get_positions()
        assert len(positions) == 1
        assert positions[0]["market_id"] == "m1"

        store.remove_position("m1", "YES")
        assert len(store.get_positions()) == 0
    finally:
        store.close()
        os.unlink(path)


def test_set_resolution():
    store, path = _make_storage()
    try:
        store.upsert_market("0xabc", "Test?")
        store.set_resolution("0xabc", "YES")
        m = store.get_market("0xabc")
        assert m["resolution"] == "YES"
        assert m["active"] == 0
    finally:
        store.close()
        os.unlink(path)


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
