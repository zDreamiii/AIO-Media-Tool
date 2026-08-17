from __future__ import annotations

import os
import subprocess
import sys
from importlib.util import find_spec
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    entry = root / "aio_media_tool" / "__main__.py"
    resources = root / "aio_media_tool" / "resources"
    icon = resources / "app_icon.ico"
    version_file = root / "scripts" / "windows_version_info.txt"

    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--windowed",
        "--onefile",
        "--name",
        "AIO-Media-Tool",
        "--collect-all",
        "yt_dlp",
        "--hidden-import",
        "mutagen.id3",
        "--add-data",
        f"{resources}{os.pathsep}aio_media_tool/resources",
    ]
    if sys.platform == "win32":
        command.extend(["--icon", str(icon), "--version-file", str(version_file)])

    # Optional features are collected when they are installed in the build environment.
    # The default Windows build installs transcription + OCR, matching the source setup.
    for package in ("cryptography", "docx", "faster_whisper", "ctranslate2", "pymupdf"):
        if find_spec(package) is not None:
            command.extend(["--collect-all", package])

    command.extend(["--paths", str(root), str(entry)])
    completed = subprocess.run(command, cwd=root, check=False)
    if completed.returncode:
        return completed.returncode

    executable = root / "dist" / ("AIO-Media-Tool.exe" if sys.platform == "win32" else "AIO-Media-Tool")
    if not executable.exists():
        print(f"Build abgeschlossen, aber Datei nicht gefunden: {executable}", file=sys.stderr)
        return 2

    print(f"Fertig: {executable}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
