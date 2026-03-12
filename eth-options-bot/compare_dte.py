"""
Compare backtest results across all DTE configurations.
Reads trade_history.csv from each DTE folder and prints a side-by-side summary.
Run after all backtests have been executed.
"""
import os
import pandas as pd
import numpy as np

datasets = [
    ("data/0dte_backtest/trade_history.csv",  "0 DTE"),
    ("data/3dte_backtest/trade_history.csv",  "3 DTE"),
    ("data/7dte_backtest/trade_history.csv",  "7 DTE"),
    ("data/10dte_backtest/trade_history.csv", "10 DTE"),
    ("data/14dte_backtest/trade_history.csv", "14 DTE"),
    ("data/30dte_backtest/trade_history.csv", "30 DTE"),
]

INITIAL_CAPITAL_USD = 2200.0

rows = []
for path, label in datasets:
    if not os.path.exists(path):
        print(f"  [MISSING] {path} -- run the backtest first")
        continue
    df = pd.read_csv(path)
    if df.empty:
        continue

    # Parse PnL columns -- strip $ and + signs
    df["pnl_usd_f"] = df["PnL USD"].str.replace("$", "", regex=False).str.replace("+", "", regex=False).astype(float)
    df["fee_usd_f"] = df["Fees USD"].str.replace("$", "", regex=False).astype(float)
    df["credit_eth_f"] = df["Credit ETH"].astype(float)

    n = len(df)
    wins = df[df["pnl_usd_f"] > 0]
    losses = df[df["pnl_usd_f"] <= 0]
    win_rate = len(wins) / n * 100 if n > 0 else 0

    total_pnl = df["pnl_usd_f"].sum()
    total_fees = df["fee_usd_f"].sum()
    gross_pnl = total_pnl + total_fees
    total_return = total_pnl / INITIAL_CAPITAL_USD * 100

    avg_win  = wins["pnl_usd_f"].mean()  if not wins.empty  else 0
    avg_loss = losses["pnl_usd_f"].mean() if not losses.empty else 0
    pf = abs(wins["pnl_usd_f"].sum() / losses["pnl_usd_f"].sum()) if not losses.empty and losses["pnl_usd_f"].sum() != 0 else float("inf")

    tp  = df["Reason"].str.contains("take_profit", na=False).sum()
    sl  = df["Reason"].str.contains("stop_loss",   na=False).sum()
    rev = df["Reason"].str.contains("reversal",    na=False).sum()
    exp = df["Reason"].str.contains("expiry",      na=False).sum()

    # Strategy type breakdown
    by_type = {}
    for stype in ["BullPut", "BearCall", "IronCond"]:
        sub = df[df["Type"].str.strip() == stype.strip()]
        if len(sub) > 0:
            wr = len(sub[sub["pnl_usd_f"] > 0]) / len(sub) * 100
            by_type[stype] = f"{wr:.0f}% ({len(sub)})"
        else:
            by_type[stype] = "-"

    rows.append({
        "DTE": label,
        "Trades": n,
        "Win %": f"{win_rate:.1f}%",
        "Net PnL": f"${total_pnl:+,.0f}",
        "Fees": f"${total_fees:,.0f}",
        "Gross PnL": f"${gross_pnl:+,.0f}",
        "Return": f"{total_return:+.1f}%",
        "AvgWin": f"${avg_win:+.0f}",
        "AvgLoss": f"${avg_loss:+.0f}",
        "PF": f"{pf:.2f}",
        "TP": tp, "SL": sl, "Rev": rev, "Exp": exp,
        "BullPut": by_type.get("BullPut ", by_type.get("BullPut", "-")),
        "BearCall": by_type.get("BearCall", "-"),
        "IronCond": by_type.get("IronCond", "-"),
    })

if not rows:
    print("No results found. Run the backtest scripts first.")
else:
    summary = pd.DataFrame(rows)

    print("\n" + "=" * 110)
    print("  DTE COMPARISON  --  EMA Hybrid Strategy (2 Years, $2,200 Capital)")
    print("=" * 110)
    cols = ["DTE", "Trades", "Win %", "Net PnL", "Fees", "Gross PnL", "Return", "AvgWin", "AvgLoss", "PF"]
    print(summary[cols].to_string(index=False))

    print("\n" + "=" * 110)
    print("  WIN RATE BY STRATEGY TYPE  (win% | trade count)")
    print("=" * 110)
    cols2 = ["DTE", "Trades", "Win %", "BullPut", "BearCall", "IronCond"]
    print(summary[cols2].to_string(index=False))

    print("\n" + "=" * 110)
    print("  EXIT BREAKDOWN")
    print("=" * 110)
    cols3 = ["DTE", "Trades", "TP", "SL", "Rev", "Exp"]
    print(summary[cols3].to_string(index=False))
    print("=" * 110)
    print("  TP=take-profit  SL=stop-loss  Rev=signal-reversal  Exp=closed-at-expiry")
    print()
