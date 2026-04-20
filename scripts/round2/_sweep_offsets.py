"""Sweep bid/ask offset pairs to validate 3/3 and 2/2 vs current 3/1."""
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
ALGO = '/Users/markiejr/Propserity_4/scripts/round2/round2_algo_final.py'


def make_trader(bid_off, ask_off):
    m = parse_algorithm(ALGO); importlib.reload(m)
    m.OSM_BID_OFFSET = bid_off
    m.OSM_ASK_OFFSET = ask_off
    return m.Trader()


def run_day(trader, day):
    res = p2bt_run(trader, r, round_num=2, day_num=day, print_output=False,
                   disable_trades_matching=False, no_names=False,
                   show_progress_bar=False)
    last_ts = res.activity_logs[-1].timestamp
    osm, total = 0.0, 0.0
    for row in res.activity_logs:
        if row.timestamp == last_ts:
            mid_ = row.columns[-2]
            if mid_ and float(mid_) > 0:
                total += row.columns[-1]
                if row.columns[2] == 'ASH_COATED_OSMIUM':
                    osm += row.columns[-1]
    return total, osm


configs = [(3, 1), (3, 3), (2, 2), (3, 2), (2, 3)]
print(f"{'bid/ask':>8}  {'day-1 osm':>10}  {'day 0 osm':>10}  {'day+1 osm':>10}  {'3-day osm':>11}  {'3-day tot':>11}")
for bid_off, ask_off in configs:
    row_osm, row_tot = [], []
    for day in [-1, 0, 1]:
        t = make_trader(bid_off, ask_off)
        total, osm = run_day(t, day)
        row_osm.append(osm); row_tot.append(total)
    print(f"  {bid_off}/{ask_off:1d}    {row_osm[0]:>10,.0f}  {row_osm[1]:>10,.0f}  {row_osm[2]:>10,.0f}  {sum(row_osm):>11,.0f}  {sum(row_tot):>11,.0f}")
