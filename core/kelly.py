"""Kelly criterion for binary outcome position sizing."""

from core.constants import DEFAULT_KELLY_FRACTION, TAKER_FEE_BPS


def kelly_fraction(
    true_prob: float,
    market_price: float,
    fee_bps: int = TAKER_FEE_BPS,
) -> float:
    """Calculate Kelly fraction for a binary outcome bet.

    For buying YES at price p when true probability is q:
      Win payout = (1 - p) per share (pay p, receive 1 if correct)
      Loss = p per share
      Kelly f* = (q * (1-p) - (1-q) * p) / (1 - p)
             = (q - p) / (1 - p)

    Adjusted for fees on entry.

    Returns:
        Fraction of bankroll to bet (0.0 to 1.0). Negative means don't bet.
    """
    if market_price <= 0 or market_price >= 1:
        return 0.0
    if true_prob <= 0 or true_prob >= 1:
        return 0.0

    # Adjust for taker fee
    fee_rate = fee_bps / 10000
    effective_price = market_price * (1 + fee_rate)

    if effective_price >= 1:
        return 0.0

    # Kelly for binary: f* = (q - p) / (1 - p)
    # where q = true_prob, p = effective_price
    edge = true_prob - effective_price
    if edge <= 0:
        return 0.0

    f = edge / (1 - effective_price)
    return max(0.0, min(1.0, f))


def size_position(
    bankroll: float,
    true_prob: float,
    market_price: float,
    kelly_mult: float = DEFAULT_KELLY_FRACTION,
    max_position: float = 0.0,
    fee_bps: int = TAKER_FEE_BPS,
) -> float:
    """Calculate position size in USDC using fractional Kelly.

    Args:
        bankroll: Total available capital in USDC
        true_prob: Model's estimated probability (0-1)
        market_price: Current market price (0-1)
        kelly_mult: Fraction of full Kelly to use (default 0.25 = quarter Kelly)
        max_position: Maximum position size in USDC (0 = no cap)
        fee_bps: Fee in basis points

    Returns:
        Position size in USDC
    """
    if bankroll <= 0:
        return 0.0

    f = kelly_fraction(true_prob, market_price, fee_bps)
    if f <= 0:
        return 0.0

    size = bankroll * f * kelly_mult

    if max_position > 0:
        size = min(size, max_position)

    # Round to 2 decimal places (USDC precision)
    return round(size, 2)


def expected_value(
    true_prob: float,
    market_price: float,
    size: float,
    fee_bps: int = TAKER_FEE_BPS,
) -> float:
    """Calculate expected value of a position.

    Args:
        true_prob: Model's estimated probability
        market_price: Price per share
        size: Number of shares
        fee_bps: Fee in basis points

    Returns:
        Expected profit/loss in USDC
    """
    fee_rate = fee_bps / 10000
    cost = size * market_price * (1 + fee_rate)
    ev_payout = true_prob * size  # Expected payout (1 per share if correct)
    return ev_payout - cost
