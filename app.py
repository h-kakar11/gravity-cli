"""gravity-cli: download YouTube videos, playlists and mixes from the terminal.

Run with:  py app.py
"""

import os
import sys

from gravity_cli import config, ffmpeg_check, history, mixes, playlist, ui
from gravity_cli.bridge import DownloadError, downloader_info, inspect, inspect_playlist

JUST_THIS_VIDEO = "Just this video"
THE_WHOLE_PLAYLIST = "The whole playlist"
THE_MIX = "The mix (auto-generated)"


def check_environment() -> bool:
    info = downloader_info()
    if not info.get("available"):
        ui.error("yt-dlp is not installed in this environment. "
                 "Run: pip install -r requirements.txt")
        return False
    if info.get("stale"):
        ui.warn(f"yt-dlp is {info.get('ageDays')} days old; YouTube extractors break "
                "quickly. Run: pip install -U yt-dlp")
    if not ffmpeg_check.find_ffmpeg():
        ui.warn(ffmpeg_check.MISSING_MESSAGE)
    return True


def normalize_dir(path: str) -> str:
    """Absolute, so a relative answer here is not ambiguous later in the history file."""
    return os.path.abspath(os.path.expanduser(path.strip()))


def ask_destination(settings: dict, subfolder: str = "") -> str:
    chosen = ui.choose_output_dir(settings["outputDirectory"])
    if not chosen:
        return ""

    chosen = normalize_dir(chosen)
    if chosen != settings["outputDirectory"]:
        settings["outputDirectory"] = chosen
        config.save(settings)

    if subfolder:
        folder = ui.prompt_text("Folder name for this list:", default=subfolder)
        if folder is None:
            return ""
        chosen = os.path.join(chosen, playlist.suggest_folder_name(folder))
    return chosen


def download_single(url: str, settings: dict) -> None:
    ui.info("Fetching video details...")
    metadata = inspect(url)
    ui.show_metadata(metadata)

    quality = ui.choose_quality(settings["quality"])
    if quality is None:
        return
    settings["quality"] = quality
    config.save(settings)

    destination = ask_destination(settings)
    if not destination:
        return

    try:
        with ui.single_download_progress(metadata.title) as on_progress:
            output_path = playlist.download_item(
                url=url,
                output_dir=destination,
                quality=quality,
                title=metadata.title,
                ffmpeg_location=ffmpeg_check.find_ffmpeg(),
                on_progress=on_progress,
            )
    except DownloadError as exc:
        history.record(url, metadata.title, "video", quality, history.FAILED,
                       error=f"{exc.code}: {exc.message}")
        raise

    history.record(url, metadata.title, "video", quality, history.COMPLETED,
                   output_path=output_path)
    ui.success(f"Saved to {output_path}")


def download_list(listing, kind: str, settings: dict) -> None:
    quality = ui.choose_quality(settings["quality"])
    if quality is None:
        return
    settings["quality"] = quality
    config.save(settings)

    destination = ask_destination(settings, subfolder=playlist.suggest_folder_name(listing.title))
    if not destination:
        return

    with ui.playlist_download_progress(len(listing.entries)) as view:
        results = playlist.download_playlist(
            playlist=listing,
            output_dir=destination,
            quality=quality,
            kind=kind,
            ffmpeg_location=ffmpeg_check.find_ffmpeg(),
            on_item_start=view.start_item,
            on_progress=view.on_progress,
            on_item_finish=view.finish_item,
        )

    completed = sum(1 for r in results if r.status == history.COMPLETED)
    failed = len(results) - completed
    ui.success(f"{completed} of {len(results)} downloaded to {destination}")
    if failed:
        ui.warn(f"{failed} entries failed; they are listed in your download history.")


def handle_mix(kind, settings: dict) -> None:
    if kind.video_url:
        choice = ui.choose("This link is a YouTube Mix. What do you want?",
                           [JUST_THIS_VIDEO, THE_MIX])
        if choice is None:
            return
        if choice == JUST_THIS_VIDEO:
            download_single(kind.video_url, settings)
            return

    limit_text = ui.prompt_text("How many entries from the mix?",
                                default=str(settings["mixLimit"]))
    if limit_text is None:
        return
    try:
        limit = max(1, min(config.MAX_MIX_LIMIT, int(limit_text.strip())))
    except ValueError:
        ui.error("That is not a number.")
        return
    settings["mixLimit"] = limit
    config.save(settings)

    ui.warn(mixes.MIX_DISCLAIMER.format(limit=limit))
    ui.info("Reading the mix...")
    listing = mixes.probe_mix(kind.url, limit)

    if not listing.entries:
        ui.error("YouTube returned no entries for this mix.")
        return

    ui.show_playlist(listing, f"{listing.title} - {listing.count} entries")
    if not ui.confirm(f"Download these {listing.count} entries?"):
        return

    download_list(listing, mixes.MIX, settings)


def handle_playlist(kind, settings: dict) -> None:
    if kind.video_url:
        choice = ui.choose("This link has a playlist attached. What do you want?",
                           [JUST_THIS_VIDEO, THE_WHOLE_PLAYLIST])
        if choice is None:
            return
        if choice == JUST_THIS_VIDEO:
            download_single(kind.video_url, settings)
            return

    ui.info("Reading the playlist...")
    listing = inspect_playlist(kind.url)

    if not listing.entries:
        ui.error("That playlist has no downloadable entries.")
        return

    ui.show_playlist(listing, f"{listing.title} - {listing.count} entries")
    if not ui.confirm(f"Download these {listing.count} entries?"):
        return

    download_list(listing, mixes.PLAYLIST, settings)


def handle_link(url: str, settings: dict) -> None:
    kind = mixes.classify(url)
    if kind.kind == mixes.MIX:
        handle_mix(kind, settings)
    elif kind.kind == mixes.PLAYLIST:
        handle_playlist(kind, settings)
    else:
        download_single(kind.url, settings)


def edit_settings(settings: dict) -> None:
    quality = ui.choose_quality(settings["quality"])
    if quality is None:
        return
    destination = ui.choose_output_dir(settings["outputDirectory"])
    if not destination:
        return
    limit_text = ui.prompt_text("Default number of mix entries:",
                                default=str(settings["mixLimit"]))
    if limit_text is None:
        return

    settings["quality"] = quality
    settings["outputDirectory"] = normalize_dir(destination)
    try:
        settings["mixLimit"] = max(1, min(config.MAX_MIX_LIMIT, int(limit_text.strip())))
    except ValueError:
        ui.warn("Mix entry count unchanged: that is not a number.")

    config.save(settings)
    ui.success(f"Saved to {config.config_path()}")


def main() -> int:
    ui.banner()
    if not check_environment():
        return 1

    settings = config.load()

    while True:
        try:
            choice = ui.main_menu()
            if choice is None or choice == ui.QUIT:
                return 0
            if choice == ui.VIEW_HISTORY:
                ui.show_history(history.load())
                continue
            if choice == ui.SETTINGS:
                edit_settings(settings)
                continue

            url = ui.prompt_url()
            if url and url.strip():
                handle_link(url.strip(), settings)
        except DownloadError as exc:
            ui.error(f"{exc.message} [{exc.code}]")
        except KeyboardInterrupt:
            ui.warn("Cancelled.")


if __name__ == "__main__":
    sys.exit(main())
