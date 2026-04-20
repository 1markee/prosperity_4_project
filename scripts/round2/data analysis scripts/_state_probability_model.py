"""State-conditional probability model for OSM next-tick direction.

Goal: at each tick, compute features describing the "state" of the market.
For each unique combination of discretized feature values, measure the
conditional probability that the NEXT tick's mid moves up vs down.

If certain states are strongly directional (e.g. P(up|state X) = 0.7),
we can exploit by biasing our fair-value estimate toward the expected
direction in those states.

Features tested:
  - last_return  (mid[t] − mid[t−1])       : short-term momentum
  - obi          (bid_vol − ask_vol)/total : book imbalance
  - anchor_dev   (mid − EWMA_anchor)       : distance from long-run mean
  - spread       (best_ask − best_bid)     : liquidity regime
  - vol50        stdev of last 50 mids     : volatility regime
  - last_obi     OBI at t−1                : lagged imbalance
  - return_sign_run  consecutive ticks of same sign : trendiness

For each state, reports:
  n         : sample size
  p_up      : P(next mid > current mid)
  edge      : |p_up − 0.5|
  score     : edge × sqrt(n)   (statistical confidence × effect size)
"""
import pandas as pd
import numpy as np
from pathlib import Path

DATA_DIR = Path('/Users/markiejr/Propserity_4/data/ROUND2/ROUND_2_DATA')

# ─── Load + prep ─────────────────────────────────────────────────────────────
dfs = []
for day in [-1, 0, 1]:
    df = pd.read_csv(DATA_DIR / f'prices_round_2_day_{day}.csv', sep=';')
    df = df[df['product'] == 'ASH_COATED_OSMIUM'].copy()
    df['day_key'] = day
    dfs.append(df)
df = pd.concat(dfs, ignore_index=True)
df = df.sort_values(['day_key', 'timestamp']).reset_index(drop=True)

# Features
df['bid_vol'] = df[['bid_volume_1', 'bid_volume_2', 'bid_volume_3']].sum(axis=1, skipna=True)
df['ask_vol'] = df[['ask_volume_1', 'ask_volume_2', 'ask_volume_3']].sum(axis=1, skipna=True)
df['obi']    = (df['bid_vol'] - df['ask_vol']) / (df['bid_vol'] + df['ask_vol'])
df['spread'] = df['ask_price_1'] - df['bid_price_1']

# Use only top-of-book for a cleaner OBI (matches algo)
df['top_obi'] = (df['bid_volume_1'].fillna(0) - df['ask_volume_1'].fillna(0)) / \
                (df['bid_volume_1'].fillna(0) + df['ask_volume_1'].fillna(0))

# Per-day computations so last_return doesn't span days
def per_day(g):
    g = g.copy()
    g['last_return'] = g['mid_price'].diff()
    g['next_return'] = g['mid_price'].diff().shift(-1)
    g['last_obi']    = g['top_obi'].shift(1)
    # Rolling anchor (EWMA, α=0.0001 matches the algo final)
    g['anchor'] = g['mid_price'].ewm(alpha=0.0001, adjust=False).mean()
    g['anchor_dev'] = g['mid_price'] - g['anchor']
    # Rolling std over last 50 ticks
    g['vol50'] = g['mid_price'].rolling(50).std()
    # Run length (consecutive same-sign returns)
    sign = np.sign(g['last_return'].fillna(0))
    run = [0]
    for i in range(1, len(sign)):
        s = sign.iloc[i]
        if s == 0:
            run.append(run[-1])
        elif s == sign.iloc[i-1]:
            run.append(run[-1] + s)
        else:
            run.append(s)
    g['run'] = run
    return g

df = df.groupby('day_key', group_keys=False).apply(per_day, include_groups=False).reset_index(drop=True)
df = df.dropna(subset=['last_return', 'next_return', 'top_obi'])
# Clip outliers (day-boundary artifacts) to realistic OSM tick-move range
df = df[df['next_return'].abs() < 20]
df = df[df['last_return'].abs() < 20]

print(f"Total ticks: {len(df):,}")
print(f"Base rate P(next_return > 0): {(df['next_return'] > 0).mean():.4f}")
print(f"Base rate P(next_return = 0): {(df['next_return'] == 0).mean():.4f}")
print(f"Base rate P(next_return < 0): {(df['next_return'] < 0).mean():.4f}")
print(f"Mean next_return:  {df['next_return'].mean():.4f}")
print(f"Stdev next_return: {df['next_return'].std():.4f}")
P_UP_BASE = (df['next_return'] > 0).mean()

def bucket(s, edges, labels):
    return pd.cut(s, bins=edges, labels=labels, include_lowest=True)

# ─── Discretize features ─────────────────────────────────────────────────────
df['ret_b']    = bucket(df['last_return'], [-100, -2, -1, -0.01, 0.01, 1, 2, 100],
                        ['≤-2', '-1', '-0', '0', '+0', '+1', '≥+2'])
df['obi_b']    = bucket(df['top_obi'], [-1.01, -0.5, -0.1, 0.1, 0.5, 1.01],
                        ['--', '-', '0', '+', '++'])
df['anc_b']    = bucket(df['anchor_dev'], [-100, -5, -2, 2, 5, 100],
                        ['<<', '<', '~', '>', '>>'])
df['vol_b']    = bucket(df['vol50'], [0, 1.3, 1.6, 1.9, 100],
                        ['lo', 'med', 'hi', 'xhi'])
df['spd_b']    = bucket(df['spread'], [0, 4, 6, 100], ['tight', 'mid', 'wide'])

def ep(label, *groupbys, min_n=100):
    agg = df.groupby(list(groupbys), observed=True)['next_return'].agg(
        n='size', p_up=lambda x: (x > 0).mean(), p_dn=lambda x: (x < 0).mean(), mean='mean'
    ).reset_index()
    agg['edge']  = (agg['p_up'] - P_UP_BASE).abs()
    agg['score'] = agg['edge'] * np.sqrt(agg['n'])
    agg = agg[agg['n'] >= min_n].sort_values('score', ascending=False)
    print(f"\n─── {label} ─── (n ≥ {min_n}, base P_up = {P_UP_BASE:.3f})")
    with pd.option_context('display.max_columns', None, 'display.width', 200,
                           'display.float_format', '{:.4f}'.format):
        print(agg.head(20).to_string(index=False))

# ─── Univariate edges ────────────────────────────────────────────────────────
ep("By last-return bucket", 'ret_b', min_n=500)
ep("By top-of-book OBI bucket", 'obi_b', min_n=500)
ep("By anchor deviation bucket", 'anc_b', min_n=500)
ep("By vol bucket", 'vol_b', min_n=500)
ep("By run-length", 'run', min_n=200)

# ─── Bivariate: the pair we care about most ─────────────────────────────────
ep("last_return × OBI", 'ret_b', 'obi_b', min_n=200)
ep("last_return × anchor_dev", 'ret_b', 'anc_b', min_n=200)
ep("OBI × anchor_dev", 'obi_b', 'anc_b', min_n=200)
ep("OBI × vol_b", 'obi_b', 'vol_b', min_n=200)

# ─── Trivariate: the strongest state cube ───────────────────────────────────
ep("last_return × OBI × anchor_dev", 'ret_b', 'obi_b', 'anc_b', min_n=100)

# ─── Best states overall: rank by score × mean-magnitude ─────────────────────
print("\n─── Top states by score (trivariate) ───")
summary = df.groupby(['ret_b', 'obi_b', 'anc_b'], observed=True)['next_return'].agg(
    n='size', p_up=lambda x: (x > 0).mean(), mean='mean'
).reset_index()
summary['edge']  = (summary['p_up'] - P_UP_BASE).abs()
summary['score'] = summary['edge'] * np.sqrt(summary['n'])
summary = summary[summary['n'] >= 100].sort_values('score', ascending=False)
with pd.option_context('display.max_columns', None, 'display.width', 200):
    print(summary.head(15).to_string(index=False))

# ─── Save for inspection ─────────────────────────────────────────────────────
summary.to_csv('/tmp/r2_state_probs.csv', index=False)
print(f"\nFull table saved to /tmp/r2_state_probs.csv ({len(summary)} states)")
