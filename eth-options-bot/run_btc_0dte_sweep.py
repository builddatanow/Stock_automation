"""
BTC 0 DTE Parameter Sweep Backtest
====================================
Same strategy as ETH 0 DTE sweep but for Bitcoin.
Tests all combinations of:
  - Entry times  : 1PM, 2PM, 3PM, 4PM Sydney
  - Take profit  : 40%, 50%, 60%, 75%
  - Stop loss    : 1.5x, 2.0x, 2.5x, 3.0x
  - EMA strength : 0.003, 0.005, 0.010

Key differences from ETH sweep:
  - BTC-PERPETUAL price data
  - BTC historical volatility
  - Strike grid rounded to $500 increments (BTC ~$80k)
  - Option names: BTC-DDMMMYY-STRIKE-C/P

Period: Mar 2024 - Mar 2026 | Capital: $5,000
Results ranked by Net PnL, CAGR, Sharpe.
"""

import sys, os, math, warnings, itertools
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

BASE_URL = "https://www.deribit.com"

# ---------------------------------------------------------------------------
# Data fetchers — BTC
# ---------------------------------------------------------------------------

def fetch_btc_ohlcv(start_dt, end_dt):
    resp = requests.get(
        f"{BASE_URL}/api/v2/public/get_tradingview_chart_data",
        params={"instrument_name": "BTC-PERPETUAL",
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
                        params={"currency": "BTC"}, timeout=20)
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
    expiry = timestamp + timedelta(days=expiry_days)
    T = T_override if T_override is not None else max(expiry_days, 0.25) / 365.0
    T = T / 365.0 if T > 1 else T

    # BTC strike grid: 1.5% steps, rounded to nearest $500
    raw = [anchor_spot * (1 + i * 0.015) for i in range(-12, 13)]
    strikes = sorted(set(round(k / 500) * 500 for k in raw if k > 0))

    def smile_iv(K):
        m = (K - spot) / spot
        return base_iv * (1 + 3.0 * m**2 - 0.5 * m) if m < 0 else base_iv * (1 + 1.5 * m**2)

    quotes = []
    for K in strikes:
        for is_call in [True, False]:
            iv  = max(smile_iv(K), 0.20)
            p   = bs_price(spot, K, T, iv, is_call) / spot
            d   = bs_delta(spot, K, T, iv, is_call)
            if p < 0.000005:
                continue
            bid = max(p * 0.92, 0.000005)
            ask = p * 1.08
            opt = OptionType.CALL if is_call else OptionType.PUT
            sfx = "C" if is_call else "P"
            name = f"BTC-{expiry.strftime('%d%b%y').upper()}-{int(K)}-{sfx}"
            quotes.append(OptionQuote(
                timestamp=timestamp, instrument_name=name,
                strike=K, expiry=expiry, option_type=opt,
                bid=round(bid, 6), ask=round(ask, 6), mark_price=round(p, 6),
                implied_volatility=iv, delta=round(d, 4),
                gamma=0.001, theta=-p / (T * 365) if T > 0 else 0,
                vega=T**0.5 * 0.01, underlying_price=spot,
            ))
    return quotes


setup_logging("WARNING", "logs/btc_0dte_sweep.log")

# ---------------------------------------------------------------------------
# Parameter grid
# ---------------------------------------------------------------------------
ENTRY_WINDOWS = [
    {"label": "1PM-Syd (02:00 UTC)", "T_days": 6/24},
    {"label": "2PM-Syd (03:00 UTC)", "T_days": 5/24},
    {"label": "3PM-Syd (04:00 UTC)", "T_days": 4/24},
    {"label": "4PM-Syd (05:00 UTC)", "T_days": 3/24},
]
TAKE_PROFITS  = [0.40, 0.50, 0.60, 0.75]
STOP_LOSSES   = [1.5, 2.0, 2.5, 3.0]
MIN_STRENGTHS = [0.003, 0.005, 0.010]

CAPITAL = 5000.0   # BTC spreads are $500+ wide; need higher capital than ETH
START   = datetime(2024, 3, 1, tzinfo=timezone.utc)
END     = datetime(2026, 3, 1, tzinfo=timezone.utc)
DAYS    = (END - START).days

# ---------------------------------------------------------------------------
# Build merged OHLCV + IV dataframe
# ---------------------------------------------------------------------------
def build_merged():
    print("[1/2] Fetching BTC price history...")
    ohlcv = fetch_btc_ohlcv(START - timedelta(days=60), END)
    ohlcv["date"] = ohlcv["timestamp"].dt.date
    print(f"      {len(ohlcv)} candles")

    print("[2/2] Fetching BTC historical IV...")
    iv_df = fetch_historical_iv()
    if not iv_df.empty:
        merged = ohlcv.merge(iv_df[["date", "iv_decimal"]], on="date", how="left")
        merged["iv_decimal"] = merged["iv_decimal"].ffill().fillna(0.60)
    else:
        merged = ohlcv.copy()
        merged["iv_decimal"] = 0.60
    print(f"      {len(iv_df)} IV points")
    return merged

# ---------------------------------------------------------------------------
# Build parquet storage for a given T_days (cached per window)
# ---------------------------------------------------------------------------

def build_storage(merged, T_days, cache_dir):
    label = f"T{int(T_days*24)}h"
    parquet_dir = os.path.join(cache_dir, label)
    os.makedirs(parquet_dir, exist_ok=True)

    storage = ParquetStorage(parquet_dir)

    existing = [f for f in os.listdir(parquet_dir) if f.endswith(".parquet")]
    if len(existing) > 100:
        return storage  # already cached

    expiry_dates = [START + timedelta(days=d) for d in range(0, DAYS + 30)]
    expiry_anchor = {}
    for exp in expiry_dates:
        row = merged[merged["date"] <= exp.date()]
        expiry_anchor[exp] = float(row["close"].iloc[-1]) if not row.empty else float(merged["close"].iloc[0])

    for _, row in merged.iterrows():
        date = row["date"]
        spot = float(row["close"])
        iv   = float(row["iv_decimal"])
        dt   = datetime.combine(date, datetime.min.time()).replace(tzinfo=timezone.utc)
        chains = []
        for exp in expiry_dates:
            dte = (exp - dt).days
            if dte < 0 or dte > 2:
                continue
            T_override = T_days / 365.0 if dte == 0 else None
            chains.extend(build_chain(spot, dt, iv, dte, expiry_anchor[exp], T_override=T_override))
        if chains:
            storage.save_quotes(chains)

    return storage

# ---------------------------------------------------------------------------
# Run one combination
# ---------------------------------------------------------------------------

def run_combo(storage, tp, sl, strength):
    cfg = EMASpreadConfig(
        fast_ema=9, slow_ema=21,
        target_dte_min=0, target_dte_max=1,
        short_delta_min=0.20, short_delta_max=0.35,
        wing_delta_min=0.08, wing_delta_max=0.15,
        take_profit_pct=tp,
        stop_loss_multiplier=sl,
        close_dte=0,
        iv_percentile_min=10.0,
        min_trend_strength=strength,
        condor_on_low_iv=False,
        entry_every_day=True,
        account_size=CAPITAL,
        max_risk_per_trade_pct=0.20,
    )

    engine = EMASpreadBacktest(
        config=cfg,
        parquet_storage=storage,
        start_date=str(START.date()),
        end_date=str(END.date()),
        initial_capital=CAPITAL,
        fee_per_contract=0.0001,   # BTC fee per contract (lower than ETH)
        slippage_pct=0.001,
    )
    return engine.run()


def summarise(results, window_label, tp, sl, strength):
    trades = results.get("trades", [])
    if not trades:
        return None

    pnls      = [t.realized_pnl or 0 for t in trades]
    avg_spot  = results.get("metrics", {}).get("avg_spot", 80000)
    total_usd = sum(pnls) * avg_spot

    wins     = sum(1 for p in pnls if p > 0)
    win_rate = wins / len(trades) * 100
    cagr     = ((1 + total_usd / CAPITAL) ** (365 / DAYS) - 1) * 100

    equity = CAPITAL
    peak   = CAPITAL
    max_dd = 0.0
    for p in pnls:
        equity += p * avg_spot
        peak    = max(peak, equity)
        max_dd  = max(max_dd, (peak - equity) / peak * 100)

    daily = [p * avg_spot / CAPITAL for p in pnls]
    sharpe = (np.mean(daily) / np.std(daily) * math.sqrt(252)
              if np.std(daily) > 0 else 0)

    return {
        "window":   window_label,
        "tp_pct":   int(tp * 100),
        "sl_mult":  sl,
        "strength": strength,
        "trades":   len(trades),
        "win_rate": round(win_rate, 1),
        "net_pnl":  round(total_usd, 0),
        "cagr":     round(cagr, 1),
        "max_dd":   round(max_dd, 1),
        "sharpe":   round(sharpe, 2),
    }

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    combos_per_window = len(TAKE_PROFITS) * len(STOP_LOSSES) * len(MIN_STRENGTHS)
    total_combos      = len(ENTRY_WINDOWS) * combos_per_window
    print("=" * 65)
    print("  BTC 0 DTE Parameter Sweep -- Mar 2024 to Mar 2026 | Capital: $5,000")
    print(f"  Entry windows : {len(ENTRY_WINDOWS)}")
    print(f"  Take profits  : {[f'{int(t*100)}%' for t in TAKE_PROFITS]}")
    print(f"  Stop losses   : {STOP_LOSSES}x")
    print(f"  EMA strengths : {MIN_STRENGTHS}")
    print(f"  Total combos  : {total_combos}")
    print("=" * 65)

    merged    = build_merged()
    cache_dir = "data/btc_0dte_sweep_cache"
    os.makedirs(cache_dir, exist_ok=True)

    results = []
    done    = 0

    for window in ENTRY_WINDOWS:
        print(f"\nBuilding chain cache for {window['label']}...")
        storage = build_storage(merged, window["T_days"], cache_dir)
        print(f"  Running {combos_per_window} combos...")

        for tp, sl, strength in itertools.product(TAKE_PROFITS, STOP_LOSSES, MIN_STRENGTHS):
            try:
                r = run_combo(storage, tp, sl, strength)
                row = summarise(r, window["label"], tp, sl, strength)
                if row:
                    results.append(row)
            except Exception as e:
                pass
            done += 1
            if done % 20 == 0:
                pct = done / total_combos * 100
                best = max(results, key=lambda x: x["net_pnl"]) if results else None
                best_str = f" | best so far: ${best['net_pnl']:,.0f}" if best else ""
                print(f"  Progress: {done}/{total_combos} ({pct:.0f}%){best_str}")

    if not results:
        print("No results found.")
        return

    df = pd.DataFrame(results)

    # -- Top 20 by Net PnL
    print("\n" + "=" * 95)
    print("  BTC TOP 20 -- Ranked by Net PnL")
    print("=" * 95)
    hdr = f"{'Window':<25} {'TP':>5} {'SL':>5} {'Str':>6} {'Trades':>7} {'Win%':>6} {'Net PnL':>9} {'CAGR':>7} {'MaxDD':>7} {'Sharpe':>7}"
    print(hdr)
    print("-" * 95)
    for _, r in df.sort_values("net_pnl", ascending=False).head(20).iterrows():
        print(f"{r['window']:<25} {r['tp_pct']:>4}% {r['sl_mult']:>4.1f}x {r['strength']:>6.3f} "
              f"{r['trades']:>7} {r['win_rate']:>5.1f}% ${r['net_pnl']:>8,.0f} "
              f"{r['cagr']:>6.1f}% {r['max_dd']:>6.1f}% {r['sharpe']:>7.2f}")

    # -- Top 10 by CAGR
    print("\n" + "=" * 95)
    print("  BTC TOP 10 -- Ranked by CAGR")
    print("=" * 95)
    print(hdr)
    print("-" * 95)
    for _, r in df.sort_values("cagr", ascending=False).head(10).iterrows():
        print(f"{r['window']:<25} {r['tp_pct']:>4}% {r['sl_mult']:>4.1f}x {r['strength']:>6.3f} "
              f"{r['trades']:>7} {r['win_rate']:>5.1f}% ${r['net_pnl']:>8,.0f} "
              f"{r['cagr']:>6.1f}% {r['max_dd']:>6.1f}% {r['sharpe']:>7.2f}")

    # -- Top 10 by Sharpe
    print("\n" + "=" * 95)
    print("  BTC TOP 10 -- Ranked by Sharpe Ratio")
    print("=" * 95)
    print(hdr)
    print("-" * 95)
    for _, r in df.sort_values("sharpe", ascending=False).head(10).iterrows():
        print(f"{r['window']:<25} {r['tp_pct']:>4}% {r['sl_mult']:>4.1f}x {r['strength']:>6.3f} "
              f"{r['trades']:>7} {r['win_rate']:>5.1f}% ${r['net_pnl']:>8,.0f} "
              f"{r['cagr']:>6.1f}% {r['max_dd']:>6.1f}% {r['sharpe']:>7.2f}")

    # -- Save
    out_path = "data/btc_0dte_sweep_results.csv"
    df.sort_values("net_pnl", ascending=False).to_csv(out_path, index=False)
    print(f"\nFull results saved: {out_path}")
    print(f"Total combos tested: {done} | With trades: {len(results)}")


if __name__ == "__main__":
    main()
