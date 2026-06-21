"""Local text-to-speech — the agent speaks its replies (voice Stage 1).

100% offline and torch-free: uses ``kokoro-onnx`` (onnxruntime) with the Kokoro
v1.0 voice model. Synthesis runs faster than real-time on CPU and uses a SEPARATE
onnxruntime session, so it never competes for the main LLM's VRAM (this is why
voice is safe here where the translation-pivot idea was not). Everything is
optional and opt-in — a missing model, a failed import, or a synthesis hiccup
just disables speech for that turn; it never raises into the conversation.

The engine is loaded lazily ONCE (a ~1 s cost) and reused. Callers hand in plain
answer text; :func:`clean_for_speech` strips markdown / code / emoji / badges so
the voice reads prose, not symbols, and trims very long answers to a sentence
boundary so a wall of text doesn't become minutes of audio.
"""

from __future__ import annotations

import logging
import re
import threading
from pathlib import Path

from orchestrator import config

logger = logging.getLogger("zero_agent.tts")

# Lazily-created singleton engine, guarded so two concurrent turns don't both
# load the 310 MB model. ``_load_failed`` latches so we don't retry a broken
# setup (missing files / bad install) on every single turn.
_engine = None
_engine_lock = threading.Lock()
_load_failed = False

# Stripping patterns for turning markdown answers into clean speakable prose.
_FENCED_CODE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`]*)`")
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_MD_EMPH = re.compile(r"(\*\*|\*|__|_|#+|>)")
_URL = re.compile(r"https?://\S+")
# Emoji / pictographs / our status badges (⏱️ ⏹️ ✅ 🧠 …). Broad ranges are fine
# here — we only want letters, digits and basic punctuation spoken.
_EMOJI = re.compile(
    "[" "\U0001f000-\U0001faff" "\U00002600-\U000027bf" "\U0000fe00-\U0000fe0f"
    "\U00002190-\U000021ff" "\U00002b00-\U00002bff" "\U00002000-\U0000206f"
    "\U00002300-\U000023ff"  # Misc Technical: ⏱ ⏹ ⏰ … (our status badges)
    "]+",
    flags=re.UNICODE,
)
_WS = re.compile(r"[ \t]*\n[ \t]*")
_MULTISPACE = re.compile(r"[ \t]{2,}")
# Collapse the ". . ." / ".." artifacts left when markup between sentences is stripped.
_DOT_RUN = re.compile(r"(?:\.\s*){2,}")


def available() -> bool:
    """True if the engine can (probably) run — files present and not yet failed."""
    if _load_failed:
        return False
    return (
        Path(config.TTS_MODEL_PATH).exists()
        and Path(config.TTS_VOICES_PATH).exists()
    )


def _get_engine():
    """Load (once) and return the Kokoro engine, or None if unavailable."""
    global _engine, _load_failed
    if _engine is not None:
        return _engine
    if _load_failed or not available():
        return None
    with _engine_lock:
        if _engine is not None:
            return _engine
        try:
            from kokoro_onnx import Kokoro

            _engine = Kokoro(config.TTS_MODEL_PATH, config.TTS_VOICES_PATH)
            logger.info("TTS engine loaded (kokoro-onnx, voice=%s)", config.TTS_VOICE)
        except Exception:  # noqa: BLE001 - any failure just disables speech
            logger.exception("TTS engine failed to load; speech disabled")
            _load_failed = True
            return None
    return _engine


def clean_for_speech(text: str, max_chars: int | None = None) -> str:
    """Turn a markdown answer into clean, speakable prose.

    Drops code blocks, URLs, markdown syntax, emoji and status badges, collapses
    whitespace, and trims to the last sentence boundary under ``max_chars`` so a
    long answer doesn't become minutes of audio.
    """
    max_chars = config.TTS_MAX_CHARS if max_chars is None else max_chars
    t = _FENCED_CODE.sub(" ", text)          # drop code blocks entirely
    t = _INLINE_CODE.sub(r"\1", t)           # keep inline-code words, drop ticks
    t = _MD_LINK.sub(r"\1", t)               # [label](url) -> label
    t = _URL.sub(" ", t)                     # bare URLs are noise spoken aloud
    t = _EMOJI.sub(" ", t)
    t = _MD_EMPH.sub(" ", t)                 # **, *, #, > markers
    t = _WS.sub(". ", t)                     # newlines -> sentence pauses
    t = _DOT_RUN.sub(". ", t)                # ". . ." -> ". "
    t = _MULTISPACE.sub(" ", t).strip()
    if len(t) > max_chars:
        cut = t[:max_chars]
        # Prefer to end on a sentence boundary so it doesn't stop mid-word.
        m = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
        t = (cut[: m + 1] if m > max_chars * 0.5 else cut).rstrip() + " …"
    return t


async def synthesize(text: str, out_path: str | Path) -> Path | None:
    """Synthesize ``text`` to a WAV at ``out_path``. Returns the path, or None.

    Runs the blocking onnxruntime synthesis in a worker thread so it never
    stalls the event loop. Never raises — any problem returns None and the caller
    simply skips audio for this turn.
    """
    import asyncio

    speech = clean_for_speech(text)
    if not speech:
        return None
    engine = _get_engine()
    if engine is None:
        return None

    out_path = Path(out_path)

    def _run() -> Path | None:
        try:
            import soundfile as sf

            samples, sample_rate = engine.create(
                speech, voice=config.TTS_VOICE, speed=config.TTS_SPEED,
                lang=config.TTS_LANG,
            )
            out_path.parent.mkdir(parents=True, exist_ok=True)
            sf.write(str(out_path), samples, sample_rate)
            return out_path
        except Exception:  # noqa: BLE001 - speech must never break a turn
            logger.exception("TTS synthesis failed; skipping audio for this turn")
            return None

    return await asyncio.to_thread(_run)


async def synthesize_segment(
    text: str, out_path: str | Path
) -> tuple[Path, float] | None:
    """Synthesize ONE short segment (a sentence) and report its DURATION.

    Like :func:`synthesize`, but (a) does NOT trim to ``TTS_MAX_CHARS`` — the
    caller feeds a sentence at a time — and (b) returns ``(path, seconds)`` so a
    streaming speaker can play segments back-to-back without overlap. This is what
    lets Zero start talking on the first sentence while the rest still generates.
    Never raises; returns None on any problem.
    """
    import asyncio

    speech = clean_for_speech(text, max_chars=10_000)
    if not speech:
        return None
    engine = _get_engine()
    if engine is None:
        return None

    out_path = Path(out_path)

    def _run() -> tuple[Path, float] | None:
        try:
            import soundfile as sf

            samples, sample_rate = engine.create(
                speech, voice=config.TTS_VOICE, speed=config.TTS_SPEED,
                lang=config.TTS_LANG,
            )
            out_path.parent.mkdir(parents=True, exist_ok=True)
            sf.write(str(out_path), samples, sample_rate)
            duration = len(samples) / float(sample_rate or 24000)
            return out_path, duration
        except Exception:  # noqa: BLE001 - speech must never break a turn
            logger.exception("TTS segment synthesis failed; skipping this segment")
            return None

    return await asyncio.to_thread(_run)
