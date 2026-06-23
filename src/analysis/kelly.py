"""
Kelly Criterion position sizing.

Full Kelly: f* = (b·p - q) / b
  where:
    b = net fractional odds (= 1/price - 1)
    p = probability of winning
    q = 1 - p = probability of losing

Fractional Kelly: f = fraction * f*
  We use 25% Kelly (fraction=0.25) to reduce variance.

Position size in USD = f * account_equity, capped at max_risk_per_trade.
"""

from __future__ import annotations


def full_kelly(win_prob: float, net_odds: float) -> float:
    """
    Compute full Kelly fraction.

    win_prob : probability the bet wins (0–1)
    net_odds : net decimal odds, i.e. (1/price - 1)
    Returns the fraction of bankroll to bet (can be negative → no bet).
    """
    if net_odds <= 0:
        return 0.0
    loss_prob = 1.0 - win_prob
    return (net_odds * win_prob - loss_prob) / net_odds


def fractional_kelly(
    win_prob: float,
    price: float,
    fraction: float = 0.25,
) -> float:
    """
    Compute fractional Kelly fraction of bankroll.

    win_prob : probability the chosen side wins
    price    : market price for the chosen side (0–1)
    fraction : Kelly multiplier, default 0.25
    Returns fraction of bankroll to risk (0–1). Returns 0 if no edge.
    """
    price = max(0.001, min(0.999, price))
    net_odds = (1.0 / price) - 1.0
    fk = full_kelly(win_prob, net_odds)
    if fk <= 0:
        return 0.0
    return fraction * fk


def position_size_usd(
    win_prob: float,
    price: float,
    account_equity: float,
    fraction: float = 0.25,
    max_risk_pct: float = 0.01,
) -> float:
    """
    Return dollar amount to risk on this trade.

    Applies fractional Kelly capped at max_risk_pct of account equity.
    """
    kelly_frac = fractional_kelly(win_prob, price, fraction)
    max_size = account_equity * max_risk_pct
    suggested_size = kelly_frac * account_equity
    return min(suggested_size, max_size)


def kelly_explanation(
    win_prob: float,
    price: float,
    account_equity: float,
    fraction: float = 0.25,
    max_risk_pct: float = 0.01,
) -> str:
    """Return a human-readable Kelly sizing breakdown."""
    net_odds = (1.0 / price) - 1.0
    fk = full_kelly(win_prob, net_odds)
    frac_k = fractional_kelly(win_prob, price, fraction)
    size = position_size_usd(win_prob, price, account_equity, fraction, max_risk_pct)

    return (
        f"Kelly Sizing\n"
        f"  Win prob:          {win_prob:.1%}\n"
        f"  Price (implied):   {price:.1%}\n"
        f"  Net odds:          {net_odds:.3f}x\n"
        f"  Full Kelly:        {fk:.1%} of bankroll\n"
        f"  Fractional Kelly:  {frac_k:.1%} of bankroll  ({fraction:.0%} × Full)\n"
        f"  Account equity:    ${account_equity:,.2f}\n"
        f"  Suggested size:    ${frac_k * account_equity:,.2f}\n"
        f"  Capped at {max_risk_pct:.0%}:    ${account_equity * max_risk_pct:,.2f}\n"
        f"  FINAL SIZE:        ${size:,.2f}"
    )
