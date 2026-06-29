"""Persistent vector store backed by ChromaDB + Ollama embeddings.

The store is intentionally tiny: ``remember`` adds a note, ``recall`` does a
semantic search. Embeddings are produced by the local Ollama server (no cloud),
and ChromaDB persists everything to disk so memories survive restarts.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

import chromadb

from orchestrator import config
from orchestrator.models.ollama_client import OllamaClient

logger = logging.getLogger("zero_agent.memory")

_COLLECTION = "zero_agent_memory"


class MemoryStore:
    """A thin async facade over a persistent ChromaDB collection.

    Embeddings are computed via the shared :class:`OllamaClient` so the same
    local model server powers both chat and memory. We pass precomputed vectors
    to Chroma (``embeddings=...``) instead of letting Chroma call out, keeping
    everything offline and consistent.
    """

    def __init__(self, client: OllamaClient, *, persist_dir: str | None = None) -> None:
        self._client = client
        self._db = chromadb.PersistentClient(path=persist_dir or config.MEMORY_DIR)
        self._collection = self._db.get_or_create_collection(
            name=_COLLECTION,
            metadata={"hnsw:space": "cosine"},
        )

    async def remember(self, text: str, *, tags: str = "") -> str:
        """Embed ``text`` and store it. Returns the new memory id."""
        text = text.strip()
        if not text:
            raise ValueError("cannot remember empty text")

        vectors = await self._client.embed([text])
        if not vectors:
            raise RuntimeError("embedding failed: no vector returned")

        mem_id = uuid.uuid4().hex[:12]
        metadata: dict[str, Any] = {"created": time.time()}
        if tags.strip():
            metadata["tags"] = tags.strip()

        self._collection.add(
            ids=[mem_id],
            embeddings=[vectors[0]],
            documents=[text],
            metadatas=[metadata],
        )
        logger.info("remembered id=%s len=%d tags=%r", mem_id, len(text), tags)
        return mem_id

    async def recall(self, query: str, *, k: int | None = None) -> list[dict[str, Any]]:
        """Semantic search. Returns up to ``k`` memories ordered by relevance."""
        query = query.strip()
        if not query:
            return []

        count = self._collection.count()
        if count == 0:
            return []

        top_k = min(k or config.MEMORY_TOP_K, count)
        vectors = await self._client.embed([query])
        if not vectors:
            raise RuntimeError("embedding failed: no vector returned")

        res = self._collection.query(
            query_embeddings=[vectors[0]],
            n_results=top_k,
        )

        docs = (res.get("documents") or [[]])[0]
        metas = (res.get("metadatas") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]
        ids = (res.get("ids") or [[]])[0]

        hits: list[dict[str, Any]] = []
        for doc, meta, dist, mid in zip(docs, metas, dists, ids):
            # cosine distance -> similarity score in [0, 1].
            score = round(1.0 - float(dist), 3)
            hits.append(
                {
                    "id": mid,
                    "text": doc,
                    "tags": (meta or {}).get("tags", ""),
                    "score": score,
                }
            )
        logger.info("recall query=%r hits=%d", query[:40], len(hits))
        return hits

    def list_by_tag(self, tag: str, *, limit: int = 100) -> list[dict[str, Any]]:
        """Return stored memories whose ``tags`` exactly match ``tag``.

        Non-semantic (no embedding needed) — used to show the user everything the
        agent has learned/remembered of a given kind (e.g. all ``lesson`` notes).
        Newest first.
        """
        try:
            res = self._collection.get(where={"tags": tag}, limit=limit)
        except Exception:  # noqa: BLE001 - bad/empty filter -> nothing to show
            return []
        docs = res.get("documents") or []
        metas = res.get("metadatas") or []
        ids = res.get("ids") or []
        items = [
            {"id": i, "text": d, "created": (m or {}).get("created", 0)}
            for i, d, m in zip(ids, docs, metas)
        ]
        items.sort(key=lambda x: x["created"], reverse=True)
        return items

    def delete_by_tag(self, tag: str) -> int:
        """Delete all memories with exactly this ``tag``. Returns how many."""
        try:
            existing = self._collection.get(where={"tags": tag})
            ids = existing.get("ids") or []
            if ids:
                self._collection.delete(ids=ids)
            logger.info("deleted %d memories with tag=%r", len(ids), tag)
            return len(ids)
        except Exception:  # noqa: BLE001 - nothing to delete / bad filter
            logger.exception("delete_by_tag failed for tag=%r", tag)
            return 0

    def all_items(self, *, with_embeddings: bool = True) -> list[dict[str, Any]]:
        """Every stored memory as {id, text, tags, created[, embedding]}.

        Used by the memory-hygiene pass to dedup / decay the whole collection.
        Embeddings are returned straight from Chroma (no re-embedding, no Ollama),
        so this is cheap and offline.
        """
        include = ["documents", "metadatas"] + (["embeddings"] if with_embeddings else [])
        try:
            res = self._collection.get(include=include)
        except Exception:  # noqa: BLE001
            logger.exception("all_items failed")
            return []
        ids = res.get("ids") or []
        docs = res.get("documents") or []
        metas = res.get("metadatas") or []
        embs = res.get("embeddings")
        if embs is None:
            embs = [None] * len(ids)
        items: list[dict[str, Any]] = []
        for i, d, m, e in zip(ids, docs, metas, embs):
            meta = m or {}
            item = {
                "id": i,
                "text": d,
                "tags": meta.get("tags", ""),
                "created": meta.get("created", 0),
            }
            if with_embeddings:
                item["embedding"] = list(e) if e is not None else None
            items.append(item)
        return items

    def delete_ids(self, ids: list[str]) -> int:
        """Delete memories by id. Returns how many were requested for deletion."""
        ids = [i for i in ids if i]
        if not ids:
            return 0
        try:
            self._collection.delete(ids=ids)
            logger.info("deleted %d memories by id", len(ids))
            return len(ids)
        except Exception:  # noqa: BLE001
            logger.exception("delete_ids failed")
            return 0

    def count(self) -> int:
        """Total number of stored memories."""
        return self._collection.count()


# Process-wide singleton, created lazily so importing the module is cheap and
# does not require Ollama to be up.
_store: MemoryStore | None = None


def get_store(client: OllamaClient | None = None) -> MemoryStore:
    """Return the shared :class:`MemoryStore`, creating it on first use.

    Args:
        client: Ollama client to embed with. Required on the first call; reused
            automatically afterwards.
    """
    global _store
    if _store is None:
        if client is None:
            client = OllamaClient()
        _store = MemoryStore(client)
    return _store
