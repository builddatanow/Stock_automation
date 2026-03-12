"""Tests for the risk manager module."""
from __future__ import annotations

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from datetime import datetime, timezone
import pytest

from src.data.models import AccountState, PositionStatus
from src.risk.risk_manager import RiskManager, RiskViolation
from config.settings import RiskConfig
from tests.test_strategy import build_test_chain, select_strikes, build_condor


def make_risk_manager() -> RiskManager:
    cfg = RiskConfig(
        account_size=2200.0,
        max_risk_per_trade_pct=0.10,
        max_open_positions=1,
        daily_loss_limit_pct=0.05,
    )
    return RiskManager(cfg)


def make_account(equity: float = 2200.0) -> AccountState:
    return AccountState(balance=equity, equity=equity)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_kill_switch_prevents_new_trades():
    rm = make_risk_manager()
    rm.activate_kill_switch("test")
    assert rm.is_halted

    chain = build_test_chain()
    from config.settings import StrategyConfig
    strikes = select_strikes(chain, StrategyConfig())
    condor = build_condor(strikes)

    with pytest.raises(RiskViolation, match="Kill switch"):
        rm.check_new_trade(condor, 0, make_account())


def test_max_positions_enforced():
    rm = make_risk_manager()
    chain = build_test_chain()
    from config.settings import StrategyConfig
    strikes = select_strikes(chain, StrategyConfig())
    condor = build_condor(strikes)

    with pytest.raises(RiskViolation, match="Max open positions"):
        rm.check_new_trade(condor, 1, make_account())


def test_max_risk_per_trade_enforced():
    cfg = RiskConfig(account_size=100.0, max_risk_per_trade_pct=0.01)  # $1 max
    rm = RiskManager(cfg)

    # Build a condor directly with a known large max_loss (200 > $1 limit)
    from tests.test_strategy import make_quote
    from src.data.models import OptionType
    strikes = {
        "short_call": make_quote(3300, 0.12, OptionType.CALL, bid=0.05, ask=0.07),
        "long_call":  make_quote(3500, 0.03, OptionType.CALL, bid=0.01, ask=0.015),
        "short_put":  make_quote(2700, -0.12, OptionType.PUT, bid=0.05, ask=0.07),
        "long_put":   make_quote(2500, -0.03, OptionType.PUT, bid=0.01, ask=0.015),
    }
    condor = build_condor(strikes)
    assert condor.max_loss > 1.0  # wider than the $1 limit

    with pytest.raises(RiskViolation, match="max loss"):
        rm.check_new_trade(condor, 0, make_account(100.0))


def test_daily_loss_limit_triggers_kill_switch():
    rm = make_risk_manager()
    assert not rm.is_halted

    # Record loss equal to or exceeding limit (5% of $2200 = $110)
    rm.record_pnl(-200.0)
    assert rm.is_halted


def test_daily_pnl_resets_on_new_day():
    rm = make_risk_manager()
    yesterday = datetime(2024, 1, 1, tzinfo=timezone.utc)
    today = datetime(2024, 1, 2, tzinfo=timezone.utc)

    rm.record_pnl(-50.0, as_of=yesterday)
    assert rm.daily_pnl == -50.0

    rm._kill_switch_active = False  # manual reset for test
    rm.record_pnl(-20.0, as_of=today)
    assert rm.daily_pnl == -20.0  # should reset


def test_position_sizing():
    rm = make_risk_manager()
    # max_risk = 10% of $2200 = $220
    # per-unit loss of $100 → should get 2 contracts
    n = rm.size_position(condor_max_loss_per_unit=100.0, account_equity=2200.0)
    assert n == 2.0


def test_position_sizing_min_one():
    rm = make_risk_manager()
    # per-unit loss of $1000 → only 0.22 contracts, rounds to 1
    n = rm.size_position(condor_max_loss_per_unit=1000.0, account_equity=2200.0)
    assert n == 1.0


def test_api_error_kill_switch():
    rm = make_risk_manager()
    rm.check_api_health(3, error_threshold=5)
    assert not rm.is_halted

    rm.check_api_health(5, error_threshold=5)
    assert rm.is_halted


def test_status_report():
    rm = make_risk_manager()
    report = rm.status_report()
    assert "kill_switch_active" in report
    assert "daily_pnl" in report
    assert "max_risk_per_trade" in report
