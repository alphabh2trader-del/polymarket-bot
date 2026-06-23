"""
Detects unusual volume spikes and rapid price moves.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from statistics import mean, stdev
from typing import Optional

from src.api.polymarket import PricePoint
from src.utils.logger import get_logger

log = get_logger(__name__)


@dataclass
class AnomalySignal:
    anomaly_type: str        # "volume_spike" | "price_move"
    magnitude: float         # e.g. 4.2 = 4.2× average, or 0.15 = 15% move
    market_condition_id: str
    market_question: str
    detected_at: datetime
    description: str


class AnomalyDetector:
    def __init__(
        self,
        volume_spike_multiplier: float = 3.0,
        price_move_threshold: float = 0.10,
    ):
        self.volume_spike_multiplier = volume_spike_multiplier
        self.price_move_threshold = price_move_threshold

    def check_price_history(
        self,
        condition_id: str,
        question: str,
        history: list[PricePoint],
    ) -> list[AnomalySignal]:
        """
        Analyse price history for anomalies.
        Requires at least 3 data points; more is better.
        """
        if len(history) < 3:
            return []

        signals: list[AnomalySignal] = []

        # Sort oldest → newest
        history = sorted(history, key=lambda p: p.timestamp)

        # ---- Volume spike ----
        volumes = [p.volume for p in history if p.volume > 0]
        if len(volumes) >= 3:
            avg_volume = mean(volumes[:-1])  # average of all but last
            latest_volume = volumes[-1]
            if avg_volume > 0 and latest_volume > self.volume_spike_multiplier * avg_volume:
                ratio = latest_volume / avg_volume
                signals.append(AnomalySignal(
                    anomaly_type="volume_spike",
                    magnitude=round(ratio, 2),
                    market_condition_id=condition_id,
                    market_question=question,
                    detected_at=datetime.utcnow(),
                    description=(
                        f"Volume {ratio:.1f}× above average "
                        f"(latest={latest_volume:,.0f}, avg={avg_volume:,.0f})"
                    ),
                ))

        # ---- Price move ----
        prices = [p.yes_price for p in history]
        if len(prices) >= 2:
            # Compare last price to price N steps back (up to 3 steps)
            lookback = min(3, len(prices) - 1)
            old_price = prices[-(lookback + 1)]
            new_price = prices[-1]
            if old_price > 0:
                move = abs(new_price - old_price) / old_price
                if move >= self.price_move_threshold:
                    direction = "UP" if new_price > old_price else "DOWN"
                    signals.append(AnomalySignal(
                        anomaly_type="price_move",
                        magnitude=round(move, 4),
                        market_condition_id=condition_id,
                        market_question=question,
                        detected_at=datetime.utcnow(),
                        description=(
                            f"Price moved {direction} {move:.1%} "
                            f"({old_price:.3f} → {new_price:.3f})"
                        ),
                    ))

        return signals

    def check_current_vs_baseline(
        self,
        condition_id: str,
        question: str,
        current_volume: float,
        baseline_volume: float,
        current_price: float,
        baseline_price: float,
    ) -> list[AnomalySignal]:
        """Simpler check when we only have current vs stored baseline values."""
        signals: list[AnomalySignal] = []

        if baseline_volume > 0:
            ratio = current_volume / baseline_volume
            if ratio >= self.volume_spike_multiplier:
                signals.append(AnomalySignal(
                    anomaly_type="volume_spike",
                    magnitude=round(ratio, 2),
                    market_condition_id=condition_id,
                    market_question=question,
                    detected_at=datetime.utcnow(),
                    description=f"Volume {ratio:.1f}× above baseline",
                ))

        if baseline_price > 0:
            move = abs(current_price - baseline_price) / baseline_price
            if move >= self.price_move_threshold:
                direction = "UP" if current_price > baseline_price else "DOWN"
                signals.append(AnomalySignal(
                    anomaly_type="price_move",
                    magnitude=round(move, 4),
                    market_condition_id=condition_id,
                    market_question=question,
                    detected_at=datetime.utcnow(),
                    description=f"Price {direction} {move:.1%} vs baseline",
                ))

        return signals
