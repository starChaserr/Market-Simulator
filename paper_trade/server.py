from __future__ import annotations

import argparse
import json
import logging
import signal
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

try:
    from .broker import PaperBroker, PaperConfig
    from .upstox_client import UpstoxClient, UpstoxError, access_token_from_env
except ImportError:  # pragma: no cover - allows python paper_trade/server.py
    from broker import PaperBroker, PaperConfig
    from upstox_client import UpstoxClient, UpstoxError, access_token_from_env


ROOT = Path(__file__).resolve().parent
SYMBOLS_FILE = ROOT / "symbols.json"
STATIC_ROOT = ROOT / "static"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a local paper broker backed by Upstox live quotes.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8780)
    parser.add_argument("--access-token", default=None, help="Upstox access token. Prefer UPSTOX_ACCESS_TOKEN or --token-file.")
    parser.add_argument("--token-file", default=None, help="File containing the Upstox access token.")
    parser.add_argument("--token-env", default="UPSTOX_ACCESS_TOKEN")
    parser.add_argument("--api-base", default="https://api.upstox.com/v2")
    parser.add_argument("--symbol", default="RELIANCE")
    parser.add_argument("--instrument-key", default=None)
    parser.add_argument("--tick", type=float, default=None)
    parser.add_argument("--starting-cash", type=float, default=10_000.0)
    parser.add_argument("--refresh", type=float, default=1.0, help="Seconds between Upstox quote polls.")
    parser.add_argument("--maker-fee-rate", type=float, default=0.0002)
    parser.add_argument("--taker-fee-rate", type=float, default=0.0005)
    parser.add_argument("--slippage-bps", type=float, default=1.0)
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args()


def load_symbols() -> list[dict[str, Any]]:
    with open(SYMBOLS_FILE, "r", encoding="utf-8") as handle:
        return json.load(handle)


def resolve_symbol(symbol: str, instrument_key: str | None, tick: float | None) -> dict[str, Any]:
    watchlist = load_symbols()
    if instrument_key:
        match = next((row for row in watchlist if row["instrument_key"] == instrument_key), {})
        return {
            "symbol": symbol or match.get("symbol") or instrument_key,
            "instrument_key": instrument_key,
            "tick": tick if tick is not None else float(match.get("tick", 0.05)),
        }
    wanted = symbol.upper()
    for row in watchlist:
        if row["symbol"].upper() == wanted:
            return {"symbol": row["symbol"], "instrument_key": row["instrument_key"], "tick": tick if tick is not None else float(row["tick"])}
    raise ValueError(f"unknown symbol {symbol}; add it to {SYMBOLS_FILE} or pass --instrument-key")


class QuotePoller(threading.Thread):
    def __init__(self, server: "PaperHTTPServer") -> None:
        super().__init__(daemon=True)
        self.server = server
        self.stop_event = threading.Event()

    def run(self) -> None:
        while not self.stop_event.is_set():
            started = time.time()
            try:
                quotes = self.server.upstox.full_market_quotes([self.server.broker.config.instrument_key])
                quote = quotes.get(self.server.broker.config.instrument_key) or next(iter(quotes.values()))
                self.server.broker.set_quote(quote)
                self.server.last_quote_error = None
            except Exception as exc:  # noqa: BLE001 - keep poller alive on broker/API glitches
                self.server.last_quote_error = str(exc)
                self.server.broker.record_event(f"quote refresh failed: {exc}", level="error")
                logging.warning("quote refresh failed: %s", exc)
            elapsed = time.time() - started
            self.stop_event.wait(max(0.1, self.server.refresh_interval - elapsed))

    def stop(self) -> None:
        self.stop_event.set()


class PaperHTTPServer(HTTPServer):
    def __init__(self, address: tuple[str, int], handler: type[BaseHTTPRequestHandler], broker: PaperBroker, upstox: UpstoxClient, refresh_interval: float) -> None:
        super().__init__(address, handler)
        self.broker = broker
        self.upstox = upstox
        self.refresh_interval = max(0.5, float(refresh_interval))
        self.last_quote_error: str | None = None


class Handler(BaseHTTPRequestHandler):
    server: PaperHTTPServer
    server_version = "UpstoxPaperTrade/1.0"

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self._common_headers("application/json")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        try:
            if path == "/api/health":
                quote = self.server.broker.quote
                self._json({"ok": True, "mode": "upstox_paper", "quote_loaded": quote is not None, "last_quote_error": self.server.last_quote_error})
                return
            if path == "/api/config":
                cfg = self.server.broker.config
                self._json(
                    {
                        "mode": "upstox_paper",
                        "symbol": cfg.symbol,
                        "instrument_key": cfg.instrument_key,
                        "chart_refresh_interval": self.server.refresh_interval,
                        "fees": {"maker_fee_rate": cfg.maker_fee_rate, "taker_fee_rate": cfg.taker_fee_rate},
                    }
                )
                return
            if path == "/api/chart-refresh":
                self._json(self._chart_refresh())
                return
            if path == "/api/state":
                self._state()
                return
            if path == "/api/stream":
                self._stream_once()
                return
            if path == "/api/orderbook":
                self._json(self.server.broker.snapshot()["order_book"])
                return
            if path == "/api/trades":
                self._json({"trades": self.server.broker.snapshot()["trades"]})
                return
            if path == "/api/agents":
                self._json({"agent_counts": {}, "agents": []})
                return
            if path == "/api/users":
                self._json({"users": self.server.broker.list_accounts()})
                return
            if path == "/api/accounts":
                self._json({"accounts": self.server.broker.list_accounts()})
                return
            if path == "/api/account":
                user = self._query_one(query, "user")
                if not user:
                    raise ValueError("query parameter user is required")
                self._json(self.server.broker.account(user))
                return
            if path == "/api/orders":
                self._json(
                    {
                        "orders": self.server.broker.list_orders(
                            owner=self._query_one(query, "user"),
                            status=self._query_one(query, "status"),
                            limit=int(self._query_one(query, "limit") or 100),
                        )
                    }
                )
                return
            if path.startswith("/api/orders/"):
                order_id = path.rsplit("/", 1)[-1]
                order = self.server.broker.orders.get(order_id)
                if order is None:
                    self._error(HTTPStatus.NOT_FOUND, "order not found")
                    return
                self._json(self.server.broker._order_response(order))
                return
            if path == "/api/symbols":
                self._json({"symbols": load_symbols()})
                return
            if path == "/favicon.ico":
                self.send_response(HTTPStatus.NO_CONTENT)
                self.end_headers()
                return
            if not path.startswith("/api/"):
                self._serve_static(path)
                return
            self._error(HTTPStatus.NOT_FOUND, f"No route for {path}")
        except ValueError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:  # noqa: BLE001
            logging.exception("request failed")
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            payload = self._read_json()
            if path in {"/api/buy", "/api/sell"}:
                payload = {**payload, "side": path.rsplit("/", 1)[-1]}
                self._json(self.server.broker.submit_order(payload))
                return
            if path == "/api/order":
                self._json(self.server.broker.submit_order(payload))
                return
            if path == "/api/cancel":
                order_id = str(payload.get("order_id", "")).strip()
                if not order_id:
                    raise ValueError("payload must include order_id")
                self._json(self.server.broker.cancel_order(order_id, owner=self._payload_user(payload)))
                return
            if path == "/api/accounts":
                user = self._payload_user(payload)
                self._json(self.server.broker.ensure_account(user, starting_cash=self._optional_float(payload.get("starting_cash"))))
                return
            if path == "/api/accounts/fund":
                user = self._payload_user(payload)
                amount = self._optional_float(payload.get("amount", payload.get("funds")))
                if amount is None:
                    raise ValueError("payload must include amount")
                self._json(self.server.broker.fund_account(user, amount))
                return
            if path == "/api/chart-refresh":
                interval = self._optional_float(payload.get("chart_refresh_interval", payload.get("interval")))
                if interval is None:
                    milliseconds = self._optional_float(payload.get("chart_refresh_ms", payload.get("milliseconds")))
                    interval = milliseconds / 1000.0 if milliseconds is not None else None
                if interval is None:
                    raise ValueError("payload must include chart_refresh_interval or chart_refresh_ms")
                self.server.refresh_interval = max(0.5, interval)
                self._json(self._chart_refresh())
                return
            if path == "/api/reset":
                self._json(self.server.broker.reset())
                return
            if path == "/api/simulation":
                self._json({"running": True, "mode": "upstox_paper"})
                return
            self._error(HTTPStatus.NOT_FOUND, f"No route for {path}")
        except ValueError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:  # noqa: BLE001
            logging.exception("request failed")
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        try:
            if path == "/api/users":
                self._json(self.server.broker.clear_users())
                return
            if path.startswith("/api/orders/"):
                order_id = path.rsplit("/", 1)[-1]
                self._json(self.server.broker.cancel_order(order_id, owner=self._query_one(query, "user")))
                return
            self._error(HTTPStatus.NOT_FOUND, f"No route for {path}")
        except ValueError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:  # noqa: BLE001
            logging.exception("request failed")
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    def _state(self) -> None:
        if self.server.broker.quote is None:
            self._error(HTTPStatus.SERVICE_UNAVAILABLE, self.server.last_quote_error or "no live quote loaded yet")
            return
        self._json(self.server.broker.snapshot())

    def _stream_once(self) -> None:
        if self.server.broker.quote is None:
            self._error(HTTPStatus.SERVICE_UNAVAILABLE, self.server.last_quote_error or "no live quote loaded yet")
            return
        payload = json.dumps(self.server.broker.snapshot()).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self._common_headers("text/event-stream")
        self.end_headers()
        self.wfile.write(b"event: snapshot\n")
        self.wfile.write(b"data: " + payload + b"\n\n")

    def _chart_refresh(self) -> dict[str, Any]:
        interval = self.server.refresh_interval
        return {"chart_refresh_interval": interval, "chart_refresh_ms": int(interval * 1000), "min_interval": 0.5, "max_interval": 60.0}

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self._common_headers("application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: HTTPStatus, message: str) -> None:
        self._json({"error": message, "status": int(status)}, status=status)

    def _common_headers(self, content_type: str) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,DELETE,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type,Authorization,X-API-User")

    def _serve_static(self, path: str) -> None:
        relative = "index.html" if path in {"", "/"} else path.lstrip("/")
        target = (STATIC_ROOT / relative).resolve()
        if STATIC_ROOT.resolve() not in target.parents and target != STATIC_ROOT.resolve():
            self._error(HTTPStatus.FORBIDDEN, "invalid static path")
            return
        if not target.exists() or not target.is_file():
            self._error(HTTPStatus.NOT_FOUND, "static file not found")
            return
        content_types = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".svg": "image/svg+xml",
        }
        body = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self._common_headers(content_types.get(target.suffix, "application/octet-stream"))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _query_one(self, query: dict[str, list[str]], key: str) -> str | None:
        values = query.get(key)
        return values[0] if values else None

    def _payload_user(self, payload: dict[str, Any]) -> str:
        for key in ("user", "user_name", "username", "api_user", "client", "client_id", "model", "owner", "name"):
            value = str(payload.get(key, "")).strip()
            if value:
                return value
        header_user = self.headers.get("X-API-User", "").strip()
        if header_user:
            return header_user
        raise ValueError("payload must include user, api_user, client_id, model, owner, or X-API-User")

    def _optional_float(self, value: Any) -> float | None:
        if value in (None, ""):
            return None
        return float(value)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        logging.info("%s - %s", self.address_string(), format % args)


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO), format="%(asctime)s %(levelname)s %(message)s")
    symbol = resolve_symbol(args.symbol, args.instrument_key, args.tick)
    token = access_token_from_env(args.access_token, args.token_file, args.token_env)
    client = UpstoxClient(token, base_url=args.api_base)
    broker = PaperBroker(
        PaperConfig(
            symbol=symbol["symbol"],
            instrument_key=symbol["instrument_key"],
            tick=float(symbol["tick"]),
            starting_cash=args.starting_cash,
            maker_fee_rate=args.maker_fee_rate,
            taker_fee_rate=args.taker_fee_rate,
            slippage_bps=args.slippage_bps,
        )
    )
    httpd = PaperHTTPServer((args.host, args.port), Handler, broker, client, args.refresh)
    poller = QuotePoller(httpd)

    def shutdown(_signum: int, _frame: object) -> None:
        poller.stop()
        httpd.shutdown()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    poller.start()
    logging.info("Upstox paper bridge running on http://%s:%s/api for %s (%s)", args.host, args.port, symbol["symbol"], symbol["instrument_key"])
    try:
        httpd.serve_forever()
    finally:
        poller.stop()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
