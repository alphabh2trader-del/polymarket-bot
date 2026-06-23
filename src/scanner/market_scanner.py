"""
Main market scanner — runs every 15 minutes, analyses all active markets,
persists opportunities to the database.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Optional

from apscheduler.schedulers.blocking import BlockingScheduler

from config.settings import settings
from src.api.news import NewsAggregator
from src.api.polymarket import MarketData, PolymarketClient
from src.analysis.ev_calculator import EVResult, find_best_opportunity
from src.analysis.kelly import position_size_usd
from src.analysis.probability import ProbabilityEstimate, ProbabilityEstimator
from src.database.db import get_session, init_db
from src.database.models import Market, Opportunity, PriceHistory, ScanRun
from src.risk.risk_manager import RiskManager
from src.scanner.anomaly_detector import AnomalyDetector, AnomalySignal
from src.utils.logger import get_logger

log = get_logger(__name__)


class MarketScanner:
    def __init__(self):
        self.poly = PolymarketClient(
            host=settings.polymarket_host,
            api_key=settings.polymarket_api_key,
            private_key=settings.polymarket_private_key,
        )
        self.news = NewsAggregator(
            newsapi_key=settings.newsapi_key,
            gnews_key=settings.gnews_api_key,
        )
        self.estimator = ProbabilityEstimator(
            api_key=settings.anthropic_api_key,
            model=settings.anthropic_model,
        )
        self.risk = RiskManager(
            account_equity=settings.account_equity_usd,
            max_trade_risk_pct=settings.max_trade_risk_pct,
            max_daily_risk_pct=settings.max_daily_risk_pct,
            max_category_exposure_pct=settings.max_category_exposure_pct,
            min_liquidity_usd=settings.min_liquidity_usd,
            min_hours_to_resolution=settings.min_hours_to_resolution,
        )
        self.anomaly = AnomalyDetector(
            volume_spike_multiplier=settings.volume_spike_multiplier,
            price_move_threshold=settings.price_move_threshold,
        )
        self._scheduler = BlockingScheduler(timezone="UTC")

    # ------------------------------------------------------------------ #
    # Scheduler                                                            #
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        """Start the 15-minute scanning loop."""
        log.info(f"Scanner starting — interval: {settings.scan_interval_minutes} minutes")
        # Run immediately, then on schedule
        self.run_scan()
        self._scheduler.add_job(
            self.run_scan,
            "interval",
            minutes=settings.scan_interval_minutes,
            id="market_scan",
            max_instances=1,
        )
        try:
            self._scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            log.info("Scanner stopped by user.")

    # ------------------------------------------------------------------ #
    # Main scan                                                            #
    # ------------------------------------------------------------------ #

    def run_scan(self) -> ScanRun:
        started_at = datetime.utcnow()
        log.info("=== Scan started ===")

        with get_session() as session:
            scan_run = ScanRun(started_at=started_at)
            session.add(scan_run)
            session.flush()
            scan_run_id = scan_run.id

        markets = self.poly.get_all_active_markets()
        log.info(f"Fetched {len(markets)} markets from Polymarket")

        # Filter by minimum volume
        eligible = [
            m for m in markets
            if m.volume_24h >= settings.min_volume_usd and m.active
        ]
        log.info(f"{len(eligible)} markets pass minimum volume filter")

        opportunities_found = 0
        errors = 0

        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {
                executor.submit(self._analyse_market, m, scan_run_id): m
                for m in eligible
            }
            for future in as_completed(futures):
                market = futures[future]
                try:
                    opp = future.result()
                    if opp:
                        opportunities_found += 1
                except Exception as exc:
                    import traceback
                    log.error(f"Error analysing market {market.condition_id}: {exc}\n{traceback.format_exc()}")
                    errors += 1

        duration = (datetime.utcnow() - started_at).total_seconds()

        with get_session() as session:
            run = session.get(ScanRun, scan_run_id)
            if run:
                run.completed_at = datetime.utcnow()
                run.markets_scanned = len(eligible)
                run.opportunities_found = opportunities_found
                run.errors = errors
                run.duration_seconds = duration

        log.info(
            f"=== Scan complete === "
            f"scanned={len(eligible)} opportunities={opportunities_found} "
            f"errors={errors} duration={duration:.1f}s"
        )
        return scan_run_id

    # ------------------------------------------------------------------ #
    # Per-market analysis                                                  #
    # ------------------------------------------------------------------ #

    def _analyse_market(self, market: MarketData, scan_run_id: int) -> Optional[int]:
        """
        Full analysis pipeline for one market.
        Returns the Opportunity id if an opportunity is found, else None.
        """
        # --- Upsert market in DB ---
        db_market_id = self._upsert_market(market)

        # --- Fetch & store price history ---
        history = []
        if market.tokens:
            token_id = market.tokens[0] if isinstance(market.tokens[0], str) else market.tokens[0].get("token_id", "")
            if token_id:
                history = self.poly.get_price_history(token_id, interval="1h", fidelity=24)
                self._store_price_history(db_market_id, history)

        # --- Anomaly detection ---
        anomalies: list[AnomalySignal] = []
        if history:
            anomalies = self.anomaly.check_price_history(
                market.condition_id, market.question, history
            )
            for sig in anomalies:
                log.info(f"ANOMALY [{sig.anomaly_type}] {market.question[:60]}: {sig.description}")

        # --- News search ---
        query = self._extract_search_query(market.question)
        articles = self.news.search_news(query, days_back=7)
        news_text = NewsAggregator.format_for_prompt(articles)

        # --- Resolution date string ---
        res_str = (
            market.resolution_date.strftime("%Y-%m-%d")
            if market.resolution_date
            else "Unknown"
        )

        # --- Probability estimation ---
        estimate: ProbabilityEstimate = self.estimator.estimate(
            question=market.question,
            description=market.description,
            yes_price=market.yes_price,
            no_price=market.no_price,
            resolution_date=res_str,
            news_text=news_text,
        )
        log.debug(
            f"{market.question[:60]} | "
            f"implied={market.yes_price:.2f} estimated={estimate.probability:.2f} "
            f"conf={estimate.confidence}"
        )

        # --- EV calculation ---
        ev_result: EVResult = find_best_opportunity(
            yes_price=market.yes_price,
            no_price=market.no_price,
            estimated_yes_prob=estimate.probability,
            min_ev_threshold=settings.min_ev_threshold,
        )

        if not ev_result.is_opportunity:
            return None

        # --- Risk check ---
        kelly_size = position_size_usd(
            win_prob=ev_result.estimated_prob,
            price=ev_result.implied_prob,
            account_equity=settings.account_equity_usd,
            fraction=settings.kelly_fraction,
            max_risk_pct=settings.max_trade_risk_pct,
        )
        decision = self.risk.check_trade(
            proposed_size_usd=kelly_size,
            volume_24h=market.volume_24h,
            resolution_date=market.resolution_date,
            category=market.category,
            min_ev=settings.min_ev_threshold,
            ev=ev_result.ev,
        )

        if not decision.approved:
            log.debug(f"Risk rejected: {market.question[:60]} — {decision.reason}")
            return None

        # --- Save opportunity ---
        opp_id = self._save_opportunity(
            db_market_id=db_market_id,
            scan_run_id=scan_run_id,
            ev_result=ev_result,
            estimate=estimate,
            size_usd=decision.adjusted_size_usd,
            articles=articles,
        )

        log.info(
            f"OPPORTUNITY: {market.question[:70]} | "
            f"{ev_result.side} EV={ev_result.ev:.1%} edge={ev_result.edge:+.1%} "
            f"size=${decision.adjusted_size_usd:.2f} conf={estimate.confidence}"
        )
        return opp_id

    # ------------------------------------------------------------------ #
    # Database helpers                                                     #
    # ------------------------------------------------------------------ #

    def _upsert_market(self, market: MarketData) -> int:
        with get_session() as session:
            db_market = session.query(Market).filter_by(
                condition_id=market.condition_id
            ).first()

            if db_market is None:
                db_market = Market(condition_id=market.condition_id)
                session.add(db_market)

            db_market.question = market.question
            db_market.description = market.description
            db_market.category = market.category
            db_market.yes_price = market.yes_price
            db_market.no_price = market.no_price
            db_market.volume_24h = market.volume_24h
            db_market.open_interest = market.open_interest
            db_market.resolution_date = market.resolution_date
            db_market.active = market.active
            db_market.last_updated = datetime.utcnow()
            session.flush()
            return db_market.id

    def _store_price_history(self, market_id: int, history: list) -> None:
        if not history:
            return
        with get_session() as session:
            for point in history[-6:]:  # store last 6 points only to avoid bloat
                session.add(PriceHistory(
                    market_id=market_id,
                    timestamp=point.timestamp,
                    yes_price=point.yes_price,
                    no_price=point.no_price,
                    volume=point.volume,
                ))

    def _save_opportunity(
        self,
        db_market_id: int,
        scan_run_id: int,
        ev_result: EVResult,
        estimate: ProbabilityEstimate,
        size_usd: float,
        articles: list,
    ) -> int:
        from src.analysis.kelly import fractional_kelly
        kelly_frac = fractional_kelly(
            win_prob=ev_result.estimated_prob,
            price=ev_result.implied_prob,
            fraction=settings.kelly_fraction,
        )

        evidence = "\n".join(
            f"• [{a.published_at.strftime('%Y-%m-%d')}] {a.source}: {a.title}"
            for a in articles[:5]
        )

        with get_session() as session:
            opp = Opportunity(
                market_id=db_market_id,
                scan_run_id=scan_run_id,
                implied_prob=ev_result.implied_prob,
                estimated_prob=ev_result.estimated_prob,
                edge=ev_result.edge,
                ev=ev_result.ev,
                kelly_fraction=kelly_frac,
                position_size_usd=size_usd,
                recommended_side=ev_result.side,
                confidence=estimate.confidence,
                evidence_summary=evidence,
                key_factors="\n".join(f"• {f}" for f in estimate.key_factors),
                risks="\n".join(f"• {r}" for r in estimate.risks),
            )
            session.add(opp)
            session.flush()
            return opp.id

    @staticmethod
    def _extract_search_query(question: str) -> str:
        """Trim the question for a news search query (first 80 chars, no question mark)."""
        q = question.replace("?", "").strip()
        return q[:80] if len(q) > 80 else q
