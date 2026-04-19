"""Backtest round2_algo_final.py and report honest PnL net of MAF.

The community prosperity2bt harness does not simulate the Market Access Fee,
so its PnL overstates the real submission. This wrapper:
  1. Runs the algo through p2bt normally (gross PnL).
  2. Deducts MAF_BID once (per round, not per day) to report net PnL.
  3. Reports OSM/IPR breakdown and per-day detail.

It also accepts an optional --bid override to compare different MAF bids
without editing the algo file.
"""
import sys, argparse, importlib
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


def run(algo_path: str, bid_override=None, days=(-1, 0, 1)):
    reader = RemappedReader(Path('/Users/markiejr/Propserity_4/data'))
    m = parse_algorithm(algo_path)
    importlib.reload(m)
    if bid_override is not None:
        m.MAF_BID = bid_override

    trader = m.Trader()
    gross_total = 0.0
    per_day = {}
    per_product = {'ASH_COATED_OSMIUM': 0.0, 'INTARIAN_PEPPER_ROOT': 0.0}

    for d in days:
        res = p2bt_run(trader, reader, round_num=2, day_num=d,
                       print_output=False, disable_trades_matching=False,
                       no_names=False, show_progress_bar=False)
        last_ts = res.activity_logs[-1].timestamp
        day_pnl = 0.0
        for row in res.activity_logs:
            if row.timestamp == last_ts:
                mid = row.columns[-2]
                if mid and float(mid) > 0:
                    prod = row.columns[2]
                    pnl = row.columns[-1]
                    day_pnl += pnl
                    per_product[prod] = per_product.get(prod, 0.0) + pnl
        per_day[d] = day_pnl
        gross_total += day_pnl

    maf = m.MAF_BID
    net_total = gross_total - maf   # fee charged once per round
    return {
        'gross_total': gross_total,
        'maf_bid': maf,
        'net_total': net_total,
        'per_day': per_day,
        'per_product': per_product,
    }


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--algo', default='/Users/markiejr/Propserity_4/scripts/round2/round2_algo_final.py')
    p.add_argument('--bid', type=int, default=None,
                   help='override MAF_BID for this run (e.g. --bid 2000)')
    p.add_argument('--sweep', action='store_true',
                   help='sweep bid values {0, 500, 1000, 1500, 2000, 2500, 3000}')
    args = p.parse_args()

    if args.sweep:
        print(f"{'bid':>6} {'gross':>12} {'net':>12}   per-day")
        for b in [0, 500, 1000, 1500, 2000, 2500, 3000]:
            r = run(args.algo, bid_override=b)
            pd = '  '.join(f"d{d:+d}={v:>7,.0f}" for d, v in r['per_day'].items())
            print(f"{b:>6} {r['gross_total']:>12,.0f} {r['net_total']:>12,.0f}   {pd}")
    else:
        r = run(args.algo, bid_override=args.bid)
        print(f"Algo:         {args.algo}")
        print(f"MAF_BID:      {r['maf_bid']:,}")
        print(f"Gross PnL:    {r['gross_total']:>10,.0f}")
        print(f"Net PnL:      {r['net_total']:>10,.0f}   (gross − MAF)")
        print(f"Per product:  OSM={r['per_product']['ASH_COATED_OSMIUM']:>8,.0f}   "
              f"IPR={r['per_product']['INTARIAN_PEPPER_ROOT']:>8,.0f}")
        print(f"Per day:")
        for d, v in r['per_day'].items():
            print(f"  day {d:+d}: {v:>10,.0f}")
