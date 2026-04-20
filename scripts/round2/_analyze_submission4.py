"""Analyze submission4 live log vs. p2bt backtest."""
import json, re
from pathlib import Path
from collections import defaultdict

LOG = Path('/Users/markiejr/Propserity_4/data/ROUND2/ROUND_2_LOGS/submission4logs/344997.log')

# The .log is a JSON with activitiesLog (csv) + trade history (JSON list)
raw = LOG.read_text()
obj = json.loads(raw)

# ─── Parse activity log (per-product PnL over time) ─────────────────────────
lines = obj['activitiesLog'].strip().split('\n')
header = lines[0].split(';')
rows = []
for line in lines[1:]:
    parts = line.split(';')
    if len(parts) < len(header): continue
    rows.append(dict(zip(header, parts)))

# Last PnL per (day, product)
by_day_prod = defaultdict(dict)
for r in rows:
    day = int(r['day'])
    ts = int(r['timestamp'])
    p = r['product']
    pnl = float(r['profit_and_loss']) if r['profit_and_loss'] else 0.0
    mid = float(r['mid_price']) if r['mid_price'] else 0.0
    by_day_prod[(day, p)] = (ts, pnl, mid)

print("=== FINAL PnL (live) ===")
total_live = 0.0
for (day, p), (ts, pnl, mid) in sorted(by_day_prod.items()):
    print(f"  day {day} {p:25s} @ts {ts:>6}: PnL={pnl:>10,.2f}  mid={mid}")
    total_live += pnl
print(f"  TOTAL:        {total_live:>10,.2f}")

# ─── Parse trade history ────────────────────────────────────────────────────
# activitiesLog was just csv; the trade history is in raw JSON after that.
# Actually the .log is entirely JSON — let me find trade keys
keys = list(obj.keys())
print(f"\nLog JSON keys: {keys}")

# Find trades list (any value that's a list of dicts w/ buyer/seller)
trades = []
for k, v in obj.items():
    if isinstance(v, list) and v and isinstance(v[0], dict) and 'buyer' in v[0]:
        trades = v
        break

# Our fills = trades where buyer or seller is 'SUBMISSION'
our_buys = [t for t in trades if t['buyer'] == 'SUBMISSION']
our_sells = [t for t in trades if t['seller'] == 'SUBMISSION']

print(f"\n=== OUR FILLS ===")
print(f"  Buys:  {len(our_buys)}")
print(f"  Sells: {len(our_sells)}")

# By product
buys_by_prod = defaultdict(list)
sells_by_prod = defaultdict(list)
for t in our_buys:  buys_by_prod[t['symbol']].append(t)
for t in our_sells: sells_by_prod[t['symbol']].append(t)

for prod in sorted(set(list(buys_by_prod) + list(sells_by_prod))):
    b = buys_by_prod.get(prod, [])
    s = sells_by_prod.get(prod, [])
    bq = sum(t['quantity'] for t in b)
    sq = sum(t['quantity'] for t in s)
    bv = sum(t['price']*t['quantity'] for t in b)
    sv = sum(t['price']*t['quantity'] for t in s)
    avg_buy = bv/bq if bq else 0
    avg_sell = sv/sq if sq else 0
    realized = sv - bv
    print(f"\n  {prod}:")
    print(f"    Buys:  {len(b)} fills, qty={bq}, avg={avg_buy:.2f}")
    print(f"    Sells: {len(s)} fills, qty={sq}, avg={avg_sell:.2f}")
    print(f"    Net qty: {bq - sq:+d}  (positive = still long)")
    print(f"    Realized cash flow: {realized:,.2f}")

# ─── Compare to p2bt prediction ──────────────────────────────────────────────
print(f"\n=== COMPARISON ===")
print(f"  Live total PnL:           {total_live:>10,.2f}")
print(f"  p2bt predicted (gross):     251,494 (3 days)")
print(f"  p2bt predicted per-day avg:  83,831")

# If this log is only 1 day (day 1), compare to day 1 prediction
days_in_log = set(day for (day, _) in by_day_prod)
print(f"  Days in log: {sorted(days_in_log)}")

# Fill-rate check: OSM passive quote size is 10 per side, ~10000 ticks/day
if 'ASH_COATED_OSMIUM' in buys_by_prod or 'ASH_COATED_OSMIUM' in sells_by_prod:
    osm_b = buys_by_prod.get('ASH_COATED_OSMIUM', [])
    osm_s = sells_by_prod.get('ASH_COATED_OSMIUM', [])
    n_days = len(days_in_log)
    expected_ticks = n_days * 10000
    fill_rate_buy = len(osm_b) / expected_ticks if expected_ticks else 0
    fill_rate_sell = len(osm_s) / expected_ticks if expected_ticks else 0
    print(f"\n  OSM fill rate (live, per tick):")
    print(f"    Buy:  {fill_rate_buy:.4f} ({len(osm_b)} fills / {expected_ticks} ticks)")
    print(f"    Sell: {fill_rate_sell:.4f} ({len(osm_s)} fills / {expected_ticks} ticks)")

# ─── Adverse-selection probe: OSM fill price vs subsequent mid ──────────────
# For each OSM fill, look at mid 5 ticks later — did price move against us?
print("\n=== ADVERSE SELECTION PROBE (OSM fills) ===")
# Build timestamp → mid lookup for OSM
osm_mid_by_ts = {}
for r in rows:
    if r['product'] == 'ASH_COATED_OSMIUM':
        osm_mid_by_ts[int(r['timestamp'])] = float(r['mid_price']) if r['mid_price'] else None

def mid_at(ts):
    return osm_mid_by_ts.get(ts)

for label, fills, direction in [('BUYS', buys_by_prod.get('ASH_COATED_OSMIUM', []), +1),
                                 ('SELLS', sells_by_prod.get('ASH_COATED_OSMIUM', []), -1)]:
    if not fills: continue
    pnl_1 = []; pnl_5 = []; pnl_20 = []
    for t in fills:
        ts = t['timestamp']
        price = t['price']
        qty = t['quantity']
        for lag, bucket in [(100, pnl_1), (500, pnl_5), (2000, pnl_20)]:
            m_later = mid_at(ts + lag)
            if m_later is not None:
                # direction=+1 for buys: profit if mid goes up; direction=-1 for sells: profit if mid goes down
                bucket.append(direction * (m_later - price) * qty)
    def avg(xs): return sum(xs)/len(xs) if xs else 0
    print(f"  {label}: n={len(fills)}")
    print(f"    avg edge 100 ticks later:  {avg(pnl_1):>8.2f} per fill  (n={len(pnl_1)})")
    print(f"    avg edge 500 ticks later:  {avg(pnl_5):>8.2f} per fill  (n={len(pnl_5)})")
    print(f"    avg edge 2000 ticks later: {avg(pnl_20):>8.2f} per fill  (n={len(pnl_20)})")
