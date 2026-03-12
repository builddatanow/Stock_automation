import sys, warnings, logging
sys.path.insert(0, ".")
warnings.filterwarnings("ignore")
logging.disable(logging.CRITICAL)

from config.settings import load_config
from src.deribit.rest_client import DeribitRESTClient
from src.data.ingestion import DataIngestionService
from src.strategy.ema_spread import EMASpreadConfig, select_spread_strikes
from datetime import datetime, timezone

cfg_app = load_config().deribit
client = DeribitRESTClient("https://test.deribit.com", cfg_app.client_id, cfg_app.client_secret)
ingestion = DataIngestionService(client, "ETH")
chain = ingestion.fetch_snapshot()
now = datetime.now(timezone.utc)
spot = chain[0].underlying_price

print(f"Spot: ${spot:.0f} | Chain: {len(chain)} quotes | Time: {now.strftime('%H:%M UTC')}")

# Show available 0-2 DTE options
dte2 = [q for q in chain if (q.expiry - now).total_seconds() / 86400 <= 2.0]
print(f"0-2 DTE options: {len(dte2)}")
expiries = sorted(set(q.expiry.date() for q in dte2))
for exp in expiries:
    sub = [q for q in dte2 if q.expiry.date() == exp]
    puts  = sorted([q for q in sub if q.option_type.value == "put"],  key=lambda q: q.strike)
    calls = sorted([q for q in sub if q.option_type.value == "call"], key=lambda q: q.strike)
    dte_h = (sub[0].expiry - now).total_seconds() / 3600
    print(f"\n  Expiry {exp} (~{dte_h:.1f}h): {len(puts)} puts, {len(calls)} calls")
    for q in puts:
        print(f"    PUT  {q.strike:.0f}  d={q.delta:+.3f}  bid={q.bid:.5f}")
    for q in calls:
        print(f"    CALL {q.strike:.0f}  d={q.delta:+.3f}  bid={q.bid:.5f}")

print()
cfg = EMASpreadConfig(
    target_dte_min=0, target_dte_max=2,
    short_delta_min=0.20, short_delta_max=0.35,
    wing_delta_min=0.08, wing_delta_max=0.15,
    fast_ema=9, slow_ema=21, take_profit_pct=0.50,
    stop_loss_multiplier=1.5, close_dte=0, iv_percentile_min=10.0,
    min_trend_strength=0.003, condor_on_low_iv=False,
    account_size=2200.0, max_risk_per_trade_pct=0.20,
)

for signal in ["bullish", "bearish"]:
    strikes = select_spread_strikes(chain, signal, cfg, as_of=now)
    if strikes:
        s = strikes["short"]
        l = strikes["long"]
        credit = s.bid - l.ask
        print(f"{signal.upper()}: {strikes['spread_type']}")
        print(f"  Short: {s.instrument_name}  d={s.delta:.3f}  bid={s.bid:.5f}")
        print(f"  Long : {l.instrument_name}  d={l.delta:.3f}  ask={l.ask:.5f}")
        print(f"  Credit: {credit:.5f} ETH  (~${credit*spot:.2f})")
    else:
        print(f"{signal.upper()}: no strikes found")
