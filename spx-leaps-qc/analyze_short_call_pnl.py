"""
Fetch MSFT PMCC backtest orders from QC and break down
P&L by position type: LEAPS, short calls, puts, extra calls.
"""
import hashlib, requests, time
from collections import defaultdict
from datetime import datetime, date

QC_USER_ID = "426855"
QC_TOKEN   = "a197cd7a8911f9c32603f0f10601e78d4dbf223de66d161b9551551d28723910"
PROJECT_ID = 29003521
BT_ID      = "9edbc5e5163643710772d258d3c26cf2"   # MSFT PMCC v4 (45 DTE, delta 0.15)

def auth():
    ts = str(int(time.time()))
    h  = hashlib.sha256(f"{QC_TOKEN}:{ts}".encode()).hexdigest()
    return (QC_USER_ID, h), {"Timestamp": ts}

def fetch_all_orders():
    all_orders = []
    start = 0
    page_size = 100
    while True:
        a, h = auth()
        r = requests.get(
            "https://www.quantconnect.com/api/v2/backtests/orders/read",
            auth=a, headers=h,
            params={"projectId": PROJECT_ID, "backtestId": BT_ID,
                    "start": start, "end": start + page_size}
        )
        batch = r.json().get("orders", [])
        if not batch:
            break
        all_orders.extend(batch)
        print(f"  fetched {len(all_orders)} orders...", end="\r")
        if len(batch) < page_size:
            break
        start += page_size
    print()
    return all_orders

def parse_option_symbol(sym_value):
    """
    Parse QC option symbol like 'MSFT  160115C00042000'
    Returns (expiry_date, option_type 'C'/'P', strike_float) or None
    """
    s = sym_value.strip()
    # Find C or P after the ticker+spaces
    for i, ch in enumerate(s):
        if ch in ('C', 'P') and i > 2:
            date_str   = s[i-6:i]        # YYMMDD
            option_type = ch
            strike_raw  = s[i+1:]         # e.g. '00042000'
            try:
                yr  = 2000 + int(date_str[0:2])
                mo  = int(date_str[2:4])
                dy  = int(date_str[4:6])
                exp = date(yr, mo, dy)
                strike = float(strike_raw) / 1000.0
                return exp, option_type, strike
            except:
                return None
    return None

def classify_call(trade_date_str, sym_value):
    """
    Given trade date and symbol, classify a call option as:
    'short_call' (21-35 DTE), 'leaps' (240-540 DTE), 'extra_call' (540+ DTE)
    """
    parsed = parse_option_symbol(sym_value)
    if not parsed:
        return "call_unknown"
    expiry, _, _ = parsed
    try:
        tdate = datetime.strptime(trade_date_str[:10], "%Y-%m-%d").date()
        dte = (expiry - tdate).days
    except:
        return "call_unknown"

    if dte <= 60:
        return "short_call"
    elif dte <= 540:
        return "leaps"
    else:
        return "extra_call"

def main():
    print(f"Fetching orders for backtest {BT_ID}...")
    orders = fetch_all_orders()
    print(f"Total orders: {len(orders)}\n")

    # Only look at filled option orders
    option_orders = [o for o in orders if o.get("securityType") == 2]
    print(f"Option orders: {len(option_orders)}")

    # Bucket P&L
    # direction: 0=buy, 1=sell
    # For options: value = price * qty (WITHOUT x100 multiplier in QC API)
    # Actual cash = fillPrice * fillQty * 100
    # Sell = cash in (positive), Buy = cash out (negative)

    buckets = {
        "short_call": {"credits": 0.0, "debits": 0.0, "opens": 0, "closes": 0, "yearly_net": defaultdict(float)},
        "leaps":      {"credits": 0.0, "debits": 0.0, "opens": 0, "closes": 0, "yearly_net": defaultdict(float)},
        "extra_call": {"credits": 0.0, "debits": 0.0, "opens": 0, "closes": 0, "yearly_net": defaultdict(float)},
        "put":        {"credits": 0.0, "debits": 0.0, "opens": 0, "closes": 0, "yearly_net": defaultdict(float)},
    }

    for o in option_orders:
        sym_value = o["symbol"]["value"]
        direction = o.get("direction", 0)   # 0=buy, 1=sell
        trade_time = o.get("lastFillTime") or o.get("time", "")
        year = trade_time[:4]

        # Get fill price and qty from events
        fill_price = 0.0
        fill_qty   = 0.0
        for ev in o.get("events", []):
            if ev.get("status") == "filled" and ev.get("fillQuantity", 0) != 0:
                fill_price = float(ev.get("fillPrice", 0))
                fill_qty   = abs(float(ev.get("fillQuantity", 0)))
                break

        if fill_price == 0 or fill_qty == 0:
            fill_price = float(o.get("price", 0))
            fill_qty   = abs(float(o.get("quantity", 0)))

        cash = fill_price * fill_qty * 100  # actual dollar value

        parsed = parse_option_symbol(sym_value)
        if not parsed:
            continue
        _, opt_type, _ = parsed

        if opt_type == "P":
            cat = "put"
        else:
            cat = classify_call(trade_time, sym_value)

        b = buckets.get(cat)
        if b is None:
            continue

        if direction == 1:   # sell
            b["credits"] += cash
            b["opens"]   += 1
            b["yearly_net"][year] += cash
        else:                 # buy
            b["debits"]  += cash
            b["closes"]  += 1
            b["yearly_net"][year] -= cash

    # ── Print results ──────────────────────────────────────
    SEP = "=" * 56

    print(f"\n{SEP}")
    print(f"  SHORT CALL INCOME (30-DTE sells)")
    print(SEP)
    sc = buckets["short_call"]
    net = sc["credits"] - sc["debits"]
    print(f"  Premium collected : ${sc['credits']:>10,.0f}  ({sc['opens']} opens)")
    print(f"  Cost to close     : ${sc['debits']:>10,.0f}  ({sc['closes']} closes)")
    print(f"  NET income        : ${net:>10,.0f}")
    print(f"\n  Year     Net income")
    print(f"  {'-'*24}")
    for yr in sorted(sc["yearly_net"]):
        print(f"  {yr}    ${sc['yearly_net'][yr]:>10,.0f}")

    print(f"\n{SEP}")
    print(f"  LEAPS (primary 450-DTE calls)")
    print(SEP)
    lp = buckets["leaps"]
    leaps_net = lp["credits"] - lp["debits"]
    print(f"  Sold (profit takes/rolls): ${lp['credits']:>10,.0f}  ({lp['opens']} sells)")
    print(f"  Bought (entries/rolls)   : ${lp['debits']:>10,.0f}  ({lp['closes']} buys)")
    print(f"  NET LEAPS P&L            : ${leaps_net:>10,.0f}")

    print(f"\n{SEP}")
    print(f"  EXTRA CALLS (600-DTE dip buys)")
    print(SEP)
    ex = buckets["extra_call"]
    extra_net = ex["credits"] - ex["debits"]
    print(f"  Sold                     : ${ex['credits']:>10,.0f}  ({ex['opens']} sells)")
    print(f"  Bought                   : ${ex['debits']:>10,.0f}  ({ex['closes']} buys)")
    print(f"  NET extra call P&L       : ${extra_net:>10,.0f}")

    print(f"\n{SEP}")
    print(f"  PUT HEDGES (45-60 DTE protection)")
    print(SEP)
    pt = buckets["put"]
    put_net = pt["credits"] - pt["debits"]
    print(f"  Sold (closed for profit) : ${pt['credits']:>10,.0f}  ({pt['opens']} sells)")
    print(f"  Bought (entries)         : ${pt['debits']:>10,.0f}  ({pt['closes']} buys)")
    print(f"  NET put P&L              : ${put_net:>10,.0f}")

    print(f"\n{SEP}")
    print(f"  SUMMARY")
    print(SEP)
    total_options = net + leaps_net + extra_net + put_net
    print(f"  Short call income  : ${net:>10,.0f}")
    print(f"  LEAPS P&L          : ${leaps_net:>10,.0f}")
    print(f"  Extra call P&L     : ${extra_net:>10,.0f}")
    print(f"  Put hedge P&L      : ${put_net:>10,.0f}")
    print(f"  -------------------------")
    print(f"  Total options P&L  : ${total_options:>10,.0f}")
    print(f"{SEP}\n")

if __name__ == "__main__":
    main()
