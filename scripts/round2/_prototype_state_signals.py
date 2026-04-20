"""Test the two signals the state-probability model flagged as novel:
  A) Extreme-OBI ACF veto (fine threshold sweep)
  B) Run-length amplifier (extra reversion bias when last 2 returns share a sign)
  C) Combined A+B
"""
import sys, importlib, types, math
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


def make_trader(obi_veto_thr=None, run_amp=1.0):
    """
    obi_veto_thr: if set, zero out ACF when |imbalance| > threshold
    run_amp:      multiply ACF coef when sign(last_ret) == sign(prev_ret)
    """
    m = parse_algorithm(ALGO)
    importlib.reload(m)
    OSM, POSITION_LIMIT, Order = m.OSM, m.POSITION_LIMIT, m.Order

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

        # ACF with modifiers
        if hist:
            last_return = mid - hist[-1]
            coef = m.OSM_ACF_COEF

            # A) Extreme-OBI veto
            if obi_veto_thr is not None and abs(imbalance) > obi_veto_thr:
                coef = 0.0

            # B) Run-length amplifier
            if len(hist) >= 2 and run_amp != 1.0:
                prev_return = hist[-1] - hist[-2]
                if last_return * prev_return > 0:   # both positive OR both negative
                    coef = coef * run_amp

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
    return t


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
b_tot, b_osm = run(make_trader())
print(f"BASELINE: total={b_tot:,.0f}  OSM={b_osm:,.0f}")
def rel(t_, o_):
    return f"total={t_:>10,.0f} (Δ={t_-b_tot:+6,.0f})   OSM={o_:>8,.0f} (Δ={o_-b_osm:+5,.0f})"

# ── A: Fine-grained OBI veto threshold sweep ─────────────────────────────────
print("\n=== A: Extreme-OBI ACF veto — fine sweep ===")
for thr in [0.4, 0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8]:
    t = make_trader(obi_veto_thr=thr)
    tot, osm = run(t)
    print(f"  |OBI|>{thr}: {rel(tot, osm)}")

# ── B: Run-length amplifier sweep ────────────────────────────────────────────
print("\n=== B: Run-length amplifier — sweep ===")
for amp in [1.1, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0]:
    t = make_trader(run_amp=amp)
    tot, osm = run(t)
    print(f"  amp={amp:.2f}: {rel(tot, osm)}")

# ── C: Combined best-A + best-B ──────────────────────────────────────────────
print("\n=== C: Combined A + B ===")
for obi_thr in [0.55, 0.6, 0.65, 0.7]:
    for amp in [1.25, 1.5, 2.0]:
        t = make_trader(obi_veto_thr=obi_thr, run_amp=amp)
        tot, osm = run(t)
        print(f"  |OBI|>{obi_thr}, amp={amp:.2f}: {rel(tot, osm)}")
