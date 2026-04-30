from datamodel import OrderDepth, UserId, TradingState, Order
from typing import List, Dict, Tuple
import json

# ============================================================================
#  ROUND 5  —  50 products, 10 categories of 5, position limit 10 each
#
#  Edges (from EDA on days 2/3/4):
#
#  HARD EDGE — PEBBLES basket constraint:
#    PEBBLES_XS + PEBBLES_S + PEBBLES_M + PEBBLES_L + PEBBLES_XL = 50,000
#    (std 2.8 over 30,000 ticks; max |dev| = 18.5; ~1.5% of ticks have |dev|>5)
#    Strategy: per-pebble fair = 50,000 − sum(other 4 mids). Aggressive take
#    when market diverges. MM around basket-fair otherwise.
#
#  SOFT EDGE — SNACKPACK structure:
#    CHOCOLATE + VANILLA ≈ 19,940  (std 76)
#    Sum of all 5 ≈ 50,221         (std 190)
#    Tight std (170–360) + wide spread (16–18) → best raw MM ratio of any
#    product class (≈2.7 spread/ret-std, vs ≈1.0 elsewhere). Per-product MM
#    around micro-price + pair-constraint bias on fair.
#
#  GENERIC MM — all other 35 products:
#    Light passive MM around micro-price with category-tuned offsets. Lower
#    edge but with position limit 10 and active flow it's incremental free PnL.
# ============================================================================

POS_LIMIT = 10

CATEGORIES = {
    'GALAXY_SOUNDS': ['DARK_MATTER','BLACK_HOLES','PLANETARY_RINGS','SOLAR_WINDS','SOLAR_FLAMES'],
    'SLEEP_POD':     ['SUEDE','LAMB_WOOL','POLYESTER','NYLON','COTTON'],
    'MICROCHIP':     ['CIRCLE','OVAL','SQUARE','RECTANGLE','TRIANGLE'],
    'PEBBLES':       ['XS','S','M','L','XL'],
    'ROBOT':         ['VACUUMING','MOPPING','DISHES','LAUNDRY','IRONING'],
    'UV_VISOR':      ['YELLOW','AMBER','ORANGE','RED','MAGENTA'],
    'TRANSLATOR':    ['SPACE_GRAY','ASTRO_BLACK','ECLIPSE_CHARCOAL','GRAPHITE_MIST','VOID_BLUE'],
    'PANEL':         ['1X2','2X2','1X4','2X4','4X4'],
    'OXYGEN_SHAKE':  ['MORNING_BREATH','EVENING_BREATH','MINT','CHOCOLATE','GARLIC'],
    'SNACKPACK':     ['CHOCOLATE','VANILLA','PISTACHIO','STRAWBERRY','RASPBERRY'],
}
def sym(cat, name): return f'{cat}_{name}'

PEBBLES        = [sym('PEBBLES', n) for n in CATEGORIES['PEBBLES']]
PEBBLES_TOTAL  = 50_000
SNACKPACK      = [sym('SNACKPACK', n) for n in CATEGORIES['SNACKPACK']]
SNACK_C        = 'SNACKPACK_CHOCOLATE'
SNACK_V        = 'SNACKPACK_VANILLA'
SNACK_CV_TARGET = 19_940    # mean of CHOCOLATE+VANILLA across days 2/3/4

# ── Per-category MM offsets (tuned via 3-day sweep, +94k/day backtest) ─────
# Offsets sit just inside visible half-spread so quotes are at/near BBO without
# crossing the existing book. Sizes 6-8 are well below pos limit 10 so we can
# layer fills before refreshing. Combined offset+size tuning lifted backtest
# from +184k baseline → +283k over 3 days.
#
# Empirical spreads (from EDA): SNACKPACK 16-18, UV_VISOR/OXYGEN/GALAXY 12-15,
# PEBBLES 10-17, PANEL/TRANSLATOR/SLEEP_POD 8-10, ROBOT/MICROCHIP 7-12.
CATEGORY_MM = {
    'SNACKPACK':     {'offset': 6, 'size': 8, 'take_edge': 4},   # tightest MR class
    'UV_VISOR':      {'offset': 5, 'size': 7, 'take_edge': 3},
    'OXYGEN_SHAKE':  {'offset': 5, 'size': 7, 'take_edge': 3},
    'GALAXY_SOUNDS': {'offset': 6, 'size': 7, 'take_edge': 3},
    'PEBBLES':       {'offset': 6, 'size': 7, 'take_edge': 999}, # passive only — basket-take loses to spread
    'PANEL':         {'offset': 4, 'size': 6, 'take_edge': 3},
    'TRANSLATOR':    {'offset': 3, 'size': 6, 'take_edge': 3},
    'SLEEP_POD':     {'offset': 4, 'size': 6, 'take_edge': 3},
    'ROBOT':         {'offset': 4, 'size': 6, 'take_edge': 3},
    'MICROCHIP':     {'offset': 4, 'size': 6, 'take_edge': 3},
}

PRODUCT_TO_CAT = {sym(c, n): c for c, names in CATEGORIES.items() for n in names}


class Trader:

    def bid(self):
        return 0  # MAF was R2; unused in R5

    def run(self, state: TradingState):
        result: Dict[str, List[Order]] = {}

        # Cache mids per product for this tick (used by basket fair-value)
        mids: Dict[str, float] = {}
        for prod, od in state.order_depths.items():
            m = self._mid(od)
            if m is not None: mids[prod] = m

        # ── PEBBLES basket signal: sum should equal 50,000 ──
        # Lesson from naive impl: aggressive take on per-pebble basket-fair
        # bleeds money — captured "edge" is sub-spread noise on each individual
        # pebble. Instead we use the basket deviation as a SKEW on the passive
        # quotes only, biasing fair by a fraction of the per-pebble share.
        pebble_skew = 0.0
        if all(p in mids for p in PEBBLES):
            basket_dev = sum(mids[p] for p in PEBBLES) - PEBBLES_TOTAL
            # Apply only on meaningful deviations (typical noise std ≈ 2.8).
            # Each pebble bears 1/5 of the deviation; cap effect at ±5 ticks.
            if abs(basket_dev) > 6:
                pebble_skew = max(-5.0, min(5.0, -basket_dev / 5))

        # ── SNACKPACK pair constraint bias for CHOCOLATE/VANILLA ───────────
        # If C+V > target, both are relatively elevated → bias both fairs down
        # (and vice versa). Half the deviation is attributed to each side.
        cv_bias = 0.0
        if SNACK_C in mids and SNACK_V in mids:
            cv_dev = (mids[SNACK_C] + mids[SNACK_V]) - SNACK_CV_TARGET
            # Apply only when deviation is meaningful (std≈76, fade past 1σ).
            if abs(cv_dev) > 75:
                cv_bias = -cv_dev / 4   # subtract from each side's fair

        # ── Trade each product ─────────────────────────────────────────────
        for prod, od in state.order_depths.items():
            if not od.buy_orders or not od.sell_orders: continue
            cat = PRODUCT_TO_CAT.get(prod)
            if cat is None: continue
            cfg = CATEGORY_MM[cat]
            position = state.position.get(prod, 0)
            mid = mids.get(prod)
            if mid is None: continue
            micro, _, _ = self._micro(od)

            # Determine fair by product type
            if prod in PEBBLES:
                fair = micro + pebble_skew              # micro + small basket bias
            elif prod in (SNACK_C, SNACK_V):
                fair = micro + cv_bias                  # micro + pair bias
            else:
                fair = micro                            # generic micro-price

            result[prod] = self._mm_orders(
                prod, od, position, fair,
                offset=cfg['offset'], size=cfg['size'],
                take_edge=cfg['take_edge'],
            )

        return result, 0, ""

    # ── Helpers ──────────────────────────────────────────────────────────
    @staticmethod
    def _mid(od):
        if od and od.buy_orders and od.sell_orders:
            return (max(od.buy_orders.keys()) + min(od.sell_orders.keys())) / 2
        return None

    @staticmethod
    def _micro(od) -> Tuple[float, float, float]:
        bb = max(od.buy_orders.keys()); ba = min(od.sell_orders.keys())
        bv = od.buy_orders[bb]; av = -od.sell_orders[ba]
        if bv + av == 0:
            mid = (bb + ba) / 2
            return mid, 0.0, mid
        micro = (ba * bv + bb * av) / (bv + av)
        obi = (bv - av) / (bv + av)
        return micro, obi, (bb + ba) / 2

    def _mm_orders(self, prod: str, od: OrderDepth, position: int,
                   fair: float, offset: int, size: int, take_edge: int):
        """Take + passive MM around `fair` with inventory skew."""
        orders: List[Order] = []
        buy_cap  = POS_LIMIT - position
        sell_cap = POS_LIMIT + position

        # Take when book is decisively off fair
        for price in sorted(od.sell_orders.keys()):
            if price > fair - take_edge or buy_cap <= 0: break
            qty = min(-od.sell_orders[price], buy_cap)
            if qty > 0:
                orders.append(Order(prod, price, qty)); buy_cap -= qty
        for price in sorted(od.buy_orders.keys(), reverse=True):
            if price < fair + take_edge or sell_cap <= 0: break
            qty = min(od.buy_orders[price], sell_cap)
            if qty > 0:
                orders.append(Order(prod, price, -qty)); sell_cap -= qty

        # Passive quotes with inventory skew
        # Pos limit is only 10 — even modest skew dominates passive levels.
        skew = (position / POS_LIMIT) * (offset // 2 + 1)
        bid_p = round(fair - offset - skew)
        ask_p = round(fair + offset - skew)
        if bid_p >= ask_p: ask_p = bid_p + 1
        if buy_cap > 0:
            orders.append(Order(prod, bid_p, min(size, buy_cap)))
        if sell_cap > 0:
            orders.append(Order(prod, ask_p, -min(size, sell_cap)))
        return orders
