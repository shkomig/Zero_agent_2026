"""Telegram entry point for the Zero Agent.

A second front-end (next to ``app.py`` / Chainlit and ``main.py`` / CLI) that
lets you talk to the SAME agent from Telegram — same tools, same long-term
memory, same projects. The model still runs 100% locally on Ollama; only the
chat transport goes through Telegram's Bot API.

Design (kept deliberately small + dependency-free):
  * Raw long-polling against the Telegram Bot API via ``httpx`` (already a
    project dependency) — no python-telegram-bot, no webhooks, no extra deps.
  * One :class:`Session` per Telegram chat: its own Ollama client + agent +
    rolling message history, serialized by a per-chat lock so two messages in
    the same chat never corrupt the shared history. Different chats run
    concurrently.
  * Memory + projects are shared with the desktop UI (same ChromaDB store).
  * SECURITY: the agent can run shell commands and read/write files, so the bot
    only serves the numeric user IDs in ``TELEGRAM_ALLOWED_IDS``. Anyone else
    just gets told their own ID (so you can add it) and is otherwise ignored.

Run it:

    python telegram_bot.py

Configure ``ZERO_AGENT_TELEGRAM_TOKEN`` and ``ZERO_AGENT_TELEGRAM_ALLOWED_IDS``
(via env vars or the local ``zero-agent/.env`` file, loaded below).
"""

from __future__ import annotations

import os
from pathlib import Path


def _load_dotenv() -> None:
    """Load ``zero-agent/.env`` into the environment, if present.

    Done BEFORE importing ``orchestrator.config`` (which reads env vars at
    import time) so the token/allow-list can live in a local .env file. Existing
    real environment variables always win (``setdefault``). Tiny hand-rolled
    parser — no python-dotenv dependency.
    """
    env_path = Path(__file__).with_name(".env")
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


_load_dotenv()

import asyncio  # noqa: E402 - must follow _load_dotenv()
import logging  # noqa: E402
from dataclasses import dataclass, field  # noqa: E402
from typing import Any  # noqa: E402

import httpx  # noqa: E402

from orchestrator import auto_memory  # noqa: E402
from orchestrator import config  # noqa: E402
from orchestrator import lessons  # noqa: E402
from orchestrator import summarizer  # noqa: E402
from orchestrator import observability as obs  # noqa: E402
from orchestrator import projects  # noqa: E402
from orchestrator import telegram_history  # noqa: E402
from orchestrator.agents.base_agent import BaseAgent  # noqa: E402
from orchestrator.memory import get_store  # noqa: E402
from orchestrator.models.ollama_client import OllamaClient  # noqa: E402

# Side-effect import: registers all tools against the shared registry.
from orchestrator.tools import registry  # noqa: E402
from orchestrator.tools.media_tools import extract_media_paths  # noqa: E402
from orchestrator.tools import tws_tools  # noqa: F401 — registers launch_tws / check_tws_status / stop_tws

logger = logging.getLogger("zero_agent.telegram")

# Models offered by the /model command (mirrors the Chainlit settings panel).
MODEL_OPTIONS = [
    "qwen3:4b",
    "gemma3:4b",
    "qwen3:32b",
    "qwen3-coder-abliterated",
    "qwen3.6-uncensored",
    "supergemma4-uncensored",
    "hermes3:8b",
]

# Telegram caps a single text message at 4096 chars; split a hair below that and
# prefer to break on a newline so we don't cut words/code mid-line.
_MAX_MSG = 4000

HELP_TEXT = (
    "🤖 *Zero Agent* — local AI, now on Telegram.\n"
    "Just send a message and I'll work on it (files, shell, web, images, memory).\n\n"
    "Commands:\n"
    "/help — this help\n"
    "/menu — show the quick-action buttons\n"
    "/reset — start a fresh conversation (clear my short-term context)\n"
    "/model — pick the active model from buttons\n"
    "/projects — list knowledge-base projects\n"
    "/project [name] — set/clear the active project\n"
    "/learn <text> — add knowledge to the active project\n"
    "/always [text|clear] — standing instructions applied to every answer\n"
    "/whoami — show your Telegram ID\n\n"
    "Tip: use the buttons below the text box — 🔭 Research, 🎨 Image, "
    "🧠 Remember — to switch mode, then just send your topic. 💬 Chat returns to "
    "normal."
)

# Slash commands registered in Telegram's "/" menu (command names must be
# lowercase letters/digits/underscores — /forget-project is handled but not
# listed here because the hyphen is not a valid registered command name).
BOT_COMMANDS = [
    {"command": "help", "description": "Show help"},
    {"command": "menu", "description": "Show quick-action buttons"},
    {"command": "model", "description": "Choose the AI model"},
    {"command": "reset", "description": "Start a fresh conversation"},
    {"command": "projects", "description": "List knowledge-base projects"},
    {"command": "project", "description": "Set/clear the active project"},
    {"command": "learn", "description": "Add knowledge to the active project"},
    {"command": "always", "description": "View/add standing instructions"},
    {"command": "whoami", "description": "Show your Telegram ID"},
]


# Persistent reply-keyboard quick actions (the Telegram equivalent of the web
# UI's composer buttons). Tapping a button sends its label text; we map that to
# a "mode" applied to the chat's following messages until the user switches.
MODE_LABELS = {
    "🔭 Research": "research",
    "🎨 Image": "image",
    "🧠 Remember": "remember",
    "💬 Chat": "chat",
}

_MODE_ON = {
    "research": (
        "🔭 Research mode is ON. Send a topic and I'll research it across multiple "
        "sources and cite them. Tap 💬 Chat to return to normal."
    ),
    "image": (
        "🎨 Image mode is ON. Send a prompt and I'll generate an image. "
        "Tap 💬 Chat to return to normal."
    ),
    "remember": (
        "🧠 Remember mode is ON. Everything you send is saved to long-term memory. "
        "Tap 💬 Chat to stop."
    ),
    "chat": "💬 Back to normal chat.",
}


def _mode_keyboard() -> dict[str, Any]:
    """A persistent reply keyboard with the quick-action mode buttons."""
    return {
        "keyboard": [["🔭 Research", "🎨 Image"], ["🧠 Remember", "💬 Chat"]],
        "resize_keyboard": True,
        "is_persistent": True,
    }


def _model_keyboard(active: str) -> dict[str, Any]:
    """Build an inline keyboard of model buttons, ticking the active one."""
    rows: list[list[dict[str, str]]] = []
    row: list[dict[str, str]] = []
    for model in MODEL_OPTIONS:
        label = ("✅ " if model == active else "") + model
        row.append({"text": label, "callback_data": f"m:{model}"})
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return {"inline_keyboard": rows}


# ---------------------------------------------------------------------------
# Telegram Bot API client (raw long-polling over httpx)
# ---------------------------------------------------------------------------
class TelegramAPI:
    """Minimal async wrapper over the bits of the Bot API we use."""

    def __init__(self, token: str, poll_timeout: int) -> None:
        self.base = f"https://api.telegram.org/bot{token}"
        self.poll_timeout = poll_timeout
        # Read timeout must outlive the long-poll, plus a margin for the round trip.
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(poll_timeout + 20, read=poll_timeout + 20)
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _call(self, method: str, **params: Any) -> dict[str, Any]:
        resp = await self._http.post(f"{self.base}/{method}", json=params)
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {method} failed: {data.get('description')}")
        return data

    async def get_me(self) -> dict[str, Any]:
        return (await self._call("getMe")).get("result", {})

    async def get_updates(
        self, offset: int | None, *, timeout: int | None = None
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "timeout": self.poll_timeout if timeout is None else timeout,
            "allowed_updates": ["message", "callback_query"],
        }
        if offset is not None:
            params["offset"] = offset
        data = await self._call("getUpdates", **params)
        return data.get("result", [])

    async def send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        try:
            await self._call("sendChatAction", chat_id=chat_id, action=action)
        except (httpx.HTTPError, RuntimeError):
            pass  # cosmetic only — never let a failed "typing" break a turn

    async def send_message(
        self, chat_id: int, text: str, *, reply_markup: dict[str, Any] | None = None
    ) -> None:
        """Send text, transparently splitting anything over the 4096-char cap.

        Sent as plain text (no parse_mode) so the agent's free-form markdown can
        never trigger a Telegram parse error that would drop the whole reply. An
        optional ``reply_markup`` (e.g. an inline keyboard) is attached to the
        last chunk.
        """
        chunks = _split_message(text)
        for i, chunk in enumerate(chunks):
            params: dict[str, Any] = {"chat_id": chat_id, "text": chunk}
            if reply_markup is not None and i == len(chunks) - 1:
                params["reply_markup"] = reply_markup
            try:
                await self._call("sendMessage", **params)
            except (httpx.HTTPError, RuntimeError) as exc:
                logger.warning("sendMessage failed for chat %s: %s", chat_id, exc)

    async def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        """Edit an existing message's text (and optionally its keyboard)."""
        params: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
        }
        if reply_markup is not None:
            params["reply_markup"] = reply_markup
        try:
            await self._call("editMessageText", **params)
        except (httpx.HTTPError, RuntimeError) as exc:
            # "message is not modified" (tapping the active model) lands here —
            # harmless, just log it.
            logger.debug("editMessageText no-op/fail for chat %s: %s", chat_id, exc)

    async def answer_callback_query(self, callback_id: str, text: str = "") -> None:
        """Acknowledge a button tap so Telegram stops the loading spinner."""
        try:
            await self._call(
                "answerCallbackQuery", callback_query_id=callback_id, text=text
            )
        except (httpx.HTTPError, RuntimeError):
            pass

    async def set_my_commands(self, commands: list[dict[str, str]]) -> None:
        """Register the slash-command menu shown in Telegram's '/' picker."""
        try:
            await self._call("setMyCommands", commands=commands)
        except (httpx.HTTPError, RuntimeError) as exc:
            logger.warning("setMyCommands failed: %s", exc)

    async def send_file(self, chat_id: int, path: Path, *, as_photo: bool) -> None:
        """Upload a local file as a photo (images) or document (video/other)."""
        method = "sendPhoto" if as_photo else "sendDocument"
        field_name = "photo" if as_photo else "document"
        try:
            with path.open("rb") as fh:
                resp = await self._http.post(
                    f"{self.base}/{method}",
                    data={"chat_id": chat_id},
                    files={field_name: (path.name, fh)},
                )
            if not resp.json().get("ok"):
                logger.warning("%s rejected %s: %s", method, path.name, resp.text[:300])
        except (httpx.HTTPError, OSError) as exc:
            logger.warning("failed to send file %s: %s", path, exc)


def _split_message(text: str) -> list[str]:
    """Split ``text`` into <=_MAX_MSG chunks, preferring newline boundaries."""
    text = text or "_(empty response)_"
    if len(text) <= _MAX_MSG:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > _MAX_MSG:
        cut = remaining.rfind("\n", 0, _MAX_MSG)
        if cut <= 0:
            cut = _MAX_MSG
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks


# ---------------------------------------------------------------------------
# Per-chat session
# ---------------------------------------------------------------------------
@dataclass
class Session:
    """One Telegram chat's agent + rolling history, guarded by a lock."""

    client: OllamaClient
    agent: BaseAgent
    model: str
    mode: str = "chat"  # chat | research | image | remember (quick-action mode)
    history: list[dict[str, Any]] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_sessions: dict[int, Session] = {}


async def _get_session(chat_id: int) -> Session | None:
    """Return (creating if needed) the session for a chat, or None if Ollama down."""
    session = _sessions.get(chat_id)
    if session is not None:
        return session

    # Restore any persisted history + last-used model for this chat so a bot
    # restart resumes the conversation instead of starting cold.
    saved_history, saved_model = telegram_history.load(chat_id)
    preferred = saved_model or config.TELEGRAM_MODEL

    client = OllamaClient(model=preferred)
    model = await client.resolve_model(preferred)
    if model is None:
        await client.aclose()
        return None
    session = Session(client=client, agent=BaseAgent(client, registry), model=model)
    if saved_history:
        session.history = saved_history
        logger.info("restored %d msgs of history for chat %s", len(saved_history), chat_id)
    _sessions[chat_id] = session
    return session


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
async def _handle_command(
    api: TelegramAPI, session: Session, chat_id: int, user_id: int, text: str
) -> None:
    """Handle a /command, sending the reply (or a button menu) directly."""
    parts = text.split(maxsplit=1)
    cmd = parts[0].lower().split("@")[0]  # strip any @botname suffix
    arg = parts[1].strip() if len(parts) > 1 else ""
    store = projects.get_project_store()

    if cmd in {"/start", "/help"}:
        await api.send_message(chat_id, HELP_TEXT, reply_markup=_mode_keyboard())
        return

    if cmd == "/menu":
        await api.send_message(
            chat_id, "Quick actions — pick a mode:", reply_markup=_mode_keyboard()
        )
        return

    if cmd == "/whoami":
        await api.send_message(
            chat_id,
            f"Your Telegram ID is: {user_id}\nUse it in TELEGRAM_ALLOWED_IDS.",
        )
        return

    if cmd == "/lessons":
        await api.send_message(chat_id, lessons.list_lessons(get_store()))
        return

    if cmd == "/always":
        mem = get_store()
        if arg.lower() == "clear":
            n = mem.delete_by_tag(auto_memory.PREFERENCE_TAG)
            await api.send_message(chat_id, f"🧹 Cleared {n} standing instruction(s).")
            return
        if arg:
            try:
                await mem.remember(arg, tags=auto_memory.PREFERENCE_TAG)
                await api.send_message(chat_id, f"📌 Standing instruction added: {arg}")
            except Exception as exc:  # noqa: BLE001
                await api.send_message(chat_id, f"⚠️ Could not save: {exc}")
            return
        rules = mem.list_by_tag(auto_memory.PREFERENCE_TAG, limit=50)
        if rules:
            listing = "\n".join(f"- {r['text']}" for r in rules)
            await api.send_message(
                chat_id,
                "📌 Standing instructions (always applied):\n" + listing
                + "\n\nAdd: /always <text> · Clear: /always clear. "
                "Also learned automatically from your feedback.",
            )
        else:
            await api.send_message(
                chat_id,
                "📌 No standing instructions yet. Learned automatically from your "
                "feedback, or add one with /always <text>.",
            )
        return

    if cmd == "/reset":
        session.history.clear()
        telegram_history.clear(chat_id)
        await api.send_message(
            chat_id,
            "🧹 Fresh conversation — short-term context cleared. (Long-term memory kept.)",
        )
        return

    if cmd == "/model":
        if not arg:
            await api.send_message(
                chat_id,
                f"🎛️ Active model: {session.model}\nTap a button to switch:",
                reply_markup=_model_keyboard(session.model),
            )
            return
        # Typing a name still works too (e.g. for a model not in the buttons).
        session.client.model = arg
        session.model = arg
        telegram_history.save(chat_id, session.history, model=arg)
        await api.send_message(chat_id, f"✅ Active model switched to {arg}.")
        return

    if cmd == "/projects":
        names = store.list_projects()
        active = projects.get_active_project()
        if not names:
            await api.send_message(
                chat_id, "📁 No projects yet. Create one with /project <name>."
            )
            return
        listing = "\n".join(
            f"- {n} ({store.count(n)} items)" + ("  ⬅️ active" if n == active else "")
            for n in names
        )
        await api.send_message(chat_id, f"📁 Projects:\n{listing}")
        return

    if cmd == "/project":
        chosen = arg or None
        projects.set_active_project(chosen)
        if chosen:
            await api.send_message(
                chat_id,
                f"📁 Active project: {chosen} ({store.count(chosen)} items). "
                "Add knowledge with /learn <text>.",
            )
        else:
            await api.send_message(chat_id, "📁 Active project cleared.")
        return

    if cmd == "/learn":
        active = projects.get_active_project()
        if not active:
            await api.send_message(
                chat_id, "⚠️ No active project. Set one first: /project <name>."
            )
            return
        if not arg:
            await api.send_message(chat_id, "⚠️ Usage: /learn <text to remember>.")
            return
        try:
            await store.add_knowledge(active, arg)
            await api.send_message(
                chat_id, f"🧠 Added to {active} (now {store.count(active)} items)."
            )
        except Exception as exc:  # noqa: BLE001
            await api.send_message(chat_id, f"⚠️ Could not save: {exc}")
        return

    if cmd == "/forget-project":
        if not arg:
            await api.send_message(chat_id, "⚠️ Usage: /forget-project <name>.")
            return
        removed = store.delete_project(arg)
        if projects.get_active_project() == arg:
            projects.set_active_project(None)
        await api.send_message(
            chat_id, f"🗑️ Deleted project {arg} ({removed} items removed)."
        )
        return

    await api.send_message(chat_id, f"Unknown command {cmd}. Try /help.")


# ---------------------------------------------------------------------------
# Message handling
# ---------------------------------------------------------------------------
async def _keep_typing(api: TelegramAPI, chat_id: int, stop: asyncio.Event) -> None:
    """Re-send the 'typing…' action every few seconds until ``stop`` is set.

    Telegram's typing indicator lasts ~5s, so we refresh it while the agent
    works (a render or a slow first token can take a while).
    """
    while not stop.is_set():
        await api.send_chat_action(chat_id, "typing")
        try:
            await asyncio.wait_for(stop.wait(), timeout=4.0)
        except asyncio.TimeoutError:
            continue


async def _run_agent_turn(
    api: TelegramAPI,
    session: Session,
    chat_id: int,
    prompt: str,
    *,
    recall_text: str | None = None,
) -> None:
    """Run one user message through the agent and reply (with any media).

    ``prompt`` is what the model sees (it may carry a mode instruction);
    ``recall_text`` is the raw user topic used for memory/project recall and
    distillation so those stay clean. Defaults to ``prompt``.
    """
    history = session.history
    recall_text = recall_text or prompt

    # Rolling summary: compress the oldest turns if the history has grown large,
    # so Ollama doesn't silently truncate them (Telegram keeps history in memory).
    await summarizer.maybe_summarize(_distiller, history, config.OLLAMA_NUM_CTX)

    # Active project: recall relevant knowledge and keep it in context.
    active_project = projects.get_active_project()
    if active_project:
        store = projects.get_project_store()
        hits = await store.recall(active_project, recall_text)
        block = projects.build_project_block(active_project, hits) if hits else None
        projects.inject_project_message(history, block)

    # Self-improving memory: surface relevant remembered facts for this turn.
    if config.AUTO_MEMORY:
        block = await auto_memory.recall_memories(get_store(), recall_text)
        auto_memory.inject_memory_message(history, block)
        # Standing instructions: injected every turn (not similarity-gated).
        auto_memory.inject_preferences_message(
            history, auto_memory.recall_preferences(get_store())
        )
        # Learn-from-mistakes: surface relevant lessons from past failures.
        lesson_block = await lessons.recall_lessons(get_store(), recall_text)
        lessons.inject_lessons_message(history, lesson_block)

    # Collect any media produced by tools (e.g. ComfyUI renders) so we can send
    # the images/videos after the text answer, ChatGPT-style.
    media_images: list[Path] = []
    media_others: list[Path] = []

    async def on_tool_end(_handle: Any, result: str) -> None:
        imgs, others = extract_media_paths(result)
        media_images.extend(imgs)
        media_others.extend(others)

    stop = asyncio.Event()
    typing = asyncio.create_task(_keep_typing(api, chat_id, stop))
    try:
        answer = await session.agent.run(
            prompt, history=history, on_tool_end=on_tool_end
        )
    finally:
        stop.set()
        await typing

    # Persist the updated history so this chat survives a bot restart (the
    # long-term ChromaDB memory is separate and already persistent).
    telegram_history.save(chat_id, history, model=session.model)

    await api.send_message(chat_id, answer or "_(no content returned)_")

    for path in media_images:
        if path.exists():
            await api.send_file(chat_id, path, as_photo=True)
    for path in media_others:
        if path.exists():
            await api.send_file(chat_id, path, as_photo=False)

    # Fire-and-forget: distill durable facts from this exchange for future chats.
    if config.AUTO_MEMORY and answer:
        asyncio.create_task(
            auto_memory.distill_and_store(_distiller, get_store(), recall_text, answer)
        )
        # Learn-from-mistakes: if this run hit failures, distill a lesson.
        failures = "\n".join(getattr(session.agent, "last_run_failures", []))
        if failures:
            asyncio.create_task(
                lessons.distill_lessons(
                    _distiller, get_store(), recall_text, answer, failures
                )
            )


async def _handle_message(api: TelegramAPI, message: dict[str, Any]) -> None:
    """Authorize, route to a command or the agent, and never raise."""
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    user = message.get("from") or {}
    user_id = user.get("id")
    text = (message.get("text") or "").strip()
    if chat_id is None or not text:
        return

    # --- Security gate ------------------------------------------------------
    if user_id not in config.TELEGRAM_ALLOWED_IDS:
        logger.warning("rejected message from unauthorized user_id=%s", user_id)
        await api.send_message(
            chat_id,
            "⛔ You're not authorized to use this bot.\n"
            f"Your Telegram ID is: {user_id}\n"
            "Ask the owner to add it to TELEGRAM_ALLOWED_IDS.",
        )
        return

    session = await _get_session(chat_id)
    if session is None:
        await api.send_message(
            chat_id,
            "⚠️ Could not reach Ollama or no models are installed. "
            "Start it (`ollama serve`) and pull a model, then try again.",
        )
        return

    # Serialize messages within one chat so the shared history stays consistent.
    async with session.lock:
        try:
            # Quick-action mode buttons (persistent reply keyboard).
            if text in MODE_LABELS:
                session.mode = MODE_LABELS[text]
                await api.send_message(
                    chat_id, _MODE_ON[session.mode], reply_markup=_mode_keyboard()
                )
                return

            if text.startswith("/"):
                await _handle_command(api, session, chat_id, user_id, text)
                return

            # Remember mode: save directly to memory, no model round-trip.
            if session.mode == "remember":
                try:
                    await get_store().remember(text, tags="manual")
                    await api.send_message(chat_id, "🧠 Saved to long-term memory.")
                except Exception as exc:  # noqa: BLE001
                    await api.send_message(chat_id, f"⚠️ Could not save: {exc}")
                return

            # Research / Image modes prepend an instruction so the agent reaches
            # for the right tool; the raw topic is kept for recall/memory.
            prompt = text
            if session.mode == "research":
                prompt = (
                    "Do thorough, up-to-date web research on the following and answer "
                    "with a clear synthesis and cited sources. Use the deep_research "
                    "tool.\n\n" + text
                )
            elif session.mode == "image":
                prompt = (
                    "Generate an image with ComfyUI for the following prompt "
                    "(use run_comfyui_workflow):\n\n" + text
                )
            await _run_agent_turn(api, session, chat_id, prompt, recall_text=text)
        except Exception as exc:  # noqa: BLE001 - one bad turn must not kill the bot
            logger.exception("handling message failed: %s", exc)
            await api.send_message(
                chat_id,
                f"⚠️ Something went wrong ({type(exc).__name__}). It was logged — try again.",
            )


async def _handle_callback(api: TelegramAPI, callback: dict[str, Any]) -> None:
    """Handle an inline-keyboard tap. Currently only model selection (``m:<name>``)."""
    callback_id = callback.get("id", "")
    user_id = (callback.get("from") or {}).get("id")
    msg = callback.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    message_id = msg.get("message_id")
    data = callback.get("data") or ""

    if user_id not in config.TELEGRAM_ALLOWED_IDS:
        await api.answer_callback_query(callback_id, "Not authorized")
        return
    if chat_id is None or not data.startswith("m:"):
        await api.answer_callback_query(callback_id)
        return

    model = data[2:]
    session = await _get_session(chat_id)
    if session is None:
        await api.answer_callback_query(callback_id, "Ollama not reachable")
        return

    async with session.lock:
        session.client.model = model
        session.model = model
        # Persist the choice so a restart before the next turn keeps this model.
        telegram_history.save(chat_id, session.history, model=model)

    await api.answer_callback_query(callback_id, f"Model: {model}")
    if message_id is not None:
        await api.edit_message_text(
            chat_id,
            message_id,
            f"✅ Active model: {model}\nTap a button to switch:",
            reply_markup=_model_keyboard(model),
        )


# Shared clients for the memory subsystem (embeddings + background distillation),
# created once at startup so importing this module stays cheap.
_distiller: OllamaClient


async def _run() -> int:
    obs.configure_logging()

    token = config.TELEGRAM_BOT_TOKEN
    if not token:
        logger.error(
            "No Telegram token. Set ZERO_AGENT_TELEGRAM_TOKEN (from @BotFather) "
            "in the environment or in zero-agent/.env, then rerun."
        )
        return 1
    if not config.TELEGRAM_ALLOWED_IDS:
        logger.warning(
            "TELEGRAM_ALLOWED_IDS is empty — every message will be refused. "
            "Message the bot once to learn your ID, then add it to "
            "ZERO_AGENT_TELEGRAM_ALLOWED_IDS and restart."
        )

    # Bind the shared memory + project stores to a dedicated embedding client,
    # and prepare the small-model distiller. Mirrors app.py's wiring.
    global _distiller
    embed_client = OllamaClient()
    get_store(embed_client)
    projects.get_project_store(embed_client)
    _distiller = OllamaClient(model=config.MEMORY_DISTILL_MODEL)

    if not await embed_client.health_check():
        logger.warning(
            "Ollama did not answer at %s — start it (`ollama serve`). The bot will "
            "keep running and retry per message.",
            config.OLLAMA_HOST,
        )

    api = TelegramAPI(token, config.TELEGRAM_POLL_TIMEOUT)
    try:
        me = await api.get_me()
    except (httpx.HTTPError, RuntimeError) as exc:
        logger.error("Could not reach Telegram (bad token?): %s", exc)
        await api.aclose()
        return 1
    await api.set_my_commands(BOT_COMMANDS)
    logger.info(
        "Telegram bot @%s online | model=%s | allowed=%d user(s) | tools=%s",
        me.get("username", "?"),
        config.TELEGRAM_MODEL,
        len(config.TELEGRAM_ALLOWED_IDS),
        ", ".join(registry.names()),
    )

    # Skip any backlog so we don't replay messages sent while the bot was down.
    offset: int | None = None
    try:
        backlog = await api.get_updates(offset=-1, timeout=0)
        if backlog:
            offset = backlog[-1]["update_id"] + 1
    except (httpx.HTTPError, RuntimeError):
        pass

    logger.info("listening for messages…")
    try:
        while True:
            try:
                updates = await api.get_updates(offset)
            except (httpx.HTTPError, RuntimeError) as exc:
                logger.warning("getUpdates error: %s — retrying in 3s", exc)
                await asyncio.sleep(3)
                continue
            for update in updates:
                offset = update["update_id"] + 1
                message = update.get("message")
                if message:
                    # One task per message: a slow render in one chat never
                    # blocks polling or other chats. Per-chat lock keeps order.
                    asyncio.create_task(_handle_message(api, message))
                callback = update.get("callback_query")
                if callback:
                    asyncio.create_task(_handle_callback(api, callback))
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("shutting down…")
    finally:
        await api.aclose()
        await embed_client.aclose()
        await _distiller.aclose()
        for session in _sessions.values():
            await session.client.aclose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run()))
