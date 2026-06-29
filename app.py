"""Chainlit "split-screen" UI for the Zero Agent.

Wraps the existing BaseAgent. Every tool call (read_file /
execute_terminal_command) is rendered as a collapsible ``cl.Step`` showing the
tool name, the JSON input, and the raw output — while the final natural-language
answer goes to the main chat thread.

Run it:

    chainlit run app.py -w
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any


def _load_dotenv() -> None:
    """Load zero-agent/.env into the environment before any config import.

    Same tiny hand-rolled parser used by telegram_bot.py — no python-dotenv
    dependency. Existing real environment variables always win (setdefault).
    Must run before `import orchestrator.config` (which reads env vars at
    import time) so tokens/secrets in .env are visible to all modules.
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


_load_dotenv()  # must be first — config reads env vars at import time

# Chainlit requires an auth secret (to sign local session cookies) before it is
# imported. For a single-user local app a stable default is fine; override with
# the CHAINLIT_AUTH_SECRET env var for extra safety. Set BEFORE importing chainlit.
os.environ.setdefault(
    "CHAINLIT_AUTH_SECRET",
    "zero-agent-local-secret-change-me-via-CHAINLIT_AUTH_SECRET",
)

# Chainlit persists file elements (generated images, spoken-reply audio) to a
# local ".files/<session>" dir, creating the session subdir with
# mkdir(exist_ok=True) — which on Windows raises FileNotFoundError (WinError 3)
# if the ".files" ROOT is missing (it doesn't create parents). Ensure the root
# exists up front so attaching an image/audio element can never crash the turn.
os.makedirs(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".files"),
    exist_ok=True,
)

import chainlit as cl
from chainlit.input_widget import Select, Switch

from orchestrator import auto_memory
from orchestrator import config
from orchestrator import idle_hygiene
from orchestrator import lessons
from orchestrator import memory_hygiene
from orchestrator import notify
from orchestrator import self_improve
from orchestrator import scheduler as task_scheduler
from orchestrator import session_context
from orchestrator import stt
from orchestrator import summarizer
from orchestrator import tts
from orchestrator import observability as obs
from orchestrator import persistence
from orchestrator import projects
from orchestrator.agents.base_agent import BaseAgent
from orchestrator.memory import get_store
from orchestrator.models.ollama_client import OllamaClient
from orchestrator.supervisor import Supervisor
from orchestrator.research_agent import ResearchAgent

# Side-effect imports: register all tools.
from orchestrator.tools import registry
from orchestrator.tools.media_tools import extract_media_paths
from orchestrator.tools import session_tools  # noqa: F401 — registers update_task_state / log_task_action / get_task_status
from orchestrator.tools import mcp_tools              # noqa: F401 — registers mcp_connect / mcp_list / mcp_call / mcp_disconnect
from orchestrator.tools import cookbook_tools          # noqa: F401 — registers cookbook_list / cookbook_install
from orchestrator.tools import github_tools            # noqa: F401 — registers get_github_trending / get_github_repo_info
from orchestrator.tools import telegram_channel_tools  # noqa: F401 — registers read_telegram_channels / list_telegram_channels
from orchestrator.tools import tws_tools               # noqa: F401 — registers launch_tws / check_tws_status / stop_tws
from orchestrator.tools import rss_tools               # noqa: F401 — registers read_rss_feeds
from orchestrator import telegram_notify as _tg_notify_module  # noqa: F401 — registers send_telegram_report tool
from orchestrator import cookbook as _cookbook
from orchestrator import mcp_client as _mcp_client

# Structured logging + trace IDs (Phase 1 observability).
obs.configure_logging()

# Models selectable from the in-UI settings panel. The first entry is the
# default; config.DEFAULT_MODEL takes precedence if it's one of these.
# qwen3:4b is fast (good for everyday chat + image requests); the larger models
# are smarter/uncensored but much slower.
MODEL_OPTIONS = [
    "qwen3:4b",
    "gemma3:4b",
    "qwen3:32b",
    "qwen3-coder-30b",  # fast MoE (~3B active) — the Supervisor-Worker coder
    "qwen3-coder-abliterated",
    "qwen3.6-uncensored",
    "supergemma4-uncensored",
    "hermes3:8b",
]
DEFAULT_MODEL = (
    config.DEFAULT_MODEL if config.DEFAULT_MODEL in MODEL_OPTIONS else MODEL_OPTIONS[0]
)

# Voice-input language (⚙️ setting). Label → faster-whisper code ("" = auto-detect).
# Default English: speaking English was being transcribed as Hebrew on auto-detect.
_VOICE_LANGS: "dict[str, str]" = {"English": "en", "עברית": "he", "Auto-detect": ""}
_VOICE_LANG_LABELS = list(_VOICE_LANGS)
_DEFAULT_STT_LANG = "en"

# Wake words used when the ⚙️ "Wake word" switch is ON (Hebrew + English forms of
# "Zero"). Env ZERO_AGENT_VOICE_WAKE_WORDS overrides the default set at startup.
_DEFAULT_WAKE = config.VOICE_WAKE_WORDS or ["זירו", "zero"]


def _voice_lang_index(code: "str | None") -> int:
    code = (code or _DEFAULT_STT_LANG)
    for i, label in enumerate(_VOICE_LANG_LABELS):
        if _VOICE_LANGS[label] == code:
            return i
    return 0

# Quick-action buttons shown in the message composer (Chainlit "commands"). The
# user taps one, types the subject, and sends — no need to phrase the
# instruction. `message.command` carries the chosen id into on_message, where we
# route it to the right tool. Icons are lucide names.
CHAT_COMMANDS = [
    {
        "id": "Agent",
        "icon": "bot",
        "description": "Autonomous multi-step build — plan → do → verify on disk",
        "button": True,
        "persistent": False,
    },
    {
        "id": "Research",
        "icon": "telescope",
        "description": "Deep web research — reads multiple sources and cites them",
        "button": True,
        "persistent": False,
    },
    {
        "id": "Image",
        "icon": "image",
        "description": "Generate an image with ComfyUI from your prompt",
        "button": True,
        "persistent": False,
    },
    {
        "id": "Video",
        "icon": "video",
        "description": "Generate a video with ComfyUI (LTX/Wan) — runs in the background",
        "button": True,
        "persistent": False,
    },
    {
        "id": "Graphify",
        "icon": "share-2",
        "description": "Build a knowledge graph of a local project folder",
        "button": True,
        "persistent": False,
    },
    {
        "id": "Remember",
        "icon": "brain",
        "description": "Save your text to long-term memory",
        "button": True,
        "persistent": False,
    },
    {
        "id": "Schedule",
        "icon": "clock",
        "description": "Schedule a task to run at a specific time or on a recurring schedule",
        "button": True,
        "persistent": False,
    },
]


@cl.set_starters
async def _starters() -> "list[cl.Starter]":
    """One-click example prompts shown on the empty welcome screen."""
    return [
        cl.Starter(
            label="📡 עדכון חדשות בוקר",
            message="קרא חדשות מטלגרם ו-RSS (hours=8) וכתוב דוח חדשות בעברית מסודר לפי קטגוריות: ביטחון, כלכלה, טכנולוגיה, בינלאומי, כללי. שלח לטלגרם עם send_telegram_report.",
        ),
        cl.Starter(
            label="📈 דוח מניות AI",
            message="בצע מחקר על מניות NVDA, AMD, MSFT, GOOGL, META, ARM, PLTR. חפש חדשות היום, תנועות מחיר, המלצות אנליסטים. כתוב דוח בעברית עם המלצה לכל מניה ושלח לטלגרם.",
        ),
        cl.Starter(
            label="🔥 GitHub Trending AI",
            message="הבא את הפרויקטים הטרנדיים ב-GitHub היום עם get_github_trending category=ai. עבור כל פרויקט כתוב בעברית: שם, מה הוא עושה, כמה כוכבים, ולמה מעניין. שלח לטלגרם.",
        ),
        cl.Starter(
            label="📋 משימות מתוזמנות",
            message="/scheduled",
        ),
        cl.Starter(
            label="🤖 בנה פרויקט",
            message="/agent ",
        ),
        cl.Starter(
            label="🔬 מחקר עמוק (Sub-agents)",
            message="/deep ",
        ),
        cl.Starter(
            label="🔭 מחקר איטרטיבי",
            message="/research ",
        ),
    ]


def _make_tool_callbacks() -> tuple[Any, Any, list[Any]]:
    """Build (on_tool_start, on_tool_end, media) callbacks bound to Chainlit steps.

    A single message turn can trigger several tool calls; we track each open
    step by id() of the handle returned from on_tool_start. Any image files a
    tool produces (e.g. ComfyUI renders) are collected into ``media`` so the
    caller can show them inline in the chat, like ChatGPT.
    """
    media: list[Any] = []

    async def on_tool_start(name: str, args: "dict[str, Any] | str") -> cl.Step:
        step = cl.Step(name=name, type="tool")
        step.input = (
            args if isinstance(args, str)
            else json.dumps(args, ensure_ascii=False, indent=2)
        )
        await step.send()
        return step

    async def on_tool_end(step: cl.Step, result: str) -> None:
        # Pull any generated image files out of the result and attach them both
        # to this step and to the running media list (shown in the main thread).
        images, _others = extract_media_paths(result)
        for path in images:
            if path.exists():
                media.append(cl.Image(path=str(path), name=path.name, display="inline"))
        step.output = result
        await step.update()

    return on_tool_start, on_tool_end, media


def _make_approver():
    """Build an `on_approval(name, args)` callback that asks the user Approve/Deny
    before a destructive tool runs (HITL). Deny on timeout — in an interactive UI,
    no answer means "don't do it".

    Supports "✅ Approve all": once clicked, a session flag auto-approves every
    later gated tool for the rest of the session (no more prompts). Reset by
    starting a new chat (safety re-arms) or `/hitl on`.
    """

    async def on_approval(name: str, args: "dict[str, Any] | str") -> bool:
        # "Approve all" already chosen this session → don't prompt again.
        if cl.user_session.get("hitl_approve_all"):
            return True
        try:
            args_str = (
                args if isinstance(args, str)
                else json.dumps(args, ensure_ascii=False, indent=2)
            )
        except Exception:  # noqa: BLE001
            args_str = str(args)
        if len(args_str) > 800:
            args_str = args_str[:800] + "\n… (truncated)"
        # Heads-up toast so the user notices the gate even if the tab isn't focused.
        if config.HITL_NOTIFY:
            notify.notify(
                "Zero Agent — approval needed",
                f"Run `{name}`? Open the chat to Approve or Deny.",
            )
        # timeout=0 → wait FOREVER. The user chose no auto-deny: a missed prompt
        # should pause the run indefinitely, not silently kill it. Chainlit needs
        # a positive int, so 0 maps to a very large value (~1 week ≈ forever).
        ask_timeout = config.HITL_TIMEOUT if config.HITL_TIMEOUT > 0 else 7 * 24 * 3600
        res = await cl.AskActionMessage(
            content=f"⚠️ The agent wants to run **`{name}`**:\n```\n{args_str}\n```\nApprove this action?",
            actions=[
                cl.Action(name="approve", payload={"decision": "approve"}, label="✅ Approve"),
                cl.Action(name="approve_all", payload={"decision": "approve_all"},
                          label="✅✅ Approve all (this session)"),
                cl.Action(name="deny", payload={"decision": "deny"}, label="🚫 Deny"),
            ],
            timeout=ask_timeout,
        ).send()
        if not res:
            return False  # explicitly dismissed → deny (timeout is effectively off)
        decision = res.get("payload", {}).get("decision") or res.get("name")
        if decision == "approve_all":
            cl.user_session.set("hitl_approve_all", True)
            await cl.Message(
                content="✅ Approvals are now automatic for the rest of this session. "
                "Start a new chat (or `/hitl on`) to re-enable prompts."
            ).send()
            return True
        return decision == "approve"

    return on_approval


# --- Local chat-history persistence (ChatGPT-style sidebar) -----------------
# Registers the on-disk SQLite data layer so every conversation is saved and can
# be reopened from the left sidebar. 100% local/offline. Returns None (and
# Chainlit simply runs without history) if persistence is disabled.
if config.PERSIST_CHATS:

    @cl.data_layer
    def _data_layer():  # noqa: D401 - Chainlit hook
        return persistence.build_data_layer()

    @cl.password_auth_callback
    def _auth(username: str, password: str) -> "cl.User | None":
        """Local-only login that ties saved chats to a single user.

        Credentials are checked against config (env-overridable) and never leave
        this machine. Auth is required by Chainlit to attach threads to a user
        for the history sidebar.
        """
        if username == config.AUTH_USERNAME and password == config.AUTH_PASSWORD:
            return cl.User(identifier=username, metadata={"role": "owner"})
        return None


# Background distiller client (small/fast model) reused across turns for the
# self-improving memory. Created lazily so importing the module stays cheap.
_distiller: "OllamaClient | None" = None


def _get_distiller() -> OllamaClient:
    """Return the shared small-model client used for memory distillation."""
    global _distiller
    if _distiller is None:
        _distiller = OllamaClient(model=config.MEMORY_DISTILL_MODEL)
    return _distiller


async def _init_session(selected: str) -> "tuple[OllamaClient, str] | None":
    """Build the client + agent + memory for a session and store them.

    Shared by a fresh chat (``on_chat_start``) and a resumed one
    (``on_chat_resume``). Returns ``(client, resolved_model)`` or ``None`` if
    Ollama is unreachable.
    """
    client = OllamaClient(model=selected)
    model = await client.resolve_model(selected)
    if model is None:
        await client.aclose()
        return None

    agent = BaseAgent(client, registry)
    # The memory + project stores keep their OWN long-lived embedding client
    # (created lazily on first use) — NOT this per-session client, which is
    # closed by on_chat_end. Binding them to the session client caused
    # "Cannot send a request, as the client has been closed" on the next turn
    # after a chat ended or the app hot-reloaded.
    get_store()
    projects.get_project_store()

    cl.user_session.set("client", client)
    cl.user_session.set("agent", agent)
    cl.user_session.set("history", [])
    # Default the session's active project to the last one used (if any).
    cl.user_session.set("project", projects.get_active_project())
    # Spoken-reply toggle: default from config, only meaningful if the engine is
    # actually available (model files present + import OK).
    cl.user_session.set("tts", config.TTS_ENABLED and tts.available())
    # Voice-input language default (overridable in ⚙️). English by default since
    # auto-detect was mis-hearing English as Hebrew.
    cl.user_session.set("stt_lang", config.STT_LANGUAGE or _DEFAULT_STT_LANG)
    # Wake word (conversation mode): on only if configured at startup, else off
    # (every spoken utterance is a turn). Toggle live in ⚙️.
    cl.user_session.set(
        "voice_wake", list(config.VOICE_WAKE_WORDS) if config.VOICE_WAKE_WORDS else []
    )
    # Instant voice: spoken turns answer directly (thinking off) for low latency.
    cl.user_session.set("voice_instant", config.VOICE_INSTANT)
    return client, model


@cl.on_chat_start
async def start() -> None:
    # Initialize the session first (Ollama may pick a fallback model). On a fresh
    # chat there is no prior settings input, so start from the default model.
    result = await _init_session(DEFAULT_MODEL)
    if result is None:
        await cl.Message(
            content=(
                "⚠️ Could not reach Ollama or no models are installed.\n\n"
                f"- Start the server: `ollama serve`\n"
                f"- Pull a tool-capable model: `ollama pull {DEFAULT_MODEL}`"
            )
        ).send()
        return
    _client, model = result

    # Start the idle-hygiene watcher once: when the machine sits unused, it runs the
    # memory consolidation pass (dedup + decay + contradiction) automatically. No-op
    # if disabled/unsupported, and it never spawns a second watcher.
    idle_hygiene.start_idle_watcher(get_store(), client=_client)

    # Auto-connect MCP servers configured in .env (ZERO_AGENT_MCP_SERVERS).
    # Runs only once per process — subsequent on_chat_start calls skip already-
    # connected servers. Failures are logged; they never crash the UI.
    if config.MCP_ENABLED and config.MCP_SERVERS and not _mcp_client.list_connections():
        asyncio.create_task(_mcp_client.autoconnect_from_config())

    # Start the task scheduler (idempotent — safe to call on every new session).
    # Wire up the runner so scheduled tasks go through the full agent pipeline
    # AND deliver the result ONLY to Telegram (UI is usually closed at fire time).
    if not task_scheduler.get_scheduler().running:
        from orchestrator import telegram_notify as _tg_notify
        async def _scheduled_task_runner(prompt: str, model: str = "") -> None:
            """Run a scheduled prompt through Zero; send result to Telegram.

            Uses the task-specific model if provided, otherwise falls back to
            the active session model or DEFAULT_MODEL. Always sends to Telegram
            so reports arrive even when the Chainlit UI tab is closed.
            """
            try:
                # Pick model: task-level override > active session > default
                chosen_model = (
                    model.strip()
                    or (cl.user_session.get("model") if cl.user_session else None)
                    or config.DEFAULT_MODEL
                )
                task_client = OllamaClient(model=chosen_model)
                agent = BaseAgent(task_client, registry)
                history: list[dict[str, Any]] = []
                result = await agent.run(prompt, history)
                # Send to Telegram — primary delivery channel for scheduled tasks
                header = f"⏰ *Scheduled*  `{chosen_model}`\n_{prompt[:100]}_\n\n"
                await _tg_notify.send_to_owner(header + result)
                # Also echo to UI if a session happens to be open
                try:
                    await cl.Message(content=f"⏰ **Scheduled task:**\n\n{result}").send()
                except Exception:
                    pass
            except Exception:
                logger.exception("scheduled task runner failed")
        task_scheduler.set_task_runner(_scheduled_task_runner)
        task_scheduler.start()

    # Build the project dropdown: "(no project)" + every existing project. The
    # active project (last used) is pre-selected.
    project_values = ["(no project)"] + projects.get_project_store().list_projects()
    active = cl.user_session.get("project")
    project_index = (
        project_values.index(active) if active in project_values else 0
    )

    # Render the settings panel: model + active-project dropdowns.
    await cl.ChatSettings(
        [
            Select(
                id="model",
                label="Active Model",
                values=MODEL_OPTIONS,
                initial_index=MODEL_OPTIONS.index(model)
                if model in MODEL_OPTIONS
                else 0,
            ),
            Select(
                id="project",
                label="Active Project",
                values=project_values,
                initial_index=project_index,
            ),
            Switch(
                id="speak",
                label="🔊 Speak replies (local voice)",
                initial=cl.user_session.get("tts") or False,
            ),
            Switch(
                id="voice_instant",
                label="⚡ Instant voice (skip thinking on spoken turns)",
                initial=cl.user_session.get("voice_instant", config.VOICE_INSTANT),
            ),
            Switch(
                id="wake_word",
                label='🗣️ Wake word ("זירו …") — only reply when addressed',
                initial=bool(cl.user_session.get("voice_wake")),
            ),
            Select(
                id="voice_lang",
                label="🎤 Voice input language",
                values=_VOICE_LANG_LABELS,
                initial_index=_voice_lang_index(cl.user_session.get("stt_lang")),
            ),
        ]
    ).send()

    # Quick-action buttons in the composer (🔭 Research / 🎨 Image / 🧠 Remember).
    await cl.context.emitter.set_commands(CHAT_COMMANDS)

    note = ""
    if model != DEFAULT_MODEL:
        note = (
            f"\n\n_(`{DEFAULT_MODEL}` not installed — using `{model}`. "
            f"Run `ollama pull {DEFAULT_MODEL}` for the full model.)_"
        )
    # Cross-session task context: load what Zero was last working on and inject
    # it into the history so the first turn is already oriented.
    if config.SESSION_CONTEXT_ENABLED:
        history: list[dict[str, Any]] = cl.user_session.get("history")
        block = session_context.build_session_block(history_has_summary=False)
        session_context.inject_session_message(history, block)

    # No welcome message — Chainlit starters serve as the welcome screen.
    # Sending any message here hides the starters immediately.


@cl.on_settings_update
async def update_settings(settings: dict[str, Any]) -> None:
    """Apply settings changes (active model and/or active project) live.

    BaseAgent reads ``self.client.model`` fresh on every turn, so mutating the
    session client's ``model`` is enough to take effect on the next message. The
    active project is stored on the session and persisted for future chats.
    """
    client: OllamaClient | None = cl.user_session.get("client")
    if client is None:
        return

    # --- model ---
    new_model = settings.get("model")
    if new_model and new_model != client.model:
        client.model = new_model
        await cl.Message(content=f"✅ Active model switched to `{new_model}`.").send()

    # --- project ---
    new_project = settings.get("project")
    if new_project is not None:
        chosen = None if new_project == "(no project)" else new_project
        if chosen != cl.user_session.get("project"):
            cl.user_session.set("project", chosen)
            projects.set_active_project(chosen)
            label = f"`{chosen}`" if chosen else "_(none)_"
            await cl.Message(content=f"📁 Active project set to {label}.").send()

    # --- speak replies (local TTS) ---
    if "speak" in settings:
        want = bool(settings.get("speak"))
        if want and not tts.available():
            await cl.Message(
                content="⚠️ Voice model not found — speech stays off. "
                "(Expected the Kokoro files under `data/tts/`.)"
            ).send()
            cl.user_session.set("tts", False)
        elif want != bool(cl.user_session.get("tts")):
            cl.user_session.set("tts", want)
            await cl.Message(
                content=("🔊 Spoken replies ON." if want else "🔇 Spoken replies OFF.")
            ).send()

    # --- voice input language ---
    if "voice_lang" in settings:
        code = _VOICE_LANGS.get(settings.get("voice_lang"), "")
        if code != (cl.user_session.get("stt_lang") or ""):
            cl.user_session.set("stt_lang", code)
            shown = settings.get("voice_lang")
            await cl.Message(content=f"🎤 Voice input language: **{shown}**.").send()

    # --- instant voice (skip thinking on spoken turns) ---
    if "voice_instant" in settings:
        want = bool(settings.get("voice_instant"))
        if want != bool(cl.user_session.get("voice_instant")):
            cl.user_session.set("voice_instant", want)
            await cl.Message(
                content=(
                    "⚡ Instant voice ON — spoken replies skip the thinking step (snappier)."
                    if want else
                    "🧠 Instant voice OFF — spoken replies think first (slower, deeper)."
                )
            ).send()

    # --- wake word (conversation mode) ---
    if "wake_word" in settings:
        want = bool(settings.get("wake_word"))
        if want != bool(cl.user_session.get("voice_wake")):
            cl.user_session.set("voice_wake", list(_DEFAULT_WAKE) if want else [])
            await cl.Message(
                content=(
                    '🗣️ Wake word ON — I\'ll only reply when you say "זירו".'
                    if want else "🗣️ Wake word OFF — every spoken line is a turn."
                )
            ).send()


# UI notices emitted as assistant messages (welcome banner, "model switched",
# project/memory confirmations, mode toggles). They are NOT real conversation
# turns, so on resume they must be filtered out of the rebuilt history — left in,
# they pollute the model's context (and the banner's giant tool list especially).
_UI_NOTICE_PREFIXES = (
    "**Zero Agent**",
    "✅ Active model switched",
    "📁",
    "🧠",
    "🗑️",
    "⚠️",
    "🔭",
    "🎨",
    "🎬",
)
# "✅ Active model switched to `X`." and the banner "... · model `X` ·".
_SWITCH_MODEL_RE = re.compile(r"Active model switched to `([^`]+)`")
_BANNER_MODEL_RE = re.compile(r"model `([^`]+)`")
# Trailing display-only response-time badge: "\n\n`⏱️ 12.3s`".
_TIME_BADGE_RE = re.compile(r"\n\n`⏱️[^`]*`\s*$")


def _is_ui_notice(text: str) -> bool:
    return text.startswith(_UI_NOTICE_PREFIXES)


def _model_from_notice(text: str) -> str | None:
    """Extract the active model from a banner or 'model switched' notice, if any."""
    m = _SWITCH_MODEL_RE.search(text)
    if m:
        return m.group(1)
    if text.startswith("**Zero Agent**"):
        b = _BANNER_MODEL_RE.search(text)
        if b:
            return b.group(1)
    return None


def _strip_time_badge(text: str) -> str:
    return _TIME_BADGE_RE.sub("", text)


@cl.on_chat_resume
async def resume(thread: "dict[str, Any]") -> None:
    """Reopen a saved conversation from the sidebar and restore its context.

    Rebuilds the in-memory message history from the persisted steps so the agent
    "remembers" the whole thread and can continue it seamlessly. UI notices
    (banner / model-switch / project confirmations) are skipped so they don't
    pollute context, and the model the thread was actually using is restored
    (otherwise a resume silently falls back to DEFAULT_MODEL).
    """
    result = await _init_session(DEFAULT_MODEL)
    if result is None:
        await cl.Message(
            content="⚠️ Could not reach Ollama — start it, then reopen this chat."
        ).send()
        return
    client, _model = result

    # Restore the composer quick-action buttons on a resumed chat too.
    await cl.context.emitter.set_commands(CHAT_COMMANDS)

    history: list[dict[str, Any]] = cl.user_session.get("history")
    restored_model: str | None = None
    for step in thread.get("steps") or []:
        step_type = step.get("type")
        content = (step.get("output") or "").strip()
        if not content:
            continue
        if step_type == "user_message":
            history.append({"role": "user", "content": content})
        elif step_type == "assistant_message":
            found = _model_from_notice(content)
            if found:
                restored_model = found  # remember the thread's active model
            if _is_ui_notice(content):
                continue  # skip banners / switch / project notices
            history.append(
                {"role": "assistant", "content": _strip_time_badge(content)}
            )

    # Switch back to the model the thread was using (resolve so a missing model
    # falls back gracefully instead of erroring on the first message).
    if restored_model and restored_model != client.model:
        resolved = await client.resolve_model(restored_model)
        if resolved:
            client.model = resolved

    # Cross-session task context: inject session state + journal into the resumed
    # history. Skip the persisted summary if the thread already contains one
    # (rebuilt from the stored steps above), to avoid stale duplication.
    if config.SESSION_CONTEXT_ENABLED:
        has_summary = session_context._history_has_rolling_summary(history)
        block = session_context.build_session_block(history_has_summary=has_summary)
        session_context.inject_session_message(history, block)

    logging.getLogger("zero_agent.ui").info(
        "resumed thread: %d restored messages, model=%s", len(history), client.model
    )


async def _handle_project_command(text: str) -> bool:
    """Handle project slash-commands. Returns True if a command was handled.

    Commands:
        /projects                 — list all projects + the active one
        /project <name>           — switch to (or create) a project
        /project                  — clear the active project
        /learn <text>             — add knowledge to the active project
        /forget-project <name>    — delete a project and all its knowledge
    """
    store = projects.get_project_store()
    parts = text.split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd == "/projects":
        names = store.list_projects()
        active = cl.user_session.get("project")
        if names:
            listing = "\n".join(
                f"- `{n}` ({store.count(n)} items)"
                + ("  ⬅️ active" if n == active else "")
                for n in names
            )
        else:
            listing = "_(no projects yet — create one with `/project <name>`)_"
        await cl.Message(content=f"📁 **Projects**\n{listing}").send()
        return True

    if cmd == "/project":
        chosen = arg or None
        cl.user_session.set("project", chosen)
        projects.set_active_project(chosen)
        if chosen:
            await cl.Message(
                content=(
                    f"📁 Active project: `{chosen}` "
                    f"({store.count(chosen)} items). Add knowledge with "
                    f"`/learn <text>`."
                )
            ).send()
        else:
            await cl.Message(content="📁 Active project cleared.").send()
        return True

    if cmd == "/learn":
        active = cl.user_session.get("project")
        if not active:
            await cl.Message(
                content="⚠️ No active project. Set one first: `/project <name>`."
            ).send()
            return True
        if not arg:
            await cl.Message(content="⚠️ Usage: `/learn <text to remember>`.").send()
            return True
        try:
            await store.add_knowledge(active, arg)
            await cl.Message(
                content=f"🧠 Added to `{active}` (now {store.count(active)} items)."
            ).send()
        except Exception as exc:  # noqa: BLE001
            await cl.Message(content=f"⚠️ Could not save: {exc}").send()
        return True

    if cmd == "/forget-project":
        if not arg:
            await cl.Message(content="⚠️ Usage: `/forget-project <name>`.").send()
            return True
        removed = store.delete_project(arg)
        if cl.user_session.get("project") == arg:
            cl.user_session.set("project", None)
            projects.set_active_project(None)
        await cl.Message(
            content=f"🗑️ Deleted project `{arg}` ({removed} items removed)."
        ).send()
        return True

    if cmd == "/always":
        # Standing instructions applied to EVERY answer (always-injected, not
        # similarity-gated). Usually captured automatically from your feedback;
        # this command just lets you view / add / clear them by hand.
        mem = get_store()
        if arg.lower() == "clear":
            n = mem.delete_by_tag(auto_memory.PREFERENCE_TAG)
            await cl.Message(content=f"🧹 Cleared {n} standing instruction(s).").send()
            return True
        if arg:
            try:
                await mem.remember(arg, tags=auto_memory.PREFERENCE_TAG)
                await cl.Message(content=f"📌 Standing instruction added: _{arg}_").send()
            except Exception as exc:  # noqa: BLE001
                await cl.Message(content=f"⚠️ Could not save: {exc}").send()
            return True
        rules = mem.list_by_tag(auto_memory.PREFERENCE_TAG, limit=50)
        if rules:
            listing = "\n".join(f"- {r['text']}" for r in rules)
            body = (
                f"📌 **Standing instructions** (always applied):\n{listing}\n\n"
                "_Add one with `/always <text>`, clear all with `/always clear`. "
                "These are also learned automatically from your feedback._"
            )
        else:
            body = (
                "📌 No standing instructions yet. They're learned automatically "
                "from your feedback, or add one with `/always <text>` "
                "(e.g. `/always always cite your sources`)."
            )
        await cl.Message(content=body).send()
        return True

    return False


_AGENT_PREFIXES = ("/agent", "/build")


async def _handle_agent_command(text: str) -> bool:
    """Run a multi-step goal through the Supervisor-Worker loop with a live
    TaskList sidebar. Returns True if it handled the message.

    Usage: ``/agent <goal>`` (or ``/build <goal>``). The Planner (reasoning
    model) breaks the goal into steps; the Worker (fast coder) executes each one;
    the RealityVerifier checks the result actually landed on disk; failures
    trigger retry then replan. Each tool call streams into the chat as a step.
    """
    lowered = text.lower()
    if not any(lowered == p or lowered.startswith(p + " ") for p in _AGENT_PREFIXES):
        return False
    goal = text.split(" ", 1)[1].strip() if " " in text else ""
    if not goal:
        await cl.Message(
            content="Usage: `/agent <goal>` — describe what to build or do, "
            "e.g. `/agent create a Hebrew coffee-shop landing page in C:\\\\projects\\\\coffee`."
        ).send()
        return True

    # Default workspace so files never land at a drive root if the goal omits a path.
    workspace = os.path.join(os.path.dirname(config.TASKS_DIR), "workspace")
    os.makedirs(workspace, exist_ok=True)

    # Dedicated Worker on the fast coder model (Planner uses SUPERVISOR_MODEL).
    worker = BaseAgent(OllamaClient(model=config.WORKER_MODEL), registry)
    on_tool_start, on_tool_end, _media = _make_tool_callbacks()

    task_list = cl.TaskList()
    task_list.status = "Planning…"
    await task_list.send()
    tasks_by_id: "dict[str, cl.Task]" = {}

    async def on_event(kind: str, payload: str) -> None:
        if kind == "planning":
            task_list.status = "Planning…"
            await task_list.send()
        elif kind == "plan_ready":
            for line in payload.splitlines():
                m = re.match(r"\s*(\d+)\.\s*(.+)", line)
                if m:
                    t = cl.Task(title=m.group(2).strip()[:90], status=cl.TaskStatus.READY)
                    tasks_by_id[m.group(1)] = t
                    await task_list.add_task(t)
            task_list.status = "Running…"
            await task_list.send()
        elif kind in ("task_start", "task_done", "task_failed"):
            m = re.match(r"\s*\[(\d+)\]\s*(.*)", payload)
            if not m:
                return
            tid, rest = m.group(1), m.group(2)
            task = tasks_by_id.get(tid)
            if task is None:
                task = cl.Task(title=rest[:90], status=cl.TaskStatus.READY)
                tasks_by_id[tid] = task
                await task_list.add_task(task)
            task.status = {
                "task_start": cl.TaskStatus.RUNNING,
                "task_done": cl.TaskStatus.DONE,
                "task_failed": cl.TaskStatus.FAILED,
            }[kind]
            await task_list.send()
        elif kind == "replan":
            task_list.status = f"Replanning ({payload})…"
            await task_list.send()
            await cl.Message(content=f"🔄 Replanning: {payload}").send()
        elif kind == "synthesizing":
            task_list.status = "Writing final report…"
            await task_list.send()
        elif kind == "error":
            await cl.Message(content=f"⚠️ {payload}").send()

    sup = Supervisor(
        worker,
        registry,
        cwd=workspace,
        on_event=on_event,
        on_tool_start=on_tool_start,
        on_tool_end=on_tool_end,
        on_approval=_make_approver(),
    )
    await cl.Message(
        content=f"🤖 **Agent goal:** {goal}\n\n_Worker: `{config.WORKER_MODEL}` · "
        f"Planner: `{config.SUPERVISOR_MODEL}` · workspace: `{workspace}`_"
    ).send()
    # Run as a cancellable task so the ⏹ Stop button (see @cl.on_stop) can abort it.
    run_task = asyncio.create_task(sup.run_goal(goal))
    cl.user_session.set("run_task", run_task)
    try:
        status = await run_task
    except asyncio.CancelledError:
        task_list.status = "Stopped"
        await task_list.send()
        await cl.Message(content="⏹️ Agent run stopped by you.").send()
        return True
    except Exception as exc:  # noqa: BLE001 - never tear down the session
        logging.getLogger("zero_agent.ui").exception("agent run failed: %s", exc)
        task_list.status = "Failed"
        await task_list.send()
        await cl.Message(
            content=f"⚠️ Agent run failed: {type(exc).__name__}: {exc}"
        ).send()
        return True
    finally:
        cl.user_session.set("run_task", None)

    ok = sup.task_manager.all_succeeded()
    task_list.status = "Done" if ok else "Stopped"
    await task_list.send()
    await cl.Message(content=("✅ " if ok else "⚠️ ") + status).send()
    return True


_RESEARCH_PREFIXES = ("/research", "/deepresearch")
_DEEP_PREFIXES = ("/deep",)


async def _handle_research_command(text: str) -> bool:
    """Run the iterative ResearchAgent (search → assess gaps → search more →
    calibrated synthesis) with a live TaskList. Returns True if it handled the msg.
    """
    lowered = text.lower()
    if not any(lowered == p or lowered.startswith(p + " ") for p in _RESEARCH_PREFIXES):
        return False
    question = text.split(" ", 1)[1].strip() if " " in text else ""
    if not question:
        await cl.Message(
            content="Usage: `/research <question>` — multi-round, source-tiered, "
            "calibrated research (best with qwen3:32b selected)."
        ).send()
        return True

    session_client = cl.user_session.get("client")
    model = session_client.model if session_client else config.SUPERVISOR_MODEL

    task_list = cl.TaskList()
    task_list.status = "Planning research…"
    await task_list.send()
    tasks: "dict[str, cl.Task]" = {}

    async def on_event(kind: str, payload: str) -> None:
        if kind == "planning":
            task_list.status = "Planning research…"
            await task_list.send()
        elif kind == "plan_ready":
            for line in payload.splitlines():
                m = re.match(r"\s*\d+\.\s*(.+)", line)
                if m:
                    q = m.group(1).strip()
                    t = cl.Task(title=q[:90], status=cl.TaskStatus.READY)
                    tasks[q.lower()] = t
                    await task_list.add_task(t)
            task_list.status = "Researching…"
            await task_list.send()
        elif kind in ("query", "query_done"):
            q = payload.strip()
            t = tasks.get(q.lower())
            if t is None:
                t = cl.Task(title=q[:90], status=cl.TaskStatus.READY)
                tasks[q.lower()] = t
                await task_list.add_task(t)
            t.status = cl.TaskStatus.RUNNING if kind == "query" else cl.TaskStatus.DONE
            await task_list.send()
        elif kind == "gaps":
            task_list.status = f"Following up: {payload}"
            await task_list.send()
        elif kind == "synthesizing":
            task_list.status = "Synthesizing (calibrated)…"
            await task_list.send()
        elif kind == "done":
            task_list.status = "Done"
            await task_list.send()
        elif kind == "error":
            await cl.Message(content=f"⚠️ {payload}").send()

    research_client = OllamaClient(model=model)
    agent = ResearchAgent(research_client, registry, on_event=on_event)
    await cl.Message(
        content=f"🔬 **Researching:** {question}\n\n_iterative · source-tiered · "
        f"calibrated · model `{model}`_"
    ).send()
    # Cancellable so the ⏹ Stop button (see @cl.on_stop) can abort the research.
    run_task = asyncio.create_task(agent.investigate(question))
    cl.user_session.set("run_task", run_task)
    try:
        answer = await run_task
        await cl.Message(content=answer).send()
    except asyncio.CancelledError:
        task_list.status = "Stopped"
        await task_list.send()
        await cl.Message(content="⏹️ Research stopped by you.").send()
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("zero_agent.ui").exception("research failed: %s", exc)
        task_list.status = "Failed"
        await task_list.send()
        await cl.Message(content=f"⚠️ Research failed: {type(exc).__name__}: {exc}").send()
    finally:
        cl.user_session.set("run_task", None)
        await research_client.aclose()
    return True


async def _handle_deep_command(text: str) -> bool:
    """Parallel multi-angle deep research: runs 5 angles concurrently, synthesizes,
    saves to active project, names thread with 🔬 prefix. Returns True if handled.

    Usage: /deep <question>
    """
    lowered = text.lower()
    if not any(lowered == p or lowered.startswith(p + " ") for p in _DEEP_PREFIXES):
        return False

    question = text.split(" ", 1)[1].strip() if " " in text else ""
    if not question:
        await cl.Message(
            content=(
                "Usage: `/deep <שאלה>` — מחקר מקבילי מ-5 מקורות + סינתזה + שמירה לפרויקט.\n"
                "לדוגמה: `/deep מצב שוק ה-AI Agents ב-2026`"
            )
        ).send()
        return True

    session_client: OllamaClient | None = cl.user_session.get("client")
    client = session_client or OllamaClient(model=config.SUPERVISOR_MODEL)
    active_project = cl.user_session.get("active_project") or ""

    # Rename thread with 🔬 prefix so sidebar JS can group it
    thread_id = cl.context.session.thread_id
    short_q = question[:60]
    try:
        import chainlit as _cl_inner
        await _cl_inner.Thread.update(thread_id, name=f"🔬 {short_q}", tags=["research"])
    except Exception:
        pass  # thread rename is best-effort

    # Progress message
    angles_label = "📚 אקדמי · 🏢 תעשייה · 💻 GitHub · 📰 חדשות · 📖 רקע"
    intro = await cl.Message(
        content=(
            f"🔬 **מחקר עמוק:** {question}\n\n"
            f"_מריץ {5} sub-agents במקביל: {angles_label}_"
        )
    ).send()

    from orchestrator.deep_research import run_deep_research

    try:
        result = await run_deep_research(question, client, project_name=active_project)
    except Exception as exc:
        logging.getLogger("zero_agent.ui").exception("deep research failed: %s", exc)
        await cl.Message(content=f"⚠️ מחקר נכשל: {exc}").send()
        return True

    # Build final report message
    save_note = (
        f"\n\n💾 _נשמר לפרויקט: **{active_project}**_"
        if result.get("saved") else ""
    )
    elapsed = result.get("elapsed", 0)

    report_md = (
        f"{result['report']}\n\n"
        f"---\n_⏱️ {elapsed}s · 5 sub-agents במקביל{save_note}_"
    )
    await cl.Message(content=report_md).send()
    return True


@cl.on_message
async def on_message(message: cl.Message) -> None:
    # Guard against duplicate sends: if a turn is already in flight, drop the
    # extra send silently. This prevents the double-Ollama-request that causes
    # template-parser errors and context corruption when the user re-sends a
    # message because the UI appeared stuck.
    if cl.user_session.get("_processing"):
        return
    cl.user_session.set("_processing", True)
    try:
        await _handle_user_message(message)
    finally:
        cl.user_session.set("_processing", False)


async def _handle_user_message(message: cl.Message, from_voice: bool = False) -> None:
    agent: BaseAgent | None = cl.user_session.get("agent")
    if agent is None:
        await cl.Message(
            content="Agent not initialized — is Ollama running? Reload the page."
        ).send()
        return

    # Composer quick-action buttons (🔭 Research / 🎨 Image / 🧠 Remember).
    # message.command holds the tapped button's id (or None for a plain message).
    command = message.command
    if command == "Remember":
        text = message.content.strip()
        if not text:
            await cl.Message(
                content="🧠 Type what you'd like me to remember, then tap 🧠 Remember."
            ).send()
            return
        try:
            await get_store().remember(text, tags="manual")
            await cl.Message(content="🧠 Saved to long-term memory.").send()
        except Exception as exc:  # noqa: BLE001
            await cl.Message(content=f"⚠️ Could not save to memory: {exc}").send()
        return

    # 🤖 Agent button: run the typed text as an autonomous multi-step goal.
    if command == "Agent":
        goal = message.content.strip()
        if not goal:
            await cl.Message(
                content="🤖 Type a goal, then tap **Agent** — e.g. "
                '"build a Kotlin Android todo app at C:\\\\projects\\\\todo".'
            ).send()
            return
        await _handle_agent_command(f"/agent {goal}")
        return

    # Show what the agent has learned from its mistakes (phase 3: transparency).
    if message.content.strip().lower() == "/lessons":
        await cl.Message(content=lessons.list_lessons(get_store())).send()
        return

    # Task status: show what Zero is currently working on across sessions.
    low = message.content.strip().lower()
    if low in ("/status", "/task"):
        state = session_context.load_session_state()
        journal = session_context._load_journal_tail(10)
        if state.get("active_task"):
            lines = [f"**Active task:** {state['active_task']}"]
            if state.get("current_step"):
                lines.append(f"**Current step:** {state['current_step']}")
            if state.get("next_step"):
                lines.append(f"**Next step:** {state['next_step']}")
            if state.get("notes"):
                lines.append(f"**Notes:** {state['notes']}")
            if state.get("updated_at"):
                lines.append(f"_Last updated: {state['updated_at']}_")
        else:
            lines = ["_No active task recorded._"]
        if journal:
            lines.append("\n**Recent actions:**")
            lines.extend(f"- {e}" for e in journal)
        await cl.Message(content="\n".join(lines)).send()
        return

    # Clear session task context (when starting a completely new project).
    if low == "/cleartask":
        session_context.clear_session_state()
        await cl.Message(content="✅ Session task context cleared.").send()
        return

    # Scheduled tasks: /scheduled lists all; /cancel <id> removes one.
    if low in ("/scheduled", "/tasks", "/schedule"):
        tasks = task_scheduler.list_tasks()
        if not tasks:
            await cl.Message(content="⏰ No scheduled tasks. Use the ⏰ Schedule button or `schedule_task()`.").send()
        else:
            lines = ["⏰ **Scheduled Tasks**\n"]
            for t in tasks:
                sched = t.get("cron") or t.get("run_at", "?")
                nxt = t.get("next_run", "?")
                lines.append(
                    f"• `{t['id']}` — **{t.get('label','(no label)')}**\n"
                    f"  Schedule: `{sched}` | Next: {nxt}\n"
                    f"  Prompt: _{t.get('prompt','')[:70]}_"
                )
            await cl.Message(content="\n\n".join(lines)).send()
        return

    if low.startswith("/cancel "):
        task_id = message.content.strip().split(None, 1)[1].strip()
        removed = task_scheduler.cancel_task(task_id)
        if removed:
            await cl.Message(content=f"✅ Task `{task_id}` cancelled.").send()
        else:
            await cl.Message(content=f"Task `{task_id}` not found. Check `/scheduled` for valid ids.").send()
        return

    # Check a background ComfyUI render job: /check_job <job_id>
    if low.startswith("/check_job") or low.startswith("/checkjob"):
        from orchestrator.tools.media_tools import _JOBS
        parts = message.content.strip().split(None, 1)
        job_id = parts[1].strip() if len(parts) > 1 else ""
        if not job_id:
            if _JOBS:
                lines = []
                for jid, j in _JOBS.items():
                    elapsed = int(time.time() - j.created)
                    lines.append(f"• `{jid}` — **{j.status}** ({elapsed}s) — {j.prompt_text[:60]}")
                await cl.Message(content="**Active render jobs:**\n" + "\n".join(lines)).send()
            else:
                await cl.Message(content="No render jobs in this session. Use `/check_job <job_id>`.").send()
            return
        job = _JOBS.get(job_id)
        if job is None:
            known = ", ".join(f"`{k}`" for k in _JOBS) or "none"
            await cl.Message(content=f"No job `{job_id}` found. Known: {known}").send()
            return
        elapsed = int(time.time() - job.created)
        if job.status in {"queued", "running"}:
            await cl.Message(content=f"🎬 Job `{job.id}` is **{job.status}** — {elapsed}s elapsed. Check again soon.").send()
        elif job.status == "error":
            await cl.Message(content=f"❌ Job `{job.id}` **failed** after {elapsed}s:\n{job.error or job.result}").send()
        else:
            await cl.Message(content=f"✅ Job `{job.id}` **done** in {elapsed}s.\n{job.result}").send()
        return

    # Memory hygiene: dedup near-identical memories (+ decay stale auto-facts if a
    # TTL is set). `/hygiene` runs it; `/hygiene dry` previews without deleting.
    if low.startswith("/hygiene") or low.startswith("/cleanmemory"):
        dry = "dry" in low or "preview" in low
        # A judge enables the LLM contradiction phase (older of a conflicting pair is
        # dropped); on a dry-run it still computes what WOULD go, deleting nothing.
        judge = memory_hygiene.make_contradiction_judge()
        r = await asyncio.to_thread(
            memory_hygiene.consolidate, get_store(), dry_run=dry, judge=judge
        )
        verb = "Would remove" if dry else "Removed"
        await cl.Message(
            content=(f"🧹 Memory hygiene{' (dry-run)' if dry else ''} — examined "
                     f"{r['examined']}. {verb} {r['removed_duplicates']} duplicate(s) + "
                     f"{r['removed_expired']} stale + {r['removed_contradictions']} "
                     f"contradiction(s). {r['remaining']} kept.")
        ).send()
        return

    # Self-improvement loop: analyses task history for failure patterns and converts
    # them into standing RULES (same channel as /always). Works alongside /hygiene.
    if low.startswith("/improve"):
        arg = low[len("/improve"):].strip()
        dry = arg in ("dry", "preview", "dry-run")
        stats_only = arg in ("stats", "status", "stat")
        _client_now = cl.user_session.get("client")
        if stats_only:
            text = self_improve.get_failure_stats()
            await cl.Message(content=text).send()
            return
        async with cl.Step(name="🧠 Self-improvement cycle", show_input=False):
            result = await self_improve.run_improvement_cycle(
                get_store(),
                _client_now,
                dry_run=dry,
                force=True,
            )
        await cl.Message(content=result).send()
        return

    # Model Cookbook: curated catalog with VRAM requirements, capabilities, and
    # one-click install. `/cookbook` → all models; `/cookbook coding` → by category.
    if low.startswith("/cookbook"):
        arg = low[len("/cookbook"):].strip()
        from orchestrator.tools.cookbook_tools import cookbook_list
        result = cookbook_list(category=arg) if arg else cookbook_list()
        await cl.Message(content=result).send()
        return

    # MCP management shortcuts: connect servers, list tools, disconnect.
    # Full tool calls via the agent; these are quick UI shortcuts.
    if low.startswith("/mcp"):
        arg = message.content.strip()[4:].strip()  # preserve case for URLs/paths
        parts = arg.split()
        sub = parts[0].lower() if parts else "list"
        if sub == "list" or not sub:
            conns = _mcp_client.list_connections()
            if not conns:
                await cl.Message(
                    content=(
                        "No MCP servers connected.\n\n"
                        "Connect one: `/mcp connect <name> <command> [args…]`\n"
                        "Examples:\n"
                        "  `/mcp connect fs uvx mcp-server-filesystem C:\\projects`\n"
                        "  `/mcp connect github uvx mcp-server-github`\n\n"
                        "Or set `ZERO_AGENT_MCP_SERVERS` in `.env` for auto-connect at startup."
                    )
                ).send()
            else:
                from orchestrator.tools.mcp_tools import mcp_list
                text = await mcp_list()
                await cl.Message(content=text).send()
        elif sub == "connect" and len(parts) >= 3:
            name = parts[1]
            cmd = parts[2]
            args_str = " ".join(parts[3:])
            async with cl.Step(name=f"MCP connect: {name}", show_input=False):
                from orchestrator import mcp_client as _mc
                r = await _mc.connect_stdio(name=name, command=cmd, args=parts[3:])
            await cl.Message(content=r).send()
        elif sub == "disconnect" and len(parts) >= 2:
            r = await _mcp_client.disconnect(parts[1])
            await cl.Message(content=r).send()
        else:
            await cl.Message(
                content=(
                    "MCP commands:\n"
                    "  `/mcp list` — show connected servers and their tools\n"
                    "  `/mcp connect <name> <cmd> [args…]` — connect stdio server\n"
                    "  `/mcp disconnect <name>` — disconnect a server\n\n"
                    "For SSE servers or advanced config, ask Zero: "
                    '`connect to MCP server at http://localhost:8000/sse`'
                )
            ).send()
        return

    # HITL control: `/hitl off` = approve everything this session (no prompts);
    # `/hitl on` = re-enable the Approve/Deny prompt; `/hitl` shows the state.
    low = message.content.strip().lower()
    if low.startswith("/hitl"):
        arg = low[len("/hitl"):].strip()
        if arg == "off":
            cl.user_session.set("hitl_approve_all", True)
            await cl.Message(content="🔓 HITL off — destructive actions auto-approved this session.").send()
        elif arg == "on":
            cl.user_session.set("hitl_approve_all", False)
            await cl.Message(content="🔒 HITL on — I'll ask before destructive actions.").send()
        else:
            state = "OFF (auto-approving)" if cl.user_session.get("hitl_approve_all") else "ON (prompting)"
            await cl.Message(content=f"HITL is **{state}**. Use `/hitl off` to stop prompts, `/hitl on` to resume.").send()
        return

    # Multi-step autonomous goal: Supervisor-Worker loop with a live TaskList.
    if await _handle_agent_command(message.content.strip()):
        return

    # Iterative deep research (search → assess → search more → calibrated synthesis).
    if await _handle_research_command(message.content.strip()):
        return

    # Parallel multi-angle deep research: 5 sub-agents + synthesis + project save.
    if await _handle_deep_command(message.content.strip()):
        return

    # Project slash-commands are handled here and never reach the model.
    if message.content.strip().startswith("/"):
        if await _handle_project_command(message.content.strip()):
            return

    # Research / Image buttons prepend a clear instruction so the agent reaches
    # for the right tool without the user having to phrase it.
    prompt = message.content
    if command == "Research":
        prompt = (
            "Do thorough, up-to-date web research on the following and answer with "
            "a clear synthesis and cited sources. Use the deep_research tool.\n\n"
            + message.content
        )
    elif command == "Graphify":
        prompt = (
            "Build a knowledge graph of this local project folder with the "
            "graphify_project tool (mode='code'). When done, report where graph.html "
            "and GRAPH_REPORT.md were written and summarize the top god nodes and "
            "communities:\n\n" + message.content
        )
    elif command == "Image":
        prompt = (
            "Generate an image with ComfyUI for the following prompt "
            "(use run_comfyui_workflow):\n\n" + message.content
        )
    elif command == "Video":
        prompt = (
            "Generate a VIDEO with ComfyUI for the following prompt. Video renders "
            "are slow, so submit it as a background job with submit_comfyui_job "
            f"using the workflow at '{config.DEFAULT_VIDEO_WORKFLOW}', then report "
            "the job_id and tell the user to check back (check_job at most twice).\n\n"
            + message.content
        )
    elif command == "Schedule":
        raw = message.content.strip()
        if not raw:
            await cl.Message(
                content=(
                    "⏰ **Schedule a task** — type your request in this format:\n\n"
                    "`<what to do> | <when>`\n\n"
                    "**Examples:**\n"
                    "- `חדשות מונדיאל של היום | כל בוקר 09:00`\n"
                    "- `הרץ pipeline פרק 42 | היום 22:00`\n"
                    "- `סיכום שוק ההון | כל שני 08:30`\n"
                    "- `בדוק מזג אוויר בתל אביב | כל 30 דקות`"
                )
            ).send()
            return
        # Split on "|" — left = prompt, right = schedule
        if "|" in raw:
            task_prompt, _, schedule_expr = raw.partition("|")
            prompt = (
                f"Schedule this task using the schedule_task tool:\n"
                f"task_prompt: {task_prompt.strip()}\n"
                f"schedule: {schedule_expr.strip()}"
            )
        else:
            prompt = f"Schedule this task using the schedule_task tool. Parse both the task and schedule from: {raw}"

    history: list[dict[str, Any]] = cl.user_session.get("history")

    # Rolling summary: if the conversation has grown large, compress the oldest
    # turns into a running summary so Ollama doesn't silently truncate them.
    await summarizer.maybe_summarize(_get_distiller(), history, config.OLLAMA_NUM_CTX)

    # Active project: recall its relevant knowledge and keep it in context so the
    # agent "always remembers" the project's content (ChatGPT-Projects style).
    active_project = cl.user_session.get("project")
    if active_project:
        store = projects.get_project_store()
        hits = await store.recall(active_project, message.content)
        block = projects.build_project_block(active_project, hits) if hits else None
        projects.inject_project_message(history, block)

    # Self-improving memory: surface relevant remembered facts for this turn.
    if config.AUTO_MEMORY:
        store = get_store()
        block = await auto_memory.recall_memories(store, message.content)
        auto_memory.inject_memory_message(history, block)
        # Standing instructions ("always cite sources", …): injected EVERY turn,
        # not similarity-gated, so the user's feedback reliably applies.
        auto_memory.inject_preferences_message(
            history, auto_memory.recall_preferences(store)
        )
        # Learn-from-mistakes: surface relevant lessons from past failures so the
        # agent avoids repeating them.
        lesson_block = await lessons.recall_lessons(store, message.content)
        lessons.inject_lessons_message(history, lesson_block)

    on_tool_start, on_tool_end, media = _make_tool_callbacks()

    # Final answer streams token-by-token into this message. Send it NOW, before
    # any tool/thinking step opens, so Chainlit locks its parent_id to top-level
    # (None). Otherwise the answer gets nested under an open step and won't show
    # as a main message when the thread is reopened from the sidebar.
    answer_msg = cl.Message(content="")
    answer_msg.parent_id = None
    await answer_msg.send()

    async def on_token(token: str) -> None:
        await answer_msg.stream_token(token)

    # Reasoning ("thinking") from qwen3-style models streams into a collapsible
    # step so the user sees the model working without cluttering the answer.
    thinking_step: dict[str, Any] = {"step": None}

    async def on_thinking(token: str) -> None:
        if thinking_step["step"] is None:
            step = cl.Step(name="🧠 thinking", type="llm")
            await step.send()
            thinking_step["step"] = step
        await thinking_step["step"].stream_token(token)

    # Instant voice: for SPOKEN turns, answer directly (thinking block OFF) and
    # optionally on a faster model — the real fix for the long pre-answer silence.
    # Same capable model by default (not dumber); restored in `finally`. Typed
    # turns are untouched. We toggle on the agent's own client (what run() reads).
    _voice_restore: "tuple[bool, str] | None" = None
    if from_voice and cl.user_session.get("voice_instant", config.VOICE_INSTANT):
        _vc = agent.client
        _voice_restore = (_vc.think, _vc.model)
        _vc.think = False
        if config.VOICE_MODEL:
            _vc.model = config.VOICE_MODEL

    started = time.perf_counter()
    # Run the agent as a cancellable task so the user can hit the ⏹ stop button
    # (handled by @cl.on_stop) to abort a long/runaway turn. There is NO time or
    # iteration cap — heavy tasks may run as long as needed; stopping is manual.
    run_task = asyncio.create_task(
        agent.run(
            prompt,
            history=history,  # mutated in place -> conversation persists across turns
            on_tool_start=on_tool_start,
            on_tool_end=on_tool_end,
            on_token=on_token,
            on_thinking=on_thinking,
            on_approval=_make_approver(),
        )
    )
    cl.user_session.set("run_task", run_task)
    try:
        answer = await run_task

        # Close the thinking step if one was opened.
        if thinking_step["step"] is not None:
            await thinking_step["step"].update()

        if not answer_msg.content:
            answer_msg.content = answer or "_(no content returned)_"

        # Build all elements (generated images + optional spoken-reply audio)
        # and attach them in ONE final update. The text is already visible from
        # live streaming, so a single update avoids a double render — which with
        # an auto-playing audio element would otherwise play the voice twice and
        # spawn two players (so pausing one left the other audible).
        elements = list(media) if media else []

        # Speak the reply (local voice) if enabled — ONE clean clip for the whole
        # answer (the per-sentence streaming experiment was choppy; reverted).
        # Synthesis runs in a worker thread and never raises; on failure we skip it.
        if cl.user_session.get("tts") and answer:
            wav = os.path.join(config.TTS_OUTPUT_DIR, f"reply_{uuid.uuid4().hex[:8]}.wav")
            audio_path = await tts.synthesize(answer, wav)
            if audio_path:
                elements.append(
                    cl.Audio(
                        path=str(audio_path), name="reply.wav",
                        display="inline", auto_play=True,
                    )
                )

        if elements:
            answer_msg.elements = elements
        # Response-time badge, appended to the DISPLAYED message only (kept out
        # of `answer`, so the distilled long-term memory stays clean).
        elapsed = time.perf_counter() - started
        answer_msg.content = f"{answer_msg.content}\n\n`⏱️ {elapsed:.1f}s`"
        await answer_msg.update()

        # Self-improving memory: in the background, distill durable facts from
        # this exchange and store them for future chats. Fire-and-forget so it
        # never delays the visible reply.
        if config.AUTO_MEMORY and answer:
            distiller = _get_distiller()
            asyncio.create_task(
                auto_memory.distill_and_store(
                    distiller, get_store(), message.content, answer
                )
            )
            # Learn-from-mistakes: if this run hit failures, distill a lesson.
            failures = "\n".join(getattr(agent, "last_run_failures", []))
            if failures:
                asyncio.create_task(
                    lessons.distill_lessons(
                        distiller, get_store(), message.content, answer, failures
                    )
                )
    except asyncio.CancelledError:
        # User pressed the ⏹ stop button (see @cl.on_stop). Wrap up cleanly:
        # show whatever was produced so far + a "stopped" note, and keep the
        # history well-formed so the next turn isn't confused by a dangling
        # user/tool message.
        if thinking_step["step"] is not None:
            await thinking_step["step"].update()
        if history and history[-1].get("role") != "assistant":
            history.append({"role": "assistant", "content": "(stopped by user)"})
        if media:
            answer_msg.elements = media
        elapsed = time.perf_counter() - started
        note = (answer_msg.content or "").rstrip()
        answer_msg.content = (
            f"{note}\n\n`⏹️ Stopped by you after {elapsed:.1f}s`"
        ).lstrip()
        await answer_msg.update()
    except Exception as exc:  # noqa: BLE001 - outer safety net for the UI
        # Never let an error tear down the session / reset the page. Log it and
        # show a friendly message in place of the answer.
        logging.getLogger("zero_agent.ui").exception("on_message failed: %s", exc)
        answer_msg.content = (
            "⚠️ Something went wrong handling that message. "
            f"({type(exc).__name__}). It was logged — please try again."
        )
        await answer_msg.update()
    finally:
        # Restore the model/thinking we toggled for an instant-voice turn.
        if _voice_restore is not None:
            agent.client.think, agent.client.model = _voice_restore
        cl.user_session.set("run_task", None)


@cl.on_stop
async def stop_running() -> None:
    """Cancel the in-flight agent turn when the user taps the ⏹ stop button.

    Cancellation propagates at the next ``await`` inside the agent loop (the
    Ollama stream, a tool's awaits), so the agent stops consuming and on_message
    handles the CancelledError. Any background render already submitted to
    ComfyUI keeps going server-side, but the agent stops waiting on it.
    """
    run_task: "asyncio.Task[Any] | None" = cl.user_session.get("run_task")
    if run_task is not None and not run_task.done():
        logging.getLogger("zero_agent.ui").info("user requested stop — cancelling turn")
        run_task.cancel()


# --- Voice input (Stage 2): mic → local Whisper → normal message flow -------
# Hands-free: we detect the end of an utterance from a short trailing silence
# (server-side VAD) and auto-send it — so you don't have to press the stop button
# after every sentence. The stop button still works (flushes whatever's pending).
def _audio_sample_rate() -> int:
    try:
        from chainlit.config import config as _clcfg

        return int(getattr(_clcfg.features.audio, "sample_rate", 24000) or 24000)
    except Exception:  # noqa: BLE001
        return 24000


@cl.on_audio_start
async def on_audio_start() -> bool:
    """Accept a mic recording; reset this session's audio state."""
    cl.user_session.set("audio_buffer", bytearray())
    cl.user_session.set("audio_speech_s", 0.0)
    cl.user_session.set("audio_silence_s", 0.0)
    cl.user_session.set("audio_busy", False)
    cl.user_session.set("audio_mute_until", 0.0)
    return True


@cl.on_audio_chunk
async def on_audio_chunk(chunk: "cl.InputAudioChunk") -> None:
    """Accumulate PCM and auto-finalize the utterance after a trailing pause."""
    # Echo suppression (conversation mode): while Zero is thinking/speaking
    # (audio_busy) or during the short post-turn cooldown, IGNORE the mic — drop
    # the audio so it never transcribes, and reply to, its own voice. This is what
    # makes a hands-free loop possible without an infinite self-trigger.
    import time as _t

    mute_until = float(cl.user_session.get("audio_mute_until") or 0.0)
    if cl.user_session.get("audio_busy") or _t.monotonic() < mute_until:
        cl.user_session.set("audio_speech_s", 0.0)
        cl.user_session.set("audio_silence_s", 0.0)
        return

    buf = cl.user_session.get("audio_buffer")
    if buf is None:
        buf = bytearray()
        cl.user_session.set("audio_buffer", buf)
    buf.extend(chunk.data)

    if not config.STT_AUTOSTOP:
        return

    # Track speech vs. silence by chunk loudness (RMS) to detect end-of-utterance.
    import audioop  # stdlib; purpose-built for PCM RMS

    sr = _audio_sample_rate()
    dur = (len(chunk.data) / 2) / sr  # 16-bit mono -> 2 bytes/sample
    try:
        rms = audioop.rms(bytes(chunk.data), 2)
    except Exception:  # noqa: BLE001
        rms = config.STT_SILENCE_RMS + 1  # treat as speech if unsure

    speech_s = cl.user_session.get("audio_speech_s") or 0.0
    silence_s = cl.user_session.get("audio_silence_s") or 0.0
    if rms >= config.STT_SILENCE_RMS:
        speech_s += dur
        silence_s = 0.0
    else:
        silence_s += dur
    cl.user_session.set("audio_speech_s", speech_s)
    cl.user_session.set("audio_silence_s", silence_s)

    if (
        not cl.user_session.get("audio_busy")
        and speech_s >= config.STT_MIN_SPEECH_S
        and silence_s >= config.STT_END_SILENCE_S
    ):
        await _finalize_audio()


@cl.on_audio_end
async def on_audio_end() -> None:
    """User pressed stop — flush any unsent speech."""
    await _finalize_audio(manual=True)


def _apply_wake_word(text: str) -> str | None:
    """Gate an utterance by the configured wake word(s).

    No wake words configured → pass everything through (return text unchanged).
    Otherwise return the command with the wake word stripped, or None if the
    utterance isn't addressed to Zero (mic is on but we shouldn't act on it).
    """
    words = cl.user_session.get("voice_wake")
    if words is None:
        words = config.VOICE_WAKE_WORDS
    if not words:
        return text
    low = text.lower()
    for w in words:
        i = low.find(w)
        if i != -1:
            stripped = (text[:i] + text[i + len(w):]).strip(" ,.!?—-").strip()
            return stripped or text  # if they said ONLY the wake word, keep it
    return None


async def _finalize_audio(manual: bool = False) -> None:
    """Transcribe the buffered utterance and run it like a typed message."""
    import time as _t

    if cl.user_session.get("audio_busy"):
        return
    buf = cl.user_session.get("audio_buffer") or bytearray()
    pcm = bytes(buf)
    # Reset for the next utterance before the (slow) transcribe + agent turn.
    cl.user_session.set("audio_buffer", bytearray())
    cl.user_session.set("audio_speech_s", 0.0)
    cl.user_session.set("audio_silence_s", 0.0)

    sr = _audio_sample_rate()
    if len(pcm) < sr:  # < ~0.5 s of audio -> ignore
        return

    cl.user_session.set("audio_busy", True)
    try:
        text = await stt.transcribe_pcm(pcm, sr, language=cl.user_session.get("stt_lang"))
        if not text:
            if manual:
                await cl.Message(content="🎤 _Didn't catch that — please try again._").send()
            return
        # Wake word (optional): if configured, only act when addressed (e.g. say
        # "זירו …"); the wake word is stripped before the agent sees the command.
        cmd = _apply_wake_word(text)
        if cmd is None:
            return  # mic on, but this utterance wasn't addressed to Zero — ignore
        await cl.Message(content=cmd, type="user_message").send()
        await _handle_user_message(cl.Message(content=cmd), from_voice=True)
    finally:
        cl.user_session.set("audio_busy", False)
        # Echo cooldown: keep ignoring the mic briefly so the tail of Zero's own
        # last words (still in the air / OS buffer) doesn't trigger a new turn.
        cl.user_session.set(
            "audio_mute_until", _t.monotonic() + config.VOICE_ECHO_COOLDOWN_S
        )


@cl.on_chat_end
async def end() -> None:
    client: OllamaClient | None = cl.user_session.get("client")
    if client is not None:
        await client.aclose()