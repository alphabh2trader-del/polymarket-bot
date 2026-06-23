import pytest
from datetime import datetime, timedelta
from src.risk.risk_manager import RiskManager


@pytest.fixture
def rm():
    return RiskManager(
        account_equity=1000.0,
        max_trade_risk_pct=0.01,
        max_daily_risk_pct=0.05,
        min_liquidity_usd=10_000.0,
        min_hours_to_resolution=48,
        min_volume_usd=5_000.0,
    )


def test_approve_valid_trade(rm):
    decision = rm.check_trade(
        proposed_size_usd=5.0,
        volume_24h=50_000,
        resolution_date=datetime.utcnow() + timedelta(days=10),
        ev=0.10,
        min_ev=0.05,
    )
    assert decision.approved
    assert decision.adjusted_size_usd == 5.0


def test_reject_low_volume(rm):
    decision = rm.check_trade(
        proposed_size_usd=5.0,
        volume_24h=1_000,
        resolution_date=datetime.utcnow() + timedelta(days=10),
        ev=0.10,
    )
    assert not decision.approved
    assert "volume" in decision.reason.lower()


def test_reject_resolving_soon(rm):
    decision = rm.check_trade(
        proposed_size_usd=5.0,
        volume_24h=50_000,
        resolution_date=datetime.utcnow() + timedelta(hours=10),
        ev=0.10,
    )
    assert not decision.approved
    assert "resolution" in decision.reason.lower()


def test_size_capped_at_max(rm):
    decision = rm.check_trade(
        proposed_size_usd=100.0,   # over 1% cap of $1000
        volume_24h=50_000,
        resolution_date=datetime.utcnow() + timedelta(days=10),
        ev=0.10,
    )
    assert decision.approved
    assert decision.adjusted_size_usd <= 10.0  # 1% of $1000


def test_reject_low_ev(rm):
    decision = rm.check_trade(
        proposed_size_usd=5.0,
        volume_24h=50_000,
        resolution_date=datetime.utcnow() + timedelta(days=10),
        ev=0.02,
        min_ev=0.05,
    )
    assert not decision.approved


def test_daily_limit(rm):
    rm._daily_loss = 50.0  # simulate $50 daily loss on $1000 (= 5% limit hit)
    decision = rm.check_trade(
        proposed_size_usd=5.0,
        volume_24h=50_000,
        resolution_date=datetime.utcnow() + timedelta(days=10),
        ev=0.10,
    )
    assert not decision.approved
    assert "daily" in decision.reason.lower()
