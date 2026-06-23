import pytest
from src.analysis.ev_calculator import calculate_ev, find_best_opportunity


def test_ev_positive_edge():
    result = calculate_ev(implied_prob=0.40, estimated_prob=0.55, side="YES")
    assert result.edge > 0
    assert result.ev > 0
    assert result.is_opportunity


def test_ev_negative_edge():
    result = calculate_ev(implied_prob=0.60, estimated_prob=0.40, side="YES")
    assert result.edge < 0
    assert not result.is_opportunity


def test_ev_no_edge():
    result = calculate_ev(implied_prob=0.50, estimated_prob=0.50, side="YES")
    assert abs(result.edge) < 0.001
    assert not result.is_opportunity


def test_find_best_opportunity_yes():
    result = find_best_opportunity(0.30, 0.70, estimated_yes_prob=0.55)
    assert result.side == "YES"
    assert result.is_opportunity


def test_find_best_opportunity_no():
    result = find_best_opportunity(0.70, 0.30, estimated_yes_prob=0.40)
    assert result.side == "NO"
    assert result.is_opportunity


def test_ev_formula():
    # If we estimate 60% and market is at 40%, EV should be positive and significant
    result = calculate_ev(implied_prob=0.40, estimated_prob=0.60, side="YES")
    expected_ev = 0.60 * (1/0.40 - 1) - 0.40 * 1
    assert abs(result.ev - expected_ev) < 0.001
