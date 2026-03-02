"""Tests for bridge executor retry logic."""

import sys
import os
import time
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
from execution.bridge_executor import BridgeExecutor


def _mock_response(status_code=200, json_data=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    resp.text = str(json_data or {})
    if status_code >= 400:
        resp.raise_for_status.side_effect = requests.exceptions.HTTPError(
            response=resp
        )
    else:
        resp.raise_for_status.return_value = None
    return resp


def test_successful_request_no_retry():
    executor = BridgeExecutor(max_retries=2, retry_delay=0.01)
    with patch("requests.get", return_value=_mock_response(200, {"ok": True})) as mock_get:
        result = executor._request("get", "/health")
        assert result == {"ok": True}
        assert mock_get.call_count == 1


def test_retry_on_connection_error():
    executor = BridgeExecutor(max_retries=2, retry_delay=0.01)
    with patch("requests.get") as mock_get:
        mock_get.side_effect = [
            requests.exceptions.ConnectionError("refused"),
            requests.exceptions.ConnectionError("refused"),
            _mock_response(200, {"ok": True}),
        ]
        result = executor._request("get", "/health")
        assert result == {"ok": True}
        assert mock_get.call_count == 3


def test_retry_on_timeout():
    executor = BridgeExecutor(max_retries=1, retry_delay=0.01)
    with patch("requests.get") as mock_get:
        mock_get.side_effect = [
            requests.exceptions.Timeout("timed out"),
            _mock_response(200, {"ok": True}),
        ]
        result = executor._request("get", "/health")
        assert result == {"ok": True}
        assert mock_get.call_count == 2


def test_retry_on_500():
    executor = BridgeExecutor(max_retries=1, retry_delay=0.01)
    with patch("requests.post") as mock_post:
        mock_post.side_effect = [
            _mock_response(500, {"error": "internal"}),
            _mock_response(200, {"order_id": "abc"}),
        ]
        result = executor._request("post", "/order", json={"side": "BUY"})
        assert result == {"order_id": "abc"}
        assert mock_post.call_count == 2


def test_no_retry_on_400():
    executor = BridgeExecutor(max_retries=2, retry_delay=0.01)
    with patch("requests.post") as mock_post:
        mock_post.return_value = _mock_response(400, {"error": "bad request"})
        try:
            executor._request("post", "/order", json={"bad": "data"})
            assert False, "Should have raised HTTPError"
        except requests.exceptions.HTTPError:
            pass
        # Should NOT retry on 4xx
        assert mock_post.call_count == 1


def test_no_retry_on_401():
    executor = BridgeExecutor(max_retries=2, retry_delay=0.01)
    with patch("requests.get") as mock_get:
        mock_get.return_value = _mock_response(401, {"error": "unauthorized"})
        try:
            executor._request("get", "/positions")
            assert False, "Should have raised HTTPError"
        except requests.exceptions.HTTPError:
            pass
        assert mock_get.call_count == 1


def test_exhausted_retries_raises():
    executor = BridgeExecutor(max_retries=2, retry_delay=0.01)
    with patch("requests.get") as mock_get:
        mock_get.side_effect = requests.exceptions.ConnectionError("refused")
        try:
            executor._request("get", "/health")
            assert False, "Should have raised ConnectionError"
        except requests.exceptions.ConnectionError:
            pass
        # 1 initial + 2 retries = 3 total
        assert mock_get.call_count == 3


def test_health_ok_caches():
    executor = BridgeExecutor(max_retries=0, retry_delay=0.01)
    executor._health_check_interval = 0.1

    with patch("requests.get", return_value=_mock_response(200, {"ok": True})):
        assert executor.health_ok is True

    # Should use cached value (no new request)
    with patch("requests.get", side_effect=requests.exceptions.ConnectionError("refused")):
        assert executor.health_ok is True  # Still cached

    # Wait for cache to expire
    time.sleep(0.15)
    with patch("requests.get", side_effect=requests.exceptions.ConnectionError("refused")):
        assert executor.health_ok is False


def test_health_ok_false_on_error():
    executor = BridgeExecutor(max_retries=0, retry_delay=0.01)
    with patch("requests.get", side_effect=requests.exceptions.ConnectionError("refused")):
        assert executor.health_ok is False


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
