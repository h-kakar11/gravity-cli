"""Drives the vendored downloader.py over its NDJSON-on-stdio protocol.

This is the Python counterpart of Gravity's engines/downloader/YtDlpProvider.cpp: spawn
the script with --command-stdin, write one JSON command line, read event lines off stdout
until it exits. Keeping that boundary means the vendored script stays byte-identical to
upstream and all of its yt-dlp handling -- probe options, error classification, progress
hooks, output-template escaping -- is reused exactly as written.
"""

import collections
import json
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from typing import Callable, Optional

from .vendored import DOWNLOADER_SCRIPT

# A metadata fetch gets a wall-clock deadline because yt-dlp's own socket_timeout bounds
# one connect/read, not a whole multi-request extraction. A download deliberately gets
# none: a slow but progressing transfer is not a hang.
INSPECT_TIMEOUT_SECONDS = 60

_STDERR_TAIL_LINES = 40


class DownloadError(Exception):
    def __init__(self, code: str, category: str, message: str,
                 recoverable: bool = False, details: str = ""):
        super().__init__(message)
        self.code = code
        self.category = category
        self.message = message
        self.recoverable = recoverable
        self.details = details


@dataclass
class Progress:
    downloaded_bytes: Optional[int] = None
    total_bytes: Optional[int] = None
    speed_bytes_per_second: Optional[float] = None
    eta_seconds: Optional[float] = None
    status_message: str = ""

    @property
    def percentage(self) -> Optional[float]:
        if not self.total_bytes or self.downloaded_bytes is None:
            return None
        return min(100.0, self.downloaded_bytes / self.total_bytes * 100.0)


@dataclass
class Metadata:
    title: str = "Untitled"
    uploader: Optional[str] = None
    duration: Optional[float] = None
    webpage_url: Optional[str] = None
    thumbnail_url: Optional[str] = None
    extractor: Optional[str] = None
    formats: list = field(default_factory=list)


@dataclass
class PlaylistEntry:
    index: int
    url: str
    title: str
    duration: Optional[float] = None


@dataclass
class PlaylistInfo:
    title: str = "Untitled playlist"
    uploader: Optional[str] = None
    webpage_url: Optional[str] = None
    count: int = 0
    truncated: bool = False
    unavailable_count: int = 0
    entries: list = field(default_factory=list)


def metadata_from_payload(data: dict) -> Metadata:
    return Metadata(
        title=data.get("title") or "Untitled",
        uploader=data.get("uploader"),
        duration=data.get("duration"),
        webpage_url=data.get("webpageUrl"),
        thumbnail_url=data.get("thumbnailUrl"),
        extractor=data.get("extractor"),
        formats=data.get("formats") or [],
    )


def playlist_from_payload(data: dict) -> PlaylistInfo:
    return PlaylistInfo(
        title=data.get("title") or "Untitled playlist",
        uploader=data.get("uploader"),
        webpage_url=data.get("webpageUrl"),
        count=data.get("count") or 0,
        truncated=bool(data.get("truncated")),
        unavailable_count=data.get("unavailableCount") or 0,
        entries=[
            PlaylistEntry(
                index=e.get("index", 0),
                url=e.get("url", ""),
                title=e.get("title") or "Untitled",
                duration=e.get("duration"),
            )
            for e in (data.get("entries") or [])
        ],
    )


def _drain(stream, sink) -> None:
    for line in stream:
        sink.append(line.rstrip("\n"))
    stream.close()


def run_command(command: str, params: dict,
                on_event: Optional[Callable[[str, dict], None]] = None,
                timeout: Optional[float] = None) -> None:
    """Runs one downloader command, dispatching each NDJSON event to `on_event`.

    Raises DownloadError if the script reports an error event or dies without completing.
    """
    proc = subprocess.Popen(
        [sys.executable, str(DOWNLOADER_SCRIPT), "--command-stdin"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )

    # yt-dlp's own chatter goes to stderr and is drained continuously: left unread, a full
    # pipe buffer would deadlock the child mid-download.
    stderr_tail = collections.deque(maxlen=_STDERR_TAIL_LINES)
    stderr_thread = threading.Thread(target=_drain, args=(proc.stderr, stderr_tail),
                                      daemon=True)
    stderr_thread.start()

    timed_out = threading.Event()

    def on_deadline():
        timed_out.set()
        proc.kill()

    watchdog = threading.Timer(timeout, on_deadline) if timeout else None
    if watchdog:
        watchdog.start()

    failure = None
    completed = False
    try:
        proc.stdin.write(json.dumps({"command": command, "params": params}) + "\n")
        proc.stdin.flush()
        proc.stdin.close()

        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue  # stdout is protocol-only by contract; ignore anything that isn't

            event = message.get("event")
            data = message.get("data") or {}
            if event == "error":
                failure = DownloadError(
                    data.get("code") or "E_DOWNLOAD_FAILED",
                    data.get("category") or "UNKNOWN",
                    data.get("message") or "Download failed",
                    bool(data.get("recoverable")),
                    data.get("details") or "",
                )
            elif event == "completed":
                completed = True
            if on_event:
                on_event(event, data)
    finally:
        if watchdog:
            watchdog.cancel()
        proc.wait()
        stderr_thread.join(timeout=5)

    if timed_out.is_set():
        raise DownloadError("E_INSPECT_TIMEOUT", "NETWORK_ERROR",
                            f"Timed out after {timeout:.0f}s fetching from the network.",
                            recoverable=True)
    if failure:
        raise failure
    if not completed:
        raise DownloadError(
            "E_DOWNLOAD_FAILED", "UNKNOWN",
            "The downloader exited without completing.",
            details="\n".join(stderr_tail))


def downloader_info() -> dict:
    """Reports yt-dlp availability/version. Never raises: absence is itself the answer."""
    info = {}

    def capture(event, data):
        if event == "version":
            info.update(data)

    try:
        run_command("version", {}, capture, timeout=30)
    except DownloadError:
        pass
    return info


def inspect(url: str, timeout: float = INSPECT_TIMEOUT_SECONDS) -> Metadata:
    result = {}

    def capture(event, data):
        if event == "metadata":
            result.update(data)

    run_command("inspect", {"url": url}, capture, timeout=timeout)
    return metadata_from_payload(result)


def inspect_playlist(url: str, timeout: float = INSPECT_TIMEOUT_SECONDS) -> PlaylistInfo:
    result = {}

    def capture(event, data):
        if event == "playlist":
            result.update(data)

    run_command("inspectPlaylist", {"url": url}, capture, timeout=timeout)
    return playlist_from_payload(result)


def download(url: str, output_dir: str, format_selector: str, filename_base: str,
             on_progress: Optional[Callable[[Progress], None]] = None,
             ffmpeg_location: Optional[str] = None) -> str:
    params = {
        "url": url,
        "outputDir": output_dir,
        "formatSelector": format_selector,
        "filenameBase": filename_base,
    }
    if ffmpeg_location:
        params["ffmpegLocation"] = ffmpeg_location

    output_path = {}

    def capture(event, data):
        if event == "progress" and on_progress:
            on_progress(Progress(
                downloaded_bytes=data.get("downloadedBytes"),
                total_bytes=data.get("totalBytes"),
                speed_bytes_per_second=data.get("speedBytesPerSecond"),
                eta_seconds=data.get("etaSeconds"),
                status_message=data.get("statusMessage") or "",
            ))
        elif event == "completed":
            output_path["path"] = data.get("outputPath")

    run_command("download", params, capture)
    return output_path.get("path") or ""
