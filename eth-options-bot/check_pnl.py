import csv, os
from datetime import datetime, timezone

today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
print("Date:", today)
print()

def read_pnl(path, label):
    if not os.path.exists(path):
        print(f"{label}: no file yet")
        return 0.0
    trades = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            if row.get("exit_time", "").startswith(today):
                trades.append(row)
    if not trades:
        print(f"{label}: no trades closed today")
        return 0.0
    total = 0.0
    for t in trades:
        pnl = float(t.get("pnl_usd", "0").replace("$", "").replace("+", ""))
        total += pnl
        print(f"  [{t.get('window')}] {t.get('spread_type')} | entry=${t.get('spot_entry')} exit=${t.get('spot_exit')} | PnL={t.get('pnl_usd')} | {t.get('exit_reason')}")
    wins = sum(1 for t in trades if float(t.get("pnl_usd", "0").replace("$", "").replace("+", "")) > 0)
    print(f"  TOTAL {label}: ${total:+.2f} | {len(trades)} trades | {wins}W {len(trades)-wins}L")
    return total

base = "C:/Users/Administrator/Desktop/projects/eth-options-bot/data"
t1 = read_pnl(f"{base}/live_0dte_trades.csv", "ETH 0DTE")
print()
t2 = read_pnl(f"{base}/live_trades.csv", "ETH 7DTE")
print()
t3 = read_pnl(f"{base}/live_btc_0dte_trades.csv", "BTC 0DTE")
print()
print(f"GRAND TOTAL: ${t1+t2+t3:+.2f}")
