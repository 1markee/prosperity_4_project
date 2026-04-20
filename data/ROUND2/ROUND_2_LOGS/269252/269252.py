from datamodel import OrderDepth, UserId, TradingState, Order
from typing import List, Dict

# ── Configuration ─────────────────────────────────────────────────────────────
# UPDATE this when the actual per-product limit is confirmed on the portal
POSITION_LIMIT = 80

IPR = "INTARIAN_PEPPER_ROOT"   # Trend product  → strategy: always max long
OSM = "ASH_COATED_OSMIUM"      # Mean-revert    → strategy: market make around 10,000

# Osmium market-making parameters
OSM_QUOTE_OFFSET   = 1   # ticks from fair value for passive bid/ask quotes
OSM_PASSIVE_SIZE   = 10  # max units per passive quote
OSM_MAX_SKEW_TICKS = 3   # max inventory skew applied to quotes (in ticks)


class Trader:

    def bid(self):
        # Required for Round 2 submission format; ignored in all other rounds
        return 15

    def run(self, state: TradingState):
        result: Dict[str, List[Order]] = {}

        pos_ipr = state.position.get(IPR, 0)
        pos_osm = state.position.get(OSM, 0)

        result[IPR] = self._trade_ipr(state.order_depths.get(IPR), pos_ipr)
        result[OSM] = self._trade_osm(state.order_depths.get(OSM), pos_osm)

        return result, 0, ""

    # ── INTARIAN_PEPPER_ROOT: Max Long (ride the trend) ───────────────────────
    def _trade_ipr(self, order_depth: OrderDepth, position: int) -> List[Order]:
        """
        Price trends up ~1,000 ticks per day. Strategy: hold max long position
        at all times. Aggressively sweep all available asks each tick, then post
        a passive bid just inside the spread for any remaining capacity.
        """
        orders = []
        if order_depth is None:
            return orders

        buy_cap = POSITION_LIMIT - position
        if buy_cap <= 0:
            return orders

        # Sweep every ask level from cheapest upward
        for price in sorted(order_depth.sell_orders.keys()):
            if buy_cap <= 0:
                break
            # sell_orders quantities are negative, so negate to get size
            available = -order_depth.sell_orders[price]
            qty = min(available, buy_cap)
            orders.append(Order(IPR, price, qty))
            buy_cap -= qty

        # If still room, post a passive bid 1 tick above the best bid
        # (inside the spread) so bots may sell to us next iteration
        if buy_cap > 0 and order_depth.buy_orders:
            best_bid = max(order_depth.buy_orders.keys())
            orders.append(Order(IPR, best_bid + 1, buy_cap))

        return orders

    # ── ASH_COATED_OSMIUM: Market Making ──────────────────────────────────────
    def _trade_osm(self, order_depth: OrderDepth, position: int) -> List[Order]:
        """
        Price oscillates around ~10,000 with a ~16-tick spread. Strategy:
          1. Take: immediately buy asks below fair value / sell bids above it.
          2. Make: post passive bid + ask quotes inside the spread.
        Inventory skew shifts both quotes to encourage unwinding large positions.
        """
        orders = []
        if order_depth is None:
            return orders
        if not order_depth.buy_orders or not order_depth.sell_orders:
            return orders

        best_bid = max(order_depth.buy_orders.keys())
        best_ask = min(order_depth.sell_orders.keys())
        # Micro-price: volume-weighted fair value. Heavy bid volume pulls fair
        # toward the ask (the next trade is more likely to happen on the side
        # with LESS volume), which is a better fair-value estimate than raw mid.
        bid_vol = order_depth.buy_orders[best_bid]
        ask_vol = -order_depth.sell_orders[best_ask]
        fair = (best_ask * bid_vol + best_bid * ask_vol) / (bid_vol + ask_vol)

        # Remaining capacity on each side
        buy_cap  = POSITION_LIMIT - position   # how many more units we can buy
        sell_cap = POSITION_LIMIT + position   # how many more units we can sell short

        # ── Step 1: Take mispriced orders ─────────────────────────────────────
        # Buy any ask that is strictly below fair value (someone selling too cheap)
        for price in sorted(order_depth.sell_orders.keys()):
            if price >= fair or buy_cap <= 0:
                break
            qty = min(-order_depth.sell_orders[price], buy_cap)
            orders.append(Order(OSM, price, qty))
            buy_cap -= qty

        # Sell any bid that is strictly above fair value (someone buying too high)
        for price in sorted(order_depth.buy_orders.keys(), reverse=True):
            if price <= fair or sell_cap <= 0:
                break
            qty = min(order_depth.buy_orders[price], sell_cap)
            orders.append(Order(OSM, price, -qty))
            sell_cap -= qty

        # ── Step 2: Post passive quotes with inventory skew ───────────────────
        # Skew: if we're long, shift BOTH quotes down to encourage selling
        #       if we're short, shift BOTH quotes up to encourage buying
        # This prevents getting stuck holding a large one-sided position.
        skew = round(position / POSITION_LIMIT * OSM_MAX_SKEW_TICKS)

        bid_price = round(fair) - OSM_QUOTE_OFFSET - skew
        ask_price = round(fair) + OSM_QUOTE_OFFSET - skew

        # Safety: quotes must not cross
        if bid_price >= ask_price:
            ask_price = bid_price + 1

        bid_qty = min(OSM_PASSIVE_SIZE, buy_cap)
        ask_qty = min(OSM_PASSIVE_SIZE, sell_cap)

        if bid_qty > 0:
            orders.append(Order(OSM, bid_price, bid_qty))
        if ask_qty > 0:
            orders.append(Order(OSM, ask_price, -ask_qty))

        return orders