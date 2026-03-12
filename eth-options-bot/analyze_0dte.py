import pandas as pd
import numpy as np
import sys

AVG_SPOT = 3051.0
TYPE_MAP = {'IronCond': 'Iron Condor', 'BullPut ': 'Bull Put Spread', 'BearCall': 'Bear Call Spread'}

def analyze(csv_path, label):
    df = pd.read_csv(csv_path)
    df['pnl'] = df['PnL ETH'].astype(float)
    df['win'] = df['pnl'] > 0
    df['TradeType'] = df['Type'].map(TYPE_MAP).fillna(df['Type'].str.strip())

    print('\n' + '=' * 62)
    print(f'  {label} -- Success Rate by Strategy Type')
    print('=' * 62)

    summary_rows = []
    for ttype, grp in df.groupby('TradeType'):
        wins   = grp[grp['win']]['pnl']
        losses = grp[~grp['win']]['pnl']
        total  = len(grp)
        win_n  = len(wins)
        win_r  = win_n / total * 100
        avg_w  = wins.mean() if len(wins) else 0
        avg_l  = losses.mean() if len(losses) else 0
        net    = grp['pnl'].sum()
        ls     = losses.sum()
        pf     = abs(wins.sum() / ls) if (len(losses) > 0 and ls != 0) else float('inf')
        summary_rows.append({
            'Type': ttype, 'Trades': total,
            'Win%': win_r, 'W': win_n, 'L': total - win_n,
            'AvgWin$': avg_w * AVG_SPOT, 'AvgLoss$': avg_l * AVG_SPOT,
            'NetETH': net, 'Net$': net * AVG_SPOT, 'PF': pf,
        })
        print(f'\n  {ttype}')
        print(f'    Trades        : {total}')
        print(f'    Win rate      : {win_r:.1f}%  ({win_n}W / {total - win_n}L)')
        print(f'    Avg win       : +{avg_w:.5f} ETH  (~${avg_w * AVG_SPOT:+.2f})')
        print(f'    Avg loss      :  {avg_l:.5f} ETH  (~${avg_l * AVG_SPOT:.2f})')
        print(f'    Net PnL (ETH) : {net:+.5f}  (~${net * AVG_SPOT:+.0f})')
        print(f'    Profit factor : {pf:.2f}')

    total = len(df)
    wins  = df['win'].sum()
    net   = df['pnl'].sum()
    print('\n' + '-' * 62)
    print(f'  TOTAL  {total} trades | {wins}W / {total-wins}L | {wins/total*100:.1f}% win rate')
    print(f'  Net PnL: {net:+.5f} ETH  (~${net * AVG_SPOT:+.0f})')
    print('=' * 62)
    return pd.DataFrame(summary_rows)


datasets = [
    ('data/0dte_backtest/trade_history.csv',  '0 DTE'),
    ('data/3dte_backtest/trade_history.csv',  '3 DTE'),
    ('data/7dte_backtest/trade_history.csv',  '7 DTE'),
]

all_summaries = {}
for path, label in datasets:
    try:
        all_summaries[label] = analyze(path, label)
    except FileNotFoundError:
        print(f'\n  [SKIP] {label}: file not found ({path})')

# Side-by-side win rate comparison
if len(all_summaries) > 1:
    print('\n\n' + '=' * 62)
    print('  WIN RATE COMPARISON  (all DTE)')
    print('=' * 62)
    print(f'  {"Strategy":<20}', end='')
    for lbl in all_summaries:
        print(f'  {lbl:>8}', end='')
    print()
    print('  ' + '-' * 50)
    for stype in ['Bear Call Spread', 'Bull Put Spread', 'Iron Condor']:
        print(f'  {stype:<20}', end='')
        for lbl, sdf in all_summaries.items():
            row = sdf[sdf['Type'] == stype]
            val = f"{row['Win%'].values[0]:.1f}%" if len(row) else '  n/a'
            print(f'  {val:>8}', end='')
        print()
    print()
    print(f'  {"Net PnL (USD)":<20}', end='')
    for lbl, sdf in all_summaries.items():
        net = sdf['Net$'].sum()
        print(f'  {f"${net:+.0f}":>8}', end='')
    print()
    print('=' * 62)
