"""Download history: a bounded, newest-first record of what was downloaded and when.

Mirrors Gravity's JobHistoryStore (a capped JSON ring buffer written atomically), scoped
down to what a CLI needs.
"""

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .storage import data_dir, read_json, write_json_atomic

HISTORY_FILENAME = "history.json"
MAX_ENTRIES = 500

COMPLETED = "COMPLETED"
FAILED = "FAILED"


def history_path() -> Path:
    return data_dir() / HISTORY_FILENAME


def load() -> list:
    stored = read_json(history_path(), [])
    return stored if isinstance(stored, list) else []


def record(url: str, title: str, kind: str, quality: str, status: str,
           output_path: str = "", error: Optional[str] = None,
           source: Optional[str] = None) -> bool:
    entry = {
        "url": url,
        "title": title,
        "kind": kind,
        "quality": quality,
        "status": status,
        "outputPath": output_path,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if error:
        entry["error"] = error
    if source:
        entry["source"] = source

    entries = load()
    entries.insert(0, entry)
    return write_json_atomic(history_path(), entries[:MAX_ENTRIES])
