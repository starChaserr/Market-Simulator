from __future__ import annotations

import argparse
import json
import logging
import math
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass
from typing import Any


OPEN_STATUSES = {"open", "partially_filled", "pending_trigger"}
ACCEPTED_STATUSES = {"open", "partially_filled", "filled"}


def clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(value, upper))


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(result):
        return default
    return result


def pct(newer: float, older: float) -> float:
    return newer / older - 1.0 if older > 0 else 0.0


def api_user_path(path: str, user: str) -> str:
    separator = "&" if "?" in path else "?"
    return f"{path}{separator}user={urllib.parse.quote(user)}"


def call_api(
    api_url: str,
    path: str,
    method: str = "GET",
    data: dict[str, Any] | None = None,
    timeout: float = 3.0,
) -> dict[str, Any] | None:
    body = None
    req = urllib.request.Request(f"{api_url.rstrip('/')}{path}", method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
        body = json.dumps(data).encode("utf-8")
    try:
        with urllib.request.urlopen(req, data=body, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        logging.debug("API %s %s failed: %s", method, path, exc)
        return None


def trade_key(trade: dict[str, Any]) -> str:
    for key in ("id", "trade_id", "order_id", "timestamp", "time", "created_at"):
        if trade.get(key) is not None:
            return f"{key}:{trade[key]}"
    side = trade.get("side", "")
    quantity = trade.get("quantity", "")
    price = trade.get("price", trade.get("last_price", trade.get("mark", "")))
    owner = trade.get("owner", trade.get("user", ""))
    return f"{side}:{quantity}:{price}:{owner}"


def snap_price(price: float, tick: float) -> float:
    if tick <= 0:
        return round(price, 8)
    decimals = max(0, min(10, int(math.ceil(-math.log10(tick))) + 2))
    return round(round(price / tick) * tick, decimals)


def top_volume(levels: list[dict[str, Any]], depth: int = 5) -> float:
    return sum(max(safe_float(level.get("quantity")), 0.0) for level in levels[:depth])


def weighted_price(levels: list[dict[str, Any]], depth: int = 5) -> float | None:
    notional = 0.0
    volume = 0.0
    for level in levels[:depth]:
        price = safe_float(level.get("price"))
        qty = max(safe_float(level.get("quantity")), 0.0)
        if price <= 0 or qty <= 0:
            continue
        notional += price * qty
        volume += qty
    return notional / volume if volume > 0 else None


def extract_history_mid(point: dict[str, Any], fallback: float) -> float:
    for key in ("close", "mark", "mid", "last", "last_price"):
        value = safe_float(point.get(key), 0.0)
        if value > 0:
            return value
    return fallback


def robust_realized_vol(prices: list[float]) -> float:
    if len(prices) < 4:
        return 0.0
    returns = [pct(prices[i], prices[i - 1]) for i in range(1, len(prices)) if prices[i - 1] > 0]
    if not returns:
        return 0.0
    mean = sum(returns) / len(returns)
    variance = sum((ret - mean) ** 2 for ret in returns) / max(len(returns) - 1, 1)
    return math.sqrt(max(variance, 0.0))


class TickEstimator:
    STANDARD_TICKS = (
        0.00000001,
        0.0000001,
        0.000001,
        0.00001,
        0.0001,
        0.0005,
        0.001,
        0.005,
        0.01,
        0.02,
        0.05,
        0.1,
        0.25,
        0.5,
        1.0,
        2.0,
        5.0,
        10.0,
    )

    def __init__(self, fallback: float) -> None:
        self.fallback = max(fallback, 0.00000001)
        self.samples: deque[float] = deque(maxlen=40)
        self.tick = self.fallback

    def update(self, state: dict[str, Any], mid: float) -> float:
        explicit = self._explicit_tick(state, mid)
        if explicit is not None:
            self.samples.append(explicit)

        book = state.get("order_book", {})
        gaps: list[float] = []
        for side in ("bids", "asks"):
            prices = [
                safe_float(level.get("price"))
                for level in book.get(side, [])
                if safe_float(level.get("price")) > 0
            ]
            for left, right in zip(prices, prices[1:]):
                gap = abs(left - right)
                if 0 < gap <= max(mid * 0.02, self.fallback * 200):
                    gaps.append(gap)
        if gaps:
            self.samples.append(self._nearest_standard(min(gaps)))

        if self.samples:
            counts: dict[float, int] = {}
            for sample in self.samples:
                counts[sample] = counts.get(sample, 0) + 1
            self.tick = max(0.00000001, max(counts, key=lambda value: (counts[value], -value)))
        return self.tick

    def _explicit_tick(self, state: dict[str, Any], mid: float) -> float | None:
        for key in ("price_tick", "price_increment", "min_tick"):
            raw = safe_float(state.get(key), 0.0)
            if 0 < raw <= max(mid * 0.02, self.fallback * 200):
                return max(raw, 0.00000001)
        return None

    def _nearest_standard(self, raw: float) -> float:
        return min(self.STANDARD_TICKS, key=lambda tick: abs(tick - raw))


class TradeFlow:
    def __init__(self, window: int = 140) -> None:
        self.trades: deque[tuple[str, float, float]] = deque(maxlen=window)
        self.seen_keys: deque[str] = deque(maxlen=800)
        self.seen_key_set: set[str] = set()

    def record_trades(self, trades: list[dict[str, Any]]) -> None:
        for trade in trades:
            key = trade_key(trade)
            if key in self.seen_key_set:
                continue
            if len(self.seen_keys) >= self.seen_keys.maxlen:
                old_key = self.seen_keys.popleft()
                self.seen_key_set.discard(old_key)
            self.seen_keys.append(key)
            self.seen_key_set.add(key)
            side = str(trade.get("side", "")).lower()
            qty = max(safe_float(trade.get("quantity")), 0.0)
            price = safe_float(trade.get("price", trade.get("last_price")), 0.0)
            if qty > 0:
                self.trades.append((side, qty, price))

    def imbalance(self) -> float:
        if not self.trades:
            return 0.0
        buy_qty = sum(qty for side, qty, _ in self.trades if side == "buy")
        sell_qty = sum(qty for side, qty, _ in self.trades if side == "sell")
        total = buy_qty + sell_qty
        return (buy_qty - sell_qty) / total if total > 0 else 0.0


class TradeAnalyzer:
    """Legacy test/API shim; the live agent uses TradeFlow."""

    def __init__(self, window: int = 140) -> None:
        self.trades: deque[tuple[str, float]] = deque(maxlen=window)
        self.seen_keys: deque[str] = deque(maxlen=800)
        self.seen_key_set: set[str] = set()

    def record_trades(self, trades: list[dict[str, Any]]) -> None:
        for trade in trades:
            key = trade_key(trade)
            if key in self.seen_key_set:
                continue
            if len(self.seen_keys) >= self.seen_keys.maxlen:
                old_key = self.seen_keys.popleft()
                self.seen_key_set.discard(old_key)
            self.seen_keys.append(key)
            self.seen_key_set.add(key)
            side = str(trade.get("side", "")).lower()
            qty = max(safe_float(trade.get("quantity")), 0.0)
            if side in {"buy", "sell"} and qty > 0:
                self.trades.append((side, qty))

    def sentiment(self) -> float:
        if not self.trades:
            return 0.0
        buy_qty = sum(qty for side, qty in self.trades if side == "buy")
        sell_qty = sum(qty for side, qty in self.trades if side == "sell")
        total = buy_qty + sell_qty
        return (buy_qty - sell_qty) / total if total > 0 else 0.0


class FillTracker:
    def __init__(self, window: int = 24) -> None:
        self.fills: deque[str] = deque(maxlen=window)
        self.last_change_at = 0.0

    def record_inventory_change(self, previous: float, current: float) -> None:
        if abs(current - previous) <= 1e-9:
            return
        self.fills.append("buy" if current > previous else "sell")
        self.last_change_at = time.time()

    def one_sided_score(self) -> float:
        n = len(self.fills)
        if n < 5:
            return 0.0
        buys = self.fills.count("buy")
        return max(buys, n - buys) / n


class OrderThrottle:
    def __init__(self, order_budget: int) -> None:
        self.order_budget = max(order_budget, 1)
        self.timestamps: deque[float] = deque()
        self.block_until = 0.0

    def can_submit(self) -> bool:
        self._trim()
        return time.time() >= self.block_until and len(self.timestamps) < self.order_budget

    def record(self, count: int) -> None:
        now = time.time()
        for _ in range(max(count, 0)):
            self.timestamps.append(now)

    def cool_down(self, seconds: float) -> None:
        self.block_until = max(self.block_until, time.time() + seconds)

    def used(self) -> int:
        self._trim()
        return len(self.timestamps)

    def _trim(self) -> None:
        cutoff = time.time() - 60.0
        while self.timestamps and self.timestamps[0] < cutoff:
            self.timestamps.popleft()


@dataclass
class Signals:
    regime: str
    trend: float
    short_return: float
    medium_return: float
    realized_vol: float
    displayed_spread: float
    spread_bps: float
    dislocation: float
    book_imbalance: float
    trade_imbalance: float
    microprice: float
    fair_value: float
    shock: float
    stress: float


def build_signals(state: dict[str, Any], trade_flow: TradeFlow, mid: float, tick: float) -> Signals:
    best_bid = safe_float(state.get("best_bid"), mid)
    best_ask = safe_float(state.get("best_ask"), mid)
    displayed_spread = max(best_ask - best_bid, safe_float(state.get("spread")), tick)
    spread_bps = displayed_spread / max(mid, 0.01) * 10_000.0

    history = state.get("history", [])
    prices = [extract_history_mid(point, mid) for point in history[-80:] if isinstance(point, dict)]
    if not prices or prices[-1] != mid:
        prices.append(mid)
    short_return = pct(prices[-1], prices[-6]) if len(prices) >= 6 else 0.0
    medium_return = pct(prices[-1], prices[-22]) if len(prices) >= 22 else short_return
    shock = pct(prices[-1], prices[-2]) if len(prices) >= 2 else 0.0
    realized_vol = max(robust_realized_vol(prices[-32:]), safe_float(state.get("volatility"), 0.0))

    book = state.get("order_book", {})
    bids = book.get("bids", [])
    asks = book.get("asks", [])
    bid_vol = top_volume(bids)
    ask_vol = top_volume(asks)
    book_imbalance = (bid_vol - ask_vol) / (bid_vol + ask_vol + 1e-9)
    micro = microprice_from_book(mid, best_bid, best_ask, bids, asks)

    fundamental = safe_float(state.get("fundamental_price", mid), mid)
    dislocation = clamp(pct(fundamental, mid), -0.04, 0.04) if fundamental > 0 else 0.0
    trade_imbalance = trade_flow.imbalance()
    trend = clamp(0.62 * medium_return + 0.38 * short_return, -0.05, 0.05)
    stress = max(abs(shock) * 2.8, realized_vol * 2.0, spread_bps / 2500.0)

    if stress > 0.028:
        regime = "shock"
    elif stress > 0.014:
        regime = "volatile"
    elif trend > max(0.0025, realized_vol * 0.7):
        regime = "trend_up"
    elif trend < -max(0.0025, realized_vol * 0.7):
        regime = "trend_down"
    elif abs(dislocation) > max(0.004, realized_vol * 1.8):
        regime = "mean_revert"
    elif realized_vol < 0.0035 and spread_bps < 35:
        regime = "calm"
    else:
        regime = "drift"

    fair = fair_value(
        mid=mid,
        micro=micro,
        fundamental=fundamental,
        dislocation=dislocation,
        trend=trend,
        book_imbalance=book_imbalance,
        trade_imbalance=trade_imbalance,
        regime=regime,
    )
    return Signals(
        regime=regime,
        trend=trend,
        short_return=short_return,
        medium_return=medium_return,
        realized_vol=realized_vol,
        displayed_spread=displayed_spread,
        spread_bps=spread_bps,
        dislocation=dislocation,
        book_imbalance=book_imbalance,
        trade_imbalance=trade_imbalance,
        microprice=micro,
        fair_value=fair,
        shock=shock,
        stress=stress,
    )


def price_tick_from_state(state: dict[str, Any], fallback: float, mid: float) -> float:
    return TickEstimator(fallback).update(state, mid)


def compute_regime(state: dict[str, Any]) -> str:
    mid = safe_float(state.get("mid_price"), 0.0)
    if mid <= 0:
        return "unknown"
    tick = price_tick_from_state(state, fallback=0.01, mid=mid)
    return build_signals(state, TradeFlow(), mid, tick).regime


def microprice_from_book(
    mid: float,
    best_bid: float,
    best_ask: float,
    bids: list[dict[str, Any]],
    asks: list[dict[str, Any]],
) -> float:
    bid_vol = top_volume(bids)
    ask_vol = top_volume(asks)
    total = bid_vol + ask_vol
    if total > 0 and best_bid > 0 and best_ask > 0:
        return (best_ask * bid_vol + best_bid * ask_vol) / total
    bid_vwap = weighted_price(bids)
    ask_vwap = weighted_price(asks)
    if bid_vwap and ask_vwap:
        return (bid_vwap + ask_vwap) / 2.0
    return mid


def fair_value(
    *,
    mid: float,
    micro: float,
    fundamental: float,
    dislocation: float,
    trend: float,
    book_imbalance: float,
    trade_imbalance: float,
    regime: str,
) -> float:
    if regime in {"shock", "volatile"}:
        fundamental_weight = 0.34
        micro_weight = 0.20
        trend_weight = 0.10
    elif regime in {"trend_up", "trend_down"}:
        fundamental_weight = 0.18
        micro_weight = 0.30
        trend_weight = 0.34
    elif regime == "mean_revert":
        fundamental_weight = 0.48
        micro_weight = 0.22
        trend_weight = 0.08
    else:
        fundamental_weight = 0.20
        micro_weight = 0.34
        trend_weight = 0.16
    mid_weight = max(0.0, 1.0 - fundamental_weight - micro_weight)
    bounded_fundamental = mid * (1.0 + clamp(dislocation, -0.025, 0.025))
    pressure = clamp(0.65 * book_imbalance + 0.35 * trade_imbalance, -1.0, 1.0)
    trend_bias = clamp(trend, -0.018, 0.018) * trend_weight
    pressure_bias = pressure * 0.00055
    fair = (
        mid * mid_weight
        + micro * micro_weight
        + bounded_fundamental * fundamental_weight
    )
    return fair * (1.0 + trend_bias + pressure_bias)


def account_loss_pressure(account: dict[str, Any], initial_cash: float) -> float:
    equity = safe_float(account.get("equity"), initial_cash)
    profit_loss = safe_float(account.get("profit_loss"), equity - initial_cash)
    drawdown = safe_float(account.get("max_drawdown"), 0.0)
    loss_ratio = max(0.0, -profit_loss) / max(initial_cash, 1.0)
    dd_ratio = max(0.0, drawdown) / max(initial_cash, 1.0)
    return clamp(max(loss_ratio / 0.018, dd_ratio / 0.030), 0.0, 1.0)


def capital_limits(args: argparse.Namespace, account: dict[str, Any], mid: float) -> tuple[float, float]:
    initial = max(safe_float(account.get("initial_cash"), args.starting_cash), 1.0)
    equity = max(safe_float(account.get("equity"), initial), 1.0)
    max_notional = min(args.max_notional, max(equity * args.max_notional_fraction, args.min_quantity * mid))
    order_notional = min(args.order_notional, max(equity * args.order_notional_fraction, args.min_quantity * mid))
    max_pos = min(args.max_pos, max_notional / max(mid, 0.01))
    order_size = min(args.order_size, order_notional / max(mid, 0.01))
    return max(max_pos, args.min_quantity), max(order_size, 0.0)


def dynamic_limits(args: argparse.Namespace, account: dict[str, Any], mid: float) -> tuple[float, float]:
    return capital_limits(args, account, mid)


def target_inventory_ratio(signals: Signals, loss_pressure: float) -> float:
    if loss_pressure > 0.72 or signals.regime == "shock":
        return 0.0
    if signals.regime in {"trend_up", "trend_down"}:
        trend_component = clamp(signals.trend / max(signals.realized_vol * 4.0, 0.006), -1.0, 1.0)
        return clamp(trend_component * 0.24, -0.30, 0.30)
    if signals.regime == "mean_revert":
        return clamp(signals.dislocation / max(abs(signals.dislocation) + 0.008, 0.008), -1.0, 1.0) * 0.22
    flow = clamp(0.55 * signals.book_imbalance + 0.45 * signals.trade_imbalance, -1.0, 1.0)
    return clamp(flow * 0.12, -0.16, 0.16)


def quote_width(signals: Signals, mid: float, tick: float, adverse: float, loss_pressure: float) -> float:
    regime_mult = {
        "calm": 0.88,
        "drift": 1.12,
        "mean_revert": 1.18,
        "trend_up": 1.42,
        "trend_down": 1.42,
        "volatile": 1.95,
        "shock": 3.15,
    }.get(signals.regime, 1.2)
    vol_width = mid * min(max(signals.realized_vol * 2.1, 0.00025), 0.035)
    trend_width = mid * min(abs(signals.trend) * 0.34, 0.010)
    spread_width = max(signals.displayed_spread * 1.12, tick * 2.0)
    width = max(spread_width, vol_width + trend_width, mid * 0.00028)
    width *= regime_mult * (1.0 + adverse * 0.50 + loss_pressure * 1.15)
    return max(width, tick * 2.0)


def quote_sizes(
    args: argparse.Namespace,
    account: dict[str, Any],
    bid_price: float,
    order_size: float,
    max_pos: float,
    target_ratio: float = 0.0,
    loss_pressure: float = 0.0,
) -> tuple[float, float]:
    inventory = safe_float(account.get("inventory"), 0.0)
    cash = max(safe_float(account.get("cash"), 0.0), 0.0)
    inv_ratio = inventory / max(max_pos, args.min_quantity)
    lean = clamp(target_ratio - inv_ratio, -1.0, 1.0)
    base_size = max(order_size, 0.0)
    risk_scale = clamp(1.0 - loss_pressure * 0.65, 0.35, 1.0)
    buy_size = base_size * (1.0 + max(lean, 0.0) * 1.15 - max(-lean, 0.0) * 0.62) * risk_scale
    sell_size = base_size * (1.0 + max(-lean, 0.0) * 1.15 - max(lean, 0.0) * 0.62) * risk_scale
    buy_capacity = max(0.0, max_pos - inventory)
    sell_capacity = max(0.0, max_pos + inventory)
    cash_capacity = cash * 0.94 / max(bid_price, 0.01)
    return min(max(buy_size, 0.0), buy_capacity, cash_capacity), min(max(sell_size, 0.0), sell_capacity)


def cancel_open_orders(api_url: str, user: str) -> None:
    orders = call_api(api_url, api_user_path("/orders", user), timeout=2.0)
    for order in (orders or {}).get("orders", []):
        if order.get("status") in OPEN_STATUSES:
            call_api(api_url, api_user_path(f"/orders/{order['order_id']}", user), method="DELETE", timeout=2.0)


def submit_order(api_url: str, user: str, side: str, quantity: float, price: float) -> bool:
    payload = {
        "side": side,
        "quantity": round(quantity, 4),
        "order_type": "limit",
        "price": round(price, 8),
        "user": user,
        "post_only": True,
    }
    result = call_api(api_url, "/order", "POST", payload, timeout=2.0)
    if not result:
        logging.debug("%s quote failed without API response", side)
        return False
    if result.get("status") not in ACCEPTED_STATUSES and safe_float(result.get("filled_quantity"), 0.0) <= 0:
        logging.info("%s quote rejected at %.6f: %s", side, price, result.get("reject_reason", result.get("status")))
        return False
    return True


def trade_loop(args: argparse.Namespace) -> int:
    logging.info("Deploying RaiderCore v8.0 generalist risk engine as %s", args.user)
    call_api(args.url, "/accounts", "POST", {"user": args.user, "starting_cash": args.starting_cash})

    tick_estimator = TickEstimator(args.tick)
    trade_flow = TradeFlow()
    fills = FillTracker()
    throttle = OrderThrottle(args.order_budget)

    last_inventory = 0.0
    last_bid = last_ask = 0.0
    last_quote_at = 0.0
    last_status_at = 0.0

    while True:
        state = call_api(args.url, api_user_path("/state", args.user), timeout=2.0)
        account = call_api(args.url, api_user_path("/account", args.user), timeout=2.0)
        if not state or not account:
            time.sleep(max(args.interval, 0.25))
            continue
        if any(state.get(key) is None for key in ("mid_price", "best_bid", "best_ask")):
            time.sleep(args.interval)
            continue

        mid = safe_float(state.get("mid_price"), 0.0)
        best_bid = safe_float(state.get("best_bid"), 0.0)
        best_ask = safe_float(state.get("best_ask"), 0.0)
        if mid <= 0 or best_bid <= 0 or best_ask <= 0 or best_bid >= best_ask:
            time.sleep(args.interval)
            continue

        initial_cash = max(safe_float(account.get("initial_cash"), args.starting_cash), 1.0)
        equity = safe_float(account.get("equity"), initial_cash)
        if int(account.get("orders", 0)) >= args.max_orders:
            logging.warning("Order cap %d reached; shutting down", args.max_orders)
            cancel_open_orders(args.url, args.user)
            return 0
        if equity < initial_cash * (1.0 - args.drawdown_limit):
            logging.error("Drawdown limit hit: equity=%.2f initial=%.2f", equity, initial_cash)
            cancel_open_orders(args.url, args.user)
            return 1

        inventory = safe_float(account.get("inventory"), 0.0)
        fills.record_inventory_change(last_inventory, inventory)
        last_inventory = inventory

        tick = tick_estimator.update(state, mid)
        trade_flow.record_trades(state.get("trades", []))
        signals = build_signals(state, trade_flow, mid, tick)
        loss_pressure = account_loss_pressure(account, initial_cash)
        adverse = fills.one_sided_score()
        max_pos, base_size = capital_limits(args, account, mid)
        target_ratio = target_inventory_ratio(signals, loss_pressure)
        width = quote_width(signals, mid, tick, adverse, loss_pressure)
        inv_ratio = inventory / max(max_pos, args.min_quantity)
        inv_error = inv_ratio - target_ratio
        skew = math.copysign(abs(inv_error) ** 1.25, inv_error) * width * args.skew_strength

        fair = signals.fair_value
        jitter = (random.random() - 0.5) * tick * 0.65
        bid = snap_price(min(fair - width / 2.0 - skew + jitter, best_ask - tick), tick)
        ask = snap_price(max(fair + width / 2.0 - skew + jitter, best_bid + tick), tick)
        if bid <= 0 or ask <= 0 or bid >= ask:
            time.sleep(args.interval)
            continue

        now = time.time()
        regime_gap = {
            "calm": 0.80,
            "drift": 1.05,
            "mean_revert": 1.10,
            "trend_up": 1.35,
            "trend_down": 1.35,
            "volatile": 2.30,
            "shock": 5.00,
        }.get(signals.regime, 1.2)
        duty_gap = regime_gap * (1.0 + loss_pressure * 1.7 + adverse * 0.35)
        min_requote_move = max(tick * args.requote_ticks, width * 0.16)
        price_moved = abs(bid - last_bid) >= min_requote_move or abs(ask - last_ask) >= min_requote_move
        toxic = (last_bid > 0 and last_bid >= best_ask) or (last_ask > 0 and last_ask <= best_bid)
        stale = now - last_quote_at > duty_gap * 4.5

        if adverse > 0.82 or loss_pressure > 0.82:
            throttle.cool_down(1.5 + 4.0 * max(adverse - 0.82, loss_pressure - 0.82, 0.0))

        if (price_moved or toxic or stale) and now - last_quote_at >= duty_gap and throttle.can_submit():
            cancel_open_orders(args.url, args.user)
            buy_size, sell_size = quote_sizes(
                args,
                account,
                bid,
                base_size,
                max_pos,
                target_ratio,
                loss_pressure,
            )

            if abs(inv_ratio) >= args.unwind_threshold or loss_pressure >= 0.74:
                close_size = min(max(abs(inventory) * 0.42, args.min_quantity), max_pos + abs(inventory))
                if inventory > 0:
                    buy_size = 0.0
                    sell_size = max(sell_size, close_size)
                    ask = snap_price(max(min(ask, best_ask + width * 0.25), best_bid + tick), tick)
                elif inventory < 0:
                    sell_size = 0.0
                    buy_size = max(buy_size, close_size)
                    bid = snap_price(min(max(bid, best_bid - width * 0.25), best_ask - tick), tick)
                else:
                    buy_size *= 0.35
                    sell_size *= 0.35

            placed = 0
            bid_ok = ask_ok = False
            if buy_size >= args.min_quantity:
                bid_ok = submit_order(args.url, args.user, "buy", buy_size, bid)
                placed += int(bid_ok)
            if sell_size >= args.min_quantity:
                ask_ok = submit_order(args.url, args.user, "sell", sell_size, ask)
                placed += int(ask_ok)
            if placed:
                throttle.record(placed)
            last_bid = bid if bid_ok else 0.0
            last_ask = ask if ask_ok else 0.0
            last_quote_at = now

        if now - last_status_at >= 30.0:
            logging.info(
                "state=%s equity=%.2f inv=%.4f target=%.2f width=%.5f dd_pressure=%.2f orders60=%d",
                signals.regime,
                equity,
                inventory,
                target_ratio,
                width,
                loss_pressure,
                throttle.used(),
            )
            last_status_at = now

        time.sleep(args.interval)


def main() -> int:
    parser = argparse.ArgumentParser(description="RaiderCore v8.0 generalist market-making agent")
    parser.add_argument("user", nargs="?", default="RaiderCore")
    parser.add_argument("--url", default="http://127.0.0.1:8000/api")
    parser.add_argument("--starting-cash", type=float, default=10_000.0)
    parser.add_argument("--interval", type=float, default=0.18)
    parser.add_argument("--tick", type=float, default=0.01)
    parser.add_argument("--order-size", type=float, default=40.0, help="Absolute unit cap per order.")
    parser.add_argument("--order-notional", type=float, default=20_000.0)
    parser.add_argument("--order-notional-fraction", type=float, default=0.18)
    parser.add_argument("--max-pos", type=float, default=600.0)
    parser.add_argument("--max-notional", type=float, default=100_000.0)
    parser.add_argument("--max-notional-fraction", type=float, default=0.85)
    parser.add_argument("--drawdown-limit", type=float, default=0.10)
    parser.add_argument("--max-orders", type=int, default=5000)
    parser.add_argument("--unwind-threshold", type=float, default=0.58)
    parser.add_argument("--min-quantity", type=float, default=1.0)
    parser.add_argument("--order-budget", type=int, default=36, help="Max quote orders per rolling 60 seconds.")
    parser.add_argument("--requote-ticks", type=float, default=4.0)
    parser.add_argument("--skew-strength", type=float, default=2.9)
    parser.add_argument("--model-path", help="Accepted for supervisor compatibility; unused.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        return trade_loop(args)
    except KeyboardInterrupt:
        cancel_open_orders(args.url, args.user)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
