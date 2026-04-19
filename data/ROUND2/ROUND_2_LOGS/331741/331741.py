from datamodel import OrderDepth, UserId, TradingState, Order
from typing import List, Dict
import json
import math

# ── Configuration ─────────────────────────────────────────────────────────────
POSITION_LIMIT = 80

IPR = "INTARIAN_PEPPER_ROOT"   # trend product → max-long
OSM = "ASH_COATED_OSMIUM"      # mean-revert   → MM around 10,000

# ── OSM base MM (Tier 1, tuned) ───────────────────────────────────────────────
OSM_BID_OFFSET             = 1
OSM_ASK_OFFSET             = 1
OSM_PASSIVE_SIZE           = 10
OSM_MAX_SKEW_TICKS         = 3
# Lag-1 ACF bias: fair -= OSM_ACF_COEF × last_return. Tuned jointly with Kalman
# (univariate best 0.5; with Kalman enabled, 0.35 is optimal — the Kalman term
# absorbs some of the mean-reversion signal, so ACF is reduced).
OSM_ACF_COEF               = 0.35
IPR_ACF_PATIENCE_THRESHOLD = 2

# ── Tier 2/3 sweep results (all disabled EXCEPT Kalman anchor) ───────────────
# 2a Multi-tick ACF:  REJECTED (ΔPnL −225 at coef 0.1, worse at higher)
# 2b OBI fair-input:  REJECTED (ΔPnL −1,714 at coef 1.0, −19,624 at coef 6.0)
#   — regression showed high univariate R², but imbalance is already priced
#     into micro-price, so biasing fair on top fades moves we can't trade.
# 2c Z-score take:    REJECTED (worst: ΔPnL −190,799) — spread cost > reversion
# 2d Vol-scaled off:  REJECTED (ΔPnL −79 to −802) — vol is too stable on R2
# 2e Kalman anchor:   ACCEPTED — ΔPnL +2,266 at α=0.001, coef=0.20 (stand-alone)
#                                ΔPnL +4,802 at ACF 0.35 + α=0.0005, coef=0.20
# 2f Avellaneda-Stoikov: REJECTED (ΔPnL −66 to −1,312) — no gain over ACF fair
OSM_AR2_COEF              = 0.0
OSM_OBI_COEF              = 0.0
OSM_ZSCORE_WIN            = 50
OSM_ZSCORE_THRESHOLD      = float('inf')
OSM_ZSCORE_TAKE_SIZE      = 15
OSM_VOL_OFFSET_ENABLED    = False
OSM_VOL_OFFSET_BASE_STD   = 1.58
OSM_KALMAN_ENABLED        = True        # ← accepted Tier-2 feature
OSM_KALMAN_ALPHA          = 0.0005       # EWMA rate (~half-life 1,400 ticks)
OSM_KALMAN_COEF           = 0.20         # fair += 0.20 × (anchor − mid)
OSM_AS_ENABLED            = False
OSM_AS_GAMMA              = 0.1
OSM_AS_SIGMA              = 3.7

# MAF (Market Access Fee)
MAF_BID = 800

# State-management: length of per-product mid-price history kept in traderData
MID_HISTORY_SIZE = 200


class Trader:

    def bid(self):
        return MAF_BID

    # ── Main entry ───────────────────────────────────────────────────────────
    def run(self, state: TradingState):
        result: Dict[str, List[Order]] = {}
        memory   = json.loads(state.traderData) if state.traderData else {}
        last_mid = memory.get("last_mid", {})
        hist_osm = memory.get("osm_hist", [])
        anchor   = memory.get("osm_anchor", 10000.0)

        ipr_od = state.order_depths.get(IPR)
        osm_od = state.order_depths.get(OSM)
        pos_ipr = state.position.get(IPR, 0)
        pos_osm = state.position.get(OSM, 0)

        result[IPR] = self._trade_ipr(ipr_od, pos_ipr, last_mid.get(IPR))
        result[OSM], new_anchor = self._trade_osm(
            osm_od, pos_osm, hist_osm, anchor, state.timestamp
        )

        # Update memory for next tick
        new_last_mid = dict(last_mid)
        ipr_mid = self._mid(ipr_od)
        if ipr_mid is not None:
            new_last_mid[IPR] = ipr_mid
        osm_mid = self._mid(osm_od)
        if osm_mid is not None:
            new_last_mid[OSM] = osm_mid
            hist_osm = (hist_osm + [osm_mid])[-MID_HISTORY_SIZE:]

        memory["last_mid"] = new_last_mid
        memory["osm_hist"] = hist_osm
        memory["osm_anchor"] = new_anchor
        return result, 0, json.dumps(memory)

    # ── Helpers ──────────────────────────────────────────────────────────────
    @staticmethod
    def _mid(order_depth):
        if order_depth and order_depth.buy_orders and order_depth.sell_orders:
            return (max(order_depth.buy_orders.keys()) +
                    min(order_depth.sell_orders.keys())) / 2
        return None

    # ── IPR ──────────────────────────────────────────────────────────────────
    def _trade_ipr(self, order_depth: OrderDepth, position: int, prev_mid):
        orders: List[Order] = []
        if order_depth is None or not order_depth.sell_orders:
            return orders
        buy_cap = POSITION_LIMIT - position
        if buy_cap <= 0:
            return orders

        current_mid = self._mid(order_depth)
        last_return = (current_mid - prev_mid) if (current_mid is not None and prev_mid is not None) else 0.0

        # Patience after a strong up-tick
        if last_return > IPR_ACF_PATIENCE_THRESHOLD:
            if order_depth.buy_orders:
                best_bid = max(order_depth.buy_orders.keys())
                orders.append(Order(IPR, best_bid + 1, buy_cap))
            return orders

        # Normal sweep
        for price in sorted(order_depth.sell_orders.keys()):
            if buy_cap <= 0:
                break
            qty = min(-order_depth.sell_orders[price], buy_cap)
            orders.append(Order(IPR, price, qty))
            buy_cap -= qty
        if buy_cap > 0 and order_depth.buy_orders:
            best_bid = max(order_depth.buy_orders.keys())
            orders.append(Order(IPR, best_bid + 1, buy_cap))
        return orders

    # ── OSM ──────────────────────────────────────────────────────────────────
    def _trade_osm(self, order_depth: OrderDepth, position: int,
                   hist: List[float], anchor: float, timestamp: int):
        orders: List[Order] = []
        if order_depth is None or not order_depth.buy_orders or not order_depth.sell_orders:
            return orders, anchor

        best_bid = max(order_depth.buy_orders.keys())
        best_ask = min(order_depth.sell_orders.keys())
        bid_vol  = order_depth.buy_orders[best_bid]
        ask_vol  = -order_depth.sell_orders[best_ask]
        mid      = (best_bid + best_ask) / 2

        micro     = (best_ask * bid_vol + best_bid * ask_vol) / (bid_vol + ask_vol)
        imbalance = (bid_vol - ask_vol) / (bid_vol + ask_vol)

        # ── Fair value composition ──────────────────────────────────────────
        fair = micro

        # AR(1) — lag-1 ACF bias (established winner)
        if hist:
            fair -= OSM_ACF_COEF * (mid - hist[-1])
        # AR(2) — toggle
        if len(hist) >= 2 and OSM_AR2_COEF != 0.0:
            fair -= OSM_AR2_COEF * (hist[-1] - hist[-2])
        # OBI
        fair += OSM_OBI_COEF * imbalance
        # Kalman anchor bias
        new_anchor = anchor
        if OSM_KALMAN_ENABLED:
            new_anchor = (1 - OSM_KALMAN_ALPHA) * anchor + OSM_KALMAN_ALPHA * mid
            fair += OSM_KALMAN_COEF * (new_anchor - mid)

        # ── Offsets (possibly vol-scaled or AS-derived) ─────────────────────
        bid_off, ask_off = OSM_BID_OFFSET, OSM_ASK_OFFSET
        if OSM_VOL_OFFSET_ENABLED and len(hist) >= 50:
            recent = hist[-50:]
            mean_r = sum(recent) / len(recent)
            std_r  = (sum((x - mean_r)**2 for x in recent) / len(recent)) ** 0.5
            scale  = std_r / OSM_VOL_OFFSET_BASE_STD if OSM_VOL_OFFSET_BASE_STD > 0 else 1.0
            bid_off = max(1, round(OSM_BID_OFFSET * scale))
            ask_off = max(1, round(OSM_ASK_OFFSET * scale))

        as_skew_adjust = 0.0
        if OSM_AS_ENABLED:
            # Reservation price: s - q·γσ²(T-t)
            q = position / POSITION_LIMIT
            time_frac = max(0.0, 1 - timestamp / 1_000_000)
            as_skew_adjust = q * OSM_AS_GAMMA * (OSM_AS_SIGMA ** 2) * time_frac
            # Optimal half-spread
            half = 0.5 * OSM_AS_GAMMA * (OSM_AS_SIGMA ** 2) * time_frac + \
                   (1 / OSM_AS_GAMMA) * math.log(1 + OSM_AS_GAMMA)
            bid_off = max(1, round(half))
            ask_off = max(1, round(half))

        buy_cap  = POSITION_LIMIT - position
        sell_cap = POSITION_LIMIT + position

        # ── Z-score take overlay ────────────────────────────────────────────
        if OSM_ZSCORE_THRESHOLD < float('inf') and len(hist) >= OSM_ZSCORE_WIN:
            recent = hist[-OSM_ZSCORE_WIN:]
            m_r = sum(recent) / len(recent)
            s_r = (sum((x - m_r)**2 for x in recent) / len(recent)) ** 0.5
            if s_r > 0:
                z = (mid - m_r) / s_r
                if z > OSM_ZSCORE_THRESHOLD and sell_cap > 0:
                    available = order_depth.buy_orders.get(best_bid, 0)
                    qty = min(OSM_ZSCORE_TAKE_SIZE, sell_cap, available)
                    if qty > 0:
                        orders.append(Order(OSM, best_bid, -qty))
                        sell_cap -= qty
                elif z < -OSM_ZSCORE_THRESHOLD and buy_cap > 0:
                    available = -order_depth.sell_orders.get(best_ask, 0)
                    qty = min(OSM_ZSCORE_TAKE_SIZE, buy_cap, available)
                    if qty > 0:
                        orders.append(Order(OSM, best_ask, qty))
                        buy_cap -= qty

        # ── Take mispriced orders against fair ──────────────────────────────
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

        # ── Passive quotes with skew ────────────────────────────────────────
        skew = (position / POSITION_LIMIT) * OSM_MAX_SKEW_TICKS + as_skew_adjust

        bid_price = round(fair - bid_off - skew)
        ask_price = round(fair + ask_off - skew)
        if bid_price >= ask_price:
            ask_price = bid_price + 1

        bid_qty = min(OSM_PASSIVE_SIZE, buy_cap)
        ask_qty = min(OSM_PASSIVE_SIZE, sell_cap)
        if bid_qty > 0:
            orders.append(Order(OSM, bid_price, bid_qty))
        if ask_qty > 0:
            orders.append(Order(OSM, ask_price, -ask_qty))

        return orders, new_anchor