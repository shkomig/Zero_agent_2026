"""Reflexion-style "learn from mistakes" layer.

A thin sibling of :mod:`orchestrator.auto_memory`. Where auto_memory remembers
durable FACTS about the user, this remembers LESSONS from the agent's own
failures, so it stops repeating them:

* :func:`distill_lessons` — after a turn that hit errors/failures, a small model
  turns "what went wrong" into one-line reusable rules and stores them in the
  same vector store, tagged ``lesson``.
* :func:`recall_lessons` — before a turn, surface the most relevant past lessons
  as a system-prompt block the agent reads ("things you learned the hard way").

This is the realistic, local form of self-improvement: retrieval over past
mistakes (NOT live fine-tuning). Everything runs on the local Ollama server +
the on-disk ChromaDB store, exactly like auto_memory. Fire-and-forget on the
write side so a learning hiccup never slows or breaks a reply.
"""

from __future__ import annotations

import logging

from orchestrator import config
from orchestrator.memory.store import MemoryStore
from orchestrator.models.ollama_client import OllamaClient

logger = logging.getLogger("zero_agent.lessons")

# Tag stored on lesson memories so recall can tell them apart from plain facts.
LESSON_TAG = "lesson"
# Marker so the injected lessons system-message can be found + refreshed each
# turn (mirrors auto_memory's MEMORY_MARK), and never duplicated.
LESSON_MARK = "[[ZERO_LESSONS]]"

_LESSON_PROMPT = (
    "The exchange below is one where the agent HIT ERRORS or failed to complete "
    "the task. Extract at most 2 short, GENERAL lessons to follow next time so "
    "the same mistake is avoided. Format each lesson on its own line as:\n"
    "  <what went wrong> -> <what to do instead>\n"
    "Rules:\n"
    "- Make each lesson reusable and tool/behavior-oriented, NOT specific to this "
    "exact text (e.g. 'launching a server via execute_terminal_command blocks -> "
    "use launch_program for long-running processes').\n"
    "- One line each, no numbering, no commentary.\n"
    "- If there is no useful general lesson, output exactly: NONE\n\n"
    "User request: {user}\n"
    "Agent result: {assistant}\n"
    "Failures observed during the run:\n{failures}"
)


def list_lessons(store: MemoryStore, limit: int = 50) -> str:
    """Return a human-readable list of everything the agent has learned.

    Phase 3 (transparency): surfaces the lessons to the USER so they can see the
    agent's accumulated friction and act on it (fix/add a tool) — human-in-the-
    loop self-improvement, not the agent editing its own code.
    """
    items = store.list_by_tag(LESSON_TAG, limit=limit)
    if not items:
        return "No lessons learned yet — the agent hasn't hit (and distilled) any failures."
    lines = "\n".join(f"{i}. {it['text']}" for i, it in enumerate(items, 1))
    return f"🧠 Lessons the agent has learned ({len(items)}):\n{lines}"


def inject_lessons_message(history: list[dict], block: str | None) -> None:
    """Refresh the single lessons system-message inside ``history`` in place.

    Mirrors ``auto_memory.inject_memory_message``: drops any previous lessons
    block, then inserts the new one (if any) right after the main system prompt
    so it is always current and never duplicated.
    """
    history[:] = [
        m
        for m in history
        if not (
            m.get("role") == "system"
            and str(m.get("content", "")).startswith(LESSON_MARK)
        )
    ]
    if not block:
        return
    insert_at = 1 if history and history[0].get("role") == "system" else 0
    history.insert(insert_at, {"role": "system", "content": block})


async def recall_lessons(store: MemoryStore, query: str) -> str | None:
    """Return a system-prompt block of relevant past lessons, or ``None``.

    Over-fetches then keeps only ``lesson``-tagged memories at/above the recall
    threshold, so lessons surface without being crowded out by ordinary facts.
    """
    try:
        k = config.MEMORY_RECALL_K
        hits = await store.recall(query, k=max(k * 4, 12))
    except Exception:  # noqa: BLE001 - learning must never break a chat turn
        logger.exception("lesson recall failed")
        return None

    good = [
        h
        for h in hits
        if LESSON_TAG in (h.get("tags") or "")
        and h.get("score", 0) >= config.MEMORY_RECALL_MIN_SCORE
    ][: config.MEMORY_RECALL_K]
    if not good:
        return None

    lines = "\n".join(f"- {h['text']}" for h in good)
    return (
        f"{LESSON_MARK}\nLessons you learned from past mistakes — follow them to "
        f"avoid repeating the same errors (do not mention this list):\n{lines}"
    )


async def distill_lessons(
    distiller: OllamaClient,
    store: MemoryStore,
    user_msg: str,
    assistant_msg: str,
    failures: str,
) -> None:
    """Turn one failed exchange into durable lesson(s). Fire-and-forget.

    Only runs when ``failures`` is non-empty (the caller passes a summary of the
    errors/timeouts/iteration-caps seen during the run). All errors are
    swallowed/logged so a learning hiccup never surfaces to the user.
    """
    failures = (failures or "").strip()
    if not failures:
        return  # nothing went wrong → nothing to learn

    prompt = _LESSON_PROMPT.format(
        user=(user_msg or "").strip()[:1500],
        assistant=(assistant_msg or "").strip()[:1500],
        failures=failures[:1500],
    )
    try:
        resp = await distiller.chat(
            [{"role": "user", "content": prompt}], options={"temperature": 0.0}
        )
    except Exception:  # noqa: BLE001
        logger.exception("lesson distillation chat failed")
        return

    raw = (resp.message.content or "").strip()
    if not raw or raw.upper().startswith("NONE"):
        return

    for line in raw.splitlines():
        lesson = line.strip().lstrip("-*0123456789. ").strip()
        if len(lesson) < 8 or lesson.upper() == "NONE":
            continue
        try:
            # Skip a near-duplicate lesson already stored.
            existing = await store.recall(lesson, k=1)
            if existing and existing[0].get("score", 0) >= 0.93:
                continue
            await store.remember(lesson, tags=LESSON_TAG)
            logger.info("learned lesson: %s", lesson[:90])
        except Exception:  # noqa: BLE001
            logger.exception("failed to store lesson")
