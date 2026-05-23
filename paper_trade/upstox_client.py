from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any


DEFAULT_BASE_URL = "https://api.upstox.com/v2"
DEFAULT_USER_AGENT = "MarketSimulationPaperTrade/1.0"


class UpstoxError(RuntimeError):
    """Raised when Upstox returns an error response or malformed data."""


@dataclass(frozen=True)
class QuoteLevel:
    price: float
    quantity: float
    orders: int = 0


@dataclass(frozen=True)
class MarketQuote:
    instrument_key: str
    symbol: str
    last_price: float
    best_bid: float | None
    best_ask: float | None
    bid_levels: list[QuoteLevel] = field(default_factory=list)
    ask_levels: list[QuoteLevel] = field(default_factory=list)
    open_price: float | None = None
    high_price: float | None = None
    low_price: float | None = None
    close_price: float | None = None
    average_price: float | None = None
    total_volume: float = 0.0
    timestamp: float = field(default_factory=time.time)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def mid_price(self) -> float:
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2.0
        return self.last_price

    @property
    def spread(self) -> float:
        if self.best_bid is None or self.best_ask is None:
            return 0.0
        return max(0.0, self.best_ask - self.best_bid)


def access_token_from_env(token: str | None = None, token_file: str | None = None, env_var: str = "UPSTOX_ACCESS_TOKEN") -> str:
    if token:
        return token.strip()
    if token_file:
        with open(token_file, "r", encoding="utf-8") as handle:
            loaded = handle.read().strip()
        if loaded:
            return loaded
    loaded = os.environ.get(env_var, "").strip()
    if loaded:
        return loaded
    raise UpstoxError(f"missing Upstox access token; pass --access-token, --token-file, or set {env_var}")


def _float(value: Any, default: float | None = 0.0) -> float | None:
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _levels(raw_levels: Any) -> list[QuoteLevel]:
    levels: list[QuoteLevel] = []
    if not isinstance(raw_levels, list):
        return levels
    for level in raw_levels:
        if not isinstance(level, dict):
            continue
        price = _float(level.get("price"), None)
        quantity = _float(level.get("quantity", level.get("qty")), 0.0)
        if price is None or price <= 0 or quantity is None or quantity <= 0:
            continue
        levels.append(QuoteLevel(price=price, quantity=quantity, orders=_int(level.get("orders", level.get("order_count")))))
    return levels


def _ohlc_value(payload: dict[str, Any], key: str) -> float | None:
    ohlc = payload.get("ohlc")
    if isinstance(ohlc, dict):
        return _float(ohlc.get(key), None)
    return _float(payload.get(key), None)


def parse_full_market_quote(instrument_key: str, payload: dict[str, Any]) -> MarketQuote:
    if not isinstance(payload, dict):
        raise UpstoxError("quote payload must be a JSON object")

    depth = payload.get("depth") if isinstance(payload.get("depth"), dict) else {}
    bid_levels = _levels(depth.get("buy"))
    ask_levels = _levels(depth.get("sell"))
    last_price = _float(payload.get("last_price", payload.get("ltp")), None)
    if last_price is None or last_price <= 0:
        raise UpstoxError(f"quote for {instrument_key} has no usable last price")

    exchange_timestamp = _float(payload.get("last_trade_time", payload.get("timestamp")), None)
    if exchange_timestamp and exchange_timestamp > 10_000_000_000:
        exchange_timestamp /= 1000.0

    return MarketQuote(
        instrument_key=str(payload.get("instrument_token") or payload.get("instrument_key") or instrument_key),
        symbol=str(payload.get("trading_symbol") or payload.get("symbol") or instrument_key),
        last_price=last_price,
        best_bid=bid_levels[0].price if bid_levels else None,
        best_ask=ask_levels[0].price if ask_levels else None,
        bid_levels=bid_levels,
        ask_levels=ask_levels,
        open_price=_ohlc_value(payload, "open"),
        high_price=_ohlc_value(payload, "high"),
        low_price=_ohlc_value(payload, "low"),
        close_price=_ohlc_value(payload, "close"),
        average_price=_float(payload.get("average_price", payload.get("atp")), None),
        total_volume=_float(payload.get("volume", payload.get("volume_traded")), 0.0) or 0.0,
        timestamp=exchange_timestamp or time.time(),
        raw=payload,
    )


class UpstoxClient:
    def __init__(self, access_token: str, *, base_url: str = DEFAULT_BASE_URL, timeout: float = 4.0, user_agent: str = DEFAULT_USER_AGENT) -> None:
        self.access_token = access_token.strip()
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.user_agent = user_agent

    def request_json(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        query = f"?{urllib.parse.urlencode(params)}" if params else ""
        request = urllib.request.Request(f"{self.base_url}{path}{query}", method="GET")
        request.add_header("Accept", "application/json")
        request.add_header("Content-Type", "application/json")
        request.add_header("Authorization", f"Bearer {self.access_token}")
        request.add_header("User-Agent", self.user_agent)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise UpstoxError(f"Upstox HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise UpstoxError(f"Upstox request failed: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise UpstoxError("Upstox returned invalid JSON") from exc

    def full_market_quotes(self, instrument_keys: list[str]) -> dict[str, MarketQuote]:
        if not instrument_keys:
            return {}
        body = self.request_json("/market-quote/quotes", {"instrument_key": ",".join(instrument_keys)})
        data = body.get("data")
        if not isinstance(data, dict):
            raise UpstoxError(f"unexpected quote response: {body}")

        quotes: dict[str, MarketQuote] = {}
        remaining = set(instrument_keys)
        for response_key, raw_quote in data.items():
            quote = parse_full_market_quote(str(response_key), raw_quote)
            requested_key = quote.instrument_key if quote.instrument_key in remaining else str(response_key)
            if requested_key not in remaining and len(instrument_keys) == 1:
                requested_key = instrument_keys[0]
            quotes[requested_key] = quote
            remaining.discard(requested_key)
            remaining.discard(quote.instrument_key)
        return quotes

    def instrument_search(self, query: str) -> dict[str, Any]:
        return self.request_json("/instruments/search", {"query": query})
