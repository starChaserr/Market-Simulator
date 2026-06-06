from __future__ import annotations

import math
import random
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .config import DEFAULT_CONFIG


EPSILON = 1e-9


@dataclass
class Order:
    id: str
    side: str
    quantity: float
    remaining: float
    price: float | None
    order_type: str
    owner: str
    agent_type: str
    created_at: float
    ttl_ticks: int | None = None
    age_ticks: int = 0
    time_in_force: str = "gtc"
    post_only: bool = False
    stop_price: float | None = None
    status: str = "accepted"
    filled_quantity: float = 0.0
    filled_notional: float = 0.0
    updated_at: float = field(default_factory=time.time)
    triggered_at: float | None = None
    reject_reason: str | None = None

    @property
    def average_fill_price(self) -> float | None:
        if self.filled_quantity <= EPSILON:
            return None
        return self.filled_notional / self.filled_quantity


@dataclass
class Trade:
    id: str
    timestamp: float
    price: float
    quantity: float
    aggressor_side: str
    buyer: str
    seller: str
    buyer_type: str
    seller_type: str
    buyer_order_id: str | None = None
    seller_order_id: str | None = None
    buyer_fee: float = 0.0
    seller_fee: float = 0.0


@dataclass
class AgentAccount:
    owner: str
    agent_type: str
    cash: float = 0.0
    initial_cash: float = 0.0
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
    min_equity: float = 0.0
    created_at: float = field(default_factory=time.time)
    last_active_at: float = field(default_factory=time.time)
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.max_equity = self.cash
        self.min_equity = self.cash

    def mark_to_market(self, last_price: float) -> float:
        return self.cash + self.inventory * last_price

    def profit_loss(self, last_price: float) -> float:
        return self.mark_to_market(last_price) - self.initial_cash

    def unrealized_pnl(self, last_price: float) -> float:
        return self.profit_loss(last_price) - self.realized_pnl

    def refresh_equity_bounds(self, last_price: float) -> None:
        equity = self.mark_to_market(last_price)
        self.max_equity = max(self.max_equity, equity)
        self.min_equity = min(self.min_equity, equity)


class MatchingEngine:
    """Single-symbol limit order book with lifecycle, risk checks, fees, and slippage."""

    def __init__(self, symbol: str = "SIM", start_price: float = 100.0, config: dict[str, Any] | None = None) -> None:
        self.symbol = symbol
        self.start_price = start_price
        self.config = {**DEFAULT_CONFIG, **(config or {})}
        self.last_price = start_price
        self.fundamental_price = start_price
        self.reference_price = start_price
        self.tick = 0
        self.bids: list[Order] = []
        self.asks: list[Order] = []
        self.orders: dict[str, Order] = {}
        self.stop_order_ids: list[str] = []
        self.trades: deque[Trade] = deque(maxlen=1000)
        self.events: deque[dict[str, Any]] = deque(maxlen=1600)
        self.history: deque[dict[str, float]] = deque(maxlen=1200)
        self.accounts: dict[str, AgentAccount] = {}
        self.total_volume = 0.0
        self.session_high = start_price
        self.session_low = start_price
        self._tick_volume = 0.0
        self._tick_notional = 0.0
        self._tick_open: float | None = None
        self._tick_high: float | None = None
        self._tick_low: float | None = None
        self._tick_close: float | None = None
        self._mid_returns: deque[float] = deque(maxlen=100)
        self._event_sequence = 0
        self._triggering_stops = False
        self._flash_crashed = False
        self._circuit_upper = False   # upper circuit breaker triggered
        self._circuit_lower = False   # lower circuit breaker triggered
        self._realized_volatility = max(
            float(self.config.get("min_realized_volatility", 0.00008)),
            float(self.config.get("fundamental_volatility", 0.00045)),
        )
        self._news_impulse = 0.0
        self._signed_flow: deque[float] = deque(maxlen=80)
        self._liquidity_stress = 0.0
        if self.config.get("seed_order_book", True):
            self._seed_order_book()
        self.record_history()

    def reset(self) -> None:
        self.__init__(self.symbol, self.start_price, self.config)

    def clear_api_users(self) -> int:
        """Removes all API user accounts and their associated orders."""
        api_owners = [owner for owner, acc in self.accounts.items() if acc.agent_type == "api-user"]
        for owner in api_owners:
            self.cancel_orders_for_owner(owner, agent_type="api-user")
            del self.accounts[owner]
        self.orders = {oid: o for oid, o in self.orders.items() if o.agent_type != "api-user"}
        self.stop_order_ids = [oid for oid in self.stop_order_ids if oid in self.orders]
        self._emit("api_users_cleared", {"count": len(api_owners)})
        return len(api_owners)

    def register_account(self, owner: str, agent_type: str, cash: float = 0.0) -> AgentAccount:
        account = self.accounts.get(owner)
        if account is None:
            account = AgentAccount(owner=owner, agent_type=agent_type, cash=float(cash), initial_cash=float(cash))
            self.accounts[owner] = account
            self._emit("account_created", {"owner": owner, "agent_type": agent_type, "initial_cash": float(cash)})
        account.last_active_at = time.time()
        return account

    def add_account_funds(self, owner: str, amount: float, *, agent_type: str = "api-user") -> dict[str, Any]:
        if not math.isfinite(amount) or amount <= 0:
            raise ValueError("amount must be a positive number")
        account = self.register_account(owner, agent_type, cash=0.0)
        account.cash += float(amount)
        account.initial_cash += float(amount)
        account.max_equity += float(amount)
        account.min_equity += float(amount)
        account.last_active_at = time.time()
        account.refresh_equity_bounds(self.last_price)
        self._emit("account_funded", {"owner": owner, "agent_type": account.agent_type, "amount": round(float(amount), 2)})
        return self.account_summary(owner)

    def submit_order(
        self,
        *,
        side: str,
        quantity: float,
        price: float | None = None,
        order_type: str = "market",
        owner: str = "manual",
        agent_type: str = "manual",
        ttl_ticks: int | None = None,
        time_in_force: str = "gtc",
        post_only: bool = False,
        stop_price: float | None = None,
    ) -> dict[str, Any]:
        side = side.lower().strip()
        order_type = order_type.lower().strip().replace("-", "_")
        time_in_force = time_in_force.lower().strip()

        self._validate_order_input(side, quantity, price, order_type, time_in_force, stop_price)
        if order_type == "market":
            price = None
            time_in_force = "ioc"
        elif price is not None:
            price = self._snap_tick(float(price))
        if stop_price is not None:
            stop_price = self._snap_tick(float(stop_price))

        self._prune_outlier_orders()

        starting_cash = float(self.config.get("api_starting_cash", 0.0)) if agent_type == "api-user" else 0.0
        account = self.register_account(owner, agent_type, cash=starting_cash)
        account.orders += 1

        order = Order(
            id=self._make_id("ord"),
            side=side,
            quantity=float(quantity),
            remaining=float(quantity),
            price=price,
            order_type=order_type,
            owner=owner,
            agent_type=agent_type,
            created_at=time.time(),
            ttl_ticks=ttl_ticks,
            time_in_force=time_in_force,
            post_only=bool(post_only),
            stop_price=stop_price,
        )
        self.orders[order.id] = order

        rejection = self._risk_rejection(order, account)
        if rejection:
            order.status = "rejected"
            order.reject_reason = rejection
            order.updated_at = time.time()
            self._emit("order_rejected", self._order_to_dict(order))
            return self._order_response(order)

        if order_type in {"stop", "stop_limit"}:
            order.status = "pending_trigger"
            self.stop_order_ids.append(order.id)
            self._emit("order_pending_trigger", self._order_to_dict(order))
            self._trigger_stop_orders()
            return self._order_response(order)

        if post_only and order_type == "limit" and self._would_cross(order):
            order.status = "rejected"
            order.reject_reason = "post_only order would cross the book"
            order.updated_at = time.time()
            self._emit("order_rejected", self._order_to_dict(order))
            return self._order_response(order)

        if time_in_force == "fok" and not self._has_fill_or_kill_liquidity(order):
            order.status = "rejected"
            order.reject_reason = "fill_or_kill order cannot fully execute against visible liquidity"
            order.updated_at = time.time()
            self._emit("order_rejected", self._order_to_dict(order))
            return self._order_response(order)

        self._execute_active_order(order)
        self._trigger_stop_orders()
        return self._order_response(order)

    def cancel_order(self, order_id: str, owner: str | None = None) -> dict[str, Any]:
        order = self.orders.get(order_id)
        if order is None:
            raise ValueError(f"unknown order_id '{order_id}'")
        if owner is not None and order.owner != owner:
            raise ValueError("order does not belong to requested user")
        if order.status not in {"open", "partially_filled", "pending_trigger"}:
            return self._order_response(order)

        self.bids = [resting for resting in self.bids if resting.id != order_id]
        self.asks = [resting for resting in self.asks if resting.id != order_id]
        self.stop_order_ids = [item for item in self.stop_order_ids if item != order_id]
        order.status = "canceled" if order.filled_quantity <= EPSILON else "partial_canceled"
        order.updated_at = time.time()
        self._emit("order_canceled", self._order_to_dict(order))
        return self._order_response(order)

    def cancel_orders_for_owner(self, owner: str, *, agent_type: str | None = None) -> int:
        candidates = [
            order.id
            for order in list(self.bids) + list(self.asks)
            if order.owner == owner and (agent_type is None or order.agent_type == agent_type)
        ]
        count = 0
        for order_id in candidates:
            order = self.orders.get(order_id)
            if order is not None and order.status in {"open", "partially_filled"}:
                self.cancel_order(order_id)
                count += 1
        return count

    def list_orders(
        self,
        *,
        owner: str | None = None,
        status: str | None = None,
        include_internal: bool = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        orders = list(self.orders.values())
        if owner:
            orders = [order for order in orders if order.owner == owner]
        if status:
            orders = [order for order in orders if order.status == status]
        if not include_internal and owner is None:
            orders = [order for order in orders if order.agent_type == "api-user"]
        orders.sort(key=lambda order: order.updated_at, reverse=True)
        return [self._order_to_dict(order) for order in orders[: max(1, min(1000, limit))]]

    def account_summary(self, owner: str) -> dict[str, Any]:
        account = self.accounts.get(owner)
        if account is None:
            raise ValueError(f"unknown account '{owner}'")
        return self._account_to_dict(account)

    def advance_environment(self) -> None:
        previous_mid = self.reference_mid_price
        self.tick += 1

        flash = self.config.get("flash_crash", {})
        if flash.get("enabled") and not self._flash_crashed and self.tick >= int(flash.get("tick", 80)):
            flash_return = float(flash.get("shock", -0.08))
            self.fundamental_price *= 1 + flash_return
            self._liquidity_stress = min(3.0, self._liquidity_stress + abs(flash_return) * 12)
            self._flash_crashed = True
            self._emit("scenario_event", {"name": "flash_crash", "tick": self.tick, "shock": flash.get("shock")})

        trend = float(self.config.get("trend_per_tick", 0.0))
        recovery = float(flash.get("recovery", 0.0)) if self._flash_crashed else 0.0
        if random.random() < float(self.config.get("news_probability", 0.015)):
            news_direction = random.choice([-1, 1])
            news_size = random.uniform(float(self.config.get("news_min", 0.0015)), float(self.config.get("news_max", 0.008)))
            self._news_impulse += news_direction * news_size
            self._liquidity_stress = min(3.0, self._liquidity_stress + news_size * 5)
            self._emit("scenario_event", {"name": "news", "tick": self.tick, "direction": news_direction, "size": round(news_size, 5)})

        drift_to_reference = 0.0
        if self.fundamental_price > 0:
            drift_to_reference = (self.reference_price / self.fundamental_price - 1) * float(self.config.get("mean_reversion", 0.00005))
        flow_impact = self._order_flow_return()
        random_return = random.gauss(0.0, self._realized_volatility)
        if random.random() < 0.012 + min(0.045, self._liquidity_stress * 0.015):
            random_return *= random.uniform(2.0, 4.8)
        return_move = trend + recovery + drift_to_reference + self._news_impulse + flow_impact + random_return
        self.fundamental_price = max(1.0, self.fundamental_price * math.exp(max(-0.18, min(0.18, return_move))))
        self._news_impulse *= float(self.config.get("news_decay", 0.82))
        self._update_realized_volatility(return_move)
        self._update_liquidity_stress(return_move)

        # NSE-style circuit breaker: clamp fundamental price within ±circuit_limit_pct
        # of the reference (start) price.  Once a circuit fires, orders on that side
        # are blocked until the next session reset.
        circuit_pct = float(self.config.get("circuit_limit_pct", 0.0))
        if circuit_pct > 0:
            upper = self.start_price * (1 + circuit_pct)
            lower = self.start_price * (1 - circuit_pct)
            if self.fundamental_price >= upper:
                self.fundamental_price = upper
                self._circuit_upper = True
                self._emit("scenario_event", {"name": "circuit_upper", "tick": self.tick, "limit": round(upper, 2)})
            elif self.fundamental_price <= lower:
                self.fundamental_price = lower
                self._circuit_lower = True
                self._emit("scenario_event", {"name": "circuit_lower", "tick": self.tick, "limit": round(lower, 2)})

        self._expire_orders()
        self._cancel_queue_orders()
        needs_liquidity = self.best_bid is None or self.best_ask is None
        if needs_liquidity or random.random() < float(self.config.get("liquidity_probability", 0.55)):
            self._replenish_background_depth()
        for _ in range(3):
            if self.best_bid is not None and self.best_ask is not None:
                break
            self._replenish_background_depth()
        self._trigger_stop_orders()
        current_mid = self.reference_mid_price
        if previous_mid > 0 and current_mid > 0:
            self._mid_returns.append(math.log(current_mid / previous_mid))

    def record_history(self) -> None:
        spread = self.spread
        mid = self.mid_price
        reference_mid = self.reference_mid_price
        mark = self.mark_price
        traded = self._tick_volume > EPSILON and self._tick_close is not None
        open_price = self._tick_open
        if open_price is None:
            open_price = self.history[-1]["close"] if self.history else mark
        if traded:
            high_price = self._tick_high if self._tick_high is not None else max(open_price, self.last_price)
            low_price = self._tick_low if self._tick_low is not None else min(open_price, self.last_price)
            close_price = self._tick_close if self._tick_close is not None else self.last_price
            vwap = self._tick_notional / self._tick_volume
        else:
            high_price = max(open_price, mark)
            low_price = min(open_price, mark)
            close_price = mark
            vwap = mark
        self.history.append(
            {
                "t": time.time(),
                "tick": float(self.tick),
                "open": round(open_price, 4),
                "high": round(high_price, 4),
                "low": round(low_price, 4),
                "close": round(close_price, 4),
                "last": round(close_price, 4),
                "last_trade": round(self.last_price, 4),
                "mark": round(mark, 4),
                "mid": round(mid, 4),
                "reference_mid": round(reference_mid, 4),
                "fundamental": round(self.fundamental_price, 4),
                "spread": round(spread, 4),
                "volume": round(self._tick_volume, 4),
                "vwap": round(vwap, 4),
            }
        )
        self._tick_volume = 0.0
        self._tick_notional = 0.0
        self._tick_open = None
        self._tick_high = None
        self._tick_low = None
        self._tick_close = None

    def _order_flow_return(self) -> float:
        imbalance = self._order_flow_imbalance()
        coefficient = float(self.config.get("order_flow_price_impact", 0.000035))
        return imbalance * coefficient * (1 + self._liquidity_stress)

    def _order_flow_imbalance(self) -> float:
        if not self._signed_flow:
            return 0.0
        depth = max(1.0, float(self.config.get("latent_liquidity_depth", 3200.0)) * max(0.1, float(self.config.get("depth_scale", 1.0))))
        return max(-1.0, min(1.0, sum(self._signed_flow) / depth))

    def _update_realized_volatility(self, return_move: float) -> None:
        base = float(self.config.get("fundamental_volatility", 0.00045))
        decay = max(0.0, min(0.995, float(self.config.get("volatility_decay", 0.94))))
        clustering = max(0.0, float(self.config.get("volatility_clustering", 0.65)))
        min_vol = max(0.0, float(self.config.get("min_realized_volatility", 0.00008)))
        max_vol = max(min_vol, float(self.config.get("max_realized_volatility", 0.0045)))
        target = max(min_vol, min(max_vol, base + abs(return_move) * clustering))
        self._realized_volatility = max(min_vol, min(max_vol, decay * self._realized_volatility + (1 - decay) * target))

    def _update_liquidity_stress(self, return_move: float) -> None:
        base = max(float(self.config.get("fundamental_volatility", 0.00045)), 0.000001)
        resilience = max(0.0, min(0.95, float(self.config.get("liquidity_resilience", 0.08))))
        volatility_pressure = max(0.0, self._realized_volatility / base - 1.0) * 0.22
        return_pressure = min(2.0, abs(return_move) * 18)
        target = min(3.0, volatility_pressure + return_pressure)
        self._liquidity_stress = max(0.0, min(3.0, self._liquidity_stress * (1 - resilience) + target * resilience))

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    @property
    def mid_price(self) -> float:
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2
        return self.last_price

    @property
    def depth_imbalance(self) -> float:
        bid_depth = sum(order.remaining for order in self.bids[:5])
        ask_depth = sum(order.remaining for order in self.asks[:5])
        total = bid_depth + ask_depth
        if total <= EPSILON:
            return 0.0
        return max(-1.0, min(1.0, (bid_depth - ask_depth) / total))

    @property
    def microprice(self) -> float:
        if self.best_bid is None or self.best_ask is None:
            return self.mid_price
        bid_depth = sum(order.remaining for order in self.bids[:5])
        ask_depth = sum(order.remaining for order in self.asks[:5])
        total = bid_depth + ask_depth
        if total <= EPSILON:
            return self.mid_price
        return (self.best_ask * bid_depth + self.best_bid * ask_depth) / total

    @property
    def reference_mid_price(self) -> float:
        return self._bounded_price(self.mid_price, float(self.config.get("max_reference_deviation", 0.04)))

    @property
    def mark_price(self) -> float:
        if self.best_bid is not None and self.best_ask is not None:
            return self.reference_mid_price
        return self._bounded_price(self.last_price, float(self.config.get("max_reference_deviation", 0.04)))

    @property
    def spread(self) -> float:
        if self.best_bid is None or self.best_ask is None:
            return 0.0
        return max(0.0, self.best_ask - self.best_bid)

    @property
    def volatility(self) -> float:
        if len(self._mid_returns) < 2:
            return 0.0
        mean = sum(self._mid_returns) / len(self._mid_returns)
        variance = sum((value - mean) ** 2 for value in self._mid_returns) / (len(self._mid_returns) - 1)
        return math.sqrt(variance) * math.sqrt(240)

    def snapshot(self) -> dict[str, Any]:
        recent_history = list(self.history)[-420:]
        recent_trades = [self._trade_to_dict(trade) for trade in list(self.trades)[-100:]][::-1]
        return {
            "symbol": self.symbol,
            "tick": self.tick,
            "last_price": round(self.last_price, 4),
            "fundamental_price": round(self.fundamental_price, 4),
            "mid_price": round(self.mid_price, 4),
            "microprice": round(self.microprice, 4),
            "reference_mid_price": round(self.reference_mid_price, 4),
            "mark_price": round(self.mark_price, 4),
            "best_bid": round(self.best_bid, 4) if self.best_bid is not None else None,
            "best_ask": round(self.best_ask, 4) if self.best_ask is not None else None,
            "spread": round(self.spread, 4),
            "session_high": round(self.session_high, 4),
            "session_low": round(self.session_low, 4),
            "total_volume": round(self.total_volume, 4),
            "volatility": round(self.volatility, 6),
            "realized_volatility": round(self._realized_volatility, 6),
            "liquidity_stress": round(self._liquidity_stress, 4),
            "order_flow_imbalance": round(self._order_flow_imbalance(), 6),
            "depth_imbalance": round(self.depth_imbalance, 6),
            "fees": {
                "maker_fee_rate": float(self.config.get("maker_fee_rate", 0.0)),
                "taker_fee_rate": float(self.config.get("taker_fee_rate", 0.0)),
            },
            "order_book": {"bids": self._aggregate_book("buy", depth=20), "asks": self._aggregate_book("sell", depth=20)},
            "history": recent_history,
            "trades": recent_trades,
            "agents": self._agent_summaries(),
            "api_users": self._api_user_summaries(),
            "open_orders": self.list_orders(status="open", include_internal=False, limit=100),
            "events": list(self.events)[-100:],
            "circuit_upper": self._circuit_upper,
            "circuit_lower": self._circuit_lower,
            "tick_size":     float(self.config.get("tick_size", 0.0)),
        }

    def _execute_active_order(self, order: Order) -> None:
        order.status = "accepted"
        self._match(order)

        if order.remaining > EPSILON and order.order_type == "market" and self.config.get("latent_liquidity", True):
            self._fill_from_latent_liquidity(order)

        if order.remaining > EPSILON and order.order_type == "limit" and order.time_in_force == "gtc":
            self._book_order(order)
        elif order.remaining > EPSILON:
            order.status = "canceled" if order.filled_quantity <= EPSILON else "partial_canceled"
            order.updated_at = time.time()
        else:
            order.status = "filled"
            order.updated_at = time.time()

        self._emit("order_update", self._order_to_dict(order))

    def _match(self, incoming: Order) -> None:
        opposite_book = self.asks if incoming.side == "buy" else self.bids
        while incoming.remaining > EPSILON and opposite_book:
            best = opposite_book[0]
            if incoming.order_type == "limit":
                if incoming.side == "buy" and best.price is not None and best.price > (incoming.price or 0):
                    break
                if incoming.side == "sell" and best.price is not None and best.price < (incoming.price or 0):
                    break

            trade_qty = min(incoming.remaining, best.remaining)
            trade_price = best.price if best.price is not None else self.last_price
            trade = self._execute_trade(
                price=trade_price,
                quantity=trade_qty,
                aggressor_side=incoming.side,
                buyer=incoming.owner if incoming.side == "buy" else best.owner,
                seller=best.owner if incoming.side == "buy" else incoming.owner,
                buyer_type=incoming.agent_type if incoming.side == "buy" else best.agent_type,
                seller_type=best.agent_type if incoming.side == "buy" else incoming.agent_type,
                buyer_order_id=incoming.id if incoming.side == "buy" else best.id,
                seller_order_id=best.id if incoming.side == "buy" else incoming.id,
            )
            self._record_fill(incoming, trade.price, trade.quantity)
            self._record_fill(best, trade.price, trade.quantity)
            incoming.remaining -= trade_qty
            best.remaining -= trade_qty
            best.updated_at = time.time()
            best.status = "filled" if best.remaining <= EPSILON else "partially_filled"
            self._emit("order_update", self._order_to_dict(best))
            if best.remaining <= EPSILON:
                opposite_book.pop(0)

    def _fill_from_latent_liquidity(self, incoming: Order) -> None:
        remaining = incoming.remaining
        if remaining <= EPSILON:
            return
        available = self._latent_liquidity_available(incoming.side)
        fillable = min(remaining, available)
        if fillable <= EPSILON:
            return
        tranche_count = min(10, max(1, int(math.ceil(fillable / 800))))
        tranche_qty = fillable / tranche_count
        for index in range(tranche_count):
            qty = min(incoming.remaining, tranche_qty if index < tranche_count - 1 else fillable - tranche_qty * index)
            if qty <= EPSILON:
                break
            price = self._latent_liquidity_price(incoming.side, qty, index)
            if incoming.side == "buy":
                buyer = incoming.owner
                seller = "external-liquidity"
                buyer_type = incoming.agent_type
                seller_type = "latent-liquidity"
            else:
                buyer = "external-liquidity"
                seller = incoming.owner
                buyer_type = "latent-liquidity"
                seller_type = incoming.agent_type
            trade = self._execute_trade(
                price=price,
                quantity=qty,
                aggressor_side=incoming.side,
                buyer=buyer,
                seller=seller,
                buyer_type=buyer_type,
                seller_type=seller_type,
                buyer_order_id=incoming.id if incoming.side == "buy" else None,
                seller_order_id=None if incoming.side == "buy" else incoming.id,
            )
            self._record_fill(incoming, trade.price, trade.quantity)
            incoming.remaining = max(0.0, incoming.remaining - qty)

    def _latent_liquidity_available(self, side: str) -> float:
        top_depth = sum(level["quantity"] for level in self._aggregate_book("sell" if side == "buy" else "buy", depth=5))
        configured_depth = max(0.0, float(self.config.get("latent_liquidity_depth", 3200.0)))
        depth_scale = max(0.05, float(self.config.get("depth_scale", 1.0)))
        min_fraction = max(0.0, min(1.0, float(self.config.get("latent_min_fill_fraction", 0.18))))
        stress_haircut = 1 / (1 + self._liquidity_stress * 0.9 + min(4.0, self.volatility * 75))
        randomized = random.uniform(min_fraction, 1.0)
        return (configured_depth * depth_scale + top_depth * 0.35) * stress_haircut * randomized

    def _execute_trade(
        self,
        *,
        price: float,
        quantity: float,
        aggressor_side: str,
        buyer: str,
        seller: str,
        buyer_type: str,
        seller_type: str,
        buyer_order_id: str | None = None,
        seller_order_id: str | None = None,
    ) -> Trade:
        price = round(max(0.01, price), 4)
        quantity = round(max(0.0, quantity), 6)
        notional = price * quantity
        maker_fee_rate = float(self.config.get("maker_fee_rate", 0.00005))
        taker_fee_rate = float(self.config.get("taker_fee_rate", 0.0002))
        buyer_fee = notional * (taker_fee_rate if aggressor_side == "buy" else maker_fee_rate)
        seller_fee = notional * (taker_fee_rate if aggressor_side == "sell" else maker_fee_rate)

        trade = Trade(
            id=self._make_id("trd"),
            timestamp=time.time(),
            price=price,
            quantity=quantity,
            aggressor_side=aggressor_side,
            buyer=buyer,
            seller=seller,
            buyer_type=buyer_type,
            seller_type=seller_type,
            buyer_order_id=buyer_order_id,
            seller_order_id=seller_order_id,
            buyer_fee=round(buyer_fee, 6),
            seller_fee=round(seller_fee, 6),
        )
        self.trades.append(trade)
        signed_quantity = quantity if aggressor_side == "buy" else -quantity
        self._signed_flow.append(signed_quantity)
        self.last_price = price
        self.session_high = max(self.session_high, price)
        self.session_low = min(self.session_low, price)
        self.total_volume += quantity
        self._tick_volume += quantity
        self._tick_notional += notional
        if self._tick_open is None:
            self._tick_open = price
            self._tick_high = price
            self._tick_low = price
        else:
            self._tick_high = max(self._tick_high or price, price)
            self._tick_low = min(self._tick_low or price, price)
        self._tick_close = price

        buyer_account = self.register_account(buyer, buyer_type)
        seller_account = self.register_account(seller, seller_type)
        buyer_account.cash -= notional + buyer_fee
        seller_account.cash += notional - seller_fee
        self._apply_position(buyer_account, "buy", price, quantity, buyer_fee)
        self._apply_position(seller_account, "sell", price, quantity, seller_fee)
        buyer_account.fills += 1
        buyer_account.volume += quantity
        buyer_account.realized_notional += notional
        seller_account.fills += 1
        seller_account.volume += quantity
        seller_account.realized_notional += notional
        self._refresh_account_marks()

        self._emit("trade", self._trade_to_dict(trade))
        return trade

    def _refresh_account_marks(self) -> None:
        for account in self.accounts.values():
            account.refresh_equity_bounds(self.last_price)

    def _apply_position(self, account: AgentAccount, side: str, price: float, quantity: float, fee: float) -> None:
        account.fees_paid += fee
        account.realized_pnl -= fee
        if side == "buy":
            account.buy_volume += quantity
            if account.inventory >= -EPSILON:
                new_inventory = max(0.0, account.inventory) + quantity
                account.avg_entry_price = self._weighted_average(account.avg_entry_price, max(0.0, account.inventory), price, quantity)
                account.inventory = new_inventory
                return
            close_qty = min(quantity, -account.inventory)
            account.realized_pnl += (account.avg_entry_price - price) * close_qty
            account.inventory += close_qty
            remaining = quantity - close_qty
            if abs(account.inventory) <= EPSILON:
                account.inventory = 0.0
                account.avg_entry_price = 0.0
            if remaining > EPSILON:
                account.inventory = remaining
                account.avg_entry_price = price
            return

        account.sell_volume += quantity
        if account.inventory <= EPSILON:
            short_qty = max(0.0, -account.inventory)
            new_short = short_qty + quantity
            account.avg_entry_price = self._weighted_average(account.avg_entry_price, short_qty, price, quantity)
            account.inventory = -new_short
            return
        close_qty = min(quantity, account.inventory)
        account.realized_pnl += (price - account.avg_entry_price) * close_qty
        account.inventory -= close_qty
        remaining = quantity - close_qty
        if abs(account.inventory) <= EPSILON:
            account.inventory = 0.0
            account.avg_entry_price = 0.0
        if remaining > EPSILON:
            account.inventory = -remaining
            account.avg_entry_price = price

    def _book_order(self, order: Order) -> None:
        order.status = "open" if order.filled_quantity <= EPSILON else "partially_filled"
        order.updated_at = time.time()
        if order.side == "buy":
            self.bids.append(order)
            self.bids.sort(key=lambda item: (-(item.price or 0), item.created_at))
        else:
            self.asks.append(order)
            self.asks.sort(key=lambda item: ((item.price or 0), item.created_at))
        self._emit("order_open", self._order_to_dict(order))

    def _expire_orders(self) -> None:
        fresh_bids: list[Order] = []
        fresh_asks: list[Order] = []
        for order in self.bids:
            order.age_ticks += 1
            expiry_reason = self._resting_order_expiry_reason(order)
            if expiry_reason is None:
                fresh_bids.append(order)
            else:
                self._expire_resting_order(order, expiry_reason)
        for order in self.asks:
            order.age_ticks += 1
            expiry_reason = self._resting_order_expiry_reason(order)
            if expiry_reason is None:
                fresh_asks.append(order)
            else:
                self._expire_resting_order(order, expiry_reason)
        self.bids = fresh_bids
        self.asks = fresh_asks

    def _cancel_queue_orders(self) -> None:
        base_probability = max(0.0, float(self.config.get("order_cancel_probability", 0.018)))
        if base_probability <= 0:
            return
        cancel_api = bool(self.config.get("cancel_api_resting_orders", False))
        mid = max(0.01, self.reference_mid_price)
        queue_decay = max(0.0, float(self.config.get("queue_decay_strength", 0.12)))
        stress_multiplier = 1 + self._liquidity_stress * 0.9 + min(3.0, self.volatility * 80)

        def keep_or_cancel(order: Order) -> bool:
            if order.agent_type == "api-user" and not cancel_api:
                return True
            if order.agent_type in {"latent-liquidity"}:
                return True
            distance = abs((order.price or mid) / mid - 1)
            age_multiplier = 1 + min(4.0, order.age_ticks * queue_decay / 25)
            distance_multiplier = 1 + min(4.0, distance * 45)
            probability = min(0.85, base_probability * age_multiplier * distance_multiplier * stress_multiplier)
            if random.random() >= probability:
                return True
            self._expire_resting_order(order, "queue canceled")
            return False

        self.bids = [order for order in self.bids if keep_or_cancel(order)]
        self.asks = [order for order in self.asks if keep_or_cancel(order)]

    def _prune_outlier_orders(self) -> None:
        fresh_bids: list[Order] = []
        fresh_asks: list[Order] = []
        for order in self.bids:
            if self._is_outlier_order(order):
                self._expire_resting_order(order, "stale price outside reference band")
            else:
                fresh_bids.append(order)
        for order in self.asks:
            if self._is_outlier_order(order):
                self._expire_resting_order(order, "stale price outside reference band")
            else:
                fresh_asks.append(order)
        self.bids = fresh_bids
        self.asks = fresh_asks

    def _resting_order_expiry_reason(self, order: Order) -> str | None:
        if order.ttl_ticks is not None and order.age_ticks > order.ttl_ticks:
            return "ttl expired"
        if self._is_outlier_order(order):
            return "stale price outside reference band"
        return None

    def _is_outlier_order(self, order: Order) -> bool:
        if order.price is None:
            return False
        if order.agent_type == "api-user" and not self.config.get("prune_api_outlier_orders", False):
            return False
        max_deviation = float(self.config.get("max_resting_order_deviation", 0.08))
        if max_deviation <= 0:
            return False
        reference = max(0.01, self.fundamental_price)
        return abs(order.price / reference - 1) > max_deviation

    def _expire_resting_order(self, order: Order, reason: str) -> None:
        order.status = "expired" if order.filled_quantity <= EPSILON else "partial_expired"
        order.reject_reason = reason
        order.updated_at = time.time()
        self._emit("order_expired", self._order_to_dict(order))

    def _trigger_stop_orders(self) -> None:
        if self._triggering_stops:
            return
        self._triggering_stops = True
        try:
            for order_id in list(self.stop_order_ids):
                order = self.orders.get(order_id)
                if order is None or order.status != "pending_trigger":
                    self.stop_order_ids = [item for item in self.stop_order_ids if item != order_id]
                    continue
                if not self._should_trigger(order):
                    continue
                self.stop_order_ids = [item for item in self.stop_order_ids if item != order_id]
                order.triggered_at = time.time()
                order.order_type = "limit" if order.order_type == "stop_limit" else "market"
                if order.order_type == "market":
                    order.price = None
                    order.time_in_force = "ioc"
                order.status = "triggered"
                order.updated_at = time.time()
                self._emit("order_triggered", self._order_to_dict(order))
                self._execute_active_order(order)
        finally:
            self._triggering_stops = False

    def _should_trigger(self, order: Order) -> bool:
        if order.stop_price is None:
            return False
        if order.side == "buy":
            return self.last_price >= order.stop_price
        return self.last_price <= order.stop_price

    def _effective_depth_scale(self) -> float:
        base = max(0.01, float(self.config.get("depth_scale", 1.0)))
        stress_haircut = 1 / (1 + self._liquidity_stress * 0.75 + min(3.0, self.volatility * 45))
        return max(0.01, base * stress_haircut)

    def _effective_spread_scale(self) -> float:
        base = max(0.01, float(self.config.get("spread_scale", 1.0)))
        stress_markup = 1 + self._liquidity_stress * 0.55 + min(4.0, self.volatility * 55)
        return max(0.01, base * stress_markup)

    def _replenish_background_depth(self) -> None:
        depth_owner = "background-depth"
        stress = self._liquidity_stress + min(3.0, self.volatility * 70)
        max_levels = 4 if stress < 0.8 else 3 if stress < 1.8 else 2
        levels = random.randint(1, max_levels)
        last_weight = max(0.0, min(0.35, float(self.config.get("background_last_price_weight", 0.04))))
        bounded_last = self._bounded_price(self.last_price, float(self.config.get("max_reference_deviation", 0.04)))
        base = (self.fundamental_price * (1 - last_weight) + bounded_last * last_weight) * (1 + self._order_flow_imbalance() * 0.0009)
        depth_scale = self._effective_depth_scale()
        spread_scale = self._effective_spread_scale()
        for _ in range(levels):
            distance = random.lognormvariate(math.log(0.0045), 0.65) * spread_scale
            distance = max(0.0007, min(0.035, distance))
            size = random.lognormvariate(math.log(240), 0.55) * depth_scale
            bid_price = base * (1 - distance)
            ask_price = base * (1 + distance + random.uniform(0.0002, 0.0025 + stress * 0.0007) * spread_scale)
            tick = max(0.0001, float(self.config.get("tick_size", 0.01)))
            if self.best_ask is not None:
                bid_price = min(bid_price, self.best_ask - tick)
            if self.best_bid is not None:
                ask_price = max(ask_price, self.best_bid + tick)
            self.submit_order(
                side="buy",
                quantity=size,
                price=bid_price,
                order_type="limit",
                owner=depth_owner,
                agent_type="background-liquidity",
                ttl_ticks=random.randint(35, 100),
            )
            self.submit_order(
                side="sell",
                quantity=size * random.uniform(0.85, 1.2),
                price=ask_price,
                order_type="limit",
                owner=depth_owner,
                agent_type="background-liquidity",
                ttl_ticks=random.randint(35, 100),
            )
        self._trim_book()

    def _seed_order_book(self) -> None:
        depth_scale = self._effective_depth_scale()
        spread_scale = self._effective_spread_scale()
        for index in range(24):
            distance = (0.001 + index * 0.00125 + random.lognormvariate(math.log(0.00055), 0.45)) * spread_scale
            size = random.lognormvariate(math.log(430), 0.45) * depth_scale
            self.submit_order(
                side="buy",
                quantity=size,
                price=self.start_price * (1 - distance),
                order_type="limit",
                owner="opening-liquidity",
                agent_type="background-liquidity",
                ttl_ticks=250,
            )
            self.submit_order(
                side="sell",
                quantity=size * random.uniform(0.9, 1.15),
                price=self.start_price * (1 + distance),
                order_type="limit",
                owner="opening-liquidity",
                agent_type="background-liquidity",
                ttl_ticks=250,
            )
        self.accounts["opening-liquidity"].extra["display"] = False

    def _risk_rejection(self, order: Order, account: AgentAccount) -> str | None:
        if order.agent_type != "api-user" or not self.config.get("enforce_risk_limits", True):
            return None
        if order.quantity > float(self.config.get("max_order_quantity", 1_000_000)):
            return "quantity exceeds max_order_quantity"
        reserved_cash, reserved_buy_qty, reserved_sell_qty = self._open_order_reservations(account.owner)
        projected_long = account.inventory + reserved_buy_qty + (order.quantity if order.side == "buy" else 0.0)
        projected_short = account.inventory - reserved_sell_qty - (order.quantity if order.side == "sell" else 0.0)
        if not self.config.get("allow_short", True) and projected_short < -EPSILON:
            return "short selling is disabled"
        if max(abs(projected_long), abs(projected_short)) > float(self.config.get("max_position_abs", 1_000_000)):
            return "projected position exceeds max_position_abs"
        # NSE circuit breaker: block buy orders at upper circuit, sell at lower circuit
        if self._circuit_upper and order.side == "buy" and order.order_type in {"limit", "market"}:
            return "upper circuit breaker active — buy orders suspended"
        if self._circuit_lower and order.side == "sell" and order.order_type in {"limit", "market"}:
            return "lower circuit breaker active — sell orders suspended"
        if order.side == "buy":
            estimated_price = self._estimated_order_price(order)
            required_cash = reserved_cash + estimated_price * order.quantity * (1 + float(self.config.get("taker_fee_rate", 0.0002)))
            if account.cash + EPSILON < required_cash:
                return "insufficient buying power"
        return None

    def _open_order_reservations(self, owner: str) -> tuple[float, float, float]:
        reserved_cash = 0.0
        reserved_buy_qty = 0.0
        reserved_sell_qty = 0.0
        fee_rate = float(self.config.get("taker_fee_rate", 0.0002))
        for order in self.orders.values():
            if order.owner != owner or order.status not in {"open", "partially_filled", "pending_trigger"}:
                continue
            remaining = max(0.0, order.remaining)
            if remaining <= EPSILON:
                continue
            if order.side == "buy":
                reserved_buy_qty += remaining
                reserved_cash += self._estimated_order_price(order) * remaining * (1 + fee_rate)
            else:
                reserved_sell_qty += remaining
        return reserved_cash, reserved_buy_qty, reserved_sell_qty

    def _estimated_order_price(self, order: Order) -> float:
        if order.price is not None:
            return order.price
        if order.stop_price is not None:
            return order.stop_price
        reference = self.best_ask if order.side == "buy" and self.best_ask is not None else self.best_bid
        if reference is None:
            reference = self.last_price
        buffer = max(0.0, float(self.config.get("market_order_price_buffer", 0.02))) if order.order_type == "market" else 0.0
        return max(0.01, reference * (1 + buffer if order.side == "buy" else 1 - buffer))

    def _snap_tick(self, price: float) -> float:
        """Round price to the nearest tick size if tick_size is configured."""
        tick = float(self.config.get("tick_size", 0.0))
        if tick <= 0:
            return round(price, 4)
        snapped = round(round(price / tick) * tick, 10)
        decimals = max(0, -int(math.floor(math.log10(tick))) if tick < 1 else 0)
        return round(snapped, decimals + 2)

    def _validate_order_input(
        self,
        side: str,
        quantity: float,
        price: float | None,
        order_type: str,
        time_in_force: str,
        stop_price: float | None,
    ) -> None:
        if side not in {"buy", "sell"}:
            raise ValueError("side must be 'buy' or 'sell'")
        if order_type not in {"market", "limit", "stop", "stop_limit"}:
            raise ValueError("order_type must be market, limit, stop, or stop_limit")
        if time_in_force not in {"gtc", "ioc", "fok"}:
            raise ValueError("time_in_force must be gtc, ioc, or fok")
        if not math.isfinite(quantity) or quantity <= 0:
            raise ValueError("quantity must be a positive number")
        if order_type in {"limit", "stop_limit"} and (price is None or not math.isfinite(price) or price <= 0):
            raise ValueError("limit and stop_limit orders require a positive price")
        if order_type in {"stop", "stop_limit"} and (stop_price is None or not math.isfinite(stop_price) or stop_price <= 0):
            raise ValueError("stop and stop_limit orders require a positive stop_price")

    def _would_cross(self, order: Order) -> bool:
        if order.order_type != "limit":
            return False
        if order.side == "buy" and self.best_ask is not None:
            return (order.price or 0) >= self.best_ask
        if order.side == "sell" and self.best_bid is not None:
            return (order.price or 0) <= self.best_bid
        return False

    def _has_fill_or_kill_liquidity(self, order: Order) -> bool:
        return self._available_visible_quantity(order) + EPSILON >= order.quantity

    def _available_visible_quantity(self, order: Order) -> float:
        opposite_book = self.asks if order.side == "buy" else self.bids
        available = 0.0
        for resting in opposite_book:
            if order.order_type == "limit":
                if order.side == "buy" and resting.price is not None and resting.price > (order.price or 0):
                    break
                if order.side == "sell" and resting.price is not None and resting.price < (order.price or 0):
                    break
            available += resting.remaining
            if available >= order.quantity:
                break
        return available

    def _record_fill(self, order: Order, price: float, quantity: float) -> None:
        order.filled_quantity += quantity
        order.filled_notional += price * quantity
        order.updated_at = time.time()

    def _latent_liquidity_price(self, side: str, quantity: float, tranche_index: int) -> float:
        direction = 1 if side == "buy" else -1
        top_price = self.best_ask if side == "buy" else self.best_bid
        anchor = top_price if top_price is not None else self.last_price
        anchor = self._bounded_price(anchor, float(self.config.get("max_reference_deviation", 0.04)))
        normalized_qty = max(0.01, quantity / 1000.0)
        volatility_bump = 1 + min(4.0, self.volatility * 80) + self._liquidity_stress * 0.55
        impact = (0.00045 + 0.00085 * math.sqrt(normalized_qty)) * volatility_bump
        tranche_penalty = tranche_index * 0.00028
        noise = random.uniform(0.00005, 0.00035 + self._liquidity_stress * 0.00025)
        raw_price = anchor * (1 + direction * (impact + tranche_penalty + noise))
        return self._bounded_price(raw_price, float(self.config.get("max_latent_trade_deviation", 0.12)))

    def _bounded_price(self, price: float, max_deviation: float) -> float:
        if not math.isfinite(price) or price <= 0:
            return max(0.01, self.fundamental_price)
        if max_deviation <= 0:
            return max(0.01, price)
        reference = max(0.01, self.fundamental_price)
        lower = reference * max(0.01, 1 - max_deviation)
        upper = reference * (1 + max_deviation)
        return min(max(price, lower), upper)

    def _aggregate_book(self, side: str, depth: int) -> list[dict[str, float]]:
        book = self.bids if side == "buy" else self.asks
        levels: dict[float, float] = {}
        for order in book:
            if order.price is None:
                continue
            price = round(order.price, 2)
            levels[price] = levels.get(price, 0.0) + order.remaining
        ordered = sorted(levels.items(), reverse=(side == "buy"))
        cumulative = 0.0
        result = []
        for price, quantity in ordered[:depth]:
            cumulative += quantity
            result.append({"price": round(price, 2), "quantity": round(quantity, 4), "cumulative": round(cumulative, 4)})
        return result

    def inject_external_shock(self, return_move: float, *, source: str = "operator") -> dict[str, Any]:
        if not math.isfinite(return_move):
            raise ValueError("shock must be a finite decimal return")
        if abs(return_move) > 0.12:
            raise ValueError("shock must be between -0.12 and 0.12")

        self.fundamental_price = max(1.0, self.fundamental_price * math.exp(return_move))
        self._news_impulse += return_move * 0.35
        self._liquidity_stress = min(3.0, self._liquidity_stress + abs(return_move) * 18)
        max_volatility = max(
            float(self.config.get("min_realized_volatility", 0.00008)),
            float(self.config.get("max_realized_volatility", 0.0045)),
        )
        self._realized_volatility = min(
            max_volatility,
            max(self._realized_volatility, abs(return_move) * 0.12),
        )
        payload = {
            "name": "operator_shock",
            "source": source,
            "tick": self.tick,
            "shock": round(return_move, 6),
        }
        self._emit("scenario_event", payload)
        return payload

    def _agent_summaries(self) -> list[dict[str, Any]]:
        summaries = []
        for account in self.accounts.values():
            if account.extra.get("display") is False:
                continue
            if account.agent_type in {"api-user", "background-liquidity", "latent-liquidity"}:
                continue
            summaries.append(self._account_to_dict(account))
        return sorted(summaries, key=lambda item: (item["agent_type"], item["owner"]))

    def _api_user_summaries(self) -> list[dict[str, Any]]:
        users = [self._account_to_dict(account) for account in self.accounts.values() if account.agent_type == "api-user"]
        return sorted(users, key=lambda item: item["profit_loss"], reverse=True)

    def _account_to_dict(self, account: AgentAccount) -> dict[str, Any]:
        equity = account.mark_to_market(self.last_price)
        profit_loss = account.profit_loss(self.last_price)
        avg_trade_price = account.realized_notional / account.volume if account.volume else None
        reserved_cash, reserved_buy_qty, reserved_sell_qty = self._open_order_reservations(account.owner)
        return {
            "owner": account.owner,
            "user": account.owner,
            "agent_type": account.agent_type,
            "initial_cash": round(account.initial_cash, 2),
            "cash": round(account.cash, 2),
            "buying_power": round(max(0.0, account.cash - reserved_cash), 2),
            "reserved_buying_power": round(reserved_cash, 2),
            "reserved_buy_quantity": round(reserved_buy_qty, 4),
            "reserved_sell_quantity": round(reserved_sell_qty, 4),
            "inventory": round(account.inventory, 4),
            "avg_entry_price": round(account.avg_entry_price, 4),
            "orders": account.orders,
            "fills": account.fills,
            "volume": round(account.volume, 4),
            "buy_volume": round(account.buy_volume, 4),
            "sell_volume": round(account.sell_volume, 4),
            "average_trade_price": round(avg_trade_price, 4) if avg_trade_price is not None else None,
            "equity": round(equity, 2),
            "profit_loss": round(profit_loss, 2),
            "realized_pnl": round(account.realized_pnl, 2),
            "unrealized_pnl": round(account.unrealized_pnl(self.last_price), 2),
            "fees_paid": round(account.fees_paid, 4),
            "max_drawdown": round(max(0.0, account.max_equity - equity), 2),
            "mark_to_market": round(equity, 2),
            "last_active_seconds_ago": round(time.time() - account.last_active_at, 1),
            "extra": account.extra,
        }

    def _order_response(self, order: Order) -> dict[str, Any]:
        response = self._order_to_dict(order)
        response.update(
            {
                "order_id": order.id,
                "symbol": self.symbol,
                "user": order.owner,
                "requested_quantity": order.quantity,
                "filled_quantity": round(order.filled_quantity, 6),
                "remaining_quantity": round(max(order.remaining, 0.0), 6),
                "average_price": round(order.average_fill_price, 4) if order.average_fill_price is not None else None,
                "fills": self._fills_for_order(order)[-20:],
            }
        )
        return response

    def _order_to_dict(self, order: Order) -> dict[str, Any]:
        return {
            "id": order.id,
            "order_id": order.id,
            "symbol": self.symbol,
            "side": order.side,
            "order_type": order.order_type,
            "time_in_force": order.time_in_force,
            "post_only": order.post_only,
            "quantity": round(order.quantity, 6),
            "filled_quantity": round(order.filled_quantity, 6),
            "remaining_quantity": round(max(order.remaining, 0.0), 6),
            "price": round(order.price, 4) if order.price is not None else None,
            "stop_price": round(order.stop_price, 4) if order.stop_price is not None else None,
            "average_fill_price": round(order.average_fill_price, 4) if order.average_fill_price is not None else None,
            "owner": order.owner,
            "user": order.owner,
            "agent_type": order.agent_type,
            "status": order.status,
            "created_at": order.created_at,
            "updated_at": order.updated_at,
            "triggered_at": order.triggered_at,
            "reject_reason": order.reject_reason,
        }

    def _trade_to_dict(self, trade: Trade) -> dict[str, Any]:
        return {
            "id": trade.id,
            "timestamp": trade.timestamp,
            "price": round(trade.price, 4),
            "quantity": round(trade.quantity, 6),
            "notional": round(trade.price * trade.quantity, 4),
            "aggressor_side": trade.aggressor_side,
            "buyer": trade.buyer,
            "seller": trade.seller,
            "buyer_type": trade.buyer_type,
            "seller_type": trade.seller_type,
            "buyer_order_id": trade.buyer_order_id,
            "seller_order_id": trade.seller_order_id,
            "buyer_fee": round(trade.buyer_fee, 6),
            "seller_fee": round(trade.seller_fee, 6),
        }

    def _fills_for_order(self, order: Order) -> list[dict[str, Any]]:
        if order.filled_quantity <= EPSILON:
            return []
        fills = []
        for trade in self.trades:
            if trade.buyer_order_id == order.id:
                fills.append(self._trade_to_dict(trade))
            elif trade.seller_order_id == order.id:
                fills.append(self._trade_to_dict(trade))
        return fills

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        self._event_sequence += 1
        self.events.append({"seq": self._event_sequence, "type": event_type, "timestamp": time.time(), "payload": payload})

    def _trim_book(self) -> None:
        self.bids = self.bids[:300]
        self.asks = self.asks[:300]

    @staticmethod
    def _weighted_average(existing_price: float, existing_qty: float, new_price: float, new_qty: float) -> float:
        total = existing_qty + new_qty
        if total <= EPSILON:
            return 0.0
        return (existing_price * existing_qty + new_price * new_qty) / total

    @staticmethod
    def _make_id(prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex[:10]}"
