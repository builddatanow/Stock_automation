"""
EMA Spread Strategy -- 0 DTE Backtest Using REAL Deribit Data
=============================================================
Same EMA(9/21) hybrid strategy targeting options that expire
on the same day (0 DTE).

How 0 DTE works in daily-bar simulation:
  - Entry at today's close with options expiring TODAY (dte=0)
  - Options priced with T = 0.25/365 (~6 hours) to simulate
    a morning entry, giving realistic time-value credits
  - Same-day close: position is evaluated and closed on the
    same bar it was entered (engine same-day close logic)
  - P&L = credit - intrinsic value at close
    => if ETH closes OTM of both strikes: keep full credit
    => if ETH closes ITM: lose up to (spread_width - credit)

Key differences vs 3 DTE:
  - Almost pure theta/gamma play -- no overnight risk
  - Much smaller credits (near-zero time value)
  - Highest trade frequency (one per day max)
  - No TP/SL during the day (daily bars only)
  - Pure binary: OTM = win, ITM = lose

Public API only -- no credentials required.
"""
import sys, os, math, time, warnings
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
from src.strategy.ema_spread import EMASpreadConfig, compute_ema
from src.monitoring.logger import setup_logging

setup_logging("WARNING", "logs/0dte_bt.log")

BASE_URL = "https://www.deribit.com"

# ---------------------------------------------------------------------------
# Deribit data fetchers
# ---------------------------------------------------------------------------

def fetch_eth_ohlcv(start_dt: datetime, end_dt: datetime) -> pd.DataFrame:
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms   = int(end_dt.timestamp()   * 1000)
    resp = requests.get(
        f"{BASE_URL}/api/v2/public/get_tradingview_chart_data",
        params={"instrument_name": "ETH-PERPETUAL", "start_timestamp": start_ms,
                "end_timestamp": end_ms, "resolution": "1D"},
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


def fetch_historical_iv(currency: str = "ETH") -> pd.DataFrame:
    resp = requests.get(
        f"{BASE_URL}/api/v2/public/get_historical_volatility",
        params={"currency": currency}, timeout=20,
    )
    resp.raise_for_status()
    raw = resp.json().get("result", [])
    if not raw:
        return pd.DataFrame()
    df = pd.DataFrame(raw, columns=["timestamp_ms", "iv"])
    df["timestamp"] = pd.to_datetime(df["timestamp_ms"], unit="ms", utc=True)
    df["date"] = df["timestamp"].dt.date
    df["iv_decimal"] = df["iv"] / 100.0
    df = df.sort_values("timestamp").drop_duplicates("date").reset_index(drop=True)
    return df[["date", "iv_decimal", "iv"]]


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
    """
    Build an option chain for one expiry.
    For 0 DTE, uses T = 0.25/365 (~6 hours) to simulate morning-entry pricing.
    """
    expiry = timestamp + timedelta(days=expiry_days)
    # 0 DTE: simulate ~6 hours of time value (morning entry, EOD close)
    T = max(expiry_days, 0.25) / 365.0

    # Tight strike grid (2% steps, anchored to spot)
    raw = [anchor_spot * (1 + i * 0.015) for i in range(-12, 13)]
    strikes = sorted(set(round(k / 25) * 25 for k in raw if k > 0))

    def smile_iv(K):
        moneyness = (K - spot) / spot
        if moneyness < 0:
            return base_iv * (1 + 3.0 * moneyness**2 - 0.5 * moneyness)
        else:
            return base_iv * (1 + 1.5 * moneyness**2)

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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

print("=" * 62)
print("  EMA Spread Strategy  --  0 DTE Backtest (Real Deribit Data)")
print("  Period : Mar 2024 - Mar 2026  |  Capital: $2,200")
print("  DTE    : 0 (same-day expiry)  |  Entry: every weekday")
print("  Note   : daily bars -- intraday TP/SL not simulated")
print("=" * 62)

end_dt   = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
start_dt = end_dt - timedelta(days=730)

# ---------------------------------------------------------------------------
# Step 1: Fetch ETH price history
# ---------------------------------------------------------------------------
print(f"\n[1/4] Fetching real ETH price history from Deribit...")
try:
    ohlcv = fetch_eth_ohlcv(start_dt, end_dt)
    if ohlcv.empty:
        raise ValueError("Empty OHLCV")
    print(f"      Got {len(ohlcv)} daily candles")
    print(f"      ETH range: ${ohlcv['low'].min():.0f} - ${ohlcv['high'].max():.0f}")
    print(f"      Start: ${ohlcv['close'].iloc[0]:.0f}  |  End: ${ohlcv['close'].iloc[-1]:.0f}")
except Exception as e:
    print(f"      ERROR: {e}"); sys.exit(1)

# ---------------------------------------------------------------------------
# Step 2: Fetch IV history
# ---------------------------------------------------------------------------
print(f"\n[2/4] Fetching real Deribit historical volatility...")
try:
    iv_df = fetch_historical_iv("ETH")
    iv_df = iv_df[iv_df["date"] >= start_dt.date()].reset_index(drop=True)
    print(f"      Got {len(iv_df)} IV data points | avg: {iv_df['iv'].mean():.1f}%")
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

# ---------------------------------------------------------------------------
# Step 3: Build 0 DTE option chains (same-day + next-day expiries)
# ---------------------------------------------------------------------------
print(f"\n[3/4] Building 0-DTE option chain snapshots...")
parquet_dir = "data/0dte_backtest/parquet"

# Each date is its own expiry (0 DTE) plus next day (1 DTE)
expiry_dates = [start_dt + timedelta(days=d) for d in range(0, 731)]

# Anchor = same day's close
expiry_anchor: dict = {}
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
        if dte < 0 or dte > 2:    # 0, 1, 2 DTE chains only
            continue
        chains.extend(build_chain(spot, dt, iv, dte, expiry_anchor[exp]))

    if chains:
        storage.save_quotes(chains)
        built += 1

print(f"      Done. Stored {built} daily snapshots with 0-DTE chains.")

# ---------------------------------------------------------------------------
# Step 4: Run 0 DTE EMA spread backtest
# ---------------------------------------------------------------------------
print(f"\n[4/4] Running 0-DTE EMA spread backtest...")

cfg = EMASpreadConfig(
    fast_ema=9,
    slow_ema=21,
    target_dte_min=0,             # same-day expiry
    target_dte_max=1,
    short_delta_min=0.20,         # slightly wider for short-dated options
    short_delta_max=0.35,
    wing_delta_min=0.08,
    wing_delta_max=0.15,
    take_profit_pct=0.50,
    stop_loss_multiplier=1.5,
    close_dte=0,                  # close on expiry day (same day for 0 DTE)
    iv_percentile_min=10.0,
    min_trend_strength=0.003,
    condor_on_low_iv=True,
    ic_short_delta_min=0.15,
    ic_short_delta_max=0.30,
    ic_wing_delta_min=0.05,
    ic_wing_delta_max=0.12,
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
engine.print_summary(results)

# ---------------------------------------------------------------------------
# Detailed output
# ---------------------------------------------------------------------------
m      = results.get("metrics", {})
trades = results.get("trades", [])
avg_spot = float(merged["close"].mean())

if trades:
    wins   = [t.realized_pnl for t in trades if (t.realized_pnl or 0) > 0]
    losses = [t.realized_pnl for t in trades if (t.realized_pnl or 0) <= 0]
    tp  = sum(1 for t in trades if "take_profit"         in (t.exit_reason or ""))
    sl  = sum(1 for t in trades if "stop_loss"           in (t.exit_reason or ""))
    rev = sum(1 for t in trades if "signal_reversal"     in (t.exit_reason or ""))
    exp = sum(1 for t in trades if "close_before_expiry" in (t.exit_reason or ""))
    be  = sum(1 for t in trades if "backtest_end"        in (t.exit_reason or ""))

    fee_per_contract = 0.0003
    total_fees_eth = sum(
        fee_per_contract * (4 if (t.short_call_strike > 0 and t.short_put_strike > 0) else 2) * 2
        for t in trades
    )
    total_fees_usd = total_fees_eth * avg_spot
    total_pnl_usd  = m.get("total_pnl", 0) * avg_spot

    print(f"\n  Total PnL (USD est)  : ${total_pnl_usd:+.2f}  (at avg ETH ${avg_spot:.0f})")
    print(f"  Total fees paid      : {total_fees_eth:.5f} ETH  (~${total_fees_usd:.2f})")
    print(f"  Gross PnL (pre-fee)  : ${total_pnl_usd + total_fees_usd:+.2f}")
    print(f"\n  Exit breakdown:")
    print(f"    Expired OTM (keep credit): {exp}")
    print(f"    Take-profit              : {tp}")
    print(f"    Stop-loss (1.5x)         : {sl}")
    print(f"    Signal reversal          : {rev}")
    print(f"    Closed at BT end         : {be}")
    if wins:
        avg_win_usd  = np.mean(wins) * avg_spot
        print(f"    Avg win  : +{np.mean(wins):.5f} ETH  (~${avg_win_usd:+.2f})")
    if losses:
        avg_loss_usd = np.mean(losses) * avg_spot
        print(f"    Avg loss : {np.mean(losses):.5f} ETH  (~${avg_loss_usd:+.2f})")
    if wins and losses and sum(losses) != 0:
        print(f"    Profit factor: {abs(sum(wins)/sum(losses)):.2f}")

    print("\n" + "=" * 80)
    print("  Per-Trade Log  (0 DTE | Real ETH Prices + Real Deribit IV)")
    print("=" * 80)
    rows = []
    for t in trades:
        pnl_usd = (t.realized_pnl or 0) * t.underlying_at_entry
        if t.short_call_strike > 0 and t.short_put_strike > 0:
            stype = "IronCond"
            stk   = f"SC={int(t.short_call_strike)} SP={int(t.short_put_strike)}"
        elif t.short_call_strike > 0:
            stype = "BearCall"
            stk   = f"SC={int(t.short_call_strike)} LC={int(t.long_call_strike)}"
        else:
            stype = "BullPut "
            stk   = f"SP={int(t.short_put_strike)}  LP={int(t.long_put_strike)}"
        n_legs  = 4 if (t.short_call_strike > 0 and t.short_put_strike > 0) else 2
        fee_eth = fee_per_contract * n_legs * 2
        fee_usd = fee_eth * t.underlying_at_entry
        rows.append({
            "Date":       str(t.entry_time.date()),
            "Type":       stype,
            "ETH":        f"${t.underlying_at_entry:.0f}",
            "Strikes":    stk,
            "Credit ETH": f"{t.credit_received:.5f}",
            "Fees USD":   f"${fee_usd:.2f}",
            "PnL ETH":    f"{(t.realized_pnl or 0):+.5f}",
            "PnL USD":    f"${pnl_usd:+.0f}",
            "Reason":     (t.exit_reason or "")[:28],
        })
    df = pd.DataFrame(rows)
    print(df.to_string(index=False))

    os.makedirs("data/0dte_backtest", exist_ok=True)
    df.to_csv("data/0dte_backtest/trade_history.csv", index=False)
    print(f"\n  Trade history -> data/0dte_backtest/trade_history.csv")
