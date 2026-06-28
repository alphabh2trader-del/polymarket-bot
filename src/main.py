"""
Polymarket Bot — CLI entry point.

Commands:
  scan         Start the 15-minute market scanner
  dashboard    Launch the Streamlit dashboard
  backtest     Run backtest on stored data
  report       Print current top opportunities to console
  check        Verify API connectivity and configuration
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# Ensure project root is on the path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import settings
from src.database.db import init_db
from src.utils.logger import get_logger

log = get_logger(__name__)


def cmd_scan(args) -> None:
    """Start the recurring market scanner."""
    log.info("Initialising database...")
    init_db(settings.db_url)

    from src.scanner.market_scanner import MarketScanner
    scanner = MarketScanner()
    log.info("Starting scanner loop (Ctrl+C to stop)")
    scanner.start()


def cmd_dashboard(args) -> None:
    """Launch Streamlit dashboard."""
    dashboard_path = Path(__file__).parent / "dashboard" / "app.py"
    print(f"Launching dashboard on http://localhost:{settings.dashboard_port}")
    subprocess.run([
        sys.executable, "-m", "streamlit", "run",
        str(dashboard_path),
        "--server.port", str(settings.dashboard_port),
        "--server.headless", "true",
    ], check=True)


def cmd_backtest(args) -> None:
    """Run backtesting."""
    init_db(settings.db_url)

    from src.backtest.engine import BacktestEngine
    engine = BacktestEngine(
        account_equity=args.equity,
        kelly_fraction=settings.kelly_fraction,
        max_trade_risk_pct=settings.max_trade_risk_pct,
        min_ev_threshold=settings.min_ev_threshold,
    )

    start = datetime.fromisoformat(args.start)
    end = datetime.fromisoformat(args.end)

    if args.csv:
        result = engine.run_from_csv(args.csv, start, end)
    else:
        result = engine.run_from_db(start, end)

    engine.print_report(result)


def cmd_report(args) -> None:
    """Print top current opportunities from DB."""
    init_db(settings.db_url)

    from sqlalchemy import desc
    from src.database.db import get_session
    from src.database.models import Market, Opportunity

    with get_session() as session:
        rows = (
            session.query(Opportunity, Market)
            .join(Market, Opportunity.market_id == Market.id)
            .order_by(desc(Opportunity.ev))
            .limit(settings.top_opportunities)
            .all()
        )

    if not rows:
        print("No opportunities found. Run 'scan' first.")
        return

    print(f"\n{'='*80}")
    print(f"TOP {len(rows)} OPPORTUNITIES")
    print(f"{'='*80}")
    for i, (opp, mkt) in enumerate(rows, 1):
        print(
            f"\n{i}. {mkt.question[:70]}\n"
            f"   Side={opp.recommended_side} | "
            f"Implied={opp.implied_prob:.1%} | "
            f"Estimated={opp.estimated_prob:.1%} | "
            f"Edge={opp.edge:+.1%} | "
            f"EV={opp.ev:.1%} | "
            f"Confidence={opp.confidence} | "
            f"Size=${opp.position_size_usd:.2f}\n"
            f"   Evidence: {(opp.evidence_summary or '')[:120]}"
        )
    print(f"\n{'='*80}\n")


def cmd_check(args) -> None:
    """Verify API connectivity."""
    print("\n--- Configuration Check ---")

    checks = {
        "Polymarket API key": bool(settings.polymarket_api_key),
        "Anthropic API key": bool(settings.anthropic_api_key),
        "TheNewsAPI key": bool(settings.thenewsapi_key),
        "NewsAPI key": bool(settings.newsapi_key),
        "GNews key": bool(settings.gnews_api_key),
        "Database URL": bool(settings.database_url),
    }
    for k, v in checks.items():
        status = "OK" if v else "MISSING"
        print(f"  [{status}]  {k}")

    print("\n--- Polymarket Connectivity ---")
    try:
        from src.api.polymarket import PolymarketClient
        client = PolymarketClient()
        markets = client.get_markets(limit=5)
        print(f"  [OK]  Connected -- fetched {len(markets)} markets")
        if markets:
            m = markets[0]
            print(f"  Sample: {m.question[:60]}")
            print(f"  YES={m.yes_price:.2f}  Volume24h=${m.volume_24h:,.0f}")
    except Exception as exc:
        print(f"  [FAIL]  Failed: {exc}")

    print("\n--- TheNewsAPI Connectivity ---")
    if settings.thenewsapi_key:
        try:
            from src.api.news import NewsAggregator
            agg = NewsAggregator(thenewsapi_key=settings.thenewsapi_key)
            articles = agg._search_thenewsapi("US election", days_back=7)
            print(f"  [OK]  Connected -- {len(articles)} articles returned")
        except Exception as exc:
            print(f"  [FAIL]  Failed: {exc}")
    else:
        print("  - Skipped (no key)")

    print("\n--- NewsAPI Connectivity ---")
    if settings.newsapi_key:
        try:
            from src.api.news import NewsAggregator
            agg = NewsAggregator(newsapi_key=settings.newsapi_key)
            articles = agg._search_newsapi("US election", days_back=7)
            print(f"  [OK]  Connected -- {len(articles)} articles returned")
        except Exception as exc:
            print(f"  [FAIL]  Failed: {exc}")
    else:
        print("  - Skipped (no key)")

    print("\n--- RSS Feeds ---")
    try:
        from src.api.news import NewsAggregator
        agg = NewsAggregator()
        articles = agg._search_rss("economy", days_back=3)
        print(f"  [OK]  RSS feeds working -- {len(articles)} articles")
    except Exception as exc:
        print(f"  [FAIL]  Failed: {exc}")

    print()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="polymarket-bot",
        description="Polymarket quantitative trading bot",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # scan
    subparsers.add_parser("scan", help="Start the 15-minute market scanner")

    # dashboard
    subparsers.add_parser("dashboard", help="Launch Streamlit dashboard")

    # backtest
    bt = subparsers.add_parser("backtest", help="Run backtest")
    bt.add_argument("--start", required=True, help="Start date YYYY-MM-DD")
    bt.add_argument("--end", required=True, help="End date YYYY-MM-DD")
    bt.add_argument("--equity", type=float, default=settings.account_equity_usd)
    bt.add_argument("--csv", default=None, help="Path to CSV file (optional)")

    # report
    subparsers.add_parser("report", help="Print top opportunities to console")

    # check
    subparsers.add_parser("check", help="Verify API connectivity")

    args = parser.parse_args()

    dispatch = {
        "scan": cmd_scan,
        "dashboard": cmd_dashboard,
        "backtest": cmd_backtest,
        "report": cmd_report,
        "check": cmd_check,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
