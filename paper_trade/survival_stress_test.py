from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = Path(__file__).resolve().parent / "results"
SCENARIOS = [
    "calm",
    "high_volatility",
    "trending_up",
    "trending_down",
    "flash_crash",
    "liquidity_drought",
    "mean_reverting",
    "news_shock",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Neutral 5k survival stress test for API agents.")
    parser.add_argument("--duration", type=float, default=45.0)
    parser.add_argument("--seeds", default="101,202")
    parser.add_argument("--port-base", type=int, default=8820)
    parser.add_argument("--starting-cash", type=float, default=5000.0)
    parser.add_argument("--order-notional", type=float, default=1100.0)
    parser.add_argument("--max-notional", type=float, default=5000.0)
    parser.add_argument("--drawdown-limit", type=float, default=0.12)
    parser.add_argument("--agent-interval", type=float, default=0.25)
    parser.add_argument("--target-loop-ms", type=float, default=500.0)
    parser.add_argument("--scenarios", default=",".join(SCENARIOS))
    parser.add_argument("--result-prefix", default=None)
    return parser.parse_args()


def get_json(url: str, timeout: float = 3.0) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None


def wait_for_server(api_url: str, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if get_json(f"{api_url}/health", timeout=1.0):
            return
        time.sleep(0.25)
    raise RuntimeError(f"simulator did not start at {api_url}")


def terminate(process: subprocess.Popen[Any], timeout: float = 5.0) -> None:
    if process.poll() is not None:
        return
    process.send_signal(signal.SIGINT)
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=timeout)


def agent_commands(args: argparse.Namespace, api_url: str) -> list[tuple[str, list[str]]]:
    order_units = max(args.order_notional / 100.0, 1.0)
    max_units = max(args.max_notional / 100.0, 1.0)
    return [
        (
            "RaiderCore",
            [
                sys.executable,
                "example_agents/raider_core.py",
                "RaiderCore",
                "--url",
                api_url,
                "--starting-cash",
                str(args.starting_cash),
                "--tick",
                "0.01",
                "--interval",
                str(args.agent_interval),
                "--order-notional",
                str(args.order_notional),
                "--max-notional",
                str(args.max_notional),
                "--drawdown-limit",
                str(args.drawdown_limit),
                "--max-orders",
                "5000",
            ],
        ),
        (
            "ApexMaker",
            [
                sys.executable,
                "example_agents/apex_maker_v5.py",
                "ApexMaker",
                "--url",
                api_url,
                "--starting-cash",
                str(args.starting_cash),
                "--order-notional",
                str(args.order_notional),
                "--max-notional",
                str(args.max_notional),
                "--drawdown-limit",
                str(args.drawdown_limit),
                "--target-loop-ms",
                str(args.target_loop_ms),
                "--max-orders",
                "5000",
            ],
        ),
        (
            "AdaptiveEdgeMaker",
            [
                sys.executable,
                "example_agents/adaptive_edge_maker.py",
                "AdaptiveEdgeMaker",
                "--url",
                api_url,
                "--starting-cash",
                str(args.starting_cash),
                "--max-pos",
                str(max_units),
                "--order-size",
                str(order_units),
                "--tick",
                "0.01",
                "--interval",
                str(args.agent_interval),
                "--drawdown-limit",
                str(args.drawdown_limit),
                "--max-orders",
                "5000",
            ],
        ),
    ]


def write_config(path: Path, starting_cash: float) -> None:
    path.write_text(
        json.dumps(
            {
                "api_starting_cash": starting_cash,
                "api_rate_limit_enabled": True,
                "api_rate_limit_per_second": 60,
                "api_rate_limit_per_minute": 6000,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def run_case(args: argparse.Namespace, scenario: str, seed: int, port: int) -> dict[str, Any]:
    api_url = f"http://127.0.0.1:{port}/api"
    with tempfile.TemporaryDirectory(prefix="survival-stress-") as tmp:
        config_path = Path(tmp) / "config.json"
        write_config(config_path, args.starting_cash)
        server_cmd = [
            sys.executable,
            "main.py",
            "--port",
            str(port),
            "--scenario",
            scenario,
            "--seed",
            str(seed),
            "--config",
            str(config_path),
        ]
        server = subprocess.Popen(server_cmd, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        agents: list[tuple[str, subprocess.Popen[Any]]] = []
        started_at = time.time()
        try:
            wait_for_server(api_url)
            for label, command in agent_commands(args, api_url):
                agents.append(
                    (
                        label,
                        subprocess.Popen(command, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
                    )
                )
            deadline = time.time() + args.duration
            while time.time() < deadline:
                time.sleep(min(1.0, max(0.05, deadline - time.time())))
            accounts_payload = get_json(f"{api_url}/accounts", timeout=5.0) or {}
            accounts = {str(row.get("owner")): row for row in accounts_payload.get("accounts", [])}
            rows = []
            for label, process in agents:
                account = accounts.get(label) or get_json(
                    f"{api_url}/account?user={urllib.parse.quote(label)}",
                    timeout=3.0,
                )
                exit_code = process.poll()
                status = "running" if exit_code is None else f"exited:{exit_code}"
                row = {
                    "agent": label,
                    "status": status,
                    "survived": exit_code is None or exit_code == 0,
                    "equity": None,
                    "profit_loss": None,
                    "max_drawdown": None,
                    "orders": None,
                    "fills": None,
                    "inventory": None,
                }
                if account and not account.get("error"):
                    row.update(
                        {
                            "equity": float(account.get("equity", 0.0)),
                            "profit_loss": float(account.get("profit_loss", 0.0)),
                            "max_drawdown": float(account.get("max_drawdown", 0.0)),
                            "orders": int(account.get("orders", 0)),
                            "fills": int(account.get("fills", 0)),
                            "inventory": float(account.get("inventory", 0.0)),
                        }
                    )
                rows.append(row)
            return {
                "scenario": scenario,
                "seed": seed,
                "port": port,
                "duration": round(time.time() - started_at, 3),
                "agents": rows,
            }
        finally:
            for _, process in agents:
                terminate(process)
            terminate(server)


def aggregate(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    labels = sorted({row["agent"] for case in cases for row in case.get("agents", [])})
    result = []
    for label in labels:
        rows = [row for case in cases for row in case.get("agents", []) if row.get("agent") == label]
        valid = [row for row in rows if row.get("profit_loss") is not None]
        survived = sum(1 for row in rows if row.get("survived"))
        crashes = len(rows) - survived
        p_l = [float(row["profit_loss"]) for row in valid]
        dds = [float(row["max_drawdown"]) for row in valid]
        orders = [int(row["orders"]) for row in valid]
        fills = [int(row["fills"]) for row in valid]
        result.append(
            {
                "agent": label,
                "runs": len(rows),
                "valid_runs": len(valid),
                "survived_runs": survived,
                "crashes_or_stops": crashes,
                "avg_pl": sum(p_l) / len(p_l) if p_l else None,
                "median_pl": sorted(p_l)[len(p_l) // 2] if p_l else None,
                "worst_pl": min(p_l) if p_l else None,
                "best_pl": max(p_l) if p_l else None,
                "avg_drawdown": sum(dds) / len(dds) if dds else None,
                "worst_drawdown": max(dds) if dds else None,
                "avg_orders": sum(orders) / len(orders) if orders else None,
                "avg_fills": sum(fills) / len(fills) if fills else None,
            }
        )
    result.sort(
        key=lambda row: (
            -int(row["survived_runs"]),
            int(row["crashes_or_stops"]),
            float(row["worst_drawdown"] if row["worst_drawdown"] is not None else 1e12),
            -float(row["avg_pl"] if row["avg_pl"] is not None else -1e12),
        )
    )
    return result


def fmt(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def write_reports(payload: dict[str, Any], prefix: str) -> tuple[Path, Path]:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    json_path = RESULTS_DIR / f"{prefix}.json"
    md_path = RESULTS_DIR / f"{prefix}.md"
    json_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    lines = [
        "# Agent Survival Stress Test",
        "",
        f"Generated: {payload['generated_at']}",
        f"Starting cash: {payload['settings']['starting_cash']:.2f}",
        f"Duration per case: {payload['settings']['duration']:.1f}s",
        f"Scenarios: {', '.join(payload['settings']['scenarios'])}",
        f"Seeds: {', '.join(str(seed) for seed in payload['settings']['seeds'])}",
        "",
        "## Survival Ranking",
        "",
        "| Rank | Agent | Survived | Stops | Avg P/L | Worst P/L | Worst DD | Avg Orders | Avg Fills |",
        "| ---: | :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for index, row in enumerate(payload["aggregate"], start=1):
        lines.append(
            f"| {index} | {row['agent']} | {row['survived_runs']}/{row['runs']} | "
            f"{row['crashes_or_stops']} | {fmt(row['avg_pl'])} | {fmt(row['worst_pl'])} | "
            f"{fmt(row['worst_drawdown'])} | {fmt(row['avg_orders'])} | {fmt(row['avg_fills'])} |"
        )

    lines.extend(["", "## Per Case", ""])
    for case in payload["cases"]:
        lines.append(f"### {case['scenario']} seed {case['seed']}")
        lines.append("")
        lines.append("| Agent | Status | P/L | Equity | DD | Orders | Fills | Inventory |")
        lines.append("| :--- | :--- | ---: | ---: | ---: | ---: | ---: | ---: |")
        for row in sorted(case["agents"], key=lambda item: item["agent"]):
            lines.append(
                f"| {row['agent']} | {row['status']} | {fmt(row['profit_loss'])} | "
                f"{fmt(row['equity'])} | {fmt(row['max_drawdown'])} | {fmt(row['orders'])} | "
                f"{fmt(row['fills'])} | {fmt(row['inventory'])} |"
            )
        lines.append("")

    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path


def main() -> int:
    args = parse_args()
    scenarios = [part.strip() for part in args.scenarios.split(",") if part.strip()]
    seeds = [int(part.strip()) for part in args.seeds.split(",") if part.strip()]
    cases = []
    case_number = 0
    total = len(scenarios) * len(seeds)
    for scenario in scenarios:
        for seed in seeds:
            port = args.port_base + case_number
            case_number += 1
            print(f"[{case_number}/{total}] scenario={scenario} seed={seed} port={port}", flush=True)
            case = run_case(args, scenario, seed, port)
            cases.append(case)
            ranking = sorted(
                case["agents"],
                key=lambda row: (
                    row.get("profit_loss") if row.get("profit_loss") is not None else -1e12
                ),
                reverse=True,
            )
            print(
                "  "
                + " | ".join(
                    f"{row['agent']}: P/L={fmt(row['profit_loss'])}, DD={fmt(row['max_drawdown'])}, {row['status']}"
                    for row in ranking
                ),
                flush=True,
            )

    payload = {
        "generated_at": dt.datetime.now(dt.UTC).isoformat(),
        "settings": {
            "duration": args.duration,
            "seeds": seeds,
            "scenarios": scenarios,
            "starting_cash": args.starting_cash,
            "order_notional": args.order_notional,
            "max_notional": args.max_notional,
            "drawdown_limit": args.drawdown_limit,
        },
        "cases": cases,
        "aggregate": aggregate(cases),
    }
    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    prefix = args.result_prefix or f"survival_stress_{timestamp}"
    json_path, md_path = write_reports(payload, prefix)
    print(f"JSON: {json_path}", flush=True)
    print(f"Report: {md_path}", flush=True)
    print("Final ranking:", flush=True)
    for index, row in enumerate(payload["aggregate"], start=1):
        print(
            f"{index}. {row['agent']} survived={row['survived_runs']}/{row['runs']} "
            f"avg_pl={fmt(row['avg_pl'])} worst_dd={fmt(row['worst_drawdown'])}",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
