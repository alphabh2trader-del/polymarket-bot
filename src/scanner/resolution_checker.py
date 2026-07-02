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

from config.settings import settings
from src.api.polymarket import PolymarketClient
from src.database.db import get_session
from src.database.models import Prediction
from src.notifications.telegram import TelegramNotifier
from src.utils.logger import get_logger

log = get_logger(__name__)

_MAX_PER_RUN = 50
_GAMMA_HOST = "https://gamma-api.polymarket.com"


class ResolutionChecker:
    def __init__(self, poly: PolymarketClient, notifier: TelegramNotifier,
                 estimator=None, news=None):
        self.poly = poly
        self.notifier = notifier
        # Optional — enable the news-driven thesis re-check. If absent, the
        # re-check is silently skipped (price-only behaviour).
        self.estimator = estimator
        self.news = news

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
                    "created_at": p.created_at,
                    "last_recheck": p.last_recheck_at,
                }
                for p in rows
            ]

        if not pending:
            return 0, 0

        wins = losses = 0
        rechecks_done = 0

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
                elif self._time_exit_due(p["created_at"], current, p["entry"]):
                    # Time close (24h). Close at the current price. If the price
                    # barely moved (within the breakeven band) the trade didn't
                    # work either way -> BREAKEVEN (not counted as win or loss).
                    outcome = self._classify(current, p["entry"])
                    exit_price, exit_reason = current, "TIME_EXIT"
            else:
                # Not in the active set -> market is likely closed; settle on outcome.
                resolution = self._resolve(p["condition_id"])
                if resolution is not None:
                    # Under the trade-the-price strategy a position should always
                    # exit via target/stop/time BEFORE the market resolves. If it
                    # still resolved, the close is an artifact (price snapped to
                    # $1 or $0), so it doesn't count either way -> VOID. This keeps
                    # the win rate symmetric: neither resolution-wins nor
                    # resolution-losses inflate or deflate the stats.
                    exit_value = 1.0 if resolution == p["side"] else 0.0
                    outcome, exit_price, exit_reason = "VOID", exit_value, "RESOLVED"

            # Thesis re-check: position still open but moving against us. Re-read the
            # news and ask Claude again; if the edge is gone, close now (THESIS_EXIT)
            # instead of waiting for the stop. Triggered + capped so it barely adds
            # to the Claude bill.
            did_recheck = False
            if (
                outcome is None
                and current is not None
                and self.estimator is not None
                and settings.thesis_recheck_enabled
                and rechecks_done < settings.recheck_max_per_sweep
                and current <= p["entry"] * (1 - settings.recheck_trigger_pct)
                and self._recheck_cooldown_ok(p["last_recheck"])
            ):
                did_recheck = True
                rechecks_done += 1
                if self._thesis_broken(p, current):
                    outcome = self._classify(current, p["entry"])
                    exit_price, exit_reason = current, "THESIS_EXIT"

            # Safety net: any close at $0 doesn't count.
            if outcome in ("WIN", "LOSS") and exit_price is not None and exit_price <= 0.0:
                outcome = "VOID"

            with get_session() as session:
                pred = session.get(Prediction, p["id"])
                if not pred:
                    continue
                if current is not None:
                    pred.current_price = round(current, 4)
                if did_recheck:
                    pred.last_recheck_at = datetime.now(timezone.utc)
                if outcome:
                    pred.outcome = outcome
                    pred.exit_price = round(exit_price, 4)
                    pred.exit_reason = exit_reason
                    pred.resolved_at = datetime.now(timezone.utc)
                    if exit_reason == "RESOLVED":
                        # Record the true winning side. RESOLVED closes are VOID
                        # either way now, so infer the winner from exit_price:
                        # 1.0 -> our side won, 0.0 -> the other side won.
                        our_side_won = exit_price is not None and exit_price >= 1.0
                        pred.resolution_value = p["side"] if our_side_won else (
                            "NO" if p["side"] == "YES" else "YES"
                        )

            if outcome:
                ret = (exit_price - p["entry"]) / p["entry"] if p["entry"] else 0.0
                # VOID + BREAKEVEN don't count toward win/loss and get no alert.
                if outcome in ("WIN", "LOSS"):
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

    @staticmethod
    def _time_exit_due(created_at, current: float, entry: float) -> bool:
        """
        Time-based close:
          - in profit (current > entry)  -> close after profit_hold_hours (24h)
          - otherwise                    -> hold until the hard cap max_hold_hours (24h)
        """
        if created_at is None:
            return False
        from config.settings import settings
        # created_at is stored naive UTC (datetime.utcnow)
        age_hours = (datetime.utcnow() - created_at).total_seconds() / 3600
        if current > entry and age_hours >= settings.profit_hold_hours:
            return True
        if age_hours >= settings.max_hold_hours:
            return True
        return False

    @staticmethod
    def _classify(current: float, entry: float) -> str:
        """WIN / LOSS / BREAKEVEN for a time- or thesis-exit, based on how far
        the price moved from entry. Within +/- breakeven_band_pct = BREAKEVEN."""
        ret = (current - entry) / entry if entry else 0.0
        if abs(ret) <= settings.breakeven_band_pct:
            return "BREAKEVEN"
        return "WIN" if ret > 0 else "LOSS"

    @staticmethod
    def _recheck_cooldown_ok(last) -> bool:
        """True if the position hasn't been re-checked within the cooldown window."""
        if last is None:
            return True
        last_utc = last if last.tzinfo else last.replace(tzinfo=timezone.utc)
        age_h = (datetime.now(timezone.utc) - last_utc).total_seconds() / 3600
        return age_h >= settings.recheck_cooldown_hours

    def _get_market(self, condition_id: str):
        """Fetch the active market dict (or None) from the Gamma API."""
        try:
            data = self.poly._get_gamma("/markets", params={"condition_ids": condition_id})
            if not data:
                return None
            return data[0] if isinstance(data, list) else data
        except Exception as exc:
            log.debug(f"Cannot fetch market {condition_id}: {exc}")
            return None

    def _thesis_broken(self, p: dict, current: float) -> bool:
        """
        Re-read the news and re-run Claude on an open position. The thesis is
        "broken" when Claude no longer values our side above what we paid (entry)
        — i.e. the reason for the trade is gone. On any error we return False so a
        transient failure never force-closes a position.
        """
        if self.estimator is None:
            return False
        try:
            market = self._get_market(p["condition_id"])
            if not market:
                return False
            raw = market.get("outcomePrices", "")
            prices = _json.loads(raw) if isinstance(raw, str) else raw
            yes_price = float(prices[0]) if prices else current
            no_price = (
                float(prices[1]) if prices and len(prices) > 1 else round(1.0 - yes_price, 4)
            )
            description = market.get("description", "") or ""
            end = market.get("endDate") or market.get("end_date_iso") or "Unknown"

            articles = []
            news_text = ""
            if self.news is not None:
                query = self.news.build_search_query(p["question"])
                articles = self.news.search_news(query, days_back=7)
                news_text = self.news.format_for_prompt(articles)

            est = self.estimator.estimate(
                question=p["question"],
                description=description,
                yes_price=yes_price,
                no_price=no_price,
                resolution_date=str(end)[:10],
                news_text=news_text,
            )
            new_side_prob = (
                est.probability if p["side"] == "YES" else round(1.0 - est.probability, 4)
            )
            broken = new_side_prob <= p["entry"]
            log.info(
                f"THESIS RECHECK [{'BROKEN' if broken else 'intact'}] "
                f"{p['question'][:50]} | side={p['side']} entry={p['entry']:.2f} "
                f"new_fair={new_side_prob:.2f}"
            )
            return broken
        except Exception as exc:
            log.warning(f"Thesis recheck failed for {p['condition_id']}: {exc}")
            return False

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
