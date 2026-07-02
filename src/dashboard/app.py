"""
Polymarket Bot — multi-page Streamlit dashboard.

Pages (sidebar navigation):
  Home    — win-rate donut + live positions feed
  Wins    — searchable list of every winning prediction
  Losses  — searchable list of every losing prediction
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from sqlalchemy import desc

from config.settings import settings
from src.database.db import get_session, init_db
from src.database.models import Prediction, ScanRun

# Display all stored (naive-UTC) times in the configured timezone (Eastern).
# Use stdlib zoneinfo (APScheduler 3.10+ no longer ships pytz), with a pytz
# fallback just in case.
from datetime import timezone as _utc
try:
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo(settings.timezone)
except Exception:
    try:
        import pytz
        _TZ = pytz.timezone(settings.timezone)
    except Exception:
        _TZ = None


def _fmt_local(dt, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """Format a naive-UTC datetime in the configured timezone (e.g. Eastern)."""
    if dt is None:
        return "—"
    if _TZ is None:
        return dt.strftime(fmt)
    # Treat the naive value as UTC, then convert. astimezone works for both
    # ZoneInfo and pytz tz objects.
    return dt.replace(tzinfo=_utc.utc).astimezone(_TZ).strftime(fmt)


# ------------------------------------------------------------------ #
# Page config                                                          #
# ------------------------------------------------------------------ #

st.set_page_config(
    page_title="Polymarket Bot",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

init_db(settings.db_url)

# ------------------------------------------------------------------ #
# Data loaders                                                         #
# ------------------------------------------------------------------ #

@st.cache_data(ttl=30)
def load_predictions(outcome: str | None = None, limit: int = 1000) -> pd.DataFrame:
    with get_session() as session:
        q = session.query(Prediction).order_by(desc(Prediction.created_at))
        if outcome:
            q = q.filter(Prediction.outcome == outcome)
        rows = q.limit(limit).all()
        if not rows:
            return pd.DataFrame()
        out = []
        for p in rows:
            entry = p.implied_prob
            target = p.predicted_prob
            current = p.current_price if p.current_price is not None else entry
            exit_p = p.exit_price
            # Buying $100 of the chosen side at `entry` gets 100/entry shares.
            # Profit when selling at price x = 100 * (x - entry) / entry dollars.
            def _money(x: float) -> str:
                return f"+${x:.0f}" if x >= 0 else f"-${abs(x):.0f}"
            if entry:
                # PROFIT if it hits the target (your goal).
                expected_profit = 100.0 * (target - entry) / entry
                # PROFIT if you cash out NOW (exit price if closed, else live price).
                live_price = exit_p if exit_p is not None else current
                live_profit = 100.0 * (live_price - entry) / entry
                # Realistic fill: paper P&L assumes a mid-price fill, but a real
                # order buys at the ask and sells at the bid, giving up ~one full
                # bid/ask spread round-trip. Haircut on $100 ≈ 100 * spread / entry.
                spread = p.entry_spread
                if spread is not None:
                    net_profit = live_profit - 100.0 * spread / entry
                    net_str = _money(net_profit)
                else:
                    net_str = "—"
                expected_str = _money(expected_profit)
                live_str = _money(live_profit)
            else:
                expected_str = live_str = net_str = "—"
            out.append({
                "Time": _fmt_local(p.created_at),
                "Market": p.question,
                "Side": p.predicted_side,
                "Entry": f"{entry:.0%}",
                "Target": f"{target:.0%}",
                "Current": f"{current:.0%}",
                "Expected/$100": expected_str,
                "Live/$100": live_str,
                "Net/$100": net_str,
                "Confidence": p.confidence.title(),
                "Outcome": p.outcome,
                "_question": p.question,
            })
        return pd.DataFrame(out)


@st.cache_data(ttl=30)
def get_last_scan() -> dict | None:
    with get_session() as session:
        run = (
            session.query(ScanRun)
            .filter(ScanRun.completed_at.isnot(None))
            .order_by(desc(ScanRun.completed_at))
            .first()
        )
        if not run:
            return None
        return {
            "completed_at": _fmt_local(run.completed_at, "%Y-%m-%d %H:%M %Z"),
            "markets_scanned": run.markets_scanned,
            "opportunities_found": run.opportunities_found,
            "errors": run.errors or 0,
        }


@st.cache_data(ttl=30)
def get_stats() -> tuple[int, int, int, int]:
    with get_session() as session:
        from sqlalchemy import func
        rows = (
            session.query(Prediction.outcome, func.count(Prediction.id))
            .group_by(Prediction.outcome)
            .all()
        )
        counts = {r[0]: r[1] for r in rows}
    # Wins, Losses, Pending, Breakeven. Breakeven (price barely moved) is shown
    # separately and excluded from the win-rate donut — it isn't a win or a loss.
    return (
        counts.get("WIN", 0),
        counts.get("LOSS", 0),
        counts.get("PENDING", 0),
        counts.get("BREAKEVEN", 0),
    )


@st.cache_data(ttl=30)
def get_bet_activity() -> dict | None:
    """How many bets the bot opens per day (by Eastern date it was created)."""
    from collections import Counter
    from datetime import datetime as _dt
    with get_session() as session:
        rows = session.query(Prediction.created_at).all()
    dates = []
    for (cr,) in rows:
        if cr is None:
            continue
        d = cr.replace(tzinfo=_utc.utc).astimezone(_TZ).date() if _TZ else cr.date()
        dates.append(d)
    if not dates:
        return None
    cnt = Counter(dates)
    today = (_dt.now(_TZ).date() if _TZ else _dt.utcnow().date())
    return {
        "total": len(dates),
        "days": len(cnt),
        "avg_per_day": len(dates) / len(cnt),
        "today": cnt.get(today, 0),
    }


@st.cache_data(ttl=30)
def get_performance() -> dict | None:
    """
    Performance over every closed WIN/LOSS bet (VOID/PENDING ignored).

      total_profit    — flat $100 per bet: sum of return * $100
      avg_profit_bet  — mean return % per bet (= return on any capital split
                        equally across every bet)
      avg_profit_day  — mean of each day's average return % (avg profit per day)
      days            — calendar days since the first bet
      per_day         — closed bets per day
    """
    from datetime import datetime as _dt
    with get_session() as session:
        rows = (
            session.query(Prediction)
            .filter(Prediction.outcome.in_(("WIN", "LOSS")))
            .order_by(Prediction.created_at)
            .all()
        )
        bets = [
            {
                "entry": p.implied_prob,
                "exit": p.exit_price if p.exit_price is not None else p.current_price,
                "outcome": p.outcome,
                "day": (p.resolved_at or p.created_at).date(),
                "created": p.created_at,
                "spread": p.entry_spread,
            }
            for p in rows
        ]
    if not bets:
        return None

    total_profit = 0.0
    total_profit_net = 0.0      # after estimated bid/ask spread cost
    net_covered = 0            # bets that had a recorded spread
    returns_by_day: dict = {}   # day -> list of per-bet returns
    all_returns: list = []
    for b in bets:
        if not b["entry"] or b["exit"] is None:
            continue
        ret = (b["exit"] - b["entry"]) / b["entry"]
        total_profit += ret * 100.0
        all_returns.append(ret)
        returns_by_day.setdefault(b["day"], []).append(ret)
        # Net return subtracts a ~one-spread round-trip cost (buy ask / sell bid).
        if b["spread"] is not None:
            net_ret = ret - (b["spread"] / b["entry"])
            net_covered += 1
        else:
            net_ret = ret
        total_profit_net += net_ret * 100.0

    # Average profit per bet = return on any capital split equally across every bet.
    avg_profit_bet = sum(all_returns) / len(all_returns) if all_returns else None
    # Average profit per day = mean of each day's average bet return.
    daily_avgs = [sum(r) / len(r) for r in returns_by_day.values() if r]
    avg_profit_day = sum(daily_avgs) / len(daily_avgs) if daily_avgs else None

    first = bets[0]["created"]
    days = max((_dt.utcnow() - first).days, 1)
    n = len(all_returns)

    return {
        "total_profit": total_profit,
        "total_profit_net": total_profit_net,
        "avg_profit_bet": avg_profit_bet,
        "avg_profit_bet_net": total_profit_net / 100.0 / n if n else None,
        "avg_profit_day": avg_profit_day,
        "net_covered": net_covered,
        "days": days,
        "per_day": len(bets) / days,
        "n": len(bets),
    }


# ------------------------------------------------------------------ #
# Sidebar — navigation                                                 #
# ------------------------------------------------------------------ #

with st.sidebar:
    st.markdown("## 📈 Polymarket Bot")
    st.divider()
    page = st.radio(
        "nav",
        ["🏠  Home", "✅  Wins", "❌  Losses"],
        label_visibility="collapsed",
    )
    st.divider()
    if st.button("🔄 Refresh"):
        st.cache_data.clear()
        st.rerun()
    st.divider()
    last = get_last_scan()
    if last:
        st.caption("**Last scan**")
        st.caption(last["completed_at"])
        st.caption(f"Markets: {last['markets_scanned']}  |  Opps: {last['opportunities_found']}")
        if last["errors"]:
            st.caption(f"⚠️ Errors: {last['errors']}")
    else:
        st.caption("No scan recorded yet")
    st.divider()
    st.caption(f"Scan every {settings.scan_interval_minutes} min")

# ------------------------------------------------------------------ #
# Shared stats                                                         #
# ------------------------------------------------------------------ #

wins, losses, pending, breakeven = get_stats()
total_resolved = wins + losses   # breakeven excluded — it's neither a win nor a loss
win_rate = wins / total_resolved if total_resolved > 0 else None


def _donut_chart() -> go.Figure:
    if total_resolved == 0:
        fig = go.Figure(go.Pie(
            values=[1],
            labels=["Awaiting predictions"],
            hole=0.68,
            marker_colors=["#2a2a2a"],
            textinfo="none",
            hoverinfo="skip",
        ))
        center_text = "—<br><span style='font-size:14px'>No data yet</span>"
    else:
        fig = go.Figure(go.Pie(
            values=[wins, losses],
            labels=["Wins", "Losses"],
            hole=0.68,
            marker_colors=["#00c471", "#ff4444"],
            textinfo="percent",
            textfont=dict(size=15, color="white"),
            hovertemplate="%{label}: %{value}  (%{percent})<extra></extra>",
            sort=False,
            direction="clockwise",
        ))
        center_text = f"<b>{win_rate:.1%}</b><br><span style='font-size:14px'>Win Rate</span>"

    fig.update_layout(
        annotations=[dict(
            text=center_text,
            x=0.5, y=0.5,
            font=dict(size=26, color="white"),
            showarrow=False,
            xanchor="center",
            yanchor="middle",
        )],
        showlegend=True,
        legend=dict(
            orientation="h",
            x=0.5, xanchor="center",
            y=-0.06,
            font=dict(color="white", size=14),
        ),
        margin=dict(t=10, b=40, l=10, r=10),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        height=340,
    )
    return fig


def _style_feed(df: pd.DataFrame):
    def row_style(row):
        if row["Outcome"] == "WIN":
            return ["background-color: #0c2b0c"] * len(row)
        if row["Outcome"] == "LOSS":
            return ["background-color: #2b0c0c"] * len(row)
        return [""] * len(row)
    return df.style.apply(row_style, axis=1)


# ------------------------------------------------------------------ #
# HOME                                                                 #
# ------------------------------------------------------------------ #

if page == "🏠  Home":
    # Centred donut
    _, mid, _ = st.columns([1, 2, 1])
    with mid:
        st.plotly_chart(_donut_chart(), use_container_width=True)
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Wins", wins)
        c2.metric("Losses", losses)
        c3.metric("Breakeven", breakeven, help="Price closed essentially flat (within ~0.5%, i.e. $0 on a $100 bet). Not counted as a win or loss, and excluded from the win-rate donut.")
        c4.metric("Pending", pending)

    st.divider()
    st.subheader("Performance")
    perf = get_performance()
    if not perf:
        st.info("No closed bets yet — performance will appear once positions start resolving.")
    else:
        p1, p2, p3 = st.columns(3)
        p1.metric(
            "Total Profit ($100/bet)",
            f"${perf['total_profit']:+,.0f}",
            help="If you staked a flat $100 on every closed bet, this is the summed profit.",
        )
        p2.metric(
            "Avg Profit / Bet",
            f"{perf['avg_profit_bet']:+.1%}" if perf["avg_profit_bet"] is not None else "—",
            help="Average return per bet — i.e. your return if you'd split a flat stake equally across every bet.",
        )
        p3.metric(
            "Avg Profit / Day",
            f"{perf['avg_profit_day']:+.1%}" if perf["avg_profit_day"] is not None else "—",
            help="Average of each day's average return %. Differs from Avg Profit/Bet when bet volume varies "
                 "day to day — a day with 1 bet counts as much as a day with 10.",
        )
        st.caption(
            f"Based on {perf['n']} closed bets over {perf['days']} day(s) "
            f"(~{perf['per_day']:.1f} per day)."
        )

        st.markdown("##### Return on your capital")
        cap_col, result_col = st.columns([1, 2])
        with cap_col:
            capital = st.number_input(
                "Starting capital ($)", min_value=100, value=1000, step=100,
                help="Hypothetical bankroll, split equally across every closed bet.",
            )
        with result_col:
            if perf["avg_profit_bet"] is not None:
                capital_profit = capital * perf["avg_profit_bet"]
                st.metric(
                    f"On ${capital:,.0f} split across {perf['n']} bets",
                    f"${capital_profit:+,.2f}",
                    f"{perf['avg_profit_bet']:+.1%}",
                    help="Same as Avg Profit/Bet, just shown in dollars for a real bankroll instead of the "
                         "flat $100-per-bet stat above.",
                )
            else:
                st.info("Not enough closed bets yet.")

        if perf.get("net_covered"):
            st.caption(
                f"📉 Estimated net of bid/ask spread: **${perf['total_profit_net']:+,.0f}** "
                f"(vs ${perf['total_profit']:+,.0f} gross at mid-price). A real order buys at "
                f"the ask and sells at the bid, giving up ~one spread round-trip. Spread was "
                f"recorded on {perf['net_covered']} of {perf['n']} closed bets; older bets "
                f"predate spread capture and are shown at their mid-price fill."
            )
        else:
            st.caption(
                "📉 Net-of-spread estimate will appear once positions opened after this "
                "update start closing (entry spread is now recorded at open)."
            )

    act = get_bet_activity()
    if act:
        b1, b2 = st.columns(2)
        b1.metric("Avg Bets Opened / Day", f"{act['avg_per_day']:.1f}",
                  help=f"{act['total']} bets opened over {act['days']} day(s).")
        b2.metric("Bets Opened Today", act["today"], help="Positions opened so far today (Eastern).")

    st.divider()
    st.subheader("Live Positions")

    df_all = load_predictions()
    if df_all.empty:
        st.info("No predictions yet — the bot will start recording positions on its first scan.")
    else:
        display_cols = ["Time", "Market", "Side", "Entry", "Target", "Current", "Expected/$100", "Live/$100", "Net/$100", "Confidence", "Outcome"]
        st.dataframe(
            _style_feed(df_all[display_cols]),
            hide_index=True,
            height=520,
        )

# ------------------------------------------------------------------ #
# WINS                                                                 #
# ------------------------------------------------------------------ #

elif page == "✅  Wins":
    st.title("Wins")

    col_a, col_b = st.columns([3, 1])
    with col_a:
        search = st.text_input("Search", placeholder="Type any keyword to filter...")
    with col_b:
        st.metric("Total Wins", wins)

    df_wins = load_predictions(outcome="WIN")
    if df_wins.empty:
        st.info("No wins recorded yet — they will appear here as markets resolve.")
    else:
        if search.strip():
            df_wins = df_wins[df_wins["_question"].str.contains(search.strip(), case=False, na=False)]
        display_cols = ["Time", "Market", "Side", "Entry", "Target", "Current", "Expected/$100", "Live/$100", "Net/$100", "Confidence"]
        st.dataframe(
            df_wins[display_cols].reset_index(drop=True),
            hide_index=True,
            height=620,
        )

# ------------------------------------------------------------------ #
# LOSSES                                                               #
# ------------------------------------------------------------------ #

elif page == "❌  Losses":
    st.title("Losses")

    col_a, col_b = st.columns([3, 1])
    with col_a:
        search = st.text_input("Search", placeholder="Type any keyword to filter...")
    with col_b:
        st.metric("Total Losses", losses)

    df_losses = load_predictions(outcome="LOSS")
    if df_losses.empty:
        st.info("No losses recorded yet.")
    else:
        if search.strip():
            df_losses = df_losses[df_losses["_question"].str.contains(search.strip(), case=False, na=False)]
        display_cols = ["Time", "Market", "Side", "Entry", "Target", "Current", "Expected/$100", "Live/$100", "Net/$100", "Confidence"]
        st.dataframe(
            df_losses[display_cols].reset_index(drop=True),
            hide_index=True,
            height=620,
        )

# ------------------------------------------------------------------ #
# Auto-refresh every 30 s                                             #
# ------------------------------------------------------------------ #

import time as _time
_time.sleep(30)
st.rerun()
