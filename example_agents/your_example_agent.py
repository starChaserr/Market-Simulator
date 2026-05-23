from __future__ import annotations

import argparse
import json
import logging
import time
import urllib.parse
import urllib.request
from typing import Any


OPEN_STATUSES = {"open", "partially_filled", "pending_trigger"}


class ApiClient:
    def __init__(self, base_url: str, timeout: float = 2.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def request(self, path: str, method: str = "GET", data: dict[str, Any] | None = None) -> dict[str, Any] | None:
        body = None
        request = urllib.request.Request(f"{self.base_url}{path}", method=method)
        if data is not None:
            request.add_header("Content-Type", "application/json")
            body = json.dumps(data).encode("utf-8")
        try:
            with urllib.request.urlopen(request, data=body, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            logging.debug("%s %s failed: %s", method, path, exc)
            return None

    def user_path(self, path: str, user: str) -> str:
        separator = "&" if "?" in path else "?"
        return f"{path}{separator}user={urllib.parse.quote(user)}"

    def health(self) -> dict[str, Any] | None:
        return self.request("/health")

    def config(self) -> dict[str, Any] | None:
        return self.request("/config")

    def currency(self) -> dict[str, Any] | None:
        query = urllib.parse.urlencode({"locale": "en-US", "timezone": "UTC", "currency": "USD"})
        return self.request(f"/currency?{query}")

    def chart_refresh(self) -> dict[str, Any] | None:
        return self.request("/chart-refresh")

    def set_chart_refresh(self, chart_refresh_ms: int) -> dict[str, Any] | None:
        return self.request("/chart-refresh", "POST", {"chart_refresh_ms": chart_refresh_ms})

    def state(self, user: str | None = None) -> dict[str, Any] | None:
        return self.request(self.user_path("/state", user) if user else "/state")

    def stream_once(self, user: str | None = None) -> dict[str, Any] | None:
        path = self.user_path("/stream", user) if user else "/stream"
        try:
            with urllib.request.urlopen(f"{self.base_url}{path}", timeout=self.timeout) as response:
                for raw_line in response:
                    line = raw_line.decode("utf-8").strip()
                    if line.startswith("data: "):
                        return json.loads(line.removeprefix("data: "))
        except Exception as exc:
            logging.debug("GET /stream failed: %s", exc)
        return None

    def orderbook(self) -> dict[str, Any] | None:
        return self.request("/orderbook")

    def trades(self) -> dict[str, Any] | None:
        return self.request("/trades")

    def agents(self) -> dict[str, Any] | None:
        return self.request("/agents")

    def users(self) -> dict[str, Any] | None:
        return self.request("/users")

    def accounts(self) -> dict[str, Any] | None:
        return self.request("/accounts")

    def create_account(self, user: str, starting_cash: float) -> dict[str, Any] | None:
        return self.request("/accounts", "POST", {"user": user, "starting_cash": starting_cash})

    def fund_account(self, user: str, amount: float) -> dict[str, Any] | None:
        return self.request("/accounts/fund", "POST", {"user": user, "amount": amount})

    def account(self, user: str) -> dict[str, Any] | None:
        return self.request(self.user_path("/account", user))

    def orders(self, user: str | None = None, status: str | None = None, include_internal: bool = False) -> dict[str, Any] | None:
        params: dict[str, str] = {"limit": "100"}
        if user:
            params["user"] = user
        if status:
            params["status"] = status
        if include_internal:
            params["include_internal"] = "true"
        return self.request(f"/orders?{urllib.parse.urlencode(params)}")

    def order_status(self, order_id: str) -> dict[str, Any] | None:
        return self.request(f"/orders/{urllib.parse.quote(order_id)}")

    def cancel_order_delete(self, order_id: str, user: str) -> dict[str, Any] | None:
        return self.request(self.user_path(f"/orders/{urllib.parse.quote(order_id)}", user), "DELETE")

    def buy(self, user: str, quantity: float, price: float) -> dict[str, Any] | None:
        return self.request("/buy", "POST", self.limit_payload("buy", user, quantity, price))

    def sell(self, user: str, quantity: float, price: float) -> dict[str, Any] | None:
        return self.request("/sell", "POST", self.limit_payload("sell", user, quantity, price))

    def order(self, side: str, user: str, quantity: float, price: float) -> dict[str, Any] | None:
        return self.request("/order", "POST", self.limit_payload(side, user, quantity, price))

    def cancel_order_post(self, order_id: str, user: str) -> dict[str, Any] | None:
        return self.request("/cancel", "POST", {"order_id": order_id, "user": user})

    def simulation(self, running: bool) -> dict[str, Any] | None:
        return self.request("/simulation", "POST", {"running": running})

    def reset(self) -> dict[str, Any] | None:
        return self.request("/reset", "POST", {})

    def clear_users(self) -> dict[str, Any] | None:
        return self.request("/users", "DELETE")

    @staticmethod
    def limit_payload(side: str, user: str, quantity: float, price: float) -> dict[str, Any]:
        return {
            "side": side,
            "quantity": round(quantity, 4),
            "order_type": "limit",
            "price": round(price, 4),
            "post_only": True,
            "user": user,
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Tiny example agent that demonstrates the simulator API.")
    parser.add_argument("user", nargs="?", default="YourExampleAgent")
    parser.add_argument("--url", default="http://127.0.0.1:8000/api")
    parser.add_argument("--starting-cash", type=float, default=1_000_000.0)
    parser.add_argument("--fund-demo-amount", type=float, default=1_000.0)
    parser.add_argument("--quantity", type=float, default=5.0)
    parser.add_argument("--max-orders", type=int, default=20)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--offline-limit", type=int, default=20)
    parser.add_argument("--exercise-destructive", action="store_true", help="Also call /api/reset and DELETE /api/users. Use only in an isolated demo.")
    return parser.parse_args()


def safe_price(state: dict[str, Any], side: str, cushion: float = 0.05) -> float | None:
    best_bid = state.get("best_bid")
    best_ask = state.get("best_ask")
    mid = state.get("mid_price")
    if best_bid is None or best_ask is None or mid is None:
        return None
    if side == "buy":
        return max(0.01, min(float(best_bid), float(mid) - cushion))
    return max(float(best_ask), float(mid) + cushion)


def cancel_all_open(client: ApiClient, user: str) -> None:
    response = client.orders(user=user)
    for order in (response or {}).get("orders", []):
        if order.get("status") in OPEN_STATUSES:
            client.cancel_order_delete(order["order_id"], user)


def exercise_api_surface(client: ApiClient, args: argparse.Namespace) -> None:
    client.health()
    client.config()
    client.currency()
    chart_refresh = client.chart_refresh()
    if chart_refresh:
        client.set_chart_refresh(int(chart_refresh.get("chart_refresh_ms", 1000)))
    state = client.state(args.user)
    client.stream_once(args.user)
    client.orderbook()
    client.trades()
    client.agents()
    client.users()
    client.accounts()

    funding = max(0.0, min(args.fund_demo_amount, args.starting_cash))
    account_cash = max(0.0, args.starting_cash - funding)
    client.create_account(args.user, account_cash)
    if funding > 0:
        client.fund_account(args.user, funding)
    client.account(args.user)

    if state:
        client.simulation(bool(state.get("running", True)))

    state = client.state(args.user) or {}
    buy_price = safe_price(state, "buy")
    sell_price = safe_price(state, "sell")
    if buy_price is not None:
        buy_order = client.buy(args.user, 1, buy_price)
        if buy_order and buy_order.get("order_id"):
            client.order_status(buy_order["order_id"])
            client.cancel_order_delete(buy_order["order_id"], args.user)
    if sell_price is not None:
        sell_order = client.sell(args.user, 1, sell_price)
        if sell_order and sell_order.get("order_id"):
            client.order_status(sell_order["order_id"])
            client.cancel_order_post(sell_order["order_id"], args.user)

    client.orders(user=args.user)
    if args.exercise_destructive:
        client.reset()
        client.clear_users()
        client.create_account(args.user, args.starting_cash)
    else:
        logging.info("Skipped destructive API calls: POST /reset and DELETE /users")


def trade_loop(client: ApiClient, args: argparse.Namespace) -> None:
    last_bid = 0.0
    last_ask = 0.0
    offline_misses = 0
    while True:
        state = client.state(args.user)
        account = client.account(args.user)
        if not state or not account:
            offline_misses += 1
            if offline_misses >= args.offline_limit:
                logging.warning("Simulator unavailable after %s retries; exiting", offline_misses)
                return
            time.sleep(args.interval)
            continue
        offline_misses = 0
        if int(account.get("orders", 0)) >= args.max_orders:
            cancel_all_open(client, args.user)
            return

        buy_price = safe_price(state, "buy", cushion=0.03)
        sell_price = safe_price(state, "sell", cushion=0.03)
        if buy_price is None or sell_price is None:
            time.sleep(args.interval)
            continue

        inventory = float(account.get("inventory", 0.0))
        can_buy = inventory < 50
        can_sell = inventory > -50
        if abs(buy_price - last_bid) > 0.01 or abs(sell_price - last_ask) > 0.01:
            cancel_all_open(client, args.user)
            if can_buy:
                client.order("buy", args.user, args.quantity, buy_price)
            if can_sell:
                client.order("sell", args.user, args.quantity, sell_price)
            last_bid = buy_price
            last_ask = sell_price
        time.sleep(args.interval)


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    client = ApiClient(args.url)
    exercise_api_surface(client, args)
    trade_loop(client, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
