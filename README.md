# gravity-cli

## A terminal downloader for YouTube videos, playlists and auto-generated mixes.

To run, paste this in cmd/pwsh:

```
git clone https://github.com/h-kakar11/gravity-cli
cd gravity-cli
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
py app.py
```

You get an arrow-key menu, paste a link, pick a quality and a destination, and watch a
live progress bar. Everything you download is recorded in a local history.

## What it does

- **Single videos** — paste any YouTube video link.
- **Playlists** — enumerated up front, shown as a list, then downloaded one at a time in
  order with `01 - Title` numbering into a folder of their own. A private or deleted
  entry costs exactly that one entry; the rest of the list carries on.
- **YouTube Mixes** (`list=RD...`) — supported, with a cap and a warning. A Mix is not a
  real playlist: YouTube synthesizes it endlessly around a seed track, so it has no fixed
  contents or order, it can differ between runs, and it can repeat tracks. You choose how
  many entries to take (default 50), the resolved list is shown, and you confirm before
  anything downloads.
- **Combo links** (`watch?v=...&list=...`) — you are asked whether you meant the one video
  or the whole list, rather than the tool guessing.

## Requirements

- **Python 3.9+**
- **ffmpeg** on your `PATH`. YouTube serves video and audio as separate streams for
  anything above the lowest quality, and yt-dlp needs ffmpeg to merge them. On Windows:
  `winget install Gyan.FFmpeg`, then reopen your terminal.

## Setup

```powershell
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
py app.py
```

## Where your files go

| What | Where |
|---|---|
| Downloads | wherever you point them (remembered between runs) |
| Settings | `%LOCALAPPDATA%\gravity-cli\config.json` |
| History | `%LOCALAPPDATA%\gravity-cli\history.json` (newest first, last 500) |

## How this relates to Gravity

The yt-dlp integration is not reimplemented here. `vendor/downloader.py` is a
byte-identical copy of Gravity's `python/downloader/downloader.py`, driven exactly the way
Gravity's C++ core drives it: spawned with `--command-stdin`, handed one JSON command,
streaming NDJSON events back over stdout. That brings across its yt-dlp probe options,
error classification, progress hooks, output-template escaping and mix/playlist URL
detection unchanged.

To re-sync it after upstream changes, copy the file over and run the tests:

```powershell
copy ..\gravity\python\downloader\downloader.py vendor\downloader.py
py -m unittest discover -s tests -t .
```

`tests/test_downloader_protocol.py` and `tests/test_downloader_selftest.py` are Gravity's
own suites for that file, carried over so drift shows up as a test failure.

Three things Gravity keeps in C++ are ported to Python here, because there is no C++ layer
in this tool:

| Ported from | To |
|---|---|
| `engines/downloader/YtDlpFormatSelector.cpp` | `gravity_cli/format_selector.py` |
| `core/filesystem/FilenameSanitizer.{h,cpp}` | `gravity_cli/filenames.py` |
| `core/jobs/JobHistoryStore.{h,cpp}` | `gravity_cli/history.py` |

**One deliberate divergence:** Gravity refuses auto-generated Mixes outright. This tool
downloads them, capped and clearly labelled — see `gravity_cli/mixes.py`.

## Tests

```powershell
py -m unittest discover -s tests -t .
```

Tests that exercise the "yt-dlp is not installed" fallback skip themselves when yt-dlp is
present, which it will be in a normal setup here.
