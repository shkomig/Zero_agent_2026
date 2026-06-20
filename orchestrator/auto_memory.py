"""Self-improving long-term memory glue for the Chainlit UI.

Two halves:

* :func:`recall_memories` — before a turn, semantically search stored memories
  for anything relevant to the new user message and return a short system-prompt
  block the agent can read ("things you remember about the user").
* :func:`distill_and_store` — after a turn, a small fast model extracts durable
  facts/preferences from the exchange and saves them to vector memory. Run it as
  a background task so it never slows the visible reply.

Everything is local: embeddings + distillation both run on the local Ollama
server, storage is the on-disk ChromaDB store.
"""

from __future__ import annotations

import logging

from orchestrator import config
from orchestrator.memory.store import MemoryStore
from orchestrator.models.ollama_client import OllamaClient

logger = logging.getLogger("zero_agent.auto_memory")

# Marker so we can find + refresh the injected memory message each turn instead
# of stacking a new one every time.
MEMORY_MARK = "[[ZERO_MEMORY]]"
# Separate marker for STANDING INSTRUCTIONS ("rules") the user wants applied to
# every future answer. Unlike facts (recalled by similarity), rules are injected
# on EVERY turn regardless of the topic, so a "always cite sources" preference
# reliably applies. Tagged ``preference`` in the store.
RULES_MARK = "[[ZERO_RULES]]"
PREFERENCE_TAG = "preference"

# One distiller call per turn returns BOTH groups (keeping it to a single call
# matters under OLLAMA_MAX_LOADED_MODELS=1, where each call may swap the model):
#   FACTS — durable facts about the user (recalled later by similarity)
#   RULES — standing instructions about HOW to answer (always injected)
_DISTILL_PROMPT = (
    "From the exchange below, extract two things about the USER.\n\n"
    "FACTS: durable facts worth remembering for future chats — their name, "
    "hardware, projects, recurring tasks, decisions. Skip small talk and "
    "one-off questions.\n"
    "RULES: standing instructions the user wants applied to ALL FUTURE answers, "
    "i.e. feedback on HOW you should respond (e.g. 'always cite sources', 'be "
    "concise', 'answer in Hebrew', 'show a table'). Only include a rule if the "
    "user actually expressed such a preference in THIS exchange; phrase each as a "
    "short imperative.\n\n"
    "Output EXACTLY this format (use NONE for an empty section):\n"
    "FACTS:\n- <fact>\n"
    "RULES:\n- <rule>\n\n"
    "At most 3 lines per section. No commentary.\n\n"
    "User: {user}\n"
    "Assistant: {assistant}"
)


def _parse_sections(raw: str) -> tuple[list[str], list[str]]:
    """Split the distiller output into (facts, rules) lists."""
    facts: list[str] = []
    rules: list[str] = []
    bucket: list[str] | None = None
    for line in raw.splitlines():
        stripped = line.strip()
        head = stripped.upper().rstrip(":")
        if head == "FACTS":
            bucket = facts
            continue
        if head == "RULES":
            bucket = rules
            continue
        item = stripped.lstrip("-*0123456789. ").strip()
        if not item or item.upper() == "NONE" or len(item) < 4:
            continue
        if bucket is not None:
            bucket.append(item)
    return facts, rules


async def recall_memories(store: MemoryStore, query: str) -> str | None:
    """Return a system-prompt block of relevant memories, or ``None``.

    Only memories scoring at/above the configured threshold are included.
    """
    try:
        hits = await store.recall(query, k=config.MEMORY_RECALL_K)
    except Exception:  # noqa: BLE001 - memory must never break a chat turn
        logger.exception("recall failed")
        return None

    good = [h for h in hits if h.get("score", 0) >= config.MEMORY_RECALL_MIN_SCORE]
    if not good:
        return None

    lines = "\n".join(f"- {h['text']}" for h in good)
    return (
        f"{MEMORY_MARK}\nThings you remember about the user from past "
        f"conversations (use them when relevant, do not mention this list):\n{lines}"
    )


def _inject_marked(history: list[dict], mark: str, block: str | None) -> None:
    """Refresh the single system-message starting with ``mark`` in ``history``.

    Removes any previous block with that marker, then inserts the new one (if
    any) right after the main system prompt so it is always current, never
    duplicated, and survives turns (BaseAgent.run keeps "[[" system messages).
    """
    history[:] = [
        m
        for m in history
        if not (
            m.get("role") == "system"
            and str(m.get("content", "")).startswith(mark)
        )
    ]
    if not block:
        return
    insert_at = 1 if history and history[0].get("role") == "system" else 0
    history.insert(insert_at, {"role": "system", "content": block})


def inject_memory_message(history: list[dict], block: str | None) -> None:
    """Refresh the single recalled-memory system-message inside ``history``."""
    _inject_marked(history, MEMORY_MARK, block)


def recall_preferences(store: MemoryStore) -> str | None:
    """Return the always-on block of standing instructions, or ``None``.

    Unlike :func:`recall_memories`, this is NOT similarity-gated — every stored
    preference/rule is injected on every turn so it reliably applies. Capped to
    the most recent ~15 so a long-lived agent doesn't bloat the context.
    """
    try:
        rules = store.list_by_tag(PREFERENCE_TAG, limit=15)
    except Exception:  # noqa: BLE001 - never break a turn over preferences
        logger.exception("preference recall failed")
        return None
    if not rules:
        return None
    lines = "\n".join(f"- {r['text']}" for r in rules)
    return (
        f"{RULES_MARK}\nStanding instructions from the user — ALWAYS follow these "
        f"in every answer (do not mention this list):\n{lines}"
    )


def inject_preferences_message(history: list[dict], block: str | None) -> None:
    """Refresh the single standing-instructions system-message in ``history``."""
    _inject_marked(history, RULES_MARK, block)


async def distill_and_store(
    distiller: OllamaClient,
    store: MemoryStore,
    user_msg: str,
    assistant_msg: str,
) -> None:
    """Extract durable facts from one exchange and persist novel ones.

    Designed to be launched via ``asyncio.create_task`` (fire-and-forget). All
    errors are swallowed/logged so a memory hiccup never surfaces to the user.
    """
    user_msg = (user_msg or "").strip()
    assistant_msg = (assistant_msg or "").strip()
    if not user_msg:
        return

    prompt = _DISTILL_PROMPT.format(user=user_msg[:2000], assistant=assistant_msg[:2000])
    try:
        resp = await distiller.chat(
            [{"role": "user", "content": prompt}],
            options={"temperature": 0.0},
        )
    except Exception:  # noqa: BLE001
        logger.exception("distillation chat failed")
        return

    raw = (resp.message.content or "").strip()
    if not raw:
        return

    facts, rules = _parse_sections(raw)

    async def _store(items: list[str], *, tag: str, dedupe: float) -> None:
        for text in items:
            try:
                # Skip near-duplicates already in memory.
                existing = await store.recall(text, k=1)
                if existing and existing[0].get("score", 0) >= dedupe:
                    continue
                await store.remember(text, tags=tag)
                logger.info("stored [%s]: %s", tag, text[:80])
            except Exception:  # noqa: BLE001
                logger.exception("failed to store distilled %s", tag)

    await _store(facts, tag="auto", dedupe=0.95)
    # Rules dedupe a bit looser so paraphrased repeats of the same instruction
    # ("be concise" / "keep it short") don't pile up.
    await _store(rules, tag=PREFERENCE_TAG, dedupe=0.9)
