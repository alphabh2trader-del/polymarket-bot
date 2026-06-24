"""
Telegram Bot notification sender + two-way command listener.
Uses the Bot API via plain requests — no extra library needed.

Supported incoming commands:
  /status   — win rate, wins, losses, pending
  /top      — top 5 current opportunities
  /pending  — list pending predictions
  /ping     — check bot is alive
  /help     — list commands
"""
from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Callable

import requests

from src.utils.logger import get_logger

log = get_logger(__name__)

_API_BASE = "https://api.telegram.org/bot{token}"


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str):
        self._token = bot_token
        self._chat_id = chat_id
        self._base = _API_BASE.format(token=bot_token)
        self._url = f"{self._base}/sendMessage"
        self._enabled = bool(bot_token and chat_id)
        if not self._enabled:
            log.warning("Telegram notifier disabled — set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")

    # ------------------------------------------------------------------ #
    # Core send                                                            #
    # ------------------------------------------------------------------ #

    def _send(self, text: str, chat_id: str | None = None) -> bool:
        if not self._enabled:
            log.debug(f"Telegram (disabled): {text[:80]}")
            return False
        target = str(chat_id) if chat_id else self._chat_id
        try:
            resp = requests.post(
                self._url,
                json={"chat_id": target, "text": text, "parse_mode": "HTML"},
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

    # ------------------------------------------------------------------ #
    # Two-way command listener                                             #
    # ------------------------------------------------------------------ #

    def start_polling(self, on_command: Callable[[str, str], None]) -> threading.Thread:
        """
        Start a background daemon thread that long-polls Telegram for incoming
        messages and calls on_command(command_text, chat_id) for each /command.
        """
        if not self._enabled:
            log.warning("Telegram polling skipped — bot token not configured")
            return None
        t = threading.Thread(
            target=self._poll_loop,
            args=(on_command,),
            daemon=True,
            name="telegram-poll",
        )
        t.start()
        log.info("Telegram command listener started (long-polling)")
        return t

    def _poll_loop(self, on_command: Callable[[str, str], None]) -> None:
        offset = 0
        while True:
            try:
                resp = requests.get(
                    f"{self._base}/getUpdates",
                    params={"timeout": 30, "offset": offset},
                    timeout=35,
                )
                data = resp.json()
                for update in data.get("result", []):
                    offset = update["update_id"] + 1
                    msg = update.get("message", {})
                    text = msg.get("text", "").strip()
                    chat_id = str(msg.get("chat", {}).get("id", ""))
                    if text.startswith("/") and chat_id:
                        command = text.split()[0].lower()
                        log.info(f"Telegram command received: {command} from {chat_id}")
                        try:
                            on_command(command, chat_id)
                        except Exception as exc:
                            log.error(f"Command handler error: {exc}")
            except requests.exceptions.Timeout:
                pass  # normal long-poll timeout, loop again
            except Exception as exc:
                log.error(f"Telegram poll error: {exc}")
                time.sleep(5)
