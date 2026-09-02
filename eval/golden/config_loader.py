"""Layered configuration loading: defaults, then a file, then the environment."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import yaml

ENV_PREFIX = "APP_"
DEFAULTS: dict[str, Any] = {
    "host": "127.0.0.1",
    "port": 8000,
    "debug": False,
    "workers": 4,
    "log_level": "INFO",
}
REQUIRED_KEYS = ("host", "port")


class ConfigError(Exception):
    """Raised when configuration is missing, malformed or invalid."""


def _coerce(value: str) -> Any:
    lowered = value.strip().lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if lowered in {"none", "null", ""}:
        return None
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def load_file(path: Path) -> dict[str, Any]:
    """Read YAML or JSON. YAML is parsed with the safe loader, never the full one."""
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return {}
    if path.suffix in {".yaml", ".yml"}:
        data = yaml.safe_load(text)
    elif path.suffix == ".json":
        data = json.loads(text)
    else:
        raise ConfigError(f"unsupported config format: {path.suffix!r}")
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError("config file must contain a mapping at the top level")
    return data


def from_env(environ: dict[str, str] | None = None) -> dict[str, Any]:
    source = os.environ if environ is None else environ
    return {
        key[len(ENV_PREFIX) :].lower(): _coerce(value)
        for key, value in source.items()
        if key.startswith(ENV_PREFIX)
    }


def validate(config: dict[str, Any]) -> dict[str, Any]:
    missing = [key for key in REQUIRED_KEYS if config.get(key) is None]
    if missing:
        raise ConfigError(f"missing required config keys: {', '.join(missing)}")
    if not 1 <= int(config["port"]) <= 65535:
        raise ConfigError("port must be between 1 and 65535")
    return config


def load(path: Path | None = None, environ: dict[str, str] | None = None) -> dict[str, Any]:
    """Later layers win: defaults < file < environment."""
    merged: dict[str, Any] = dict(DEFAULTS)
    if path is not None:
        merged.update(load_file(path))
    merged.update(from_env(environ))
    return validate(merged)
