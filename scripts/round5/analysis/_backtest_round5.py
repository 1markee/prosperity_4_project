"""Round 5 backtest. Same structure as the R3 sim — read CSV order book per
tick, run trader, match against book + next-tick passive fills."""
import sys, csv, importlib.util
from pathlib import Path
from collections import defaultdict

ALGO_DIR = Path(__file__).resolve().parent.parent / 'algos'
sys.path.insert(0, str(ALGO_DIR))
import datamodel as dm

spec = importlib.util.spec_from_file_location("round5_algo", ALGO_DIR / 'round5_algo.py')
ralgo = importlib.util.module_from_spec(spec); spec.loader.exec_module(ralgo)

DATA = Path('/Users/markiejr/Propserity_4/data/ROUND5/ROUND_5')
PRODUCTS = list(ralgo.PRODUCT_TO_CAT.keys())


def load_day(day):
    ticks = defaultdict(dict)
    with open(DATA / f'prices_round_5_day_{day}.csv') as f:
        r = csv.DictReader(f, delimiter=';')
        for row in r:
            ts = int(row['timestamp']); prod = row['product']
            buys = {}; sells = {}
            for i in [1, 2, 3]:
                bp = row.get(f'bid_price_{i}'); bv = row.get(f'bid_volume_{i}')
                ap = row.get(f'ask_price_{i}'); av = row.get(f'ask_volume_{i}')
                if bp and bv: buys[int(float(bp))] = int(bv)
                if ap and av: sells[int(float(ap))] = -int(av)
            mid = float(row['mid_price']) if row['mid_price'] else None
            ticks[ts][prod] = {'buys': buys, 'sells': sells, 'mid': mid}
    return ticks


def make_od(book):
    od = dm.OrderDepth()
    od.buy_orders  = dict(book['buys'])
    od.sell_orders = dict(book['sells'])
    return od


def run_day(day, verbose=False):
    ticks = load_day(day)
    timestamps = sorted(ticks.keys())
    trader = ralgo.Trader()
    position = {p: 0 for p in PRODUCTS}
    cash = 0.0
    trader_data = ""
    fills = defaultdict(int)
    pnl_per_prod = defaultdict(float)

    # Fill model: only fill against the visible book at this tick (i.e. taker
    # behavior). No passive-next-tick fills because that overstates fills and
    # systematically counts adversely-selected ones (book moves against us).
    for ts in timestamps:
        state = dm.TradingState(trader_data, ts, {}, {}, {}, {}, position.copy(), {})
        state.order_depths = {prod: make_od(b) for prod, b in ticks[ts].items()}
        result, _, trader_data = trader.run(state)

        for prod, orders in result.items():
            book = ticks[ts].get(prod)
            if book is None: continue
            sells = dict(book['sells']); buys = dict(book['buys'])
            for order in orders:
                p = int(round(order.price)); q = order.quantity
                if q > 0:
                    remain = q
                    for ask_p in sorted(sells):
                        if ask_p > p or remain <= 0: break
                        avail = -sells[ask_p]; f = min(avail, remain)
                        if f > 0:
                            position[prod] += f; cash -= f*ask_p
                            pnl_per_prod[prod] -= f*ask_p
                            remain -= f; sells[ask_p] = -(avail-f)
                            if sells[ask_p] == 0: del sells[ask_p]
                            fills[prod] += f
                elif q < 0:
                    remain = -q
                    for bid_p in sorted(buys, reverse=True):
                        if bid_p < p or remain <= 0: break
                        avail = buys[bid_p]; f = min(avail, remain)
                        if f > 0:
                            position[prod] -= f; cash += f*bid_p
                            pnl_per_prod[prod] += f*bid_p
                            remain -= f; buys[bid_p] = avail-f
                            if buys[bid_p] == 0: del buys[bid_p]
                            fills[prod] += f

    last = ticks[timestamps[-1]]
    pnl = cash
    for prod, pos in position.items():
        m = last.get(prod, {}).get('mid', 0) or 0
        if m: pnl += pos*m; pnl_per_prod[prod] += pos*m

    if verbose:
        print(f'\n--- Day {day} ---  PnL: {pnl:+,.2f}')
        # Group by category
        by_cat = defaultdict(lambda: [0.0, 0])
        for prod, pl in pnl_per_prod.items():
            cat = ralgo.PRODUCT_TO_CAT.get(prod, '?')
            by_cat[cat][0] += pl; by_cat[cat][1] += fills.get(prod, 0)
        for cat in sorted(by_cat, key=lambda c: -by_cat[c][0]):
            pl, fl = by_cat[cat]
            print(f'    {cat:18s}  PnL {pl:+10,.0f}  fills {fl}')

    return pnl, position, fills, pnl_per_prod


if __name__ == '__main__':
    totals = []
    for d in [2, 3, 4]:
        pnl, _, _, _ = run_day(d, verbose=True)
        totals.append(pnl)
    print(f'\n=== 3-day total: {sum(totals):+,.2f} (avg/day {sum(totals)/3:+,.2f}) ===')
