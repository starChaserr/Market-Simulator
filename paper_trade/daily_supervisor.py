from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import shlex
import subprocess
import sys
import time
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from paper_trade.agent_registry import (
    DEFAULT_AGENT_REGISTRY_FILE,
    ensure_agent_registry,
    prune_losing_agents,
    register_challenger,
    requested_source_agents,
    save_agent_registry,
    update_agent_performance,
    unique_candidate_path,
)
from paper_trade.auto_live_optimize import (
    AGENT_FILES,
    PAPER_ROOT,
    RESULTS_DIR,
    ROOT,
    SENSITIVE_ENV_KEYS,
    display_path,
    load_result,
    market_window_for,
    parse_csv,
    parse_hhmm,
    resolve_symbols,
    summarize_results,
    run_verification,
)
from paper_trade.upstox_auth import UpstoxAuthError, ensure_access_token_file, metadata_path_for, token_file_is_fresh


SESSION_RESULTS_DIR = RESULTS_DIR / "daily_sessions"
REPORTS_DIR = PAPER_ROOT / "reports"
SESSION_REPORTS_DIR = REPORTS_DIR / "30_min"
FULL_DAY_REPORTS_DIR = REPORTS_DIR / "full_day"
STATUS_FILE = REPORTS_DIR / "status.json"
DAILY_REPORTS_DIR = FULL_DAY_REPORTS_DIR
SUPERVISOR_PROMPTS_DIR = PAPER_ROOT / "supervisor_prompts"
SUPERVISOR_LOGS_DIR = PAPER_ROOT / "supervisor_logs"
SUPERVISION_MARKERS_DIR = REPORTS_DIR / "supervision"

DEFAULT_AGENTS = "adaptive_edge_maker,raider_core,apex_maker"
DEFAULT_CODEX_TARGET = "adaptive_edge_maker"
DEFAULT_GEMINI_TARGET = "raider_core"


@dataclass(frozen=True)
class SessionRun:
    result_file: Path
    stdout: str
    stderr: str
    duration: float
    symbols: list[str]


@dataclass(frozen=True)
class ToolSupervisor:
    label: str
    command: str
    prompt_style: str
    target_agent: str
    timeout: float


@dataclass(frozen=True)
class UpgradeCandidate:
    source_agent: str
    script_path: Path
    baseline_path: Path
    baseline_source: str | None
    source_path: Path
    supervisor_label: str
    stage: str
    day: str
    session_index: int | None


@dataclass
class BackgroundSupervisionJob:
    session_index: int
    latest_session_report: Path
    future: Future[int]
    summary: dict[str, Any]
    symbols: list[str]


def sanitized_env() -> dict[str, str]:
    env = os.environ.copy()
    for key in SENSITIVE_ENV_KEYS:
        env.pop(key, None)
    return env


def resolve_tool_executable(name: str, env: dict[str, str]) -> str:
    if os.path.sep in name:
        return name

    found = shutil.which(name, path=env.get("PATH"))
    if found:
        return found

    for folder in ("/opt/homebrew/bin", "/usr/local/bin"):
        candidate = Path(folder) / name
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)

    if name == "codex":
        candidates = sorted(
            Path.home().glob(".vscode/extensions/openai.chatgpt-*/bin/*/codex"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for candidate in candidates:
            if candidate.exists() and os.access(candidate, os.X_OK):
                return str(candidate)

    return name


def now_in_timezone(timezone: str) -> dt.datetime:
    return dt.datetime.now(ZoneInfo(timezone))


def seconds_to_market_open(now: dt.datetime, market_open: dt.time, market_close: dt.time) -> float:
    window = market_window_for(now, market_open, market_close)
    if window.open_at <= now < window.close_at:
        return 0.0
    return max(0.0, (window.open_at - now).total_seconds())


def seconds_remaining_in_window(now: dt.datetime, market_open: dt.time, market_close: dt.time) -> float:
    window = market_window_for(now, market_open, market_close)
    if window.open_at <= now < window.close_at:
        return max(0.0, (window.close_at - now).total_seconds())
    return 0.0


def planned_session_seconds(
    remaining_seconds: float,
    session_seconds: float,
    *,
    close_buffer: float,
    min_session_seconds: float,
    run_final_partial: bool,
) -> float:
    usable = max(0.0, remaining_seconds - close_buffer)
    if usable >= session_seconds:
        return session_seconds
    if run_final_partial and usable >= min_session_seconds:
        return usable
    return 0.0


def is_rate_limit_error(exc: BaseException) -> bool:
    text = str(exc)
    return "HTTP 429" in text or "UDAPI10005" in text or "Too Many Request" in text


def make_session_result_path(day: str, session_index: int) -> Path:
    return SESSION_RESULTS_DIR / day / f"session_{session_index:02d}.json"


def make_symbol_result_path(day: str, session_index: int, symbol: str) -> Path:
    return SESSION_RESULTS_DIR / day / "symbols" / f"session_{session_index:02d}_{symbol}.json"


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)


def report_payload(
    *,
    report_type: str,
    day: str,
    stage: str,
    symbols: list[str],
    result_files: list[Path],
    summary: dict[str, Any],
    session_index: int | None = None,
    duration: float | None = None,
) -> dict[str, Any]:
    return {
        "report_type": report_type,
        "day": day,
        "stage": stage,
        "session_index": session_index,
        "duration_seconds": duration,
        "symbols": symbols,
        "result_files": [display_path(path) for path in result_files],
        "summary": summary,
        "created_at": dt.datetime.now(dt.UTC).isoformat(),
    }


def markdown_report(payload: dict[str, Any]) -> str:
    aggregate = payload.get("summary", {}).get("aggregate", [])
    rankings = payload.get("summary", {}).get("symbol_rankings", [])
    lines = [
        f"# {payload.get('stage', 'Report')}",
        "",
        f"- Day: {payload.get('day')}",
        f"- Type: {payload.get('report_type')}",
        f"- Symbols: {', '.join(payload.get('symbols', []))}",
        f"- Created: {payload.get('created_at')}",
        "",
        "## Aggregate",
        "",
        "| Agent | Symbols | Wins | Total P/L | Avg Rank | Worst DD | Orders | Fills |",
        "| :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in aggregate:
        lines.append(
            f"| {row.get('agent')} | {row.get('symbols')} | {row.get('wins')} | "
            f"{row.get('total_pl')} | {row.get('avg_rank')} | {row.get('worst_drawdown')} | "
            f"{row.get('orders')} | {row.get('fills')} |"
        )
    lines.extend(
        [
            "",
            "## Symbol Rankings",
            "",
            "| Symbol | Rank | Agent | P/L | Max DD | Orders | Fills |",
            "| :--- | ---: | :--- | ---: | ---: | ---: | ---: |",
        ]
    )
    for row in rankings:
        lines.append(
            f"| {row.get('symbol')} | {row.get('rank')} | {row.get('agent')} | "
            f"{row.get('profit_loss')} | {row.get('max_drawdown')} | {row.get('orders')} | {row.get('fills')} |"
        )
    symbol_errors = payload.get("summary", {}).get("symbol_errors", [])
    if symbol_errors:
        lines.extend(
            [
                "",
                "## Symbol Errors",
                "",
                "| Symbol | Error |",
                "| :--- | :--- |",
            ]
        )
        for row in symbol_errors:
            lines.append(f"| {row.get('symbol')} | {row.get('error')} |")
    lines.extend(["", "## Result Files", ""])
    for path in payload.get("result_files", []):
        lines.append(f"- {path}")
    lines.append("")
    return "\n".join(lines)


def write_report_pair(json_path: Path, payload: dict[str, Any]) -> Path:
    write_json(json_path, payload)
    markdown_path = json_path.with_suffix(".md")
    markdown_path.write_text(markdown_report(payload), encoding="utf-8")
    return json_path


def write_session_report(
    *,
    day: str,
    session_index: int,
    session: SessionRun,
    summary: dict[str, Any],
) -> Path:
    payload = report_payload(
        report_type="30_min",
        day=day,
        stage=f"30-minute session {session_index}",
        session_index=session_index,
        duration=session.duration,
        symbols=session.symbols,
        result_files=[session.result_file],
        summary=summary,
    )
    return write_report_pair(SESSION_REPORTS_DIR / day / f"session_{session_index:02d}.json", payload)


def session_supervision_marker_path(
    day: str,
    session_index: int,
    *,
    base_dir: Path | None = None,
) -> Path:
    if base_dir is None:
        base_dir = SUPERVISION_MARKERS_DIR
    return base_dir / day / f"session_{session_index:02d}.json"


def full_day_supervision_marker_path(day: str, *, base_dir: Path | None = None) -> Path:
    if base_dir is None:
        base_dir = SUPERVISION_MARKERS_DIR
    return base_dir / day / "full_day.json"


def supervision_marker_complete(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        payload = load_result(path)
    except (OSError, json.JSONDecodeError):
        return False
    return bool(payload.get("completed"))


def write_supervision_marker(
    path: Path,
    *,
    day: str,
    stage: str,
    result_files: list[Path],
    summary: dict[str, Any],
    session_index: int | None = None,
) -> None:
    payload = {
        "completed": True,
        "day": day,
        "stage": stage,
        "session_index": session_index,
        "result_files": [display_path(path) for path in result_files],
        "summary": summary,
        "completed_at": dt.datetime.now(dt.UTC).isoformat(),
    }
    write_json(path, payload)


def existing_session_reports(day: str) -> list[Path]:
    folder = SESSION_REPORTS_DIR / day
    if not folder.exists():
        return []
    return sorted(folder.glob("session_*.json"))


def session_index_from_path(path: Path) -> int:
    try:
        return int(path.stem.rsplit("_", 1)[-1])
    except ValueError:
        return 0


def result_files_from_reports(report_files: list[Path]) -> list[Path]:
    paths: list[Path] = []
    for report in report_files:
        try:
            payload = load_result(report)
        except (OSError, json.JSONDecodeError):
            continue
        for value in payload.get("result_files", []):
            path = Path(str(value))
            if not path.is_absolute():
                path = ROOT / path
            if path.exists():
                paths.append(path)
    return paths


def write_status(
    *,
    phase: str,
    message: str,
    day: str | None,
    symbols: list[str],
    agents: str,
    session_index: int | None = None,
    latest_session_report: Path | None = None,
    latest_daily_report: Path | None = None,
    summary: dict[str, Any] | None = None,
) -> None:
    payload = {
        "phase": phase,
        "message": message,
        "day": day,
        "symbols": symbols,
        "agents": parse_csv(agents),
        "session_index": session_index,
        "latest_session_report": display_path(latest_session_report) if latest_session_report else None,
        "latest_daily_report": display_path(latest_daily_report) if latest_daily_report else None,
        "summary": summary or {"aggregate": [], "symbol_rankings": []},
        "updated_at": dt.datetime.now(dt.UTC).isoformat(),
    }
    write_json(STATUS_FILE, payload)


def combine_result_payloads(payloads: list[dict[str, Any]]) -> dict[str, Any]:
    combined: list[dict[str, Any]] = []
    for payload in payloads:
        combined.extend(payload.get("results", []))
    return {"results": combined}


def combine_result_files(paths: list[Path]) -> dict[str, Any]:
    return combine_result_payloads([load_result(path) for path in paths])


def apply_agent_selection(
    args: argparse.Namespace,
    *,
    day: str,
    stage: str,
    session_index: int,
    summary: dict[str, Any],
) -> dict[str, Any]:
    if not getattr(args, "league_mode", False):
        return {"updated": [], "deactivated": []}
    registry_path = Path(getattr(args, "agent_registry", DEFAULT_AGENT_REGISTRY_FILE))
    registry = ensure_agent_registry(registry_path)
    updated = update_agent_performance(
        registry,
        summary,
        day=day,
        stage=stage,
        session_index=session_index,
    )
    deactivated: list[dict[str, Any]] = []
    if getattr(args, "prune_losing_agents", True):
        deactivated = prune_losing_agents(
            registry,
            requested_sources=requested_source_agents(args.agents),
            min_evaluations=max(int(args.prune_min_evaluations), 1),
            loss_threshold=float(args.prune_loss_threshold),
            keep_top_per_source=max(int(args.prune_keep_top_per_source), 0),
        )
    if updated or deactivated:
        save_agent_registry(registry, registry_path)
    return {
        "updated": [str(entry.get("label")) for entry in updated],
        "deactivated": deactivated,
    }


def upstox_token_paths(args: argparse.Namespace) -> tuple[Path, Path]:
    token_file = Path(args.token_file)
    metadata_file = Path(args.upstox_token_metadata_file) if args.upstox_token_metadata_file else metadata_path_for(token_file)
    return token_file, metadata_file


def upstox_credentials(args: argparse.Namespace) -> tuple[str | None, str | None]:
    client_id = args.upstox_client_id or os.environ.get("UPSTOX_CLIENT_ID")
    client_secret = args.upstox_client_secret or os.environ.get("UPSTOX_CLIENT_SECRET")
    return client_id, client_secret


def ensure_upstox_token(args: argparse.Namespace, *, day: str | None, symbols: list[str]) -> dict[str, Any]:
    if not getattr(args, "ensure_upstox_token", True):
        return {"status": "disabled"}
    token_file, metadata_file = upstox_token_paths(args)
    client_id, client_secret = upstox_credentials(args)
    write_status(
        phase="waiting_for_upstox_token",
        message="Checking Upstox access token before live paper trading.",
        day=day,
        symbols=symbols,
        agents=args.agents,
    )
    return ensure_access_token_file(
        token_file=token_file,
        metadata_file=metadata_file,
        client_id=client_id,
        client_secret=client_secret,
        request_if_stale=args.request_upstox_token,
        wait_seconds=args.upstox_token_wait_seconds,
        min_valid_seconds=args.upstox_token_min_valid_seconds,
        timezone=args.timezone,
        auth_base_url=args.upstox_auth_base_url,
    )


def token_is_ready(args: argparse.Namespace) -> bool:
    if not getattr(args, "ensure_upstox_token", True):
        return True
    token_file, metadata_file = upstox_token_paths(args)
    return token_file_is_fresh(
        token_file,
        metadata_file,
        min_valid_seconds=args.upstox_token_min_valid_seconds,
        timezone=args.timezone,
    )


def terminate_process(process: subprocess.Popen[Any], timeout: float = 5.0) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=timeout)


def live_match_command(args: argparse.Namespace, *, symbol: str, duration: float, port: int, result_file: Path) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "paper_trade.run_live_match",
        "--token-file",
        args.token_file,
        "--symbols",
        symbol,
        "--agents",
        args.agents,
        "--duration",
        str(duration),
        "--host",
        args.host,
        "--port",
        str(port),
        "--starting-cash",
        str(args.starting_cash),
        "--refresh",
        str(args.refresh),
        "--agent-interval",
        str(args.agent_interval),
        "--result-file",
        str(result_file),
    ]
    command.append("--continue-on-symbol-error" if args.continue_on_symbol_error else "--no-continue-on-symbol-error")
    append_league_args(command, args)
    return command


def append_league_args(command: list[str], args: argparse.Namespace) -> None:
    command.extend(["--agent-registry", str(args.agent_registry)])
    command.extend(["--max-agent-versions-per-source", str(args.max_agent_versions_per_source)])
    command.append("--league-mode" if args.league_mode else "--no-league-mode")


def run_parallel_symbol_session(args: argparse.Namespace, *, day: str, session_index: int, symbols: list[str], duration: float) -> SessionRun:
    session_file = make_session_result_path(day, session_index)
    log_dir = SUPERVISOR_LOGS_DIR / day / f"session_{session_index:02d}"
    log_dir.mkdir(parents=True, exist_ok=True)
    processes: list[tuple[str, Path, Path, Path, subprocess.Popen[Any]]] = []
    start = time.time()
    try:
        for offset, symbol in enumerate(symbols):
            result_file = make_symbol_result_path(day, session_index, symbol)
            result_file.parent.mkdir(parents=True, exist_ok=True)
            stdout_path = log_dir / f"{symbol}.out.log"
            stderr_path = log_dir / f"{symbol}.err.log"
            stdout = open(stdout_path, "w", encoding="utf-8")
            stderr = open(stderr_path, "w", encoding="utf-8")
            command = live_match_command(args, symbol=symbol, duration=duration, port=args.port + offset, result_file=result_file)
            process = subprocess.Popen(command, cwd=ROOT, stdout=stdout, stderr=stderr, text=True)
            stdout.close()
            stderr.close()
            processes.append((symbol, result_file, stdout_path, stderr_path, process))

        timeout = args.match_timeout if args.match_timeout is not None else duration + args.match_timeout_buffer
        failures: list[str] = []
        tolerated_failures: list[str] = []
        succeeded_symbols = 0
        for symbol, result_file, stdout_path, stderr_path, process in processes:
            remaining = max(1.0, timeout - (time.time() - start))
            try:
                code = process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                terminate_process(process)
                message = f"{symbol}: timed out after {timeout:.0f}s"
                if args.continue_on_symbol_error:
                    tolerated_failures.append(message)
                    write_json(result_file, {"results": [{"symbol": symbol, "duration": duration, "accounts": [], "error": message}]})
                else:
                    failures.append(message)
                continue
            if code != 0:
                stderr_tail = stderr_path.read_text(encoding="utf-8")[-2000:] if stderr_path.exists() else ""
                message = f"{symbol}: exited {code}; stderr={stderr_tail}"
                if args.continue_on_symbol_error:
                    tolerated_failures.append(message)
                    if not result_file.exists():
                        write_json(result_file, {"results": [{"symbol": symbol, "duration": duration, "accounts": [], "error": message}]})
                    continue
                failures.append(message)
                continue
            succeeded_symbols += 1
        if failures:
            raise RuntimeError("parallel live session failed\n" + "\n".join(failures))
        if tolerated_failures and succeeded_symbols == 0:
            raise RuntimeError("parallel live session failed\n" + "\n".join(tolerated_failures))

        symbol_files = [make_symbol_result_path(day, session_index, symbol) for symbol in symbols]
        payload = combine_result_files(symbol_files)
        write_json(session_file, payload)
        stdout_text = "\n".join(path.read_text(encoding="utf-8") for _, _, path, _, _ in processes if path.exists())
        stderr_text = "\n".join(path.read_text(encoding="utf-8") for _, _, _, path, _ in processes if path.exists())
        if tolerated_failures:
            stderr_text = "\n".join([stderr_text, *tolerated_failures]).strip()
        return SessionRun(session_file, stdout_text, stderr_text, duration, symbols)
    finally:
        for _, _, _, _, process in processes:
            terminate_process(process)


def run_split_symbol_session(args: argparse.Namespace, *, day: str, session_index: int, symbols: list[str], duration: float) -> SessionRun:
    session_file = make_session_result_path(day, session_index)
    per_symbol = max(args.min_symbol_seconds, duration / max(len(symbols), 1))
    command = [
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
        str(per_symbol),
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
        str(session_file),
    ]
    command.append("--continue-on-symbol-error" if args.continue_on_symbol_error else "--no-continue-on-symbol-error")
    append_league_args(command, args)
    timeout = args.match_timeout if args.match_timeout is not None else per_symbol * len(symbols) + args.match_timeout_buffer
    completed = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, timeout=timeout)
    if completed.returncode != 0:
        raise RuntimeError(f"split live session failed\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}")
    return SessionRun(session_file, completed.stdout, completed.stderr, duration, symbols)


def build_supervisor_prompt(
    *,
    stage: str,
    supervisor_label: str,
    target_agent: str,
    target_path: Path | None = None,
    result_files: list[Path],
    summary: dict[str, Any],
    symbols: list[str],
    session_minutes: float,
    day: str,
) -> str:
    baseline_path = AGENT_FILES[target_agent]
    target_path = target_path or baseline_path
    if not target_path.is_absolute():
        target_path = ROOT / target_path
    source_path = target_path if target_path.exists() else agent_source_path(target_agent)
    target_source = source_path.read_text(encoding="utf-8")
    challenger_mode = target_path != baseline_path
    competitor_paths = []
    for agent, path in sorted(AGENT_FILES.items()):
        if not path.exists():
            continue
        if agent != target_agent or challenger_mode:
            competitor_paths.append((agent, path))
    competitor_lines = "\n".join(f"- {path.relative_to(ROOT)}" for _, path in competitor_paths)
    result_lines = "\n".join(f"- {display_path(path)}" for path in result_files)
    if challenger_mode and baseline_path.exists():
        baseline_line = f"- Baseline parent file: {baseline_path.relative_to(ROOT)}\n"
    elif challenger_mode:
        baseline_line = f"- Baseline parent file is missing; challenger seed source: {source_path.relative_to(ROOT)}\n"
    else:
        baseline_line = ""
    edit_rule = (
        f"- Edit only the challenger target file `{target_path.relative_to(ROOT)}` unless a tiny focused test update is required.\n"
        f"- Do not edit or recreate the baseline parent file `{baseline_path.relative_to(ROOT)}`; it must keep trading as the old version when present.\n"
        if challenger_mode
        else "- Edit only the target file unless a tiny focused test update is required.\n"
    )
    return (
        f"You are {supervisor_label}, supervising `{target_agent}` for the Upstox paper-trading experiment.\n\n"
        "Scope:\n"
        f"- Stage: {stage}\n"
        f"- Trading day: {day}\n"
        f"- Session length: {session_minutes:.1f} minutes\n"
        f"- Symbols: {', '.join(symbols)}\n"
        f"- Target file: {target_path.relative_to(ROOT)}\n"
        f"{baseline_line}"
        "- Decision goal: improve buy / sell / hold behavior. Holding, cancelling, or quoting smaller is correct when edge is weak.\n\n"
        "Hard rules:\n"
        f"{edit_rule}"
        "- Do not edit the other supervisor's agent.\n"
        "- Do not read or use Upstox tokens, token files, account credentials, or environment secrets.\n"
        "- Do not add look-ahead bias. Use only current and past data available from `/api/state`, `/api/account`, and `/api/orders` at runtime.\n"
        "- Do not hardcode symbols, prices, timestamps, result-file paths, market-close outcomes, or one-session behavior.\n"
        "- Do not weaken risk controls just to chase the latest profit result.\n"
        "- Optimize for robust aggregate behavior across the whole basket and the full day, not one ticker or one 30-minute slice.\n"
        "- Keep simulator and paper_trade HTTP API compatibility.\n"
        "- Prefer small, explainable changes to sizing, spread, hold filters, inventory control, stale quote control, churn limits, or regime detection.\n\n"
        "Result files to inspect:\n"
        f"{result_lines}\n\n"
        "Reference agents:\n"
        f"{competitor_lines or '- none'}\n\n"
        "Aggregate summary:\n"
        f"{json.dumps(summary, indent=2)}\n\n"
        "Embedded target source for tool contexts that cannot read ignored files:\n"
        f"```python\n{target_source}\n```\n\n"
        "Task:\n"
        "1. Study the result files and target agent.\n"
        "2. Identify one or two high-confidence improvements for real-market survivability.\n"
        "3. Implement the change without look-ahead bias.\n"
        "4. Leave the baseline old version untouched so the league can test old versus new.\n"
        "5. Run py_compile for the changed agent and the unit tests when feasible.\n"
        "6. In the final message, report changed files, risk rationale, and verification.\n"
    )


def write_supervisor_prompt(label: str, prompt: str) -> Path:
    SUPERVISOR_PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
    safe_label = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in label.lower())
    path = SUPERVISOR_PROMPTS_DIR / f"{int(time.time())}_{safe_label}.md"
    path.write_text(prompt, encoding="utf-8")
    return path


def run_tool_supervisor(supervisor: ToolSupervisor, prompt: str) -> subprocess.CompletedProcess[str]:
    prompt_path = write_supervisor_prompt(supervisor.label, prompt)
    command = shlex.split(supervisor.command)
    env = sanitized_env()
    if command:
        command[0] = resolve_tool_executable(command[0], env)
    if supervisor.prompt_style == "prompt-arg":
        command.extend(["-p", prompt])
        input_text = None
    elif supervisor.prompt_style == "stdin":
        input_text = prompt
    else:
        raise ValueError(f"unknown prompt style {supervisor.prompt_style}")

    print(f"{supervisor.label} prompt written to {prompt_path}")
    print(f"Running {supervisor.label}: {' '.join(shlex.quote(part) for part in command)}")
    completed = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        input=input_text,
        capture_output=True,
        timeout=supervisor.timeout,
        env=env,
    )
    log_dir = SUPERVISOR_LOGS_DIR / "tools"
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = int(time.time())
    label = supervisor.label.lower().replace(" ", "_")
    (log_dir / f"{stamp}_{label}.out.log").write_text(completed.stdout, encoding="utf-8")
    (log_dir / f"{stamp}_{label}.err.log").write_text(completed.stderr, encoding="utf-8")
    return completed


def use_upgrade_candidates(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "league_mode", False) and getattr(args, "register_upgrade_candidates", True))


def generated_agent_candidates(source_agent: str) -> list[Path]:
    folder = ROOT / "example_agents" / "generated"
    if not folder.exists():
        return []
    return sorted(folder.glob(f"{source_agent}_*.py"), key=lambda path: path.stat().st_mtime, reverse=True)


def agent_source_path(source_agent: str) -> Path:
    baseline_path = AGENT_FILES[source_agent]
    if baseline_path.exists():
        return baseline_path
    candidates = generated_agent_candidates(source_agent)
    if candidates:
        return candidates[0]
    raise FileNotFoundError(
        f"No baseline or generated source found for {source_agent}. "
        f"Expected {baseline_path} or example_agents/generated/{source_agent}_*.py"
    )


def prepare_upgrade_candidate(
    *,
    supervisor: ToolSupervisor,
    day: str,
    stage: str,
    session_index: int | None,
) -> UpgradeCandidate:
    baseline_path = AGENT_FILES[supervisor.target_agent]
    source_path = agent_source_path(supervisor.target_agent)
    baseline_source = baseline_path.read_text(encoding="utf-8") if baseline_path.exists() else None
    candidate_path = unique_candidate_path(
        source_agent=supervisor.target_agent,
        day=day,
        session_index=session_index,
        supervisor_label=supervisor.label,
    )
    candidate_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, candidate_path)
    return UpgradeCandidate(
        source_agent=supervisor.target_agent,
        script_path=candidate_path,
        baseline_path=baseline_path,
        baseline_source=baseline_source,
        source_path=source_path,
        supervisor_label=supervisor.label,
        stage=stage,
        day=day,
        session_index=session_index,
    )


def restore_baseline_if_modified(candidate: UpgradeCandidate) -> bool:
    if candidate.baseline_source is None:
        return False
    try:
        current = candidate.baseline_path.read_text(encoding="utf-8")
    except OSError:
        return False
    if current == candidate.baseline_source:
        return False
    candidate.baseline_path.write_text(candidate.baseline_source, encoding="utf-8")
    print(f"Restored baseline old version after supervisor touched {candidate.baseline_path.relative_to(ROOT)}")
    return True


def run_verification_paths(paths: list[Path]) -> int:
    files = []
    for path in paths:
        try:
            files.append(str(path.relative_to(ROOT)))
        except ValueError:
            files.append(str(path))
    if not files:
        return 0
    py_compile = [sys.executable, "-m", "py_compile", *files]
    tests = [sys.executable, "-m", "unittest", "discover", "-s", "tests"]
    first = subprocess.run(py_compile, cwd=ROOT)
    if first.returncode != 0:
        return first.returncode
    second = subprocess.run(tests, cwd=ROOT)
    return second.returncode


def register_upgrade_candidates(args: argparse.Namespace, candidates: list[UpgradeCandidate]) -> list[dict[str, Any]]:
    registry_path = Path(getattr(args, "agent_registry", DEFAULT_AGENT_REGISTRY_FILE))
    registry = ensure_agent_registry(registry_path)
    entries = [
        register_challenger(
            registry,
            source_agent=candidate.source_agent,
            script_path=candidate.script_path,
            day=candidate.day,
            stage=candidate.stage,
            session_index=candidate.session_index,
            supervisor_label=candidate.supervisor_label,
        )
        for candidate in candidates
    ]
    save_agent_registry(registry, registry_path)
    return entries


def continue_on_supervisor_error(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "continue_on_supervisor_error", True))


def run_supervisors(
    args: argparse.Namespace,
    *,
    stage: str,
    day: str,
    result_files: list[Path],
    summary: dict[str, Any],
    symbols: list[str],
    session_minutes: float,
    session_index: int | None = None,
) -> int:
    supervisors = []
    if not args.skip_codex:
        supervisors.append(
            ToolSupervisor(
                label="Codex AdaptiveEdgeMaker",
                command=args.codex_command,
                prompt_style=args.codex_prompt_style,
                target_agent=args.codex_target,
                timeout=args.codex_timeout,
            )
        )
    if not args.skip_gemini:
        supervisors.append(
            ToolSupervisor(
                label="Gemini RaiderCore",
                command=args.gemini_command,
                prompt_style=args.gemini_prompt_style,
                target_agent=args.gemini_target,
                timeout=args.gemini_timeout,
            )
        )

    changed_targets: list[str] = []
    candidate_paths: list[Path] = []
    candidates_to_register: list[UpgradeCandidate] = []
    candidate_mode = use_upgrade_candidates(args)
    for supervisor in supervisors:
        candidate: UpgradeCandidate | None = None
        target_path: Path | None = None
        if candidate_mode:
            if args.dry_run_supervisors:
                target_path = unique_candidate_path(
                    source_agent=supervisor.target_agent,
                    day=day,
                    session_index=session_index,
                    supervisor_label=supervisor.label,
                )
            else:
                candidate = prepare_upgrade_candidate(
                    supervisor=supervisor,
                    day=day,
                    stage=stage,
                    session_index=session_index,
                )
                target_path = candidate.script_path

        prompt = build_supervisor_prompt(
            stage=stage,
            supervisor_label=supervisor.label,
            target_agent=supervisor.target_agent,
            target_path=target_path,
            result_files=result_files,
            summary=summary,
            symbols=symbols,
            session_minutes=session_minutes,
            day=day,
        )
        if args.dry_run_supervisors:
            write_supervisor_prompt(supervisor.label, prompt)
            print(f"Dry run: skipped {supervisor.label}")
            continue
        completed = run_tool_supervisor(supervisor, prompt)
        print(completed.stdout)
        if completed.stderr.strip():
            print(completed.stderr, file=sys.stderr)
        if completed.returncode != 0:
            return completed.returncode
        if candidate:
            restore_baseline_if_modified(candidate)
            if not candidate.script_path.exists():
                print(f"{supervisor.label} did not leave a challenger file at {candidate.script_path.relative_to(ROOT)}", file=sys.stderr)
                return 1
            candidate_paths.append(candidate.script_path)
            candidates_to_register.append(candidate)
        else:
            changed_targets.append(supervisor.target_agent)

    if not args.skip_verify:
        if candidate_paths:
            code = run_verification_paths(sorted(set(candidate_paths), key=str))
            if code != 0:
                return code
        elif changed_targets:
            code = run_verification(sorted(set(changed_targets)))
            if code != 0:
                return code
    if candidates_to_register and not args.dry_run_supervisors:
        entries = register_upgrade_candidates(args, candidates_to_register)
        labels = ", ".join(str(entry.get("label")) for entry in entries)
        print(f"Registered challenger agents: {labels}")
    return 0


def run_session_supervision(
    args: argparse.Namespace,
    *,
    day: str,
    session_index: int,
    result_files: list[Path],
    summary: dict[str, Any],
    symbols: list[str],
    session_minutes: float,
    latest_session_report: Path,
    stage_prefix: str = "30-minute session",
) -> int:
    stage = f"{stage_prefix} {session_index}"
    try:
        code = run_supervisors(
            args,
            stage=stage,
            day=day,
            result_files=result_files,
            summary=summary,
            symbols=symbols,
            session_minutes=session_minutes,
            session_index=session_index,
        )
    except Exception as exc:
        if not continue_on_supervisor_error(args):
            raise
        print(f"Supervisor upgrade for session {session_index} failed and was deferred: {exc}", file=sys.stderr)
        return 1
    if code == 0 and not args.dry_run_supervisors:
        write_supervision_marker(
            session_supervision_marker_path(day, session_index),
            day=day,
            stage=stage,
            session_index=session_index,
            result_files=result_files,
            summary=summary,
        )
    return code


def catch_up_incomplete_session_upgrades(
    args: argparse.Namespace,
    *,
    day: str,
    report_files: list[Path],
    symbols: list[str],
) -> int:
    if not args.catch_up_session_upgrades:
        return 0

    for report in report_files:
        session_index = session_index_from_path(report)
        if session_index <= 0:
            continue
        marker = session_supervision_marker_path(day, session_index)
        if supervision_marker_complete(marker):
            continue

        try:
            report_payload = load_result(report)
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"could not load session report {display_path(report)} for upgrade catch-up: {exc}") from exc

        result_files = result_files_from_reports([report])
        if not result_files:
            raise RuntimeError(f"session report {display_path(report)} has no readable result files for upgrade catch-up")

        summary = report_payload.get("summary") or summarize_results(combine_result_files(result_files))
        report_symbols = [str(symbol) for symbol in report_payload.get("symbols", []) if symbol]
        duration = report_payload.get("duration_seconds")
        session_minutes = float(duration) / 60.0 if duration else args.session_minutes

        write_status(
            phase="catching_up_session_upgrade",
            message=f"Replaying missed supervisor review for session {session_index} before the next live session.",
            day=day,
            symbols=report_symbols or symbols,
            agents=args.agents,
            session_index=session_index,
            latest_session_report=report,
            summary=summary,
        )
        print(f"Replaying missed supervisor review for session {session_index}.")
        code = run_session_supervision(
            args,
            day=day,
            session_index=session_index,
            result_files=result_files,
            summary=summary,
            symbols=report_symbols or symbols,
            session_minutes=session_minutes,
            latest_session_report=report,
            stage_prefix="missed 30-minute session",
        )
        if code != 0:
            if continue_on_supervisor_error(args):
                write_status(
                    phase="session_upgrade_deferred",
                    message=(
                        f"Supervisor returned {code} while replaying session {session_index}; "
                        "upgrade catch-up is deferred so live trading can continue."
                    ),
                    day=day,
                    symbols=report_symbols or symbols,
                    agents=args.agents,
                    session_index=session_index,
                    latest_session_report=report,
                    summary=summary,
                )
                print(f"Deferred missed supervisor review for session {session_index}; continuing live flow.")
                return 0
            write_status(
                phase="error",
                message=f"Supervisor returned {code} while replaying session {session_index}.",
                day=day,
                symbols=report_symbols or symbols,
                agents=args.agents,
                session_index=session_index,
                latest_session_report=report,
                summary=summary,
            )
            return code

        write_status(
            phase="session_upgrade_complete",
            message=f"Session {session_index} supervisor review is complete.",
            day=day,
            symbols=report_symbols or symbols,
            agents=args.agents,
            session_index=session_index,
            latest_session_report=report,
            summary=summary,
        )
    return 0


def reap_background_supervision_jobs(
    jobs: list[BackgroundSupervisionJob],
    *,
    args: argparse.Namespace,
    day: str,
) -> int:
    for job in jobs[:]:
        if not job.future.done():
            continue
        try:
            code = job.future.result()
        except Exception as exc:
            if continue_on_supervisor_error(args):
                jobs.remove(job)
                write_status(
                    phase="session_upgrade_deferred",
                    message=f"Background supervisor failed for session {job.session_index}; upgrade is deferred: {exc}",
                    day=day,
                    symbols=job.symbols,
                    agents=args.agents,
                    session_index=job.session_index,
                    latest_session_report=job.latest_session_report,
                    summary=job.summary,
                )
                continue
            write_status(
                phase="error",
                message=f"Background supervisor failed for session {job.session_index}: {exc}",
                day=day,
                symbols=job.symbols,
                agents=args.agents,
                session_index=job.session_index,
                latest_session_report=job.latest_session_report,
                summary=job.summary,
            )
            return 1
        jobs.remove(job)
        if code != 0:
            if continue_on_supervisor_error(args):
                write_status(
                    phase="session_upgrade_deferred",
                    message=f"Background supervisor returned {code} after session {job.session_index}; upgrade is deferred.",
                    day=day,
                    symbols=job.symbols,
                    agents=args.agents,
                    session_index=job.session_index,
                    latest_session_report=job.latest_session_report,
                    summary=job.summary,
                )
                continue
            write_status(
                phase="error",
                message=f"Background supervisor returned {code} after session {job.session_index}.",
                day=day,
                symbols=job.symbols,
                agents=args.agents,
                session_index=job.session_index,
                latest_session_report=job.latest_session_report,
                summary=job.summary,
            )
            return code
        write_status(
            phase="session_upgrade_complete",
            message=f"Session {job.session_index} challenger upgrade is registered.",
            day=day,
            symbols=job.symbols,
            agents=args.agents,
            session_index=job.session_index,
            latest_session_report=job.latest_session_report,
            summary=job.summary,
        )
    return 0


def wait_for_background_supervision_jobs(
    jobs: list[BackgroundSupervisionJob],
    *,
    args: argparse.Namespace,
    day: str,
    symbols: list[str],
    latest_session_report: Path | None,
) -> int:
    while jobs:
        write_status(
            phase="waiting_for_session_upgrades",
            message=f"Waiting for {len(jobs)} background session upgrade(s) before the full-day report.",
            day=day,
            symbols=symbols,
            agents=args.agents,
            session_index=jobs[-1].session_index,
            latest_session_report=latest_session_report,
            summary=jobs[-1].summary,
        )
        job = jobs[0]
        try:
            code = job.future.result(timeout=args.max_sleep_chunk)
        except FutureTimeoutError:
            continue
        except Exception as exc:
            if continue_on_supervisor_error(args):
                jobs.pop(0)
                write_status(
                    phase="session_upgrade_deferred",
                    message=f"Background supervisor failed for session {job.session_index}; upgrade is deferred: {exc}",
                    day=day,
                    symbols=job.symbols,
                    agents=args.agents,
                    session_index=job.session_index,
                    latest_session_report=job.latest_session_report,
                    summary=job.summary,
                )
                continue
            write_status(
                phase="error",
                message=f"Background supervisor failed for session {job.session_index}: {exc}",
                day=day,
                symbols=job.symbols,
                agents=args.agents,
                session_index=job.session_index,
                latest_session_report=job.latest_session_report,
                summary=job.summary,
            )
            return 1
        jobs.pop(0)
        if code != 0:
            if continue_on_supervisor_error(args):
                write_status(
                    phase="session_upgrade_deferred",
                    message=f"Background supervisor returned {code} after session {job.session_index}; upgrade is deferred.",
                    day=day,
                    symbols=job.symbols,
                    agents=args.agents,
                    session_index=job.session_index,
                    latest_session_report=job.latest_session_report,
                    summary=job.summary,
                )
                continue
            write_status(
                phase="error",
                message=f"Background supervisor returned {code} after session {job.session_index}.",
                day=day,
                symbols=job.symbols,
                agents=args.agents,
                session_index=job.session_index,
                latest_session_report=job.latest_session_report,
                summary=job.summary,
            )
            return code
    return 0


def write_daily_report(day: str, session_files: list[Path], summary: dict[str, Any]) -> Path:
    payload = report_payload(
        report_type="full_day",
        day=day,
        stage="full-day report",
        symbols=sorted({row.get("symbol", "") for row in summary.get("symbol_rankings", []) if row.get("symbol")}),
        result_files=session_files,
        summary=summary,
    )
    return write_report_pair(DAILY_REPORTS_DIR / f"{day}.json", payload)


def run_market_day(args: argparse.Namespace, symbols: list[str]) -> int:
    market_open = parse_hhmm(args.market_open)
    market_close = parse_hhmm(args.market_close)
    last_token_attempt_at = 0.0
    while True:
        now = now_in_timezone(args.timezone)
        open_wait = seconds_to_market_open(now, market_open, market_close)
        if open_wait <= 0:
            break
        if not args.wait:
            raise RuntimeError("market is not open; rerun with --wait")
        wake = now + dt.timedelta(seconds=open_wait)
        if (
            getattr(args, "ensure_upstox_token", True)
            and open_wait <= args.upstox_token_request_lead_seconds
            and not token_is_ready(args)
            and time.time() - last_token_attempt_at >= args.upstox_token_retry_seconds
        ):
            last_token_attempt_at = time.time()
            try:
                ensure_upstox_token(args, day=None, symbols=symbols)
            except UpstoxAuthError as exc:
                write_status(
                    phase="waiting_for_upstox_token",
                    message=f"Waiting for Upstox token before market open: {exc}",
                    day=None,
                    symbols=symbols,
                    agents=args.agents,
                )
                print(f"Waiting for Upstox token before market open: {exc}", file=sys.stderr)
                time.sleep(min(args.upstox_token_retry_seconds, args.max_sleep_chunk, max(open_wait, 1.0)))
                continue
        write_status(
            phase="waiting_for_market",
            message=f"Waiting for market open at {wake:%Y-%m-%d %H:%M:%S %Z}.",
            day=None,
            symbols=symbols,
            agents=args.agents,
        )
        print(f"Waiting for market open at {wake:%Y-%m-%d %H:%M:%S %Z}.")
        time.sleep(min(open_wait, args.max_sleep_chunk))

    day = now_in_timezone(args.timezone).strftime("%Y%m%d")
    try:
        ensure_upstox_token(args, day=day, symbols=symbols)
    except UpstoxAuthError as exc:
        write_status(
            phase="error",
            message=f"Upstox token is not ready for live paper trading: {exc}",
            day=day,
            symbols=symbols,
            agents=args.agents,
        )
        raise
    existing_reports = existing_session_reports(day)
    write_status(
        phase="market_live",
        message="Market is live. Preparing the next 30-minute session.",
        day=day,
        symbols=symbols,
        agents=args.agents,
        latest_session_report=existing_reports[-1] if existing_reports else None,
    )
    session_files: list[Path] = result_files_from_reports(existing_reports)
    session_report_files: list[Path] = existing_reports[:]
    session_index = max([session_index_from_path(path) for path in existing_reports], default=0)
    session_target_seconds = args.session_minutes * 60.0
    catch_up_code = catch_up_incomplete_session_upgrades(args, day=day, report_files=existing_reports, symbols=symbols)
    if catch_up_code != 0:
        return catch_up_code
    background_jobs: list[BackgroundSupervisionJob] = []
    supervision_executor = (
        ThreadPoolExecutor(max_workers=args.max_background_supervisors)
        if args.background_session_supervisors
        else None
    )

    while True:
        code = reap_background_supervision_jobs(background_jobs, args=args, day=day)
        if code != 0:
            if supervision_executor:
                supervision_executor.shutdown(wait=False, cancel_futures=False)
            return code
        now = now_in_timezone(args.timezone)
        remaining = seconds_remaining_in_window(now, market_open, market_close)
        duration = planned_session_seconds(
            remaining,
            session_target_seconds,
            close_buffer=args.market_close_buffer,
            min_session_seconds=args.min_session_seconds,
            run_final_partial=args.run_final_partial,
        )
        if duration <= 0:
            if args.wait_for_close_before_eod and remaining > 0:
                write_status(
                    phase="waiting_for_close",
                    message="Waiting for market close before the full-day report.",
                    day=day,
                    symbols=symbols,
                    agents=args.agents,
                    session_index=session_index,
                    latest_session_report=session_report_files[-1] if session_report_files else None,
                )
                print(f"Waiting {remaining / 60.0:.1f} minutes for market close before end-of-day review.")
                time.sleep(min(remaining, args.max_sleep_chunk))
                continue
            break

        next_session_index = session_index + 1
        write_status(
            phase="running_session",
            message=(
                f"Running session {next_session_index} for {duration / 60.0:.1f} minutes "
                f"with {args.symbol_mode} symbol mode and {args.refresh:.1f}s quote refresh."
            ),
            day=day,
            symbols=symbols,
            agents=args.agents,
            session_index=next_session_index,
            latest_session_report=session_report_files[-1] if session_report_files else None,
        )
        print(
            f"Starting {duration / 60.0:.1f} minute session {next_session_index} "
            f"for {', '.join(symbols)} using {args.symbol_mode} mode."
        )
        try:
            if args.symbol_mode == "parallel":
                session = run_parallel_symbol_session(args, day=day, session_index=next_session_index, symbols=symbols, duration=duration)
            else:
                session = run_split_symbol_session(args, day=day, session_index=next_session_index, symbols=symbols, duration=duration)
        except Exception as exc:
            if args.continue_on_rate_limit and is_rate_limit_error(exc):
                write_status(
                    phase="rate_limited",
                    message=(
                        f"Upstox rate limit hit before session {next_session_index} completed. "
                        f"Cooling down for {args.rate_limit_cooldown:.0f}s, then retrying if the market is still open."
                    ),
                    day=day,
                    symbols=symbols,
                    agents=args.agents,
                    session_index=next_session_index,
                    latest_session_report=session_report_files[-1] if session_report_files else None,
                )
                print(f"Upstox rate limit hit. Cooling down for {args.rate_limit_cooldown:.0f}s before retrying.")
                time.sleep(args.rate_limit_cooldown)
                continue
            raise
        session_index = next_session_index
        session_files.append(session.result_file)

        payload = load_result(session.result_file)
        summary = summarize_results(payload)
        selection = apply_agent_selection(
            args,
            day=day,
            stage=f"30-minute session {session_index}",
            session_index=session_index,
            summary=summary,
        )
        if selection["updated"] or selection["deactivated"]:
            summary["agent_selection"] = selection
        for action in selection["deactivated"]:
            print(f"Deactivated losing challenger {action['label']}: {action['reason']}")
        session_report = write_session_report(day=day, session_index=session_index, session=session, summary=summary)
        session_report_files.append(session_report)
        print("Session aggregate:")
        print(json.dumps(summary["aggregate"], indent=2))
        if args.background_session_supervisors and supervision_executor:
            future = supervision_executor.submit(
                run_session_supervision,
                args,
                day=day,
                session_index=session_index,
                result_files=[session.result_file],
                summary=summary,
                symbols=symbols,
                session_minutes=session.duration / 60.0,
                latest_session_report=session_report,
            )
            background_jobs.append(
                BackgroundSupervisionJob(
                    session_index=session_index,
                    latest_session_report=session_report,
                    future=future,
                    summary=summary,
                    symbols=symbols,
                )
            )
            write_status(
                phase="session_complete",
                message=f"Session {session_index} complete. Challenger upgrade is running in the background.",
                day=day,
                symbols=symbols,
                agents=args.agents,
                session_index=session_index,
                latest_session_report=session_report,
                summary=summary,
            )
            continue

        write_status(
            phase="supervising_session",
            message=f"Session {session_index} complete. Supervisors are reviewing.",
            day=day,
            symbols=symbols,
            agents=args.agents,
            session_index=session_index,
            latest_session_report=session_report,
            summary=summary,
        )
        code = run_session_supervision(
            args,
            day=day,
            session_index=session_index,
            result_files=[session.result_file],
            summary=summary,
            symbols=symbols,
            session_minutes=session.duration / 60.0,
            latest_session_report=session_report,
        )
        if code != 0:
            if continue_on_supervisor_error(args):
                write_status(
                    phase="session_upgrade_deferred",
                    message=f"Supervisor returned {code} after session {session_index}; live trading will continue and catch-up can retry later.",
                    day=day,
                    symbols=symbols,
                    agents=args.agents,
                    session_index=session_index,
                    latest_session_report=session_report,
                    summary=summary,
                )
                continue
            write_status(
                phase="error",
                message=f"Supervisor returned {code} after session {session_index}.",
                day=day,
                symbols=symbols,
                agents=args.agents,
                session_index=session_index,
                latest_session_report=session_report,
                summary=summary,
            )
            if supervision_executor:
                supervision_executor.shutdown(wait=False, cancel_futures=False)
            return code
        write_status(
            phase="session_complete",
            message=f"Session {session_index} review complete. Waiting for the next session.",
            day=day,
            symbols=symbols,
            agents=args.agents,
            session_index=session_index,
            latest_session_report=session_report,
            summary=summary,
        )

    if not session_files:
        write_status(
            phase="no_session",
            message="No usable market session was available today.",
            day=day,
            symbols=symbols,
            agents=args.agents,
        )
        print("No usable market session was available today.")
        if supervision_executor:
            supervision_executor.shutdown(wait=False, cancel_futures=False)
        return 0

    code = wait_for_background_supervision_jobs(
        background_jobs,
        args=args,
        day=day,
        symbols=symbols,
        latest_session_report=session_report_files[-1] if session_report_files else None,
    )
    if supervision_executor:
        supervision_executor.shutdown(wait=False, cancel_futures=False)
    if code != 0:
        return code

    combined = combine_result_files(session_files)
    day_summary = summarize_results(combined)
    report_path = write_daily_report(day, session_files, day_summary)
    write_status(
        phase="supervising_full_day",
        message="Full-day report is ready. Supervisors are reviewing the day.",
        day=day,
        symbols=symbols,
        agents=args.agents,
        session_index=session_index,
        latest_session_report=session_report_files[-1] if session_report_files else None,
        latest_daily_report=report_path,
        summary=day_summary,
    )
    print(f"Daily report written to {report_path}")
    try:
        code = run_supervisors(
            args,
            stage="end-of-day full-session review",
            day=day,
            result_files=[report_path, *session_files],
            summary=day_summary,
            symbols=symbols,
            session_minutes=args.session_minutes,
        )
    except Exception as exc:
        if not continue_on_supervisor_error(args):
            raise
        print(f"Full-day supervisor failed and was deferred: {exc}", file=sys.stderr)
        code = 1
    if code == 0 and not args.dry_run_supervisors:
        write_supervision_marker(
            full_day_supervision_marker_path(day),
            day=day,
            stage="end-of-day full-session review",
            result_files=[report_path, *session_files],
            summary=day_summary,
        )
    write_status(
        phase="day_complete" if code == 0 or continue_on_supervisor_error(args) else "error",
        message=(
            "Full-day supervision complete."
            if code == 0
            else f"Full-day supervisor returned {code}; report is complete and upgrade is deferred."
        ),
        day=day,
        symbols=symbols,
        agents=args.agents,
        session_index=session_index,
        latest_session_report=session_report_files[-1] if session_report_files else None,
        latest_daily_report=report_path,
        summary=day_summary,
    )
    if code != 0 and continue_on_supervisor_error(args):
        return 0
    return code


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run 30-minute live paper sessions and supervise AdaptiveEdgeMaker with Codex and RaiderCore with Gemini.")
    parser.add_argument("--symbols", default="all", help="Comma-separated symbol list from symbols.json, or 'all'.")
    parser.add_argument("--allow-single-symbol", action="store_true", help="Allow one-symbol debug runs. Disabled by default.")
    parser.add_argument("--agents", default=DEFAULT_AGENTS)
    parser.add_argument("--league-mode", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--agent-registry", default=str(DEFAULT_AGENT_REGISTRY_FILE))
    parser.add_argument(
        "--max-agent-versions-per-source",
        type=int,
        default=4,
        help="Maximum active versions per source agent launched in each paper match. 0 means no limit.",
    )
    parser.add_argument(
        "--prune-losing-agents",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Deactivate underperforming challenger versions after live paper sessions. Baseline agents are kept as protected seeds.",
    )
    parser.add_argument("--prune-min-evaluations", type=int, default=2)
    parser.add_argument("--prune-loss-threshold", type=float, default=0.0)
    parser.add_argument("--prune-keep-top-per-source", type=int, default=3)
    parser.add_argument(
        "--register-upgrade-candidates",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Write supervisor upgrades as generated challenger agents instead of overwriting the baseline agents.",
    )
    parser.add_argument(
        "--background-session-supervisors",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Run session upgrade jobs in the background while the next live paper session continues.",
    )
    parser.add_argument("--max-background-supervisors", type=int, default=1)
    parser.add_argument("--symbol-mode", choices=["parallel", "split"], default="split")
    parser.add_argument("--session-minutes", type=float, default=30.0)
    parser.add_argument("--min-session-seconds", type=float, default=300.0)
    parser.add_argument("--min-symbol-seconds", type=float, default=60.0)
    parser.add_argument("--run-final-partial", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8780)
    parser.add_argument("--starting-cash", type=float, default=100_000.0)
    parser.add_argument("--refresh", type=float, default=3.0)
    parser.add_argument("--agent-interval", type=float, default=0.5)
    parser.add_argument("--token-file", default=str(PAPER_ROOT / ".upstox_token"))
    parser.add_argument(
        "--ensure-upstox-token",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Validate the Upstox token file before live sessions and optionally request a fresh token through Upstox notifier auth.",
    )
    parser.add_argument("--request-upstox-token", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--upstox-client-id", default=None)
    parser.add_argument("--upstox-client-secret", default=None)
    parser.add_argument("--upstox-auth-base-url", default="https://api.upstox.com")
    parser.add_argument("--upstox-token-metadata-file", default=None)
    parser.add_argument("--upstox-token-request-lead-seconds", type=float, default=1800.0)
    parser.add_argument("--upstox-token-wait-seconds", type=float, default=900.0)
    parser.add_argument("--upstox-token-min-valid-seconds", type=float, default=1800.0)
    parser.add_argument("--upstox-token-retry-seconds", type=float, default=300.0)
    parser.add_argument("--timezone", default="Asia/Kolkata")
    parser.add_argument("--market-open", default="09:15")
    parser.add_argument("--market-close", default="15:30")
    parser.add_argument("--market-close-buffer", type=float, default=30.0)
    parser.add_argument("--wait-for-close-before-eod", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--wait", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--max-sleep-chunk", type=float, default=300.0)
    parser.add_argument("--max-days", type=int, default=0, help="Trading days to run. 0 means run every day until stopped.")
    parser.add_argument("--day-pause", type=float, default=300.0)
    parser.add_argument("--match-timeout", type=float, default=None)
    parser.add_argument("--match-timeout-buffer", type=float, default=240.0)
    parser.add_argument("--rate-limit-cooldown", type=float, default=300.0)
    parser.add_argument("--continue-on-rate-limit", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument(
        "--continue-on-symbol-error",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Keep a live session running when one symbol fails to load quotes, as long as another symbol completes.",
    )
    parser.add_argument("--codex-target", default=DEFAULT_CODEX_TARGET)
    parser.add_argument("--gemini-target", default=DEFAULT_GEMINI_TARGET)
    parser.add_argument("--codex-command", default="codex -s workspace-write -a never exec -")
    parser.add_argument("--gemini-command", default="gemini --approval-mode auto_edit --skip-trust")
    parser.add_argument("--codex-prompt-style", choices=["stdin", "prompt-arg"], default="stdin")
    parser.add_argument("--gemini-prompt-style", choices=["stdin", "prompt-arg"], default="prompt-arg")
    parser.add_argument("--codex-timeout", type=float, default=1200.0)
    parser.add_argument("--gemini-timeout", type=float, default=1200.0)
    parser.add_argument("--skip-codex", action="store_true")
    parser.add_argument("--skip-gemini", action="store_true")
    parser.add_argument("--skip-verify", action="store_true")
    parser.add_argument("--dry-run-supervisors", action="store_true")
    parser.add_argument(
        "--continue-on-supervisor-error",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Do not stop live trading when Codex/Gemini or generated-agent verification fails; leave the upgrade incomplete for later catch-up.",
    )
    parser.add_argument(
        "--catch-up-session-upgrades",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Replay supervisor reviews for existing session reports that do not have completion markers.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    symbols = resolve_symbols(args.symbols, allow_single_symbol=args.allow_single_symbol)
    if args.codex_target not in AGENT_FILES:
        raise ValueError(f"unknown Codex target {args.codex_target}; choices: {', '.join(sorted(AGENT_FILES))}")
    if args.gemini_target not in AGENT_FILES:
        raise ValueError(f"unknown Gemini target {args.gemini_target}; choices: {', '.join(sorted(AGENT_FILES))}")
    if args.league_mode:
        ensure_agent_registry(Path(args.agent_registry))

    days_run = 0
    while args.max_days == 0 or days_run < args.max_days:
        try:
            code = run_market_day(args, symbols)
        except Exception as exc:
            write_status(
                phase="error",
                message=str(exc),
                day=None,
                symbols=symbols,
                agents=args.agents,
            )
            raise
        if code != 0:
            return code
        days_run += 1
        if args.max_days != 0 and days_run >= args.max_days:
            break
        print(f"Day complete. Waiting {args.day_pause:.0f}s before checking the next market day.")
        time.sleep(args.day_pause)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
