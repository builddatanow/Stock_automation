"""
Test: Open One Position on Deribit Testnet
==========================================
Fetches live option chain, picks the best trade using the EMA hybrid
strategy, and opens ONE position on your demo account.

Run: python test_open_position.py
"""

import os, sys, time, logging
sys.path.insert(0, ".")

from datetime import datetime, timezone

import requests

from src.deribit.rest_client import DeribitRESTClient
from src.execution.deribit_broker import DeribitBroker
from src.data.ingestion import DataIngestionService
from src.risk.risk_manager import RiskManager, RiskViolation
from src.strategy.ema_spread import (
    EMASpreadConfig, get_ema_signal, ema_trend_strength,
    select_spread_strikes, build_spread,
)
from src.strategy.weekly_iron_condor import (
    select_strikes as ic_select_strikes,
    build_condor as ic_build_condor,
)
from config.settings import StrategyConfig as ICStrategyConfig, RiskConfig, load_config
from src.monitoring.logger import setup_logging

os.makedirs("logs", exist_ok=True)
setup_logging("INFO", "logs/test_open.log")
logger = logging.getLogger(__name__)

MAINNET_URL  = "https://www.deribit.com"
TESTNET_URL  = "https://test.deribit.com"
ACCOUNT_SIZE = 2200.0

# -- Strategy config ----------------------------------------------------------

cfg = EMASpreadConfig(
    fast_ema=9, slow_ema=21,
    target_dte_min=5, target_dte_max=10,
    short_delta_min=0.20, short_delta_max=0.30,
    wing_delta_min=0.08,  wing_delta_max=0.12,
    take_profit_pct=0.50, stop_loss_multiplier=1.5,
    close_dte=1,
    iv_percentile_min=10.0, min_trend_strength=0.003,
    condor_on_low_iv=True,
    ic_short_delta_min=0.15, ic_short_delta_max=0.25,
    ic_wing_delta_min=0.05,  ic_wing_delta_max=0.10,
    account_size=ACCOUNT_SIZE, max_risk_per_trade_pct=0.20,
)

ic_cfg = ICStrategyConfig(
    target_dte_min=cfg.target_dte_min, target_dte_max=cfg.target_dte_max,
    short_delta_min=cfg.ic_short_delta_min, short_delta_max=cfg.ic_short_delta_max,
    wing_delta_min=cfg.ic_wing_delta_min,   wing_delta_max=cfg.ic_wing_delta_max,
    take_profit_pct=0.50, stop_loss_multiplier=1.5, close_dte=1,
    iv_percentile_min=0.0, max_daily_move_pct=100.0,
)

risk_cfg = RiskConfig(
    account_size=ACCOUNT_SIZE,
    max_risk_per_trade_pct=cfg.max_risk_per_trade_pct,
    max_open_positions=1,
    daily_loss_limit_pct=0.10,
)

# -- Helpers ------------------------------------------------------------------

def fetch_price_history(days: int = 30) -> list[float]:
    """Fetch real ETH daily closes from mainnet (public endpoint)."""
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
        print(f"  WARNING: Could not fetch price history: {exc}")
        return []

# -- Main ---------------------------------------------------------------------

def main():
    print("=" * 60)
    print("  Test: Open One Position on Deribit Testnet")
    print("=" * 60)

    # Load credentials
    app_cfg       = load_config()
    client_id     = app_cfg.deribit.client_id
    client_secret = app_cfg.deribit.client_secret

    if not client_id or not client_secret:
        print("\n  ERROR: API credentials missing.")
        print("  Set DERIBIT_CLIENT_ID and DERIBIT_CLIENT_SECRET env vars")
        print("  or edit config/config.yaml\n")
        sys.exit(1)

    print(f"\n  Connecting to {TESTNET_URL} ...")
    client = DeribitRESTClient(
        base_url=TESTNET_URL,
        client_id=client_id,
        client_secret=client_secret,
    )

    # Verify auth + show account
    try:
        acct = client.get_account_summary("ETH")
    except Exception as exc:
        print(f"\n  ERROR: Auth failed — {exc}")
        sys.exit(1)

    balance = acct.get("balance", 0.0)
    equity  = acct.get("equity",  0.0)
    print(f"  Auth OK")
    print(f"  ETH Balance : {balance:.4f} ETH")
    print(f"  ETH Equity  : {equity:.4f} ETH")

    # Fetch option chain
    print("\n  Fetching live option chain from testnet...")
    ingestion = DataIngestionService(client=client, currency="ETH")
    chain = ingestion.fetch_snapshot()
    if not chain:
        print("  ERROR: Empty option chain — try again later.")
        sys.exit(1)

    spot = chain[0].underlying_price or ingestion.fetch_underlying_price()
    print(f"  Got {len(chain)} option quotes | ETH spot: ${spot:.0f}")

    # EMA signal from real price history
    print("\n  Computing EMA signal from last 30 days of real ETH prices...")
    price_history = fetch_price_history(days=30)
    if price_history:
        print(f"  Price history: {len(price_history)} days | Latest: ${price_history[-1]:.0f}")
    else:
        print("  No price history — defaulting to neutral signal (will use IC)")

    now    = datetime.now(timezone.utc)
    signal = get_ema_signal(price_history, cfg.fast_ema, cfg.slow_ema) if price_history else "neutral"
    strength = abs(ema_trend_strength(price_history, cfg.slow_ema)) if price_history else 0.0
    print(f"  EMA signal : {signal.upper()}  (strength: {strength*100:.2f}%)")

    # Pick trade
    spread      = None
    spread_label = ""

    # Try directional spread
    if signal != "neutral" and strength >= cfg.min_trend_strength:
        strikes = select_spread_strikes(chain, signal, cfg, as_of=now)
        if strikes:
            spread = build_spread(strikes, quantity=1.0, fill_model="mid")
            spread_label = strikes["spread_type"].replace("_", " ").title()
            s = strikes["short"]
            l = strikes["long"]
            print(f"\n  Trade type : {spread_label}")
            print(f"  Short leg  : {s.instrument_name}  d={s.delta:+.2f}  bid={s.bid:.5f}")
            print(f"  Long  leg  : {l.instrument_name}  d={l.delta:+.2f}  ask={l.ask:.5f}")
        else:
            print("  Could not find directional strikes — falling back to Iron Condor")

    # IC fallback
    if spread is None:
        print(f"\n  Signal is {signal} / weak trend — using Iron Condor")
        ic_strikes = ic_select_strikes(chain, ic_cfg, as_of=now)
        if not ic_strikes:
            print("  ERROR: Could not find Iron Condor strikes either. Try different DTE range.")
            sys.exit(1)
        spread = ic_build_condor(ic_strikes, quantity=1.0, fill_model="mid")
        spread.__dict__["spread_type"] = "iron_condor"
        spread_label = "Iron Condor"
        print(f"  Trade type : Iron Condor")
        print(f"  Short Call : {ic_strikes['short_call'].instrument_name}  d={ic_strikes['short_call'].delta:+.2f}")
        print(f"  Long  Call : {ic_strikes['long_call'].instrument_name}  d={ic_strikes['long_call'].delta:+.2f}")
        print(f"  Short Put  : {ic_strikes['short_put'].instrument_name}  d={ic_strikes['short_put'].delta:+.2f}")
        print(f"  Long  Put  : {ic_strikes['long_put'].instrument_name}  d={ic_strikes['long_put'].delta:+.2f}")

    print(f"\n  Credit     : {spread.credit_received:.5f} ETH  (~${spread.credit_received*spot:.2f})")
    print(f"  Max loss   : {spread.max_loss:.4f} ETH  (~${spread.max_loss*spot:.2f})")
    print(f"  Take-profit: {spread.credit_received*cfg.take_profit_pct:.5f} ETH (50% of credit)")
    print(f"  Stop-loss  : {spread.credit_received*cfg.stop_loss_multiplier:.5f} ETH (1.5× credit)")

    # Risk check
    broker = DeribitBroker(client=client, config=app_cfg.execution, currency="ETH")
    risk   = RiskManager(risk_cfg)
    try:
        risk.check_new_trade(spread, 0, broker.get_account_state())
    except RiskViolation as e:
        print(f"\n  RISK CHECK FAILED: {e}")
        sys.exit(1)
    print(f"\n  Risk check : PASSED")

    # Confirm
    print("\n" + "-" * 60)
    confirm = input(f"  Open this {spread_label} on Deribit TESTNET? [y/N] ").strip().lower()
    if confirm != "y":
        print("  Aborted — no orders placed.")
        sys.exit(0)

    # Place orders
    print(f"\n  Placing orders on testnet...")
    try:
        orders = broker.open_condor(spread)
    except Exception as exc:
        print(f"\n  ERROR placing orders: {exc}")
        sys.exit(1)

    print(f"\n  Orders submitted: {len(orders)}")
    print("-" * 60)
    for i, o in enumerate(orders, 1):
        status = o.status.value if hasattr(o.status, "value") else str(o.status)
        bid_id = o.broker_order_id or "n/a"
        print(f"  [{i}] {o.instrument_name}")
        print(f"       Side: {o.side.value}  Qty: {o.quantity}  Price: {o.price:.5f}")
        print(f"       Status: {status}  Fill: {o.filled_quantity}@{o.avg_fill_price:.5f}")
        print(f"       Order ID: {bid_id}")

    filled = sum(1 for o in orders if o.status.value in ("filled", "partially_filled"))
    print("-" * 60)
    print(f"\n  {filled}/{len(orders)} legs filled")
    print(f"  Position ID : {spread.id}")
    print(f"  Credit rcvd : {spread.credit_received:.5f} ETH")
    print(f"\n  Position is now OPEN on your Deribit testnet account.")
    print(f"  Run  python run_live.py  to start monitoring and auto-exit.\n")

    # Save state so run_live.py can pick up the position
    import json, os
    os.makedirs("data", exist_ok=True)
    spread_type = spread.__dict__.get("spread_type", "")
    state = {
        "id": spread.id, "spread_type": spread_type,
        "entry_time": spread.entry_time.isoformat(),
        "credit": spread.credit_received, "max_loss": spread.max_loss,
        "spot_at_entry": spread.underlying_price_at_entry,
        "legs": [
            {"instrument_name": leg.instrument_name, "strike": leg.strike,
             "option_type": leg.option_type.value, "side": leg.side.value,
             "quantity": leg.quantity, "entry_price": leg.entry_price,
             "expiry": leg.expiry.isoformat()}
            for leg in spread.legs if leg.quantity > 0 and leg.instrument_name != "STUB"
        ],
    }
    with open("data/live_state.json", "w") as f:
        json.dump(state, f, indent=2)
    print(f"  State saved -> data/live_state.json")


if __name__ == "__main__":
    main()
