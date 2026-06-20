"""Projects — named, persistent knowledge bases (ChatGPT-Projects style).

A *project* groups a body of knowledge under a name (e.g. "Trading System",
"Kids Reading App"). Everything you add to a project is embedded and stored
locally in ChromaDB; whenever that project is *active*, the most relevant pieces
are recalled and handed to the agent each turn, so it "always remembers" the
project's content — even across brand-new chats.

This keeps long, information-rich context available without stuffing the whole
body into every prompt: we semantically recall just the relevant chunks.

100% local: embeddings run on the local Ollama server, storage is on disk.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import Any

import chromadb

from orchestrator import config
from orchestrator.models.ollama_client import OllamaClient

logger = logging.getLogger("zero_agent.projects")

_COLLECTION = "zero_agent_projects"

# Tiny file remembering the last active project so new chats default to it.
_ACTIVE_FILE = os.path.join(config.MEMORY_DIR, "active_project.json")


class ProjectStore:
    """Persistent, per-project knowledge base backed by ChromaDB + Ollama.

    Each stored chunk carries a ``project`` metadata field so projects share one
    collection but stay isolated via metadata filtering.
    """

    def __init__(self, client: OllamaClient, *, persist_dir: str | None = None) -> None:
        self._client = client
        self._db = chromadb.PersistentClient(path=persist_dir or config.MEMORY_DIR)
        self._collection = self._db.get_or_create_collection(
            name=_COLLECTION,
            metadata={"hnsw:space": "cosine"},
        )

    # --- knowledge --------------------------------------------------------
    async def add_knowledge(self, project: str, text: str) -> str:
        """Embed ``text`` and store it under ``project``. Returns the chunk id."""
        project = project.strip()
        text = text.strip()
        if not project:
            raise ValueError("project name is required")
        if not text:
            raise ValueError("cannot add empty knowledge")

        vectors = await self._client.embed([text])
        if not vectors:
            raise RuntimeError("embedding failed: no vector returned")

        chunk_id = uuid.uuid4().hex[:12]
        self._collection.add(
            ids=[chunk_id],
            embeddings=[vectors[0]],
            documents=[text],
            metadatas=[{"project": project, "created": time.time()}],
        )
        logger.info("project %r += chunk id=%s len=%d", project, chunk_id, len(text))
        return chunk_id

    async def recall(
        self, project: str, query: str, *, k: int | None = None
    ) -> list[dict[str, Any]]:
        """Semantic search within a single project."""
        project = project.strip()
        query = query.strip()
        if not project or not query:
            return []

        top_k = k or config.PROJECT_RECALL_K
        vectors = await self._client.embed([query])
        if not vectors:
            return []

        res = self._collection.query(
            query_embeddings=[vectors[0]],
            n_results=top_k,
            where={"project": project},
        )
        docs = (res.get("documents") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]
        ids = (res.get("ids") or [[]])[0]

        hits: list[dict[str, Any]] = []
        for doc, dist, cid in zip(docs, dists, ids):
            hits.append(
                {"id": cid, "text": doc, "score": round(1.0 - float(dist), 3)}
            )
        return hits

    # --- project management ----------------------------------------------
    def list_projects(self) -> list[str]:
        """Return all distinct project names, sorted."""
        data = self._collection.get(include=["metadatas"])
        names = {
            (m or {}).get("project")
            for m in (data.get("metadatas") or [])
            if (m or {}).get("project")
        }
        return sorted(n for n in names if n)

    def count(self, project: str) -> int:
        """Number of knowledge chunks in a project."""
        data = self._collection.get(where={"project": project.strip()})
        return len(data.get("ids") or [])

    def delete_project(self, project: str) -> int:
        """Delete all knowledge for a project. Returns chunks removed."""
        project = project.strip()
        data = self._collection.get(where={"project": project})
        ids = data.get("ids") or []
        if ids:
            self._collection.delete(ids=ids)
        logger.info("deleted project %r (%d chunks)", project, len(ids))
        return len(ids)


# --- active-project state (persists last selection across restarts) -------
def get_active_project() -> str | None:
    """Return the last active project name, or ``None``."""
    try:
        with open(_ACTIVE_FILE, "r", encoding="utf-8") as fh:
            return (json.load(fh) or {}).get("active") or None
    except (OSError, json.JSONDecodeError):
        return None


def set_active_project(name: str | None) -> None:
    """Persist the active project so new chats default to it."""
    os.makedirs(os.path.dirname(_ACTIVE_FILE), exist_ok=True)
    try:
        with open(_ACTIVE_FILE, "w", encoding="utf-8") as fh:
            json.dump({"active": name or ""}, fh)
    except OSError:
        logger.exception("failed to persist active project")


# Process-wide singleton, created lazily.
_store: ProjectStore | None = None


def get_project_store(client: OllamaClient | None = None) -> ProjectStore:
    """Return the shared :class:`ProjectStore`, creating it on first use."""
    global _store
    if _store is None:
        if client is None:
            client = OllamaClient()
        _store = ProjectStore(client)
    return _store


def build_project_block(project: str, hits: list[dict[str, Any]]) -> str:
    """Render recalled project knowledge as a system-prompt block."""
    lines = "\n".join(f"- {h['text']}" for h in hits)
    return (
        f"{PROJECT_MARK}\nActive project: \"{project}\". Relevant project "
        f"knowledge (use it as authoritative context, do not mention this "
        f"list):\n{lines}"
    )


# Marker so the injected project block can be found + refreshed each turn.
PROJECT_MARK = "[[ZERO_PROJECT]]"


def inject_project_message(history: list[dict], block: str | None) -> None:
    """Refresh the single project-knowledge system message in ``history``.

    Removes any previous project block, then inserts the new one (if any) right
    after the main system prompt so it is always current and never duplicated.
    """
    history[:] = [
        m
        for m in history
        if not (
            m.get("role") == "system"
            and str(m.get("content", "")).startswith(PROJECT_MARK)
        )
    ]
    if not block:
        return
    insert_at = 1 if history and history[0].get("role") == "system" else 0
    history.insert(insert_at, {"role": "system", "content": block})
