"""LLM-based probability estimation using Claude and GPT-4.

Two-tier approach:
  1. Screening tier (cheap/fast model): Quick estimate to filter out markets with no edge
  2. Final tier (expensive model): Detailed analysis for markets that pass screening
Results cached for 1 hour to control API costs.
"""

import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Optional

from core.types import Market, Outcome, ProbabilityEstimate
from models.base import ProbabilityModel

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an expert prediction market forecaster. Your job is to estimate
the probability of specific outcomes in prediction markets.

You must be well-calibrated: when you say 70%, events should happen about 70% of the time.
Consider base rates, available evidence, and your uncertainty.

IMPORTANT: Be specific about your probability estimate. Do NOT default to 50% unless you
genuinely have no information. Markets already price in obvious information — your value
comes from finding mispricings through careful analysis."""

PREDICTION_PROMPT_TEMPLATE = """Analyze this prediction market and estimate the probability of the specified outcome.

MARKET QUESTION: {question}

MARKET DESCRIPTION:
{description}

CURRENT MARKET PRICE: {market_price:.1%} (this is what the market currently thinks)

OUTCOME TO EVALUATE: {outcome}

RESOLUTION DATE: {end_date}

{context_section}

Instructions:
1. Consider all available evidence, base rates, and historical precedents
2. Think about what information the market might be missing or overweighting
3. Be specific — avoid anchoring to the current market price unless you agree with it

Respond in this exact JSON format:
{{
  "probability": <float between 0.01 and 0.99>,
  "confidence": <float between 0.0 and 1.0 — how confident you are in your estimate>,
  "reasoning": "<2-3 sentence explanation of your key reasoning>",
  "key_factors": ["<factor 1>", "<factor 2>", "<factor 3>"],
  "edge_direction": "<OVER if you think market underprices, UNDER if overprices, FAIR if roughly correct>"
}}"""


def _format_context(context: dict) -> str:
    """Format context data into a readable section for the prompt."""
    if not context:
        return ""

    sections = []

    news = context.get("news", [])
    if news:
        headlines = []
        for article in news[:5]:
            title = article.title if hasattr(article, "title") else article.get("title", "")
            if title:
                headlines.append(f"  - {title}")
        if headlines:
            sections.append("RECENT NEWS:\n" + "\n".join(headlines))

    polls = context.get("polls")
    if polls:
        sections.append(f"POLLING DATA:\n  {polls}")

    economic = context.get("economic", {})
    if economic:
        items = []
        for key, val in economic.items():
            items.append(f"  - {key}: {val}")
        if items:
            sections.append("ECONOMIC INDICATORS:\n" + "\n".join(items))

    custom = context.get("additional_context", "")
    if custom:
        sections.append(f"ADDITIONAL CONTEXT:\n  {custom}")

    if not sections:
        return ""

    return "CONTEXT DATA:\n" + "\n\n".join(sections)


def _parse_llm_response(text: str) -> Optional[dict]:
    """Extract structured prediction from LLM response text."""
    # Try to find JSON block
    json_match = re.search(r'\{[^{}]*"probability"[^{}]*\}', text, re.DOTALL)
    if json_match:
        try:
            data = json.loads(json_match.group())
            prob = float(data.get("probability", 0))
            conf = float(data.get("confidence", 0.5))
            reasoning = data.get("reasoning", "")
            if 0 < prob < 1:
                return {
                    "probability": prob,
                    "confidence": max(0.0, min(1.0, conf)),
                    "reasoning": reasoning,
                    "key_factors": data.get("key_factors", []),
                    "edge_direction": data.get("edge_direction", "FAIR"),
                }
        except (json.JSONDecodeError, ValueError, TypeError):
            pass

    # Fallback: try to find probability in text
    prob_match = re.search(r'probability["\s:]+([0-9]*\.?[0-9]+)', text.lower())
    if prob_match:
        prob = float(prob_match.group(1))
        if prob > 1:
            prob = prob / 100  # Handle percentage format
        if 0 < prob < 1:
            return {
                "probability": prob,
                "confidence": 0.5,
                "reasoning": "Extracted from unstructured response",
                "key_factors": [],
                "edge_direction": "FAIR",
            }

    logger.warning("Could not parse LLM response")
    return None


class LLMForecaster(ProbabilityModel):
    """LLM-based probability forecaster with two-tier caching.

    Tier 1 (screening): Cheap model (Haiku/GPT-4o-mini) for initial filtering
    Tier 2 (final): Expensive model (Sonnet/GPT-4) for detailed estimation
    """

    def __init__(
        self,
        provider: str = "anthropic",
        screening_model: str = "claude-haiku-4-5-20251001",
        final_model: str = "claude-sonnet-4-5-20250929",
        cache_ttl: int = 3600,
        max_daily_cost: float = 5.0,
    ):
        self.provider = provider
        self.screening_model = screening_model
        self.final_model = final_model
        self.cache_ttl = cache_ttl
        self.max_daily_cost = max_daily_cost

        self._cache: dict[str, tuple[float, dict]] = {}  # key -> (timestamp, result)
        self._daily_cost = 0.0
        self._daily_cost_reset: float = 0.0
        self._client = None

    @property
    def name(self) -> str:
        return f"llm_{self.provider}"

    def _get_client(self):
        """Lazy-init the API client."""
        if self._client is not None:
            return self._client

        if self.provider == "anthropic":
            try:
                import anthropic
                from config.loader import get_env
                api_key = get_env("ANTHROPIC_API_KEY")
                if not api_key:
                    logger.warning("ANTHROPIC_API_KEY not set")
                    return None
                self._client = anthropic.Anthropic(api_key=api_key)
            except ImportError:
                logger.error("anthropic package not installed")
                return None
        elif self.provider == "openai":
            try:
                import openai
                from config.loader import get_env
                api_key = get_env("OPENAI_API_KEY")
                if not api_key:
                    logger.warning("OPENAI_API_KEY not set")
                    return None
                self._client = openai.OpenAI(api_key=api_key)
            except ImportError:
                logger.error("openai package not installed")
                return None

        return self._client

    def _cache_key(self, market_id: str, outcome: str, model: str) -> str:
        return f"{market_id}:{outcome}:{model}"

    def _get_cached(self, key: str) -> Optional[dict]:
        if key in self._cache:
            ts, result = self._cache[key]
            if time.time() - ts < self.cache_ttl:
                return result
            del self._cache[key]
        return None

    def _set_cache(self, key: str, result: dict):
        self._cache[key] = (time.time(), result)

    def _check_budget(self, cost: float) -> bool:
        """Check if we're within daily cost budget."""
        now = time.time()
        # Reset daily counter at midnight
        if now - self._daily_cost_reset > 86400:
            self._daily_cost = 0.0
            self._daily_cost_reset = now
        return self._daily_cost + cost <= self.max_daily_cost

    def _call_anthropic(self, prompt: str, model: str) -> Optional[str]:
        client = self._get_client()
        if not client:
            return None

        try:
            response = client.messages.create(
                model=model,
                max_tokens=500,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
            # Estimate cost (rough)
            input_tokens = response.usage.input_tokens
            output_tokens = response.usage.output_tokens
            if "haiku" in model:
                cost = (input_tokens * 0.8 + output_tokens * 4.0) / 1_000_000
            else:
                cost = (input_tokens * 3.0 + output_tokens * 15.0) / 1_000_000
            self._daily_cost += cost
            logger.debug(f"LLM call cost: ${cost:.4f} (daily total: ${self._daily_cost:.4f})")

            return response.content[0].text
        except Exception as e:
            logger.error(f"Anthropic API error: {e}")
            return None

    def _call_openai(self, prompt: str, model: str) -> Optional[str]:
        client = self._get_client()
        if not client:
            return None

        try:
            response = client.chat.completions.create(
                model=model,
                max_tokens=500,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
            )
            # Estimate cost
            usage = response.usage
            if "mini" in model:
                cost = (usage.prompt_tokens * 0.15 + usage.completion_tokens * 0.6) / 1_000_000
            else:
                cost = (usage.prompt_tokens * 2.5 + usage.completion_tokens * 10.0) / 1_000_000
            self._daily_cost += cost

            return response.choices[0].message.content
        except Exception as e:
            logger.error(f"OpenAI API error: {e}")
            return None

    def _call_llm(self, prompt: str, model: str) -> Optional[str]:
        if self.provider == "anthropic":
            return self._call_anthropic(prompt, model)
        elif self.provider == "openai":
            return self._call_openai(prompt, model)
        return None

    def _build_prompt(self, market: Market, outcome: Outcome, context: dict = None) -> str:
        end_date_str = market.end_date.strftime("%Y-%m-%d %H:%M UTC") if market.end_date else "Unknown"
        market_price = market.last_price_yes if outcome == Outcome.YES else market.last_price_no
        context_section = _format_context(context)

        return PREDICTION_PROMPT_TEMPLATE.format(
            question=market.question,
            description=market.description[:1000],
            market_price=market_price,
            outcome=outcome.value,
            end_date=end_date_str,
            context_section=context_section,
        )

    def predict(
        self,
        market: Market,
        outcome: Outcome,
        context: dict = None,
        tier: str = "screening",
    ) -> Optional[ProbabilityEstimate]:
        """Estimate probability using LLM.

        Args:
            market: Market to evaluate
            outcome: YES or NO
            context: News, polls, economic data, etc.
            tier: 'screening' (cheap/fast) or 'final' (expensive/detailed)
        """
        model = self.screening_model if tier == "screening" else self.final_model

        # Check cache
        cache_key = self._cache_key(market.condition_id, outcome.value, model)
        cached = self._get_cached(cache_key)
        if cached:
            logger.debug(f"Cache hit for {market.condition_id[:20]}... ({tier})")
            return ProbabilityEstimate(
                market_id=market.condition_id,
                outcome=outcome,
                probability=cached["probability"],
                confidence=cached["confidence"],
                reasoning=cached["reasoning"],
                model_name=f"{self.name}_{tier}",
                sources=cached.get("key_factors", []),
            )

        # Check budget
        est_cost = 0.005 if tier == "screening" else 0.02
        if not self._check_budget(est_cost):
            logger.warning(f"Daily LLM budget exceeded (${self._daily_cost:.2f}/${self.max_daily_cost:.2f})")
            return None

        # Build prompt and call LLM
        prompt = self._build_prompt(market, outcome, context)
        response_text = self._call_llm(prompt, model)
        if not response_text:
            return None

        # Parse response
        parsed = _parse_llm_response(response_text)
        if not parsed:
            return None

        # Cache result
        self._set_cache(cache_key, parsed)

        estimate = ProbabilityEstimate(
            market_id=market.condition_id,
            outcome=outcome,
            probability=parsed["probability"],
            confidence=parsed["confidence"],
            reasoning=parsed["reasoning"],
            model_name=f"{self.name}_{tier}",
            sources=parsed.get("key_factors", []),
        )

        # Set market price for edge calculation
        market_price = market.last_price_yes if outcome == Outcome.YES else market.last_price_no
        estimate.set_market_price(market_price)

        logger.info(
            f"LLM {tier}: {market.question[:50]}... -> "
            f"{outcome.value}={parsed['probability']:.1%} "
            f"(confidence={parsed['confidence']:.1%}, "
            f"market={market_price:.1%})"
        )

        return estimate

    def screen(self, market: Market, outcome: Outcome, context: dict = None) -> Optional[ProbabilityEstimate]:
        """Quick screening estimate (cheap model)."""
        return self.predict(market, outcome, context, tier="screening")

    def final_estimate(self, market: Market, outcome: Outcome, context: dict = None) -> Optional[ProbabilityEstimate]:
        """Detailed final estimate (expensive model)."""
        return self.predict(market, outcome, context, tier="final")

    def cost_per_call(self) -> float:
        return 0.01  # Average across tiers

    @property
    def daily_cost(self) -> float:
        return self._daily_cost

    @property
    def cache_size(self) -> int:
        return len(self._cache)
