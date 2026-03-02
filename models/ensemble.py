"""Multi-model weighted ensemble aggregator.

Combines predictions from multiple models using weights that are
updated based on rolling Brier scores. Better-calibrated models
get more weight over time.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from core.types import Market, Outcome, ProbabilityEstimate
from models.base import ProbabilityModel

logger = logging.getLogger(__name__)

# Default equal weight
DEFAULT_WEIGHT = 1.0

# Minimum weight — prevents any model from being fully silenced
MIN_WEIGHT = 0.05


class EnsembleModel(ProbabilityModel):
    """Weighted ensemble of multiple probability models.

    Weights are updated based on each model's rolling Brier score.
    Models with lower Brier scores (better calibration) get higher weight.
    """

    def __init__(self, models: list[ProbabilityModel] = None):
        self.models: list[ProbabilityModel] = models or []
        self._weights: dict[str, float] = {}  # model_name -> weight
        self._brier_scores: dict[str, list[float]] = {}  # model_name -> recent scores

        # Initialize equal weights
        for model in self.models:
            self._weights[model.name] = DEFAULT_WEIGHT

    @property
    def name(self) -> str:
        return "ensemble"

    def add_model(self, model: ProbabilityModel, weight: float = DEFAULT_WEIGHT):
        """Add a model to the ensemble."""
        self.models.append(model)
        self._weights[model.name] = weight

    def remove_model(self, model_name: str):
        """Remove a model by name."""
        self.models = [m for m in self.models if m.name != model_name]
        self._weights.pop(model_name, None)
        self._brier_scores.pop(model_name, None)

    def predict(
        self,
        market: Market,
        outcome: Outcome,
        context: dict = None,
        **kwargs,
    ) -> Optional[ProbabilityEstimate]:
        """Aggregate predictions from all models using weighted average."""
        predictions = []
        total_weight = 0.0

        for model in self.models:
            if not model.supports_market(market):
                continue

            try:
                estimate = model.predict(market, outcome, context, **kwargs)
                if estimate is None:
                    continue

                weight = self._weights.get(model.name, DEFAULT_WEIGHT)
                # Scale weight by model's own confidence
                effective_weight = weight * estimate.confidence

                predictions.append({
                    "model": model.name,
                    "probability": estimate.probability,
                    "confidence": estimate.confidence,
                    "weight": effective_weight,
                    "reasoning": estimate.reasoning,
                })
                total_weight += effective_weight

            except Exception as e:
                logger.error(f"Model {model.name} prediction failed: {e}")
                continue

        if not predictions or total_weight == 0:
            return None

        # Weighted average
        weighted_prob = sum(p["probability"] * p["weight"] for p in predictions) / total_weight
        weighted_prob = max(0.01, min(0.99, weighted_prob))

        # Ensemble confidence: mild penalty for disagreement (20% max reduction)
        avg_confidence = sum(p["confidence"] * p["weight"] for p in predictions) / total_weight
        disagreement = self._measure_disagreement(predictions)
        ensemble_confidence = avg_confidence * (1 - disagreement * 0.2)

        # Build reasoning summary
        model_summaries = []
        for p in predictions:
            model_summaries.append(
                f"{p['model']}: {p['probability']:.1%} (w={p['weight']:.2f})"
            )
        reasoning = f"Ensemble of {len(predictions)} models: {', '.join(model_summaries)}"

        market_price = market.last_price_yes if outcome == Outcome.YES else market.last_price_no

        estimate = ProbabilityEstimate(
            market_id=market.condition_id,
            outcome=outcome,
            probability=weighted_prob,
            confidence=ensemble_confidence,
            reasoning=reasoning,
            model_name=self.name,
            sources=[p["model"] for p in predictions],
        )
        estimate.set_market_price(market_price)

        logger.info(
            f"Ensemble: {market.question[:40]}... -> {outcome.value}={weighted_prob:.1%} "
            f"(conf={ensemble_confidence:.1%}, models={len(predictions)}, "
            f"market={market_price:.1%})"
        )

        return estimate

    def _measure_disagreement(self, predictions: list[dict]) -> float:
        """Measure how much models disagree (0 = perfect agreement, 1 = max disagreement).

        Uses variance of probability estimates.
        """
        if len(predictions) <= 1:
            return 0.0

        probs = [p["probability"] for p in predictions]
        mean = sum(probs) / len(probs)
        variance = sum((p - mean) ** 2 for p in probs) / len(probs)

        # Normalize: max variance for binary is 0.25 (all at 0 and 1)
        return min(1.0, variance / 0.25)

    def update_weights_from_brier(self, model_name: str, brier_score: float):
        """Update a model's weight based on its Brier score.

        Lower Brier = better calibration = higher weight.
        Uses exponential smoothing to prevent volatile weight swings.
        """
        if model_name not in self._brier_scores:
            self._brier_scores[model_name] = []

        self._brier_scores[model_name].append(brier_score)

        # Keep last 50 scores
        if len(self._brier_scores[model_name]) > 50:
            self._brier_scores[model_name] = self._brier_scores[model_name][-50:]

        # Calculate new weight with capped inverse
        avg_brier = sum(self._brier_scores[model_name]) / len(self._brier_scores[model_name])
        # Capped: Perfect=0 -> 10, Random=0.25 -> 3.6, Bad=0.5 -> 1.9
        raw_weight = max(MIN_WEIGHT, 1.0 / (avg_brier + 0.1))

        # Exponential smoothing (alpha=0.3) — prevents wild swings
        old_weight = self._weights.get(model_name, DEFAULT_WEIGHT)
        smoothed = 0.3 * raw_weight + 0.7 * old_weight

        self._weights[model_name] = smoothed

        logger.debug(
            f"Weight update: {model_name} avg_brier={avg_brier:.4f} -> weight={smoothed:.2f}"
        )

    def get_weights(self) -> dict[str, float]:
        """Get current model weights."""
        return dict(self._weights)

    def get_model_stats(self) -> list[dict]:
        """Get stats for each model in the ensemble."""
        stats = []
        for model in self.models:
            scores = self._brier_scores.get(model.name, [])
            avg_brier = sum(scores) / len(scores) if scores else None
            stats.append({
                "model": model.name,
                "weight": self._weights.get(model.name, DEFAULT_WEIGHT),
                "avg_brier": avg_brier,
                "n_scores": len(scores),
            })
        return stats
