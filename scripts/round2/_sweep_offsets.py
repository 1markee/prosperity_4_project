"""Sweep OSM_BID_OFFSET and OSM_ASK_OFFSET through p2bt and save results."""
import sys, json, importlib
from pathlib import Path

sys.path.insert(0, '/Users/markiejr/Propserity_4/scripts/round2')
from prosperity2bt.file_reader import FileReader, wrap_in_context_manager
from prosperity2bt.runner import run_backtest as p2bt_run
from prosperity2bt.__main__ import parse_algorithm
import prosperity2bt.data as p2bt_data

p2bt_data.LIMITS['ASH_COATED_OSMIUM']    = 80
p2bt_data.LIMITS['INTARIAN_PEPPER_ROOT'] = 80

class RemappedReader(FileReader):
    def __init__(self, base): self._base = base
    def file(self, parts):
        folder, filename = parts[0], parts[1]
        n = folder.replace('round', '')
        cand = self._base / f'ROUND{n}' / f'ROUND_{n}_DATA' / filename
        return wrap_in_context_manager(cand if cand.is_file() else None)

DATA_ROOT = Path('/Users/markiejr/Propserity_4/data')
ALGO_PATH = '/Users/markiejr/Propserity_4/scripts/round2/round2_algo.py'
reader = RemappedReader(DATA_ROOT)

algo_module = parse_algorithm(ALGO_PATH)

def run_one(days=(-1, 0, 1)):
    """Run p2bt for the given days and return (total_pnl, per_product_pnl_by_day, fills)."""
    trader = algo_module.Trader()
    total_pnl = 0.0
    by_day = {}
    all_fills = []
    for day in days:
        res = p2bt_run(trader, reader, round_num=2, day_num=day,
                       print_output=False, disable_trades_matching=False,
                       no_names=False, show_progress_bar=False)
        last_ts = res.activity_logs[-1].timestamp
        # Sum per-product PnL at last timestamp (only where mid > 0)
        day_pnl, day_by_prod = 0.0, {}
        for row in res.activity_logs:
            if row.timestamp == last_ts:
                mid = row.columns[-2]
                if mid and float(mid) > 0:
                    prod = row.columns[2]
                    day_by_prod[prod] = row.columns[-1]
        day_pnl = sum(day_by_prod.values())
        by_day[day] = day_by_prod
        total_pnl += day_pnl
        # Count our trades
        for t in res.sandbox_logs:
            pass
    return total_pnl, by_day

# ── Baseline: 3/5 asymmetric with all new features ───────────────────────────
print("=== BASELINE (3/5 asymmetric, ACF on, EOD on) ===")
importlib.reload(algo_module)
algo_module.OSM_BID_OFFSET = 3
algo_module.OSM_ASK_OFFSET = 5
total, by_day = run_one()
print(f"  Total PnL: {total:,.2f}")
for d, bp in by_day.items():
    print(f"    Day {d:+d}: {bp}")

# ── Offset sweep: symmetric offsets n for n in {1..8} ────────────────────────
print("\n=== SYMMETRIC OFFSET SWEEP (bid=ask=n) ===")
sym_results = {}
for n in [1, 2, 3, 4, 5, 6, 7, 8]:
    importlib.reload(algo_module)
    algo_module.OSM_BID_OFFSET = n
    algo_module.OSM_ASK_OFFSET = n
    total, by_day = run_one()
    sym_results[n] = total
    osm_final = sum(bp.get('ASH_COATED_OSMIUM', 0) for bp in by_day.values())
    ipr_final = sum(bp.get('INTARIAN_PEPPER_ROOT', 0) for bp in by_day.values())
    print(f"  offset={n}: total={total:>10,.0f}   OSM={osm_final:>8,.0f}   IPR={ipr_final:>8,.0f}")

# ── Asymmetric sweep: bid_offset < ask_offset ────────────────────────────────
print("\n=== ASYMMETRIC SWEEP (bid, ask) ===")
asym_pairs = [(2,4), (2,5), (2,6), (3,4), (3,5), (3,6), (3,7), (4,5), (4,6), (4,7), (4,8), (5,7), (5,8)]
asym_results = {}
for b, a in asym_pairs:
    importlib.reload(algo_module)
    algo_module.OSM_BID_OFFSET = b
    algo_module.OSM_ASK_OFFSET = a
    total, by_day = run_one()
    asym_results[(b, a)] = total
    osm_final = sum(bp.get('ASH_COATED_OSMIUM', 0) for bp in by_day.values())
    ipr_final = sum(bp.get('INTARIAN_PEPPER_ROOT', 0) for bp in by_day.values())
    print(f"  ({b},{a}): total={total:>10,.0f}   OSM={osm_final:>8,.0f}   IPR={ipr_final:>8,.0f}")

# ── Ablation: turn features off one at a time to measure their contribution ──
print("\n=== ABLATION (best offsets, toggle features off) ===")
# Find best offset from sweep
best_asym = max(asym_results, key=asym_results.get)
print(f"  Using best asymmetric offsets: {best_asym}")
b, a = best_asym

# ACF off — set coefficient to 0
importlib.reload(algo_module)
algo_module.OSM_BID_OFFSET = b
algo_module.OSM_ASK_OFFSET = a
algo_module.OSM_ACF_COEF   = 0.0
total, _ = run_one()
print(f"  ACF OFF:       total={total:>10,.0f}  (delta vs best = {total - asym_results[best_asym]:+,.0f})")

# EOD off — raise thresholds past end of day
importlib.reload(algo_module)
algo_module.OSM_BID_OFFSET = b
algo_module.OSM_ASK_OFFSET = a
algo_module.EOD_SOFT_THRESHOLD = 10**9
algo_module.EOD_HARD_THRESHOLD = 10**9
total, _ = run_one()
print(f"  EOD OFF:       total={total:>10,.0f}  (delta vs best = {total - asym_results[best_asym]:+,.0f})")

# IPR ACF patience off
importlib.reload(algo_module)
algo_module.OSM_BID_OFFSET = b
algo_module.OSM_ASK_OFFSET = a
algo_module.IPR_ACF_PATIENCE_THRESHOLD = 10**9  # disable
total, _ = run_one()
print(f"  IPR patience OFF: total={total:>10,.0f}  (delta vs best = {total - asym_results[best_asym]:+,.0f})")

# All features off (revert to old skeleton with widened offset only)
importlib.reload(algo_module)
algo_module.OSM_BID_OFFSET = b
algo_module.OSM_ASK_OFFSET = a
algo_module.OSM_ACF_COEF   = 0.0
algo_module.EOD_SOFT_THRESHOLD = 10**9
algo_module.EOD_HARD_THRESHOLD = 10**9
algo_module.IPR_ACF_PATIENCE_THRESHOLD = 10**9
total, _ = run_one()
print(f"  ALL TIER-1 OFF (offset only): total={total:>10,.0f}  (delta = {total - asym_results[best_asym]:+,.0f})")

# Save summary
with open('/tmp/sweep_results.json', 'w') as f:
    json.dump({
        'symmetric': sym_results,
        'asymmetric': {f"{b}_{a}": v for (b,a), v in asym_results.items()},
    }, f, indent=2)
print("\nSaved to /tmp/sweep_results.json")
