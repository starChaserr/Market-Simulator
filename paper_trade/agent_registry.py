from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
PAPER_ROOT = Path(__file__).resolve().parent
DEFAULT_AGENT_REGISTRY_FILE = PAPER_ROOT / "agent_registry.json"
GENERATED_AGENTS_DIR = ROOT / "example_agents" / "generated"

BASELINE_AGENTS: dict[str, dict[str, str]] = {
    "raider_core": {
        "label": "RaiderCore",
        "script": "example_agents/raider_core.py",
    },
    "adaptive_edge_maker": {
        "label": "AdaptiveEdgeMaker",
        "script": "example_agents/adaptive_edge_maker.py",
    },
    "apex_maker": {
        "label": "ApexMaker",
        "script": "example_agents/apex_maker_v5.py",
    },
    "your_example_agent": {
        "label": "YourExampleAgent",
        "script": "example_agents/your_example_agent.py",
    },
}


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def slug(value: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else "_" for ch in value)
    parts = [part for part in cleaned.split("_") if part]
    return "_".join(parts) or "agent"


def display_path(path: Path) -> str:
    absolute = path if path.is_absolute() else ROOT / path
    try:
        return str(absolute.relative_to(ROOT))
    except ValueError:
        return str(path)


def resolve_script_path(value: str | Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return ROOT / path


def baseline_key(source_agent: str) -> str:
    return f"{source_agent}__baseline"


def baseline_entry(source_agent: str) -> dict[str, Any]:
    agent = BASELINE_AGENTS[source_agent]
    return {
        "key": baseline_key(source_agent),
        "source_agent": source_agent,
        "label": agent["label"],
        "role": "baseline",
        "script": agent["script"],
        "active": True,
        "generation": 0,
        "created_at": None,
    }


def default_registry() -> dict[str, Any]:
    return {
        "version": 1,
        "updated_at": utc_now(),
        "agents": [baseline_entry(source_agent) for source_agent in BASELINE_AGENTS],
    }


def normalize_registry(registry: dict[str, Any]) -> dict[str, Any]:
    registry.setdefault("version", 1)
    registry.setdefault("updated_at", utc_now())
    agents = registry.setdefault("agents", [])
    by_key = {str(entry.get("key")): entry for entry in agents if entry.get("key")}
    for source_agent in BASELINE_AGENTS:
        key = baseline_key(source_agent)
        default = baseline_entry(source_agent)
        if key not in by_key:
            agents.append(baseline_entry(source_agent))
            continue
        entry = by_key[key]
        entry["source_agent"] = source_agent
        entry["label"] = default["label"]
        entry["role"] = "baseline"
        entry["script"] = default["script"]
        entry["active"] = True
        entry["generation"] = 0
    return registry


def load_agent_registry(path: Path = DEFAULT_AGENT_REGISTRY_FILE) -> dict[str, Any]:
    if not path.exists():
        return default_registry()
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"agent registry {display_path(path)} must contain a JSON object")
    return normalize_registry(payload)


def save_agent_registry(registry: dict[str, Any], path: Path = DEFAULT_AGENT_REGISTRY_FILE) -> None:
    registry["updated_at"] = utc_now()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(registry, handle, indent=2)
        handle.write("\n")
    temporary.replace(path)


def ensure_agent_registry(path: Path = DEFAULT_AGENT_REGISTRY_FILE) -> dict[str, Any]:
    registry = load_agent_registry(path)
    save_agent_registry(registry, path)
    return registry


def requested_source_agents(value: str | list[str] | tuple[str, ...] | set[str] | None) -> set[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        requested = [part.strip() for part in value.split(",") if part.strip()]
    else:
        requested = [str(part).strip() for part in value if str(part).strip()]
    return set(requested)


def active_agent_entries(
    registry: dict[str, Any],
    *,
    requested_sources: set[str] | None = None,
    max_versions_per_source: int = 0,
) -> list[dict[str, Any]]:
    entries = [
        entry
        for entry in registry.get("agents", [])
        if entry.get("active", True)
        and entry.get("script")
        and (requested_sources is None or entry.get("source_agent") in requested_sources)
    ]
    entries.sort(key=lambda entry: (str(entry.get("source_agent", "")), int(entry.get("generation", 0)), str(entry.get("key", ""))))

    if max_versions_per_source <= 0:
        return entries

    limited: list[dict[str, Any]] = []
    for source_agent in sorted({str(entry.get("source_agent")) for entry in entries}):
        source_entries = [entry for entry in entries if entry.get("source_agent") == source_agent]
        baselines = [entry for entry in source_entries if int(entry.get("generation", 0)) == 0]
        challengers = sorted(
            [entry for entry in source_entries if int(entry.get("generation", 0)) > 0],
            key=agent_selection_key,
            reverse=True,
        )
        room = max(max_versions_per_source - len(baselines), 0)
        limited.extend(baselines + challengers[:room])
    return limited


def next_generation(registry: dict[str, Any], source_agent: str) -> int:
    generations = [
        int(entry.get("generation", 0))
        for entry in registry.get("agents", [])
        if entry.get("source_agent") == source_agent
    ]
    return max(generations, default=0) + 1


def unique_candidate_path(
    *,
    source_agent: str,
    day: str,
    session_index: int | None,
    supervisor_label: str,
    directory: Path = GENERATED_AGENTS_DIR,
) -> Path:
    stage = f"s{session_index:02d}" if session_index is not None else "full_day"
    base_name = f"{slug(source_agent)}_{day}_{stage}_{slug(supervisor_label)}"
    candidate = directory / f"{base_name}.py"
    counter = 2
    while candidate.exists():
        candidate = directory / f"{base_name}_{counter}.py"
        counter += 1
    return candidate


def challenger_label(source_agent: str, generation: int, session_index: int | None) -> str:
    base = BASELINE_AGENTS.get(source_agent, {}).get("label", "".join(part.capitalize() for part in source_agent.split("_")))
    stage = f"S{session_index:02d}" if session_index is not None else "FullDay"
    return f"{base}_{stage}_G{generation}"


def register_challenger(
    registry: dict[str, Any],
    *,
    source_agent: str,
    script_path: Path,
    day: str,
    stage: str,
    session_index: int | None,
    supervisor_label: str,
) -> dict[str, Any]:
    generation = next_generation(registry, source_agent)
    key = f"{source_agent}__g{generation}__{slug(script_path.stem)}"
    existing = next((entry for entry in registry.get("agents", []) if entry.get("script") == display_path(script_path)), None)
    if existing:
        existing.update({"active": True, "stage": stage, "updated_at": utc_now()})
        return existing

    entry = {
        "key": key,
        "source_agent": source_agent,
        "label": challenger_label(source_agent, generation, session_index),
        "role": "challenger",
        "script": display_path(script_path),
        "active": True,
        "generation": generation,
        "parent_key": baseline_key(source_agent),
        "day": day,
        "stage": stage,
        "session_index": session_index,
        "supervisor": supervisor_label,
        "created_at": utc_now(),
    }
    registry.setdefault("agents", []).append(entry)
    return entry


def stage_key(day: str, stage: str, session_index: int | None) -> str:
    index = "full_day" if session_index is None else f"session_{session_index:02d}"
    return f"{day}:{index}:{stage}"


def entry_performance(entry: dict[str, Any]) -> dict[str, Any]:
    performance = entry.setdefault("performance", {})
    performance.setdefault("evaluations", 0)
    performance.setdefault("symbols", 0)
    performance.setdefault("wins", 0)
    performance.setdefault("total_pl", 0.0)
    performance.setdefault("worst_drawdown", 0.0)
    performance.setdefault("stage_keys", [])
    return performance


def update_agent_performance(
    registry: dict[str, Any],
    summary: dict[str, Any],
    *,
    day: str,
    stage: str,
    session_index: int | None,
) -> list[dict[str, Any]]:
    key = stage_key(day, stage, session_index)
    by_label = {str(entry.get("label")): entry for entry in registry.get("agents", []) if entry.get("label")}
    updated: list[dict[str, Any]] = []
    for row in summary.get("aggregate", []):
        entry = by_label.get(str(row.get("agent", "")))
        if not entry:
            continue
        performance = entry_performance(entry)
        seen = performance.setdefault("stage_keys", [])
        if key in seen:
            continue
        total_pl = float(row.get("total_pl", 0.0))
        wins = int(row.get("wins", 0))
        symbols = int(row.get("symbols", 0))
        worst_drawdown = float(row.get("worst_drawdown", 0.0))
        performance["evaluations"] = int(performance.get("evaluations", 0)) + 1
        performance["symbols"] = int(performance.get("symbols", 0)) + symbols
        performance["wins"] = int(performance.get("wins", 0)) + wins
        performance["total_pl"] = round(float(performance.get("total_pl", 0.0)) + total_pl, 4)
        performance["worst_drawdown"] = round(max(float(performance.get("worst_drawdown", 0.0)), worst_drawdown), 4)
        performance["last_pl"] = round(total_pl, 4)
        performance["last_wins"] = wins
        performance["last_symbols"] = symbols
        performance["last_stage"] = stage
        performance["last_session_index"] = session_index
        performance["last_seen_at"] = utc_now()
        seen.append(key)
        updated.append(entry)
    return updated


def agent_selection_score(entry: dict[str, Any]) -> float:
    performance = entry.get("performance") if isinstance(entry.get("performance"), dict) else {}
    total_pl = float(performance.get("total_pl", 0.0))
    worst_drawdown = float(performance.get("worst_drawdown", 0.0))
    wins = int(performance.get("wins", 0))
    symbols = max(int(performance.get("symbols", 0)), 1)
    return total_pl + wins * 0.25 - worst_drawdown * 0.10 + total_pl / symbols


def agent_selection_key(entry: dict[str, Any]) -> tuple[bool, float, int]:
    performance = entry.get("performance") if isinstance(entry.get("performance"), dict) else {}
    evaluations = int(performance.get("evaluations", 0))
    return (evaluations == 0, agent_selection_score(entry), int(entry.get("generation", 0)))


def deactivate_agent(entry: dict[str, Any], reason: str) -> dict[str, Any]:
    entry["active"] = False
    entry["deactivated_at"] = utc_now()
    entry["deactivation_reason"] = reason
    return {
        "label": str(entry.get("label")),
        "source_agent": str(entry.get("source_agent")),
        "generation": int(entry.get("generation", 0)),
        "reason": reason,
    }


def prune_losing_agents(
    registry: dict[str, Any],
    *,
    requested_sources: set[str] | None = None,
    min_evaluations: int = 2,
    loss_threshold: float = 0.0,
    keep_top_per_source: int = 3,
) -> list[dict[str, Any]]:
    deactivated: list[dict[str, Any]] = []
    sources = sorted(
        {
            str(entry.get("source_agent"))
            for entry in registry.get("agents", [])
            if entry.get("source_agent") and (requested_sources is None or entry.get("source_agent") in requested_sources)
        }
    )
    for source_agent in sources:
        active_challengers = [
            entry
            for entry in registry.get("agents", [])
            if entry.get("source_agent") == source_agent
            and entry.get("role") == "challenger"
            and entry.get("active", True)
        ]
        for entry in active_challengers:
            performance = entry_performance(entry)
            evaluations = int(performance.get("evaluations", 0))
            total_pl = float(performance.get("total_pl", 0.0))
            wins = int(performance.get("wins", 0))
            if evaluations >= min_evaluations and total_pl < loss_threshold and wins <= 0:
                deactivated.append(deactivate_agent(entry, f"loss threshold: total_pl={total_pl:.4f}, wins={wins}"))

        if keep_top_per_source <= 0:
            continue
        survivors = [
            entry
            for entry in active_challengers
            if entry.get("active", True)
        ]
        ranked = sorted(survivors, key=agent_selection_key, reverse=True)
        for entry in ranked[keep_top_per_source:]:
            performance = entry_performance(entry)
            if int(performance.get("evaluations", 0)) < min_evaluations:
                continue
            deactivated.append(deactivate_agent(entry, f"outside top {keep_top_per_source} active challengers"))
    return deactivated
