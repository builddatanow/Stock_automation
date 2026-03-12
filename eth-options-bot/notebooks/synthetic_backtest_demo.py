"""
Synthetic Backtest Demo
=======================
Run a full iron condor backtest on generated data — no Deribit connection needed.

Usage:
    python notebooks/synthetic_backtest_demo.py
"""
from __future__ import annotations

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import os
import tempfile
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from config.settings import AppConfig, BacktestConfig, RiskConfig, StorageConfig, StrategyConfig
from src.backtest.engine import BacktestEngine
from src.data.models import OptionQuote, OptionType
from src.data.storage import ParquetStorage
from src.monitoring.logger import setup_logging

setup_logging("INFO", "logs/synthetic_demo.log")


# ---------------------------------------------------------------------------
# Synthetic chain builder (more realistic)
# ---------------------------------------------------------------------------

def black_scholes_delta(S: float, K: float, T: float, sigma: float, is_call: bool) -> float:
    """Approximate BS delta using scipy."""
    from scipy.stats import norm
    import math

    if T <= 0:
        return 1.0 if (is_call and S > K) else 0.0

    d1 = (math.log(S / K) + 0.5 * sigma**2 * T) / (sigma * math.sqrt(T))
    return float(norm.cdf(d1)) if is_call else float(norm.cdf(d1) - 1)


def bs_option_price(S: float, K: float, T: float, sigma: float, is_call: bool) -> float:
    """Black-Scholes option price (simplified, no interest rate)."""
    from scipy.stats import norm
    import math

    if T <= 0:
        intrinsic = max(S - K, 0) if is_call else max(K - S, 0)
        return intrinsic

    d1 = (math.log(S / K) + 0.5 * sigma**2 * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)

    if is_call:
        return S * norm.cdf(d1) - K * norm.cdf(d2)
    else:
        return K * norm.cdf(-d2) - S * norm.cdf(-d1)


def build_realistic_chain(
    spot: float,
    timestamp: datetime,
    base_iv: float = 0.80,
    expiry_days: float = 7.0,
    anchor_spot: float = None,
) -> list[OptionQuote]:
    """
    Build a realistic option chain for one expiry.

    anchor_spot: the spot used to set the strike grid for this expiry cycle.
    Using a fixed anchor_spot per expiry ensures instrument names stay stable
    for the life of the contract, even as spot moves daily.

    Prices are in ETH (normalized by spot), matching Deribit conventions.
    """
    if anchor_spot is None:
        anchor_spot = spot

    expiry = timestamp + timedelta(days=expiry_days)
    T = max(expiry_days, 0.5) / 365.0

    # Strike grid: 30 strikes centred on anchor_spot, spaced 2.5% apart, rounded to nearest 50
    raw_strikes = [anchor_spot * (1 + i * 0.025) for i in range(-12, 13)]
    strikes = sorted(set(round(k / 50) * 50 for k in raw_strikes if k > 0))

    def smile_iv(K: float) -> float:
        moneyness = abs(K - spot) / spot
        return base_iv * (1 + 1.5 * moneyness ** 2)

    quotes = []
    for K in strikes:
        for is_call in [True, False]:
            iv = smile_iv(K)
            # USD price
            price_usd = bs_option_price(spot, K, T, iv, is_call)
            delta = black_scholes_delta(spot, K, T, iv, is_call)

            # Convert to ETH-denominated (Deribit style: price / spot)
            price_eth = price_usd / spot

            if price_eth < 0.0001:
                continue

            spread_pct = 0.05
            bid = max(price_eth * (1 - spread_pct), 0.0001)
            ask = price_eth * (1 + spread_pct)

            opt_type = OptionType.CALL if is_call else OptionType.PUT
            suffix = "C" if is_call else "P"
            # Instrument name uses anchor-based strike → stable for the whole expiry week
            name = f"ETH-{expiry.strftime('%d%b%y').upper()}-{int(K)}-{suffix}"

            q = OptionQuote(
                timestamp=timestamp,
                instrument_name=name,
                strike=K,
                expiry=expiry,
                option_type=opt_type,
                bid=round(bid, 6),
                ask=round(ask, 6),
                mark_price=round(price_eth, 6),
                implied_volatility=iv,
                delta=round(delta, 4),
                gamma=0.001,
                theta=-price_eth / (T * 365) if T > 0 else 0,
                vega=T ** 0.5 * 0.01,
                underlying_price=spot,
            )
            quotes.append(q)

    return quotes


def generate_synthetic_dataset(
    parquet_dir: str,
    start: datetime,
    n_weeks: int = 52,
    initial_spot: float = 3000.0,
    annual_vol: float = 0.75,
) -> None:
    """
    Simulate ETH GBM price path and generate synthetic option chains.

    Expiries are FIXED weekly dates (every 7 days from start).
    Strikes are anchored to the spot on the Monday each expiry cycle starts,
    so instrument names are identical for all days a contract is alive.
    """
    storage = ParquetStorage(parquet_dir)

    total_days = n_weeks * 7
    daily_vol = annual_vol / 252 ** 0.5
    rng = np.random.default_rng(42)

    # Simulate the full price/IV path once
    spots = [initial_spot]
    ivs = [0.80]
    iv_mean, iv_speed = 0.80, 0.1
    for _ in range(total_days):
        s = spots[-1] * np.exp((daily_vol * rng.standard_normal()) - 0.5 * daily_vol ** 2)
        iv = ivs[-1] + iv_speed * (iv_mean - ivs[-1]) + 0.03 * rng.standard_normal()
        spots.append(s)
        ivs.append(max(0.30, min(2.0, iv)))

    # Fixed weekly expiry dates (every 7 days from start)
    expiry_dates = [start + timedelta(weeks=w) for w in range(1, n_weeks + 5)]

    # Anchor spot for each expiry = spot on the Monday 7 days before expiry
    expiry_anchor: dict[datetime, float] = {}
    for exp in expiry_dates:
        anchor_day = exp - timedelta(days=7)
        anchor_idx = max(0, min((anchor_day - start).days, len(spots) - 1))
        expiry_anchor[exp] = spots[anchor_idx]

    # Generate daily snapshots
    for day in range(total_days):
        date = start + timedelta(days=day)
        spot = spots[day]
        iv = ivs[day]

        chains = []
        for exp in expiry_dates:
            dte = (exp - date).days
            if dte < 1 or dte > 35:
                continue  # only include active expirations
            anchor = expiry_anchor[exp]
            chain = build_realistic_chain(
                spot=spot,
                timestamp=date,
                base_iv=iv,
                expiry_days=dte,
                anchor_spot=anchor,
            )
            chains.extend(chain)

        if chains:
            storage.save_quotes(chains)

    print(f"Generated {total_days} days of synthetic data in {parquet_dir}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 60)
    print("  ETH Iron Condor — Synthetic Backtest Demo")
    print("=" * 60)

    tmpdir = "data/synthetic_demo"
    os.makedirs(tmpdir, exist_ok=True)
    parquet_dir = os.path.join(tmpdir, "parquet")

    start_date = datetime(2023, 1, 2, tzinfo=timezone.utc)
    end_date = datetime(2024, 1, 1, tzinfo=timezone.utc)

    print("\n[1/3] Generating synthetic option chain data...")
    generate_synthetic_dataset(
        parquet_dir=parquet_dir,
        start=start_date,
        n_weeks=52,
        initial_spot=1800.0,
        annual_vol=0.85,
    )

    print("\n[2/3] Running backtest...")
    cfg = AppConfig(
        backtest=BacktestConfig(
            start_date=start_date.strftime("%Y-%m-%d"),
            end_date=end_date.strftime("%Y-%m-%d"),
            initial_capital=2200.0,
            fee_per_contract=0.0003,
            slippage_pct=0.001,
            fill_model="mid",
        ),
        strategy=StrategyConfig(
            iv_percentile_min=40.0,  # relaxed for synthetic data
            max_daily_move_pct=8.0,
        ),
        risk=RiskConfig(account_size=2200.0),
        storage=StorageConfig(parquet_dir=parquet_dir),
    )

    storage = ParquetStorage(parquet_dir)
    engine = BacktestEngine(cfg, storage)
    results = engine.run()

    print("\n[3/3] Results:")
    engine.print_summary(results)

    # Save trade detail
    trades = results.get("trades", [])
    if trades:
        rows = []
        for t in trades:
            rows.append({
                "entry": t.entry_time.date() if t.entry_time else "",
                "exit": t.exit_time.date() if t.exit_time else "",
                "SC": t.short_call_strike,
                "LC": t.long_call_strike,
                "SP": t.short_put_strike,
                "LP": t.long_put_strike,
                "credit": round(t.credit_received, 4),
                "max_loss": round(t.max_loss, 4),
                "pnl": round(t.realized_pnl or 0, 4),
                "reason": t.exit_reason,
            })
        df = pd.DataFrame(rows)
        out_path = "data/synthetic_demo/trade_history.csv"
        df.to_csv(out_path, index=False)
        print(f"\nTrade history saved to {out_path}")
        print(df.to_string(index=False))
