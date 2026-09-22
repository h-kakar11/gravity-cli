"""Terminal presentation: menus, tables, prompts and progress bars.

Deliberately thin -- every function here either asks the user something or draws
something. All download logic lives in bridge/playlist/mixes.
"""

from contextlib import contextmanager
from typing import Optional

import questionary
from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn
from rich.table import Table

from . import format_selector

console = Console()

_STYLE = questionary.Style([
    ("qmark", "fg:#8b5cf6 bold"),
    ("question", "bold"),
    ("pointer", "fg:#8b5cf6 bold"),
    ("highlighted", "fg:#8b5cf6 bold"),
    ("selected", "fg:#22c55e"),
])

PASTE_A_LINK = "Paste a link"
VIEW_HISTORY = "View download history"
SETTINGS = "Settings"
QUIT = "Quit"


def format_bytes(value: Optional[float]) -> str:
    if not value:
        return "--"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TB"


def format_speed(value: Optional[float]) -> str:
    return "--" if not value else f"{format_bytes(value)}/s"


def format_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "--"
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def banner() -> None:
    console.print(Panel.fit(
        "[bold]gravity cli[/bold]\n[dim]YouTube videos, playlists and mixes[/dim]",
        border_style="magenta"))


def info(message: str) -> None:
    console.print(f"[cyan]{message}[/cyan]")


def warn(message: str) -> None:
    console.print(f"[yellow]{message}[/yellow]")


def error(message: str) -> None:
    console.print(f"[red]{message}[/red]")


def success(message: str) -> None:
    console.print(f"[green]{message}[/green]")


def main_menu() -> Optional[str]:
    return questionary.select(
        "What would you like to do?",
        choices=[PASTE_A_LINK, VIEW_HISTORY, SETTINGS, QUIT],
        style=_STYLE,
    ).ask()


def prompt_url() -> Optional[str]:
    return questionary.text("Paste a YouTube link:", style=_STYLE).ask()


def choose(message: str, choices: list, default=None) -> Optional[str]:
    return questionary.select(message, choices=choices, default=default,
                              style=_STYLE).ask()


def choose_quality(default: str) -> Optional[str]:
    labels = [format_selector.PRESET_LABELS[p] for p in format_selector.PRESETS]
    picked = questionary.select(
        "Quality:",
        choices=labels,
        default=format_selector.PRESET_LABELS.get(default),
        style=_STYLE,
    ).ask()
    if picked is None:
        return None
    for preset, label in format_selector.PRESET_LABELS.items():
        if label == picked:
            return preset
    return format_selector.BEST


def choose_output_dir(default: str) -> Optional[str]:
    return questionary.path("Download to:", default=default, only_directories=True,
                            style=_STYLE).ask()


def prompt_text(message: str, default: str = "") -> Optional[str]:
    return questionary.text(message, default=default, style=_STYLE).ask()


def confirm(message: str, default: bool = True) -> Optional[bool]:
    return questionary.confirm(message, default=default, style=_STYLE).ask()


def show_metadata(metadata) -> None:
    lines = [f"[bold]{metadata.title}[/bold]"]
    if metadata.uploader:
        lines.append(f"[dim]{metadata.uploader}[/dim]")
    if metadata.duration:
        lines.append(f"[dim]{format_duration(metadata.duration)}[/dim]")
    console.print(Panel("\n".join(lines), border_style="cyan", expand=False))


def show_playlist(playlist, heading: str) -> None:
    table = Table(title=heading, title_justify="left", header_style="bold magenta")
    table.add_column("#", justify="right", style="dim", width=4)
    table.add_column("Title")
    table.add_column("Length", justify="right", style="dim", width=8)

    for entry in playlist.entries:
        table.add_row(str(entry.index), entry.title, format_duration(entry.duration))

    console.print(table)
    if playlist.unavailable_count:
        warn(f"{playlist.unavailable_count} unavailable entries were skipped "
             "(deleted or private).")
    if playlist.truncated:
        warn(f"Only the first {playlist.count} entries are listed; the playlist is longer.")


def show_history(entries: list, limit: int = 20) -> None:
    if not entries:
        info("No downloads recorded yet.")
        return

    table = Table(title="Recent downloads", title_justify="left",
                  header_style="bold magenta")
    table.add_column("When", style="dim", width=17)
    table.add_column("Title")
    table.add_column("Quality", style="dim", width=10)
    table.add_column("Status", width=10)

    for entry in entries[:limit]:
        status = entry.get("status", "")
        colour = "green" if status == "COMPLETED" else "red"
        table.add_row(
            entry.get("timestamp", "")[:16].replace("T", " "),
            entry.get("title", ""),
            entry.get("quality", ""),
            f"[{colour}]{status}[/{colour}]",
        )

    console.print(table)


def _progress_widget() -> Progress:
    return Progress(
        SpinnerColumn(style="magenta"),
        TextColumn("[bold]{task.description}"),
        BarColumn(complete_style="magenta", finished_style="green"),
        TextColumn("{task.fields[percent]:>6}"),
        TextColumn("{task.fields[size]:>18}"),
        TextColumn("{task.fields[speed]:>12}"),
        TextColumn("{task.fields[eta]:>11}"),
        console=console,
    )


_EMPTY_FIELDS = {"percent": "--", "size": "--", "speed": "--", "eta": "ETA --"}
# The overall playlist counter has no bytes of its own, so its columns stay blank rather
# than showing placeholders for numbers that will never arrive.
_BLANK_FIELDS = {"percent": "", "size": "", "speed": "", "eta": ""}


def _fields_for(progress_event) -> dict:
    percentage = progress_event.percentage
    return {
        "percent": f"{percentage:.1f}%" if percentage is not None else "--",
        "size": (f"{format_bytes(progress_event.downloaded_bytes)} / "
                  f"{format_bytes(progress_event.total_bytes)}"),
        "speed": format_speed(progress_event.speed_bytes_per_second),
        "eta": f"ETA {format_duration(progress_event.eta_seconds)}",
    }


def _truncate(text: str, width: int = 38) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


@contextmanager
def single_download_progress(title: str):
    """Yields an on_progress callback rendering one video's transfer."""
    with _progress_widget() as widget:
        task = widget.add_task(_truncate(title), total=None, **_EMPTY_FIELDS)

        def on_progress(event):
            widget.update(
                task,
                total=event.total_bytes,
                completed=event.downloaded_bytes or 0,
                description=_truncate(event.status_message or title),
                **_fields_for(event),
            )

        yield on_progress


class PlaylistProgressView:
    """An overall item counter plus a live bar for the entry being downloaded."""

    def __init__(self, widget, total_items: int):
        self._widget = widget
        self._overall = widget.add_task(f"0/{total_items} entries", total=total_items,
                                         **_BLANK_FIELDS)
        self._current = widget.add_task("waiting", total=None, **_EMPTY_FIELDS)
        self._total_items = total_items
        self._done = 0

    def start_item(self, position: int, total: int, title: str) -> None:
        self._widget.reset(self._current, total=None, completed=0,
                           description=_truncate(f"{position}. {title}"),
                           **_EMPTY_FIELDS)

    def on_progress(self, event) -> None:
        self._widget.update(self._current, total=event.total_bytes,
                            completed=event.downloaded_bytes or 0, **_fields_for(event))

    def finish_item(self, result) -> None:
        self._done += 1
        self._widget.update(self._overall, completed=self._done,
                            description=f"{self._done}/{self._total_items} entries")
        if result.status != "COMPLETED":
            self._widget.console.print(f"  [red]failed[/red] {result.title} "
                                       f"[dim]{result.error}[/dim]")


@contextmanager
def playlist_download_progress(total_items: int):
    with _progress_widget() as widget:
        yield PlaylistProgressView(widget, total_items)
