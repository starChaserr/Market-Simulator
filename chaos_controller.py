from __future__ import annotations

import argparse
import json
import logging
import math
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Phase:
    name: str
    duration: float
    start_level: float
    end_level: float
    shock_size: float = 0.0


PHASES = (
    Phase("calm", 45.0, 28.0, 28.0),
    Phase("build", 45.0, 28.0, 62.0),
    Phase("storm", 35.0, 82.0, 88.0, 0.035),
    Phase("aftershock", 35.0, 68.0, 58.0, 0.016),
    Phase("recovery", 60.0, 42.0, 30.0),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Operate deterministic, guarded market chaos.")
    parser.add_argument("--url", default="http://127.0.0.1:8000/api")
    parser.add_argument("--seed", type=int, default=7331)
    parser.add_argument("--interval", type=float, default=4.0)
    parser.add_argument("--cycles", type=int, default=0, help="0 runs until stopped.")
    return parser.parse_args()


def call_api(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    request = urllib.request.Request(f"{base_url.rstrip('/')}{path}", method=method)
    body = None
    if payload is not None:
        request.add_header("Content-Type", "application/json")
        request.add_header("X-API-User", "ChaosController")
        body = json.dumps(payload).encode("utf-8")
    try:
        with urllib.request.urlopen(request, data=body, timeout=5.0) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        message = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {path} failed: {exc.code} {message}") from exc
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{method} {path} failed: {exc}") from exc


def guarded_level(target: float, state: dict[str, Any]) -> tuple[float, str]:
    level = target
    reasons: list[str] = []
    realized_volatility = float(state.get("realized_volatility") or 0)
    liquidity_stress = float(state.get("liquidity_stress") or 0)
    mid = max(0.01, float(state.get("mid_price") or state.get("last_price") or 0.01))
    relative_spread = float(state.get("spread") or 0) / mid

    if realized_volatility > 0.014:
        level = min(level, 38)
        reasons.append("volatility brake")
    elif realized_volatility > 0.010:
        level = min(level, 52)
        reasons.append("volatility trim")
    if liquidity_stress > 2.7:
        level = min(level, 35)
        reasons.append("stress brake")
    elif liquidity_stress > 2.2:
        level = min(level, 50)
        reasons.append("stress trim")
    if relative_spread > 0.05:
        level = min(level, 30)
        reasons.append("spread brake")
    elif relative_spread > 0.03:
        level = min(level, 45)
        reasons.append("spread trim")

    return max(0.0, min(100.0, level)), ", ".join(reasons) or "on target"


def phase_target(phase: Phase, elapsed: float) -> float:
    progress = max(0.0, min(1.0, elapsed / max(phase.duration, 0.001)))
    return phase.start_level + (phase.end_level - phase.start_level) * progress


def run(args: argparse.Namespace) -> int:
    rng = random.Random(args.seed)
    cycle = 0
    shock_direction = rng.choice((-1.0, 1.0))
    logging.info("Chaos controller online seed=%d", args.seed)

    while args.cycles <= 0 or cycle < args.cycles:
        for phase in PHASES:
            started = time.monotonic()
            shock_pending = phase.shock_size > 0
            while True:
                elapsed = time.monotonic() - started
                if elapsed >= phase.duration:
                    break
                try:
                    state = call_api(args.url, "/state")
                    target = phase_target(phase, elapsed)
                    level, guard = guarded_level(target, state)
                    shock = None
                    if shock_pending and elapsed >= min(args.interval, phase.duration / 3):
                        shock = shock_direction * phase.shock_size
                        shock_pending = False
                        shock_direction *= -1
                    payload: dict[str, Any] = {
                        "level": round(level, 2),
                        "source": f"controller:{phase.name}",
                    }
                    if shock is not None:
                        payload["shock"] = round(shock, 6)
                    result = call_api(args.url, "/chaos", method="POST", payload=payload)
                    chaos = result.get("chaos", {})
                    logging.info(
                        "phase=%-10s target=%5.1f applied=%5.1f guard=%s shock=%s tick=%s",
                        phase.name,
                        target,
                        float(chaos.get("level", level)),
                        guard,
                        f"{shock:+.3%}" if shock is not None else "-",
                        result.get("tick"),
                    )
                except RuntimeError as exc:
                    logging.warning("%s", exc)
                time.sleep(max(0.5, args.interval))
        cycle += 1

    return 0


def main() -> int:
    args = parse_args()
    if not math.isfinite(args.interval) or args.interval <= 0:
        raise ValueError("interval must be positive")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
