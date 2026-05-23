from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from paper_trade.agent_registry import (
    DEFAULT_AGENT_REGISTRY_FILE,
    active_agent_entries,
    load_agent_registry,
    requested_source_agents,
    resolve_script_path,
)


ROOT = Path(__file__).resolve().parents[1]
PAPER_ROOT = Path(__file__).resolve().parent
RESULTS_DIR = PAPER_ROOT / "results"


AGENT_COMMANDS: dict[str, list[str]] = {
    "raider_core": [
        "example_agents/raider_core.py",
        "{user}",
        "--url",
        "{api_url}",
        "--starting-cash",
        "{starting_cash}",
        "--interval",
        "{agent_interval}",
    ],
    "adaptive_edge_maker": [
        "example_agents/adaptive_edge_maker.py",
        "{user}",
        "--url",
        "{api_url}",
        "--starting-cash",
        "{starting_cash}",
        "--interval",
        "{agent_interval}",
    ],
    "apex_maker": [
        "example_agents/apex_maker_v5.py",
        "{user}",
        "--url",
        "{api_url}",
        "--starting-cash",
        "{starting_cash}",
        "--target-loop-ms",
        "{agent_interval_ms}",
    ],
    "your_example_agent": [
        "example_agents/your_example_agent.py",
        "{user}",
        "--url",
        "{api_url}",
        "--starting-cash",
        "{starting_cash}",
        "--interval",
        "1.0",
    ],
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run example agents against the Upstox-backed paper bridge.")
    parser.add_argument("--symbols", default="RELIANCE,HDFCBANK,INFY", help="Comma-separated symbols from paper_trade/symbols.json.")
    parser.add_argument("--agents", default="raider_core,adaptive_edge_maker,apex_maker")
    parser.add_argument("--duration", type=float, default=900.0, help="Seconds to run each symbol match.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8780)
    parser.add_argument("--starting-cash", type=float, default=10_000.0)
    parser.add_argument("--refresh", type=float, default=1.0)
    parser.add_argument("--agent-interval", type=float, default=0.5)
    parser.add_argument("--token-file", default=None)
    parser.add_argument("--token-env", default="UPSTOX_ACCESS_TOKEN")
    parser.add_argument("--access-token", default=None)
    parser.add_argument("--keep-results", action="store_true")
    parser.add_argument("--result-file", default=None, help="Optional JSON output path. Implies --keep-results.")
    parser.add_argument("--league-mode", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--agent-registry", default=str(DEFAULT_AGENT_REGISTRY_FILE))
    parser.add_argument(
        "--continue-on-symbol-error",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Record a failed symbol and continue when at least one symbol match still succeeds.",
    )
    parser.add_argument(
        "--max-agent-versions-per-source",
        type=int,
        default=4,
        help="Maximum active versions per source agent in league mode. 0 means no limit.",
    )
    return parser.parse_args()


def get_json(url: str, timeout: float = 2.0) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None


def wait_for_server(api_url: str, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    last_error = None
    while time.time() < deadline:
        health = get_json(f"{api_url}/health", timeout=1.0)
        if health and health.get("quote_loaded"):
            return
        if health and health.get("last_quote_error"):
            last_error = health["last_quote_error"]
        time.sleep(0.5)
    suffix = f": {last_error}" if last_error else ""
    raise RuntimeError(f"paper bridge did not load a quote at {api_url}{suffix}")


def command_for_agent(agent: str, api_url: str, starting_cash: float, agent_interval: float) -> list[str]:
    template = AGENT_COMMANDS[agent]
    user = "".join(part.capitalize() for part in agent.split("_"))
    values = {
        "api_url": api_url,
        "user": user,
        "starting_cash": str(starting_cash),
        "agent_interval": str(agent_interval),
        "agent_interval_ms": str(max(agent_interval * 1000.0, 1.0)),
    }
    return [sys.executable, *[piece.format(**values) for piece in template]]


def command_for_registry_entry(entry: dict[str, Any], api_url: str, starting_cash: float, agent_interval: float) -> list[str]:
    script = resolve_script_path(str(entry["script"]))
    try:
        script_arg = str(script.relative_to(ROOT))
    except ValueError:
        script_arg = str(script)
    values = {
        "api_url": api_url,
        "user": str(entry["label"]),
        "starting_cash": str(starting_cash),
        "agent_interval": str(agent_interval),
    }
    command = [
        sys.executable,
        script_arg,
        values["user"],
        "--url",
        values["api_url"],
        "--starting-cash",
        values["starting_cash"],
    ]
    if entry.get("source_agent") == "apex_maker":
        command.extend(["--target-loop-ms", str(max(agent_interval * 1000.0, 1.0))])
    else:
        command.extend(["--interval", values["agent_interval"]])
    return command


def selected_agent_commands(args: argparse.Namespace, api_url: str) -> list[tuple[str, list[str]]]:
    requested = [name.strip() for name in args.agents.split(",") if name.strip()]
    if not args.league_mode:
        commands = []
        for agent in requested:
            if agent not in AGENT_COMMANDS:
                raise ValueError(f"unknown agent {agent}; choices: {', '.join(sorted(AGENT_COMMANDS))}")
            commands.append((agent, command_for_agent(agent, api_url, args.starting_cash, args.agent_interval)))
        return commands

    registry = load_agent_registry(Path(args.agent_registry))
    requested_sources = requested_source_agents(requested)
    entries = active_agent_entries(
        registry,
        requested_sources=requested_sources,
        max_versions_per_source=args.max_agent_versions_per_source,
    )
    missing = sorted(set(requested) - {str(entry.get("source_agent")) for entry in entries})
    if missing:
        raise ValueError(f"unknown agent source(s) in league registry: {', '.join(missing)}")
    commands = []
    for entry in entries:
        script = resolve_script_path(str(entry["script"]))
        if not script.exists():
            raise ValueError(f"registered agent script does not exist for {entry.get('label')}: {script}")
        commands.append((str(entry["label"]), command_for_registry_entry(entry, api_url, args.starting_cash, args.agent_interval)))
    return commands


def terminate(process: subprocess.Popen[Any], timeout: float = 5.0) -> None:
    if process.poll() is not None:
        return
    process.send_signal(signal.SIGINT)
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=timeout)


def run_symbol(args: argparse.Namespace, symbol: str) -> dict[str, Any]:
    api_url = f"http://{args.host}:{args.port}/api"
    server_cmd = [
        sys.executable,
        "-m",
        "paper_trade.server",
        "--host",
        args.host,
        "--port",
        str(args.port),
        "--symbol",
        symbol,
        "--starting-cash",
        str(args.starting_cash),
        "--refresh",
        str(args.refresh),
        "--token-env",
        args.token_env,
    ]
    if args.token_file:
        server_cmd.extend(["--token-file", args.token_file])
    if args.access_token:
        server_cmd.extend(["--access-token", args.access_token])

    env = os.environ.copy()
    server = subprocess.Popen(server_cmd, cwd=ROOT, env=env)
    agents: list[subprocess.Popen[Any]] = []
    try:
        wait_for_server(api_url)
        for label, cmd in selected_agent_commands(args, api_url):
            print(f"Launching {label}: {' '.join(cmd)}")
            agents.append(subprocess.Popen(cmd, cwd=ROOT, env=env))
        deadline = time.time() + args.duration
        while time.time() < deadline:
            time.sleep(min(5.0, max(0.1, deadline - time.time())))
        accounts = (get_json(f"{api_url}/accounts", timeout=3.0) or {}).get("accounts", [])
        accounts = sorted(accounts, key=lambda row: row.get("profit_loss", 0), reverse=True)
        return {"symbol": symbol, "duration": args.duration, "accounts": accounts}
    finally:
        for process in agents:
            terminate(process)
        terminate(server)


def print_ranking(result: dict[str, Any]) -> None:
    print(f"\n{result['symbol']} paper match")
    print("| Rank | Agent | P/L | Equity | Max DD | Orders | Fills |")
    print("| ---: | :--- | ---: | ---: | ---: | ---: | ---: |")
    for index, account in enumerate(result.get("accounts", []), start=1):
        print(
            f"| {index} | {account['owner']} | {account['profit_loss']:.2f} | "
            f"{account['equity']:.2f} | {account['max_drawdown']:.2f} | {account['orders']} | {account['fills']} |"
        )


def symbol_error_result(args: argparse.Namespace, symbol: str, exc: Exception) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "duration": args.duration,
        "accounts": [],
        "error": str(exc),
    }


def main() -> int:
    args = parse_args()
    results = []
    successful_symbols = 0
    for symbol in [value.strip().upper() for value in args.symbols.split(",") if value.strip()]:
        try:
            result = run_symbol(args, symbol)
        except Exception as exc:
            if not args.continue_on_symbol_error:
                raise
            result = symbol_error_result(args, symbol, exc)
            results.append(result)
            print(f"\n{symbol} paper match skipped: {exc}", file=sys.stderr)
            continue
        successful_symbols += 1
        results.append(result)
        print_ranking(result)

    if args.keep_results or args.result_file:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        path = Path(args.result_file) if args.result_file else RESULTS_DIR / f"paper_match_{int(time.time())}.json"
        if not path.is_absolute():
            path = ROOT / path
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"results": results}, handle, indent=2)
        print(f"\nWrote {path}")
    if results and successful_symbols == 0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
