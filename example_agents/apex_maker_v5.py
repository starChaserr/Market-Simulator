from __future__ import annotations

import argparse
import json
import logging
import statistics
import time
import urllib.parse
import urllib.request
from collections import Counter, deque
from typing import Any

VERSION       = "ApexMaker v5.0"
OPEN_STATUSES = {"open", "partially_filled", "pending_trigger"}
ACCEPTED      = {"open", "partially_filled", "filled"}


# ══ CLI ════════════════════════════════════════════════════════════════════════

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=VERSION)
    p.add_argument("user",               nargs="?",  default="ApexMaker")
    p.add_argument("--url",              default="http://127.0.0.1:8000/api")
    p.add_argument("--starting-cash",    type=float, default=100_000.0)
    p.add_argument("--order-notional",   type=float, default=40_000.0,
                   help="Target notional per order in currency units")
    p.add_argument("--max-notional",     type=float, default=400_000.0,
                   help="Max gross notional exposure (long or short)")
    p.add_argument("--min-spread-bps",   type=float, default=6.0,
                   help="Minimum half-spread in bps of mid price")
    p.add_argument("--target-loop-ms",   type=float, default=500.0,
                   help="Target loop period ms (adapts around API latency)")
    # Risk
    p.add_argument("--drawdown-limit",   type=float, default=0.15)
    p.add_argument("--profit-lock",      type=float, default=0.12)
    # InventoryManager
    p.add_argument("--unwind-threshold", type=float, default=0.75,
                   help="Fraction of max_pos that starts unwind timer")
    p.add_argument("--unwind-age",       type=float, default=45.0,
                   help="Seconds at threshold before UnwindMode fires")
    p.add_argument("--unwind-spread-mult", type=float, default=1.5,
                   help="Spread multiplier for aggressive unwind quotes")
    # MomentumGuard
    p.add_argument("--momentum-window",  type=int,   default=8)
    p.add_argument("--momentum-thresh",  type=float, default=0.70,
                   help="Fraction of window same direction = trending")
    # FeeBrake
    p.add_argument("--fee-burn-limit",   type=float, default=0.0030,
                   help="Max fees as fraction of equity per 60s")
    p.add_argument("--fee-brake-pause",  type=float, default=30.0,
                   help="Pause seconds after fee brake fires")
    # Requote
    p.add_argument("--requote-ticks",    type=int,   default=3,
                   help="Min tick move before requoting")
    # Sweeps (off by default)
    p.add_argument("--sweeps",           action="store_true", default=False)
    p.add_argument("--sweep-fee-mult",   type=float, default=5.0,
                   help="Required edge as multiple of taker fee")
    p.add_argument("--sweep-cooldown",   type=float, default=2.0)
    # Caps
    p.add_argument("--max-orders",       type=int,   default=5000)
    p.add_argument("--order-budget",     type=int,   default=60,
                   help="Max orders per 60s rolling window")
    return p.parse_args()


# ══ API ════════════════════════════════════════════════════════════════════════

def call_api(
    api_url: str,
    path: str,
    method: str = "GET",
    data: dict[str, Any] | None = None,
    timeout: float = 3.0,
) -> tuple[dict[str, Any] | None, float]:
    req  = urllib.request.Request(f"{api_url.rstrip('/')}{path}", method=method)
    body = None
    if data is not None:
        req.add_header("Content-Type", "application/json")
        body = json.dumps(data).encode()
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, data=body, timeout=timeout) as r:
            return json.loads(r.read().decode()), time.monotonic() - t0
    except Exception as exc:
        logging.debug("API %s %s -> %s", method, path, exc)
        return None, time.monotonic() - t0


def upath(path: str, user: str) -> str:
    sep = "&" if "?" in path else "?"
    return f"{path}{sep}user={urllib.parse.quote(user)}"


def api_get(url: str, path: str, user: str) -> tuple[dict | None, float]:
    return call_api(url, upath(path, user))


# ══ Math helpers ═══════════════════════════════════════════════════════════════

def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(v, hi))

def pct(newer: float, older: float) -> float:
    return (newer / older - 1.0) if older > 0 else 0.0

def bps_abs(bps: float, mid: float) -> float:
    return mid * bps / 10_000.0

def notional_units(notional: float, price: float) -> float:
    return notional / price if price > 0 else 0.0

def book_vol(levels: list[dict], depth: int = 6) -> float:
    return sum(float(lv.get("quantity", 0)) for lv in levels[:depth])


# ══ TickDetector ══════════════════════════════════════════════════════════════

class TickDetector:
    """Infers minimum price increment from live order book gaps."""

    STANDARD = [0.001, 0.005, 0.01, 0.02, 0.05, 0.10, 0.25,
                0.50, 1.0, 2.0, 5.0, 10.0, 25.0, 50.0, 100.0]

    def __init__(self) -> None:
        self._hist: deque[float] = deque(maxlen=30)
        self.tick: float         = 0.05   # NSE-safe fallback

    def _snap(self, raw: float) -> float:
        return min(self.STANDARD, key=lambda t: abs(t - raw))

    def update(self, state: dict[str, Any]) -> None:
        gaps: list[float] = []
        for side in ("bids", "asks"):
            prices = [float(lv["price"])
                      for lv in state.get("order_book", {}).get(side, [])
                      if lv.get("price") is not None]
            for i in range(len(prices) - 1):
                g = abs(prices[i] - prices[i + 1])
                if g > 0:
                    gaps.append(g)
        if not gaps:
            return
        snapped = self._snap(min(gaps))
        self._hist.append(snapped)
        mode = Counter(self._hist).most_common(1)[0][0]
        if mode != self.tick:
            logging.info("TickDetector: %.5f -> %.5f", self.tick, mode)
        self.tick = mode


# ══ LatencyMonitor ════════════════════════════════════════════════════════════

class LatencyMonitor:
    def __init__(self, target_ms: float) -> None:
        self._target = target_ms / 1000.0
        self._samples: deque[float] = deque(maxlen=30)
        self.avg_rtt = 0.10

    def record(self, elapsed: float) -> None:
        self._samples.append(elapsed)
        if self._samples:
            self.avg_rtt = statistics.mean(self._samples)

    def sleep(self, loop_elapsed: float) -> float:
        return max(0.05, self._target - loop_elapsed)


# ══ MomentumGuard — detects trending via price deltas NOT vol field ════════════

class MomentumGuard:
    """
    Fixes the VolGuard blindspot: smooth trends have LOW vol but HIGH
    directional consistency. This tracks consecutive price move directions.

    If >= momentum_thresh fraction of the last N moves share a direction
    -> trending = True. Sweeps blocked. Width inflated. Inventory fades trend.
    """

    def __init__(self, window: int = 8, threshold: float = 0.70) -> None:
        self._window    = window
        self._threshold = threshold
        self._deltas:  deque[int] = deque(maxlen=window)
        self._last_mid  = 0.0
        self.trending   = False
        self.direction  = 0   # +1 up, -1 down

    def update(self, mid: float) -> None:
        if self._last_mid > 0:
            d = mid - self._last_mid
            self._deltas.append(1 if d > 1e-9 else (-1 if d < -1e-9 else 0))
        self._last_mid = mid

        n = len(self._deltas)
        if n < max(3, self._window // 2):
            self.trending  = False
            self.direction = 0
            return

        ups   = self._deltas.count(1)
        downs = self._deltas.count(-1)
        ratio = max(ups, downs) / n
        was   = self.trending

        self.trending  = ratio >= self._threshold
        self.direction = (1 if ups > downs else -1) if self.trending else 0

        if self.trending and not was:
            logging.info("MomentumGuard TRENDING %s (%.0f%% of %d bars)",
                         "UP" if self.direction > 0 else "DOWN", ratio * 100, n)
        elif not self.trending and was:
            logging.info("MomentumGuard: trend cleared")


# ══ InventoryManager — age-out timer + UnwindMode ════════════════════════════

class InventoryManager:
    """
    Fires UnwindMode when inventory stays >= unwind_threshold * max_pos
    for >= unwind_age seconds. In UnwindMode only closing orders are
    posted and quotes are shifted aggressively toward exit.

    This is the direct fix for RELIANCE always ending at -600 units.
    """

    def __init__(self, threshold: float, age_s: float, spread_mult: float) -> None:
        self._threshold   = threshold
        self._age         = age_s
        self._spread_mult = spread_mult
        self._over_since: float | None = None
        self.unwind_mode  = False
        self.direction    = 0   # +1 = need to buy back short, -1 = need to sell long

    def update(self, inv: float, max_pos: float) -> None:
        ratio = abs(inv) / max(max_pos, 1.0)
        over  = ratio >= self._threshold

        if over:
            if self._over_since is None:
                self._over_since = time.time()
                logging.warning("InventoryManager: %.0f%% exposure — unwind timer started",
                                ratio * 100)
            elif not self.unwind_mode and (time.time() - self._over_since) >= self._age:
                self.unwind_mode = True
                self.direction   = 1 if inv < 0 else -1
                logging.warning("InventoryManager: UNWIND MODE inv=%.1f max=%.1f dir=%s",
                                inv, max_pos, "BUY" if self.direction > 0 else "SELL")
        else:
            if ratio < 0.50:
                if self.unwind_mode:
                    logging.info("InventoryManager: unwind cleared (%.0f%% exposure)", ratio * 100)
                self.unwind_mode = False
                self._over_since = None
                self.direction   = 0
            elif self.unwind_mode:
                self.direction = 1 if inv < 0 else -1

    def allow_buy(self, inv: float, max_pos: float) -> bool:
        if not self.unwind_mode:
            return inv < max_pos
        return self.direction > 0

    def allow_sell(self, inv: float, max_pos: float) -> bool:
        if not self.unwind_mode:
            return inv > -max_pos
        return self.direction < 0

    def adjust_quotes(self, bid: float, ask: float,
                      width: float, tick: float, inv: float) -> tuple[float, float]:
        """Shift quotes aggressively toward the closing side in UnwindMode."""
        if not self.unwind_mode:
            return bid, ask
        push = width * self._spread_mult
        if self.direction > 0:    # short -> buy back
            bid = round(min(bid + push, ask - tick), 4)
        else:                      # long -> sell off
            ask = round(max(ask - push, bid + tick), 4)
        return bid, ask


# ══ FeeBrake — rolling fee burn circuit breaker ════════════════════════════════

class FeeBrake:
    """
    Tracks estimated fees in a rolling 60s window.
    If fees > fee_burn_limit * equity -> pause all orders for fee_brake_pause s.

    Would have caught TCS S1 within ~20 seconds of the sweep cascade starting.
    """

    def __init__(self, limit_frac: float, pause_s: float) -> None:
        self._limit        = limit_frac
        self._pause        = pause_s
        self._events:      deque[tuple[float, float]] = deque()
        self._paused_until = 0.0
        self.braked        = False

    def record(self, fee_est: float) -> None:
        self._events.append((time.time(), fee_est))

    def _trim(self) -> None:
        cutoff = time.time() - 60.0
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()

    def check(self, equity: float) -> bool:
        now = time.time()
        if now < self._paused_until:
            self.braked = True
            return False
        self._trim()
        window_fees = sum(f for _, f in self._events)
        limit       = max(equity * self._limit, 1.0)
        if window_fees >= limit:
            self._paused_until = now + self._pause
            self.braked        = True
            logging.warning("FeeBrake FIRED: %.2f fees/60s vs limit %.2f — pausing %.0fs",
                            window_fees, limit, self._pause)
            return False
        if self.braked:
            logging.info("FeeBrake cleared")
            self.braked = False
        return True


# ══ SweepGuard — 5-condition gate replacing blind IOC ════════════════════════

class SweepGuard:
    """
    ALL five conditions must pass before an IOC sweep is allowed:
      1. --sweeps flag enabled (off by default)
      2. MomentumGuard NOT trending
      3. FeeBrake NOT active
      4. abs(inventory) < 40% of max_pos
      5. edge > sweep_fee_mult * taker_fee
      6. cooldown elapsed

    Conditions 2 and 4 alone would have blocked every single TCS S1 sweep.
    """

    def __init__(self, enabled: bool, fee_mult: float, cooldown: float) -> None:
        self._enabled   = enabled
        self._fee_mult  = fee_mult
        self._cooldown  = cooldown
        self._last      = 0.0

    def allowed(self, momentum: MomentumGuard, brake: FeeBrake,
                inv: float, max_pos: float,
                edge: float, taker_fee: float, equity: float) -> bool:
        if not self._enabled:                          return False
        if momentum.trending:                          return False
        if not brake.check(equity):                    return False
        if abs(inv) > 0.40 * max_pos:                 return False
        if edge < taker_fee * self._fee_mult:          return False
        if time.time() - self._last < self._cooldown:  return False
        return True

    def mark(self) -> None:
        self._last = time.time()


# ══ FillTracker ════════════════════════════════════════════════════════════════

class FillTracker:
    def __init__(self, window: int = 20) -> None:
        self._fills: deque[str] = deque(maxlen=window)

    def record(self, side: str) -> None:
        self._fills.append(side)

    def adverse_score(self) -> float:
        n = len(self._fills)
        if n < 5:
            return 0.0
        buys = self._fills.count("buy")
        return max(buys, n - buys) / n


# ══ OrderBudget ════════════════════════════════════════════════════════════════

class OrderBudget:
    def __init__(self, budget: int = 60) -> None:
        self._budget     = budget
        self._timestamps: deque[float] = deque()

    def _trim(self) -> None:
        cutoff = time.time() - 60.0
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()

    def can_order(self) -> bool:
        self._trim()
        return len(self._timestamps) < self._budget

    def record(self, n: int = 1) -> None:
        now = time.time()
        for _ in range(n):
            self._timestamps.append(now)

    def used(self) -> int:
        self._trim()
        return len(self._timestamps)


# ══ Microprice ════════════════════════════════════════════════════════════════

def microprice(state: dict[str, Any]) -> float:
    bb  = float(state["best_bid"])
    ba  = float(state["best_ask"])
    bv  = book_vol(state.get("order_book", {}).get("bids", []))
    av  = book_vol(state.get("order_book", {}).get("asks", []))
    tot = bv + av
    if tot <= 0:
        return float(state["mid_price"])
    return (ba * bv + bb * av) / tot


# ══ Regime Engine (5 regimes, leaner than v4) ═════════════════════════════════

def compute_signals(state: dict[str, Any], momentum: MomentumGuard) -> dict[str, Any]:
    """
    MomentumGuard informs regime directly — if it sees trending price action
    the regime is set to 'trend' regardless of vol/stress thresholds.
    This stops misclassifying smooth RELIANCE/TCS uptrends as 'calm'.
    """
    hist   = state.get("history", [])
    mid    = float(state["mid_price"])
    fund   = float(state.get("fundamental_price", mid))
    vol    = max(float(state.get("volatility", 0.0)), 0.0)
    disloc = pct(fund, mid)

    s_mom = m_mom = shock = 0.0
    if len(hist) >= 2:
        def hval(idx: int) -> float:
            pt = hist[idx]
            for k in ("close", "mark", "mid", "last"):
                if pt.get(k) is not None:
                    return float(pt[k])
            return mid

        last     = hval(-1)
        shock    = pct(last, hval(-2))
        anchor_s = hval(max(-6,  -len(hist)))
        anchor_m = hval(max(-18, -len(hist)))
        s_mom    = pct(last, anchor_s)
        m_mom    = pct(last, anchor_m)

    stress = max(abs(disloc), abs(shock) * 1.5, vol * 1.4)
    trend  = 0.55 * m_mom + 0.45 * s_mom

    if   stress > 0.014:                              regime = "shock"
    elif stress > 0.007:                              regime = "volatile"
    elif momentum.trending or abs(trend) > 0.003:     regime = "trend"
    elif vol < 0.003 and abs(disloc) < 0.002:         regime = "calm"
    else:                                             regime = "drift"

    return dict(
        regime=regime, disloc=disloc, trend=trend,
        s_mom=s_mom, vol=vol, stress=stress, shock=shock,
    )


# ══ Fair Value (3 signals — simpler than v4, less noise) ══════════════════════

def fair_value(state: dict[str, Any], sig: dict[str, Any]) -> float:
    mid   = float(state["mid_price"])
    fund  = float(state.get("fundamental_price", mid))
    micro = microprice(state)
    bids  = state.get("order_book", {}).get("bids", [])
    asks  = state.get("order_book", {}).get("asks", [])
    bv    = book_vol(bids)
    av    = book_vol(asks)
    imbal = (bv - av) / (bv + av + 1e-9)

    # In trend: lean hard on fundamental — don't chase price
    weights = {
        "shock":    (0.75, 0.05),
        "volatile": (0.58, 0.12),
        "trend":    (0.52, 0.13),
        "drift":    (0.25, 0.35),
        "calm":     (0.10, 0.50),
    }
    fw, mw = weights.get(sig["regime"], (0.25, 0.35))
    midw   = max(0.0, 1.0 - fw - mw)
    return fund * fw + micro * mw + mid * midw + imbal * mid * 0.0005


# ══ Quote Width ════════════════════════════════════════════════════════════════

def quote_width(state: dict[str, Any], sig: dict[str, Any],
                args: argparse.Namespace, tick: float,
                momentum: MomentumGuard) -> float:
    mid    = float(state["mid_price"])
    spread = max(float(state.get("spread", 0.0)), tick)
    vol    = sig["vol"]
    stress = sig["stress"]
    floor  = bps_abs(args.min_spread_bps, mid)
    base   = mid * (0.00020 + min(vol, 0.025) * 1.35 + min(stress, 0.04) * 0.10)

    mults = {
        "calm":     (0.80, 0.82),
        "drift":    (1.10, 1.05),
        "trend":    (1.55, 1.20),
        "volatile": (2.00, 1.30),
        "shock":    (2.80, 1.50),
    }
    bm, sm = mults.get(sig["regime"], (1.10, 1.05))
    w = max(floor * bm, spread * sm, base)
    # Belt-and-suspenders: extra width if MomentumGuard sees trend
    return w * (1.30 if momentum.trending else 1.0)


# ══ Target Inventory — fades trend rather than following it ═══════════════════

def target_inv(sig: dict[str, Any], max_pos: float,
               momentum: MomentumGuard) -> float:
    """
    Key fix: in trending markets lean AGAINST the trend direction.
    v4 followed momentum, building the directional position that trapped us.
    Now: trending UP -> slight short lean; trending DOWN -> slight long lean.
    """
    d, t, s = sig["disloc"], sig["trend"], sig["s_mom"]
    regime   = sig["regime"]

    if   regime == "shock":    raw = d / 0.007
    elif regime == "calm":     raw = (d / 0.015 + t / 0.006) * 0.25
    elif regime == "volatile": raw = d / 0.009 + t / 0.007
    else:                      raw = d / 0.012 + t / 0.006 + s / 0.012

    if momentum.trending and momentum.direction != 0:
        fade = -momentum.direction * 0.20
        raw  = raw * 0.50 + fade

    return clamp(raw, -0.90, 0.90) * max_pos


# ══ Quote Sizes ════════════════════════════════════════════════════════════════

def quote_sizes(
    account: dict[str, Any],
    args: argparse.Namespace,
    bid_price: float,
    tgt: float,
    max_pos: float,
    inv_mgr: InventoryManager,
) -> tuple[float, float]:
    inv   = float(account.get("inventory", 0))
    cash  = float(account.get("cash", 0))
    lean  = clamp((tgt - inv) / max(max_pos, 1.0), -1.0, 1.0)
    base  = notional_units(args.order_notional, bid_price)

    buy_sz  = base * (1.0 + max(lean,  0.0) * 1.40 - max(-lean, 0.0) * 0.55)
    sell_sz = base * (1.0 + max(-lean, 0.0) * 1.40 - max(lean,  0.0) * 0.55)

    buy_cap  = (max_pos - inv) if inv_mgr.allow_buy(inv,  max_pos) else 0.0
    sell_cap = (max_pos + inv) if inv_mgr.allow_sell(inv, max_pos) else 0.0
    cash_cap = cash * 0.95 / max(bid_price, 0.01)

    return (
        clamp(buy_sz,  0.0, min(buy_cap, cash_cap)),
        clamp(sell_sz, 0.0, sell_cap),
    )


# ══ Order helpers ══════════════════════════════════════════════════════════════

def cancel_all(api_url: str, user: str) -> None:
    resp, _ = call_api(api_url, upath("/orders", user))
    for o in (resp or {}).get("orders", []):
        if o.get("status") in OPEN_STATUSES:
            call_api(api_url, upath(f"/orders/{o['order_id']}", user), method="DELETE")


def post_limit(api_url: str, user: str, side: str,
               qty: float, price: float) -> tuple[bool, float]:
    r, _ = call_api(api_url, "/order", "POST", {
        "side": side, "quantity": round(qty, 4),
        "order_type": "limit", "price": round(price, 4),
        "user": user, "post_only": True,
    })
    if not r or r.get("status") not in ACCEPTED:
        if r:
            logging.info("POST %s %.4f rejected: %s", side, price,
                         r.get("reject_reason", r.get("status")))
        return False, 0.0
    return True, qty * price * 0.0003   # estimated maker fee


def sweep_ioc(api_url: str, user: str, side: str,
              qty: float, price: float) -> tuple[bool, float]:
    r, _ = call_api(api_url, "/order", "POST", {
        "side": side, "quantity": round(qty, 4),
        "order_type": "limit", "time_in_force": "ioc",
        "price": round(price, 4), "user": user,
    })
    if not r:
        return False, 0.0
    ok = r.get("status") in ACCEPTED or float(r.get("filled_quantity", 0)) > 0
    return ok, (qty * price * 0.0006 if ok else 0.0)   # taker ~2x maker


# ══ Main Loop ════════════════════════════════════════════════════════════════

def run(args: argparse.Namespace) -> int:
    target_s = args.target_loop_ms / 1000.0
    logging.info(
        "%s | user=%s notional=%.0f max=%.0f spread=%.1fbps loop=%.0fms sweeps=%s",
        VERSION, args.user, args.order_notional, args.max_notional,
        args.min_spread_bps, args.target_loop_ms, "ON" if args.sweeps else "OFF",
    )
    call_api(args.url, "/accounts", "POST",
             {"user": args.user, "starting_cash": args.starting_cash})

    tick_det  = TickDetector()
    lat_mon   = LatencyMonitor(args.target_loop_ms)
    momentum  = MomentumGuard(args.momentum_window, args.momentum_thresh)
    inv_mgr   = InventoryManager(args.unwind_threshold, args.unwind_age,
                                 args.unwind_spread_mult)
    fee_brake = FeeBrake(args.fee_burn_limit, args.fee_brake_pause)
    sweep_grd = SweepGuard(args.sweeps, args.sweep_fee_mult, args.sweep_cooldown)
    fills     = FillTracker(20)
    budget    = OrderBudget(args.order_budget)

    last_bid     = last_ask = 0.0
    last_regime  = ""
    last_inv     = 0.0
    consec_fills = 0.0
    cool_until   = 0.0
    loop_n       = 0

    try:
        while True:
            loop_start = time.monotonic()
            loop_n    += 1

            state,   t1 = api_get(args.url, "/state",   args.user)
            account, t2 = api_get(args.url, "/account", args.user)
            lat_mon.record((t1 + t2) / 2)

            if not state or not account:
                time.sleep(1.0)
                continue

            if int(account.get("orders", 0)) >= args.max_orders:
                logging.warning("Lifetime cap %d — shutting down", args.max_orders)
                cancel_all(args.url, args.user)
                return 0

            initial = max(float(account.get("initial_cash", args.starting_cash)), 1.0)
            equity  = float(account.get("equity", initial))

            if equity < initial * (1.0 - args.drawdown_limit):
                logging.error("Drawdown limit: equity=%.2f initial=%.2f", equity, initial)
                cancel_all(args.url, args.user)
                return 1

            if any(state.get(k) is None for k in ("mid_price", "best_bid", "best_ask")):
                time.sleep(target_s)
                continue

            mid = float(state["mid_price"])

            # Profit lock
            max_pos = notional_units(args.max_notional, mid)
            if equity > initial * (1.0 + args.profit_lock):
                max_pos *= 0.50
                logging.info("Profit lock: max_pos=%.1f", max_pos)

            # Update subsystems
            tick_det.update(state)
            tick = tick_det.tick
            momentum.update(mid)

            # Fill velocity cooldown
            inv = float(account.get("inventory", 0))
            if abs(inv - last_inv) > 1e-9:
                fills.record("buy" if inv > last_inv else "sell")
                consec_fills += 1
                if consec_fills >= 5:
                    logging.warning("Rapid fills — cooling 5s")
                    cool_until   = time.time() + 5.0
                    consec_fills = 0.0
            else:
                consec_fills = max(0.0, consec_fills - 0.4)
            last_inv = inv

            if time.time() < cool_until:
                cancel_all(args.url, args.user)
                time.sleep(1.0)
                continue

            inv_mgr.update(inv, max_pos)

            # Core calculations
            sig    = compute_signals(state, momentum)
            fv     = fair_value(state, sig)
            width  = quote_width(state, sig, args, tick, momentum)
            regime = sig["regime"]

            adv = fills.adverse_score()
            if adv > 0.75:
                width *= 1.0 + (adv - 0.75) * 3.0
                logging.info("Adverse %.2f -> width inflated", adv)

            tgt      = clamp(target_inv(sig, max_pos, momentum), -max_pos, max_pos)
            inv_gap  = clamp((inv - tgt) / max(max_pos, 1.0), -2.0, 2.0)
            inv_skew = inv_gap * width * 1.70

            # Build raw quotes
            bb = float(state["best_bid"])
            ba = float(state["best_ask"])
            bid = round(min(fv - width / 2.0 - inv_skew, ba - tick), 4)
            ask = round(max(fv + width / 2.0 - inv_skew, bb + tick), 4)

            if bid <= 0.0 or ask <= 0.0 or bid >= ask:
                time.sleep(lat_mon.sleep(time.monotonic() - loop_start))
                continue

            # InventoryManager adjusts quotes in UnwindMode
            bid, ask = inv_mgr.adjust_quotes(bid, ask, width, tick, inv)

            # IOC sweep — only if SweepGuard clears all 5 conditions
            taker_fee = float(state.get("fees", {}).get("taker_fee_rate", 0.0003))
            buy_edge  = (fv - ba) / max(mid, 0.01)
            sell_edge = (bb - fv) / max(mid, 0.01)
            best_edge = max(buy_edge, sell_edge)

            if sweep_grd.allowed(momentum, fee_brake, inv, max_pos,
                                 best_edge, taker_fee, equity):
                sweep_side  = "buy" if buy_edge > sell_edge else "sell"
                sweep_price = ba   if sweep_side == "buy"   else bb
                sweep_qty   = min(
                    notional_units(args.order_notional, mid),
                    (max_pos - abs(inv)) * 0.40,
                )
                if sweep_qty >= 1.0:
                    ok, fee_est = sweep_ioc(args.url, args.user,
                                            sweep_side, sweep_qty, sweep_price)
                    if ok:
                        fee_brake.record(fee_est)
                        budget.record(1)
                        sweep_grd.mark()
                        acc2, _ = api_get(args.url, "/account", args.user)
                        account  = acc2 or account
                        inv      = float(account.get("inventory", inv))
                        inv_mgr.update(inv, max_pos)

            # Requote decision
            requote_min = tick * args.requote_ticks
            should_requote = (
                (abs(bid - last_bid) > requote_min
                 or abs(ask - last_ask) > requote_min
                 or regime != last_regime
                 or adv > 0.75
                 or inv_mgr.unwind_mode)
                and budget.can_order()
                and fee_brake.check(equity)
            )

            if should_requote:
                cancel_all(args.url, args.user)
                buy_sz, sell_sz = quote_sizes(account, args, bid, tgt, max_pos, inv_mgr)

                placed = fee_total = 0.0
                if buy_sz >= 1.0:
                    ok, fee = post_limit(args.url, args.user, "buy", buy_sz, bid)
                    placed += int(ok); fee_total += fee
                if sell_sz >= 1.0:
                    ok, fee = post_limit(args.url, args.user, "sell", sell_sz, ask)
                    placed += int(ok); fee_total += fee

                if placed:
                    budget.record(int(placed))
                    fee_brake.record(fee_total)

                last_bid    = bid
                last_ask    = ask
                last_regime = regime

            if loop_n % 60 == 0:
                logging.info(
                    "| regime=%-8s inv=%+.1f tgt=%+.1f unwind=%-3s "
                    "trend=%-3s budget=%d fee_braked=%-3s equity=%.2f |",
                    regime, inv, tgt,
                    "YES" if inv_mgr.unwind_mode else "no",
                    "YES" if momentum.trending   else "no",
                    budget.used(),
                    "YES" if fee_brake.braked    else "no",
                    equity,
                )

            time.sleep(lat_mon.sleep(time.monotonic() - loop_start))

    finally:
        cancel_all(args.url, args.user)
        logging.info("%s shutdown | avg_rtt=%.0fms orders_used=%d",
                     VERSION, lat_mon.avg_rtt * 1000, budget.used())
    return 0


# ══ Entry ══════════════════════════════════════════════════════════════════════

def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
