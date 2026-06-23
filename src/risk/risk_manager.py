"""
Risk management layer.

Enforces:
  - Max 1% of account equity per trade
  - Max 5% daily loss
  - Max 20% exposure per category
  - Minimum $10k 24h volume
  - Resolution date must be > 48 hours away
  - Position size floored at $1 (no micro-trades)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from src.utils.logger import get_logger

log = get_logger(__name__)


@dataclass
class RiskDecision:
    approved: bool
    reason: str
    adjusted_size_usd: float = 0.0

    def __str__(self) -> str:
        status = "APPROVED" if self.approved else "REJECTED"
        return f"[{status}] {self.reason} | size=${self.adjusted_size_usd:.2f}"


class RiskManager:
    def __init__(
        self,
        account_equity: float,
        max_trade_risk_pct: float = 0.01,
        max_daily_risk_pct: float = 0.05,
        max_category_exposure_pct: float = 0.20,
        min_liquidity_usd: float = 10_000.0,
        min_hours_to_resolution: int = 48,
        min_volume_usd: float = 5_000.0,
    ):
        self.account_equity = account_equity
        self.max_trade_risk_pct = max_trade_risk_pct
        self.max_daily_risk_pct = max_daily_risk_pct
        self.max_category_exposure_pct = max_category_exposure_pct
        self.min_liquidity_usd = min_liquidity_usd
        self.min_hours_to_resolution = min_hours_to_resolution
        self.min_volume_usd = min_volume_usd

        self._daily_loss: float = 0.0
        self._daily_reset_date: datetime = datetime.utcnow().date()
        self._category_exposure: dict[str, float] = {}

    # ------------------------------------------------------------------ #
    # Main gate                                                            #
    # ------------------------------------------------------------------ #

    def check_trade(
        self,
        proposed_size_usd: float,
        volume_24h: float,
        resolution_date: Optional[datetime],
        category: str = "",
        min_ev: float = 0.05,
        ev: float = 0.0,
    ) -> RiskDecision:
        """
        Approve or reject a proposed trade.
        Returns RiskDecision with adjusted size if approved.
        """
        self._maybe_reset_daily_loss()

        # 1. EV must be positive and above threshold
        if ev < min_ev:
            return RiskDecision(False, f"EV {ev:.1%} below minimum threshold {min_ev:.1%}")

        # 2. Minimum liquidity
        if volume_24h < self.min_volume_usd:
            return RiskDecision(
                False,
                f"24h volume ${volume_24h:,.0f} below minimum ${self.min_volume_usd:,.0f}",
            )

        # 3. Resolution date check
        if resolution_date:
            from datetime import timezone as _tz
            now = datetime.now(_tz.utc)
            res = resolution_date if resolution_date.tzinfo else resolution_date.replace(tzinfo=_tz.utc)
            hours_left = (res - now).total_seconds() / 3600
            if hours_left < self.min_hours_to_resolution:
                return RiskDecision(
                    False,
                    f"Only {hours_left:.1f}h until resolution (min {self.min_hours_to_resolution}h)",
                )

        # 4. Per-trade size cap
        max_trade = self.account_equity * self.max_trade_risk_pct
        size = min(proposed_size_usd, max_trade)
        if size < 1.0:
            return RiskDecision(False, "Position size too small (< $1.00)")

        # 5. Daily loss limit
        daily_limit = self.account_equity * self.max_daily_risk_pct
        if self._daily_loss >= daily_limit:
            return RiskDecision(
                False,
                f"Daily loss limit reached: ${self._daily_loss:.2f} / ${daily_limit:.2f}",
            )
        remaining_daily = daily_limit - self._daily_loss
        size = min(size, remaining_daily)

        # 6. Category exposure
        if category:
            cat_limit = self.account_equity * self.max_category_exposure_pct
            current_exposure = self._category_exposure.get(category, 0.0)
            if current_exposure >= cat_limit:
                return RiskDecision(
                    False,
                    f"Category '{category}' exposure ${current_exposure:.2f} at limit ${cat_limit:.2f}",
                )
            size = min(size, cat_limit - current_exposure)

        if size < 1.0:
            return RiskDecision(False, "Adjusted size below $1.00 after limits")

        return RiskDecision(
            approved=True,
            reason=f"All checks passed (capped at ${size:.2f})",
            adjusted_size_usd=round(size, 2),
        )

    # ------------------------------------------------------------------ #
    # State updates                                                        #
    # ------------------------------------------------------------------ #

    def record_trade_opened(self, size_usd: float, category: str = "") -> None:
        if category:
            self._category_exposure[category] = (
                self._category_exposure.get(category, 0.0) + size_usd
            )
        log.info(f"Trade opened: ${size_usd:.2f} in category '{category}'")

    def record_trade_closed(self, size_usd: float, pnl: float, category: str = "") -> None:
        if pnl < 0:
            self._daily_loss += abs(pnl)
        if category and category in self._category_exposure:
            self._category_exposure[category] = max(
                0.0, self._category_exposure[category] - size_usd
            )
        log.info(f"Trade closed: ${size_usd:.2f} PnL={pnl:+.2f} daily_loss=${self._daily_loss:.2f}")

    def _maybe_reset_daily_loss(self) -> None:
        today = datetime.utcnow().date()
        if today > self._daily_reset_date:
            self._daily_loss = 0.0
            self._daily_reset_date = today
            log.info("Daily loss counter reset")

    # ------------------------------------------------------------------ #
    # Status report                                                        #
    # ------------------------------------------------------------------ #

    def status_report(self) -> dict:
        daily_limit = self.account_equity * self.max_daily_risk_pct
        return {
            "account_equity": self.account_equity,
            "daily_loss": self._daily_loss,
            "daily_limit": daily_limit,
            "daily_utilization_pct": self._daily_loss / daily_limit * 100 if daily_limit else 0,
            "max_trade_size": self.account_equity * self.max_trade_risk_pct,
            "category_exposure": dict(self._category_exposure),
        }
