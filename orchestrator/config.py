"""Central configuration for the Zero Agent ecosystem.

Everything is overridable via environment variables so the same code runs
unchanged whether you are on the RTX 5090 box or a smaller dev machine.
"""

from __future__ import annotations

import os
import tempfile

# --- Inference engine -------------------------------------------------------
def _normalize_ollama_host(raw: str) -> str:
    """Turn whatever is in OLLAMA_HOST into a valid URL to CONNECT to.

    OLLAMA_HOST doubles as the server's BIND address, so people commonly set it
    to ``0.0.0.0`` (all interfaces) or ``0.0.0.0:11434`` to expose Ollama on the
    LAN. But ``0.0.0.0`` is not a connectable address, and the value often lacks
    the ``http://`` scheme and/or the port — all of which make the client fail
    with "Could not reach Ollama". Normalize defensively: a bind-all/empty host
    becomes 127.0.0.1, and the scheme + default port are filled in.
    """
    raw = (raw or "").strip()
    if not raw:
        return "http://127.0.0.1:11434"
    if "://" not in raw:
        raw = "http://" + raw
    scheme, _, rest = raw.partition("://")
    host, sep, port = rest.partition(":")
    # 0.0.0.0 / :: / empty mean "all interfaces" for binding — connect to loopback.
    if host in ("", "0.0.0.0", "::", "[::]", "*"):
        host = "127.0.0.1"
    if not sep or not port:
        port = "11434"
    return f"{scheme}://{host}:{port}"


OLLAMA_HOST: str = _normalize_ollama_host(os.getenv("OLLAMA_HOST", ""))

# Intended production brain (6-bit GGUF on the 5090). Override with
# ZERO_AGENT_MODEL if a different tag is pulled locally.
DEFAULT_MODEL: str = os.getenv("ZERO_AGENT_MODEL", "qwen3:32b")

# Request timeout for a single /api/chat call (seconds). 32B models can be
# slow on the first token, so keep this generous.
REQUEST_TIMEOUT: float = float(os.getenv("ZERO_AGENT_TIMEOUT", "300"))

# Context window (tokens) sent to Ollama on every chat call. Many models default
# to only 8192, which silently truncates long conversations — dropping the
# system prompt and producing empty/garbled replies. The RTX 5090 (32 GB) has
# plenty of headroom, so we request a large window. Raised to 32768 to hold more
# of a conversation / more code verbatim for agentic work. Safe for the qwen3
# family on a 32 GB card; for the 70B (already CPU-offloaded) lower this if it
# crawls or OOMs. Override per machine via ZERO_AGENT_NUM_CTX.
OLLAMA_NUM_CTX: int = int(os.getenv("ZERO_AGENT_NUM_CTX", "16384"))

# Maximum tokens the model may GENERATE per turn (Ollama "num_predict"). Without
# a cap, a reasoning model on a complex task can run away — e.g. emitting a
# 50k-character monologue plus the whole file content into the chat instead of
# quietly calling write_file. This bounds every reply to a sane size, keeping
# the UI responsive. -1 means "unlimited" (the Ollama default). Override with
# ZERO_AGENT_NUM_PREDICT. Applies to all models.
OLLAMA_NUM_PREDICT: int = int(os.getenv("ZERO_AGENT_NUM_PREDICT", "4096"))

# Reasoning ("thinking") models like qwen3 can spend thousands of tokens on an
# internal monologue. Keeping it ON gives clean final answers (the reasoning
# goes to a separate "thinking" field, shown collapsibly in the UI) instead of
# the model rambling its thoughts into the visible answer. The flag is only sent
# to models that actually support thinking (qwen3 family); others ignore it.
# Set ZERO_AGENT_THINK=0 to disable. For maximum speed, pick a non-thinking
# model like gemma3:4b from the model dropdown.
ENABLE_THINKING: bool = os.getenv("ZERO_AGENT_THINK", "1").lower() in (
    "1",
    "true",
    "yes",
)

# --- Resilience (Phase 2: graceful degradation) -----------------------------
# How many times to retry a failed Ollama call (connection error / 5xx) before
# giving up. Transient hiccups (server still loading a model) recover silently.
OLLAMA_MAX_RETRIES: int = int(os.getenv("ZERO_AGENT_OLLAMA_RETRIES", "3"))

# Base backoff (seconds) between retries; grows exponentially (base, 2x, 4x...).
OLLAMA_RETRY_BACKOFF: float = float(os.getenv("ZERO_AGENT_OLLAMA_BACKOFF", "0.5"))

# --- Agent loop -------------------------------------------------------------
# Hard cap on tool-call round trips before the agent gives up. Prevents
# runaway loops if the model keeps requesting tools. Raised from 8 to 20 so the
# agent can complete longer multi-step coding tasks (read several files, edit,
# run, fix) closer to a real coding agent. It's only a CAP — simple chats still
# finish in 1-2 steps; this just lets complex tasks go further before stopping.
MAX_TOOL_ITERATIONS: int = int(os.getenv("ZERO_AGENT_MAX_ITERATIONS", "20"))

# --- Vector memory (Phase 3) ------------------------------------------------
# Embedding model used to turn notes/queries into vectors. Pull it once with
# `ollama pull nomic-embed-text` (768-dim, fast, runs locally).
EMBED_MODEL: str = os.getenv("ZERO_AGENT_EMBED_MODEL", "nomic-embed-text")

# Where ChromaDB persists its vector store on disk. Survives restarts.
MEMORY_DIR: str = os.getenv(
    "ZERO_AGENT_MEMORY_DIR",
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "memory"),
)

# Default number of memories returned by a recall() search.
MEMORY_TOP_K: int = int(os.getenv("ZERO_AGENT_MEMORY_TOP_K", "5"))

# --- Supervisor-Worker task state (Milestone: orchestration) ----------------
# Where the TaskManager persists the active plan + working memory as JSON, so a
# multi-step goal survives a crash/restart and the Worker's context stays small
# (state lives here, not in the conversation). One file per run.
TASKS_DIR: str = os.getenv(
    "ZERO_AGENT_TASKS_DIR",
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "tasks"),
)

# Hard ceiling on Supervisor replan/retry cycles, so a goal the local model
# simply can't finish fails loudly instead of looping forever.
SUPERVISOR_MAX_REPLANS: int = int(os.getenv("ZERO_AGENT_MAX_REPLANS", "3"))

# Default model for the Supervisor/Planner (reasoning) vs. the Worker (coding/
# tools). Worker defaults to the fast Qwen3-Coder MoE; override per deployment.
SUPERVISOR_MODEL: str = os.getenv("ZERO_AGENT_SUPERVISOR_MODEL", "qwen3:32b")
WORKER_MODEL: str = os.getenv("ZERO_AGENT_WORKER_MODEL", "qwen3-coder-30b")

# --- Chat history persistence (local, offline) ------------------------------
# Chainlit stores every thread/message in a local SQLite file so the sidebar can
# list past conversations and resume them (ChatGPT-style). 100% offline — no
# cloud, no network. Override the path or disable entirely below.
PERSIST_CHATS: bool = os.getenv("ZERO_AGENT_PERSIST_CHATS", "1").lower() in (
    "1",
    "true",
    "yes",
)
HISTORY_DB_PATH: str = os.getenv(
    "ZERO_AGENT_HISTORY_DB",
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "chat_history.db"),
)

# Simple local login used only to tie saved chats to a user. Credentials never
# leave this machine. Change them via env vars (recommended).
AUTH_USERNAME: str = os.getenv("ZERO_AGENT_USER", "admin")
AUTH_PASSWORD: str = os.getenv("ZERO_AGENT_PASSWORD", "zero")

# --- Telegram chat history persistence (local, offline) ---------------------
# The Chainlit front-end persists threads to SQLite (above); the Telegram bot
# kept its per-chat history in RAM only, so a restart lost the conversation.
# When enabled, each Telegram chat's rolling history is mirrored to a small JSON
# file on disk so a chat survives bot restarts (the long-term ChromaDB memory is
# separate and already persistent). 100% offline. Set =0 to keep RAM-only.
TELEGRAM_PERSIST_HISTORY: bool = os.getenv(
    "ZERO_AGENT_TELEGRAM_PERSIST_HISTORY", "1"
).lower() in ("1", "true", "yes")
TELEGRAM_HISTORY_DIR: str = os.getenv(
    "ZERO_AGENT_TELEGRAM_HISTORY_DIR",
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "telegram"),
)
# Cap how many messages are kept per chat file (the rolling summarizer keeps the
# in-RAM history bounded too; this is a hard backstop against unbounded files).
TELEGRAM_HISTORY_MAX_MSGS: int = int(
    os.getenv("ZERO_AGENT_TELEGRAM_HISTORY_MAX_MSGS", "200")
)

# --- Self-improving long-term memory ----------------------------------------
# After each turn, a small fast model distills durable facts/preferences from
# the exchange and stores them in vector memory, so the agent "remembers" the
# user across brand-new chats. Recall runs before each turn to surface them.
AUTO_MEMORY: bool = os.getenv("ZERO_AGENT_AUTO_MEMORY", "1").lower() in (
    "1",
    "true",
    "yes",
)
# Small, fast model used for the background distillation (keep it cheap).
MEMORY_DISTILL_MODEL: str = os.getenv("ZERO_AGENT_MEMORY_MODEL", "qwen3:4b")
# Minimum similarity score (0..1) for a recalled memory to be injected.
MEMORY_RECALL_MIN_SCORE: float = float(
    os.getenv("ZERO_AGENT_MEMORY_MIN_SCORE", "0.55")
)
# How many memories to surface at the top of each turn.
MEMORY_RECALL_K: int = int(os.getenv("ZERO_AGENT_MEMORY_RECALL_K", "3"))

# --- Text-to-speech (the agent speaks its replies) --------------------------
# 100% offline, torch-free: kokoro-onnx (onnxruntime) + the Kokoro v1.0 voice
# model. Synthesis is faster than real-time on CPU and uses a SEPARATE
# onnxruntime session — it does NOT compete for the main LLM's VRAM. Opt-in: the
# UI exposes a "🔊 Speak replies" switch; this is just the session default.
TTS_ENABLED: bool = os.getenv("ZERO_AGENT_TTS", "0").lower() in ("1", "true", "yes")
_TTS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "tts")
TTS_MODEL_PATH: str = os.getenv(
    "ZERO_AGENT_TTS_MODEL", os.path.join(_TTS_DIR, "kokoro-v1.0.onnx")
)
TTS_VOICES_PATH: str = os.getenv(
    "ZERO_AGENT_TTS_VOICES", os.path.join(_TTS_DIR, "voices-v1.0.bin")
)
# Spoken-reply WAVs are written to the system temp dir (NOT under the repo) so
# they don't trigger the dev `-w` file watcher or clutter the project.
TTS_OUTPUT_DIR: str = os.getenv(
    "ZERO_AGENT_TTS_OUT", os.path.join(tempfile.gettempdir(), "zero_agent_tts")
)
# Kokoro voice. Male: am_michael / am_fenrir / am_puck (US), bm_george (British).
# Female: af_heart / af_bella (US). Override with ZERO_AGENT_TTS_VOICE.
TTS_VOICE: str = os.getenv("ZERO_AGENT_TTS_VOICE", "am_michael")
TTS_SPEED: float = float(os.getenv("ZERO_AGENT_TTS_SPEED", "1.0"))
TTS_LANG: str = os.getenv("ZERO_AGENT_TTS_LANG", "en-us")
# Cap spoken length so a long answer doesn't produce minutes of audio; the text
# is trimmed to the last sentence boundary under this many characters.
TTS_MAX_CHARS: int = int(os.getenv("ZERO_AGENT_TTS_MAX_CHARS", "1200"))

# --- Speech-to-text (talk to the agent: voice Stage 2) ----------------------
# 100% offline, torch-free: faster-whisper (CTranslate2). The mic button in the
# UI records audio; on stop it's transcribed locally and sent as a normal
# message. Multilingual (auto-detects Hebrew/English). Gated by the Chainlit
# `[features.audio]` switch in .chainlit/config.toml.
# "small" on CPU is ~3x faster than "medium" (much snappier mic→text). The Arabic/
# Hebrew mis-transcription that pushed us to "medium" was really an auto-DETECT
# problem — with the language forced (the ⚙️ "Voice language" setting), "small" is
# both fast AND accurate. Set ZERO_AGENT_STT_MODEL=medium for max accuracy.
STT_MODEL: str = os.getenv("ZERO_AGENT_STT_MODEL", "small")  # base/small/medium
STT_DEVICE: str = os.getenv("ZERO_AGENT_STT_DEVICE", "cpu")  # cpu / cuda / auto
STT_COMPUTE_TYPE: str = os.getenv("ZERO_AGENT_STT_COMPUTE", "int8")
# Empty = auto-detect language; set "he"/"en" to force one.
STT_LANGUAGE: str = os.getenv("ZERO_AGENT_STT_LANGUAGE", "").strip()
# Where faster-whisper caches its model files (kept local to the project).
STT_MODEL_DIR: str = os.getenv(
    "ZERO_AGENT_STT_DIR", os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "stt")
)
# Hands-free mic: auto-send a spoken utterance after a short pause, so you don't
# have to press the stop button. Tunable for noisy mics. Set AUTOSTOP=0 to revert
# to manual (only the stop button ends a recording).
STT_AUTOSTOP: bool = os.getenv("ZERO_AGENT_STT_AUTOSTOP", "1").lower() in ("1","true","yes")
# RMS level (16-bit, 0–32767) below which a chunk counts as silence.
STT_SILENCE_RMS: int = int(os.getenv("ZERO_AGENT_STT_SILENCE_RMS", "500"))
# Trailing silence (seconds) after speech that ends an utterance → auto-send.
STT_END_SILENCE_S: float = float(os.getenv("ZERO_AGENT_STT_END_SILENCE_S", "1.0"))
# Minimum speech (seconds) before a pause can finalize (ignores brief blips).
STT_MIN_SPEECH_S: float = float(os.getenv("ZERO_AGENT_STT_MIN_SPEECH_S", "0.4"))

# --- Conversation mode (hands-free voice, stage 2) --------------------------
# When on, the mic stays open and each utterance you speak (after Zero finishes)
# starts a turn — a real back-and-forth, no button per sentence. The essential
# guard is ECHO SUPPRESSION: ignore mic input while Zero is thinking/speaking, so
# it never hears (and replies to) its own voice. A short cooldown after a turn
# lets the speakers settle before listening resumes. Toggle live in the UI ⚙️.
VOICE_CONVERSATION: bool = os.getenv("ZERO_AGENT_VOICE_CONVERSATION", "0").lower() in ("1", "true", "yes")
# Seconds to keep ignoring the mic AFTER a turn finishes (kills the tail echo of
# Zero's own last words still in the air / OS audio buffer).
VOICE_ECHO_COOLDOWN_S: float = float(os.getenv("ZERO_AGENT_VOICE_ECHO_COOLDOWN_S", "0.6"))
# Optional wake words: if set (comma-separated), an utterance is acted on ONLY if
# it contains one, and the wake word is stripped before the agent sees it. Empty =
# off (every utterance is a turn). e.g. "זירו,hey zero,בוקר טוב זירו".
VOICE_WAKE_WORDS: list[str] = [
    w.strip().lower() for w in os.getenv("ZERO_AGENT_VOICE_WAKE_WORDS", "").split(",") if w.strip()
]
# Instant voice: run SPOKEN turns with the thinking block OFF, so Zero answers
# immediately instead of generating a long reasoning monologue first (the real
# voice-latency villain). This does NOT swap to a weaker model — the same capable
# model just replies directly; thinking mostly helps hard multi-step problems,
# which are rare in conversation. Typed turns keep thinking as configured, so deep
# reasoning is one keystroke away. Toggle live in ⚙️.
VOICE_INSTANT: bool = os.getenv("ZERO_AGENT_VOICE_INSTANT", "1").lower() in ("1", "true", "yes")
# Optional faster model for SPOKEN turns only (empty = keep the active/selected
# model — recommended, so voice isn't dumber). Set e.g. "qwen3:4b" or "hermes3:8b"
# to trade a little smarts for an even snappier reply. Typed turns are unaffected.
VOICE_MODEL: str = os.getenv("ZERO_AGENT_VOICE_MODEL", "").strip()

# --- Reserved ports ---------------------------------------------------------
# Ports the agent must treat as ALWAYS occupied: never bind a project's dev/server
# to them (avoids clobbering Zero's own UI or a service the user keeps running, and
# the "blank Hello-World page" confusion from a port collision). 8000 by default;
# override with a comma list, e.g. ZERO_AGENT_RESERVED_PORTS="8000,8188,11434".
RESERVED_PORTS: list[int] = [
    int(p) for p in os.getenv("ZERO_AGENT_RESERVED_PORTS", "8000").replace(" ", "").split(",")
    if p.strip().isdigit()
]

# --- Self-verification (learn-layer phase 2) --------------------------------
# After the agent finishes an ACTION task (a turn that used >=1 tool), a strict
# reviewer checks whether the result was actually verified or merely claimed
# done; if not, the agent gets ONE corrective iteration to test + fix. Catches
# the "success:true on a broken result" failure. Only runs on tool-using turns,
# so plain chat is unaffected. Set ZERO_AGENT_SELF_VERIFY=0 to disable.
SELF_VERIFY: bool = os.getenv("ZERO_AGENT_SELF_VERIFY", "1").lower() in (
    "1",
    "true",
    "yes",
)

# --- Faithfulness check (research grounding) --------------------------------
# On a turn that retrieved web/research sources, a fact-checker sees ONLY the
# drafted answer + the raw retrieved text and flags claims the sources don't
# support (the "confident but unsourced / false-precision" failure). On FAIL the
# agent gets one corrective pass to re-state using only what the sources say.
FAITHFULNESS: bool = os.getenv("ZERO_AGENT_FAITHFULNESS", "1").lower() in (
    "1",
    "true",
    "yes",
)

# --- Projects (named knowledge bases, ChatGPT-Projects style) ---------------
# When a project is active, the most relevant chunks of its stored knowledge are
# recalled and injected each turn so the agent always has the project's context.
PROJECT_RECALL_K: int = int(os.getenv("ZERO_AGENT_PROJECT_RECALL_K", "6"))

# --- Observability ----------------------------------------------------------
# Log verbosity: DEBUG / INFO / WARNING / ERROR.
LOG_LEVEL: str = os.getenv("ZERO_AGENT_LOG_LEVEL", "INFO")

# Emit one JSON object per log line (for aggregators) instead of key=value text.
LOG_JSON: bool = os.getenv("ZERO_AGENT_LOG_JSON", "0").lower() in ("1", "true", "yes")

# Optional path for an additional file log sink. Empty = console only.
LOG_FILE: str = os.getenv("ZERO_AGENT_LOG_FILE", "")

# --- Tool safety ------------------------------------------------------------
# Human-in-the-Loop: require explicit user approval before the agent runs a
# potentially destructive / outward-facing tool. Now that the Supervisor runs
# multi-step autonomously and uncensored models are told not to refuse, an
# approval gate is the safety net. Enforced in the agent loop; a front-end that
# passes an `on_approval` callback gets the prompt, one that doesn't (e.g. a
# headless run) falls back to HITL_DEFAULT_ALLOW.
HITL_ENABLED: bool = os.getenv("ZERO_AGENT_HITL", "1").lower() in ("1", "true", "yes")

# When HITL is on but no approval callback is wired (CLI/headless), allow (True)
# or deny (False) the gated tool by default. UI/Telegram supply a real prompt.
HITL_DEFAULT_ALLOW: bool = os.getenv("ZERO_AGENT_HITL_DEFAULT_ALLOW", "1").lower() in (
    "1",
    "true",
    "yes",
)

# Tools that require approval when HITL is on (lowercased tool names). These
# write/delete/run/launch/download — i.e. change the machine or reach outward.
def _parse_csv_set(raw: str, default: set[str]) -> set[str]:
    items = {x.strip().lower() for x in raw.split(",") if x.strip()}
    return items or default


# Default set leans toward AUTONOMY for the agent's constructive work: writing
# and moving files flow freely (that's the build agent's whole job), but anything
# IRREVERSIBLE or that runs arbitrary code / reaches outward needs a Y/N. Add
# write_file/move_path via ZERO_AGENT_HITL_TOOLS for maximum strictness.
HITL_TOOLS: set[str] = _parse_csv_set(
    os.getenv("ZERO_AGENT_HITL_TOOLS", ""),
    {
        "delete_path",  # irreversible
        "execute_terminal_command",  # arbitrary shell — the main injection vector
        "launch_program",  # launches arbitrary processes
        "download_hf_model",  # network + disk
        "download_file",
        "register_gguf_model",  # mutates the Ollama model registry
    },
)

# Max bytes returned by read_file (protects context window + RAM).
READ_FILE_MAX_BYTES: int = int(os.getenv("ZERO_AGENT_READ_MAX_BYTES", str(256 * 1024)))

# Hard ceiling on the size of ANY single tool result fed back into the model, so
# one big result (a long dir listing, a fetched page, a verbose command) can't
# blow past num_ctx mid-loop. Read-file/terminal have their own caps; this is the
# universal backstop applied in the agent loop. ~8000 chars ≈ 2k tokens.
TOOL_RESULT_MAX_CHARS: int = int(os.getenv("ZERO_AGENT_TOOL_RESULT_MAX", "8000"))

# Wall-clock timeout for execute_terminal_command (seconds).
TERMINAL_TIMEOUT: float = float(os.getenv("ZERO_AGENT_TERMINAL_TIMEOUT", "20"))

# Max characters of captured terminal output handed back to the model.
TERMINAL_MAX_OUTPUT_CHARS: int = int(
    os.getenv("ZERO_AGENT_TERMINAL_MAX_OUTPUT", "8000")
)

# fetch_webpage: request timeout (seconds) and max bytes kept from the body.
FETCH_TIMEOUT: float = float(os.getenv("ZERO_AGENT_FETCH_TIMEOUT", "10"))
FETCH_MAX_BYTES: int = int(os.getenv("ZERO_AGENT_FETCH_MAX_BYTES", str(50 * 1024)))

# --- Media generation (ComfyUI) --------------------------------------------
# Base URL of the local ComfyUI server (the same one its web UI runs on).
COMFYUI_HOST: str = os.getenv("COMFYUI_HOST", "http://127.0.0.1:8188")

# Wall-clock budget for a single generation, from submit to finished outputs.
# Generous by default: rendering 4K video can take many minutes.
COMFYUI_TIMEOUT: float = float(os.getenv("ZERO_AGENT_COMFYUI_TIMEOUT", str(15 * 60)))

# How often to poll /history while waiting for the job to finish (seconds).
COMFYUI_POLL_INTERVAL: float = float(os.getenv("ZERO_AGENT_COMFYUI_POLL", "2"))

# Local folder where generated images/videos are downloaded so the UI can show
# them inline (like ChatGPT). Created on demand.
MEDIA_OUTPUT_DIR: str = os.getenv(
    "ZERO_AGENT_MEDIA_DIR",
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "outputs"),
)

# Ready-made API-format workflows the agent can use without the user supplying a
# path. SDXL = fast + light on VRAM (default); FLUX = best quality but heavier
# and slower (use when the user explicitly wants top quality). Override via env.
DEFAULT_IMAGE_WORKFLOW: str = os.getenv(
    "ZERO_AGENT_IMAGE_WORKFLOW",
    r"C:\AI-MEDIA-RTX5090\zero_agent_sdxl_api.json",
)
HQ_IMAGE_WORKFLOW: str = os.getenv(
    "ZERO_AGENT_HQ_WORKFLOW",
    r"C:\AI-MEDIA-RTX5090\zero_agent_flux_api.json",
)

# Ready-made API-format VIDEO workflow (LTX / Wan etc.). Video renders are slow,
# so the agent submits it as a background job (submit_comfyui_job + check_job).
# NOTE: this MUST be an API-format JSON (ComfyUI "Save (API Format)") — the
# UI-format exports are rejected. Point this at your LTX/Wan API workflow.
DEFAULT_VIDEO_WORKFLOW: str = os.getenv(
    "ZERO_AGENT_VIDEO_WORKFLOW",
    r"C:\AI-MEDIA-RTX5090\zero_agent_ltx_api.json",
)

# --- Telegram bridge --------------------------------------------------------
# A second entry point (telegram_bot.py) that lets you talk to the SAME agent
# (same tools, memory, and projects) from Telegram. The LLM still runs locally;
# only the chat transport goes through Telegram's Bot API. Configure via env
# vars or the local zero-agent/.env file (loaded by telegram_bot.py at startup).


def _parse_int_set(raw: str) -> frozenset[int]:
    """Parse a comma/semicolon-separated list of integers, ignoring junk."""
    out: set[int] = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.add(int(part))
        except ValueError:
            pass
    return frozenset(out)


# Bot token from @BotFather. Empty => the Telegram entry point refuses to start.
TELEGRAM_BOT_TOKEN: str = os.getenv(
    "ZERO_AGENT_TELEGRAM_TOKEN", os.getenv("TELEGRAM_BOT_TOKEN", "")
).strip()

# Numeric Telegram user IDs allowed to use the bot. REQUIRED for safety: the
# agent can run shell commands and read/write files, so an open bot would hand
# that power to anyone. An empty allow-list => every message is refused (the bot
# still replies with the sender's own ID so you can add it here easily).
TELEGRAM_ALLOWED_IDS: frozenset[int] = _parse_int_set(
    os.getenv("ZERO_AGENT_TELEGRAM_ALLOWED_IDS", os.getenv("TELEGRAM_ALLOWED_IDS", ""))
)

# Long-poll timeout (seconds) for getUpdates. Telegram holds the request open
# this long when there are no new messages, so we are not hammering the API.
TELEGRAM_POLL_TIMEOUT: int = int(os.getenv("ZERO_AGENT_TELEGRAM_POLL", "30"))

# Default model for Telegram chats (defaults to the global DEFAULT_MODEL).
TELEGRAM_MODEL: str = os.getenv("ZERO_AGENT_TELEGRAM_MODEL", DEFAULT_MODEL)

# --- Deep web research ------------------------------------------------------
# The deep_research tool runs a full search -> fetch -> clean-extract ->
# synthesize loop in a single tool call, so the model gets real article text
# from several sources to cross-reference (not just search snippets). All local.

# Self-hosted SearXNG metasearch endpoint (70+ engines, JSON API, no API keys,
# no rate limits). If reachable, both deep_research and the search backend use
# it; otherwise they fall back to DuckDuckGo automatically. Start it with the
# bundled docker-compose.searxng.yml / START_SEARXNG.bat. Set empty to force DDG.
SEARXNG_HOST: str = os.getenv("ZERO_AGENT_SEARXNG_HOST", "http://localhost:8888")

# How many distinct web pages deep_research reads per query (each is fetched and
# cleaned). More sources = more thorough but slower and more context used.
RESEARCH_MAX_SOURCES: int = int(os.getenv("ZERO_AGENT_RESEARCH_SOURCES", "5"))

# Max characters of cleaned article text kept per source (protects the context
# window). The model still gets the gist of each page.
RESEARCH_PER_SOURCE_CHARS: int = int(
    os.getenv("ZERO_AGENT_RESEARCH_SOURCE_CHARS", "3500")
)

# Per-page fetch timeout (seconds) while gathering sources for deep_research.
RESEARCH_FETCH_TIMEOUT: float = float(
    os.getenv("ZERO_AGENT_RESEARCH_FETCH_TIMEOUT", "12")
)

# --- Iterative research agent (search -> assess gaps -> search more) ----------
# How many search ROUNDS the ResearchAgent runs before forced synthesis. Each
# round = one batch of sub-queries + a gap assessment. 1 = single-shot (like the
# bare tool); 2-3 = "iterate-until-confident" (broader, cross-checked evidence).
RESEARCH_MAX_ROUNDS: int = int(os.getenv("ZERO_AGENT_RESEARCH_ROUNDS", "3"))

# Max sub-queries dispatched per round (breadth cap, protects time/context).
RESEARCH_MAX_SUBQUERIES: int = int(os.getenv("ZERO_AGENT_RESEARCH_SUBQUERIES", "4"))

# Max characters of the accumulated corpus handed to the synthesis step.
RESEARCH_SYNTH_MAX_CHARS: int = int(
    os.getenv("ZERO_AGENT_RESEARCH_SYNTH_CHARS", "14000")
)
