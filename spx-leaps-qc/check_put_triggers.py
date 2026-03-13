"""
Shows when SPX drawdown from 52-week high exceeds 10%.
Prints entry date, price, peak price, and drawdown %.
No LEAN required — uses yfinance SPY as SPX proxy.
"""
import yfinance as yf
import pandas as pd

# Download SPY (proxy for SPX) from 1999 to present
df = yf.download("^GSPC", start="1999-01-01", end="2026-03-01", auto_adjust=True, progress=False)
df = df["Close"].squeeze().dropna()

# Rolling 252-day high (52-week)
rolling_high = df.rolling(window=252, min_periods=30).max()
drawdown = (df - rolling_high) / rolling_high  # negative values = drawdown

THRESHOLD = -0.10  # 10% drawdown triggers put buy

# Find dates where drawdown crosses below threshold (new trigger, not already triggered)
in_trigger = False
triggers = []

for date, dd in drawdown.items():
    price = float(df[date])
    peak  = float(rolling_high[date])
    dd_val = float(dd)

    if pd.isna(dd_val):
        continue

    if not in_trigger and dd_val <= THRESHOLD:
        in_trigger = True
        triggers.append({
            "date":     date.strftime("%Y-%m-%d"),
            "spx":      round(price, 1),
            "peak":     round(peak, 1),
            "drawdown": f"{dd_val*100:.1f}%",
        })
    elif in_trigger and dd_val > -0.05:  # reset when recovered to -5%
        in_trigger = False

print(f"\n{'Date':<14} {'SPX Price':>10} {'52wk Peak':>10} {'Drawdown':>10}")
print("-" * 48)
for t in triggers:
    print(f"{t['date']:<14} {t['spx']:>10,.1f} {t['peak']:>10,.1f} {t['drawdown']:>10}")

print(f"\nTotal triggers: {len(triggers)}")
