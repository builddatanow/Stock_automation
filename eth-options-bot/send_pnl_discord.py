import json, requests

webhook = "https://discord.com/api/webhooks/1480698774736076820/9i3IVBM0ik3TBcHLenoAeTyx1E-yDvC7IE8tmnlzsCZcYo64aPUBjk6qX35fq73Yw9Dv"

msg = """**ETH 0DTE P&L Summary — 2026-03-09**

Settlement price: **$1,980.71** at 08:00 UTC

**2PM Sydney | Bear Call Spread**
  Sell 2025-C @ 0.00750 ETH | Buy 2075-C @ 0.00330 ETH
  Settlement $1,980.71 < 2025 → Expired OTM
  PnL: **+0.00420 ETH = +$8.25**

**3PM Sydney | Bear Call Spread**
  Sell 2050-C @ 0.00600 ETH | Buy 2100-C @ 0.00270 ETH
  Settlement $1,980.71 < 2050 → Expired OTM
  PnL: **+0.00330 ETH = +$6.53**

**Total: +0.00750 ETH = +$14.86**
Both spreads expired fully OTM — kept 100% of credit"""

resp = requests.post(webhook, json={"content": msg}, timeout=10)
print("Sent!" if resp.ok else f"Failed: {resp.status_code} {resp.text}")
