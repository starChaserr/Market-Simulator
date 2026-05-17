from __future__ import annotations

import argparse
import json
import mimetypes
import signal
import sys
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from market_sim import MarketSimulator
from market_sim.currency import currency_preferences


ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
#Entry point

class MarketHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128
    allow_reuse_address = True


class MarketRequestHandler(BaseHTTPRequestHandler):
    simulator: MarketSimulator

    server_version = "MarketSimulator/1.0"

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self._send_common_headers("application/json")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        try:
            if path == "/api/health":
                self._send_json({"ok": True})
                return
            if path == "/api/config":
                self._send_json({"config": self.simulator.config, "scenario": self.simulator.config.get("scenario"), "seed": self.simulator.seed})
                return
            if path == "/api/currency":
                self._send_json(
                    currency_preferences(
                        locale=self._query_one(query, "locale") or self.headers.get("X-Client-Locale"),
                        timezone=self._query_one(query, "timezone") or self.headers.get("X-Client-Timezone"),
                        explicit_currency=self._query_one(query, "currency") or self.headers.get("X-Currency"),
                        accept_language=self.headers.get("Accept-Language"),
                    )
                )
                return
            if path == "/api/state":
                self._send_json(self.simulator.snapshot())
                return
            if path == "/api/stream":
                self._send_stream()
                return
            if path == "/api/orderbook":
                self._send_json(self.simulator.snapshot()["order_book"])
                return
            if path == "/api/trades":
                self._send_json({"trades": self.simulator.snapshot()["trades"]})
                return
            if path == "/api/agents":
                snapshot = self.simulator.snapshot()
                self._send_json({"agent_counts": snapshot["agent_counts"], "agents": snapshot["agents"]})
                return
            if path == "/api/users":
                self._send_json({"users": self.simulator.snapshot()["api_users"]})
                return
            if path == "/api/accounts":
                self._send_json({"accounts": self.simulator.list_accounts()})
                return
            if path == "/api/account":
                user = self._query_one(query, "user")
                if not user:
                    raise ValueError("query parameter user is required")
                self._send_json(self.simulator.account(user))
                return
            if path == "/api/orders":
                self._send_json(
                    {
                        "orders": self.simulator.list_orders(
                            user_name=self._query_one(query, "user"),
                            status=self._query_one(query, "status"),
                            include_internal=self._query_bool(query, "include_internal"),
                            limit=int(self._query_one(query, "limit") or 100),
                        )
                    }
                )
                return
            if path.startswith("/api/orders/"):
                order_id = path.rsplit("/", 1)[-1]
                orders = self.simulator.list_orders(include_internal=True, limit=1000)
                match = next((order for order in orders if order["id"] == order_id), None)
                if match is None:
                    self._send_error(HTTPStatus.NOT_FOUND, "order not found")
                    return
                self._send_json(match)
                return
            if path == "/openapi.json":
                self._serve_static("/openapi.json")
                return
            self._serve_static(path)
        except ValueError as exc:
            self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:
            self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            payload = self._read_json()
            if path == "/api/buy":
                result = self.simulator.buy(
                    self._quantity(payload),
                    price=self._optional_float(payload.get("price")),
                    order_type=str(payload.get("order_type", payload.get("type", "market"))),
                    user_name=self._request_user(payload),
                    time_in_force=str(payload.get("time_in_force", payload.get("tif", "gtc"))),
                    post_only=bool(payload.get("post_only", False)),
                    stop_price=self._optional_float(payload.get("stop_price")),
                )
                self._send_json(result)
                return
            if path == "/api/sell":
                result = self.simulator.sell(
                    self._quantity(payload),
                    price=self._optional_float(payload.get("price")),
                    order_type=str(payload.get("order_type", payload.get("type", "market"))),
                    user_name=self._request_user(payload),
                    time_in_force=str(payload.get("time_in_force", payload.get("tif", "gtc"))),
                    post_only=bool(payload.get("post_only", False)),
                    stop_price=self._optional_float(payload.get("stop_price")),
                )
                self._send_json(result)
                return
            if path == "/api/order":
                header_user = self._request_user(payload)
                if header_user and not any(key in payload for key in ("user", "user_name", "username", "api_user", "client", "client_id", "model", "owner", "name")):
                    payload = {**payload, "user": header_user}
                self._send_json(self.simulator.submit_order(payload))
                return
            if path == "/api/accounts/fund":
                user = self._request_user(payload)
                if not user:
                    raise ValueError("payload must include user, api_user, client_id, model, or X-API-User")
                amount = self._optional_float(payload.get("amount", payload.get("funds")))
                if amount is None:
                    raise ValueError("payload must include amount")
                self._send_json(self.simulator.fund_account(user, amount))
                return
            if path == "/api/accounts":
                user = self._request_user(payload)
                if not user:
                    raise ValueError("payload must include user, api_user, client_id, model, or X-API-User")
                self._send_json(self.simulator.ensure_account(user, starting_cash=self._optional_float(payload.get("starting_cash"))))
                return
            if path == "/api/cancel":
                order_id = str(payload.get("order_id", "")).strip()
                if not order_id:
                    raise ValueError("payload must include order_id")
                self._send_json(self.simulator.cancel_order(order_id, user_name=self._request_user(payload)))
                return
            if path == "/api/simulation":
                if "running" not in payload:
                    raise ValueError("payload must include a running boolean")
                self._send_json(self.simulator.set_running(bool(payload["running"])))
                return
            if path == "/api/reset":
                self._send_json(self.simulator.reset())
                return
            self._send_error(HTTPStatus.NOT_FOUND, f"No route for {path}")
        except ValueError as exc:
            self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:
            self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        try:
            if path == "/api/users":
                self._send_json(self.simulator.clear_api_users())
                return
            if path.startswith("/api/orders/"):
                order_id = path.rsplit("/", 1)[-1]
                self._send_json(self.simulator.cancel_order(order_id, user_name=self._query_one(query, "user")))
                return
            self._send_error(HTTPStatus.NOT_FOUND, f"No route for {path}")
        except ValueError as exc:
            self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:
            self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    def do_HEAD(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            path = "/index.html"
        candidate = (STATIC_DIR / path.lstrip("/")).resolve()
        if candidate.exists() and candidate.is_file():
            self.send_response(HTTPStatus.OK)
            self._send_common_headers(mimetypes.guess_type(str(candidate))[0] or "application/octet-stream")
            self.end_headers()
            return
        self.send_response(HTTPStatus.NOT_FOUND)
        self._send_common_headers("application/json")
        self.end_headers()

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def _serve_static(self, path: str) -> None:
        if path == "/":
            path = "/index.html"
        relative = path.lstrip("/")
        candidate = (STATIC_DIR / relative).resolve()
        if not str(candidate).startswith(str(STATIC_DIR.resolve())):
            self._send_error(HTTPStatus.FORBIDDEN, "Forbidden")
            return
        if not candidate.exists() or not candidate.is_file():
            self._send_error(HTTPStatus.NOT_FOUND, "Not found")
            return
        content = candidate.read_bytes()
        content_type = mimetypes.guess_type(str(candidate))[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self._send_common_headers(content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            value = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON: {exc}") from exc
        if not isinstance(value, dict):
            raise ValueError("JSON payload must be an object")
        return value

    def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        self.send_response(status)
        self._send_common_headers("application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_stream(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        last_seq = 0
        while True:
            snapshot = self.simulator.snapshot()
            events = [event for event in snapshot.get("events", []) if event.get("seq", 0) > last_seq]
            if events:
                last_seq = max(event.get("seq", last_seq) for event in events)
            payload = json.dumps(self._stream_payload(snapshot, events[-12:]), separators=(",", ":"), ensure_ascii=True)
            message = f"event: state\ndata: {payload}\n\n".encode("utf-8")
            try:
                self.wfile.write(message)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                break
            time.sleep(0.5)

    @staticmethod
    def _stream_payload(snapshot: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "symbol": snapshot.get("symbol"),
            "tick": snapshot.get("tick"),
            "last_price": snapshot.get("last_price"),
            "mid_price": snapshot.get("mid_price"),
            "best_bid": snapshot.get("best_bid"),
            "best_ask": snapshot.get("best_ask"),
            "spread": snapshot.get("spread"),
            "total_volume": snapshot.get("total_volume"),
            "volatility": snapshot.get("volatility"),
            "order_book": {
                "bids": snapshot.get("order_book", {}).get("bids", [])[:5],
                "asks": snapshot.get("order_book", {}).get("asks", [])[:5],
            },
            "latest_trade": snapshot.get("trades", [None])[0] if snapshot.get("trades") else None,
            "api_users": snapshot.get("api_users", [])[:25],
            "events": [MarketRequestHandler._stream_event(event) for event in events],
            "running": snapshot.get("running"),
            "scenario": snapshot.get("scenario"),
            "seed": snapshot.get("seed"),
        }

    @staticmethod
    def _stream_event(event: dict[str, Any]) -> dict[str, Any]:
        payload = event.get("payload", {})
        compact_payload = payload
        if isinstance(payload, dict):
            compact_payload = {
                key: payload.get(key)
                for key in (
                    "id",
                    "order_id",
                    "status",
                    "side",
                    "order_type",
                    "price",
                    "quantity",
                    "filled_quantity",
                    "remaining_quantity",
                    "buyer",
                    "seller",
                    "aggressor_side",
                    "owner",
                    "user",
                    "name",
                    "tick",
                )
                if key in payload
            }
        return {
            "seq": event.get("seq"),
            "type": event.get("type"),
            "timestamp": event.get("timestamp"),
            "payload": compact_payload,
        }

    def _send_error(self, status: HTTPStatus, message: str) -> None:
        self._send_json({"error": message, "status": status.value}, status=status)

    def _send_common_headers(self, content_type: str) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-API-User, X-Client-Name, X-Model-Name, X-Client-Locale, X-Client-Timezone, X-Currency")

    @staticmethod
    def _quantity(payload: dict[str, Any]) -> float:
        if "quantity" not in payload:
            raise ValueError("payload must include quantity")
        return float(payload["quantity"])

    @staticmethod
    def _optional_float(value: Any) -> float | None:
        if value in (None, ""):
            return None
        return float(value)

    def _request_user(self, payload: dict[str, Any]) -> str | None:
        for key in ("user", "user_name", "username", "api_user", "client", "client_id", "model", "owner", "name"):
            value = payload.get(key)
            if value not in (None, ""):
                return str(value)
        for header in ("X-API-User", "X-Client-Name", "X-Model-Name"):
            value = self.headers.get(header)
            if value:
                return value
        return None

    @staticmethod
    def _query_one(query: dict[str, list[str]], key: str) -> str | None:
        values = query.get(key)
        if not values:
            return None
        return values[0]

    @staticmethod
    def _query_bool(query: dict[str, list[str]], key: str) -> bool:
        value = MarketRequestHandler._query_one(query, key)
        return str(value).lower() in {"1", "true", "yes", "on"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the market simulator API and dashboard.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--symbol", default="SIM")
    parser.add_argument("--start-price", type=float, default=100.0)
    parser.add_argument("--tick-interval", type=float, default=0.25)
    parser.add_argument("--scenario", default="default")
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--config", default=None, help="Optional JSON config override file.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    simulator = MarketSimulator(
        symbol=args.symbol,
        start_price=args.start_price,
        tick_interval=max(0.05, args.tick_interval),
        scenario=args.scenario,
        seed=args.seed,
        config_path=args.config,
    )
    MarketRequestHandler.simulator = simulator
    server = MarketHTTPServer((args.host, args.port), MarketRequestHandler)

    def shutdown(_signum: int, _frame: Any) -> None:
        simulator.stop()
        server.shutdown()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    print(f"Market simulator running at http://{args.host}:{args.port}")
    print("API endpoints: GET /api/state, GET /api/stream, POST /api/order, GET /openapi.json")
    try:
        server.serve_forever()
    finally:
        simulator.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
