"""On-disk persistence for Telegram per-chat conversation history.

The Telegram bot keeps one rolling ``history`` list per chat in memory; a
restart used to lose it (unlike the Chainlit front-end, which persists threads
to SQLite). This module mirrors each chat's history to a tiny JSON file so a
conversation survives a bot restart.

Deliberately small + dependency-free (stdlib ``json`` only):
  * One file per chat: ``<TELEGRAM_HISTORY_DIR>/chat_<id>.json``.
  * Atomic writes (temp file + ``os.replace``) so a crash mid-write can't leave
    a half-written, unreadable file.
  * Only conversation turns are stored — NOT the transient system blocks
    (persona / memory / lessons / rolling summary) that ``BaseAgent.run``
    re-injects every turn. The one exception is the ``[[ZERO_SUMMARY]]`` block,
    which IS kept because it carries compressed older context.
  * Never raises: any I/O hiccup is logged and the bot keeps running on its
    in-RAM history. Persistence is a convenience, never a correctness gate.

This is the in-memory long-term ChromaDB memory's transcript counterpart: the
distilled facts/lessons live in ChromaDB regardless; this just preserves the
literal back-and-forth so a chat can be resumed verbatim after a restart.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from orchestrator import config
from orchestrator.summarizer import SUMMARY_MARK

logger = logging.getLogger("zero_agent.telegram_history")

# Bump if the on-disk shape ever changes so old files can be detected/migrated.
_SCHEMA = 1


def _enabled() -> bool:
    return bool(getattr(config, "TELEGRAM_PERSIST_HISTORY", False))


def _dir() -> Path:
    return Path(config.TELEGRAM_HISTORY_DIR)


def _path(chat_id: int) -> Path:
    return _dir() / f"chat_{chat_id}.json"


def _is_transient_system(msg: dict[str, Any]) -> bool:
    """True for system blocks re-injected each turn (persona/memory/lessons).

    These must NOT be persisted: ``BaseAgent.run`` rebuilds them every turn, so
    saving them would let stale copies accumulate. The rolling summary
    (``[[ZERO_SUMMARY]]``) is the deliberate exception — it carries compressed
    history and is kept.
    """
    if msg.get("role") != "system":
        return False
    content = str(msg.get("content", ""))
    return not content.startswith(SUMMARY_MARK)


def _persistable(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The subset of ``history`` worth writing to disk (see module docstring)."""
    msgs = [m for m in history if not _is_transient_system(m)]
    cap = getattr(config, "TELEGRAM_HISTORY_MAX_MSGS", 200)
    if cap and len(msgs) > cap:
        # Keep a leading summary (if any) plus the most-recent turns.
        summary = [m for m in msgs[:1] if str(m.get("content", "")).startswith(SUMMARY_MARK)]
        msgs = summary + msgs[-(cap - len(summary)):]
    return msgs


def load(chat_id: int) -> tuple[list[dict[str, Any]], str | None]:
    """Return ``(history, model)`` for a chat, or ``([], None)`` if none/disabled."""
    if not _enabled():
        return [], None
    path = _path(chat_id)
    if not path.exists():
        return [], None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        history = data.get("history") or []
        model = data.get("model")
        if not isinstance(history, list):
            return [], None
        return history, model
    except (OSError, ValueError) as exc:  # corrupt/unreadable -> start fresh
        logger.warning("could not load Telegram history for chat %s: %s", chat_id, exc)
        return [], None


def save(chat_id: int, history: list[dict[str, Any]], model: str | None = None) -> None:
    """Atomically mirror a chat's history to disk. Never raises."""
    if not _enabled():
        return
    try:
        _dir().mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": _SCHEMA,
            "chat_id": chat_id,
            "model": model,
            "history": _persistable(history),
        }
        path = _path(chat_id)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)  # atomic on Windows + POSIX
    except OSError as exc:  # disk full / permissions — log and carry on
        logger.warning("could not save Telegram history for chat %s: %s", chat_id, exc)


def clear(chat_id: int) -> None:
    """Delete a chat's on-disk history (used by /reset). Never raises."""
    if not _enabled():
        return
    try:
        _path(chat_id).unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("could not clear Telegram history for chat %s: %s", chat_id, exc)
