"""Prototype: OBI as regime filter on the ACF reversion signal.

Hypothesis (from external idea):
  - Current ACF: fair -= OSM_ACF_COEF × (mid - prev_mid)
  - If OBI agrees in sign with last_return, the move has continuation pressure
    → shrink the reversion bias (don't fight a trend)
  - If OBI disagrees, reversion is more likely → keep full bias

Variants tested:
  A) Binary alignment veto: shrink ACF by SHRINK_FACTOR when last_return × imbalance > 0
  B) Same but only gated on |last_return| >= threshold (filter small-move noise)
  C) Symmetric flip: amplify ACF when OBI disagrees (bonus for "good setup")

We compare every variant to the current baseline (OSM=12,603, total=250,414).
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


def build_patched_trader(shrink_factor: float, threshold: float, amplify_on_disagree: float):
    """Reload the algo module, monkey-patch _trade_osm to apply the OBI veto.

    shrink_factor: multiplier on ACF_COEF when sign(last_return) == sign(imbalance)
                   (1.0 = no veto, 0.0 = full veto)
    threshold:     only apply veto if |last_return| >= threshold
    amplify_on_disagree: multiplier when signs disagree (1.0 = no change, >1 = amplify)
    """
    m = parse_algorithm(ALGO)
    importlib.reload(m)

    # Store overrides on the module for the patched method to read
    m._VETO_SHRINK = shrink_factor
    m._VETO_THRESHOLD = threshold
    m._VETO_AMPLIFY = amplify_on_disagree

    OSM = m.OSM
    POSITION_LIMIT = m.POSITION_LIMIT
    Order = m.Order

    def _trade_osm_patched(self, order_depth, position, hist, anchor, timestamp):
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

        # ── ACF with OBI regime filter ──────────────────────────────────────
        if hist:
            last_return = mid - hist[-1]
            effective_coef = m.OSM_ACF_COEF
            if abs(last_return) >= m._VETO_THRESHOLD:
                aligned = last_return * imbalance > 0
                if aligned:
                    effective_coef = m.OSM_ACF_COEF * m._VETO_SHRINK
                else:
                    effective_coef = m.OSM_ACF_COEF * m._VETO_AMPLIFY
            fair -= effective_coef * last_return

        # ── Rest identical to baseline ──────────────────────────────────────
        if len(hist) >= 2 and m.OSM_AR2_COEF != 0.0:
            fair -= m.OSM_AR2_COEF * (hist[-1] - hist[-2])
        fair += m.OSM_OBI_COEF * imbalance

        new_anchor = anchor
        if m.OSM_KALMAN_ENABLED:
            new_anchor = (1 - m.OSM_KALMAN_ALPHA) * anchor + m.OSM_KALMAN_ALPHA * mid
            fair += m.OSM_KALMAN_COEF * (new_anchor - mid)

        bid_off, ask_off = m.OSM_BID_OFFSET, m.OSM_ASK_OFFSET
        buy_cap = POSITION_LIMIT - position
        sell_cap = POSITION_LIMIT + position

        # Take mispriced
        for price in sorted(order_depth.sell_orders.keys()):
            if price >= fair or buy_cap <= 0:
                break
            qty = min(-order_depth.sell_orders[price], buy_cap)
            orders.append(Order(OSM, price, qty))
            buy_cap -= qty
        for price in sorted(order_depth.buy_orders.keys(), reverse=True):
            if price <= fair or sell_cap <= 0:
                break
            qty = min(order_depth.buy_orders[price], sell_cap)
            orders.append(Order(OSM, price, -qty))
            sell_cap -= qty

        skew = (position / POSITION_LIMIT) * m.OSM_MAX_SKEW_TICKS
        bid_price = round(fair - bid_off - skew)
        ask_price = round(fair + ask_off - skew)
        if bid_price >= ask_price:
            ask_price = bid_price + 1

        bid_qty = min(m.OSM_PASSIVE_SIZE, buy_cap)
        ask_qty = min(m.OSM_PASSIVE_SIZE, sell_cap)
        if bid_qty > 0:
            orders.append(Order(OSM, bid_price, bid_qty))
        if ask_qty > 0:
            orders.append(Order(OSM, ask_price, -ask_qty))

        return orders, new_anchor

    t = m.Trader()
    t._trade_osm = types.MethodType(_trade_osm_patched, t)
    return t


def run(trader):
    total, osm, ipr = 0.0, 0.0, 0.0
    for d in [-1, 0, 1]:
        res = p2bt_run(trader, r, round_num=2, day_num=d, print_output=False,
                       disable_trades_matching=False, no_names=False,
                       show_progress_bar=False)
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


# Baseline (no veto)
print("=== BASELINE ===")
t = build_patched_trader(1.0, 0.0, 1.0)
b_tot, b_osm, b_ipr = run(t)
print(f"  total={b_tot:,.0f}   OSM={b_osm:,.0f}")

def rel(t, o):
    return f"total={t:>10,.0f} (Δ={t-b_tot:+6,.0f})   OSM={o:>8,.0f} (Δ={o-b_osm:+5,.0f})"

# ── Variant A: binary alignment veto, no threshold ───────────────────────────
print("\n=== A: binary veto on ALL moves (shrink factor sweep) ===")
for sf in [0.0, 0.25, 0.5, 0.75]:
    t = build_patched_trader(sf, 0.0, 1.0)
    tot, osm, ipr = run(t)
    print(f"  shrink={sf:.2f}: {rel(tot, osm)}")

# ── Variant B: veto gated on move magnitude ──────────────────────────────────
print("\n=== B: binary veto only on big moves (shrink=0 — full veto, threshold sweep) ===")
for thr in [1.0, 1.5, 2.0, 2.5, 3.0]:
    t = build_patched_trader(0.0, thr, 1.0)
    tot, osm, ipr = run(t)
    print(f"  |ret|≥{thr:.1f}: {rel(tot, osm)}")

print("\n=== B': half-veto only on big moves (shrink=0.5, threshold sweep) ===")
for thr in [1.0, 1.5, 2.0, 2.5, 3.0]:
    t = build_patched_trader(0.5, thr, 1.0)
    tot, osm, ipr = run(t)
    print(f"  |ret|≥{thr:.1f}: {rel(tot, osm)}")

# ── Variant C: amplify when OBI disagrees with move ──────────────────────────
print("\n=== C: amplify ACF when OBI disagrees with move (no shrink, amplify sweep) ===")
for amp in [1.0, 1.25, 1.5, 2.0]:
    t = build_patched_trader(1.0, 0.0, amp)
    tot, osm, ipr = run(t)
    print(f"  amp={amp:.2f}: {rel(tot, osm)}")

# ── Variant D: best of all — shrink when aligned AND amplify when disagreed ──
print("\n=== D: shrink+amplify combined (sweep jointly) ===")
for sf in [0.0, 0.5]:
    for amp in [1.25, 1.5]:
        for thr in [0.0, 1.5]:
            t = build_patched_trader(sf, thr, amp)
            tot, osm, ipr = run(t)
            print(f"  shrink={sf:.2f}, amp={amp:.2f}, thr={thr:.1f}: {rel(tot, osm)}")
