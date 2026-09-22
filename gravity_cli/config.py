"""Persisted user preferences: where downloads go, default quality, Mix entry cap."""

from pathlib import Path

from . import format_selector
from .mixes import DEFAULT_MIX_LIMIT
from .storage import data_dir, read_json, write_json_atomic

CONFIG_FILENAME = "config.json"

MAX_MIX_LIMIT = 500


def _default_output_directory() -> str:
    return str(Path.home() / "Videos" / "Gravity")


def config_path() -> Path:
    return data_dir() / CONFIG_FILENAME


def load() -> dict:
    stored = read_json(config_path(), {})
    if not isinstance(stored, dict):
        stored = {}
    return {
        "outputDirectory": stored.get("outputDirectory") or _default_output_directory(),
        "quality": stored.get("quality") if stored.get("quality") in format_selector.PRESETS
                    else format_selector.BEST,
        "mixLimit": _clamp_mix_limit(stored.get("mixLimit")),
    }


def save(settings: dict) -> bool:
    return write_json_atomic(config_path(), settings)


def _clamp_mix_limit(value) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return DEFAULT_MIX_LIMIT
    return max(1, min(MAX_MIX_LIMIT, limit))
