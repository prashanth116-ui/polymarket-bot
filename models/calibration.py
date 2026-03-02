"""Calibration tracking — Brier scores, calibration curves, model evaluation."""

import logging
import math
from datetime import datetime, timezone
from typing import Optional

from data.storage import Storage

logger = logging.getLogger(__name__)


def brier_score(predicted_prob: float, actual_outcome: int) -> float:
    """Calculate Brier score for a single prediction.

    Brier score = (predicted_prob - actual_outcome)^2
    Range: 0 (perfect) to 1 (worst possible)
    Reference: random guessing on balanced binary = 0.25

    Args:
        predicted_prob: Model's predicted probability (0-1)
        actual_outcome: 1 if event occurred, 0 if not

    Returns:
        Brier score (lower is better)
    """
    return (predicted_prob - actual_outcome) ** 2


def log_loss(predicted_prob: float, actual_outcome: int, eps: float = 1e-7) -> float:
    """Calculate log loss (cross-entropy) for a single prediction.

    More harshly penalizes confident wrong predictions than Brier score.

    Args:
        predicted_prob: Model's predicted probability (0-1)
        actual_outcome: 1 if event occurred, 0 if not
        eps: Small constant to avoid log(0)

    Returns:
        Log loss (lower is better)
    """
    p = max(eps, min(1 - eps, predicted_prob))
    if actual_outcome == 1:
        return -math.log(p)
    return -math.log(1 - p)


class CalibrationTracker:
    """Tracks model calibration and Brier scores over time.

    Records predictions and outcomes, computes calibration curves,
    and stores results in SQLite for analysis.
    """

    def __init__(self, storage: Storage = None):
        self.storage = storage
        self._pending: dict[str, list[dict]] = {}  # model_name -> list of {market_id, predicted_prob}

    def record_prediction(self, model_name: str, market_id: str, predicted_prob: float):
        """Record a prediction for later scoring when the market resolves."""
        if model_name not in self._pending:
            self._pending[model_name] = []

        self._pending[model_name].append({
            "market_id": market_id,
            "predicted_prob": predicted_prob,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    def score_resolution(self, market_id: str, actual_outcome: int) -> dict[str, float]:
        """Score all pending predictions for a resolved market.

        Args:
            market_id: The resolved market ID
            actual_outcome: 1 if YES won, 0 if NO won

        Returns:
            Dict mapping model_name -> brier_score
        """
        scores = {}

        for model_name, predictions in self._pending.items():
            matching = [p for p in predictions if p["market_id"] == market_id]
            if not matching:
                continue

            # Use the most recent prediction
            pred = matching[-1]
            bs = brier_score(pred["predicted_prob"], actual_outcome)
            scores[model_name] = bs

            # Store in database
            if self.storage:
                self.storage.record_model_score(
                    model_name=model_name,
                    market_id=market_id,
                    predicted_prob=pred["predicted_prob"],
                    actual_outcome=actual_outcome,
                    brier_score=bs,
                )

            logger.info(
                f"Scored {model_name} on {market_id[:20]}...: "
                f"predicted={pred['predicted_prob']:.1%}, "
                f"actual={actual_outcome}, "
                f"brier={bs:.4f}"
            )

            # Remove scored predictions
            self._pending[model_name] = [
                p for p in predictions if p["market_id"] != market_id
            ]

        return scores

    def get_model_brier(self, model_name: str, days: int = 30) -> Optional[float]:
        """Get average Brier score for a model over recent history."""
        if self.storage:
            return self.storage.get_model_brier(model_name, days=days)
        return None

    def get_calibration_curve(self, model_name: str, n_bins: int = 10, days: int = 90) -> list[dict]:
        """Compute calibration curve — predicted vs actual frequency.

        A perfectly calibrated model has predicted_prob == actual_frequency
        for each bin.

        Returns:
            List of {bin_center, predicted_avg, actual_frequency, count}
        """
        if not self.storage:
            return []

        scores = self.storage.get_model_scores(model_name, days=days)
        if not scores:
            return []

        # Bin predictions
        bin_width = 1.0 / n_bins
        bins = [{"predicted_sum": 0, "actual_sum": 0, "count": 0} for _ in range(n_bins)]

        for score in scores:
            pred = score["predicted_prob"]
            actual = score["actual_outcome"]
            bin_idx = min(int(pred / bin_width), n_bins - 1)
            bins[bin_idx]["predicted_sum"] += pred
            bins[bin_idx]["actual_sum"] += actual
            bins[bin_idx]["count"] += 1

        curve = []
        for i, b in enumerate(bins):
            if b["count"] == 0:
                continue
            curve.append({
                "bin_center": (i + 0.5) * bin_width,
                "predicted_avg": b["predicted_sum"] / b["count"],
                "actual_frequency": b["actual_sum"] / b["count"],
                "count": b["count"],
            })

        return curve

    def get_summary(self, model_name: str, days: int = 30) -> dict:
        """Get calibration summary for a model."""
        avg_brier = self.get_model_brier(model_name, days=days)
        curve = self.get_calibration_curve(model_name, days=days)

        # Calculate calibration error (mean absolute deviation from diagonal)
        cal_error = 0.0
        total_count = 0
        if curve:
            for point in curve:
                cal_error += abs(point["predicted_avg"] - point["actual_frequency"]) * point["count"]
                total_count += point["count"]
            if total_count > 0:
                cal_error /= total_count

        n_predictions = 0
        if self.storage:
            scores = self.storage.get_model_scores(model_name, days=days)
            n_predictions = len(scores)

        return {
            "model": model_name,
            "avg_brier": avg_brier,
            "calibration_error": cal_error if curve else None,
            "n_predictions": n_predictions,
            "n_calibration_bins": len(curve),
            "days": days,
        }

    @property
    def pending_count(self) -> int:
        return sum(len(preds) for preds in self._pending.values())
