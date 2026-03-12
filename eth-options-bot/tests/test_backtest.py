"""End-to-end backtest test using synthetic data."""
from __future__ import annotations

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import os
import tempfile
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from config.settings import AppConfig, BacktestConfig, StrategyConfig, RiskConfig, StorageConfig
from src.backtest.engine import BacktestEngine, compute_metrics
from src.data.storage import ParquetStorage
from tests.test_strategy import build_test_chain


# ---------------------------------------------------------------------------
# Synthetic data generator
# ---------------------------------------------------------------------------

def generate_synthetic_parquet(parquet_dir: str, n_weeks: int = 8) -> None:
    """Create synthetic daily option chain snapshots for n_weeks."""
    storage = ParquetStorage(parquet_dir)

    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    spot = 3000.0

    for week in range(n_weeks):
        for day in range(7):
            date = start + timedelta(weeks=week, days=day)
            # Simulate small spot drift
            spot = spot * (1 + (day % 3 - 1) * 0.01)
            quotes = build_test_chain(spot=spot)
            # Stamp all quotes with the current date
            for q in quotes:
                q.timestamp = date
                q.underlying_price = spot
            storage.save_quotes(quotes)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_backtest_runs_with_synthetic_data():
    with tempfile.TemporaryDirectory() as tmpdir:
        parquet_dir = os.path.join(tmpdir, "parquet")
        generate_synthetic_parquet(parquet_dir, n_weeks=4)

        cfg = AppConfig(
            backtest=BacktestConfig(
                start_date="2024-01-01",
                end_date="2024-02-28",
                initial_capital=2200.0,
                fee_per_contract=0.0003,
                slippage_pct=0.001,
                fill_model="mid",
            ),
            strategy=StrategyConfig(),
            risk=RiskConfig(account_size=2200.0),
            storage=StorageConfig(parquet_dir=parquet_dir),
        )

        storage = ParquetStorage(parquet_dir)
        engine = BacktestEngine(cfg, storage)
        results = engine.run()

        assert "metrics" in results
        assert "equity_curve" in results
        assert "trades" in results
        assert isinstance(results["equity_curve"], pd.Series)


def test_compute_metrics_basic():
    import numpy as np
    equity = pd.Series(
        [1000, 1010, 1005, 1020, 1015, 1030],
        index=pd.date_range("2024-01-01", periods=6, tz="UTC"),
    )

    from src.data.models import TradeRecord
    trade = TradeRecord(
        id="t1",
        entry_time=datetime(2024, 1, 1, tzinfo=timezone.utc),
        exit_time=datetime(2024, 1, 5, tzinfo=timezone.utc),
        underlying_at_entry=3000.0,
        underlying_at_exit=3010.0,
        short_call_strike=3300.0,
        long_call_strike=3500.0,
        short_put_strike=2700.0,
        long_put_strike=2500.0,
        expiry=datetime(2024, 1, 7, tzinfo=timezone.utc),
        credit_received=0.05,
        max_loss=0.20,
        realized_pnl=0.03,
        exit_reason="take_profit",
        iv_percentile_at_entry=65.0,
    )

    metrics = compute_metrics(equity, [trade])

    assert metrics["total_trades"] == 1
    assert metrics["win_rate_pct"] == 100.0
    assert metrics["avg_trade_pnl"] == pytest.approx(0.03)
    assert "sharpe_ratio" in metrics
    assert "max_drawdown_pct" in metrics
