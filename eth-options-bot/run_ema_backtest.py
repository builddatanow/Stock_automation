"""
EMA-Based Directional Spread  --  1-Year Backtest
==================================================
Bullish signal (EMA9 > EMA21 + price > EMA21) : sell Bull Put Spread
Bearish signal (EMA9 < EMA21 + price < EMA21) : sell Bear Call Spread

Run: python run_ema_backtest.py
"""
import sys, os, math, warnings
sys.path.insert(0, ".")
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd
from datetime import datetime, timedelta, timezone
from scipy.stats import norm

from src.backtest.ema_backtest import EMASpreadBacktest
from src.data.models import OptionQuote, OptionType
from src.data.storage import ParquetStorage
from src.strategy.ema_spread import EMASpreadConfig, compute_ema, get_ema_signal
from src.monitoring.logger import setup_logging

setup_logging("WARNING", "logs/ema_bt.log")

# ---------------------------------------------------------------------------
# Black-Scholes helpers
# ---------------------------------------------------------------------------
def bs_delta(S, K, T, sigma, is_call):
    if T <= 0:
        return 1.0 if (is_call and S > K) else 0.0
    d1 = (math.log(S/K) + 0.5*sigma**2*T) / (sigma*math.sqrt(T))
    return float(norm.cdf(d1)) if is_call else float(norm.cdf(d1)-1)

def bs_price(S, K, T, sigma, is_call):
    if T <= 0:
        return max(S-K,0) if is_call else max(K-S,0)
    d1 = (math.log(S/K) + 0.5*sigma**2*T) / (sigma*math.sqrt(T))
    d2 = d1 - sigma*math.sqrt(T)
    if is_call:
        return S*norm.cdf(d1) - K*norm.cdf(d2)
    return K*norm.cdf(-d2) - S*norm.cdf(-d1)

def build_chain(spot, timestamp, base_iv, expiry_days, anchor_spot):
    expiry = timestamp + timedelta(days=expiry_days)
    T = max(expiry_days, 0.5) / 365.0
    raw = [anchor_spot*(1+i*0.025) for i in range(-14, 15)]
    strikes = sorted(set(round(k/50)*50 for k in raw if k > 0))
    def smile_iv(K):
        return base_iv * (1 + 2.0*((K-spot)/spot)**2)
    quotes = []
    for K in strikes:
        for is_call in [True, False]:
            iv = smile_iv(K)
            p  = bs_price(spot, K, T, iv, is_call) / spot
            d  = bs_delta(spot, K, T, iv, is_call)
            if p < 0.00005:
                continue
            bid = max(p*0.94, 0.00005)
            ask = p*1.06
            opt_type = OptionType.CALL if is_call else OptionType.PUT
            suffix   = "C" if is_call else "P"
            name = f"ETH-{expiry.strftime('%d%b%y').upper()}-{int(K)}-{suffix}"
            quotes.append(OptionQuote(
                timestamp=timestamp, instrument_name=name,
                strike=K, expiry=expiry, option_type=opt_type,
                bid=round(bid,6), ask=round(ask,6), mark_price=round(p,6),
                implied_volatility=iv, delta=round(d,4),
                gamma=0.001, theta=-p/(T*365) if T>0 else 0,
                vega=T**0.5*0.01, underlying_price=spot,
            ))
    return quotes

# ---------------------------------------------------------------------------
# Simulate ETH price path
# ---------------------------------------------------------------------------
print("=" * 60)
print("  EMA Spread Strategy  --  1-Year Backtest")
print("  Period : Mar 2025 - Mar 2026  |  Capital: $2,200")
print("  Signals: EMA(9) / EMA(21) crossover")
print("=" * 60)
print("\n[1/4] Simulating ETH price path...")

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
    s  = spots[-1] * math.exp(drift + daily_vol*rng.standard_normal() - 0.5*daily_vol**2)
    iv = ivs[-1] + iv_speed*(iv_mean - ivs[-1]) + 0.018*rng.standard_normal()
    spots.append(s)
    ivs.append(max(0.40, min(2.0, iv)))

print(f"  Start  : ${spots[0]:.0f}")
print(f"  End    : ${spots[total_days-1]:.0f}")
print(f"  Range  : ${min(spots[:total_days]):.0f} - ${max(spots[:total_days]):.0f}")
print(f"  Ann vol: {float(np.std(np.diff(np.log(spots[:total_days]))))*math.sqrt(252)*100:.1f}%")

# ---------------------------------------------------------------------------
# EMA signals preview
# ---------------------------------------------------------------------------
print("\n[2/4] EMA signal analysis on price path...")
fast_ema = compute_ema(spots[:total_days], 9)
slow_ema = compute_ema(spots[:total_days], 21)

signals = []
for i in range(total_days):
    f, s, p = fast_ema[i], slow_ema[i], spots[i]
    if math.isnan(f) or math.isnan(s):
        signals.append("neutral")
    elif f > s and p > s:
        signals.append("bullish")
    elif f < s and p < s:
        signals.append("bearish")
    else:
        signals.append("neutral")

bull_days = signals.count("bullish")
bear_days = signals.count("bearish")
neut_days = signals.count("neutral")
print(f"  Bullish days : {bull_days} ({bull_days/total_days*100:.0f}%)")
print(f"  Bearish days : {bear_days} ({bear_days/total_days*100:.0f}%)")
print(f"  Neutral days : {neut_days} ({neut_days/total_days*100:.0f}%)")

# ---------------------------------------------------------------------------
# Build option chain dataset
# ---------------------------------------------------------------------------
print("\n[3/4] Building option chain snapshots...")
parquet_dir  = "data/ema_backtest/parquet"
expiry_dates = [start + timedelta(weeks=w) for w in range(1, n_weeks+5)]
expiry_anchor = {}
for exp in expiry_dates:
    idx = max(0, min((exp - timedelta(days=7) - start).days, len(spots)-1))
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
print("\n[4/4] Running EMA spread backtest engine...")
cfg = EMASpreadConfig(
    fast_ema=9,
    slow_ema=21,
    target_dte_min=5,
    target_dte_max=10,
    # Higher delta = more premium collected per trade
    short_delta_min=0.30,
    short_delta_max=0.40,
    wing_delta_min=0.10,
    wing_delta_max=0.15,
    # Tighter stop-loss cuts losers faster
    take_profit_pct=0.50,
    stop_loss_multiplier=1.5,
    close_dte=1,
    iv_percentile_min=25.0,
    # Trend strength filter: skip weak / whipsaw signals
    min_trend_strength=0.005,
    account_size=2200.0,
    max_risk_per_trade_pct=0.12,
)

engine = EMASpreadBacktest(
    config=cfg,
    parquet_storage=storage,
    start_date="2025-03-05",
    end_date="2026-03-05",
    initial_capital=2200.0,
    fee_per_contract=0.0003,
    slippage_pct=0.001,
)
results = engine.run()
engine.print_summary(results)

# ---------------------------------------------------------------------------
# Detailed results
# ---------------------------------------------------------------------------
m      = results.get("metrics", {})
trades = results.get("trades", [])
signal_log = results.get("signal_log", [])
avg_spot = float(np.mean(spots[:total_days]))

if trades:
    wins   = [t.realized_pnl for t in trades if (t.realized_pnl or 0) > 0]
    losses = [t.realized_pnl for t in trades if (t.realized_pnl or 0) <= 0]
    tp  = sum(1 for t in trades if "take_profit"         in (t.exit_reason or ""))
    sl  = sum(1 for t in trades if "stop_loss"           in (t.exit_reason or ""))
    exp = sum(1 for t in trades if "close_before_expiry" in (t.exit_reason or ""))
    rev = sum(1 for t in trades if "signal_reversal"     in (t.exit_reason or ""))
    be  = sum(1 for t in trades if "backtest_end"        in (t.exit_reason or ""))

    total_pnl_usd = m.get("total_pnl", 0) * avg_spot

    print(f"\n  Total PnL (USD est)  : ${total_pnl_usd:+.2f}")
    print(f"\n  Exit breakdown:")
    print(f"    Take-profit        : {tp}")
    print(f"    Stop-loss          : {sl}")
    print(f"    Signal reversal    : {rev}")
    print(f"    Closed at expiry   : {exp}")
    print(f"    Closed at BT end   : {be}")
    if wins:
        print(f"    Avg win            : +{np.mean(wins):.5f} ETH  (~${np.mean(wins)*avg_spot:+.2f})")
    if losses:
        print(f"    Avg loss           : {np.mean(losses):.5f} ETH  (~${np.mean(losses)*avg_spot:+.2f})")
    if wins and losses and sum(losses) != 0:
        print(f"    Profit factor      : {abs(sum(wins)/sum(losses)):.2f}")

    print("\n" + "=" * 78)
    print("  Per-Trade Log")
    print("=" * 78)
    rows = []
    for t in trades:
        pnl_usd = (t.realized_pnl or 0) * t.underlying_at_entry
        if t.short_call_strike > 0:
            spread_type = "BearCall"
            strikes_str = f"SC={int(t.short_call_strike)} LC={int(t.long_call_strike)}"
        else:
            spread_type = "BullPut "
            strikes_str = f"SP={int(t.short_put_strike)} LP={int(t.long_put_strike)}"

        rows.append({
            "Entry":      str(t.entry_time.date()),
            "Exit":       str(t.exit_time.date()) if t.exit_time else "-",
            "Type":       spread_type,
            "ETH":        f"${t.underlying_at_entry:.0f}",
            "Strikes":    strikes_str,
            "Credit":     f"{t.credit_received:.5f}",
            "MaxLoss":    f"{t.max_loss:.2f}",
            "PnL ETH":    f"{(t.realized_pnl or 0):+.5f}",
            "PnL USD":    f"${pnl_usd:+.1f}",
            "Reason":     (t.exit_reason or "")[:24],
        })
    df = pd.DataFrame(rows)
    print(df.to_string(index=False))

    # Save outputs
    os.makedirs("data/ema_backtest", exist_ok=True)
    df.to_csv("data/ema_backtest/trade_history.csv", index=False)

    # Signal log
    sl_df = pd.DataFrame(signal_log)
    sl_df.to_csv("data/ema_backtest/signal_log.csv", index=False)
    print(f"\n  Trade history  -> data/ema_backtest/trade_history.csv")
    print(f"  Signal log     -> data/ema_backtest/signal_log.csv")

    # Weekly signal table
    print("\n" + "=" * 60)
    print("  Weekly Signal Log (Mondays only)")
    print("=" * 60)
    mon_signals = sl_df if not sl_df.empty else pd.DataFrame()
    if not mon_signals.empty:
        print(mon_signals[["date","spot","signal","action","reason"]].to_string(index=False))
