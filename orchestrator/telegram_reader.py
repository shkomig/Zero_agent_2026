"""Telethon MTProto client — read messages from Telegram channels.

Provides read-only access to channels the user is subscribed to, using
their personal account (not the bot). The client authenticates once
interactively (run setup_telegram_reader.py), then reuses the saved
.session file for all subsequent calls — no further interaction needed.

Session file: data/telethon/zero_agent.session
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger("zero_agent.telegram_reader")

_SESSION_DIR = Path(__file__).parent.parent / "data" / "telethon"
_SESSION_PATH = str(_SESSION_DIR / "zero_agent")  # telethon adds .session

# Lazy import so the app boots fine even if telethon isn't installed yet.
try:
    from telethon import TelegramClient
    from telethon.tl.types import Channel, Chat, User
    _TELETHON_OK = True
except ImportError:
    TelegramClient = None  # type: ignore[assignment, misc]
    _TELETHON_OK = False


def _api_creds() -> tuple[int, str]:
    """Read API credentials from env (set in .env)."""
    api_id_raw = os.getenv("ZERO_AGENT_TG_API_ID", "").strip()
    api_hash = os.getenv("ZERO_AGENT_TG_API_HASH", "").strip()
    if not api_id_raw or not api_hash:
        raise RuntimeError(
            "ZERO_AGENT_TG_API_ID / ZERO_AGENT_TG_API_HASH not set in .env. "
            "Get them from https://my.telegram.org → App configuration."
        )
    return int(api_id_raw), api_hash


def _session_exists() -> bool:
    return Path(_SESSION_PATH + ".session").exists()


def _unavailable(reason: str) -> str:
    return f"Telegram reader unavailable: {reason}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def read_channel_messages(
    channels: list[str],
    hours: int = 6,
    limit_per_channel: int = 30,
    min_length: int = 50,
    skip_seen: bool = True,
) -> list[dict[str, Any]]:
    """Fetch recent messages from a list of channel usernames / IDs.

    Args:
        channels:           List of @usernames or numeric channel IDs.
        hours:              How far back to look (default: last 6 hours).
        limit_per_channel:  Max messages to fetch per channel (Telegram API cap).
        min_length:         Skip messages shorter than this (filters button/spam noise).

    Returns a list of dicts: {channel, sender, date_iso, text, views, url}.
    """
    if not _TELETHON_OK:
        raise RuntimeError(
            "telethon is not installed. Run: pip install telethon>=1.36"
        )
    if not _session_exists():
        raise RuntimeError(
            "Telethon session not found. Run setup_telegram_reader.py once "
            "to authenticate with your phone number."
        )

    api_id, api_hash = _api_creds()
    _SESSION_DIR.mkdir(parents=True, exist_ok=True)

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    results: list[dict[str, Any]] = []

    client = TelegramClient(_SESSION_PATH, api_id, api_hash)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError(
                "Telethon session expired. Re-run setup_telegram_reader.py."
            )

        for ch in channels:
            ch = ch.strip().lstrip("@")
            if not ch:
                continue
            try:
                entity = await client.get_entity(ch)
                # Subscriber count — available on Channel/Megagroup entities
                subscribers = getattr(entity, "participants_count", None) or 0
                ch_title = getattr(entity, "title", None) or ch
                ch_username = getattr(entity, "username", None) or ch
                ch_name = f"@{ch_username}" if ch_username != ch else ch_title

                msgs = await client.get_messages(entity, limit=limit_per_channel)
                ch_msgs: list[dict[str, Any]] = []
                for msg in msgs:
                    if not msg.text or len(msg.text) < min_length:
                        continue
                    msg_date = msg.date
                    if msg_date.tzinfo is None:
                        msg_date = msg_date.replace(tzinfo=timezone.utc)
                    if msg_date < cutoff:
                        break
                    ch_msgs.append({
                        "channel": ch_name,
                        "channel_title": ch_title,
                        "subscribers": subscribers,
                        "date_iso": msg_date.isoformat(),
                        "text": msg.text,
                        "views": getattr(msg, "views", 0) or 0,
                        "url": f"https://t.me/{ch_username}/{msg.id}",
                    })
                results.extend(ch_msgs)
                logger.info("telegram_reader: %s — %d msgs, %s subscribers",
                            ch_name, len(ch_msgs), f"{subscribers:,}" if subscribers else "?")
            except Exception as exc:  # noqa: BLE001
                logger.warning("telegram_reader: failed to read '%s': %s", ch, exc)
                results.append({
                    "channel": f"@{ch}",
                    "error": str(exc),
                    "text": "",
                })
    finally:
        await client.disconnect()

    if skip_seen:
        from orchestrator import news_memory
        original_count = len(results)
        results = news_memory.filter_new(results)
        news_memory.mark_seen(results)
        skipped = original_count - len(results)
        if skipped:
            logger.info("telegram_reader: dedup filtered %d already-seen items", skipped)

    return results


async def list_subscribed_channels(limit: int = 50) -> list[dict[str, Any]]:
    """List channels/groups the authenticated user is subscribed to."""
    if not _TELETHON_OK:
        raise RuntimeError("telethon is not installed.")
    if not _session_exists():
        raise RuntimeError("Run setup_telegram_reader.py first.")

    api_id, api_hash = _api_creds()
    client = TelegramClient(_SESSION_PATH, api_id, api_hash)
    await client.connect()
    out: list[dict[str, Any]] = []
    try:
        async for dialog in client.iter_dialogs(limit=limit):
            entity = dialog.entity
            kind = type(entity).__name__
            out.append({
                "name": getattr(entity, "title", None) or getattr(entity, "username", str(dialog.id)),
                "username": getattr(entity, "username", None),
                "id": dialog.id,
                "type": kind,
                "unread": dialog.unread_count,
            })
    finally:
        await client.disconnect()
    return out


def format_messages_report(messages: list[dict[str, Any]], title: str = "Telegram News Digest") -> str:
    """Format raw messages into a rich, categorised digest report.

    Channels are sorted by subscriber count (largest first — most authoritative).
    Each item shows ~500 chars of content plus link and view count.
    """
    if not messages:
        return f"📭 {title}\n\nאין הודעות חדשות בחלון הזמן שהוגדר."

    by_channel: dict[str, list[dict]] = {}
    errors: list[str] = []
    for m in messages:
        if m.get("error"):
            errors.append(f"- {m['channel']}: {m['error']}")
            continue
        ch = m["channel"]
        by_channel.setdefault(ch, []).append(m)

    # Sort channels by subscriber count descending (most followed first)
    def _subs(msgs: list[dict]) -> int:
        return msgs[0].get("subscribers", 0) if msgs else 0

    sorted_channels = sorted(by_channel.items(), key=lambda kv: _subs(kv[1]), reverse=True)

    from datetime import datetime, timezone
    now_str = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M")
    lines = [f"📡 *{title}*", f"_{now_str} UTC_\n"]

    for ch, msgs in sorted_channels:
        msgs.sort(key=lambda x: x.get("date_iso", ""), reverse=True)
        subs = msgs[0].get("subscribers", 0) if msgs else 0
        subs_str = f" · {subs:,} מנויים" if subs else ""
        title_str = msgs[0].get("channel_title", ch) if msgs else ch
        lines.append(f"\n*{title_str}* ({ch}{subs_str})")
        lines.append("─" * 30)

        for m in msgs[:4]:  # up to 4 items per channel
            ts = m.get("date_iso", "")[:16].replace("T", " ")
            views = f"{m['views']:,} 👁" if m.get("views") else ""
            # More content per item — 500 chars
            text = m["text"].replace("*", "·").replace("_", " ").strip()
            if len(text) > 500:
                # Try to cut at sentence boundary
                cut = text.rfind(". ", 0, 500)
                text = text[: cut + 1 if cut > 200 else 500] + "…"
            url = f"🔗 {m['url']}" if m.get("url") else ""
            meta = "  ".join(filter(None, [ts, views, url]))
            lines.append(f"\n{text}")
            if meta:
                lines.append(f"_{meta}_")

    if errors:
        lines.append(f"\n\n⚠️ *ערוצים שלא נטענו:* {len(errors)}")

    return "\n".join(lines)
