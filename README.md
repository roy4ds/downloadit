# DownloadIt

A small desktop app for saving videos from **TikTok, Instagram, YouTube, X/Twitter,
Facebook, Reddit, Vimeo** and [1000+ other sites](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md) —
just paste the links and press **Download**.

Built with Python + Tkinter on top of [yt-dlp](https://github.com/yt-dlp/yt-dlp).
Quality merging and MP3 conversion use a bundled ffmpeg binary, so nothing else
needs to be installed.

![layout](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-1a73e8)

## Features

- Paste one link per line (or a whole list) and download them sequentially
- Quality picker: Best / 1080p / 720p / 480p / 360p / Audio only (MP3)
- TikTok **photo/slideshow** posts work too, via an optional `gallery-dl` fallback
  (slides save as image files plus the post's audio track)
- Per-download progress, size, and status in a queue table
- **Retry failed** re-downloads every row that failed, in place (a retry also
  works from the right-click menu for a single row)
- **Clear completed** removes only the finished rows from the list — files on
  disk are never touched
- Cancel mid-batch, resume partial downloads automatically on the next try
- Choose where files are saved (defaults to `~/Downloads/DownloadIt`)
- Optional "cookies from browser" for private or login-only posts
- Right-click a row to open the file, show it in Explorer/Finder, copy the link,
  retry it, or inspect an error
- Files are organised into per-site subfolders

## Requirements

- Python 3.10 or newer (with Tkinter, included in the python.org installer)
- Internet access

## Setup

```powershell
cd c:\Users\Admin\projects\others\downloadit
c:/python314/python.exe -m pip install -r requirements.txt
```

## Run

```powershell
c:/python314/python.exe main.py
```

1. Paste links into the box — e.g. `https://www.tiktok.com/@user/video/1234567890`
2. Pick a quality (leave "Best available" for the highest resolution)
3. Press **Download**

### Retrying and cleaning up

| Button | What it does |
| --- | --- |
| **Retry failed** | Re-queues every `Failed` row using the current quality/cookies/folder settings. Enabled automatically as soon as something fails. |
| **Clear completed** | Removes `Done` rows from the list so only unfinished/failed work remains. Downloaded files stay on disk. |

Failed rows are kept on purpose: fix the cause (update yt-dlp, pick cookies,
change the folder) and press **Retry failed**. Right-click any row → **Retry
download** to re-download just that link, and **Remove from list** to drop a row.

## Usage notes

### TikTok

TikTok changes its site often. If downloads suddenly fail, update yt-dlp first:

```powershell
c:/python314/python.exe -m pip install -U yt-dlp
```

TikTok links are saved without the watermark whenever TikTok exposes the clean
source. Private or region-locked posts need a logged-in session, so pick your
browser under **Cookies from browser** (close the browser first if it is running
on Windows, since its cookie database may be locked).

### TikTok photo / slideshow posts

Links such as `https://www.tiktok.com/@user/photo/1234567890123456789` (and short
`vt.`/`vm.` links that redirect to them) are not videos, and yt-dlp cannot handle
them at all. DownloadIt detects these and falls back to `gallery-dl`, which saves
every slide image **and** the post's audio track into the same output folder.

That fallback is the optional `gallery-dl` package from `requirements.txt`. If the
Options row says *"photo posts: install gallery-dl"*, install it with:

```powershell
c:/python314/python.exe -m pip install gallery-dl
```

### Instagram / Facebook / private posts

These usually require login cookies. Choose the browser you are logged in with
under **Cookies from browser**, then retry.

### Audio only (MP3)

Requires ffmpeg. The app automatically uses the static ffmpeg shipped by the
`imageio-ffmpeg` package. If the options row says *"ffmpeg: not found"*,
reinstall the requirements or put `ffmpeg` on your `PATH`.

## Project layout

| File | Purpose |
| --- | --- |
| `main.py` | Entry point; checks dependencies and opens the window |
| `gui.py` | Tkinter UI, queue table, progress and row actions |
| `downloader.py` | yt-dlp engine: format selection, events, cancellation, error handling, photo-post fallback |
| `requirements.txt` | Runtime dependencies |

`downloader.py` is deliberately UI-free, so it can be reused from a script:

```python
import threading
from pathlib import Path

from downloader import DownloadOptions, DownloadTask, DownloadWorker

options = DownloadOptions.create(Path.home() / "Downloads" / "DownloadIt", quality="720")
worker = DownloadWorker(
    [DownloadTask(url="https://www.tiktok.com/@user/video/123", row_id="1")],
    options,
    emit=lambda kind, payload: print(kind, payload),
    cancel_event=threading.Event(),
)
worker.start()
```

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| "Requested format is not available" | Update yt-dlp, or pick **Best available** |
| Login / "Sign in to confirm" | Set **Cookies from browser** |
| Download fails with HTTP 403 | The link may be expired; re-copy it from the app |
| "Unsupported URL" for a `/photo/` link | Photo posts need `gallery-dl` (see above) |
| Photo post saves 0 files | Post may be private/deleted; retry with browser cookies |
| MP3 options unavailable | ffmpeg missing — install `imageio-ffmpeg` |
| Nothing happens on launch | Run from a terminal to see the traceback |

## Packaging into an .exe (optional)

```powershell
c:/python314/python.exe -m pip install pyinstaller
c:/python314/python.exe -m PyInstaller --noconfirm --windowed --name DownloadIt --collect-all imageio_ffmpeg main.py
```

The resulting app is in `dist\DownloadIt\`.

## Legal

Only download content you own or have permission to save. Respect each site's
terms of service and local copyright law.
