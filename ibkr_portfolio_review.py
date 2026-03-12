"""
IBKR Options Portfolio Review
==============================
Connects to TWS, reads all option positions, computes net Greeks,
identifies risks, then sends a Claude-generated analysis to Discord.

Schedule: run daily after market close (e.g. 21:30 UTC = 4:30 PM ET)

TWS Setup:
  - Edit > Global Configuration > API > Settings
  - Enable "Enable ActiveX and Socket Clients"
  - Uncheck "Read-Only API" (so Greeks can be requested)
  - Socket port: 7496 (live) or 7497 (paper)
"""

import os
import time
import requests
import anthropic
from collections import defaultdict
from ib_insync import IB, Option, util

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
TWS_HOST      = "127.0.0.1"
TWS_PORT      = 7496          # 7496 = live, 7497 = paper
TWS_CLIENT_ID = 10            # use a unique ID not used by other scripts

DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

GREEKS_WAIT_SECS  = 3    # seconds to wait for market data per batch
MAX_DISCORD_CHARS = 1900  # leave headroom below 2000 limit

# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------
def send_discord(msg: str):
    """Send message, splitting into chunks if over Discord's 2000-char limit."""
    chunks = [msg[i:i+MAX_DISCORD_CHARS] for i in range(0, len(msg), MAX_DISCORD_CHARS)]
    for chunk in chunks:
        try:
            resp = requests.post(DISCORD_WEBHOOK, json={"content": chunk}, timeout=10)
            if not resp.ok:
                print(f"Discord error: {resp.status_code} {resp.text}")
        except Exception as e:
            print(f"Discord send failed: {e}")
        time.sleep(0.5)


# ---------------------------------------------------------------------------
# Portfolio fetching
# ---------------------------------------------------------------------------
def fetch_option_positions(ib: IB) -> list[dict]:
    """Return list of option position dicts with Greeks."""
    positions = ib.positions()
    option_positions = [p for p in positions if p.contract.secType == "OPT"]

    if not option_positions:
        return []

    # Qualify contracts to get full details (multiplier, etc.)
    contracts = [p.contract for p in option_positions]
    ib.qualifyContracts(*contracts)

    # Request market data for all positions (snapshot)
    tickers = []
    for pos in option_positions:
        ticker = ib.reqMktData(pos.contract, "225", False, False)  # 225 = Greeks
        tickers.append((pos, ticker))

    ib.sleep(GREEKS_WAIT_SECS)

    results = []
    for pos, ticker in tickers:
        c = pos.contract
        greeks = ticker.modelGreeks

        delta  = greeks.delta  if greeks else None
        gamma  = greeks.gamma  if greeks else None
        theta  = greeks.theta  if greeks else None
        vega   = greeks.vega   if greeks else None
        iv     = greeks.impliedVol if greeks else None
        opt_price = greeks.optPrice if greeks else (ticker.last or ticker.close or 0)

        multiplier = float(c.multiplier or 100)
        qty        = pos.position  # positive = long, negative = short

        results.append({
            "symbol":     c.symbol,
            "expiry":     c.lastTradeDateOrContractMonth,  # YYYYMMDD
            "strike":     float(c.strike),
            "right":      c.right,          # C or P
            "qty":        qty,
            "multiplier": multiplier,
            "price":      opt_price or 0,
            "delta":      delta,
            "gamma":      gamma,
            "theta":      theta,
            "vega":       vega,
            "iv":         iv,
            # position-level Greeks (scaled by qty * multiplier)
            "pos_delta":  (delta * qty * multiplier) if delta is not None else None,
            "pos_theta":  (theta * qty * multiplier) if theta is not None else None,
            "pos_vega":   (vega  * qty * multiplier) if vega  is not None else None,
            "pos_gamma":  (gamma * qty * multiplier) if gamma is not None else None,
            "market_value": opt_price * abs(qty) * multiplier if opt_price else None,
        })

    # Cancel market data subscriptions
    for pos, ticker in tickers:
        ib.cancelMktData(pos.contract)

    return results


# ---------------------------------------------------------------------------
# Risk helpers
# ---------------------------------------------------------------------------
def days_to_expiry(expiry_str: str) -> int:
    from datetime import date
    exp = date(int(expiry_str[:4]), int(expiry_str[4:6]), int(expiry_str[6:8]))
    return (exp - date.today()).days


def format_expiry(expiry_str: str) -> str:
    from datetime import date
    exp = date(int(expiry_str[:4]), int(expiry_str[4:6]), int(expiry_str[6:8]))
    return exp.strftime("%d %b %Y")


def identify_structures(positions: list[dict]) -> list[dict]:
    """
    Group legs by (symbol, expiry) and label as:
    Naked Call/Put, Spread (2 same-type legs), Iron Condor (4 legs), Other
    """
    groups = defaultdict(list)
    for p in positions:
        groups[(p["symbol"], p["expiry"])].append(p)

    structures = []
    for (sym, exp), legs in groups.items():
        calls = [l for l in legs if l["right"] == "C"]
        puts  = [l for l in legs if l["right"] == "P"]
        n     = len(legs)

        if n == 1:
            leg  = legs[0]
            kind = "Naked Call" if leg["right"] == "C" else "Naked Put"
        elif n == 2 and len(calls) == 2:
            kind = "Call Spread"
        elif n == 2 and len(puts) == 2:
            kind = "Put Spread"
        elif n == 2 and len(calls) == 1 and len(puts) == 1:
            kind = "Strangle/Straddle"
        elif n == 4 and len(calls) == 2 and len(puts) == 2:
            kind = "Iron Condor"
        else:
            kind = f"{n}-Leg Position"

        structures.append({
            "symbol":     sym,
            "expiry":     exp,
            "dte":        days_to_expiry(exp),
            "kind":       kind,
            "legs":       legs,
            "net_delta":  sum(l["pos_delta"] for l in legs if l["pos_delta"] is not None),
            "net_theta":  sum(l["pos_theta"] for l in legs if l["pos_theta"] is not None),
            "net_vega":   sum(l["pos_vega"]  for l in legs if l["pos_vega"]  is not None),
            "net_gamma":  sum(l["pos_gamma"] for l in legs if l["pos_gamma"] is not None),
        })

    structures.sort(key=lambda x: x["dte"])
    return structures


def build_portfolio_text(structures: list[dict]) -> str:
    """Build a text summary of the portfolio for Claude."""
    lines = ["IBKR Options Portfolio Snapshot\n"]

    for s in structures:
        exp_label = format_expiry(s["expiry"])
        lines.append(f"{s['symbol']} | {s['kind']} | Exp: {exp_label} ({s['dte']} DTE)")
        for leg in sorted(s["legs"], key=lambda x: x["strike"]):
            side = "LONG" if leg["qty"] > 0 else "SHORT"
            iv_str = f" IV:{leg['iv']*100:.1f}%" if leg["iv"] else ""
            delta_str = f" delta:{leg['delta']:.2f}" if leg["delta"] is not None else ""
            lines.append(
                f"  {side} {abs(leg['qty'])}x {leg['strike']}{leg['right']}"
                f"{delta_str}{iv_str} @ ${leg['price']:.2f}"
            )
        lines.append(
            f"  Net: delta={s['net_delta']:+.2f}  theta={s['net_theta']:+.2f}/day"
            f"  vega={s['net_vega']:+.2f}  gamma={s['net_gamma']:+.4f}"
        )
        lines.append("")

    # Portfolio totals
    all_delta = sum(s["net_delta"] for s in structures)
    all_theta = sum(s["net_theta"] for s in structures)
    all_vega  = sum(s["net_vega"]  for s in structures)

    lines.append("--- Portfolio Totals ---")
    lines.append(f"Net Delta: {all_delta:+.2f}")
    lines.append(f"Net Theta: {all_theta:+.2f}/day")
    lines.append(f"Net Vega:  {all_vega:+.2f}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Claude analysis
# ---------------------------------------------------------------------------
def get_claude_analysis(portfolio_text: str) -> str:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    prompt = f"""You are an options trading risk advisor. Analyze this portfolio and provide:

1. **Key Risks** — identify the top 3-5 risks (e.g. high delta exposure, positions expiring soon, naked calls with unlimited risk, high vega in volatile market)
2. **Positions to Watch** — flag any specific positions that need attention today or this week
3. **Actionable Ideas** — 2-3 concrete suggestions (roll, hedge, close, adjust strikes)

Be direct and concise. Use bullet points. Focus on what matters most right now.

{portfolio_text}"""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}]
    )
    return message.content[0].text


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    print("Connecting to TWS...")
    ib = IB()
    try:
        ib.connect(TWS_HOST, TWS_PORT, clientId=TWS_CLIENT_ID, timeout=10)
    except Exception as e:
        msg = f"**IBKR Review** — Could not connect to TWS: {e}"
        print(msg)
        send_discord(msg)
        return

    print("Connected. Fetching positions...")
    try:
        positions = fetch_option_positions(ib)
    finally:
        ib.disconnect()

    if not positions:
        msg = "**IBKR Daily Review** — No option positions found."
        print(msg)
        send_discord(msg)
        return

    structures   = identify_structures(positions)
    portfolio_txt = build_portfolio_text(structures)
    print(portfolio_txt)
    print("\nAsking Claude for analysis...")

    analysis = get_claude_analysis(portfolio_txt)

    header = "**IBKR Options Portfolio Review**\n"
    send_discord(header + "```\n" + portfolio_txt + "\n```")
    send_discord("**Risk Analysis & Ideas**\n" + analysis)
    print("Sent to Discord.")


if __name__ == "__main__":
    util.logToConsole()
    main()
