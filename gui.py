"""Tkinter desktop interface for DownloadIt."""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from downloader import (
    BROWSER_LABELS,
    QUALITY_LABELS,
    DownloadOptions,
    DownloadTask,
    DownloadWorker,
    find_ffmpeg,
    gallery_dl_available,
    human_size,
    parse_urls,
)

QUALITY_BY_LABEL = {label: value for label, value in QUALITY_LABELS}
BROWSER_BY_LABEL = {
    label: (None if label == "None" else label.lower()) for label in BROWSER_LABELS
}

#: Row states used for logic (display text lives in the tree instead).
STATE_QUEUED = "queued"
STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"
TERMINAL_STATES = {STATE_DONE, STATE_FAILED, STATE_CANCELLED}


def default_output_dir() -> Path:
    """Default save location: ~/Downloads/DownloadIt when possible."""
    downloads = Path.home() / "Downloads"
    base = downloads if downloads.exists() else Path.home()
    return base / "DownloadIt"


def quality_from_label(label: str) -> str:
    """Map a combobox label back to a quality key (labels may be shortened)."""
    for text, value in QUALITY_LABELS:
        if label in (text, text.replace(" (MP3)", "")):
            return value
    return "best"


def open_path(path: Path) -> None:
    """Open a file or folder in the OS default handler."""
    if sys.platform == "win32":
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


class DownloadItApp(tk.Tk):
    """Main window: paste links, pick options, watch the queue."""

    POLL_MS = 80

    def __init__(self) -> None:
        super().__init__()

        self.title("DownloadIt — Social Video Downloader")
        self.geometry("1020x740")
        self.minsize(880, 620)

        self._events: queue.Queue[tuple[str, dict]] = queue.Queue()
        self._cancel_event = threading.Event()
        self._worker: DownloadWorker | None = None
        self._rows: dict[str, dict] = {}
        self._row_seq = 0
        self._total_tasks = 0
        self._finished_tasks = 0
        self._pump_job: str | None = None
        self._menu_row: str | None = None
        self._busy = False

        self._ffmpeg = find_ffmpeg()
        self._slideshow_ready = gallery_dl_available()
        self._quality_labels = [label for label, _ in QUALITY_LABELS]
        if not self._ffmpeg:
            # Without ffmpeg we cannot convert to MP3, only fetch audio as-is.
            self._quality_labels = [
                label.replace(" (MP3)", "") for label in self._quality_labels
            ]

        self._output_var = tk.StringVar(value=str(default_output_dir()))
        self._quality_var = tk.StringVar(value=self._quality_labels[0])
        self._cookies_var = tk.StringVar(value="None")
        self._status_var = tk.StringVar(value="Ready.")
        self._open_when_done = tk.BooleanVar(value=False)

        self._build_ui()
        self._apply_theme()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._url_text.focus_set()

    # ---------------------------------------------------------------- layout

    def _build_ui(self) -> None:
        outer = ttk.Frame(self, padding=(16, 14, 16, 12))
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(3, weight=1)

        header = ttk.Frame(outer)
        header.grid(row=0, column=0, sticky="ew")
        ttk.Label(header, text="DownloadIt", style="Title.TLabel").pack(side="left")
        ttk.Label(
            header,
            text="Paste links from TikTok, Instagram, YouTube, X, Facebook, Reddit and 1000+ more sites.",
            style="Hint.TLabel",
        ).pack(side="left", padx=(12, 0), pady=(8, 0))

        self._build_links(outer)
        self._build_options(outer)
        self._build_queue(outer)
        self._build_actions(outer)
        self._refresh_buttons()

    def _build_links(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Video links", padding=10)
        frame.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        frame.columnconfigure(0, weight=1)

        self._url_text = tk.Text(
            frame,
            height=4,
            wrap="word",
            undo=True,
            font=("Segoe UI", 10),
            relief="solid",
            borderwidth=1,
            padx=8,
            pady=6,
        )
        self._url_text.grid(row=0, column=0, rowspan=2, sticky="nsew")

        scroll = ttk.Scrollbar(frame, orient="vertical", command=self._url_text.yview)
        scroll.grid(row=0, column=1, rowspan=2, sticky="ns")
        self._url_text.configure(yscrollcommand=scroll.set)

        buttons = ttk.Frame(frame)
        buttons.grid(row=0, column=2, rowspan=2, sticky="ns", padx=(10, 0))
        ttk.Button(buttons, text="Paste", command=self._paste).pack(fill="x")
        ttk.Button(buttons, text="Clear", command=self._clear_links).pack(
            fill="x", pady=(6, 0)
        )

        ttk.Label(
            frame,
            text="One link per line. Duplicates are skipped automatically.",
            style="Hint.TLabel",
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(6, 0))

    def _build_options(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Options", padding=10)
        frame.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        frame.columnconfigure(1, weight=1)

        ttk.Label(frame, text="Save to").grid(row=0, column=0, sticky="w")
        ttk.Entry(frame, textvariable=self._output_var).grid(
            row=0, column=1, sticky="ew", padx=(10, 8)
        )
        ttk.Button(frame, text="Browse…", command=self._browse).grid(row=0, column=2)

        controls = ttk.Frame(frame)
        controls.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(10, 0))

        ttk.Label(controls, text="Quality").pack(side="left")
        ttk.Combobox(
            controls,
            textvariable=self._quality_var,
            values=self._quality_labels,
            state="readonly",
            width=20,
        ).pack(side="left", padx=(8, 20))

        ttk.Label(controls, text="Cookies from browser").pack(side="left")
        ttk.Combobox(
            controls,
            textvariable=self._cookies_var,
            values=BROWSER_LABELS,
            state="readonly",
            width=12,
        ).pack(side="left", padx=(8, 0))

        secondary = ttk.Frame(frame)
        secondary.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(8, 0))

        if self._ffmpeg:
            ffmpeg_text = "ffmpeg: ready (HD merging and MP3 enabled)"
            ffmpeg_style = "Hint.TLabel"
        else:
            ffmpeg_text = "ffmpeg: not found — HD merging and MP3 disabled"
            ffmpeg_style = "Warn.TLabel"
        ttk.Label(secondary, text=ffmpeg_text, style=ffmpeg_style).pack(side="left")

        if self._slideshow_ready:
            slideshow_text = " · photo posts: ready"
            slideshow_style = "Hint.TLabel"
        else:
            slideshow_text = " · photo posts: install gallery-dl"
            slideshow_style = "Warn.TLabel"
        ttk.Label(secondary, text=slideshow_text, style=slideshow_style).pack(
            side="left"
        )

        ttk.Checkbutton(
            secondary,
            text="Open folder when finished",
            variable=self._open_when_done,
        ).pack(side="right")

        ttk.Label(
            frame,
            text="Tip: choose a browser for private or login-only posts (Instagram, age-gated YouTube).",
            style="Hint.TLabel",
        ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(8, 0))

    def _build_queue(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Downloads", padding=10)
        frame.grid(row=3, column=0, sticky="nsew", pady=(10, 0))
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)

        columns = ("title", "platform", "status", "size", "progress")
        headings = {
            "title": "Title",
            "platform": "Site",
            "status": "Status",
            "size": "Size",
            "progress": "Progress",
        }
        widths = {
            "title": 480,
            "platform": 110,
            "status": 150,
            "size": 100,
            "progress": 90,
        }
        anchors = {
            "title": "w",
            "platform": "w",
            "status": "w",
            "size": "e",
            "progress": "center",
        }

        self._tree = ttk.Treeview(
            frame, columns=columns, show="headings", selectmode="browse"
        )
        for column in columns:
            self._tree.heading(column, text=headings[column])
            self._tree.column(
                column,
                width=widths[column],
                anchor=anchors[column],
                stretch=(column == "title"),
            )
        self._tree.grid(row=0, column=0, sticky="nsew")

        scroll = ttk.Scrollbar(frame, orient="vertical", command=self._tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self._tree.configure(yscrollcommand=scroll.set)

        self._tree.tag_configure("done", foreground="#137333")
        self._tree.tag_configure("error", foreground="#c5221f")
        self._tree.tag_configure("active", foreground="#1a73e8")
        self._tree.tag_configure("cancelled", foreground="#80868b")

        self._tree.bind("<Double-1>", self._on_double_click)
        self._tree.bind("<Button-3>", self._on_right_click)

        self._menu = tk.Menu(self, tearoff=0)
        self._menu.add_command(label="Open file", command=self._open_selected_file)
        self._menu.add_command(
            label="Show in folder", command=self._show_selected_in_folder
        )
        self._menu.add_command(label="Copy link", command=self._copy_selected_link)
        self._menu.add_command(label="Show details", command=self._show_selected_details)
        self._menu.add_separator()
        self._menu.add_command(
            label="Retry download", command=self._retry_selected
        )
        self._menu.add_command(
            label="Remove from list", command=self._remove_selected_row
        )

    def _build_actions(self, parent: ttk.Frame) -> None:
        actions = ttk.Frame(parent)
        actions.grid(row=4, column=0, sticky="ew", pady=(12, 0))

        self._download_btn = ttk.Button(
            actions, text="Download", style="Accent.TButton", command=self._start
        )
        self._download_btn.pack(side="left")

        self._cancel_btn = ttk.Button(
            actions, text="Cancel", command=self._cancel, state="disabled"
        )
        self._cancel_btn.pack(side="left", padx=(8, 0))

        self._retry_btn = ttk.Button(
            actions,
            text="Retry failed",
            command=self._retry_failed,
            state="disabled",
        )
        self._retry_btn.pack(side="left", padx=(8, 0))

        ttk.Button(
            actions, text="Open downloads folder", command=self._open_output_folder
        ).pack(side="right")

        self._clear_btn = ttk.Button(
            actions,
            text="Clear completed",
            command=self._clear_completed,
            state="disabled",
        )
        self._clear_btn.pack(side="right", padx=(0, 8))

        footer = ttk.Frame(parent)
        footer.grid(row=5, column=0, sticky="ew", pady=(10, 0))
        footer.columnconfigure(0, weight=1)

        ttk.Label(footer, textvariable=self._status_var, style="Hint.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        self._progress = ttk.Progressbar(
            footer, mode="determinate", maximum=100, length=280
        )
        self._progress.grid(row=0, column=1, sticky="e", padx=(12, 0))

    def _apply_theme(self) -> None:
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")

        style.configure("Title.TLabel", font=("Segoe UI", 15, "bold"))
        style.configure("Hint.TLabel", font=("Segoe UI", 9), foreground="#5f6368")
        style.configure("Warn.TLabel", font=("Segoe UI", 9), foreground="#b06000")
        style.configure("Accent.TButton", font=("Segoe UI", 10, "bold"))
        style.configure("Treeview", rowheight=26, font=("Segoe UI", 9))
        style.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"))

    # ------------------------------------------------------------- user input

    def _paste(self) -> None:
        try:
            text = self.clipboard_get()
        except tk.TclError:
            return

        current = self._url_text.get("1.0", "end").strip()
        if current:
            self._url_text.insert("end", "\n")
        self._url_text.insert("end", text.strip())
        self._url_text.see("end")

    def _clear_links(self) -> None:
        self._url_text.delete("1.0", "end")

    def _browse(self) -> None:
        selected = filedialog.askdirectory(
            title="Choose where downloads are saved",
            initialdir=self._output_var.get() or str(default_output_dir()),
        )
        if selected:
            self._output_var.set(selected)

    # -------------------------------------------------------------- downloads

    def _start(self) -> None:
        if self._is_running():
            return

        urls, _ignored = parse_urls(self._url_text.get("1.0", "end"))

        if not urls:
            if self._url_text.get("1.0", "end").strip():
                messagebox.showinfo(
                    "No links found",
                    "We couldn't find a video link in that text.\n\n"
                    "Paste a link such as https://www.tiktok.com/@user/video/123…",
                )
            else:
                messagebox.showinfo("No links yet", "Paste at least one video link first.")
            return

        output_dir = self._resolve_output_dir()
        if output_dir is None:
            return

        tasks = [
            DownloadTask(url=url, row_id=self._add_row(url)) for url in urls
        ]
        self._run_tasks(tasks, output_dir)

    def _resolve_output_dir(self) -> Path | None:
        """Validate/create the chosen save folder, or return None on failure."""
        output_dir = Path(
            self._output_var.get().strip() or str(default_output_dir())
        ).expanduser()
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror(
                "Cannot use that folder", f"{output_dir}\n\n{exc}"
            )
            return None
        self._output_var.set(str(output_dir))
        return output_dir

    def _run_tasks(self, tasks: list[DownloadTask], output_dir: Path) -> None:
        """Start a worker for ``tasks`` using the options currently in the UI."""
        if not tasks:
            return

        options = DownloadOptions(
            output_dir=output_dir,
            quality=quality_from_label(self._quality_var.get()),
            cookies_browser=BROWSER_BY_LABEL.get(self._cookies_var.get()),
            ffmpeg_location=self._ffmpeg,
        )

        self._cancel_event = threading.Event()
        self._total_tasks = len(tasks)
        self._finished_tasks = 0

        self._set_busy(True)
        self._progress["value"] = 0
        self._status_var.set(f"Downloading 0 of {self._total_tasks}…")

        self._worker = DownloadWorker(
            tasks, options, self._emit_event, self._cancel_event
        )
        self._worker.start()
        self._schedule_poll()

    def _cancel(self) -> None:
        if self._worker and self._worker.is_alive():
            self._cancel_event.set()
            self._cancel_btn.configure(state="disabled")
            self._status_var.set("Cancelling…")

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._refresh_buttons()

    def _is_running(self) -> bool:
        """True while a batch is downloading."""
        return self._busy or bool(self._worker and self._worker.is_alive())

    def _refresh_buttons(self) -> None:
        """Enable actions that make sense for the current queue contents."""
        has_failed = any(
            row["state"] == STATE_FAILED for row in self._rows.values()
        )
        has_done = any(row["state"] == STATE_DONE for row in self._rows.values())

        self._download_btn.configure(
            state="disabled" if self._busy else "normal"
        )
        self._cancel_btn.configure(
            state="normal" if self._busy else "disabled"
        )
        self._retry_btn.configure(
            state="normal" if (has_failed and not self._busy) else "disabled"
        )
        self._clear_btn.configure(
            state="normal" if has_done else "disabled"
        )

    # ---------------------------------------------------------------- retry

    def _retry_failed(self) -> None:
        """Re-queue every row that failed, keeping the same table entries."""
        if self._is_running():
            messagebox.showinfo(
                "Download in progress",
                "Wait for the current downloads to finish, or press Cancel first.",
            )
            return

        failed = [
            row_id
            for row_id, row in self._rows.items()
            if row["state"] == STATE_FAILED
        ]
        if not failed:
            messagebox.showinfo(
                "Nothing to retry", "There are no failed downloads to retry."
            )
            return

        output_dir = self._resolve_output_dir()
        if output_dir is None:
            return

        tasks = [self._reset_row(row_id) for row_id in failed]
        self._status_var.set(
            f"Retrying {len(tasks)} failed "
            f"download{'s' if len(tasks) != 1 else ''}…"
        )
        self._run_tasks(tasks, output_dir)

    def _retry_selected(self) -> None:
        """Re-queue just the highlighted row (useful for cancelled items)."""
        row_id = self._selected_row()
        row = self._rows.get(row_id) if row_id else None
        if not row_id or row is None:
            return

        if self._is_running():
            messagebox.showinfo(
                "Download in progress",
                "Wait for the current downloads to finish, or press Cancel first.",
            )
            return

        output_dir = self._resolve_output_dir()
        if output_dir is None:
            return

        task = self._reset_row(row_id)
        self._status_var.set("Retrying 1 download…")
        self._run_tasks([task], output_dir)

    def _reset_row(self, row_id: str) -> DownloadTask:
        """Put a row back into the queue and return a task for it."""
        row = self._rows[row_id]
        row["path"] = ""
        row["files"] = []
        row["error"] = ""

        self._update_row(
            row_id,
            status="Queued",
            size="—",
            progress="—",
            state=STATE_QUEUED,
        )
        self._tree.item(row_id, tags=())
        self._tree.see(row_id)
        return DownloadTask(url=row["url"], row_id=row_id)

    def _emit_event(self, kind: str, payload: dict) -> None:
        """Called from the worker thread — only touches the queue."""
        self._events.put((kind, payload))

    def _schedule_poll(self) -> None:
        if self._pump_job is None:
            self._pump_job = self.after(self.POLL_MS, self._poll_events)

    def _poll_events(self) -> None:
        self._pump_job = None

        while True:
            try:
                kind, payload = self._events.get_nowait()
            except queue.Empty:
                break
            self._handle_event(kind, payload)

        if (self._worker and self._worker.is_alive()) or not self._events.empty():
            self._schedule_poll()

    def _handle_event(self, kind: str, payload: dict) -> None:
        row_id = payload.get("row")

        if kind == "status":
            self._update_row(
                row_id,
                status=payload.get("status"),
                title=payload.get("title"),
                platform=payload.get("platform"),
                tag="active",
                state=STATE_RUNNING,
            )
        elif kind == "progress":
            percent = payload.get("percent")
            downloaded = payload.get("downloaded")
            total = payload.get("total")
            speed = payload.get("speed")

            size_text = (
                human_size(total)
                if total
                else (human_size(downloaded) if downloaded else "—")
            )
            progress_text = (
                f"{percent:.0f}%" if percent is not None else human_size(downloaded)
            )

            self._update_row(
                row_id,
                status="Downloading",
                title=payload.get("title"),
                platform=payload.get("platform"),
                size=size_text,
                progress=progress_text,
                tag="active",
                state=STATE_RUNNING,
            )
            self._refresh_overall_progress(percent)

            current_index = min(self._finished_tasks + 1, self._total_tasks)
            status_line = f"Downloading {current_index} of {self._total_tasks}"
            if percent is not None:
                status_line += f" · {percent:.0f}%"
            if speed:
                status_line += f" · {human_size(speed)}/s"
            self._status_var.set(status_line)
        elif kind == "done":
            self._finished_tasks += 1
            path = payload.get("path") or ""
            files = [item for item in (payload.get("files") or []) if item]
            count = payload.get("count") or len(files) or 1

            self._update_row(
                row_id,
                status="Done" if count <= 1 else f"Done ({count} files)",
                title=payload.get("title"),
                platform=payload.get("platform"),
                size=human_size(payload.get("size")),
                progress="100%",
                tag="done",
                state=STATE_DONE,
            )
            row = self._rows.get(row_id)
            if row is not None:
                row["path"] = path
                row["files"] = files
                row["error"] = ""

            self._refresh_overall_progress(None)
            self._status_var.set(
                f"Downloading {self._finished_tasks} of {self._total_tasks}…"
            )
            self._refresh_buttons()
        elif kind == "error":
            self._finished_tasks += 1
            row = self._rows.get(row_id)
            if row is not None:
                row["error"] = payload.get("message", "")

            self._update_row(
                row_id,
                status="Failed",
                progress="—",
                tag="error",
                state=STATE_FAILED,
            )
            self._refresh_overall_progress(None)
            self._status_var.set(
                f"Downloading {self._finished_tasks} of {self._total_tasks}…"
            )
            self._refresh_buttons()
        elif kind == "cancelled":
            self._finished_tasks += 1
            self._update_row(
                row_id,
                status="Cancelled",
                progress="—",
                tag="cancelled",
                state=STATE_CANCELLED,
            )
            self._refresh_overall_progress(None)
            self._refresh_buttons()
        elif kind == "batch_done":
            self._finish_run(payload)

    def _refresh_overall_progress(self, current_percent: float | None) -> None:
        total = max(self._total_tasks, 1)
        done = float(self._finished_tasks)
        if current_percent is not None and self._finished_tasks < total:
            done += max(0.0, min(float(current_percent), 100.0)) / 100.0
        self._progress["value"] = max(0.0, min(100.0, done / total * 100.0))

    def _finish_run(self, payload: dict) -> None:
        completed = payload.get("completed", 0)
        failed = payload.get("failed", 0)
        cancelled = payload.get("cancelled", 0)

        parts = [f"{completed} downloaded"]
        if failed:
            parts.append(f"{failed} failed")
        if cancelled:
            parts.append(f"{cancelled} cancelled")

        self._status_var.set("Finished — " + ", ".join(parts) + ".")
        self._set_busy(False)

        if not failed and not cancelled:
            self._progress["value"] = 100

        if self._open_when_done.get():
            self._open_output_folder()

        if failed:
            messagebox.showwarning(
                "Some downloads failed",
                "Press \"Retry failed\" to try them again, or double-click a failed "
                "row (or right-click → Show details) to see why.",
            )

    # ------------------------------------------------------------------ rows

    def _add_row(self, url: str) -> str:
        self._row_seq += 1
        row_id = f"row{self._row_seq}"
        title = url if len(url) <= 100 else url[:97] + "…"

        self._tree.insert(
            "",
            "end",
            iid=row_id,
            values=(title, "—", "Queued", "—", "—"),
        )
        self._rows[row_id] = {
            "url": url,
            "title": title,
            "platform": "—",
            "status": "Queued",
            "state": STATE_QUEUED,
            "path": "",
            "files": [],
            "error": "",
        }
        self._tree.see(row_id)
        return row_id

    def _update_row(
        self,
        row_id: str | None,
        *,
        status: str | None = None,
        title: str | None = None,
        platform: str | None = None,
        size: str | None = None,
        progress: str | None = None,
        tag: str | None = None,
        state: str | None = None,
    ) -> None:
        if not row_id or row_id not in self._rows:
            return

        row = self._rows[row_id]
        values = list(self._tree.item(row_id, "values"))

        if title:
            row["title"] = title
            values[0] = title
        if platform:
            row["platform"] = platform
            values[1] = platform
        if status is not None:
            row["status"] = status
            values[2] = status
        if size is not None:
            values[3] = size
        if progress is not None:
            values[4] = progress
        if state is not None:
            row["state"] = state

        self._tree.item(row_id, values=values)
        if tag:
            self._tree.item(row_id, tags=(tag,))

    def _selected_row(self) -> str | None:
        selection = self._tree.selection()
        return selection[0] if selection else None

    # ----------------------------------------------------------- row actions

    def _on_double_click(self, event: tk.Event) -> None:
        row_id = self._tree.identify_row(event.y)
        if not row_id or row_id not in self._rows:
            return

        row = self._rows[row_id]
        if row["status"] != "Done":
            self._show_details(row_id)
            return

        files = [item for item in row.get("files") or [] if Path(item).exists()]
        if len(files) > 1:
            # Slideshows save several files; show them together instead.
            open_path(Path(files[0]).parent)
        elif files:
            open_path(Path(files[0]))
        elif row["path"] and Path(row["path"]).exists():
            open_path(Path(row["path"]))
        else:
            self._show_details(row_id)

    def _on_right_click(self, event: tk.Event) -> None:
        row_id = self._tree.identify_row(event.y)
        if not row_id:
            return

        self._tree.selection_set(row_id)
        self._menu_row = row_id
        try:
            self._menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._menu.grab_release()

    def _open_selected_file(self) -> None:
        row = self._rows.get(self._selected_row() or "")
        if row and row["path"] and Path(row["path"]).exists():
            open_path(Path(row["path"]))
        elif row:
            messagebox.showinfo("Not ready", "This file hasn't been downloaded yet.")

    def _show_selected_in_folder(self) -> None:
        row = self._rows.get(self._selected_row() or "")
        if not row or not row["path"]:
            return

        path = Path(row["path"])
        if not path.exists():
            self._open_output_folder()
            return

        if sys.platform == "win32":
            subprocess.Popen(["explorer", f"/select,{os.path.normpath(path)}"])
        else:
            open_path(path.parent)

    def _copy_selected_link(self) -> None:
        row = self._rows.get(self._selected_row() or "")
        if not row:
            return
        self.clipboard_clear()
        self.clipboard_append(row["url"])
        self._status_var.set("Link copied to clipboard.")

    def _show_selected_details(self) -> None:
        row_id = self._selected_row()
        if row_id:
            self._show_details(row_id)

    def _show_details(self, row_id: str) -> None:
        row = self._rows.get(row_id)
        if not row:
            return

        lines = [f"Status: {row['status']}", f"Link: {row['url']}"]
        if row["title"]:
            lines.insert(0, row["title"])
        if row["platform"] and row["platform"] != "—":
            lines.append(f"Site: {row['platform']}")

        files = [item for item in row.get("files") or [] if item]
        if len(files) > 1:
            lines.append(f"Files ({len(files)}):")
            lines.extend(f"  · {Path(item).name}" for item in files[:10])
            if len(files) > 10:
                lines.append(f"  …and {len(files) - 10} more")
        elif row["path"]:
            lines.append(f"Saved to: {row['path']}")

        if row["error"]:
            messagebox.showerror(
                "Download failed", "\n".join(lines) + "\n\n" + row["error"]
            )
        else:
            messagebox.showinfo("Download details", "\n".join(lines))

    def _remove_selected_row(self) -> None:
        row_id = self._selected_row()
        row = self._rows.get(row_id) if row_id else None
        if not row or row_id is None:
            return

        if row["state"] not in TERMINAL_STATES and self._is_running():
            messagebox.showinfo(
                "Download in progress",
                "Wait for this download to finish, or press Cancel first.",
            )
            return

        self._tree.delete(row_id)
        self._rows.pop(row_id, None)
        self._refresh_buttons()

    def _clear_completed(self) -> None:
        """Remove successfully downloaded rows; files stay on disk."""
        cleared = 0
        for row_id, row in list(self._rows.items()):
            if row["state"] == STATE_DONE:
                self._tree.delete(row_id)
                self._rows.pop(row_id, None)
                cleared += 1

        if cleared:
            self._status_var.set(
                f"Cleared {cleared} completed "
                f"download{'s' if cleared != 1 else ''} from the list "
                "(files are still on disk)."
            )
        self._refresh_buttons()

    # -------------------------------------------------------------- misc UI

    def _open_output_folder(self) -> None:
        path = Path(
            self._output_var.get().strip() or str(default_output_dir())
        ).expanduser()
        try:
            path.mkdir(parents=True, exist_ok=True)
            open_path(path)
        except OSError as exc:
            messagebox.showerror("Cannot open folder", str(exc))

    def _on_close(self) -> None:
        if self._worker and self._worker.is_alive():
            if not messagebox.askyesno(
                "Quit DownloadIt?",
                "A download is still running. Cancel it and quit?",
            ):
                return
            self._cancel_event.set()
            self._worker.join(timeout=2.0)

        self.destroy()
