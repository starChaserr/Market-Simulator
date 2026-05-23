from __future__ import annotations

import argparse
import json
import logging
import math
import sys
import time
import urllib.parse
import urllib.request
from collections import deque
from typing import Any

# Constants
OPEN_STATUSES = {"open", "partially_filled", "pending_trigger"}
ACCEPTED_STATUSES = {"open", "partially_filled", "filled"}

# VolGuard thresholds
VOL_GUARD_THRESHOLD = 0.015
VOL_GUARD_WINDOW = 10

# API Helper
def call_api(url: str, path: str, method: str = "GET", data: dict[str, Any] | None = None) -> dict[str, Any] | None:
    req = urllib.request.Request(f"{url.rstrip('/')}{path}", method=method)
    body = None
    if data:
        req.add_header("Content-Type", "application/json")
        body = json.dumps(data).encode("utf-8")
    try:
        with urllib.request.urlopen(req, data=body, timeout=2.0) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        logging.debug(f"API Error {method} {path}: {e}")
        return None

def upath(path: str, user: str) -> str:
    sep = "&" if "?" in path else "?"
    return f"{path}{sep}user={urllib.parse.quote(user)}"

# Logic Components
class FillTracker:
    def __init__(self, window: int = 20) -> None:
        self._fills: deque[str] = deque(maxlen=window)
        self.last_inventory = 0.0

    def update(self, current_inventory: float) -> None:
        if abs(current_inventory - self.last_inventory) > 1e-9:
            side = "buy" if current_inventory > self.last_inventory else "sell"
            self._fills.append(side)
            self.last_inventory = current_inventory

    def adverse_score(self) -> float:
        n = len(self._fills)
        if n < 5: return 0.0
        buys = self._fills.count("buy")
        return max(buys, n - buys) / n

class RegimeDetector:
    def __init__(self, window: int = 30) -> None:
        self._mids: deque[float] = deque(maxlen=window)
        self._vols: deque[float] = deque(maxlen=window)
        self.mode = "MAKER"

    def update(self, state: dict[str, Any], adverse_score: float) -> str:
        mid = float(state["mid_price"])
        fund = float(state.get("fundamental_price", mid))
        vol = float(state.get("volatility", 0.0))
        
        self._mids.append(mid)
        self._vols.append(vol)
        
        if len(self._mids) < 5: return "MAKER"

        # Calculate metrics
        dislocation = abs(fund / mid - 1.0)
        recent_vol = sum(self._vols) / len(self._vols)
        
        # Trend detection
        avg_mid = sum(self._mids) / len(self._mids)
        drift = abs(mid / avg_mid - 1.0)

        # Decision Tree
        if recent_vol > VOL_GUARD_THRESHOLD or dislocation > 0.015 or adverse_score > 0.8:
            self.mode = "SURVIVAL"
        elif drift > 0.003 or dislocation > 0.006:
            self.mode = "PREDATOR"
        else:
            self.mode = "MAKER"
            
        return self.mode

class OmniTrader:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.tracker = FillTracker()
        self.detector = RegimeDetector()
        self.last_bid = 0.0
        self.last_ask = 0.0
        self.last_quote_at = 0.0
        self.size_mult = 1.0

    def calculate_params(self, state: dict[str, Any], account: dict[str, Any], mode: str):
        mid = float(state["mid_price"])
        fund = float(state.get("fundamental_price", mid))
        inv = float(account.get("inventory", 0))
        vol = float(state.get("volatility", 0.001))
        
        # 1. Fair Price Calculation
        if mode == "MAKER":
            fair = (fund * 0.7) + (mid * 0.3)
            base_spread = mid * max(0.0005, vol * 1.5)
            post_only = True
        elif mode == "PREDATOR":
            # Lean towards mid to capture trend
            fair = (mid * 0.8) + (fund * 0.2)
            base_spread = mid * max(0.0003, vol * 1.0)
            post_only = False # Allow taking
        else: # SURVIVAL
            fair = fund
            base_spread = mid * max(0.02, vol * 5.0)
            post_only = True

        # 2. Inventory Skew
        inv_ratio = inv / self.args.max_pos
        skew = inv_ratio * base_spread * (2.0 if mode != "SURVIVAL" else 5.0)
        
        bid = fair - (base_spread / 2) - skew
        ask = fair + (base_spread / 2) - skew
        
        # 3. Size Calculation
        self.size_mult = 0.5 if mode == "SURVIVAL" else 1.0
        if mode == "PREDATOR" and abs(inv_ratio) < 0.2:
            self.size_mult = 1.5 # Be aggressive when position is low

        return round(bid, 4), round(ask, 4), post_only

    def run(self) -> None:
        logging.info(f"OmniTrader starting as {self.args.user}...")
        call_api(self.args.url, "/accounts", "POST", {"user": self.args.user, "starting_cash": self.args.starting_cash})

        while True:
            state = call_api(self.args.url, upath("/state", self.args.user))
            account = call_api(self.args.url, upath("/account", self.args.user))
            if not state or not account:
                time.sleep(0.1)
                continue

            # Update Brain
            self.tracker.update(float(account.get("inventory", 0)))
            mode = self.detector.update(state, self.tracker.adverse_score())
            
            # Calculate Heartbeat
            bid, ask, post_only = self.calculate_params(state, account, mode)
            
            # Execution
            now = time.time()
            if abs(bid - self.last_bid) > 0.01 or abs(ask - self.last_ask) > 0.01 or (now - self.last_quote_at > 5.0):
                # Cancel old
                orders_resp = call_api(self.args.url, upath("/orders", self.args.user))
                for o in (orders_resp or {}).get("orders", []):
                    if o["status"] in OPEN_STATUSES:
                        call_api(self.args.url, upath(f"/orders/{o['order_id']}", self.args.user), "DELETE")
                
                # Place new
                size = self.args.order_size * self.size_mult
                inv = float(account.get("inventory", 0))
                
                if inv < self.args.max_pos:
                    call_api(self.args.url, "/order", "POST", {
                        "side": "buy", "quantity": size, "order_type": "limit", 
                        "price": bid, "user": self.args.user, "post_only": post_only
                    })
                if inv > -self.args.max_pos:
                    call_api(self.args.url, "/order", "POST", {
                        "side": "sell", "quantity": size, "order_type": "limit", 
                        "price": ask, "user": self.args.user, "post_only": post_only
                    })
                
                self.last_bid, self.last_ask = bid, ask
                self.last_quote_at = now
                logging.info(f"[{mode}] Bid: {bid:.2f} | Ask: {ask:.2f} | Inv: {inv:.1f}")

            time.sleep(self.args.interval)

def main():
    parser = argparse.ArgumentParser(description="OmniTrader Dynamic Meta-Agent")
    parser.add_argument("user", nargs="?", default="OmniTrader")
    parser.add_argument("--url", default="http://127.0.0.1:8000/api")
    parser.add_argument("--starting-cash", type=float, default=10000.0)
    parser.add_argument("--order-size", type=float, default=40.0)
    parser.add_argument("--max-pos", type=float, default=600.0)
    parser.add_argument("--interval", type=float, default=0.2)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        OmniTrader(args).run()
    except KeyboardInterrupt:
        sys.exit(0)

if __name__ == "__main__":
    main()
