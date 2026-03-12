"""Tests for the iron condor strategy module."""
from __future__ import annotations

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from datetime import datetime, timedelta, timezone
import pytest

from src.data.models import OptionQuote, OptionType, OrderSide
from src.strategy.weekly_iron_condor import (
    _nearest_delta,
    build_condor,
    calculate_risk,
    check_exit_conditions,
    generate_trade_signal,
    select_strikes,
)
from config.settings import StrategyConfig


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def make_quote(
    strike: float,
    delta: float,
    opt_type: OptionType = OptionType.CALL,
    bid: float = None,
    ask: float = None,
    expiry_days: float = 7.0,
    underlying: float = 3000.0,
    iv: float = 0.80,
) -> OptionQuote:
    # Price proportional to |delta| so short legs (higher delta) cost more than wings
    if bid is None:
        mid = max(0.002, abs(delta) * 0.5)
        bid = round(mid * 0.92, 5)
        ask = round(mid * 1.08, 5)
    now = datetime.now(timezone.utc)
    return OptionQuote(
        timestamp=now,
        instrument_name=f"ETH-{int(now.timestamp())}-{int(strike)}-{'C' if opt_type == OptionType.CALL else 'P'}",
        strike=strike,
        expiry=now + timedelta(days=expiry_days),
        option_type=opt_type,
        bid=bid,
        ask=ask,
        mark_price=(bid + ask) / 2,
        implied_volatility=iv,
        delta=delta,
        gamma=0.001,
        theta=-0.002,
        vega=0.05,
        underlying_price=underlying,
    )


def build_test_chain(spot: float = 3000.0) -> list[OptionQuote]:
    """
    Build a synthetic option chain with explicit deltas covering all strategy ranges.

    Each entry is (strike, call_delta, put_delta).
    Delta conventions: calls positive, puts negative.
    We explicitly set both sides so OTM puts have realistic small deltas.
    """
    # (strike_pct_of_spot, call_delta, put_delta)
    entries = [
        # Deep ITM calls / deep OTM puts
        (1.35, 0.99,  -0.01),
        (1.28, 0.98,  -0.02),
        # Wing call zone (3-5 delta)
        (1.22, 0.04,  -0.96),
        (1.18, 0.045, -0.955),
        # Short call zone (10-15 delta)
        (1.12, 0.10,  -0.90),
        (1.10, 0.13,  -0.87),
        # Near ATM calls
        (1.05, 0.25,  -0.75),
        (1.00, 0.50,  -0.50),   # ATM
        # Near ATM puts
        (0.95, 0.75,  -0.25),
        # Short put zone (10-15 delta)
        (0.90, 0.87,  -0.13),
        (0.88, 0.90,  -0.10),
        # Wing put zone (3-5 delta)
        (0.82, 0.955, -0.045),
        (0.78, 0.96,  -0.04),
        # Deep OTM puts / deep ITM calls
        (0.72, 0.98,  -0.02),
        (0.65, 0.99,  -0.01),
    ]

    chain = []
    for pct, call_delta, put_delta in entries:
        strike = round(spot * pct, 0)
        chain.append(make_quote(strike, call_delta, OptionType.CALL, underlying=spot))
        chain.append(make_quote(strike, put_delta, OptionType.PUT, underlying=spot))

    return chain


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_nearest_delta_finds_correct_strike():
    chain = build_test_chain()
    calls = [q for q in chain if q.option_type == OptionType.CALL]
    result = _nearest_delta(calls, 0.10, 0.15, use_abs=True)
    assert result is not None
    assert 0.05 <= abs(result.delta) <= 0.25


def test_select_strikes_returns_four_legs():
    config = StrategyConfig()
    chain = build_test_chain()
    strikes = select_strikes(chain, config)
    assert strikes is not None
    assert "short_call" in strikes
    assert "long_call" in strikes
    assert "short_put" in strikes
    assert "long_put" in strikes


def test_select_strikes_call_wing_otm():
    config = StrategyConfig()
    chain = build_test_chain()
    strikes = select_strikes(chain, config)
    assert strikes is not None
    # Wing should be further OTM than short
    assert strikes["long_call"].strike >= strikes["short_call"].strike
    assert strikes["long_put"].strike <= strikes["short_put"].strike


def test_build_condor_credit_positive():
    config = StrategyConfig()
    chain = build_test_chain()
    strikes = select_strikes(chain, config)
    assert strikes is not None
    condor = build_condor(strikes)
    assert condor.credit_received > 0


def test_build_condor_max_loss_positive():
    condor = _make_condor_direct()
    assert condor.max_loss > 0


def _make_condor_direct():
    """Build a condor with explicit quotes, bypassing select_strikes."""
    strikes = {
        "short_call": make_quote(3300, 0.12, OptionType.CALL, bid=0.05, ask=0.07),
        "long_call":  make_quote(3500, 0.04, OptionType.CALL, bid=0.01, ask=0.015),
        "short_put":  make_quote(2700, -0.12, OptionType.PUT, bid=0.05, ask=0.07),
        "long_put":   make_quote(2500, -0.04, OptionType.PUT, bid=0.01, ask=0.015),
    }
    return build_condor(strikes)


def test_calculate_risk_reward_ratio():
    condor = _make_condor_direct()
    risk = calculate_risk(condor, account_size=2200.0)
    assert risk["reward_to_risk"] > 0
    assert risk["max_loss"] == condor.max_loss


def test_generate_signal_skips_low_iv():
    config = StrategyConfig()
    chain = build_test_chain()
    signal = generate_trade_signal(
        chain=chain,
        config=config,
        iv_percentile=30.0,  # below threshold
        daily_move_pct=1.0,
        has_open_position=False,
    )
    assert signal["action"] == "skip"
    assert "IV percentile" in signal["reason"]


def test_generate_signal_skips_high_move():
    config = StrategyConfig()
    chain = build_test_chain()
    signal = generate_trade_signal(
        chain=chain,
        config=config,
        iv_percentile=70.0,
        daily_move_pct=8.0,  # above threshold
        has_open_position=False,
    )
    assert signal["action"] == "skip"
    assert "Daily move" in signal["reason"]


def test_generate_signal_skips_open_position():
    config = StrategyConfig()
    chain = build_test_chain()
    signal = generate_trade_signal(
        chain=chain,
        config=config,
        iv_percentile=70.0,
        daily_move_pct=1.0,
        has_open_position=True,
    )
    assert signal["action"] == "skip"


def test_generate_signal_enters_when_conditions_met():
    config = StrategyConfig()
    chain = build_test_chain()
    signal = generate_trade_signal(
        chain=chain,
        config=config,
        iv_percentile=70.0,
        daily_move_pct=1.0,
        has_open_position=False,
    )
    assert signal["action"] == "enter"
    assert signal["strikes"] is not None


def test_take_profit_exit():
    config = StrategyConfig()
    chain = build_test_chain()
    strikes = select_strikes(chain, config)
    condor = build_condor(strikes)

    # Simulate quotes at 50% of entry price (profit scenario)
    quotes = {}
    for leg in condor.legs:
        q = make_quote(leg.strike, 0.05, leg.option_type)
        q2 = OptionQuote(
            timestamp=q.timestamp,
            instrument_name=leg.instrument_name,
            strike=leg.strike,
            expiry=leg.expiry,
            option_type=leg.option_type,
            bid=leg.entry_price * 0.25,
            ask=leg.entry_price * 0.27,
            mark_price=leg.entry_price * 0.26,
            implied_volatility=0.5,
            delta=q.delta,
            gamma=0.0,
            theta=0.0,
            vega=0.0,
            underlying_price=3000.0,
        )
        quotes[leg.instrument_name] = q2

    reason = check_exit_conditions(condor, quotes, config)
    assert reason is not None and "take_profit" in reason


def test_stop_loss_exit():
    config = StrategyConfig()
    condor = _make_condor_direct()

    # Simulate adverse move: short legs blow up to 10x, long legs stay cheap
    # This creates a large unrealized loss > 2x credit
    quotes = {}
    for leg in condor.legs:
        if leg.side == OrderSide.SELL:
            # Short legs are deep ITM — very expensive to close
            adverse_price = condor.credit_received * 5.0 + leg.entry_price
        else:
            # Long legs (wings) are still cheap
            adverse_price = leg.entry_price * 0.5
        q2 = OptionQuote(
            timestamp=datetime.now(timezone.utc),
            instrument_name=leg.instrument_name,
            strike=leg.strike,
            expiry=leg.expiry,
            option_type=leg.option_type,
            bid=round(adverse_price * 0.95, 5),
            ask=round(adverse_price * 1.05, 5),
            mark_price=adverse_price,
            implied_volatility=1.5,
            delta=0.5,
            gamma=0.0,
            theta=0.0,
            vega=0.0,
            underlying_price=3500.0,
        )
        quotes[leg.instrument_name] = q2

    reason = check_exit_conditions(condor, quotes, config)
    assert reason is not None and "stop_loss" in reason
