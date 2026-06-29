"""
Main market scanner — runs every 15 minutes, analyses all active markets,
persists opportunities and predictions to the database, checks resolutions,
and fires Telegram reports on schedule.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from typing import Optional

from apscheduler.schedulers.blocking import BlockingScheduler

from config.settings import settings
from src.api.news import NewsAggregator
from src.api.polymarket import MarketData, PolymarketClient
from src.analysis.ev_calculator import EVResult, find_best_opportunity
from src.analysis.kelly import position_size_usd
from src.analysis.probability import ProbabilityEstimate, ProbabilityEstimator
from src.database.db import get_session, init_db
from src.database.models import Market, Opportunity, PriceHistory, Prediction, ScanRun
from src.notifications.telegram import TelegramNotifier
from src.risk.risk_manager import RiskManager
from src.scanner.anomaly_detector import AnomalyDetector, AnomalySignal
from src.scanner.resolution_checker import ResolutionChecker
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
            thenewsapi_key=settings.thenewsapi_key,
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
        self.telegram = TelegramNotifier(
            bot_token=settings.telegram_bot_token,
            chat_id=settings.telegram_chat_id,
        )
        self.resolver = ResolutionChecker(
            poly=self.poly,
            notifier=self.telegram,
        )
        self._scheduler = BlockingScheduler(timezone=settings.timezone)
        self._scan_offset = 0  # rotates through eligible markets each scan

    # ------------------------------------------------------------------ #
    # Scheduler                                                            #
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        """Start the scanning loop with all scheduled jobs."""
        log.info(f"Scanner starting — interval: {settings.scan_interval_minutes} minutes")

        # Remove low-quality predictions saved before the quality filters existed
        self._purge_low_quality_predictions()

        # Backfill predictions from any opportunities saved before prediction tracking existed
        self._backfill_predictions()

        # Run scan + resolution check immediately on startup
        self.run_scan()
        self._run_resolution_check()

        # Recurring Claude scan on the configured interval (hourly by default)
        self._scheduler.add_job(
            self.run_scan,
            "interval",
            minutes=settings.scan_interval_minutes,
            id="market_scan",
            max_instances=1,
        )

        # Price-track open positions every few minutes (free Polymarket calls, no Claude).
        # Decoupled from the hourly scan so target/stop hits are caught promptly without
        # paying for an AI scan each time.
        self._scheduler.add_job(
            self._run_resolution_check,
            "interval",
            minutes=settings.position_check_minutes,
            start_date=datetime.now(timezone.utc) + timedelta(minutes=2),
            id="position_check",
            max_instances=1,
        )

        # Daily summary at 20:00 local time (scheduler tz = settings.timezone)
        self._scheduler.add_job(
            self._send_daily_report,
            "cron",
            hour=20,
            minute=0,
            id="daily_report",
        )

        # Weekly summary — Sunday at 20:00 local time
        self._scheduler.add_job(
            self._send_weekly_report,
            "cron",
            day_of_week="sun",
            hour=20,
            minute=0,
            id="weekly_report",
        )

        # Monthly summary — 1st of month at 20:00 local time
        self._scheduler.add_job(
            self._send_monthly_report,
            "cron",
            day=1,
            hour=20,
            minute=0,
            id="monthly_report",
        )

        # Two-way Telegram command listener
        self.telegram.start_polling(self._handle_telegram_command)

        try:
            self._scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            log.info("Scanner stopped.")

    # ------------------------------------------------------------------ #
    # Main scan                                                            #
    # ------------------------------------------------------------------ #

    def run_scan(self) -> int:
        started_at = datetime.utcnow()
        log.info("=== Scan started ===")

        with get_session() as session:
            scan_run = ScanRun(started_at=started_at)
            session.add(scan_run)
            session.flush()
            scan_run_id = scan_run.id

        markets = self.poly.get_all_active_markets()
        log.info(f"Fetched {len(markets)} markets from Polymarket")

        all_eligible = sorted(
            [
                m for m in markets
                if m.volume_24h >= settings.min_volume_usd
                and m.active
                and settings.min_implied_prob <= m.yes_price <= (1 - settings.min_implied_prob)
            ],
            key=lambda m: m.volume_24h,
            reverse=True,
        )

        # One bet per market: drop any market the bot has EVER traded so it can't
        # re-enter the same question (prevents piling into one volatile market).
        if settings.one_bet_per_market:
            with get_session() as session:
                traded = {r[0] for r in session.query(Prediction.condition_id).distinct().all()}
            before = len(all_eligible)
            all_eligible = [m for m in all_eligible if m.condition_id not in traded]
            if before != len(all_eligible):
                log.info(f"Skipped {before - len(all_eligible)} already-traded markets (one-bet-per-market)")

        # Rotate through the eligible pool so each hourly scan covers a fresh
        # batch instead of re-analysing the same top markets every time.
        cap = settings.max_markets_per_scan
        if all_eligible:
            offset = self._scan_offset % len(all_eligible)
            eligible = (all_eligible + all_eligible)[offset:offset + cap]
            self._scan_offset = (offset + cap) % len(all_eligible)
        else:
            eligible = []
        log.info(
            f"{len(all_eligible)} eligible markets; analysing {len(eligible)} "
            f"this scan (rotating offset, min ${settings.min_volume_usd:,.0f})"
        )

        opportunities_found = 0
        errors = 0
        found: list[dict] = []

        with ThreadPoolExecutor(max_workers=2) as executor:
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
                        found.append(opp)
                except Exception as exc:
                    import traceback
                    log.error(
                        f"Error analysing market {market.condition_id}: "
                        f"{exc}\n{traceback.format_exc()}"
                    )
                    errors += 1

        # Notify on Telegram that the scan ran, with edges + expected return
        try:
            self.telegram.send_scan_complete(
                markets_scanned=len(eligible),
                opportunities=found,
            )
        except Exception as exc:
            log.error(f"Scan notification failed: {exc}")

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

    def _analyse_market(self, market: MarketData, scan_run_id: int) -> Optional[dict]:
        db_market_id = self._upsert_market(market)

        history = []
        if market.tokens:
            token_id = market.tokens[0] if isinstance(market.tokens[0], str) else market.tokens[0].get("token_id", "")
            if token_id:
                history = self.poly.get_price_history(token_id, interval="1h", fidelity=24)
                self._store_price_history(db_market_id, history)

        anomalies: list[AnomalySignal] = []
        if history:
            anomalies = self.anomaly.check_price_history(
                market.condition_id, market.question, history
            )
            for sig in anomalies:
                log.info(f"ANOMALY [{sig.anomaly_type}] {market.question[:60]}: {sig.description}")

        query = self._extract_search_query(market.question)
        articles = self.news.search_news(query, days_back=7)
        news_text = NewsAggregator.format_for_prompt(articles)

        res_str = (
            market.resolution_date.strftime("%Y-%m-%d")
            if market.resolution_date
            else "Unknown"
        )

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

        ev_result: EVResult = find_best_opportunity(
            yes_price=market.yes_price,
            no_price=market.no_price,
            estimated_yes_prob=estimate.probability,
            min_ev_threshold=settings.min_ev_threshold,
        )

        if not ev_result.is_opportunity:
            return None

        # Win-rate guard: only bet sides we expect to win more often than not.
        # A positive-EV long shot still loses most of the time and tanks win rate.
        if ev_result.estimated_prob < settings.min_win_probability:
            log.debug(
                f"Skipped (win prob {ev_result.estimated_prob:.0%} < "
                f"{settings.min_win_probability:.0%}): {market.question[:50]}"
            )
            return None

        # Plausibility guard: a disagreement larger than max_edge usually means
        # our estimate is wrong, not the market. Skip it.
        if ev_result.edge > settings.max_edge:
            log.debug(
                f"Skipped (edge {ev_result.edge:.0%} > {settings.max_edge:.0%} "
                f"implausible): {market.question[:50]}"
            )
            return None

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

        opp_id = self._save_opportunity(
            db_market_id=db_market_id,
            scan_run_id=scan_run_id,
            ev_result=ev_result,
            estimate=estimate,
            size_usd=decision.adjusted_size_usd,
            articles=articles,
        )

        # Record prediction (deduped — only one PENDING prediction per market+side)
        self._save_prediction(
            db_market_id=db_market_id,
            opportunity_id=opp_id,
            condition_id=market.condition_id,
            question=market.question,
            ev_result=ev_result,
            estimate=estimate,
        )

        log.info(
            f"OPPORTUNITY: {market.question[:70]} | "
            f"{ev_result.side} EV={ev_result.ev:.1%} edge={ev_result.edge:+.1%} "
            f"size=${decision.adjusted_size_usd:.2f} conf={estimate.confidence}"
        )
        return {
            "question": market.question,
            "side": ev_result.side,
            "edge": ev_result.edge,
            "ev": ev_result.ev,
            "size": decision.adjusted_size_usd,
        }

    # ------------------------------------------------------------------ #
    # Prediction quality                                                   #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _is_quality_prediction(estimated_prob: float, implied_prob: float) -> bool:
        """
        A prediction is worth tracking only if it clears the same bar the live
        scanner now enforces:
          - the market is priced within [min_implied_prob, 1 - min_implied_prob]
          - we expect the chosen side to win at least min_win_probability
          - our disagreement with the market is not implausibly large
        estimated_prob/implied_prob are stored from the chosen side's view.
        """
        lo = settings.min_implied_prob
        if not (lo <= implied_prob <= 1 - lo):
            return False
        if estimated_prob < settings.min_win_probability:
            return False
        if (estimated_prob - implied_prob) > settings.max_edge:
            return False
        return True

    def _purge_low_quality_predictions(self) -> int:
        """Delete PENDING predictions that don't meet the current quality bar."""
        try:
            with get_session() as session:
                rows = session.query(Prediction).filter_by(outcome="PENDING").all()
                removed = 0
                for p in rows:
                    if not self._is_quality_prediction(p.predicted_prob, p.implied_prob):
                        session.delete(p)
                        removed += 1
                if removed:
                    log.info(f"Purged {removed} low-quality pending predictions")
                return removed
        except Exception as exc:
            log.error(f"Purge failed: {exc}")
            return 0

    # ------------------------------------------------------------------ #
    # Backfill                                                             #
    # ------------------------------------------------------------------ #

    def _backfill_predictions(self) -> None:
        """Create Prediction records for any Opportunities that don't have one yet."""
        try:
            with get_session() as session:
                existing_opp_ids = {
                    p.opportunity_id
                    for p in session.query(Prediction.opportunity_id).all()
                    if p.opportunity_id is not None
                }
                opps = (
                    session.query(Opportunity, Market)
                    .join(Market, Opportunity.market_id == Market.id)
                    .filter(Opportunity.id.notin_(existing_opp_ids) if existing_opp_ids else True)
                    .all()
                )
                count = 0
                for opp, mkt in opps:
                    already = (
                        session.query(Prediction)
                        .filter_by(
                            condition_id=mkt.condition_id,
                            predicted_side=opp.recommended_side,
                            outcome="PENDING",
                        )
                        .first()
                    )
                    if already:
                        continue
                    if not self._is_quality_prediction(opp.estimated_prob, opp.implied_prob):
                        continue
                    session.add(Prediction(
                        market_id=mkt.id,
                        opportunity_id=opp.id,
                        condition_id=mkt.condition_id,
                        question=mkt.question,
                        predicted_side=opp.recommended_side,
                        predicted_prob=opp.estimated_prob,
                        implied_prob=opp.implied_prob,
                        current_price=opp.implied_prob,
                        ev=opp.ev,
                        confidence=opp.confidence,
                        created_at=opp.created_at,
                    ))
                    count += 1
                if count:
                    log.info(f"Backfilled {count} predictions from existing opportunities")
        except Exception as exc:
            log.error(f"Backfill failed: {exc}")

    # ------------------------------------------------------------------ #
    # Resolution checking                                                  #
    # ------------------------------------------------------------------ #

    def _run_resolution_check(self) -> None:
        log.info("Tracking open positions (target / stop / settle)...")
        try:
            wins, losses = self.resolver.check_pending()
            log.info(f"Position check done: {wins}W / {losses}L closed this run")
        except Exception as exc:
            log.error(f"Position check failed: {exc}")

    # ------------------------------------------------------------------ #
    # Scheduled Telegram reports                                           #
    # ------------------------------------------------------------------ #

    def _send_daily_report(self) -> None:
        wins, losses, pending = self._get_prediction_stats()
        win_rate = wins / (wins + losses) if (wins + losses) > 0 else None
        self.telegram.send_daily_summary(wins=wins, losses=losses, pending=pending, win_rate=win_rate)
        log.info(f"Daily report sent: {wins}W/{losses}L win_rate={win_rate}")

    def _send_weekly_report(self) -> None:
        wins, losses, pending = self._get_prediction_stats()
        win_rate = wins / (wins + losses) if (wins + losses) > 0 else None
        since = datetime.now(timezone.utc) - timedelta(days=7)
        week_wins, week_losses = self._get_period_stats(since)
        self.telegram.send_weekly_summary(
            wins=wins, losses=losses, pending=pending, win_rate=win_rate,
            week_wins=week_wins, week_losses=week_losses,
        )
        log.info(f"Weekly report sent: {week_wins}W/{week_losses}L this week")

    def _send_monthly_report(self) -> None:
        wins, losses, pending = self._get_prediction_stats()
        win_rate = wins / (wins + losses) if (wins + losses) > 0 else None
        since = datetime.now(timezone.utc) - timedelta(days=30)
        month_wins, month_losses = self._get_period_stats(since)
        self.telegram.send_monthly_summary(
            wins=wins, losses=losses, pending=pending, win_rate=win_rate,
            month_wins=month_wins, month_losses=month_losses,
        )
        log.info(f"Monthly report sent: {month_wins}W/{month_losses}L this month")

    # ------------------------------------------------------------------ #
    # Telegram command handler                                            #
    # ------------------------------------------------------------------ #

    def _handle_telegram_command(self, command: str, chat_id: str) -> None:
        """Respond to commands sent by the user via Telegram."""
        if command == "/ping":
            self.telegram._send("Pong! Bot is alive and scanning.", chat_id)

        elif command in ("/status", "/stats"):
            wins, losses, pending = self._get_prediction_stats()
            total = wins + losses
            rate_str = f"{wins/total:.1%}" if total else "N/A"
            self.telegram._send(
                f"<b>Bot Status</b>\n\n"
                f"Win Rate: <b>{rate_str}</b>\n"
                f"Wins: {wins}  |  Losses: {losses}  |  Pending: {pending}\n\n"
                f"Next scan: every {settings.scan_interval_minutes} min",
                chat_id,
            )

        elif command in ("/top", "/opportunities"):
            with get_session() as session:
                from src.database.models import Opportunity
                from sqlalchemy import desc as _desc
                rows = (
                    session.query(Opportunity, Market)
                    .join(Market, Opportunity.market_id == Market.id)
                    .order_by(_desc(Opportunity.ev))
                    .limit(5)
                    .all()
                )
                if not rows:
                    self.telegram._send("No opportunities recorded yet.", chat_id)
                    return
                lines = ["<b>Top 5 Opportunities (by EV)</b>\n"]
                for opp, mkt in rows:
                    lines.append(
                        f"• <b>{opp.recommended_side}</b> {mkt.question[:70]}\n"
                        f"  EV {opp.ev:.1%} | edge {opp.edge:+.1%} | {opp.confidence}"
                    )
                self.telegram._send("\n".join(lines), chat_id)

        elif command == "/pending":
            with get_session() as session:
                rows = (
                    session.query(Prediction)
                    .filter_by(outcome="PENDING")
                    .order_by(Prediction.created_at.desc())
                    .limit(10)
                    .all()
                )
                if not rows:
                    self.telegram._send("No pending predictions.", chat_id)
                    return
                lines = [f"<b>Pending Predictions ({len(rows)} shown)</b>\n"]
                for p in rows:
                    lines.append(
                        f"• <b>{p.predicted_side}</b> {p.question[:70]}\n"
                        f"  EV {p.ev:.1%} | {p.confidence}"
                    )
                self.telegram._send("\n".join(lines), chat_id)

        elif command == "/help":
            self.telegram._send(
                "<b>Available Commands</b>\n\n"
                "/status — win rate &amp; all-time stats\n"
                "/top — top 5 opportunities by EV\n"
                "/pending — current open predictions\n"
                "/ping — check bot is alive\n"
                "/help — this message",
                chat_id,
            )

        else:
            self.telegram._send(
                f"Unknown command: {command}\nType /help to see what I support.",
                chat_id,
            )

    def _get_prediction_stats(self) -> tuple[int, int, int]:
        """Returns (total_wins, total_losses, total_pending)."""
        with get_session() as session:
            from sqlalchemy import func
            rows = (
                session.query(Prediction.outcome, func.count(Prediction.id))
                .group_by(Prediction.outcome)
                .all()
            )
        counts = {r[0]: r[1] for r in rows}
        return counts.get("WIN", 0), counts.get("LOSS", 0), counts.get("PENDING", 0)

    def _get_period_stats(self, since: datetime) -> tuple[int, int]:
        """Returns (wins, losses) for predictions resolved after `since`."""
        since_naive = since.replace(tzinfo=None)
        with get_session() as session:
            from sqlalchemy import func
            rows = (
                session.query(Prediction.outcome, func.count(Prediction.id))
                .filter(Prediction.resolved_at >= since_naive)
                .filter(Prediction.outcome.in_(["WIN", "LOSS"]))
                .group_by(Prediction.outcome)
                .all()
            )
        counts = {r[0]: r[1] for r in rows}
        return counts.get("WIN", 0), counts.get("LOSS", 0)

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
            for point in history[-6:]:
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

    def _save_prediction(
        self,
        db_market_id: int,
        opportunity_id: int,
        condition_id: str,
        question: str,
        ev_result: EVResult,
        estimate: ProbabilityEstimate,
    ) -> Optional[int]:
        """Save a prediction record.

        Dedup: with one_bet_per_market, never create a second prediction for a
        market the bot has already traded (any side, any outcome). Otherwise fall
        back to the old rule (one PENDING prediction per market+side).
        """
        from config.settings import settings as _settings
        with get_session() as session:
            q = session.query(Prediction).filter_by(condition_id=condition_id)
            if _settings.one_bet_per_market:
                existing = q.first()
            else:
                existing = q.filter_by(
                    predicted_side=ev_result.side,
                    outcome="PENDING",
                ).first()
            if existing:
                return existing.id

            pred = Prediction(
                market_id=db_market_id,
                opportunity_id=opportunity_id,
                condition_id=condition_id,
                question=question,
                predicted_side=ev_result.side,
                predicted_prob=ev_result.estimated_prob,   # target price
                implied_prob=ev_result.implied_prob,       # entry price
                current_price=ev_result.implied_prob,      # starts at entry
                ev=ev_result.ev,
                confidence=estimate.confidence,
            )
            session.add(pred)
            session.flush()
            log.info(
                f"POSITION opened: {ev_result.side} on {question[:55]} "
                f"entry={ev_result.implied_prob:.2f} target={ev_result.estimated_prob:.2f}"
            )
            return pred.id

    @staticmethod
    def _extract_search_query(question: str) -> str:
        q = question.replace("?", "").strip()
        return q[:80] if len(q) > 80 else q
