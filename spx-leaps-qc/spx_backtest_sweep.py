#!/usr/bin/env python3
"""
SPX LEAPS Call + Conditional Put Hedge — Parameter Sweep Backtest
==================================================================
Strategy:
  - Buy a 300-DTE SPX call when VIX < 20.
  - Buy a 90-day ATM put hedge ONLY when QQQ P/E > its 5-year rolling average.
    (When QQQ is cheap relative to history, skip the put hedge.)
  - Exit on: 100% profit target hit | crash rule triggered | DTE expiry.

QQQ P/E approximation (yfinance has no historical P/E):
  Current trailing P/E is fetched live from yfinance.
  Historical P/E is back-calculated as:
    PE_t = (QQQ_price_t / QQQ_price_now) * PE_now * (1 + earnings_growth)^years_ago
  Earnings growth assumed at 12% per year (Nasdaq-100 historical average).

Sweep dimensions (27 combos):
  VIX threshold  : 20 (fixed)
  Crash rule set : A / B / C
  Call delta     : 0.40 / 0.50 / 0.60
  Call DTE       : 300 (fixed)
  Put cost frac  : 0.05 / 0.10 / 0.15  (only when QQQ PE > 5yr avg)
  Profit target  : 100% (fixed)
"""

import warnings
warnings.filterwarnings("ignore")

import itertools
import numpy as np
import pandas as pd
import yfinance as yf
from scipy.stats import norm

# ─── Black-Scholes helpers ───────────────────────────────────────────────────

def bs_call(S, K, T, r, sigma):
    if T <= 0:
        return max(S - K, 0.0)
    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)
    return S * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2)


def bs_put(S, K, T, r, sigma):
    if T <= 0:
        return max(K - S, 0.0)
    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)
    return K * np.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)


def strike_from_call_delta(S, T, r, sigma, delta):
    d1 = norm.ppf(delta)
    return S * np.exp(-d1 * sigma * np.sqrt(T) + (r + 0.5 * sigma ** 2) * T)


# ─── Fixed parameters ─────────────────────────────────────────────────────────

CRASH_RULES = {
    "A": {7: -0.04, 10: -0.05, 14: -0.07, 30: -0.10},
    "B": {7: -0.03, 10: -0.04, 14: -0.06, 30: -0.08},
    "C": {7: -0.05, 10: -0.06, 14: -0.08, 30: -0.12},
}

VIX_THRESHOLD   = 20
CALL_DTE        = 300
PROFIT_TARGET   = 1.00        # 100 %
PUT_TENOR_DAYS  = 90
R               = 0.045       # risk-free rate (also earned on idle cash)
INITIAL_CAPITAL = 100_000.0
COOLDOWN_DAYS   = 5
EARNINGS_GROWTH = 0.12        # assumed 12% annual Nasdaq earnings growth

# Sweep
CALL_DELTAS     = [0.40, 0.50, 0.60]
PUT_COST_FRACS  = [0.05, 0.10, 0.15]
RISK_PER_TRADES = [0.10, 0.20, 0.30, 0.50]   # fraction of capital risked per trade

# ─── Download market data ─────────────────────────────────────────────────────

print("Downloading SPX, VIX and QQQ data ...")
_spx = yf.download("^GSPC", start="2010-01-01", end="2026-03-01",
                   auto_adjust=True, progress=False)["Close"].squeeze()
_vix = yf.download("^VIX",  start="2010-01-01", end="2026-03-01",
                   auto_adjust=True, progress=False)["Close"].squeeze()
_qqq = yf.download("QQQ",   start="2005-01-01", end="2026-03-01",
                   auto_adjust=True, progress=False)["Close"].squeeze()

df = pd.DataFrame({"SPX": _spx, "VIX": _vix}).dropna()
df.index = pd.to_datetime(df.index)

prices = df["SPX"].values
vixxes = df["VIX"].values
dates  = df.index
N      = len(df)

print(f"SPX data: {dates[0].date()} to {dates[-1].date()}, {N} trading days")

# ─── Build QQQ historical P/E series ─────────────────────────────────────────

print("Building QQQ historical P/E approximation ...")

# Fetch current trailing P/E from yfinance
qqq_info   = yf.Ticker("QQQ").info
current_pe = qqq_info.get("trailingPE", None)
if current_pe is None or current_pe <= 0:
    current_pe = 35.0
    print(f"  WARNING: QQQ trailingPE not found in yfinance, using fallback PE={current_pe}")
else:
    print(f"  QQQ current trailing P/E from yfinance: {current_pe:.1f}x")

# Align QQQ prices to SPX calendar
qqq_aligned = _qqq.reindex(df.index, method="ffill").ffill().bfill()
P_now = float(qqq_aligned.iloc[-1])
ref_date = df.index[-1]

# Back-calculate historical P/E:
#   PE_t = (QQQ_t / QQQ_now) * PE_now * (1 + g)^years_ago
pe_series = pd.Series(index=df.index, dtype=float)
for date in df.index:
    years_ago = (ref_date - date).days / 365.25
    pe_series[date] = (qqq_aligned[date] / P_now) * current_pe * (1 + EARNINGS_GROWTH) ** years_ago

# 5-year rolling average (252 trading days * 5)
pe_5yr_avg = pe_series.rolling(window=252 * 5, min_periods=252).mean()

# Merge into df
df["QQQ_PE"]      = pe_series
df["QQQ_PE_5yr"]  = pe_5yr_avg

# Stats
valid_mask = df["QQQ_PE_5yr"].notna()
pct_hedge_days = (df.loc[valid_mask, "QQQ_PE"] > df.loc[valid_mask, "QQQ_PE_5yr"]).mean()
print(f"  Put hedge active on {pct_hedge_days:.1%} of trading days (QQQ PE > 5yr avg)")

# ─── Backtest core ────────────────────────────────────────────────────────────

DAILY_RF = (1 + R) ** (1 / 252) - 1   # daily risk-free return


def run_backtest(crash_set, call_delta, put_cost_frac, risk_per_trade):
    """
    Buy 300-DTE SPX call when VIX < 20.
    Buy 90-day put hedge only when QQQ P/E > 5-year average.
    Exit at: 100% profit | crash rule trigger | expiry.
    Idle cash earns the daily risk-free rate.
    """
    crash_rules = CRASH_RULES[crash_set]
    max_lb      = max(crash_rules.keys())
    start_i     = max_lb + 1

    free_capital = INITIAL_CAPITAL
    position     = None
    cooldown     = 0
    trades       = []
    daily_equity = []

    for i in range(start_i, N):
        S  = prices[i]
        iv = max(vixxes[i] / 100.0, 0.05)

        # ── Conditional put hedge flag ────────────────────────────────────
        pe_now     = df["QQQ_PE"].iloc[i]
        pe_avg     = df["QQQ_PE_5yr"].iloc[i]
        use_put    = (pd.notna(pe_avg) and pe_now > pe_avg)  # expensive market

        # ── Mark-to-market equity ─────────────────────────────────────────
        if position is not None:
            days_held = (dates[i] - position["entry_date"]).days
            T_call = max((CALL_DTE - days_held) / 365.25, 1 / 365.25)
            T_put  = max((PUT_TENOR_DAYS - days_held) / 365.25, 1 / 365.25)
            call_v = bs_call(S, position["K_call"], T_call, R, iv)
            put_v  = (bs_put(S, position["K_put"], T_put, R, iv) * position["put_units"]
                      if position["has_put"] else 0.0)
            pos_value = (call_v + put_v) * position["units"]
            equity = free_capital + pos_value
        else:
            equity = free_capital

        daily_equity.append(equity)

        # Idle cash earns risk-free rate daily
        free_capital *= (1 + DAILY_RF)

        if cooldown > 0:
            cooldown -= 1
            continue

        # ── Entry ─────────────────────────────────────────────────────────
        if position is None:
            if vixxes[i] < VIX_THRESHOLD:
                T_call = CALL_DTE / 365.25
                K_call = strike_from_call_delta(S, T_call, R, iv, call_delta)
                call_price = bs_call(S, K_call, T_call, R, iv)
                if call_price <= 0:
                    continue

                # Put hedge (conditional on valuation)
                if use_put:
                    K_put     = S
                    put_price = bs_put(S, K_put, PUT_TENOR_DAYS / 365.25, R, iv)
                    put_units = ((put_cost_frac * call_price) / put_price
                                 if put_price > 0 else 0.0)
                    put_extra = put_cost_frac * call_price
                else:
                    K_put     = S
                    put_units = 0.0
                    put_extra = 0.0

                total_cost_per_unit = call_price + put_extra
                units               = (free_capital * risk_per_trade) / total_cost_per_unit
                total_invested      = total_cost_per_unit * units

                free_capital -= total_invested
                position = {
                    "entry_date":     dates[i],
                    "K_call":         K_call,
                    "K_put":          K_put,
                    "call_price0":    call_price,
                    "put_units":      put_units,
                    "units":          units,
                    "total_invested": total_invested,
                    "has_put":        use_put,
                }
            continue

        # ── Monitor open position ─────────────────────────────────────────
        days_held = (dates[i] - position["entry_date"]).days
        T_call = max((CALL_DTE - days_held) / 365.25, 1 / 365.25)
        T_put  = max((PUT_TENOR_DAYS - days_held) / 365.25, 1 / 365.25)

        call_v = bs_call(S, position["K_call"], T_call, R, iv)
        put_v  = (bs_put(S, position["K_put"], T_put, R, iv) * position["put_units"]
                  if position["has_put"] else 0.0)
        pos_value = (call_v + put_v) * position["units"]

        call_pnl_pct = (call_v - position["call_price0"]) / position["call_price0"]

        exit_reason = None

        if call_pnl_pct >= PROFIT_TARGET:
            exit_reason = "profit_target"

        if exit_reason is None:
            for lb, thresh in crash_rules.items():
                if i - lb >= 0:
                    drop = (S - prices[i - lb]) / prices[i - lb]
                    if drop <= thresh:
                        exit_reason = f"crash_{lb}d"
                        break

        if exit_reason is None and days_held >= CALL_DTE:
            exit_reason = "expiry"

        # ── Exit ──────────────────────────────────────────────────────────
        if exit_reason:
            pnl = pos_value - position["total_invested"]
            free_capital += pos_value

            trades.append({
                "entry_date":   str(position["entry_date"].date()),
                "exit_date":    str(dates[i].date()),
                "days_held":    days_held,
                "call_pnl_pct": round(call_pnl_pct, 4),
                "total_pnl":    round(pnl, 2),
                "exit_reason":  exit_reason,
                "win":          pnl > 0,
                "had_put":      position["has_put"],
            })
            position = None
            cooldown = COOLDOWN_DAYS

    # Mark-to-market open position at end of data
    final_equity = free_capital
    if position is not None:
        S  = prices[-1]
        iv = max(vixxes[-1] / 100.0, 0.05)
        days_held = (dates[-1] - position["entry_date"]).days
        T_call = max((CALL_DTE - days_held) / 365.25, 1 / 365.25)
        T_put  = max((PUT_TENOR_DAYS - days_held) / 365.25, 1 / 365.25)
        call_v = bs_call(S, position["K_call"], T_call, R, iv)
        put_v  = (bs_put(S, position["K_put"], T_put, R, iv) * position["put_units"]
                  if position["has_put"] else 0.0)
        final_equity += (call_v + put_v) * position["units"]

    if not trades:
        return None

    tdf = pd.DataFrame(trades)
    n_trades = len(tdf)
    win_rate = tdf["win"].mean()
    pct_hedged = tdf["had_put"].mean()

    total_return = (final_equity - INITIAL_CAPITAL) / INITIAL_CAPITAL
    years = (dates[-1] - dates[start_i]).days / 365.25
    cagr  = ((final_equity / INITIAL_CAPITAL) ** (1.0 / max(years, 0.5)) - 1.0
             if final_equity > 0 else -1.0)

    eq   = np.array(daily_equity)
    peak = np.maximum.accumulate(eq)
    peak[peak == 0] = INITIAL_CAPITAL
    max_dd = float(((eq - peak) / peak).min())

    pnl_arr = tdf["total_pnl"].values / INITIAL_CAPITAL
    if len(pnl_arr) > 2 and pnl_arr.std() > 0:
        avg_days      = max(tdf["days_held"].mean(), 1)
        trades_per_yr = 252.0 / avg_days
        sharpe = (pnl_arr.mean() / pnl_arr.std()) * np.sqrt(trades_per_yr)
    else:
        sharpe = 0.0

    return {
        "total_return": round(total_return, 4),
        "cagr":         round(cagr, 4),
        "max_drawdown": round(max_dd, 4),
        "sharpe":       round(sharpe, 4),
        "n_trades":     n_trades,
        "win_rate":     round(win_rate, 4),
        "pct_hedged":   round(pct_hedged, 4),
    }


# ─── Parameter sweep ─────────────────────────────────────────────────────────

# ─── SPX buy-and-hold benchmark ───────────────────────────────────────────────

start_i_bench = max(CRASH_RULES["B"].keys()) + 1
spx_start = prices[start_i_bench]
spx_end   = prices[-1]
bench_years = (dates[-1] - dates[start_i_bench]).days / 365.25
spx_total_return = (spx_end - spx_start) / spx_start
spx_cagr = (spx_end / spx_start) ** (1 / bench_years) - 1
spx_eq   = prices[start_i_bench:] / spx_start * INITIAL_CAPITAL
spx_peak = np.maximum.accumulate(spx_eq)
spx_max_dd = float(((spx_eq - spx_peak) / spx_peak).min())

print(f"\nSPX BUY-AND-HOLD BENCHMARK ({dates[start_i_bench].date()} to {dates[-1].date()}):")
print(f"  Total Return : {spx_total_return:.2%}")
print(f"  CAGR         : {spx_cagr:.2%}")
print(f"  Max Drawdown : {spx_max_dd:.2%}")

# ─── Sweep ────────────────────────────────────────────────────────────────────

combos = list(itertools.product(
    list(CRASH_RULES.keys()),
    CALL_DELTAS,
    PUT_COST_FRACS,
    RISK_PER_TRADES,
))
total = len(combos)
print(f"\nRunning {total} backtests (VIX<20, DTE=300, Profit=100%) ...\n")

results = []
for idx, (crash_s, c_delta, put_frac, rpt) in enumerate(combos, 1):
    r = run_backtest(crash_s, c_delta, put_frac, rpt)
    if r is None:
        continue
    results.append({
        "VIX_threshold":    VIX_THRESHOLD,
        "crash_rule_set":   crash_s,
        "call_delta":       c_delta,
        "call_DTE":         CALL_DTE,
        "put_cost_fraction": put_frac,
        "risk_per_trade":   f"{int(rpt*100)}%",
        "profit_target":    "100%",
        "Total_Return":     f"{r['total_return']:.2%}",
        "CAGR":             f"{r['cagr']:.2%}",
        "Max_Drawdown":     f"{r['max_drawdown']:.2%}",
        "Sharpe":           round(r["sharpe"], 3),
        "Trades":           r["n_trades"],
        "WinRate":          f"{r['win_rate']:.2%}",
        "Pct_Hedged":       f"{r['pct_hedged']:.2%}",
        "_sharpe":          r["sharpe"],
        "_cagr":            r["cagr"],
    })

print(f"Done. {len(results)} valid results.")

# ─── Save results ─────────────────────────────────────────────────────────────

DISPLAY_COLS = [
    "VIX_threshold", "crash_rule_set", "call_delta", "call_DTE",
    "put_cost_fraction", "risk_per_trade", "profit_target",
    "Total_Return", "CAGR", "Max_Drawdown", "Sharpe", "Trades", "WinRate", "Pct_Hedged",
]

results_df = pd.DataFrame(results)
csv_path = "C:/Users/Administrator/Desktop/projects/spx_sweep_results.csv"
results_df[DISPLAY_COLS].to_csv(csv_path, index=False)
print(f"Results saved to: {csv_path}\n")

# ─── Print all results sorted by Sharpe ──────────────────────────────────────

pd.set_option("display.max_columns", None)
pd.set_option("display.width", 220)

all_sorted = results_df.sort_values("_sharpe", ascending=False)[DISPLAY_COLS].reset_index(drop=True)
all_sorted.index += 1

print("=" * 120)
print("ALL RESULTS — Ranked by Sharpe  |  Conditional Put: skipped when QQQ PE < 5yr avg")
print(f"SPX Benchmark: CAGR {spx_cagr:.2%}, Max DD {spx_max_dd:.2%}")
print("=" * 120)
print(all_sorted.to_string())
print("=" * 120)

# Top 10 Sharpe
top10 = results_df.nlargest(10, "_sharpe")[DISPLAY_COLS].reset_index(drop=True)
top10.index += 1
print("\nTOP 10 by Sharpe:")
print(top10.to_string())

# Top 10 CAGR
top10c = results_df.nlargest(10, "_cagr")[DISPLAY_COLS].reset_index(drop=True)
top10c.index += 1
print("\nTOP 10 by CAGR:")
print(top10c.to_string())
