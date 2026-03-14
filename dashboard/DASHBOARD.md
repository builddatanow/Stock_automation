# Trading Bot Dashboard

## Overview

Flask web dashboard for monitoring ETH, BTC, and SPX trading bots in real time, with a full backtests results page and QuantConnect integration.

- **URL**: http://localhost:5000 (or http://45.40.97.163:5000 externally)
- **Server**: Waitress (production WSGI) or Flask dev server (via watchdog)
- **Auth**: Login required — credentials in `app.py` (`admin` / `trade2026`)
- **Auto-refresh**: HTMX partials polling every 5–15s; equity chart every 30s

---

## Running the Dashboard

### Normal Start (via Watchdog)
The watchdog (`watchdog.py`) automatically restarts the dashboard if it crashes.
It uses `py app.py` from `C:\Users\Administrator\Desktop\projects\dashboard\`.

### Manual Restart
```bash
cd C:\Users\Administrator\Desktop\projects\dashboard
"C:\Program Files\Python311\python.exe" restart_dashboard.py
```

Or directly via waitress:
```bash
"C:\Program Files\Python311\python.exe" -m waitress --host=127.0.0.1 --port=5000 --threads=4 app:app
```

### Logs
```
C:\Users\Administrator\Desktop\projects\dashboard\dashboard.log
```

---

## Architecture

### File Structure
```
dashboard/
├── app.py                      # Main Flask app — all routes, data logic, QC integration
├── backtests.json              # Persisted backtest results (pre-loaded + QC runs)
├── restart_dashboard.py        # Kill + restart waitress helper
├── start_dashboard.bat         # Windows batch launcher
├── DASHBOARD.md                # This file
├── templates/
│   ├── index.html              # Main dashboard — chart tabs, HTMX, Chart.js
│   ├── login.html              # Login page
│   ├── backtests.html          # Backtests page — list + detail panel + run modal
│   └── partials/
│       ├── status.html         # Bot status cards
│       ├── pnl.html            # P&L summary table
│       ├── spx.html            # SPX live account stats
│       ├── positions.html      # Open positions table
│       └── trades.html         # Trade history table
└── dashboard.log               # Server output log
```

### Key Config in app.py
```python
ETH_BOT_DIR  = r"C:\Users\Administrator\Desktop\projects\eth-options-bot"
TAT_DIR      = r"C:\Users\Administrator\AppData\Local\Packages\TradeAutomationToolbox_...\LocalState"
SPX_START_DATE = "2026-01-01"          # Only show SPX trades from this date
app.secret_key = "tbd-dashboard-s3cr3t-2026"
app.config['TEMPLATES_AUTO_RELOAD'] = True   # Reload templates without restart
DASHBOARD_USER     = "admin"
DASHBOARD_PASSWORD = "trade2026"

# QuantConnect integration
QC_USER_ID    = "426855"
QC_PROJECT_ID = 28932760
QC_STRATEGY_DIR = r"C:\Users\Administrator\Desktop\projects\spx-leaps-qc"
BACKTESTS_JSON  = os.path.join(BASE_DIR, "backtests.json")
```

---

## Authentication

All routes are protected by `@login_required`. Login state is stored in a Flask session (expires on browser close).

- **Login page**: `GET/POST /login`
- **Logout**: `GET /logout`
- **Credentials**: `admin` / `trade2026` (set in `app.py`)

The login page (`templates/login.html`) uses a dark theme matching the dashboard style.

---

## Bots Managed

| Bot ID      | Name        | Script                 | Capital    | Mode              |
|-------------|-------------|------------------------|------------|-------------------|
| `eth_0dte`  | ETH 0 DTE   | `run_live_0dte.py`     | $6,600     | DEMO (testnet)    |
| `eth_7dte`  | ETH 7 DTE   | `run_live.py`          | $2,200     | DEMO (testnet)    |
| `btc_0dte`  | BTC 0 DTE   | `run_live_btc_0dte.py` | $15,000    | DEMO (testnet)    |
| `spx`       | SPX 0 DTE   | External (TAT)         | $10,000    | LIVE (real money) |

**ETH and BTC** — Deribit testnet. Shown with **DEMO** badge, dashed lines on equity chart, excluded from portfolio totals.

**SPX** — TradeAutomationToolbox (TAT). Shown as **External/TAT**, live trades only.

---

## Data Sources

### ETH / BTC Bots
- Trade CSVs: `eth-options-bot/data/live_*_trades.csv`
- State files: `eth-options-bot/data/live_*_state.json`
- Logs: `eth-options-bot/logs/live_*.log`

### SPX Bot (TAT)
- TAT export CSVs: `export-YYYY-MM-DD.csv` files in `TAT_DIR`
- Only `export-*.csv` files are read (`exporttest-*` paper trade files ignored)
- Trades before `2026-01-01` are filtered out

---

## Dashboard Sections

### 1. Bot Status Cards
- Running / stopped / circuit-open status for each bot
- ETH/BTC: Start / Stop / Restart buttons
- SPX: Shows as "External / TAT" — no start/stop (managed by TAT)
- Circuit breaker fires if 3+ crashes in the last hour

### 2. P&L Summary Table
- One row per bot
- ETH/BTC rows show **DEMO** pill, excluded from portfolio totals
- SPX shows **LIVE** pill, counts toward portfolio total
- Portfolio Total footer = SPX live only

### 3. Equity Curve (Chart.js — Bar + Line Combo)
- **Bot tabs**: SPX Live | ETH 0DTE | ETH 7DTE | BTC 0DTE
- **Bars**: daily P&L (green positive, red negative) on left y-axis
- **Line**: cumulative equity on right y-axis (straight segments, `tension: 0`)
- Daily aggregation — one data point per trading day (no intraday zigzag)
- ETH/BTC labeled as `(Demo)` with dashed bar borders
- Refreshes every 30 seconds via `/api/chart_daily`

### 4. Ticker Filter Tabs (navbar)
- **All** / **ETH** / **BTC** / **SPX** — filters P&L table and trades
- SPX tab auto-expands the SPX Results section

### 5. SPX 0 DTE Results
- Visible when SPX tab is active
- Live account stats: All-Time P&L, 24h P&L, Win Rate, Trades
- Source: TAT export CSVs, live trades only

### 6. Open Positions
- Reads state JSON files for ETH/BTC bots
- Spread type, credit received, entry time, spot price, individual legs

### 7. Trade History
- Last 100 trades across all bots
- Filter: All / ETH / BTC / SPX
- Shows: date/time, bot, account (LIVE/DEMO), spread type, credit, P&L, status

### 8. Live Logs
- Last 60 lines from bot log files
- Tabs: ETH 0 DTE / ETH 7 DTE / BTC 0 DTE
- Auto-refreshes every 5s; color-coded by level

---

## Backtests Page (`/backtests`)

### Overview
A dedicated page for viewing and running QuantConnect backtests. All results are stored in `backtests.json`.

### Layout
- **Left panel**: scrollable list of all backtests grouped by strategy (SPX / QQQ / MAG7)
  - Shows name, CAGR badge, risk per trade, drawdown, run date
- **Right panel**: detail view for the selected backtest
  - Strategy description, param pills (DTE, start year)
  - 4 stat boxes: CAGR, Max Drawdown, Sharpe Ratio, End Equity
  - Inline stats: Win Rate, Total Trades, Risk/Trade (amber), Initial Capital, Run Date
  - Yearly returns bar chart (Chart.js)
  - QuantConnect backtest ID link (if run via QC)
- **Run New button**: opens modal to configure and launch a new QC backtest

### Running a New Backtest
1. Click **Run New** (green highlighted button in top-right of list panel)
2. Select strategy (SPX / QQQ / Mag7), set risk %, DTE, start year, optional name
3. Progress steps shown: Uploading → Compiling → Running → Completed
4. On completion, result is saved to `backtests.json` and appears in the list

The run happens in a background thread — the modal polls `/api/backtests/status/<run_id>` every 10 seconds.

### backtests.json Schema
```json
{
  "id":          "unique_id",
  "name":        "Human-readable name",
  "strategy":    "spx | qqq | mag7",
  "file":        "strategy_filename.py",
  "description": "What this backtest tests",
  "params": {
    "risk":       10,      // % risk per trade
    "dte":        300,     // days to expiry
    "start_year": 2010,    // backtest start year
    "capital":    100000   // initial capital ($)
  },
  "qc_id":    "quantconnect_backtest_id or null",
  "run_date": "2026-03-13",
  "status":   "completed",
  "results": {
    "cagr":       "45.6%",
    "drawdown":   "63.7%",
    "sharpe":     "1.049",
    "end_equity": 40301044,
    "win_rate":   "65%",
    "trades":     997,
    "yearly":     { "2010": 12.4, "2011": 7.1, ... }
  }
}
```

---

## Backtest Results Summary

All results use $100,000 initial capital.

### SPX LEAPS Strategies (2010–2025)

| Strategy | Risk/Trade | CAGR | Max DD | Sharpe | End Equity |
|----------|-----------|------|--------|--------|------------|
| No Put | 30% | 42.11% | 27.76% | 0.744 | $29.5M |
| Conditional Put | 30% | 40.70% | 27.30% | 0.770 | $25.8M |
| Always Put | 30% | 39.22% | 27.30% | 0.773 | $21.8M |

### SPX Portfolio Strategies (2000–2025)

| Strategy | Risk/Trade | CAGR | Max DD | Sharpe | End Equity |
|----------|-----------|------|--------|--------|------------|
| SPY75+TLT15+LEAPS10 | 17% | 18.2% | 51.9% | 0.56 | $7.7M |
| SPY75+TLT15+LEAPS+DD Put 10% | 10% | 17.2% | 42.7% | 0.548 | $6.2M |
| DD Put, Risk 15% | 15% | 21.5% | 50.0% | 0.591 | $15.7M |
| DD Put, DTE 350 | 10% | 14.4% | 42.7% | 0.464 | $3.3M |
| SPY75+TLT15+LEAPS10 (QC Live Run) | 17% | 23.0% | 54.1% | 0.605 | $21.6M |

### QQQ LEAPS Strategies (2010–2025)

| Strategy | Risk/Trade | CAGR | Max DD | Sharpe | End Equity |
|----------|-----------|------|--------|--------|------------|
| QQQ LEAPS Base | 10% | 13.6% | 64.9% | 0.40 | $2.7M |
| QQQ Tuned (crash -2.5% + DD put 15%) | 10% | 11.6% | 75.4% | 0.35 | $1.7M |

### Mag7 LEAPS Strategies (2010–2025)

| Strategy | Risk/Trade | CAGR | Max DD | Sharpe | End Equity |
|----------|-----------|------|--------|--------|------------|
| Top2 Momentum | 10% | 44.9% | 63.7% | 1.031 | $37.3M |
| **+ SPY Crash Call** | **10%** | **45.6%** | **63.7%** | **1.049** | **$40.3M** |

**Best strategy**: Mag7 LEAPS + SPY Crash Call — 45.6% CAGR, Sharpe 1.049, $40.3M end equity.

The crash call fires when SPY drops 25% from its 52-week high — sells 15% SPY core and buys a 300 DTE SPY LEAPS call. Notably boosted 2020 COVID recovery (+105.9%).

---

## QuantConnect Integration

Backtests run on QuantConnect LEAN cloud via REST API.

### Auth
HMAC SHA256: `SHA256(token:timestamp)` sent as password with timestamp header.

### Flow
1. `POST /api/v2/files/update` — upload patched strategy file as `main.py`
2. `POST /api/v2/compile/create` → poll `GET /api/v2/compile/read` until `BuildSuccess`
3. `POST /api/v2/backtests/create` → get `backtestId`
4. Poll `GET /api/v2/backtests/read` every 30s until `completed = true`
5. Parse `statistics` + `rollingWindow` (M12_YYYY keys → yearly returns)

### Strategy Files (in `spx-leaps-qc/`)
| File | Strategy |
|------|----------|
| `spx_leaps_baseline_profit_investment.py` | SPX LEAPS |
| `qqq_leaps_strategy.py` | QQQ LEAPS |
| `mag7_leaps_strategy.py` | Mag7 LEAPS + Crash Call |
| `run_mag7_backtest.py` | Standalone CLI runner for Mag7 |

### Param Patching
When a run is triggered from the dashboard, the strategy file is patched in-memory before upload:
- `LEAPS_SLEEVE_MAX` → `risk / 100`
- `CALL_DTE` → selected DTE
- `SetStartDate(...)` → selected start year

---

## API Endpoints

### Dashboard
| Endpoint | Description |
|----------|-------------|
| `GET /` | Main dashboard page (login required) |
| `GET /login` | Login page |
| `POST /login` | Authenticate |
| `GET /logout` | End session |
| `GET /api/status` | JSON — bot running/crash status |
| `GET /api/pnl` | JSON — P&L summary per bot |
| `GET /api/chart` | JSON — legacy equity curve datasets |
| `GET /api/chart_daily` | JSON — daily bar+line chart data per bot |
| `GET /api/trades` | JSON — last 100 trades |
| `GET /api/logs/<bot_id>` | JSON — last 60 log lines |
| `GET /partials/status` | HTMX — bot status cards HTML |
| `GET /partials/pnl` | HTMX — P&L table HTML |
| `GET /partials/spx` | HTMX — SPX results HTML |
| `GET /partials/positions` | HTMX — open positions HTML |
| `GET /partials/trades` | HTMX — trade history rows HTML |
| `POST /bot/<id>/start` | Start a bot |
| `POST /bot/<id>/stop` | Stop a bot |
| `POST /bot/<id>/restart` | Restart a bot |

### Backtests
| Endpoint | Description |
|----------|-------------|
| `GET /backtests` | Backtests page |
| `GET /api/backtests` | JSON — all backtests from `backtests.json` |
| `POST /api/backtests/run` | Launch new QC backtest (background thread) |
| `GET /api/backtests/status/<run_id>` | Poll in-progress backtest status |
| `POST /api/backtests/delete/<bt_id>` | Delete a backtest from `backtests.json` |

---

## Watchdog Integration

The watchdog (`C:\Users\Administrator\Desktop\projects\watchdog.py`) manages the dashboard as an always-on job:

```python
{
    "name":   "dashboard",
    "script": "app.py",
    "cwd":    r"C:\Users\Administrator\Desktop\projects\dashboard",
    "log":    r"C:\Users\Administrator\Desktop\projects\dashboard\dashboard.log",
}
```

**Note**: Watchdog detects the dashboard via `is_running("app.py")`. If launched via `waitress` (cmdline shows `waitress` not `app.py`), the watchdog won't detect it and may start a second instance. Prefer `py app.py` for watchdog-managed instances, or use `restart_dashboard.py` for manual waitress restarts.

---

## Known Issues / Notes

- **Template caching**: `TEMPLATES_AUTO_RELOAD = True` — template changes take effect immediately without restart. Only `app.py` changes require a restart.
- **Watchdog vs waitress**: Watchdog starts via `py app.py` (Flask dev server); `restart_dashboard.py` uses waitress. Both work but watchdog won't auto-detect the waitress process.
- **SPX demo**: TAT also exports `exporttest-*.csv` (paper trades) — these are ignored. SPX dashboard is live-only.
- **ETH/BTC demo**: Both bots run on Deribit testnet. Trades appear as DEMO but are real bot executions on testnet.
- **Equity chart**: Uses `/api/chart_daily` — daily aggregated P&L. Per-trade data via `/api/chart` (legacy, not used by UI).
- **Port**: Dashboard binds to `127.0.0.1:5000` internally; externally accessible via server IP on port 5000.
