"""Locates ffmpeg, which yt-dlp needs to merge separate video and audio streams.

Anything above the lowest quality on YouTube is served as separate streams, so without
ffmpeg a download fails late -- after the bytes are already on disk -- with
E_MERGE_TOOL_MISSING. Checking up front turns that into a sentence the user can act on.
"""

import shutil
from typing import Optional

MISSING_MESSAGE = (
    "ffmpeg was not found on your PATH. yt-dlp needs it to merge the separate video and "
    "audio streams YouTube serves, so anything above the lowest quality will fail without "
    "it. Install it (winget install Gyan.FFmpeg) and reopen your terminal."
)


def find_ffmpeg() -> Optional[str]:
    return shutil.which("ffmpeg")
