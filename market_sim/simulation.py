from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from .config import build_config
from .engine import MatchingEngine


@dataclass
class BaseAgent:
    owner: str
    agent_type: str
    cash: float
    min_interval_ticks: int = 1
    max_interval_ticks: int = 1
    next_action_tick: int = 0

    def attach(self, engine: MatchingEngine) -> None:
        engine.register_account(self.owner, self.agent_type, cash=self.cash)

    def act(self, engine: MatchingEngine) -> None:
        raise NotImplementedError

    def ready(self, tick: int) -> bool:
        return tick >= self.next_action_tick

    def schedule_next(self, tick: int) -> None:
        low = max(1, self.min_interval_ticks)
        high = max(low, self.max_interval_ticks)
        self.next_action_tick = tick + random.randint(low, high)

    def record_action(self, engine: MatchingEngine, action: str, side: str = "hold", **details: Any) -> None:
        account = engine.accounts.get(self.owner)
        if account is None:
            return
        clean_details = {key: value for key, value in details.items() if value is not None}
        account.extra.update(
            {
                "last_action": action,
                "last_side": side,
                "last_action_tick": engine.tick,
                **clean_details,
            }
        )


@dataclass
class InstitutionalTrader(BaseAgent):
    parent_side: str = field(default_factory=lambda: random.choice(["buy", "sell"]))
    parent_total: float = field(default_factory=lambda: random.uniform(6500, 26000))
    parent_remaining: float = 0.0
    urgency: float = field(default_factory=lambda: random.uniform(0.65, 1.45))
    cooldown: int = 0

    def __post_init__(self) -> None:
        if self.parent_remaining <= 0:
            self.parent_remaining = self.parent_total

    def attach(self, engine: MatchingEngine) -> None:
        super().attach(engine)
        account = engine.accounts[self.owner]
        account.extra.update(
            {
                "parent_side": self.parent_side,
                "parent_remaining": round(self.parent_remaining, 2),
                "style": "VWAP/TWAP execution",
            }
        )

    def act(self, engine: MatchingEngine) -> None:
        if self.cooldown > 0:
            self.cooldown -= 1
            self.record_action(engine, "hold", reason="cooldown")
            return

        if self.parent_remaining <= 10:
            self._new_parent_order(engine)
            self.record_action(engine, "hold", side=self.parent_side, reason="new parent order")
            return

        if random.random() > 0.36 * self.urgency:
            self._sync_extra(engine)
            self.record_action(engine, "hold", side=self.parent_side, reason="waiting for execution window")
            return

        account = engine.accounts[self.owner]
        inventory_bias = -1 if account.inventory > 12000 else 1 if account.inventory < -12000 else 0
        if inventory_bias:
            self.parent_side = "sell" if inventory_bias < 0 else "buy"

        child_qty = min(self.parent_remaining, random.uniform(70, 420) * self.urgency)
        side = self.parent_side
        order_type = "market" if random.random() < 0.18 * self.urgency else "limit"
        price = None
        if order_type == "limit":
            fair = engine.reference_mid_price
            participation = random.uniform(0.0002, 0.0035) * self.urgency
            if side == "buy":
                best_ask = engine.best_ask or fair * 1.002
                price = min(best_ask, fair * (1 + participation))
            else:
                best_bid = engine.best_bid or fair * 0.998
                price = max(best_bid, fair * (1 - participation))

        result = engine.submit_order(
            side=side,
            quantity=child_qty,
            price=price,
            order_type=order_type,
            owner=self.owner,
            agent_type=self.agent_type,
            ttl_ticks=random.randint(10, 28),
        )
        self.parent_remaining = max(0.0, self.parent_remaining - result["filled_quantity"])
        self._sync_extra(engine)
        self.record_action(
            engine,
            "buy" if side == "buy" else "sell",
            side=side,
            order_type=order_type,
            last_quantity=round(child_qty, 4),
            last_filled=round(result["filled_quantity"], 4),
        )

    def _new_parent_order(self, engine: MatchingEngine) -> None:
        self.parent_side = random.choice(["buy", "sell"])
        self.parent_total = random.uniform(7000, 32000)
        self.parent_remaining = self.parent_total
        self.urgency = random.uniform(0.65, 1.5)
        self.cooldown = random.randint(10, 40)
        self._sync_extra(engine)

    def _sync_extra(self, engine: MatchingEngine) -> None:
        engine.accounts[self.owner].extra.update(
            {
                "parent_side": self.parent_side,
                "parent_remaining": round(self.parent_remaining, 2),
                "urgency": round(self.urgency, 2),
                "style": "VWAP/TWAP execution",
            }
        )


@dataclass
class HighFrequencyTrader(BaseAgent):
    quote_width: float = field(default_factory=lambda: random.uniform(0.0008, 0.0028))
    max_inventory: float = field(default_factory=lambda: random.uniform(900, 2600))

    def attach(self, engine: MatchingEngine) -> None:
        super().attach(engine)
        account = engine.accounts[self.owner]
        account.extra.update(
            {
                "quote_width": round(self.quote_width, 5),
                "max_inventory": round(self.max_inventory, 2),
                "style": "market making and short-horizon momentum",
            }
        )

    def act(self, engine: MatchingEngine) -> None:
        if random.random() > 0.82:
            self.record_action(engine, "hold", reason="quote throttle")
            return

        engine.cancel_orders_for_owner(self.owner, agent_type=self.agent_type)
        account = engine.accounts[self.owner]
        fair = engine.reference_mid_price * 0.55 + engine.fundamental_price * 0.45
        inventory_skew = max(-0.004, min(0.004, account.inventory / self.max_inventory * 0.0018))
        width = self.quote_width + random.uniform(0.0001, 0.0012) + min(0.004, engine.volatility * 0.2)
        base_qty = random.uniform(12, 95)
        submitted_sides: list[str] = []

        if account.inventory < self.max_inventory:
            engine.submit_order(
                side="buy",
                quantity=base_qty,
                price=fair * (1 - width - inventory_skew),
                order_type="limit",
                owner=self.owner,
                agent_type=self.agent_type,
                ttl_ticks=random.randint(2, 6),
            )
            submitted_sides.append("buy")
        if account.inventory > -self.max_inventory:
            engine.submit_order(
                side="sell",
                quantity=base_qty * random.uniform(0.8, 1.2),
                price=fair * (1 + width - inventory_skew),
                order_type="limit",
                owner=self.owner,
                agent_type=self.agent_type,
                ttl_ticks=random.randint(2, 6),
            )
            submitted_sides.append("sell")

        if len(engine.history) > 8 and random.random() < 0.18:
            recent = list(engine.history)[-8:]
            move = recent[-1]["close"] / max(0.01, recent[0]["close"]) - 1
            if abs(move) > 0.0012:
                side = "buy" if move > 0 else "sell"
                engine.submit_order(
                    side=side,
                    quantity=random.uniform(8, 55),
                    order_type="market",
                    owner=self.owner,
                    agent_type=self.agent_type,
                )
                submitted_sides.append(side)
        if submitted_sides:
            unique_sides = sorted(set(submitted_sides))
            action = "quote"
            if unique_sides == ["buy"]:
                action = "buy"
            elif unique_sides == ["sell"]:
                action = "sell"
            elif len(unique_sides) > 1:
                action = "buy/sell"
            self.record_action(engine, action, side="/".join(unique_sides), quote_width=round(width, 5))
        else:
            self.record_action(engine, "hold", reason="inventory limit")


@dataclass
class RandomTrader(BaseAgent):
    activity_rate: float = field(default_factory=lambda: random.uniform(0.035, 0.105))
    average_size: float = field(default_factory=lambda: random.uniform(15, 130))

    def attach(self, engine: MatchingEngine) -> None:
        super().attach(engine)
        account = engine.accounts[self.owner]
        account.extra.update(
            {
                "activity_rate": round(self.activity_rate, 3),
                "average_size": round(self.average_size, 2),
                "style": "noise flow",
            }
        )

    def act(self, engine: MatchingEngine) -> None:
        if random.random() > self.activity_rate:
            self.record_action(engine, "hold", reason="idle noise trader")
            return

        side = random.choice(["buy", "sell"])
        quantity = max(1.0, random.expovariate(1 / self.average_size))
        if random.random() < 0.72:
            result = engine.submit_order(
                side=side,
                quantity=quantity,
                order_type="market",
                owner=self.owner,
                agent_type=self.agent_type,
            )
            self.record_action(
                engine,
                "buy" if side == "buy" else "sell",
                side=side,
                order_type="market",
                last_quantity=round(quantity, 4),
                last_filled=round(result["filled_quantity"], 4),
            )
        else:
            offset = random.uniform(0.0005, 0.009)
            price = engine.reference_mid_price * (1 - offset if side == "buy" else 1 + offset)
            result = engine.submit_order(
                side=side,
                quantity=quantity,
                price=price,
                order_type="limit",
                owner=self.owner,
                agent_type=self.agent_type,
                ttl_ticks=random.randint(20, 80),
            )
            self.record_action(
                engine,
                "buy" if side == "buy" else "sell",
                side=side,
                order_type="limit",
                last_quantity=round(quantity, 4),
                last_filled=round(result["filled_quantity"], 4),
            )


class MarketSimulator:
    def __init__(
        self,
        symbol: str = "SIM",
        start_price: float = 100.0,
        tick_interval: float = 0.05,
        *,
        scenario: str = "default",
        seed: int | None = None,
        config_path: str | None = None,
    ) -> None:
        if seed is not None:
            random.seed(seed)
        self.seed = seed
        self.config = build_config(scenario=scenario, config_path=config_path)
        self.engine = MatchingEngine(symbol=symbol, start_price=start_price, config=self.config)
        self.tick_interval = tick_interval
        self.interval_jitter = 0.45
        self.last_loop_sleep = tick_interval
        self.running = True
        self.started_at = time.time()
        self.lock = threading.RLock()
        self._stop = threading.Event()
        self.agents: list[BaseAgent] = self._create_agents()
        for agent in self.agents:
            agent.attach(self.engine)
        self.thread = threading.Thread(target=self._loop, name="market-simulator", daemon=True)
        self.thread.start()

    def buy(
        self,
        quantity: float,
        *,
        price: float | None = None,
        order_type: str = "market",
        user_name: str | None = None,
        time_in_force: str = "gtc",
        post_only: bool = False,
        stop_price: float | None = None,
    ) -> dict[str, Any]:
        with self.lock:
            owner = self._normalize_api_user(user_name)
            self.ensure_account(owner)
            return self.engine.submit_order(
                side="buy",
                quantity=quantity,
                price=price,
                order_type=order_type,
                owner=owner,
                agent_type="api-user",
                time_in_force=time_in_force,
                post_only=post_only,
                stop_price=stop_price,
            )

    def sell(
        self,
        quantity: float,
        *,
        price: float | None = None,
        order_type: str = "market",
        user_name: str | None = None,
        time_in_force: str = "gtc",
        post_only: bool = False,
        stop_price: float | None = None,
    ) -> dict[str, Any]:
        with self.lock:
            owner = self._normalize_api_user(user_name)
            self.ensure_account(owner)
            return self.engine.submit_order(
                side="sell",
                quantity=quantity,
                price=price,
                order_type=order_type,
                owner=owner,
                agent_type="api-user",
                time_in_force=time_in_force,
                post_only=post_only,
                stop_price=stop_price,
            )

    def submit_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        side = str(payload.get("side", "")).lower()
        user_name = self._payload_user(payload)
        if side == "buy":
            return self.buy(
                float(payload.get("quantity", 0)),
                price=self._optional_float(payload.get("price")),
                order_type=str(payload.get("order_type", payload.get("type", "market"))),
                user_name=user_name,
                time_in_force=str(payload.get("time_in_force", payload.get("tif", "gtc"))),
                post_only=bool(payload.get("post_only", False)),
                stop_price=self._optional_float(payload.get("stop_price")),
            )
        if side == "sell":
            return self.sell(
                float(payload.get("quantity", 0)),
                price=self._optional_float(payload.get("price")),
                order_type=str(payload.get("order_type", payload.get("type", "market"))),
                user_name=user_name,
                time_in_force=str(payload.get("time_in_force", payload.get("tif", "gtc"))),
                post_only=bool(payload.get("post_only", False)),
                stop_price=self._optional_float(payload.get("stop_price")),
            )
        raise ValueError("side must be 'buy' or 'sell'")

    def ensure_account(self, user_name: str, starting_cash: float | None = None) -> dict[str, Any]:
        with self.lock:
            owner = self._normalize_api_user(user_name)
            cash = float(starting_cash if starting_cash is not None else self.config.get("api_starting_cash", 1_000_000.0))
            account = self.engine.register_account(owner, "api-user", cash=cash)
            return self.engine.account_summary(account.owner)

    def fund_account(self, user_name: str, amount: float) -> dict[str, Any]:
        with self.lock:
            owner = self._normalize_api_user(user_name)
            return self.engine.add_account_funds(owner, amount, agent_type="api-user")

    def list_accounts(self) -> list[dict[str, Any]]:
        with self.lock:
            return self.engine.snapshot()["api_users"]

    def account(self, user_name: str) -> dict[str, Any]:
        with self.lock:
            return self.engine.account_summary(self._normalize_api_user(user_name))

    def cancel_order(self, order_id: str, user_name: str | None = None) -> dict[str, Any]:
        with self.lock:
            owner = self._normalize_api_user(user_name) if user_name else None
            return self.engine.cancel_order(order_id, owner=owner)

    def list_orders(
        self,
        *,
        user_name: str | None = None,
        status: str | None = None,
        include_internal: bool = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        with self.lock:
            owner = self._normalize_api_user(user_name) if user_name else None
            return self.engine.list_orders(owner=owner, status=status, include_internal=include_internal, limit=limit)

    def set_running(self, running: bool) -> dict[str, Any]:
        with self.lock:
            self.running = bool(running)
            return self.snapshot()

    def reset(self) -> dict[str, Any]:
        with self.lock:
            if self.seed is not None:
                random.seed(self.seed)
            self.engine.reset()
            self.started_at = time.time()
            self.agents = self._create_agents()
            for agent in self.agents:
                agent.attach(self.engine)
            self.running = True
            return self.snapshot()

    def clear_api_users(self) -> dict[str, Any]:
        with self.lock:
            count = self.engine.clear_api_users()
            return {"cleared": count, "ok": True}

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            state = self.engine.snapshot()
            counts: dict[str, int] = {}
            for agent in self.agents:
                counts[agent.agent_type] = counts.get(agent.agent_type, 0) + 1
            state.update(
                {
                    "running": self.running,
                    "tick_interval": self.tick_interval,
                    "last_loop_sleep": round(self.last_loop_sleep, 4),
                    "uptime_seconds": round(time.time() - self.started_at, 2),
                    "agent_counts": counts,
                    "scenario": self.config.get("scenario", "default"),
                    "seed": self.seed,
                    "risk_limits": {
                        "max_order_quantity": self.config.get("max_order_quantity"),
                        "max_position_abs": self.config.get("max_position_abs"),
                        "allow_short": self.config.get("allow_short"),
                        "enforce_risk_limits": self.config.get("enforce_risk_limits"),
                    },
                }
            )
            return state

    def stop(self) -> None:
        self._stop.set()
        self.thread.join(timeout=2)

    def _loop(self) -> None:
        while not self._stop.is_set():
            started = time.perf_counter()
            with self.lock:
                if self.running:
                    self._step()
            elapsed = time.perf_counter() - started
            jitter = random.uniform(1 - self.interval_jitter, 1 + self.interval_jitter)
            self.last_loop_sleep = max(0.01, self.tick_interval * jitter - elapsed)
            time.sleep(self.last_loop_sleep)

    def _step(self) -> None:
        self.engine.advance_environment()
        shuffled_agents = self.agents[:]
        random.shuffle(shuffled_agents)
        for agent in shuffled_agents:
            if not agent.ready(self.engine.tick):
                continue
            try:
                agent.act(self.engine)
            except Exception as exc:  # Keep one bad strategy from killing the simulation loop.
                account = self.engine.accounts.get(agent.owner)
                if account is not None:
                    account.extra["last_error"] = str(exc)
            finally:
                agent.schedule_next(self.engine.tick)
        self.engine.record_history()

    def _create_agents(self) -> list[BaseAgent]:
        agents: list[BaseAgent] = []
        counts = self.config.get("agent_counts", {})
        institutional_count = self._rand_range(counts.get("institutional", [3, 6]))
        hft_count = self._rand_range(counts.get("high_frequency", [6, 14]))
        random_count = self._rand_range(counts.get("random", [24, 46]))

        for index in range(institutional_count):
            agents.append(
                InstitutionalTrader(
                    owner=f"institution-{index + 1}",
                    agent_type="institutional",
                    cash=10_000,
                    min_interval_ticks=random.randint(2, 5),
                    max_interval_ticks=random.randint(6, 14),
                )
            )
        for index in range(hft_count):
            agents.append(
                HighFrequencyTrader(
                    owner=f"market-maker-{index + 1}",
                    agent_type="high-frequency",
                    cash=10_000,
                    min_interval_ticks=1,
                    max_interval_ticks=random.randint(1, 3),
                )
            )
        for index in range(random_count):
            agents.append(
                RandomTrader(
                    owner=f"random-{index + 1}",
                    agent_type="random",
                    cash=10_000,
                    min_interval_ticks=random.randint(3, 9),
                    max_interval_ticks=random.randint(10, 28),
                )
            )
        for agent in agents:
            agent.schedule_next(0)
        return agents

    @staticmethod
    def _optional_float(value: Any) -> float | None:
        if value in (None, ""):
            return None
        return float(value)

    @staticmethod
    def _rand_range(value: Any) -> int:
        if isinstance(value, list) and len(value) == 2:
            return random.randint(int(value[0]), int(value[1]))
        return int(value)

    @staticmethod
    def _payload_user(payload: dict[str, Any]) -> str | None:
        for key in ("user", "user_name", "username", "api_user", "client", "client_id", "model", "owner", "name"):
            value = payload.get(key)
            if value not in (None, ""):
                return str(value)
        return None

    @staticmethod
    def _normalize_api_user(user_name: str | None) -> str:
        raw = str(user_name or "anonymous-api-user").strip()
        cleaned = "".join(char if char.isalnum() or char in " ._-:@" else "-" for char in raw)
        cleaned = " ".join(cleaned.split())
        return cleaned[:64] or "anonymous-api-user"
