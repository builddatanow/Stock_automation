from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np
import pandas as pd

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
# Strike selection
# ---------------------------------------------------------------------------

def select_strikes(
    chain: list[OptionQuote],
    config: StrategyConfig,
    as_of: Optional[datetime] = None,
) -> Optional[dict[str, OptionQuote]]:
    """
    Select the four legs of an iron condor from the option chain.

    Returns a dict with keys:
        short_call, long_call, short_put, long_put
    or None if selection criteria cannot be satisfied.
    """
    if as_of is None:
        as_of = datetime.now(timezone.utc)

    # Filter to the target expiry window
    target_expiries = [
        q.expiry
        for q in chain
        if config.target_dte_min <= q.dte <= config.target_dte_max
    ]
    if not target_expiries:
        logger.warning("No expiry found in DTE window [%d, %d]", config.target_dte_min, config.target_dte_max)
        return None

    # Pick the expiry closest to the middle of the window
    target_dte_mid = (config.target_dte_min + config.target_dte_max) / 2
    best_expiry = min(
        set(target_expiries),
        key=lambda e: abs((e - as_of).total_seconds() / 86400 - target_dte_mid),
    )

    expiry_chain = [q for q in chain if q.expiry == best_expiry]
    calls = [q for q in expiry_chain if q.option_type == OptionType.CALL and q.bid > 0]
    puts = [q for q in expiry_chain if q.option_type == OptionType.PUT and q.bid > 0]

    if not calls or not puts:
        logger.warning("Insufficient call/put data for expiry %s", best_expiry)
        return None

    short_call = _nearest_delta(calls, config.short_delta_min, config.short_delta_max, use_abs=True)
    short_put = _nearest_delta(puts, config.short_delta_min, config.short_delta_max, use_abs=True)

    if short_call is None or short_put is None:
        logger.warning("Could not find short strikes in delta range [%.2f, %.2f]",
                       config.short_delta_min, config.short_delta_max)
        return None

    # Wing: OTM from the short strikes
    wing_calls = [q for q in calls if q.strike > short_call.strike]
    wing_puts = [q for q in puts if q.strike < short_put.strike]

    long_call = _nearest_delta(wing_calls, config.wing_delta_min, config.wing_delta_max, use_abs=True)
    long_put = _nearest_delta(wing_puts, config.wing_delta_min, config.wing_delta_max, use_abs=True)

    if long_call is None or long_put is None:
        logger.warning("Could not find wing strikes in delta range [%.2f, %.2f]",
                       config.wing_delta_min, config.wing_delta_max)
        return None

    logger.info(
        "Selected strikes | SC: %.0f  LC: %.0f  SP: %.0f  LP: %.0f | Expiry: %s",
        short_call.strike, long_call.strike, short_put.strike, long_put.strike,
        best_expiry.date(),
    )

    return {
        "short_call": short_call,
        "long_call": long_call,
        "short_put": short_put,
        "long_put": long_put,
    }


def _nearest_delta(
    options: list[OptionQuote],
    delta_min: float,
    delta_max: float,
    use_abs: bool = True,
) -> Optional[OptionQuote]:
    """Return option whose |delta| is closest to the center of [delta_min, delta_max]."""
    if not options:
        return None
    target = (delta_min + delta_max) / 2
    candidates = [
        q for q in options
        if delta_min <= (abs(q.delta) if use_abs else q.delta) <= delta_max
    ]
    if not candidates:
        # Relax: nearest option outside range
        candidates = options
    if not candidates:
        return None
    return min(candidates, key=lambda q: abs((abs(q.delta) if use_abs else q.delta) - target))


# ---------------------------------------------------------------------------
# Condor construction
# ---------------------------------------------------------------------------

def build_condor(
    strikes: dict[str, OptionQuote],
    quantity: float = 1.0,
    fill_model: str = "mid",
) -> IronCondor:
    """
    Build an IronCondor from selected strike quotes.

    fill_model: "mid" | "bid_ask"
        mid    = fill at mid price
        bid_ask = sell at bid, buy at ask (conservative)
    """

    def _fill_price(quote: OptionQuote, side: OrderSide) -> float:
        if fill_model == "bid_ask":
            return quote.bid if side == OrderSide.SELL else quote.ask
        return quote.mid

    sc = strikes["short_call"]
    lc = strikes["long_call"]
    sp = strikes["short_put"]
    lp = strikes["long_put"]

    sc_price = _fill_price(sc, OrderSide.SELL)
    lc_price = _fill_price(lc, OrderSide.BUY)
    sp_price = _fill_price(sp, OrderSide.SELL)
    lp_price = _fill_price(lp, OrderSide.BUY)

    call_credit = sc_price - lc_price
    put_credit = sp_price - lp_price
    credit = (call_credit + put_credit) * quantity

    call_spread_width = lc.strike - sc.strike
    put_spread_width = sp.strike - lp.strike

    # Max loss on each side; can only lose on one side at expiry
    call_max_loss = max(call_spread_width - call_credit, 0)
    put_max_loss = max(put_spread_width - put_credit, 0)
    max_loss = max(call_max_loss, put_max_loss) * quantity

    now = sc.timestamp

    condor = IronCondor(
        id=str(uuid.uuid4())[:8],
        entry_time=now,
        underlying_price_at_entry=sc.underlying_price,
        short_call=Leg(
            instrument_name=sc.instrument_name,
            strike=sc.strike,
            expiry=sc.expiry,
            option_type=OptionType.CALL,
            side=OrderSide.SELL,
            quantity=quantity,
            entry_price=sc_price,
            delta=sc.delta,
            implied_volatility=sc.implied_volatility,
        ),
        long_call=Leg(
            instrument_name=lc.instrument_name,
            strike=lc.strike,
            expiry=lc.expiry,
            option_type=OptionType.CALL,
            side=OrderSide.BUY,
            quantity=quantity,
            entry_price=lc_price,
            delta=lc.delta,
            implied_volatility=lc.implied_volatility,
        ),
        short_put=Leg(
            instrument_name=sp.instrument_name,
            strike=sp.strike,
            expiry=sp.expiry,
            option_type=OptionType.PUT,
            side=OrderSide.SELL,
            quantity=quantity,
            entry_price=sp_price,
            delta=sp.delta,
            implied_volatility=sp.implied_volatility,
        ),
        long_put=Leg(
            instrument_name=lp.instrument_name,
            strike=lp.strike,
            expiry=lp.expiry,
            option_type=OptionType.PUT,
            side=OrderSide.BUY,
            quantity=quantity,
            entry_price=lp_price,
            delta=lp.delta,
            implied_volatility=lp.implied_volatility,
        ),
        quantity=quantity,
        credit_received=credit,
        max_loss=max_loss,
    )
    return condor


# ---------------------------------------------------------------------------
# Risk checks
# ---------------------------------------------------------------------------

def calculate_risk(condor: IronCondor, account_size: float) -> dict:
    return {
        "credit_received": condor.credit_received,
        "max_loss": condor.max_loss,
        "risk_pct_of_account": condor.max_loss / account_size * 100,
        "reward_to_risk": condor.credit_received / condor.max_loss if condor.max_loss > 0 else 0,
    }


# ---------------------------------------------------------------------------
# Trade signals
# ---------------------------------------------------------------------------

def generate_trade_signal(
    chain: list[OptionQuote],
    config: StrategyConfig,
    iv_percentile: float,
    daily_move_pct: float,
    has_open_position: bool,
) -> dict:
    """
    Returns signal dict:
        action: "enter" | "skip"
        reason: human-readable string
        strikes: dict | None
    """
    if has_open_position:
        return {"action": "skip", "reason": "position already open", "strikes": None}

    if iv_percentile < config.iv_percentile_min:
        return {
            "action": "skip",
            "reason": f"IV percentile {iv_percentile:.1f} < threshold {config.iv_percentile_min}",
            "strikes": None,
        }

    if abs(daily_move_pct) > config.max_daily_move_pct:
        return {
            "action": "skip",
            "reason": f"Daily move {daily_move_pct:.1f}% > threshold {config.max_daily_move_pct}%",
            "strikes": None,
        }

    strikes = select_strikes(chain, config)
    if strikes is None:
        return {"action": "skip", "reason": "could not find suitable strikes", "strikes": None}

    return {"action": "enter", "reason": "all filters passed", "strikes": strikes}


# ---------------------------------------------------------------------------
# Exit logic
# ---------------------------------------------------------------------------

def check_exit_conditions(
    condor: IronCondor,
    current_quotes: dict[str, OptionQuote],
    config: StrategyConfig,
    as_of: Optional[datetime] = None,
) -> Optional[str]:
    """
    Return exit reason string if condor should be closed, else None.
    """
    if as_of is None:
        as_of = datetime.now(timezone.utc)

    # DTE check
    expiry = condor.short_call.expiry
    dte = (expiry - as_of).total_seconds() / 86400
    if dte <= config.close_dte:
        return f"close_before_expiry (dte={dte:.1f})"

    upnl = condor.unrealized_pnl(current_quotes)

    # Take profit: 50% of initial credit
    if upnl >= condor.credit_received * config.take_profit_pct:
        return f"take_profit (pnl={upnl:.4f} >= {condor.credit_received * config.take_profit_pct:.4f})"

    # Stop loss: 2x initial credit
    if upnl <= -condor.credit_received * config.stop_loss_multiplier:
        return f"stop_loss (pnl={upnl:.4f} <= -{condor.credit_received * config.stop_loss_multiplier:.4f})"

    return None
