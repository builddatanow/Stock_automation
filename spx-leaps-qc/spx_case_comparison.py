#!/usr/bin/env python3
"""
SPX Strategy — 3-Case Comparison
==================================
All cases share:
  Ticker        : SPX
  VIX threshold : 20
  Call delta    : 0.40
  Call DTE      : 300
  Profit target : 100%
  Risk/trade    : 30%
  Crash rules   : Set B (7d -3%, 10d -4%, 14d -6%, 30d -8%)
  Period        : 2010 - 2026
  Valuation     : SPX P/E (back-calculated from SPY trailing P/E)

Case 1 : No put hedge at all
Case 2 : Buy put only when SPX P/E > 5-year rolling average
Case 3 : Always buy put
"""

import warnings
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd
import yfinance as yf
from scipy.stats import norm

# ─── Black-Scholes ────────────────────────────────────────────────────────────

def bs_call(S, K, T, r, sigma):
    if T <= 0: return max(S - K, 0.0)
    d1 = (np.log(S/K) + (r + 0.5*sigma**2)*T) / (sigma*np.sqrt(T))
    d2 = d1 - sigma*np.sqrt(T)
    return S*norm.cdf(d1) - K*np.exp(-r*T)*norm.cdf(d2)

def bs_put(S, K, T, r, sigma):
    if T <= 0: return max(K - S, 0.0)
    d1 = (np.log(S/K) + (r + 0.5*sigma**2)*T) / (sigma*np.sqrt(T))
    d2 = d1 - sigma*np.sqrt(T)
    return K*np.exp(-r*T)*norm.cdf(-d2) - S*norm.cdf(-d1)

def strike_from_call_delta(S, T, r, sigma, delta):
    d1 = norm.ppf(delta)
    return S * np.exp(-d1*sigma*np.sqrt(T) + (r + 0.5*sigma**2)*T)

# ─── Parameters ───────────────────────────────────────────────────────────────

CRASH_RULES    = {7: -0.03, 10: -0.04, 14: -0.06, 30: -0.08}
VIX_THRESHOLD  = 20
CALL_DTE       = 300
PROFIT_TARGET  = 1.00
PUT_TENOR_DAYS = 90
PUT_COST_FRAC  = 0.10
R              = 0.045
INITIAL_CAPITAL= 100_000.0
RISK_PER_TRADE = 0.30
CALL_DELTA     = 0.40
COOLDOWN_DAYS  = 5
EARNINGS_GROWTH= 0.10   # SPX earnings growth ~10% historical
DAILY_RF       = (1 + R) ** (1/252) - 1

# ─── Download data ────────────────────────────────────────────────────────────

print("Downloading SPX, VIX and SPY data ...")
_spx = yf.download("^GSPC", start="2010-01-01", end="2026-03-01",
                   auto_adjust=True, progress=False)["Close"].squeeze()
_vix = yf.download("^VIX",  start="2010-01-01", end="2026-03-01",
                   auto_adjust=True, progress=False)["Close"].squeeze()
_spy = yf.download("SPY",   start="2005-01-01", end="2026-03-01",
                   auto_adjust=True, progress=False)["Close"].squeeze()

df = pd.DataFrame({"SPX": _spx, "VIX": _vix}).dropna()
df.index = pd.to_datetime(df.index)
prices = df["SPX"].values
vixxes = df["VIX"].values
dates  = df.index
N      = len(df)

print(f"SPX data: {dates[0].date()} to {dates[-1].date()}, {N} trading days")

# ─── SPX P/E series (using SPY as proxy) ─────────────────────────────────────

print("Building SPX P/E approximation using SPY ...")
spy_info   = yf.Ticker("SPY").info
current_pe = spy_info.get("trailingPE", None)
if not current_pe or current_pe <= 0:
    current_pe = 22.0
    print(f"  WARNING: SPY trailingPE not found, using fallback {current_pe}x")
else:
    print(f"  SPY current trailing P/E: {current_pe:.1f}x  (proxy for SPX P/E)")

spy_aligned = _spy.reindex(df.index, method="ffill").ffill().bfill()
P_now    = float(spy_aligned.iloc[-1])
ref_date = df.index[-1]

pe_series = pd.Series(index=df.index, dtype=float)
for date in df.index:
    years_ago = (ref_date - date).days / 365.25
    pe_series[date] = (spy_aligned[date] / P_now) * current_pe * (1 + EARNINGS_GROWTH)**years_ago

pe_5yr_avg = pe_series.rolling(window=252*5, min_periods=252).mean()
df["SPX_PE"]      = pe_series
df["SPX_PE_5yr"]  = pe_5yr_avg

valid = df["SPX_PE_5yr"].notna()
pct_expensive = (df.loc[valid, "SPX_PE"] > df.loc[valid, "SPX_PE_5yr"]).mean()
print(f"  SPX expensive (PE > 5yr avg) on {pct_expensive:.1%} of days\n")

# ─── SPX benchmark ────────────────────────────────────────────────────────────

max_lb    = max(CRASH_RULES.keys())
start_i   = max_lb + 1
spx_start = prices[start_i]
spx_end   = prices[-1]
bench_yrs = (dates[-1] - dates[start_i]).days / 365.25
spx_tr    = (spx_end - spx_start) / spx_start
spx_cagr  = (spx_end / spx_start) ** (1/bench_yrs) - 1
spx_eq    = prices[start_i:] / spx_start * INITIAL_CAPITAL
spx_peak  = np.maximum.accumulate(spx_eq)
spx_mdd   = float(((spx_eq - spx_peak) / spx_peak).min())

# ─── Backtest engine ──────────────────────────────────────────────────────────

def run_backtest(put_mode):
    """
    put_mode : 'none'      - never buy put
               'conditional' - buy put when SPX PE > 5yr avg
               'always'    - always buy put
    """
    free_capital = INITIAL_CAPITAL
    position     = None
    cooldown     = 0
    trades       = []
    daily_equity = []

    for i in range(start_i, N):
        S  = prices[i]
        iv = max(vixxes[i] / 100.0, 0.05)

        pe_now = df["SPX_PE"].iloc[i]
        pe_avg = df["SPX_PE_5yr"].iloc[i]

        if put_mode == "none":
            use_put = False
        elif put_mode == "conditional":
            use_put = pd.notna(pe_avg) and pe_now > pe_avg
        else:  # always
            use_put = True

        # Mark-to-market
        if position is not None:
            dh     = (dates[i] - position["entry_date"]).days
            T_call = max((CALL_DTE - dh) / 365.25, 1/365.25)
            T_put  = max((PUT_TENOR_DAYS - dh) / 365.25, 1/365.25)
            call_v = bs_call(S, position["K_call"], T_call, R, iv)
            put_v  = (bs_put(S, position["K_put"], T_put, R, iv) * position["put_units"]
                      if position["has_put"] else 0.0)
            pos_val = (call_v + put_v) * position["units"]
            equity  = free_capital + pos_val
        else:
            equity = free_capital

        daily_equity.append(equity)
        free_capital *= (1 + DAILY_RF)

        if cooldown > 0:
            cooldown -= 1
            continue

        # ── Entry ─────────────────────────────────────────────────────────
        if position is None:
            if vixxes[i] < VIX_THRESHOLD:
                T_call = CALL_DTE / 365.25
                K_call = strike_from_call_delta(S, T_call, R, iv, CALL_DELTA)
                call_price = bs_call(S, K_call, T_call, R, iv)
                if call_price <= 0:
                    continue

                if use_put:
                    K_put     = S
                    put_price = bs_put(S, K_put, PUT_TENOR_DAYS/365.25, R, iv)
                    put_units = (PUT_COST_FRAC * call_price / put_price) if put_price > 0 else 0.0
                    put_extra = PUT_COST_FRAC * call_price
                else:
                    K_put, put_units, put_extra = S, 0.0, 0.0

                total_cost = call_price * (1 + (PUT_COST_FRAC if use_put else 0))
                units      = (free_capital * RISK_PER_TRADE) / total_cost
                invested   = total_cost * units
                free_capital -= invested

                position = {
                    "entry_date": dates[i],
                    "K_call":     K_call,
                    "K_put":      K_put,
                    "call_price0":call_price,
                    "put_units":  put_units,
                    "units":      units,
                    "invested":   invested,
                    "has_put":    use_put,
                }
            continue

        # ── Monitor ───────────────────────────────────────────────────────
        dh     = (dates[i] - position["entry_date"]).days
        T_call = max((CALL_DTE - dh) / 365.25, 1/365.25)
        T_put  = max((PUT_TENOR_DAYS - dh) / 365.25, 1/365.25)
        call_v = bs_call(S, position["K_call"], T_call, R, iv)
        put_v  = (bs_put(S, position["K_put"], T_put, R, iv) * position["put_units"]
                  if position["has_put"] else 0.0)
        pos_val      = (call_v + put_v) * position["units"]
        call_pnl_pct = (call_v - position["call_price0"]) / position["call_price0"]

        exit_reason = None
        if call_pnl_pct >= PROFIT_TARGET:
            exit_reason = "profit_target"
        if exit_reason is None:
            for lb, thresh in CRASH_RULES.items():
                if i - lb >= 0 and (S - prices[i-lb]) / prices[i-lb] <= thresh:
                    exit_reason = f"crash_{lb}d"
                    break
        if exit_reason is None and dh >= CALL_DTE:
            exit_reason = "expiry"

        # ── Exit ──────────────────────────────────────────────────────────
        if exit_reason:
            pnl = pos_val - position["invested"]
            free_capital += pos_val
            trades.append({
                "entry_date":   str(position["entry_date"].date()),
                "exit_date":    str(dates[i].date()),
                "days_held":    dh,
                "call_pnl_pct": round(call_pnl_pct*100, 2),
                "pnl":          round(pnl, 2),
                "exit_reason":  exit_reason,
                "win":          pnl > 0,
                "has_put":      position["has_put"],
            })
            position = None
            cooldown = COOLDOWN_DAYS

    # Mark-to-market open position at end
    final_equity = free_capital
    if position is not None:
        S = prices[-1]; iv = max(vixxes[-1]/100, 0.05)
        dh     = (dates[-1] - position["entry_date"]).days
        T_call = max((CALL_DTE - dh)/365.25, 1/365.25)
        T_put  = max((PUT_TENOR_DAYS - dh)/365.25, 1/365.25)
        call_v = bs_call(S, position["K_call"], T_call, R, iv)
        put_v  = (bs_put(S, position["K_put"], T_put, R, iv) * position["put_units"]
                  if position["has_put"] else 0.0)
        final_equity += (call_v + put_v) * position["units"]

    if not trades:
        return None

    tdf = pd.DataFrame(trades)
    n   = len(tdf)
    wr  = tdf["win"].mean()
    tr  = (final_equity - INITIAL_CAPITAL) / INITIAL_CAPITAL
    yrs = (dates[-1] - dates[start_i]).days / 365.25
    cagr= (final_equity/INITIAL_CAPITAL)**(1/max(yrs,0.5)) - 1 if final_equity > 0 else -1.0

    eq   = np.array(daily_equity)
    peak = np.maximum.accumulate(eq)
    peak[peak == 0] = INITIAL_CAPITAL
    mdd  = float(((eq - peak)/peak).min())

    pnl_arr = tdf["pnl"].values / INITIAL_CAPITAL
    if len(pnl_arr) > 2 and pnl_arr.std() > 0:
        tpy    = 252 / max(tdf["days_held"].mean(), 1)
        sharpe = (pnl_arr.mean() / pnl_arr.std()) * np.sqrt(tpy)
    else:
        sharpe = 0.0

    exit_counts = tdf["Exit_Reason"].value_counts().to_dict() if "Exit_Reason" in tdf else \
                  tdf["exit_reason"].value_counts().to_dict()
    hedged_pct  = tdf["has_put"].mean()

    return {
        "trades":       n,
        "win_rate":     round(wr*100, 1),
        "total_return": round(tr*100, 2),
        "cagr":         round(cagr*100, 2),
        "max_dd":       round(mdd*100, 2),
        "sharpe":       round(sharpe, 3),
        "final_capital":round(final_equity, 2),
        "hedged_pct":   round(hedged_pct*100, 1),
        "exit_counts":  exit_counts,
        "trades_df":    tdf,
    }

# ─── Run all 3 cases ──────────────────────────────────────────────────────────

cases = {
    "Case 1 — No Put":            "none",
    "Case 2 — Conditional Put":   "conditional",
    "Case 3 — Always Put":        "always",
}

results = {}
for label, mode in cases.items():
    print(f"Running {label} ...")
    results[label] = run_backtest(mode)

# ─── Save transaction CSVs ────────────────────────────────────────────────────

for i, (label, mode) in enumerate(cases.items(), 1):
    r = results[label]
    out = f"C:/Users/Administrator/Desktop/projects/spx_case{i}_transactions.csv"
    r["trades_df"].to_csv(out, index=False)
    print(f"  Saved: spx_case{i}_transactions.csv")

# ─── Comparison table ─────────────────────────────────────────────────────────

print()
print("=" * 80)
print("SPX STRATEGY — 3-CASE COMPARISON  (30% risk/trade, DTE=300, Delta=0.40)")
print(f"SPX Buy-and-Hold Benchmark: CAGR {spx_cagr:.2%}, Max DD {spx_mdd:.2%}, Return {spx_tr:.2%}")
print("=" * 80)

header = f"{'Metric':<25} {'Case 1 No Put':>18} {'Case 2 Cond. Put':>18} {'Case 3 Always Put':>18}"
print(header)
print("-" * 80)

metrics = [
    ("Trades",         "trades",       ""),
    ("Win Rate",       "win_rate",     "%"),
    ("Total Return",   "total_return", "%"),
    ("CAGR",           "cagr",         "%"),
    ("Max Drawdown",   "max_dd",       "%"),
    ("Sharpe Ratio",   "sharpe",       ""),
    ("Final Capital",  "final_capital","$"),
    ("% Trades Hedged","hedged_pct",   "%"),
]

keys = list(cases.keys())
for label, key, unit in metrics:
    vals = []
    for k in keys:
        v = results[k][key]
        if unit == "$":
            vals.append(f"${v:>14,.0f}")
        elif unit == "%":
            vals.append(f"{v:>16.2f}%")
        else:
            vals.append(f"{v:>18}")
    print(f"{label:<25} {''.join(vals)}")

print("-" * 80)
print("\nExit Reason Breakdown:")
for k in keys:
    short = k.split("—")[1].strip()
    ec    = results[k]["exit_counts"]
    print(f"  {short:<20}: {dict(sorted(ec.items()))}")

print()
print("=" * 80)
print("TOP PERFORMER BY METRIC:")
cagrs   = {k: results[k]["cagr"]   for k in keys}
sharpes = {k: results[k]["sharpe"] for k in keys}
mdds    = {k: results[k]["max_dd"] for k in keys}
print(f"  Best CAGR     : {max(cagrs,   key=cagrs.get)}")
print(f"  Best Sharpe   : {max(sharpes, key=sharpes.get)}")
print(f"  Lowest Max DD : {min(mdds,    key=mdds.get)}")
print("=" * 80)
