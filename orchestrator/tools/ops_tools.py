"""Ops & self-awareness tools: machine resources, background jobs, file moves.

These give the agent eyes on the machine it runs on. Most of the failures we saw
came from the agent flying blind — starting an 18 GB download without checking
free disk, or leaving a runaway job it couldn't see or stop. All stdlib + the
``nvidia-smi`` CLI; no extra dependency (local-first).
"""

from __future__ import annotations

import asyncio
import ctypes
import shutil
import subprocess
import time
from pathlib import Path

from orchestrator.tools.registry import registry


# --- system_resources -------------------------------------------------------
def _gpu_lines() -> list[str]:
    """Per-GPU 'name: used/total MiB (util%)' via nvidia-smi, or a note."""
    try:
        res = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.used,memory.total,utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ["GPU: nvidia-smi not available"]
    if res.returncode != 0:
        return ["GPU: nvidia-smi error"]
    out: list[str] = []
    for line in res.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 4:
            name, used, total, util = parts[:4]
            try:
                free = int(total) - int(used)
                out.append(
                    f"GPU {name}: {used}/{total} MiB used "
                    f"({free} MiB free, {util}% util)"
                )
            except ValueError:
                out.append(f"GPU {name}: {used}/{total} MiB")
    return out or ["GPU: none detected"]


def _ram_line() -> str:
    """Total/available RAM via the Win32 API (no psutil dependency)."""
    if not hasattr(ctypes, "windll"):
        return "RAM: (unavailable on this OS)"

    class _MemStatus(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    m = _MemStatus()
    m.dwLength = ctypes.sizeof(_MemStatus)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
        return "RAM: (query failed)"
    total = m.ullTotalPhys / 1e9
    avail = m.ullAvailPhys / 1e9
    return f"RAM: {total - avail:.1f}/{total:.1f} GB used ({avail:.1f} GB free)"


def _disk_lines() -> list[str]:
    """Free/total space for each fixed drive that exists."""
    out: list[str] = []
    for letter in "CDEFG":
        root = f"{letter}:\\"
        if Path(root).exists():
            try:
                u = shutil.disk_usage(root)
                out.append(
                    f"Disk {letter}: {u.used / 1e9:.0f}/{u.total / 1e9:.0f} GB used "
                    f"({u.free / 1e9:.0f} GB free)"
                )
            except OSError:
                pass
    return out or ["Disk: (none found)"]


@registry.register
async def system_resources() -> str:
    """Report GPU VRAM, system RAM, and disk free space on this machine.

    Call this BEFORE heavy actions — downloading a large model (is there disk
    space?), loading a big model (is there VRAM?), or starting a render — so you
    don't run out of memory or disk mid-operation.
    """
    lines = await asyncio.to_thread(
        lambda: [*_gpu_lines(), _ram_line(), *_disk_lines()]
    )
    return "\n".join(lines)


# --- manage_jobs ------------------------------------------------------------
@registry.register
async def manage_jobs(action: str = "list", job_id: str = "") -> str:
    """List or cancel the agent's background jobs (downloads + ComfyUI renders).

    Args:
        action: 'list' (default) to show all jobs with progress, or 'cancel' to
            stop a running download by id.
        job_id: required when action='cancel' — the id to cancel.
    """
    # Lazy imports avoid a circular import at module load time.
    from orchestrator.tools import model_tools

    try:
        from orchestrator.tools import media_tools

        comfy_jobs = media_tools._JOBS
    except Exception:  # noqa: BLE001
        comfy_jobs = {}

    action = (action or "list").strip().lower()

    if action == "cancel":
        jid = job_id.strip()
        job = model_tools.DOWNLOADS.get(jid)
        if job is None:
            if jid in comfy_jobs:
                return (
                    f"Job {jid} is a ComfyUI render; those can't be cancelled "
                    "mid-run (it will finish or time out in ComfyUI)."
                )
            return f"Error: no cancellable download with id {jid!r}."
        if job.status != "running" or job.proc is None:
            return f"Job {jid} is not running (status={job.status})."
        try:
            job.proc.terminate()
            job.status = "cancelled"
            job.finished = time.time()
        except Exception as exc:  # noqa: BLE001
            return f"Error cancelling {jid}: {type(exc).__name__}: {exc}"
        return f"Cancelled download {jid} ({job.filename})."

    # action == list
    out: list[str] = []
    for job in model_tools.DOWNLOADS.values():
        out.append("⬇  " + model_tools.progress_line(job))
    for jid, cj in comfy_jobs.items():
        out.append(f"🎨 [{jid}] {cj.status} · {cj.prompt_text[:40]}")
    if not out:
        return "No background jobs."
    return "Background jobs:\n" + "\n".join(out)


# --- file operations --------------------------------------------------------
def _guard(path: Path) -> str | None:
    """Reject obviously dangerous targets (drive roots, empty). Returns an error
    string if unsafe, else None."""
    s = str(path)
    if len(path.parts) <= 1 or s.rstrip("\\/").endswith(":"):
        return f"Error: refusing to operate on a drive root or empty path: {s!r}"
    return None


@registry.register
async def move_path(source: str, destination: str) -> str:
    """Move or rename a file or folder.

    Args:
        source: Existing file/folder path.
        destination: New path (or target folder).
    """
    src, dst = Path(source.strip()), Path(destination.strip())
    if not src.exists():
        return f"Error: source not found: {src}"
    if (g := _guard(src)) :
        return g
    try:
        out = await asyncio.to_thread(shutil.move, str(src), str(dst))
        return f"Moved {src} -> {out}"
    except (OSError, shutil.Error) as exc:
        return f"Error moving: {type(exc).__name__}: {exc}"


@registry.register
async def copy_path(source: str, destination: str) -> str:
    """Copy a file or folder.

    Args:
        source: Existing file/folder path.
        destination: Destination path.
    """
    src, dst = Path(source.strip()), Path(destination.strip())
    if not src.exists():
        return f"Error: source not found: {src}"
    try:
        if src.is_dir():
            await asyncio.to_thread(shutil.copytree, str(src), str(dst))
        else:
            await asyncio.to_thread(shutil.copy2, str(src), str(dst))
        return f"Copied {src} -> {dst}"
    except (OSError, shutil.Error) as exc:
        return f"Error copying: {type(exc).__name__}: {exc}"


@registry.register
async def delete_path(path: str, recursive: bool = False) -> str:
    """Delete a file, or a folder (set recursive=True for a non-empty folder).

    Refuses drive roots. Use with care — deletion is permanent.

    Args:
        path: The file or folder to delete.
        recursive: Allow deleting a non-empty folder and its contents.
    """
    p = Path(path.strip())
    if not p.exists():
        return f"Error: path not found: {p}"
    if (g := _guard(p)) :
        return g
    try:
        if p.is_dir():
            if recursive:
                await asyncio.to_thread(shutil.rmtree, str(p))
            else:
                p.rmdir()  # only succeeds if empty
        else:
            p.unlink()
        return f"Deleted {p}"
    except OSError as exc:
        return f"Error deleting (use recursive=True for a non-empty folder?): {exc}"
