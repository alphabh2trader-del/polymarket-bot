"""
Expected Value calculator for binary prediction markets.

For a binary contract priced at p (= implied probability):
  - Buying YES at price p: pays $1 if YES, $0 if NO
  - EV_YES  = estimated_prob * (1 - p) - (1 - estimated_prob) * p
             = estimated_prob - p                              (simplified)
  - Edge    = estimated_prob - implied_prob (for YES side)

For the NO side (buying NO at price q = 1 - p):
  - EV_NO   = (1 - estimated_prob) - q
             = (1 - estimated_prob) - (1 - implied_prob)
             = implied_prob - estimated_prob
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class EVResult:
    side: str               # "YES" or "NO"
    implied_prob: float     # market price for that side
    estimated_prob: float   # our probability that this side wins
    edge: float             # estimated_prob - implied_prob
    ev: float               # expected value as a fraction of stake
    is_opportunity: bool    # True if EV exceeds threshold

    def __str__(self) -> str:
        return (
            f"{self.side}  implied={self.implied_prob:.1%}  "
            f"estimated={self.estimated_prob:.1%}  "
            f"edge={self.edge:+.1%}  EV={self.ev:+.1%}"
        )


def calculate_ev(
    implied_prob: float,
    estimated_prob: float,
    min_ev_threshold: float = 0.05,
    side: str = "YES",
) -> EVResult:
    """
    Calculate EV for one side of a binary market.

    implied_prob : market price for the chosen side (0–1)
    estimated_prob : our probability that the chosen side wins (0–1)
    """
    implied_prob = max(0.001, min(0.999, implied_prob))
    estimated_prob = max(0.001, min(0.999, estimated_prob))

    # EV = prob_win * net_gain_per_unit - prob_lose * 1
    # net_gain_per_unit = (1 / implied_prob) - 1   (decimal odds - 1)
    odds = (1.0 / implied_prob) - 1.0
    ev = estimated_prob * odds - (1.0 - estimated_prob)
    edge = estimated_prob - implied_prob

    return EVResult(
        side=side,
        implied_prob=implied_prob,
        estimated_prob=estimated_prob,
        edge=edge,
        ev=ev,
        is_opportunity=ev >= min_ev_threshold and edge > 0,
    )


def find_best_opportunity(
    yes_price: float,
    no_price: float,
    estimated_yes_prob: float,
    min_ev_threshold: float = 0.05,
) -> EVResult:
    """
    Given YES and NO prices and our YES probability estimate,
    return the better side (higher EV) if either clears the threshold.
    Returns the best EVResult regardless of whether it's an opportunity.
    """
    yes_result = calculate_ev(
        implied_prob=yes_price,
        estimated_prob=estimated_yes_prob,
        min_ev_threshold=min_ev_threshold,
        side="YES",
    )
    no_result = calculate_ev(
        implied_prob=no_price,
        estimated_prob=1.0 - estimated_yes_prob,
        min_ev_threshold=min_ev_threshold,
        side="NO",
    )

    if yes_result.ev >= no_result.ev:
        return yes_result
    return no_result


def ev_explanation(result: EVResult) -> str:
    """Return a human-readable EV breakdown string."""
    odds = (1.0 / result.implied_prob) - 1.0
    return (
        f"Side: {result.side}\n"
        f"  Market price (implied prob): {result.implied_prob:.1%}\n"
        f"  Our estimated probability:   {result.estimated_prob:.1%}\n"
        f"  Edge:                        {result.edge:+.1%}\n"
        f"  Decimal odds:                {1/result.implied_prob:.3f}x\n"
        f"  EV per $1 staked:            {result.ev:+.3f}\n"
        f"  EV%:                         {result.ev:.1%}\n"
        f"\nCalculation: {result.estimated_prob:.1%} × {odds:.3f} − {1-result.estimated_prob:.1%} = {result.ev:.3f}"
    )
