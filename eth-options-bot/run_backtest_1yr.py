import sys, os, math, warnings
sys.path.insert(0, ".")
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd
from datetime import datetime, timedelta, timezone
from scipy.stats import norm

from config.settings import AppConfig, BacktestConfig, RiskConfig, StorageConfig, StrategyConfig
from src.backtest.engine import BacktestEngine
from src.data.models import OptionQuote, OptionType
from src.data.storage import ParquetStorage
from src.monitoring.logger import setup_logging

setup_logging("WARNING", "logs/bt1yr.log")

# ---------------------------------------------------------------------------
# Black-Scholes helpers
# ---------------------------------------------------------------------------

def bs_delta(S, K, T, sigma, is_call):
    if T <= 0:
        return 1.0 if (is_call and S > K) else 0.0
    d1 = (math.log(S / K) + 0.5 * sigma**2 * T) / (sigma * math.sqrt(T))
    return float(norm.cdf(d1)) if is_call else float(norm.cdf(d1) - 1)

def bs_price(S, K, T, sigma, is_call):
    if T <= 0:
        return max(S - K, 0) if is_call else max(K - S, 0)
    d1 = (math.log(S / K) + 0.5 * sigma**2 * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if is_call:
        return S * norm.cdf(d1) - K * norm.cdf(d2)
    return K * norm.cdf(-d2) - S * norm.cdf(-d1)

def build_chain(spot, timestamp, base_iv, expiry_days, anchor_spot):
    expiry = timestamp + timedelta(days=expiry_days)
    T = max(expiry_days, 0.5) / 365.0
    raw = [anchor_spot * (1 + i * 0.025) for i in range(-14, 15)]
    strikes = sorted(set(round(k / 50) * 50 for k in raw if k > 0))
    def smile_iv(K):
        return base_iv * (1 + 2.0 * ((K - spot) / spot) ** 2)
    quotes = []
    for K in strikes:
        for is_call in [True, False]:
            iv = smile_iv(K)
            p  = bs_price(spot, K, T, iv, is_call) / spot
            d  = bs_delta(spot, K, T, iv, is_call)
            if p < 0.00005:
                continue
            bid = max(p * 0.94, 0.00005)
            ask = p * 1.06
            opt_type = OptionType.CALL if is_call else OptionType.PUT
            suffix   = "C" if is_call else "P"
            name = f"ETH-{expiry.strftime('%d%b%y').upper()}-{int(K)}-{suffix}"
            quotes.append(OptionQuote(
                timestamp=timestamp, instrument_name=name,
                strike=K, expiry=expiry, option_type=opt_type,
                bid=round(bid, 6), ask=round(ask, 6), mark_price=round(p, 6),
                implied_volatility=iv, delta=round(d, 4),
                gamma=0.001, theta=-p / (T * 365) if T > 0 else 0,
                vega=T ** 0.5 * 0.01, underlying_price=spot,
            ))
    return quotes

# ---------------------------------------------------------------------------
# Generate realistic ETH price path
# ---------------------------------------------------------------------------
print("=" * 60)
print("  ETH Weekly Iron Condor  --  1-Year Backtest")
print("  Period : Mar 2025 - Mar 2026  |  Capital: $2,200")
print("=" * 60)
print("\n[1/3] Simulating ETH price path...")

start        = datetime(2025, 3, 5, tzinfo=timezone.utc)
n_weeks      = 52
total_days   = n_weeks * 7
initial_spot = 2020.0
annual_vol   = 0.65
daily_vol    = annual_vol / math.sqrt(252)
drift        = 0.0003
rng          = np.random.default_rng(seed=2025)

spots = [initial_spot]
ivs   = [0.75]
iv_mean, iv_speed = 0.72, 0.10
for _ in range(total_days):
    s = spots[-1] * math.exp(drift + daily_vol * rng.standard_normal() - 0.5 * daily_vol**2)
    iv = ivs[-1] + iv_speed * (iv_mean - ivs[-1]) + 0.018 * rng.standard_normal()
    spots.append(s)
    ivs.append(max(0.40, min(2.0, iv)))

print(f"  Start price  : ${spots[0]:.0f}")
print(f"  End price    : ${spots[total_days-1]:.0f}")
print(f"  Range        : ${min(spots[:total_days]):.0f} - ${max(spots[:total_days]):.0f}")
print(f"  Realised vol : {float(np.std(np.diff(np.log(spots[:total_days]))))*math.sqrt(252)*100:.1f}% annualised")

# ---------------------------------------------------------------------------
# Build synthetic option chain dataset
# ---------------------------------------------------------------------------
print("\n[2/3] Building option chain snapshots (this takes ~30s)...")
parquet_dir  = "data/backtest_1yr/parquet"
expiry_dates = [start + timedelta(weeks=w) for w in range(1, n_weeks + 5)]
expiry_anchor = {}
for exp in expiry_dates:
    idx = max(0, min((exp - timedelta(days=7) - start).days, len(spots) - 1))
    expiry_anchor[exp] = spots[idx]

storage = ParquetStorage(parquet_dir)
for day in range(total_days):
    date  = start + timedelta(days=day)
    spot  = spots[day]
    iv    = ivs[day]
    chains = []
    for exp in expiry_dates:
        dte = (exp - date).days
        if dte < 1 or dte > 35:
            continue
        chains.extend(build_chain(spot, date, iv, dte, expiry_anchor[exp]))
    if chains:
        storage.save_quotes(chains)
print("  Done.")

# ---------------------------------------------------------------------------
# Run backtest
# ---------------------------------------------------------------------------
print("\n[3/3] Running backtest engine...")
cfg = AppConfig(
    backtest=BacktestConfig(
        start_date="2025-03-05",
        end_date="2026-03-05",
        initial_capital=2200.0,
        fee_per_contract=0.0003,
        slippage_pct=0.001,
        fill_model="mid",
    ),
    strategy=StrategyConfig(
        iv_percentile_min=35.0,
        max_daily_move_pct=9.0,
        short_delta_min=0.10,
        short_delta_max=0.15,
        wing_delta_min=0.03,
        wing_delta_max=0.05,
        take_profit_pct=0.50,
        stop_loss_multiplier=2.0,
        close_dte=1,
    ),
    risk=RiskConfig(account_size=2200.0, max_risk_per_trade_pct=0.10),
    storage=StorageConfig(parquet_dir=parquet_dir),
)
engine = BacktestEngine(cfg, storage)
results = engine.run()

# ---------------------------------------------------------------------------
# Print results
# ---------------------------------------------------------------------------
m      = results.get("metrics", {})
trades = results.get("trades", [])
avg_spot = float(np.mean(spots[:total_days]))

print("\n" + "=" * 60)
print("  BACKTEST RESULTS -- Weekly ETH Iron Condor")
print("  Account: $2,200  |  Max risk/trade: 10% ($220)")
print("=" * 60)
print(f"  Period              : Mar 2025 - Mar 2026")
print(f"  Trades executed     : {m.get('total_trades', 0)}")
print(f"  Win rate            : {m.get('win_rate_pct', 0):.1f}%")
total_pnl_eth = m.get('total_pnl', 0)
total_pnl_usd = total_pnl_eth * avg_spot
print(f"  Total PnL (ETH)     : {total_pnl_eth:+.4f} ETH")
print(f"  Total PnL (USD est) : ${total_pnl_usd:+.2f}")
print(f"  Avg trade PnL (ETH) : {m.get('avg_trade_pnl', 0):+.5f}")
print(f"  Total return        : {m.get('total_return_pct', 0):+.2f}%")
print(f"  CAGR                : {m.get('cagr_pct', 0):+.2f}%")
print(f"  Max drawdown        : {m.get('max_drawdown_pct', 0):.2f}%")
print(f"  Sharpe ratio        : {m.get('sharpe_ratio', 0):.2f}")
print(f"  Tail loss (5th %)   : {m.get('tail_loss_5pct', 0):.5f} ETH")
print("=" * 60)

if trades:
    wins   = [t.realized_pnl for t in trades if (t.realized_pnl or 0) > 0]
    losses = [t.realized_pnl for t in trades if (t.realized_pnl or 0) <= 0]
    tp  = sum(1 for t in trades if "take_profit"         in (t.exit_reason or ""))
    sl  = sum(1 for t in trades if "stop_loss"           in (t.exit_reason or ""))
    exp = sum(1 for t in trades if "close_before_expiry" in (t.exit_reason or ""))
    be  = sum(1 for t in trades if "backtest_end"        in (t.exit_reason or ""))

    print(f"\n  Exit breakdown:")
    print(f"    Take-profit      : {tp}")
    print(f"    Stop-loss        : {sl}")
    print(f"    Closed at expiry : {exp}")
    print(f"    Closed at BT end : {be}")
    if wins:
        print(f"    Avg win          : +{np.mean(wins):.5f} ETH  (~${np.mean(wins)*avg_spot:+.2f})")
    if losses:
        print(f"    Avg loss         : {np.mean(losses):.5f} ETH  (~${np.mean(losses)*avg_spot:+.2f})")
    if wins and losses and sum(losses) != 0:
        print(f"    Profit factor    : {abs(sum(wins)/sum(losses)):.2f}")

    print("\n" + "=" * 60)
    print("  Per-Trade Log")
    print("=" * 60)
    rows = []
    for t in trades:
        pnl_usd = (t.realized_pnl or 0) * t.underlying_at_entry
        rows.append({
            "Entry":      str(t.entry_time.date()),
            "Exit":       str(t.exit_time.date()) if t.exit_time else "-",
            "ETH@Entry":  f"${t.underlying_at_entry:.0f}",
            "SC":         int(t.short_call_strike),
            "LC":         int(t.long_call_strike),
            "SP":         int(t.short_put_strike),
            "LP":         int(t.long_put_strike),
            "Credit ETH": f"{t.credit_received:.5f}",
            "MaxLoss":    f"{t.max_loss:.2f}",
            "PnL ETH":    f"{(t.realized_pnl or 0):+.5f}",
            "PnL USD":    f"${pnl_usd:+.1f}",
            "Reason":     (t.exit_reason or "")[:22],
        })
    df = pd.DataFrame(rows)
    print(df.to_string(index=False))
    out = "data/backtest_1yr/trade_history.csv"
    df.to_csv(out, index=False)
    print(f"\nTrade history saved to {out}")
