from __future__ import annotations

import argparse
import json
import logging
import math
import sys
import time
import urllib.parse
import urllib.request
from typing import Any


OPEN_STATUSES = {"open", "partially_filled", "pending_trigger"}
ACCEPTED_STATUSES = {"open", "partially_filled", "filled"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Current-state adaptive market-making agent.")
    parser.add_argument("user", nargs="?", default="AdaptiveEdgeMaker")
    parser.add_argument("--url", default="http://127.0.0.1:8000/api")
    parser.add_argument("--starting-cash", type=float, default=10000.0)
    parser.add_argument("--max-pos", type=float, default=550.0)
    parser.add_argument("--order-size", type=float, default=35.0)
    parser.add_argument("--min-spread", type=float, default=0.06)
    parser.add_argument("--interval", type=float, default=0.25)
    parser.add_argument("--tick", type=float, default=0.01)
    parser.add_argument("--max-orders", type=int, default=1000)
    parser.add_argument("--drawdown-limit", type=float, default=0.18)
    parser.add_argument("--max-quote-age", type=float, default=25.0)
    parser.add_argument("--max-data-age", type=float, default=6.0)
    return parser.parse_args()


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


def effective_tick(state: dict[str, Any], args: argparse.Namespace) -> float:
    return max(float(state.get("tick", args.tick) or args.tick), args.tick, 0.01)


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


def cancel_open_orders(api_url: str, user: str) -> None:
    orders = call_api(api_url, user_path("/orders", user))
    for order in (orders or {}).get("orders", []):
        if order.get("status") in OPEN_STATUSES:
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


def quote_width(state: dict[str, Any], args: argparse.Namespace) -> float:
    mid = float(state["mid_price"])
    tick = effective_tick(state, args)
    displayed_spread = max(float(state.get("spread", 0.0)), tick)
    volatility = max(float(state.get("volatility", 0.0)), 0.0)
    volatility_width = mid * (0.00045 + min(volatility, 0.02) * 3.0)
    maker_fee = abs(float(state.get("fees", {}).get("maker_fee_rate", 0.0002)))
    fee_width = mid * maker_fee * 2.8
    return max(args.min_spread, displayed_spread * 1.15, volatility_width, fee_width)


def market_is_actionable(state: dict[str, Any], args: argparse.Namespace) -> bool:
    mid = float(state["mid_price"])
    best_bid = float(state["best_bid"])
    best_ask = float(state["best_ask"])
    if mid <= 0 or best_bid <= 0 or best_ask <= 0 or best_bid >= best_ask:
        return False
    quote_age = state.get("quote_age_seconds")
    if quote_age is not None and float(quote_age) > args.max_data_age:
        return False
    spread = best_ask - best_bid
    if spread > max(mid * 0.012, effective_tick(state, args) * 12.0):
        return False
    bids = state.get("order_book", {}).get("bids", [])
    asks = state.get("order_book", {}).get("asks", [])
    return bool(bids and asks and top_volume(bids, 3) > 0 and top_volume(asks, 3) > 0)


def side_permissions(
    imbalance: float,
    micro: float,
    mid: float,
    tick: float,
    inventory: float,
    max_pos: float,
) -> tuple[bool, bool]:
    allow_buy = inventory < max_pos
    allow_sell = inventory > -max_pos
    pressure = (micro - mid) / max(mid, 0.01)
    if imbalance > 0.62 and pressure > max(0.00012, tick / max(mid, 0.01)) and inventory <= 0:
        allow_sell = False
    elif imbalance < -0.62 and pressure < -max(0.00012, tick / max(mid, 0.01)) and inventory >= 0:
        allow_buy = False
    return allow_buy, allow_sell


def quote_sizes(account: dict[str, Any], args: argparse.Namespace, bid_price: float) -> tuple[float, float]:
    inventory = float(account.get("inventory", 0.0))
    cash = float(account.get("cash", 0.0))
    buy_capacity = max(0.0, args.max_pos - inventory)
    sell_capacity = max(0.0, args.max_pos + inventory)
    cash_capacity = max(0.0, cash * 0.95 / max(bid_price, 0.01))
    return min(args.order_size, buy_capacity, cash_capacity), min(args.order_size, sell_capacity)


def run(args: argparse.Namespace) -> int:
    logging.info("Starting %s", args.user)
    call_api(args.url, "/accounts", "POST", {"user": args.user, "starting_cash": args.starting_cash})
    last_bid = 0.0
    last_ask = 0.0
    last_quote_at = 0.0

    try:
        while True:
            state = call_api(args.url, "/state")
            account = call_api(args.url, user_path("/account", args.user))
            if not state or not account:
                time.sleep(0.5)
                continue
            if account.get("orders", 0) >= args.max_orders:
                cancel_open_orders(args.url, args.user)
                return 0

            initial_cash = max(float(account.get("initial_cash", args.starting_cash)), 1.0)
            if float(account.get("equity", initial_cash)) < initial_cash * (1 - args.drawdown_limit):
                cancel_open_orders(args.url, args.user)
                return 1

            if state.get("mid_price") is None or state.get("best_bid") is None or state.get("best_ask") is None:
                if last_bid or last_ask:
                    cancel_open_orders(args.url, args.user)
                    last_bid = last_ask = last_quote_at = 0.0
                time.sleep(args.interval)
                continue

            if not market_is_actionable(state, args):
                if last_bid or last_ask:
                    cancel_open_orders(args.url, args.user)
                    last_bid = last_ask = last_quote_at = 0.0
                time.sleep(args.interval)
                continue

            bids = state.get("order_book", {}).get("bids", [])
            asks = state.get("order_book", {}).get("asks", [])
            bid_vol = top_volume(bids)
            ask_vol = top_volume(asks)
            imbalance = (bid_vol - ask_vol) / (bid_vol + ask_vol + 1e-9)

            mid = float(state["mid_price"])
            micro = current_microprice(state)
            tick = effective_tick(state, args)
            width = quote_width(state, args)
            inventory = float(account.get("inventory", 0.0))
            inventory_skew = clamp(inventory / args.max_pos, -1.5, 1.5) * width * 1.8
            imbalance_adjust = clamp(imbalance, -1.0, 1.0) * width * 0.18
            fair = mid * 0.65 + micro * 0.35 + imbalance_adjust

            bid = round(min(fair - width / 2 - inventory_skew, float(state["best_ask"]) - tick), 4)
            ask = round(max(fair + width / 2 - inventory_skew, float(state["best_bid"]) + tick), 4)
            if bid <= 0 or ask <= 0 or bid >= ask:
                if last_bid or last_ask:
                    cancel_open_orders(args.url, args.user)
                    last_bid = last_ask = last_quote_at = 0.0
                time.sleep(args.interval)
                continue

            buy_size, sell_size = quote_sizes(account, args, bid)
            allow_buy, allow_sell = side_permissions(imbalance, micro, mid, tick, inventory, args.max_pos)
            if not allow_buy:
                buy_size = 0.0
            if not allow_sell:
                sell_size = 0.0

            desired_bid = buy_size >= 1
            desired_ask = sell_size >= 1
            side_set_changed = desired_bid != (last_bid > 0) or desired_ask != (last_ask > 0)
            quote_stale = last_quote_at > 0 and time.time() - last_quote_at >= args.max_quote_age
            should_requote = abs(bid - last_bid) > tick or abs(ask - last_ask) > tick or quote_stale or side_set_changed
            if should_requote:
                cancel_open_orders(args.url, args.user)
                bid_ok = desired_bid and submit_quote(args.url, args.user, "buy", buy_size, bid)
                ask_ok = desired_ask and submit_quote(args.url, args.user, "sell", sell_size, ask)
                last_bid = bid if bid_ok else 0.0
                last_ask = ask if ask_ok else 0.0
                last_quote_at = time.time() if bid_ok or ask_ok else 0.0
            time.sleep(args.interval)
    finally:
        cancel_open_orders(args.url, args.user)


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
