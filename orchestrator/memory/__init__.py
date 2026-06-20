"""Vector-memory subsystem (Phase 3).

Gives the agent long-term memory: facts, preferences and snippets are embedded
and stored in a local ChromaDB collection, then retrieved by semantic search.
"""

from orchestrator.memory.store import MemoryStore, get_store

__all__ = ["MemoryStore", "get_store"]
