"""
EMA Hybrid Strategy – Live Trading on Deribit Testnet (Demo Account)
=====================================================================
Strategy  : EMA(9/21) → Bull Put Spread (bullish) | Bear Call Spread (bearish)
            Neutral or low-IV → Iron Condor fallback
DTE       : 7 DTE (target 5-9 days to expiry)
Entry     : Any weekday at 09:00 UTC when no position is open
Exit      : Checked every 60 seconds (take-profit 50%, stop-loss 1.5x, DTE≤1)

Setup:
  1. Set environment variables:
       DERIBIT_CLIENT_ID=<your testnet client id>
       DERIBIT_CLIENT_SECRET=<your testnet client secret>
     OR edit config/config.yaml  (deribit.client_id / client_secret / use_testnet)

  2. Run:
       python run_live.py

  3. Logs are written to  logs/live.log  and  data/live_trades.csv

Note: EMA price history is seeded with real ETH closes from the public
      Deribit mainnet endpoint (no auth needed).  Orders are placed on the
      testnet (test.deribit.com) using your demo credentials.
"""

import os, sys, json, time, math, logging, csv
sys.path.insert(0, ".")

from datetime import datetime, timedelta, timezone
from typing import Optional

import requests
import numpy as np

from src.deribit.rest_client import DeribitRESTClient
from src.execution.deribit_broker import DeribitBroker
from src.data.ingestion import DataIngestionService, parse_option_quote
from src.data.models import IronCondor, PositionStatus, OptionQuote
from src.risk.risk_manager import RiskManager, RiskViolation
from src.strategy.ema_spread import (
    EMASpreadConfig, get_ema_signal, ema_trend_strength,
    select_spread_strikes, build_spread,
    check_exit_conditions as ema_check_exit,
)
from src.strategy.weekly_iron_condor import (
    select_strikes as ic_select_strikes,
    build_condor as ic_build_condor,
    check_exit_conditions as ic_check_exit,
)
from config.settings import (
    StrategyConfig as ICStrategyConfig,
    RiskConfig, ExecutionConfig, load_config,
)
from src.monitoring.logger import setup_logging
from src.monitoring.notifier import AlertManager, WhatsAppNotifier, TelegramNotifier

os.makedirs("logs", exist_ok=True)
os.makedirs("data", exist_ok=True)
setup_logging("INFO", "logs/live.log")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAINNET_URL   = "https://www.deribit.com"   # for public price history only
TESTNET_URL   = "https://test.deribit.com"  # for all authenticated calls
POLL_SECONDS  = 60
ENTRY_HOUR    = 9     # UTC hour to evaluate Monday entry
STATE_FILE    = "data/live_state.json"
TRADES_CSV    = "data/live_trades.csv"
ACCOUNT_SIZE  = 2200.0

# ---------------------------------------------------------------------------
# Strategy configuration (same params as the best backtest run)
# ---------------------------------------------------------------------------

cfg = EMASpreadConfig(
    fast_ema=9,
    slow_ema=21,
    target_dte_min=5,       # 7 DTE strategy: target 5-9 days to expiry
    target_dte_max=9,
    short_delta_min=0.20,
    short_delta_max=0.30,
    wing_delta_min=0.08,
    wing_delta_max=0.12,
    take_profit_pct=0.50,
    stop_loss_multiplier=1.5,
    close_dte=1,            # close at DTE=1 to avoid pin risk
    iv_percentile_min=10.0,
    min_trend_strength=0.003,
    condor_on_low_iv=True,
    ic_short_delta_min=0.15,
    ic_short_delta_max=0.25,
    ic_wing_delta_min=0.05,
    ic_wing_delta_max=0.10,
    account_size=ACCOUNT_SIZE,
    max_risk_per_trade_pct=0.20,
)

ic_strat_cfg = ICStrategyConfig(
    target_dte_min=cfg.target_dte_min,
    target_dte_max=cfg.target_dte_max,
    short_delta_min=cfg.ic_short_delta_min,
    short_delta_max=cfg.ic_short_delta_max,
    wing_delta_min=cfg.ic_wing_delta_min,
    wing_delta_max=cfg.ic_wing_delta_max,
    take_profit_pct=cfg.take_profit_pct,
    stop_loss_multiplier=cfg.stop_loss_multiplier,
    close_dte=cfg.close_dte,
    iv_percentile_min=0.0,
    max_daily_move_pct=100.0,
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
    """Fetch real ETH daily closes from Deribit mainnet (public, no auth)."""
    end_ms   = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = end_ms - days * 86400 * 1000
    url = f"{MAINNET_URL}/api/v2/public/get_tradingview_chart_data"
    params = {
        "instrument_name": "ETH-PERPETUAL",
        "start_timestamp": start_ms,
        "end_timestamp":   end_ms,
        "resolution":      "1D",
    }
    try:
        resp = requests.get(url, params=params, timeout=20)
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


def save_state(spread: Optional[IronCondor]) -> None:
    """Persist open position to JSON so the bot can resume after restart."""
    if spread is None:
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)
        return
    spread_type = spread.__dict__.get("spread_type", "")
    state = {
        "id":             spread.id,
        "spread_type":    spread_type,
        "entry_time":     spread.entry_time.isoformat(),
        "credit":         spread.credit_received,
        "max_loss":       spread.max_loss,
        "spot_at_entry":  spread.underlying_price_at_entry,
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
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
    logger.info("State saved -> %s", STATE_FILE)


def log_trade_csv(spread: IronCondor, exit_reason: str, spot_at_exit: float) -> None:
    """Append a closed trade row to the CSV journal."""
    file_exists = os.path.exists(TRADES_CSV)
    spread_type = spread.__dict__.get("spread_type", "")
    pnl = spread.realized_pnl or 0.0
    with open(TRADES_CSV, "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow([
                "entry_time","exit_time","spread_type","spot_entry","spot_exit",
                "credit_eth","max_loss","pnl_eth","pnl_usd","exit_reason",
            ])
        writer.writerow([
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
# Main trading loop
# ---------------------------------------------------------------------------

class EMALiveTrader:
    def __init__(self, broker: DeribitBroker, ingestion: DataIngestionService,
                 alerter: AlertManager | None = None) -> None:
        self.broker    = broker
        self.ingestion = ingestion
        self.risk      = RiskManager(risk_cfg)
        self.alerter   = alerter or AlertManager()

        self.price_history: list[float] = []
        self.iv_window:     list[float] = []
        self.open_spread:   Optional[IronCondor] = None
        self._last_entry_date: Optional[str] = None  # "YYYY-MM-DD"
        self._last_price_date: Optional[str] = None  # track daily price

    # ------------------------------------------------------------------
    def warm_up(self) -> None:
        """Seed price history with last 30 real daily ETH closes."""
        logger.info("Warming up price history...")
        prices = fetch_price_history(days=30)
        if prices:
            self.price_history = prices
            logger.info("  Loaded %d daily closes. Latest: $%.0f", len(prices), prices[-1])
        else:
            logger.warning("  Could not load price history — EMA signals may be neutral initially")

        logger.info("Warming up IV window...")
        try:
            iv_series = self.ingestion.fetch_iv_history(lookback_days=60)
            self.iv_window = list(iv_series.values / 100.0) if not iv_series.empty else []
            logger.info("  Loaded %d IV data points", len(self.iv_window))
        except Exception as exc:
            logger.warning("  Could not load IV history: %s", exc)

    # ------------------------------------------------------------------
    def run(self) -> None:
        logger.info("=" * 60)
        logger.info("  EMA HYBRID STRATEGY — DERIBIT TESTNET LIVE TRADER")
        logger.info("  Account: $%.0f | Strategy: 7 DTE EMA(9/21) + IC fallback", ACCOUNT_SIZE)
        logger.info("=" * 60)

        self.warm_up()
        logger.info("Bot running. Entry any weekday at %02d:00 UTC when flat. Polling every %ds.",
                    ENTRY_HOUR, POLL_SECONDS)
        self.alerter.alert(
            f"Bot STARTED\nStrategy: 7 DTE EMA(9/21)\nAccount: ${ACCOUNT_SIZE:.0f}\n"
            f"Entry: any weekday at {ENTRY_HOUR:02d}:00 UTC\nPoll: every {POLL_SECONDS}s",
            level="INFO",
        )

        try:
            while True:
                try:
                    self._tick()
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    logger.exception("Unhandled error in tick: %s", exc)
                    self.alerter.alert(f"Bot ERROR: {exc}", level="ERROR")
                time.sleep(POLL_SECONDS)
        except KeyboardInterrupt:
            logger.info("Shutdown requested -- stopping bot.")
            self.alerter.alert("Bot STOPPED (manual shutdown)", level="WARN")

    # ------------------------------------------------------------------
    def _tick(self) -> None:
        now  = datetime.now(timezone.utc)
        date_str = now.strftime("%Y-%m-%d")

        # Fetch market snapshot
        try:
            chain = self.ingestion.fetch_snapshot()
        except Exception as exc:
            logger.warning("Failed to fetch option chain: %s", exc)
            return

        if not chain:
            logger.warning("Empty option chain — skipping tick")
            return

        quote_map = {q.instrument_name: q for q in chain}
        spot      = chain[0].underlying_price or self.ingestion.fetch_underlying_price()

        # Track daily price (once per day)
        if self._last_price_date != date_str:
            self.price_history.append(spot)
            self._last_price_date = date_str
            avg_iv = float(np.mean([q.implied_volatility for q in chain if q.implied_volatility > 0]))
            self.iv_window.append(avg_iv)
            logger.info("[%s] Daily price tracked: $%.0f | Chain IV avg: %.1f%%",
                        date_str, spot, avg_iv * 100)

        iv_pct = iv_percentile(self.iv_window)

        # ── Exit check ────────────────────────────────────────────────
        if self.open_spread and self.open_spread.status == PositionStatus.OPEN:
            self._check_exit(quote_map, now, spot)

        # ── Entry check (any weekday, once per day, at/after ENTRY_HOUR) ─
        is_weekday     = (now.weekday() < 5)   # Mon-Fri
        after_entry_hr = (now.hour >= ENTRY_HOUR)
        not_entered    = (self._last_entry_date != date_str)

        if is_weekday and after_entry_hr and not_entered and self.open_spread is None:
            self._check_entry(chain, quote_map, spot, iv_pct, now)

    # ------------------------------------------------------------------
    def _check_exit(self, quote_map: dict, now: datetime, spot: float) -> None:
        spread = self.open_spread
        spread_type = spread.__dict__.get("spread_type", "bull_put")

        if spread_type == "iron_condor":
            reason = ic_check_exit(spread, quote_map, ic_strat_cfg, as_of=now)
        else:
            current_signal = get_ema_signal(self.price_history, cfg.fast_ema, cfg.slow_ema)
            reason = ema_check_exit(spread, quote_map, cfg, as_of=now,
                                    current_signal=current_signal)

        if reason:
            logger.info("EXIT triggered: %s | Closing %s...", reason, spread.id)
            try:
                self.broker.close_condor(spread, reason=reason)
                pnl = spread.realized_pnl or 0.0
                pnl_usd = pnl * spread.underlying_price_at_entry
                logger.info("  Closed %s | PnL: %+.5f ETH (~$%+.2f) | Reason: %s",
                            spread.id, pnl, pnl_usd, reason)
                self.risk.record_pnl(pnl)
                log_trade_csv(spread, reason, spot)
                self.alerter.trade_closed(spread, spot=spot)
                self.open_spread = None
                save_state(None)
            except Exception as exc:
                logger.error("Failed to close position: %s", exc)

    # ------------------------------------------------------------------
    def _check_entry(self, chain: list[OptionQuote], quote_map: dict,
                     spot: float, iv_pct: float, now: datetime) -> None:
        logger.info("-- Entry check at $%.0f (IV pct: %.0f%%) --", spot, iv_pct)

        signal = get_ema_signal(self.price_history, cfg.fast_ema, cfg.slow_ema)
        strength = abs(ema_trend_strength(self.price_history, cfg.slow_ema))

        logger.info("  EMA signal: %s | strength: %.2f%%", signal, strength * 100)

        spread = None
        spread_type_str = ""

        # Try directional spread first
        if signal != "neutral" and iv_pct >= cfg.iv_percentile_min and strength >= cfg.min_trend_strength:
            strikes = select_spread_strikes(chain, signal, cfg, as_of=now)
            if strikes:
                spread = build_spread(strikes, quantity=1.0, fill_model="mid")
                spread_type_str = strikes["spread_type"]
                logger.info("  Signal: %s -> %s | Strikes: %s",
                            signal, spread_type_str,
                            {k: v.strike for k, v in strikes.items() if k != "spread_type"})
            else:
                logger.info("  Could not find directional strikes")

        # IC fallback
        if spread is None and cfg.condor_on_low_iv:
            skip_reason = (
                "neutral signal" if signal == "neutral"
                else f"IV pct {iv_pct:.0f}% < {cfg.iv_percentile_min}%" if iv_pct < cfg.iv_percentile_min
                else f"trend too weak ({strength*100:.2f}%)"
            )
            logger.info("  Directional skip (%s) -> trying Iron Condor fallback", skip_reason)
            ic_strikes = ic_select_strikes(chain, ic_strat_cfg, as_of=now)
            if ic_strikes:
                spread = ic_build_condor(ic_strikes, quantity=1.0, fill_model="mid")
                spread.__dict__["spread_type"] = "iron_condor"
                spread_type_str = "iron_condor"
                logger.info("  IC strikes: SC=%.0f LC=%.0f SP=%.0f LP=%.0f",
                            ic_strikes["short_call"].strike, ic_strikes["long_call"].strike,
                            ic_strikes["short_put"].strike,  ic_strikes["long_put"].strike)
            else:
                logger.info("  Could not find IC strikes either — skipping entry")

        if spread is None:
            self._last_entry_date = now.strftime("%Y-%m-%d")
            return

        # Risk check
        try:
            account = self.broker.get_account_state()
            self.risk.check_new_trade(spread, 0, account)
        except RiskViolation as e:
            logger.warning("  Risk check blocked entry: %s", e)
            self._last_entry_date = now.strftime("%Y-%m-%d")
            return

        # Place orders
        try:
            orders = self.broker.open_condor(spread)
            filled = sum(1 for o in orders if o.status.value in ("filled", "partially_filled"))
            logger.info("  Opened %s | %d/%d legs filled | credit=%.5f ETH | max_loss=%.2f ETH",
                        spread_type_str, filled, len(orders),
                        spread.credit_received, spread.max_loss)
            self.open_spread = spread
            self._last_entry_date = now.strftime("%Y-%m-%d")
            save_state(spread)
            self.alerter.trade_opened(spread, spot=spot)
        except Exception as exc:
            logger.error("  Failed to open position: %s", exc)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Load config (env vars override config.yaml)
    app_cfg = load_config()
    deribit_cfg = app_cfg.deribit

    client_id     = deribit_cfg.client_id
    client_secret = deribit_cfg.client_secret

    if not client_id or not client_secret:
        print("\n  ERROR: Deribit API credentials not found.")
        print("  Set them via environment variables:")
        print("    DERIBIT_CLIENT_ID=<your_client_id>")
        print("    DERIBIT_CLIENT_SECRET=<your_client_secret>")
        print("  Or edit config/config.yaml → deribit.client_id / client_secret\n")
        sys.exit(1)

    # Always use testnet for live bot
    api_url = TESTNET_URL
    print("=" * 60)
    print("  EMA Hybrid Strategy — Deribit Testnet")
    print(f"  API URL  : {api_url}")
    print(f"  Client ID: {client_id[:6]}***")
    print(f"  Account  : ${ACCOUNT_SIZE:.0f}")
    print(f"  DTE      : 7 DTE (5-9 days)")
    print(f"  Entry    : Any weekday at {ENTRY_HOUR:02d}:00 UTC (when flat)")
    print(f"  Poll     : every {POLL_SECONDS}s")
    print("=" * 60)

    # Verify connectivity + credentials
    client = DeribitRESTClient(
        base_url=api_url,
        client_id=client_id,
        client_secret=client_secret,
    )
    try:
        account = client.get_account_summary("ETH")
        balance = account.get("balance", 0.0)
        equity  = account.get("equity", 0.0)
        print(f"\n  Connected to Deribit Testnet")
        print(f"  ETH Balance : {balance:.4f} ETH")
        print(f"  ETH Equity  : {equity:.4f} ETH")
    except Exception as exc:
        print(f"\n  ERROR: Could not authenticate with Deribit: {exc}")
        print("  Check your client_id and client_secret.")
        sys.exit(1)

    exec_cfg  = app_cfg.execution
    broker    = DeribitBroker(client=client, config=exec_cfg, currency="ETH")
    ingestion = DataIngestionService(client=client, currency="ETH")

    # Notifications (Telegram and/or WhatsApp)
    mon_cfg = app_cfg.monitoring if hasattr(app_cfg, "monitoring") else None

    tg_token  = os.environ.get("TELEGRAM_TOKEN",   getattr(mon_cfg, "telegram_token",   ""))
    tg_chatid = os.environ.get("TELEGRAM_CHAT_ID", getattr(mon_cfg, "telegram_chat_id", ""))
    telegram = TelegramNotifier(tg_token, tg_chatid) if tg_token and tg_chatid else None
    if telegram:
        print(f"  Telegram : notifications enabled (chat_id={tg_chatid})")
    else:
        print("  Telegram : not configured")

    wa_phone  = os.environ.get("WHATSAPP_PHONE",  getattr(mon_cfg, "whatsapp_phone",  ""))
    wa_apikey = os.environ.get("WHATSAPP_APIKEY", getattr(mon_cfg, "whatsapp_apikey", ""))
    whatsapp = WhatsAppNotifier(wa_phone, wa_apikey) if wa_phone and wa_apikey else None
    if whatsapp:
        print(f"  WhatsApp : notifications enabled -> +{wa_phone}")
    else:
        print("  WhatsApp : not configured")

    alerter = AlertManager(telegram=telegram, whatsapp=whatsapp)

    trader = EMALiveTrader(broker=broker, ingestion=ingestion, alerter=alerter)
    trader.run()
