import pandas as pd

df = pd.read_csv('data/7dte_backtest/trade_history.csv')
df['Entry'] = pd.to_datetime(df['Entry'])
df['Exit']  = pd.to_datetime(df['Exit'])
df['hold_days'] = (df['Exit'] - df['Entry']).dt.days
df['gap_after']  = (df['Entry'].shift(-1) - df['Exit']).dt.days  # gap to next trade

print('Total trades  :', len(df))
print('Avg hold (days): %.1f' % df['hold_days'].mean())
print('Min hold (days):', df['hold_days'].min())
print('Max hold (days):', df['hold_days'].max())
print()

print('Gap between close and next open:')
gap_counts = df['gap_after'].dropna().value_counts().sort_index()
for gap, cnt in gap_counts.items():
    print(f'  {int(gap):3d} day(s): {cnt} times')

print()
long_gaps = df[df['gap_after'] > 7].dropna(subset=['gap_after'])
if not long_gaps.empty:
    print('Long gaps (>7 days):')
    print(long_gaps[['Exit','Entry','gap_after','Type']].rename(columns={'Entry':'Next_Entry','gap_after':'Gap'}).to_string(index=False))
else:
    print('No gaps > 7 days -- continuous trading confirmed.')
