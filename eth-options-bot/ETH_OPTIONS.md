# ETH Options Trading Bot — Project Notes

## Overview

Automated ETH options trading system for Deribit using an EMA(9/21) hybrid strategy:
- **Bullish signal** → Bull Put Spread (sell OTM put, buy further OTM put)
- **Bearish signal** → Bear Call Spread (sell OTM call, buy further OTM call)
- **Neutral / low IV** → Iron Condor fallback

Deployed on Deribit testnet. Backtested on 2 years of real Deribit market data.
**Active strategy: 7 DTE** (best backtest results: +58% / 2 years, 81.8% win rate).

---

## Project Structure

```
eth-options-bot/
├── config/
│   ├── config.yaml               # API keys, strategy params, risk config
│   └── settings.py               # Pydantic config classes + load_config()
├── src/
│   ├── data/
│   │   ├── models.py             # OptionQuote, IronCondor, Order, etc.
│   │   ├── ingestion.py          # DataIngestionService (fetch live chain)
│   │   └── storage.py            # SQLite + Parquet storage
│   ├── deribit/
│   │   ├── rest_client.py        # DeribitRESTClient (REST API wrapper)
│   │   └── ws_client.py          # WebSocket client (used by WSPositionMonitor)
│   ├── strategy/
│   │   ├── ema_spread.py         # EMASpreadConfig, get_ema_signal, select_spread_strikes, build_spread
│   │   └── weekly_iron_condor.py # select_strikes, build_condor, check_exit_conditions
│   ├── backtest/
│   │   ├── engine.py             # Original IC backtest engine
│   │   └── ema_backtest.py       # EMA hybrid backtest engine (supports 0/3/7 DTE)
│   ├── execution/
│   │   ├── broker_interface.py   # Abstract BrokerInterface
│   │   ├── simulated_broker.py   # Paper trading with fees + sim date fix
│   │   └── deribit_broker.py     # Live Deribit broker
│   ├── risk/
│   │   └── risk_manager.py       # RiskManager, RiskViolation
│   └── monitoring/
│       ├── logger.py             # setup_logging()
│       ├── notifier.py           # Telegram/Slack alerts
│       └── position_monitor.py   # WSPositionMonitor (real-time TP/SL via WebSocket)
├── run_live.py                   # Live trading bot (7 DTE, any weekday)   <-- MAIN BOT
├── run_7dte_backtest.py          # 7 DTE backtest (best performer)          <-- MAIN BACKTEST
├── run_3dte_backtest.py          # 3 DTE backtest
├── run_0dte_backtest.py          # 0 DTE backtest
├── run_deribit_backtest.py       # Original weekly (5-10 DTE) backtest
├── test_open_position.py         # One-shot test: open single position
├── analyze_0dte.py               # Win rate breakdown by strategy type (all DTE)
├── check_7dte_gaps.py            # Verify continuous trading (gap analysis)
├── data/
│   ├── live_state.json           # Active position state (written by bot)
│   ├── live_trades.csv           # Closed trade log
│   ├── 7dte_backtest/
│   │   └── trade_history.csv     # 7 DTE backtest trade log
│   ├── 3dte_backtest/
│   │   └── trade_history.csv     # 3 DTE backtest trade log
│   └── 0dte_backtest/
│       └── trade_history.csv     # 0 DTE backtest trade log
└── logs/
    ├── live.log                  # Live bot log
    ├── 7dte_bt.log               # 7 DTE backtest log
    └── test_open.log             # One-shot test log
```

---

## Active Strategy: 7 DTE

### Parameters

```python
EMASpreadConfig(
    fast_ema=9,
    slow_ema=21,
    target_dte_min=5,         # target 5-9 days to expiry
    target_dte_max=9,
    short_delta_min=0.20,     # directional spread short leg
    short_delta_max=0.30,
    wing_delta_min=0.08,      # directional spread long leg
    wing_delta_max=0.12,
    take_profit_pct=0.50,     # close at 50% of credit received
    stop_loss_multiplier=1.5, # close at 1.5x credit received loss
    close_dte=1,              # close at DTE=1 to avoid pin risk
    iv_percentile_min=10.0,
    min_trend_strength=0.003, # |price - EMA21| / EMA21 >= 0.3%
    condor_on_low_iv=True,    # IC fallback when neutral/low IV
    ic_short_delta_min=0.15,
    ic_short_delta_max=0.25,
    ic_wing_delta_min=0.05,
    ic_wing_delta_max=0.10,
    entry_every_day=True,     # enter any weekday when flat
    account_size=2200.0,
    max_risk_per_trade_pct=0.20,
)
```

### Entry Logic
- Enter **any weekday at 09:00 UTC** when no position is open
- As soon as previous position closes, new one opens next available day
- Average gap between close and next open: **0 days** (70% same-day re-entry)

---

## Backtest Results Comparison (2 Years: Mar 2024 – Mar 2026)

| DTE | Trades | Win Rate | Net PnL (USD) | Total Return | CAGR | Fees |
|-----|--------|----------|---------------|-------------|------|------|
| 0 DTE | 522 | 53.1% | +$509 | +23.2% | +10.9% | $2,416 |
| 3 DTE | 256 | 80.5% | +$1,056 | +48.0% | +21.7% | $1,172 |
| **7 DTE** | **159** | **81.8%** | **+$1,276** | **+58.0%** | **+25.7%** | **$714** |

### 0 DTE Sydney Timed Entry — Parameter Sweep Results

Sweep tested 192 combinations: 4 entry times × 4 TPs × 4 SLs × 3 EMA strengths.
**Key finding: Entry time is the dominant factor. TP%, SL multiplier, and EMA strength made no meaningful difference within each window.**

| Window | Entry (UTC) | Net PnL | CAGR | Win% | Trades | Sharpe | Max DD |
|--------|-------------|---------|------|------|--------|--------|--------|
| 4PM Sydney | 05:00 UTC | **$1,307** | **26.3%** | 54.8% | 387 | **3.43** | 5.8% |
| 3PM Sydney | 04:00 UTC | $1,146 | 23.3% | 59.0% | 385 | 3.10 | 5.6% |
| 2PM Sydney | 03:00 UTC | $1,049 | 21.5% | 64.1% | 387 | 2.90 | 5.1% |
| 1PM Sydney | 02:00 UTC | $884 | 18.4% | 63.1% | 388 | 2.50 | 5.5% |
| **2PM+3PM+4PM combined** | — | **~$3,502** | **~49%** | — | ~1,159 | — | — |

Best parameters (consistent across all windows): `min_trend_strength=0.003`, any TP/SL combo.

Run sweep:
```bash
py run_0dte_sweep.py     # 192-combo sweep, saves to data/0dte_sweep_results.csv
```

### 7 DTE Win Rate by Strategy Type

| Strategy | Trades | Win Rate | Profit Factor | Net PnL |
|----------|--------|----------|---------------|---------|
| Bear Call Spread | 72 | 81.9% | 1.90 | +$657 |
| Bull Put Spread | 51 | 76.5% | 1.22 | +$180 |
| Iron Condor | 36 | 88.9% | 1.78 | +$438 |

### Win Rate Comparison by DTE

| Strategy | 0 DTE | 3 DTE | 7 DTE |
|----------|-------|-------|-------|
| Bear Call Spread | 62.8% | 88.7% | 81.9% |
| Bull Put Spread | 63.4% | 74.0% | 76.5% |
| Iron Condor | 25.4% | 73.4% | **88.9%** |

> Iron Condor at 0 DTE is a losing strategy (25.4%) — disable IC fallback if using 0 DTE.

Run backtests:
```bash
py run_7dte_backtest.py      # 7 DTE (recommended)
py run_3dte_backtest.py      # 3 DTE
py run_0dte_backtest.py      # 0 DTE
py analyze_0dte.py           # Win rate breakdown across all DTE
```

---

## API Configuration

File: `config/config.yaml`

```yaml
deribit:
  testnet_base_url: "https://test.deribit.com"
  client_id: "YOUR_TESTNET_CLIENT_ID"
  client_secret: "YOUR_TESTNET_CLIENT_SECRET"
  use_testnet: true

risk:
  account_size: 2200.0
  max_risk_per_trade_pct: 0.20
  max_open_positions: 1
  daily_loss_limit_pct: 0.10
```

Or use environment variables:
```bash
set DERIBIT_CLIENT_ID=your_id
set DERIBIT_CLIENT_SECRET=your_secret
```

Get testnet API keys at: https://test.deribit.com → Account → API Management

---

## Running the Bot

### Live bots (both active)
```bash
py run_live.py           # 7 DTE bot — entry any weekday at 09:00 UTC
py run_live_0dte.py      # 0 DTE bot — entry at 03:00 + 04:00 + 05:00 UTC
```

#### run_live.py (7 DTE)
- Warms up EMA from 30 days of real ETH closes (mainnet public API)
- Enters any weekday at 09:00 UTC when no position is open
- Polls every 60 seconds for exit conditions
- Auto-exits on take-profit / stop-loss / DTE=1 / signal reversal
- Saves state to `data/live_state.json` on every tick (survives restart)

#### run_live_0dte.py (ETH 0 DTE — 3 windows)
- Entry windows: **03:00 UTC** (2PM Syd) + **04:00 UTC** (3PM Syd) + **05:00 UTC** (4PM Syd)
- 20-minute tolerance per window, one trade per window per day
- Force-close at 07:50 UTC (before 08:00 UTC expiry)
- Capital: $2,200 per window ($6,600 total)
- **Exit monitoring**: WebSocket real-time (< 1s) + REST fallback every 60s
- State files: `data/live_0dte_2pm_state.json`, `data/live_0dte_3pm_state.json`, `data/live_0dte_4pm_state.json`
- Trade log: `data/live_0dte_trades.csv`

#### run_live_btc_0dte.py (BTC 0 DTE — 3 windows)
- Same windows and schedule as ETH 0 DTE bot
- Capital: $5,000 per window ($15,000 total)
- Strike grid: $500 on mainnet, $2,000 on testnet (wider risk limit: 40%)
- **Exit monitoring**: WebSocket real-time (< 1s) + REST fallback every 60s
- State files: `data/live_btc_0dte_2pm_state.json`, etc.
- Trade log: `data/live_btc_0dte_trades.csv`

### Test one position first
```bash
py test_open_position.py
```
Shows trade details, asks confirmation, places one order, saves state.

### Monitor in VSCode terminal
```powershell
# Live log stream
Get-Content logs\live.log -Wait -Tail 20

# Current open position
Get-Content data\live_state.json

# Closed trade history
Get-Content data\live_trades.csv
```

### Keep running 24/7 (Windows Task Scheduler)
1. Open Task Scheduler → Create Basic Task
2. Name: "ETH Options Bot"
3. Trigger: Daily, 08:55 AM UTC, repeat every 1 day
4. Action: Start a program
   - Program: `C:\Windows\py.exe`
   - Arguments: `run_live.py`
   - Start in: `C:\Users\Administrator\Desktop\projects\eth-options-bot`
5. Check "Run whether user is logged in or not"

---

## Exit Monitoring: WebSocket + REST Hybrid

Both `run_live_0dte.py` and `run_live_btc_0dte.py` use a two-layer exit system:

### Layer 1 — WebSocket (real-time, < 1 second)

After each trade opens, a `WSPositionMonitor` (background thread) subscribes to
`ticker.{instrument}.100ms` for every active leg via Deribit WebSocket.

On each price push it recalculates unrealized P&L and immediately sets `exit_reason`
if TP or SL is crossed. The main loop checks this flag every **5 seconds** and closes
via REST the moment it's set.

```
src/monitoring/position_monitor.py — WSPositionMonitor class
```

### Layer 2 — REST polling (fallback, every 60 seconds)

The REST tick still runs every 60 seconds and handles:
- Entry checks (new trades)
- Force-close at 07:50 UTC before expiry
- Signal reversal exits
- Fallback TP/SL if WebSocket has been silent for > 90 seconds

### Flow

```
Position opened
    |
    +-- WSPositionMonitor starts (background thread)
    |       Subscribes: ticker.{leg1}.100ms, ticker.{leg2}.100ms
    |       On each tick: calc P&L -> set exit_reason if TP/SL hit
    |
Main loop (every 5s)
    +-- _check_ws_exits(): if exit_reason set -> close via REST immediately
    |
    +-- Every 60s: REST tick (entry, force-close, IV, signal reversal fallback)
```

| | Before | After |
|---|---|---|
| TP/SL reaction time | Up to 60s | Under 1s |
| Naked call gap risk | 60s unprotected | Near real-time |
| Fallback if WS dies | None | REST takes over after 90s |

### BTC mainnet note

BTC testnet uses `max_risk_per_trade_pct=0.40` (spreads are $2,000 wide on testnet).
Switch back to `0.20` on mainnet where BTC 0DTE strikes are $500 apart.

---

## Key Technical Notes

### STUB Legs
`IronCondor` dataclass is reused for 2-leg spreads. Unused legs get
`quantity=0` and `instrument_name="STUB"`. All broker/backtest code skips
legs where `leg.quantity == 0 or leg.instrument_name == "STUB"`.

### Deribit REST API
- Buy/sell/cancel endpoints use **GET** with query params (NOT POST with JSON body)
- Order amounts must be integers (min 1 contract)
- Price tick size: 0.0001 ETH
- Auth: client_credentials flow with Bearer token auto-refresh

### EMA Signal
- Computed from daily closes of ETH-PERPETUAL (mainnet public endpoint)
- Bullish: EMA9 > EMA21 and price > EMA21
- Bearish: EMA9 < EMA21 and price < EMA21
- Neutral: weak trend (|price - EMA21| / EMA21 < 0.3%)
- Price history fetched from mainnet; orders placed on testnet

### Fee Accounting
```
fee per trade = 0.0003 ETH * active_legs * contracts
             (charged on both open AND close)
```
Active legs = legs with quantity > 0 (excludes STUBs).

### Entry Frequency (`entry_every_day`)
- `False` (default): enter Mondays only
- `True`: enter any weekday when no position is open
- 7 DTE bot uses `True` — 70% of re-entries happen same day as previous close

### Backtest Exit Date Fix
`SimulatedBroker.close_condor` accepts `as_of=dt` to record the simulation
date as exit_time (instead of `datetime.now()`). Always pass `as_of=dt`
from the backtest engine.

### Windows Encoding
No Unicode characters (`→`, `─`, `Δ`) in any log strings — Windows cp1252
cannot encode them. Use ASCII alternatives: `->`, `-`, `d=`.

---

## Important Files

| File | Purpose |
|------|---------|
| `run_live.py` | Live bot: 7 DTE, entry any weekday at 09:00 UTC |
| `run_live_0dte.py` | Live bot: 0 DTE, 3 windows (2PM+3PM+4PM Sydney) |
| `run_0dte_sweep.py` | 192-combo parameter sweep for 0 DTE strategy |
| `test_open_position.py` | One-shot position test |
| `test_entry.py` | Force a test entry bypassing time window |
| `test_strikes.py` | Inspect 0-2 DTE chain strikes/deltas on testnet |
| `run_7dte_backtest.py` | 7 DTE backtest (best performer) |
| `run_3dte_backtest.py` | 3 DTE backtest |
| `run_0dte_backtest.py` | 0 DTE backtest |
| `run_0dte_sydney.py` | 0 DTE backtest with Sydney timed entry |
| `analyze_0dte.py` | Win rate by type across all DTE |
| `src/strategy/ema_spread.py` | EMA strategy + EMASpreadConfig |
| `src/backtest/ema_backtest.py` | Backtest engine (all DTE modes) |
| `src/execution/simulated_broker.py` | Paper broker (fees + sim date) |
| `src/execution/deribit_broker.py` | Live order execution |
| `src/deribit/rest_client.py` | Deribit REST API wrapper |
| `src/deribit/ws_client.py` | WebSocket client (used by position monitor) |
| `src/monitoring/position_monitor.py` | WSPositionMonitor — real-time TP/SL via WebSocket |
| `config/config.yaml` | All configuration + API keys |
| `data/live_state.json` | 7 DTE active position state |
| `data/live_0dte_2pm_state.json` | 0 DTE 2PM window state |
| `data/live_0dte_3pm_state.json` | 0 DTE 3PM window state |
| `data/live_0dte_4pm_state.json` | 0 DTE 4PM window state |
| `data/live_trades.csv` | 7 DTE closed trade log |
| `data/live_0dte_trades.csv` | ETH 0 DTE closed trade log |
| `data/live_btc_0dte_trades.csv` | BTC 0 DTE closed trade log |
| `data/0dte_sweep_results.csv` | Full parameter sweep results (192 combos) |
| `logs/live.log` | 7 DTE bot runtime log |
| `logs/live_0dte.log` | 0 DTE bot runtime log |

---

## Emergency Stop

```powershell
# Kill the bot
taskkill /F /IM py.exe

# Manually close all positions via Deribit web UI:
# https://test.deribit.com → Portfolio → Positions → Close All
```

---

## Switching to Mainnet

1. Get mainnet API keys from https://www.deribit.com → Account → API Management
2. Edit `config/config.yaml`:
   ```yaml
   deribit:
     use_testnet: false
     client_id: "YOUR_MAINNET_ID"
     client_secret: "YOUR_MAINNET_SECRET"
   ```

**WARNING**: Real money. Run on testnet for at least 4 weeks before switching.

---

## Dependencies

```bash
pip install requests pyyaml pydantic pandas numpy scipy
```

Python 3.10+ required. Use `py` launcher on Windows (not `python`).

---

## Current Live State (as of last session)

- **Bot**: Running on Deribit testnet via `run_live.py`
- **Strategy**: 7 DTE EMA hybrid, entry any weekday at 09:00 UTC
- **Open position**: Bull Put Spread
  - SELL ETH-13MAR26-2000-P @ 0.0175 ETH (Order ETH-63530540693)
  - BUY  ETH-13MAR26-1800-P @ 0.0055 ETH (Order ETH-63530541446)
  - Credit: 0.01275 ETH | Expiry: 13 Mar 2026 (~8 DTE)
  - ETH spot at entry: $2,146
  - Exit triggers: TP at +0.00638 ETH | SL at -0.01913 ETH | DTE=1
- **State file**: `data/live_state.json`
- **Log**: `logs/live.log`
