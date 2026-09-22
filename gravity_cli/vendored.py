"""Locates and loads the vendored copy of Gravity's downloader.py.

Loaded by explicit path rather than by import so it works regardless of the working
directory the CLI was launched from, and so the vendored file can stay byte-identical to
its upstream (it is a standalone script, not a package module).
"""

import importlib.util
from pathlib import Path

DOWNLOADER_SCRIPT = Path(__file__).resolve().parents[1] / "vendor" / "downloader.py"

_module = None


def load_downloader_module():
    """Imports the vendored script as a module, for reuse of its pure helper functions."""
    global _module
    if _module is None:
        spec = importlib.util.spec_from_file_location("gravity_vendored_downloader",
                                                       DOWNLOADER_SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _module = module
    return _module
