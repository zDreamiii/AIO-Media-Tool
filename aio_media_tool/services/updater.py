from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aio_media_tool.models import AppSettings
from aio_media_tool.paths import project_root
from aio_media_tool.runtime import is_frozen
from aio_media_tool.services.common import run_command


@dataclass(slots=True)
class UpdateReport:
    checked: bool = False
    repository: bool = False
    clean_repository: bool = True
    code_update: bool = False
    package_update: bool = False
    local_commit: str = ""
    remote_commit: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def update_available(self) -> bool:
        return self.code_update or self.package_update

    @property
    def summary(self) -> str:
        if not self.checked:
            return "Updateprüfung nicht ausgeführt"
        if not self.repository and not self.package_update:
            return "Kein Git-Repository erkannt"
        if not self.clean_repository:
            return "Lokale Änderungen: automatisches Update pausiert"
        if self.code_update and self.package_update:
            return "Code- und Paketupdates verfügbar"
        if self.code_update:
            return "Codeupdate verfügbar"
        if self.package_update:
            return "Paketupdates verfügbar"
        return "Alles aktuell"


class UpdaterService:
    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or project_root()).resolve()

    @staticmethod
    def is_due(settings: AppSettings) -> bool:
        if is_frozen() or settings.update_mode == "off":
            return False
        if not settings.last_update_check:
            return True
        try:
            checked = datetime.fromisoformat(settings.last_update_check)
            if checked.tzinfo is None:
                checked = checked.replace(tzinfo=UTC)
        except ValueError:
            return True
        return datetime.now(UTC) - checked >= timedelta(
            hours=max(1, settings.update_interval_hours)
        )

    @staticmethod
    def split_remote_ref(remote_ref: str) -> tuple[str, str]:
        value = remote_ref.strip().strip("/")
        if "/" not in value:
            raise ValueError("Remote-Branch bitte als `origin/main` angeben.")
        remote, branch = value.split("/", 1)
        if not remote or not branch or any(char.isspace() for char in value):
            raise ValueError("Ungültiger Remote-Branch.")
        return remote, branch

    def _venv_python(self) -> Path:
        windows = self.root / ".venv" / "Scripts" / "python.exe"
        return windows if windows.exists() else self.root / ".venv" / "bin" / "python"

    def check(self, remote_ref: str, include_packages: bool = False) -> UpdateReport:
        report = UpdateReport(checked=True)
        if is_frozen():
            report.notes.append("Der Git-Updater ist in der EXE-Version deaktiviert.")
            return report
        git = shutil.which("git")
        if git and (self.root / ".git").exists():
            report.repository = True
            status = run_command([git, "status", "--porcelain"], cwd=self.root, timeout=15)
            report.clean_repository = status.returncode == 0 and not status.stdout.strip()
            try:
                remote, branch = self.split_remote_ref(remote_ref)
                fetched = run_command(
                    [git, "fetch", "--quiet", remote, branch], cwd=self.root, timeout=90
                )
                if fetched.returncode:
                    report.notes.append(
                        f"Git-Fetch fehlgeschlagen: {fetched.stderr.strip()[-300:]}"
                    )
                else:
                    local = run_command([git, "rev-parse", "HEAD"], cwd=self.root, timeout=10)
                    remote_commit = run_command(
                        [git, "rev-parse", f"{remote}/{branch}"], cwd=self.root, timeout=10
                    )
                    if local.returncode == 0 and remote_commit.returncode == 0:
                        report.local_commit = local.stdout.strip()
                        report.remote_commit = remote_commit.stdout.strip()
                        if report.local_commit != report.remote_commit:
                            ancestor = run_command(
                                [
                                    git,
                                    "merge-base",
                                    "--is-ancestor",
                                    report.local_commit,
                                    report.remote_commit,
                                ],
                                cwd=self.root,
                                timeout=10,
                            )
                            report.code_update = ancestor.returncode == 0
                            if ancestor.returncode != 0:
                                report.notes.append(
                                    "Remote-Stand ist kein Fast-Forward; manuelle Prüfung nötig."
                                )
            except (ValueError, subprocess.SubprocessError) as exc:
                report.notes.append(str(exc))
        elif not git:
            report.notes.append("Git wurde nicht gefunden.")

        if include_packages:
            uv = shutil.which("uv")
            if not uv:
                report.notes.append("uv wurde nicht gefunden; Paketprüfung übersprungen.")
            elif report.repository and not (self.root / "uv.lock").exists():
                report.notes.append(
                    "uv.lock fehlt; bitte einmal erzeugen und im privaten Repository speichern."
                )
            else:
                try:
                    venv_python = self._venv_python()
                    if not venv_python.exists():
                        report.notes.append(
                            "Virtuelle Umgebung fehlt; zuerst `uv sync --extra dev` ausführen."
                        )
                    else:
                        result = run_command(
                            [
                                uv,
                                "pip",
                                "list",
                                "--outdated",
                                "--format",
                                "json",
                                "--python",
                                str(venv_python),
                            ],
                            cwd=self.root,
                            timeout=180,
                        )
                        if result.returncode == 0:
                            outdated = json.loads(result.stdout or "[]")
                            report.package_update = bool(outdated)
                        else:
                            report.notes.append(
                                "Paketprüfung fehlgeschlagen; die App läuft unverändert weiter."
                            )
                except (OSError, subprocess.SubprocessError):
                    report.notes.append("Paketprüfung hat das Zeitlimit überschritten.")
                except json.JSONDecodeError:
                    report.notes.append("Paketprüfung lieferte eine unerwartete Antwort.")
        return report

    def launch_update(self, remote_ref: str, update_packages: bool) -> None:
        if is_frozen():
            raise RuntimeError(
                "Der Git-Updater ist nur in einem Source-Checkout verfügbar. "
                "Für die EXE-Version bitte eine neue Release-Datei herunterladen."
            )
        script = self.root / "scripts" / "update_and_rebuild.py"
        if not script.exists():
            raise FileNotFoundError("Update-Skript wurde nicht gefunden.")
        command = [
            sys.executable,
            str(script),
            "--project",
            str(self.root),
            "--remote-ref",
            remote_ref,
        ]
        if update_packages:
            command.append("--packages")
        kwargs: dict = {
            "cwd": self.root,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
            )
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen(command, **kwargs)
