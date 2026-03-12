"""
Support & Resistance Level Calculator
======================================
Calculates Weekly + Monthly Pivot Point S/R levels for key tickers.
Sends formatted table to Discord.

Formula (Classic Pivot Points):
  PP  = (High + Low + Close) / 3          <- Central Level (CWL/CML)
  R1  = (2 * PP) - Low                    <- Bullish Target 1
  R2  = PP + (High - Low)                 <- Bullish Target 2
  R3  = High + 2 * (PP - Low)             <- Bullish Target 3
  S1  = (2 * PP) - High                   <- Bearish Target 1
  S2  = PP - (High - Low)                 <- Bearish Target 2
  S3  = Low - 2 * (High - PP)             <- Bearish Target 3
"""

import json
import sys
from datetime import datetime, timezone
from typing import Optional

import requests
import yfinance as yf

CONFIG_FILE = "config.json"

# ---------------------------------------------------------------------------
# Tickers  (display name -> yfinance symbol)
# ---------------------------------------------------------------------------
TICKERS = {
    # Indices
    "SPX":     "^GSPC",
    "NDX":     "^NDX",
    "DJI":     "^DJI",
    "IWM":     "IWM",
    # ETFs
    "SPY":     "SPY",
    "QQQ":     "QQQ",
    # Megacaps
    "AAPL":    "AAPL",
    "MSFT":    "MSFT",
    "NVDA":    "NVDA",
    "META":    "META",
    "AMZN":    "AMZN",
    "GOOG":    "GOOG",
    "TSLA":    "TSLA",
    "PLTR":    "PLTR",
    # Crypto
    "BTC":     "BTC-USD",
    "ETH":     "ETH-USD",
}

# ---------------------------------------------------------------------------
# Pivot Point calculation
# ---------------------------------------------------------------------------

def pivot_levels(high: float, low: float, close: float) -> dict:
    pp = (high + low + close) / 3
    r1 = (2 * pp) - low
    r2 = pp + (high - low)
    r3 = high + 2 * (pp - low)
    s1 = (2 * pp) - high
    s2 = pp - (high - low)
    s3 = low - 2 * (high - pp)
    return {"S3": s3, "S2": s2, "S1": s1, "PP": pp, "R1": r1, "R2": r2, "R3": r3}


def fmt(val: float, is_crypto: bool = False) -> str:
    if is_crypto:
        return f"{val:,.0f}"
    if val >= 1000:
        return f"{val:,.1f}"
    return f"{val:.2f}"


# ---------------------------------------------------------------------------
# Fetch OHLC data
# ---------------------------------------------------------------------------

def fetch_weekly_ohlc(symbol: str) -> Optional[tuple]:
    """Returns (high, low, close) of the previous completed week."""
    try:
        df = yf.download(symbol, period="1mo", interval="1wk", progress=False, auto_adjust=True)
        if isinstance(df.columns, __import__("pandas").MultiIndex): df.columns = df.columns.droplevel(1)
        if df is None or len(df) < 2:
            return None
        prev = df.iloc[-2]  # last completed week
        return float(prev["High"]), float(prev["Low"]), float(prev["Close"])
    except Exception as e:
        print(f"  ERROR {symbol} weekly: {e}")
        return None


def fetch_monthly_ohlc(symbol: str) -> Optional[tuple]:
    """Returns (high, low, close) of the previous completed month."""
    try:
        df = yf.download(symbol, period="6mo", interval="1mo", progress=False, auto_adjust=True)
        if isinstance(df.columns, __import__("pandas").MultiIndex): df.columns = df.columns.droplevel(1)
        if df is None or len(df) < 2:
            return None
        prev = df.iloc[-2]  # last completed month
        return float(prev["High"]), float(prev["Low"]), float(prev["Close"])
    except Exception as e:
        print(f"  ERROR {symbol} monthly: {e}")
        return None


def fetch_current_price(symbol: str) -> Optional[float]:
    try:
        t = yf.Ticker(symbol)
        hist = t.history(period="1d")
        if hist.empty:
            return None
        return float(hist["Close"].iloc[-1])
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Format output
# ---------------------------------------------------------------------------

def position_marker(price: float, pp: float) -> str:
    if price > pp:
        return "ABOVE"
    if price < pp:
        return "BELOW"
    return "AT"


def format_ticker_block(name: str, weekly: dict, monthly: dict,
                         current: Optional[float], is_crypto: bool) -> str:
    lines = [f"**{name}**"]
    if current is not None:
        w_pos = position_marker(current, weekly["PP"])
        m_pos = position_marker(current, monthly["PP"])
        lines.append(f"  Price: {fmt(current, is_crypto)}  |  vs CWL: {w_pos}  |  vs CML: {m_pos}")

    lines.append("```")
    lines.append(f"{'Level':<6}  {'Weekly':>10}  {'Monthly':>10}")
    lines.append("-" * 30)
    rows = [
        ("R3", "Bullish T3"),
        ("R2", "Bullish T2"),
        ("R1", "Bullish T1"),
        ("PP", "CWL / CML "),
        ("S1", "Bearish T1"),
        ("S2", "Bearish T2"),
        ("S3", "Bearish T3"),
    ]
    for label, desc in rows:
        wval = fmt(weekly[label], is_crypto)
        mval = fmt(monthly[label], is_crypto)
        marker = " <--" if label == "PP" else ""
        lines.append(f"{label:<6}  {wval:>10}  {mval:>10}{marker}")
    lines.append("```")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(send_discord: bool = True):
    with open(CONFIG_FILE) as f:
        cfg = json.load(f)

    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y-%m-%d %H:%M UTC")

    print(f"Calculating S/R levels — {date_str}")
    print(f"{'Ticker':<8} {'Current':>10}  {'CWL':>10}  {'CML':>10}  Weekly  Monthly")
    print("-" * 65)

    sections = {
        "INDICES & ETFs": ["SPX", "NDX", "DJI", "IWM", "SPY", "QQQ"],
        "MEGACAPS":       ["AAPL", "MSFT", "NVDA", "META", "AMZN", "GOOG", "TSLA", "PLTR"],
        "CRYPTO":         ["BTC", "ETH"],
    }

    discord_blocks = [
        f"**S/R LEVELS — {now.strftime('%b %d, %Y')} (Weekly & Monthly Pivot Points)**",
        f"*Generated: {date_str}*",
        "=" * 40,
    ]

    for section, names in sections.items():
        discord_blocks.append(f"\n**━━ {section} ━━**")
        for name in names:
            symbol = TICKERS[name]
            is_crypto = name in ("BTC", "ETH")

            weekly_ohlc  = fetch_weekly_ohlc(symbol)
            monthly_ohlc = fetch_monthly_ohlc(symbol)
            current      = fetch_current_price(symbol)

            if not weekly_ohlc or not monthly_ohlc:
                print(f"{name:<8} -- data unavailable")
                continue

            wl = pivot_levels(*weekly_ohlc)
            ml = pivot_levels(*monthly_ohlc)

            w_pos = position_marker(current, wl["PP"]) if current else "?"
            m_pos = position_marker(current, ml["PP"]) if current else "?"
            cur_str = fmt(current, is_crypto) if current else "N/A"

            print(f"{name:<8} {cur_str:>10}  {fmt(wl['PP'], is_crypto):>10}  "
                  f"{fmt(ml['PP'], is_crypto):>10}  {w_pos:<7} {m_pos}")

            block = format_ticker_block(name, wl, ml, current, is_crypto)
            discord_blocks.append(block)

    # Send to Discord
    if send_discord:
        webhook = cfg.get("discord_webhook", "")
        if not webhook:
            print("No Discord webhook in config.")
            return

        full_msg = "\n".join(discord_blocks)
        # Split into <=1900 char chunks, never mid-block
        chunks = []
        current_chunk = ""
        for block in discord_blocks:
            if len(current_chunk) + len(block) + 2 > 1900:
                if current_chunk:
                    chunks.append(current_chunk.strip())
                current_chunk = block
            else:
                current_chunk += "\n" + block
        if current_chunk:
            chunks.append(current_chunk.strip())

        print(f"\nSending {len(chunks)} chunk(s) to Discord...")
        for i, chunk in enumerate(chunks):
            r = requests.post(webhook, json={"content": chunk}, timeout=10)
            print(f"  Chunk {i+1}: {r.status_code} {'OK' if r.ok else r.text[:80]}")


if __name__ == "__main__":
    no_discord = "--no-discord" in sys.argv
    run(send_discord=not no_discord)
