"""Memory hygiene — the idle "sleep" consolidation pass (ROADMAP Tier-2 #3).

Left alone, ChromaDB becomes a junk drawer: the same fact distilled five different
ways, stale facts that are no longer true, contradictions. The agent then recalls
them and is "data-grounded" confidently wrong. This pass manages memory like an
updating store, not an append-only log:

  * DEDUP — within the same tag, near-identical memories (cosine >= threshold) are
    collapsed to the NEWEST one; the older copies are deleted.
  * DECAY — optionally drop auto-distilled FACTS older than a TTL (off by default).
    Never touches user RULES/preferences or lessons.
  * CONTRADICTION — two same-tag facts that are topically related but state OPPOSITE
    things ("name is Khai" vs "name is Dan"). Embeddings can't tell "same" from
    "opposite", so a small local LLM judges candidate pairs in the gray band
    [SIM_LOW, DEDUP_THRESHOLD); on a clear contradiction the OLDER memory is dropped
    (the newer supersedes). Conservative: protected tags (user RULES + lessons) are
    never auto-removed, only a clear YES deletes, any judge error keeps both, and the
    per-pass LLM budget is capped.

The selection logic is split into PURE functions (`_find_duplicates`,
`_find_expired`, `_find_contradiction_candidates`, `_contradiction_removals`) that
take plain dicts + embeddings + canned verdicts, so they're tested deterministically
without Ollama or a live store. The only non-pure piece is the injected LLM `judge`;
`consolidate()` is the thin wrapper that reads the store, runs them, and deletes —
and never raises.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Callable
from typing import Any

from orchestrator import config

logger = logging.getLogger("zero_agent.memory_hygiene")

# A judge takes a list of (newer_text, older_text) pairs and returns one verdict per
# pair: True ⇢ the older statement CONTRADICTS the newer. Injected so the selection
# logic stays pure/testable; the real one wraps a local Ollama call.
ContradictionJudge = Callable[[list[tuple[str, str]]], list[bool]]


def _cosine(a: list[float] | None, b: list[float] | None) -> float:
    """Cosine similarity of two vectors; 0.0 if either is missing/degenerate."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _find_duplicates(items: list[dict[str, Any]], threshold: float) -> set[str]:
    """Ids to delete as near-duplicates, keeping the NEWEST in each cluster.

    Only memories sharing the same ``tags`` are compared (a fact and a lesson that
    happen to embed similarly are not merged). Items without an embedding are never
    flagged. Pure — no I/O.
    """
    # Newest first, so the first representative we keep in a cluster is the newest.
    order = sorted(items, key=lambda x: x.get("created", 0) or 0, reverse=True)
    kept: list[dict[str, Any]] = []
    remove: set[str] = set()
    for it in order:
        if it.get("embedding") is None:
            kept.append(it)  # can't compare → always keep
            continue
        dup = False
        for rep in kept:
            if rep.get("embedding") is None or rep.get("tags", "") != it.get("tags", ""):
                continue
            if _cosine(rep["embedding"], it["embedding"]) >= threshold:
                dup = True
                break
        if dup:
            remove.add(it["id"])  # older copy of an already-kept newer memory
        else:
            kept.append(it)
    return remove


def _find_expired(
    items: list[dict[str, Any]], ttl_days: int, decay_tags: list[str], now: float | None = None
) -> set[str]:
    """Ids of decay-eligible memories older than the TTL. Empty when ttl_days<=0."""
    if ttl_days <= 0:
        return set()
    now = time.time() if now is None else now
    cutoff = ttl_days * 86400
    tags = set(decay_tags)
    return {
        it["id"]
        for it in items
        if it.get("tags", "") in tags and (now - (it.get("created", 0) or 0)) > cutoff
    }


def _find_contradiction_candidates(
    items: list[dict[str, Any]],
    low: float,
    high: float,
    protected: set[str],
    *,
    exclude: set[str] | None = None,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Same-tag pairs in the gray band ``[low, high)`` that MIGHT contradict.

    Returns ``(newer, older)`` pairs sorted by cosine similarity DESC (the most
    likely contradictions first, so a capped LLM budget is spent best). Pure — no
    I/O. Pairs with a protected tag, a missing embedding, or an id in ``exclude``
    (already slated for deletion) are skipped. Each older memory appears at most
    once (paired with its single best newer match) so one fact isn't deleted twice.
    """
    exclude = exclude or set()
    usable = [
        it for it in items
        if it.get("embedding") is not None
        and it.get("id") not in exclude
        and it.get("tags", "") not in protected
    ]
    # Newest first → the first element of any pair is always the newer one.
    order = sorted(usable, key=lambda x: x.get("created", 0) or 0, reverse=True)
    best: dict[str, tuple[float, dict[str, Any], dict[str, Any]]] = {}
    for i, newer in enumerate(order):
        for older in order[i + 1:]:
            if newer.get("tags", "") != older.get("tags", ""):
                continue
            sim = _cosine(newer["embedding"], older["embedding"])
            if not (low <= sim < high):
                continue
            oid = older["id"]
            if oid not in best or sim > best[oid][0]:
                best[oid] = (sim, newer, older)
    ranked = sorted(best.values(), key=lambda t: t[0], reverse=True)
    return [(newer, older) for _sim, newer, older in ranked]


def _contradiction_removals(
    candidates: list[tuple[dict[str, Any], dict[str, Any]]],
    verdicts: list[bool],
) -> set[str]:
    """Ids to delete: the OLDER memory of each pair the judge marked True. Pure."""
    remove: set[str] = set()
    for (_newer, older), is_contradiction in zip(candidates, verdicts):
        if is_contradiction:
            remove.add(older["id"])
    return remove


_JUDGE_PROMPT = (
    "Decide if two stored facts about the user DIRECTLY CONTRADICT each other.\n"
    "A CONTRADICTION means they describe the SAME attribute of the SAME thing with "
    "MUTUALLY EXCLUSIVE values, so both cannot be true at once — e.g. name is Dan vs "
    "name is Sam; prefers NSFW vs prefers non-NSFW; status active vs status closed.\n"
    "It is NOT a contradiction when the two facts:\n"
    "  - restate or refine the same thing in different words (same value), or\n"
    "  - describe DIFFERENT things/topics/projects that can both be true at once "
    "(e.g. a race-car project AND a fighter-jet project; likes tea AND likes coffee), or\n"
    "  - are simply unrelated.\n"
    "When unsure, answer NO (keep both). Reply with ONE word only: YES or NO.\n\n"
    "FACT A: {newer}\nFACT B: {older}\n\nDo A and B directly contradict (same attribute, "
    "mutually exclusive values)? Answer YES or NO."
)


def make_contradiction_judge(
    client: Any = None, model: str | None = None
) -> ContradictionJudge:
    """Build the real LLM judge: a SYNC callable that runs all pairs on one fresh
    event loop (so it works inside the worker thread `consolidate` runs in, with no
    httpx-loop-binding issues). Conservative: any error → ``False`` (keep both).

    Pass an existing ``client`` to reuse it, or a ``model`` name to spin up a small
    dedicated client (e.g. the fast distiller model); one of the two is required."""
    from orchestrator.models.ollama_client import OllamaClient

    if client is None and model is None:
        model = config.MEMORY_CONTRADICTION_MODEL
    judge_client = client if model is None else OllamaClient(model=model)

    async def _run(pairs: list[tuple[str, str]]) -> list[bool]:
        out: list[bool] = []
        for newer, older in pairs:
            prompt = _JUDGE_PROMPT.format(newer=newer.strip()[:600], older=older.strip()[:600])
            try:
                resp = await judge_client.chat(
                    [{"role": "user", "content": prompt}], options={"temperature": 0.0}
                )
                ans = (resp.message.content or "").strip().upper()
                # Only a clear YES deletes; thinking models may prepend tokens, so
                # look for an explicit YES while guarding against "NO"/"NOT".
                out.append("YES" in ans and "NO" != ans[:2] and not ans.startswith("NOT"))
            except Exception:  # noqa: BLE001 - never delete on a judging hiccup
                logger.exception("contradiction judge chat failed")
                out.append(False)
        return out

    def judge(pairs: list[tuple[str, str]]) -> list[bool]:
        if not pairs:
            return []
        try:
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(_run(pairs))
            finally:
                loop.close()
        except Exception:  # noqa: BLE001
            logger.exception("contradiction judge loop failed")
            return [False] * len(pairs)

    return judge


def consolidate(
    store: Any, *, dry_run: bool = False, judge: ContradictionJudge | None = None
) -> dict[str, int]:
    """Run the hygiene pass over the whole memory store.

    Returns counts: examined, removed_duplicates, removed_expired,
    removed_contradictions, remaining. Never raises — a hygiene hiccup must never
    break the app; on error it returns zeros. With ``dry_run`` it computes what WOULD
    be removed but deletes nothing. When a ``judge`` is supplied (and
    ``MEMORY_CONTRADICTION`` is on), gray-band same-tag pairs are LLM-checked and the
    older of any contradicting pair is dropped; without a judge that phase is skipped.
    """
    result = {
        "examined": 0, "removed_duplicates": 0, "removed_expired": 0,
        "removed_contradictions": 0, "remaining": 0,
    }
    try:
        items = store.all_items(with_embeddings=True)
    except Exception:  # noqa: BLE001
        logger.exception("consolidate: could not read memories")
        return result
    result["examined"] = len(items)
    if not items:
        return result

    dups = _find_duplicates(items, config.MEMORY_DEDUP_THRESHOLD)
    # Don't double-count: expired ids exclude ones already slated as duplicates.
    expired = _find_expired(items, config.MEMORY_TTL_DAYS, config.MEMORY_DECAY_TAGS) - dups

    # Contradictions: LLM-judged, only when a judge is given and the feature is on.
    contradictions: set[str] = set()
    if judge is not None and config.MEMORY_CONTRADICTION:
        candidates = _find_contradiction_candidates(
            items,
            config.MEMORY_CONTRADICTION_SIM_LOW,
            config.MEMORY_DEDUP_THRESHOLD,
            set(config.MEMORY_PROTECTED_TAGS),
            exclude=dups | expired,
        )[: max(0, config.MEMORY_CONTRADICTION_MAX_PAIRS)]
        if candidates:
            try:
                verdicts = judge([(n["text"] or "", o["text"] or "") for n, o in candidates])
                contradictions = _contradiction_removals(candidates, verdicts) - dups - expired
            except Exception:  # noqa: BLE001 - judging must never break the pass
                logger.exception("consolidate: contradiction phase failed")

    to_delete = list(dups | expired | contradictions)
    if to_delete and not dry_run:
        try:
            store.delete_ids(to_delete)
        except Exception:  # noqa: BLE001
            logger.exception("consolidate: delete failed")
            return result

    result["removed_duplicates"] = len(dups)
    result["removed_expired"] = len(expired)
    result["removed_contradictions"] = len(contradictions)
    result["remaining"] = len(items) - len(to_delete)
    logger.info(
        "memory hygiene%s: examined=%d dedup=%d decayed=%d contradictions=%d remaining=%d",
        " (dry-run)" if dry_run else "", result["examined"],
        result["removed_duplicates"], result["removed_expired"],
        result["removed_contradictions"], result["remaining"],
    )
    return result
