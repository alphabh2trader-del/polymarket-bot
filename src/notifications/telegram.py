"""
Telegram Bot notification sender.
Uses the Bot API via plain requests — no extra library needed.
"""
from __future__ import annotations

from datetime import datetime

import requests

from src.utils.logger import get_logger

log = get_logger(__name__)

_API_URL = "https://api.telegram.org/bot{token}/sendMessage"


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str):
        self._token = bot_token
        self._chat_id = chat_id
        self._url = _API_URL.format(token=bot_token)
        self._enabled = bool(bot_token and chat_id)
        if not self._enabled:
            log.warning("Telegram notifier disabled — set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")

    # ------------------------------------------------------------------ #
    # Core send                                                            #
    # ------------------------------------------------------------------ #

    def _send(self, text: str) -> bool:
        if not self._enabled:
            log.debug(f"Telegram (disabled): {text[:80]}")
            return False
        try:
            resp = requests.post(
                self._url,
                json={"chat_id": self._chat_id, "text": text, "parse_mode": "HTML"},
                timeout=10,
            )
            resp.raise_for_status()
            return True
        except Exception as exc:
            log.error(f"Telegram send failed: {exc}")
            return False

    # ------------------------------------------------------------------ #
    # Outcome notification                                                 #
    # ------------------------------------------------------------------ #

    def send_outcome(
        self,
        question: str,
        predicted_side: str,
        outcome: str,
        ev: float,
        confidence: str,
        resolution_value: str,
    ) -> bool:
        label = "WIN" if outcome == "WIN" else "LOSS"
        marker = "+" if outcome == "WIN" else "-"
        text = (
            f"<b>[{marker}1] {label}</b>\n\n"
            f"<b>Market:</b> {question[:150]}\n"
            f"<b>Predicted:</b> {predicted_side}   "
            f"<b>Resolved:</b> {resolution_value}\n"
            f"<b>EV was:</b> {ev:.1%}   "
            f"<b>Confidence:</b> {confidence.title()}\n"
            f"<i>{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}</i>"
        )
        return self._send(text)

    # ------------------------------------------------------------------ #
    # Periodic summaries                                                   #
    # ------------------------------------------------------------------ #

    def send_daily_summary(
        self,
        wins: int,
        losses: int,
        pending: int,
        win_rate: float | None,
    ) -> bool:
        rate_str = f"{win_rate:.1%}" if win_rate is not None else "N/A"
        text = (
            f"<b>Daily Win Rate — {datetime.utcnow().strftime('%Y-%m-%d')}</b>\n\n"
            f"All-time:  {wins}W  /  {losses}L\n"
            f"Still pending:  {pending}\n"
            f"<b>Win rate: {rate_str}</b>"
        )
        return self._send(text)

    def send_weekly_summary(
        self,
        wins: int,
        losses: int,
        pending: int,
        win_rate: float | None,
        week_wins: int,
        week_losses: int,
    ) -> bool:
        rate_str = f"{win_rate:.1%}" if win_rate is not None else "N/A"
        text = (
            f"<b>Weekly Summary — week ending {datetime.utcnow().strftime('%Y-%m-%d')}</b>\n\n"
            f"This week:  {week_wins}W  /  {week_losses}L\n"
            f"All time:  {wins}W  /  {losses}L  /  {pending} pending\n"
            f"<b>Win rate (all time): {rate_str}</b>"
        )
        return self._send(text)

    def send_monthly_summary(
        self,
        wins: int,
        losses: int,
        pending: int,
        win_rate: float | None,
        month_wins: int,
        month_losses: int,
    ) -> bool:
        rate_str = f"{win_rate:.1%}" if win_rate is not None else "N/A"
        text = (
            f"<b>Monthly Summary — {datetime.utcnow().strftime('%B %Y')}</b>\n\n"
            f"This month:  {month_wins}W  /  {month_losses}L\n"
            f"All time:  {wins}W  /  {losses}L  /  {pending} pending\n"
            f"<b>Win rate (all time): {rate_str}</b>"
        )
        return self._send(text)
