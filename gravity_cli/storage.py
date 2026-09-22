"""Shared on-disk storage helpers for config and history.

The atomic temp-file-then-rename write is the pattern Gravity's JobHistoryStore uses: a
crash mid-write must not leave a truncated JSON file that the next run refuses to read.
"""

import json
import os
import tempfile
from pathlib import Path

from platformdirs import user_data_dir

APP_NAME = "gravity-cli"


def data_dir() -> Path:
    path = Path(user_data_dir(APP_NAME, appauthor=False))
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_json(path: Path, default):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return default


def write_json_atomic(path: Path, payload) -> bool:
    """Best-effort: a failure to persist preferences must never take down a download."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, indent=2, ensure_ascii=False)
            os.replace(temp_path, path)
        except BaseException:
            os.unlink(temp_path)
            raise
        return True
    except OSError:
        return False
