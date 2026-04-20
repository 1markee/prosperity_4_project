"""Test the 5 out-of-box upgrade ideas against current baseline.

Baseline: round2_algo_final.py WITH Finding 1 (OBI veto at 0.65) → OSM 13,683.

Ideas:
 1. Split quote_fair / take_fair — take uses deeper-book signal, quote doesn't
 2. One-sided quote shading on (ret, imb) regimes
 3. micro2 vs micro1 as veto (don't post cheap ask if book goes up deep)
 4. Multi-level passive quotes (primary + secondary layer further out)
 5. State-dependent anchor strength (nonlinear in |anchor - mid|)
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


def fresh():
    m = parse_algorithm(ALGO); importlib.reload(m)
    return m


def run_trader(t):
    total, osm = 0.0, 0.0
    for d in [-1, 0, 1]:
        res = p2bt_run(t, r, round_num=2, day_num=d, print_output=False,
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


def compute_fair_base(m, mid, micro, hist, imbalance, anchor):
    """Current algo fair composition (identical to _trade_osm)."""
    fair = micro
    if hist:
        if abs(imbalance) > m.OSM_OBI_VETO_THRESHOLD:
            pass
        else:
            fair -= m.OSM_ACF_COEF * (mid - hist[-1])
    new_anchor = anchor
    if m.OSM_KALMAN_ENABLED:
        new_anchor = (1 - m.OSM_KALMAN_ALPHA) * anchor + m.OSM_KALMAN_ALPHA * mid
        fair += m.OSM_KALMAN_COEF * (new_anchor - mid)
    return fair, new_anchor


def compute_micro2(order_depth):
    """2-level volume-weighted microprice.
    micro2 = (ask1*bidvol1 + bid1*askvol1 + ask2*bidvol2 + bid2*askvol2)
             / (bidvol1 + askvol1 + bidvol2 + askvol2)
    Uses weighted top-2 levels on each side.
    """
    bids = sorted(order_depth.buy_orders.keys(), reverse=True)[:2]
    asks = sorted(order_depth.sell_orders.keys())[:2]
    if not bids or not asks:
        return None
    bid1 = bids[0]; bv1 = order_depth.buy_orders[bid1]
    ask1 = asks[0]; av1 = -order_depth.sell_orders[ask1]
    bid2 = bids[1] if len(bids) > 1 else bid1; bv2 = order_depth.buy_orders[bid2] if len(bids) > 1 else 0
    ask2 = asks[1] if len(asks) > 1 else ask1; av2 = -order_depth.sell_orders[ask2] if len(asks) > 1 else 0
    num = ask1*bv1 + bid1*av1 + ask2*bv2 + bid2*av2
    den = bv1 + av1 + bv2 + av2
    return num / den if den > 0 else None


def std_trade_osm(m, self_, order_depth, position, hist, anchor, timestamp,
                  quote_fair_override=None, take_fair_override=None,
                  bid_shade=0, ask_shade=0,
                  suppress_buy_take=False, suppress_sell_take=False,
                  secondary_layer=None):
    """Shared _trade_osm body. All knobs optional."""
    orders = []
    OSM, POSITION_LIMIT, Order = m.OSM, m.POSITION_LIMIT, m.Order
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

    quote_fair, new_anchor = compute_fair_base(m, mid, micro, hist, imbalance, anchor)
    if quote_fair_override is not None:
        quote_fair = quote_fair_override
    take_fair = quote_fair if take_fair_override is None else take_fair_override

    bid_off = m.OSM_BID_OFFSET + bid_shade
    ask_off = m.OSM_ASK_OFFSET + ask_shade
    buy_cap  = POSITION_LIMIT - position
    sell_cap = POSITION_LIMIT + position

    # Take logic
    if not suppress_buy_take:
        for price in sorted(order_depth.sell_orders.keys()):
            if price >= take_fair or buy_cap <= 0: break
            qty = min(-order_depth.sell_orders[price], buy_cap)
            orders.append(Order(OSM, price, qty)); buy_cap -= qty
    if not suppress_sell_take:
        for price in sorted(order_depth.buy_orders.keys(), reverse=True):
            if price <= take_fair or sell_cap <= 0: break
            qty = min(order_depth.buy_orders[price], sell_cap)
            orders.append(Order(OSM, price, -qty)); sell_cap -= qty

    skew = (position / POSITION_LIMIT) * m.OSM_MAX_SKEW_TICKS
    bid_price = round(quote_fair - bid_off - skew)
    ask_price = round(quote_fair + ask_off - skew)
    if bid_price >= ask_price: ask_price = bid_price + 1

    bid_qty = min(m.OSM_PASSIVE_SIZE, buy_cap)
    ask_qty = min(m.OSM_PASSIVE_SIZE, sell_cap)
    if bid_qty > 0:
        orders.append(Order(OSM, bid_price, bid_qty)); buy_cap -= bid_qty
    if ask_qty > 0:
        orders.append(Order(OSM, ask_price, -ask_qty)); sell_cap -= ask_qty

    # Optional secondary layer
    if secondary_layer is not None:
        extra_offset, extra_size = secondary_layer
        if buy_cap > 0:
            bid2 = round(quote_fair - bid_off - extra_offset - skew)
            q = min(extra_size, buy_cap)
            orders.append(Order(OSM, bid2, q))
        if sell_cap > 0:
            ask2 = round(quote_fair + ask_off + extra_offset - skew)
            q = min(extra_size, sell_cap)
            orders.append(Order(OSM, ask2, -q))

    return orders, new_anchor


# ─── Baseline (current final algo) ───────────────────────────────────────────
def make_baseline():
    m = fresh()
    def _trade_osm(self, od, pos, h, a, ts):
        return std_trade_osm(m, self, od, pos, h, a, ts)
    t = m.Trader()
    t._trade_osm = types.MethodType(_trade_osm, t)
    return t

print("=== BASELINE (current final algo w/ Finding 1) ===")
b_tot, b_osm = run_trader(make_baseline())
print(f"  total={b_tot:,.0f}   OSM={b_osm:,.0f}")
def rel(t_, o_): return f"total={t_:>10,.0f} (Δ={t_-b_tot:+6,.0f})   OSM={o_:>8,.0f} (Δ={o_-b_osm:+5,.0f})"


# ─── IDEA 1: Split quote_fair / take_fair ───────────────────────────────────
print("\n=== IDEA 1: Split quote_fair / take_fair ===")
print("  take_fair = quote_fair + c × (micro2 − micro1)  [c sweep]")
def make_idea1(c):
    m = fresh()
    def _trade_osm(self, od, pos, h, a, ts):
        if od is None or not od.buy_orders or not od.sell_orders:
            return [], a
        m2 = compute_micro2(od)
        best_bid = max(od.buy_orders.keys())
        best_ask = min(od.sell_orders.keys())
        bv = od.buy_orders[best_bid]; av = -od.sell_orders[best_ask]
        m1 = (best_ask*bv + best_bid*av) / (bv+av)
        delta = (m2 - m1) if m2 is not None else 0.0
        # Compute quote_fair the normal way
        mid = (best_bid+best_ask)/2
        imb = (bv-av)/(bv+av)
        qf, na = compute_fair_base(m, mid, m1, h, imb, a)
        tf = qf + c * delta
        return std_trade_osm(m, self, od, pos, h, a, ts,
                             quote_fair_override=qf, take_fair_override=tf)
    t = m.Trader(); t._trade_osm = types.MethodType(_trade_osm, t); return t
for c in [0.5, 1.0, 2.0, 5.0]:
    t, o = run_trader(make_idea1(c))
    print(f"  c={c:.1f}: {rel(t, o)}")


# ─── IDEA 2: One-sided quote shading ────────────────────────────────────────
print("\n=== IDEA 2: One-sided quote shading (ret × imb regimes) ===")
print("  ret1<-1 & imb>0.1 → tighten bid by 1, widen ask by N, suppress sell-take")
print("  ret1>+1 & imb<-0.1 → tighten ask by 1, widen bid by N, suppress buy-take")
def make_idea2(widen_n, suppress_takes):
    m = fresh()
    def _trade_osm(self, od, pos, h, a, ts):
        if od is None or not od.buy_orders or not od.sell_orders:
            return [], a
        best_bid = max(od.buy_orders.keys())
        best_ask = min(od.sell_orders.keys())
        bv = od.buy_orders[best_bid]; av = -od.sell_orders[best_ask]
        mid = (best_bid+best_ask)/2
        imb = (bv-av)/(bv+av)
        last_ret = (mid - h[-1]) if h else 0.0
        bid_sh, ask_sh = 0, 0
        sup_buy, sup_sell = False, False
        # Expect UP: tighten bid, widen ask
        if last_ret < -1 and imb > 0.1:
            bid_sh = -1; ask_sh = widen_n
            if suppress_takes: sup_sell = True
        # Expect DOWN: tighten ask, widen bid
        elif last_ret > 1 and imb < -0.1:
            ask_sh = -1; bid_sh = widen_n
            if suppress_takes: sup_buy = True
        return std_trade_osm(m, self, od, pos, h, a, ts,
                             bid_shade=bid_sh, ask_shade=ask_sh,
                             suppress_buy_take=sup_buy, suppress_sell_take=sup_sell)
    t = m.Trader(); t._trade_osm = types.MethodType(_trade_osm, t); return t
for n in [1, 2]:
    for supp in [False, True]:
        t, o = run_trader(make_idea2(n, supp))
        print(f"  widen={n}, suppress_takes={supp}: {rel(t, o)}")


# ─── IDEA 3: micro2 as quote-side veto ───────────────────────────────────────
print("\n=== IDEA 3: micro2−micro1 veto — don't post cheap side when deep book disagrees ===")
print("  If m2−m1 > threshold: skip ask quote (book points up, our ask gets picked off)")
print("  If m2−m1 < −threshold: skip bid quote (book points down)")
def make_idea3(thr):
    m = fresh()
    def _trade_osm(self, od, pos, h, a, ts):
        if od is None or not od.buy_orders or not od.sell_orders:
            return [], a
        orders = []
        OSM, POSITION_LIMIT, Order = m.OSM, m.POSITION_LIMIT, m.Order
        best_bid = max(od.buy_orders.keys())
        best_ask = min(od.sell_orders.keys())
        bv = od.buy_orders[best_bid]; av = -od.sell_orders[best_ask]
        mid = (best_bid+best_ask)/2
        micro = (best_ask*bv + best_bid*av)/(bv+av)
        imb = (bv-av)/(bv+av)
        m2 = compute_micro2(od)
        delta = (m2 - micro) if m2 is not None else 0.0
        qf, na = compute_fair_base(m, mid, micro, h, imb, a)
        bid_off, ask_off = m.OSM_BID_OFFSET, m.OSM_ASK_OFFSET
        buy_cap = POSITION_LIMIT - pos; sell_cap = POSITION_LIMIT + pos
        # Take logic (unchanged)
        for price in sorted(od.sell_orders.keys()):
            if price >= qf or buy_cap <= 0: break
            q = min(-od.sell_orders[price], buy_cap)
            orders.append(Order(OSM, price, q)); buy_cap -= q
        for price in sorted(od.buy_orders.keys(), reverse=True):
            if price <= qf or sell_cap <= 0: break
            q = min(od.buy_orders[price], sell_cap)
            orders.append(Order(OSM, price, -q)); sell_cap -= q
        skew = (pos/POSITION_LIMIT) * m.OSM_MAX_SKEW_TICKS
        bid_price = round(qf - bid_off - skew)
        ask_price = round(qf + ask_off - skew)
        if bid_price >= ask_price: ask_price = bid_price + 1
        bid_qty = min(m.OSM_PASSIVE_SIZE, buy_cap)
        ask_qty = min(m.OSM_PASSIVE_SIZE, sell_cap)
        # Veto the side pointing toward adverse selection
        post_bid = delta >= -thr   # if m2 << m1, book going down — don't bid
        post_ask = delta <=  thr   # if m2 >> m1, book going up — don't ask
        if bid_qty > 0 and post_bid:
            orders.append(Order(OSM, bid_price, bid_qty))
        if ask_qty > 0 and post_ask:
            orders.append(Order(OSM, ask_price, -ask_qty))
        return orders, na
    t = m.Trader(); t._trade_osm = types.MethodType(_trade_osm, t); return t
for thr in [0.5, 1.0, 1.5, 2.0, 3.0]:
    t, o = run_trader(make_idea3(thr))
    print(f"  thr={thr}: {rel(t, o)}")


# ─── IDEA 4: Multi-level quotes ──────────────────────────────────────────────
print("\n=== IDEA 4: Secondary quote layer ===")
print("  primary: fair ± bid/ask_offset (size=10)")
print("  secondary: N ticks further (smaller size)")
def make_idea4(extra_offset, extra_size):
    m = fresh()
    def _trade_osm(self, od, pos, h, a, ts):
        return std_trade_osm(m, self, od, pos, h, a, ts,
                             secondary_layer=(extra_offset, extra_size))
    t = m.Trader(); t._trade_osm = types.MethodType(_trade_osm, t); return t
for eo in [2, 3, 4]:
    for es in [3, 5, 8]:
        t, o = run_trader(make_idea4(eo, es))
        print(f"  extra_off={eo}, extra_size={es}: {rel(t, o)}")


# ─── IDEA 5: State-dependent anchor strength ─────────────────────────────────
print("\n=== IDEA 5: State-dependent anchor coefficient ===")
print("  Replace constant 0.20 with 0.20 × (1 + k × |anchor−mid|)")
def make_idea5(k):
    m = fresh()
    def _trade_osm(self, od, pos, h, a, ts):
        if od is None or not od.buy_orders or not od.sell_orders:
            return [], a
        orders = []
        OSM, POSITION_LIMIT, Order = m.OSM, m.POSITION_LIMIT, m.Order
        best_bid = max(od.buy_orders.keys())
        best_ask = min(od.sell_orders.keys())
        bv = od.buy_orders[best_bid]; av = -od.sell_orders[best_ask]
        mid = (best_bid+best_ask)/2
        micro = (best_ask*bv + best_bid*av)/(bv+av)
        imb = (bv-av)/(bv+av)
        # Compute fair with state-dependent anchor
        fair = micro
        if h:
            if abs(imb) > m.OSM_OBI_VETO_THRESHOLD:
                pass
            else:
                fair -= m.OSM_ACF_COEF * (mid - h[-1])
        na = a
        if m.OSM_KALMAN_ENABLED:
            na = (1 - m.OSM_KALMAN_ALPHA) * a + m.OSM_KALMAN_ALPHA * mid
            dev = abs(na - mid)
            coef = m.OSM_KALMAN_COEF * (1 + k * dev)
            fair += coef * (na - mid)
        # Standard trade logic
        bid_off, ask_off = m.OSM_BID_OFFSET, m.OSM_ASK_OFFSET
        buy_cap = POSITION_LIMIT - pos; sell_cap = POSITION_LIMIT + pos
        for price in sorted(od.sell_orders.keys()):
            if price >= fair or buy_cap <= 0: break
            q = min(-od.sell_orders[price], buy_cap)
            orders.append(Order(OSM, price, q)); buy_cap -= q
        for price in sorted(od.buy_orders.keys(), reverse=True):
            if price <= fair or sell_cap <= 0: break
            q = min(od.buy_orders[price], sell_cap)
            orders.append(Order(OSM, price, -q)); sell_cap -= q
        skew = (pos/POSITION_LIMIT) * m.OSM_MAX_SKEW_TICKS
        bid_price = round(fair - bid_off - skew)
        ask_price = round(fair + ask_off - skew)
        if bid_price >= ask_price: ask_price = bid_price + 1
        bid_qty = min(m.OSM_PASSIVE_SIZE, buy_cap)
        ask_qty = min(m.OSM_PASSIVE_SIZE, sell_cap)
        if bid_qty > 0: orders.append(Order(OSM, bid_price, bid_qty))
        if ask_qty > 0: orders.append(Order(OSM, ask_price, -ask_qty))
        return orders, na
    t = m.Trader(); t._trade_osm = types.MethodType(_trade_osm, t); return t
for k in [0.05, 0.1, 0.2, 0.5, 1.0]:
    t, o = run_trader(make_idea5(k))
    print(f"  k={k}: {rel(t, o)}")
