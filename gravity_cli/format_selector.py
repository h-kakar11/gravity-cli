"""Quality preset -> concrete yt-dlp format selector string.

Ported from Gravity's engines/downloader/YtDlpFormatSelector.cpp. The vendored
downloader.py never interprets a preset itself; it executes whatever selector string it
is handed, so this is the only place the translation happens.
"""

BEST = "BEST"
P2160 = "2160P"
P1440 = "1440P"
P1080 = "1080P"
P720 = "720P"
P480 = "480P"
AUDIO_ONLY = "AUDIO_ONLY"

PRESETS = (BEST, P2160, P1440, P1080, P720, P480, AUDIO_ONLY)

PRESET_LABELS = {
    BEST: "Best available",
    P2160: "2160p (4K)",
    P1440: "1440p (2K)",
    P1080: "1080p",
    P720: "720p",
    P480: "480p",
    AUDIO_ONLY: "Audio only (no video)",
}

_SELECTORS = {
    BEST: "bestvideo*+bestaudio/best",
    P2160: "bestvideo[height<=2160]+bestaudio/best[height<=2160]",
    P1440: "bestvideo[height<=1440]+bestaudio/best[height<=1440]",
    P1080: "bestvideo[height<=1080]+bestaudio/best[height<=1080]",
    P720: "bestvideo[height<=720]+bestaudio/best[height<=720]",
    P480: "bestvideo[height<=480]+bestaudio/best[height<=480]",
    # No "/best" fallback: that fallback means "best combined format if no pure-audio
    # format exists", which can silently hand back a full video file for an audio request.
    AUDIO_ONLY: "bestaudio",
}


def format_selector_for_quality(preset: str) -> str:
    return _SELECTORS.get(preset, _SELECTORS[BEST])
