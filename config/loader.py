"""
Configuration loader for Indian_Crypto_Bot.
Loads YAML/JSON config with environment variable substitution.
Validates required fields at startup.
"""

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Default config values
DEFAULTS = {
    "exchange": {
        "name": "binance",
        "trading_mode": "futures",
        "margin_mode": "cross",
    },
    "telegram": {
        "enabled": True,
    },
    "strategy": {
        "timeframe": "2h",
        "max_open_trades": 4,
        "stake_currency": "USDT",
        "stoploss": -0.99,
    },
    "pairlists": [
        {"method": "StaticPairList", "pairs": ["TRX/USDT:USDT"]}
    ],
    "internals": {
        "process_throttle_secs": 5,
        "heartbeat_interval": 60,
    },
    "dry_run": False,
    "dry_run_wallet": 1000.0,
}


def _substitute_env_vars(value: str) -> str:
    """Replace ${VAR_NAME} patterns with environment variable values."""
    pattern = r"\$\{([^}]+)\}"

    def replacer(match):
        var_name = match.group(1)
        env_val = os.environ.get(var_name)
        if env_val is None:
            logger.warning(f"Environment variable {var_name} not set")
            return match.group(0)
        return env_val

    return re.sub(pattern, replacer, value)


def _walk_and_substitute(obj: Any) -> Any:
    """Recursively substitute env vars in all string values."""
    if isinstance(obj, str):
        return _substitute_env_vars(obj)
    elif isinstance(obj, dict):
        return {k: _walk_and_substitute(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_walk_and_substitute(item) for item in obj]
    return obj


def _deep_merge(base: dict, override: dict) -> dict:
    """Deep merge override into base, preserving nested defaults."""
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(config_path: Optional[str] = None) -> dict:
    """
    Load configuration from file with env var substitution and defaults.

    Args:
        config_path: Path to config.yaml or config.json. If None, uses CONFIG_PATH env var.

    Returns:
        Merged config dict with all defaults applied and env vars substituted.

    Raises:
        FileNotFoundError: If config file doesn't exist and no default found.
        ValueError: If required fields are missing after loading.
    """
    if config_path is None:
        config_path = os.environ.get("CONFIG_PATH", "config.yaml")

    path = Path(config_path)
    user_config = {}

    if path.exists():
        content = path.read_text()
        if path.suffix in (".yaml", ".yml"):
            try:
                import yaml
                user_config = yaml.safe_load(content) or {}
            except ImportError:
                logger.warning("PyYAML not installed, trying JSON parse for .yaml file")
                user_config = json.loads(content)
        elif path.suffix == ".json":
            user_config = json.loads(content)
        else:
            raise ValueError(f"Unsupported config format: {path.suffix}")
        logger.info(f"Loaded config from {path}")
    else:
        logger.warning(f"Config file {path} not found, using defaults + env vars")

    # Merge with defaults
    config = _deep_merge(DEFAULTS, user_config)

    # Substitute environment variables
    config = _walk_and_substitute(config)

    # Validate required fields
    _validate_config(config)

    return config


def _validate_config(config: dict) -> None:
    """Validate that required config fields are present and valid."""
    errors = []

    # Exchange credentials (required unless dry_run)
    if not config.get("dry_run", False):
        exchange = config.get("exchange", {})
        if not exchange.get("api_key"):
            errors.append("exchange.api_key is required for live trading")
        if not exchange.get("secret_key"):
            errors.append("exchange.secret_key is required for live trading")

    # Telegram (required if enabled)
    telegram = config.get("telegram", {})
    if telegram.get("enabled", True):
        if not telegram.get("token"):
            errors.append("telegram.token is required when telegram is enabled")
        if not telegram.get("chat_id"):
            errors.append("telegram.chat_id is required when telegram is enabled")

    # Strategy
    strategy = config.get("strategy", {})
    if not strategy.get("name"):
        errors.append("strategy.name is required")

    # Pairlists
    pairlists = config.get("pairlists", [])
    if not pairlists:
        errors.append("At least one pairlist method is required")

    if errors:
        raise ValueError(f"Config validation failed:\n" + "\n".join(f"  - {e}" for e in errors))

    logger.info("Config validation passed")