"""YouTube URL classification, and enumeration of auto-generated Mixes.

The classification helpers are Gravity's own, reused directly from the vendored
downloader.py (they are pure urllib string logic with no yt-dlp coupling).

Where this deliberately diverges from Gravity: Gravity refuses a true auto-generated Mix
outright, because a Mix is an endless synthesized radio with no "all of it" to download.
gravity-cli supports them anyway, but capped and clearly labelled -- see probe_mix().
"""

import urllib.parse
from dataclasses import dataclass
from typing import Optional

from .bridge import DownloadError, PlaylistInfo, playlist_from_payload
from .vendored import load_downloader_module

VIDEO = "video"
PLAYLIST = "playlist"
MIX = "mix"

DEFAULT_MIX_LIMIT = 50


@dataclass
class UrlKind:
    kind: str
    url: str
    # Set when a single video is also reachable from this URL, i.e. the user could mean
    # "just this one" instead of the list. None for a bare playlist link.
    video_url: Optional[str] = None
    mix_id: Optional[str] = None


def _has_video_component(url: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(url.strip())
    except ValueError:
        return False
    if (parsed.hostname or "").lower() in ("youtu.be", "www.youtu.be"):
        return bool(parsed.path.strip("/"))
    return any(key == "v" and value
               for key, value in urllib.parse.parse_qsl(parsed.query))


def classify(url: str) -> UrlKind:
    """Decides whether a pasted link names a video, a real playlist, or a Mix.

    A playlist-seeded mix (RDAMPL<playlist id>) normalizes back to the real playlist it
    was seeded from, so it is reported as an ordinary playlist.
    """
    downloader = load_downloader_module()
    url = url.strip()
    normalized = downloader.normalize_playlist_url(url)
    mix_id = downloader.auto_generated_mix_id(normalized)
    list_id = downloader._youtube_list_id(normalized)
    # The original URL is what a single-video download uses: the vendored script runs with
    # noplaylist=True, which resolves a combo watch?v=X&list=Y link to just the video.
    video_url = url if _has_video_component(url) else None

    if mix_id is not None:
        return UrlKind(MIX, normalized, video_url, mix_id)
    if list_id:
        return UrlKind(PLAYLIST, normalized, video_url)
    return UrlKind(VIDEO, url, url)


MIX_DISCLAIMER = (
    "This is a YouTube auto-generated Mix, not a real playlist. YouTube synthesizes it "
    "endlessly around a seed track, so it has no fixed contents or order: what you get is "
    "whatever YouTube is serving right now, it can differ between runs, and it can repeat "
    "tracks. Only the first {limit} entries will be downloaded."
)


def probe_mix(url: str, limit: int = DEFAULT_MIX_LIMIT) -> PlaylistInfo:
    """Enumerates up to `limit` entries of an auto-generated Mix.

    Runs in-process rather than through the subprocess bridge because the vendored
    script's inspectPlaylist command refuses Mixes by design; this reuses that script's own
    probe options and payload builder, only skipping its mix gate.
    """
    downloader = load_downloader_module()
    if downloader.yt_dlp is None:
        raise DownloadError("E_DOWNLOADER_NOT_FOUND", "ENGINE_FAILURE",
                            "yt-dlp is not installed in this environment.")

    opts = dict(downloader._PLAYLIST_PROBE_OPTS)
    opts["playlistend"] = limit
    try:
        with downloader.yt_dlp.YoutubeDL(opts) as probe:
            info = probe.extract_info(url, download=False)
        payload = downloader.build_playlist_payload(info, url)
    except Exception as exc:
        code, category, recoverable = downloader.classify_exception(exc)
        raise DownloadError(code, category, str(exc) or exc.__class__.__name__,
                            recoverable) from exc

    # Entries past the cap are the Mix's endlessness, not a truncated playlist -- the
    # disclaimer already says only `limit` are taken, so this isn't reported as loss.
    playlist = playlist_from_payload(payload)
    playlist.truncated = False
    return playlist
