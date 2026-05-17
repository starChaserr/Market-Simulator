from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

try:
    from llama_cpp import Llama
except ImportError:
    Llama = None


ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_MODEL_PATH = ROOT_DIR / "llama-3-8b-instruct.gguf"
OPEN_STATUSES = {"open", "partially_filled", "pending_trigger"}
ACCEPTED_STATUSES = {"open", "partially_filled", "filled"}

config = {
    "MAX_POS": 400,
    "ORDER_SIZE": 20,
    "MIN_SPREAD": 0.12,
    "SKEW_STRENGTH": 5.0,
    "ANCHOR_FUND": 0.85,
    "MAX_DRAWDOWN_LIMIT": 0.15,
}
config_lock = threading.Lock()
stop_event = threading.Event()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Adaptive RaiderCore market-making agent.")
    parser.add_argument("user", nargs="?", default="RaiderCore_Pro")
    parser.add_argument("--url", default="http://127.0.0.1:8000/api")
    parser.add_argument("--starting-cash", type=float, default=10000.0)
    parser.add_argument("--model-path", default=str(DEFAULT_MODEL_PATH))
    parser.add_argument("--interval", type=float, default=0.3)
    parser.add_argument("--tick", type=float, default=0.01)
    parser.add_argument("--min-quantity", type=float, default=1.0)
    parser.add_argument("--max-orders", type=int, default=1000)
    return parser.parse_args()


def api_user_path(path: str, user: str) -> str:
    separator = "&" if "?" in path else "?"
    return f"{path}{separator}user={urllib.parse.quote(user)}"


def call_api(api_url: str, path: str, method: str = "GET", data: dict[str, Any] | None = None, timeout: float = 2.0) -> dict[str, Any] | None:
    body = None
    req = urllib.request.Request(f"{api_url.rstrip('/')}{path}", method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
        body = json.dumps(data).encode("utf-8")
    try:
        with urllib.request.urlopen(req, data=body, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        logging.debug("API %s %s failed: %s", method, path, exc)
        return None


def clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(value, upper))


def sanitize_config(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "MAX_POS": int(clamp(float(candidate.get("MAX_POS", config["MAX_POS"])), 50, 2_000)),
        "ORDER_SIZE": int(clamp(float(candidate.get("ORDER_SIZE", config["ORDER_SIZE"])), 1, 100)),
        "MIN_SPREAD": clamp(float(candidate.get("MIN_SPREAD", config["MIN_SPREAD"])), 0.01, 5.0),
        "SKEW_STRENGTH": clamp(float(candidate.get("SKEW_STRENGTH", config["SKEW_STRENGTH"])), 0.0, 20.0),
        "ANCHOR_FUND": clamp(float(candidate.get("ANCHOR_FUND", config["ANCHOR_FUND"])), 0.0, 1.0),
        "MAX_DRAWDOWN_LIMIT": clamp(float(candidate.get("MAX_DRAWDOWN_LIMIT", config["MAX_DRAWDOWN_LIMIT"])), 0.02, 0.8),
    }


def extract_json(text: str) -> dict[str, Any]:
    start = text.find("{")
    end = text.rfind("}") + 1
    if start < 0 or end <= start:
        raise ValueError("LLM output did not contain a JSON object")
    return json.loads(text[start:end])


def llm_brain_thread(args: argparse.Namespace, llm: Any) -> None:
    logging.info("RaiderCore brain initialized")
    while not stop_event.is_set():
        try:
            state = call_api(args.url, "/state")
            account = call_api(args.url, api_user_path("/account", args.user))
            if not state or not account:
                stop_event.wait(5)
                continue

            bids = state.get("order_book", {}).get("bids", [])[:5]
            asks = state.get("order_book", {}).get("asks", [])[:5]
            bid_vol = sum(float(b.get("quantity", 0)) for b in bids)
            ask_vol = sum(float(a.get("quantity", 0)) for a in asks)
            imbalance = (bid_vol - ask_vol) / (bid_vol + ask_vol + 1e-9)

            prompt = f"""<|begin_of_text|><|start_header_id|>system<|end_header_id|>
Production-grade market-making strategist. Optimize current risk-adjusted returns using only current market/account state.
Vol: {state.get('volatility', 0) * 1000:.3f} | OBI: {imbalance:.2f}
Equity: {account.get('equity')} | Inv: {account.get('inventory')}
Respond only with JSON: {{"MAX_POS": int, "ORDER_SIZE": int, "MIN_SPREAD": float, "SKEW_STRENGTH": float, "ANCHOR_FUND": float, "MAX_DRAWDOWN_LIMIT": float}}
<|eot_id|><|start_header_id|>user<|end_header_id|>
Tune the next quoting window without using future prices.<|eot_id|><|start_header_id|>assistant<|end_header_id|>"""

            output = llm(prompt, max_tokens=120, stop=["<|eot_id|>"], echo=False)
            text = output["choices"][0]["text"].strip()
            next_config = sanitize_config(extract_json(text))
            with config_lock:
                config.update(next_config)
            logging.info("LLM config update: %s", next_config)
        except Exception:
            logging.exception("LLM brain update failed")
        stop_event.wait(10)


def cancel_open_orders(api_url: str, user: str) -> None:
    orders = call_api(api_url, api_user_path("/orders", user))
    for order in (orders or {}).get("orders", []):
        if order.get("status") in OPEN_STATUSES:
            call_api(api_url, api_user_path(f"/orders/{order['order_id']}", user), method="DELETE")


def submit_limit(api_url: str, user: str, side: str, quantity: float, price: float) -> bool:
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
        logging.warning("%s quote failed without API response", side)
        return False
    if result.get("status") not in ACCEPTED_STATUSES:
        logging.warning("%s quote rejected: %s", side, result.get("reject_reason", result.get("status")))
        return False
    return True


def quote_sizes(account: dict[str, Any], max_pos: float, order_size: float, bid_price: float) -> tuple[float, float]:
    inventory = float(account.get("inventory", 0))
    cash = float(account.get("cash", 0))
    buy_room = max(0.0, max_pos - inventory)
    sell_room = max(0.0, max_pos + inventory)
    cash_room = max(0.0, cash * 0.95 / max(bid_price, 0.01))
    buy_size = min(order_size, buy_room, cash_room)
    sell_size = min(order_size, sell_room)
    return buy_size, sell_size


def trade_loop(args: argparse.Namespace) -> int:
    model_path = Path(args.model_path).expanduser()
    if Llama is not None and model_path.exists():
        llm = Llama(model_path=str(model_path), n_ctx=2048, n_threads=4, verbose=False)
        threading.Thread(target=llm_brain_thread, args=(args, llm), daemon=True).start()
    else:
        logging.info("LLM disabled; using deterministic RaiderCore config")

    logging.info("Deploying RaiderCore as %s", args.user)
    call_api(args.url, "/accounts", "POST", {"user": args.user, "starting_cash": args.starting_cash})

    last_bid = 0.0
    last_ask = 0.0
    last_inventory = 0.0
    consecutive_fills = 0.0
    cooldown_until = 0.0

    try:
        while not stop_event.is_set():
            state = call_api(args.url, "/state")
            account = call_api(args.url, api_user_path("/account", args.user))
            if not state or not account:
                time.sleep(0.5)
                continue

            with config_lock:
                local_cfg = config.copy()

            initial_cash = max(float(account.get("initial_cash", args.starting_cash)), 1.0)
            equity = float(account.get("equity", initial_cash))
            if equity < initial_cash * (1 - float(local_cfg["MAX_DRAWDOWN_LIMIT"])):
                logging.error("Drawdown breaker tripped. Equity %.2f, initial %.2f", equity, initial_cash)
                cancel_open_orders(args.url, args.user)
                return 1
            if int(account.get("orders", 0)) >= args.max_orders:
                cancel_open_orders(args.url, args.user)
                return 0

            inventory = float(account.get("inventory", 0))
            if abs(inventory - last_inventory) > 1e-9:
                consecutive_fills += 1
                if consecutive_fills > 3:
                    logging.info("Rapid fills detected; cooling down")
                    cooldown_until = time.time() + 5.0
                    consecutive_fills = 0
            else:
                consecutive_fills = max(0.0, consecutive_fills - 0.5)
            last_inventory = inventory

            if time.time() < cooldown_until:
                cancel_open_orders(args.url, args.user)
                time.sleep(1)
                continue

            mid = state.get("mid_price")
            best_bid = state.get("best_bid")
            best_ask = state.get("best_ask")
            if mid is None or best_bid is None or best_ask is None:
                time.sleep(args.interval)
                continue

            fund = state.get("fundamental_price", mid)
            market_spread = max(float(state.get("spread", 0.0)), args.tick)
            max_pos = max(float(local_cfg["MAX_POS"]), 1.0)
            inv_ratio = clamp(inventory / max_pos, -2.0, 2.0)
            skew = math.copysign(inv_ratio * inv_ratio, inv_ratio) * max(market_spread, float(local_cfg["MIN_SPREAD"])) * float(local_cfg["SKEW_STRENGTH"])
            fair = float(fund) * float(local_cfg["ANCHOR_FUND"]) + float(mid) * (1 - float(local_cfg["ANCHOR_FUND"]))

            target_bid = fair - float(local_cfg["MIN_SPREAD"]) / 2 - skew
            target_ask = fair + float(local_cfg["MIN_SPREAD"]) / 2 - skew
            target_bid = round(min(target_bid, float(best_ask) - args.tick), 4)
            target_ask = round(max(target_ask, float(best_bid) + args.tick), 4)
            if target_bid <= 0 or target_ask <= 0 or target_bid >= target_ask:
                time.sleep(args.interval)
                continue

            buy_size, sell_size = quote_sizes(account, max_pos, float(local_cfg["ORDER_SIZE"]), target_bid)
            should_requote = abs(target_bid - last_bid) > args.tick or abs(target_ask - last_ask) > args.tick
            if should_requote:
                cancel_open_orders(args.url, args.user)
                bid_ok = buy_size >= args.min_quantity and submit_limit(args.url, args.user, "buy", buy_size, target_bid)
                ask_ok = sell_size >= args.min_quantity and submit_limit(args.url, args.user, "sell", sell_size, target_ask)
                last_bid = target_bid if bid_ok else 0.0
                last_ask = target_ask if ask_ok else 0.0

            time.sleep(args.interval)
    finally:
        stop_event.set()
        cancel_open_orders(args.url, args.user)


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    return trade_loop(args)


if __name__ == "__main__":
    raise SystemExit(main())
