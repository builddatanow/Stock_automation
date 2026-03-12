import pandas as pd, numpy as np

CAPITAL = 2200.0
df2 = pd.read_csv("data/0dte_2pm_sydney/trade_history.csv")
df3 = pd.read_csv("data/0dte_3pm_sydney/trade_history.csv")
for df in [df2, df3]:
    df["pnl"] = df["PnL USD"].str.replace("$","",regex=False).str.replace("+","",regex=False).astype(float)

combined = pd.concat([df2[["Date","pnl"]], df3[["Date","pnl"]]])
daily = combined.groupby("Date")["pnl"].sum().reset_index().sort_values("Date")
daily["equity"] = CAPITAL + daily["pnl"].cumsum()
daily["peak"] = daily["equity"].cummax()
daily["dd"] = (daily["equity"] - daily["peak"]) / daily["peak"]

net_pnl = daily["pnl"].sum()
net_ret = net_pnl / CAPITAL
cagr = ((1 + net_ret) ** 0.5 - 1) * 100
max_dd = daily["dd"].min() * 100
all_pnl = list(df2["pnl"]) + list(df3["pnl"])
wr = sum(p > 0 for p in all_pnl) / len(all_pnl) * 100

print("=" * 52)
print("  Combined 2PM + 3PM Sydney | No IC | $2,200 ea")
print("=" * 52)
print(f"  Total trades : {len(df2)+len(df3)}")
print(f"  Win rate     : {wr:.1f}%")
print(f"  Net PnL      : ${net_pnl:+,.2f}")
print(f"  Net return   : {net_ret*100:+.1f}%")
print(f"  CAGR         : {cagr:+.1f}%")
print(f"  Max drawdown : {max_dd:.1f}%")
print()
print("  --- Individual ---")
for label, df in [("2 PM", df2), ("3 PM", df3)]:
    n = df["pnl"].sum()
    w = (df["pnl"] > 0).mean() * 100
    c = ((1 + n / CAPITAL) ** 0.5 - 1) * 100
    print(f"    {label}: {len(df)}t  WR={w:.1f}%  PnL=${n:+,.0f}  CAGR={c:+.1f}%")
print()
print("  --- Worst drawdown days ---")
worst = daily.nsmallest(5, "dd")[["Date","equity","dd"]]
worst["dd_pct"] = worst["dd"].map(lambda x: f"{x*100:.1f}%")
print(worst[["Date","equity","dd_pct"]].to_string(index=False))
