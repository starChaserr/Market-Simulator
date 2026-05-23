from __future__ import annotations
import argparse, json, logging, threading, time, random
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

class MockBroker:
    def __init__(self, symbol, starting_cash):
        self.symbol = symbol
        self.starting_cash = starting_cash
        self.accounts = {}
        self.price = 2500.0
        self.orders = {}
        self.order_id_counter = 0

    def get_state(self):
        # Random walk for price
        self.price += random.uniform(-2, 2)
        return {
            "symbol": self.symbol,
            "tick": int(time.time()),
            "last_price": round(self.price, 2),
            "mid_price": round(self.price, 2),
            "best_bid": round(self.price - 0.5, 2),
            "best_ask": round(self.price + 0.5, 2),
            "spread": 1.0,
            "order_book": {"bids": [], "asks": []},
            "trades": []
        }

    def ensure_account(self, user):
        if user not in self.accounts:
            self.accounts[user] = {
                "owner": user, "initial_cash": self.starting_cash, "cash": self.starting_cash,
                "equity": self.starting_cash, "profit_loss": 0, "orders": 0, "fills": 0,
                "inventory": 0, "max_drawdown": 0
            }
        return self.accounts[user]

    def list_orders(self, user=None):
        rows = list(self.orders.values())
        if user:
            rows = [row for row in rows if row.get("user") == user]
        return rows

class Handler(BaseHTTPRequestHandler):
    broker = None
    def _json(self, data):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def do_GET(self):
        path = urlparse(self.path).path
        query = parse_qs(urlparse(self.path).query)
        if path == "/api/health": self._json({"ok": True, "quote_loaded": True})
        elif path == "/api/state": self._json(self.broker.get_state())
        elif path == "/api/accounts": self._json({"accounts": list(self.broker.accounts.values())})
        elif path == "/api/account":
            user = query.get("user", [None])[0]
            self._json(self.broker.ensure_account(user))
        elif path == "/api/orders":
            user = query.get("user", [None])[0]
            self._json({"orders": self.broker.list_orders(user)})
        else: self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        data = json.loads(self.rfile.read(length).decode()) if length > 0 else {}
        if path == "/api/accounts":
            user = data.get("user")
            self._json(self.broker.ensure_account(user))
        elif path == "/api/order":
            user = data.get("user")
            acc = self.broker.ensure_account(user)
            acc["orders"] += 1
            self.broker.order_id_counter += 1
            order_id = f"mock_{self.broker.order_id_counter}"
            # Mock fill immediately
            qty = float(data.get("quantity", 0))
            side = data.get("side")
            price = self.broker.price
            cost = qty * price
            if side == "buy":
                acc["cash"] -= cost
                acc["inventory"] += qty
            else:
                acc["cash"] += cost
                acc["inventory"] -= qty
            acc["fills"] += 1
            acc["equity"] = acc["cash"] + acc["inventory"] * self.broker.price
            acc["profit_loss"] = acc["equity"] - acc["initial_cash"]
            order = {
                "order_id": order_id,
                "user": user,
                "side": side,
                "quantity": qty,
                "filled_quantity": qty,
                "price": price,
                "status": "filled",
            }
            self.broker.orders[order_id] = order
            self._json(order)
        else: self.send_error(404)

    def do_DELETE(self):
        path = urlparse(self.path).path
        if path.startswith("/api/orders/"):
            order_id = path.rsplit("/", 1)[-1]
            order = self.broker.orders.get(order_id)
            if not order:
                self.send_error(404)
                return
            if order.get("status") not in {"filled", "cancelled"}:
                order["status"] = "cancelled"
            self._json(order)
        else:
            self.send_error(404)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8780)
    parser.add_argument("--symbol", default="RELIANCE")
    parser.add_argument("--starting-cash", type=float, default=10000.0)
    args = parser.parse_args()
    Handler.broker = MockBroker(args.symbol, args.starting_cash)
    httpd = HTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Mock Paper Server running on port {args.port}")
    httpd.serve_forever()

if __name__ == "__main__": main()
