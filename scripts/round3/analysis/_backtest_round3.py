"""Custom backtester for Round 3.

p2bt doesn't ship Round-3 (Solvenar) products, so we sim the algo against the
historical CSV directly. Per-tick: use level-1 book to feed take-orders, treat
passive quotes as filled if they sit at or inside the next-tick best.

Estimates total PnL by tracking position × mark + cumulative cash.
"""
import sys, math, json, importlib, csv
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'algos'))

import datamodel as dm  # uses round2's datamodel — same shape

# --- Reset import cache ----------------------------------------------------
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'algos'))
spec_path = Path(__file__).resolve().parent.parent / 'algos' / 'round3_algo.py'
import importlib.util
spec = importlib.util.spec_from_file_location("round3_algo", spec_path)
ralgo = importlib.util.module_from_spec(spec); spec.loader.exec_module(ralgo)

PRODUCTS = ['HYDROGEL_PACK', 'VELVETFRUIT_EXTRACT'] + [f'VEV_{K}' for K in
            [4000, 4500, 5000, 5100, 5200, 5300, 5400, 5500, 6000, 6500]]
LIMITS = {p: 200 for p in ['HYDROGEL_PACK', 'VELVETFRUIT_EXTRACT']}
for K in [4000, 4500, 5000, 5100, 5200, 5300, 5400, 5500, 6000, 6500]:
    LIMITS[f'VEV_{K}'] = 300

DATA = Path('/Users/markiejr/Propserity_4/data/ROUND3/ROUND_3')


def load_day(day):
    """Returns dict[ts] -> dict[product] -> OrderDepth-like row."""
    ticks = defaultdict(dict)
    with open(DATA / f'prices_round_3_day_{day}.csv') as f:
        r = csv.DictReader(f, delimiter=';')
        for row in r:
            ts = int(row['timestamp']); prod = row['product']
            buys = {}; sells = {}
            for i in [1, 2, 3]:
                bp, bv = row.get(f'bid_price_{i}'), row.get(f'bid_volume_{i}')
                ap, av = row.get(f'ask_price_{i}'), row.get(f'ask_volume_{i}')
                if bp and bv:
                    buys[int(float(bp))] = int(bv)
                if ap and av:
                    sells[int(float(ap))] = -int(av)
            mid = float(row['mid_price']) if row['mid_price'] else None
            ticks[ts][prod] = {'buys': buys, 'sells': sells, 'mid': mid}
    return ticks


def make_order_depth(book):
    od = dm.OrderDepth()
    od.buy_orders  = dict(book['buys'])
    od.sell_orders = dict(book['sells'])
    return od


def run_day(day, verbose=False):
    """Simulate one day. Returns (per-product PnL dict, totals over time)."""
    ticks = load_day(day)
    timestamps = sorted(ticks.keys())

    # CRITICAL: historical TTE differs from live. Day 0 → 8, day 1 → 7, day 2 → 6.
    # Live submission (day 3) → 5. Patch the algo's TTE constant for accurate sim.
    ralgo.TTE_AT_ROUND_START = 8 - day

    trader = ralgo.Trader()
    position = {p: 0 for p in PRODUCTS}
    cash = 0.0
    trader_data = ""

    pnl_curve = []  # list of (ts, total_mtm)
    fill_count = defaultdict(int)
    notional_count = defaultdict(float)

    for ts in timestamps:
        # Build TradingState
        state = dm.TradingState(trader_data, ts, {}, {}, {}, {}, position.copy(), {})
        state.order_depths = {}
        for prod, book in ticks[ts].items():
            state.order_depths[prod] = make_order_depth(book)

        result, _, trader_data = trader.run(state)

        # Match orders: take aggressively, passive quotes only fill on next tick
        # via simple rule: if our bid >= best ask available on this tick, take.
        # If our bid is strictly below best ask but at or above next-tick best
        # bid, treat as passive fill at our price (limit fill).
        next_ts = timestamps[timestamps.index(ts) + 1] if ts != timestamps[-1] else None

        for prod, orders in result.items():
            book = ticks[ts].get(prod)
            if book is None: continue
            sells = dict(book['sells'])  # negative qty
            buys  = dict(book['buys'])
            for order in orders:
                p = int(round(order.price)); q = order.quantity
                if q > 0:  # buy
                    # Take from sells (asks)
                    remain = q
                    for ask_p in sorted(sells):
                        if ask_p > p or remain <= 0: break
                        avail = -sells[ask_p]
                        f = min(avail, remain)
                        if f > 0:
                            position[prod] += f; cash -= f * ask_p
                            remain -= f
                            sells[ask_p] = -(avail - f)
                            if sells[ask_p] == 0: del sells[ask_p]
                            fill_count[prod] += f
                            notional_count[prod] += f * ask_p
                    # Passive bid: assume fills if next-tick best ask <= our bid
                    if remain > 0 and next_ts is not None:
                        nb = ticks[next_ts].get(prod)
                        if nb:
                            ns = nb['sells']
                            if ns:
                                next_best_ask = min(ns)
                                if next_best_ask <= p:
                                    f = min(remain, -ns[next_best_ask])
                                    if f > 0:
                                        position[prod] += f; cash -= f * p
                                        fill_count[prod] += f
                                        notional_count[prod] += f * p
                elif q < 0:  # sell
                    remain = -q
                    for bid_p in sorted(buys, reverse=True):
                        if bid_p < p or remain <= 0: break
                        avail = buys[bid_p]
                        f = min(avail, remain)
                        if f > 0:
                            position[prod] -= f; cash += f * bid_p
                            remain -= f
                            buys[bid_p] = avail - f
                            if buys[bid_p] == 0: del buys[bid_p]
                            fill_count[prod] += f
                            notional_count[prod] += f * bid_p
                    if remain > 0 and next_ts is not None:
                        nb = ticks[next_ts].get(prod)
                        if nb:
                            nbids = nb['buys']
                            if nbids:
                                next_best_bid = max(nbids)
                                if next_best_bid >= p:
                                    f = min(remain, nbids[next_best_bid])
                                    if f > 0:
                                        position[prod] -= f; cash += f * p
                                        fill_count[prod] += f
                                        notional_count[prod] += f * p

        # Mark to mid
        if ts % 100_000 == 0 or ts == timestamps[-1]:
            mtm = cash
            for prod, pos in position.items():
                m = ticks[ts].get(prod, {}).get('mid')
                if m is not None: mtm += pos * m
            pnl_curve.append((ts, mtm))

    # Final mark-to-mid
    final_pnl = cash
    last = ticks[timestamps[-1]]
    breakdown = {}
    for prod, pos in position.items():
        m = last.get(prod, {}).get('mid', 0)
        # PnL approximation: cash plus mark of remaining position.
        breakdown[prod] = {'pos': pos, 'mid': m, 'fills': fill_count[prod]}
        final_pnl += pos * m if m else 0

    if verbose:
        print(f'\n--- Day {day} ---')
        print(f'  final PnL (mark-to-mid): {final_pnl:+,.2f}')
        print(f'  cash balance: {cash:+,.2f}')
        print(f'  positions: ' + ', '.join(f'{p}={position[p]}' for p in PRODUCTS if position[p] != 0))
        print(f'  fills: ' + ', '.join(f'{p}={fill_count[p]}' for p in PRODUCTS if fill_count[p] > 0))
    return final_pnl, position, fill_count, pnl_curve


if __name__ == '__main__':
    totals = []
    for d in [0, 1, 2]:
        pnl, _, _, _ = run_day(d, verbose=True)
        totals.append(pnl)
    print(f'\n=== 3-day total: {sum(totals):+,.2f} (avg/day {sum(totals)/3:+,.2f}) ===')
