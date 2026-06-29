"""Telegram channel reading tools — powered by Telethon MTProto.

Reads messages from channels the authenticated user follows, using the
personal account credentials (not the bot). Requires a one-time setup:
run setup_telegram_reader.py to authenticate with a phone number.

Tools:
  read_telegram_channels  — fetch recent messages from configured channels
  list_telegram_channels  — list channels the user is subscribed to
"""

from __future__ import annotations

import os

from orchestrator import telegram_reader
from orchestrator.tools.registry import registry


def _default_channels() -> list[str]:
    """Read channel list from env (ZERO_AGENT_TG_CHANNELS)."""
    raw = os.getenv("ZERO_AGENT_TG_CHANNELS", "").strip()
    if not raw:
        return []
    return [c.strip().lstrip("@") for c in raw.split(",") if c.strip()]


@registry.register
async def read_telegram_channels(
    channels: str = "",
    hours: int = 6,
    limit_per_channel: int = 20,
) -> str:
    """Read recent messages from serious Telegram news channels.

    Connects using the authenticated personal account (MTProto) and fetches
    messages from the last ``hours`` hours. Returns a formatted digest with
    the most important updates per channel — ready to forward to the user
    or synthesize into a news report.

    Requires one-time auth: run ``setup_telegram_reader.py`` if you haven't
    done so yet.

    Args:
        channels:           Comma-separated @usernames or channel IDs to read.
                            Leave empty to use the default list from
                            ZERO_AGENT_TG_CHANNELS in .env.
        hours:              How many hours back to fetch (default 6).
        limit_per_channel:  Max messages per channel (default 20).
    """
    ch_list = (
        [c.strip().lstrip("@") for c in channels.split(",") if c.strip()]
        if channels.strip()
        else _default_channels()
    )
    if not ch_list:
        return (
            "No channels configured. Either pass them in the 'channels' argument "
            "or set ZERO_AGENT_TG_CHANNELS in .env.\n"
            "Example: ZERO_AGENT_TG_CHANNELS=bbcnews,techcrunch,Bloomberg"
        )

    try:
        messages = await telegram_reader.read_channel_messages(
            channels=ch_list,
            hours=hours,
            limit_per_channel=limit_per_channel,
        )
    except RuntimeError as exc:
        return f"Error: {exc}"
    except Exception as exc:  # noqa: BLE001
        return f"Unexpected error reading channels: {type(exc).__name__}: {exc}"

    return telegram_reader.format_messages_report(messages, title="Telegram News Digest")


@registry.register
async def list_telegram_channels(limit: int = 50) -> str:
    """List Telegram channels and groups the user is subscribed to.

    Useful for discovering what channels are available before setting
    ZERO_AGENT_TG_CHANNELS. Shows name, @username, and unread count.

    Args:
        limit: Max number of dialogs to list (default 50).
    """
    try:
        dialogs = await telegram_reader.list_subscribed_channels(limit=limit)
    except RuntimeError as exc:
        return f"Error: {exc}"
    except Exception as exc:  # noqa: BLE001
        return f"Error: {type(exc).__name__}: {exc}"

    if not dialogs:
        return "No channels found."

    lines = [f"**Telegram subscriptions** ({len(dialogs)} shown)\n"]
    for d in dialogs:
        uname = f"@{d['username']}" if d.get("username") else f"id:{d['id']}"
        unread = f" ({d['unread']} unread)" if d.get("unread") else ""
        kind = d.get("type", "?")
        lines.append(f"  • {d['name']} [{uname}]{unread}  — {kind}")
    lines.append(
        "\nTo monitor specific channels, add their @usernames to "
        "ZERO_AGENT_TG_CHANNELS in .env."
    )
    return "\n".join(lines)
