from __future__ import annotations

import ipaddress
import math
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from threading import Event
from typing import NamedTuple
from urllib.parse import urlparse

from aio_media_tool.models import JobCancelled

ProgressCallback = Callable[[int, str], None]


def noop_progress(_value: int, _message: str) -> None:
    return None


def check_cancelled(cancel: Event | None) -> None:
    if cancel and cancel.is_set():
        raise JobCancelled("Vorgang abgebrochen")


def require_executable(name: str) -> str:
    executable = shutil.which(name)
    if not executable:
        raise RuntimeError(
            f"{name} wurde nicht gefunden. Bitte installieren und zum PATH hinzufügen."
        )
    return executable


def unique_output(path: Path) -> Path:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        return path
    for number in range(2, 10_000):
        candidate = path.with_name(f"{path.stem} ({number}){path.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Kein freier Dateiname für {path.name} gefunden")


def atomic_write_bytes(output: Path, data: bytes) -> Path:
    output = unique_output(output)
    handle, temp_name = tempfile.mkstemp(
        prefix=f".{output.stem}-", suffix=output.suffix, dir=output.parent
    )
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, output)
    finally:
        Path(temp_name).unlink(missing_ok=True)
    return output


def validate_public_media_url(value: str) -> str:
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Bitte eine vollständige HTTP- oder HTTPS-Adresse eingeben.")
    if parsed.username or parsed.password:
        raise ValueError("Adressen mit eingebetteten Zugangsdaten werden nicht akzeptiert.")
    host = parsed.hostname.casefold().rstrip(".")
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise ValueError("Lokale Netzwerkadressen werden nicht verarbeitet.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address and not address.is_global:
        raise ValueError("Private oder lokale IP-Adressen werden nicht verarbeitet.")
    return value.strip()


def run_command(
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args),
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        shell=False,
    )



class CpuSetEntry(NamedTuple):
    id: int
    group: int
    logical_processor_index: int
    core_index: int
    efficiency_class: int
    flags: int


def cpu_limit_core_count(cpu_percent: int | float, logical_cpus: int | None = None) -> int:
    """Translate a requested CPU percentage into a conservative logical-core count."""
    logical = max(1, int(logical_cpus or os.cpu_count() or 1))
    percent = max(1.0, min(100.0, float(cpu_percent)))
    if percent >= 100.0:
        return logical
    return max(1, min(logical, math.floor(logical * percent / 100.0)))


def select_efficiency_cpu_set_ids(
    cpu_sets: Sequence[CpuSetEntry], cpu_percent: int | float = 100
) -> list[int]:
    """Select the most energy-efficient CPU class from a heterogeneous topology.

    Windows documents lower ``EfficiencyClass`` values as more energy efficient and
    higher values as faster. A single class means that no P/E split can be identified.
    CPU sets reserved exclusively for another process are excluded.
    """
    available = [entry for entry in cpu_sets if not (entry.flags & 0b10)]
    classes = sorted({entry.efficiency_class for entry in available})
    if len(classes) < 2:
        return []
    efficient_class = classes[0]
    efficient = sorted(
        (entry for entry in available if entry.efficiency_class == efficient_class),
        key=lambda entry: (entry.group, entry.logical_processor_index, entry.id),
    )
    if not efficient:
        return []
    count = cpu_limit_core_count(cpu_percent, len(efficient))
    return [entry.id for entry in efficient[:count]]


def _windows_system_cpu_sets() -> list[CpuSetEntry]:
    if os.name != "nt":
        return []
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_info = kernel32.GetSystemCpuSetInformation
    get_info.argtypes = [
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.c_void_p,
        ctypes.c_ulong,
    ]
    get_info.restype = ctypes.c_int

    required = ctypes.c_ulong()
    get_info(None, 0, ctypes.byref(required), None, 0)
    if required.value == 0:
        return []
    buffer = ctypes.create_string_buffer(required.value)
    returned = ctypes.c_ulong()
    if not get_info(buffer, required.value, ctypes.byref(returned), None, 0):
        return []

    raw = memoryview(buffer.raw)[: returned.value]
    entries: list[CpuSetEntry] = []
    offset = 0
    while offset + 8 <= len(raw):
        size = int.from_bytes(raw[offset : offset + 4], "little")
        info_type = int.from_bytes(raw[offset + 4 : offset + 8], "little")
        if size < 8 or offset + size > len(raw):
            break
        # CpuSetInformation == 0. The fields used below have been stable since
        # SYSTEM_CPU_SET_INFORMATION was introduced and fit in the first 20 bytes.
        if info_type == 0 and size >= 20:
            entries.append(
                CpuSetEntry(
                    id=int.from_bytes(raw[offset + 8 : offset + 12], "little"),
                    group=int.from_bytes(raw[offset + 12 : offset + 14], "little"),
                    logical_processor_index=int(raw[offset + 14]),
                    core_index=int(raw[offset + 15]),
                    efficiency_class=int(raw[offset + 18]),
                    flags=int(raw[offset + 19]),
                )
            )
        offset += size
    return entries


def windows_efficiency_cpu_set_ids(cpu_percent: int | float = 100) -> list[int]:
    """Return Windows CPU Set IDs belonging to the lowest efficiency class."""
    return select_efficiency_cpu_set_ids(_windows_system_cpu_sets(), cpu_percent)


def _apply_windows_efficiency_cpu_sets(
    process: subprocess.Popen[str], cpu_percent: int | float
) -> None:
    import ctypes

    ids = windows_efficiency_cpu_set_ids(cpu_percent)
    if not ids:
        raise RuntimeError(
            "Der E-Core-Modus ist aktiviert, aber Windows konnte keine getrennten "
            "P-/E-Core-Effizienzklassen erkennen. Bitte 'Alle Kerne' verwenden."
        )
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    set_cpu_sets = kernel32.SetProcessDefaultCpuSets
    set_cpu_sets.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong), ctypes.c_ulong]
    set_cpu_sets.restype = ctypes.c_int
    handle = ctypes.c_void_p(int(process._handle))  # type: ignore[attr-defined]
    array_type = ctypes.c_ulong * len(ids)
    cpu_ids = array_type(*ids)
    if not set_cpu_sets(handle, cpu_ids, len(ids)):
        raise OSError(ctypes.get_last_error(), "SetProcessDefaultCpuSets fehlgeschlagen")

    # Background mode should yield to foreground work even on the selected E-cores.
    below_normal_priority_class = 0x00004000
    kernel32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel32.SetPriorityClass.restype = ctypes.c_int
    kernel32.SetPriorityClass(handle, below_normal_priority_class)


def _apply_process_cpu_limit(
    process: subprocess.Popen[str],
    cpu_percent: int | float,
    cpu_mode: str = "all",
) -> None:
    """Apply the requested FFmpeg CPU policy.

    ``all`` uses process affinity as a coarse percentage limiter. ``e_cores`` uses
    Windows CPU Sets and the processor EfficiencyClass to constrain FFmpeg to the
    most energy-efficient core class, which is appropriate for hybrid Intel/AMD/ARM
    systems when background responsiveness matters.
    """
    percent = max(1.0, min(100.0, float(cpu_percent)))
    mode = str(cpu_mode or "all").casefold()
    if mode == "e_cores":
        if os.name != "nt":
            raise RuntimeError("Der E-Core-Modus wird derzeit nur unter Windows unterstützt.")
        _apply_windows_efficiency_cpu_sets(process, percent)
        return
    if mode != "all":
        raise ValueError(f"Unbekannter CPU-Modus: {cpu_mode}")
    if percent >= 100.0:
        return
    try:
        if os.name == "nt":
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = ctypes.c_void_p(int(process._handle))  # type: ignore[attr-defined]
            process_mask = ctypes.c_size_t()
            system_mask = ctypes.c_size_t()
            kernel32.GetProcessAffinityMask.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_size_t),
                ctypes.POINTER(ctypes.c_size_t),
            ]
            kernel32.GetProcessAffinityMask.restype = ctypes.c_int
            kernel32.SetProcessAffinityMask.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
            kernel32.SetProcessAffinityMask.restype = ctypes.c_int
            if not kernel32.GetProcessAffinityMask(
                handle, ctypes.byref(process_mask), ctypes.byref(system_mask)
            ):
                return
            allowed_bits = [
                bit
                for bit in range(ctypes.sizeof(ctypes.c_size_t) * 8)
                if process_mask.value & (1 << bit)
            ]
            if not allowed_bits:
                return
            cores = cpu_limit_core_count(percent, len(allowed_bits))
            limited_mask = sum(1 << bit for bit in allowed_bits[:cores])
            kernel32.SetProcessAffinityMask(handle, ctypes.c_size_t(limited_mask))
        elif hasattr(os, "sched_setaffinity") and hasattr(os, "sched_getaffinity"):
            allowed = sorted(os.sched_getaffinity(0))
            if not allowed:
                return
            cores = cpu_limit_core_count(percent, len(allowed))
            os.sched_setaffinity(process.pid, set(allowed[:cores]))
    except (OSError, ValueError, AttributeError):
        pass


def _apply_process_background_priority(process: subprocess.Popen[str]) -> None:
    """Lower FFmpeg priority without changing its CPU affinity."""
    try:
        if os.name == "nt":
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = ctypes.c_void_p(int(process._handle))  # type: ignore[attr-defined]
            below_normal_priority_class = 0x00004000
            kernel32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
            kernel32.SetPriorityClass.restype = ctypes.c_int
            kernel32.SetPriorityClass(handle, below_normal_priority_class)
        elif hasattr(os, "setpriority") and hasattr(os, "PRIO_PROCESS"):
            os.setpriority(os.PRIO_PROCESS, process.pid, 10)
    except (OSError, ValueError, AttributeError):
        pass


def run_ffmpeg(
    args: Sequence[str],
    *,
    duration_seconds: float | None,
    progress: ProgressCallback = noop_progress,
    cancel: Event | None = None,
    cpu_limit_percent: int | float = 100,
    cpu_mode: str = "all",
    background_priority: bool = False,
) -> None:
    ffmpeg = require_executable("ffmpeg")
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-progress",
        "pipe:1",
        "-nostats",
        *args,
    ]
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
    )
    try:
        _apply_process_cpu_limit(process, cpu_limit_percent, cpu_mode)
        if background_priority:
            _apply_process_background_priority(process)
    except Exception:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
        raise
    assert process.stdout is not None
    try:
        for line in process.stdout:
            check_cancelled(cancel)
            key, _, value = line.strip().partition("=")
            if key in {"out_time_us", "out_time_ms"} and duration_seconds:
                try:
                    microseconds = int(value)
                    percent = min(
                        99, max(0, int((microseconds / 1_000_000) / duration_seconds * 100))
                    )
                    progress(percent, "FFmpeg verarbeitet Medien")
                except ValueError:
                    pass
        return_code = process.wait()
        if return_code:
            error = (process.stderr.read() if process.stderr else "").strip()
            raise RuntimeError(error[-1800:] or f"FFmpeg wurde mit Code {return_code} beendet.")
    except JobCancelled:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
        raise
