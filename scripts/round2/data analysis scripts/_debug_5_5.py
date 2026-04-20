"""Debug the weird buy_edge = -1710 in 5/5 run."""
import json
from pathlib import Path

LOG = Path('/Users/markiejr/Propserity_4/data/ROUND2/ROUND_2_LOGS/offsets_5_5_logs/347857/347857.log')
obj = json.loads(LOG.read_text())

# Parse activities
lines = obj['activitiesLog'].strip().split('\n')
header = lines[0].split(';')
rows = [dict(zip(header, line.split(';'))) for line in lines[1:] if len(line.split(';')) >= len(header)]

# Build osm mid timeline
osm_mid = {}
for r in rows:
    if r['product'] == 'ASH_COATED_OSMIUM' and r['mid_price']:
        osm_mid[int(r['timestamp'])] = float(r['mid_price'])

# Get our buys
trades = []
for k, v in obj.items():
    if isinstance(v, list) and v and isinstance(v[0], dict) and 'buyer' in v[0]:
        trades = v; break
our_buys = [t for t in trades if t['buyer'] == 'SUBMISSION' and t['symbol'] == 'ASH_COATED_OSMIUM']

print(f"OSM mids: {len(osm_mid)} timestamps, range {min(osm_mid.values()):.2f} to {max(osm_mid.values()):.2f}")
print(f"\nPer-buy edge probe (lag=500):")
for t in our_buys:
    ts = t['timestamp']
    p = t['price']
    q = t['quantity']
    m = osm_mid.get(ts + 500)
    edge = (m - p) * q if m is not None else None
    print(f"  ts={ts:>5}  price={p:>6}  qty={q:>3}  mid(+500)={m}  edge={edge}")
