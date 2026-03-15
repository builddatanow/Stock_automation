"""Run mag7_pmcc_strategy.py backtest on QuantConnect.
Creates a new QC project on first run, saves project ID to mag7_pmcc_project.txt.
"""
import hashlib, requests, time, os, json

QC_USER_ID    = "426855"
QC_TOKEN      = "a197cd7a8911f9c32603f0f10601e78d4dbf223de66d161b9551551d28723910"
STRATEGY_FILE = os.path.join(os.path.dirname(__file__), "mag7_pmcc_strategy.py")
PROJECT_ID_FILE = os.path.join(os.path.dirname(__file__), "mag7_pmcc_project.txt")

def auth():
    ts = str(int(time.time()))
    h  = hashlib.sha256(f"{QC_TOKEN}:{ts}".encode()).hexdigest()
    return (QC_USER_ID, h), {"Timestamp": ts}

def get_or_create_project():
    if os.path.exists(PROJECT_ID_FILE):
        pid = int(open(PROJECT_ID_FILE).read().strip())
        print(f"Using existing project ID: {pid}")
        return pid

    print("Creating new QC project: Mag7 PMCC...")
    a, h = auth()
    r = requests.post("https://www.quantconnect.com/api/v2/projects/create",
        auth=a, headers=h,
        json={"name": "Mag7 PMCC Strategy", "language": "Py"})
    data = r.json()
    if not data.get("success"):
        raise SystemExit(f"Failed to create project: {data}")
    pid = data["projects"][0]["projectId"]
    print(f"Created project ID: {pid}")
    open(PROJECT_ID_FILE, "w").write(str(pid))
    return pid

def upload(project_id):
    content = open(STRATEGY_FILE).read()
    a, h = auth()
    r = requests.post("https://www.quantconnect.com/api/v2/files/update",
        auth=a, headers=h,
        json={"projectId": project_id, "name": "main.py", "content": content})
    ok = r.json().get("success", False)
    print(f"Upload: {'OK' if ok else 'FAILED'} — {r.json()}")
    return ok

def compile(project_id):
    a, h = auth()
    r = requests.post("https://www.quantconnect.com/api/v2/compile/create",
        auth=a, headers=h, json={"projectId": project_id})
    compile_id = r.json().get("compileId")
    print(f"Compile ID: {compile_id}")
    for i in range(20):
        time.sleep(3)
        a, h = auth()
        r = requests.get("https://www.quantconnect.com/api/v2/compile/read",
            auth=a, headers=h,
            params={"projectId": project_id, "compileId": compile_id})
        state = r.json().get("state")
        logs  = r.json().get("logs", [])
        print(f"  [{i+1}] {state}")
        if state == "BuildSuccess":
            return compile_id
        if state == "BuildError":
            print("BUILD ERRORS:")
            for l in logs:
                print(" ", l)
            return None
    return None

def launch(project_id, compile_id):
    a, h = auth()
    r = requests.post("https://www.quantconnect.com/api/v2/backtests/create",
        auth=a, headers=h,
        json={"projectId": project_id, "compileId": compile_id,
              "backtestName": "Mag7 PMCC — 30DTE + Earnings/FOMC Filter"})
    bt_id = r.json().get("backtest", {}).get("backtestId")
    print(f"Backtest ID: {bt_id}")
    return bt_id

def poll(project_id, bt_id):
    print("Polling", end="", flush=True)
    for _ in range(120):
        time.sleep(30)
        print(".", end="", flush=True)
        a, h = auth()
        r = requests.get("https://www.quantconnect.com/api/v2/backtests/read",
            auth=a, headers=h,
            params={"projectId": project_id, "backtestId": bt_id})
        bt = r.json().get("backtest", {})
        if bt.get("error"):
            print(f"\nERROR: {bt['error']}")
            return
        if bt.get("completed"):
            print("\n\nDONE")
            s = bt.get("statistics", {})
            print(f"  CAGR       : {s.get('Compounding Annual Return', '—')}")
            print(f"  Drawdown   : {s.get('Drawdown', '—')}")
            print(f"  Sharpe     : {s.get('Sharpe Ratio', '—')}")
            print(f"  End Equity : ${float(s.get('End Equity', 0)):,.0f}")
            print(f"  Win Rate   : {s.get('Win Rate', '—')}")
            print(f"  Trades     : {s.get('Total Orders', '—')}")
            rw = bt.get("rollingWindow", {}) or {}
            yearly = {
                k[4:8]: v.get("portfolioStatistics", {}).get("totalNetProfit")
                for k, v in rw.items() if k.startswith("M12_")
            }
            if yearly:
                print("\nYearly P&L:")
                for yr in sorted(yearly):
                    pnl = yearly[yr]
                    if pnl is not None:
                        print(f"  {yr}: {float(pnl):+.1%}")
            print(f"\nBacktest ID: {bt_id}")
            print(f"Project ID : {project_id}")
            return
    print("\nTimed out")

if __name__ == "__main__":
    print("=== Mag7 PMCC — LEAPS + Weekly Income Backtest ===")
    project_id = get_or_create_project()
    if not upload(project_id): raise SystemExit("Upload failed")
    cid = compile(project_id)
    if not cid: raise SystemExit("Compile failed")
    bt_id = launch(project_id, cid)
    if not bt_id: raise SystemExit("Launch failed")
    poll(project_id, bt_id)
