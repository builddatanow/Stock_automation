"""
EMA Spread Strategy -- 14 DTE Backtest Using REAL Deribit Data
==============================================================
Same EMA(9/21) hybrid strategy targeting ~14 days to expiration.
Entry every weekday, close at DTE=2.
Delta: slightly lower wings (0.06-0.10) for wider spread protection.
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

setup_logging("WARNING", "logs/14dte_bt.log")

BASE_URL = "https://www.deribit.com"

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
    expiry = timestamp + timedelta(days=expiry_days)
    T = max(expiry_days, 0.5) / 365.0
    raw = [anchor_spot * (1 + i * 0.025) for i in range(-18, 19)]
    strikes = sorted(set(round(k / 50) * 50 for k in raw if k > 0))

    def smile_iv(K):
        m = (K - spot) / spot
        return base_iv * (1 + 3.0 * m**2 - 0.5 * m) if m < 0 else base_iv * (1 + 1.5 * m**2)

    quotes = []
    for K in strikes:
        for is_call in [True, False]:
            iv  = max(smile_iv(K), 0.20)
            p   = bs_price(spot, K, T, iv, is_call) / spot
            d   = bs_delta(spot, K, T, iv, is_call)
            if p < 0.00005:
                continue
            bid = max(p * 0.93, 0.00005)
            ask = p * 1.07
            opt = OptionType.CALL if is_call else OptionType.PUT
            name = f"ETH-{expiry.strftime('%d%b%y').upper()}-{int(K)}-{'C' if is_call else 'P'}"
            quotes.append(OptionQuote(
                timestamp=timestamp, instrument_name=name,
                strike=K, expiry=expiry, option_type=opt,
                bid=round(bid, 6), ask=round(ask, 6), mark_price=round(p, 6),
                implied_volatility=iv, delta=round(d, 4),
                gamma=0.001, theta=-p / (T * 365) if T > 0 else 0,
                vega=T**0.5 * 0.01, underlying_price=spot,
            ))
    return quotes


print("=" * 62)
print("  EMA Spread Strategy  --  14 DTE Backtest (Real Deribit Data)")
print("  Period : Mar 2024 - Mar 2026  |  Capital: $2,200")
print("  DTE    : 12-16 days  |  Entry: every weekday  |  Close: DTE=2")
print("=" * 62)

end_dt   = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
start_dt = end_dt - timedelta(days=730)

print(f"\n[1/4] Fetching ETH price history...")
ohlcv = fetch_eth_ohlcv(start_dt, end_dt)
if ohlcv.empty:
    print("ERROR"); sys.exit(1)
print(f"      {len(ohlcv)} candles | ${ohlcv['close'].iloc[0]:.0f} -> ${ohlcv['close'].iloc[-1]:.0f}")

print(f"\n[2/4] Fetching Deribit IV history...")
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

print(f"\n[3/4] Building 14-DTE option chain snapshots...")
parquet_dir  = "data/14dte_backtest/parquet"
expiry_dates = [start_dt + timedelta(days=d) for d in range(1, 731)]

expiry_anchor = {}
for exp in expiry_dates:
    anchor_date = (exp - timedelta(days=14)).date()
    row = merged[merged["date"] <= anchor_date]
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
        if dte < 1 or dte > 25:
            continue
        chains.extend(build_chain(spot, dt, iv, dte, expiry_anchor[exp]))
    if chains:
        storage.save_quotes(chains)
        built += 1

print(f"      Done. {built} daily snapshots stored.")

print(f"\n[4/4] Running 14-DTE EMA spread backtest...")
cfg = EMASpreadConfig(
    fast_ema=9, slow_ema=21,
    target_dte_min=12,
    target_dte_max=16,
    short_delta_min=0.20, short_delta_max=0.30,
    wing_delta_min=0.06,  wing_delta_max=0.10,   # slightly lower wing delta = wider spread
    take_profit_pct=0.50, stop_loss_multiplier=1.5,
    close_dte=2,
    iv_percentile_min=10.0, min_trend_strength=0.003,
    condor_on_low_iv=True,
    ic_short_delta_min=0.15, ic_short_delta_max=0.25,
    ic_wing_delta_min=0.04,  ic_wing_delta_max=0.08,
    entry_every_day=True,
    account_size=2200.0, max_risk_per_trade_pct=0.20,
)

engine = EMASpreadBacktest(
    config=cfg, parquet_storage=storage,
    start_date=str(start_dt.date()), end_date=str(end_dt.date()),
    initial_capital=2200.0, fee_per_contract=0.0003, slippage_pct=0.001,
)
results = engine.run()
engine.print_summary(results)

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

    fee_per_contract = 0.0003
    total_fees_eth = sum(
        fee_per_contract * (4 if (t.short_call_strike > 0 and t.short_put_strike > 0) else 2) * 2
        for t in trades
    )
    total_pnl_usd = m.get("total_pnl", 0) * avg_spot
    print(f"\n  Total PnL (USD est)  : ${total_pnl_usd:+.2f}")
    print(f"  Total fees paid      : {total_fees_eth:.5f} ETH  (~${total_fees_eth * avg_spot:.2f})")
    print(f"  Gross PnL (pre-fee)  : ${total_pnl_usd + total_fees_eth * avg_spot:+.2f}")
    print(f"\n  Exit breakdown:")
    print(f"    Take-profit        : {tp}")
    print(f"    Stop-loss (1.5x)   : {sl}")
    print(f"    Signal reversal    : {rev}")
    print(f"    Closed at expiry   : {exp}")
    if wins:
        print(f"    Avg win            : +{np.mean(wins):.5f} ETH  (~${np.mean(wins)*avg_spot:+.2f})")
    if losses:
        print(f"    Avg loss           : {np.mean(losses):.5f} ETH  (~${np.mean(losses)*avg_spot:+.2f})")
    if wins and losses and sum(losses) != 0:
        print(f"    Profit factor      : {abs(sum(wins)/sum(losses)):.2f}")

    rows = []
    for t in trades:
        pnl_usd = (t.realized_pnl or 0) * t.underlying_at_entry
        if t.short_call_strike > 0 and t.short_put_strike > 0:
            stype = "IronCond"; stk = f"SC={int(t.short_call_strike)} SP={int(t.short_put_strike)}"
        elif t.short_call_strike > 0:
            stype = "BearCall"; stk = f"SC={int(t.short_call_strike)} LC={int(t.long_call_strike)}"
        else:
            stype = "BullPut "; stk = f"SP={int(t.short_put_strike)}  LP={int(t.long_put_strike)}"
        n_legs  = 4 if (t.short_call_strike > 0 and t.short_put_strike > 0) else 2
        fee_eth = fee_per_contract * n_legs * 2
        rows.append({
            "Entry": str(t.entry_time.date()), "Exit": str(t.exit_time.date()) if t.exit_time else "-",
            "Type": stype, "ETH": f"${t.underlying_at_entry:.0f}", "Strikes": stk,
            "Credit ETH": f"{t.credit_received:.5f}", "Fees USD": f"${fee_eth * t.underlying_at_entry:.2f}",
            "PnL ETH": f"{(t.realized_pnl or 0):+.5f}", "PnL USD": f"${pnl_usd:+.0f}",
            "Reason": (t.exit_reason or "")[:26],
        })
    df_out = pd.DataFrame(rows)
    os.makedirs("data/14dte_backtest", exist_ok=True)
    df_out.to_csv("data/14dte_backtest/trade_history.csv", index=False)
    print(f"\n  Trade history -> data/14dte_backtest/trade_history.csv")
    print(df_out.to_string(index=False))
