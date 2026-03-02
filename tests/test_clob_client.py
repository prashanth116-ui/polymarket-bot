"""Tests for CLOB REST client."""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest.mock import patch, MagicMock
from data.clob_client import ClobReader


def test_parse_order_book():
    """Test order book parsing and sorting."""
    reader = ClobReader()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "bids": [
            {"price": "0.50", "size": "100"},
            {"price": "0.55", "size": "50"},
            {"price": "0.45", "size": "200"},
        ],
        "asks": [
            {"price": "0.65", "size": "75"},
            {"price": "0.60", "size": "120"},
            {"price": "0.70", "size": "30"},
        ],
    }
    mock_response.raise_for_status = MagicMock()

    with patch("requests.get", return_value=mock_response):
        book = reader.get_order_book("test_token")

    assert book is not None
    # Bids sorted descending
    assert book.bids[0].price == 0.55
    assert book.bids[1].price == 0.50
    assert book.bids[2].price == 0.45
    # Asks sorted ascending
    assert book.asks[0].price == 0.60
    assert book.asks[1].price == 0.65
    assert book.asks[2].price == 0.70
    # Properties
    assert book.best_bid == 0.55
    assert book.best_ask == 0.60
    assert abs(book.spread - 0.05) < 0.001
    assert abs(book.midpoint - 0.575) < 0.001


def test_get_midpoint():
    reader = ClobReader()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"mid": "0.65"}
    mock_response.raise_for_status = MagicMock()

    with patch("requests.get", return_value=mock_response):
        mid = reader.get_midpoint("test_token")

    assert mid == 0.65


def test_get_price():
    reader = ClobReader()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"price": "0.62"}
    mock_response.raise_for_status = MagicMock()

    with patch("requests.get", return_value=mock_response):
        price = reader.get_price("test_token", side="buy")

    assert price == 0.62


def test_get_book_summary():
    reader = ClobReader()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "bids": [
            {"price": "0.60", "size": "100"},
            {"price": "0.59", "size": "200"},
        ],
        "asks": [
            {"price": "0.62", "size": "150"},
            {"price": "0.63", "size": "50"},
        ],
    }
    mock_response.raise_for_status = MagicMock()

    with patch("requests.get", return_value=mock_response):
        summary = reader.get_book_summary("test_token", depth=5)

    assert summary["best_bid"] == 0.60
    assert summary["best_ask"] == 0.62
    assert abs(summary["spread"] - 0.02) < 0.001
    assert summary["bid_depth"] == 300
    assert summary["ask_depth"] == 200


def test_connection_error_returns_none():
    reader = ClobReader()
    with patch("requests.get", side_effect=Exception("connection failed")):
        assert reader.get_midpoint("test") is None
        assert reader.get_order_book("test") is None


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
