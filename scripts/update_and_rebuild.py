from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


class UpdateFailure(RuntimeError):
    pass


def append_log(path: Path, message: str) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(f"[{datetime.now().isoformat(timespec='seconds')}] {message}\n")


def run(
    command: list[str], root: Path, log: Path, check: bool = True
) -> subprocess.CompletedProcess[str]:
    append_log(log, "$ " + " ".join(command))
    result = subprocess.run(
        command,
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.stdout:
        append_log(log, result.stdout[-4000:])
    if result.stderr:
        append_log(log, result.stderr[-4000:])
    if check and result.returncode:
        raise UpdateFailure(f"Befehl fehlgeschlagen ({result.returncode}): {command[0]}")
    return result


def relaunch(root: Path) -> None:
    executable = root / "dist" / (
        "AIO-Media-Tool.exe" if sys.platform == "win32" else "AIO-Media-Tool"
    )
    if executable.exists():
        subprocess.Popen([str(executable)], cwd=root)
        return
    venv_python = (
        root / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    )
    if venv_python.exists():
        subprocess.Popen([str(venv_python), "-m", "aio_media_tool"], cwd=root)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="AIO Media Tool sicher aktualisieren und neu bauen"
    )
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--remote-ref", default="origin/main")
    parser.add_argument("--packages", action="store_true")
    args = parser.parse_args()
    root = args.project.resolve()
    if not (root / "pyproject.toml").is_file():
        raise SystemExit("Ungültiges Projektverzeichnis")
    log = root / ".aio-update.log"
    time.sleep(2.5)
    git = shutil.which("git")
    uv = shutil.which("uv")
    if not uv:
        append_log(log, "Abbruch: uv wurde nicht gefunden.")
        return 2
    old_commit = ""
    changed_commit = False
    try:
        if git and (root / ".git").exists():
            status = run([git, "status", "--porcelain"], root, log)
            if status.stdout.strip():
                raise UpdateFailure("Repository enthält lokale Änderungen; nichts wurde verändert.")
            old_commit = run([git, "rev-parse", "HEAD"], root, log).stdout.strip()
            if "/" not in args.remote_ref:
                raise UpdateFailure("Remote-Branch muss wie origin/main angegeben werden.")
            remote, branch = args.remote_ref.split("/", 1)
            run([git, "fetch", remote, branch], root, log)
            run([git, "merge", "--ff-only", f"{remote}/{branch}"], root, log)
            new_commit = run([git, "rev-parse", "HEAD"], root, log).stdout.strip()
            changed_commit = new_commit != old_commit
        sync_command = [
            uv,
            "sync",
            "--extra",
            "dev",
            "--extra",
            "transcription",
            "--extra",
            "ocr",
            "--no-progress",
        ]
        if (root / "uv.lock").exists():
            sync_command.append("--locked")
        run(sync_command, root, log)
        venv_python = (
            root / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        )
        if not venv_python.exists():
            raise UpdateFailure("Die virtuelle Python-Umgebung wurde nicht erstellt.")
        if args.packages:
            run(
                [
                    uv,
                    "pip",
                    "install",
                    "--python",
                    str(venv_python),
                    "--upgrade",
                    ".[dev,transcription,ocr]",
                ],
                root,
                log,
            )
        run([str(venv_python), "-m", "pytest"], root, log)
        run([str(venv_python), "scripts/build.py"], root, log)
        append_log(log, "Update und Build erfolgreich.")
        relaunch(root)
        return 0
    except (OSError, UpdateFailure) as exc:
        append_log(log, f"FEHLER: {exc}")
        if changed_commit and old_commit and git:
            append_log(log, f"Rollback auf {old_commit[:12]}")
            run([git, "reset", "--hard", old_commit], root, log, check=False)
            sync_command = [
                uv,
                "sync",
                "--extra",
                "dev",
                "--extra",
                "transcription",
                "--extra",
                "ocr",
                "--no-progress",
            ]
            if (root / "uv.lock").exists():
                sync_command.append("--locked")
            run(sync_command, root, log, check=False)
        relaunch(root)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
