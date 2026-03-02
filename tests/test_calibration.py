"""Tests for calibration tracking."""

import sys
import os
import tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.calibration import brier_score, log_loss, CalibrationTracker
from data.storage import Storage


def test_brier_score_perfect():
    """Predicted 1.0, outcome 1 -> Brier = 0."""
    assert brier_score(1.0, 1) == 0.0
    assert brier_score(0.0, 0) == 0.0


def test_brier_score_worst():
    """Predicted 1.0, outcome 0 -> Brier = 1."""
    assert brier_score(1.0, 0) == 1.0
    assert brier_score(0.0, 1) == 1.0


def test_brier_score_uncertain():
    """Predicted 0.5 -> Brier = 0.25 regardless of outcome."""
    assert brier_score(0.5, 1) == 0.25
    assert brier_score(0.5, 0) == 0.25


def test_brier_score_partial():
    """Predicted 0.7, outcome 1 -> Brier = 0.09."""
    bs = brier_score(0.7, 1)
    assert abs(bs - 0.09) < 0.001


def test_log_loss_confident_correct():
    """Confident and correct -> low loss."""
    ll = log_loss(0.99, 1)
    assert ll < 0.02


def test_log_loss_confident_wrong():
    """Confident and wrong -> high loss."""
    ll = log_loss(0.99, 0)
    assert ll > 4.0


def test_log_loss_uncertain():
    """Uncertain -> moderate loss."""
    ll = log_loss(0.5, 1)
    assert abs(ll - 0.693) < 0.01  # ln(2)


def test_tracker_record_and_score():
    tracker = CalibrationTracker()
    tracker.record_prediction("model_a", "market_1", 0.80)
    tracker.record_prediction("model_b", "market_1", 0.30)

    scores = tracker.score_resolution("market_1", actual_outcome=1)
    assert "model_a" in scores
    assert "model_b" in scores
    # model_a: (0.8-1)^2 = 0.04, model_b: (0.3-1)^2 = 0.49
    assert abs(scores["model_a"] - 0.04) < 0.001
    assert abs(scores["model_b"] - 0.49) < 0.001


def test_tracker_pending_cleared():
    tracker = CalibrationTracker()
    tracker.record_prediction("model_a", "market_1", 0.80)
    assert tracker.pending_count == 1

    tracker.score_resolution("market_1", 1)
    assert tracker.pending_count == 0


def test_tracker_with_storage():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    storage = Storage(db_path=path)
    tracker = CalibrationTracker(storage=storage)

    try:
        tracker.record_prediction("model_a", "market_1", 0.70)
        tracker.score_resolution("market_1", 1)

        # Check it was stored
        avg = storage.get_model_brier("model_a", days=30)
        assert avg is not None
        assert abs(avg - 0.09) < 0.001
    finally:
        storage.close()
        os.unlink(path)


def test_tracker_multiple_predictions_uses_latest():
    tracker = CalibrationTracker()
    tracker.record_prediction("model_a", "market_1", 0.40)
    tracker.record_prediction("model_a", "market_1", 0.80)  # Updated prediction

    scores = tracker.score_resolution("market_1", 1)
    # Should use 0.80 (latest): (0.8-1)^2 = 0.04
    assert abs(scores["model_a"] - 0.04) < 0.001


def test_tracker_summary():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    storage = Storage(db_path=path)
    tracker = CalibrationTracker(storage=storage)

    try:
        tracker.record_prediction("model_a", "m1", 0.70)
        tracker.record_prediction("model_a", "m2", 0.90)
        tracker.score_resolution("m1", 1)
        tracker.score_resolution("m2", 1)

        summary = tracker.get_summary("model_a")
        assert summary["model"] == "model_a"
        assert summary["n_predictions"] == 2
        assert summary["avg_brier"] is not None
    finally:
        storage.close()
        os.unlink(path)


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
