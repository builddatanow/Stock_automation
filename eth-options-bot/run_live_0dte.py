"""
0 DTE Live Bot -- 2 PM + 3 PM + 4 PM Sydney AEDT (Deribit Testnet)
====================================================================
Strategy  : EMA(9/21) -> Bull Put (bullish) | Bear Call (bearish)
            No Iron Condor fallback
DTE       : 0 DTE -- options expiring at 08:00 UTC today
Entry     : 03:00 UTC (2 PM Sydney) + 04:00 UTC (3 PM Sydney) + 05:00 UTC (4 PM Sydney)
            Entry window: 10-minute tolerance after the target hour
Exit      : Checked every 60s -- take-profit 50%, stop-loss 1.5x,
            or forced close at 07:50 UTC (10 min before expiry)
Capital   : $2,200 per window ($6,600 total)

Setup:
  1. Set credentials in config/config.yaml or via env vars:
       DERIBIT_CLIENT_ID=...
       DERIBIT_CLIENT_SECRET=...

  2. Run:
       python run_live_0dte.py

  3. Logs: logs/live_0dte.log  |  Trades: data/live_0dte_trades.csv
"""

import os, sys, json, time, logging, csv
sys.path.insert(0, ".")

from datetime import datetime, timedelta, timezone
from typing import Optional

import requests
import numpy as np

from src.deribit.rest_client import DeribitRESTClient
from src.execution.deribit_broker import DeribitBroker
from src.data.ingestion import DataIngestionService
from src.data.models import IronCondor, PositionStatus, OptionQuote
from src.risk.risk_manager import RiskManager, RiskViolation
from src.strategy.ema_spread import (
    EMASpreadConfig, get_ema_signal, ema_trend_strength,
    select_spread_strikes, build_spread,
    check_exit_conditions as ema_check_exit,
)
from config.settings import RiskConfig, ExecutionConfig, load_config
from src.monitoring.logger import setup_logging
from src.monitoring.notifier import AlertManager, TelegramNotifier, WhatsAppNotifier
from src.monitoring.position_monitor import WSPositionMonitor

os.makedirs("logs", exist_ok=True)
os.makedirs("data", exist_ok=True)
setup_logging("INFO", "logs/live_0dte.log")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAINNET_URL      = "https://www.deribit.com"
TESTNET_URL      = "https://test.deribit.com"
POLL_SECONDS     = 60   # REST chain fetch interval (entry checks, force-close, IV)
WS_CHECK_SECONDS = 5    # how often main loop checks WS monitor exit signal
ACCOUNT_SIZE = 2200.0          # per window
TRADES_CSV   = "data/live_0dte_trades.csv"

# Deribit ETH options expire at 08:00 UTC -- force-close 10 min before
FORCE_CLOSE_HOUR   = 7
FORCE_CLOSE_MINUTE = 50

# Entry windows: (label, UTC hour, UTC minute, state file)
ENTRY_WINDOWS = [
    {"label": "2PM-Sydney", "hour": 3,  "minute": 0, "state": "data/live_0dte_2pm_state.json"},
    {"label": "3PM-Sydney", "hour": 4,  "minute": 0, "state": "data/live_0dte_3pm_state.json"},
    {"label": "4PM-Sydney", "hour": 5,  "minute": 0, "state": "data/live_0dte_4pm_state.json"},
]
ENTRY_WINDOW_MINUTES = 20   # accept entry up to 20 min after target time

# ---------------------------------------------------------------------------
# Strategy config (0-2 DTE, no IC)
# Note: target_dte_max=2 so the bot also considers tomorrow's expiry on testnet
# where today's 0 DTE chain has too few strikes. On mainnet, 0 DTE has full coverage.
# ---------------------------------------------------------------------------

cfg = EMASpreadConfig(
    fast_ema=9,
    slow_ema=21,
    target_dte_min=0,
    target_dte_max=2,
    short_delta_min=0.20,
    short_delta_max=0.35,
    wing_delta_min=0.08,
    wing_delta_max=0.15,
    take_profit_pct=0.50,
    stop_loss_multiplier=1.5,
    close_dte=0,
    iv_percentile_min=10.0,
    min_trend_strength=0.003,
    condor_on_low_iv=False,    # NO Iron Condor on 0 DTE
    account_size=ACCOUNT_SIZE,
    max_risk_per_trade_pct=0.20,
)

risk_cfg = RiskConfig(
    account_size=ACCOUNT_SIZE,
    max_risk_per_trade_pct=cfg.max_risk_per_trade_pct,
    max_open_positions=1,
    daily_loss_limit_pct=0.10,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def fetch_price_history(days: int = 30) -> list[float]:
    end_ms   = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = end_ms - days * 86400 * 1000
    try:
        resp = requests.get(
            f"{MAINNET_URL}/api/v2/public/get_tradingview_chart_data",
            params={"instrument_name": "ETH-PERPETUAL",
                    "start_timestamp": start_ms,
                    "end_timestamp":   end_ms,
                    "resolution":      "1D"},
            timeout=20,
        )
        resp.raise_for_status()
        closes = resp.json().get("result", {}).get("close", [])
        return [float(c) for c in closes if c]
    except Exception as exc:
        logger.warning("Could not fetch price history: %s", exc)
        return []


def iv_percentile(iv_window: list[float]) -> float:
    if len(iv_window) < 2:
        return 50.0
    current = iv_window[-1]
    return float(sum(1 for v in iv_window[:-1] if v < current) / len(iv_window[:-1]) * 100)


def save_state(state_file: str, spread: Optional[IronCondor]) -> None:
    if spread is None:
        if os.path.exists(state_file):
            os.remove(state_file)
        return
    spread_type = spread.__dict__.get("spread_type", "")
    state = {
        "id":            spread.id,
        "spread_type":   spread_type,
        "entry_time":    spread.entry_time.isoformat(),
        "credit":        spread.credit_received,
        "max_loss":      spread.max_loss,
        "spot_at_entry": spread.underlying_price_at_entry,
        "legs": [
            {
                "instrument_name": leg.instrument_name,
                "strike":          leg.strike,
                "option_type":     leg.option_type.value,
                "side":            leg.side.value,
                "quantity":        leg.quantity,
                "entry_price":     leg.entry_price,
                "expiry":          leg.expiry.isoformat(),
            }
            for leg in spread.legs
            if leg.quantity > 0 and leg.instrument_name != "STUB"
        ],
    }
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2)


def log_trade_csv(spread: IronCondor, window_label: str,
                  exit_reason: str, spot_at_exit: float) -> None:
    file_exists = os.path.exists(TRADES_CSV)
    pnl = spread.realized_pnl or 0.0
    spread_type = spread.__dict__.get("spread_type", "")
    with open(TRADES_CSV, "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow([
                "window", "entry_time", "exit_time", "spread_type",
                "spot_entry", "spot_exit", "credit_eth", "max_loss",
                "pnl_eth", "pnl_usd", "exit_reason",
            ])
        writer.writerow([
            window_label,
            spread.entry_time.strftime("%Y-%m-%d %H:%M"),
            spread.exit_time.strftime("%Y-%m-%d %H:%M") if spread.exit_time else "",
            spread_type,
            f"{spread.underlying_price_at_entry:.0f}",
            f"{spot_at_exit:.0f}",
            f"{spread.credit_received:.5f}",
            f"{spread.max_loss:.2f}",
            f"{pnl:+.5f}",
            f"${pnl * spread.underlying_price_at_entry:+.2f}",
            exit_reason,
        ])


# ---------------------------------------------------------------------------
# Window state -- one per entry time
# ---------------------------------------------------------------------------

class WindowTrader:
    """Manages one entry window (e.g. 2PM or 3PM Sydney)."""

    def __init__(self, window_cfg: dict, broker: DeribitBroker,
                 risk: RiskManager, alerter: AlertManager,
                 ws_url: str = "", client_id: str = "", client_secret: str = "") -> None:
        self.label      = window_cfg["label"]
        self.entry_hour = window_cfg["hour"]
        self.entry_min  = window_cfg["minute"]
        self.state_file = window_cfg["state"]
        self.broker     = broker
        self.risk       = risk
        self.alerter    = alerter
        self.ws_url     = ws_url
        self.client_id  = client_id
        self.client_secret = client_secret

        self.open_spread: Optional[IronCondor] = None
        self._last_entry_date: Optional[str]   = None
        self._ws_monitor: Optional[WSPositionMonitor] = None

    def should_enter(self, now: datetime) -> bool:
        """True if we're inside the 10-minute entry window and haven't entered today."""
        date_str = now.strftime("%Y-%m-%d")
        if self._last_entry_date == date_str:
            return False
        if self.open_spread is not None:
            return False
        target = now.replace(hour=self.entry_hour, minute=self.entry_min,
                             second=0, microsecond=0)
        delta_mins = (now - target).total_seconds() / 60
        return 0 <= delta_mins <= ENTRY_WINDOW_MINUTES

    def should_force_close(self, now: datetime) -> bool:
        """True if it's 07:50+ UTC and we still have a position."""
        if self.open_spread is None:
            return False
        return now.hour == FORCE_CLOSE_HOUR and now.minute >= FORCE_CLOSE_MINUTE

    def try_entry(self, chain: list[OptionQuote], quote_map: dict,
                  spot: float, iv_pct: float,
                  price_history: list[float], now: datetime) -> None:
        logger.info("[%s] Entry check at $%.0f (IV pct: %.0f%%)",
                    self.label, spot, iv_pct)

        signal   = get_ema_signal(price_history, cfg.fast_ema, cfg.slow_ema)
        strength = abs(ema_trend_strength(price_history, cfg.slow_ema))
        logger.info("[%s] Signal: %s | Strength: %.2f%%",
                    self.label, signal, strength * 100)

        if signal == "neutral" or strength < cfg.min_trend_strength or iv_pct < cfg.iv_percentile_min:
            logger.info("[%s] Entry skipped: signal=%s strength=%.3f iv_pct=%.0f",
                        self.label, signal, strength, iv_pct)
            self._last_entry_date = now.strftime("%Y-%m-%d")
            return

        strikes = select_spread_strikes(chain, signal, cfg, as_of=now)
        if strikes is None:
            logger.info("[%s] No suitable strikes found -- skipping", self.label)
            self._last_entry_date = now.strftime("%Y-%m-%d")
            return

        spread = build_spread(strikes, quantity=1.0, fill_model="mid")
        spread_type_str = strikes["spread_type"]

        # Risk check
        try:
            account = self.broker.get_account_state()
            self.risk.check_new_trade(spread, 0, account)
        except RiskViolation as e:
            logger.warning("[%s] Risk blocked entry: %s", self.label, e)
            self._last_entry_date = now.strftime("%Y-%m-%d")
            return

        # Place orders
        try:
            orders = self.broker.open_condor(spread)
            filled = sum(1 for o in orders if o.status.value in ("filled", "partially_filled"))
            logger.info("[%s] Opened %s | %d/%d legs | credit=%.5f ETH | max_loss=%.2f ETH",
                        self.label, spread_type_str, filled, len(orders),
                        spread.credit_received, spread.max_loss)
            self.open_spread = spread
            self._last_entry_date = now.strftime("%Y-%m-%d")
            save_state(self.state_file, spread)
            self.alerter.trade_opened(spread, spot=spot)
            # Start WebSocket monitor for real-time TP/SL
            if self.ws_url and self.client_id:
                self._ws_monitor = WSPositionMonitor(
                    spread=spread,
                    take_profit_pct=cfg.take_profit_pct,
                    stop_loss_multiplier=cfg.stop_loss_multiplier,
                    ws_url=self.ws_url,
                    client_id=self.client_id,
                    client_secret=self.client_secret,
                )
                self._ws_monitor.start()
        except Exception as exc:
            logger.error("[%s] Failed to open position: %s", self.label, exc)

    def try_exit(self, quote_map: dict, spot: float,
                 price_history: list[float], now: datetime,
                 force: bool = False, ws_reason: Optional[str] = None) -> None:
        if self.open_spread is None:
            return

        spread = self.open_spread
        if force:
            reason = "force_close_before_expiry"
        elif ws_reason:
            reason = ws_reason   # WS monitor already confirmed TP/SL
        else:
            current_signal = get_ema_signal(price_history, cfg.fast_ema, cfg.slow_ema)
            reason = ema_check_exit(spread, quote_map, cfg, as_of=now,
                                    current_signal=current_signal)

        if not reason:
            return

        logger.info("[%s] EXIT: %s | Closing %s...", self.label, reason, spread.id)
        try:
            self.broker.close_condor(spread, reason=reason)
            pnl = spread.realized_pnl or 0.0
            pnl_usd = pnl * spread.underlying_price_at_entry
            logger.info("[%s] Closed | PnL: %+.5f ETH (~$%+.2f) | %s",
                        self.label, pnl, pnl_usd, reason)
            self.risk.record_pnl(pnl)
            log_trade_csv(spread, self.label, reason, spot)
            self.alerter.trade_closed(spread, spot=spot)
            self.open_spread = None
            save_state(self.state_file, None)
            if self._ws_monitor:
                self._ws_monitor.stop()
                self._ws_monitor = None
        except Exception as exc:
            logger.error("[%s] Failed to close position: %s", self.label, exc)


# ---------------------------------------------------------------------------
# Main bot
# ---------------------------------------------------------------------------

class ZeroDTELiveTrader:
    def __init__(self, broker: DeribitBroker, ingestion: DataIngestionService,
                 alerter: AlertManager,
                 ws_url: str = "", client_id: str = "", client_secret: str = "") -> None:
        self.broker    = broker
        self.ingestion = ingestion
        self.alerter   = alerter
        self.risk      = RiskManager(risk_cfg)

        self.price_history: list[float] = []
        self.iv_window:     list[float] = []
        self._last_price_date: Optional[str] = None
        self._last_rest_tick:  float         = 0.0

        self.windows = [
            WindowTrader(w, broker, self.risk, alerter,
                         ws_url=ws_url, client_id=client_id, client_secret=client_secret)
            for w in ENTRY_WINDOWS
        ]

    def warm_up(self) -> None:
        logger.info("Warming up price history (30 days)...")
        prices = fetch_price_history(days=30)
        if prices:
            self.price_history = prices
            logger.info("  %d daily closes loaded. Latest: $%.0f",
                        len(prices), prices[-1])
        else:
            logger.warning("  Could not load price history")

        logger.info("Warming up IV window...")
        try:
            iv_series = self.ingestion.fetch_iv_history(lookback_days=60)
            self.iv_window = list(iv_series.values / 100.0) if not iv_series.empty else []
            logger.info("  %d IV points loaded", len(self.iv_window))
        except Exception as exc:
            logger.warning("  Could not load IV history: %s", exc)

    def run(self) -> None:
        logger.info("=" * 60)
        logger.info("  0 DTE BOT -- 2PM + 3PM + 4PM SYDNEY | DERIBIT TESTNET")
        logger.info("  Capital: $%.0f per window | No Iron Condor", ACCOUNT_SIZE)
        logger.info("=" * 60)

        self.warm_up()
        self.alerter.alert(
            f"0 DTE Bot STARTED\n"
            f"Windows: 2PM (03:00 UTC) + 3PM (04:00 UTC) + 4PM (05:00 UTC) Sydney\n"
            f"Capital: ${ACCOUNT_SIZE:.0f} per window | No IC\n"
            f"Force-close: 07:50 UTC daily",
            level="INFO",
        )

        try:
            while True:
                try:
                    # Fast path: check WS monitor exit signals every 5s
                    self._check_ws_exits()
                    # Slow path: REST chain fetch every 60s (entry, force-close, IV)
                    if time.time() - self._last_rest_tick >= POLL_SECONDS:
                        self._tick()
                        self._last_rest_tick = time.time()
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    logger.exception("Unhandled error in tick: %s", exc)
                    self.alerter.alert(f"0 DTE Bot ERROR: {exc}", level="ERROR")
                time.sleep(WS_CHECK_SECONDS)
        except KeyboardInterrupt:
            logger.info("Shutdown requested -- stopping.")
            self.alerter.alert("0 DTE Bot STOPPED (manual shutdown)", level="WARN")

    def _check_ws_exits(self) -> None:
        """Fast path: if WS monitor has triggered TP/SL, close immediately via REST."""
        for window in self.windows:
            mon = window._ws_monitor
            if mon is None or window.open_spread is None:
                continue
            reason = mon.exit_reason
            if reason:
                now  = datetime.now(timezone.utc)
                spot = self.price_history[-1] if self.price_history else 0.0
                logger.info("[%s] WS triggered exit: %s", window.label, reason)
                window.try_exit(
                    quote_map={},   # reason already set; close_condor fetches live prices
                    spot=spot,
                    price_history=self.price_history,
                    now=now,
                    force=False,
                    ws_reason=reason,
                )

    def _tick(self) -> None:
        now      = datetime.now(timezone.utc)
        date_str = now.strftime("%Y-%m-%d")

        # Fetch chain — filter to 0-3 DTE and ±40% strike range to avoid 429 rate limits
        try:
            spot = self.ingestion.fetch_underlying_price()
            chain = self.ingestion.fetch_snapshot(dte_max=3, spot_price=spot, strike_range_pct=0.40)
        except Exception as exc:
            logger.warning("Failed to fetch option chain: %s", exc)
            return

        if not chain:
            logger.warning("Empty option chain -- skipping tick")
            return

        quote_map = {q.instrument_name: q for q in chain}

        # Update daily price/IV (once per day)
        if self._last_price_date != date_str:
            self.price_history.append(spot)
            self._last_price_date = date_str
            avg_iv = float(np.mean([q.implied_volatility for q in chain
                                    if q.implied_volatility > 0]))
            self.iv_window.append(avg_iv)
            logger.info("[%s] Price=$%.0f IV=%.1f%%", date_str, spot, avg_iv * 100)

        iv_pct = iv_percentile(self.iv_window)

        for window in self.windows:
            # Force close at 07:50 UTC if still holding
            if window.should_force_close(now):
                logger.info("[%s] Force-close before expiry at %s", window.label, now.strftime("%H:%M"))
                window.try_exit(quote_map, spot, self.price_history, now, force=True)
                continue

            # Normal exit check
            if window.open_spread and window.open_spread.status == PositionStatus.OPEN:
                window.try_exit(quote_map, spot, self.price_history, now)

            # Entry check
            if window.should_enter(now):
                window.try_entry(chain, quote_map, spot, iv_pct, self.price_history, now)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app_cfg     = load_config()
    deribit_cfg = app_cfg.deribit

    client_id     = deribit_cfg.client_id
    client_secret = deribit_cfg.client_secret

    if not client_id or not client_secret:
        print("\n  ERROR: Deribit credentials not set.")
        print("  Set DERIBIT_CLIENT_ID and DERIBIT_CLIENT_SECRET env vars")
        print("  or edit config/config.yaml\n")
        sys.exit(1)

    print("=" * 60)
    print("  0 DTE Bot -- 2PM + 3PM + 4PM Sydney | Deribit Testnet")
    print(f"  API : {TESTNET_URL}")
    print(f"  ID  : {client_id[:6]}***")
    print(f"  Capital  : ${ACCOUNT_SIZE:.0f} per window (${ACCOUNT_SIZE*3:.0f} total)")
    print(f"  Windows  : 03:00 UTC (2PM) + 04:00 UTC (3PM) + 05:00 UTC (4PM) Syd")
    print(f"  Force-close: 07:50 UTC")
    print(f"  Poll     : every {POLL_SECONDS}s")
    print("=" * 60)

    client = DeribitRESTClient(
        base_url=TESTNET_URL,
        client_id=client_id,
        client_secret=client_secret,
    )
    try:
        account = client.get_account_summary("ETH")
        balance = account.get("balance", 0.0)
        equity  = account.get("equity",  0.0)
        print(f"\n  Connected to Deribit Testnet")
        print(f"  ETH Balance : {balance:.4f} ETH")
        print(f"  ETH Equity  : {equity:.4f} ETH")
    except Exception as exc:
        print(f"\n  ERROR: Cannot authenticate: {exc}")
        sys.exit(1)

    exec_cfg  = app_cfg.execution
    broker    = DeribitBroker(client=client, config=exec_cfg, currency="ETH")
    ingestion = DataIngestionService(client=client, currency="ETH")

    mon_cfg = app_cfg.monitoring if hasattr(app_cfg, "monitoring") else None

    tg_token  = os.environ.get("TELEGRAM_TOKEN",   getattr(mon_cfg, "telegram_token",   ""))
    tg_chatid = os.environ.get("TELEGRAM_CHAT_ID", getattr(mon_cfg, "telegram_chat_id", ""))
    telegram  = TelegramNotifier(tg_token, tg_chatid) if tg_token and tg_chatid else None

    wa_phone  = os.environ.get("WHATSAPP_PHONE",  getattr(mon_cfg, "whatsapp_phone",  ""))
    wa_apikey = os.environ.get("WHATSAPP_APIKEY", getattr(mon_cfg, "whatsapp_apikey", ""))
    whatsapp  = WhatsAppNotifier(wa_phone, wa_apikey) if wa_phone and wa_apikey else None

    for name, notifier in [("Telegram", telegram), ("WhatsApp", whatsapp)]:
        print(f"  {name:10}: {'enabled' if notifier else 'not configured'}")

    ws_url  = TESTNET_URL.replace("https://", "wss://") + "/ws/api/v2"
    alerter = AlertManager(telegram=telegram, whatsapp=whatsapp)
    trader  = ZeroDTELiveTrader(broker=broker, ingestion=ingestion, alerter=alerter,
                                ws_url=ws_url, client_id=client_id, client_secret=client_secret)
    trader.run()
