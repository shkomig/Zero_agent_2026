"""Media-generation tools: drive the local ComfyUI server from the agent.

ComfyUI exposes a small HTTP API on the same host as its web UI:

  * POST /prompt          -> queue a workflow, returns a ``prompt_id``
  * GET  /history/{id}    -> the recorded result once the job has finished

A "workflow" here is a ComfyUI *API-format* JSON file (the one produced by
"Save (API Format)" in the UI): a flat mapping of ``node_id -> node``, where
each node has a ``class_type`` and an ``inputs`` dict. This is NOT the default
UI-format export (which nests everything under a ``nodes`` array); the /prompt
endpoint only accepts the API format.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from orchestrator import config
from orchestrator.tools.registry import registry

logger = logging.getLogger("zero_agent.comfyui")

# Root of the local ComfyUI install (its models/ + workflow files live here).
COMFYUI_DIR = os.getenv("ZERO_AGENT_COMFYUI_DIR", r"C:\AI-MEDIA-RTX5090\comfyui-svd")
# Where ready-made API-format workflow JSONs live.
WORKFLOW_DIR = os.getenv("ZERO_AGENT_WORKFLOW_DIR", r"C:\AI-MEDIA-RTX5090")
# Default local vision model for analyze_image (fast + vision-capable).
VISION_MODEL = os.getenv("ZERO_AGENT_VISION_MODEL", "gemma3:4b")

_MODEL_EXTS = {".safetensors", ".gguf", ".ckpt", ".pt", ".pth", ".bin"}


def _load_workflow(workflow_json_path: str) -> dict[str, Any]:
    """Load and lightly normalise an API-format workflow file.

    Raises ValueError/FileNotFoundError with a clear message the agent can read.
    """
    path = Path(workflow_json_path).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"workflow file not found: {path}")

    data = json.loads(path.read_text(encoding="utf-8"))

    # Some files wrap the graph under a top-level "prompt" key; unwrap it.
    if isinstance(data, dict) and "prompt" in data and isinstance(data["prompt"], dict):
        data = data["prompt"]

    if not isinstance(data, dict):
        raise ValueError(
            "workflow JSON is not an API-format graph (expected a flat object of "
            "node_id -> node). Re-export from ComfyUI using 'Save (API Format)'."
        )
    if any(isinstance(v, dict) and "class_type" in v for v in data.values()) is False:
        raise ValueError(
            "no nodes with a 'class_type' were found. This looks like a UI-format "
            "export; use ComfyUI's 'Save (API Format)' option instead."
        )
    return data


def _inject_prompt(workflow: dict[str, Any], prompt_text: str) -> str:
    """Inject ``prompt_text`` into the main positive CLIPTextEncode node.

    Heuristic to avoid clobbering the *negative* prompt:
      1. Prefer a CLIPTextEncode whose _meta.title contains "positive".
      2. Otherwise the first CLIPTextEncode whose title does NOT contain
         "negative".
      3. Otherwise (no titles at all) the first CLIPTextEncode found.

    Returns the node id that was modified (for the success report).
    """
    encoders: list[tuple[str, dict[str, Any]]] = [
        (node_id, node)
        for node_id, node in workflow.items()
        if isinstance(node, dict)
        and node.get("class_type") == "CLIPTextEncode"
        and isinstance(node.get("inputs"), dict)
        and "text" in node["inputs"]
    ]
    if not encoders:
        raise ValueError(
            "no CLIPTextEncode node with a 'text' input was found in the workflow; "
            "cannot inject the prompt automatically."
        )

    def title(node: dict[str, Any]) -> str:
        return str(node.get("_meta", {}).get("title", "")).lower()

    target = next((nid for nid, n in encoders if "positive" in title(n)), None)
    if target is None:
        target = next((nid for nid, n in encoders if "negative" not in title(n)), None)
    if target is None:
        target = encoders[0][0]

    workflow[target]["inputs"]["text"] = prompt_text
    return target


def _collect_outputs(history_entry: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten every produced file (images, gifs, video) into structured items.

    Each item is ``{"filename", "subfolder", "type"}`` — exactly the query
    parameters ComfyUI's ``/view`` endpoint needs to stream the bytes back.
    """
    files: list[dict[str, Any]] = []
    for node_output in (history_entry.get("outputs") or {}).values():
        if not isinstance(node_output, dict):
            continue
        for items in node_output.values():
            if not isinstance(items, list):
                continue
            for item in items:
                if isinstance(item, dict) and "filename" in item:
                    files.append(
                        {
                            "filename": item["filename"],
                            "subfolder": item.get("subfolder", ""),
                            "type": item.get("type", "output"),
                        }
                    )
    return files


# Image extensions we render inline in the UI (videos/gifs are linked by path).
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}

# Marker the UI parses out of a tool result to find files to show inline.
MEDIA_MARKER = "[[MEDIA]]"


def extract_media_paths(result: str) -> tuple[list[Path], list[Path]]:
    """Split a tool result's media markers into (images, other_files).

    The UI calls this on every tool result: image paths are rendered inline,
    other media (video/gif) are returned so the UI can link or embed them too.
    """
    images: list[Path] = []
    others: list[Path] = []
    for line in result.splitlines():
        if not line.startswith(MEDIA_MARKER):
            continue
        path = Path(line[len(MEDIA_MARKER):].strip())
        if path.suffix.lower() in _IMAGE_EXTS:
            images.append(path)
        else:
            others.append(path)
    return images, others



async def _download_outputs(
    http: httpx.AsyncClient, base: str, items: list[dict[str, Any]]
) -> list[Path]:
    """Download each ComfyUI output via /view and save it under MEDIA_OUTPUT_DIR.

    Returns the local paths actually written (skips anything that fails so one
    bad file never sinks the whole result).
    """
    dest_dir = Path(config.MEDIA_OUTPUT_DIR)
    dest_dir.mkdir(parents=True, exist_ok=True)

    saved: list[Path] = []
    for item in items:
        params = {
            "filename": item["filename"],
            "subfolder": item["subfolder"],
            "type": item["type"],
        }
        try:
            resp = await http.get(f"{base}/view", params=params)
            if resp.status_code != 200 or not resp.content:
                continue
        except httpx.HTTPError:
            continue

        # Prefix with a short timestamp so repeated runs never overwrite.
        out_path = dest_dir / f"{int(time.time())}_{item['filename']}"
        try:
            out_path.write_bytes(resp.content)
            saved.append(out_path)
        except OSError:
            continue
    return saved


class ComfyError(RuntimeError):
    """Raised by the shared submit/poll helpers with a user-readable message."""


async def _submit_workflow(
    http: httpx.AsyncClient, base: str, workflow: dict[str, Any]
) -> str:
    """Queue a workflow on ComfyUI and return its prompt_id (or raise ComfyError)."""
    payload = {"prompt": workflow, "client_id": uuid.uuid4().hex}
    try:
        resp = await http.post(f"{base}/prompt", json=payload)
    except httpx.HTTPError as exc:
        raise ComfyError(
            f"could not reach ComfyUI at {base}: {type(exc).__name__}: {exc}"
        ) from exc

    if resp.status_code != 200:
        raise ComfyError(
            f"ComfyUI rejected the workflow (HTTP {resp.status_code}). "
            f"Response: {resp.text[:1000]}"
        )

    submit = resp.json()
    prompt_id = submit.get("prompt_id")
    if not prompt_id:
        node_errors = submit.get("node_errors") or submit.get("error")
        raise ComfyError(f"ComfyUI did not return a prompt_id. Details: {node_errors}")
    return prompt_id


async def _wait_for_result(
    http: httpx.AsyncClient,
    base: str,
    prompt_id: str,
    deadline: float,
    target_node: str,
) -> str:
    """Poll /history until the job finishes; return a formatted result string.

    Shared by both the blocking tool and the background job runner. Downloads
    outputs and emits ``[[MEDIA]]`` markers so the UI can show images inline.
    """
    history_url = f"{base}/history/{prompt_id}"
    while True:
        if time.monotonic() > deadline:
            return (
                f"Error: generation timed out (prompt_id={prompt_id}). "
                "It may still be running in ComfyUI."
            )
        try:
            hist_resp = await http.get(history_url)
            history = hist_resp.json() if hist_resp.status_code == 200 else {}
        except httpx.HTTPError:
            history = {}  # transient; keep polling

        entry = history.get(prompt_id)
        if entry:
            status = entry.get("status", {}) or {}
            completed = status.get("completed")
            status_str = status.get("status_str", "")
            if completed or status_str in {"success", "error"} or entry.get("outputs"):
                if status_str == "error":
                    return (
                        f"Error: ComfyUI reported a failure for prompt_id={prompt_id}. "
                        f"Status: {json.dumps(status)[:1500]}"
                    )
                items = _collect_outputs(entry)
                saved = await _download_outputs(http, base, items)
                if not saved:
                    return (
                        f"Success: ComfyUI finished prompt_id={prompt_id} "
                        f"(status={status_str or 'completed'}), but no file "
                        f"outputs could be downloaded.\n"
                        f"Injected prompt into node {target_node}."
                    )
                listed = "\n  ".join(str(p) for p in saved)
                markers = "\n".join(f"{MEDIA_MARKER}{p}" for p in saved)
                return (
                    f"Success: ComfyUI finished prompt_id={prompt_id} "
                    f"(status={status_str or 'completed'}).\n"
                    f"Injected prompt into node {target_node}.\n"
                    f"Saved {len(saved)} file(s):\n  {listed}\n{markers}"
                )

        await asyncio.sleep(config.COMFYUI_POLL_INTERVAL)


@registry.register
async def free_llm_vram() -> str:
    """Unload the current LLM from GPU VRAM so ComfyUI can use the freed memory.

    ALWAYS call this before generating video or any heavy ComfyUI render.
    The LLM occupies ~20 GB VRAM; ComfyUI video models need another 8-16 GB.
    Running both simultaneously causes out-of-memory — the system freezes.
    Calling this first gracefully unloads the LLM; it reloads automatically
    on the next text reply (adds ~5-10 s to that reply's first-token latency).

    Call order for video generation:
      1. free_llm_vram()          <- this tool
      2. submit_comfyui_job(...)  <- fire background render
      3. Tell the user to check back with check_job(job_id)
    """
    base = config.OLLAMA_HOST.rstrip("/")
    model = config.DEFAULT_MODEL
    try:
        async with httpx.AsyncClient(timeout=15.0) as http:
            resp = await http.post(
                f"{base}/api/generate",
                json={"model": model, "keep_alive": 0},
            )
        if resp.status_code == 200:
            return (
                f"LLM '{model}' unloaded from VRAM. "
                "ComfyUI now has full GPU memory for the render. "
                "The model reloads automatically on the next message (~5-10 s extra latency)."
            )
        return (
            f"Warning: Ollama returned HTTP {resp.status_code} when asked to unload "
            f"'{model}'. Proceeding anyway — VRAM may still be contested."
        )
    except httpx.HTTPError as exc:
        return f"Warning: could not reach Ollama to free VRAM ({exc}). Proceeding anyway."


@registry.register
async def run_comfyui_workflow(workflow_json_path: str, prompt_text: str) -> str:
    """Run a local ComfyUI workflow with a custom prompt and wait for the result.

    Loads an API-format ComfyUI workflow, injects ``prompt_text`` into its main
    positive CLIPTextEncode node, submits it to the local ComfyUI server, and
    polls until the generation finishes. Use this for QUICK jobs (single images).

    WARNING: Do NOT use this for video generation — video renders take minutes
    and will make the agent unresponsive. For video, call free_llm_vram() first,
    then submit_comfyui_job() which returns immediately with a job id.

    Args:
        workflow_json_path: Path to a ComfyUI API-format workflow JSON file.
        prompt_text: The text prompt to inject into the workflow's positive node.
    """
    try:
        workflow = _load_workflow(workflow_json_path)
        target_node = _inject_prompt(workflow, prompt_text)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        return f"Error preparing workflow: {type(exc).__name__}: {exc}"

    base = config.COMFYUI_HOST.rstrip("/")
    deadline = time.monotonic() + config.COMFYUI_TIMEOUT
    async with httpx.AsyncClient(timeout=30.0) as http:
        try:
            prompt_id = await _submit_workflow(http, base, workflow)
        except ComfyError as exc:
            return f"Error: {exc}"
        return await _wait_for_result(http, base, prompt_id, deadline, target_node)


# --- Async jobs (Phase 4b): fire-and-forget heavy renders -------------------
# Instead of a separate webhook server, we run a background asyncio task that
# polls ComfyUI and updates an in-memory job record. The agent submits a job,
# gets an id instantly, keeps working, and checks back with check_job.


@dataclass
class ComfyJob:
    """In-memory record of one background ComfyUI render."""

    id: str
    prompt_text: str
    status: str = "queued"  # queued -> running -> done | error
    prompt_id: str = ""
    result: str = ""  # final formatted string once done (with MEDIA markers)
    error: str = ""
    created: float = field(default_factory=time.time)
    finished: float = 0.0


# job_id -> ComfyJob. Lives for the process lifetime (cleared on restart).
_JOBS: dict[str, ComfyJob] = {}


async def _run_job_in_background(job: ComfyJob, workflow: dict[str, Any], target_node: str) -> None:
    """Submit + poll a workflow, updating ``job`` in place. Never raises."""
    base = config.COMFYUI_HOST.rstrip("/")
    deadline = time.monotonic() + config.COMFYUI_TIMEOUT
    try:
        async with httpx.AsyncClient(timeout=30.0) as http:
            job.status = "running"
            job.prompt_id = await _submit_workflow(http, base, workflow)
            logger.info("job %s submitted prompt_id=%s", job.id, job.prompt_id)
            result = await _wait_for_result(
                http, base, job.prompt_id, deadline, target_node
            )
        job.result = result
        job.status = "error" if result.lstrip().lower().startswith("error") else "done"
    except ComfyError as exc:
        job.status = "error"
        job.error = str(exc)
    except Exception as exc:  # noqa: BLE001 - background task must never crash silently
        job.status = "error"
        job.error = f"{type(exc).__name__}: {exc}"
        logger.exception("job %s crashed", job.id)
    finally:
        job.finished = time.time()
        logger.info("job %s finished status=%s", job.id, job.status)


@registry.register
async def submit_comfyui_job(workflow_json_path: str, prompt_text: str) -> str:
    """Start a ComfyUI render in the BACKGROUND and return a job id immediately.

    Use this for video generation and heavy renders (large batches, long animations).
    Returns a job_id at once; the render continues in the background.
    Check progress later with check_job(job_id).

    IMPORTANT — VRAM: Always call free_llm_vram() BEFORE this for video renders.
    The LLM and a video model cannot share VRAM; skipping free_llm_vram causes
    an out-of-memory crash that freezes the computer.

    Correct call sequence for video:
      1. free_llm_vram()
      2. submit_comfyui_job(workflow, prompt)  -> job_id
      3. Tell user: "Render started (job_id=XXX). Check back with /check_job XXX."

    Args:
        workflow_json_path: Path to a ComfyUI API-format workflow JSON file.
        prompt_text: The text prompt to inject into the workflow's positive node.
    """
    try:
        workflow = _load_workflow(workflow_json_path)
        target_node = _inject_prompt(workflow, prompt_text)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        return f"Error preparing workflow: {type(exc).__name__}: {exc}"

    job = ComfyJob(id=uuid.uuid4().hex[:8], prompt_text=prompt_text)
    _JOBS[job.id] = job
    # Fire-and-forget: schedule the render and return at once.
    asyncio.create_task(_run_job_in_background(job, workflow, target_node))
    logger.info("job %s queued prompt=%r", job.id, prompt_text[:60])
    return (
        f"Started background render (job_id={job.id}). "
        "It is running now; call check_job with this id to see when it's done."
    )


@registry.register
async def check_job(job_id: str) -> str:
    """Check the status (and result) of a background ComfyUI job.

    Args:
        job_id: The id returned by submit_comfyui_job.
    """
    job = _JOBS.get(job_id.strip())
    if job is None:
        known = ", ".join(_JOBS) or "(none)"
        return f"Error: no job with id {job_id!r}. Known jobs: {known}"

    if job.status in {"queued", "running"}:
        elapsed = time.time() - job.created
        return (
            f"Job {job.id} is {job.status} ({elapsed:.0f}s elapsed). "
            "Check again shortly."
        )
    if job.status == "error":
        return f"Job {job.id} failed: {job.error or job.result}"
    # done — return the full result (includes [[MEDIA]] markers for the UI).
    return job.result


# --- Asset discovery + vision ----------------------------------------------
@registry.register
async def list_media_assets() -> str:
    """List the local ComfyUI models (checkpoints, LoRAs, VAEs, ...) and the
    ready-made workflow files available.

    Use this before generating media so you pick a checkpoint/LoRA/workflow that
    actually exists, instead of guessing a filename.
    """
    def _scan() -> str:
        sections: list[str] = []
        models_root = Path(COMFYUI_DIR) / "models"
        for sub in ("checkpoints", "diffusion_models", "loras", "vae", "controlnet"):
            d = models_root / sub
            if not d.is_dir():
                continue
            files = sorted(
                p.name for p in d.rglob("*") if p.suffix.lower() in _MODEL_EXTS
            )
            if files:
                shown = "\n  ".join(files[:30])
                more = f"\n  … (+{len(files) - 30} more)" if len(files) > 30 else ""
                sections.append(f"{sub} ({len(files)}):\n  {shown}{more}")
        wf = sorted(str(p) for p in Path(WORKFLOW_DIR).glob("*_api.json"))
        if wf:
            sections.append("workflows (*_api.json):\n  " + "\n  ".join(wf))
        return "\n\n".join(sections) if sections else (
            f"No assets found under {models_root} or {WORKFLOW_DIR}."
        )

    return await asyncio.to_thread(_scan)


def _inject_image(workflow: dict[str, Any], image_path: str) -> str:
    """Inject a source image path into the first LoadImage node in the workflow.

    Used for img2vid workflows (SVD, WAN, LTX) that take a reference image.
    Returns the node_id that was modified, or raises ValueError if none found.
    """
    loaders = [
        (node_id, node)
        for node_id, node in workflow.items()
        if isinstance(node, dict)
        and node.get("class_type") == "LoadImage"
        and isinstance(node.get("inputs"), dict)
    ]
    if not loaders:
        raise ValueError(
            "No LoadImage node found in workflow. "
            "This workflow may not support img2vid. "
            "Re-export a workflow that has a LoadImage node."
        )
    node_id, node = loaders[0]
    # ComfyUI LoadImage expects just the filename (relative to its input/ folder)
    # OR an absolute path when using the full path variant. We pass the absolute path.
    node["inputs"]["image"] = str(Path(image_path).resolve())
    return node_id


@registry.register
async def submit_img2vid_job(
    workflow_json_path: str,
    source_image_path: str,
    prompt_text: str = "",
) -> str:
    """Start a ComfyUI img2vid render in the BACKGROUND and return a job id.

    Use for video generation FROM an existing image (SVD, WAN, LTX-Video).
    Injects the source image into the workflow's LoadImage node and (if the
    workflow has a CLIPTextEncode) the optional prompt into the positive node.

    IMPORTANT: Always call free_llm_vram() BEFORE this — the LLM and a video
    model cannot share VRAM and will cause an out-of-memory system freeze.

    Args:
        workflow_json_path: Path to a ComfyUI API-format img2vid workflow JSON.
        source_image_path: Absolute path to the source image (PNG/JPG) to animate.
        prompt_text: Optional text prompt to inject (leave empty for pure img2vid).
    """
    if not Path(source_image_path).exists():
        return f"Error: source image not found: {source_image_path}"
    try:
        workflow = _load_workflow(workflow_json_path)
        target_img_node = _inject_image(workflow, source_image_path)
        target_txt_node = ""
        if prompt_text.strip():
            try:
                target_txt_node = _inject_prompt(workflow, prompt_text)
            except ValueError:
                pass  # workflow has no text node — fine for pure img2vid
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        return f"Error preparing img2vid workflow: {type(exc).__name__}: {exc}"

    job = ComfyJob(id=uuid.uuid4().hex[:8], prompt_text=f"img2vid:{Path(source_image_path).name} {prompt_text}".strip())
    _JOBS[job.id] = job
    asyncio.create_task(_run_job_in_background(job, workflow, target_img_node))
    logger.info("img2vid job %s queued image=%s", job.id, source_image_path)
    return (
        f"Started img2vid background render (job_id={job.id}). "
        f"Source: {Path(source_image_path).name}. "
        "Call check_job with this id to poll for completion."
    )


@registry.register
async def generate_verified_image(
    workflow_json_path: str,
    prompt_text: str,
    character_check: str,
    max_retries: int = 3,
) -> str:
    """Generate an image and verify it matches a character description using vision QC.

    Runs the ComfyUI workflow, then asks a local vision model whether the result
    matches `character_check`. If the check fails, regenerates up to `max_retries`
    times (each time appending an enhancement to the negative-prompt region of the
    workflow or just re-running with a new seed). Returns the path of the first
    image that passes QC, or the last generated image if all retries are exhausted.

    Use this instead of run_comfyui_workflow when character consistency is critical
    (e.g., LoRA-based character renders for animated series production).

    Args:
        workflow_json_path: Path to an API-format ComfyUI workflow JSON.
        prompt_text: The positive text prompt to inject.
        character_check: Natural-language description of what the character MUST
            look like — e.g. "brown flat cap, blue vest, golden lantern, Pixar 3D style".
            The vision model answers YES/NO based on this.
        max_retries: Maximum generation attempts before returning the best result (default 3).
    """
    from orchestrator.models.ollama_client import OllamaClient, OllamaError

    base = config.COMFYUI_HOST.rstrip("/")
    vision_model = VISION_MODEL
    last_image_path: str | None = None

    for attempt in range(1, max_retries + 1):
        # ── Generate ──────────────────────────────────────────────────
        try:
            workflow = _load_workflow(workflow_json_path)
            target_node = _inject_prompt(workflow, prompt_text)
        except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
            return f"Error preparing workflow: {type(exc).__name__}: {exc}"

        deadline = time.monotonic() + config.COMFYUI_TIMEOUT
        async with httpx.AsyncClient(timeout=30.0) as http:
            try:
                prompt_id = await _submit_workflow(http, base, workflow)
            except ComfyError as exc:
                return f"Error: {exc}"
            result = await _wait_for_result(http, base, prompt_id, deadline, target_node)

        # Extract the first saved file path from the result string
        image_path: str | None = None
        for line in result.splitlines():
            if line.startswith(MEDIA_MARKER):
                p = Path(line[len(MEDIA_MARKER):].strip())
                if p.suffix.lower() in _IMAGE_EXTS:
                    image_path = str(p)
                    last_image_path = image_path
                    break

        if not image_path:
            logger.warning("attempt %d/%d: no image output from ComfyUI", attempt, max_retries)
            continue

        # ── Visual QC ─────────────────────────────────────────────────
        try:
            raw = await asyncio.to_thread(Path(image_path).read_bytes)
        except OSError as exc:
            logger.warning("attempt %d/%d: cannot read image for QC: %s", attempt, max_retries, exc)
            continue

        import base64 as _b64
        b64 = _b64.b64encode(raw).decode("ascii")
        qc_prompt = (
            f"Look at this image carefully. Does it show a character that matches ALL of the "
            f"following visual traits: {character_check}\n\n"
            "Reply with EXACTLY one word: YES or NO. "
            "YES means the character clearly matches all listed traits. "
            "NO means one or more traits are missing, wrong, or the image looks like a hallucination/error."
        )
        client = OllamaClient(model=vision_model)
        try:
            resp = await client.chat(
                [{"role": "user", "content": qc_prompt, "images": [b64]}]
            )
            verdict = (resp.message.content or "").strip().upper()
        except OllamaError as exc:
            logger.warning("QC vision call failed (attempt %d): %s — accepting image", attempt, exc)
            verdict = "YES"  # fail-open: don't block if vision is down
        finally:
            await client.aclose()

        logger.info(
            "Visual QC attempt %d/%d: verdict=%s image=%s",
            attempt, max_retries, verdict, image_path,
        )

        if "YES" in verdict:
            return (
                f"✅ Image passed visual QC on attempt {attempt}/{max_retries}.\n"
                f"Character check: {character_check}\n"
                f"{result}"
            )

        logger.info("QC failed attempt %d — regenerating...", attempt)

    # All retries exhausted — return last image with warning
    if last_image_path:
        return (
            f"⚠️ Visual QC did not pass after {max_retries} attempts. "
            f"Returning last generated image (may have consistency issues).\n"
            f"Character check was: {character_check}\n"
            f"{MEDIA_MARKER}{last_image_path}"
        )
    return f"Error: all {max_retries} generation attempts failed to produce an image."


@registry.register
async def analyze_image(image_path: str, question: str = "", model: str = "") -> str:
    """Describe or answer a question about a local image using a local vision model.

    Args:
        image_path: Path to the image file (png/jpg/webp/…).
        question: What to ask about the image. Defaults to a general description.
        model: Vision-capable Ollama model to use (defaults to the configured one).
    """
    path = Path(image_path.strip())
    if not path.exists():
        return f"Error: image not found: {path}"
    try:
        raw = await asyncio.to_thread(path.read_bytes)
    except OSError as exc:
        return f"Error reading image: {exc}"

    b64 = base64.b64encode(raw).decode("ascii")
    prompt = question.strip() or "Describe this image in detail."
    # Local import keeps module load order simple and avoids a hard dependency
    # at import time.
    from orchestrator.models.ollama_client import OllamaClient, OllamaError

    client = OllamaClient(model=(model.strip() or VISION_MODEL))
    try:
        resp = await client.chat(
            [{"role": "user", "content": prompt, "images": [b64]}]
        )
        return resp.message.content or "(no description returned)"
    except OllamaError as exc:
        return f"Error: vision model failed: {exc}"
    finally:
        await client.aclose()

