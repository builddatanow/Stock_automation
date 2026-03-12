"""
EMA-Based Directional Vertical Spread Strategy
===============================================

Signal logic:
  Fast EMA (default 9) crosses above Slow EMA (default 21)  --> BULLISH
    => Sell Bull Put Spread (collect credit, profit if ETH stays flat or rises)

  Fast EMA crosses below Slow EMA                           --> BEARISH
    => Sell Bear Call Spread (collect credit, profit if ETH stays flat or falls)

Strike selection:
  Short strike : 20-30 delta OTM from current spot
  Long  strike :  8-12 delta (wing, further OTM)

Position:  defined-risk credit spread (2 legs only)
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np

from src.data.models import (
    IronCondor,
    Leg,
    OptionQuote,
    OptionType,
    OrderSide,
    PositionStatus,
)
from config.settings import StrategyConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# EMA computation
# ---------------------------------------------------------------------------

def compute_ema(prices: list[float], period: int) -> list[float]:
    """Return EMA series of same length as prices (first N-1 values = SMA warmup)."""
    if len(prices) < period:
        return prices[:]
    k = 2.0 / (period + 1)
    ema = [sum(prices[:period]) / period]
    for p in prices[period:]:
        ema.append(p * k + ema[-1] * (1 - k))
    # Pad front so output length matches input
    pad = len(prices) - len(ema)
    return [float("nan")] * pad + ema


def get_ema_signal(
    price_history: list[float],
    fast_period: int = 9,
    slow_period: int = 21,
) -> str:
    """
    Return signal based on EMA crossover + price vs slow EMA.

    Returns: "bullish" | "bearish" | "neutral"
    """
    if len(price_history) < slow_period + 2:
        return "neutral"

    fast = compute_ema(price_history, fast_period)
    slow = compute_ema(price_history, slow_period)

    # Last two values for crossover detection
    f0, f1 = fast[-2], fast[-1]
    s0, s1 = slow[-2], slow[-1]
    price  = price_history[-1]

    if any(np.isnan(v) for v in [f0, f1, s0, s1]):
        return "neutral"

    golden_cross = f0 <= s0 and f1 > s1   # fast crossed above slow
    death_cross  = f0 >= s0 and f1 < s1   # fast crossed below slow

    fast_above_slow = f1 > s1
    price_above_slow = price > s1

    if fast_above_slow and price_above_slow:
        return "bullish"
    elif not fast_above_slow and not price_above_slow:
        return "bearish"
    return "neutral"


def ema_trend_strength(price_history: list[float], period: int = 21) -> float:
    """
    Return normalised distance of price from slow EMA.
    Positive = above EMA, Negative = below EMA.
    """
    if len(price_history) < period:
        return 0.0
    ema = compute_ema(price_history, period)
    current_ema = ema[-1]
    if np.isnan(current_ema) or current_ema == 0:
        return 0.0
    return (price_history[-1] - current_ema) / current_ema


# ---------------------------------------------------------------------------
# Strike selection for vertical spreads
# ---------------------------------------------------------------------------

def select_spread_strikes(
    chain: list[OptionQuote],
    signal: str,
    config: "EMASpreadConfig",
    as_of: Optional[datetime] = None,
) -> Optional[dict[str, OptionQuote]]:
    """
    Select two strikes for a directional vertical spread.

    Bullish -> Bull Put Spread: sell higher put, buy lower put
    Bearish -> Bear Call Spread: sell lower call, buy higher call

    Returns dict with "short" and "long" OptionQuote, or None.
    """
    if signal not in ("bullish", "bearish"):
        return None

    if as_of is None:
        as_of = datetime.now(timezone.utc)

    # Filter to target expiry window
    valid_expiries = [
        q.expiry for q in chain
        if config.target_dte_min <= q.dte <= config.target_dte_max
    ]
    if not valid_expiries:
        logger.warning("No expiry found in DTE window [%d, %d]", config.target_dte_min, config.target_dte_max)
        return None

    target_dte_mid = (config.target_dte_min + config.target_dte_max) / 2
    best_expiry = min(
        set(valid_expiries),
        key=lambda e: abs((e - as_of).total_seconds() / 86400 - target_dte_mid),
    )

    expiry_chain = [q for q in chain if q.expiry == best_expiry and q.bid > 0]

    if signal == "bullish":
        # Bull Put Spread: OTM puts below current spot
        puts = [q for q in expiry_chain if q.option_type == OptionType.PUT]
        if not puts:
            logger.warning("No put options found for bull put spread")
            return None

        # Short put: 20-30 delta OTM
        short = _nearest_delta(puts, config.short_delta_min, config.short_delta_max)
        if short is None:
            return None

        # Long put: further OTM (lower strike, smaller delta)
        wing_puts = [q for q in puts if q.strike < short.strike]
        long = _nearest_delta(wing_puts, config.wing_delta_min, config.wing_delta_max)
        if long is None:
            logger.warning("No wing put found for bull put spread")
            return None

        logger.info(
            "BULLISH | Bull Put Spread | Short Put: %.0f (d=%.2f) | Long Put: %.0f (d=%.2f) | Exp: %s",
            short.strike, short.delta, long.strike, long.delta, best_expiry.date(),
        )
        return {"short": short, "long": long, "spread_type": "bull_put"}

    else:  # bearish
        # Bear Call Spread: OTM calls above current spot
        calls = [q for q in expiry_chain if q.option_type == OptionType.CALL]
        if not calls:
            logger.warning("No call options found for bear call spread")
            return None

        # Short call: 20-30 delta OTM
        short = _nearest_delta(calls, config.short_delta_min, config.short_delta_max)
        if short is None:
            return None

        # Long call: further OTM (higher strike, smaller delta)
        wing_calls = [q for q in calls if q.strike > short.strike]
        long = _nearest_delta(wing_calls, config.wing_delta_min, config.wing_delta_max)
        if long is None:
            logger.warning("No wing call found for bear call spread")
            return None

        logger.info(
            "BEARISH | Bear Call Spread | Short Call: %.0f (d=%.2f) | Long Call: %.0f (d=%.2f) | Exp: %s",
            short.strike, short.delta, long.strike, long.delta, best_expiry.date(),
        )
        return {"short": short, "long": long, "spread_type": "bear_call"}


def _nearest_delta(
    options: list[OptionQuote],
    delta_min: float,
    delta_max: float,
) -> Optional[OptionQuote]:
    if not options:
        return None
    target = (delta_min + delta_max) / 2
    candidates = [q for q in options if delta_min <= abs(q.delta) <= delta_max]
    if not candidates:
        candidates = options  # relax to nearest
    if not candidates:
        return None
    return min(candidates, key=lambda q: abs(abs(q.delta) - target))


# ---------------------------------------------------------------------------
# Build spread (reuses IronCondor with 2 active legs)
# ---------------------------------------------------------------------------

def build_spread(
    strikes: dict,
    quantity: float = 1.0,
    fill_model: str = "mid",
) -> IronCondor:
    """
    Pack a 2-leg vertical spread into an IronCondor dataclass.

    For a bull put spread:
      short_put = the short put leg
      long_put  = the long put leg
      short_call / long_call = dummy zero-value legs (not traded)

    For a bear call spread:
      short_call = the short call leg
      long_call  = the long call leg
      short_put / long_put = dummy zero-value legs
    """
    def fill(q: OptionQuote, side: OrderSide) -> float:
        if fill_model == "bid_ask":
            return q.bid if side == OrderSide.SELL else q.ask
        return q.mid

    short_q = strikes["short"]
    long_q  = strikes["long"]
    spread_type = strikes["spread_type"]

    short_price = fill(short_q, OrderSide.SELL)
    long_price  = fill(long_q,  OrderSide.BUY)

    credit = (short_price - long_price) * quantity

    if spread_type == "bull_put":
        spread_width = short_q.strike - long_q.strike
    else:  # bear_call
        spread_width = long_q.strike - short_q.strike

    max_loss = max(spread_width - credit / quantity, 0) * quantity

    now = short_q.timestamp

    # Dummy call/put depending on which side is used
    dummy_expiry = short_q.expiry
    dummy_ts     = now
    if spread_type == "bull_put":
        active_short_put = Leg(
            instrument_name=short_q.instrument_name,
            strike=short_q.strike, expiry=short_q.expiry,
            option_type=OptionType.PUT, side=OrderSide.SELL,
            quantity=quantity, entry_price=short_price,
            delta=short_q.delta, implied_volatility=short_q.implied_volatility,
        )
        active_long_put = Leg(
            instrument_name=long_q.instrument_name,
            strike=long_q.strike, expiry=long_q.expiry,
            option_type=OptionType.PUT, side=OrderSide.BUY,
            quantity=quantity, entry_price=long_price,
            delta=long_q.delta, implied_volatility=long_q.implied_volatility,
        )
        # Stub call legs (price=0, will not affect PnL)
        stub_call = _stub_leg(OptionType.CALL, dummy_ts, dummy_expiry)

        condor = IronCondor(
            id=str(uuid.uuid4())[:8],
            entry_time=now,
            underlying_price_at_entry=short_q.underlying_price,
            short_call=stub_call,
            long_call=stub_call,
            short_put=active_short_put,
            long_put=active_long_put,
            quantity=quantity,
            credit_received=credit,
            max_loss=max_loss,
        )

    else:  # bear_call
        active_short_call = Leg(
            instrument_name=short_q.instrument_name,
            strike=short_q.strike, expiry=short_q.expiry,
            option_type=OptionType.CALL, side=OrderSide.SELL,
            quantity=quantity, entry_price=short_price,
            delta=short_q.delta, implied_volatility=short_q.implied_volatility,
        )
        active_long_call = Leg(
            instrument_name=long_q.instrument_name,
            strike=long_q.strike, expiry=long_q.expiry,
            option_type=OptionType.CALL, side=OrderSide.BUY,
            quantity=quantity, entry_price=long_price,
            delta=long_q.delta, implied_volatility=long_q.implied_volatility,
        )
        stub_put = _stub_leg(OptionType.PUT, dummy_ts, dummy_expiry)

        condor = IronCondor(
            id=str(uuid.uuid4())[:8],
            entry_time=now,
            underlying_price_at_entry=short_q.underlying_price,
            short_call=active_short_call,
            long_call=active_long_call,
            short_put=stub_put,
            long_put=stub_put,
            quantity=quantity,
            credit_received=credit,
            max_loss=max_loss,
        )

    # Tag spread type on the object for reporting
    condor.__dict__["spread_type"] = spread_type
    return condor


def _stub_leg(opt_type: OptionType, ts: datetime, expiry: datetime) -> Leg:
    return Leg(
        instrument_name="STUB",
        strike=0.0, expiry=expiry, option_type=opt_type,
        side=OrderSide.BUY, quantity=0.0, entry_price=0.0,
    )


# ---------------------------------------------------------------------------
# Trade signal
# ---------------------------------------------------------------------------

def generate_trade_signal(
    chain: list[OptionQuote],
    config: "EMASpreadConfig",
    price_history: list[float],
    iv_percentile: float,
    has_open_position: bool,
    current_signal: Optional[str] = None,
) -> dict:
    """
    Returns:
        action : "enter" | "skip"
        signal : "bullish" | "bearish" | "neutral"
        strikes: dict | None
        reason : str
    """
    if has_open_position:
        return {"action": "skip", "signal": "neutral",
                "reason": "position already open", "strikes": None}

    signal = get_ema_signal(price_history, config.fast_ema, config.slow_ema)

    if signal == "neutral":
        return {"action": "skip", "signal": signal,
                "reason": "EMA signal is neutral (no clear trend)", "strikes": None}

    if iv_percentile < config.iv_percentile_min:
        return {"action": "skip", "signal": signal,
                "reason": f"IV percentile {iv_percentile:.1f} below {config.iv_percentile_min}",
                "strikes": None}

    # Trend strength filter: require meaningful EMA separation to avoid whipsaws
    strength = abs(ema_trend_strength(price_history, config.slow_ema))
    if strength < config.min_trend_strength:
        return {"action": "skip", "signal": signal,
                "reason": f"Trend too weak ({strength*100:.2f}% < {config.min_trend_strength*100:.2f}%)",
                "strikes": None}

    strikes = select_spread_strikes(chain, signal, config)
    if strikes is None:
        return {"action": "skip", "signal": signal,
                "reason": "could not find suitable strikes", "strikes": None}

    return {"action": "enter", "signal": signal,
            "reason": f"EMA signal: {signal} | strength: {strength*100:.2f}%",
            "strikes": strikes}


# ---------------------------------------------------------------------------
# Exit logic
# ---------------------------------------------------------------------------

def check_exit_conditions(
    spread: IronCondor,
    current_quotes: dict[str, OptionQuote],
    config: "EMASpreadConfig",
    as_of: Optional[datetime] = None,
    current_signal: Optional[str] = None,
) -> Optional[str]:
    """Return exit reason string if spread should be closed, else None."""
    if as_of is None:
        as_of = datetime.now(timezone.utc)

    # Work out the active leg's expiry
    spread_type = spread.__dict__.get("spread_type", "bull_put")
    if spread_type == "bull_put":
        expiry = spread.short_put.expiry
    else:
        expiry = spread.short_call.expiry

    dte = (expiry - as_of).total_seconds() / 86400
    if dte <= config.close_dte:
        return f"close_before_expiry (dte={dte:.1f})"

    upnl = spread.unrealized_pnl(current_quotes)

    # Take profit
    if upnl >= spread.credit_received * config.take_profit_pct:
        return f"take_profit (pnl={upnl:.4f} >= {spread.credit_received*config.take_profit_pct:.4f})"

    # Stop loss
    if upnl <= -spread.credit_received * config.stop_loss_multiplier:
        return f"stop_loss (pnl={upnl:.4f} <= -{spread.credit_received*config.stop_loss_multiplier:.4f})"

    # Signal reversal exit
    if current_signal is not None:
        spread_type = spread.__dict__.get("spread_type", "")
        if spread_type == "bull_put" and current_signal == "bearish":
            return "signal_reversal (bull_put closed on bearish signal)"
        if spread_type == "bear_call" and current_signal == "bullish":
            return "signal_reversal (bear_call closed on bullish signal)"

    return None


# ---------------------------------------------------------------------------
# Config dataclass
# ---------------------------------------------------------------------------

from dataclasses import dataclass, field

@dataclass
class EMASpreadConfig:
    # EMA parameters
    fast_ema: int = 9
    slow_ema: int = 21

    # Expiry targeting
    target_dte_min: int = 5
    target_dte_max: int = 10

    # Strike deltas — 30-40 delta short strikes collect more premium
    short_delta_min: float = 0.30
    short_delta_max: float = 0.40
    wing_delta_min:  float = 0.10
    wing_delta_max:  float = 0.15

    # Exit rules — 1.5× SL cuts losses faster
    take_profit_pct:      float = 0.50
    stop_loss_multiplier: float = 1.5
    close_dte:            int   = 1

    # Entry filters
    iv_percentile_min:  float = 30.0
    min_trend_strength: float = 0.005  # |EMA9-EMA21|/price must exceed 0.5%

    # Iron condor fallback when IV is low or signal is neutral
    condor_on_low_iv:    bool  = True
    ic_short_delta_min:  float = 0.15
    ic_short_delta_max:  float = 0.25
    ic_wing_delta_min:   float = 0.05
    ic_wing_delta_max:   float = 0.10

    # Entry frequency
    entry_every_day: bool = False  # if True, enter any weekday (not just Mondays)

    # Risk
    account_size:            float = 2200.0
    max_risk_per_trade_pct:  float = 0.10
