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
        """Returns 'YES', 'NO', or None if the market has not yet resolved."""
        try:
            data = self.poly._get_gamma(f"/markets/{condition_id}")
            if not data.get("resolved", False):
                return None
            raw = str(data.get("resolutionValue", "")).upper().strip()
            if raw in ("YES", "1", "TRUE"):
                return "YES"
            if raw in ("NO", "0", "FALSE"):
                return "NO"
            log.warning(f"Unrecognised resolutionValue '{raw}' for {condition_id}")
            return None
        except Exception as exc:
            log.debug(f"Cannot check resolution for {condition_id}: {exc}")
            return None
