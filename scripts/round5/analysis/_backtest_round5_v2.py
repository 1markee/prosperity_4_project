"""Round 5 backtest v2 — uses TRADE history to model passive fills.

For each tick, after the algo posts orders, look at trades that occurred at
this timestamp. If a trade happened at a price <= our buy or >= our sell
order's price (and inside the prevailing market), simulate a passive fill
on our side at our quoted price (we'd have offered better than the visible
book and would have gotten the fill).

This is a more realistic but still imperfect estimate. Caps fills at our
position limit and at trade volume.
"""
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
POS_LIMIT = ralgo.POS_LIMIT


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
    # Trades indexed by ts, prod
    trades = defaultdict(lambda: defaultdict(list))
    with open(DATA / f'trades_round_5_day_{day}.csv') as f:
        r = csv.DictReader(f, delimiter=';')
        for row in r:
            try:
                ts = int(row['timestamp']); prod = row['symbol']
                price = float(row['price']); qty = int(row['quantity'])
                trades[ts][prod].append((price, qty))
            except: pass
    return ticks, trades


def make_od(book):
    od = dm.OrderDepth()
    od.buy_orders  = dict(book['buys'])
    od.sell_orders = dict(book['sells'])
    return od


def run_day(day, verbose=False):
    ticks, trades = load_day(day)
    timestamps = sorted(ticks.keys())
    trader = ralgo.Trader()
    position = {p: 0 for p in PRODUCTS}
    cash = 0.0
    trader_data = ""
    fills = defaultdict(int)
    pnl_per_prod = defaultdict(float)

    for ts in timestamps:
        state = dm.TradingState(trader_data, ts, {}, {}, {}, {}, position.copy(), {})
        state.order_depths = {prod: make_od(b) for prod, b in ticks[ts].items()}
        result, _, trader_data = trader.run(state)

        for prod, orders in result.items():
            book = ticks[ts].get(prod)
            if book is None: continue
            sells = dict(book['sells']); buys = dict(book['buys'])
            best_bid = max(buys) if buys else None
            best_ask = min(sells) if sells else None
            tick_trades = list(trades[ts].get(prod, []))

            for order in orders:
                p = int(round(order.price)); q = order.quantity
                # ── Aggressive take against visible book ───────────────────
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
                    # ── Passive fill from trades happening at this ts ───────
                    # If our buy is INSIDE the visible spread (better than best_bid)
                    # AND a trade happened at our price or below, we get filled.
                    if remain > 0 and best_bid is not None and p > best_bid and p < (best_ask or float('inf')):
                        # Match trades at price <= our bid (those would lift our bid)
                        for tp, tq in list(tick_trades):
                            if tp <= p and tq > 0:
                                f = min(tq, remain, POS_LIMIT - position[prod])
                                if f > 0:
                                    position[prod] += f; cash -= f*p   # we pay our quoted price
                                    pnl_per_prod[prod] -= f*p
                                    remain -= f
                                    fills[prod] += f
                                    # Consume from this trade
                                    idx = tick_trades.index((tp, tq))
                                    tick_trades[idx] = (tp, tq - f)
                                    if remain <= 0: break
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
                    # Passive sell: our ask is inside spread (better than best_ask)
                    if remain > 0 and best_ask is not None and p < best_ask and p > (best_bid or 0):
                        for tp, tq in list(tick_trades):
                            if tp >= p and tq > 0:
                                f = min(tq, remain, POS_LIMIT + position[prod])
                                if f > 0:
                                    position[prod] -= f; cash += f*p
                                    pnl_per_prod[prod] += f*p
                                    remain -= f
                                    fills[prod] += f
                                    idx = tick_trades.index((tp, tq))
                                    tick_trades[idx] = (tp, tq - f)
                                    if remain <= 0: break

    last = ticks[timestamps[-1]]
    pnl = cash
    for prod, pos in position.items():
        m = last.get(prod, {}).get('mid', 0) or 0
        if m: pnl += pos*m; pnl_per_prod[prod] += pos*m

    if verbose:
        print(f'\n--- Day {day} ---  PnL: {pnl:+,.2f}')
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
