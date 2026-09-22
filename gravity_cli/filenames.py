"""Windows-safe filename construction for downloaded media.

Ported from Gravity's core/filesystem/FilenameSanitizer.{h,cpp}, which is the authority
there -- the vendored downloader.py's own sanitize_filename() is explicitly only a
defense-in-depth second pass, so a caller driving it directly has to do this itself.
"""

import os

_ILLEGAL_CHARS = set('<>:"/\\|?*')

_RESERVED_DEVICE_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{n}" for n in range(1, 10)]
    + [f"LPT{n}" for n in range(1, 10)]
)

_MAX_FILENAME_CODEPOINTS = 200
_MAX_DEDUPLICATION_ATTEMPTS = 10000

_LEGACY_MAX_PATH = 259  # MAX_PATH (260) minus the terminating null
# Headroom for a numbered dedup suffix (" (9999)") plus an extension plus the separator.
_PATH_RESERVE_FOR_SUFFIX_AND_EXTENSION = 20


class DeduplicationExhausted(Exception):
    pass


def _strip_surrogates(text: str) -> str:
    """Replaces lone UTF-16 surrogates with '_'.

    YouTube metadata has been observed to carry unpaired surrogates, which survive a JSON
    round-trip as real str characters but raise UnicodeEncodeError the moment they reach a
    filesystem call.
    """
    return "".join("_" if 0xD800 <= ord(c) <= 0xDFFF else c for c in text)


def _trim_trailing_dots_and_spaces(value: str) -> str:
    return value.rstrip(". ")


def sanitize_windows_filename(raw_title: str) -> str:
    repaired = _strip_surrogates(raw_title or "")
    result = "".join(
        "_" if c in _ILLEGAL_CHARS else c for c in repaired if ord(c) >= 0x20
    )

    result = _trim_trailing_dots_and_spaces(result)
    result = result[:_MAX_FILENAME_CODEPOINTS]
    # Truncation can expose a new trailing dot or space.
    result = _trim_trailing_dots_and_spaces(result)

    if not result:
        return "untitled"

    stem, extension = os.path.splitext(result)
    if stem.upper() in _RESERVED_DEVICE_NAMES:
        return f"{stem}_file{extension}"
    return result


def truncate_base_name_for_max_path(directory: str, base_name: str) -> str:
    overhead = len(directory) + 1 + _PATH_RESERVE_FOR_SUFFIX_AND_EXTENSION
    if overhead >= _LEGACY_MAX_PATH:
        return base_name  # nothing achievable by trimming the name

    allowed = _LEGACY_MAX_PATH - overhead
    encoded = base_name.encode("utf-8")
    if len(encoded) <= allowed:
        return base_name

    truncated = encoded[:allowed].decode("utf-8", errors="ignore")
    truncated = _trim_trailing_dots_and_spaces(truncated)
    return truncated or "untitled"


def deduplicate_base_name(directory: str, desired_base_name: str) -> str:
    """Finds a base name no existing file in `directory` already uses.

    Compares against stems rather than full names because the final container extension
    is not known until yt-dlp has finished merging.
    """
    try:
        existing = os.listdir(directory)
    except OSError:
        return desired_base_name  # directory does not exist yet: nothing to collide with

    stems = {os.path.splitext(name)[0] for name in existing}
    if desired_base_name not in stems:
        return desired_base_name

    for n in range(1, _MAX_DEDUPLICATION_ATTEMPTS + 1):
        candidate = f"{desired_base_name} ({n})"
        if candidate not in stems:
            return candidate

    raise DeduplicationExhausted(
        f"Could not find a free base filename for {desired_base_name!r} in {directory}"
    )


def with_playlist_index(filename: str, index: int, total_count: int) -> str:
    width = len(str(total_count if total_count > 0 else 1))
    return f"{index:0{width}d} - {filename}"
