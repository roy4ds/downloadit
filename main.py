"""DownloadIt — a desktop video downloader for TikTok and 1000+ other sites.

Run with:
    python main.py
"""

from __future__ import annotations

import sys


def _enable_high_dpi() -> None:
    """Make Tk text crisp on high-DPI Windows displays."""
    if sys.platform != "win32":
        return
    try:
        from ctypes import windll

        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


def _show_startup_error(title: str, message: str) -> None:
    """Best-effort message box before the main window exists."""
    print(f"{title}\n\n{message}", file=sys.stderr)
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(title, message)
        root.destroy()
    except Exception:
        pass


def main() -> int:
    _enable_high_dpi()

    try:
        from gui import DownloadItApp
    except ModuleNotFoundError as exc:
        if exc.name == "tkinter":
            _show_startup_error(
                "Tkinter is missing",
                "This Python build does not include Tkinter.\n\n"
                "Install the official Python from python.org (Tkinter is included).",
            )
            return 1

        if exc.name in {"yt_dlp", "downloader"}:
            _show_startup_error(
                "Missing dependency",
                "DownloadIt needs yt-dlp.\n\n"
                "Install the requirements with:\n\n"
                f'    "{sys.executable}" -m pip install -r requirements.txt',
            )
            return 1

        raise

    app = DownloadItApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
