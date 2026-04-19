"""Sweep all Tier 2/3 features one at a time through p2bt."""
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
m = parse_algorithm('/Users/markiejr/Propserity_4/scripts/round2/round2_algo.py')

def run_config(**overrides):
    importlib.reload(m)
    for k, v in overrides.items():
        setattr(m, k, v)
    t = m.Trader()
    total = 0.0
    osm = 0.0
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
    return total, osm

# Baseline (Tier 1 final)
print("=== BASELINE (Tier 1 final: ACF on, offsets 1/1, all T2/3 OFF) ===")
b_tot, b_osm = run_config()
print(f"  total={b_tot:,.0f}   OSM={b_osm:,.0f}")

def rel(t, o):
    return f"total={t:>10,.0f} (Δ={t-b_tot:+6,.0f})   OSM={o:>8,.0f} (Δ={o-b_osm:+5,.0f})"

# ── 2a: Multi-tick ACF (AR(2)) ────────────────────────────────────────────────
print("\n=== 2a: Multi-tick ACF (AR(2)) — sweep OSM_AR2_COEF ===")
for c in [0.0, 0.1, 0.2, 0.3, 0.4, 0.5]:
    t, o = run_config(OSM_AR2_COEF=c)
    print(f"  AR2_COEF={c:.2f}: {rel(t, o)}")

# ── 2b: OBI (imbalance) as fair input ─────────────────────────────────────────
print("\n=== 2b: OBI fair-input — sweep OSM_OBI_COEF ===")
for c in [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0]:
    t, o = run_config(OSM_OBI_COEF=c)
    print(f"  OBI_COEF={c:.1f}: {rel(t, o)}")

# ── 2c: Z-score take overlay ──────────────────────────────────────────────────
print("\n=== 2c: Z-score take overlay — sweep threshold × size ===")
for thr in [1.5, 2.0, 2.5, 3.0]:
    for sz in [5, 10, 15, 20]:
        t, o = run_config(OSM_ZSCORE_THRESHOLD=thr, OSM_ZSCORE_TAKE_SIZE=sz)
        print(f"  thr={thr}, sz={sz:2d}: {rel(t, o)}")

# ── 2d: Dynamic offset from vol ───────────────────────────────────────────────
print("\n=== 2d: Vol-scaled offset ===")
for base in [1, 2, 3]:
    for enabled in [False, True]:
        if enabled:
            t, o = run_config(OSM_BID_OFFSET=base, OSM_ASK_OFFSET=base,
                              OSM_VOL_OFFSET_ENABLED=True)
        else:
            t, o = run_config(OSM_BID_OFFSET=base, OSM_ASK_OFFSET=base)
        label = f"offset={base}, vol_scale={'ON' if enabled else 'OFF'}"
        print(f"  {label:32s}: {rel(t, o)}")

# ── 2e: Kalman anchor ─────────────────────────────────────────────────────────
print("\n=== 2e: Kalman anchor — sweep COEF ===")
for c in [0.0, 0.05, 0.1, 0.2, 0.5, 1.0]:
    t, o = run_config(OSM_KALMAN_ENABLED=True, OSM_KALMAN_COEF=c)
    print(f"  KALMAN_COEF={c:.2f}: {rel(t, o)}")

# ── 2f: Avellaneda-Stoikov ────────────────────────────────────────────────────
print("\n=== 2f: Avellaneda-Stoikov — sweep gamma ===")
for g in [0.05, 0.1, 0.2, 0.5, 1.0]:
    t, o = run_config(OSM_AS_ENABLED=True, OSM_AS_GAMMA=g)
    print(f"  GAMMA={g:.2f}: {rel(t, o)}")

# ── Combined winners ──────────────────────────────────────────────────────────
print("\n=== Combined: best OBI alone (tuned later) ===")
# Will be populated after reviewing the above
