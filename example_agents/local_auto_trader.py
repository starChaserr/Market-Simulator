import urllib.request
import urllib.parse
import json
import time
import sys
import os
from pathlib import Path

try: from llama_cpp import Llama
except: Llama = None

USER = sys.argv[1] if len(sys.argv) > 1 else "LlamaTrader"
ROOT_DIR = Path(__file__).resolve().parent.parent
MODEL_PATH = str(ROOT_DIR / "llama-3-8b-instruct.gguf")
API_URL = "http://127.0.0.1:8000/api"
STARTING_CASH = 10000
MAX_POS = 400
ORDER_SIZE = 60 # Increased again to outpace Gemini

def call_api(path, method="GET", data=None):
    req = urllib.request.Request(f"{API_URL}{path}", method=method)
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

def get_llm_decision(llm, market_state, account_state):
    mid = market_state.get('mid_price', 0)
    fund = market_state.get('fundamental_price', 0)
    mkt_spread = market_state.get('spread', 0.05)
    vol = market_state.get('volatility', 0.001)
    inv = account_state.get('inventory', 0)
    equity = account_state.get('equity', 10000)
    
    prompt = f"""<|begin_of_text|><|start_header_id|>system<|end_header_id|>
ROUND 4: PREDATORY MODE.
Your rival (Gemini) is winning. You must dominate fills.
Strategy: ADAPTIVE SPREAD CAPTURE.
Market Volatility: {vol*1000:.2f}. Market Spread: {mkt_spread:.4f}.
Rules:
- If Equity < 10100: Tighten offsets to get filled FAST (Predatory).
- If Inventory > 100: Lower Ask Offset significantly to unload.
- If Inventory < -100: Lower Bid Offset significantly to cover.
Respond ONLY with JSON: {{"bid_offset": float, "ask_offset": float, "reason": "str"}}<|eot_id|><|start_header_id|>user<|end_header_id|>
MARKET: Mid {mid}, Fund {fund}
ACCOUNT: Inv {inv}, Equity {equity}
Decision for Round 4?<|eot_id|><|start_header_id|>assistant<|end_header_id|>"""
    
    out = llm(prompt, max_tokens=100, stop=["<|eot_id|>"], echo=False)
    text = out['choices'][0]['text'].strip()
    try:
        start = text.find('{'); end = text.rfind('}') + 1
        return json.loads(text[start:end])
    except: return {"bid_offset": 0.04, "ask_offset": 0.04}

def main():
    if not Llama or not os.path.exists(MODEL_PATH): sys.exit(1)
    llm = Llama(model_path=MODEL_PATH, n_ctx=2048, n_threads=4, verbose=False)
    call_api("/accounts", "POST", {"user": USER, "starting_cash": STARTING_CASH})
    
    last_bid = 0; last_ask = 0
    
    while True:
        state = call_api(user_path("/state"))
        account = call_api(user_path("/account"))
        if not state or not account: time.sleep(1); continue
        if account.get("orders", 0) >= 1000: sys.exit(0)

        fund = state.get('fundamental_price', 0); inv = account.get('inventory', 0)
        dec = get_llm_decision(llm, state, account)
        
        target_bid = round(fund - abs(dec.get("bid_offset", 0.04)), 2)
        target_ask = round(fund + abs(dec.get("ask_offset", 0.04)), 2)
        
        best_bid = state.get("best_bid")
        best_ask = state.get("best_ask")
        if best_bid is None or best_ask is None:
            time.sleep(1)
            continue
        if target_bid >= best_ask: target_bid = best_bid
        if target_ask <= best_bid: target_ask = best_ask

        if abs(target_bid - last_bid) > 0.02 or abs(target_ask - last_ask) > 0.02 or account.get("orders") == 0:
            orders_resp = call_api(user_path("/orders"))
            if orders_resp and "orders" in orders_resp:
                for o in orders_resp["orders"]:
                    if o["status"] == "open": call_api(user_path(f"/orders/{o['order_id']}"), method="DELETE")
            
            call_api("/order", "POST", {"side": "buy", "quantity": ORDER_SIZE, "order_type": "limit", "price": target_bid, "user": USER})
            call_api("/order", "POST", {"side": "sell", "quantity": ORDER_SIZE, "order_type": "limit", "price": target_ask, "user": USER})
            last_bid = target_bid; last_ask = target_ask
        
        time.sleep(1)

if __name__ == "__main__": main()
