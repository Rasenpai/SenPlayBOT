"""Native desktop dialogs (local web UI / CLI)."""

from __future__ import annotations


def pick_directory(
    title: str = "Choose folder to save images",
    initial: str | None = None,
) -> str | None:
    """Open a native folder picker. Returns absolute path or None if cancelled."""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "Folder picker needs tkinter. Install a Python build with Tcl/Tk support."
        ) from exc

    root = tk.Tk()
    root.withdraw()
    try:
        # Keep dialog above the browser on Windows
        root.attributes("-topmost", True)
        root.update()
        chosen = filedialog.askdirectory(
            parent=root,
            title=title,
            mustexist=True,
            initialdir=initial or None,
        )
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass

    if not chosen:
        return None
    from pathlib import Path

    return str(Path(chosen).expanduser().resolve())
