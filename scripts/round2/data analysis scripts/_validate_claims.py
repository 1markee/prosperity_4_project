"""Validate external tuning claims against current baseline."""
import sys, importlib
from pathlib import Path

sys.path.insert(0, '/Users/markiejr/Propserity_4/scripts/round2')
from prosperity2bt.file_reader import FileReader, wrap_in_context_manager
from prosperity2bt.runner import run_backtest as p2bt_run
from prosperity2bt.__main__ import parse_algorithm
import prosperity2bt.data as p2bt_data
p2bt_data.LIMITS['ASH_COATED_OSMIUM']    = 80
p2bt_data.LIMITS['INTARIAN_PEPPER_ROOT'] = 80

class R(FileReader):
    def __init__(self, b): self._b = b
    def file(self, parts):
        folder, filename = parts[0], parts[1]
        n = folder.replace('round', '')
        c = self._b / f'ROUND{n}' / f'ROUND_{n}_DATA' / filename
        return wrap_in_context_manager(c if c.is_file() else None)

r = R(Path('/Users/markiejr/Propserity_4/data'))
m = parse_algorithm('/Users/markiejr/Propserity_4/scripts/round2/round2_algo_final.py')

def run_config(**overrides):
    importlib.reload(m)
    for k, v in overrides.items():
        setattr(m, k, v)
    t = m.Trader()
    total = 0.0
    osm = 0.0
    ipr = 0.0
    for d in [-1, 0, 1]:
        res = p2bt_run(t, r, round_num=2, day_num=d, print_output=False,
                       disable_trades_matching=False, no_names=False, show_progress_bar=False)
        last_ts = res.activity_logs[-1].timestamp
        for row in res.activity_logs:
            if row.timestamp == last_ts:
                mid = row.columns[-2]
                if mid and float(mid) > 0:
                    total += row.columns[-1]
                    if row.columns[2] == 'ASH_COATED_OSMIUM':
                        osm += row.columns[-1]
                    elif row.columns[2] == 'INTARIAN_PEPPER_ROOT':
                        ipr += row.columns[-1]
    return total, osm, ipr

# Baseline
print("=== BASELINE (current final algo) ===")
b_tot, b_osm, b_ipr = run_config()
print(f"  total={b_tot:>10,.0f}   OSM={b_osm:>8,.0f}   IPR={b_ipr:>9,.0f}")

def rel(t, o, i):
    return (f"total={t:>10,.0f} (Δ={t-b_tot:+6,.0f})   "
            f"OSM={o:>8,.0f} (Δ={o-b_osm:+5,.0f})   "
            f"IPR={i:>9,.0f} (Δ={i-b_ipr:+5,.0f})")

# Single-param claims
print("\n=== SINGLE-PARAM CLAIMS ===")
cases = [
    ("OSM_ACF_COEF=0.25",         {'OSM_ACF_COEF': 0.25}),
    ("OSM_KALMAN_ALPHA=0.0001",   {'OSM_KALMAN_ALPHA': 0.0001}),
    ("BID=3, ASK=1",              {'OSM_BID_OFFSET': 3, 'OSM_ASK_OFFSET': 1}),
    ("OSM_MAX_SKEW_TICKS=2",      {'OSM_MAX_SKEW_TICKS': 2}),
]
for name, cfg in cases:
    t, o, i = run_config(**cfg)
    print(f"  {name:32s}: {rel(t, o, i)}")

# Combined claim
print("\n=== COMBINED RETUNE (the external recommendation) ===")
combined = {
    'OSM_BID_OFFSET': 3,
    'OSM_ASK_OFFSET': 1,
    'OSM_MAX_SKEW_TICKS': 2,
    'OSM_ACF_COEF': 0.25,
    'OSM_KALMAN_ALPHA': 0.0001,
    'OSM_KALMAN_COEF': 0.20,
}
t, o, i = run_config(**combined)
print(f"  combined: {rel(t, o, i)}")
