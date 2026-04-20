"""Compare submission4 (3/1), offsets_3_3, offsets_2_2 live logs side-by-side."""
import json
from pathlib import Path
from collections import defaultdict

RUNS = [
    ('3/1 (sub4)',   '/Users/markiejr/Propserity_4/data/ROUND2/ROUND_2_LOGS/submission4logs/344997.log'),
    ('2/2',          '/Users/markiejr/Propserity_4/data/ROUND2/ROUND_2_LOGS/offsets_2_2_logs/345854/345854.log'),
    ('3/3',          '/Users/markiejr/Propserity_4/data/ROUND2/ROUND_2_LOGS/offsets_3_3_logs/345941/345941.log'),
    ('4/4',          '/Users/markiejr/Propserity_4/data/ROUND2/ROUND_2_LOGS/offsets_4_4_logs/346162/346162.log'),
    ('3/5',          '/Users/markiejr/Propserity_4/data/ROUND2/ROUND_2_LOGS/offsets_3_5_logs/346105/346105.log'),
    ('5/5',          '/Users/markiejr/Propserity_4/data/ROUND2/ROUND_2_LOGS/offsets_5_5_logs/347857/347857.log'),
]


def analyze(path):
    obj = json.loads(Path(path).read_text())
    lines = obj['activitiesLog'].strip().split('\n')
    header = lines[0].split(';')
    rows = [dict(zip(header, line.split(';'))) for line in lines[1:] if len(line.split(';')) >= len(header)]

    # Final PnL by (day, product)
    by_day_prod = {}
    for r in rows:
        day = int(r['day'])
        ts = int(r['timestamp'])
        p = r['product']
        pnl = float(r['profit_and_loss']) if r['profit_and_loss'] else 0.0
        mid = float(r['mid_price']) if r['mid_price'] else 0.0
        by_day_prod[(day, p)] = (ts, pnl, mid)

    # Trade history
    trades = []
    for k, v in obj.items():
        if isinstance(v, list) and v and isinstance(v[0], dict) and 'buyer' in v[0]:
            trades = v; break
    our_buys  = [t for t in trades if t['buyer']  == 'SUBMISSION']
    our_sells = [t for t in trades if t['seller'] == 'SUBMISSION']

    # Per-product fills
    buys_by = defaultdict(list); sells_by = defaultdict(list)
    for t in our_buys:  buys_by[t['symbol']].append(t)
    for t in our_sells: sells_by[t['symbol']].append(t)

    # OSM adverse-selection probe (mid lookup)
    osm_mid = {}
    for r in rows:
        if r['product'] == 'ASH_COATED_OSMIUM':
            if r['mid_price']:
                osm_mid[int(r['timestamp'])] = float(r['mid_price'])

    def edge_at_lag(fills, lag, direction):
        vals = []
        for t in fills:
            m = osm_mid.get(t['timestamp'] + lag)
            if m is not None:
                vals.append(direction * (m - t['price']) * t['quantity'])
        return (sum(vals)/len(vals) if vals else 0.0, len(vals))

    osm_buys  = buys_by.get('ASH_COATED_OSMIUM', [])
    osm_sells = sells_by.get('ASH_COATED_OSMIUM', [])
    buy_edge,  nb = edge_at_lag(osm_buys,  500, +1)
    sell_edge, ns = edge_at_lag(osm_sells, 500, -1)

    # Summary dict
    total_pnl = sum(v[1] for v in by_day_prod.values())
    osm_pnl = sum(v[1] for (d, p), v in by_day_prod.items() if p == 'ASH_COATED_OSMIUM')
    ipr_pnl = sum(v[1] for (d, p), v in by_day_prod.items() if p == 'INTARIAN_PEPPER_ROOT')

    def agg(fills):
        q = sum(t['quantity'] for t in fills)
        v = sum(t['price']*t['quantity'] for t in fills)
        return len(fills), q, (v/q if q else 0)

    osm_b_n, osm_b_q, osm_b_avg = agg(osm_buys)
    osm_s_n, osm_s_q, osm_s_avg = agg(osm_sells)
    ipr_b_n, ipr_b_q, ipr_b_avg = agg(buys_by.get('INTARIAN_PEPPER_ROOT', []))
    ipr_s_n, ipr_s_q, ipr_s_avg = agg(sells_by.get('INTARIAN_PEPPER_ROOT', []))

    return {
        'total': total_pnl, 'osm': osm_pnl, 'ipr': ipr_pnl,
        'osm_b_n': osm_b_n, 'osm_b_q': osm_b_q, 'osm_b_avg': osm_b_avg,
        'osm_s_n': osm_s_n, 'osm_s_q': osm_s_q, 'osm_s_avg': osm_s_avg,
        'ipr_b_n': ipr_b_n, 'ipr_b_q': ipr_b_q,
        'ipr_s_n': ipr_s_n, 'ipr_s_q': ipr_s_q,
        'osm_buy_edge_500': buy_edge,  'osm_buy_edge_n':  nb,
        'osm_sell_edge_500': sell_edge, 'osm_sell_edge_n': ns,
        'osm_net_pos': osm_b_q - osm_s_q,
        'days': sorted({d for (d, _) in by_day_prod}),
    }


results = [(label, analyze(path)) for label, path in RUNS]

print(f"{'run':>12}  {'total':>9}  {'OSM':>8}  {'IPR':>9}  days")
for label, r in results:
    print(f"{label:>12}  {r['total']:>9,.0f}  {r['osm']:>8,.0f}  {r['ipr']:>9,.0f}  {r['days']}")

print("\n=== OSM FILLS ===")
print(f"{'run':>12}  {'buys':>6}  {'buy_q':>6}  {'buy_avg':>9}  {'sells':>6}  {'sell_q':>6}  {'sell_avg':>9}  {'net_qty':>8}")
for label, r in results:
    print(f"{label:>12}  {r['osm_b_n']:>6}  {r['osm_b_q']:>6}  {r['osm_b_avg']:>9,.2f}  {r['osm_s_n']:>6}  {r['osm_s_q']:>6}  {r['osm_s_avg']:>9,.2f}  {r['osm_net_pos']:>+8d}")

print("\n=== OSM ADVERSE-SELECTION PROBE (edge per fill, +500 ticks later) ===")
print(f"{'run':>12}  {'buy_edge':>10} (n)  {'sell_edge':>10} (n)  {'asymmetry':>10}")
for label, r in results:
    asym = r['osm_buy_edge_500'] - r['osm_sell_edge_500']
    print(f"{label:>12}  {r['osm_buy_edge_500']:>10.2f} ({r['osm_buy_edge_n']:>2})  {r['osm_sell_edge_500']:>10.2f} ({r['osm_sell_edge_n']:>2})  {asym:>+10.2f}")

print("\n=== IPR FILLS ===")
print(f"{'run':>12}  {'buys':>6}  {'buy_q':>6}  {'sells':>6}  {'sell_q':>6}")
for label, r in results:
    print(f"{label:>12}  {r['ipr_b_n']:>6}  {r['ipr_b_q']:>6}  {r['ipr_s_n']:>6}  {r['ipr_s_q']:>6}")
