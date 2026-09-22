"""Sequential download orchestration for playlists and Mixes.

One entry at a time, in order -- the same shape as Gravity's runAfter-chained playlist
jobs. A failed entry is recorded and the loop moves on, so one private or deleted video
costs exactly one video rather than the rest of the list.
"""

import os
from dataclasses import dataclass
from typing import Callable, Optional

from . import bridge, filenames, format_selector, history
from .bridge import DownloadError

ENTRY_KINDS = {"playlist": "playlist-entry", "mix": "mix-entry"}


@dataclass
class ItemResult:
    index: int
    title: str
    url: str
    status: str
    output_path: str = ""
    error: str = ""


def suggest_folder_name(title: str) -> str:
    return filenames.sanitize_windows_filename(title or "playlist")


def resolve_filename_base(output_dir: str, title: str,
                          index: Optional[int] = None,
                          total: Optional[int] = None) -> str:
    """Builds a collision-free, Windows-safe base name (no extension).

    The playlist number is applied before the MAX_PATH trim so the trim can never cut it
    off, and deduplication runs last so the name it clears is the final one.
    """
    base = filenames.sanitize_windows_filename(title)
    if index is not None:
        base = filenames.with_playlist_index(base, index, total or index)
    base = filenames.truncate_base_name_for_max_path(output_dir, base)
    return filenames.deduplicate_base_name(output_dir, base)


def download_item(url: str, output_dir: str, quality: str, title: str,
                  ffmpeg_location: Optional[str] = None,
                  index: Optional[int] = None, total: Optional[int] = None,
                  on_progress: Optional[Callable[[bridge.Progress], None]] = None) -> str:
    os.makedirs(output_dir, exist_ok=True)
    base = resolve_filename_base(output_dir, title, index, total)
    return bridge.download(
        url=url,
        output_dir=output_dir,
        format_selector=format_selector.format_selector_for_quality(quality),
        filename_base=base,
        on_progress=on_progress,
        ffmpeg_location=ffmpeg_location,
    )


def download_playlist(playlist, output_dir: str, quality: str, kind: str,
                      ffmpeg_location: Optional[str] = None,
                      on_item_start: Optional[Callable[[int, int, str], None]] = None,
                      on_progress: Optional[Callable[[bridge.Progress], None]] = None,
                      on_item_finish: Optional[Callable[[ItemResult], None]] = None) -> list:
    results = []
    total = len(playlist.entries)
    entry_kind = ENTRY_KINDS.get(kind, "playlist-entry")

    for position, entry in enumerate(playlist.entries, start=1):
        if on_item_start:
            on_item_start(position, total, entry.title)

        try:
            output_path = download_item(
                url=entry.url,
                output_dir=output_dir,
                quality=quality,
                title=entry.title,
                ffmpeg_location=ffmpeg_location,
                index=entry.index,
                total=total,
                on_progress=on_progress,
            )
            result = ItemResult(entry.index, entry.title, entry.url,
                                history.COMPLETED, output_path=output_path)
        except DownloadError as exc:
            result = ItemResult(entry.index, entry.title, entry.url,
                                history.FAILED, error=f"{exc.code}: {exc.message}")

        history.record(
            url=entry.url,
            title=entry.title,
            kind=entry_kind,
            quality=quality,
            status=result.status,
            output_path=result.output_path,
            error=result.error or None,
            source=playlist.title,
        )
        results.append(result)
        if on_item_finish:
            on_item_finish(result)

    return results
