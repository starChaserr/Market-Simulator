from __future__ import annotations

import argparse
import json
import logging
import math
import time
import urllib.parse
import urllib.request
from collections import deque
from typing import Any


OPEN_STATUSES = {"open", "partially_filled", "pending_trigger"}
ACCEPTED_STATUSES = {"open", "partially_filled", "filled"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Hybrid adaptive market-making and value-dislocation agent.")
    parser.add_argument("user", nargs="?", default="AdaptiveEdgeMaker")
    parser.add_argument("--url", default="http://127.0.0.1:8000/api")
    parser.add_argument("--starting-cash", type=float, default=10000.0)
    parser.add_argument("--max-pos", type=float, default=700.0)
    parser.add_argument("--order-size", type=float, default=34.0)
    parser.add_argument("--min-spread", type=float, default=0.035)
    parser.add_argument("--interval", type=float, default=0.25)
    parser.add_argument("--tick", type=float, default=0.01)
    parser.add_argument("--drawdown-limit", type=float, default=0.22)
    parser.add_argument("--sweep-cooldown", type=float, default=0.9)
    parser.add_argument("--max-orders", type=int, default=1000)
    parser.add_argument("--profit-lock", type=float, default=0.15)
    return parser.parse_args()


class FillTracker:
    def __init__(self, window: int = 18) -> None:
        self._fills: deque[str] = deque(maxlen=window)

    def record_inventory_change(self, previous_inventory: float, inventory: float) -> None:
        if abs(inventory - previous_inventory) <= 1e-9:
            return
        self._fills.append("buy" if inventory > previous_inventory else "sell")

    def adverse_score(self) -> float:
        count = len(self._fills)
        if count < 5:
            return 0.0
        buys = self._fills.count("buy")
        return max(buys, count - buys) / count

    def dominant_side(self) -> str | None:
        count = len(self._fills)
        if count < 5:
            return None
        buys = self._fills.count("buy")
        sells = count - buys
        if buys == sells:
            return None
        return "buy" if buys > sells else "sell"


class AdaptiveController:
    def __init__(self, window: int = 24) -> None:
        self._equity: deque[float] = deque(maxlen=window)
        self._last_fills = 0
        self._last_orders = 0
        self._quiet_until = 0.0
        self.mode = "normal"

    def update(
        self,
        account: dict[str, Any],
        state: dict[str, Any],
        signals: dict[str, float | str],
        flow: float,
        inventory: float,
        max_pos: float,
        initial_cash: float,
        adverse: float,
        churn_pressure: float,
        loss_pressure: float,
    ) -> dict[str, float | bool | str]:
        equity = float(account.get("equity", initial_cash) or initial_cash)
        fills = int(account.get("fills", 0) or 0)
        orders = int(account.get("orders", 0) or 0)
        fill_delta = max(0, fills - self._last_fills)
        order_delta = max(0, orders - self._last_orders)
        self._last_fills = fills
        self._last_orders = orders
        self._equity.append(equity)

        recent_pnl = equity - self._equity[0] if len(self._equity) > 1 else 0.0
        recent_peak = max(self._equity) if self._equity else equity
        recent_drawdown = max(0.0, recent_peak - equity) / max(initial_cash, 1.0)
        exposure = abs(inventory) / max(max_pos, 1.0)
        opportunity = opportunity_score(state, signals, flow)
        order_waste = clamp((order_delta - fill_delta) / 18.0, 0.0, 1.0)
        fill_burst = clamp(fill_delta / 4.0, 0.0, 1.0)
        now = time.time()

        if fill_delta >= 3 and recent_pnl < 0.0:
            self._quiet_until = max(self._quiet_until, now + 8.0 + 8.0 * max(churn_pressure, loss_pressure))

        if now < self._quiet_until and exposure < 0.22:
            mode = "observe"
        elif loss_pressure > 0.58 or recent_drawdown > 0.0014 or adverse > 0.90:
            mode = "defense"
        elif loss_pressure > 0.24 or churn_pressure > 0.42 or (fill_burst > 0.70 and recent_pnl < 0.0):
            mode = "repair"
        elif opportunity < 0.00055 and exposure < 0.08:
            mode = "observe"
        elif recent_pnl > initial_cash * 0.00045 and opportunity > 0.0011 and adverse < 0.74 and churn_pressure < 0.22:
            mode = "press"
        else:
            mode = "normal"
        self.mode = mode

        profile: dict[str, dict[str, float | bool]] = {
            "observe": {
                "max_pos_mult": 0.35,
                "target_mult": 0.20,
                "size_mult": 0.25,
                "width_mult": 1.45,
                "gate_mult": 1.90,
                "gap_mult": 1.75,
                "allow_sweep": False,
            },
            "defense": {
                "max_pos_mult": 0.42,
                "target_mult": 0.10,
                "size_mult": 0.28,
                "width_mult": 1.70,
                "gate_mult": 2.15,
                "gap_mult": 2.10,
                "allow_sweep": False,
            },
            "repair": {
                "max_pos_mult": 0.58,
                "target_mult": 0.45,
                "size_mult": 0.42,
                "width_mult": 1.32,
                "gate_mult": 1.55,
                "gap_mult": 1.45,
                "allow_sweep": False,
            },
            "press": {
                "max_pos_mult": 1.04,
                "target_mult": 1.00,
                "size_mult": 1.08,
                "width_mult": 0.96,
                "gate_mult": 0.88,
                "gap_mult": 0.86,
                "allow_sweep": True,
            },
            "normal": {
                "max_pos_mult": 1.00,
                "target_mult": 1.00,
                "size_mult": 1.00,
                "width_mult": 1.00,
                "gate_mult": 1.00,
                "gap_mult": 1.00,
                "allow_sweep": True,
            },
        }
        result = dict(profile[mode])
        if order_waste > 0.35:
            result["gate_mult"] = float(result["gate_mult"]) * (1.0 + order_waste * 0.35)
            result["gap_mult"] = float(result["gap_mult"]) * (1.0 + order_waste * 0.45)
        result["mode"] = mode
        return result


def notional_position_limit(
    args: argparse.Namespace,
    account: dict[str, Any],
    mid: float,
    signals: dict[str, float | str],
) -> float:
    equity = max(float(account.get("equity", account.get("initial_cash", args.starting_cash))), 1.0)
    regime = str(signals["regime"])
    exposure_fraction = {
        "calm": 0.90,
        "mixed": 0.78,
        "trend": 0.68,
        "volatile": 0.52,
        "shock": 0.38,
    }.get(regime, 0.72)
    return max(1.0, min(args.max_pos, equity * exposure_fraction / max(mid, 0.01)))


def adaptive_order_size(
    args: argparse.Namespace,
    max_pos: float,
    signals: dict[str, float | str],
    adverse: float,
    churn_pressure: float,
) -> float:
    regime = str(signals["regime"])
    capacity_fraction = {
        "calm": 0.42,
        "mixed": 0.36,
        "trend": 0.32,
        "volatile": 0.26,
        "shock": 0.20,
    }.get(regime, 0.34)
    size = min(args.order_size, max_pos * capacity_fraction)
    if adverse > 0.75:
        size *= clamp(1.0 - (adverse - 0.75) * 1.8, 0.45, 1.0)
    if churn_pressure > 0.0:
        size *= clamp(1.0 - churn_pressure * 0.65, 0.30, 1.0)
    return max(1.0, size)


def same_side_fill_lock(
    fill_tracker: FillTracker,
    adverse: float,
    inventory: float,
    max_pos: float,
) -> str | None:
    if adverse < 0.80:
        return None
    dominant_side = fill_tracker.dominant_side()
    relief_inventory = max(max_pos * 0.04, 1.0)
    if dominant_side == "buy" and inventory > -relief_inventory:
        return "buy"
    if dominant_side == "sell" and inventory < relief_inventory:
        return "sell"
    return None


def quote_move_threshold(args: argparse.Namespace, width: float, signals: dict[str, float | str], adverse: float) -> float:
    regime = str(signals["regime"])
    width_fraction = {
        "calm": 0.16,
        "mixed": 0.22,
        "trend": 0.28,
        "volatile": 0.34,
        "shock": 0.42,
    }.get(regime, 0.22)
    if adverse > 0.75:
        width_fraction *= 0.75
    return max(args.tick * 2.0, width * width_fraction)


def min_quote_gap(
    args: argparse.Namespace,
    signals: dict[str, float | str],
    inventory: float,
    max_pos: float,
    adverse: float,
    orders: int,
    churn_pressure: float,
    gap_multiplier: float = 1.0,
) -> float:
    regime = str(signals["regime"])
    gap = {
        "calm": 3.0,
        "mixed": 3.8,
        "trend": 4.6,
        "volatile": 6.2,
        "shock": 8.0,
    }.get(regime, 3.8)
    exposure = min(abs(inventory) / max(max_pos, 1.0), 1.5)
    order_ratio = orders / max(float(args.max_orders), 1.0)
    gap *= 1.0 + exposure * 0.30
    if adverse > 0.75:
        gap *= 1.25 + (adverse - 0.75) * 2.0
    if churn_pressure > 0.0:
        gap *= 1.0 + churn_pressure * 1.40
    if order_ratio > 0.20:
        gap *= 1.15
    if order_ratio > 0.45:
        gap *= 1.30
    if order_ratio > 0.70:
        gap *= 1.55
    if order_ratio > 0.85:
        gap *= 1.85
    gap *= gap_multiplier
    return clamp(gap, args.interval, 18.0)


def account_churn_pressure(account: dict[str, Any]) -> float:
    fills = int(account.get("fills", 0) or 0)
    volume = max(float(account.get("volume", 0.0) or 0.0), 0.0)
    if fills < 3 or volume <= 0.0:
        return 0.0

    inventory = abs(float(account.get("inventory", 0.0) or 0.0))
    churn_ratio = clamp((volume - inventory) / volume, 0.0, 1.0)
    if churn_ratio < 0.28:
        return 0.0

    fees = max(float(account.get("fees_paid", 0.0) or 0.0), 0.0)
    realized = float(account.get("realized_pnl", 0.0) or 0.0)
    if fees <= 0.0 and realized >= 0.0:
        return 0.0

    net_drag = fees + max(-realized, 0.0) - max(realized, 0.0) * 0.35
    drag_score = clamp(net_drag / max(fees + abs(realized), 1.0), 0.0, 1.0)
    fill_score = clamp((fills - 2.0) / 7.0, 0.0, 1.0)
    churn_score = clamp((churn_ratio - 0.28) / 0.52, 0.0, 1.0)
    return clamp(churn_score * fill_score * drag_score, 0.0, 1.0)


def account_loss_pressure(account: dict[str, Any], initial_cash: float) -> float:
    equity = float(account.get("equity", initial_cash) or initial_cash)
    profit_loss = equity - initial_cash
    if profit_loss >= 0.0:
        return 0.0

    fills = int(account.get("fills", 0) or 0)
    fees = max(float(account.get("fees_paid", 0.0) or 0.0), 0.0)
    realized = float(account.get("realized_pnl", 0.0) or 0.0)
    loss_ratio = abs(profit_loss) / max(initial_cash, 1.0)
    loss_score = clamp(loss_ratio / 0.0014, 0.0, 1.0)
    fee_drag = fees + max(-realized, 0.0) - max(realized, 0.0) * 0.25
    drag_score = clamp(fee_drag / max(initial_cash * 0.0011, 1.0), 0.0, 1.0)
    fill_score = clamp((fills - 2.0) / 10.0, 0.0, 1.0)
    return max(loss_score, drag_score * fill_score)


def opportunity_score(
    state: dict[str, Any],
    signals: dict[str, float | str],
    flow: float,
) -> float:
    mid = max(float(state["mid_price"]), 0.01)
    displayed_spread = max(float(state.get("spread", 0.0)), 0.0) / mid
    dislocation = abs(float(signals["dislocation"]))
    trend = abs(float(signals["trend"]))
    short_momentum = abs(float(signals["short_momentum"]))
    stress = abs(float(signals["stress"]))
    volatility = abs(float(signals["volatility"]))
    return max(
        dislocation,
        trend * 1.35,
        short_momentum * 1.10,
        stress * 0.70,
        volatility * 0.85,
        abs(flow) * displayed_spread * 1.8,
    )


def opportunity_guarded_sizes(
    buy_size: float,
    sell_size: float,
    state: dict[str, Any],
    signals: dict[str, float | str],
    flow: float,
    inventory: float,
    target_inventory: float,
    max_pos: float,
    loss_pressure: float,
    churn_pressure: float,
    gate_multiplier: float = 1.0,
) -> tuple[float, float]:
    regime = str(signals["regime"])
    threshold = {
        "calm": 0.00045,
        "mixed": 0.00075,
        "trend": 0.00105,
        "volatile": 0.00155,
        "shock": 0.00210,
    }.get(regime, 0.00075)
    pressure = max(loss_pressure, churn_pressure)
    if pressure > 0.25:
        threshold *= 1.0 + pressure * 0.85
    threshold *= gate_multiplier
    if opportunity_score(state, signals, flow) >= threshold:
        return buy_size, sell_size

    risk_base = max(max_pos, 1.0)
    target_gap = inventory - target_inventory
    exposure_ratio = abs(inventory) / risk_base
    rebalance_band = risk_base * (0.04 + pressure * 0.05)
    if exposure_ratio < 0.06 and abs(target_gap) <= rebalance_band:
        return 0.0, 0.0
    if target_gap > rebalance_band:
        return 0.0, sell_size * 0.55
    if target_gap < -rebalance_band:
        return buy_size * 0.55, 0.0
    scale = clamp(1.0 - pressure * 1.25, 0.0, 0.55)
    return buy_size * scale, sell_size * scale


def loss_guarded_sizes(
    buy_size: float,
    sell_size: float,
    inventory: float,
    target_inventory: float,
    max_pos: float,
    loss_pressure: float,
) -> tuple[float, float]:
    if loss_pressure < 0.30:
        return buy_size, sell_size

    risk_base = max(max_pos, 1.0)
    target_gap = inventory - target_inventory
    rebalance_band = risk_base * (0.04 + loss_pressure * 0.08)
    relief_scale = clamp(1.0 - loss_pressure * 0.45, 0.35, 0.85)
    if target_gap > rebalance_band:
        return 0.0, sell_size * relief_scale
    if target_gap < -rebalance_band:
        return buy_size * relief_scale, 0.0

    conserve = clamp(1.0 - loss_pressure * 1.55, 0.0, 0.45)
    return buy_size * conserve, sell_size * conserve


def passive_edge_threshold(
    args: argparse.Namespace,
    state: dict[str, Any],
    signals: dict[str, float | str],
    adverse: float,
    order_count: int,
    churn_pressure: float,
) -> float:
    fees = state.get("fees", {})
    maker_fee = abs(float(fees.get("maker_fee_rate", 0.0002)))
    regime = str(signals["regime"])
    threshold = maker_fee + {
        "calm": 0.00003,
        "mixed": 0.00008,
        "trend": 0.00014,
        "volatile": 0.00026,
        "shock": 0.00045,
    }.get(regime, 0.00008)
    threshold += min(float(signals["stress"]), 0.02) * 0.018
    if adverse > 0.75:
        threshold *= 1.0 + (adverse - 0.75) * 2.0
    if churn_pressure > 0.0:
        threshold *= 1.0 + churn_pressure * 1.35
    order_ratio = order_count / max(float(args.max_orders), 1.0)
    if order_ratio > 0.70:
        threshold *= 1.0 + (order_ratio - 0.70) * 1.8
    return clamp(threshold, 0.00008, 0.0024)


def quality_adjusted_sizes(
    buy_size: float,
    sell_size: float,
    bid: float,
    ask: float,
    fair: float,
    mid: float,
    args: argparse.Namespace,
    state: dict[str, Any],
    signals: dict[str, float | str],
    inventory: float,
    target_inventory: float,
    max_pos: float,
    adverse: float,
    order_count: int,
    churn_pressure: float,
) -> tuple[float, float]:
    threshold = passive_edge_threshold(args, state, signals, adverse, order_count, churn_pressure)
    bid_edge = (fair - bid) / max(mid, 0.01)
    ask_edge = (ask - fair) / max(mid, 0.01)
    rebalance_band = max_pos * (0.08 if str(signals["regime"]) in {"volatile", "shock"} else 0.05)
    buy_rebalances = inventory < target_inventory - rebalance_band
    sell_rebalances = inventory > target_inventory + rebalance_band

    def adjusted(size: float, edge: float, rebalances: bool) -> float:
        if size < 1.0:
            return 0.0
        if edge >= threshold:
            return size
        if not rebalances:
            return 0.0
        edge_score = clamp((edge + threshold) / max(2.0 * threshold, 1e-9), 0.0, 1.0)
        return size * (0.30 + 0.45 * edge_score)

    buy_size = adjusted(buy_size, bid_edge, buy_rebalances)
    sell_size = adjusted(sell_size, ask_edge, sell_rebalances)

    order_ratio = order_count / max(float(args.max_orders), 1.0)
    if order_ratio > 0.82:
        conserve = clamp(1.0 - (order_ratio - 0.82) * 2.0, 0.35, 1.0)
        if not buy_rebalances:
            buy_size *= conserve
        if not sell_rebalances:
            sell_size *= conserve
    if churn_pressure > 0.0:
        conserve = clamp(1.0 - churn_pressure * 0.55, 0.25, 1.0)
        if not buy_rebalances:
            buy_size *= conserve
        if not sell_rebalances:
            sell_size *= conserve
    return buy_size, sell_size


def churn_guarded_sizes(
    buy_size: float,
    sell_size: float,
    inventory: float,
    target_inventory: float,
    max_pos: float,
    churn_pressure: float,
) -> tuple[float, float]:
    if churn_pressure <= 0.0:
        return buy_size, sell_size

    risk_base = max(max_pos, 1.0)
    target_gap = inventory - target_inventory
    rebalance_band = risk_base * (0.07 + churn_pressure * 0.05)
    if abs(target_gap) <= rebalance_band:
        scale = clamp(1.0 - churn_pressure * 1.10, 0.0, 1.0)
        return buy_size * scale, sell_size * scale

    relief_scale = clamp(1.0 - churn_pressure * 0.30, 0.45, 1.0)
    if target_gap > 0.0:
        buy_size = 0.0
        sell_size *= relief_scale
    else:
        sell_size = 0.0
        buy_size *= relief_scale
    return buy_size, sell_size


def tape_flow_score(signals: dict[str, float | str], imbalance: float) -> float:
    trend = float(signals["trend"])
    short_momentum = float(signals["short_momentum"])
    return clamp(
        0.55 * clamp(trend / 0.004, -1.0, 1.0)
        + 0.30 * clamp(short_momentum / 0.003, -1.0, 1.0)
        + 0.15 * clamp(imbalance, -1.0, 1.0),
        -1.0,
        1.0,
    )


def flow_adjusted_fundamental_weight(
    fundamental_weight: float,
    signals: dict[str, float | str],
    flow: float,
) -> float:
    dislocation = float(signals["dislocation"])
    if abs(dislocation) < 0.0012 or abs(flow) < 0.35 or dislocation * flow >= 0:
        return fundamental_weight
    conflict = clamp((abs(flow) - 0.35) / 0.65, 0.0, 1.0)
    return fundamental_weight * (1.0 - 0.55 * conflict)


def flow_guarded_sizes(
    buy_size: float,
    sell_size: float,
    signals: dict[str, float | str],
    flow: float,
    inventory: float,
    max_pos: float,
) -> tuple[float, float]:
    regime = str(signals["regime"])
    threshold = 0.46 if regime in {"trend", "volatile", "shock"} else 0.62
    if abs(flow) < threshold:
        return buy_size, sell_size

    relief_inventory = max(max_pos * 0.04, 1.0)
    if flow > 0.0:
        sell_size = sell_size * 0.55 if inventory > relief_inventory else 0.0
    else:
        buy_size = buy_size * 0.55 if inventory < -relief_inventory else 0.0
    return buy_size, sell_size


def call_api(api_url: str, path: str, method: str = "GET", data: dict[str, Any] | None = None, timeout: float = 2.0) -> dict[str, Any] | None:
    body = None
    request = urllib.request.Request(f"{api_url.rstrip('/')}{path}", method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
        body = json.dumps(data).encode("utf-8")
    try:
        with urllib.request.urlopen(request, data=body, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        logging.debug("API %s %s failed: %s", method, path, exc)
        return None


def user_path(path: str, user: str) -> str:
    separator = "&" if "?" in path else "?"
    return f"{path}{separator}user={urllib.parse.quote(user)}"


def clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(value, upper))


def top_volume(levels: list[dict[str, Any]], depth: int = 5) -> float:
    return sum(float(level.get("quantity", 0)) for level in levels[:depth])


def actionable_market_data(state: dict[str, Any], args: argparse.Namespace) -> tuple[bool, str]:
    try:
        mid = float(state["mid_price"])
        best_bid = float(state["best_bid"])
        best_ask = float(state["best_ask"])
    except (KeyError, TypeError, ValueError):
        return False, "missing_top_of_book"

    if not all(math.isfinite(value) for value in (mid, best_bid, best_ask)):
        return False, "non_finite_price"
    if mid <= 0.0 or best_bid <= 0.0 or best_ask <= 0.0:
        return False, "non_positive_price"
    if best_bid >= best_ask:
        return False, "crossed_book"

    spread = best_ask - best_bid
    if spread < max(args.tick * 0.5, 0.0):
        return False, "locked_or_subtick_book"
    if spread / max(mid, 0.01) > 0.035:
        return False, "extreme_spread"

    top_mid = (best_bid + best_ask) / 2.0
    if abs(top_mid - mid) / max(mid, 0.01) > 0.01:
        return False, "stale_or_inconsistent_mid"

    quote_age = state.get("quote_age_seconds")
    if quote_age is not None:
        try:
            age = float(quote_age)
        except (TypeError, ValueError):
            return False, "invalid_quote_age"
        if age > max(2.5, args.interval * 10.0):
            return False, "stale_quote"

    return True, ""


def current_microprice(state: dict[str, Any]) -> float:
    best_bid = float(state["best_bid"])
    best_ask = float(state["best_ask"])
    bids = state.get("order_book", {}).get("bids", [])
    asks = state.get("order_book", {}).get("asks", [])
    bid_vol = top_volume(bids)
    ask_vol = top_volume(asks)
    total = bid_vol + ask_vol
    if total <= 0:
        return float(state["mid_price"])
    return (best_ask * bid_vol + best_bid * ask_vol) / total


def cancel_open_orders(api_url: str, user: str, side: str | None = None) -> None:
    orders = call_api(api_url, user_path("/orders?limit=1000", user))
    for order in (orders or {}).get("orders", []):
        if order.get("status") in OPEN_STATUSES and (side is None or order.get("side") == side):
            call_api(api_url, user_path(f"/orders/{order['order_id']}", user), method="DELETE")


def submit_quote(api_url: str, user: str, side: str, quantity: float, price: float) -> bool:
    result = call_api(
        api_url,
        "/order",
        "POST",
        {
            "side": side,
            "quantity": round(quantity, 4),
            "order_type": "limit",
            "price": round(price, 4),
            "user": user,
            "post_only": True,
        },
    )
    if not result:
        return False
    if result.get("status") not in ACCEPTED_STATUSES:
        logging.info("%s rejected at %.4f: %s", side, price, result.get("reject_reason", result.get("status")))
        return False
    return True


def submit_ioc_limit(api_url: str, user: str, side: str, quantity: float, price: float) -> bool:
    result = call_api(
        api_url,
        "/order",
        "POST",
        {
            "side": side,
            "quantity": round(quantity, 4),
            "order_type": "limit",
            "time_in_force": "ioc",
            "price": round(price, 4),
            "user": user,
        },
    )
    if not result:
        return False
    return result.get("status") in ACCEPTED_STATUSES or result.get("filled_quantity", 0) > 0


def pct_change(newer: float, older: float) -> float:
    if older <= 0:
        return 0.0
    return newer / older - 1.0


def history_value(history: list[dict[str, Any]], index: int, *keys: str) -> float | None:
    if not history:
        return None
    point = history[index]
    for key in keys:
        value = point.get(key)
        if value is not None:
            return float(value)
    return None


def market_signals(state: dict[str, Any]) -> dict[str, float | str]:
    history = state.get("history", [])
    mid = float(state["mid_price"])
    fundamental = float(state.get("fundamental_price", mid))
    volatility = max(float(state.get("volatility", 0.0)), 0.0)
    dislocation = pct_change(fundamental, mid)

    short_momentum = 0.0
    medium_momentum = 0.0
    fundamental_momentum = 0.0
    shock_return = 0.0
    if len(history) >= 2:
        last = history_value(history, -1, "close", "mark", "mid", "last") or mid
        previous = history_value(history, -2, "close", "mark", "mid", "last") or last
        shock_return = pct_change(last, previous)
        short_anchor = history_value(history, max(-4, -len(history)), "close", "mark", "mid", "last") or previous
        medium_anchor = history_value(history, max(-12, -len(history)), "close", "mark", "mid", "last") or short_anchor
        short_momentum = pct_change(last, short_anchor)
        medium_momentum = pct_change(last, medium_anchor)
        last_fund = history_value(history, -1, "fundamental") or fundamental
        old_fund = history_value(history, max(-8, -len(history)), "fundamental") or last_fund
        fundamental_momentum = pct_change(last_fund, old_fund)

    stress = max(abs(dislocation), abs(shock_return) * 1.4, volatility * 1.3)
    trend = 0.55 * medium_momentum + 0.30 * short_momentum + 0.15 * fundamental_momentum
    if stress > 0.012:
        regime = "shock"
    elif stress > 0.006:
        regime = "volatile"
    elif abs(trend) > 0.002:
        regime = "trend"
    elif volatility < 0.0035 and abs(dislocation) < 0.0025:
        regime = "calm"
    else:
        regime = "mixed"

    return {
        "regime": regime,
        "dislocation": dislocation,
        "trend": trend,
        "short_momentum": short_momentum,
        "volatility": volatility,
        "stress": stress,
    }


def quote_width(state: dict[str, Any], args: argparse.Namespace, signals: dict[str, float | str]) -> float:
    mid = float(state["mid_price"])
    displayed_spread = max(float(state.get("spread", 0.0)), args.tick)
    volatility = float(signals["volatility"])
    stress = float(signals["stress"])
    volatility_width = mid * (0.00020 + min(volatility, 0.02) * 1.35 + min(stress, 0.03) * 0.10)
    regime = str(signals["regime"])
    if regime == "calm":
        return max(args.min_spread, displayed_spread * 0.82, volatility_width * 0.55)
    if regime == "trend":
        return max(args.min_spread * 1.25, displayed_spread * 1.05, volatility_width)
    if regime == "volatile":
        return max(args.min_spread * 1.9, displayed_spread * 1.25, volatility_width * 1.15)
    if regime == "shock":
        return max(args.min_spread * 2.8, displayed_spread * 1.55, volatility_width * 1.45)
    return max(args.min_spread * 1.15, displayed_spread * 1.05, volatility_width * 0.9)


def quote_sizes(account: dict[str, Any], args: argparse.Namespace, bid_price: float, target_inventory: float) -> tuple[float, float]:
    inventory = float(account.get("inventory", 0.0))
    cash = float(account.get("cash", 0.0))
    order_cap = max(float(args.order_size), 0.0)
    buy_capacity = max(0.0, args.max_pos - inventory)
    sell_capacity = max(0.0, args.max_pos + inventory)
    cash_capacity = max(0.0, cash * 0.95 / max(bid_price, 0.01))
    lean = clamp((target_inventory - inventory) / max(args.max_pos, 1.0), -1.0, 1.0)
    buy_size = order_cap * (1.0 + max(lean, 0.0) * 1.4 - max(-lean, 0.0) * 0.55)
    sell_size = order_cap * (1.0 + max(-lean, 0.0) * 1.4 - max(lean, 0.0) * 0.55)
    return (
        min(max(0.0, buy_size), order_cap, buy_capacity, cash_capacity),
        min(max(0.0, sell_size), order_cap, sell_capacity),
    )


def inventory_guarded_sizes(
    buy_size: float,
    sell_size: float,
    inventory: float,
    target_inventory: float,
    max_pos: float,
    adverse: float,
) -> tuple[float, float]:
    risk_base = max(max_pos, 1.0)
    target_gap = inventory - target_inventory
    gap_ratio = abs(target_gap) / risk_base
    exposure_ratio = abs(inventory) / risk_base

    if gap_ratio > 0.18:
        if target_gap > 0:
            buy_size *= 0.35
        else:
            sell_size *= 0.35

    if gap_ratio > 0.35:
        if target_gap > 0:
            buy_size = 0.0
        elif target_gap < 0:
            sell_size = 0.0

    if exposure_ratio > 0.75:
        if inventory > 0:
            buy_size = 0.0
        elif inventory < 0:
            sell_size = 0.0

    if adverse > 0.85 and exposure_ratio > 0.35:
        if inventory > 0:
            buy_size = 0.0
        elif inventory < 0:
            sell_size = 0.0

    if exposure_ratio > 0.95:
        if inventory > 0:
            buy_size = 0.0
        elif inventory < 0:
            sell_size = 0.0

    return buy_size, sell_size


def target_inventory(args: argparse.Namespace, signals: dict[str, float | str]) -> float:
    dislocation = float(signals["dislocation"])
    trend = float(signals["trend"])
    short_momentum = float(signals["short_momentum"])
    regime = str(signals["regime"])
    raw = dislocation / 0.010 + trend / 0.004 + short_momentum / 0.010
    if regime == "shock":
        raw = dislocation / 0.012 + trend / 0.010
    elif regime == "volatile":
        raw = dislocation / 0.014 + trend / 0.010
    elif regime == "calm":
        raw *= 0.35
    target_ratio = clamp(raw, -0.82, 0.82)
    return target_ratio * args.max_pos


def maybe_sweep_edge(
    api_url: str,
    user: str,
    state: dict[str, Any],
    account: dict[str, Any],
    fair: float,
    args: argparse.Namespace,
    signals: dict[str, float | str],
    max_pos: float,
    blocked_side: str | None = None,
) -> bool:
    mid = float(state["mid_price"])
    best_bid = float(state["best_bid"])
    best_ask = float(state["best_ask"])
    fees = state.get("fees", {})
    taker_fee = float(fees.get("taker_fee_rate", 0.0002))
    regime = str(signals["regime"])
    threshold_mult = {
        "calm": 2.4,
        "mixed": 3.4,
        "trend": 3.2,
        "volatile": 5.8,
        "shock": 8.0,
    }.get(regime, 3.4)
    threshold = max(0.0009, taker_fee * threshold_mult)
    inventory = float(account.get("inventory", 0.0))
    cash = float(account.get("cash", 0.0))
    exposure_ratio = inventory / max(max_pos, 1.0)
    buy_edge = (fair - best_ask) / max(mid, 0.01)
    sell_edge = (best_bid - fair) / max(mid, 0.01)
    if regime in {"volatile", "shock"}:
        dislocation = float(signals["dislocation"])
        trend = float(signals["trend"])
        if (buy_edge > 0 and (dislocation <= 0 or trend < -0.0015)) or (sell_edge > 0 and (dislocation >= 0 or trend > 0.0015)):
            return False
    size = min(args.order_size * (0.65 if regime in {"volatile", "shock"} else 1.05), 48.0)
    if blocked_side != "buy" and buy_edge > threshold and inventory < max_pos and exposure_ratio <= 0.45:
        quantity = min(size, max_pos - inventory, cash * 0.90 / max(best_ask, 0.01))
        return quantity >= 1.0 and submit_ioc_limit(api_url, user, "buy", quantity, best_ask)
    if blocked_side != "sell" and sell_edge > threshold and inventory > -max_pos and exposure_ratio >= -0.45:
        quantity = min(size, max_pos + inventory)
        return quantity >= 1.0 and submit_ioc_limit(api_url, user, "sell", quantity, best_bid)
    return False


def run(args: argparse.Namespace) -> int:
    logging.info("Starting %s", args.user)
    call_api(args.url, "/accounts", "POST", {"user": args.user, "starting_cash": args.starting_cash})
    last_bid = 0.0
    last_ask = 0.0
    last_regime = ""
    last_quote_at = 0.0
    last_sweep_at = 0.0
    last_inventory = 0.0
    consecutive_fills = 0.0
    cooldown_until = 0.0
    buy_pause_until = 0.0
    sell_pause_until = 0.0
    fill_tracker = FillTracker()
    controller = AdaptiveController()

    try:
        while True:
            state = call_api(args.url, user_path("/state", args.user))
            account = call_api(args.url, user_path("/account", args.user))
            if not state or not account:
                cancel_open_orders(args.url, args.user)
                last_bid = 0.0
                last_ask = 0.0
                last_quote_at = 0.0
                time.sleep(0.5)
                continue
            order_count = int(account.get("orders", 0))
            if order_count >= args.max_orders:
                cancel_open_orders(args.url, args.user)
                return 0

            initial_cash = max(float(account.get("initial_cash", args.starting_cash)), 1.0)
            if float(account.get("equity", initial_cash)) < initial_cash * (1 - args.drawdown_limit):
                cancel_open_orders(args.url, args.user)
                return 1

            actionable, reason = actionable_market_data(state, args)
            if not actionable:
                logging.debug("Holding quotes on %s", reason)
                cancel_open_orders(args.url, args.user)
                last_bid = 0.0
                last_ask = 0.0
                last_quote_at = 0.0
                time.sleep(args.interval)
                continue

            bids = state.get("order_book", {}).get("bids", [])
            asks = state.get("order_book", {}).get("asks", [])
            bid_vol = top_volume(bids)
            ask_vol = top_volume(asks)
            imbalance = (bid_vol - ask_vol) / (bid_vol + ask_vol + 1e-9)

            mid = float(state["mid_price"])
            fundamental = float(state.get("fundamental_price", mid))
            micro = current_microprice(state)
            signals = market_signals(state)
            width = quote_width(state, args, signals)
            inventory = float(account.get("inventory", 0.0))
            loss_pressure = account_loss_pressure(account, initial_cash)
            churn_pressure = max(account_churn_pressure(account), loss_pressure * 0.65)
            inventory_changed = abs(inventory - last_inventory) > 1e-9
            fill_tracker.record_inventory_change(last_inventory, inventory)
            if inventory_changed:
                filled_side = "buy" if inventory > last_inventory else "sell"
                fill_now = time.time()
                fill_pause = 2.8 + churn_pressure * 4.5
                if churn_pressure > 0.45:
                    cancel_open_orders(args.url, args.user)
                    last_bid = 0.0
                    last_ask = 0.0
                    buy_pause_until = fill_now + fill_pause
                    sell_pause_until = fill_now + fill_pause
                else:
                    cancel_open_orders(args.url, args.user, side=filled_side)
                    if filled_side == "buy":
                        last_bid = 0.0
                        buy_pause_until = fill_now + fill_pause
                    else:
                        last_ask = 0.0
                        sell_pause_until = fill_now + fill_pause
                if filled_side == "buy":
                    last_bid = 0.0
                else:
                    last_ask = 0.0
                consecutive_fills += 1
                if consecutive_fills >= 4:
                    cooldown_until = time.time() + 4.0
                    consecutive_fills = 0.0
            else:
                consecutive_fills = max(0.0, consecutive_fills - 0.4)
            last_inventory = inventory
            if time.time() < cooldown_until:
                cancel_open_orders(args.url, args.user)
                time.sleep(1.0)
                continue

            adverse = fill_tracker.adverse_score()
            flow = tape_flow_score(signals, imbalance)
            max_pos = notional_position_limit(args, account, mid, signals)
            if float(account.get("equity", initial_cash)) > initial_cash * (1.0 + args.profit_lock):
                max_pos *= 0.55
            if str(signals["regime"]) in {"volatile", "shock"}:
                max_pos *= 0.62
            if loss_pressure > 0.0:
                max_pos *= clamp(1.0 - loss_pressure * 0.42, 0.48, 1.0)
            control = controller.update(
                account,
                state,
                signals,
                flow,
                inventory,
                max_pos,
                initial_cash,
                adverse,
                churn_pressure,
                loss_pressure,
            )
            max_pos *= float(control["max_pos_mult"])
            max_pos = max(max_pos, 1.0)

            target_inv = clamp(target_inventory(args, signals), -max_pos, max_pos)
            target_inv *= float(control["target_mult"])
            if loss_pressure > 0.0:
                target_inv *= clamp(1.0 - loss_pressure * 0.55, 0.35, 1.0)
            inv_gap = clamp((inventory - target_inv) / max(max_pos, 1.0), -1.8, 1.8)
            inventory_skew = inv_gap * width * 1.9
            imbalance_adjust = clamp(imbalance, -1.0, 1.0) * width * 0.16
            trend_adjust = mid * clamp(float(signals["trend"]) * 0.33, -0.004, 0.004)
            regime = str(signals["regime"])
            flow = tape_flow_score(signals, imbalance)
            if regime == "shock":
                fundamental_weight = 0.62
                micro_weight = 0.10
            elif regime == "volatile":
                fundamental_weight = 0.46
                micro_weight = 0.18
            elif regime == "trend":
                fundamental_weight = 0.38
                micro_weight = 0.22
            elif regime == "calm":
                fundamental_weight = 0.08
                micro_weight = 0.42
            else:
                fundamental_weight = 0.24
                micro_weight = 0.30
            fundamental_weight = flow_adjusted_fundamental_weight(fundamental_weight, signals, flow)
            mid_weight = max(0.0, 1.0 - fundamental_weight - micro_weight)
            fair = fundamental * fundamental_weight + micro * micro_weight + mid * mid_weight + imbalance_adjust + trend_adjust
            blocked_side = same_side_fill_lock(fill_tracker, adverse, inventory, max_pos)
            if adverse > 0.75:
                width *= 1.0 + (adverse - 0.75) * 4.0
            if churn_pressure > 0.0:
                width *= 1.0 + churn_pressure * 0.55
            width *= float(control["width_mult"])

            needs_rebalance = abs(inventory - target_inv) > max(max_pos * 0.18, 1.0)
            trading_size = adaptive_order_size(args, max_pos, signals, adverse, churn_pressure)
            trading_size *= float(control["size_mult"])
            size_args = argparse.Namespace(**{**vars(args), "max_pos": max_pos, "order_size": trading_size})
            order_ratio = order_count / max(float(args.max_orders), 1.0)
            sweep_budget_limit = 0.92 if needs_rebalance else 0.78
            can_sweep = (
                (bool(control["allow_sweep"]) or needs_rebalance)
                and (churn_pressure < 0.45 or needs_rebalance)
                and loss_pressure < 0.72
                and order_ratio < sweep_budget_limit
            )
            if can_sweep and time.time() - last_sweep_at >= args.sweep_cooldown:
                if maybe_sweep_edge(args.url, args.user, state, account, fair, size_args, signals, max_pos, blocked_side):
                    last_sweep_at = time.time()
                    account = call_api(args.url, user_path("/account", args.user)) or account
                    inventory = float(account.get("inventory", inventory))
                    loss_pressure = account_loss_pressure(account, initial_cash)
                    churn_pressure = max(account_churn_pressure(account), loss_pressure * 0.65)
                    trading_size = adaptive_order_size(args, max_pos, signals, adverse, churn_pressure)
                    trading_size *= float(control["size_mult"])
                    size_args = argparse.Namespace(**{**vars(args), "max_pos": max_pos, "order_size": trading_size})
                    target_inv = clamp(target_inventory(args, signals), -max_pos, max_pos)
                    target_inv *= float(control["target_mult"])
                    if loss_pressure > 0.0:
                        target_inv *= clamp(1.0 - loss_pressure * 0.55, 0.35, 1.0)
                    inv_gap = clamp((inventory - target_inv) / max(max_pos, 1.0), -1.8, 1.8)
                    inventory_skew = inv_gap * width * 1.9

            bid = round(min(fair - width / 2 - inventory_skew, float(state["best_ask"]) - args.tick), 4)
            ask = round(max(fair + width / 2 - inventory_skew, float(state["best_bid"]) + args.tick), 4)
            if bid <= 0 or ask <= 0 or bid >= ask:
                cancel_open_orders(args.url, args.user)
                last_bid = 0.0
                last_ask = 0.0
                last_quote_at = 0.0
                time.sleep(args.interval)
                continue

            buy_size, sell_size = quote_sizes(account, size_args, bid, target_inv)
            buy_size, sell_size = inventory_guarded_sizes(buy_size, sell_size, inventory, target_inv, max_pos, adverse)
            buy_size, sell_size = quality_adjusted_sizes(
                buy_size,
                sell_size,
                bid,
                ask,
                fair,
                mid,
                args,
                state,
                signals,
                inventory,
                target_inv,
                max_pos,
                adverse,
                order_count,
                churn_pressure,
            )
            buy_size, sell_size = flow_guarded_sizes(buy_size, sell_size, signals, flow, inventory, max_pos)
            buy_size, sell_size = opportunity_guarded_sizes(
                buy_size,
                sell_size,
                state,
                signals,
                flow,
                inventory,
                target_inv,
                max_pos,
                loss_pressure,
                churn_pressure,
                gate_multiplier=float(control["gate_mult"]),
            )
            buy_size, sell_size = churn_guarded_sizes(
                buy_size,
                sell_size,
                inventory,
                target_inv,
                max_pos,
                churn_pressure,
            )
            buy_size, sell_size = loss_guarded_sizes(
                buy_size,
                sell_size,
                inventory,
                target_inv,
                max_pos,
                loss_pressure,
            )
            if blocked_side == "buy":
                buy_size = 0.0
            elif blocked_side == "sell":
                sell_size = 0.0

            move_threshold = quote_move_threshold(args, width, signals, adverse)
            best_bid = float(state["best_bid"])
            best_ask = float(state["best_ask"])
            now = time.time()
            pause_band = max(max_pos * 0.04, 1.0)
            if now < buy_pause_until and inventory >= target_inv - pause_band:
                buy_size = 0.0
            if now < sell_pause_until and inventory <= target_inv + pause_band:
                sell_size = 0.0
            quote_ready = now - last_quote_at >= min_quote_gap(
                args,
                signals,
                inventory,
                max_pos,
                adverse,
                order_count,
                churn_pressure,
                gap_multiplier=float(control["gap_mult"]),
            )
            first_quote = last_quote_at <= 0.0
            bid_disabled = buy_size < 1.0 and last_bid > 0.0
            ask_disabled = sell_size < 1.0 and last_ask > 0.0
            bid_unsafe = last_bid > 0.0 and (last_bid >= best_ask or last_bid > bid + move_threshold)
            ask_unsafe = last_ask > 0.0 and (last_ask <= best_bid or last_ask < ask - move_threshold)
            bid_missing = buy_size >= 1.0 and last_bid <= 0.0
            ask_missing = sell_size >= 1.0 and last_ask <= 0.0
            bid_price_moved = buy_size >= 1.0 and last_bid > 0.0 and abs(bid - last_bid) > move_threshold
            ask_price_moved = sell_size >= 1.0 and last_ask > 0.0 and abs(ask - last_ask) > move_threshold
            refresh_for_risk = inventory_changed or adverse > 0.80

            refresh_bid = buy_size >= 1.0 and (
                ((first_quote or quote_ready) and (bid_missing or bid_unsafe))
                or (quote_ready and (bid_price_moved or regime != last_regime or refresh_for_risk))
            )
            refresh_ask = sell_size >= 1.0 and (
                ((first_quote or quote_ready) and (ask_missing or ask_unsafe))
                or (quote_ready and (ask_price_moved or regime != last_regime or refresh_for_risk))
            )

            submitted_quote = False
            if bid_disabled or bid_unsafe or refresh_bid:
                cancel_open_orders(args.url, args.user, side="buy")
                last_bid = 0.0
            if ask_disabled or ask_unsafe or refresh_ask:
                cancel_open_orders(args.url, args.user, side="sell")
                last_ask = 0.0
            if refresh_bid:
                bid_ok = submit_quote(args.url, args.user, "buy", buy_size, bid)
                last_bid = bid if bid_ok else 0.0
                submitted_quote = submitted_quote or bid_ok
            if refresh_ask:
                ask_ok = submit_quote(args.url, args.user, "sell", sell_size, ask)
                last_ask = ask if ask_ok else 0.0
                submitted_quote = submitted_quote or ask_ok
            if submitted_quote:
                last_regime = regime
                last_quote_at = now
            time.sleep(args.interval)
    finally:
        cancel_open_orders(args.url, args.user)


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
