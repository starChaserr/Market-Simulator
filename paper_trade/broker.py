from __future__ import annotations

import math
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any

try:
    from .upstox_client import MarketQuote, QuoteLevel
except ImportError:  # pragma: no cover - allows python paper_trade/broker.py style imports
    from upstox_client import MarketQuote, QuoteLevel


EPSILON = 1e-9
OPEN_STATUSES = {"open", "partially_filled", "pending_trigger"}


@dataclass
class PaperConfig:
    symbol: str
    instrument_key: str
    tick: float = 0.05
    starting_cash: float = 10_000.0
    maker_fee_rate: float = 0.0002
    taker_fee_rate: float = 0.0005
    slippage_bps: float = 1.0
    max_history: int = 420


@dataclass
class Account:
    owner: str
    initial_cash: float
    cash: float
    inventory: float = 0.0
    avg_entry_price: float = 0.0
    realized_pnl: float = 0.0
    fees_paid: float = 0.0
    orders: int = 0
    fills: int = 0
    volume: float = 0.0
    buy_volume: float = 0.0
    sell_volume: float = 0.0
    realized_notional: float = 0.0
    max_equity: float = 0.0
    last_active_at: float = field(default_factory=time.time)

    def mark_to_market(self, mark_price: float) -> float:
        return self.cash + self.inventory * mark_price

    def profit_loss(self, mark_price: float) -> float:
        return self.mark_to_market(mark_price) - self.initial_cash

    def unrealized_pnl(self, mark_price: float) -> float:
        if self.inventory > 0:
            return (mark_price - self.avg_entry_price) * self.inventory
        if self.inventory < 0:
            return (self.avg_entry_price - mark_price) * abs(self.inventory)
        return 0.0


@dataclass
class PaperOrder:
    id: str
    owner: str
    side: str
    quantity: float
    order_type: str
    time_in_force: str
    price: float | None = None
    stop_price: float | None = None
    post_only: bool = False
    status: str = "accepted"
    filled_quantity: float = 0.0
    remaining_quantity: float = 0.0
    average_fill_price: float | None = None
    reject_reason: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    triggered_at: float | None = None


class PaperBroker:
    def __init__(self, config: PaperConfig) -> None:
        self.config = config
        self.accounts: dict[str, Account] = {}
        self.orders: dict[str, PaperOrder] = {}
        self.trades: deque[dict[str, Any]] = deque(maxlen=250)
        self.events: deque[dict[str, Any]] = deque(maxlen=150)
        self.history: deque[dict[str, Any]] = deque(maxlen=config.max_history)
        self.quote: MarketQuote | None = None
        self.reference_mid_price: float | None = None
        self.fundamental_price: float | None = None

    def set_quote(self, quote: MarketQuote) -> None:
        self.quote = self._with_fallback_depth(quote)
        mid = self.quote.mid_price
        if self.reference_mid_price is None:
            self.reference_mid_price = mid
        anchor = self.quote.average_price or self.quote.close_price or self.quote.open_price or mid
        if self.fundamental_price is None:
            self.fundamental_price = anchor
        else:
            self.fundamental_price = self.fundamental_price * 0.995 + anchor * 0.005
        self._append_history()
        self._match_open_orders()

    def record_event(self, message: str, level: str = "info") -> None:
        self.events.append({"time": time.time(), "level": level, "message": message})

    def ensure_account(self, owner: str, starting_cash: float | None = None) -> dict[str, Any]:
        owner = self._normalize_owner(owner)
        if owner not in self.accounts:
            cash = float(starting_cash if starting_cash is not None else self.config.starting_cash)
            account = Account(owner=owner, initial_cash=cash, cash=cash, max_equity=cash)
            self.accounts[owner] = account
        elif starting_cash is not None and self.accounts[owner].orders == 0 and self.accounts[owner].fills == 0:
            cash = float(starting_cash)
            account = self.accounts[owner]
            account.initial_cash = cash
            account.cash = cash
            account.max_equity = cash
        return self.account(owner)

    def fund_account(self, owner: str, amount: float) -> dict[str, Any]:
        owner = self._normalize_owner(owner)
        self.ensure_account(owner)
        account = self.accounts[owner]
        account.cash += float(amount)
        account.initial_cash += float(amount)
        account.max_equity = max(account.max_equity, account.mark_to_market(self.mark_price))
        account.last_active_at = time.time()
        return self.account(owner)

    def clear_users(self) -> dict[str, Any]:
        self.accounts.clear()
        self.orders.clear()
        self.trades.clear()
        return {"ok": True, "users": []}

    def reset(self) -> dict[str, Any]:
        self.clear_users()
        self.events.clear()
        self.record_event("paper accounts reset")
        return self.snapshot()

    def submit_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        side = str(payload.get("side", "")).lower().strip()
        owner = self._payload_owner(payload)
        quantity = self._positive_float(payload.get("quantity"))
        order_type = str(payload.get("order_type", payload.get("type", "market"))).lower().strip().replace("-", "_")
        time_in_force = str(payload.get("time_in_force", payload.get("tif", "gtc"))).lower().strip()
        price = self._optional_float(payload.get("price"))
        stop_price = self._optional_float(payload.get("stop_price"))
        post_only = bool(payload.get("post_only", False))

        self._validate_order(side, quantity, order_type, time_in_force, price, stop_price)
        if order_type == "market":
            time_in_force = "ioc"

        self.ensure_account(owner)
        account = self.accounts[owner]
        account.orders += 1
        account.last_active_at = time.time()
        order = PaperOrder(
            id=f"paper_{uuid.uuid4().hex[:12]}",
            owner=owner,
            side=side,
            quantity=quantity,
            order_type=order_type,
            time_in_force=time_in_force,
            price=price,
            stop_price=stop_price,
            post_only=post_only,
            remaining_quantity=quantity,
        )
        self.orders[order.id] = order

        if self.quote is None:
            return self._reject(order, "no live quote loaded yet")
        if post_only and order_type == "limit" and self._would_cross(order):
            return self._reject(order, "post_only order would cross current live quote")

        if order_type in {"stop", "stop_limit"} and not self._stop_triggered(order):
            order.status = "pending_trigger"
            order.updated_at = time.time()
            return self._order_response(order)

        self._try_fill_order(order, passive=False)
        if order.remaining_quantity > EPSILON and order.order_type == "limit" and order.time_in_force == "gtc":
            order.status = "open"
        elif order.remaining_quantity > EPSILON:
            order.status = "canceled" if order.filled_quantity <= EPSILON else "partial_canceled"
        else:
            order.status = "filled"
        order.updated_at = time.time()
        return self._order_response(order)

    def cancel_order(self, order_id: str, owner: str | None = None) -> dict[str, Any]:
        order = self.orders.get(order_id)
        if order is None:
            raise ValueError("order not found")
        if owner is not None and order.owner != self._normalize_owner(owner):
            raise ValueError("order does not belong to user")
        if order.status in OPEN_STATUSES:
            order.status = "canceled"
            order.updated_at = time.time()
        return self._order_response(order)

    def list_orders(self, owner: str | None = None, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        normalized_owner = self._normalize_owner(owner) if owner else None
        rows = list(self.orders.values())
        if normalized_owner:
            rows = [order for order in rows if order.owner == normalized_owner]
        if status:
            rows = [order for order in rows if order.status == status]
        rows.sort(key=lambda order: order.updated_at, reverse=True)
        return [self._order_to_dict(order) for order in rows[: max(1, min(1000, int(limit)))]]

    def list_accounts(self) -> list[dict[str, Any]]:
        return sorted((self.account(owner) for owner in self.accounts), key=lambda row: row["profit_loss"], reverse=True)

    def account(self, owner: str) -> dict[str, Any]:
        owner = self._normalize_owner(owner)
        self.ensure_account(owner) if owner not in self.accounts else None
        account = self.accounts[owner]
        mark = self.mark_price
        equity = account.mark_to_market(mark)
        account.max_equity = max(account.max_equity, equity)
        avg_trade_price = account.realized_notional / account.volume if account.volume else None
        return {
            "owner": account.owner,
            "user": account.owner,
            "agent_type": "paper-agent",
            "initial_cash": round(account.initial_cash, 2),
            "cash": round(account.cash, 2),
            "inventory": round(account.inventory, 4),
            "avg_entry_price": round(account.avg_entry_price, 4),
            "orders": account.orders,
            "fills": account.fills,
            "volume": round(account.volume, 4),
            "buy_volume": round(account.buy_volume, 4),
            "sell_volume": round(account.sell_volume, 4),
            "average_trade_price": round(avg_trade_price, 4) if avg_trade_price is not None else None,
            "equity": round(equity, 2),
            "profit_loss": round(account.profit_loss(mark), 2),
            "realized_pnl": round(account.realized_pnl, 2),
            "unrealized_pnl": round(account.unrealized_pnl(mark), 2),
            "fees_paid": round(account.fees_paid, 4),
            "max_drawdown": round(max(0.0, account.max_equity - equity), 2),
            "mark_to_market": round(equity, 2),
            "last_active_seconds_ago": round(time.time() - account.last_active_at, 1),
            "extra": {"broker": "upstox-paper", "instrument_key": self.config.instrument_key},
        }

    def snapshot(self) -> dict[str, Any]:
        quote = self.quote
        mark = self.mark_price
        bid = self.best_bid
        ask = self.best_ask
        return {
            "mode": "upstox_paper",
            "running": True,
            "symbol": self.config.symbol,
            "instrument_key": self.config.instrument_key,
            "tick": self.config.tick,
            "last_price": round(mark, 4),
            "fundamental_price": round(self.fundamental_price or mark, 4),
            "mid_price": round(self.mid_price, 4),
            "reference_mid_price": round(self.reference_mid_price or self.mid_price, 4),
            "mark_price": round(mark, 4),
            "best_bid": round(bid, 4) if bid is not None else None,
            "best_ask": round(ask, 4) if ask is not None else None,
            "spread": round(max(0.0, (ask or mark) - (bid or mark)), 4),
            "session_high": round((quote.high_price if quote else None) or mark, 4),
            "session_low": round((quote.low_price if quote else None) or mark, 4),
            "total_volume": round((quote.total_volume if quote else 0.0), 4),
            "volatility": round(self._volatility(), 6),
            "fees": {
                "maker_fee_rate": self.config.maker_fee_rate,
                "taker_fee_rate": self.config.taker_fee_rate,
            },
            "order_book": {"bids": self._book_side("buy"), "asks": self._book_side("sell")},
            "history": list(self.history),
            "trades": list(self.trades)[-100:][::-1],
            "agents": [],
            "agent_counts": {},
            "api_users": self.list_accounts(),
            "open_orders": self.list_orders(status="open", limit=100),
            "events": list(self.events)[-100:],
            "quote_age_seconds": round(time.time() - quote.timestamp, 3) if quote else None,
        }

    @property
    def mark_price(self) -> float:
        if self.quote:
            return self.quote.last_price
        return self.reference_mid_price or self.fundamental_price or 0.01

    @property
    def mid_price(self) -> float:
        if self.quote:
            return self.quote.mid_price
        return self.mark_price

    @property
    def best_bid(self) -> float | None:
        return self.quote.best_bid if self.quote else None

    @property
    def best_ask(self) -> float | None:
        return self.quote.best_ask if self.quote else None

    def _match_open_orders(self) -> None:
        for order in list(self.orders.values()):
            if order.status not in OPEN_STATUSES:
                continue
            if order.order_type in {"stop", "stop_limit"} and order.status == "pending_trigger":
                if not self._stop_triggered(order):
                    continue
                order.triggered_at = time.time()
                order.order_type = "limit" if order.order_type == "stop_limit" else "market"
                if order.order_type == "market":
                    order.time_in_force = "ioc"
            self._try_fill_order(order, passive=True)
            if order.remaining_quantity <= EPSILON:
                order.status = "filled"
            elif order.time_in_force in {"ioc", "fok"} and order.filled_quantity > EPSILON:
                order.status = "partial_canceled"
            elif order.time_in_force in {"ioc", "fok"}:
                order.status = "canceled"
            else:
                order.status = "open"
            order.updated_at = time.time()

    def _try_fill_order(self, order: PaperOrder, *, passive: bool) -> None:
        fill = self._fill_price(order, passive=passive)
        if fill is None:
            return
        price, available = fill
        quantity = min(order.remaining_quantity, available if available > 0 else order.remaining_quantity)
        if quantity <= EPSILON:
            return
        self._apply_fill(order, quantity, price)

    def _fill_price(self, order: PaperOrder, *, passive: bool) -> tuple[float, float] | None:
        quote = self.quote
        if quote is None:
            return None
        bid = quote.best_bid
        ask = quote.best_ask
        last = quote.last_price
        if order.order_type == "market":
            raw_price = ask if order.side == "buy" else bid
            return (self._slipped(raw_price or last, order.side), self._top_quantity(order.side)) if raw_price or last else None
        if order.order_type != "limit" or order.price is None:
            return None
        if order.side == "buy":
            if ask is not None and order.price >= ask:
                return self._slipped(min(order.price, ask), order.side), self._top_quantity(order.side)
            if passive and last <= order.price:
                return order.price, order.remaining_quantity
        else:
            if bid is not None and order.price <= bid:
                return self._slipped(max(order.price, bid), order.side), self._top_quantity(order.side)
            if passive and last >= order.price:
                return order.price, order.remaining_quantity
        return None

    def _apply_fill(self, order: PaperOrder, quantity: float, price: float) -> None:
        account = self.accounts[order.owner]
        fee_rate = self.config.maker_fee_rate if order.post_only else self.config.taker_fee_rate
        notional = quantity * price
        fee = notional * fee_rate
        before_filled = order.filled_quantity
        order.filled_quantity += quantity
        order.remaining_quantity = max(0.0, order.quantity - order.filled_quantity)
        order.average_fill_price = (
            price
            if before_filled <= EPSILON or order.average_fill_price is None
            else ((order.average_fill_price * before_filled) + notional) / order.filled_quantity
        )
        account.fills += 1
        account.volume += quantity
        account.realized_notional += notional
        account.fees_paid += fee
        account.last_active_at = time.time()
        if order.side == "buy":
            self._apply_buy(account, quantity, price, fee)
        else:
            self._apply_sell(account, quantity, price, fee)
        self.trades.append(
            {
                "id": f"trade_{uuid.uuid4().hex[:12]}",
                "order_id": order.id,
                "symbol": self.config.symbol,
                "instrument_key": self.config.instrument_key,
                "side": order.side,
                "price": round(price, 4),
                "quantity": round(quantity, 4),
                "owner": order.owner,
                "timestamp": time.time(),
                "paper": True,
            }
        )

    def _apply_buy(self, account: Account, quantity: float, price: float, fee: float) -> None:
        account.cash -= quantity * price + fee
        account.buy_volume += quantity
        if account.inventory >= 0:
            total_qty = account.inventory + quantity
            account.avg_entry_price = ((account.avg_entry_price * account.inventory) + price * quantity) / total_qty if total_qty else 0.0
            account.inventory = total_qty
            return
        closing = min(quantity, abs(account.inventory))
        account.realized_pnl += (account.avg_entry_price - price) * closing
        remaining = quantity - closing
        account.inventory += quantity
        account.avg_entry_price = price if remaining > EPSILON else (account.avg_entry_price if account.inventory < -EPSILON else 0.0)

    def _apply_sell(self, account: Account, quantity: float, price: float, fee: float) -> None:
        account.cash += quantity * price - fee
        account.sell_volume += quantity
        if account.inventory <= 0:
            short_qty = abs(account.inventory) + quantity
            account.avg_entry_price = ((account.avg_entry_price * abs(account.inventory)) + price * quantity) / short_qty if short_qty else 0.0
            account.inventory -= quantity
            return
        closing = min(quantity, account.inventory)
        account.realized_pnl += (price - account.avg_entry_price) * closing
        remaining = quantity - closing
        account.inventory -= quantity
        account.avg_entry_price = price if remaining > EPSILON else (account.avg_entry_price if account.inventory > EPSILON else 0.0)

    def _append_history(self) -> None:
        quote = self.quote
        if quote is None:
            return
        self.history.append(
            {
                "time": round(time.time(), 3),
                "close": round(quote.last_price, 4),
                "mid": round(quote.mid_price, 4),
                "mark": round(quote.last_price, 4),
                "fundamental": round(self.fundamental_price or quote.mid_price, 4),
                "volume": round(quote.total_volume, 4),
            }
        )

    def _volatility(self) -> float:
        if len(self.history) < 3:
            return 0.0
        mids = [float(point["mid"]) for point in self.history if float(point.get("mid", 0)) > 0]
        returns = [mids[index] / mids[index - 1] - 1.0 for index in range(1, len(mids)) if mids[index - 1] > 0]
        if len(returns) < 2:
            return 0.0
        mean = sum(returns) / len(returns)
        variance = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
        return math.sqrt(variance) * math.sqrt(240)

    def _book_side(self, side: str) -> list[dict[str, Any]]:
        if not self.quote:
            return []
        levels = self.quote.bid_levels if side == "buy" else self.quote.ask_levels
        return [{"price": round(level.price, 4), "quantity": round(level.quantity, 4), "orders": level.orders} for level in levels[:20]]

    def _top_quantity(self, side: str) -> float:
        if not self.quote:
            return 0.0
        levels = self.quote.ask_levels if side == "buy" else self.quote.bid_levels
        return levels[0].quantity if levels else 0.0

    def _slipped(self, price: float, side: str) -> float:
        adjustment = self.config.slippage_bps / 10_000.0
        return price * (1.0 + adjustment if side == "buy" else 1.0 - adjustment)

    def _would_cross(self, order: PaperOrder) -> bool:
        if order.price is None or self.quote is None:
            return False
        if order.side == "buy" and self.quote.best_ask is not None:
            return order.price >= self.quote.best_ask
        if order.side == "sell" and self.quote.best_bid is not None:
            return order.price <= self.quote.best_bid
        return False

    def _stop_triggered(self, order: PaperOrder) -> bool:
        if order.stop_price is None:
            return False
        mark = self.mark_price
        return mark >= order.stop_price if order.side == "buy" else mark <= order.stop_price

    def _with_fallback_depth(self, quote: MarketQuote) -> MarketQuote:
        bid = quote.best_bid
        ask = quote.best_ask
        bids = quote.bid_levels
        asks = quote.ask_levels
        if bid is None:
            bid = max(self.config.tick, quote.last_price - self.config.tick)
            bids = [QuoteLevel(price=bid, quantity=10_000, orders=1)]
        if ask is None:
            ask = quote.last_price + self.config.tick
            asks = [QuoteLevel(price=ask, quantity=10_000, orders=1)]
        return MarketQuote(
            instrument_key=quote.instrument_key,
            symbol=quote.symbol,
            last_price=quote.last_price,
            best_bid=bid,
            best_ask=ask,
            bid_levels=bids,
            ask_levels=asks,
            open_price=quote.open_price,
            high_price=quote.high_price,
            low_price=quote.low_price,
            close_price=quote.close_price,
            average_price=quote.average_price,
            total_volume=quote.total_volume,
            timestamp=quote.timestamp,
            raw=quote.raw,
        )

    def _reject(self, order: PaperOrder, reason: str) -> dict[str, Any]:
        order.status = "rejected"
        order.reject_reason = reason
        order.updated_at = time.time()
        return self._order_response(order)

    def _order_response(self, order: PaperOrder) -> dict[str, Any]:
        row = self._order_to_dict(order)
        row.update(
            {
                "order_id": order.id,
                "requested_quantity": order.quantity,
                "fills": [trade for trade in self.trades if trade.get("order_id") == order.id][-20:],
            }
        )
        return row

    def _order_to_dict(self, order: PaperOrder) -> dict[str, Any]:
        return {
            "id": order.id,
            "order_id": order.id,
            "symbol": self.config.symbol,
            "instrument_key": self.config.instrument_key,
            "side": order.side,
            "order_type": order.order_type,
            "time_in_force": order.time_in_force,
            "post_only": order.post_only,
            "quantity": round(order.quantity, 6),
            "filled_quantity": round(order.filled_quantity, 6),
            "remaining_quantity": round(max(0.0, order.remaining_quantity), 6),
            "price": round(order.price, 4) if order.price is not None else None,
            "stop_price": round(order.stop_price, 4) if order.stop_price is not None else None,
            "average_fill_price": round(order.average_fill_price, 4) if order.average_fill_price is not None else None,
            "average_price": round(order.average_fill_price, 4) if order.average_fill_price is not None else None,
            "owner": order.owner,
            "user": order.owner,
            "agent_type": "paper-agent",
            "status": order.status,
            "created_at": order.created_at,
            "updated_at": order.updated_at,
            "triggered_at": order.triggered_at,
            "reject_reason": order.reject_reason,
        }

    def _payload_owner(self, payload: dict[str, Any]) -> str:
        for key in ("user", "user_name", "username", "api_user", "client", "client_id", "model", "owner", "name"):
            value = str(payload.get(key, "")).strip()
            if value:
                return self._normalize_owner(value)
        return "PaperAgent"

    def _normalize_owner(self, owner: str | None) -> str:
        value = str(owner or "").strip()
        if not value:
            raise ValueError("user is required")
        return value

    def _validate_order(
        self,
        side: str,
        quantity: float,
        order_type: str,
        time_in_force: str,
        price: float | None,
        stop_price: float | None,
    ) -> None:
        if side not in {"buy", "sell"}:
            raise ValueError("side must be 'buy' or 'sell'")
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        if order_type not in {"market", "limit", "stop", "stop_limit"}:
            raise ValueError("order_type must be market, limit, stop, or stop_limit")
        if time_in_force not in {"gtc", "ioc", "fok"}:
            raise ValueError("time_in_force must be gtc, ioc, or fok")
        if order_type in {"limit", "stop_limit"} and (price is None or price <= 0):
            raise ValueError("limit and stop_limit orders require a positive price")
        if order_type in {"stop", "stop_limit"} and (stop_price is None or stop_price <= 0):
            raise ValueError("stop and stop_limit orders require a positive stop_price")

    def _positive_float(self, value: Any) -> float:
        try:
            result = float(value)
        except (TypeError, ValueError):
            raise ValueError("quantity must be positive") from None
        if not math.isfinite(result) or result <= 0:
            raise ValueError("quantity must be positive")
        return result

    def _optional_float(self, value: Any) -> float | None:
        if value in (None, ""):
            return None
        try:
            result = float(value)
        except (TypeError, ValueError):
            return None
        return result if math.isfinite(result) else None
