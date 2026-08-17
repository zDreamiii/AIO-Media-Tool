from __future__ import annotations

import os
import sys
import tempfile
import traceback
from datetime import datetime
from pathlib import Path


def _startup_log_path() -> Path:
    try:
        if sys.platform == "win32":
            root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
            folder = root / "AIO Media Tool" / "logs"
        else:
            folder = Path(tempfile.gettempdir()) / "aio-media-tool"
        folder.mkdir(parents=True, exist_ok=True)
        return folder / "startup_error.log"
    except OSError:
        return Path(tempfile.gettempdir()) / "aio-media-tool-startup-error.log"


def _report_startup_error() -> None:
    details = traceback.format_exc()
    log_path = _startup_log_path()
    try:
        log_path.write_text(
            f"AIO Media Tool startup error - {datetime.now().isoformat()}\n\n{details}",
            encoding="utf-8",
        )
    except OSError:
        pass

    message = (
        "AIO Media Tool konnte nicht gestartet werden.\n\n"
        f"Fehlerdetails wurden gespeichert unter:\n{log_path}\n\n"
        "Schick diese Datei mit, wenn du den Fehler meldest."
    )
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, message, "AIO Media Tool - Startfehler", 0x10)
            return
        except Exception:
            pass
    print(message, file=sys.stderr)
    print(details, file=sys.stderr)


def run() -> int:
    try:
        from aio_media_tool.main import main

        return int(main())
    except SystemExit:
        raise
    except BaseException:
        _report_startup_error()
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
