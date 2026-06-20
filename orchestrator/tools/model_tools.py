"""Model-lifecycle tools: download a GGUF from Hugging Face, register it with
Ollama, and list what's installed.

Built after the agent failed a model download by (a) trying to pull a whole repo
(all 16 quant variants) instead of one file and (b) running it through the 20 s
``execute_terminal_command`` tool, which killed it. These tools encapsulate the
*correct* behavior so the model can't repeat those mistakes:

* :func:`download_hf_model` — downloads exactly ONE file as a **cancellable
  background subprocess** (no event-loop blocking, no 20 s cap). Progress and
  cancellation are exposed through ``manage_jobs``.
* :func:`register_gguf_model` — writes a Modelfile and runs ``ollama create`` so
  a downloaded ``.gguf`` becomes a usable Ollama model.
* :func:`list_ollama_models` — what's installed right now.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from orchestrator.tools.registry import registry

# Default place to store downloaded models (matches where the user keeps them).
DEFAULT_MODELS_DIR = os.getenv("ZERO_AGENT_MODELS_DIR", r"C:\AI-ALL-PRO\models")

# Windows flag: run the download detached, with no console window popping up.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass
class DownloadJob:
    """In-memory record of one background file download."""

    id: str
    repo_id: str
    filename: str
    dest_dir: Path
    expected_bytes: int = 0
    proc: "subprocess.Popen[bytes] | None" = None
    log_path: Path | None = None
    status: str = "running"  # running -> done | error | cancelled
    created: float = field(default_factory=time.time)
    finished: float = 0.0

    @property
    def final_path(self) -> Path:
        return self.dest_dir / self.filename


# job_id -> DownloadJob. Lives for the process lifetime. Read by manage_jobs.
DOWNLOADS: dict[str, DownloadJob] = {}


def _downloaded_bytes(job: DownloadJob) -> int:
    """Best-effort current byte count: the finished file, else the partial."""
    if job.final_path.exists():
        return job.final_path.stat().st_size
    cache = job.dest_dir / ".cache" / "huggingface" / "download"
    total = 0
    if cache.is_dir():
        for p in cache.glob("*.incomplete"):
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


def _refresh(job: DownloadJob) -> None:
    """Update a running job's status by inspecting its subprocess + filesystem."""
    if job.status != "running" or job.proc is None:
        return
    code = job.proc.poll()
    if code is None:
        return  # still running
    job.finished = time.time()
    # Process exited: success only if the final file exists at ~full size.
    if job.final_path.exists() and (
        job.expected_bytes == 0
        or job.final_path.stat().st_size >= job.expected_bytes * 0.999
    ):
        job.status = "done"
    else:
        job.status = "error"


def progress_line(job: DownloadJob) -> str:
    """A one-line human-readable status for ``job`` (used by manage_jobs)."""
    _refresh(job)
    got = _downloaded_bytes(job)
    gb = got / 1e9
    if job.expected_bytes:
        pct = got * 100 / job.expected_bytes
        size = f"{gb:.2f}/{job.expected_bytes / 1e9:.2f} GB ({pct:.0f}%)"
    else:
        size = f"{gb:.2f} GB"
    return f"[{job.id}] {job.status} · {job.filename} · {size}"


@registry.register
async def download_hf_model(
    repo_id: str, filename: str, dest_dir: str = ""
) -> str:
    """Download ONE file from a Hugging Face repo as a background job.

    Use this for model weights (e.g. a single .gguf quant). It downloads exactly
    the file you name — NEVER the whole repo — as a cancellable background
    process, so it does not block and is not subject to the terminal timeout.
    Check progress or cancel it with manage_jobs. Pick ONE quant (e.g.
    '...Q4_K_M.gguf'); do not try to fetch every variant.

    Args:
        repo_id: The HF repo, e.g. 'mradermacher/Qwen3-30B-A3B-...-i1-GGUF'.
        filename: The single file to download, e.g. '...i1-Q4_K_M.gguf'.
        dest_dir: Destination folder. Defaults to the models directory; the file
            lands in a subfolder named after the repo.
    """
    repo_id = (repo_id or "").strip()
    filename = (filename or "").strip()
    if not repo_id or not filename:
        return "Error: both repo_id and filename are required (pick ONE file)."

    base = Path(dest_dir.strip()) if dest_dir.strip() else Path(DEFAULT_MODELS_DIR)
    target = base / repo_id.split("/")[-1]
    target.mkdir(parents=True, exist_ok=True)

    # Look up the expected size so progress can show a percentage (best-effort).
    expected = 0
    try:
        from huggingface_hub import HfApi

        info = await asyncio.to_thread(
            HfApi().model_info, repo_id, files_metadata=True
        )
        for s in info.siblings:
            if s.rfilename == filename:
                expected = int(s.size or 0)
                break
        if expected == 0:
            avail = [s.rfilename for s in info.siblings if s.rfilename.endswith(".gguf")]
            return (
                f"Error: '{filename}' not found in {repo_id}. Available .gguf files:\n"
                + "\n".join(f"  - {a}" for a in avail[:40])
            )
    except Exception:  # noqa: BLE001 - size is optional; proceed without it
        pass

    job_id = uuid.uuid4().hex[:8]
    log_path = target / f".download_{job_id}.log"
    # xet disabled => plain reliable HTTPS (the xet backend stalled in testing).
    env = {**os.environ, "HF_HUB_DISABLE_XET": "1", "PYTHONUTF8": "1"}
    # Retry loop: a big file's HTTPS stream can drop mid-way
    # (RemoteProtocolError); hf_hub_download resumes from the partial on the next
    # call, so we retry several times before giving up.
    code = (
        "import time\n"
        "from huggingface_hub import hf_hub_download\n"
        "for a in range(1, 13):\n"
        "    try:\n"
        f"        print(hf_hub_download(repo_id={repo_id!r}, "
        f"filename={filename!r}, local_dir={str(target)!r})); break\n"
        "    except Exception as e:\n"
        "        print('retry', a, type(e).__name__, flush=True); time.sleep(5)\n"
        "else:\n"
        "    raise SystemExit(1)\n"
    )
    try:
        log_f = open(log_path, "wb")  # noqa: SIM115 - closed when proc ends
        proc = subprocess.Popen(
            [sys.executable, "-c", code],
            stdout=log_f,
            stderr=subprocess.STDOUT,
            env=env,
            creationflags=_NO_WINDOW,
        )
    except OSError as exc:
        return f"Error: could not start download: {type(exc).__name__}: {exc}"

    job = DownloadJob(
        id=job_id,
        repo_id=repo_id,
        filename=filename,
        dest_dir=target,
        expected_bytes=expected,
        proc=proc,
        log_path=log_path,
    )
    DOWNLOADS[job_id] = job
    size_note = f" (~{expected / 1e9:.1f} GB)" if expected else ""
    return (
        f"Started download (job_id={job_id}) of {filename}{size_note} into {target}.\n"
        "It runs in the background. Use manage_jobs to see progress or cancel it."
    )


@registry.register
async def download_file(url: str, dest_path: str) -> str:
    """Download ANY file from a URL to a local path as a background job.

    Use this for large or non-HuggingFace downloads — GitHub release zips,
    installers (e.g. WinPython), binaries (e.g. llama.cpp), datasets — that
    execute_terminal_command CANNOT fetch (its ~20 s timeout kills big
    downloads). For HuggingFace model weights use download_hf_model instead.
    Follows redirects; runs in the background (not blocked, no timeout). Check
    progress or cancel with manage_jobs. Do NOT poll in a loop — start it, tell
    the user it's downloading, and move on.

    Args:
        url: Direct download URL.
        dest_path: Full destination FILE path (parent folders are created).
    """
    url = (url or "").strip()
    dest_path = (dest_path or "").strip()
    if not url or not dest_path:
        return "Error: both url and dest_path are required."
    dest = Path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)

    # Best-effort size (for progress %) via a HEAD request; optional.
    expected = 0
    try:
        import urllib.request

        def _head() -> int:
            req = urllib.request.Request(
                url, method="HEAD", headers={"User-Agent": "ZeroAgent/0.1"}
            )
            with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310
                return int(r.headers.get("Content-Length") or 0)

        expected = await asyncio.to_thread(_head)
    except Exception:  # noqa: BLE001 - size is optional
        pass

    job_id = uuid.uuid4().hex[:8]
    log_path = dest.parent / f".download_{job_id}.log"
    # Stream to disk with urllib (stdlib, follows redirects); retry on drop.
    code = (
        "import urllib.request, shutil, time\n"
        f"url={url!r}; dest={str(dest)!r}\n"
        "for a in range(1, 6):\n"
        "    try:\n"
        "        req=urllib.request.Request(url, headers={'User-Agent':'ZeroAgent/0.1'})\n"
        "        with urllib.request.urlopen(req, timeout=60) as r, open(dest,'wb') as f:\n"
        "            shutil.copyfileobj(r, f, 262144)\n"
        "        print('done', dest); break\n"
        "    except Exception as e:\n"
        "        print('retry', a, type(e).__name__, e, flush=True); time.sleep(5)\n"
        "else:\n"
        "    raise SystemExit(1)\n"
    )
    try:
        log_f = open(log_path, "wb")  # noqa: SIM115
        proc = subprocess.Popen(
            [sys.executable, "-c", code],
            stdout=log_f,
            stderr=subprocess.STDOUT,
            env={**os.environ, "PYTHONUTF8": "1"},
            creationflags=_NO_WINDOW,
        )
    except OSError as exc:
        return f"Error: could not start download: {type(exc).__name__}: {exc}"

    DOWNLOADS[job_id] = DownloadJob(
        id=job_id,
        repo_id=url,
        filename=dest.name,
        dest_dir=dest.parent,
        expected_bytes=expected,
        proc=proc,
        log_path=log_path,
    )
    size_note = f" (~{expected / 1e9:.2f} GB)" if expected else ""
    return (
        f"Started download (job_id={job_id}) of {dest.name}{size_note} into "
        f"{dest.parent}.\nRuns in the background — use manage_jobs to check "
        "progress or cancel. Do NOT poll in a loop."
    )


@registry.register
async def register_gguf_model(
    gguf_path: str, model_name: str, num_ctx: int = 32768, system: str = ""
) -> str:
    """Register a downloaded .gguf file with Ollama so it becomes a usable model.

    Writes a Modelfile next to the .gguf and runs `ollama create`, after which
    the model shows up in `ollama list` and can be selected. Note: a custom GGUF
    import is usually NOT registered with tool support (completion only), so the
    agent will treat it as a tool-less chat model.

    Args:
        gguf_path: Absolute path to the .gguf file.
        model_name: The Ollama name to create, e.g. 'qwen3-erotic'.
        num_ctx: Context window to bake into the Modelfile (default 32768).
        system: Optional system prompt to embed in the model.
    """
    gguf = Path(gguf_path.strip())
    model_name = (model_name or "").strip()
    if not gguf.exists():
        return f"Error: gguf file not found: {gguf}"
    if not model_name:
        return "Error: model_name is required."

    lines = [f'FROM "{gguf}"', f"PARAMETER num_ctx {int(num_ctx)}"]
    if system.strip():
        esc = system.replace('"', '\\"')
        lines.append(f'SYSTEM "{esc}"')
    modelfile = gguf.parent / f"Modelfile.{model_name}"
    try:
        modelfile.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as exc:
        return f"Error writing Modelfile: {exc}"

    def _create() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["ollama", "create", model_name, "-f", str(modelfile)],
            capture_output=True,
            text=True,
            timeout=900,  # importing an 18 GB blob can take a couple of minutes
        )
    try:
        res = await asyncio.to_thread(_create)
    except subprocess.TimeoutExpired:
        return f"Error: 'ollama create {model_name}' timed out (still importing?)."
    except FileNotFoundError:
        return "Error: 'ollama' not found on PATH."

    if res.returncode != 0:
        return f"Error: ollama create failed:\n{(res.stderr or res.stdout)[:1500]}"
    return (
        f"Registered '{model_name}' with Ollama (Modelfile: {modelfile}).\n"
        f"It now appears in `ollama list`. Add it to the model picker to use it in "
        f"the UI/Telegram."
    )


@registry.register
async def list_ollama_models() -> str:
    """List the models currently installed in Ollama (name, size, age)."""
    def _list() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["ollama", "list"], capture_output=True, text=True, timeout=20
        )
    try:
        res = await asyncio.to_thread(_list)
    except FileNotFoundError:
        return "Error: 'ollama' not found on PATH."
    except subprocess.TimeoutExpired:
        return "Error: 'ollama list' timed out."
    if res.returncode != 0:
        return f"Error: ollama list failed: {(res.stderr or '').strip()[:500]}"
    return res.stdout.strip() or "(no models installed)"
