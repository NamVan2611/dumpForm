"""
core.py
-------
Shared contracts, error types, and logging helpers for the project.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ProjectError(Exception):
    """Base project error."""


class ConfigError(ProjectError):
    """Configuration loading/validation error."""


class ParserError(ProjectError):
    """Form parser specific error."""


class FillerError(ProjectError):
    """Form filler specific error."""


class RatioError(ProjectError):
    """Ratio engine specific error."""


@dataclass(frozen=True)
class Config:
    """Runtime config loaded from `config.json`."""

    headless: bool = True
    delay_min: float = 2.0
    delay_max: float = 5.0
    retry: int = 3


def get_logger(name: str = "dumpForm") -> logging.Logger:
    """
    Return a lightweight console logger.
    """
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    logger.addHandler(handler)
    logger.propagate = False
    return logger


def load_config(path: str = "config.json") -> Config:
    """Load and validate config file with safe defaults."""
    cfg_path = Path(path)
    if not cfg_path.exists():
        return Config()

    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise ConfigError(f"Cannot read config file '{path}': {exc}") from exc

    if not isinstance(data, dict):
        raise ConfigError("config.json must contain a JSON object.")

    headless = bool(data.get("headless", True))
    try:
        delay_min = float(data.get("delay_min", 2))
        delay_max = float(data.get("delay_max", 5))
    except (TypeError, ValueError) as exc:
        raise ConfigError("delay_min and delay_max must be numeric.") from exc
    if delay_min < 0 or delay_max < 0:
        raise ConfigError("delay_min and delay_max must be >= 0.")
    if delay_max < delay_min:
        delay_min, delay_max = delay_max, delay_min

    try:
        retry = int(data.get("retry", 3))
    except (TypeError, ValueError) as exc:
        raise ConfigError("retry must be an integer.") from exc
    if retry < 0:
        raise ConfigError("retry must be >= 0.")

    return Config(
        headless=headless,
        delay_min=delay_min,
        delay_max=delay_max,
        retry=retry,
    )


@dataclass(frozen=True)
class OperationResult:
    """Normalized success/failure result object."""

    success: bool
    message: str
    data: dict[str, Any] | None = None

