"""
Performance metrics for backtest results.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence


@dataclass
class BacktestMetrics:
    total_trades: int
    winning_trades: int
    losing_trades: int
    hit_rate: float           # % of winning trades
    total_pnl: float
    total_staked: float
    roi: float                # total_pnl / total_staked
    annualized_roi: float
    sharpe_ratio: float
    max_drawdown: float       # fraction of peak equity
    brier_score: float        # calibration (lower = better, perfect = 0)
    avg_edge: float           # average (estimated_prob - implied_prob) on winning trades
    days: int

    def __str__(self) -> str:
        return (
            f"Backtest Results ({self.days} days)\n"
            f"  Trades:        {self.total_trades} ({self.winning_trades}W / {self.losing_trades}L)\n"
            f"  Hit Rate:      {self.hit_rate:.1%}\n"
            f"  Total PnL:     ${self.total_pnl:+,.2f}\n"
            f"  Total Staked:  ${self.total_staked:,.2f}\n"
            f"  ROI:           {self.roi:.2%}\n"
            f"  Ann. ROI:      {self.annualized_roi:.2%}\n"
            f"  Sharpe:        {self.sharpe_ratio:.2f}\n"
            f"  Max Drawdown:  {self.max_drawdown:.2%}\n"
            f"  Brier Score:   {self.brier_score:.4f}\n"
            f"  Avg Edge:      {self.avg_edge:+.2%}\n"
        )


def compute_metrics(
    pnl_series: list[float],         # per-trade PnL
    staked_series: list[float],      # per-trade amount staked
    estimated_probs: list[float],    # our probability estimate per trade
    outcomes: list[float],           # actual outcome: 1.0 = win, 0.0 = loss
    days: int,
) -> BacktestMetrics:
    n = len(pnl_series)
    if n == 0:
        return BacktestMetrics(0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, days)

    winning = [p for p in pnl_series if p > 0]
    losing  = [p for p in pnl_series if p <= 0]
    hit_rate = len(winning) / n
    total_pnl = sum(pnl_series)
    total_staked = sum(staked_series)
    roi = total_pnl / total_staked if total_staked > 0 else 0.0
    ann_roi = (1 + roi) ** (365 / max(days, 1)) - 1

    # Sharpe (assume daily returns approximation)
    if n > 1:
        returns = [p / s for p, s in zip(pnl_series, staked_series) if s > 0]
        avg_r = sum(returns) / len(returns)
        variance = sum((r - avg_r) ** 2 for r in returns) / len(returns)
        std_r = math.sqrt(variance) if variance > 0 else 0.0
        sharpe = (avg_r / std_r) * math.sqrt(252) if std_r > 0 else 0.0
    else:
        sharpe = 0.0

    # Max drawdown (cumulative equity curve)
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for pnl in pnl_series:
        equity += pnl
        if equity > peak:
            peak = equity
        if peak > 0:
            dd = (peak - equity) / peak
            max_dd = max(max_dd, dd)

    # Brier score: mean((estimated_prob - outcome)^2)
    brier = (
        sum((p - o) ** 2 for p, o in zip(estimated_probs, outcomes)) / n
        if n > 0 else 0.0
    )

    # Average edge on winning trades (edge = estimated_prob used at time of trade)
    avg_edge = sum(estimated_probs) / n - sum(outcomes) / n  # simplification

    return BacktestMetrics(
        total_trades=n,
        winning_trades=len(winning),
        losing_trades=len(losing),
        hit_rate=hit_rate,
        total_pnl=total_pnl,
        total_staked=total_staked,
        roi=roi,
        annualized_roi=ann_roi,
        sharpe_ratio=sharpe,
        max_drawdown=max_dd,
        brier_score=brier,
        avg_edge=avg_edge,
        days=days,
    )
