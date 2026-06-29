"""Cross-session task context — persists what Zero is working on between restarts.

Three complementary stores, all pure file I/O (<1 ms each, zero Ollama calls):

* session_state.json  — current task/step (JSON), written by update_task_state tool
* task_journal.md     — append-only action log, written by log_task_action tool
* last_summary.txt    — the rolling [[ZERO_SUMMARY]] saved after each compression,
                        so it survives a session restart instead of being lost

All three are read ONCE at session start and merged into a single [[ZERO_SESSION]]
system message injected into the conversation history. Per-turn overhead: zero.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Any

from orchestrator import config

logger = logging.getLogger("zero_agent.session_context")

# Marker recognised by BaseAgent.run — messages starting with "[[" are kept
# across turns (the persona-refresh only strips non-"[["  system messages).
SESSION_MARK = "[[ZERO_SESSION]]"

# How many recent journal lines to inject at session start (keeps context lean).
_JOURNAL_TAIL = 20

# --- Paths (all under data/session/) ----------------------------------------
_SESSION_DIR = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data", "session"
)
STATE_PATH = os.path.join(_SESSION_DIR, "session_state.json")
JOURNAL_PATH = os.path.join(_SESSION_DIR, "task_journal.md")
SUMMARY_PATH = os.path.join(_SESSION_DIR, "last_summary.txt")


def _ensure_dir() -> None:
    os.makedirs(_SESSION_DIR, exist_ok=True)


# --- Session state (current task / step) ------------------------------------

def load_session_state() -> dict[str, Any]:
    """Return the last saved session state, or an empty dict."""
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh) or {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_session_state(
    task: str,
    step: str = "",
    notes: str = "",
    next_step: str = "",
) -> None:
    """Persist current task context. Called by the update_task_state tool."""
    _ensure_dir()
    state = {
        "active_task": task.strip(),
        "current_step": step.strip(),
        "next_step": next_step.strip(),
        "notes": notes.strip(),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
        logger.info("session state saved: task=%r step=%r", task[:60], step[:60])
    except OSError:
        logger.exception("failed to save session state")


def clear_session_state() -> None:
    """Erase the current session state (task is done / new context)."""
    try:
        os.remove(STATE_PATH)
    except OSError:
        pass


# --- Task journal (append-only action log) ----------------------------------

def append_journal(entry: str) -> None:
    """Append one line to the task journal with a timestamp."""
    _ensure_dir()
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] {entry.strip()}\n"
    try:
        with open(JOURNAL_PATH, "a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        logger.exception("failed to append to task journal")


def _load_journal_tail(n: int = _JOURNAL_TAIL) -> list[str]:
    """Return the last ``n`` non-empty lines of the journal."""
    try:
        with open(JOURNAL_PATH, "r", encoding="utf-8") as fh:
            lines = [l.rstrip() for l in fh.readlines() if l.strip()]
        return lines[-n:]
    except OSError:
        return []


# --- Cross-session rolling summary ------------------------------------------

def save_last_summary(text: str) -> None:
    """Persist the current [[ZERO_SUMMARY]] text so the next session can load it."""
    _ensure_dir()
    text = text.strip()
    if not text:
        return
    try:
        with open(SUMMARY_PATH, "w", encoding="utf-8") as fh:
            fh.write(text)
        logger.debug("persisted rolling summary (%d chars)", len(text))
    except OSError:
        logger.exception("failed to save last summary")


def load_last_summary() -> str | None:
    """Return the persisted summary from the previous session, or None."""
    try:
        with open(SUMMARY_PATH, "r", encoding="utf-8") as fh:
            text = fh.read().strip()
        return text if text else None
    except OSError:
        return None


# --- Session block builder ---------------------------------------------------

def build_session_block(*, history_has_summary: bool = False) -> str | None:
    """Build the [[ZERO_SESSION]] system-prompt block from all three stores.

    Returns None when there is nothing worth injecting (fresh state, no journal,
    no persisted summary), so we don't add an empty block.

    Args:
        history_has_summary: True when the current history already contains a
            [[ZERO_SUMMARY]] (i.e. the thread was resumed from the sidebar and
            the summary was rebuilt from the thread steps). In that case, skip
            loading the persisted summary to avoid stale duplication.
    """
    parts: list[str] = []

    # 1. Current task / step
    state = load_session_state()
    if state.get("active_task"):
        task_lines = [f"Active task: {state['active_task']}"]
        if state.get("current_step"):
            task_lines.append(f"Current step: {state['current_step']}")
        if state.get("next_step"):
            task_lines.append(f"Next step: {state['next_step']}")
        if state.get("notes"):
            task_lines.append(f"Notes: {state['notes']}")
        if state.get("updated_at"):
            task_lines.append(f"Last updated: {state['updated_at']}")
        parts.append("\n".join(task_lines))

    # 2. Recent action log
    journal = _load_journal_tail()
    if journal:
        lines = "\n".join(journal)
        parts.append(f"Recent actions (newest last):\n{lines}")

    # 3. Persisted rolling summary from the previous session (skip if already in history)
    if not history_has_summary:
        summary = load_last_summary()
        if summary:
            parts.append(f"Summary from previous session:\n{summary}")

    if not parts:
        return None

    body = "\n\n".join(parts)
    return (
        f"{SESSION_MARK}\n"
        "Context from previous sessions (use it to maintain continuity; "
        "do not mention this block to the user):\n"
        f"{body}"
    )


# --- History injection -------------------------------------------------------

def inject_session_message(history: list[dict], block: str | None) -> None:
    """Refresh the single [[ZERO_SESSION]] system message in ``history``.

    Removes any previous block, then inserts the new one (if any) right after
    the main system prompt — same pattern as inject_project_message.
    """
    history[:] = [
        m
        for m in history
        if not (
            m.get("role") == "system"
            and str(m.get("content", "")).startswith(SESSION_MARK)
        )
    ]
    if not block:
        return
    insert_at = 1 if history and history[0].get("role") == "system" else 0
    history.insert(insert_at, {"role": "system", "content": block})


def _history_has_rolling_summary(history: list[dict]) -> bool:
    """True if ``history`` already contains a [[ZERO_SUMMARY]] system message."""
    from orchestrator.summarizer import SUMMARY_MARK
    return any(
        m.get("role") == "system"
        and str(m.get("content", "")).startswith(SUMMARY_MARK)
        for m in history
    )
