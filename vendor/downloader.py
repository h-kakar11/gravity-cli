"""Bridges yt-dlp to MediaTool's NDJSON download protocol over stdio.

Protocol: docs/protocols/downloader.md (summarized in docs/ipc-contract.md). Only the
standard library plus yt_dlp may be imported here (see requirements.txt) -- the venv this
runs in provides nothing else.

Three modes, mutually exclusive:
  --selftest       emits a canned event sequence, no network access, exits 0.
  --command-stdin  reads one JSON command line from stdin, then acts on it:
                      {"command": "version", "params": {}}
                      {"command": "inspect", "params": {"url": ...}}
                      {"command": "download", "params": {"url", "outputDir",
                                                          "formatSelector", "filenameBase",
                                                          "ffmpegLocation"?}}

Every stdout line is exactly one JSON object (json.dumps + flush). Anything else
(yt-dlp's own log chatter, debug output) goes to stderr instead -- stdout is reserved for
the NDJSON protocol so the C++ core never has to distinguish protocol lines from noise.

Format selection and quality presets are NOT resolved here: engines/downloader (C++) is
the only place that translates an application-level QualityPreset into a concrete yt-dlp
selector string (spec section 10) -- this script just executes whatever `formatSelector`
it's given.
"""

import argparse
import json
import os
import re
import socket
import sys
import traceback
import urllib.error
import urllib.parse

try:
    import yt_dlp
except ImportError:  # --selftest never touches yt_dlp, so this must not be fatal here
    yt_dlp = None


_ILLEGAL_WINDOWS_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class DownloaderError(Exception):
    """Base for errors this script classifies itself, bypassing the exit-message
    heuristics in classify_exception() -- raised with a code/category already decided."""

    def __init__(self, code: str, category: str, message: str, recoverable: bool = False):
        super().__init__(message)
        self.code = code
        self.category = category
        self.recoverable = recoverable


def emit(event: str, data: dict) -> None:
    print(json.dumps({"event": event, "data": data}), flush=True)


def sanitize_filename(name: str) -> str:
    """Best-effort local fallback; core/filesystem/FilenameSanitizer is authoritative."""
    cleaned = _ILLEGAL_WINDOWS_CHARS.sub("_", name or "")
    cleaned = cleaned.rstrip(" .")
    return cleaned or "download"


# (message substring, code, category, recoverable) -- checked in order, first match wins.
# This is a heuristic over yt-dlp's exception text (it doesn't expose a stable error-code
# enum), not a guarantee every phrasing yt-dlp ever uses is covered -- unmatched failures
# fall through to the generic E_DOWNLOAD_FAILED/UNKNOWN classification below.
_KNOWN_FAILURE_PATTERNS = (
    ("private video", "E_VIDEO_PRIVATE", "PERMISSION_ERROR", False),
    ("sign in", "E_VIDEO_PRIVATE", "PERMISSION_ERROR", False),
    ("this video is unavailable", "E_VIDEO_UNAVAILABLE", "DOWNLOAD_FAILURE", False),
    ("video unavailable", "E_VIDEO_UNAVAILABLE", "DOWNLOAD_FAILURE", False),
    ("has been removed", "E_VIDEO_REMOVED", "DOWNLOAD_FAILURE", False),
    ("account associated with this video has been terminated", "E_VIDEO_REMOVED", "DOWNLOAD_FAILURE", False),
    ("not available in your country", "E_GEO_RESTRICTED", "PERMISSION_ERROR", False),
    ("not made this video available in your country", "E_GEO_RESTRICTED", "PERMISSION_ERROR", False),
    ("requested format is not available", "E_FORMAT_UNAVAILABLE", "UNSUPPORTED_FORMAT", False),
    ("no video formats found", "E_FORMAT_UNAVAILABLE", "UNSUPPORTED_FORMAT", False),
    ("unsupported url", "E_UNSUPPORTED_URL", "UNSUPPORTED_FORMAT", False),
    ("no space left on device", "E_DISK_SPACE", "DISK_SPACE_ERROR", False),
    ("permission denied", "E_PERMISSION_DENIED", "PERMISSION_ERROR", False),

    # --- Merge / post-processing -------------------------------------------------------
    # Everything above fails before a single byte is written. These fail AFTER the bytes
    # are on disk, in yt-dlp's own ffmpeg step, and they split cleanly into two kinds --
    # which is the whole reason they are classified separately instead of falling through
    # to E_DOWNLOAD_FAILED/UNKNOWN, where a retry policy has nothing to go on.
    #
    #   Permanent: the merge tool is missing, or ffmpeg rejected the streams. Running the
    #   exact same command again produces the exact same failure; the user has to install
    #   ffmpeg or pick a different format. Retrying only wastes the download.
    #
    #   Recoverable: a fragment could not be fetched. That is a transport failure wearing
    #   a download-stage costume, and a second attempt genuinely often works.
    #
    # Substrings verified against yt-dlp's own source (checked at 2026.8.19):
    # YoutubeDL.py "You have requested merging of multiple formats but ffmpeg is not
    # installed" and f"Postprocessing: {err}"; postprocessor/ffmpeg.py "ffmpeg not found.
    # Please install...", "ffprobe and ffmpeg not found...", "ffprobe not found...",
    # f"audio conversion failed: {err.msg}"; downloader/external.py f"Unable to open
    # fragment {frag_index}; {err}"; downloader/fragment.py f"fragment {frag_index} not
    # found, unable to continue".
    ("but ffmpeg is not installed", "E_MERGE_TOOL_MISSING", "ENGINE_FAILURE", False),
    ("ffmpeg not found. please install", "E_MERGE_TOOL_MISSING", "ENGINE_FAILURE", False),
    ("ffprobe not found. please install", "E_MERGE_TOOL_MISSING", "ENGINE_FAILURE", False),
    ("unable to open fragment", "E_FRAGMENT_DOWNLOAD_FAILED", "NETWORK_ERROR", True),
    ("not found, unable to continue", "E_FRAGMENT_MISSING", "DOWNLOAD_FAILURE", False),
    ("audio conversion failed", "E_MERGE_FAILED", "ENGINE_FAILURE", False),
    # Last of the merge group on purpose: yt-dlp prefixes the *underlying* message with
    # "Postprocessing: ", so a more specific pattern above (a missing ffmpeg, say) must
    # get the first look at the same string.
    ("postprocessing:", "E_MERGE_FAILED", "ENGINE_FAILURE", False),
)

# Per-socket-operation bound applied to every yt-dlp call this script makes. It is NOT a
# bound on a whole fetch -- a fetch is many socket operations and yt-dlp retries each one --
# which is why the C++ side layers a wall-clock deadline on top for inspect (see
# engines/downloader/YtDlpProvider.h). This one exists so a single stalled connect or read
# cannot hang forever underneath that deadline. One constant so the two call sites cannot
# drift apart.
_SOCKET_TIMEOUT_SECONDS = 30

# A timeout is a network failure, but not an interchangeable one: it is the network failure
# most likely to succeed on a second attempt, and the retry policy reads the code as well as
# the flag. Split out so "the host stopped responding" and "the host does not exist" are not
# the same event.
_TIMEOUT_TYPES = (socket.timeout, TimeoutError)
_NETWORK_TYPES = (socket.gaierror, ConnectionError, urllib.error.URLError)

_TIMEOUT_KEYWORDS = ("timed out", "timeout", "the read operation timed out")

_NETWORK_KEYWORDS = (
    "network", "connection", "unreachable",
    "unable to download webpage", "name or service not known",
    "getaddrinfo failed", "dns", "temporary failure in name resolution",
)


def classify_exception(exc: BaseException):
    """Heuristic mapping to (code, category, recoverable) -- see _KNOWN_FAILURE_PATTERNS
    and spec section 25's list of failure kinds a downloader must distinguish."""
    if isinstance(exc, DownloaderError):
        return exc.code, exc.category, exc.recoverable

    if isinstance(exc, _TIMEOUT_TYPES):
        return "E_NETWORK_TIMEOUT", "NETWORK_ERROR", True
    if isinstance(exc, _NETWORK_TYPES):
        return "E_NETWORK", "NETWORK_ERROR", True

    message = str(exc).lower()
    for substring, code, category, recoverable in _KNOWN_FAILURE_PATTERNS:
        if substring in message:
            return code, category, recoverable
    # Timeout keywords are checked before the general network ones, since "connection timed
    # out" contains both and the timeout is the more specific reading.
    if any(keyword in message for keyword in _TIMEOUT_KEYWORDS):
        return "E_NETWORK_TIMEOUT", "NETWORK_ERROR", True
    if any(keyword in message for keyword in _NETWORK_KEYWORDS):
        return "E_NETWORK", "NETWORK_ERROR", True

    return "E_DOWNLOAD_FAILED", "UNKNOWN", False


def emit_error(exc: BaseException) -> None:
    code, category, recoverable = classify_exception(exc)
    emit("error", {
        "code": code,
        "category": category,
        "message": str(exc) or exc.__class__.__name__,
        "details": traceback.format_exc(),
        "recoverable": recoverable,
    })


class _StderrLogger:
    """Routes yt-dlp's own log chatter to stderr so stdout stays pure NDJSON.

    Every method swallows its own write failure instead of letting it propagate: a video
    title can contain characters a Windows console/pipe rejects outright with a raw
    OSError (not the more common UnicodeEncodeError) rather than transliterating or
    replacing them, and a logging call failing must never take down the whole job with it
    -- see issue #56 (a second retry attempt failed with a traceback originating from this
    exact print() call).
    """

    def _safe_print(self, msg):
        try:
            print(msg, file=sys.stderr)
        except (OSError, UnicodeError):
            try:
                encoding = sys.stderr.encoding or "utf-8"
                sys.stderr.buffer.write(str(msg).encode(encoding, errors="replace") + b"\n")
                sys.stderr.buffer.flush()
            except Exception:
                pass  # best-effort diagnostic logging; never let it fail the job

    def debug(self, msg):
        self._safe_print(msg)

    def info(self, msg):
        self._safe_print(msg)

    def warning(self, msg):
        self._safe_print(msg)

    def error(self, msg):
        self._safe_print(msg)


def format_entry(f: dict) -> dict:
    vcodec = f.get("vcodec")
    acodec = f.get("acodec")
    has_video = bool(vcodec) and vcodec != "none"
    has_audio = bool(acodec) and acodec != "none"
    width = f.get("width")
    height = f.get("height")
    resolution = f"{width}x{height}" if (has_video and width and height) else None
    return {
        "formatId": f.get("format_id") or "",
        "extension": f.get("ext"),
        "resolution": resolution,
        "width": width if has_video else None,
        "height": height if has_video else None,
        "fps": f.get("fps") if has_video else None,
        "videoCodec": vcodec if has_video else None,
        "audioCodec": acodec if has_audio else None,
        "videoBitrateKbps": f.get("vbr") or (f.get("tbr") if has_video else None),
        "audioBitrateKbps": f.get("abr"),
        "filesizeBytes": f.get("filesize"),
        "approxFilesizeBytes": f.get("filesize_approx"),
        "hasVideo": has_video,
        "hasAudio": has_audio,
    }


def build_metadata_payload(info: dict, url: str) -> dict:
    # Still a hard error on THIS command: a DownloadJob downloads exactly one video, so a
    # playlist reaching it is a bug, not a feature. Playlists are supported via the separate
    # `inspectPlaylist` command, which enumerates entries the caller then submits as one
    # DownloadJob each -- see run_inspect_playlist() below. The frontend treats this specific
    # code as "ask inspectPlaylist instead", which is what makes playlist support work on any
    # site rather than only URLs whose shape we recognize.
    if info.get("_type") == "playlist":
        raise DownloaderError(
            "E_PLAYLIST_NOT_SUPPORTED", "UNSUPPORTED_FORMAT",
            "This URL is a playlist, not a single video. Use the playlist flow to download "
            "its entries.")

    formats = []
    for f in info.get("formats") or []:
        entry = format_entry(f)
        if entry["hasVideo"] or entry["hasAudio"]:
            formats.append(entry)

    return {
        "title": info.get("title") or "Untitled",
        "uploader": info.get("uploader") or info.get("channel"),
        "duration": info.get("duration"),
        "webpageUrl": info.get("webpage_url") or url,
        "thumbnailUrl": info.get("thumbnail"),
        "extractor": info.get("extractor_key") or info.get("extractor"),
        "playlistIndex": info.get("playlist_index"),
        "playlistCount": info.get("n_entries") or info.get("playlist_count"),
        "formats": formats,
    }


# A playlist becomes one queued DownloadJob per entry, so this bound is a real resource
# limit, not cosmetics: an unbounded fan-out (YouTube allows playlists in the thousands, and
# a channel "uploads" pseudo-playlist can be far larger) would flood the scheduler and the
# recovery store with jobs the user never meant to create. Entries past the cap are dropped
# and reported via `truncated`, so the UI can say so plainly instead of silently downloading
# a prefix.
_MAX_PLAYLIST_ENTRIES = 500


def build_playlist_payload(info: dict, url: str) -> dict:
    """Flattens a yt-dlp playlist extraction into the entry list the core fans out into jobs.

    Entries yt-dlp reports as unavailable (deleted/private videos surface as None, or with no
    resolvable URL) are dropped rather than passed through as jobs that would each fail
    individually. Surviving entries are renumbered contiguously from 1, so the `01 - `,
    `02 - ` prefixes on disk have no gaps where a dead entry used to be.
    """
    if info.get("_type") != "playlist":
        raise DownloaderError(
            "E_NOT_A_PLAYLIST", "UNSUPPORTED_FORMAT",
            "This URL is a single video, not a playlist.")

    raw_entries = info.get("entries") or []
    entries = []
    # Set only when a downloadable entry is actually dropped for the cap. Reporting
    # `len(entries) >= cap` instead claimed truncation for a playlist of exactly 500
    # complete videos, and the UI told the user entries had been dropped when none had.
    truncated = False
    # Counts entries dropped because yt-dlp reported them unavailable (deleted/private),
    # separately from `truncated` (which means "hit the cap"). Without this, a playlist
    # with one dead video silently reported `count: 499` out of a 500-entry list with no
    # signal anything was skipped -- indistinguishable from a bug to whoever read it.
    unavailable_count = 0
    for raw in raw_entries:
        if not isinstance(raw, dict):
            unavailable_count += 1  # unavailable entry -- yt-dlp yields None for these
            continue
        entry_url = raw.get("url") or raw.get("webpage_url")
        if not entry_url:
            unavailable_count += 1
            continue
        if len(entries) >= _MAX_PLAYLIST_ENTRIES:
            truncated = True
            break
        entries.append({
            # Assigned after the loop: index must count surviving entries, not raw position.
            "index": 0,
            "url": entry_url,
            "title": raw.get("title") or "Untitled",
            "duration": raw.get("duration"),
        })

    for position, entry in enumerate(entries, start=1):
        entry["index"] = position

    return {
        "title": info.get("title") or "Untitled playlist",
        "uploader": info.get("uploader") or info.get("channel"),
        "webpageUrl": info.get("webpage_url") or url,
        "count": len(entries),
        "truncated": truncated,
        "unavailableCount": unavailable_count,
        "entries": entries,
    }


# --- YouTube "list=" ids: which of them name a real list, and which only look like one ---
#
# A YouTube `list=` id encodes what kind of list it is, and one common kind is not a fixed
# list at all: `RD...` is an auto-generated *mix* (YouTube's own UI calls it a radio) --
# an endless stream synthesized around a seed video, which yt-dlp will happily page through
# continuation after continuation. Enumerating one does not converge on "the songs in the
# playlist"; it converges on `playlistend`. That is the whole bug: a user pastes what
# they think is their 40-song playlist and the app reports hundreds of videos, because the
# link they copied while the music was playing carries a mix id rather than the playlist's.
#
# Measured against yt-dlp 2026.8.19 with this script's own probe options:
#   list=PLbpi6...      real playlist         ->  19 entries, enumeration terminates
#   list=RDCLAK5uy_...  curated (see below)   ->  51 entries, enumeration terminates
#   list=RDdQw4w9WgXcQ  mix seeded by a video -> 349-500 entries, capped, entries repeat
#   list=RDAMVMgJYj...  YouTube Music radio   -> 500 entries (379 unique), capped
_YOUTUBE_HOSTS = frozenset({
    "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
    "youtu.be", "www.youtu.be", "youtube-nocookie.com", "www.youtube-nocookie.com",
})

# The exception to "RD means mix": YouTube Music's *curated* playlists carry RD-prefixed ids
# and are ordinary finite lists. Rejecting those would break a legitimate case, so the check
# below is a prefix test with this hole in it rather than a bare `startswith("RD")`.
_CURATED_LIST_ID_PREFIXES = ("RDCLAK",)

# The other exception, and a more useful one: `RDAMPL<playlist id>` is the radio seeded from
# a real playlist -- pressing play on your own playlist in YouTube Music produces exactly
# this. The id of the list the user actually meant is embedded in the mix id, so it is
# recovered rather than rejected. (Left alone, yt-dlp resolves such a URL to the single
# seed video and `inspectPlaylist` fails with the flatly wrong "this is a single video".)
_PLAYLIST_SEEDED_MIX_PREFIX = "RDAMPL"


def _youtube_list_id(url: str):
    """The `list=` id of a YouTube URL, or None if the URL is not YouTube or carries none.

    Scoped to YouTube hosts on purpose: "an id starting with RD" is a fact about YouTube's
    URL scheme, not about playlist ids in general, and must not be applied to the other
    sites yt-dlp supports.
    """
    try:
        parsed = urllib.parse.urlsplit(url.strip())
    except ValueError:
        return None
    if (parsed.hostname or "").lower() not in _YOUTUBE_HOSTS:
        return None
    for key, value in urllib.parse.parse_qsl(parsed.query):
        if key == "list" and value:
            return value
    return None


def normalize_playlist_url(url: str) -> str:
    """Rewrites a playlist-seeded mix URL to the playlist it was seeded from.

    Everything else is returned unchanged -- this resolves one specific, very common
    mis-copied link shape, it is not a general URL cleaner.
    """
    list_id = _youtube_list_id(url)
    if not list_id or not list_id.startswith(_PLAYLIST_SEEDED_MIX_PREFIX):
        return url
    seed = list_id[len(_PLAYLIST_SEEDED_MIX_PREFIX):]
    if not seed:
        return url
    parsed = urllib.parse.urlsplit(url.strip())
    # Rebuilt as a bare playlist URL rather than patched in place: the original also carries
    # `v=`, `index=` and `start_radio=`, and every one of them pulls the extraction back
    # towards the single seed video.
    return urllib.parse.urlunsplit((
        parsed.scheme or "https", parsed.netloc, "/playlist",
        urllib.parse.urlencode([("list", seed)]), ""))


def auto_generated_mix_id(url: str):
    """The mix/radio id when `url` names one of YouTube's endless auto-generated streams.

    Returns None for real playlists (including the curated RD-prefixed ones) and for every
    non-YouTube URL. Call `normalize_playlist_url` first: a playlist-seeded mix is a
    recoverable playlist, not a mix, and normalizing turns it into the former.
    """
    list_id = _youtube_list_id(url)
    if not list_id or not list_id.startswith("RD"):
        return None
    if list_id.startswith(_CURATED_LIST_ID_PREFIXES):
        return None
    return list_id


# yt-dlp releases are dated (e.g. "2026.8.19"), so its own version string says how stale
# the extractors are. Two years is generous: extractors for the big sites break on a scale
# of weeks, so a build this old is almost certainly failing on real URLs and the user should
# be told that rather than left reading "video unavailable" for every link.
_YT_DLP_STALE_AFTER_DAYS = 730


def _yt_dlp_release_date(version: str):
    """Parses yt-dlp's dated version string into a date, or None if it isn't one.

    Not every build carries a dated version -- a source checkout can report something like
    "2026.08.19.123456" or a git description -- so this takes the leading YYYY.MM.DD and
    ignores whatever follows, and returns None rather than guessing when it can't.
    """
    import datetime

    parts = version.split(".")
    if len(parts) < 3:
        return None
    try:
        return datetime.date(int(parts[0]), int(parts[1]), int(parts[2][:2]))
    except (TypeError, ValueError):
        return None


def _yt_dlp_version_string() -> str:
    """yt-dlp's version, or "" if this build does not report one.

    `yt_dlp.__version__` is NOT it. Current releases (verified against 2026.08.19) expose
    the string only as `yt_dlp.version.__version__`; there is no `__version__` on the
    package itself. Reading the wrong attribute silently produced "" for every install,
    which made `ytDlpVersion` and `ageDays` always null and `stale` always false -- so the
    "your extractors are two years old, that is why every link fails" warning this whole
    function exists to raise could never fire.

    Both spellings are tried, newest-first, so an older build that did carry the package
    attribute still reports correctly.
    """
    try:
        from yt_dlp.version import __version__ as version  # type: ignore[import-not-found]

        if version:
            return str(version)
    except Exception:
        pass
    return str(getattr(yt_dlp, "__version__", "") or "")


def run_version() -> int:
    """Reports what the downloader stack actually is, so the app can say so before a
    download fails for a reason the user cannot see.

    Never fails: a missing yt_dlp is reported as `available: false`, not as an error, because
    "is the downloader usable" is exactly the question being asked.
    """
    import datetime

    data = {
        "available": yt_dlp is not None,
        "ytDlpVersion": None,
        "pythonVersion": ".".join(str(part) for part in sys.version_info[:3]),
        "ageDays": None,
        "stale": False,
    }

    if yt_dlp is not None:
        version = _yt_dlp_version_string()
        data["ytDlpVersion"] = version or None
        released = _yt_dlp_release_date(version)
        if released is not None:
            age = (datetime.date.today() - released).days
            data["ageDays"] = age
            data["stale"] = age > _YT_DLP_STALE_AFTER_DAYS

    emit("version", data)
    emit("completed", {})
    return 0


def run_selftest() -> int:
    emit("metadata", {
        "title": "MediaTool Self-Test Video",
        "duration": 12,
        "playlistIndex": None,
        "playlistCount": None,
    })

    total_bytes = 4194304
    # Increasing downloadedBytes, decreasing etaSeconds -- what the protocol test asserts.
    steps = [
        (1048576, 9),
        (2097152, 6),
        (3145728, 3),
        (4194304, 0),
    ]
    for downloaded, eta in steps:
        emit("progress", {
            "downloadedBytes": downloaded,
            "totalBytes": total_bytes,
            "speedBytesPerSecond": 1048576,
            "etaSeconds": eta,
            "statusMessage": "Downloading",
        })

    emit("completed", {"outputPath": os.path.join(os.getcwd(), "selftest-output.mp4")})
    return 0


# Phase 2 supports single videos only (spec section 30) -- these two options make that
# fast rather than merely correct-eventually:
#   noplaylist    -- when a URL points to a video that's ALSO part of a playlist (e.g.
#                     "watch?v=X&list=Y", a very common way people share/copy links),
#                     process just that one video instead of the whole playlist.
#   extract_flat  -- when a URL points to nothing BUT a playlist, don't walk every entry
#                     resolving its full metadata (which can mean dozens of extra network
#                     requests) just to discover it's a playlist -- "in_playlist" keeps
#                     entries shallow while leaving a direct single-video URL unaffected,
#                     so build_metadata_payload()'s `_type == "playlist"` check still
#                     fires, just without the network cost of a full walk first.
_SINGLE_VIDEO_PROBE_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "noprogress": True,
    "logger": _StderrLogger(),
    "skip_download": True,
    "noplaylist": True,
    "extract_flat": "in_playlist",
    "socket_timeout": _SOCKET_TIMEOUT_SECONDS,
}

# The playlist counterpart of the options above, and the one intended difference is
# `noplaylist: False` -- a "watch?v=X&list=Y" URL must resolve to the LIST here, which is
# exactly what the single-video probe suppresses. The frontend only sends such a URL to this
# command after the user explicitly chose "the whole playlist" over "just this video".
#
# `extract_flat` stays "in_playlist" (same as the single-video probe), and the distinction
# matters more than it reads. Both values keep entries shallow -- one request for the
# playlist page rather than one per video, which is what makes enumerating a 200-video
# playlist a sub-second operation. But `extract_flat: True` also flattens the *root*: for a
# combo URL, YouTube's extractor hands back an unresolved `{"_type": "url"}` pointing at the
# playlist, and True returns that as-is instead of following it. The result had no entries
# and no "playlist" _type, so build_playlist_payload() rejected the user's own playlist link
# with "This URL is a single video, not a playlist." Verified against yt-dlp 2026.8.19:
# watch?v=X&list=PL... yields 0 entries with True and all 19 with "in_playlist"; a bare
# playlist URL yields the same 19 either way.
_PLAYLIST_PROBE_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "noprogress": True,
    "logger": _StderrLogger(),
    "skip_download": True,
    "noplaylist": False,
    "extract_flat": "in_playlist",
    "playlistend": _MAX_PLAYLIST_ENTRIES,
    "socket_timeout": _SOCKET_TIMEOUT_SECONDS,
}


def run_inspect(url: str) -> int:
    if yt_dlp is None:
        emit_error(RuntimeError("yt_dlp is not installed in this environment"))
        return 1
    try:
        with yt_dlp.YoutubeDL(dict(_SINGLE_VIDEO_PROBE_OPTS)) as probe:
            info = probe.extract_info(url, download=False)
        emit("metadata", build_metadata_payload(info, url))
        emit("completed", {})
        return 0
    except Exception as exc:
        emit_error(exc)
        return 1


def run_inspect_playlist(url: str) -> int:
    try:
        # Both checks are pure URL inspection, so they run before the probe: enumerating a
        # mix is the slowest thing this command can do and the most useless.
        url = normalize_playlist_url(url)
        mix_id = auto_generated_mix_id(url)
        if mix_id is not None:
            raise DownloaderError(
                "E_PLAYLIST_IS_MIX", "UNSUPPORTED_FORMAT",
                "This link is a YouTube Mix -- an endless auto-generated radio, not a "
                "fixed playlist, so there is no \"all of it\" to download. Open the "
                "playlist itself on YouTube and paste that link, or download just this "
                "video.")
        if yt_dlp is None:
            raise RuntimeError("yt_dlp is not installed in this environment")
        with yt_dlp.YoutubeDL(dict(_PLAYLIST_PROBE_OPTS)) as probe:
            info = probe.extract_info(url, download=False)
        emit("playlist", build_playlist_payload(info, url))
        emit("completed", {})
        return 0
    except Exception as exc:
        emit_error(exc)
        return 1


def escape_outtmpl_literal(text: str) -> str:
    """Escapes text that must appear VERBATIM in a yt-dlp output template.

    `outtmpl` is a template, not a path: yt-dlp expands `%(field)s` in it. Both halves of
    the template built below are attacker-influenced -- the filename base is derived from
    the video's title, which comes from whatever remote site is being downloaded from --
    so splicing them in raw let a title inject template fields:

        title "100%(ext)s weird"  -> file named "100mp4 weird.mp4"
        title "%(title)200000s"   -> a 200,000-character filename

    `%%` is the template's own escape for a literal percent, so doubling every `%` makes
    the text mean itself. A name with no `%` in it is unchanged.
    """
    return text.replace("%", "%%")


def _resolve_output_path(downloader: "yt_dlp.YoutubeDL", result_info: dict) -> str:
    """`prepare_filename()` is a template renderer, not a report of what yt-dlp actually
    wrote to disk -- it doesn't reflect post-processing (e.g. yt-dlp forcing a different
    container on an incompatible video+audio merge, the common case for anything but the
    lowest-quality formats). The real path is on `requested_downloads[0]['filepath']` once
    download/merge has actually happened; fall back to `prepare_filename()` only if that's
    somehow absent. See issue #25 -- a wrong path here used to trigger the destructive
    cleanup issue #3 fixed, and independently explains issue #56's "completed job reports
    the wrong/intermediate file" symptom.
    """
    requested = result_info.get("requested_downloads") or []
    if requested and requested[0].get("filepath"):
        return requested[0]["filepath"]
    return downloader.prepare_filename(result_info)


def run_download(params: dict) -> int:
    url = params["url"]
    output_dir = params["outputDir"]
    format_selector = params.get("formatSelector") or "bestvideo*+bestaudio/best"
    filename_base = sanitize_filename(params.get("filenameBase") or "download")
    ffmpeg_location = params.get("ffmpegLocation")

    if yt_dlp is None:
        emit_error(RuntimeError("yt_dlp is not installed in this environment"))
        return 1

    try:
        # No separate metadata probe here (issue #35): this used to run its own
        # extract_info(download=False) -- a full extra network round-trip to yt-dlp's
        # extractor on every single download -- purely to emit a "metadata" event that
        # DownloadJob::Execute() (the only real caller of this command) always discards
        # with a no-op onMetadata callback, since it already fetched metadata itself via
        # its own mandatory Inspect() call moments earlier (that's also what rejects a
        # playlist URL before a "download" command is ever sent -- see
        # DownloadJob.PlaylistUrlSurfacesAsUnsupportedFormat -- so `noplaylist` below on
        # the real download is enough; there is no path that reaches this function without
        # already having passed that gate).
        os.makedirs(output_dir, exist_ok=True)
        # Only the trailing ".%(ext)s" is meant as a template field; everything the caller
        # supplied is literal text and is escaped as such. See escape_outtmpl_literal.
        outtmpl = os.path.join(
            escape_outtmpl_literal(output_dir), escape_outtmpl_literal(filename_base) + ".%(ext)s"
        )

        def progress_hook(status: dict) -> None:
            state = status.get("status")
            if state == "downloading":
                emit("progress", {
                    "downloadedBytes": status.get("downloaded_bytes"),
                    "totalBytes": status.get("total_bytes") or status.get("total_bytes_estimate"),
                    "speedBytesPerSecond": status.get("speed"),
                    "etaSeconds": status.get("eta"),
                    "statusMessage": "Downloading",
                })
            elif state == "finished":
                emit("progress", {
                    "downloadedBytes": status.get("downloaded_bytes") or status.get("total_bytes"),
                    "totalBytes": status.get("total_bytes"),
                    "speedBytesPerSecond": None,
                    "etaSeconds": 0,
                    "statusMessage": "Finalizing",
                })

        download_opts = {
            "outtmpl": outtmpl,
            "format": format_selector,
            "progress_hooks": [progress_hook],
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "logger": _StderrLogger(),
            # Defense in depth: DownloadJob always rejects a playlist URL via an earlier
            # Inspect() call before ever reaching here, but a video+playlist combo URL
            # should still resolve to just the one video if this is ever invoked directly.
            "noplaylist": True,
            # Per-socket-operation bound, not a total-download deadline -- only fires if a
            # single connect/read stalls, so it doesn't cut off a legitimately slow but
            # progressing large download.
            "socket_timeout": _SOCKET_TIMEOUT_SECONDS,
        }
        # Points yt-dlp's own internal merge step (when the selector picks separate
        # video+audio streams) at the SAME ffmpeg the C++ core already resolved, instead
        # of letting yt-dlp run a second, independent PATH search -- see
        # docs/decisions.md "Video/audio merge strategy".
        if ffmpeg_location:
            download_opts["ffmpeg_location"] = ffmpeg_location

        with yt_dlp.YoutubeDL(download_opts) as downloader:
            result_info = downloader.extract_info(url, download=True)
            output_path = _resolve_output_path(downloader, result_info)

        emit("completed", {"outputPath": output_path})
        return 0
    except Exception as exc:
        emit_error(exc)
        return 1


def run_command_stdin() -> int:
    raw_line = sys.stdin.readline()
    try:
        request = json.loads(raw_line)
        command = request.get("command")
        params = request.get("params", {})
    except Exception as exc:  # malformed request from the C++ core, not a download failure
        emit_error(exc)
        return 1

    if command == "inspect":
        try:
            url = params["url"]
        except Exception as exc:
            emit_error(exc)
            return 1
        return run_inspect(url)

    if command == "inspectPlaylist":
        try:
            url = params["url"]
        except Exception as exc:
            emit_error(exc)
            return 1
        return run_inspect_playlist(url)

    if command == "version":
        return run_version()

    if command == "download":
        try:
            _ = params["url"]
            _ = params["outputDir"]
        except Exception as exc:
            emit_error(exc)
            return 1
        return run_download(params)

    emit_error(ValueError(f"unsupported command: {command!r}"))
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="MediaTool yt-dlp downloader bridge")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--selftest", action="store_true",
                       help="Emit a canned NDJSON sequence, no network access")
    mode.add_argument("--command-stdin", action="store_true",
                       help="Read one JSON command from stdin (inspect, inspectPlaylist, "
                            "download or version)")
    args = parser.parse_args()

    if args.selftest:
        return run_selftest()
    return run_command_stdin()


if __name__ == "__main__":
    sys.exit(main())
