"""
0 DTE Backtest -- Sydney Timed Entry (No Iron Condor)
=====================================================
Strategy  : EMA(9/21) directional only -- Bull Put OR Bear Call
            Iron Condor disabled (0 DTE IC win rate is too low at 25%)
Entry     : 1 PM Sydney AEDT first (2:00 AM UTC, 6h before expiry)
            If no directional signal -> try 2 PM Sydney AEDT (3:00 AM UTC, 5h before expiry)
            If still no signal -> skip the day
Expiry    : Deribit ETH options expire 8:00 AM UTC (= 7:00 PM Sydney AEDT)
Exit      : End of day (at expiry) using near-zero T pricing

Timezone reference:
  Sydney AEDT (Oct-Apr) = UTC+11
  Sydney AEST (Apr-Oct) = UTC+10
  This script uses AEDT (+11) throughout for consistency.
  1 PM AEDT = 02:00 UTC | 2 PM AEDT = 03:00 UTC

Period : Mar 2024 - Mar 2026  |  Capital: $2,200
"""
import sys, os, math, warnings
sys.path.insert(0, ".")
warnings.filterwarnings("ignore")

import requests
import numpy as np
import pandas as pd
from datetime import datetime, timedelta, timezone
from scipy.stats import norm

from src.backtest.ema_backtest import EMASpreadBacktest
from src.data.models import OptionQuote, OptionType
from src.data.storage import ParquetStorage
from src.strategy.ema_spread import EMASpreadConfig
from src.monitoring.logger import setup_logging

setup_logging("WARNING", "logs/0dte_sydney_bt.log")

BASE_URL = "https://www.deribit.com"

# ---------------------------------------------------------------------------
# Sydney time entry windows (hours before 8 AM UTC expiry)
# ---------------------------------------------------------------------------
ENTRY_WINDOWS = [
    {"label": "1 PM Sydney (02:00 UTC)", "hours_before_expiry": 6,  "T_days": 6/24},
    {"label": "2 PM Sydney (03:00 UTC)", "hours_before_expiry": 5,  "T_days": 5/24},
]

# ---------------------------------------------------------------------------
# Data fetchers
# ---------------------------------------------------------------------------

def fetch_eth_ohlcv(start_dt, end_dt):
    resp = requests.get(
        f"{BASE_URL}/api/v2/public/get_tradingview_chart_data",
        params={"instrument_name": "ETH-PERPETUAL",
                "start_timestamp": int(start_dt.timestamp() * 1000),
                "end_timestamp":   int(end_dt.timestamp()   * 1000),
                "resolution": "1D"},
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json().get("result", {})
    ticks = data.get("ticks", [])
    if not ticks:
        return pd.DataFrame()
    df = pd.DataFrame({
        "timestamp": pd.to_datetime(ticks, unit="ms", utc=True),
        "open": data["open"], "high": data["high"],
        "low": data["low"], "close": data["close"], "volume": data["volume"],
    })
    return df.sort_values("timestamp").reset_index(drop=True)


def fetch_historical_iv():
    resp = requests.get(f"{BASE_URL}/api/v2/public/get_historical_volatility",
                        params={"currency": "ETH"}, timeout=20)
    resp.raise_for_status()
    raw = resp.json().get("result", [])
    if not raw:
        return pd.DataFrame()
    df = pd.DataFrame(raw, columns=["timestamp_ms", "iv"])
    df["timestamp"] = pd.to_datetime(df["timestamp_ms"], unit="ms", utc=True)
    df["date"] = df["timestamp"].dt.date
    df["iv_decimal"] = df["iv"] / 100.0
    return df.sort_values("timestamp").drop_duplicates("date").reset_index(drop=True)[["date", "iv_decimal", "iv"]]


# ---------------------------------------------------------------------------
# Black-Scholes helpers
# ---------------------------------------------------------------------------

def bs_delta(S, K, T, sigma, is_call):
    if T <= 0:
        return (1.0 if S > K else 0.0) if is_call else (-1.0 if S < K else 0.0)
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

def build_chain(spot, timestamp, base_iv, expiry_days, anchor_spot, T_override=None):
    """
    Build an option chain for one expiry.
    T_override: use a specific T (in days) instead of expiry_days.
                Used to simulate intraday entry times (e.g., 6h = 0.25 days).
    """
    expiry = timestamp + timedelta(days=expiry_days)
    T = T_override if T_override is not None else max(expiry_days, 0.25) / 365.0
    T = T / 365.0 if T > 1 else T   # T_override is in days, convert to years

    # Tight strike grid anchored to entry-day spot
    raw = [anchor_spot * (1 + i * 0.015) for i in range(-12, 13)]
    strikes = sorted(set(round(k / 25) * 25 for k in raw if k > 0))

    def smile_iv(K):
        m = (K - spot) / spot
        return base_iv * (1 + 3.0 * m**2 - 0.5 * m) if m < 0 else base_iv * (1 + 1.5 * m**2)

    quotes = []
    for K in strikes:
        for is_call in [True, False]:
            iv  = max(smile_iv(K), 0.20)
            p   = bs_price(spot, K, T, iv, is_call) / spot
            d   = bs_delta(spot, K, T, iv, is_call)
            if p < 0.00003:
                continue
            bid = max(p * 0.92, 0.00003)
            ask = p * 1.08
            opt = OptionType.CALL if is_call else OptionType.PUT
            sfx = "C" if is_call else "P"
            name = f"ETH-{expiry.strftime('%d%b%y').upper()}-{int(K)}-{sfx}"
            quotes.append(OptionQuote(
                timestamp=timestamp, instrument_name=name,
                strike=K, expiry=expiry, option_type=opt,
                bid=round(bid, 6), ask=round(ask, 6), mark_price=round(p, 6),
                implied_volatility=iv, delta=round(d, 4),
                gamma=0.001, theta=-p / (T * 365) if T > 0 else 0,
                vega=T**0.5 * 0.01, underlying_price=spot,
            ))
    return quotes


def run_window_backtest(window_label, T_days, merged, parquet_dir, start_dt, end_dt):
    """Run a single entry-window backtest and return results."""

    expiry_dates = [start_dt + timedelta(days=d) for d in range(0, 731)]

    expiry_anchor = {}
    for exp in expiry_dates:
        row = merged[merged["date"] <= exp.date()]
        expiry_anchor[exp] = float(row["close"].iloc[-1]) if not row.empty else float(merged["close"].iloc[0])

    storage = ParquetStorage(parquet_dir)
    built = 0
    for _, row in merged.iterrows():
        date  = row["date"]
        spot  = float(row["close"])
        iv    = float(row["iv_decimal"])
        dt    = datetime.combine(date, datetime.min.time()).replace(tzinfo=timezone.utc)
        chains = []
        for exp in expiry_dates:
            dte = (exp - dt).days
            if dte < 0 or dte > 2:
                continue
            # Use T_override for 0 DTE to simulate specific entry time
            T_override = T_days / 365.0 if dte == 0 else None
            chains.extend(build_chain(spot, dt, iv, dte, expiry_anchor[exp], T_override=T_override))
        if chains:
            storage.save_quotes(chains)
            built += 1

    cfg = EMASpreadConfig(
        fast_ema=9, slow_ema=21,
        target_dte_min=0,
        target_dte_max=1,
        short_delta_min=0.20,
        short_delta_max=0.35,
        wing_delta_min=0.08,
        wing_delta_max=0.15,
        take_profit_pct=0.50,
        stop_loss_multiplier=1.5,
        close_dte=0,
        iv_percentile_min=10.0,
        min_trend_strength=0.003,
        condor_on_low_iv=False,       # *** NO IRON CONDOR ***
        entry_every_day=True,
        account_size=2200.0,
        max_risk_per_trade_pct=0.20,
    )

    engine = EMASpreadBacktest(
        config=cfg,
        parquet_storage=storage,
        start_date=str(start_dt.date()),
        end_date=str(end_dt.date()),
        initial_capital=2200.0,
        fee_per_contract=0.0003,
        slippage_pct=0.001,
    )
    results = engine.run()
    return results, storage, built


def print_window_summary(label, results, avg_spot):
    m      = results.get("metrics", {})
    trades = results.get("trades", [])
    if not trades:
        print(f"  {label}: no trades")
        return

    wins   = [t.realized_pnl for t in trades if (t.realized_pnl or 0) > 0]
    losses = [t.realized_pnl for t in trades if (t.realized_pnl or 0) <= 0]
    win_rate = len(wins)/len(trades)*100

    fee_per_contract = 0.0003
    total_fees_eth = sum(
        fee_per_contract * (2 if (t.short_call_strike > 0) != (t.short_put_strike > 0) else 4) * 2
        for t in trades
    )
    total_pnl_usd  = m.get("total_pnl", 0) * avg_spot
    net_return     = total_pnl_usd / 2200.0 * 100
    gross_pnl_usd  = total_pnl_usd + total_fees_eth * avg_spot

    bp = [t for t in trades if t.short_put_strike > 0 and t.short_call_strike == 0]
    bc = [t for t in trades if t.short_call_strike > 0 and t.short_put_strike == 0]

    bp_wr = f"{len([t for t in bp if (t.realized_pnl or 0)>0])/len(bp)*100:.0f}% ({len(bp)}t)" if bp else "-"
    bc_wr = f"{len([t for t in bc if (t.realized_pnl or 0)>0])/len(bc)*100:.0f}% ({len(bc)}t)" if bc else "-"

    tp  = sum(1 for t in trades if "take_profit"         in (t.exit_reason or ""))
    sl  = sum(1 for t in trades if "stop_loss"           in (t.exit_reason or ""))
    exp = sum(1 for t in trades if "close_before_expiry" in (t.exit_reason or ""))

    print(f"\n  {label}")
    print(f"  {'Trades':<20}: {len(trades)}")
    print(f"  {'Win rate':<20}: {win_rate:.1f}%")
    print(f"  {'Net PnL (USD)':<20}: ${total_pnl_usd:+,.2f}")
    print(f"  {'Gross PnL (USD)':<20}: ${gross_pnl_usd:+,.2f}  (before fees)")
    print(f"  {'Fees (USD)':<20}: -${total_fees_eth * avg_spot:,.2f}")
    print(f"  {'Net return':<20}: {net_return:+.1f}%  (on $2,200 capital)")
    print(f"  {'BullPut':<20}: {bp_wr}")
    print(f"  {'BearCall':<20}: {bc_wr}")
    print(f"  {'TP / SL / Exp':<20}: {tp} / {sl} / {exp}")
    if wins:
        print(f"  {'Avg win':<20}: +{np.mean(wins):.5f} ETH  (~${np.mean(wins)*avg_spot:+.2f})")
    if losses:
        print(f"  {'Avg loss':<20}: {np.mean(losses):.5f} ETH  (~${np.mean(losses)*avg_spot:+.2f})")
    if wins and losses and sum(losses) != 0:
        print(f"  {'Profit factor':<20}: {abs(sum(wins)/sum(losses)):.2f}")


def save_trades_csv(trades, avg_spot, path):
    fee_per_contract = 0.0003
    rows = []
    for t in trades:
        pnl_usd = (t.realized_pnl or 0) * t.underlying_at_entry
        if t.short_call_strike > 0 and t.short_put_strike > 0:
            stype = "IronCond"; stk = f"SC={int(t.short_call_strike)} SP={int(t.short_put_strike)}"
            n_legs = 4
        elif t.short_call_strike > 0:
            stype = "BearCall"; stk = f"SC={int(t.short_call_strike)} LC={int(t.long_call_strike)}"
            n_legs = 2
        else:
            stype = "BullPut "; stk = f"SP={int(t.short_put_strike)}  LP={int(t.long_put_strike)}"
            n_legs = 2
        fee_eth = fee_per_contract * n_legs * 2
        rows.append({
            "Date":       str(t.entry_time.date()),
            "Type":       stype,
            "ETH":        f"${t.underlying_at_entry:.0f}",
            "Strikes":    stk,
            "Credit ETH": f"{t.credit_received:.5f}",
            "Fees USD":   f"${fee_eth * t.underlying_at_entry:.2f}",
            "PnL ETH":    f"{(t.realized_pnl or 0):+.5f}",
            "PnL USD":    f"${pnl_usd:+.0f}",
            "Reason":     (t.exit_reason or "")[:28],
        })
    os.makedirs(os.path.dirname(path), exist_ok=True)
    pd.DataFrame(rows).to_csv(path, index=False)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

print("=" * 66)
print("  0 DTE Backtest -- Sydney Timed Entry  (No Iron Condor)")
print("  Period : Mar 2024 - Mar 2026  |  Capital: $2,200")
print("  1 PM Sydney AEDT = 02:00 UTC  |  T = 6h before expiry")
print("  2 PM Sydney AEDT = 03:00 UTC  |  T = 5h before expiry")
print("  Expiry : 08:00 UTC = 7:00 PM Sydney AEDT")
print("  Signal : EMA(9/21) -- Bull Put or Bear Call only")
print("=" * 66)

end_dt   = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
start_dt = end_dt - timedelta(days=730)

print(f"\n[1/5] Fetching ETH price history...")
ohlcv = fetch_eth_ohlcv(start_dt, end_dt)
if ohlcv.empty:
    print("ERROR: no price data"); sys.exit(1)
print(f"      {len(ohlcv)} candles | ${ohlcv['close'].iloc[0]:.0f} -> ${ohlcv['close'].iloc[-1]:.0f}")

print(f"\n[2/5] Fetching Deribit IV history...")
try:
    iv_df = fetch_historical_iv()
    iv_df = iv_df[iv_df["date"] >= start_dt.date()].reset_index(drop=True)
    print(f"      {len(iv_df)} IV points | avg {iv_df['iv'].mean():.1f}%")
except Exception as e:
    print(f"      WARNING: {e}")
    iv_df = pd.DataFrame()

ohlcv["date"] = ohlcv["timestamp"].dt.date
if not iv_df.empty:
    merged = ohlcv.merge(iv_df[["date", "iv_decimal"]], on="date", how="left")
    merged["iv_decimal"] = merged["iv_decimal"].ffill().fillna(0.80)
else:
    merged = ohlcv.copy()
    merged["iv_decimal"] = 0.80
merged = merged.sort_values("date").reset_index(drop=True)

avg_spot = float(merged["close"].mean())

all_results = {}

for i, window in enumerate(ENTRY_WINDOWS, 3):
    label    = window["label"]
    T_days   = window["T_days"]
    tag      = "1pm" if "1 PM" in label else "2pm"
    parquet_dir = f"data/0dte_{tag}_sydney/parquet"

    print(f"\n[{i}/5] Building chains + running backtest for {label}...")
    results, storage, built = run_window_backtest(
        label, T_days, merged, parquet_dir, start_dt, end_dt
    )
    print(f"      {built} daily snapshots | {len(results.get('trades', []))} trades")
    all_results[label] = results

    save_trades_csv(
        results.get("trades", []),
        avg_spot,
        f"data/0dte_{tag}_sydney/trade_history.csv",
    )

# ---------------------------------------------------------------------------
# Combined results
# ---------------------------------------------------------------------------

print(f"\n\n{'=' * 66}")
print(f"  RESULTS  --  0 DTE Sydney Timed Entry (No Iron Condor)")
print(f"{'=' * 66}")

for label, results in all_results.items():
    print_window_summary(label, results, avg_spot)

# Side-by-side comparison table
print(f"\n{'=' * 66}")
print(f"  SIDE-BY-SIDE COMPARISON")
print(f"{'=' * 66}")

rows = []
for label, results in all_results.items():
    m      = results.get("metrics", {})
    trades = results.get("trades", [])
    if not trades:
        continue
    wins   = [t.realized_pnl for t in trades if (t.realized_pnl or 0) > 0]
    losses = [t.realized_pnl for t in trades if (t.realized_pnl or 0) <= 0]
    fee_eth = sum(0.0003 * (2) * 2 for t in trades)  # all 2-leg (no IC)
    net_pnl = m.get("total_pnl", 0) * avg_spot
    rows.append({
        "Window":   label,
        "Trades":   len(trades),
        "Win%":     f"{len(wins)/len(trades)*100:.1f}%",
        "Net PnL":  f"${net_pnl:+,.0f}",
        "Fees":     f"-${fee_eth * avg_spot:,.0f}",
        "Net Ret":  f"{net_pnl/2200*100:+.1f}%",
        "PF":       f"{abs(sum(wins)/sum(losses)):.2f}" if losses and sum(losses) != 0 else "inf",
        "AvgWin":   f"${np.mean(wins)*avg_spot:+.1f}" if wins else "-",
        "AvgLoss":  f"${np.mean(losses)*avg_spot:+.1f}" if losses else "-",
    })

if rows:
    print(pd.DataFrame(rows).to_string(index=False))

print(f"\n  Trade logs:")
print(f"    data/0dte_1pm_sydney/trade_history.csv")
print(f"    data/0dte_2pm_sydney/trade_history.csv")
print()
