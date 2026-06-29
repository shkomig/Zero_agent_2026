"""Studio API tools — drive the Project_Mair_Full_AI production pipeline from Zero.

The Meir Studio runs a FastAPI server (default port 3333) that exposes the full
chapter production pipeline: script generation → image/video render → audio →
Remotion render → YouTube upload.

Zero can now orchestrate entire episodes autonomously:
  1. generate_studio_script(concept)        ← Claude writes Chapter_N.json
  2. run_studio_pipeline(chapter_num)       ← FLUX images + TTS + WanVideo
  3. poll get_studio_pipeline_status()      ← monitor progress
  4. regenerate_studio_scene(num, scene_id) ← fix bad frames
  5. render_studio_chapter(num)             ← Remotion final MP4
  6. upload_studio_to_youtube(num)          ← publish

All calls are non-blocking where possible; the pipeline itself runs as a
background task inside the Studio server.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

import httpx

from orchestrator.tools.registry import registry

logger = logging.getLogger("zero_agent.studio")

STUDIO_HOST: str = os.getenv("MEIR_STUDIO_HOST", "http://127.0.0.1:3333")
_TIMEOUT = httpx.Timeout(30.0, read=120.0)


async def _get(path: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as http:
        r = await http.get(f"{STUDIO_HOST}{path}")
        r.raise_for_status()
        return r.json()


async def _post(path: str, body: dict | None = None) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as http:
        r = await http.post(f"{STUDIO_HOST}{path}", json=body or {})
        r.raise_for_status()
        return r.json()


# ── Chapter management ────────────────────────────────────────────────────────

@registry.register
async def get_studio_chapters() -> str:
    """List all Meir Studio chapters and their production status.

    Returns a summary of every chapter: number, title, scene count, how many
    images/videos/audio files exist, and whether the final MP4 is rendered.
    Use this to decide which chapters need work before running the pipeline.
    """
    try:
        chapters = await _get("/api/chapters")
    except httpx.HTTPError as exc:
        return f"Error: could not reach Meir Studio at {STUDIO_HOST}: {exc}"

    if not chapters:
        return "No chapters found. Generate a script first with generate_studio_script()."

    lines = [f"{'#':<4} {'Title':<30} {'Scenes':>6} {'Images':>7} {'Videos':>7} {'Audio':>6} {'Status':<10}"]
    lines.append("-" * 75)
    for ch in chapters:
        lines.append(
            f"{ch['number']:<4} {ch.get('title','(no title)')[:30]:<30} "
            f"{ch['scene_count']:>6} {ch['video_count']:>7} {ch['video_count']:>7} "
            f"{ch['audio_count']:>6} {ch['status']:<10}"
        )
    return "\n".join(lines)


@registry.register
async def get_studio_chapter(chapter_num: int) -> str:
    """Get detailed scene list for a specific chapter including per-scene status.

    Shows which scenes have images, videos, and audio already generated so you
    can identify exactly which scenes need regeneration.

    Args:
        chapter_num: The chapter number (e.g. 42).
    """
    try:
        data = await _get(f"/api/chapters/{chapter_num}")
    except httpx.HTTPStatusError as exc:
        return f"Error: chapter {chapter_num} not found ({exc.response.status_code})."
    except httpx.HTTPError as exc:
        return f"Error reaching Studio: {exc}"

    scenes = data.get("scenes", [])
    missing_img = [s["id"] for s in scenes if not s.get("has_image")]
    missing_vid = [s["id"] for s in scenes if not s.get("has_video")]
    missing_aud = [s["id"] for s in scenes if not s.get("has_audio")]

    summary = (
        f"Chapter {chapter_num} — {len(scenes)} scenes\n"
        f"Missing images: {missing_img or 'none'}\n"
        f"Missing videos: {missing_vid or 'none'}\n"
        f"Missing audio:  {missing_aud or 'none'}\n"
    )
    return summary


# ── Script generation ─────────────────────────────────────────────────────────

@registry.register
async def generate_studio_script(
    concept: str,
    music_style: str = "adventure",
    episode_length: str = "regular",
    engine: str = "local",
) -> str:
    """Generate a new Meir Studio chapter (script + audio JSON) from a concept.

    Calls Claude (via the Studio server) to write a full Chapter_N.json with
    20-35 scenes in the "מאיר והפנס" format, plus an Audio JSON for ElevenLabs.

    Args:
        concept: Hebrew or English description of the episode concept, e.g.
            "יעקב מגיע עם מפה עתיקה שמובילה לאוצר נסתר בכפר. מאיר מגלה שהאוצר הוא זיכרון משותף."
        music_style: BGM style — "adventure", "calm", "tension", "celebration" (default: "adventure").
        episode_length: "short" (10-15 scenes), "regular" (25-32), "long" (30-40) (default: "regular").
        engine: Image engine — "local" (FLUX via ComfyUI) or "gemini" (Gemini Nano Banana) (default: "local").
    """
    try:
        result = await _post("/api/generate-script", {
            "concept": concept,
            "form": {"music": music_style, "length": episode_length, "engine": engine},
        })
    except httpx.HTTPError as exc:
        return f"Error generating script: {exc}"

    return (
        f"✅ Chapter {result['chapter_num']} created — {result['scene_count']} scenes.\n"
        f"Engine: {result['engine']} | Path: {result['chapter_path']}\n"
        f"Next: run_studio_pipeline({result['chapter_num']}) to generate images + video + audio."
    )


# ── Pipeline ──────────────────────────────────────────────────────────────────

@registry.register
async def run_studio_pipeline(
    chapter_num: int,
    scene_ids: list[int] | None = None,
) -> str:
    """Start the Meir Studio production pipeline for a chapter (background task).

    Runs: FLUX image generation → ElevenLabs TTS → WanVideo (img2vid) for every
    scene. If scene_ids is provided, only those scenes are processed (useful for
    regenerating specific failed scenes). Returns immediately — poll with
    get_studio_pipeline_status() to monitor progress.

    Args:
        chapter_num: The chapter number to process.
        scene_ids: Optional list of scene ids to process (default: all scenes).
    """
    if await _is_pipeline_running():
        return "⚠️ Pipeline is already running. Wait for it to finish or check get_studio_pipeline_status()."
    try:
        body: dict[str, Any] = {"chapter": chapter_num}
        if scene_ids:
            body["scene_ids"] = scene_ids
        await _post("/api/pipeline/run", body)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 409:
            return "⚠️ Pipeline already running."
        return f"Error starting pipeline: {exc}"
    except httpx.HTTPError as exc:
        return f"Error reaching Studio: {exc}"

    target = f"scenes {scene_ids}" if scene_ids else "all scenes"
    return (
        f"🎬 Pipeline started for Chapter {chapter_num} ({target}).\n"
        "Call get_studio_pipeline_status() to monitor progress."
    )


@registry.register
async def get_studio_pipeline_status() -> str:
    """Get the current status of the Meir Studio production pipeline.

    Returns whether the pipeline is running, which chapter and scene it is on,
    progress percentage, and the latest status message.
    Use this to poll progress after run_studio_pipeline().
    """
    try:
        s = await _get("/api/pipeline/status")
    except httpx.HTTPError as exc:
        return f"Error reaching Studio: {exc}"

    if not s.get("running"):
        prog = s.get("progress", 0)
        total = s.get("total", 0)
        msg = s.get("message", "idle")
        return f"Pipeline idle. Last: chapter {s.get('chapter','?')} — {prog}/{total} scenes. Message: {msg}"

    prog = s.get("progress", 0)
    total = s.get("total", 1)
    pct = int(prog / total * 100) if total else 0
    return (
        f"🔄 Pipeline RUNNING — Chapter {s['chapter']} | Scene {s.get('scene','?')} | "
        f"{prog}/{total} ({pct}%) | {s.get('message','...')}"
    )


async def _is_pipeline_running() -> bool:
    try:
        s = await _get("/api/pipeline/status")
        return bool(s.get("running"))
    except Exception:
        return False


@registry.register
async def regenerate_studio_scene(
    chapter_num: int,
    scene_id: int,
    extra_prompt: str = "",
    engine: str = "local",
) -> str:
    """Regenerate a single failed or low-quality scene in a Meir Studio chapter.

    Deletes the existing image + video for that scene and re-runs the pipeline
    for just that scene. Optionally appends extra_prompt to the visual_prompt to
    guide the regeneration (e.g. to fix a character consistency issue).

    Args:
        chapter_num: The chapter number.
        scene_id: The scene id to regenerate.
        extra_prompt: Optional text appended to the scene's visual_prompt, e.g.
            "make sure brown flat cap is clearly visible, no other characters".
        engine: "local" (FLUX) or "gemini" (default: "local").
    """
    try:
        result = await _post(
            f"/api/pipeline/regenerate-scene/{chapter_num}/{scene_id}",
            {"extra_prompt": extra_prompt, "engine": engine},
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 409:
            return "⚠️ Pipeline is busy. Wait for it to finish first."
        if exc.response.status_code == 404:
            return f"Error: chapter {chapter_num} or scene {scene_id} not found."
        return f"Error: {exc}"
    except httpx.HTTPError as exc:
        return f"Error reaching Studio: {exc}"

    deleted = result.get("deleted", [])
    return (
        f"🔄 Regenerating Chapter {chapter_num} scene {scene_id}.\n"
        f"Deleted: {deleted or 'nothing'}\n"
        f"Extra prompt: {extra_prompt or '(none)'}\n"
        "Poll get_studio_pipeline_status() to track completion."
    )


# ── Render & publish ──────────────────────────────────────────────────────────

@registry.register
async def render_studio_chapter(chapter_num: int) -> str:
    """Render the final MP4 for a Meir Studio chapter using Remotion.

    This is the last production step after all scenes have images + videos + audio.
    Generates the scenes.ts data file, then runs Remotion to compose and export a
    final Chapter_N_FINAL.mp4. Runs as a background task — poll the Studio
    WebSocket or check the output directory for the file.

    Args:
        chapter_num: The chapter number to render.
    """
    try:
        await _post(f"/api/render/{chapter_num}")
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return f"Error: no Remotion template available for chapter {chapter_num}."
        return f"Error: {exc}"
    except httpx.HTTPError as exc:
        return f"Error reaching Studio: {exc}"

    return (
        f"🎞️ Remotion render started for Chapter {chapter_num}.\n"
        f"Output: C:/AI-MEDIA-RTX5090/Project_Mair_Full_AI/output/Chapter_{chapter_num}/Chapter_{chapter_num}_FINAL.mp4\n"
        "This takes several minutes. The Studio UI will notify when done."
    )


@registry.register
async def upload_studio_to_youtube(chapter_num: int, privacy: str = "private") -> str:
    """Upload a finished Meir Studio chapter to YouTube.

    Requires the final MP4 to exist (run render_studio_chapter first).
    The chapter is uploaded with its title from the Audio JSON.

    Args:
        chapter_num: The chapter number to upload.
        privacy: YouTube privacy setting — "private", "unlisted", or "public" (default: "private").
    """
    try:
        result = await _post("/api/youtube/upload", {"chapter": chapter_num, "privacy": privacy})
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return f"Error: final MP4 not found for chapter {chapter_num}. Run render_studio_chapter first."
        return f"Error: {exc.response.text[:300]}"
    except httpx.HTTPError as exc:
        return f"Error reaching Studio: {exc}"

    return f"✅ Chapter {chapter_num} uploaded to YouTube ({privacy}).\n{result.get('output','')}"
