from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
PAPER_ROOT = Path(__file__).resolve().parent
SYMBOLS_FILE = PAPER_ROOT / "symbols.json"
RESULTS_DIR = PAPER_ROOT / "results"
PROMPTS_DIR = PAPER_ROOT / "gemini_prompts"

DEFAULT_AGENTS = "raider_core,adaptive_edge_maker,apex_maker"
DEFAULT_TARGET_AGENTS = "adaptive_edge_maker"
AGENT_FILES = {
    "raider_core": ROOT / "example_agents" / "raider_core.py",
    "adaptive_edge_maker": ROOT / "example_agents" / "adaptive_edge_maker.py",
    "apex_maker": ROOT / "example_agents" / "apex_maker_v5.py",
    "your_example_agent": ROOT / "example_agents" / "your_example_agent.py",
}
SENSITIVE_ENV_KEYS = {
    "UPSTOX_ACCESS_TOKEN",
    "UPSTOX_TOKEN",
    "UPSTOX_API_KEY",
    "UPSTOX_API_SECRET",
    "UPSTOX_CLIENT_ID",
    "UPSTOX_CLIENT_SECRET",
    "UPSTOX_WEBHOOK_SECRET",
}


@dataclass(frozen=True)
class MarketWindow:
    open_at: dt.datetime
    close_at: dt.datetime


@dataclass(frozen=True)
class MatchRun:
    result_file: Path
    stdout: str
    stderr: str


def parse_hhmm(value: str) -> dt.time:
    try:
        hour, minute = value.split(":", 1)
        return dt.time(hour=int(hour), minute=int(minute))
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(f"{value!r} must use HH:MM format") from exc


def parse_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def load_symbol_names(path: Path = SYMBOLS_FILE) -> list[str]:
    with open(path, "r", encoding="utf-8") as handle:
        rows = json.load(handle)
    return [str(row["symbol"]).upper() for row in rows if row.get("symbol")]


def resolve_symbols(value: str, *, symbols_file: Path = SYMBOLS_FILE, allow_single_symbol: bool = False) -> list[str]:
    available = load_symbol_names(symbols_file)
    requested = available if value.strip().lower() in {"all", "*"} else [symbol.upper() for symbol in parse_csv(value)]
    unknown = [symbol for symbol in requested if symbol not in available]
    if unknown:
        raise ValueError(f"unknown symbols: {', '.join(unknown)}; choices: {', '.join(available)}")
    if len(requested) < 2 and not allow_single_symbol:
        raise ValueError("live optimizer needs at least two symbols; pass --allow-single-symbol only for debugging")
    return requested


def next_weekday(day: dt.date) -> dt.date:
    current = day
    while current.weekday() >= 5:
        current += dt.timedelta(days=1)
    return current


def market_window_for(now: dt.datetime, market_open: dt.time, market_close: dt.time) -> MarketWindow:
    day = next_weekday(now.date())
    open_at = dt.datetime.combine(day, market_open, tzinfo=now.tzinfo)
    close_at = dt.datetime.combine(day, market_close, tzinfo=now.tzinfo)
    if now >= close_at:
        next_day = next_weekday(day + dt.timedelta(days=1))
        open_at = dt.datetime.combine(next_day, market_open, tzinfo=now.tzinfo)
        close_at = dt.datetime.combine(next_day, market_close, tzinfo=now.tzinfo)
    return MarketWindow(open_at=open_at, close_at=close_at)


def seconds_until_market_live(
    now: dt.datetime,
    market_open: dt.time,
    market_close: dt.time,
    *,
    min_remaining: float,
) -> float:
    window = market_window_for(now, market_open, market_close)
    if window.open_at <= now < window.close_at and (window.close_at - now).total_seconds() >= min_remaining:
        return 0.0
    if now < window.open_at:
        return (window.open_at - now).total_seconds()
    next_window = market_window_for(window.close_at + dt.timedelta(seconds=1), market_open, market_close)
    return (next_window.open_at - now).total_seconds()


def make_result_path(args: argparse.Namespace, cycle_index: int) -> Path:
    timestamp = dt.datetime.now(ZoneInfo(args.timezone)).strftime("%Y%m%d_%H%M%S")
    return RESULTS_DIR / f"auto_live_{timestamp}_cycle{cycle_index}.json"


def run_match(args: argparse.Namespace, symbols: list[str], result_file: Path) -> MatchRun:
    cmd = [
        sys.executable,
        "-m",
        "paper_trade.run_live_match",
        "--token-file",
        args.token_file,
        "--symbols",
        ",".join(symbols),
        "--agents",
        args.agents,
        "--duration",
        str(args.duration),
        "--host",
        args.host,
        "--port",
        str(args.port),
        "--starting-cash",
        str(args.starting_cash),
        "--refresh",
        str(args.refresh),
        "--agent-interval",
        str(args.agent_interval),
        "--result-file",
        str(result_file),
    ]
    completed = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True, timeout=args.match_timeout)
    if completed.returncode != 0:
        raise RuntimeError(
            "live match failed\n"
            f"command: {' '.join(shlex.quote(piece) for piece in cmd)}\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )
    return MatchRun(result_file=result_file, stdout=completed.stdout, stderr=completed.stderr)


def load_result(path: Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def summarize_results(payload: dict[str, Any]) -> dict[str, Any]:
    rows: dict[str, dict[str, Any]] = {}
    symbol_rankings: list[dict[str, Any]] = []
    symbol_errors: list[dict[str, str]] = []
    for result in payload.get("results", []):
        accounts = sorted(result.get("accounts", []), key=lambda row: float(row.get("profit_loss", 0.0)), reverse=True)
        symbol = result.get("symbol", "")
        if result.get("error"):
            symbol_errors.append({"symbol": str(symbol), "error": str(result.get("error"))})
        for rank, account in enumerate(accounts, start=1):
            owner = str(account.get("owner", "unknown"))
            row = rows.setdefault(
                owner,
                {
                    "symbols": 0,
                    "wins": 0,
                    "total_pl": 0.0,
                    "worst_drawdown": 0.0,
                    "orders": 0,
                    "fills": 0,
                    "rank_sum": 0,
                },
            )
            row["symbols"] += 1
            row["wins"] += 1 if rank == 1 else 0
            row["total_pl"] += float(account.get("profit_loss", 0.0))
            row["worst_drawdown"] = max(row["worst_drawdown"], float(account.get("max_drawdown", 0.0)))
            row["orders"] += int(account.get("orders", 0))
            row["fills"] += int(account.get("fills", 0))
            row["rank_sum"] += rank
            symbol_rankings.append(
                {
                    "symbol": symbol,
                    "rank": rank,
                    "agent": owner,
                    "profit_loss": round(float(account.get("profit_loss", 0.0)), 4),
                    "max_drawdown": round(float(account.get("max_drawdown", 0.0)), 4),
                    "orders": int(account.get("orders", 0)),
                    "fills": int(account.get("fills", 0)),
                }
            )
    aggregate = []
    for owner, row in rows.items():
        symbols = max(int(row["symbols"]), 1)
        aggregate.append(
            {
                "agent": owner,
                "symbols": symbols,
                "wins": row["wins"],
                "total_pl": round(row["total_pl"], 4),
                "avg_rank": round(row["rank_sum"] / symbols, 3),
                "worst_drawdown": round(row["worst_drawdown"], 4),
                "orders": row["orders"],
                "fills": row["fills"],
            }
        )
    aggregate.sort(key=lambda row: (float(row["total_pl"]), -float(row["worst_drawdown"]), -float(row["avg_rank"])), reverse=True)
    return {"aggregate": aggregate, "symbol_rankings": symbol_rankings, "symbol_errors": symbol_errors}


def agent_paths(agent_names: list[str]) -> list[Path]:
    paths = []
    for name in agent_names:
        path = AGENT_FILES.get(name)
        if not path:
            raise ValueError(f"unknown target agent {name}; choices: {', '.join(sorted(AGENT_FILES))}")
        paths.append(path)
    return paths


def display_path(path: Path) -> str:
    absolute = path if path.is_absolute() else ROOT / path
    try:
        return str(absolute.relative_to(ROOT))
    except ValueError:
        return str(path)


def build_gemini_prompt(
    *,
    result_file: Path,
    summary: dict[str, Any],
    target_agents: list[str],
    competitor_agents: list[str],
    symbols: list[str],
) -> str:
    target_files = agent_paths(target_agents)
    competitor_files = agent_paths([name for name in competitor_agents if name in AGENT_FILES])
    target_lines = "\n".join(f"- {path.relative_to(ROOT)}" for path in target_files)
    competitor_lines = "\n".join(f"- {path.relative_to(ROOT)}" for path in competitor_files)
    return (
        "You are improving a local paper-trading agent in this repository.\n\n"
        "Hard rules:\n"
        "- Edit only the target agent file(s) listed below unless a small test update is required.\n"
        "- Do not read or use Upstox tokens, token files, environment secrets, or live account credentials.\n"
        "- Do not add look-ahead bias: the agent can only use fields returned by the current/past /api/state, /api/account, /api/orders responses at runtime.\n"
        "- Do not hardcode symbols, prices, timestamps, result-file values, market-close outcomes, or one-symbol behavior.\n"
        "- Optimize for robust aggregate behavior across the full basket, not for any single symbol.\n"
        "- Keep the existing simulator/paper_trade HTTP API compatibility.\n"
        "- Keep risk controls conservative enough for a real paper market: cap churn, stale quotes, inventory, drawdown, and toxic fills.\n"
        "- After editing, run py_compile for changed agents and the existing unit test suite if available.\n\n"
        f"Symbols in this live basket: {', '.join(symbols)}\n"
        f"Result JSON: {display_path(result_file)}\n\n"
        "Target agent file(s):\n"
        f"{target_lines}\n\n"
        "Competitor/reference agent file(s):\n"
        f"{competitor_lines or '- none'}\n\n"
        "Live paper result summary:\n"
        f"{json.dumps(summary, indent=2)}\n\n"
        "Task:\n"
        "1. Inspect the target agent logic and the result JSON.\n"
        "2. Make a focused improvement that should help the target survive multiple live NSE symbols, not just the latest winner/loser.\n"
        "3. Prefer small parameter/risk-control/signaling improvements over broad rewrites.\n"
        "4. Report exactly what changed and which verification command passed or failed.\n"
    )


def write_prompt(prompt: str) -> Path:
    PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
    path = PROMPTS_DIR / f"gemini_optimize_{int(time.time())}.md"
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(prompt)
    return path


def gemini_env() -> dict[str, str]:
    env = os.environ.copy()
    for key in SENSITIVE_ENV_KEYS:
        env.pop(key, None)
    return env


def run_gemini(args: argparse.Namespace, prompt: str) -> subprocess.CompletedProcess[str]:
    prompt_path = write_prompt(prompt)
    command = [*shlex.split(args.gemini_command), *args.gemini_arg, "-p", prompt]
    print(f"Gemini prompt written to {prompt_path}")
    print(f"Running Gemini CLI: {' '.join(shlex.quote(part) for part in command[:-1])} '<prompt>'")
    return subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=args.gemini_timeout,
        env=gemini_env(),
    )


def run_verification(target_agents: list[str]) -> int:
    files = [str(path.relative_to(ROOT)) for path in agent_paths(target_agents)]
    py_compile = [sys.executable, "-m", "py_compile", *files]
    tests = [sys.executable, "-m", "unittest", "discover", "-s", "tests"]
    first = subprocess.run(py_compile, cwd=ROOT)
    if first.returncode != 0:
        return first.returncode
    second = subprocess.run(tests, cwd=ROOT)
    return second.returncode


def wait_for_market(args: argparse.Namespace, symbols: list[str]) -> None:
    tz = ZoneInfo(args.timezone)
    market_open = parse_hhmm(args.market_open)
    market_close = parse_hhmm(args.market_close)
    minimum = args.min_remaining if args.min_remaining is not None else len(symbols) * args.duration + args.market_close_buffer
    while True:
        now = dt.datetime.now(tz)
        wait_seconds = seconds_until_market_live(now, market_open, market_close, min_remaining=minimum)
        if wait_seconds <= 0:
            return
        if not args.wait:
            raise RuntimeError(f"market is not live or does not have {minimum:.0f}s remaining; rerun with --wait")
        wake = now + dt.timedelta(seconds=wait_seconds)
        print(f"Market not live for this basket. Sleeping until {wake:%Y-%m-%d %H:%M:%S %Z}.")
        time.sleep(min(wait_seconds, args.max_sleep_chunk))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run live multi-symbol paper tests and ask Gemini CLI to improve a target bot.")
    parser.add_argument("--symbols", default="all", help="Comma-separated symbol list from symbols.json, or 'all'.")
    parser.add_argument("--allow-single-symbol", action="store_true", help="Allow one-symbol debug runs. Disabled by default to avoid overfitting.")
    parser.add_argument("--agents", default=DEFAULT_AGENTS)
    parser.add_argument("--target-agents", default=DEFAULT_TARGET_AGENTS, help="Comma-separated agent keys Gemini may edit.")
    parser.add_argument("--duration", type=float, default=600.0, help="Seconds per symbol.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8780)
    parser.add_argument("--starting-cash", type=float, default=100_000.0)
    parser.add_argument("--refresh", type=float, default=1.0)
    parser.add_argument("--agent-interval", type=float, default=0.5)
    parser.add_argument("--token-file", default=str(PAPER_ROOT / ".upstox_token"))
    parser.add_argument("--timezone", default="Asia/Kolkata")
    parser.add_argument("--market-open", default="09:15")
    parser.add_argument("--market-close", default="15:30")
    parser.add_argument("--market-close-buffer", type=float, default=120.0)
    parser.add_argument("--min-remaining", type=float, default=None, help="Required live-market seconds remaining before starting.")
    parser.add_argument("--wait", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--max-sleep-chunk", type=float, default=300.0)
    parser.add_argument("--max-cycles", type=int, default=1, help="Number of live optimize cycles. Use 0 for continuous.")
    parser.add_argument("--cycle-pause", type=float, default=60.0)
    parser.add_argument("--match-timeout", type=float, default=None)
    parser.add_argument("--gemini-command", default="gemini")
    parser.add_argument("--gemini-arg", action="append", default=[], help="Extra argument passed before '-p <prompt>'. Repeat as needed.")
    parser.add_argument("--gemini-timeout", type=float, default=900.0)
    parser.add_argument("--skip-gemini", action="store_true")
    parser.add_argument("--skip-verify", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    symbols = resolve_symbols(args.symbols, allow_single_symbol=args.allow_single_symbol)
    agents = parse_csv(args.agents)
    target_agents = parse_csv(args.target_agents)
    if args.match_timeout is None:
        args.match_timeout = max(120.0, len(symbols) * args.duration + 180.0)

    cycle = 0
    while args.max_cycles == 0 or cycle < args.max_cycles:
        cycle += 1
        wait_for_market(args, symbols)
        result_file = make_result_path(args, cycle)
        print(f"Starting live paper cycle {cycle} on symbols: {', '.join(symbols)}")
        match = run_match(args, symbols, result_file)
        print(match.stdout)
        if match.stderr.strip():
            print(match.stderr, file=sys.stderr)

        payload = load_result(match.result_file)
        summary = summarize_results(payload)
        print("Aggregate result:")
        print(json.dumps(summary["aggregate"], indent=2))

        if not args.skip_gemini:
            prompt = build_gemini_prompt(
                result_file=match.result_file,
                summary=summary,
                target_agents=target_agents,
                competitor_agents=agents,
                symbols=symbols,
            )
            gemini = run_gemini(args, prompt)
            print(gemini.stdout)
            if gemini.stderr.strip():
                print(gemini.stderr, file=sys.stderr)
            if gemini.returncode != 0:
                return gemini.returncode
            if not args.skip_verify:
                code = run_verification(target_agents)
                if code != 0:
                    return code

        if args.max_cycles == 0 or cycle < args.max_cycles:
            time.sleep(args.cycle_pause)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
