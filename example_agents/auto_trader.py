import urllib.request
import urllib.parse
import json
import time
import sys

USER = sys.argv[1] if len(sys.argv) > 1 else "AutoMaker_PRO"
URL = "http://127.0.0.1:8000/api"
STARTING_CASH = 10000
MAX_POS = 500 
ORDER_SIZE = 50
MIN_SPREAD_PCT = 0.0005 # 5bps (Covers maker fees easily)

def call_api(path, method="GET", data=None):
    req = urllib.request.Request(f"{URL}{path}", method=method)
    if data:
        req.add_header('Content-Type', 'application/json')
        body = json.dumps(data).encode('utf-8')
    else: body = None
    try:
        with urllib.request.urlopen(req, data=body) as f: return json.loads(f.read().decode('utf-8'))
    except: return None

def user_path(path):
    separator = "&" if "?" in path else "?"
    return f"{path}{separator}user={urllib.parse.quote(USER)}"

def trade_loop():
    print(f"PASSIVE MAKER START: {USER}...")
    call_api("/accounts", "POST", {"user": USER, "starting_cash": STARTING_CASH})
    
    last_bid = 0; last_ask = 0
    
    while True:
        state = call_api(user_path("/state"))
        account = call_api(user_path("/account"))
        if not state or not account: time.sleep(1); continue
            
        equity = account.get("equity", 1000)
        orders_count = account.get("orders", 0)
        inv = account.get("inventory", 0)
        
        if equity <= 0 :
            print(f"{USER} FINISHED. Equity: {equity}")
            sys.exit(0)
                
        mid = state.get("mid_price")
        fund = state.get("fundamental_price")
        vol = state.get("volatility", 0.001)
        
        # Fair price anchored to fundamental but reactive to mid
        fair = (fund * 0.8) + (mid * 0.2)
        
        # Dynamic spread based on volatility
        spread = mid * max(MIN_SPREAD_PCT, vol * 2.0)
        
        # Inventory Skew (Negative skew if long, Positive if short)
        skew = (inv / MAX_POS) * spread * 2.0
        
        target_bid = round(fair - (spread / 2) - skew, 4)
        target_ask = round(fair + (spread / 2) - skew, 4)
        
        # Safety: Never cross the current best bid/ask to ensure we stay MAKER
        best_bid = state.get("best_bid")
        best_ask = state.get("best_ask")
        if best_bid is None or best_ask is None:
            time.sleep(0.5)
            continue
        if target_bid >= best_ask: target_bid = best_bid
        if target_ask <= best_bid: target_ask = best_ask

        if abs(target_bid - last_bid) > 0.01 or abs(target_ask - last_ask) > 0.01:
            orders_resp = call_api(user_path("/orders"))
            if orders_resp and "orders" in orders_resp:
                for o in orders_resp["orders"]:
                    if o["status"] == "open": call_api(user_path(f"/orders/{o['order_id']}"), method="DELETE")
            
            # Use post_only=True to guarantee maker status and lower fees
            call_api("/order", "POST", {"side": "buy", "quantity": ORDER_SIZE, "order_type": "limit", "price": target_bid, "user": USER, "post_only": True})
            call_api("/order", "POST", {"side": "sell", "quantity": ORDER_SIZE, "order_type": "limit", "price": target_ask, "user": USER, "post_only": True})
            last_bid = target_bid; last_ask = target_ask
        
        time.sleep(0.5)

if __name__ == "__main__": trade_loop()
