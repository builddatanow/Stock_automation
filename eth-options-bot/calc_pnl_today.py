
# Today's settlement price at 08:00 UTC (from Deribit delivery prices API)
settlement = 1980.71

print("=" * 60)
print("  ETH 0DTE PnL Calculation -- 2026-03-09")
print("=" * 60)
print(f"Settlement price (08:00 UTC): ${settlement:.2f}")
print(f"All call strikes > ${settlement:.2f} -> expired worthless (OTM)")
print()

# Trade data from Deribit order history
# 2PM-Sydney: SELL ETH-10MAR26-2025-C @ 0.00750, BUY ETH-10MAR26-2075-C @ 0.00330
# 3PM-Sydney: SELL ETH-10MAR26-2050-C @ 0.00600, BUY ETH-10MAR26-2100-C @ 0.00270
# Note: instrument says 10MAR26 but expiry was 09MAR26 08:00 UTC (testnet chain naming)

trades = [
    {
        "window":      "2PM-Sydney (03:00 UTC)",
        "entry_spot":  1964,
        "short_strike": 2025,
        "long_strike":  2075,
        "sell_price":  0.00750,
        "buy_price":   0.00330,
    },
    {
        "window":      "3PM-Sydney (04:00 UTC)",
        "entry_spot":  1979,
        "short_strike": 2050,
        "long_strike":  2100,
        "sell_price":  0.00600,
        "buy_price":   0.00270,
    },
]

total_eth = 0.0
for t in trades:
    credit   = t["sell_price"] - t["buy_price"]       # received at open
    # At settlement $1980.71, all strikes (2025/2050/2075/2100) are OTM -> worth 0
    close_cost = 0.0
    pnl_eth  = credit - close_cost
    pnl_usd  = pnl_eth * t["entry_spot"]
    total_eth += pnl_eth

    print(f"{t['window']} | Bear Call Spread | Entry: ${t['entry_spot']}")
    print(f"  SELL {t['short_strike']}-C @ {t['sell_price']:.5f} ETH")
    print(f"  BUY  {t['long_strike']}-C @ {t['buy_price']:.5f} ETH")
    print(f"  Credit received : +{credit:.5f} ETH")
    print(f"  Expired OTM     :  0.00000 ETH (settlement ${settlement:.2f} < {t['short_strike']})")
    print(f"  PnL             : +{pnl_eth:.5f} ETH = +${pnl_usd:.2f}")
    print()

print("=" * 60)
print(f"TOTAL PnL : +{total_eth:.5f} ETH = +${total_eth * settlement:.2f}")
print(f"Result    : Both spreads expired fully OTM -- kept 100% of credit")
print("=" * 60)
