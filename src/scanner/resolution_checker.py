"""
Position tracker for the trade-the-price strategy.

Every scan it walks each open paper position and decides whether to close it:
  - current price >= target (Claude's estimate)  -> WIN  (take profit)
  - current price <= symmetric stop              -> LOSS (cut loss)
  - market resolved before either was hit        -> settle on the final outcome
  - otherwise                                    -> stay open, refresh current price

"Current price" is the live price of the side we hold (YES price, or 1-YES for NO).
Resolution detection reuses _resolve() (verified against the Polymarket Gamma API).
"""
from __future__ import annotations

import json as _json
import time
from datetime import datetime, timezone

from src.api.polymarket import PolymarketClient
from src.database.db import get_session
from src.database.models import Prediction
from src.notifications.telegram import TelegramNotifier
from src.utils.logger import get_logger

log = get_logger(__name__)

_MAX_PER_RUN = 50
_GAMMA_HOST = "https://gamma-api.polymarket.com"


class ResolutionChecker:
    def __init__(self, poly: PolymarketClient, notifier: TelegramNotifier):
        self.poly = poly
        self.notifier = notifier

    def check_pending(self) -> tuple[int, int]:
        """
        Check up to _MAX_PER_RUN open positions. Returns (wins_closed, losses_closed).
        """
        with get_session() as session:
            rows = (
                session.query(Prediction)
                .filter_by(outcome="PENDING")
                .order_by(Prediction.created_at)
                .limit(_MAX_PER_RUN)
                .all()
            )
            pending = [
                {
                    "id": p.id,
                    "condition_id": p.condition_id,
                    "question": p.question,
                    "side": p.predicted_side,
                    "entry": p.implied_prob,
                    "target": p.predicted_prob,
                    "stop": p.stop_price,
                    "ev": p.ev,
                    "confidence": p.confidence,
                }
                for p in rows
            ]

        if not pending:
            return 0, 0

        wins = losses = 0

        for p in pending:
            current = self._current_side_price(p["condition_id"], p["side"])

            outcome = None
            exit_price = None
            exit_reason = None

            if current is not None:
                if current >= p["target"]:
                    outcome, exit_price, exit_reason = "WIN", current, "TARGET_HIT"
                elif current <= p["stop"]:
                    outcome, exit_price, exit_reason = "LOSS", current, "STOP_LOSS"
            else:
                # Not in the active set -> market is likely closed; settle on outcome.
                resolution = self._resolve(p["condition_id"])
                if resolution is not None:
                    if resolution == p["side"]:
                        outcome, exit_price, exit_reason = "WIN", 1.0, "RESOLVED"
                    else:
                        outcome, exit_price, exit_reason = "LOSS", 0.0, "RESOLVED"

            with get_session() as session:
                pred = session.get(Prediction, p["id"])
                if not pred:
                    continue
                if current is not None:
                    pred.current_price = round(current, 4)
                if outcome:
                    pred.outcome = outcome
                    pred.exit_price = round(exit_price, 4)
                    pred.exit_reason = exit_reason
                    pred.resolved_at = datetime.now(timezone.utc)
                    if exit_reason == "RESOLVED":
                        pred.resolution_value = p["side"] if outcome == "WIN" else (
                            "NO" if p["side"] == "YES" else "YES"
                        )

            if outcome:
                ret = (exit_price - p["entry"]) / p["entry"] if p["entry"] else 0.0
                self.notifier.send_outcome(
                    question=p["question"],
                    predicted_side=p["side"],
                    outcome=outcome,
                    entry_price=p["entry"],
                    exit_price=exit_price,
                    return_pct=ret,
                    exit_reason=exit_reason,
                    confidence=p["confidence"],
                )
                if outcome == "WIN":
                    wins += 1
                else:
                    losses += 1
                log.info(
                    f"{outcome} [{exit_reason}]: {p['question'][:55]} | "
                    f"entry={p['entry']:.2f} exit={exit_price:.2f} ret={ret:+.1%}"
                )
            time.sleep(0.2)

        if wins or losses:
            log.info(f"Position sweep closed: {wins}W / {losses}L")
        return wins, losses

    # ------------------------------------------------------------------ #
    # Live price + resolution helpers                                      #
    # ------------------------------------------------------------------ #

    def _current_side_price(self, condition_id: str, side: str) -> float | None:
        """
        Return the live price (0-1) of the side we hold, or None if the market
        is no longer active (closed/resolved -> caller should settle instead).
        Queries /markets?condition_ids=<cid> which only returns active markets.
        """
        try:
            data = self.poly._get_gamma("/markets", params={"condition_ids": condition_id})
            if not data:
                return None
            market = data[0] if isinstance(data, list) else data
            if not market or market.get("closed", False):
                return None
            raw = market.get("outcomePrices", "")
            prices = _json.loads(raw) if isinstance(raw, str) else raw
            if not prices:
                return None
            yes_price = float(prices[0])
            return yes_price if side == "YES" else round(1.0 - yes_price, 4)
        except Exception as exc:
            log.debug(f"Cannot fetch current price for {condition_id}: {exc}")
            return None

    def _resolve(self, condition_id: str) -> str | None:
        """
        Returns 'YES', 'NO', or None if the market has not yet resolved.

        Polymarket Gamma API: query /markets?condition_ids=<cid>&closed=true (the
        /markets/<id> path needs the numeric id, not the condition id). A market
        is resolved when closed == True and umaResolutionStatus == 'resolved'.
        The winning side is read from outcomePrices: index 0 is YES; the side
        priced at ~1 won.
        """
        try:
            data = self.poly._get_gamma(
                "/markets",
                params={"condition_ids": condition_id, "closed": "true"},
            )
            if not data:
                return None
            market = data[0] if isinstance(data, list) else data
            if not market:
                return None

            closed = bool(market.get("closed", False))
            uma = str(market.get("umaResolutionStatus", "")).lower()
            if not closed or uma != "resolved":
                return None

            raw_prices = market.get("outcomePrices", "")
            if isinstance(raw_prices, str):
                prices = _json.loads(raw_prices)
            elif isinstance(raw_prices, list):
                prices = raw_prices
            else:
                return None

            if len(prices) < 2:
                return None

            yes_price = float(prices[0])
            no_price = float(prices[1])
            if yes_price >= 0.99:
                return "YES"
            if no_price >= 0.99:
                return "NO"
            log.warning(f"Resolved market {condition_id} has ambiguous prices {prices}")
            return None
        except Exception as exc:
            log.debug(f"Cannot check resolution for {condition_id}: {exc}")
            return None
