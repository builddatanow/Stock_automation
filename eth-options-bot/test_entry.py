"""Force a test entry right now, bypassing the time window check."""
import sys, warnings, logging
sys.path.insert(0, ".")
warnings.filterwarnings("ignore")

from datetime import datetime, timezone
from config.settings import load_config
from src.deribit.rest_client import DeribitRESTClient
from src.execution.deribit_broker import DeribitBroker
from src.data.ingestion import DataIngestionService
from src.strategy.ema_spread import (
    EMASpreadConfig, get_ema_signal, ema_trend_strength,
    select_spread_strikes, build_spread,
)
from src.monitoring.notifier import AlertManager, TelegramNotifier
from src.monitoring.logger import setup_logging

setup_logging("INFO", "logs/live_0dte.log")

app_cfg     = load_config()
deribit_cfg = app_cfg.deribit
mon_cfg     = app_cfg.monitoring

client    = DeribitRESTClient("https://test.deribit.com", deribit_cfg.client_id, deribit_cfg.client_secret)
broker    = DeribitBroker(client=client, config=app_cfg.execution, currency="ETH")
ingestion = DataIngestionService(client=client, currency="ETH")
telegram  = TelegramNotifier(mon_cfg.telegram_token, mon_cfg.telegram_chat_id)
alerter   = AlertManager(telegram=telegram)

cfg = EMASpreadConfig(
    fast_ema=9, slow_ema=21,
    target_dte_min=0, target_dte_max=2,
    short_delta_min=0.20, short_delta_max=0.35,
    wing_delta_min=0.08, wing_delta_max=0.15,
    take_profit_pct=0.50, stop_loss_multiplier=1.5,
    close_dte=0, iv_percentile_min=10.0, min_trend_strength=0.003,
    condor_on_low_iv=False, account_size=2200.0, max_risk_per_trade_pct=0.20,
)

print("Fetching chain...")
chain     = ingestion.fetch_snapshot()
spot      = chain[0].underlying_price
now       = datetime.now(timezone.utc)
print(f"Spot: ${spot:.0f} | {len(chain)} quotes | {now.strftime('%H:%M UTC')}")

# Load price history for EMA signal
from run_live_0dte import fetch_price_history
prices = fetch_price_history(days=30)
signal   = get_ema_signal(prices, cfg.fast_ema, cfg.slow_ema)
strength = abs(ema_trend_strength(prices, cfg.slow_ema))
print(f"EMA Signal: {signal} | Strength: {strength*100:.2f}%")

# Override to bullish for test if neutral
if signal == "neutral":
    print("Signal is neutral -- overriding to 'bullish' for test entry")
    signal = "bullish"

strikes = select_spread_strikes(chain, signal, cfg, as_of=now)
if not strikes:
    print("ERROR: No strikes found even with override. Check chain.")
    sys.exit(1)

spread = build_spread(strikes, quantity=1.0, fill_model="mid")
s = strikes["short"]
l = strikes["long"]
credit = s.bid - l.ask
print(f"\nSpread: {strikes['spread_type'].upper()}")
print(f"  Short: {s.instrument_name}  delta={s.delta:.3f}  bid={s.bid:.5f}")
print(f"  Long : {l.instrument_name}  delta={l.delta:.3f}  ask={l.ask:.5f}")
print(f"  Credit: {credit:.5f} ETH  (~${credit*spot:.2f})")
print(f"  Max loss: {spread.max_loss:.5f} ETH  (~${spread.max_loss*spot:.2f})")

confirm = input("\nPlace this order on TESTNET? (yes/no): ").strip().lower()
if confirm != "yes":
    print("Cancelled.")
    sys.exit(0)

print("\nPlacing orders...")
orders = broker.open_condor(spread)
filled = sum(1 for o in orders if o.status.value in ("filled", "partially_filled"))
print(f"Orders placed: {filled}/{len(orders)} legs filled")
print(f"Credit received: {spread.credit_received:.5f} ETH")

alerter.trade_opened(spread, spot=spot)
print("Telegram notification sent.")
print(f"\nSpread ID: {spread.id}")
print("Monitor with: tail -f logs/live_0dte.log")
