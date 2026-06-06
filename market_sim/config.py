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
    "market_order_price_buffer": 0.02,
    "api_rate_limit_enabled": True,
    "api_rate_limit_per_second": 25,
    "api_rate_limit_per_minute": 900,
    "tick_size": 0.01,
    "maker_fee_rate": 0.00005,
    "taker_fee_rate": 0.0002,
    "fundamental_volatility": 0.00045,
    "volatility_decay": 0.94,
    "volatility_clustering": 0.65,
    "min_realized_volatility": 0.00008,
    "max_realized_volatility": 0.0045,
    "trend_per_tick": 0.0,
    "mean_reversion": 0.00005,
    "news_probability": 0.015,
    "news_min": 0.0015,
    "news_max": 0.008,
    "news_decay": 0.82,
    "order_flow_price_impact": 0.000035,
    "liquidity_probability": 0.55,
    "depth_scale": 1.0,
    "spread_scale": 1.0,
    "order_cancel_probability": 0.018,
    "queue_decay_strength": 0.12,
    "liquidity_resilience": 0.08,
    "latent_liquidity": True,
    "latent_liquidity_depth": 3200.0,
    "latent_min_fill_fraction": 0.18,
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
        "max_realized_volatility": 0.0016,
        "news_probability": 0.004,
        "liquidity_probability": 0.72,
        "depth_scale": 1.45,
        "spread_scale": 0.75,
        "order_cancel_probability": 0.010,
        "latent_liquidity_depth": 5200.0,
    },
    "high_volatility": {
        "fundamental_volatility": 0.0012,
        "volatility_clustering": 0.88,
        "max_realized_volatility": 0.009,
        "news_probability": 0.04,
        "news_min": 0.004,
        "news_max": 0.018,
        "liquidity_probability": 0.42,
        "depth_scale": 0.72,
        "spread_scale": 1.8,
        "order_cancel_probability": 0.042,
        "latent_liquidity_depth": 2200.0,
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
        "volatility_clustering": 0.92,
        "max_realized_volatility": 0.011,
        "liquidity_probability": 0.38,
        "depth_scale": 0.62,
        "spread_scale": 2.2,
        "order_cancel_probability": 0.055,
        "latent_liquidity_depth": 1800.0,
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
        "order_cancel_probability": 0.065,
        "queue_decay_strength": 0.22,
        "latent_liquidity_depth": 950.0,
        "latent_liquidity": True,
        "max_reference_deviation": 0.06,
        "max_latent_trade_deviation": 0.18,
    },
    "mean_reverting": {
        "fundamental_volatility": 0.00055,
        "mean_reversion": 0.00045,
        "trend_per_tick": 0.0,
        "news_probability": 0.008,
        "order_flow_price_impact": 0.000018,
    },
    "news_shock": {
        "fundamental_volatility": 0.0005,
        "volatility_clustering": 0.82,
        "max_realized_volatility": 0.008,
        "news_probability": 0.08,
        "news_min": 0.006,
        "news_max": 0.025,
        "news_decay": 0.88,
        "spread_scale": 1.5,
        "order_cancel_probability": 0.038,
        "latent_liquidity_depth": 2400.0,
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
