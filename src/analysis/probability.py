"""
Independent probability estimator using Claude.

Sends market context + relevant news to Claude and asks for a calibrated
probability estimate with reasoning. Returns a structured ProbabilityEstimate.
"""

from __future__ import annotations

import json
import time
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
    web_searches: int = 0    # how many live web searches Claude actually ran (for cost accounting)

    @property
    def confidence_weight(self) -> float:
        return {"low": 0.5, "medium": 0.75, "high": 1.0}.get(self.confidence, 0.5)


class ProbabilityEstimator:
    def __init__(self, api_key: str, model: str = "claude-sonnet-4-6", on_api_error=None):
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model
        # Optional callback(service: str, reason: str) fired when Claude rejects
        # the key (401) or denies permission (403 — often an exhausted credit
        # balance). Used to send a Telegram alert.
        self.on_api_error = on_api_error

    def _notify_api_error(self, exc) -> None:
        if self.on_api_error is None:
            return
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status == 401:
            reason = "API key rejected (401) — likely expired or revoked"
        elif status == 403:
            reason = ("permission denied (403) — often means the credit balance is "
                      "exhausted or the key is disabled")
        else:
            reason = f"authentication problem ({status})"
        try:
            self.on_api_error("Anthropic Claude", reason)
        except Exception as exc2:
            log.debug(f"API-error alert callback failed: {exc2}")

    def estimate(
        self,
        question: str,
        description: str,
        yes_price: float,
        no_price: float,
        resolution_date: str,
        news_text: str,
        allow_web_search: bool = False,
        web_search_max_uses: int = 2,
    ) -> ProbabilityEstimate:
        # Master pause backstop: refuse to touch the API while paused, from ANY
        # caller (scan OR thesis re-check). Returns the market price as a no-cost
        # fallback estimate. This is the belt to run_scan's suspenders.
        from config.settings import settings as _settings
        if _settings.paused:
            return self._fallback_estimate(yes_price)

        prompt = ESTIMATION_PROMPT.format(
            question=question,
            description=description or "No additional description provided.",
            yes_price=yes_price,
            no_price=no_price,
            resolution_date=resolution_date,
            news_text=news_text or "No recent news found.",
        )

        # When allowed (news came back thin and we're under the daily budget),
        # give Claude the Anthropic server-side web_search tool so he can look up
        # the specific market himself instead of relying on pre-fetched headlines.
        # It's a server tool — Anthropic runs the searches and returns the final
        # answer in one call, so the text-extraction below is unchanged.
        tools = None
        if allow_web_search:
            tools = [{
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": max(1, web_search_max_uses),
            }]

        for attempt in range(3):
            try:
                kwargs = dict(
                    model=self.model,
                    # COST CONTROL: Sonnet 5 runs adaptive thinking by DEFAULT,
                    # and thinking tokens (billed at the $10/1M output rate) are
                    # what made this expensive — a single estimate could emit
                    # thousands of thinking tokens. This task is just "read news,
                    # output one JSON probability"; it does not need extended
                    # reasoning. Disabling thinking cuts output tokens ~80% and is
                    # the main lever keeping the daily bill low. max_tokens is now
                    # a tight ceiling on the bare JSON answer.
                    max_tokens=1200,
                    thinking={"type": "disabled"},
                    system=SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": prompt}],
                    timeout=90.0,
                )
                if tools is not None:
                    kwargs["tools"] = tools
                try:
                    message = self.client.messages.create(**kwargs)
                except anthropic.BadRequestError as exc:
                    # Tool unsupported/misconfigured on this account or model —
                    # don't lose the estimate over it. Retry once WITHOUT the
                    # tool; a plain estimate still beats falling back to price.
                    if tools is not None:
                        log.warning(f"Web search rejected ({exc}); retrying without it")
                        tools = None
                        kwargs.pop("tools", None)
                        message = self.client.messages.create(**kwargs)
                    else:
                        raise
                # content may start with a thinking block on Sonnet 5, and (with
                # search) server_tool_use / web_search_result blocks — take the
                # text block, wherever it is, not content[0].
                raw = next(
                    (b.text for b in message.content if b.type == "text"), ""
                ).strip()
                searches_used = self._count_searches(message)
                if message.stop_reason == "max_tokens" and not raw:
                    # Adaptive thinking used the whole budget before any text
                    # block was written. Log this distinctly from a parse
                    # failure below — same failure signature (empty estimate,
                    # falls back to market price -> no opportunity found), but
                    # a different cause, and worth knowing which one recurs.
                    log.warning(
                        f"Claude hit max_tokens before producing an answer "
                        f"(attempt {attempt+1}/3) — thinking used the full "
                        f"4000-token budget with no output"
                    )
                return self._parse_response(raw, web_searches=searches_used)
            except anthropic.RateLimitError:
                wait = 20 * (attempt + 1)
                log.warning(f"Claude rate limited — waiting {wait}s (attempt {attempt+1}/3)")
                time.sleep(wait)
            except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
                # 401 = key expired/revoked; 403 = credit exhausted or key disabled.
                # This is the "key/subscription is finishing" signal — alert, then
                # stop retrying (a bad key won't recover in 5s) and use the fallback.
                log.error(f"Claude auth/permission error: {exc}")
                self._notify_api_error(exc)
                break
            except anthropic.APIError as exc:
                # Credit-exhausted comes back as a 400 invalid_request_error, NOT a
                # 401/403 — so it never reached _notify_api_error and the balance
                # could drain to zero with no Telegram warning. Detect it here,
                # alert once, and stop retrying (more attempts can't help).
                msg = str(getattr(exc, "message", "") or exc).lower()
                if "credit balance" in msg or "too low" in msg or "billing" in msg:
                    log.error(f"Claude credit balance exhausted: {exc}")
                    if self.on_api_error is not None:
                        try:
                            self.on_api_error(
                                "Anthropic Claude",
                                "credit balance exhausted — the bot has stopped "
                                "thinking; add funds to resume",
                            )
                        except Exception as exc2:
                            log.debug(f"Billing alert failed: {exc2}")
                    break
                log.error(f"Claude API error (attempt {attempt+1}/3): {exc}")
                if attempt < 2:
                    time.sleep(5)
            except Exception as exc:
                log.error(f"Unexpected error in probability estimation: {exc}")
                if attempt < 2:
                    time.sleep(5)
        return self._fallback_estimate(yes_price)

    @staticmethod
    def _count_searches(message) -> int:
        """Actual number of live web searches Anthropic ran for this message, read
        from usage.server_tool_use.web_search_requests. Defensive: returns 0 if the
        field is absent (no tool used, or an older SDK that doesn't expose it)."""
        try:
            stu = getattr(message.usage, "server_tool_use", None)
            if stu is None:
                return 0
            return int(getattr(stu, "web_search_requests", 0) or 0)
        except Exception:
            return 0

    def _parse_response(self, raw: str, web_searches: int = 0) -> ProbabilityEstimate:
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
            web_searches=web_searches,
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
