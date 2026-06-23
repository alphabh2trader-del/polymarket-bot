"""
Independent probability estimator using Claude.

Sends market context + relevant news to Claude and asks for a calibrated
probability estimate with reasoning. Returns a structured ProbabilityEstimate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import anthropic

from src.utils.logger import get_logger

log = get_logger(__name__)

SYSTEM_PROMPT = """You are a quantitative prediction market analyst. Your job is to estimate
the probability that a binary market question resolves YES, based on available evidence.

Rules:
- Be calibrated: 50% means genuine uncertainty, not a default.
- Anchor on base rates, not just recent news.
- Distinguish between what is likely and what is merely possible.
- Be explicit about your uncertainty.
- Output ONLY valid JSON, no other text."""

ESTIMATION_PROMPT = """Analyze the following prediction market and estimate the probability it resolves YES.

MARKET QUESTION:
{question}

RESOLUTION CRITERIA:
{description}

CURRENT MARKET PRICE (implied probability):
YES: {yes_price:.1%}  |  NO: {no_price:.1%}

RESOLUTION DATE: {resolution_date}

RELEVANT NEWS (last 7 days):
{news_text}

OUTPUT FORMAT (JSON only):
{{
  "probability": <float between 0 and 1>,
  "confidence": "<low|medium|high>",
  "reasoning": "<2-3 sentence explanation of key factors>",
  "key_factors": ["<factor 1>", "<factor 2>", "<factor 3>"],
  "risks_to_estimate": ["<risk 1>", "<risk 2>"],
  "base_rate_notes": "<brief base rate consideration>"
}}"""


@dataclass
class ProbabilityEstimate:
    probability: float
    confidence: str          # low / medium / high
    reasoning: str
    key_factors: list[str]
    risks: list[str]
    base_rate_notes: str
    raw_response: str = ""

    @property
    def confidence_weight(self) -> float:
        return {"low": 0.5, "medium": 0.75, "high": 1.0}.get(self.confidence, 0.5)


class ProbabilityEstimator:
    def __init__(self, api_key: str, model: str = "claude-sonnet-4-6"):
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model

    def estimate(
        self,
        question: str,
        description: str,
        yes_price: float,
        no_price: float,
        resolution_date: str,
        news_text: str,
    ) -> ProbabilityEstimate:
        prompt = ESTIMATION_PROMPT.format(
            question=question,
            description=description or "No additional description provided.",
            yes_price=yes_price,
            no_price=no_price,
            resolution_date=resolution_date,
            news_text=news_text or "No recent news found.",
        )

        try:
            message = self.client.messages.create(
                model=self.model,
                max_tokens=800,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = message.content[0].text.strip()
            return self._parse_response(raw)
        except anthropic.APIError as exc:
            log.error(f"Claude API error during probability estimation: {exc}")
            return self._fallback_estimate(yes_price)
        except Exception as exc:
            log.error(f"Unexpected error in probability estimation: {exc}")
            return self._fallback_estimate(yes_price)

    def _parse_response(self, raw: str) -> ProbabilityEstimate:
        # Strip markdown code fences if present
        text = raw
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]

        data = json.loads(text.strip())

        prob = float(data.get("probability", 0.5))
        prob = max(0.01, min(0.99, prob))  # clamp to valid range

        return ProbabilityEstimate(
            probability=prob,
            confidence=data.get("confidence", "medium"),
            reasoning=data.get("reasoning", ""),
            key_factors=data.get("key_factors", []),
            risks=data.get("risks_to_estimate", []),
            base_rate_notes=data.get("base_rate_notes", ""),
            raw_response=raw,
        )

    @staticmethod
    def _fallback_estimate(market_price: float) -> ProbabilityEstimate:
        """Return the market price as estimate when Claude is unavailable."""
        return ProbabilityEstimate(
            probability=market_price,
            confidence="low",
            reasoning="Claude API unavailable; using market price as fallback estimate.",
            key_factors=[],
            risks=["LLM estimation unavailable"],
            base_rate_notes="",
        )
