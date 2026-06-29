"""RSS feed reader — read_rss_feeds tool.

Fetches items from RSS/Atom feeds and returns a digest in the same format
as read_telegram_channels, so both sources can be combined in one news report.

Default feed list covers Israeli news (Hebrew + English) and international
tech/finance. The user can override or extend via ZERO_AGENT_RSS_FEEDS in .env.

Dedup: integrates with orchestrator.news_memory — already-seen items (by URL)
are filtered out, matching the same 48 h TTL used for Telegram news.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

from orchestrator import news_memory
from orchestrator.tools.registry import registry

logger = logging.getLogger("zero_agent.rss_tools")

# ---------------------------------------------------------------------------
# Default feeds — override or extend with ZERO_AGENT_RSS_FEEDS in .env
# (comma-separated URLs)
# ---------------------------------------------------------------------------
_DEFAULT_FEEDS: list[tuple[str, str]] = [
    # Israeli news (English)
    ("Times of Israel",   "https://www.timesofisrael.com/feed/"),
    ("Jerusalem Post",    "https://www.jpost.com/Rss/RssFeedsHeadlines.aspx"),
    ("Ynet English",      "https://www.ynetnews.com/category/3082.html"),
    # International
    ("Reuters",           "https://feeds.reuters.com/reuters/topNews"),
    ("BBC News",          "http://feeds.bbci.co.uk/news/rss.xml"),
    ("AP News",           "https://rsshub.app/apnews/topics/apf-topnews"),
    # Tech
    ("TechCrunch",        "https://techcrunch.com/feed/"),
    ("The Verge",         "https://www.theverge.com/rss/index.xml"),
    ("Hacker News",       "https://hnrss.org/frontpage"),
    # Finance
    ("MarketWatch",       "https://feeds.marketwatch.com/marketwatch/topstories/"),
    ("Investing.com",     "https://www.investing.com/rss/news.rss"),
]


def _parse_date(entry: Any) -> datetime | None:
    """Try to extract a UTC datetime from a feedparser entry."""
    # feedparser parses most date fields into published_parsed (struct_time UTC)
    parsed = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if parsed:
        try:
            return datetime(*parsed[:6], tzinfo=timezone.utc)
        except Exception:
            pass
    # Fallback: raw string
    raw = getattr(entry, "published", None) or getattr(entry, "updated", None)
    if raw:
        try:
            dt = parsedate_to_datetime(raw)
            return dt.astimezone(timezone.utc)
        except Exception:
            pass
    return None


def _clean(text: str | None, max_chars: int = 500) -> str:
    if not text:
        return ""
    # Strip HTML tags (feedparser sometimes leaves summary HTML)
    import re
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_chars:
        cut = text[:max_chars]
        last_dot = cut.rfind(".")
        text = cut[: last_dot + 1] if last_dot > max_chars // 2 else cut + "…"
    return text


def _default_feeds() -> list[tuple[str, str]]:
    raw = os.getenv("ZERO_AGENT_RSS_FEEDS", "").strip()
    if raw:
        extra = []
        for item in raw.split(","):
            item = item.strip()
            if item.startswith("http"):
                extra.append(("Custom", item))
        return _DEFAULT_FEEDS + extra
    return _DEFAULT_FEEDS


# ---------------------------------------------------------------------------
# Registered tool
# ---------------------------------------------------------------------------

@registry.register
async def read_rss_feeds(
    feeds: str = "",
    hours: int = 8,
    limit_per_feed: int = 10,
    skip_seen: bool = True,
) -> str:
    """Read recent items from RSS/Atom news feeds and return a digest.

    Fetches from a curated list of Israeli + international + tech news feeds.
    Integrates with news_memory dedup: already-seen items (by URL) are filtered
    out automatically (48 h TTL), so the same article never appears twice.

    Args:
        feeds:          Comma-separated feed URLs to read. Leave empty to use
                        the built-in default list (Israeli + intl + tech).
        hours:          How many hours back to include (default 8).
        limit_per_feed: Max items per feed (default 10).
        skip_seen:      Filter out already-reported items (default True).
    """
    try:
        import feedparser
    except ImportError:
        return "Error: feedparser not installed. Run: pip install feedparser>=6.0"

    import asyncio

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    feed_list: list[tuple[str, str]]
    if feeds.strip():
        feed_list = [("Custom", u.strip()) for u in feeds.split(",") if u.strip().startswith("http")]
    else:
        feed_list = _default_feeds()

    all_items: list[dict[str, Any]] = []

    def _fetch_feed(name: str, url: str) -> list[dict[str, Any]]:
        try:
            parsed = feedparser.parse(url)
            items: list[dict[str, Any]] = []
            for entry in parsed.entries[:limit_per_feed]:
                pub_dt = _parse_date(entry)
                if pub_dt and pub_dt < cutoff:
                    continue
                title = _clean(getattr(entry, "title", ""), 120)
                summary = _clean(getattr(entry, "summary", "") or getattr(entry, "description", ""), 400)
                text = f"{title}. {summary}".strip(" .") if summary else title
                if len(text) < 30:
                    continue
                link = getattr(entry, "link", "") or ""
                items.append({
                    "channel": name,
                    "channel_title": name,
                    "subscribers": 0,
                    "date_iso": pub_dt.isoformat() if pub_dt else "",
                    "text": text,
                    "views": 0,
                    "url": link,
                    "source": "rss",
                })
            return items
        except Exception as exc:
            logger.warning("rss_tools: failed to fetch '%s': %s", url, exc)
            return []

    # Fetch all feeds concurrently in a thread pool (feedparser is sync)
    loop = asyncio.get_event_loop()
    tasks = [loop.run_in_executor(None, _fetch_feed, name, url) for name, url in feed_list]
    results_nested = await asyncio.gather(*tasks)
    for items in results_nested:
        all_items.extend(items)

    if not all_items:
        return f"No new RSS items found in the last {hours} hours."

    if skip_seen:
        original = len(all_items)
        all_items = news_memory.filter_new(all_items)
        news_memory.mark_seen(all_items)
        skipped = original - len(all_items)
        if skipped:
            logger.info("rss_tools: dedup filtered %d already-seen items", skipped)

    if not all_items:
        return f"All RSS items in the last {hours} hours were already reported (dedup active)."

    # Format output
    lines = [f"## RSS News Digest — last {hours}h ({len(all_items)} new items)\n"]
    by_source: dict[str, list[dict]] = {}
    for item in all_items:
        by_source.setdefault(item["channel"], []).append(item)

    for source, items in by_source.items():
        lines.append(f"### {source}")
        for item in items[:limit_per_feed]:
            dt = item.get("date_iso", "")[:16].replace("T", " ") if item.get("date_iso") else ""
            url = item.get("url", "")
            lines.append(f"**{dt}** — {item['text']}")
            if url:
                lines.append(f"  🔗 {url}")
        lines.append("")

    return "\n".join(lines)
