"""
Polls the Polymarket Gamma API to detect when predicted markets have resolved.
Updates Prediction records in the DB and fires Telegram notifications.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from src.api.polymarket import PolymarketClient
from src.database.db import get_session
from src.database.models import Prediction
from src.notifications.telegram import TelegramNotifier
from src.utils.logger import get_logger

log = get_logger(__name__)

_MAX_PER_RUN = 50


class ResolutionChecker:
    def __init__(self, poly: PolymarketClient, notifier: TelegramNotifier):
        self.poly = poly
        self.notifier = notifier

    def check_pending(self) -> tuple[int, int]:
        """
        Check up to _MAX_PER_RUN pending predictions against Polymarket.
        Returns (wins_found, losses_found).
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
                (p.id, p.condition_id, p.question, p.predicted_side, p.ev, p.confidence)
                for p in rows
            ]

        if not pending:
            return 0, 0

        wins = losses = 0

        for pred_id, condition_id, question, predicted_side, ev, confidence in pending:
            resolution = self._resolve(condition_id)
            if resolution is None:
                time.sleep(0.2)
                continue

            outcome = "WIN" if resolution == predicted_side else "LOSS"
            if outcome == "WIN":
                wins += 1
            else:
                losses += 1

            with get_session() as session:
                pred = session.get(Prediction, pred_id)
                if pred:
                    pred.outcome = outcome
                    pred.resolution_value = resolution
                    pred.resolved_at = datetime.now(timezone.utc)

            self.notifier.send_outcome(
                question=question,
                predicted_side=predicted_side,
                outcome=outcome,
                ev=ev,
                confidence=confidence,
                resolution_value=resolution,
            )
            log.info(
                f"{outcome}: {question[:60]} | "
                f"predicted={predicted_side} resolved={resolution}"
            )
            time.sleep(0.3)

        if wins or losses:
            log.info(f"Resolution sweep: {wins}W / {losses}L")

        return wins, losses

    def _resolve(self, condition_id: str) -> str | None:
        """
        Returns 'YES', 'NO', or None if the market has not yet resolved.

        Polymarket Gamma API: query /markets?condition_ids=<cid> (the
        /markets/<id> path needs the numeric id, not the condition id).
        A market is resolved when closed == True and umaResolutionStatus
        == 'resolved'. The winner is read from outcomePrices: a JSON string
        like '["1", "0"]' where index 0 is the YES outcome — the side priced
        at 1 is the winner.
        """
        import json as _json
        try:
            # closed=true: the API excludes closed markets by default, and we
            # only care about markets that have already closed (resolved).
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
                try:
                    prices = _json.loads(raw_prices)
                except ValueError:
                    return None
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
            log.warning(
                f"Resolved market {condition_id} has ambiguous prices {prices}"
            )
            return None
        except Exception as exc:
            log.debug(f"Cannot check resolution for {condition_id}: {exc}")
            return None
