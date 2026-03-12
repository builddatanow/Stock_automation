"""0 DTE Backtest -- 3 PM Sydney AEDT (04:00 UTC, T=4h before expiry, No Iron Condor)"""
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

setup_logging("WARNING", "logs/0dte_3pm_bt.log")
BASE_URL = "https://www.deribit.com"

def fetch_eth_ohlcv(start_dt, end_dt):
    resp = requests.get(f"{BASE_URL}/api/v2/public/get_tradingview_chart_data",
        params={"instrument_name":"ETH-PERPETUAL",
                "start_timestamp":int(start_dt.timestamp()*1000),
                "end_timestamp":int(end_dt.timestamp()*1000),
                "resolution":"1D"}, timeout=20)
    resp.raise_for_status()
    data = resp.json().get("result", {})
    ticks = data.get("ticks", [])
    if not ticks: return pd.DataFrame()
    df = pd.DataFrame({"timestamp": pd.to_datetime(ticks, unit="ms", utc=True),
        "open":data["open"],"high":data["high"],"low":data["low"],
        "close":data["close"],"volume":data["volume"]})
    return df.sort_values("timestamp").reset_index(drop=True)

def fetch_historical_iv():
    resp = requests.get(f"{BASE_URL}/api/v2/public/get_historical_volatility",
        params={"currency":"ETH"}, timeout=20)
    resp.raise_for_status()
    raw = resp.json().get("result", [])
    if not raw: return pd.DataFrame()
    df = pd.DataFrame(raw, columns=["timestamp_ms","iv"])
    df["timestamp"] = pd.to_datetime(df["timestamp_ms"], unit="ms", utc=True)
    df["date"] = df["timestamp"].dt.date
    df["iv_decimal"] = df["iv"] / 100.0
    return df.sort_values("timestamp").drop_duplicates("date").reset_index(drop=True)[["date","iv_decimal","iv"]]

def bs_delta(S, K, T, sigma, is_call):
    if T <= 0: return (1.0 if S>K else 0.0) if is_call else (-1.0 if S<K else 0.0)
    d1 = (math.log(S/K) + 0.5*sigma**2*T) / (sigma*math.sqrt(T))
    return float(norm.cdf(d1)) if is_call else float(norm.cdf(d1)-1)

def bs_price(S, K, T, sigma, is_call):
    if T <= 0: return max(S-K,0) if is_call else max(K-S,0)
    d1 = (math.log(S/K) + 0.5*sigma**2*T) / (sigma*math.sqrt(T))
    d2 = d1 - sigma*math.sqrt(T)
    if is_call: return S*norm.cdf(d1) - K*norm.cdf(d2)
    return K*norm.cdf(-d2) - S*norm.cdf(-d1)

def build_chain(spot, timestamp, base_iv, expiry_days, anchor_spot, T_days):
    expiry = timestamp + timedelta(days=expiry_days)
    T = T_days / 365.0
    raw = [anchor_spot*(1+i*0.015) for i in range(-12,13)]
    strikes = sorted(set(round(k/25)*25 for k in raw if k>0))
    def smile_iv(K):
        m = (K-spot)/spot
        return base_iv*(1+3.0*m**2-0.5*m) if m<0 else base_iv*(1+1.5*m**2)
    quotes = []
    for K in strikes:
        for is_call in [True, False]:
            iv = max(smile_iv(K), 0.20)
            p  = bs_price(spot, K, T, iv, is_call) / spot
            d  = bs_delta(spot, K, T, iv, is_call)
            if p < 0.00003: continue
            bid = max(p*0.92, 0.00003); ask = p*1.08
            opt = OptionType.CALL if is_call else OptionType.PUT
            sfx = "C" if is_call else "P"
            name = f"ETH-{expiry.strftime('%d%b%y').upper()}-{int(K)}-{sfx}"
            quotes.append(OptionQuote(timestamp=timestamp, instrument_name=name,
                strike=K, expiry=expiry, option_type=opt,
                bid=round(bid,6), ask=round(ask,6), mark_price=round(p,6),
                implied_volatility=iv, delta=round(d,4), gamma=0.001,
                theta=-p/(T*365) if T>0 else 0, vega=T**0.5*0.01, underlying_price=spot))
    return quotes


print("=" * 58)
print("  0 DTE 3 PM Sydney (04:00 UTC) | T=4h | No Iron Condor")
print("  Period: Mar 2024 - Mar 2026  |  Capital: $2,200")
print("=" * 58)

end_dt   = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
start_dt = end_dt - timedelta(days=730)

print("\n[1/3] Fetching market data...")
ohlcv = fetch_eth_ohlcv(start_dt, end_dt)
try:
    iv_df = fetch_historical_iv()
    iv_df = iv_df[iv_df["date"] >= start_dt.date()].reset_index(drop=True)
    print(f"      {len(ohlcv)} candles | {len(iv_df)} IV points | avg IV {iv_df['iv'].mean():.1f}%")
except Exception as e:
    print(f"      {len(ohlcv)} candles | IV fetch failed: {e}")
    iv_df = pd.DataFrame()

ohlcv["date"] = ohlcv["timestamp"].dt.date
if not iv_df.empty:
    merged = ohlcv.merge(iv_df[["date","iv_decimal"]], on="date", how="left")
    merged["iv_decimal"] = merged["iv_decimal"].ffill().fillna(0.80)
else:
    merged = ohlcv.copy(); merged["iv_decimal"] = 0.80
merged = merged.sort_values("date").reset_index(drop=True)
avg_spot = float(merged["close"].mean())

# 3 PM Sydney AEDT = 04:00 UTC = 4 hours before 8 AM UTC expiry
T_DAYS = 4.0 / 24.0

print("\n[2/3] Building 0-DTE chains (T=4h)...")
expiry_dates = [start_dt + timedelta(days=d) for d in range(0, 731)]
expiry_anchor = {}
for exp in expiry_dates:
    row = merged[merged["date"] <= exp.date()]
    expiry_anchor[exp] = float(row["close"].iloc[-1]) if not row.empty else float(merged["close"].iloc[0])

parquet_dir = "data/0dte_3pm_sydney/parquet"
storage = ParquetStorage(parquet_dir)
built = 0
for _, row in merged.iterrows():
    date = row["date"]; spot = float(row["close"]); iv = float(row["iv_decimal"])
    dt = datetime.combine(date, datetime.min.time()).replace(tzinfo=timezone.utc)
    chains = []
    for exp in expiry_dates:
        dte = (exp - dt).days
        if dte < 0 or dte > 2: continue
        T_use = T_DAYS if dte == 0 else max(dte, 0.25)
        chains.extend(build_chain(spot, dt, iv, dte, expiry_anchor[exp], T_use))
    if chains: storage.save_quotes(chains); built += 1
print(f"      {built} daily snapshots stored.")

print("\n[3/3] Running backtest...")
cfg = EMASpreadConfig(
    fast_ema=9, slow_ema=21,
    target_dte_min=0, target_dte_max=1,
    short_delta_min=0.20, short_delta_max=0.35,
    wing_delta_min=0.08,  wing_delta_max=0.15,
    take_profit_pct=0.50, stop_loss_multiplier=1.5,
    close_dte=0, iv_percentile_min=10.0, min_trend_strength=0.003,
    condor_on_low_iv=False, entry_every_day=True,
    account_size=2200.0, max_risk_per_trade_pct=0.20,
)
engine = EMASpreadBacktest(config=cfg, parquet_storage=storage,
    start_date=str(start_dt.date()), end_date=str(end_dt.date()),
    initial_capital=2200.0, fee_per_contract=0.0003, slippage_pct=0.001)
results = engine.run()

trades = results.get("trades", [])
m      = results.get("metrics", {})
wins   = [t.realized_pnl for t in trades if (t.realized_pnl or 0) > 0]
losses = [t.realized_pnl for t in trades if (t.realized_pnl or 0) <= 0]
fee_eth = sum(0.0003*2*2 for t in trades)
net_pnl_usd = m.get("total_pnl", 0) * avg_spot
cagr = ((1 + net_pnl_usd/2200)**0.5 - 1)*100
pf = abs(sum(wins)/sum(losses)) if losses and sum(losses) != 0 else 0
bp = [t for t in trades if t.short_put_strike>0 and t.short_call_strike==0]
bc = [t for t in trades if t.short_call_strike>0 and t.short_put_strike==0]
bp_wr = f"{len([t for t in bp if (t.realized_pnl or 0)>0])/len(bp)*100:.0f}% ({len(bp)}t)" if bp else "-"
bc_wr = f"{len([t for t in bc if (t.realized_pnl or 0)>0])/len(bc)*100:.0f}% ({len(bc)}t)" if bc else "-"

print()
print("=" * 58)
print("  RESULTS")
print("=" * 58)
print(f"  Trades        : {len(trades)}")
print(f"  Win rate      : {len(wins)/len(trades)*100:.1f}%")
print(f"  Net PnL       : ${net_pnl_usd:+,.2f}")
print(f"  Gross PnL     : ${net_pnl_usd + fee_eth*avg_spot:+,.2f}")
print(f"  Fees          : -${fee_eth*avg_spot:,.2f}")
print(f"  Net return    : {net_pnl_usd/2200*100:+.1f}%")
print(f"  CAGR          : {cagr:+.1f}%")
print(f"  Profit factor : {pf:.2f}")
print(f"  BullPut       : {bp_wr}")
print(f"  BearCall      : {bc_wr}")
if wins:   print(f"  Avg win       : +{np.mean(wins):.5f} ETH  (~${np.mean(wins)*avg_spot:+.2f})")
if losses: print(f"  Avg loss      : {np.mean(losses):.5f} ETH  (~${np.mean(losses)*avg_spot:+.2f})")

# Compare all windows
print()
print("=" * 58)
print("  ALL SYDNEY WINDOWS COMPARISON (No IC, $2,200)")
print("=" * 58)
compare = [
    ("1 PM (T=6h)", "data/0dte_1pm_sydney/trade_history.csv"),
    ("2 PM (T=5h)", "data/0dte_2pm_sydney/trade_history.csv"),
    ("3 PM (T=4h)", "data/0dte_3pm_sydney/trade_history.csv"),
]
rows_cmp = []
for label, path in compare:
    if not os.path.exists(path): continue
    df = pd.read_csv(path)
    df["pnl"] = df["PnL USD"].str.replace("$","",regex=False).str.replace("+","",regex=False).astype(float)
    n = len(df)
    wr = len(df[df["pnl"]>0])/n*100
    net = df["pnl"].sum()
    cagr_w = ((1+net/2200)**0.5-1)*100
    rows_cmp.append({"Window":label, "Trades":n, "Win%":f"{wr:.1f}%",
        "Net PnL":f"${net:+,.0f}", "Net Ret":f"{net/2200*100:+.1f}%", "CAGR":f"{cagr_w:+.1f}%"})
# add 3pm from current run
df3 = pd.DataFrame([{"Window":"3 PM (T=4h)","Trades":len(trades),
    "Win%":f"{len(wins)/len(trades)*100:.1f}%",
    "Net PnL":f"${net_pnl_usd:+,.0f}","Net Ret":f"{net_pnl_usd/2200*100:+.1f}%",
    "CAGR":f"{cagr:+.1f}%"}])
rows_cmp = [r for r in rows_cmp if r["Window"] != "3 PM (T=4h)"]
print(pd.DataFrame(rows_cmp + [df3.iloc[0].to_dict()]).to_string(index=False))

# Save CSV
rows = []
for t in trades:
    pnl_usd = (t.realized_pnl or 0)*t.underlying_at_entry
    if t.short_call_strike>0 and t.short_put_strike>0:
        stype="IronCond"; stk=f"SC={int(t.short_call_strike)} SP={int(t.short_put_strike)}"
    elif t.short_call_strike>0:
        stype="BearCall"; stk=f"SC={int(t.short_call_strike)} LC={int(t.long_call_strike)}"
    else:
        stype="BullPut "; stk=f"SP={int(t.short_put_strike)}  LP={int(t.long_put_strike)}"
    rows.append({"Date":str(t.entry_time.date()),"Type":stype,
        "ETH":f"${t.underlying_at_entry:.0f}","Strikes":stk,
        "Credit ETH":f"{t.credit_received:.5f}",
        "Fees USD":f"${0.0003*2*2*t.underlying_at_entry:.2f}",
        "PnL ETH":f"{(t.realized_pnl or 0):+.5f}","PnL USD":f"${pnl_usd:+.0f}",
        "Reason":(t.exit_reason or "")[:28]})
os.makedirs("data/0dte_3pm_sydney", exist_ok=True)
pd.DataFrame(rows).to_csv("data/0dte_3pm_sydney/trade_history.csv", index=False)
print(f"\n  Saved -> data/0dte_3pm_sydney/trade_history.csv")
