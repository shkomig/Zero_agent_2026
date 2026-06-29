"""Send messages to the owner's Telegram via the existing bot.

Used by the scheduler bridge so that scheduled task results arrive in
Telegram even when the Chainlit UI is closed. No new dependency — uses
the same httpx client that telegram_bot.py already uses.

Also registers ``send_telegram_report`` as a Zero Agent tool so the model
can explicitly forward any text to the owner's Telegram — useful when the
user asks for a report "to be sent to Telegram" during a manual chat session.

The owner ID(s) come from config.TELEGRAM_ALLOWED_IDS (the same list
that gates incoming messages). A message is sent to EVERY ID in the list
so it works if you use the bot from multiple accounts.
"""

from __future__ import annotations

import logging
from typing import Sequence

import httpx

from orchestrator import config

logger = logging.getLogger("zero_agent.telegram_notify")

_TG_BASE = "https://api.telegram.org/bot{token}"
_MAX_LEN = 4096          # Telegram hard cap per message
_CHUNK_LEN = 4000        # leave headroom for markdown


def _chunks(text: str, size: int = _CHUNK_LEN) -> list[str]:
    """Split long text into ≤size-char chunks, breaking on newlines."""
    if len(text) <= size:
        return [text]
    parts: list[str] = []
    while text:
        if len(text) <= size:
            parts.append(text)
            break
        cut = text.rfind("\n", 0, size)
        if cut == -1:
            cut = size
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return parts


async def send_to_owner(text: str, parse_mode: str = "Markdown") -> bool:
    """Send text to every owner ID in TELEGRAM_ALLOWED_IDS.

    Returns True if at least one message was delivered successfully.
    Silently logs errors rather than raising, so a Telegram outage never
    crashes a scheduled task.
    """
    token = config.TELEGRAM_BOT_TOKEN
    if not token:
        logger.warning("telegram_notify: no bot token configured")
        return False

    owner_ids: Sequence[int] = list(config.TELEGRAM_ALLOWED_IDS)
    if not owner_ids:
        logger.warning("telegram_notify: TELEGRAM_ALLOWED_IDS is empty")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    ok = False
    async with httpx.AsyncClient(timeout=15) as client:
        for chat_id in owner_ids:
            for chunk in _chunks(text):
                try:
                    r = await client.post(url, json={
                        "chat_id": chat_id,
                        "text": chunk,
                        "parse_mode": parse_mode,
                        "disable_web_page_preview": True,
                    })
                    if r.status_code == 200:
                        ok = True
                    else:
                        # Markdown parse error → retry as plain text
                        if r.status_code == 400 and parse_mode != "":
                            r2 = await client.post(url, json={
                                "chat_id": chat_id,
                                "text": chunk,
                            })
                            if r2.status_code == 200:
                                ok = True
                            else:
                                logger.warning(
                                    "telegram_notify: send failed chat=%s: %s",
                                    chat_id, r2.text[:200],
                                )
                        else:
                            logger.warning(
                                "telegram_notify: send failed chat=%s status=%s: %s",
                                chat_id, r.status_code, r.text[:200],
                            )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("telegram_notify: exception chat=%s: %s", chat_id, exc)
    return ok


# ---------------------------------------------------------------------------
# Registered tool — Zero can call this explicitly to forward text to Telegram
# ---------------------------------------------------------------------------

from orchestrator.tools.registry import registry  # noqa: E402


@registry.register
async def send_telegram_report(text: str, title: str = "") -> str:
    """Send a report or message to the owner's Telegram.

    Use this whenever the user asks to receive a report/summary/update
    via Telegram, or after generating a news digest / stock report / GitHub
    summary that should be delivered to Telegram instead of (or in addition
    to) the chat UI.

    Args:
        text:  The report content to send. Markdown is supported.
        title: Optional short title shown as a header (e.g. "📈 מניות AI").
               Leave empty to send text as-is.
    """
    body = f"*{title}*\n\n{text}" if title else text
    ok = await send_to_owner(body)
    if ok:
        return f"✅ Report sent to Telegram ({len(text)} chars)."
    return "⚠️ Failed to send to Telegram — check bot token in .env and that the bot is not blocked."
