"""Download engine for DownloadIt, built on top of yt-dlp.

The GUI never talks to yt-dlp directly. It creates a ``DownloadWorker`` which
runs on a background thread and reports back through an ``emit`` callback:

    worker = DownloadWorker(tasks, options, emit, cancel_event)
    worker.start()

Event kinds emitted by the worker:
    ("status",    {...})  - a coarse status change for one row
    ("progress",  {...})  - download progress for one row
    ("done",      {...})  - one row finished successfully
    ("error",     {...})  - one row failed
    ("cancelled", {...})  - one row was cancelled
    ("batch_done", {...}) - every row has been processed

TikTok *photo/slideshow* posts (``/@user/photo/<id>``) are not supported by
yt-dlp at all. When one is detected and the optional ``gallery-dl`` package is
installed, the worker falls back to it so those links still download.
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from functools import lru_cache
from glob import escape as glob_escape
from pathlib import Path
from typing import Callable, Sequence

import yt_dlp
from yt_dlp.utils import DownloadError

#: Human readable quality label -> yt-dlp-independent quality key.
QUALITY_LABELS: list[tuple[str, str]] = [
    ("Best available", "best"),
    ("1080p", "1080"),
    ("720p", "720"),
    ("480p", "480"),
    ("360p", "360"),
    ("Audio only (MP3)", "audio"),
]

#: Browsers yt-dlp can read cookies from.
BROWSER_LABELS: list[str] = [
    "None",
    "Chrome",
    "Edge",
    "Firefox",
    "Brave",
    "Opera",
    "Vivaldi",
    "Safari",
]

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_URL_START_RE = re.compile(r"^https?://", re.IGNORECASE)
_URL_RE = re.compile(
    r"(?:https?://)?(?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.)+[A-Za-z]{2,}"
    r"(?::\d{2,5})?(?:[/?#][^\s<>\"',;]*)?",
    re.IGNORECASE,
)
#: Scheme-less local targets such as ``localhost:8080/video.mp4`` or an IP.
_LOCAL_URL_RE = re.compile(
    r"(?:localhost|\d{1,3}(?:\.\d{1,3}){3})(?::\d{2,5})?(?:[/?#][^\s<>\"',;]*)?",
    re.IGNORECASE,
)
_TRAILING_CHARS = ".,;:!?)]}'\""

#: Optional fallback package used for TikTok photo/slideshow posts.
GALLERY_DL_MODULE = "gallery_dl"

_TIKTOK_PHOTO_RE = re.compile(
    r"https?://[^\s\"']*tiktok\.com/@[^/\s\"']+/photo/\d+",
    re.IGNORECASE,
)
_GALLERY_LOG_PREFIX_RE = re.compile(r"^\[[^\]]+\]\[[^\]]+\]\s*")
_PHOTO_POST_HINT = (
    "This link is a TikTok photo/slideshow post, which yt-dlp cannot download.\n\n"
    "Install the optional slideshow helper and try again:\n\n"
    '    "{python}" -m pip install gallery-dl\n\n'
    "Resolved link: {url}"
)


class DownloadCancelled(Exception):
    """Raised from inside a yt-dlp hook when the user presses Cancel."""


@dataclass(slots=True)
class DownloadOptions:
    """Everything needed to run a batch of downloads."""

    output_dir: Path
    quality: str = "best"
    cookies_browser: str | None = None
    audio_format: str = "mp3"
    ffmpeg_location: str | None = None

    @classmethod
    def create(
        cls,
        output_dir: str | Path,
        quality: str = "best",
        cookies_browser: str | None = None,
        audio_format: str = "mp3",
    ) -> "DownloadOptions":
        return cls(
            output_dir=Path(output_dir).expanduser(),
            quality=quality,
            cookies_browser=cookies_browser,
            audio_format=audio_format,
            ffmpeg_location=find_ffmpeg(),
        )


@dataclass(slots=True)
class DownloadTask:
    """A single URL plus the GUI row that should receive its updates."""

    url: str
    row_id: str


@lru_cache(maxsize=1)
def find_ffmpeg() -> str | None:
    """Return a usable ffmpeg path, preferring one already on PATH.

    Falls back to the static binary shipped by ``imageio-ffmpeg`` so users do
    not have to install ffmpeg manually on Windows.
    """
    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return system_ffmpeg

    try:
        import imageio_ffmpeg

        bundled = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # pragma: no cover - optional dependency
        bundled = None

    if bundled and os.path.exists(bundled):
        return bundled
    return None


@lru_cache(maxsize=1)
def gallery_dl_available() -> bool:
    """True when the optional ``gallery-dl`` package can be used."""
    try:
        return importlib.util.find_spec(GALLERY_DL_MODULE) is not None
    except (ImportError, ValueError):  # pragma: no cover - broken install
        return False


def find_tiktok_photo_url(*texts: str | None) -> str | None:
    """Return the first TikTok ``/photo/<id>`` link found in ``texts``.

    yt-dlp reports the *resolved* URL in its "Unsupported URL" error, so a
    short ``vt.tiktok.com`` share link can be recognised after it fails.
    """
    for text in texts:
        if not text:
            continue
        match = _TIKTOK_PHOTO_RE.search(str(text))
        if match:
            return match.group(0).rstrip("/")
    return None


def describe_photo_url(url: str) -> dict:
    """Build display fields for a TikTok photo post URL."""
    match = re.search(r"tiktok\.com/@([^/\s]+)/photo/(\d+)", url)
    if not match:
        return {"title": "TikTok photo post", "platform": "TikTok"}
    return {
        "title": f"Photo post by @{match.group(1)}",
        "platform": "TikTok",
    }


def human_size(num: float | int | None) -> str:
    """Format a byte count for display."""
    if not num:
        return "—"
    value = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"  # pragma: no cover - unreachable


def clean_message(text: object) -> str:
    """Strip ANSI colours and yt-dlp severity prefixes from a message."""
    cleaned = _ANSI_RE.sub("", str(text)).strip()
    for prefix in ("ERROR: ", "WARNING: "):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix) :]
    return cleaned


def parse_urls(text: str) -> tuple[list[str], list[str]]:
    """Extract links from pasted text.

    Text is split on whitespace/commas/semicolons and each token is searched
    for something URL-shaped. A full TikTok share sheet ("Check this out!
    https://vm.tiktok.com/ZMabc/") therefore just works: caption words are
    ignored. Tokens that already start with a scheme are used verbatim (which
    keeps ports, IPs and query strings intact); others get ``https://``
    prepended.

    Returns ``(urls, rejected)`` where ``rejected`` holds tokens that contained
    no usable link. Duplicates are removed while preserving order.
    """
    urls: list[str] = []
    rejected: list[str] = []
    seen: set[str] = set()

    for raw in re.split(r"[\s,;]+", text or ""):
        token = raw.strip().strip("<>\"'()[]")
        if not token:
            continue

        if _URL_START_RE.match(token):
            # Already a complete URL: keep it as-is so ports, IPs, query
            # strings and paths survive untouched.
            candidate = token.rstrip(_TRAILING_CHARS)
        else:
            matches = [
                match
                for match in (_URL_RE.search(token), _LOCAL_URL_RE.search(token))
                if match
            ]
            match = min(matches, key=lambda item: item.start()) if matches else None
            if not match or (
                match.start() > 0 and token[match.start() - 1] == "@"
            ):
                rejected.append(token)
                continue
            candidate = f"https://{match.group(0).rstrip(_TRAILING_CHARS)}"

        dedupe_key = candidate.lower().rstrip("/")
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        urls.append(candidate)

    return urls, rejected


def build_format_selector(quality: str, can_merge: bool = True) -> str:
    """Translate a quality key into a yt-dlp format selector.

    Merging separate video/audio streams and extracting MP3 both require
    ffmpeg, so when ffmpeg is unavailable we ask for a ready-made single file.
    """
    if quality == "audio":
        return "bestaudio/best"

    if quality in ("", "best", None):
        return "bestvideo*+bestaudio/best" if can_merge else "best"

    try:
        height = int(quality)
    except (TypeError, ValueError):
        return "bestvideo*+bestaudio/best" if can_merge else "best"

    if can_merge:
        return f"bestvideo*[height<={height}]+bestaudio/best[height<={height}]/best"
    return f"best[height<={height}]/best"


def build_ydl_options(
    options: DownloadOptions,
    progress_hook: Callable[[dict], None],
    logger: object | None = None,
) -> dict:
    """Build the yt-dlp option dictionary for one task."""
    ffmpeg = options.ffmpeg_location
    output_dir = Path(options.output_dir)
    output_template = str(
        output_dir / "%(extractor_key)s" / "%(title).120B [%(id)s].%(ext)s"
    )

    ydl_opts: dict = {
        "format": build_format_selector(options.quality, can_merge=bool(ffmpeg)),
        "outtmpl": {"default": output_template},
        "noplaylist": True,
        "windowsfilenames": True,
        "trim_file_name": 180,
        "quiet": True,
        "noprogress": True,
        "no_warnings": False,
        "ignoreerrors": False,
        "retries": 5,
        "fragment_retries": 5,
        "socket_timeout": 30,
        "concurrent_fragment_downloads": 4,
        "continuedl": True,
        "progress_hooks": [progress_hook],
    }

    if logger is not None:
        ydl_opts["logger"] = logger

    if ffmpeg:
        ydl_opts["ffmpeg_location"] = ffmpeg

    if options.cookies_browser:
        ydl_opts["cookiesfrombrowser"] = (options.cookies_browser.lower(),)

    if options.quality == "audio" and ffmpeg:
        ydl_opts["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": options.audio_format or "mp3",
                "preferredquality": "0",
            }
        ]

    return ydl_opts


def describe(info: dict) -> dict:
    """Pull the display fields out of a yt-dlp info dict."""
    if not info:
        return {}

    title = info.get("title") or info.get("description") or ""
    title = " ".join(str(title).split())
    if len(title) > 110:
        title = title[:107] + "…"

    platform = (
        info.get("extractor_key")
        or info.get("extractor")
        or info.get("webpage_url_domain")
        or ""
    )

    return {"title": title, "platform": str(platform)}


def _single_entry(info: dict) -> dict:
    """Unwrap a playlist-like result down to its first real entry."""
    seen: set[int] = set()
    while isinstance(info, dict) and info.get("_type") in ("playlist", "multi_video"):
        if id(info) in seen:
            break
        seen.add(id(info))
        entries = [entry for entry in (info.get("entries") or []) if entry]
        if not entries:
            break
        info = entries[0]
    return info


class _CaptureLogger:
    """Minimal yt-dlp logger that remembers the last error message."""

    def __init__(self) -> None:
        self.errors: list[str] = []

    def debug(self, msg: str) -> None:
        pass

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        pass

    def error(self, msg: str) -> None:
        text = clean_message(msg)
        if text:
            self.errors.append(text)

    def __call__(self, msg: str) -> None:  # pragma: no cover - older yt-dlp
        self.info(msg)


def make_progress_hook(
    emit: Callable[[str, dict], None],
    cancel_event: threading.Event,
    row_id: str,
    min_interval: float = 0.15,
) -> Callable[[dict], None]:
    """Create a yt-dlp progress hook that emits throttled GUI events."""
    last_emit = 0.0

    def hook(data: dict) -> None:
        nonlocal last_emit

        if cancel_event.is_set():
            raise DownloadCancelled()

        status = data.get("status")
        info = data.get("info_dict") or {}
        label = describe(info)

        if status == "downloading":
            now = time.monotonic()
            if now - last_emit < min_interval:
                return
            last_emit = now

            total = data.get("total_bytes") or data.get("total_bytes_estimate")
            downloaded = data.get("downloaded_bytes") or 0
            percent = (downloaded / total * 100.0) if total else None

            emit(
                "progress",
                {
                    "row": row_id,
                    "percent": percent,
                    "downloaded": downloaded,
                    "total": total,
                    "speed": data.get("speed"),
                    **label,
                },
            )
        elif status == "finished":
            emit(
                "status",
                {
                    "row": row_id,
                    "status": "Processing…",
                    **label,
                },
            )

    return hook


def resolve_output_file(info: dict, output_dir: Path) -> Path | None:
    """Find the file yt-dlp produced (post-processing can rename it)."""
    candidates: list[str] = []

    for download in info.get("requested_downloads") or []:
        for key in ("filepath", "_filename", "filename"):
            value = download.get(key)
            if value:
                candidates.append(value)

    for key in ("filepath", "_filename"):
        value = info.get(key)
        if value:
            candidates.append(value)

    for value in candidates:
        path = Path(value)
        if path.exists() and path.suffix not in {".part", ".ytdl"}:
            return path

    video_id = info.get("id")
    if video_id:
        pattern = f"*{glob_escape(f'[{video_id}]')}*"
        matches = [
            path
            for path in Path(output_dir).rglob(pattern)
            if path.is_file() and path.suffix not in {".part", ".ytdl"}
        ]
        if matches:
            return max(matches, key=lambda path: path.stat().st_mtime)

    return None


def format_error(exc: BaseException, logger: _CaptureLogger) -> str:
    """Produce a readable, user-facing error message."""
    message = clean_message(exc)
    errors = [error for error in logger.errors if error]

    if errors:
        last = errors[-1]
        if last not in message:
            message = f"{last}\n\n{message}" if message else last

    return message or exc.__class__.__name__


def _read_gallery_dl_paths(list_file: Path) -> list[Path]:
    """Read the file list written by ``gallery-dl --Print-to-file``."""
    if not list_file.exists():
        return []

    files: list[Path] = []
    seen: set[str] = set()
    for line in list_file.read_text(encoding="utf-8", errors="replace").splitlines():
        raw = line.strip()
        if not raw:
            continue
        path = Path(raw)
        key = str(path).lower()
        if key in seen:
            continue
        seen.add(key)
        if path.is_file():
            files.append(path)
    return files


def _read_gallery_dl_error(log_file: Path) -> str:
    """Pick the most useful line out of a gallery-dl log file."""
    if not log_file.exists():
        return ""

    lines = [
        _GALLERY_LOG_PREFIX_RE.sub("", line.strip())
        for line in log_file.read_text(encoding="utf-8", errors="replace").splitlines()
        if line.strip()
    ]
    for line in reversed(lines):
        if "error" in line.lower() or "warning" in line.lower():
            return clean_message(line)
    return clean_message(lines[-1]) if lines else ""


class DownloadWorker(threading.Thread):
    """Sequentially downloads a batch of tasks on a background thread."""

    def __init__(
        self,
        tasks: Sequence[DownloadTask],
        options: DownloadOptions,
        emit: Callable[[str, dict], None],
        cancel_event: threading.Event,
    ) -> None:
        super().__init__(name="downloadit-worker", daemon=True)
        self.tasks = list(tasks)
        self.options = options
        self._emit = emit
        self._cancel = cancel_event
        self.completed = 0
        self.failed = 0
        self.cancelled = 0

    def run(self) -> None:
        try:
            Path(self.options.output_dir).mkdir(parents=True, exist_ok=True)
            for task in self.tasks:
                if self._cancel.is_set():
                    self.cancelled += 1
                    self._emit("cancelled", {"row": task.row_id})
                    continue
                self._download(task)
        finally:
            self._emit(
                "batch_done",
                {
                    "completed": self.completed,
                    "failed": self.failed,
                    "cancelled": self.cancelled,
                },
            )

    def _download(self, task: DownloadTask) -> None:
        logger = _CaptureLogger()
        hook = make_progress_hook(self._emit, self._cancel, task.row_id)
        self._emit("status", {"row": task.row_id, "status": "Starting…"})

        try:
            ydl_opts = build_ydl_options(self.options, hook, logger)
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(task.url, download=True)

            if not info:
                raise DownloadError("No downloadable media was found at that link.")

            info = _single_entry(info)
            path = resolve_output_file(info, Path(self.options.output_dir))

            self.completed += 1
            self._emit(
                "done",
                {
                    "row": task.row_id,
                    "path": str(path) if path else "",
                    "size": info.get("filesize") or info.get("filesize_approx"),
                    **describe(info),
                },
            )
        except DownloadCancelled:
            self.cancelled += 1
            self._emit("cancelled", {"row": task.row_id})
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            if self._cancel.is_set():
                self.cancelled += 1
                self._emit("cancelled", {"row": task.row_id})
                return

            error_text = format_error(exc, logger)
            if self._download_photo_post(task, error_text):
                return

            self.failed += 1
            self._emit(
                "error",
                {
                    "row": task.row_id,
                    "message": error_text,
                    "url": task.url,
                },
            )

    # ------------------------------------------------------- photo posts

    def _download_photo_post(self, task: DownloadTask, error_text: str) -> bool:
        """Try the gallery-dl fallback for a TikTok photo/slideshow post.

        Returns ``True`` when this task was handled (success, failure or
        cancellation) so the caller must not report the yt-dlp error.
        """
        photo_url = find_tiktok_photo_url(error_text, task.url)
        if not photo_url:
            return False

        if not gallery_dl_available():
            self.failed += 1
            self._emit(
                "error",
                {
                    "row": task.row_id,
                    "message": _PHOTO_POST_HINT.format(
                        python=sys.executable, url=photo_url
                    ),
                    "url": task.url,
                },
            )
            return True

        self._emit(
            "status",
            {
                "row": task.row_id,
                "status": "Downloading photo post…",
                **describe_photo_url(photo_url),
            },
        )

        try:
            files = self._run_gallery_dl(task, photo_url)
        except DownloadCancelled:
            self.cancelled += 1
            self._emit("cancelled", {"row": task.row_id})
            return True
        except Exception as exc:
            if self._cancel.is_set():
                self.cancelled += 1
                self._emit("cancelled", {"row": task.row_id})
                return True

            self.failed += 1
            self._emit(
                "error",
                {"row": task.row_id, "message": clean_message(exc), "url": task.url},
            )
            return True

        if not files:
            self.failed += 1
            self._emit(
                "error",
                {
                    "row": task.row_id,
                    "message": "The photo post downloaded nothing — it may be private or deleted.",
                    "url": task.url,
                },
            )
            return True

        size = sum(path.stat().st_size for path in files if path.exists())
        self.completed += 1
        self._emit(
            "done",
            {
                "row": task.row_id,
                "path": str(files[0]),
                "files": [str(path) for path in files],
                "count": len(files),
                "size": size,
                **describe_photo_url(photo_url),
            },
        )
        return True

    def _run_gallery_dl(self, task: DownloadTask, url: str) -> list[Path]:
        """Run gallery-dl for one URL and return the files it wrote."""
        output_dir = Path(self.options.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory(prefix="downloadit-gdl-") as tmp:
            list_file = Path(tmp) / "files.txt"
            log_file = Path(tmp) / "log.txt"

            cmd = [
                sys.executable,
                "-m",
                GALLERY_DL_MODULE,
                "--config-ignore",
                "--quiet",
                "--no-mtime",
                "--dest",
                str(output_dir),
                "--Print-to-file",
                "after:{_path}",
                str(list_file),
            ]
            if self.options.cookies_browser:
                cmd += ["--cookies-from-browser", self.options.cookies_browser]
            cmd.append(url)

            popen_kwargs: dict = {}
            if sys.platform == "win32":
                popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

            with open(log_file, "w", encoding="utf-8", errors="replace") as log:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=log,
                    stdin=subprocess.DEVNULL,
                    **popen_kwargs,
                )
                try:
                    while proc.poll() is None:
                        if self._cancel.is_set():
                            proc.terminate()
                            try:
                                proc.wait(timeout=5)
                            except subprocess.TimeoutExpired:
                                proc.kill()
                            raise DownloadCancelled()
                        time.sleep(0.1)
                finally:
                    if proc.poll() is None:
                        proc.kill()

            files = _read_gallery_dl_paths(list_file)
            if not files and proc.returncode not in (0, None):
                raise DownloadError(
                    _read_gallery_dl_error(log_file)
                    or f"gallery-dl failed with exit code {proc.returncode}."
                )
            return files
