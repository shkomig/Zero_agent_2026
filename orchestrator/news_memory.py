"""News dedup memory — prevent re-reporting the same news item.

Stores a set of seen item keys (URL or text hash) in a JSON file under
data/news_seen.json. Each entry carries a timestamp so old entries can be
cleaned up automatically (default TTL: 48 hours).

Used by:
  telegram_reader.read_channel_messages()  — filters already-seen messages
  rss_tools.read_rss_feeds()               — filters already-seen RSS items

Flow per digest run:
  1. Fetch raw items from source (Telegram / RSS)
  2. Filter out keys already in the store  → only NEW items remain
  3. Mark the new items as seen             → next run skips them
  4. Return filtered list to the LLM

TTL of 48 h means a big story that runs two days will only appear once
on day 1 and once on day 3 (after the entry expires).
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("zero_agent.news_memory")

_STORE_PATH = Path(__file__).parent.parent / "data" / "news_seen.json"
_DEFAULT_TTL_HOURS = 48


def _load() -> dict[str, float]:
    """Load store from disk. Returns {key: timestamp}."""
    try:
        if _STORE_PATH.exists():
            return json.loads(_STORE_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save(store: dict[str, float]) -> None:
    _STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        _STORE_PATH.write_text(json.dumps(store, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        logger.warning("news_memory: could not save store: %s", exc)


def _key(url: str, text: str = "") -> str:
    """Canonical key: URL if available, else sha1 of first 120 chars of text."""
    if url and url.startswith("http"):
        return url
    return "text:" + hashlib.sha1((text or "")[:120].encode()).hexdigest()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def cleanup(ttl_hours: int = _DEFAULT_TTL_HOURS) -> int:
    """Remove entries older than ttl_hours. Returns number removed."""
    store = _load()
    cutoff = time.time() - ttl_hours * 3600
    old_keys = [k for k, ts in store.items() if ts < cutoff]
    for k in old_keys:
        del store[k]
    if old_keys:
        _save(store)
    return len(old_keys)


def filter_new(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return only items not yet seen. Does NOT mark them — call mark_seen after."""
    store = _load()
    new = []
    for item in items:
        k = _key(item.get("url", ""), item.get("text", ""))
        if k not in store:
            new.append(item)
    return new


def mark_seen(items: list[dict[str, Any]], ttl_hours: int = _DEFAULT_TTL_HOURS) -> None:
    """Mark items as seen (call after returning them to the agent)."""
    store = _load()
    now = time.time()
    for item in items:
        k = _key(item.get("url", ""), item.get("text", ""))
        store[k] = now
    # cleanup while we have the store open
    cutoff = now - ttl_hours * 3600
    store = {k: ts for k, ts in store.items() if ts >= cutoff}
    _save(store)


def stats() -> dict[str, Any]:
    """Return store stats for diagnostics."""
    store = _load()
    now = time.time()
    return {
        "total": len(store),
        "fresh_24h": sum(1 for ts in store.values() if now - ts < 86400),
        "fresh_48h": sum(1 for ts in store.values() if now - ts < 172800),
        "path": str(_STORE_PATH),
    }
