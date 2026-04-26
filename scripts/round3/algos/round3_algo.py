from datamodel import OrderDepth, UserId, TradingState, Order
from typing import List, Dict, Tuple
import json
import math

# ============================================================================
#  ROUND 3  —  HYDROGEL_PACK, VELVETFRUIT_EXTRACT, 10 VEV_<K> vouchers
#
#  Asset map (with empirical reads from days 0/1/2):
#
#  HYDROGEL_PACK (HG):  delta-1, MR around 10,000, std 32, spread ~16, ret_std 2.17.
#    Strategy: MM around a Kalman-tracked anchor (10k), wide offsets, aggressive
#    take when book mispriced vs fair.
#
#  VELVETFRUIT_EXTRACT (VE): delta-1 underlying for vouchers. Mean ~5250, std 16,
#    spread ~5, ret_std 1.13. Mild upward drift across days (5244→5265→5295).
#    Strategy: MM around micro-price with tight offsets; this also serves as the
#    delta-hedge instrument.
#
#  VEV_4000, VEV_4500: deep ITM, time value ≈ 0. Trade as synthetic (S − K).
#    Spread 16/21 → wide → MM with a small offset captures spread cleanly.
#
#  VEV_5000–5500: active options on VE. IV smile clusters at σ ≈ 0.0125 per √day.
#    BS fair (r=0). MM around fair; take when book is > 1 unit off fair.
#
#  VEV_6000, VEV_6500: pinned at 0.50 floor (S far below K). Skip.
#
#  Delta hedging: track signed option deltas from voucher fills; if aggregate
#  |delta| > threshold, hedge with VE.
# ============================================================================

# ── Position limits ─────────────────────────────────────────────────────────
POS_LIMIT_DELTA1  = 200
POS_LIMIT_VOUCHER = 300

HG = "HYDROGEL_PACK"
VE = "VELVETFRUIT_EXTRACT"
STRIKES = [4000, 4500, 5000, 5100, 5200, 5300, 5400, 5500, 6000, 6500]
def vev(K): return f"VEV_{K}"

# ── HYDROGEL_PACK MM (10,000-anchored, spread ~16 wide) ────────────────────
# Tuned via 3-day backtest sweep: skew=10, acf=0.4 are big wins. Kalman is
# essential — disabling it dropped PnL from +106k to −0.3k.
#
# HG_FAIR_PULL_CAP: post-mortem from live submission 464472 — when HG drifted
# 50pts away from the 10000 anchor, the Kalman pull biased fair so far above
# mid that the algo bought to the +200 long limit and held the bag. Capping
# the total fair-vs-mid bias at ±8 turns trending periods from -909 → +214 on
# the partial-day case, costing ~9k on full 3-day backtest (108k → 99k). Net:
# trades a bit of MR upside for trending-regime survival.
HG_BID_OFFSET     = 6
HG_ASK_OFFSET     = 6
HG_SIZE           = 20
HG_MAX_SKEW_TICKS = 10
HG_KALMAN_ALPHA   = 0.0001
HG_KALMAN_COEF    = 0.20
HG_ACF_COEF       = 0.40
HG_OBI_VETO       = 0.65
HG_FAIR_PULL_CAP  = 8         # |fair − mid| ≤ this many ticks

# ── VELVETFRUIT_EXTRACT MM (spread ~5, narrow) ─────────────────────────────
# Note: spread is 5 but ve_off=1 was catastrophic on day 2 (−42k). ve_off=3
# stays clear of the tight existing market and avoids adverse selection.
VE_BID_OFFSET     = 3
VE_ASK_OFFSET     = 3
VE_SIZE           = 15
VE_MAX_SKEW_TICKS = 2
VE_ACF_COEF       = 0.15

# ── Voucher pricing ─────────────────────────────────────────────────────────
# Time to expiry at start of round 3 = 5 days. Within-day clock fraction handled
# from state.timestamp (0..999_900). Days completed before the live round = 0.
# Round 3's data was: day 0 (TTE 8), day 1 (TTE 7), day 2 (TTE 6) historical,
# round 3 live = day 3 (TTE 5).
TTE_AT_ROUND_START = 5.0
TICKS_PER_DAY      = 1_000_000

# Flat IV smile on traded strikes (5000–5500) — empirical mean ≈ 0.0125/√day
IV_PER_SQRT_DAY    = 0.0125

# Deep-ITM strikes (priced as pure intrinsic + tiny TV)
DEEP_ITM_STRIKES   = [4000, 4500]
# Active option strikes (anchored on market mid, light MM around it)
ACTIVE_STRIKES     = [5000, 5100, 5200, 5300, 5400, 5500]
# Skipped (floor-pinned)
SKIP_STRIKES       = [6000, 6500]
# Per-strike empirical IV (computed from days 0/1/2 mid snapshots).
# Using these instead of a flat smile makes BS fair ≈ market mid by construction.
IV_BY_STRIKE = {
    5000: 0.0126, 5100: 0.0125, 5200: 0.0127,
    5300: 0.0129, 5400: 0.0121, 5500: 0.0130,
}

# Active strikes (5000–5500): spreads are 1–3 ticks wide. Sweeps confirmed
# active-voucher MM is approximately neutral (+91k → +91k disabled). Disabled
# by default; flip VOUCHER_SIZE > 0 to re-enable for further exploration.
VOUCHER_BID_OFFSET = 2
VOUCHER_ASK_OFFSET = 2
VOUCHER_SIZE       = 0        # disabled — see comment above
VOUCHER_TAKE_EDGE  = 999      # disabled

# Deep-ITM (4000, 4500): spread ~16-21 wide → MM well inside, capture spread.
DEEP_BID_OFFSET    = 3
DEEP_ASK_OFFSET    = 3
DEEP_SIZE          = 15

# ── Delta hedging ───────────────────────────────────────────────────────────
DELTA_HEDGE_THRESHOLD = 30    # hedge when |aggregate delta| exceeds this
DELTA_HEDGE_MAX_SIZE  = 40    # max hedge clip per tick

MID_HISTORY_SIZE = 100


# ── BS helpers (r=0) ────────────────────────────────────────────────────────
def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def bs_call(S: float, K: float, T: float, sigma: float) -> float:
    if T <= 0 or sigma <= 0:
        return max(0.0, S - K)
    sT = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + 0.5 * sigma * sigma * T) / sT
    d2 = d1 - sT
    return S * _norm_cdf(d1) - K * _norm_cdf(d2)

def bs_delta(S: float, K: float, T: float, sigma: float) -> float:
    if T <= 0 or sigma <= 0:
        return 1.0 if S > K else 0.0
    sT = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + 0.5 * sigma * sigma * T) / sT
    return _norm_cdf(d1)


class Trader:

    def bid(self):
        # MAF mechanic was Round 2 — Round 3 spec doesn't mention it, return 0 just in case.
        return 0

    # ── Main entry ───────────────────────────────────────────────────────────
    def run(self, state: TradingState):
        result: Dict[str, List[Order]] = {}
        memory = json.loads(state.traderData) if state.traderData else {}
        last_mid = memory.get("last_mid", {})
        hg_hist  = memory.get("hg_hist", [])
        ve_hist  = memory.get("ve_hist", [])
        hg_anchor = memory.get("hg_anchor", 10000.0)

        # Time to expiry in days (5 days at start of round, decreasing during)
        tte = max(0.0, TTE_AT_ROUND_START - state.timestamp / TICKS_PER_DAY)

        # ── HYDROGEL_PACK ────────────────────────────────────────────────────
        hg_od  = state.order_depths.get(HG)
        hg_pos = state.position.get(HG, 0)
        result[HG], hg_anchor = self._trade_hg(hg_od, hg_pos, hg_hist, hg_anchor,
                                                last_mid.get(HG))

        # ── VELVETFRUIT_EXTRACT ──────────────────────────────────────────────
        ve_od  = state.order_depths.get(VE)
        ve_pos = state.position.get(VE, 0)
        ve_orders, ve_mid = self._trade_ve(ve_od, ve_pos, last_mid.get(VE))
        result[VE] = ve_orders

        # ── Vouchers ─────────────────────────────────────────────────────────
        # Aggregate signed option delta across all voucher positions
        total_option_delta = 0.0
        if ve_mid is not None:
            for K in DEEP_ITM_STRIKES:
                sym = vev(K)
                pos = state.position.get(sym, 0)
                od  = state.order_depths.get(sym)
                if od is not None:
                    result[sym] = self._trade_deep_itm(od, pos, ve_mid, K)
                # Deep ITM: delta ≈ 1
                total_option_delta += pos * 1.0

            for K in ACTIVE_STRIKES:
                sym = vev(K)
                pos = state.position.get(sym, 0)
                od  = state.order_depths.get(sym)
                if od is not None:
                    result[sym] = self._trade_active_voucher(od, pos, ve_mid, K, tte)
                d = bs_delta(ve_mid, float(K), tte, IV_PER_SQRT_DAY)
                total_option_delta += pos * d

            # Skip 6000/6500 entirely

        # ── Delta hedge: adjust VE orders if option delta is large ───────────
        if ve_mid is not None and abs(total_option_delta) > DELTA_HEDGE_THRESHOLD:
            # We have +Δ option exposure → need to short VE to hedge (and vice versa).
            # The signed option delta in *VE units* is total_option_delta directly
            # (calls have delta in [0, 1], so 100 long calls of delta 0.5 ≈ +50 VE).
            # Hedge size: bring net |VE pos + option delta| toward zero.
            target_ve = -round(total_option_delta)  # desired VE position
            ve_diff = target_ve - ve_pos
            if ve_diff != 0:
                hedge_qty = max(-DELTA_HEDGE_MAX_SIZE,
                                min(DELTA_HEDGE_MAX_SIZE, ve_diff))
                hedge_orders = self._delta_hedge_ve(ve_od, ve_pos, hedge_qty)
                # Append hedge orders to existing VE order list
                result[VE] = result[VE] + hedge_orders

        # ── Update memory ────────────────────────────────────────────────────
        new_last_mid = dict(last_mid)
        hg_mid = self._mid(hg_od)
        if hg_mid is not None:
            new_last_mid[HG] = hg_mid
            hg_hist = (hg_hist + [hg_mid])[-MID_HISTORY_SIZE:]
        if ve_mid is not None:
            new_last_mid[VE] = ve_mid
            ve_hist = (ve_hist + [ve_mid])[-MID_HISTORY_SIZE:]

        memory["last_mid"]  = new_last_mid
        memory["hg_hist"]   = hg_hist
        memory["ve_hist"]   = ve_hist
        memory["hg_anchor"] = hg_anchor
        return result, 0, json.dumps(memory)

    # ── Helpers ──────────────────────────────────────────────────────────────
    @staticmethod
    def _mid(od):
        if od and od.buy_orders and od.sell_orders:
            return (max(od.buy_orders.keys()) + min(od.sell_orders.keys())) / 2
        return None

    @staticmethod
    def _micro_imbalance(od) -> Tuple[float, float, float]:
        """Return (micro-price, OBI, mid). Caller must check book is two-sided."""
        bb = max(od.buy_orders.keys()); ba = min(od.sell_orders.keys())
        bv = od.buy_orders[bb]; av = -od.sell_orders[ba]
        if bv + av == 0:
            return (bb + ba) / 2, 0.0, (bb + ba) / 2
        micro = (ba * bv + bb * av) / (bv + av)
        obi = (bv - av) / (bv + av)
        return micro, obi, (bb + ba) / 2

    # ── HG ───────────────────────────────────────────────────────────────────
    def _trade_hg(self, od, position, hist, anchor, prev_mid):
        orders: List[Order] = []
        if od is None or not od.buy_orders or not od.sell_orders:
            return orders, anchor

        micro, obi, mid = self._micro_imbalance(od)
        fair = micro

        # Lag-1 ACF (mean-reversion) with OBI veto
        if hist and abs(obi) <= HG_OBI_VETO:
            fair -= HG_ACF_COEF * (mid - hist[-1])

        # Kalman-tracked anchor (10,000 long-run)
        new_anchor = (1 - HG_KALMAN_ALPHA) * anchor + HG_KALMAN_ALPHA * mid
        fair += HG_KALMAN_COEF * (new_anchor - mid)

        # Cap the fair-vs-mid bias — protects against trending regimes where the
        # Kalman pull would otherwise drive us to position-limit the wrong way.
        fair = max(mid - HG_FAIR_PULL_CAP, min(mid + HG_FAIR_PULL_CAP, fair))

        buy_cap  = POS_LIMIT_DELTA1 - position
        sell_cap = POS_LIMIT_DELTA1 + position

        # Take mispriced asks/bids (price < fair → buy; price > fair → sell)
        for price in sorted(od.sell_orders.keys()):
            if price >= fair or buy_cap <= 0: break
            qty = min(-od.sell_orders[price], buy_cap)
            orders.append(Order(HG, price, qty)); buy_cap -= qty
        for price in sorted(od.buy_orders.keys(), reverse=True):
            if price <= fair or sell_cap <= 0: break
            qty = min(od.buy_orders[price], sell_cap)
            orders.append(Order(HG, price, -qty)); sell_cap -= qty

        # Passive quotes with inventory skew
        skew = (position / POS_LIMIT_DELTA1) * HG_MAX_SKEW_TICKS
        bid_p = round(fair - HG_BID_OFFSET - skew)
        ask_p = round(fair + HG_ASK_OFFSET - skew)
        if bid_p >= ask_p:
            ask_p = bid_p + 1
        if buy_cap > 0:
            orders.append(Order(HG, bid_p, min(HG_SIZE, buy_cap)))
        if sell_cap > 0:
            orders.append(Order(HG, ask_p, -min(HG_SIZE, sell_cap)))

        return orders, new_anchor

    # ── VE ───────────────────────────────────────────────────────────────────
    def _trade_ve(self, od, position, prev_mid):
        orders: List[Order] = []
        if od is None or not od.buy_orders or not od.sell_orders:
            return orders, None

        micro, obi, mid = self._micro_imbalance(od)
        fair = micro

        # Mild ACF — VE drifts so don't over-anchor
        if prev_mid is not None and abs(obi) <= 0.7:
            fair -= VE_ACF_COEF * (mid - prev_mid)

        buy_cap  = POS_LIMIT_DELTA1 - position
        sell_cap = POS_LIMIT_DELTA1 + position

        # Take
        for price in sorted(od.sell_orders.keys()):
            if price >= fair or buy_cap <= 0: break
            qty = min(-od.sell_orders[price], buy_cap)
            orders.append(Order(VE, price, qty)); buy_cap -= qty
        for price in sorted(od.buy_orders.keys(), reverse=True):
            if price <= fair or sell_cap <= 0: break
            qty = min(od.buy_orders[price], sell_cap)
            orders.append(Order(VE, price, -qty)); sell_cap -= qty

        # Passive quotes
        skew = (position / POS_LIMIT_DELTA1) * VE_MAX_SKEW_TICKS
        bid_p = round(fair - VE_BID_OFFSET - skew)
        ask_p = round(fair + VE_ASK_OFFSET - skew)
        if bid_p >= ask_p:
            ask_p = bid_p + 1
        if buy_cap > 0:
            orders.append(Order(VE, bid_p, min(VE_SIZE, buy_cap)))
        if sell_cap > 0:
            orders.append(Order(VE, ask_p, -min(VE_SIZE, sell_cap)))

        return orders, mid

    def _delta_hedge_ve(self, od, position, qty):
        """Aggressive (cross-spread) hedge for VE delta-neutralization."""
        orders: List[Order] = []
        if od is None or qty == 0: return orders
        if qty > 0 and od.sell_orders:
            ba = min(od.sell_orders.keys())
            available = -od.sell_orders[ba]
            buy_cap = POS_LIMIT_DELTA1 - position
            q = min(qty, available, buy_cap)
            if q > 0: orders.append(Order(VE, ba, q))
        elif qty < 0 and od.buy_orders:
            bb = max(od.buy_orders.keys())
            available = od.buy_orders[bb]
            sell_cap = POS_LIMIT_DELTA1 + position
            q = min(-qty, available, sell_cap)
            if q > 0: orders.append(Order(VE, bb, -q))
        return orders

    # ── Deep-ITM voucher (synthetic at S-K, MM tightly) ──────────────────────
    def _trade_deep_itm(self, od, position, ve_mid, K):
        orders: List[Order] = []
        if od is None or not od.buy_orders or not od.sell_orders:
            return orders
        sym = vev(K)
        fair = max(0.5, ve_mid - K)        # synthetic fair (intrinsic, floored)

        buy_cap  = POS_LIMIT_VOUCHER - position
        sell_cap = POS_LIMIT_VOUCHER + position

        # Take
        for price in sorted(od.sell_orders.keys()):
            if price >= fair - 0.5 or buy_cap <= 0: break
            qty = min(-od.sell_orders[price], buy_cap)
            orders.append(Order(sym, price, qty)); buy_cap -= qty
        for price in sorted(od.buy_orders.keys(), reverse=True):
            if price <= fair + 0.5 or sell_cap <= 0: break
            qty = min(od.buy_orders[price], sell_cap)
            orders.append(Order(sym, price, -qty)); sell_cap -= qty

        # Passive quotes
        skew_off = (position / POS_LIMIT_VOUCHER) * 1.0
        bid_p = round(fair - DEEP_BID_OFFSET - skew_off)
        ask_p = round(fair + DEEP_ASK_OFFSET - skew_off)
        if bid_p >= ask_p:
            ask_p = bid_p + 1
        if buy_cap > 0:
            orders.append(Order(sym, bid_p, min(DEEP_SIZE, buy_cap)))
        if sell_cap > 0:
            orders.append(Order(sym, ask_p, -min(DEEP_SIZE, sell_cap)))

        return orders

    # ── Active voucher (BS-priced, take-only when book diverges) ─────────────
    # MM around BS fair was bleeding because BS fair ≠ market mid in the data —
    # quotes ended up skewed and one-sided fills accumulated. Strategy switch:
    # use market micro-price as MM anchor, BS-fair only as a take-side filter.
    def _trade_active_voucher(self, od, position, ve_mid, K, tte):
        orders: List[Order] = []
        if od is None or not od.buy_orders or not od.sell_orders:
            return orders
        sym = vev(K)
        sigma = IV_BY_STRIKE.get(K, IV_PER_SQRT_DAY)
        bs_fair = bs_call(ve_mid, float(K), tte, sigma)
        if bs_fair < 0.5: bs_fair = 0.5

        micro, _, mid = self._micro_imbalance(od)
        # Anchor MM on market micro; only take if both market and BS agree on
        # mispricing (book ask < both micro − edge AND BS_fair − edge).
        mm_fair = micro

        buy_cap  = POS_LIMIT_VOUCHER - position
        sell_cap = POS_LIMIT_VOUCHER + position

        # Conservative take: only if book crosses *both* anchors by a margin
        for price in sorted(od.sell_orders.keys()):
            if buy_cap <= 0: break
            if price >= bs_fair - VOUCHER_TAKE_EDGE: break
            if price >= mid - 1: break
            qty = min(-od.sell_orders[price], buy_cap, 5)  # small clip per take
            orders.append(Order(sym, price, qty)); buy_cap -= qty
        for price in sorted(od.buy_orders.keys(), reverse=True):
            if sell_cap <= 0: break
            if price <= bs_fair + VOUCHER_TAKE_EDGE: break
            if price <= mid + 1: break
            qty = min(od.buy_orders[price], sell_cap, 5)
            orders.append(Order(sym, price, -qty)); sell_cap -= qty

        # Passive MM around micro-price (not BS fair) so quotes stay symmetric
        skew_off = (position / POS_LIMIT_VOUCHER) * 1.0
        bid_p = max(1, round(mm_fair - VOUCHER_BID_OFFSET - skew_off))
        ask_p = round(mm_fair + VOUCHER_ASK_OFFSET - skew_off)
        if bid_p >= ask_p:
            ask_p = bid_p + 1
        if buy_cap > 0:
            orders.append(Order(sym, bid_p, min(VOUCHER_SIZE, buy_cap)))
        if sell_cap > 0:
            orders.append(Order(sym, ask_p, -min(VOUCHER_SIZE, sell_cap)))

        return orders
