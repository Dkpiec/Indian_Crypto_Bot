"""
Dynamic strategy discovery and loading for Indian_Crypto_Bot.
Scans strategies/ directory for IStrategy implementations.
"""

import importlib
import logging
import sys
from pathlib import Path
from typing import Type

logger = logging.getLogger(__name__)

STRATEGIES_DIR = Path(__file__).parent.parent.parent / "strategies"


def load_strategy(name: str) -> Type:
    """
    Load a strategy class by name from the strategies/ directory.

    Args:
        name: Strategy class name (e.g., "ATR_CHANNEL_2H")

    Returns:
        The strategy class (not an instance).

    Raises:
        FileNotFoundError: If strategy module doesn't exist.
        AttributeError: If class not found in module.
    """
    module_name = f"strategies.{name}"
    file_path = STRATEGIES_DIR / f"{name}.py"

    if not file_path.exists():
        raise FileNotFoundError(f"Strategy file not found: {file_path}")

    # Ensure strategies/ is importable
    strategies_parent = str(STRATEGIES_DIR.parent)
    if strategies_parent not in sys.path:
        sys.path.insert(0, strategies_parent)

    try:
        module = importlib.import_module(module_name)
    except ImportError as e:
        raise ImportError(f"Failed to import strategy {name}: {e}")

    cls = getattr(module, name, None)
    if cls is None:
        raise AttributeError(f"Class {name} not found in {module_name}")

    logger.info(f"Loaded strategy: {name}")
    return cls


def list_strategies() -> list[str]:
    """List all available strategy names in strategies/ directory."""
    if not STRATEGIES_DIR.exists():
        return []
    return [
        p.stem for p in STRATEGIES_DIR.glob("*.py")
        if not p.name.startswith("_")
    ]