"""
Backtesting engine.

Uses resolved markets stored in the database (or a CSV export) to simulate
what the scanner would have done historically, then computes performance metrics.

Usage:
  python src/main.py backtest --start 2024-01-01 --end 2024-12-31 --equity 1000
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from src.analysis.ev_calculator import find_best_opportunity
from src.analysis.kelly import position_size_usd
from src.backtest.metrics import BacktestMetrics, compute_metrics
from src.database.db import get_session
from src.database.models import Market, Opportunity, Trade
from src.utils.logger import get_logger

log = get_logger(__name__)


@dataclass
class BacktestTrade:
    condition_id: str
    question: str
    side: str
    price: float
    size_usd: float
    estimated_prob: float
    implied_prob: float
    ev: float
    opened_at: datetime
    resolved_yes: Optional[bool] = None   # True/False when known, None if unresolved
    pnl: Optional[float] = None


@dataclass
class BacktestResult:
    trades: list[BacktestTrade] = field(default_factory=list)
    metrics: Optional[BacktestMetrics] = None


class BacktestEngine:
    def __init__(
        self,
        account_equity: float = 1000.0,
        kelly_fraction: float = 0.25,
        max_trade_risk_pct: float = 0.01,
        min_ev_threshold: float = 0.05,
        min_volume_usd: float = 5_000.0,
    ):
        self.account_equity = account_equity
        self.kelly_fraction = kelly_fraction
        self.max_trade_risk_pct = max_trade_risk_pct
        self.min_ev_threshold = min_ev_threshold
        self.min_volume_usd = min_volume_usd

    # ------------------------------------------------------------------ #
    # Run backtest from database                                           #
    # ------------------------------------------------------------------ #

    def run_from_db(
        self,
        start_date: datetime,
        end_date: datetime,
    ) -> BacktestResult:
        """
        Simulate scanner on historical Opportunity records already in the DB.
        Requires resolved Markets (markets where resolution is known).

        NOTE: This is a replay backtest. For a true forward simulation,
        store probability estimates at the time of scan and compare to resolution.
        """
        result = BacktestResult()

        with get_session() as session:
            opportunities = (
                session.query(Opportunity, Market)
                .join(Market, Opportunity.market_id == Market.id)
                .filter(
                    Opportunity.created_at >= start_date,
                    Opportunity.created_at <= end_date,
                )
                .all()
            )

        log.info(f"Backtesting {len(opportunities)} opportunities from {start_date.date()} to {end_date.date()}")

        for opp, mkt in opportunities:
            size = position_size_usd(
                win_prob=opp.estimated_prob,
                price=opp.implied_prob,
                account_equity=self.account_equity,
                fraction=self.kelly_fraction,
                max_risk_pct=self.max_trade_risk_pct,
            )

            trade = BacktestTrade(
                condition_id=mkt.condition_id,
                question=mkt.question,
                side=opp.recommended_side,
                price=opp.implied_prob,
                size_usd=size,
                estimated_prob=opp.estimated_prob,
                implied_prob=opp.implied_prob,
                ev=opp.ev,
                opened_at=opp.created_at,
            )
            result.trades.append(trade)

        # Compute metrics for trades that have resolved
        resolved = [t for t in result.trades if t.pnl is not None]
        if resolved:
            days = max(1, (end_date - start_date).days)
            result.metrics = compute_metrics(
                pnl_series=[t.pnl for t in resolved],
                staked_series=[t.size_usd for t in resolved],
                estimated_probs=[t.estimated_prob for t in resolved],
                outcomes=[1.0 if t.pnl > 0 else 0.0 for t in resolved],
                days=days,
            )

        return result

    # ------------------------------------------------------------------ #
    # Run backtest from CSV                                                #
    # ------------------------------------------------------------------ #

    def run_from_csv(self, csv_path: str, start_date: datetime, end_date: datetime) -> BacktestResult:
        """
        Load historical market data from a CSV file.

        Expected columns:
          condition_id, question, yes_price, no_price, volume_24h,
          snapshot_date, resolved (YES/NO/UNRESOLVED), category
        """
        path = Path(csv_path)
        if not path.exists():
            raise FileNotFoundError(f"CSV not found: {csv_path}")

        result = BacktestResult()
        pnl_list, staked_list, probs, outcomes = [], [], [], []

        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    snap_date = datetime.fromisoformat(row["snapshot_date"])
                    if not (start_date <= snap_date <= end_date):
                        continue

                    yes_price = float(row["yes_price"])
                    no_price = float(row.get("no_price", str(1 - yes_price)))
                    volume = float(row.get("volume_24h", 0))

                    if volume < self.min_volume_usd:
                        continue

                    # For CSV backtest we use market price as "estimated" (no LLM)
                    # A real use-case would have stored estimates in the CSV
                    estimated_prob = float(row.get("estimated_prob", yes_price))

                    ev_result = find_best_opportunity(
                        yes_price=yes_price,
                        no_price=no_price,
                        estimated_yes_prob=estimated_prob,
                        min_ev_threshold=self.min_ev_threshold,
                    )

                    if not ev_result.is_opportunity:
                        continue

                    size = position_size_usd(
                        win_prob=ev_result.estimated_prob,
                        price=ev_result.implied_prob,
                        account_equity=self.account_equity,
                        fraction=self.kelly_fraction,
                        max_risk_pct=self.max_trade_risk_pct,
                    )

                    resolved_str = row.get("resolved", "UNRESOLVED").upper()
                    if resolved_str == "UNRESOLVED":
                        pnl = None
                        resolved_yes = None
                    else:
                        resolved_yes = resolved_str == "YES"
                        if ev_result.side == "YES":
                            pnl = size * (1 / ev_result.implied_prob - 1) if resolved_yes else -size
                        else:
                            pnl = size * (1 / ev_result.implied_prob - 1) if not resolved_yes else -size

                    trade = BacktestTrade(
                        condition_id=row.get("condition_id", ""),
                        question=row.get("question", ""),
                        side=ev_result.side,
                        price=ev_result.implied_prob,
                        size_usd=size,
                        estimated_prob=estimated_prob,
                        implied_prob=ev_result.implied_prob,
                        ev=ev_result.ev,
                        opened_at=snap_date,
                        resolved_yes=resolved_yes,
                        pnl=pnl,
                    )
                    result.trades.append(trade)

                    if pnl is not None:
                        pnl_list.append(pnl)
                        staked_list.append(size)
                        probs.append(estimated_prob)
                        outcomes.append(1.0 if pnl > 0 else 0.0)

                except (KeyError, ValueError) as exc:
                    log.warning(f"Skipping row: {exc}")
                    continue

        if pnl_list:
            days = max(1, (end_date - start_date).days)
            result.metrics = compute_metrics(pnl_list, staked_list, probs, outcomes, days)

        log.info(f"Backtest complete: {len(result.trades)} trades, {len(pnl_list)} resolved")
        return result

    def print_report(self, result: BacktestResult) -> None:
        print(f"\nBacktest: {len(result.trades)} total trades found")
        resolved = [t for t in result.trades if t.pnl is not None]
        print(f"Resolved: {len(resolved)}")

        if result.metrics:
            print(result.metrics)
        else:
            print("No resolved trades to compute metrics.")

        print("\nTop 10 trades by EV:")
        top = sorted(result.trades, key=lambda t: t.ev, reverse=True)[:10]
        for t in top:
            pnl_str = f"${t.pnl:+.2f}" if t.pnl is not None else "unresolved"
            print(f"  [{t.side}] {t.question[:60]} | EV={t.ev:.1%} | PnL={pnl_str}")
