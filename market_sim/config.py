from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any


DEFAULT_CONFIG: dict[str, Any] = {
    "api_starting_cash": 10000.0,
    "enforce_risk_limits": True,
    "allow_short": True,
    "max_order_quantity": 1_000_000.0,
    "max_position_abs": 1_000_000.0,
    "maker_fee_rate": 0.00005,
    "taker_fee_rate": 0.0002,
    "fundamental_volatility": 0.00045,
    "trend_per_tick": 0.0,
    "mean_reversion": 0.00005,
    "news_probability": 0.015,
    "news_min": 0.0015,
    "news_max": 0.008,
    "liquidity_probability": 0.55,
    "depth_scale": 1.0,
    "spread_scale": 1.0,
    "latent_liquidity": True,
    "seed_order_book": True,
    "max_reference_deviation": 0.04,
    "max_resting_order_deviation": 0.08,
    "prune_api_outlier_orders": False,
    "max_latent_trade_deviation": 0.12,
    "background_last_price_weight": 0.04,
    "agent_counts": {
        "institutional": [3, 6],
        "high_frequency": [6, 14],
        "random": [24, 46],
    },
    "flash_crash": {
        "enabled": False,
        "tick": 80,
        "shock": -0.08,
        "recovery": 0.0008,
    },
}


SCENARIOS: dict[str, dict[str, Any]] = {
    "default": {},
    "calm": {
        "fundamental_volatility": 0.00018,
        "news_probability": 0.004,
        "liquidity_probability": 0.72,
        "depth_scale": 1.45,
        "spread_scale": 0.75,
    },
    "high_volatility": {
        "fundamental_volatility": 0.0012,
        "news_probability": 0.04,
        "news_min": 0.004,
        "news_max": 0.018,
        "liquidity_probability": 0.42,
        "depth_scale": 0.72,
        "spread_scale": 1.8,
        "max_reference_deviation": 0.06,
        "max_latent_trade_deviation": 0.18,
    },
    "trending_up": {
        "trend_per_tick": 0.00018,
        "fundamental_volatility": 0.00038,
        "news_probability": 0.012,
    },
    "trending_down": {
        "trend_per_tick": -0.00018,
        "fundamental_volatility": 0.00038,
        "news_probability": 0.012,
    },
    "flash_crash": {
        "fundamental_volatility": 0.0007,
        "liquidity_probability": 0.38,
        "depth_scale": 0.62,
        "spread_scale": 2.2,
        "max_reference_deviation": 0.07,
        "max_latent_trade_deviation": 0.2,
        "flash_crash": {
            "enabled": True,
            "tick": 80,
            "shock": -0.09,
            "recovery": 0.0012,
        },
    },
    "liquidity_drought": {
        "liquidity_probability": 0.18,
        "depth_scale": 0.35,
        "spread_scale": 2.6,
        "latent_liquidity": True,
        "max_reference_deviation": 0.06,
        "max_latent_trade_deviation": 0.18,
    },
    "mean_reverting": {
        "fundamental_volatility": 0.00055,
        "mean_reversion": 0.00045,
        "trend_per_tick": 0.0,
        "news_probability": 0.008,
    },
    "news_shock": {
        "fundamental_volatility": 0.0005,
        "news_probability": 0.08,
        "news_min": 0.006,
        "news_max": 0.025,
        "spread_scale": 1.5,
        "max_reference_deviation": 0.07,
        "max_latent_trade_deviation": 0.2,
    },
}


def build_config(scenario: str = "default", config_path: str | None = None) -> dict[str, Any]:
    if scenario not in SCENARIOS:
        available = ", ".join(sorted(SCENARIOS))
        raise ValueError(f"unknown scenario '{scenario}'. Available scenarios: {available}")
    config = deepcopy(DEFAULT_CONFIG)
    _deep_update(config, deepcopy(SCENARIOS[scenario]))
    if config_path:
        with Path(config_path).expanduser().open("r", encoding="utf-8") as handle:
            user_config = json.load(handle)
        if not isinstance(user_config, dict):
            raise ValueError("config file must contain a JSON object")
        _deep_update(config, user_config)
    config["scenario"] = scenario
    return config


def _deep_update(base: dict[str, Any], override: dict[str, Any]) -> None:
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_update(base[key], value)
        else:
            base[key] = value
