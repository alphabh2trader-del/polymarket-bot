import pytest
from src.analysis.kelly import full_kelly, fractional_kelly, position_size_usd


def test_full_kelly_positive():
    fk = full_kelly(win_prob=0.60, net_odds=1.5)
    assert fk > 0


def test_full_kelly_no_edge():
    fk = full_kelly(win_prob=0.40, net_odds=1.0)
    assert fk <= 0


def test_fractional_kelly_smaller_than_full():
    fk = fractional_kelly(win_prob=0.60, price=0.40, fraction=0.25)
    full = full_kelly(0.60, 1.5)
    assert fk == pytest.approx(0.25 * full, rel=0.01)


def test_position_size_capped():
    # With $1000 equity and 1% cap, max size is $10
    size = position_size_usd(
        win_prob=0.80, price=0.10,
        account_equity=1000, fraction=0.25, max_risk_pct=0.01
    )
    assert size <= 10.0


def test_position_size_zero_no_edge():
    size = position_size_usd(
        win_prob=0.40, price=0.60,
        account_equity=1000, fraction=0.25, max_risk_pct=0.01
    )
    assert size == 0.0
