"""Run mag7_leaps_strategy.py backtest on QuantConnect."""
import hashlib, requests, time, os

QC_USER_ID    = "426855"
QC_TOKEN      = "a197cd7a8911f9c32603f0f10601e78d4dbf223de66d161b9551551d28723910"
QC_PROJECT_ID = 28932760
STRATEGY_FILE = os.path.join(os.path.dirname(__file__), "mag7_leaps_strategy.py")

def auth():
    ts = str(int(time.time()))
    h  = hashlib.sha256(f"{QC_TOKEN}:{ts}".encode()).hexdigest()
    return (QC_USER_ID, h), {"Timestamp": ts}

def upload():
    content = open(STRATEGY_FILE).read()
    a, h = auth()
    r = requests.post("https://www.quantconnect.com/api/v2/files/update",
        auth=a, headers=h, json={"projectId": QC_PROJECT_ID, "name": "main.py", "content": content})
    ok = r.json().get("success", False)
    print(f"Upload: {'OK' if ok else 'FAILED'} — {r.json()}")
    return ok

def compile():
    a, h = auth()
    r = requests.post("https://www.quantconnect.com/api/v2/compile/create",
        auth=a, headers=h, json={"projectId": QC_PROJECT_ID})
    compile_id = r.json().get("compileId")
    print(f"Compile ID: {compile_id}")
    for i in range(20):
        time.sleep(3)
        a, h = auth()
        r = requests.get("https://www.quantconnect.com/api/v2/compile/read",
            auth=a, headers=h, params={"projectId": QC_PROJECT_ID, "compileId": compile_id})
        state = r.json().get("state")
        logs  = r.json().get("logs", [])
        print(f"  [{i+1}] {state}")
        if state == "BuildSuccess":
            return compile_id
        if state == "BuildError":
            print("BUILD ERRORS:")
            for l in logs: print(" ", l)
            return None
    return None

def launch(compile_id):
    a, h = auth()
    r = requests.post("https://www.quantconnect.com/api/v2/backtests/create",
        auth=a, headers=h,
        json={"projectId": QC_PROJECT_ID, "compileId": compile_id,
              "backtestName": "Mag7 LEAPS + Crash Call"})
    bt_id = r.json().get("backtest", {}).get("backtestId")
    print(f"Backtest ID: {bt_id}")
    return bt_id

def poll(bt_id):
    print("Polling", end="", flush=True)
    for _ in range(120):
        time.sleep(30)
        print(".", end="", flush=True)
        a, h = auth()
        r = requests.get("https://www.quantconnect.com/api/v2/backtests/read",
            auth=a, headers=h, params={"projectId": QC_PROJECT_ID, "backtestId": bt_id})
        bt = r.json().get("backtest", {})
        if bt.get("error"):
            print(f"\nERROR: {bt['error']}")
            return
        if bt.get("completed"):
            print("\n\nDONE")
            s = bt.get("statistics", {})
            print(f"  CAGR       : {s.get('Compounding Annual Return','—')}")
            print(f"  Drawdown   : {s.get('Drawdown','—')}")
            print(f"  Sharpe     : {s.get('Sharpe Ratio','—')}")
            print(f"  End Equity : ${float(s.get('End Equity', 0)):,.0f}")
            print(f"  Win Rate   : {s.get('Win Rate','—')}")
            print(f"  Trades     : {s.get('Total Orders','—')}")
            rw = bt.get("rollingWindow", {}) or {}
            yearly = {k[4:8]: v.get("portfolioStatistics",{}).get("totalNetProfit")
                      for k, v in rw.items() if k.startswith("M12_")}
            if yearly:
                print("\nYearly P&L:")
                for yr in sorted(yearly):
                    pnl = yearly[yr]
                    if pnl is not None:
                        print(f"  {yr}: {float(pnl):+.1%}")
            print(f"\nBacktest ID: {bt_id}")
            return
    print("\nTimed out")

if __name__ == "__main__":
    print("=== Mag7 LEAPS + Crash Call Backtest ===")
    if not upload(): raise SystemExit("Upload failed")
    cid = compile()
    if not cid: raise SystemExit("Compile failed")
    bt_id = launch(cid)
    if not bt_id: raise SystemExit("Launch failed")
    poll(bt_id)
