from __future__ import annotations

import argparse
import json
import logging
import math
import sys
import threading
import time
import urllib.parse
import urllib.request
from collections import deque
from pathlib import Path
from typing import Any

# Global constants
OPEN_STATUSES = {"open", "partially_filled", "pending_trigger"}
ACCEPTED_STATUSES = {"open", "partially_filled", "filled"}

class FillTracker:
    def __init__(self, window: int = 20) -> None:
        self._fills: deque[str] = deque(maxlen=window)
        self.last_fill_time = 0.0

    def record(self, side: str) -> None:
        self._fills.append(side)
        self.last_fill_time = time.time()

    def adverse_score(self) -> float:
        n = len(self._fills)
        if n < 5: return 0.0
        buys = self._fills.count("buy")
        return max(buys, n - buys) / n

    def fill_rate(self, window_sec: float = 10.0) -> float:
        """Heuristic fill rate: high means we are quoting too narrow or being picked off."""
        if not self._fills: return 0.0
        # This is a simple counter of fills in the window
        return len(self._fills) / window_sec

class ParametricController:
    """The 'Brain' that self-tunes parameters based on performance feedback."""
    def __init__(self):
        # Target internal states
        self.target_spread_bps = 5.0 # Start slightly wider
        self.skew_intensity = 4.0
        self.order_size_mult = 1.0
        
        # Performance history
        self.pnl_history = deque(maxlen=25) # Reduced from 40 for faster reaction
        self.last_adjustment_at = time.time()

    def tune(self, account: dict[str, Any], adverse: float, fill_rate: float, regime: str, market_spread_bps: float):
        now = time.time()
        if now - self.last_adjustment_at < 1.2: return # Faster tuning cycle (1.2s)
        
        equity = float(account.get("equity", 0))
        self.pnl_history.append(equity)
        
        # 1. Proportional Spread Tuning
        # If adverse selection is high, widen quickly.
        if adverse > 0.60:
            step = (adverse - 0.60) * 15.0 # Higher sensitivity
            self.target_spread_bps += step
        elif fill_rate > 2.5:
            self.target_spread_bps += (fill_rate - 2.5) * 2.0
        elif adverse < 0.35 and len(self.pnl_history) == self.pnl_history.maxlen:
            # Check if equity is stagnant
            pnl_diff = self.pnl_history[-1] - self.pnl_history[0]
            if abs(pnl_diff) < 15.0:
                # Tighten more aggressively if very stagnant
                tighten_step = 0.6 if abs(pnl_diff) < 5.0 else 0.3
                self.target_spread_bps = max(1.1, self.target_spread_bps - tighten_step)

        # Market Awareness: Don't be too narrow if the market is giving us more edge
        self.target_spread_bps = max(self.target_spread_bps, market_spread_bps * 0.95)
        
        # Elastic Recovery: If we are way wider than the market and adverse is low, snap back
        if self.target_spread_bps > market_spread_bps * 2.2 and adverse < 0.30:
            self.target_spread_bps = self.target_spread_bps * 0.6 + market_spread_bps * 0.4
            logging.info("ELASTIC RECOVERY: Snapping spread to %.1f bps", self.target_spread_bps)

        self.target_spread_bps = clamp(self.target_spread_bps, 1.1, 100.0)

        # 2. Skew Tuning (with Hysteresis)
        inventory = abs(float(account.get("inventory", 0)))
        if inventory > 200:
            self.skew_intensity = min(15.0, self.skew_intensity + 1.0)
        elif inventory < 40:
            self.skew_intensity = max(1.2, self.skew_intensity - 0.4)

        # 3. Size Tuning
        if adverse > 0.70 or fill_rate > 2.8:
            self.order_size_mult = max(0.15, self.order_size_mult - 0.2)
        elif adverse < 0.40 and fill_rate < 0.2:
            self.order_size_mult = min(2.0, self.order_size_mult + 0.08)

        self.last_adjustment_at = now
        logging.info("AUTOTUNE: Spread %.1f bps (Mkt %.1f) | Skew %.1f | SizeMult %.2f", 
                     self.target_spread_bps, market_spread_bps, self.skew_intensity, self.order_size_mult)

def compute_regime_and_stress(state: dict[str, Any]) -> tuple[str, float]:
    mid = float(state["mid_price"])
    fund = float(state.get("fundamental_price", mid))
    vol = float(state.get("volatility", 0.0))
    disloc = abs(fund / mid - 1.0)
    stress = max(disloc, vol * 1.3)
    
    if stress > 0.010: return "shock", stress
    if stress > 0.005: return "volatile", stress
    if stress < 0.002: return "calm", stress
    return "drift", stress

def microprice(state: dict[str, Any]) -> float:
    bb = float(state["best_bid"])
    ba = float(state["best_ask"])
    bids = state.get("order_book", {}).get("bids", [])
    asks = state.get("order_book", {}).get("asks", [])
    bv = sum(float(lv.get("quantity", 0)) for lv in bids[:5]) 
    av = sum(float(lv.get("quantity", 0)) for lv in asks[:5])
    tot = bv + av
    raw_micro = (ba * bv + bb * av) / tot if tot > 0 else float(state["mid_price"])
    # Weighted average with mid to smooth out outliers
    return raw_micro * 0.8 + float(state["mid_price"]) * 0.2

def clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(value, upper))

def api_user_path(path: str, user: str) -> str:
    separator = "&" if "?" in path else "?"
    return f"{path}{separator}user={urllib.parse.quote(user)}"

def call_api(api_url: str, path: str, method: str = "GET", data: dict[str, Any] | None = None, timeout: float = 2.0) -> dict[str, Any] | None:
    if "127.0.0.1" not in api_url and "localhost" not in api_url:
        raise RuntimeError(f"RAIDER_CORE SAFETY: Connection to non-local API blocked: {api_url}")
    
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

def cancel_open_orders(api_url: str, user: str) -> None:
    orders = call_api(api_url, api_user_path("/orders", user))
    for order in (orders or {}).get("orders", []):
        if order.get("status") in OPEN_STATUSES:
            call_api(api_url, api_user_path(f"/orders/{order['order_id']}", user), method="DELETE")

def submit_order(api_url: str, user: str, side: str, quantity: float, price: float, order_type: str = "limit", post_only: bool = True) -> bool:
    payload = {
        "side": side,
        "quantity": round(quantity, 4),
        "order_type": order_type,
        "user": user,
    }
    if order_type == "limit":
        payload["price"] = round(price, 4)
        payload["post_only"] = post_only
    
    result = call_api(api_url, "/order", "POST", payload)
    if not result: return False
    return result.get("status") in ACCEPTED_STATUSES or result.get("filled_quantity", 0) > 0

def trade_loop(args: argparse.Namespace) -> int:
    logging.info("Deploying Autonomous RaiderCore v7.0 (The Stressed Self-Tuner) as %s", args.user)
    call_api(args.url, "/accounts", "POST", {"user": args.user, "starting_cash": args.starting_cash})

    tracker = FillTracker(window=20)
    controller = ParametricController()
    last_inventory = 0.0
    last_bid = last_ask = 0.0
    last_quote_at = 0.0
    
    while True:
        state = call_api(args.url, api_user_path("/state", args.user))
        account = call_api(args.url, api_user_path("/account", args.user))
        if not state or not account:
            time.sleep(0.1)
            continue
        
        # ── Autonomous Tuning ───────────────────────────────────────────
        inventory = float(account.get("inventory", 0))
        inventory_changed = abs(inventory - last_inventory) > 1e-9
        if inventory_changed:
            tracker.record("buy" if inventory > last_inventory else "sell")
        last_inventory = inventory
        
        adverse = tracker.adverse_score()
        fill_rate = tracker.fill_rate()
        regime, stress = compute_regime_and_stress(state)
        
        mid = float(state["mid_price"])
        best_bid = float(state["best_bid"])
        best_ask = float(state["best_ask"])
        market_spread_bps = (best_ask - best_bid) / mid * 10000.0
        controller.tune(account, adverse, fill_rate, regime, market_spread_bps)
        fund = float(state.get("fundamental_price", mid))
        micro = microprice(state)
        
        # Fair competition: Blend signals rather than exploit EMAs
        if regime == "calm":
            fair = fund * 0.15 + micro * 0.45 + mid * 0.40
        elif regime == "shock":
            fair = fund * 0.70 + micro * 0.10 + mid * 0.20
        else:
            fair = fund * 0.50 + micro * 0.20 + mid * 0.30

        # ── Dynamic Sizing & Width ──────────────────────────────────────
        # Base width from self-tuned controller
        width = mid * (controller.target_spread_bps / 10000.0)
        
        # Stress-based protection: broaden if volatility/dislocation is high
        width = max(width, mid * stress * 0.6)
        
        inv_ratio = clamp(inventory / args.max_pos, -1.8, 1.8)
        skew = math.copysign(abs(inv_ratio) ** 1.25, inv_ratio) * width * controller.skew_intensity
        
        target_bid = round(min(fair - width/2 - skew, best_ask - args.tick), 4)
        target_ask = round(max(fair + width/2 - skew, best_bid + args.tick), 4)

        # ── Execution Guardrails ────────────────────────────────────────
        # Holding/Cancelling when edge is weak or risk is high
        if regime == "shock" and abs(inv_ratio) > 0.6:
            # In a shock, don't increase position if already significant
            if inv_ratio > 0: target_bid = 0
            if inv_ratio < 0: target_ask = 0
        
        # ── Execution ───────────────────────────────────────────────────
        now = time.time()
        
        # Immediate toxic quote protection
        toxic_quote = (last_bid > 0 and last_bid >= best_ask) or (last_ask > 0 and last_ask <= best_bid)
        if toxic_quote:
            cancel_open_orders(args.url, args.user)
            last_bid = last_ask = 0.0

        # Heartbeat log every 5 seconds
        if int(now) % 5 == 0 and now - last_quote_at > 0.5:
            logging.info("HEARTBEAT: Mid %.2f | Fair %.2f | Stress %.4f | Inv %.1f | Mode %s",
                         mid, fair, stress, inventory, regime)

        # "Fairness" Guardrail: Respect the market's quoting speed
        throttle_gap = {"shock": 2.5, "volatile": 1.2, "calm": 0.4}.get(regime, 0.7)
        
        move_threshold = width * 0.18
        price_moved = abs(target_bid - last_bid) > move_threshold or abs(target_ask - last_ask) > move_threshold

        if (price_moved or inventory_changed) and (now - last_quote_at >= throttle_gap):
            cancel_open_orders(args.url, args.user)
            
            # Size adjusted by controller mult
            order_size = args.order_size * controller.order_size_mult
            b_size = min(order_size, args.max_pos - inventory)
            s_size = min(order_size, args.max_pos + inventory)
            
            b_ok = b_size >= args.min_quantity and target_bid > 0 and submit_order(args.url, args.user, "buy", b_size, target_bid)
            s_ok = s_size >= args.min_quantity and target_ask > 0 and submit_order(args.url, args.user, "sell", s_size, target_ask)
            
            last_bid = target_bid if b_ok else 0.0
            last_ask = target_ask if s_ok else 0.0
            last_quote_at = now

        time.sleep(args.interval)

def main() -> int:
    parser = argparse.ArgumentParser(description="Autonomous RaiderCore v7.0")
    parser.add_argument("user", nargs="?", default="RaiderCore")
    parser.add_argument("--url", default="http://127.0.0.1:8780/api")
    parser.add_argument("--starting-cash", type=float, default=100000.0)
    parser.add_argument("--interval", type=float, default=0.1) 
    parser.add_argument("--tick", type=float, default=0.05)
    parser.add_argument("--order-size", type=float, default=40.0)
    parser.add_argument("--max-pos", type=float, default=600.0)
    parser.add_argument("--min-quantity", type=float, default=1.0)
    parser.add_argument("--max-orders", type=int, default=10000)
    args = parser.parse_args()
    
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        return trade_loop(args)
    except KeyboardInterrupt:
        return 0

if __name__ == "__main__":
    raise SystemExit(main())
