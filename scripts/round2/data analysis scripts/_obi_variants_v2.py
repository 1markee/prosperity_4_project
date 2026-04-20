"""Second batch of OBI-based formulas — qualitatively different from the first.

All share the structure: apply `fair -= effective_coef * last_return`
but with `effective_coef` modulated by different OBI-derived functions.
"""
import sys, importlib, types
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


def make_trader(modulator_fn):
    """modulator_fn(last_return, imbalance, hist_tail) -> effective coef."""
    m = parse_algorithm(ALGO)
    importlib.reload(m)
    OSM = m.OSM
    POSITION_LIMIT = m.POSITION_LIMIT
    Order = m.Order

    def _trade_osm(self, order_depth, position, hist, anchor, timestamp):
        orders = []
        if (order_depth is None or not order_depth.buy_orders
                or not order_depth.sell_orders):
            return orders, anchor
        best_bid = max(order_depth.buy_orders.keys())
        best_ask = min(order_depth.sell_orders.keys())
        bid_vol = order_depth.buy_orders[best_bid]
        ask_vol = -order_depth.sell_orders[best_ask]
        mid = (best_bid + best_ask) / 2
        micro = (best_ask * bid_vol + best_bid * ask_vol) / (bid_vol + ask_vol)
        imbalance = (bid_vol - ask_vol) / (bid_vol + ask_vol)

        fair = micro
        if hist:
            last_return = mid - hist[-1]
            coef = modulator_fn(last_return, imbalance, hist)
            fair -= coef * last_return

        new_anchor = anchor
        if m.OSM_KALMAN_ENABLED:
            new_anchor = (1 - m.OSM_KALMAN_ALPHA) * anchor + m.OSM_KALMAN_ALPHA * mid
            fair += m.OSM_KALMAN_COEF * (new_anchor - mid)

        bid_off, ask_off = m.OSM_BID_OFFSET, m.OSM_ASK_OFFSET
        buy_cap = POSITION_LIMIT - position
        sell_cap = POSITION_LIMIT + position

        for price in sorted(order_depth.sell_orders.keys()):
            if price >= fair or buy_cap <= 0: break
            qty = min(-order_depth.sell_orders[price], buy_cap)
            orders.append(Order(OSM, price, qty)); buy_cap -= qty
        for price in sorted(order_depth.buy_orders.keys(), reverse=True):
            if price <= fair or sell_cap <= 0: break
            qty = min(order_depth.buy_orders[price], sell_cap)
            orders.append(Order(OSM, price, -qty)); sell_cap -= qty

        skew = (position / POSITION_LIMIT) * m.OSM_MAX_SKEW_TICKS
        bid_price = round(fair - bid_off - skew)
        ask_price = round(fair + ask_off - skew)
        if bid_price >= ask_price: ask_price = bid_price + 1
        bid_qty = min(m.OSM_PASSIVE_SIZE, buy_cap)
        ask_qty = min(m.OSM_PASSIVE_SIZE, sell_cap)
        if bid_qty > 0: orders.append(Order(OSM, bid_price, bid_qty))
        if ask_qty > 0: orders.append(Order(OSM, ask_price, -ask_qty))
        return orders, new_anchor

    t = m.Trader()
    t._trade_osm = types.MethodType(_trade_osm, t)
    return t, m


def run(trader):
    total, osm = 0.0, 0.0
    for d in [-1, 0, 1]:
        res = p2bt_run(trader, r, round_num=2, day_num=d, print_output=False,
                       disable_trades_matching=False, no_names=False,
                       show_progress_bar=False)
        last_ts = res.activity_logs[-1].timestamp
        for row in res.activity_logs:
            if row.timestamp == last_ts:
                mid_ = row.columns[-2]
                if mid_ and float(mid_) > 0:
                    total += row.columns[-1]
                    if row.columns[2] == 'ASH_COATED_OSMIUM':
                        osm += row.columns[-1]
    return total, osm


# Baseline
BASE_COEF = 0.25
t, m = make_trader(lambda r_, i_, h_: BASE_COEF)
b_tot, b_osm = run(t)
print(f"BASELINE: total={b_tot:,.0f}  OSM={b_osm:,.0f}")

def rel(t_, o_):
    return f"total={t_:>10,.0f} (Δ={t_-b_tot:+6,.0f})   OSM={o_:>8,.0f} (Δ={o_-b_osm:+5,.0f})"

# V1 — only apply ACF when |OBI| is SMALL (near-balanced book = cleanest reversion regime)
print("\n=== V1: only apply ACF when |imbalance| < threshold ===")
for thr in [0.2, 0.3, 0.5, 0.7]:
    def mod(r_, i_, h_, _thr=thr): return BASE_COEF if abs(i_) < _thr else 0.0
    t, _ = make_trader(mod); tot, osm = run(t)
    print(f"  |OBI|<{thr}: {rel(tot, osm)}")

# V2 — continuous: coef = BASE × (1 − |OBI|)  (trust ACF more when book is balanced)
print("\n=== V2: coef = BASE × (1 − |OBI|) ===")
t, _ = make_trader(lambda r_, i_, h_: BASE_COEF * (1 - abs(i_)))
tot, osm = run(t); print(f"  {rel(tot, osm)}")

# V3 — OBI as sign-agreement scorer (−1=disagree, +1=agree), scale coef
#      coef = BASE × (1 − k × sign_agreement) → amplify on disagree, shrink on agree
print("\n=== V3: coef = BASE × (1 − k × sign(return)·sign(imbalance)) ===")
for k in [0.3, 0.5, 0.7]:
    def mod(r_, i_, h_, _k=k):
        if r_ == 0 or i_ == 0: return BASE_COEF
        sa = (1 if r_*i_ > 0 else -1)
        return BASE_COEF * (1 - _k * sa)
    t, _ = make_trader(mod); tot, osm = run(t)
    print(f"  k={k}: {rel(tot, osm)}")

# V4 — use OBI *change* as signal (Δ-imbalance) — flow acceleration
print("\n=== V4: shrink ACF if OBI is DECAYING in same direction as move ===")
# need last imbalance — simulate via hist extension (we'll store only mid). Skip — requires deeper refactor.

# V5 — extreme OBI filter: when OBI is near ±1, the book is fragile — zero out ACF
print("\n=== V5: zero ACF only when |OBI| is extreme (>threshold) ===")
for thr in [0.7, 0.85, 0.95]:
    def mod(r_, i_, h_, _thr=thr): return 0.0 if abs(i_) > _thr else BASE_COEF
    t, _ = make_trader(mod); tot, osm = run(t)
    print(f"  |OBI|>{thr}: {rel(tot, osm)}")

# V6 — OBI-WEIGHTED: use imbalance as a continuous "confidence" in reversion
#      coef = BASE × exp(−k|imbalance|)  — soft decay as book gets imbalanced
print("\n=== V6: coef = BASE × exp(−k · |OBI|) ===")
import math
for k in [0.5, 1.0, 2.0]:
    def mod(r_, i_, h_, _k=k): return BASE_COEF * math.exp(-_k * abs(i_))
    t, _ = make_trader(mod); tot, osm = run(t)
    print(f"  k={k}: {rel(tot, osm)}")
