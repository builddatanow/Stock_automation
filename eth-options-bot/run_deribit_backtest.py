"""
EMA Spread Strategy -- 1-Year Backtest Using REAL Deribit Data
==============================================================
Step 1: Fetch real ETH daily price history from Deribit (ETH-PERPETUAL OHLCV)
Step 2: Fetch real Deribit historical implied volatility
Step 3: Build option chains with real prices + real IV (BS pricing)
Step 4: Run EMA spread backtest on this real-data-grounded dataset

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

setup_logging("WARNING", "logs/deribit_bt.log")

BASE_URL = "https://www.deribit.com"

# ---------------------------------------------------------------------------
# Deribit data fetchers
# ---------------------------------------------------------------------------

def fetch_eth_ohlcv(start_dt: datetime, end_dt: datetime, resolution: str = "1D") -> pd.DataFrame:
    """Fetch ETH-PERPETUAL daily OHLCV from Deribit TradingView endpoint."""
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms   = int(end_dt.timestamp()   * 1000)
    url = f"{BASE_URL}/api/v2/public/get_tradingview_chart_data"
    params = {
        "instrument_name": "ETH-PERPETUAL",
        "start_timestamp": start_ms,
        "end_timestamp":   end_ms,
        "resolution":      resolution,
    }
    resp = requests.get(url, params=params, timeout=20)
    resp.raise_for_status()
    data = resp.json().get("result", {})

    ticks  = data.get("ticks",  [])
    opens  = data.get("open",   [])
    highs  = data.get("high",   [])
    lows   = data.get("low",    [])
    closes = data.get("close",  [])
    vols   = data.get("volume", [])

    if not ticks:
        return pd.DataFrame()

    df = pd.DataFrame({
        "timestamp": pd.to_datetime(ticks, unit="ms", utc=True),
        "open":   opens,
        "high":   highs,
        "low":    lows,
        "close":  closes,
        "volume": vols,
    })
    df = df.sort_values("timestamp").reset_index(drop=True)
    return df


def fetch_historical_iv(currency: str = "ETH") -> pd.DataFrame:
    """Fetch Deribit historical daily IV (full history available)."""
    url = f"{BASE_URL}/api/v2/public/get_historical_volatility"
    resp = requests.get(url, params={"currency": currency}, timeout=20)
    resp.raise_for_status()
    raw = resp.json().get("result", [])
    if not raw:
        return pd.DataFrame()

    df = pd.DataFrame(raw, columns=["timestamp_ms", "iv"])
    df["timestamp"] = pd.to_datetime(df["timestamp_ms"], unit="ms", utc=True)
    df["date"] = df["timestamp"].dt.date
    df["iv_decimal"] = df["iv"] / 100.0   # Deribit returns IV as percentage
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
    """Build an option chain for one expiry using real spot + real IV."""
    expiry = timestamp + timedelta(days=expiry_days)
    T = max(expiry_days, 0.5) / 365.0

    # Strike grid anchored to Monday spot (stable instrument names all week)
    raw = [anchor_spot * (1 + i * 0.025) for i in range(-14, 15)]
    strikes = sorted(set(round(k / 50) * 50 for k in raw if k > 0))

    def smile_iv(K):
        # Vol smile: skew for puts, slight smile for calls
        moneyness = (K - spot) / spot
        if moneyness < 0:   # OTM puts: steeper skew
            return base_iv * (1 + 3.0 * moneyness**2 - 0.5 * moneyness)
        else:               # OTM calls: moderate smile
            return base_iv * (1 + 1.5 * moneyness**2)

    quotes = []
    for K in strikes:
        for is_call in [True, False]:
            iv  = max(smile_iv(K), 0.20)
            p   = bs_price(spot, K, T, iv, is_call) / spot   # ETH-denominated
            d   = bs_delta(spot, K, T, iv, is_call)
            if p < 0.00005:
                continue
            bid = max(p * 0.93, 0.00005)
            ask = p * 1.07
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
print("  EMA Spread Strategy  --  Deribit Real-Data Backtest")
print("  Period : Mar 2024 - Mar 2026  |  Capital: $2,200")
print("  Data   : Real Deribit ETH prices + Real Deribit IV")
print("=" * 62)

end_dt   = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
start_dt = end_dt - timedelta(days=730)

# ---------------------------------------------------------------------------
# Step 1: Fetch real ETH price history
# ---------------------------------------------------------------------------
print(f"\n[1/4] Fetching real ETH price history from Deribit...")
print(f"      {start_dt.date()} -> {end_dt.date()}")

try:
    ohlcv = fetch_eth_ohlcv(start_dt, end_dt)
    if ohlcv.empty:
        raise ValueError("Empty OHLCV response")
    print(f"      Got {len(ohlcv)} daily candles")
    print(f"      ETH range: ${ohlcv['low'].min():.0f} - ${ohlcv['high'].max():.0f}")
    print(f"      Start: ${ohlcv['close'].iloc[0]:.0f}  |  End: ${ohlcv['close'].iloc[-1]:.0f}")
except Exception as e:
    print(f"      ERROR fetching OHLCV: {e}")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Step 2: Fetch real Deribit IV history
# ---------------------------------------------------------------------------
print(f"\n[2/4] Fetching real Deribit historical volatility...")
try:
    iv_df = fetch_historical_iv("ETH")
    # Filter to our date range
    iv_df = iv_df[iv_df["date"] >= start_dt.date()].reset_index(drop=True)
    print(f"      Got {len(iv_df)} daily IV data points")
    if not iv_df.empty:
        print(f"      IV range: {iv_df['iv'].min():.1f}% - {iv_df['iv'].max():.1f}%")
        print(f"      Avg IV  : {iv_df['iv'].mean():.1f}%")
except Exception as e:
    print(f"      WARNING: Could not fetch IV history: {e}")
    iv_df = pd.DataFrame()

# Merge price + IV by date
ohlcv["date"] = ohlcv["timestamp"].dt.date
if not iv_df.empty:
    merged = ohlcv.merge(iv_df[["date", "iv_decimal"]], on="date", how="left")
    # Forward-fill any missing IV values
    merged["iv_decimal"] = merged["iv_decimal"].ffill().fillna(0.80)
else:
    merged = ohlcv.copy()
    merged["iv_decimal"] = 0.80

merged = merged.sort_values("date").reset_index(drop=True)
print(f"\n      Merged dataset: {len(merged)} rows, columns: {list(merged.columns)}")

# Realised vol from actual prices
log_returns = np.diff(np.log(merged["close"].values))
realised_vol = float(np.std(log_returns) * np.sqrt(252))
print(f"      Realised vol (ETH): {realised_vol*100:.1f}% annualised")

# ---------------------------------------------------------------------------
# Step 3: Build real-data option chains
# ---------------------------------------------------------------------------
print(f"\n[3/4] Building option chain snapshots from real data...")
parquet_dir  = "data/deribit_backtest/parquet"
expiry_dates = [
    start_dt + timedelta(weeks=w)
    for w in range(1, 115)
]

# Anchor spot per expiry = closing price on the Monday 7 days before expiry
expiry_anchor: dict = {}
for exp in expiry_dates:
    anchor_date = (exp - timedelta(days=7)).date()
    row = merged[merged["date"] <= anchor_date]
    if row.empty:
        expiry_anchor[exp] = float(merged["close"].iloc[0])
    else:
        expiry_anchor[exp] = float(row["close"].iloc[-1])

storage = ParquetStorage(parquet_dir)
for _, row in merged.iterrows():
    date  = row["date"]
    spot  = float(row["close"])
    iv    = float(row["iv_decimal"])
    dt    = datetime.combine(date, datetime.min.time()).replace(tzinfo=timezone.utc)

    chains = []
    for exp in expiry_dates:
        dte = (exp - dt).days
        if dte < 1 or dte > 35:
            continue
        chains.extend(build_chain(spot, dt, iv, dte, expiry_anchor[exp]))

    if chains:
        storage.save_quotes(chains)

print(f"      Done. Stored {len(merged)} daily snapshots.")

# ---------------------------------------------------------------------------
# Step 4: Run EMA spread backtest
# ---------------------------------------------------------------------------
print(f"\n[4/4] Running EMA spread backtest on real Deribit data...")

cfg = EMASpreadConfig(
    fast_ema=9,
    slow_ema=21,
    target_dte_min=5,
    target_dte_max=10,
    short_delta_min=0.20,
    short_delta_max=0.30,
    wing_delta_min=0.08,
    wing_delta_max=0.12,
    take_profit_pct=0.50,
    stop_loss_multiplier=1.5,
    close_dte=1,
    iv_percentile_min=10.0,       # relaxed from 25.0 → more entries
    min_trend_strength=0.003,     # relaxed from 0.005 → 0.3% threshold
    account_size=2200.0,
    max_risk_per_trade_pct=0.20,  # raised from 0.12 → allows wider spreads
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
signal_log = results.get("signal_log", [])
avg_spot   = float(merged["close"].mean())

if trades:
    wins   = [t.realized_pnl for t in trades if (t.realized_pnl or 0) > 0]
    losses = [t.realized_pnl for t in trades if (t.realized_pnl or 0) <= 0]
    tp  = sum(1 for t in trades if "take_profit"         in (t.exit_reason or ""))
    sl  = sum(1 for t in trades if "stop_loss"           in (t.exit_reason or ""))
    rev = sum(1 for t in trades if "signal_reversal"     in (t.exit_reason or ""))
    exp = sum(1 for t in trades if "close_before_expiry" in (t.exit_reason or ""))
    be  = sum(1 for t in trades if "backtest_end"        in (t.exit_reason or ""))

    fee_per_contract = 0.0003  # ETH per leg per contract
    total_fees_eth = sum(
        fee_per_contract * (4 if (t.short_call_strike > 0 and t.short_put_strike > 0) else 2) * 2
        for t in trades
    )
    total_fees_usd = total_fees_eth * avg_spot

    total_pnl_usd = m.get("total_pnl", 0) * avg_spot
    print(f"\n  Total PnL (USD est)  : ${total_pnl_usd:+.2f}  (at avg ETH ${avg_spot:.0f})")
    print(f"  Total fees paid      : {total_fees_eth:.5f} ETH  (~${total_fees_usd:.2f})")
    print(f"  Gross PnL (pre-fee)  : ${total_pnl_usd + total_fees_usd:+.2f}")
    print(f"\n  Exit breakdown:")
    print(f"    Take-profit        : {tp}")
    print(f"    Stop-loss (1.5x)   : {sl}")
    print(f"    Signal reversal    : {rev}")
    print(f"    Closed at expiry   : {exp}")
    print(f"    Closed at BT end   : {be}")
    if wins:
        print(f"    Avg win            : +{np.mean(wins):.5f} ETH  (~${np.mean(wins)*avg_spot:+.2f})")
    if losses:
        print(f"    Avg loss           : {np.mean(losses):.5f} ETH  (~${np.mean(losses)*avg_spot:+.2f})")
    if wins and losses and sum(losses) != 0:
        print(f"    Profit factor      : {abs(sum(wins)/sum(losses)):.2f}")

    print("\n" + "=" * 80)
    print("  Per-Trade Log  (Real ETH Prices + Real Deribit IV)")
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
        n_legs = 4 if (t.short_call_strike > 0 and t.short_put_strike > 0) else 2
        fee_eth = fee_per_contract * n_legs * 2  # open + close
        fee_usd = fee_eth * t.underlying_at_entry
        rows.append({
            "Entry":      str(t.entry_time.date()),
            "Exit":       str(t.exit_time.date()) if t.exit_time else "-",
            "Type":       stype,
            "ETH":        f"${t.underlying_at_entry:.0f}",
            "Strikes":    stk,
            "Credit ETH": f"{t.credit_received:.5f}",
            "Fees USD":   f"${fee_usd:.2f}",
            "PnL ETH":    f"{(t.realized_pnl or 0):+.5f}",
            "PnL USD":    f"${pnl_usd:+.0f}",
            "Reason":     (t.exit_reason or "")[:26],
        })
    df = pd.DataFrame(rows)
    print(df.to_string(index=False))

    os.makedirs("data/deribit_backtest", exist_ok=True)
    df.to_csv("data/deribit_backtest/trade_history.csv", index=False)

    # EMA signal log
    if signal_log:
        sl_df = pd.DataFrame(signal_log)
        sl_df.to_csv("data/deribit_backtest/signal_log.csv", index=False)
        print(f"\n  Trade history -> data/deribit_backtest/trade_history.csv")
        print(f"  Signal log    -> data/deribit_backtest/signal_log.csv")

        print("\n" + "=" * 70)
        print("  Weekly EMA Signal Log (Mondays)")
        print("=" * 70)
        print(sl_df[["date","spot","signal","action","reason"]].to_string(index=False))

    # ETH price stats used
    print("\n" + "=" * 70)
    print("  Real ETH Price Data Summary")
    print("=" * 70)
    price_summary = merged[["date","close","iv_decimal"]].copy()
    price_summary["iv_pct"] = (price_summary["iv_decimal"] * 100).round(1)
    price_summary["date"] = price_summary["date"].astype(str)
    price_summary = price_summary.rename(columns={"close": "ETH_close", "iv_pct": "Deribit_IV%"})
    # Show weekly samples
    weekly = price_summary.iloc[::7]
    print(weekly[["date","ETH_close","Deribit_IV%"]].to_string(index=False))
