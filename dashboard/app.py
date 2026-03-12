"""
Trading Bot Dashboard
=====================
Flask web dashboard for ETH/BTC options bots.
Run: python app.py
Access: http://localhost:5000
"""

import csv
import json
import os
import subprocess
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import psutil
from flask import Flask, jsonify, redirect, render_template, request, url_for

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
ETH_BOT_DIR  = r"C:\Users\Administrator\Desktop\projects\eth-options-bot"
PROJECTS_DIR = r"C:\Users\Administrator\Desktop\projects"
PYTHON       = r"C:\Program Files\Python311\python.exe"
TAT_DIR      = r"C:\Users\Administrator\AppData\Local\Packages\TradeAutomationToolbox_f46cr67q31chc\LocalState"

BOTS = {
    "eth_0dte": {
        "name":    "ETH 0 DTE",
        "script":  "run_live_0dte.py",
        "cwd":     ETH_BOT_DIR,
        "log":     os.path.join(ETH_BOT_DIR, "logs", "live_0dte.log"),
        "csv":     os.path.join(ETH_BOT_DIR, "data", "live_0dte_trades.csv"),
        "states":  [
            os.path.join(ETH_BOT_DIR, "data", "live_0dte_2pm_state.json"),
            os.path.join(ETH_BOT_DIR, "data", "live_0dte_3pm_state.json"),
            os.path.join(ETH_BOT_DIR, "data", "live_0dte_4pm_state.json"),
        ],
        "windows": ["2PM-Sydney", "3PM-Sydney", "4PM-Sydney"],
        "color":   "primary",
        "capital": 6600.0,   # 3 windows x $2,200
    },
    "eth_7dte": {
        "name":    "ETH 7 DTE",
        "script":  "run_live.py",
        "cwd":     ETH_BOT_DIR,
        "log":     os.path.join(ETH_BOT_DIR, "logs", "live.log"),
        "csv":     os.path.join(ETH_BOT_DIR, "data", "live_trades.csv"),
        "states":  [
            os.path.join(ETH_BOT_DIR, "data", "live_state.json"),
        ],
        "windows": ["09:00 UTC"],
        "color":   "info",
        "capital": 2200.0,
    },
    "btc_0dte": {
        "name":    "BTC 0 DTE",
        "script":  "run_live_btc_0dte.py",
        "cwd":     ETH_BOT_DIR,
        "log":     os.path.join(ETH_BOT_DIR, "logs", "live_btc_0dte.log"),
        "csv":     os.path.join(ETH_BOT_DIR, "data", "live_btc_0dte_trades.csv"),
        "states":  [
            os.path.join(ETH_BOT_DIR, "data", "live_btc_0dte_2pm_state.json"),
            os.path.join(ETH_BOT_DIR, "data", "live_btc_0dte_3pm_state.json"),
            os.path.join(ETH_BOT_DIR, "data", "live_btc_0dte_4pm_state.json"),
        ],
        "windows": ["2PM-Sydney", "3PM-Sydney", "4PM-Sydney"],
        "color":   "warning",
        "capital": 15000.0,  # 3 windows x $5,000
    },
    "spx": {
        "name":    "SPX 0 DTE",
        "script":  None,       # managed by TradeAutomationTool, not a Python script
        "cwd":     None,
        "log":     None,
        "csv":     None,       # uses TAT daily CSVs — read via read_spx_trades()
        "states":  [],
        "windows": ["1PM ET"],
        "color":   "success",
        "capital": 10000.0,
    },
}

# Crash tracker: {bot_id: [(timestamp), ...]}
_crash_log: dict[str, list[float]] = defaultdict(list)
_auto_restart: dict[str, bool]     = {"eth_0dte": True, "eth_7dte": True, "btc_0dte": True}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def is_running(script: str) -> bool:
    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            if "python" in (proc.info["name"] or "").lower():
                if script in " ".join(proc.info["cmdline"] or []):
                    return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return False


def start_bot(bot_id: str) -> bool:
    bot = BOTS[bot_id]
    if is_running(bot["script"]):
        return True
    log_path = bot["log"]
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    try:
        with open(log_path, "a") as lf:
            subprocess.Popen(
                [PYTHON, bot["script"]],
                cwd=bot["cwd"],
                stdout=lf, stderr=lf,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        time.sleep(3)
        return is_running(bot["script"])
    except Exception:
        return False


def stop_bot(bot_id: str):
    script = BOTS[bot_id]["script"]
    for proc in psutil.process_iter(["name", "cmdline", "pid"]):
        try:
            if "python" in (proc.info["name"] or "").lower():
                if script in " ".join(proc.info["cmdline"] or []):
                    proc.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass


def parse_pnl(val: str) -> float:
    try:
        return float(str(val).replace("$", "").replace("+", "").strip())
    except Exception:
        return 0.0


def _is_demo_trade(row: dict, filename: str) -> bool:
    """Detect if a TAT export row represents a paper/demo/simulation trade."""
    fname_lower = filename.lower()
    if any(k in fname_lower for k in ("paper", "demo", "sim", "test", "sandbox")):
        return True
    for col in ("AccountType", "Account", "TradeMode", "Mode",
                "PaperTrading", "IsPaper", "IsDemo", "SimulationMode"):
        val = (row.get(col) or "").strip().lower()
        if val in ("paper", "demo", "sim", "simulation", "true", "1", "yes", "sandbox", "test"):
            return True
    return False


def read_trades(csv_path: str) -> list[dict]:
    trades = []
    try:
        with open(csv_path, newline="") as f:
            for row in csv.DictReader(f):
                trades.append(row)
    except FileNotFoundError:
        pass
    return trades


SPX_START_DATE = "2026-01-01"   # only include trades on/after this date


def read_spx_trades() -> list[dict]:
    """Read all TAT daily export CSVs (export-* and exporttest-*) and return normalised trade dicts."""
    trades = []
    try:
        for fname in sorted(os.listdir(TAT_DIR)):
            # Accept export-YYYY-MM-DD.csv (live) and exporttest-YYYY-MM-DD.csv (demo)
            if not (fname.startswith("export") and fname.endswith(".csv")):
                continue
            path = os.path.join(TAT_DIR, fname)
            is_demo = _is_demo_trade({}, fname)  # filename-level detection first
            try:
                with open(path, newline="", encoding="utf-8") as f:
                    for row in csv.DictReader(f):
                        if not row.get("CloseDate"):
                            continue
                        # Row-level demo detection (checks Account column etc.)
                        row_is_demo = is_demo or _is_demo_trade(row, fname)

                        # Skip trades before SPX_START_DATE
                        if row.get("CloseDate", "") < SPX_START_DATE:
                            continue

                        try:
                            close_dt = datetime.strptime(
                                f"{row['CloseDate']} {row.get('CloseTime','00:00:00')}",
                                "%Y-%m-%d %H:%M:%S"
                            )
                            exit_time = close_dt.strftime("%Y-%m-%d %H:%M")
                        except Exception:
                            exit_time = row.get("CloseDate", "")

                        pnl = 0.0
                        try:
                            pnl = float(row.get("ProfitLoss", "0") or "0")
                        except Exception:
                            pass

                        # Credit = TotalPremium in dollars (e.g. 240 = $240)
                        credit_str = ""
                        try:
                            premium = float(row.get("TotalPremium", "0") or "0")
                            credit_str = f"${premium:.0f}"
                        except Exception:
                            pass

                        # Build leg label: "SELL 6850 / BUY 6930 C" etc.
                        trade_type = row.get("TradeType", "")
                        if "Call" in trade_type:
                            sc = row.get("ShortCall", "")
                            lc = row.get("LongCall", "")
                            legs = f"SELL {sc} / BUY {lc} C" if sc and lc else f"{sc}/{lc} C"
                        else:
                            sp = row.get("ShortPut", "")
                            lp = row.get("LongPut", "")
                            legs = f"SELL {sp} / BUY {lp} P" if sp and lp else f"{sp}/{lp} P"

                        # Derive window from OpenTime (13:00:00 -> 1PM ET)
                        open_time = row.get("OpenTime", "")
                        window = "1PM ET"
                        try:
                            hr = int(open_time.split(":")[0])
                            window = f"{hr % 12 or 12}{'AM' if hr < 12 else 'PM'} ET"
                        except Exception:
                            pass

                        trades.append({
                            "exit_time":   exit_time,
                            "pnl_usd":     str(pnl),
                            "spread_type": trade_type,
                            "strategy":    row.get("Template", row.get("Strategy", "")),
                            "status":      row.get("Status", ""),
                            "legs":        legs,
                            "credit_eth":  credit_str,
                            "bot_name":    "SPX",
                            "window":      window,
                            "color":       "success",
                            "is_demo":     row_is_demo,
                            "account":     row.get("Account", ""),
                        })
            except Exception:
                pass
    except FileNotFoundError:
        pass
    return trades


def pnl_summary(trades: list[dict]) -> dict:
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M")
    recent_trades = [t for t in trades if t.get("exit_time", "") >= cutoff_str]
    recent_pnl = sum(parse_pnl(t.get("pnl_usd", "0")) for t in recent_trades)
    total_pnl  = sum(parse_pnl(t.get("pnl_usd", "0")) for t in trades)
    wins       = sum(1 for t in trades if parse_pnl(t.get("pnl_usd", "0")) > 0)
    win_pct    = round(100 * wins / len(trades), 1) if trades else 0
    return {
        "today_pnl":   round(recent_pnl, 2),
        "today_trades": len(recent_trades),
        "total_pnl":   round(total_pnl, 2),
        "total_trades": len(trades),
        "win_pct":     win_pct,
    }


def read_state(path: str) -> dict | None:
    try:
        with open(path) as f:
            data = json.load(f)
        if not data:
            return None
        return data
    except Exception:
        return None


def read_log_tail(path: str, lines: int = 50) -> list[str]:
    try:
        with open(path, "r", errors="replace") as f:
            all_lines = f.readlines()
        return [l.rstrip() for l in all_lines[-lines:]]
    except Exception:
        return ["(log not found)"]


def equity_curve(trades: list[dict]) -> list[dict]:
    """Build cumulative P&L series for Chart.js."""
    sorted_trades = sorted(trades, key=lambda t: t.get("exit_time", ""))
    cumulative = 0.0
    points = []
    for t in sorted_trades:
        cumulative += parse_pnl(t.get("pnl_usd", "0"))
        points.append({
            "x": t.get("exit_time", "")[:10],
            "y": round(cumulative, 2),
        })
    return points

# ---------------------------------------------------------------------------
# Background auto-restart checker (called on each page load)
# ---------------------------------------------------------------------------

def check_auto_restarts():
    now = time.time()
    for bot_id, bot in BOTS.items():
        if not _auto_restart.get(bot_id):
            continue
        if is_running(bot["script"]):
            continue
        # Crashed — count recent crashes (last 1 hour)
        recent = [t for t in _crash_log[bot_id] if now - t < 3600]
        if len(recent) >= 3:
            continue  # circuit breaker: too many crashes, don't restart
        _crash_log[bot_id] = recent + [now]
        start_bot(bot_id)

# ---------------------------------------------------------------------------
# Routes — Pages
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    check_auto_restarts()
    return render_template("index.html")


@app.route("/bot/<bot_id>/start", methods=["POST"])
def bot_start(bot_id: str):
    if bot_id in BOTS:
        start_bot(bot_id)
    return redirect(url_for("index"))


@app.route("/bot/<bot_id>/stop", methods=["POST"])
def bot_stop(bot_id: str):
    if bot_id in BOTS:
        stop_bot(bot_id)
    return redirect(url_for("index"))


@app.route("/bot/<bot_id>/restart", methods=["POST"])
def bot_restart(bot_id: str):
    if bot_id in BOTS:
        stop_bot(bot_id)
        time.sleep(2)
        start_bot(bot_id)
    return redirect(url_for("index"))


@app.route("/bot/<bot_id>/autorestart", methods=["POST"])
def toggle_autorestart(bot_id: str):
    if bot_id in BOTS:
        _auto_restart[bot_id] = not _auto_restart.get(bot_id, True)
    return redirect(url_for("index"))

# ---------------------------------------------------------------------------
# Routes — API (used by HTMX partials + Grafana)
# ---------------------------------------------------------------------------

@app.route("/api/status")
def api_status():
    result = {}
    for bot_id, bot in BOTS.items():
        running = is_running(bot["script"])
        recent_crashes = [t for t in _crash_log[bot_id] if time.time() - t < 3600]
        circuit_open = len(recent_crashes) >= 3
        result[bot_id] = {
            "name":          bot["name"],
            "running":       running,
            "auto_restart":  _auto_restart.get(bot_id, True),
            "crash_count":   len(recent_crashes),
            "circuit_open":  circuit_open,
        }
    return jsonify(result)


@app.route("/api/pnl")
def api_pnl():
    result = {}
    grand_today = 0.0
    grand_total = 0.0
    for bot_id, bot in BOTS.items():
        trades = read_trades(bot["csv"])
        s = pnl_summary(trades)
        result[bot_id] = s
        grand_today += s["today_pnl"]
        grand_total += s["total_pnl"]
    result["combined"] = {
        "today_pnl": round(grand_today, 2),
        "total_pnl": round(grand_total, 2),
    }
    return jsonify(result)


@app.route("/api/positions")
def api_positions():
    positions = []
    for bot_id, bot in BOTS.items():
        for i, state_path in enumerate(bot["states"]):
            state = read_state(state_path)
            window = bot["windows"][i] if i < len(bot["windows"]) else f"Window {i+1}"
            if state and state.get("status") == "open":
                positions.append({
                    "bot":        bot["name"],
                    "bot_id":     bot_id,
                    "window":     window,
                    "spread_type": state.get("spread_type", ""),
                    "credit":     state.get("credit_received", 0),
                    "max_loss":   state.get("max_loss", 0),
                    "entry_time": state.get("entry_time", "")[:16],
                    "spot":       state.get("underlying_price_at_entry", 0),
                    "legs":       _parse_legs(state),
                })
            else:
                positions.append({
                    "bot":    bot["name"],
                    "bot_id": bot_id,
                    "window": window,
                    "empty":  True,
                })
    return jsonify(positions)


def _parse_legs(state: dict) -> list[dict]:
    legs = []
    for key in ["short_call", "long_call", "short_put", "long_put"]:
        leg = state.get(key, {})
        if leg and leg.get("instrument_name") != "STUB" and leg.get("quantity", 0) > 0:
            legs.append({
                "instrument": leg.get("instrument_name", ""),
                "side":       "SELL" if leg.get("side") in ("sell", "OrderSide.SELL") else "BUY",
                "strike":     leg.get("strike", 0),
                "type":       "C" if "call" in key else "P",
                "price":      leg.get("entry_price", 0),
            })
    return legs


@app.route("/api/trades")
def api_trades():
    all_trades = []
    for bot_id, bot in BOTS.items():
        for t in read_trades(bot["csv"]):
            t["bot_id"]   = bot_id
            t["bot_name"] = bot["name"]
            all_trades.append(t)
    all_trades.sort(key=lambda t: t.get("exit_time", ""), reverse=True)
    return jsonify(all_trades[:100])


@app.route("/api/logs/<bot_id>")
def api_logs(bot_id: str):
    if bot_id not in BOTS:
        return jsonify([])
    lines = read_log_tail(BOTS[bot_id]["log"], lines=60)
    return jsonify(lines)


@app.route("/api/chart")
def api_chart():
    """Equity curves per bot for Chart.js (includes SPX live + demo)."""
    datasets = []
    colors = {"eth_0dte": "#3b82f6", "eth_7dte": "#06b6d4", "btc_0dte": "#f59e0b"}
    for bot_id, bot in BOTS.items():
        if bot_id == "spx":
            spx = read_spx_trades()
            real = [t for t in spx if not t.get("is_demo")]
            demo = [t for t in spx if t.get("is_demo")]
            if real:
                datasets.append({
                    "label": "SPX Live", "data": equity_curve(real),
                    "borderColor": "#10b981", "backgroundColor": "transparent", "tension": 0.3,
                })
            if demo:
                datasets.append({
                    "label": "SPX Demo", "data": equity_curve(demo),
                    "borderColor": "#6b7280", "backgroundColor": "transparent", "tension": 0.3,
                    "borderDash": [5, 5],
                })
            continue
        trades = read_trades(bot["csv"])
        points = equity_curve(trades)
        if points:
            datasets.append({
                "label":           bot["name"],
                "data":            points,
                "borderColor":     colors.get(bot_id, "#adb5bd"),
                "backgroundColor": "transparent",
                "tension":         0.3,
            })
    return jsonify(datasets)


# ---------------------------------------------------------------------------
# HTMX Partials
# ---------------------------------------------------------------------------

@app.route("/partials/status")
def partial_status():
    cards = []
    for bot_id, bot in BOTS.items():
        if bot["script"] is None:
            # External tool (TAT) — show as always external, no start/stop
            cards.append({
                "id": bot_id, "name": bot["name"], "color": bot["color"],
                "running": None, "circuit": False, "auto": False, "crashes": 0,
                "capital": bot.get("capital", 0), "windows": bot.get("windows", []),
            })
            continue
        running = is_running(bot["script"])
        recent  = [t for t in _crash_log[bot_id] if time.time() - t < 3600]
        circuit = len(recent) >= 3
        auto    = _auto_restart.get(bot_id, True)
        cards.append({
            "id": bot_id, "name": bot["name"], "color": bot["color"],
            "running": running, "circuit": circuit, "auto": auto, "crashes": len(recent),
            "capital": bot.get("capital", 0), "windows": bot.get("windows", []),
        })
    return render_template("partials/status.html", cards=cards)


@app.route("/partials/pnl")
def partial_pnl():
    data = {}
    grand_today = grand_total = grand_capital = 0.0
    for bot_id, bot in BOTS.items():
        capital = bot.get("capital", 0.0)
        if bot_id == "spx":
            all_spx    = read_spx_trades()
            real_trades = [t for t in all_spx if not t.get("is_demo")]
            demo_trades = [t for t in all_spx if t.get("is_demo")]
            s_live = pnl_summary(real_trades)
            s_demo = pnl_summary(demo_trades)
            data["spx_live"] = {
                "name": "SPX 0 DTE", "capital": capital, "is_demo": False,
                "current_capital": round(capital + s_live["total_pnl"], 2), **s_live,
            }
            data["spx_demo"] = {
                "name": "SPX 0 DTE", "capital": capital, "is_demo": True,
                "current_capital": round(capital + s_demo["total_pnl"], 2), **s_demo,
            }
            grand_today   += s_live["today_pnl"]
            grand_total   += s_live["total_pnl"]
            grand_capital += capital
            continue
        trades = read_trades(bot["csv"])
        s      = pnl_summary(trades)
        data[bot_id] = {
            "name": bot["name"], "capital": capital, "is_demo": False,
            "current_capital": round(capital + s["total_pnl"], 2), **s,
        }
        grand_today   += s["today_pnl"]
        grand_total   += s["total_pnl"]
        grand_capital += capital
    return render_template("partials/pnl.html", data=data,
                           grand_today=round(grand_today, 2),
                           grand_total=round(grand_total, 2),
                           grand_capital=round(grand_capital, 2),
                           grand_current=round(grand_capital + grand_total, 2))


@app.route("/partials/positions")
def partial_positions():
    rows = []
    for bot_id, bot in BOTS.items():
        for i, state_path in enumerate(bot["states"]):
            state  = read_state(state_path)
            window = bot["windows"][i] if i < len(bot["windows"]) else f"Window {i+1}"
            if state and state.get("status") == "open":
                rows.append({
                    "bot": bot["name"], "color": bot["color"], "window": window,
                    "spread_type": state.get("spread_type", ""),
                    "credit": state.get("credit_received", 0),
                    "entry_time": state.get("entry_time", "")[:16],
                    "spot": state.get("underlying_price_at_entry", 0),
                    "legs": _parse_legs(state),
                    "empty": False,
                })
            else:
                rows.append({"bot": bot["name"], "color": bot["color"],
                             "window": window, "empty": True})
    return render_template("partials/positions.html", rows=rows)


@app.route("/partials/spx")
def partial_spx():
    spx_trades  = read_spx_trades()
    live_trades = [t for t in spx_trades if not t.get("is_demo")]
    demo_trades = [t for t in spx_trades if t.get("is_demo")]
    return render_template("partials/spx.html",
                           live=pnl_summary(live_trades),
                           demo=pnl_summary(demo_trades))


@app.route("/partials/trades")
def partial_trades():
    all_trades = []
    for bot_id, bot in BOTS.items():
        if bot_id == "spx":
            all_trades.extend(read_spx_trades())
        else:
            for t in read_trades(bot["csv"]):
                t["bot_name"] = bot["name"]
                t["color"]    = bot["color"]
                t["is_demo"]  = False
                all_trades.append(t)
    all_trades.sort(key=lambda t: t.get("exit_time", ""), reverse=True)
    return render_template("partials/trades.html", trades=all_trades[:100])


# ---------------------------------------------------------------------------
# Grafana Simple JSON datasource endpoints
# ---------------------------------------------------------------------------

@app.route("/grafana/")
def grafana_health():
    return "OK", 200


@app.route("/grafana/search", methods=["POST"])
def grafana_search():
    metrics = [
        "eth_0dte_pnl", "eth_7dte_pnl", "btc_0dte_pnl", "combined_pnl",
        "eth_0dte_trades", "btc_0dte_trades",
    ]
    return jsonify(metrics)


@app.route("/grafana/query", methods=["POST"])
def grafana_query():
    body    = request.get_json(force=True) or {}
    targets = body.get("targets", [])
    results = []

    all_trades: dict[str, list] = {}
    for bot_id, bot in BOTS.items():
        all_trades[bot_id] = read_trades(bot["csv"])

    for target in targets:
        metric = target.get("target", "")
        datapoints = []

        if metric.endswith("_pnl"):
            bot_id = metric.replace("_pnl", "")
            trades = all_trades.get(bot_id, [])
            if bot_id == "combined":
                trades = [t for ts in all_trades.values() for t in ts]
            cumulative = 0.0
            for t in sorted(trades, key=lambda x: x.get("exit_time", "")):
                cumulative += parse_pnl(t.get("pnl_usd", "0"))
                try:
                    ts_ms = int(datetime.fromisoformat(
                        t["exit_time"].replace("Z", "+00:00")
                    ).timestamp() * 1000)
                    datapoints.append([round(cumulative, 2), ts_ms])
                except Exception:
                    pass

        elif metric.endswith("_trades"):
            bot_id = metric.replace("_trades", "")
            trades = all_trades.get(bot_id, [])
            by_day: dict[str, float] = defaultdict(float)
            for t in trades:
                day = t.get("exit_time", "")[:10]
                by_day[day] += parse_pnl(t.get("pnl_usd", "0"))
            for day, pnl in sorted(by_day.items()):
                try:
                    ts_ms = int(datetime.fromisoformat(day).timestamp() * 1000)
                    datapoints.append([round(pnl, 2), ts_ms])
                except Exception:
                    pass

        results.append({"target": metric, "datapoints": datapoints})

    return jsonify(results)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 50)
    print("  Trading Bot Dashboard")
    print("  http://0.0.0.0:5000")
    print("=" * 50)
    app.run(host="0.0.0.0", port=5000, debug=False)
