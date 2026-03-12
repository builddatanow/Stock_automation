"""
Watchdog — Scheduled Bot Manager
==================================
Schedule:
  - discord_bot.py   : 24/7 (always running)
  - monitor.py       : 24/7 (always running)
  - run_live_0dte.py : Start at 1:30 PM Sydney, stop at 08:10 UTC (after 0DTE expiry)
  - run_live.py      : Start at 1:30 PM Sydney, stop at 08:10 UTC

Sydney timezone: AEDT (UTC+11) in summer / AEST (UTC+10) in winter
1:30 PM AEDT = 02:30 UTC  (Oct–Apr)
1:30 PM AEST = 03:30 UTC  (Apr–Oct)

0DTE expiry: 08:00 UTC daily → bots stop at 08:10 UTC
"""

import json
import os
import csv
import subprocess
import time
import requests
import psutil
from datetime import datetime, timezone, timedelta

# ---------------------------------------------------------------------------
# Always-on jobs (24/7)
# ---------------------------------------------------------------------------
ALWAYS_ON_JOBS = [
    {
        "name":   "discord_bot",
        "script": "discord_bot.py",
        "cwd":    r"C:\Users\Administrator\Desktop\projects\substack-monitor",
        "log":    r"C:\Users\Administrator\Desktop\projects\substack-monitor\discord_bot.log",
    },
    {
        "name":   "substack_monitor",
        "script": "substack_monitor.py",
        "cwd":    r"C:\Users\Administrator\Desktop\projects\substack-monitor",
        "log":    r"C:\Users\Administrator\Desktop\projects\substack-monitor\substack_monitor.log",
    },
    {
        "name":   "dashboard",
        "script": "app.py",
        "cwd":    r"C:\Users\Administrator\Desktop\projects\dashboard",
        "log":    r"C:\Users\Administrator\Desktop\projects\dashboard\dashboard.log",
    },
]

# ---------------------------------------------------------------------------
# Scheduled jobs (start at 1:30 PM Sydney, stop at 08:10 UTC)
# ---------------------------------------------------------------------------
ETH_BOT_DIR = r"C:\Users\Administrator\Desktop\projects\eth-options-bot"

SCHEDULED_JOBS = [
    {
        "name":   "eth_0dte_bot",
        "script": "run_live_0dte.py",
        "cwd":    ETH_BOT_DIR,
        "log":    r"C:\Users\Administrator\Desktop\projects\eth-options-bot\logs\live_0dte.log",
    },
    {
        "name":   "eth_7dte_bot",
        "script": "run_live.py",
        "cwd":    ETH_BOT_DIR,
        "log":    r"C:\Users\Administrator\Desktop\projects\eth-options-bot\logs\live.log",
    },
    {
        "name":   "btc_0dte_bot",
        "script": "run_live_btc_0dte.py",
        "cwd":    ETH_BOT_DIR,
        "log":    r"C:\Users\Administrator\Desktop\projects\eth-options-bot\logs\live_btc_0dte.log",
    },
]

CHECK_INTERVAL   = 60       # check every 60 seconds
START_UTC_HOUR   = 2        # 1:30 PM AEDT = 02:30 UTC (adjust to 3 for AEST)
START_UTC_MINUTE = 30
STOP_UTC_HOUR    = 8        # 0DTE expiry 08:00 UTC
STOP_UTC_MINUTE  = 10       # stop at 08:10 to allow expiry processing

TRADES_0DTE     = os.path.join(ETH_BOT_DIR, "data", "live_0dte_trades.csv")
TRADES_7DTE     = os.path.join(ETH_BOT_DIR, "data", "live_trades.csv")
TRADES_BTC_0DTE = os.path.join(ETH_BOT_DIR, "data", "live_btc_0dte_trades.csv")

IBKR_REVIEW_SCRIPT = r"C:\Users\Administrator\Desktop\projects\ibkr_portfolio_review.py"
IBKR_REVIEW_LOG    = r"C:\Users\Administrator\Desktop\projects\ibkr_portfolio_review.log"
IBKR_REVIEW_UTC_HOUR   = 21   # 4:30 PM ET = 21:30 UTC
IBKR_REVIEW_UTC_MINUTE = 30

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
ETH_BOT_DISCORD_WEBHOOK = "https://discord.com/api/webhooks/1480698774736076820/9i3IVBM0ik3TBcHLenoAeTyx1E-yDvC7IE8tmnlzsCZcYo64aPUBjk6qX35fq73Yw9Dv"

def load_discord_webhook():
    return ETH_BOT_DISCORD_WEBHOOK

def send_discord(msg: str):
    webhook = load_discord_webhook()
    if not webhook:
        return
    try:
        requests.post(webhook, json={"content": msg}, timeout=10)
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Process helpers
# ---------------------------------------------------------------------------
def is_running(script_name: str) -> bool:
    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            if proc.info["name"] and "python" in proc.info["name"].lower():
                cmdline = " ".join(proc.info["cmdline"] or [])
                if script_name in cmdline:
                    return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return False


def start_job(job: dict) -> bool:
    try:
        with open(job["log"], "a") as log_file:
            subprocess.Popen(
                ["py", job["script"]],
                cwd=job["cwd"],
                stdout=log_file,
                stderr=log_file,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        time.sleep(3)
        return is_running(job["script"])
    except Exception as e:
        print(f"  ERROR starting {job['name']}: {e}")
        return False


def stop_job(script_name: str):
    for proc in psutil.process_iter(["name", "cmdline", "pid"]):
        try:
            if proc.info["name"] and "python" in proc.info["name"].lower():
                cmdline = " ".join(proc.info["cmdline"] or [])
                if script_name in cmdline:
                    proc.terminate()
                    print(f"  Stopped PID {proc.info['pid']} ({script_name})")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

# ---------------------------------------------------------------------------
# P&L summary
# ---------------------------------------------------------------------------
def _parse_pnl(row: dict) -> float:
    return float(row.get("pnl_usd", "0").replace("$", "").replace("+", ""))


def read_todays_pnl(csv_path: str) -> dict:
    """Read today's closed trades from CSV and sum up P&L."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    trades = []
    try:
        with open(csv_path, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("exit_time", "").startswith(today):
                    trades.append(row)
    except FileNotFoundError:
        return {"trades": 0, "pnl_usd": 0.0, "wins": 0, "losses": 0}

    total_pnl = sum(_parse_pnl(r) for r in trades)
    wins      = sum(1 for r in trades if _parse_pnl(r) > 0)
    losses    = len(trades) - wins
    return {"trades": len(trades), "pnl_usd": total_pnl, "wins": wins, "losses": losses}


def read_alltime_pnl(csv_path: str) -> dict:
    """Read all closed trades from CSV and sum up P&L."""
    trades = []
    try:
        with open(csv_path, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                trades.append(row)
    except FileNotFoundError:
        return {"trades": 0, "pnl_usd": 0.0, "wins": 0, "losses": 0}

    total_pnl = sum(_parse_pnl(r) for r in trades)
    wins      = sum(1 for r in trades if _parse_pnl(r) > 0)
    losses    = len(trades) - wins
    return {"trades": len(trades), "pnl_usd": total_pnl, "wins": wins, "losses": losses}


def send_pnl_summary(stopped_at: str):
    # Today
    pnl_0dte     = read_todays_pnl(TRADES_0DTE)
    pnl_7dte     = read_todays_pnl(TRADES_7DTE)
    pnl_btc_0dte = read_todays_pnl(TRADES_BTC_0DTE)

    today_total = pnl_0dte["pnl_usd"] + pnl_7dte["pnl_usd"] + pnl_btc_0dte["pnl_usd"]

    # All-time
    all_0dte     = read_alltime_pnl(TRADES_0DTE)
    all_7dte     = read_alltime_pnl(TRADES_7DTE)
    all_btc_0dte = read_alltime_pnl(TRADES_BTC_0DTE)

    alltime_total = all_0dte["pnl_usd"] + all_7dte["pnl_usd"] + all_btc_0dte["pnl_usd"]

    def win_pct(d):
        return f"{100 * d['wins'] / d['trades']:.0f}%" if d["trades"] else "N/A"

    lines = [
        f"**Bot Session Ended** | {stopped_at}",
        f"**Today's P&L: {today_total:+.2f}**",
        "",
        "**ETH 0 DTE:** "
        f"Trades: {pnl_0dte['trades']} | W/L: {pnl_0dte['wins']}/{pnl_0dte['losses']} | P&L: {pnl_0dte['pnl_usd']:+.2f}",
        "**ETH 7 DTE:** "
        f"Trades: {pnl_7dte['trades']} | W/L: {pnl_7dte['wins']}/{pnl_7dte['losses']} | P&L: {pnl_7dte['pnl_usd']:+.2f}",
        "**BTC 0 DTE:** "
        f"Trades: {pnl_btc_0dte['trades']} | W/L: {pnl_btc_0dte['wins']}/{pnl_btc_0dte['losses']} | P&L: {pnl_btc_0dte['pnl_usd']:+.2f}",
    ]
    if pnl_0dte["trades"] == 0 and pnl_7dte["trades"] == 0 and pnl_btc_0dte["trades"] == 0:
        lines.append("  *(No trades closed today)*")

    lines += [
        "",
        "**All-Time Totals**",
        f"**Combined: {alltime_total:+.2f}**",
        f"ETH 0 DTE: {all_0dte['trades']} trades | {win_pct(all_0dte)} win | {all_0dte['pnl_usd']:+.2f}",
        f"ETH 7 DTE: {all_7dte['trades']} trades | {win_pct(all_7dte)} win | {all_7dte['pnl_usd']:+.2f}",
        f"BTC 0 DTE: {all_btc_0dte['trades']} trades | {win_pct(all_btc_0dte)} win | {all_btc_0dte['pnl_usd']:+.2f}",
    ]

    send_discord("\n".join(lines))
    print("\n".join(lines))

# ---------------------------------------------------------------------------
# Scheduling helpers
# ---------------------------------------------------------------------------
def sydney_offset_hours() -> int:
    """Returns Sydney UTC offset: +11 (AEDT Oct-Apr) or +10 (AEST Apr-Oct)."""
    now = datetime.now(timezone.utc)
    # AEDT: last Sun Oct → first Sun Apr
    # Simple approximation: month 4-10 = AEST (+10), else AEDT (+11)
    if 4 <= now.month <= 9:
        return 10
    return 11


def is_start_window(now: datetime) -> bool:
    """True if current UTC time is within 1 minute of the scheduled start."""
    return now.hour == START_UTC_HOUR and now.minute == START_UTC_MINUTE


def is_stop_time(now: datetime) -> bool:
    """True if current UTC time is at or past the stop time."""
    return now.hour == STOP_UTC_HOUR and now.minute >= STOP_UTC_MINUTE


def bots_should_be_running(now: datetime) -> bool:
    """True if current time is in the active window [start_time, stop_time)."""
    start = now.replace(hour=START_UTC_HOUR, minute=START_UTC_MINUTE, second=0, microsecond=0)
    stop  = now.replace(hour=STOP_UTC_HOUR,  minute=STOP_UTC_MINUTE,  second=0, microsecond=0)
    if stop < start:  # stop is next day (shouldn't happen with these hours)
        stop += timedelta(days=1)
    return start <= now < stop

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def main():
    print("=" * 50)
    print("Watchdog started")
    offset = sydney_offset_hours()
    print(f"Sydney offset: UTC+{offset}")
    print(f"ETH bots start: {START_UTC_HOUR:02d}:{START_UTC_MINUTE:02d} UTC (1:30 PM Sydney)")
    print(f"ETH bots stop : {STOP_UTC_HOUR:02d}:{STOP_UTC_MINUTE:02d} UTC (after 0DTE expiry)")
    print("=" * 50)

    send_discord(
        f"**Watchdog started**\n"
        f"ETH bots schedule: start `{START_UTC_HOUR:02d}:{START_UTC_MINUTE:02d} UTC` | "
        f"stop `{STOP_UTC_HOUR:02d}:{STOP_UTC_MINUTE:02d} UTC`\n"
        f"24/7: discord_bot, substack_monitor"
    )

    bots_started_today   = False
    bots_stopped_today   = False
    ibkr_review_done_today = False
    last_date = None

    while True:
        now  = datetime.now(timezone.utc)
        date = now.date()

        # Reset daily flags at midnight UTC
        if date != last_date:
            bots_started_today     = False
            bots_stopped_today     = False
            ibkr_review_done_today = False
            last_date = date

        # ── Always-on jobs ──────────────────────────────────────────────
        for job in ALWAYS_ON_JOBS:
            if not is_running(job["script"]):
                print(f"[{now.strftime('%H:%M')}] {job['name']} DOWN — restarting...")
                ok = start_job(job)
                if ok:
                    send_discord(f"**Watchdog** Restarted `{job['name']}`")
                else:
                    send_discord(f"**Watchdog WARNING** Failed to restart `{job['name']}`")

        # ── Scheduled ETH bots ─────────────────────────────────────────
        in_window = bots_should_be_running(now)

        # Start at 1:30 PM Sydney
        if in_window and not bots_started_today:
            print(f"[{now.strftime('%H:%M')}] Starting ETH bots (1:30 PM Sydney window)")
            started = []
            for job in SCHEDULED_JOBS:
                if not is_running(job["script"]):
                    ok = start_job(job)
                    if ok:
                        started.append(job["name"])
                        print(f"  {job['name']} started OK")
                    else:
                        print(f"  {job['name']} FAILED to start")
                else:
                    started.append(job["name"] + " (already running)")
            bots_started_today = True
            send_discord(
                f"**ETH Bots Started** | {now.strftime('%H:%M UTC')}\n"
                + "\n".join(f"  `{j}`" for j in started)
            )

        # Stop at 08:10 UTC (after 0DTE expiry)
        if not in_window and bots_started_today and not bots_stopped_today and now.hour == STOP_UTC_HOUR:
            print(f"[{now.strftime('%H:%M')}] Stopping ETH bots (0DTE expiry passed)")
            for job in SCHEDULED_JOBS:
                stop_job(job["script"])
            bots_stopped_today = True
            stopped_at = now.strftime("%Y-%m-%d %H:%M UTC")
            time.sleep(5)
            send_pnl_summary(stopped_at)

        # Watchdog for scheduled bots — restart if they crash during window
        if in_window and bots_started_today:
            for job in SCHEDULED_JOBS:
                if not is_running(job["script"]):
                    print(f"[{now.strftime('%H:%M')}] {job['name']} crashed — restarting...")
                    ok = start_job(job)
                    msg = f"**Watchdog** Restarted `{job['name']}`" if ok else f"**Watchdog WARNING** Failed to restart `{job['name']}`"
                    send_discord(msg)

        # ── IBKR portfolio review at 21:30 UTC (4:30 PM ET, Mon-Fri) ───────
        is_weekday = now.weekday() < 5  # 0=Mon, 4=Fri
        if (is_weekday
                and now.hour == IBKR_REVIEW_UTC_HOUR
                and now.minute == IBKR_REVIEW_UTC_MINUTE
                and not ibkr_review_done_today):
            print(f"[{now.strftime('%H:%M')}] Running IBKR portfolio review...")
            ibkr_review_done_today = True
            try:
                with open(IBKR_REVIEW_LOG, "a") as log_file:
                    subprocess.Popen(
                        ["py", IBKR_REVIEW_SCRIPT],
                        cwd=r"C:\Users\Administrator\Desktop\projects",
                        stdout=log_file,
                        stderr=log_file,
                        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                    )
            except Exception as e:
                send_discord(f"**Watchdog WARNING** Failed to run IBKR review: {e}")

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
