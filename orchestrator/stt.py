"""Local speech-to-text — talk to the agent (voice Stage 2).

100% offline and torch-free: uses ``faster-whisper`` (CTranslate2). The Chainlit
mic records audio; on stop the accumulated PCM is transcribed here and fed into
the normal message flow. Multilingual — auto-detects Hebrew/English.

Like the TTS side, the model loads lazily ONCE and is reused, a load failure
latches (so a broken setup isn't retried every clip), and nothing here ever
raises into a turn — a hiccup just yields no transcript.
"""

from __future__ import annotations

import logging
import threading
import wave
from pathlib import Path

from orchestrator import config

logger = logging.getLogger("zero_agent.stt")

_model = None
_model_lock = threading.Lock()
_load_failed = False


def available() -> bool:
    """True unless a previous load attempt failed (the package is installed)."""
    return not _load_failed


def _get_model():
    """Load (once) and return the faster-whisper model, or None on failure."""
    global _model, _load_failed
    if _model is not None:
        return _model
    if _load_failed:
        return None
    with _model_lock:
        if _model is not None:
            return _model
        try:
            from faster_whisper import WhisperModel

            Path(config.STT_MODEL_DIR).mkdir(parents=True, exist_ok=True)
            _model = WhisperModel(
                config.STT_MODEL,
                device=config.STT_DEVICE,
                compute_type=config.STT_COMPUTE_TYPE,
                download_root=config.STT_MODEL_DIR,
            )
            logger.info(
                "STT model loaded (faster-whisper %s, %s/%s)",
                config.STT_MODEL, config.STT_DEVICE, config.STT_COMPUTE_TYPE,
            )
        except Exception:  # noqa: BLE001 - any failure just disables STT
            logger.exception("STT model failed to load; speech input disabled")
            _load_failed = True
            return None
    return _model


def _write_wav(pcm: bytes, sample_rate: int, path: Path) -> None:
    """Write 16-bit mono PCM bytes to a WAV file (so faster-whisper can decode)."""
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)


async def transcribe_pcm(
    pcm: bytes, sample_rate: int, language: str | None = None
) -> str | None:
    """Transcribe raw 16-bit mono PCM. Returns the text, or None.

    ``language`` ("en"/"he"/…) forces the spoken language; empty/None falls back
    to ``config.STT_LANGUAGE`` and finally to auto-detect. Forcing it both fixes
    mis-detection (English heard as Hebrew) and is faster (skips the detect pass).
    Runs the blocking model in a worker thread; never raises.
    """
    import asyncio
    import tempfile

    if not pcm or len(pcm) < sample_rate:  # < ~0.5 s of audio -> ignore
        return None
    model = _get_model()
    if model is None:
        return None
    lang = (language or config.STT_LANGUAGE or "").strip() or None

    def _run() -> str | None:
        tmp = Path(tempfile.gettempdir()) / f"zero_stt_{id(pcm)}.wav"
        try:
            _write_wav(pcm, sample_rate, tmp)
            segments, _info = model.transcribe(
                str(tmp),
                language=lang,
                vad_filter=True,  # drop silence so empty/near-silent clips -> ""
            )
            text = " ".join(seg.text.strip() for seg in segments).strip()
            return text or None
        except Exception:  # noqa: BLE001 - transcription must never break a turn
            logger.exception("transcription failed")
            return None
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    return await asyncio.to_thread(_run)
