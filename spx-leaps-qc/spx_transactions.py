#!/usr/bin/env python3
"""
Generate detailed transaction files for the top 3 parameter sets (by Sharpe).

Top 3 (from sweep):
  #1  Crash B, delta=0.40, put_cost=0.15, risk=10%  -> Sharpe 1.702, CAGR 18.56%
  #2  Crash B, delta=0.40, put_cost=0.10, risk=10%  -> Sharpe 1.674, CAGR 18.89%
  #3  Crash B, delta=0.40, put_cost=0.05, risk=10%  -> Sharpe 1.634, CAGR 19.25%

Output: spx_transactions_rank1.csv / rank2.csv / rank3.csv
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

def bs_call_delta(S, K, T, r, sigma):
    if T <= 0: return 1.0 if S > K else 0.0
    d1 = (np.log(S/K) + (r + 0.5*sigma**2)*T) / (sigma*np.sqrt(T))
    return float(norm.cdf(d1))

def strike_from_call_delta(S, T, r, sigma, delta):
    d1 = norm.ppf(delta)
    return S * np.exp(-d1*sigma*np.sqrt(T) + (r + 0.5*sigma**2)*T)

# ─── Fixed parameters ─────────────────────────────────────────────────────────

CRASH_RULES = {
    "B": {7: -0.03, 10: -0.04, 14: -0.06, 30: -0.08},
}

VIX_THRESHOLD   = 20
CALL_DTE        = 300
PROFIT_TARGET   = 1.00
PUT_TENOR_DAYS  = 90
R               = 0.045
INITIAL_CAPITAL = 100_000.0
COOLDOWN_DAYS   = 5
EARNINGS_GROWTH = 0.12
DAILY_RF        = (1 + R) ** (1/252) - 1

TOP3 = [
    {"rank": 1, "crash_set": "B", "call_delta": 0.40, "put_cost_frac": 0.15, "risk_per_trade": 0.30},
    {"rank": 2, "crash_set": "B", "call_delta": 0.40, "put_cost_frac": 0.10, "risk_per_trade": 0.30},
    {"rank": 3, "crash_set": "B", "call_delta": 0.40, "put_cost_frac": 0.05, "risk_per_trade": 0.30},
]

# ─── Download data ────────────────────────────────────────────────────────────

print("Downloading data ...")
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

# ─── QQQ P/E series ───────────────────────────────────────────────────────────

qqq_info   = yf.Ticker("QQQ").info
current_pe = qqq_info.get("trailingPE", 35.0)
print(f"QQQ trailing P/E: {current_pe:.1f}x")

qqq_aligned = _qqq.reindex(df.index, method="ffill").ffill().bfill()
P_now    = float(qqq_aligned.iloc[-1])
ref_date = df.index[-1]

pe_series = pd.Series(index=df.index, dtype=float)
for date in df.index:
    years_ago = (ref_date - date).days / 365.25
    pe_series[date] = (qqq_aligned[date] / P_now) * current_pe * (1 + EARNINGS_GROWTH)**years_ago

pe_5yr_avg = pe_series.rolling(window=252*5, min_periods=252).mean()
df["QQQ_PE"]     = pe_series
df["QQQ_PE_5yr"] = pe_5yr_avg

# ─── Backtest with full trade log ─────────────────────────────────────────────

def run_and_log(crash_set, call_delta, put_cost_frac, risk_per_trade):
    crash_rules = CRASH_RULES[crash_set]
    max_lb      = max(crash_rules.keys())
    start_i     = max_lb + 1

    free_capital = INITIAL_CAPITAL
    position     = None
    cooldown     = 0
    trades       = []
    trade_num    = 0

    for i in range(start_i, N):
        S  = prices[i]
        iv = max(vixxes[i] / 100.0, 0.05)
        pe_now = df["QQQ_PE"].iloc[i]
        pe_avg = df["QQQ_PE_5yr"].iloc[i]
        use_put = pd.notna(pe_avg) and pe_now > pe_avg

        # Idle cash earns risk-free rate
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

                if use_put:
                    K_put     = S
                    put_price = bs_put(S, K_put, PUT_TENOR_DAYS/365.25, R, iv)
                    put_units = (put_cost_frac * call_price / put_price) if put_price > 0 else 0.0
                    put_extra = put_cost_frac * call_price
                    put_price_entry = put_price
                else:
                    K_put           = S
                    put_units       = 0.0
                    put_extra       = 0.0
                    put_price_entry = 0.0

                total_cost_per_unit = call_price + put_extra
                units               = (free_capital * risk_per_trade) / total_cost_per_unit
                total_invested      = total_cost_per_unit * units
                free_capital       -= total_invested
                trade_num          += 1

                position = {
                    "trade_num":        trade_num,
                    "entry_date":       dates[i],
                    "entry_spx":        S,
                    "entry_vix":        vixxes[i],
                    "entry_iv":         iv,
                    "K_call":           K_call,
                    "call_dte":         CALL_DTE,
                    "call_price0":      call_price,
                    "entry_delta":      call_delta,
                    "has_put":          use_put,
                    "K_put":            K_put,
                    "put_price0":       put_price_entry,
                    "put_units":        put_units,
                    "put_extra":        put_extra,
                    "units":            units,
                    "total_invested":   total_invested,
                    "capital_before":   free_capital + total_invested,
                    "qqq_pe_entry":     round(pe_now, 1),
                    "qqq_pe_avg_entry": round(pe_avg, 1) if pd.notna(pe_avg) else None,
                }
            continue

        # ── Monitor ───────────────────────────────────────────────────────
        days_held = (dates[i] - position["entry_date"]).days
        T_call = max((CALL_DTE - days_held) / 365.25, 1/365.25)
        T_put  = max((PUT_TENOR_DAYS - days_held) / 365.25, 1/365.25)

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
            capital_after = free_capital + pos_value
            free_capital  = capital_after

            # Compute actual delta at exit
            exit_delta = bs_call_delta(S, position["K_call"], T_call, R, iv)

            trades.append({
                "Trade_#":            position["trade_num"],
                "Entry_Date":         str(position["entry_date"].date()),
                "Exit_Date":          str(dates[i].date()),
                "Days_Held":          days_held,
                # SPX levels
                "SPX_Entry":          round(position["entry_spx"], 2),
                "SPX_Exit":           round(S, 2),
                "SPX_Change_%":       round((S - position["entry_spx"]) / position["entry_spx"] * 100, 2),
                # VIX
                "VIX_Entry":          round(position["entry_vix"], 2),
                "VIX_Exit":           round(vixxes[i], 2),
                # Call details
                "Call_Strike":        round(position["K_call"], 2),
                "Call_DTE":           CALL_DTE,
                "Call_Entry_Price":   round(position["call_price0"], 2),
                "Call_Exit_Price":    round(call_v, 2),
                "Call_PnL_%":         round(call_pnl_pct * 100, 2),
                "Call_Delta_Entry":   position["entry_delta"],
                "Call_Delta_Exit":    round(exit_delta, 3),
                # Put hedge
                "Put_Hedge":          "YES" if position["has_put"] else "NO",
                "Put_Strike":         round(position["K_put"], 2) if position["has_put"] else "N/A",
                "Put_Entry_Price":    round(position["put_price0"], 4) if position["has_put"] else "N/A",
                "Put_Exit_Price":     round(bs_put(S, position["K_put"], T_put, R, iv), 4) if position["has_put"] else "N/A",
                "Put_Units":          round(position["put_units"], 4),
                "Put_Cost_Paid":      round(position["put_extra"] * position["units"], 2),
                "Put_Exit_Value":     round(put_v, 2),
                # QQQ valuation at entry
                "QQQ_PE_Entry":       position["qqq_pe_entry"],
                "QQQ_PE_5yr_Avg":     position["qqq_pe_avg_entry"],
                # Position sizing
                "Units":              round(position["units"], 4),
                "Total_Invested":     round(position["total_invested"], 2),
                "Exit_Value":         round(pos_value, 2),
                "Trade_PnL_$":        round(pnl, 2),
                "Trade_PnL_%":        round(pnl / position["total_invested"] * 100, 2),
                # Capital
                "Capital_Before":     round(position["capital_before"], 2),
                "Capital_After":      round(capital_after, 2),
                # Exit
                "Exit_Reason":        exit_reason,
                "Win":                "WIN" if pnl > 0 else "LOSS",
            })

            position = None
            cooldown = COOLDOWN_DAYS

    return trades

# ─── Run and save ─────────────────────────────────────────────────────────────

for combo in TOP3:
    rank = combo["rank"]
    print(f"\nGenerating transactions for Rank #{rank} "
          f"(crash={combo['crash_set']}, delta={combo['call_delta']}, "
          f"put_cost={combo['put_cost_frac']}, risk={combo['risk_per_trade']}) ...")

    trades = run_and_log(
        crash_set      = combo["crash_set"],
        call_delta     = combo["call_delta"],
        put_cost_frac  = combo["put_cost_frac"],
        risk_per_trade = combo["risk_per_trade"],
    )

    tdf = pd.DataFrame(trades)
    out_path = f"C:/Users/Administrator/Desktop/projects/spx_transactions_rank{rank}.csv"
    tdf.to_csv(out_path, index=False)

    # Summary
    wins   = (tdf["Win"] == "WIN").sum()
    losses = (tdf["Win"] == "LOSS").sum()
    total_pnl = tdf["Trade_PnL_$"].sum()
    hedged    = (tdf["Put_Hedge"] == "YES").sum()

    print(f"  Trades   : {len(tdf)}")
    print(f"  Wins/Losses: {wins}W / {losses}L  ({wins/len(tdf):.1%} win rate)")
    print(f"  Hedged trades: {hedged}/{len(tdf)}")
    print(f"  Total PnL  : ${total_pnl:,.2f}")
    print(f"  Final Capital: ${tdf['Capital_After'].iloc[-1]:,.2f}")
    print(f"  Saved to   : {out_path}")

print("\nAll 3 transaction files saved.")
