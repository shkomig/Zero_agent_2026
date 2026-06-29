"""Rolling conversation summary — compress old turns before Ollama truncates them.

The full transcript is sent to the model every turn, but Ollama silently drops
anything past the context window (`num_ctx`) — so on a long chat the agent
quietly loses its earliest context. Instead, when the running history approaches
the window we summarize the OLDEST turns into one compact `[[ZERO_SUMMARY]]`
system message and keep the most recent turns verbatim. This gives a "big
context" feel without a giant-context model, and is the highest-value item from
the context/memory roadmap (especially after num_ctx was lowered to 16384 for
VRAM safety).

Local + cheap: the summary is produced by the small distiller model, only when
the history actually grows large. Never raises — a hiccup leaves history as-is.
"""

from __future__ import annotations

import logging

from orchestrator.models.ollama_client import OllamaClient

logger = logging.getLogger("zero_agent.summarizer")

# Marker so the summary system-message is recognised, kept across turns (it
# starts with "[[", which BaseAgent.run preserves), and folded into the next
# summary instead of stacking.
SUMMARY_MARK = "[[ZERO_SUMMARY]]"

# Rough chars-per-token for the trigger heuristic (no tokenizer dependency).
_CHARS_PER_TOKEN = 4
# Summarize once the conversation text exceeds this fraction of the context
# budget, leaving room for the system prompt + the model's reply.
_TRIGGER_FRACTION = 0.6
# Keep this many most-recent conversation messages verbatim (summarize the rest).
_KEEP_RECENT = 6
# Cap the excerpt fed to the summarizer so the summarization call itself stays small.
_MAX_EXCERPT_CHARS = 8000

_SUMMARY_PROMPT = (
    "Summarize the conversation excerpt below into a compact, factual running "
    "summary that preserves what matters for continuing the conversation: "
    "decisions made, facts established, file paths / models / tools involved, the "
    "user's goals, and any unresolved tasks. Be terse and information-dense; no "
    "preamble. Output ONLY the summary.\n\nExcerpt:\n{excerpt}"
)


def _convo_chars(history: list[dict]) -> int:
    """Total characters of the non-system conversation messages."""
    return sum(
        len(str(m.get("content", "")))
        for m in history
        if m.get("role") != "system"
    )


async def maybe_summarize(
    distiller: OllamaClient, history: list[dict], num_ctx: int
) -> bool:
    """Compress the oldest turns into a running summary, in place, if needed.

    Returns True if it summarized. System messages (persona / memory / lessons /
    a previous summary) are preserved; only old conversation turns are folded
    into the new summary. Recent turns stay verbatim.
    """
    budget = int(num_ctx * _CHARS_PER_TOKEN * _TRIGGER_FRACTION)
    if _convo_chars(history) <= budget:
        return False

    system_msgs = [m for m in history if m.get("role") == "system"]
    convo = [m for m in history if m.get("role") != "system"]
    if len(convo) <= _KEEP_RECENT:
        return False  # nothing old enough to compress

    old, recent = convo[:-_KEEP_RECENT], convo[-_KEEP_RECENT:]

    prev_summary = next(
        (
            str(m.get("content", ""))
            for m in system_msgs
            if str(m.get("content", "")).startswith(SUMMARY_MARK)
        ),
        "",
    )
    excerpt = ""
    if prev_summary:
        excerpt += prev_summary + "\n\n"
    excerpt += "\n".join(
        f"{m.get('role')}: {str(m.get('content', ''))[:1500]}" for m in old
    )

    try:
        resp = await distiller.chat(
            [{"role": "user", "content": _SUMMARY_PROMPT.format(excerpt=excerpt[:_MAX_EXCERPT_CHARS])}],
            options={"temperature": 0.0},
        )
    except Exception:  # noqa: BLE001 - summarization must never break a turn
        logger.exception("summarization failed; leaving history unchanged")
        return False

    summary = (resp.message.content or "").strip()
    if not summary:
        return False

    summary_msg = {
        "role": "system",
        "content": f"{SUMMARY_MARK}\nSummary of earlier conversation:\n{summary}",
    }
    # Rebuild in place: keep non-summary system msgs, add the fresh summary, then
    # the recent verbatim turns. (BaseAgent.run re-inserts the persona and
    # refreshes memory/lessons blocks; this summary persists because it starts
    # with "[[".)
    kept_system = [
        m
        for m in system_msgs
        if not str(m.get("content", "")).startswith(SUMMARY_MARK)
    ]
    history[:] = kept_system + [summary_msg] + recent
    logger.info(
        "summarized %d old msgs -> running summary (%d chars); kept %d recent",
        len(old),
        len(summary),
        len(recent),
    )
    # Persist the summary so the NEXT session can load it and maintain continuity.
    try:
        from orchestrator import session_context as _sc
        _sc.save_last_summary(summary)
    except Exception:  # noqa: BLE001 - never break a turn over persistence
        logger.exception("failed to persist rolling summary to disk")
    return True
