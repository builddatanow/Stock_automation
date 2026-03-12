import requests
from datetime import datetime, timezone, timedelta

r = requests.get("https://www.deribit.com/api/v2/public/get_instruments?currency=BTC&kind=option&expired=false")
instruments = r.json()["result"]

now = datetime.now(timezone.utc)
cutoff = now + timedelta(days=3)

near = []
for inst in instruments:
    exp_ts = inst["expiration_timestamp"] / 1000
    exp_dt = datetime.fromtimestamp(exp_ts, tz=timezone.utc)
    if exp_dt <= cutoff:
        near.append({"name": inst["instrument_name"], "exp": exp_dt.strftime("%Y-%m-%d"), "strike": inst["strike"]})

near.sort(key=lambda x: (x["exp"], x["strike"]))
strikes_by_exp = {}
for n in near:
    strikes_by_exp.setdefault(n["exp"], set()).add(n["strike"])

for exp, strikes in sorted(strikes_by_exp.items()):
    strikes = sorted(strikes)
    diffs = [strikes[i] - strikes[i-1] for i in range(1, len(strikes))]
    min_gap = min(diffs) if diffs else 0
    max_gap = max(diffs) if diffs else 0
    print(f"{exp} | {len(strikes)} strikes | spacing min=${min_gap:,.0f}  max=${max_gap:,.0f} | range ${strikes[0]:,.0f}–${strikes[-1]:,.0f}")
