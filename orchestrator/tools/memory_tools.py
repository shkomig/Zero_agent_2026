"""Memory tools: let the agent persist and recall knowledge across sessions.

These wrap the :class:`~orchestrator.memory.store.MemoryStore` so the model can
choose to ``remember`` an important fact and later ``recall`` it by meaning,
not just exact words.
"""

from __future__ import annotations

from orchestrator.memory import get_store
from orchestrator.tools.registry import registry


@registry.register
async def remember(text: str, tags: str = "") -> str:
    """Save an important fact, preference, or note to long-term memory.

    Use this whenever the user shares something worth recalling in future
    conversations (a preference, a decision, a name, a fact about a project).

    Args:
        text: The information to store, written as a self-contained sentence.
        tags: Optional comma-separated labels to categorize the memory.
    """
    store = get_store()
    mem_id = await store.remember(text, tags=tags)
    return f"Saved to memory (id={mem_id})."


@registry.register
async def recall(query: str, k: int = 5) -> str:
    """Search long-term memory for information relevant to a query.

    Use this at the start of a task to retrieve anything you previously learned
    about the user or project before answering.

    Args:
        query: What to look for, described in natural language.
        k: Maximum number of memories to return (default 5).
    """
    store = get_store()
    hits = await store.recall(query, k=k)
    if not hits:
        return "No relevant memories found."

    lines = []
    for i, hit in enumerate(hits, 1):
        tags = f" [{hit['tags']}]" if hit.get("tags") else ""
        lines.append(f"{i}. (score {hit['score']}{tags}) {hit['text']}")
    return "\n".join(lines)
