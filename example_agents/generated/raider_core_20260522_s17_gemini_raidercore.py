from __future__ import annotations

import argparse
import json
import logging
import math
import sys
import time
import urllib.parse
import urllib.request
import random
from collections import deque
from typing import Any

# Global constants
OPEN_STATUSES = {"open", "partially_filled", "pending_trigger"}
ACCEPTED_STATUSES = {"open", "partially_filled", "filled"}

class TradeAnalyzer:
    """Analyzes market sentiment from the trade tape."""
    def __init__(self, window: int = 100) -> None:
        self.trades: deque[tuple[str, float]] = deque(maxlen=window)

    def record(self, side: str, quantity: float) -> None:
        self.trades.append((side, quantity))

    def sentiment(self) -> float:
        """Returns -1.0 (bearish) to 1.0 (bullish) based on trade flow."""
        if not self.trades: return 0.0
        buys = sum(q for s, q in self.trades if s == "buy")
        sells = sum(q for s, q in self.trades if s == "sell")
        total = buys + sells
        return (buys - sells) / total if total > 0 else 0.0

class FillTracker:
    def __init__(self, window: int = 20) -> None:
        self._fills: deque[str] = deque(maxlen=window)

    def record(self, side: str) -> None:
        self._fills.append(side)

    def adverse_score(self) -> float:
        n = len(self._fills)
        if n < 5: return 0.0
        buys = self._fills.count("buy")
        return max(buys, n - buys) / n

class ProbabilisticBrain:
    """Unconventional controller using state-based 'moods' and dynamic targets."""
    def __init__(self):
        self.spread_bps = 8.0
        self.target_inv_ratio = 0.0  # Where the agent *wants* to be
        self.skew_intensity = 6.0
        self.last_adjustment_at = time.time()
        self.pnl_history = deque(maxlen=50)

    def think(self, account: dict[str, Any], adverse: float, regime: str, sentiment: float):
        now = time.time()
        if now - self.last_adjustment_at < 2.5: return

        equity = float(account.get("equity", 0))
        self.pnl_history.append(equity)

        # 1. Dynamic Inventory Preference (Thinking Different)
        # Instead of zero-bias, we lean into trends or hedge against sentiment
        if regime in ["trending_up", "news_shock"] and sentiment > 0.2:
            self.target_inv_ratio = clamp(sentiment * 0.4, 0.0, 0.5)
        elif regime in ["trending_down", "flash_crash"] and sentiment < -0.2:
            self.target_inv_ratio = clamp(sentiment * 0.4, -0.5, 0.0)
        else:
            self.target_inv_ratio = clamp(sentiment * 0.1, -0.1, 0.1)

        # 2. Mood-based Spread
        if adverse > 0.75:
            # "Panic" mode: widen significantly
            self.spread_bps = min(150.0, self.spread_bps * 1.3)
        elif adverse < 0.35:
            # "Greed" mode: tighten slowly to capture flow
            self.spread_bps = max(1.8, self.spread_bps * 0.92)

        self.last_adjustment_at = now
        logging.info("BRAIN: Spread %.1fbps | TargetInv %.2f | Sentiment %.2f", 
                     self.spread_bps, self.target_inv_ratio, sentiment)

def compute_regime(state: dict[str, Any]) -> str:
    vol = float(state.get("volatility", 0.0))
    if vol > 0.015: return "shock"
    if vol > 0.008: return "volatile"
    
    mid = float(state["mid_price"])
    fund = float(state.get("fundamental_price", mid))
    disloc = (fund / mid) - 1.0
    if disloc > 0.005: return "trending_up"
    if disloc < -0.005: return "trending_down"
    return "calm"

def clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(value, upper))

def api_user_path(path: str, user: str) -> str:
    separator = "&" if "?" in path else "?"
    return f"{path}{separator}user={urllib.parse.quote(user)}"

def call_api(api_url: str, path: str, method: str = "GET", data: dict[str, Any] | None = None) -> dict[str, Any] | None:
    body = None
    req = urllib.request.Request(f"{api_url.rstrip('/')}{path}", method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
        body = json.dumps(data).encode("utf-8")
    try:
        with urllib.request.urlopen(req, data=body, timeout=3.0) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception:
        return None

def cancel_open_orders(api_url: str, user: str) -> None:
    orders = call_api(api_url, api_user_path("/orders", user))
    for order in (orders or {}).get("orders", []):
        if order.get("status") in OPEN_STATUSES:
            call_api(api_url, api_user_path(f"/orders/{order['order_id']}", user), method="DELETE")

def submit_order(api_url: str, user: str, side: str, quantity: float, price: float) -> bool:
    payload = {
        "side": side,
        "quantity": round(quantity, 4),
        "order_type": "limit",
        "price": round(price, 4),
        "user": user,
        "post_only": True
    }
    result = call_api(api_url, "/order", "POST", payload)
    return bool(result and (result.get("status") in ACCEPTED_STATUSES or result.get("filled_quantity", 0) > 0))

def trade_loop(args: argparse.Namespace) -> int:
    logging.info("Deploying Autonomous RaiderCore v7.0 (The Contrarian) as %s", args.user)
    call_api(args.url, "/accounts", "POST", {"user": args.user, "starting_cash": args.starting_cash})

    fills = FillTracker()
    analyzer = TradeAnalyzer()
    brain = ProbabilisticBrain()
    
    last_inventory = 0.0
    last_bid = last_ask = 0.0
    last_quote_at = 0.0
    
    while True:
        state = call_api(args.url, api_user_path("/state", args.user))
        account = call_api(args.url, api_user_path("/account", args.user))
        if not state or not account:
            time.sleep(0.1)
            continue
        
        # 1. Update Market Analytics
        inventory = float(account.get("inventory", 0))
        if abs(inventory - last_inventory) > 1e-9:
            fills.record("buy" if inventory > last_inventory else "sell")
        last_inventory = inventory
        
        for t in state.get("trades", []):
            analyzer.record(t["side"], float(t["quantity"]))
            
        regime = compute_regime(state)
        sentiment = analyzer.sentiment()
        adverse = fills.adverse_score()
        brain.think(account, adverse, regime, sentiment)

        # 2. Fair Price Calculation (Bias by Sentiment - scaled down for safety)
        mid = float(state["mid_price"])
        fair = mid * (1.0 + sentiment * 0.0005) 
        
        # 3. Dynamic Quote Sizing & Width
        width = mid * (brain.spread_bps / 10000.0)
        
        # Exponential Inventory Error Relative to Target
        inv_error = (inventory / args.max_pos) - brain.target_inv_ratio
        skew = math.copysign(abs(inv_error) ** 1.6, inv_error) * width * brain.skew_intensity
        
        target_bid = fair - width/2 - skew
        target_ask = fair + width/2 - skew

        # 4. Stochastic Jitter
        jitter = (random.random() - 0.5) * args.tick * 2.0
        target_bid += jitter
        target_ask += jitter
        
        # 5. Quote Bounding (Post-Only Safety)
        best_bid = float(state["best_bid"])
        best_ask = float(state["best_ask"])
        # Ensure we don't cross the spread to avoid post_only rejections
        target_bid = min(target_bid, best_ask - args.tick)
        target_ask = max(target_ask, best_bid + args.tick)

        # 6. Execution & Duty-Cycle Throttling
        now = time.time()
        duty_cycle_gap = {"shock": 4.0, "volatile": 1.5, "calm": 0.4}.get(regime, 0.8)
        
        # Immediate Cancellation: toxic or significantly off-market quotes
        price_moved = (last_bid > 0 and abs(target_bid - last_bid) > width * 0.15) or \
                      (last_ask > 0 and abs(target_ask - last_ask) > width * 0.15)
        toxic = (last_bid > 0 and last_bid >= best_ask) or \
                (last_ask > 0 and last_ask <= best_bid)

        if toxic or price_moved:
            cancel_open_orders(args.url, args.user)
            last_bid = last_ask = 0.0

        if (now - last_quote_at >= duty_cycle_gap):
            # Reduce size in 'shock' or 'volatile' regimes
            base_size = args.order_size
            if regime == "shock": base_size *= 0.25
            elif regime == "volatile": base_size *= 0.5
            
            # Reduce size near absolute limits
            limit_mult = clamp((1.0 - abs(inventory / args.max_pos)) * 2.0, 0.1, 1.0)
            order_size = base_size * limit_mult
            
            b_size = min(order_size, args.max_pos - inventory)
            s_size = min(order_size, args.max_pos + inventory)
            
            b_ok = b_size >= args.min_quantity and submit_order(args.url, args.user, "buy", b_size, target_bid)
            s_ok = s_size >= args.min_quantity and submit_order(args.url, args.user, "sell", s_size, target_ask)
            
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
    parser.add_argument("--model-path", help="Path to model (ignored in v7.0)")
    args = parser.parse_args()
    
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        return trade_loop(args)
    except KeyboardInterrupt:
        return 0

if __name__ == "__main__":
    raise SystemExit(main())
