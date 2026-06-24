"""
Streamlit dashboard — run with:  streamlit run src/dashboard/app.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow imports from project root when run directly
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pandas as pd
import streamlit as st
from sqlalchemy import desc

from config.settings import settings
from src.analysis.ev_calculator import ev_explanation
from src.analysis.kelly import kelly_explanation
from src.database.db import get_session, init_db
from src.database.models import Market, Opportunity, Prediction, ScanRun, Trade
from src.risk.risk_manager import RiskManager

# ------------------------------------------------------------------ #
# Page config                                                          #
# ------------------------------------------------------------------ #

st.set_page_config(
    page_title="Polymarket Bot Dashboard",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ------------------------------------------------------------------ #
# Init DB (idempotent)                                                 #
# ------------------------------------------------------------------ #

init_db(settings.db_url)

# ------------------------------------------------------------------ #
# Helper: load data                                                    #
# ------------------------------------------------------------------ #

@st.cache_data(ttl=60)
def load_opportunities(limit: int = 50) -> pd.DataFrame:
    with get_session() as session:
        rows = (
            session.query(Opportunity, Market)
            .join(Market, Opportunity.market_id == Market.id)
            .order_by(desc(Opportunity.ev))
            .limit(limit)
            .all()
        )
        if not rows:
            return pd.DataFrame()

        records = []
        for opp, mkt in rows:
            records.append({
                "opp_id": opp.id,
                "Question": mkt.question[:80] + ("…" if len(mkt.question) > 80 else ""),
                "Category": mkt.category or "—",
                "Side": opp.recommended_side,
                "Implied %": f"{opp.implied_prob:.1%}",
                "Estimated %": f"{opp.estimated_prob:.1%}",
                "Edge": f"{opp.edge:+.1%}",
                "EV": f"{opp.ev:.1%}",
                "Confidence": opp.confidence.title(),
                "Size $": f"${opp.position_size_usd:.2f}",
                "Found": opp.created_at.strftime("%Y-%m-%d %H:%M") if opp.created_at else "—",
                # raw for sorting
                "_ev": opp.ev,
                "_edge": opp.edge,
                "_implied_prob": opp.implied_prob,
                "_estimated_prob": opp.estimated_prob,
                "_size": opp.position_size_usd,
                "_evidence": opp.evidence_summary,
                "_key_factors": opp.key_factors,
                "_risks": opp.risks,
                "_resolution": mkt.resolution_date.strftime("%Y-%m-%d") if mkt.resolution_date else "—",
                "_volume": mkt.volume_24h,
                "_condition_id": mkt.condition_id,
            })
        return pd.DataFrame(records)


@st.cache_data(ttl=60)
def load_scan_history(limit: int = 48) -> pd.DataFrame:
    with get_session() as session:
        runs = (
            session.query(ScanRun)
            .order_by(desc(ScanRun.started_at))
            .limit(limit)
            .all()
        )
        if not runs:
            return pd.DataFrame()
        return pd.DataFrame([{
            "Started": r.started_at.strftime("%Y-%m-%d %H:%M") if r.started_at else "—",
            "Scanned": r.markets_scanned,
            "Opportunities": r.opportunities_found,
            "Errors": r.errors,
            "Duration (s)": f"{r.duration_seconds:.1f}" if r.duration_seconds else "—",
        } for r in runs])


@st.cache_data(ttl=60)
def load_predictions(limit: int = 100) -> pd.DataFrame:
    with get_session() as session:
        rows = (
            session.query(Prediction)
            .order_by(desc(Prediction.created_at))
            .limit(limit)
            .all()
        )
        if not rows:
            return pd.DataFrame()
        return pd.DataFrame([{
            "Question": p.question[:80] + ("…" if len(p.question) > 80 else ""),
            "Side": p.predicted_side,
            "Estimated %": f"{p.predicted_prob:.1%}",
            "Implied %": f"{p.implied_prob:.1%}",
            "EV": f"{p.ev:.1%}",
            "Confidence": p.confidence.title(),
            "Outcome": p.outcome,
            "Predicted": p.created_at.strftime("%Y-%m-%d") if p.created_at else "—",
            "Resolved": p.resolved_at.strftime("%Y-%m-%d") if p.resolved_at else "—",
            "_outcome": p.outcome,
        } for p in rows])


@st.cache_data(ttl=60)
def load_trades() -> pd.DataFrame:
    with get_session() as session:
        trades = (
            session.query(Trade, Market)
            .join(Market, Trade.market_id == Market.id)
            .order_by(desc(Trade.opened_at))
            .limit(100)
            .all()
        )
        if not trades:
            return pd.DataFrame()
        return pd.DataFrame([{
            "Question": mkt.question[:60] + "…",
            "Side": t.side,
            "Price": f"{t.price:.3f}",
            "Size $": f"${t.size_usd:.2f}",
            "Status": t.status.upper(),
            "Opened": t.opened_at.strftime("%Y-%m-%d %H:%M") if t.opened_at else "—",
            "PnL $": f"${t.pnl:+.2f}" if t.pnl is not None else "—",
        } for t, mkt in trades])


# ------------------------------------------------------------------ #
# Sidebar                                                              #
# ------------------------------------------------------------------ #

with st.sidebar:
    st.title("⚙️ Controls")
    if st.button("🔄 Refresh Data"):
        st.cache_data.clear()
        st.rerun()

    st.divider()
    st.subheader("Account")
    equity = st.number_input(
        "Equity ($)", value=settings.account_equity_usd, min_value=0.0, step=100.0
    )

    st.divider()
    st.subheader("Filters")
    min_ev = st.slider("Min EV", 0.00, 0.30, 0.05, 0.01, format="%.0f%%")
    min_conf = st.selectbox("Min Confidence", ["all", "medium", "high"])

    st.divider()
    st.caption(f"Model: {settings.anthropic_model}")
    st.caption(f"Scan interval: {settings.scan_interval_minutes} min")

# ------------------------------------------------------------------ #
# Main content                                                         #
# ------------------------------------------------------------------ #

st.title("📈 Polymarket Bot — Opportunity Dashboard")
st.caption("Auto-refreshes every 60 seconds. Click Refresh to update now.")

# ---- Risk Status bar ----
risk_mgr = RiskManager(
    account_equity=equity,
    max_trade_risk_pct=settings.max_trade_risk_pct,
    max_daily_risk_pct=settings.max_daily_risk_pct,
)
status = risk_mgr.status_report()

col1, col2, col3, col4 = st.columns(4)
col1.metric("Account Equity", f"${status['account_equity']:,.2f}")
col2.metric("Max Trade Size", f"${status['max_trade_size']:,.2f}")
col3.metric("Daily Loss", f"${status['daily_loss']:.2f}", delta=f"/ ${status['daily_limit']:.2f} limit")
col4.metric("Daily Utilization", f"{status['daily_utilization_pct']:.1f}%")

st.divider()

# ---- Prediction Win Rate ----
st.subheader("🏆 Prediction Accuracy")
df_preds = load_predictions()
if df_preds.empty:
    st.info("No predictions recorded yet — win rate will appear after the first scan finds opportunities.")
else:
    total_wins = int((df_preds["_outcome"] == "WIN").sum())
    total_losses = int((df_preds["_outcome"] == "LOSS").sum())
    total_pending = int((df_preds["_outcome"] == "PENDING").sum())
    total_resolved = total_wins + total_losses
    win_rate = total_wins / total_resolved if total_resolved > 0 else None

    p1, p2, p3, p4 = st.columns(4)
    p1.metric("Win Rate", f"{win_rate:.1%}" if win_rate is not None else "—")
    p2.metric("Wins", total_wins)
    p3.metric("Losses", total_losses)
    p4.metric("Pending", total_pending)

    st.markdown("**Recent Predictions**")
    display_cols = ["Question", "Side", "Estimated %", "Implied %", "EV", "Confidence", "Outcome", "Predicted", "Resolved"]

    def _highlight_outcome(row):
        if row["Outcome"] == "WIN":
            return ["background-color: #1a3a1a"] * len(row)
        if row["Outcome"] == "LOSS":
            return ["background-color: #3a1a1a"] * len(row)
        return [""] * len(row)

    st.dataframe(
        df_preds[display_cols].style.apply(_highlight_outcome, axis=1),
        hide_index=True,
    )

st.divider()

# ---- Opportunities ----
st.subheader(f"🎯 Top {settings.top_opportunities} Opportunities")

df_raw = load_opportunities(limit=200)
if df_raw.empty:
    st.info("No opportunities yet. Run the scanner first: `python src/main.py scan`")
else:
    # Apply filters
    df = df_raw.copy()
    df = df[df["_ev"] >= min_ev]
    if min_conf == "medium":
        df = df[df["Confidence"].isin(["Medium", "High"])]
    elif min_conf == "high":
        df = df[df["Confidence"] == "High"]

    df = df.head(settings.top_opportunities)

    display_cols = ["Question", "Category", "Side", "Implied %", "Estimated %", "Edge", "EV", "Confidence", "Size $", "Found"]
    st.dataframe(df[display_cols], width="stretch", hide_index=True)

    # ---- Opportunity detail ----
    st.subheader("🔍 Opportunity Detail")
    if not df.empty:
        questions = df["Question"].tolist()
        selected = st.selectbox("Select market", questions)
        row = df[df["Question"] == selected].iloc[0]

        c1, c2 = st.columns(2)
        with c1:
            st.markdown(f"**Market:** {row['Question']}")
            st.markdown(f"**Category:** {row['Category']}")
            st.markdown(f"**Recommended:** {row['Side']} @ {row['Implied %']} implied")
            st.markdown(f"**Resolution date:** {row['_resolution']}")
            st.markdown(f"**24h Volume:** ${row['_volume']:,.0f}")
            st.markdown(f"**Condition ID:** `{row['_condition_id']}`")

        with c2:
            st.markdown("**EV Breakdown:**")
            st.code(ev_explanation(type('EVR', (), {
                'side': row['Side'],
                'implied_prob': row['_implied_prob'],
                'estimated_prob': row['_estimated_prob'],
                'edge': row['_edge'],
                'ev': row['_ev'],
            })()))

        st.markdown("**Kelly Sizing:**")
        st.code(kelly_explanation(
            win_prob=row['_estimated_prob'],
            price=row['_implied_prob'],
            account_equity=equity,
            fraction=settings.kelly_fraction,
            max_risk_pct=settings.max_trade_risk_pct,
        ))

        if row["_evidence"]:
            st.markdown("**Supporting Evidence:**")
            st.text(row["_evidence"])

        if row["_key_factors"]:
            st.markdown("**Key Factors:**")
            st.text(row["_key_factors"])

        if row["_risks"]:
            st.markdown("**Risks:**")
            st.text(row["_risks"])

st.divider()

# ---- Scan History ----
st.subheader("📋 Scan History (last 48 runs)")
df_scans = load_scan_history()
if df_scans.empty:
    st.info("No scan runs recorded yet.")
else:
    st.dataframe(df_scans, width="stretch", hide_index=True)

st.divider()

# ---- Trade History ----
st.subheader("💼 Trade Log")
df_trades = load_trades()
if df_trades.empty:
    st.info("No trades recorded yet.")
else:
    pnl_total = sum(
        float(t.replace("$", "").replace("+", ""))
        for t in df_trades["PnL $"]
        if t != "—"
    )
    st.metric("Total PnL", f"${pnl_total:+.2f}")
    st.dataframe(df_trades, width="stretch", hide_index=True)

# ---- Auto-refresh ----
import time as _time
_time.sleep(0)  # prevents Streamlit from blocking
st.markdown(
    "<script>setTimeout(function(){window.location.reload()}, 60000);</script>",
    unsafe_allow_html=True,
)
