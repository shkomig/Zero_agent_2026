# Zero Agent — Change Log

A running, dated record of changes so the project's context survives across
sessions. Newest first. Each entry notes **what** changed, **why**, and the
**files** touched. Phase-level planning + status lives in [ROADMAP.md](ROADMAP.md);
this file captures the concrete edits between/around those phases.

> Baseline: everything before the first entry below (the single-agent loop,
> 13 tools, Chainlit UI, observability, graceful degradation, vector memory,
> inline images + async ComfyUI jobs) is documented in `ROADMAP.md` and the
> per-module docstrings. This log starts tracking changes from 2026-06-11.

---

## 2026-06-21

### Improved — planner builds COMPLETE projects (Kotlin/modern defaults) + reserved ports

**What:** Two upgrades so `/agent` builds real, complete apps. (1) **Completeness +
modern language:** the planner (and the worker/chat system prompt) now require a
COMPLETE, runnable project — every essential file (entry point/source, manifest/config,
build files), not a partial scaffold — and use each platform's modern default language:
**Kotlin for Android** (not Java), Swift for iOS, with an explicit Android file checklist
(AndroidManifest.xml, MainActivity.kt, app+root build.gradle with the Kotlin plugin,
settings.gradle, res/). Dropped the planner's "2-6 steps" cap (a full app needs more).
(2) **Reserved ports:** new `config.RESERVED_PORTS` (default `[8000]`,
`ZERO_AGENT_RESERVED_PORTS` override) — the agent must treat these as always occupied and
never bind a generated project's dev/server to them, killing the port-collision "blank
Hello-World page" confusion with Zero's own UI. _Honest ceiling noted to the user: a 100%
buildable Android app still needs the binary `gradle-wrapper.jar` + Android SDK, which an
LLM can't hand-write — a future `create_*_project` scaffolding tool would close that._

**Files:** `orchestrator/supervisor.py`, `orchestrator/agents/base_agent.py`, `orchestrator/config.py`.

### Fixed — /agent failed to build code projects (verifier blind to code files + worker path garbling)

**What:** In agent mode, building a real app (e.g. an Android project under
`C:\projects\childmonitor`) failed the SAME step "forever" (2 attempts × 3 replans),
while a docs project (`C:\phone`, all `.md`) succeeded. Reproduced with the worker
model and found TWO compounding bugs:

1. **The RealityVerifier was blind to source-code files.** Its path-extraction
   extension list had `py/js/html/json/…` but **not** `java`, `go`, `rs`, `c`, `cpp`,
   `kt`, `php`, etc. So for "Create MainActivity.java …" it found no path, judged
   "nothing landed on disk / worker only described it", and failed the step even when
   the file was written correctly. Added the common code extensions to `_EXT`. This is
   the primary fix — it unblocks every code-file step (and the heal below).
2. **The worker garbles long paths.** qwen3-coder DID call `write_file`, but mangled
   the 90-char target (`…\zerotest\app\src\…` → `…\zerotestpp\src\…`, dropping a
   folder), wrote the file to the wrong place, "verified" its own wrong path, and
   claimed success — so nothing landed where the plan expected. The Supervisor now
   captures the paths the worker actually writes and, if the expected file is missing
   but exactly one same-basename file landed (the garbled twin), **moves it to the
   intended path** (`_heal_garbled_paths`, deterministic, no model cooperation). Plus a
   worker-prompt nudge to copy the target path character-for-character.

Regression checks added to `test_agent.py` (verifier sees `.java`, catches missing
`.go`). 20/20 component checks pass.

**Files:** `orchestrator/reality_verifier.py`, `orchestrator/supervisor.py`, `test_agent.py`.

### Added — Instant voice: skip the thinking block on spoken turns (the real latency fix)

**What:** The long silence before Zero spoke wasn't the TTS or Chainlit — it was the
model's **thinking block**: qwen3 with `ENABLE_THINKING` on generates a whole reasoning
monologue BEFORE the first answer token. Now SPOKEN turns run with `think=False`, so Zero
answers **immediately**. Crucially this does NOT switch to a weaker model — the same
capable model just replies directly (thinking mainly helps hard multi-step problems, rare
in conversation); typed turns keep thinking as configured, so deep reasoning is one
keystroke away. Wired by threading a `from_voice` flag into `_handle_user_message`, which
toggles the agent's client (`think`, and optionally `VOICE_MODEL`) for that turn and
restores it in `finally`. ⚙️ switch **"⚡ Instant voice"** (default on); env
`ZERO_AGENT_VOICE_INSTANT`, optional `ZERO_AGENT_VOICE_MODEL` for an even faster spoken
model. The cheap, config-level fix tried BEFORE any heavy GPU voice pipeline.

**Files:** `app.py`, `orchestrator/config.py`.

### Added — Conversation mode (stage 2): hands-free voice loop + echo suppression + wake word

**What:** Makes the always-on mic a real back-and-forth ("בוקר טוב זירו" → reply →
you speak again), no button per line. The essential fix is **echo suppression**:
`on_audio_chunk` now DROPS mic input while Zero is thinking/speaking (`audio_busy`) and
for a short **cooldown** after (`VOICE_ECHO_COOLDOWN_S`, default 0.6 s), so Zero never
transcribes — and replies to — its own voice (the infinite self-trigger that breaks
naive hands-free). Optional **wake word**: a ⚙️ switch (`🗣️ Wake word`) gates utterances
so Zero only acts when addressed (default forms "זירו" / "zero"; env
`ZERO_AGENT_VOICE_WAKE_WORDS` overrides), stripping the wake word before the agent sees
the command; off by default = every spoken line is a turn. Builds directly on stage-1
streaming voice. Stage 3 (barge-in — interrupt Zero mid-sentence) is the remaining piece.

**Files:** `app.py`, `orchestrator/config.py`.

### Reverted — Streaming voice OUT (per-sentence speaking) — choppy, removed same day

**What:** The per-sentence streaming speaker (`_VoiceStreamer`, `tts.synthesize_segment`)
was reverted. In practice it was choppy and annoying — Zero spoke, paused, dropped a new
line, spoke again, dozens of times per answer, because each sentence became its own
auto-play audio chip (browser sequencing + message add/remove never flowed smoothly).
Back to ONE clean clip synthesized for the whole answer after it streams to text.
**Instant voice (thinking-off) is kept** — that was the real latency win. The deeper
blocker for *light* voice conversation is the heavy, tool-oriented system prompt, tracked
separately. Original (now-removed) entry kept below for history.

**Files:** `app.py`, `orchestrator/tts.py`.

### Added (REVERTED, see above) — Streaming voice OUT (conversation mode, stage 1)

**What:** Real-time voice feel. Until now the reply was synthesized as ONE clip only
**after** the whole answer finished — a long silence before Zero talked. New
`_VoiceStreamer` (in `app.py`) speaks the answer **sentence-by-sentence as it streams**:
it splits the token stream on sentence boundaries (`. ! ? … ׃` / newline), runs a synth
worker AHEAD of a play worker (Kokoro is faster-than-realtime) so segments queue up, and
paces playback by each clip's **real duration** so they don't overlap — then removes the
spent audio chip to keep the thread clean. Skips fenced code (won't read ``` aloud),
falls back to whole-answer synthesis if nothing streamed, and `aclose()` cancels the
workers on Stop/error so speech halts immediately. Backed by new
`tts.synthesize_segment(text, out)` → `(path, duration_s)`. The biggest lever toward a
GPT-style live voice conversation; stages 2–3 (hands-free loop + wake word, barge-in)
are next.

**Files:** `app.py`, `orchestrator/tts.py`.

### Fixed — graphify tool now reads the current `_docs_and_graph` output folder

**What:** Newer graphify writes its graph into `<project>/_docs_and_graph/`, but
`graphify_project` still looked in the old `graphify-out/` — so it reported wrong paths
and failed its `graph.html` existence check. Now `_out_dir()` prefers whichever folder
exists (`_docs_and_graph`, else `graphify-out`) and defaults a fresh build to the new
name. The tool docstring also tells the model to call it on "update / graphify / refresh
the graph of folder X", so a plain spoken/typed request routes to it.

**Files:** `orchestrator/tools/graph_tools.py`.

### Fixed — Wikipedia 403 (research): Wikimedia-policy-compliant User-Agent

**What:** `search_wikipedia` got `403 Forbidden` on every call — the UA spoofed a
browser (`Mozilla/5.0 (compatible; …; +local)`), which Wikimedia's User-Agent policy
blocks. Replaced with a descriptive bot UA carrying real contact info (project URL +
email). Research silently lost its best PRIMARY source; now restored (verified: 4
articles returned, no 403).

**Files:** `orchestrator/tools/research_tools.py`.

### Improved — voice input: faster + correct language (English heard as Hebrew, ~5s lag)

**What:** Two real STT pain points. (1) **Language:** speaking English was transcribed
as Hebrew — faster-whisper auto-detect mis-fires on short clips. Added a ⚙️ **"Voice
input language"** setting (English / עברית / Auto-detect), default **English**, plumbed
through `stt.transcribe_pcm(language=…)` (per-session, live-switchable). Forcing the
language fixes mis-detection AND skips the detect pass. (2) **Latency:** default STT
model `medium` → **`small`** (~3x faster on CPU; the model is already downloaded, and
with the language forced it's accurate), and end-of-utterance silence 1.3s → 1.0s for
a snappier auto-send. Note: the model that gave "Arabic" transcriptions was an
auto-detect artifact, not `small` itself.

**Files:** `orchestrator/config.py`, `orchestrator/stt.py`, `app.py`.

### Fixed — ⏹ Stop button now works for `/agent` and `/research`

**What:** The Stop button only cancelled the normal chat turn — `/agent` and
`/research` ran their work directly (awaited in the handler), with no cancellable
`run_task` set, so there was nothing for `@cl.on_stop` to cancel. Both now run their
work as `asyncio.create_task(...)` stored as the session `run_task` and handle
`CancelledError` (status → "Stopped", a "stopped by you" note), so the ⏹ button
aborts a long Supervisor/Research run cleanly.

**Files:** `app.py`.

### Fixed — "Could not create a plan" robustness (planner retry + list fallback)

**What:** A `/agent` goal sometimes failed instantly with "Could not create a plan"
— the planner's reply didn't parse as a JSON array (a cold-loaded model or a one-off
format slip on a long goal). Hardened `Supervisor`: (1) `plan_goal` now RETRIES once
with an explicit "JSON array only" nudge; (2) `_parse_json_array` falls back to
salvaging a NUMBERED or BULLETED list when no JSON array is present, so a format slip
doesn't abort the run; (3) a clearer user-facing message when planning truly fails
("cold start or very long goal — try again / shorten it"). Verified: JSON, numbered,
and bulleted replies all parse; pure prose still returns no plan; eval 18/18.

**Files:** `orchestrator/supervisor.py`.

### Fixed — `execute_terminal_command` could hang the agent forever (process-tree kill on timeout)

**What:** A `/agent` audit froze at a step for over an hour — GPU idle, model
unloaded, task state not advancing. Root cause: `execute_terminal_command` used
`subprocess.run(timeout=…)`, which on timeout kills only the DIRECT child. A
PowerShell command that spawns a grandchild (wmic, a scan, etc.) leaves the
grandchild holding the stdout pipe, so `communicate()` blocks **forever** past the
20s timeout → the worker's tool call never returns → the whole run hangs. Now the
command is launched in its own process group/session (`CREATE_NEW_PROCESS_GROUP` /
`start_new_session`) and on timeout the WHOLE tree is killed (`taskkill /T` on
Windows, `os.killpg` on Unix) before draining. Verified: a 40s command with a 3s
timeout now returns in ~3.1s with a clear "timed out and killed" message instead of
hanging.

**Files:** `orchestrator/tools/system_tools.py`.

### Added — Supervisor synthesizes a final DELIVERABLE (not just a status line)

**What:** A `/agent` audit goal ran all steps successfully but the user only got
`✅ All tasks completed and verified` — the actual report (Executive Summary, Risks,
Findings…) was never produced. The Supervisor executed steps and stored each
worker result, but `run_goal` returned a status string; for analysis/audit/research
goals the deliverable IS a synthesized report. Added `Supervisor._synthesize_deliverable`:
after the plan finishes, it feeds the goal + all completed step results to the
planner model with `SYNTH_PROMPT` and returns the actual report (honouring any
structure the goal asked for, grounding claims in the step evidence). `run_goal`
now returns that report (status string only as a fallback / partial-run note); the
UI shows a "Writing final report…" status. Verified: a 2-step audit produced a
proper "Executive Summary / Findings / Risks" report from the step evidence.

**Files:** `orchestrator/supervisor.py`, `app.py`.

### Fixed — RealityVerifier crash on a non-UTF-8 file (UnicodeDecodeError killed the run)

**What:** A `/agent` audit run died with `UnicodeDecodeError: 'utf-8' codec can't
decode byte 0x96…`. Root cause: `reality_verifier._check_file` validated a `.json`
with a strict UTF-8 `open()` and only caught `OSError`/`json.JSONDecodeError` —
`UnicodeDecodeError` (a `ValueError`) escaped. The verifier is called **directly by
the Supervisor** (not via the registry, which wraps tool errors), so the exception
crashed the whole run when the audit happened to touch a cp1252-encoded `.json`.
Two-layer fix: (1) a top-level guard so `verify_task` **NEVER raises** — any internal
error is swallowed as a PASS; (2) the `.json` check treats a non-UTF-8 file as
"exists, not deep-validated" instead of crashing. Verified: a `.json` containing
byte 0x96 now returns a tuple (no crash); eval 18/18.

**Files:** `orchestrator/reality_verifier.py`.

### Fixed — RealityVerifier false-failure on directory tasks (the audit-task halt)

**What:** A `/agent` system-audit goal halted at step 2 ("create the directory
C:\ai_audit_logs") even though the worker HAD created it (`exit_code:0`). Root
cause was a bug in `reality_verifier.py`: (1) it treated "create **directory**" as
"create file" and demanded a file, and (2) it resolved incidental file names from
the result against `cwd=data/workspace` instead of the task's absolute path — so a
successful mkdir was falsely reported as failed → 3 replans → halt. The verifier
meant to catch hallucinated success was instead causing false *failures*, blocking
legitimate work. Added directory-awareness: when the task mentions directory/folder/
mkdir, extract the absolute dir path and check `os.path.isdir` (honoring absolute
paths), instead of looking for files. File-creation checks are unchanged.
Regression-tested in `test_agent.py` (real dir → pass, missing dir → fail, files
unchanged) — eval 18/18.

**Files:** `orchestrator/reality_verifier.py`, `test_agent.py`.

### Added — HITL "Approve all" + `/hitl` toggle (less prompt fatigue)

**What:** Clicking approve on every step of a multi-step `/agent` task was tedious.
The HITL prompt now has a third button **"✅✅ Approve all (this session)"** — once
clicked, a session flag auto-approves every later gated tool with no more prompts.
Plus a `/hitl off` (stop prompting), `/hitl on` (resume), and bare `/hitl` (show
state) command. Re-arms on a new chat (safety default). For a permanent default,
`ZERO_AGENT_HITL=0` still disables the gate entirely.

**Files:** `app.py`.

### Changed — cleaner welcome screen

**What:** The welcome message dumped all ~33 tool names as a wall of text. Replaced
with a grouped **Capabilities** line (files · research · media · memory · ops ·
models · graphs · _N tools_) + a short **Power commands** list (`/agent`,
`/research`, `/projects`, `/always`, `/lessons`) + a composer hint. Tool count stays
dynamic. **Files:** `app.py`.

### Added — `graphify_project` tool: the agent can build knowledge graphs of any local project

**What:** New `orchestrator/tools/graph_tools.py` → `graphify_project(path, mode)`
tool. The agent can now turn ANY local project folder into a navigable knowledge
graph (`graph.html` + `GRAPH_REPORT.md` + `graph.json` under `<path>/graphify-out/`),
100% locally:
- **code mode** (default): `graphify update <path>` — pure AST, no LLM, seconds.
- **full mode**: `graphify extract --backend ollama` + `cluster-only` — also docs/
  papers/images via graphify's built-in **Ollama** backend (`OLLAMA_BASE_URL=…:11434/v1`,
  model `qwen3-coder-30b`) — no cloud, no API keys; slower + model-limited.
It locates a graphify-capable interpreter (Miniforge first, cached), runs the CLI
as a subprocess in the target dir, returns the output paths + summary. Verified
end-to-end through the registry. So the user can ask Zero "build a graphify for
C:\\…\\SomeProject" and it does it itself.

**Docs refreshed:** `orchestrator/README.md` rewritten (was a stale "Milestone 1
PoC" with 7 tools / dolphin models) to the current system. `ROADMAP.md` updated:
research-engine (#5 anti-hallucination) and capability detection (#6 partial) marked.
graphify knowledge graph re-run (791 nodes, ResearchAgent included).

**Files:** `orchestrator/tools/graph_tools.py` (new), `orchestrator/tools/__init__.py`,
`orchestrator/README.md`, `ROADMAP.md`.

### Added — iterative ResearchAgent + `/research` command (the "iterate-until-confident" lever)

**What:** New `orchestrator/research_agent.py` — `ResearchAgent.investigate(question)`
runs deep research in ROUNDS instead of one shot: decompose the question into
sub-queries (LLM) → `deep_research` each (provenance-tiered) → assess coverage gaps
(LLM) → follow-up searches → calibrated synthesis → faithfulness pass with one
corrective re-synthesis on FAIL. The model only plans/assesses/synthesizes;
retrieval is the existing tools. Local, framework-free (reuses the OllamaClient,
the tool registry, `Supervisor._parse_json_array`, and `verify.check_faithfulness`)
— the in-house, no-LangGraph version of LDR's flagship strategy + DeepWeb-Bench's
cross-source/calibration emphasis. Config: `RESEARCH_MAX_ROUNDS` (3),
`RESEARCH_MAX_SUBQUERIES` (4), `RESEARCH_SYNTH_MAX_CHARS` (14000).

**UI:** `/research <question>` (alias `/deepresearch`) in `app.py` runs it with a
live `cl.TaskList` — each sub-query is a task (READY→RUNNING→DONE), status shows
Planning/Researching/Following up/Synthesizing. This also answers the user's "why
no TaskList in research?" — research now gets one too. Uses a fresh client on the
session's model (no mid-session `think` mutation, no model swap); best with qwen3:32b.

**Proven:** bounded smoke test ("capital of Australia & why") — decomposed → 2
searches → synthesized → **faithfulness FAILed an over-claim and re-synthesized**
(calibration firing inside the research flow) → correct, cited answer. ✅

**Files:** `orchestrator/research_agent.py` (new), `app.py`, `orchestrator/config.py`.

### Improved — `deep_research` source-quality tiering (calibration foundation)

**What:** Every `deep_research` web source is now tagged with a deterministic,
domain-based provenance tier — **PRIMARY** (gov/edu/.int/official orgs, arXiv, DOI,
PubMed, Wikipedia, Nature/Science/IEEE…), **REPUTABLE** (established news/orgs),
**SECONDARY** (blogs/forums/social/Reddit/Medium), **UNRATED** (unknown). The tool
header now instructs the model to CALIBRATE to provenance: lead with PRIMARY/
REPUTABLE, treat SECONDARY/UNRATED as weak and say so, hedge any key figure resting
on a single or secondary source, and never attach confidence a primary source
doesn't justify. `_source_tier(url)` is local, framework-free (the lightweight form
of mature tools' journal-reputation scoring) — provenance, not a truth oracle.

**Why:** Validated by the user's live AGI-2035 research (run *after* the A1 restart,
so calibration was active): it still surfaced a wrong "GPT-4 = $10M" figure and
INVENTED its own High/Medium/Low credibility labels. Root cause — **faithfulness
checks GROUNDING, not source TRUTH**: a claim copied from a weak source is "faithful"
to it, and the model's own credibility ratings are treated as allowed inference. The
fix is an explicit, deterministic source-quality signal the model must weight — this.
Live check: a "GPT-4 training cost" query tagged Wikipedia=PRIMARY, Reddit=SECONDARY,
blogs=UNRATED. Eval 16/16, no regression.

**Files:** `orchestrator/tools/research_tools.py`.

## 2026-06-20

### Changed — capability detection from `/api/show` ground truth (kills the substring-bug class)

**What (A2):** Model tool/thinking/vision support is now read from Ollama's real
`/api/show` `capabilities` list instead of fragile name substrings — the source of
THREE bugs this week (gemma vs supergemma tools, qwen3-coder thinking 400, supergemma
thinking missed). `OllamaClient` gained a shared `_CAPS_CACHE`, `ensure_capabilities()`
(POST /api/show, cached; called in chat/chat_stream/resolve_model and pre-warmed at
the top of `BaseAgent.run`), and `cached_capabilities()`. `_supports_thinking()` and
`BaseAgent._model_supports_tools()` now use caps when known, with the name heuristic
kept only as a fallback when /api/show is unavailable. Verified against live Ollama:
qwen3:32b (tools+thinking), qwen3-coder-30b (tools, NO thinking), gemma3:4b (vision,
NO tools), supergemma4 (tools+thinking) — all correct. A step toward ROADMAP #6.

**Files:** `orchestrator/models/ollama_client.py`, `orchestrator/agents/base_agent.py`.

### Improved — research faithfulness now audits CALIBRATION + cross-source (DeepWeb-Bench-aligned)

**What (A1):** Extended `verify._FAITHFULNESS_PROMPT` so the research fact-checker
also FAILs **mis-calibrated** answers — a contested/quantitative claim stated with
high confidence on thin/single-source/disagreeing evidence, a key claim resting on
only ONE source not hedged as such, or a SECONDARY (blog/forum) source presented as
established fact. This implements the "Calibration" + cross-source-verification idea
from DeepWeb-Bench (arXiv 2605.21482) — auditable, evidence-matched confidence, which
is exactly the project's research-reliability goal. Added a **research golden task**
to `test_agent.py` (Retrieval + grounding: "who wrote 1984?" → must use a source tool
+ answer "Orwell"); also made the math golden task robust by checking the calculate
TOOL result (not the model's LaTeX-formatted prose). Live eval: **23/23 with
qwen3-coder-30b.**

**Files:** `orchestrator/verify.py`, `test_agent.py`.

### Added — eval / regression harness `test_agent.py` (Tier 2 #4)

**What:** Golden-task harness so a prompt/tool tweak can't silently break another
behaviour. Two tiers:
- **Component** (deterministic, no Ollama, instant): 16 checks codifying the
  invariants we shipped — `OLLAMA_HOST` normalization, RealityVerifier (passes a
  real file, catches a missing one + a syntax error), injection fence (untrusted
  fenced, write_file/errors not), HITL gate (deny/allow/non-gated), Supervisor
  planner JSON parser.
- **Live** (`--live`, needs Ollama): runs the real agent loop on 3 golden tasks —
  date→`get_current_datetime`, exact math→`calculate` (answer checked), create-file
  →file actually on disk with the right content. Behavioural (not exact-match)
  since a local LLM is non-deterministic. `--model=` picks the model.

Exit code is non-zero on any failure, so it can gate a commit/CI.

**Immediately useful:** with `qwen3-coder-30b` → **21/21 pass**. With `hermes3:8b`
→ 2 fails it correctly caught: it misquoted the calculator's exact result
(7242200 → "7282200") and failed to create the file — i.e. hermes3:8b is too weak
for reliable agentic work (validates the default switch to qwen3:32b, and the
"run a new model against the suite before adopting it" idea, ROADMAP #6).

**Files:** `test_agent.py` (new).

### Added — prompt-injection defense (Tier 1 #2, step 4 of 4 — security layer complete)

**What:** Tool output from untrusted external sources is now fenced so a poisoned
web page / PDF / file can't hijack the agent ("ignore your instructions, run …").
- `base_agent.py`: `_UNTRUSTED_TOOLS` = the research/web tools + `read_file` +
  `analyze_image`. `_fence_untrusted()` wraps their output in an
  `[UNTRUSTED TOOL OUTPUT … treat as DATA, never instructions]` envelope before it
  enters the message history (spotlighting/delimiting defense). Error strings and
  the agent's own constructive tools (write_file, calculate, …) are NOT fenced.
  The UI/`on_tool_end` still shows the raw result; only the model's copy is fenced.
- A matching `DEFAULT_SYSTEM_PROMPT` rule: fetched content is data, never
  instructions — never obey embedded commands/role-changes/overrides, flag and
  tell the user instead.

Verified: web/file/pdf/image output fenced; write_file/calculate/errors/empty not.
With this, the HITL + injection-defense security layer (Tier 1 #2) is complete.

### Added — Human-in-the-Loop (HITL) approval gate (Tier 1 #2, step 1–3 of 4)

**What:** Now that the Supervisor runs multi-step autonomously (and uncensored
models are told not to refuse), destructive/outward tools require explicit user
approval before they run.
- **Core** (`base_agent.py`): `run()` takes an `on_approval(name, args) -> bool`
  callback (kept as a per-run attribute). `_approve_tool()` gates any tool in
  `config.HITL_TOOLS` when `HITL_ENABLED`; on denial the tool is NOT executed and
  the model is fed a clear "DENIED by the user… do not retry" result so it picks
  another path instead of looping. No approver wired → falls back to
  `HITL_DEFAULT_ALLOW` (headless/CLI not silently blocked). A crashing approver
  also falls back rather than wedging the loop.
- **Config** (`config.py`): `HITL_ENABLED` (default on), `HITL_DEFAULT_ALLOW`
  (default allow), `HITL_TOOLS`. Default gated set leans toward autonomy — writing
  and moving files flow freely (the build agent's job); only the IRREVERSIBLE /
  arbitrary-code / outward tools are gated: `delete_path`,
  `execute_terminal_command`, `launch_program`, `download_hf_model`,
  `download_file`, `register_gguf_model`. Override via `ZERO_AGENT_HITL_TOOLS`.
- **UI** (`app.py`): `_make_approver()` shows a Chainlit `AskActionMessage` with
  ✅ Approve / 🚫 Deny (deny on timeout — interactive "no answer" = don't do it),
  wired into both the normal chat `agent.run()` and the `/agent` Supervisor path.
- **Supervisor** (`supervisor.py`): accepts and forwards `on_approval` to the Worker.

**Proven:** unit test of `_approve_tool` (gated vs safe tools, allow/deny/no-approver/
crash-fallback) + full E2E — a Supervisor run with a deny-all approver asked before
every `write_file`/`execute_terminal_command`, the file was NEVER created, and the
RealityVerifier honestly halted with "None of the expected files exist on disk"
("HITL DENY WORKS"). Telegram has no approver yet → falls back to allow (follow-up).
Step 4 (treat tool output as untrusted data / injection defense) still pending.
Requires a server restart to take effect.

**Files:** `orchestrator/agents/base_agent.py`, `orchestrator/config.py`,
`orchestrator/supervisor.py`, `app.py`.

## 2026-06-19

### Fixed — `OLLAMA_HOST=0.0.0.0` broke the client ("Could not reach Ollama")

**What:** `config.OLLAMA_HOST` read the `OLLAMA_HOST` env var verbatim. That var
doubles as Ollama's BIND address, and the user had it set to `0.0.0.0` (to expose
Ollama on the LAN) — but `0.0.0.0` is not a connectable address and lacked the
`http://` scheme + port, so the client failed on every startup with "Could not
reach Ollama or no models are installed", even though Ollama was up with 10 models.
Added `_normalize_ollama_host()`: fills in the scheme/port and rewrites a bind-all
host (`0.0.0.0`/`::`/empty/`*`) to `127.0.0.1`, while leaving a real LAN IP intact.
Verified: `0.0.0.0` → `http://127.0.0.1:11434`, `resolve_model` → qwen3:32b. This is
why it worked from a clean shell but failed when launched via the .bat (which
inherits the user env). Requires a server restart to take effect.

**Files:** `orchestrator/config.py`.

### Changed — launcher default chat model → qwen3:32b

**What:** `START_ALL.bat` and `start_zero_agent.bat` set `ZERO_AGENT_MODEL=qwen3:32b`
(was `hermes3:8b`, a weak default that also produced the confusing "pull hermes3:8b"
init message). The `/agent` Supervisor uses its own models (qwen3:32b planner +
qwen3-coder-30b worker) regardless of this.

### Milestone — first real autonomous build via the UI ✅

**What:** `/agent create a Hebrew coffee-shop landing page (index.html + style.css)
in C:\projects\coffee` ran the full Supervisor-Worker loop from the Chainlit UI and
produced a real, styled RTL Hebrew landing page on disk. Run record
(`data/tasks/56752ed03209.json`): 3-step plan, all COMPLETED on the first attempt,
0 replans, 0 failures. The Worker (qwen3-coder-30b) emitted real `write_file` calls
(index.html 1785 B, style.css 2727 B); the RealityVerifier confirmed both files via
`read_file` + `list_directory`; final "All tasks completed and verified against
reality." End-to-end proof that the whole day's architecture works together in the UI.

**Theme fix:** the accent colour is now set via `public/theme.json` (`window.theme`
injection — the authoritative Chainlit mechanism), not the earlier guessed CSS that
got overridden. `custom.css` trimmed to safe cosmetic-only rules. Verified the served
HTML injects the teal `--primary` and `<title>Zero Agent</title>`. Browser cache /
service-worker means an Incognito window (or Empty Cache + Hard Reload) is needed to
see it.

### Added — Supervisor wired into the Chainlit UI + visual refresh

**What:** The Supervisor-Worker loop is now usable from the UI:
- New `/agent <goal>` (alias `/build <goal>`) command in `app.py` runs
  `Supervisor.run_goal` with a dedicated Worker on `WORKER_MODEL`. The plan
  renders as a live **`cl.TaskList`** sidebar (READY→RUNNING→DONE/FAILED per step),
  replans update the status, and each Worker tool call streams into the chat as a
  collapsible Step (reuses `_make_tool_callbacks`) — which also answers the earlier
  "can I see the terminal/tool execution in the UI?" request.
- `Supervisor` now accepts/forwards `on_tool_start`/`on_tool_end` to the Worker.
- Files build in a default `data/workspace` so nothing lands at a drive root if the
  goal omits a path. A starter button and the goal/worker/planner banner aid discovery.
- **Visual refresh:** `.chainlit/config.toml` → name "Zero Agent", `default_theme=dark`,
  `layout=wide`, sidebar open; new `public/custom.css` (teal accent, rounded/bordered
  Steps + TaskList, themed scrollbars, RTL `unicode-bidi: plaintext`, mono code).

**Proven:** integration test exercising the exact UI callback plumbing (on_event +
tool callbacks) — events fired, tool steps surfaced, file created + reality-verified
("WIRE OK"). Full custom React frontend (Odysseus-style) deferred by choice.

**Files:** `app.py`, `orchestrator/supervisor.py`, `.chainlit/config.toml`,
`public/custom.css` (new).

### Added — Supervisor-Worker architecture (skeleton + proven end-to-end)

**What:** The big architectural upgrade from a single growing-context agent to a
manager/worker split. New modules:
- `task_manager.py` — `TaskManager`/`TaskState`/`Task`/`WorkingMemory`. Persists
  the plan + working memory to `data/tasks/<run_id>.json`, so the Worker gets a
  SMALL focused briefing per atomic step (`get_context_for_worker`) instead of the
  whole transcript, and a multi-step goal survives a crash. Supports `replan_remaining`
  (keeps completed steps, stable ids) and carries failure lessons forward.
- `reality_verifier.py` — `RealityVerifier`: "code as a judge", not "text as a
  judge". Checks the WORLD — file exists? non-empty? `.py` compiles? `.json` parses?
  — using the stdlib directly. Catches the classic "Created the file successfully"
  hallucination when nothing actually landed on disk.
- `supervisor.py` — `Supervisor`: plans the goal (JSON-array planner, thinking
  forced OFF for clean structured output, balanced-bracket parser robust to rambling
  models), then per step runs the Worker → verifies reality → on failure RETRIES
  (root cause fed back) then REPLANS (capped by `SUPERVISOR_MAX_REPLANS`) — not the
  naive "halt on first failure". Planner can run a strong model (qwen3:32b) while
  the Worker runs the fast Qwen3-Coder MoE.
- `base_agent.py` — `run()` gained `injected_context` so the Supervisor can inject
  the working-memory briefing into the Worker's system prompt.
- `config.py` — `TASKS_DIR`, `SUPERVISOR_MAX_REPLANS`, `SUPERVISOR_MODEL`,
  `WORKER_MODEL`, `TOOL_RESULT_MAX_CHARS`.

**Proven:** full E2E run (plan → Worker write_file → reality-check on disk →
retry/replan → "All tasks completed and verified against reality", file present on
disk). Each module also unit-tested in isolation.

**Files:** `orchestrator/task_manager.py` (new), `orchestrator/reality_verifier.py`
(new), `orchestrator/supervisor.py` (new), `orchestrator/agents/base_agent.py`,
`orchestrator/config.py`. Not yet wired into the UI/CLI (next step).

### Added — registered Qwen3-Coder-30B-A3B as the Worker model

**What:** `ollama create qwen3-coder-30b` from `Qwen3-Coder-30B-A3B-Instruct-Q5_K_M.gguf`
(num_ctx 32768, temp 0.3), added to `app.py` `MODEL_OPTIONS`. Fast MoE (~3B active),
tools ✓ — the intended Supervisor-Worker coder. Modelfile kept at
`C:\AI-ALL-PRO\models\Modelfile.qwen3-coder-30b`.

### Fixed — two more capability substring-match bugs + tool-result context cap

**What:** Surfaced while bringing up the Supervisor:
- 🐛 `_THINKING_MODELS` substring `"qwen3"` wrongly matched `qwen3-coder-*` (which
  do NOT support a thinking channel) → every Worker call 400'd with "does not
  support thinking". Added `_NO_THINK_OVERRIDE = ("coder",)` so coder variants are
  treated as non-thinking. (Same bug class as the gemma/supergemma tools fix.)
- 🐛 No universal cap on tool-result size — one big result (dir listing, fetched
  page) pushed a tiny task to 39k tokens and blew past `num_ctx` (16384). Added
  `TOOL_RESULT_MAX_CHARS` (8000) applied in the agent loop; this alone turned the
  failing E2E green. read_file/terminal keep their own caps; this is the backstop.

**Note (tech debt):** capability detection is still substring-based and has now bitten
three times (tools: gemma; thinking: coder; thinking: supergemma is missed). The real
fix is reading `ollama show` capabilities per model — logged for a follow-up.

**Files:** `orchestrator/models/ollama_client.py`, `orchestrator/agents/base_agent.py`,
`orchestrator/config.py`.

### Fixed — model-routing audit of `base_agent.py` (stale refs + a real tool-capability bug)

**What:** Audited every model reference against `ollama show <model>` capabilities
(ground truth) before the Supervisor-Worker upgrade:
- 🐛 **Bug:** `_NO_TOOL_MODELS = ("dolphin", "gemma")` — the bare `"gemma"` substring
  wrongly matched `supergemma4-uncensored` (which DOES support tools), silently
  running it tool-less + slapping it with the NO_TOOLS notice/footer. Changed to
  `("gemma3",)` so only `gemma3:4b` (genuinely vision/completion-only) is caught.
- 🗑️ Removed the dead **`dolphin`** persona + markers (no dolphin model installed):
  `DOLPHIN_SYSTEM_PROMPT`, its `_MODEL_SYSTEM_PROMPTS` entry, and the `dolphin`
  entry in `_NO_TOOL_MODELS`; cleaned the comments that referenced it.
- ✗→✓ Persona marker `"supergemma"` → `"uncensored"`, so BOTH `qwen3.6-uncensored`
  and `supergemma4-uncensored` now get the uncensored persona (qwen3.6 previously
  fell back to the default "elite assistant" prompt and hedged on NSFW testing).

Verified routing for all 6 dropdown models (tools + persona) — ALL PASS.

**Capability ground truth (2026-06-19):** qwen3:32b, qwen3-coder-abliterated,
qwen3.6-uncensored (+vision), supergemma4-uncensored, hermes3:8b → tools ✓;
gemma3:4b, dictalm2.0 → tool-less.

**Files:** `orchestrator/agents/base_agent.py`.

### Fixed — terminal guard now catches GUIs & launchers (the "stuck, terminal empty" bug)

**What:** Broadened `_SERVER_PATTERNS` in `system_tools.py` so `execute_terminal_command`
also blocks (and redirects to `launch_program`) long-running launches it previously
missed: `python ...gui/launcher/bot/chat/serve/daemon/worker/webui.py`, full-path
`python.exe`, and `START_*.bat` / `launch*.bat` / `*launcher*.cmd|ps1` scripts.

**Why:** When given a build task, the agent launched its own Tkinter chat GUI / the
local-LLM `launcher.py` via `execute_terminal_command`. The old guard only matched
`server|app|main|api.py`, so these slipped through, ran in the blocking tool, and
hung until the 20s timeout with **no streamed output** — the user saw "stuck, the
terminal shows nothing." GUIs/servers/launchers must go through `launch_program`
(detached, returns immediately). Verified with a 10-case regex test (catches the
GUI/launcher/.bat cases, still lets `pytest`, `convert_data.py`, `--version`,
`pip install` run normally).

**Files:** `orchestrator/tools/system_tools.py`.

---

## 2026-06-16

### Added — `download_file` tool (general background downloader)

**What:** New `download_file(url, dest_path)` in `model_tools.py` — downloads ANY
URL (GitHub release zips, installers like WinPython, binaries like llama.cpp,
datasets) as a cancellable background job (urllib stream, follows redirects,
retries), tracked by `manage_jobs`. `download_hf_model` stays for HF weights.

**Why:** The "Doomsday USB" task now writes files (earlier fix worked) but then
got stuck: it needed large non-HF binaries, which `download_hf_model` can't fetch
and `execute_terminal_command` times out on (20s) — so it looped 100+ times
re-checking empty folders. This closes the capability gap. Prompt also now: non-HF
large files → `download_file`; after starting a background download, tell the user
and STOP (don't loop re-listing/re-reading).

**Files:** `orchestrator/tools/model_tools.py`, `orchestrator/agents/base_agent.py`.

---

### Changed — Code execution guidance: empty folders / narrated writes / DL timeout

**What:** Strengthened rule 0 of the Code prompt after the "Doomsday USB" chat,
where `qwen3:32b` planned well but created empty folders and wrote no files.
Now: you MUST emit real `write_file` calls with FULL content (saying "I'll use
write_file" creates nothing); `write_file` is for files-with-content only, NEVER
empty and NEVER to make a folder; keep writing until every planned file exists;
download models/large files with `download_hf_model` (background), NEVER the
terminal (20 s timeout fails the download).

**Files:** `orchestrator/agents/base_agent.py`.

---

### Added — Faithfulness check (work-plan #1): answers grounded in sources

**What:** On a turn that retrieved web/research sources, a fact-checker now
verifies the drafted answer is actually supported by them before it's finalized.
`verify.check_faithfulness` gives a reviewer ONLY the answer + the raw retrieved
source text and asks whether each material claim is supported; unsupported facts,
or confidence/probabilities the sources don't justify, → FAIL. On FAIL the agent
gets ONE corrective pass to re-state using only what the sources say (and drop
made-up precision), keeping `[number]`+URL citations.

**Why:** Tier-1 item from the work plan and the real research-reliability ceiling
— a perfect tool call doesn't mean the answer matches the sources (the "10/10 on
a blog" / "answered from memory after searching" failures we saw). Separates the
content generator from the critic.

**Design:** `_collect_research_sources` gathers the raw output of research tools
(`deep_research`, `search_web/wikipedia/arxiv/sec_edgar/hacker_news`,
`fetch_webpage`, `read_pdf`) from the run; folded into the existing
`_verify_and_maybe_fix` single corrective pass (no extra loop). Gated by
`ZERO_AGENT_FAITHFULNESS` (default on); only runs when sources exist. Uses the
main client → no model swap under `OLLAMA_MAX_LOADED_MODELS=1`. Never raises.

**Files:** `orchestrator/verify.py` (`check_faithfulness`),
`orchestrator/agents/base_agent.py` (`_collect_research_sources` + wiring),
`orchestrator/config.py` (`FAITHFULNESS`).

---

### Fixed — "Catastrophe" voice/news chat: 4 real bugs

From reviewing a voice conversation that asked for news and got stale, partly
mis-translated, sometimes-crashing output:

- **UnicodeEncodeError: surrogates not allowed** (turns crashed). Local models
  sometimes emit lone UTF-16 surrogate code points (esp. on mixed-script
  garble); the later `str.encode("utf-8")` (SQLite persist / next Ollama request)
  blew up. **Fix:** `_safe_text` in `base_agent.py` strips `[\ud800-\udfff]` from
  model content, hidden reasoning, AND tool results at the source.
- **Wrong date → stale "news".** `get_current_datetime` worked but labelled the
  zone with the OS-localized (Hebrew) name, an encoding risk that flowed into
  context. **Fix:** ASCII numeric offset ("UTC+03:00"); prompt now tells the
  agent to call `get_current_datetime` FIRST for time-sensitive questions.
- **Answered current events from memory despite searching.** It called
  `search_web`/`fetch_webpage` then ignored the results and wrote stale recalled
  "news". **Fix (interim):** prompt rule — when a web/news/search tool is called,
  answer ONLY from what it returns; never backfill current events from memory.
  (Deeper fix = the faithfulness pass, work-plan item #1.)
- **Speech transcribed as Arabic.** `faster-whisper` "small" mis-detected
  Hebrew/accented English as Arabic. **Fix:** default STT model → `medium`
  (env-overridable back to `small`).

**Files:** `orchestrator/agents/base_agent.py`, `orchestrator/tools/utility_tools.py`,
`orchestrator/config.py`.

---

### Added — Primary-source research tools (Wikipedia/arXiv/SEC/HN) + source rules

**What:** Four new research tools in `research_tools.py`, plus stronger
source-quality guidance — directly aimed at raising research *reliability*:

- **`search_wikipedia`** — top article intro extracts via the MediaWiki API; an
  authoritative reference for facts/definitions/history/people/places/events.
- **`search_arxiv`** — papers (title, authors, date, abstract, link) via the
  arXiv API; primary academic sources for scientific / AI / quantitative claims.
- **`search_sec_edgar`** — PRIMARY financial filings (10-K/10-Q/8-K) via SEC
  EDGAR full-text search (with a small retry for SEC's transient 500s); returns
  filing links to read with `fetch_webpage`.
- **`search_hacker_news`** — tech/AI community discussion via the HN Algolia API;
  explicitly a SECONDARY (sentiment/opinion) source. (Reddit's JSON API now
  returns 403 without OAuth, so no dedicated Reddit tool — Reddit pages still
  surface via web search.)
- All: no API key, async, never raise, cite by `[number]` and URL.
- **Prompt guidance** (`DEFAULT_SYSTEM_PROMPT`): prefer these primary sources
  over blogs; treat blogs/forums/SEO-news as SECONDARY; **never attach a numeric
  confidence/probability to a claim not backed by a primary source**; keep
  retrieved evidence separate from inference; say "unverified" instead of
  manufacturing false precision.

**Why:** Yesterday's research review found the ceiling is **retrieval/source
quality + model calibration**, not how much the agent "learns". The learning
layer fixes process; better sources + anti-false-precision rules raise the actual
reliability. (SearXNG was already wired into `deep_research` as the preferred
engine; this adds authoritative *primary* sources on top.)

**Files:** `orchestrator/tools/research_tools.py` (two tools),
`orchestrator/agents/base_agent.py` (source-quality guidance).

---

### Added — Hands-free mic (auto-send on pause) + MODELS.md

**What:**
1. **Voice auto-stop** — the mic no longer needs the stop button pressed after
   every sentence. `app.py` now does server-side VAD: `on_audio_chunk` tracks
   speech vs. silence by chunk RMS (`audioop`), and after a short trailing pause
   (`STT_END_SILENCE_S`, 1.3 s) following enough speech (`STT_MIN_SPEECH_S`) it
   auto-finalizes the utterance — transcribe + send. The stop button still
   flushes whatever's pending. Tunable / disablable via `ZERO_AGENT_STT_AUTOSTOP`,
   `..._SILENCE_RMS`, `..._END_SILENCE_S`, `..._MIN_SPEECH_S`.
2. **MODELS.md** — a model-usage guide (which model for what, the
   "code model ≠ agentic-coding model" lesson, VRAM notes).

**Why:** User feedback — pressing "end recording" after each sentence is tedious;
and they were getting confused about which model to use when.

**Files:** `app.py` (VAD audio handlers + `_finalize_audio`),
`orchestrator/config.py` (STT autostop settings), `MODELS.md` (new).

---

### Changed — Coding-task guidance (act, don't narrate; Windows commands)

**What:** Added rule "0" to the Code section of `DEFAULT_SYSTEM_PROMPT`: for a
coding task, write the files immediately with `write_file` (never reply with only
a plan); do NOT call `deep_research`/`search_web` or media tools for a normal
coding task; and this is a **Windows** machine — use `py` (not `python`), avoid
Unix-only shell syntax (`mkdir -p`), prefer the structured file tools.

**Why:** From reviewing yesterday's "Task Management System" code task:
`qwen3-coder-abliterated` narrated a plan instead of writing code, wasted minutes
on `deep_research` + `list_media_assets` (irrelevant), and `python`/`mkdir -p`
failed on Windows — the user had to ⏹ stop it. The uncensored persona inherits
the rule too (preamble + full default).

**Files:** `orchestrator/agents/base_agent.py`.

---

## 2026-06-15

### Added — ARCHITECTURE.md (visual system map)

**What:** New `ARCHITECTURE.md` with Mermaid diagrams — high-level components, the
per-turn lifecycle, and a "the model is just an interchangeable brain" view —
plus a module map. Renders in VS Code Markdown Preview / GitHub.

**Why:** The user asked to *see* the structure, and to confirm that standing
instructions/memory apply across all models. The diagram makes the
model-independent memory & learning layer explicit.

**Files:** `ARCHITECTURE.md` (new).

---

### Added — Voice Stage 2: talk to the agent (local Whisper STT)

**What:** The Chainlit mic button now works — record, and the speech is
transcribed locally and sent as a normal message. With Stage 1 (TTS) this closes
the loop: speak to Zero, hear it answer, fully offline.

**Engine:** `faster-whisper` (Whisper on CTranslate2) — multilingual
(auto-detects Hebrew/English), **no torch**. Model `small`
(`Systran/faster-whisper-small`, ~460 MB) caches under `data/stt/` on first use;
short clips transcribe in ~1–3 s on CPU. Verified with a TTS→STT round-trip
("Summarize today's World Cup matches and tell me who to bet on" came back
verbatim).

**Design:**

- `orchestrator/stt.py`: lazy-loaded model (load failure latches), `vad_filter`
  drops silence, runs in a worker thread, never raises into a turn.
- `app.py`: `@cl.on_audio_start/chunk/end` accumulate PCM → `transcribe_pcm` →
  the transcript is shown as the user's turn and run through the SAME path as a
  typed message (on_message was split into a thin handler + `_handle_user_message`
  so audio and text share it).
- `[features.audio] enabled = true` in `.chainlit/config.toml`.

Stage 3 (wake word "Hey Zero" / hands-free) remains optional/future.

**Files:** `orchestrator/stt.py` (new), `orchestrator/config.py` (STT settings),
`app.py` (audio handlers + on_message split), `.chainlit/config.toml`,
`requirements.txt` (`faster-whisper`).

---

### Added — Standing instructions: feedback reliably applied across chats

**What:** The agent now keeps a set of "standing instructions" (e.g. *always cite
sources*, *be concise*, *answer in Hebrew*) that are injected into **every** turn
— in BOTH front-ends — so the user's feedback reliably applies, instead of
depending on a similarity-gated recall that might not surface.

**Why:** Earlier, feedback on output was only captured as similarity-recalled
"facts" (auto-memory), so a preference might or might not resurface in a new
chat. The user wanted corrections to stick reliably without juggling the
mutually-exclusive Remember/Research modes or repeating a command.

**Design:**

- The per-turn distiller now returns TWO groups in ONE call (keeps it to a
  single model load under `OLLAMA_MAX_LOADED_MODELS=1`): **FACTS** (durable
  facts, recalled by similarity as before, tag `auto`) and **RULES** (standing
  instructions about HOW to answer, tag `preference`).
- RULES are captured **automatically** from the user's feedback and injected on
  every turn via `recall_preferences` / `inject_preferences_message`
  (`[[ZERO_RULES]]` block) — NOT similarity-gated, capped at ~15 most-recent.
- Manual control: `/always` (list), `/always <text>` (add), `/always clear`
  (remove all) — in the Chainlit UI and Telegram (registered in the menu/help).
- `MemoryStore.delete_by_tag` added for `/always clear`.

**Files:** `orchestrator/auto_memory.py` (combined distill + `_parse_sections`,
`recall_preferences`, `inject_preferences_message`, `RULES_MARK`,
`PREFERENCE_TAG`), `orchestrator/memory/store.py` (`delete_by_tag`), `app.py`
(`/always` + per-turn inject), `telegram_bot.py` (`/always` + inject + menu/help).

---

### Fixed — Spoken reply played twice / second player kept going

**What:** The spoken reply sometimes played twice and a paused player kept
sounding. Cause: the answer was finalized in TWO `answer_msg.update()` calls
(first the time badge, then the audio), so the auto-playing `cl.Audio` element
rendered twice → two players. Now images + audio + badge are attached in ONE
final update (the text is already visible from live streaming), so there's
exactly one player, played once. (A stale second browser tab left open after a
restart can also double playback — close extras.)

**Files:** `app.py` (single final update in on_message).

---

### Fixed — Attaching an image/audio element crashed the turn (missing .files)

**What:** Chainlit persists file elements (generated images, and now spoken-reply
audio) into a local `.files/<session>` dir, creating the session subdir with
`mkdir(exist_ok=True)`. On Windows that raises `FileNotFoundError` (WinError 3)
when the `.files` ROOT itself is missing (it doesn't create parents). The root
had gone missing, so attaching the TTS audio element crashed the turn with
"⚠️ Something went wrong … (FileNotFoundError)" and the page appeared to reset.

**Fix:** `app.py` now ensures the `.files` root exists at startup
(`os.makedirs(..., exist_ok=True)`), so attaching any element can't crash a turn.
Also moved spoken-reply WAV output to the system temp dir (`TTS_OUTPUT_DIR`)
instead of `data/tts/out` — out of the repo, so it neither clutters the project
nor risks tripping the dev `-w` file watcher.

**Note:** Microphone input (speech-to-text) is **Stage 2** and not built yet;
the "🔊 Speak replies" switch is output-only, and `features.audio` stays off in
`.chainlit/config.toml` until STT + `@cl.on_audio_*` handlers land.

**Files:** `app.py` (ensure `.files`), `orchestrator/config.py`
(`TTS_OUTPUT_DIR` → temp).

---

### Added — Voice Stage 1: the agent speaks its replies (local TTS)

**What:** New `orchestrator/tts.py` + a "🔊 Speak replies (local voice)" switch in
the Chainlit settings panel. When on, each reply is synthesized to a WAV and
attached as an inline, auto-playing audio element under the message.

**Engine:** `kokoro-onnx` (Kokoro v1.0 voice on onnxruntime) — natural English,
**faster than real-time on CPU** (RTF ~0.1–0.35 measured), **no torch**, and a
SEPARATE onnxruntime session so it never competes for the main LLM's VRAM (this
is exactly why voice is safe where the translation-pivot wrapper idea was not).
Default voice `am_michael` (US male; `af_heart`/`bm_george`/etc. selectable via
`ZERO_AGENT_TTS_VOICE`). Model files (`kokoro-v1.0.onnx` + `voices-v1.0.bin`,
~340 MB) live under `data/tts/` — a one-time download, 100% local thereafter.

**Design:**

- Opt-in (`TTS_ENABLED` default 0; the switch is the live control) and fully
  graceful — missing model files / import failure / synth hiccup just disables
  speech for that turn, never raises into the conversation.
- Engine loads lazily ONCE (~1 s) and is reused; load failure latches so a
  broken setup isn't retried every turn.
- `clean_for_speech` strips code blocks, URLs, markdown, emoji and the ⏱️/⏹️
  status badges, and trims long answers to a sentence boundary under
  `TTS_MAX_CHARS` (1200) so a wall of text isn't minutes of audio.
- Synthesis runs in a worker thread (`asyncio.to_thread`) so it never stalls the
  event loop; the text answer is shown first, audio attached a beat later.

This is Stage 1 of the voice plan (agent speaks). Stage 2 = speak TO it
(Whisper STT, push-to-talk); Stage 3 = wake word ("Hey Zero") / hands-free.

**Files:** `orchestrator/tts.py` (new), `orchestrator/config.py` (TTS settings),
`app.py` (Switch widget, settings handling, synth + `cl.Audio` attach),
`requirements.txt` (`kokoro-onnx`, `soundfile`).

---

### Added — Stop button (cancel a running turn) + malformed-tool-call fallback

**What:** Two stability features, no time/iteration cap added (heavy tasks may
run as long as needed — stopping is manual):

1. **⏹ Stop button** (`app.py`). The agent turn now runs as a cancellable
   `asyncio.Task`; `@cl.on_stop` cancels it when the user taps Chainlit's stop
   button. Cancellation propagates at the next `await` (Ollama stream / a tool),
   so a long or runaway turn can be aborted on demand. on_message catches the
   `CancelledError`, shows what was produced so far + a "⏹️ Stopped after Ns"
   note, and appends an `(stopped by user)` assistant turn so history stays
   well-formed for the next message. (`CancelledError` is BaseException-only, so
   the agent's `except Exception` safety nets don't swallow it.)
2. **Malformed-tool-call fallback** (`ollama_client.py`, `base_agent.py`). When
   the model emits a broken NATIVE tool call (Ollama 500 — `qwen3-coder`'s
   truncated JSON), the client now raises a distinct `OllamaToolCallError`
   immediately instead of burning retries on a deterministic failure, and
   `_stream_turn` retries that turn ONCE with tools disabled. In text mode the
   model writes its tool call as text, which `_parse_text_tool_calls` recovers —
   turning a hard crash (the World Cup thread's 386 s → 500) into a graceful
   continuation. Nothing is streamed before the error fires, so no duplication.

**Why:** Directly addresses the user's #1 recurring pain (a turn spiralling /
freezing with no way to stop it) and the `qwen3-coder-abliterated` tool-call
crash class — while preserving the ability to run long when genuinely needed.

**Files:** `app.py` (cancellable task, `@cl.on_stop`, CancelledError handling),
`orchestrator/models/ollama_client.py` (`OllamaToolCallError`,
`_is_tool_call_error`, no-retry on tool-call 500),
`orchestrator/agents/base_agent.py` (`_stream_turn` fallback → `_consume_stream`).

---

### Changed — Image/vision + effort guidance in the system prompt

**What:** Extended the "Image generation" block of `DEFAULT_SYSTEM_PROMPT`
(`base_agent.py`) with three rules: (1) image models (SDXL/FLUX) cannot render
readable text/tables/charts/data — present data as a markdown table, never as a
generated image; (2) if `analyze_image` verification doesn't match the request,
don't claim success — say what came out and regenerate or explain; (3) match
effort to the request — a greeting/thanks gets a short reply with NO tools.

**Why:** From the 2026-06-15 World Cup thread review: the agent generated an
image of a *standings table* (which SDXL renders as gibberish), `analyze_image`
correctly caught it was wrong, but the agent ignored that and moved on. And on a
trivial "i love the picture :)" it launched a 6.5-min tool spree (re-research +
rewrite files) that then crashed on a malformed `qwen3-coder-abliterated` tool
call. These rules close both gaps at the prompt level (low-risk) — the uncensored
persona inherits them too (preamble + full default).

**Files:** `orchestrator/agents/base_agent.py`.

---

### Fixed — Resuming a saved chat: wrong model + context pollution

**What:** Two defects in `on_chat_resume` (`app.py`):
1. It always reset the model to `DEFAULT_MODEL`, so reopening a thread that had
   been switched to (say) `qwen3:32b` silently answered with the default model.
   Now the thread's actual model is detected from its banner / "model switched"
   notices and restored.
2. It restored EVERY assistant message into history — including the welcome
   banner (with its giant tool list), "✅ model switched" confirmations, and
   project/memory notices — polluting the model's context. These UI notices are
   now filtered out, and the display-only `⏱️ Ns` response-time badge is stripped
   from restored answers.

**Why:** Investigating a report of "I reopen a chat, ask something, and get the
same answer back very fast." Root cause is that on resume the full prior answer
is restored into context, so a similar follow-up makes the model re-emit what is
already in context instead of re-running its tools (fast + near-identical). The
banner/notice pollution and wrong-model fallback made this worse and less
predictable. Filtering notices + restoring the right model makes resumed chats
behave like the live session. (A separate finding: `qwen3-coder-abliterated`
emitted invalid JSON tool-call arguments for `run_comfyui_workflow` on a trivial
"thanks!" turn, 500-ing after ~6 min — tracked as a model-reliability note, see
ROADMAP.)

**Files:** `app.py` (`import re`; `_is_ui_notice` / `_model_from_notice` /
`_strip_time_badge` helpers; rewritten resume loop).

---

### Added — Telegram chat history persists to disk (survives bot restarts)

**What:** New `orchestrator/telegram_history.py`. Each Telegram chat's rolling
history is now mirrored to a small per-chat JSON file
(`data/telegram/chat_<id>.json`); on the next message after a restart the bot
restores that history (and the last-used model) instead of starting cold. This
closes the last cross-front-end gap — Chainlit already persisted threads to
SQLite, Telegram was RAM-only.

**Why:** the #1 remaining ROADMAP item. A bot restart (or crash) used to wipe
every Telegram conversation; long sessions and any later review were impossible.
Prompted by a real session the user had in Telegram that couldn't be inspected
afterwards because nothing was saved.

**Design:**

- One JSON file per chat, **atomic writes** (temp file + `os.replace`) so a
  crash mid-write can't corrupt it; unreadable files are logged and skipped
  (start fresh) — persistence is never a correctness gate.
- **Only conversation turns are stored**, not the transient system blocks
  (persona / memory / lessons) that `BaseAgent.run` re-injects every turn. The
  `[[ZERO_SUMMARY]]` block IS kept (it carries compressed older context).
- Hard backstop cap `TELEGRAM_HISTORY_MAX_MSGS` (default 200) on top of the
  rolling summarizer.
- Saved after every turn, on model switch (button or typed), and **deleted by
  `/reset`**. Distinct from the long-term ChromaDB memory, which is separate and
  already persistent.

**Config:** `ZERO_AGENT_TELEGRAM_PERSIST_HISTORY` (default 1),
`ZERO_AGENT_TELEGRAM_HISTORY_DIR`, `ZERO_AGENT_TELEGRAM_HISTORY_MAX_MSGS`.

**Files:** `orchestrator/telegram_history.py` (new), `orchestrator/config.py`
(3 settings), `telegram_bot.py` (load on session create, save after turn /
model switch, clear on `/reset`).

---

## 2026-06-14

### Added — Rolling conversation summary (no more silent context truncation)

**What:** New `orchestrator/summarizer.py`. When the running history grows past
~60 % of the context budget, the OLDEST turns are compressed into a single
`[[ZERO_SUMMARY]]` system message (via the small distiller) and the most recent
turns are kept verbatim — so Ollama no longer silently drops the earliest
context on long chats. Wired into both front-ends (`app.py`, `telegram_bot.py`)
before each turn.

**Why:** the top item from the context/memory roadmap, and more important now
that `num_ctx` was lowered to 16384 (VRAM safety) — long chats hit the window
sooner. Gives a "big context" feel without a giant-context model.

**Design:**
- Trigger: char-based heuristic (`num_ctx * 4 * 0.6`), no tokenizer dependency.
- Keeps all system blocks (persona / memory / lessons), folds any PREVIOUS
  summary into the new one (so it doesn't stack), keeps the last 6 conversation
  messages verbatim. The summary starts with `[[` so `BaseAgent.run` preserves
  it across turns like the memory/lessons blocks.
- Compresses the in-memory model context only — the UI's saved SQLite transcript
  (sidebar) is untouched.
- Uses the existing distiller model; never raises (a hiccup leaves history as-is).

**Files:** `orchestrator/summarizer.py` *(new)*, `app.py`, `telegram_bot.py`.

**Verified locally:** `py_compile` + import OK; a 62-message history compressed to
9 (54 old → 1 summary, system blocks kept, 6 recent verbatim); a short history is
left unchanged.

**To activate:** restart the UI / bot.

### Audit — base_agent + system files reviewed; model-call errors now learned

**Audit:** read `base_agent.py` end-to-end (after the day's many edits: personas,
NO_TOOLS notice/footer, grounding net, text-embedded tool-call parsing,
self-verify, failure capture) — logic is consistent, no internal bug found.
Compiled all 26 modules (0 failures), full stack imports, 27 tools, config values
correct (NUM_CTX=16384, SELF_VERIFY on). Live end-to-end smoke test (hermes3:8b):
tool fires → self-verify → finalize → clean answer, no crash.

**Changed (coverage gap closed):** `BaseAgent.run` now records model-call errors
(`OllamaError`) and unexpected crashes into `last_run_failures`, so the lessons
layer learns from them too (e.g. "a model whose template 400s with tools →
switch model"). Previously only failed tool results + the iteration cap were
captured.

**Decision (NOT done, on purpose):** considered moving the self-verify reviewer
to a small model (qwen3:4b) for speed — but with `OLLAMA_MAX_LOADED_MODELS=1`
(the freeze fix) that would EVICT + reload the big chat model mid-response
(slow). Using the already-loaded chat model as the reviewer is correct now; left
as-is.

**Files:** `orchestrator/agents/base_agent.py`.

**Verified locally:** `py_compile` clean; a run against a non-existent model
returns the friendly message AND records `model call failed: …` in
`last_run_failures` (no crash).

### Fixed — GPU/VRAM overflow froze the whole machine (multi-model + 32k ctx)

**What:** Two changes to stop the machine freezing when a task runs:
- Set Ollama env (user scope): `OLLAMA_MAX_LOADED_MODELS=1`,
  `OLLAMA_NUM_PARALLEL=1` — Ollama no longer keeps multiple models resident at
  once.
- Lowered `ZERO_AGENT_NUM_CTX` 32768 → **16384** (config default + all 4
  launchers).

**Why:** live diagnosis with `ollama ps` + `nvidia-smi` caught it: `qwen3.6-
uncensored` (28 GB) loaded at a 32768 context used **32005/32607 MiB (98 %)** of
the 32 GB card, 99 % util. With the card that full, the fire-and-forget
background distillers (auto-memory + the new lessons layer, `qwen3:4b`) tried to
load a SECOND model on top → VRAM overflow → spill to RAM/swap → the entire
machine froze. `MAX_LOADED_MODELS=1` stops the co-load (the freeze trigger);
16384 ctx gives the single big model real headroom (a 28 GB model + 32k KV had
≈0 spare). Reverts the earlier "raise ctx to 32768" change, which is unsafe for
the 28 GB models on a 32 GB card.

**Files:** Ollama user env (`setx`); `orchestrator/config.py`; `START_ALL.bat`,
`start_zero_agent.bat`, `START_TELEGRAM_ALL.bat`, `start_telegram.bat`.

**Verified locally:** env vars read back as `1`/`1`; all launchers + config now
say 16384.

**To activate:** **restart Ollama** (quit from the tray + reopen, or reboot) so
it reads the new env vars, then restart the UI/bot. The currently-stuck model
frees on Ollama restart (or after its keep-alive expires).

**Guidance:** for heavy/agentic work prefer a model that fits with headroom —
`qwen3:32b` (20 GB) or `qwen3-coder-abliterated` (25 GB) — over `qwen3.6` (28 GB),
which is the tightest on a 32 GB card.

### Added — Video generation button in the UI (🎬 Video → background ComfyUI job)

**What:** A `Video` composer command (next to Research/Image/Remember) in the
Chainlit UI. Tapping it + a prompt routes the agent to `submit_comfyui_job` with
the configured video workflow (video is slow → background job + `check_job`).
New config `DEFAULT_VIDEO_WORKFLOW` (env `ZERO_AGENT_VIDEO_WORKFLOW`, default
`C:\AI-MEDIA-RTX5090\zero_agent_ltx_api.json`).

**Why:** the user asked for a video-creation feature in the interface (LTX/Wan).

**⚠️ Dependency (blocker, currently unmet):** ComfyUI's `/prompt` API and the
agent's tools accept ONLY **API-format** workflows. The user's existing video
workflows (`ltx2_workflow.json`, `wan2.2_t2v_high_noise_quality.json`,
`wan_i2v_lightx2v_4step.json`) are all **UI-format** → rejected. To activate the
button (and to test LTX at all): open the workflow in ComfyUI →
**Save (API Format)** → save as the `DEFAULT_VIDEO_WORKFLOW` path (or point the
env var at it). This was also the real 3rd reason the "test LTX" task couldn't
run (on top of the qwen3.6 template 400).

**Files:** `app.py` (`CHAT_COMMANDS` + `Video` routing in `on_message`),
`orchestrator/config.py` (`DEFAULT_VIDEO_WORKFLOW`).

**Verified locally:** compile + import OK; Video button present; config resolves.
The default workflow file does NOT exist yet (needs the API-format export above).

**To activate:** restart the UI + provide an API-format video workflow.

### Added — Learn-layer: Telegram wiring + phase 3 (`/lessons` viewer)

**What:**
- **Telegram wiring** — `telegram_bot.py` now does the same lessons recall
  (before a turn) + distill-on-failure (after) as `app.py`. Self-verification was
  already automatic for Telegram (it lives inside `BaseAgent.run`), so the full
  learning layer (lessons + verify) now runs on BOTH front-ends.
- **Phase 3 — transparency** — a `/lessons` command (UI + Telegram) lists
  everything the agent has learned, so the user can SEE the accumulated friction
  and act on it (fix/add a tool). This is the human-in-the-loop form of
  "tool-improvement", not the agent editing its own code.

**Design:**
- `MemoryStore.list_by_tag(tag, limit)` — non-semantic `collection.get(where=…)`
  to fetch all memories of a kind (e.g. `lesson`), newest first.
- `lessons.list_lessons(store)` formats them; `/lessons` returns it and never
  reaches the model.

**Files:** `telegram_bot.py` (lessons wiring + `/lessons`),
`app.py` (`/lessons`), `orchestrator/lessons.py` (`list_lessons`),
`orchestrator/memory/store.py` (`list_by_tag`).

**Verified locally:** `py_compile` + import OK for both front-ends;
`list_lessons` renders a numbered list and the empty case.

**Learning layer status:** phase 1 (lessons) ✅, phase 2 (self-verification) ✅,
phase 3 (transparency `/lessons`) ✅ — all on UI + Telegram. Possible future:
auto-detect tool GAPS (not just failures) and suggest specific new tools.

**To activate:** restart the UI / bot.

### Added — Self-verification (learn-layer phase 2): catch "claimed done, actually broken"

**What:** After the agent finishes an **action** turn (one that used ≥1 tool), a
strict reviewer judges whether the task was actually accomplished/verified or
merely claimed done. If it looks unverified, the agent gets **one corrective
iteration** to actually test + fix before the answer is returned.

**Why:** the agent's biggest weakness, seen repeatedly today — it reports
"success:true" / "it works" without checking the real output (the translation
server answered HTTP 200 while returning Arabic/echo/empty). This is the
Reflexion "evaluate → reflect → retry" step; it directly targets the gap that
stops Zero from reliably FINISHING a system (vs claiming it finished).

**Design:**
- `orchestrator/verify.py` *(new)*: `verify_answer(client, user, answer, tools)`
  → `(ok, critique)`. A strict-QA prompt flags claims-without-testing. Never
  raises (verifier failure ⇒ OK), so it can't stall a reply.
- `BaseAgent._verify_and_maybe_fix`: runs ONLY when `span.tools` is non-empty
  (plain chat is untouched). On FAIL it appends a directive ("don't just claim
  it works — actually test the real result and fix it") and runs the loop ONE
  more time. Bounded: no re-verification after the corrective pass (no loop).
- `config.SELF_VERIFY` (default on; `ZERO_AGENT_SELF_VERIFY=0` to disable). Uses
  the active chat model as the reviewer (self-critique).

**Files:** `orchestrator/verify.py` *(new)*,
`orchestrator/agents/base_agent.py` (wired in `run()`), `orchestrator/config.py`.

**Verified locally:** `py_compile` + `app` import OK; `verify_answer` returns
(True,"") on "OK" and (False, critique) on "FAIL: …"; `_verify_and_maybe_fix`
returns the answer unchanged on a no-tool (chat) turn.

**Caveats / honest limits:** same-model self-critique (a model sure it succeeded
may also judge itself OK — but the reviewer prompt is adversarial and catches a
lot); only ONE corrective pass; adds one short model call on tool-using turns.
Phase 3 (tool-improvement suggestions to the user) still pending.

**To activate:** restart the UI.

### Added — Learn-from-mistakes layer (Reflexion-style lessons memory) — phase 1

**What:** The agent now learns from its own failures. New module
`orchestrator/lessons.py` (sibling of `auto_memory.py`):
- After a turn that hit failures, a small model distills 1-2 GENERAL, reusable
  lessons ("what went wrong -> what to do instead") and stores them in the
  existing ChromaDB store, tagged `lesson`.
- Before each turn, the most relevant past lessons are recalled and injected as
  a `[[ZERO_LESSONS]]` system block so the agent avoids repeating the mistake.

**Why:** first step of the user's "self-improving autonomous agent" goal. This is
the realistic local form — retrieval over past mistakes, NOT live fine-tuning.
Every bug we hit today (server via terminal, whole-repo download, coder narrating)
is exactly the kind of lesson this captures so it isn't repeated.

**Design:**
- `BaseAgent` now records `last_run_failures` each run (failed tool results +
  iteration-cap), read by the front-end to feed `distill_lessons`.
- `lessons.recall_lessons` over-fetches then keeps only `lesson`-tagged hits at/
  above the recall threshold, so lessons aren't crowded out by ordinary facts.
- `lessons.inject_lessons_message` mirrors `auto_memory.inject_memory_message`
  (single, refreshed, never duplicated block).
- Wired into `app.py` (recall before the turn; distill fire-and-forget after, only
  when `last_run_failures` is non-empty). Reuses the auto-memory distiller + store.

**Files:** `orchestrator/lessons.py` *(new)*, `orchestrator/agents/base_agent.py`
(failure capture), `app.py` (wiring).

**Verified locally:** `py_compile` + full `app` import OK; `inject_lessons_message`
inserts after the persona and refreshes without duplicating; `recall_lessons`
keeps only above-threshold lesson-tagged memories (excludes plain facts + a
below-threshold lesson) and frames them correctly.

**Status / next:** wired into the Chainlit UI. **Telegram wiring is the same
pattern, pending.** Future phases (from the plan): (2) self-verification step
(check the ACTUAL output, not just "success:true"), (3) tool-improvement
suggestions surfaced to the user (not auto-editing code).

**To activate:** restart the UI.

### Fixed — qwen3-coder tool calls not parsed (model narrated, looped) — REAL root cause

**What:** `_stream_turn` now parses tool calls a model wrote as **text** and
executes them, and suppresses streaming that raw markup to the UI.

**Why:** the "narrates instead of acting / loops" bug had a deeper cause than the
persona. An empirical probe (`OllamaClient.chat` with tools) showed
`qwen3-coder-abliterated` returns **0 structured `tool_calls`** while its content
is its NATIVE format:
`<function=write_file><parameter=filepath>…</parameter><parameter=content>…</parameter></function>`.
Ollama's GGUF chat template for this model doesn't translate that into the
`tool_calls` field, so the agent saw a plain text answer → never executed →
"fast reply, nothing happens", and on retries it looped. (Qwen3-Coder uses an
XML tool-call format; this is a known Ollama-template gap.)

**Design:**
- `_parse_text_tool_calls()` extracts both the qwen-coder `<function=…>` XML and
  the Hermes/JSON `<tool_call>{…}</tool_call>` forms into the loop's tool-call
  shape. Used in `_stream_turn` only when no structured `tool_calls` arrived.
- Buffered streaming: the first tokens are held until we can tell if the turn is
  a text-embedded tool call; if so, the raw markup is NOT streamed to the UI
  (otherwise normal answers stream live as before).

**Files:** `orchestrator/agents/base_agent.py`.

**Verified locally:** `py_compile` clean; the parser handles the exact XML the
model produced + the JSON form + a normal answer (→ none). **End-to-end:** an
agent run with `qwen3-coder-abliterated` now FIRES `write_file` and actually
creates the file (content "HELLO"), with a clean final answer (no XML leak) —
where before it only narrated.

**To activate:** watch-mode reload; re-run the task.

### Fixed — Uncensored persona broke agentic tool use (model narrated, didn't act)

**What:** `UNCENSORED_SYSTEM_PROMPT` is now an uncensored **preamble prepended to
the full `DEFAULT_SYSTEM_PROMPT`**, instead of a short standalone persona that
REPLACED it.

**Why:** observed live — `qwen3-coder-abliterated` started responding to "fix
it / try again" with text plans ("Let me retry creating…", "let me create an
improved version…") and **no tool calls**, so nothing happened ("fast reply,
system still down"). Root cause was the previous change: mapping `abliterated` →
the short uncensored persona stripped ALL the agentic guidance ("call the
appropriate tool rather than guessing", `launch_program` for servers, etc.) that
lives in `DEFAULT_SYSTEM_PROMPT`. A tool-capable model under the bare persona
narrates instead of executing. The preamble approach keeps every tool
instruction AND the uncensored stance, and explicitly says "actually DO the task
by calling the tools; never just describe what you would do."

**Impact:** applies to all uncensored models (`erotic`/`abliterated`/
`supergemma`). Dolphin keeps its own short persona (it's tool-less anyway).

**Files:** `orchestrator/agents/base_agent.py`.

**Verified locally:** `py_compile` clean; the coder's resolved prompt now
contains the uncensored framing + `call the appropriate tool` + `launch_program`
+ the "never just describe" rule (len 4664).

**To activate:** watch-mode reload picks it up; re-run the task.

### Fixed — execute_terminal_command now refuses server launches (hard guard)

**What:** `execute_terminal_command` rejects commands that start a long-running
server/process (`uvicorn`, `gunicorn`, `flask run`, `streamlit/chainlit run`,
`npm/yarn/pnpm run dev|start`, `vite/next/nuxt dev`, `python -m http.server`,
`manage.py runserver`, `ollama serve`, and `python …{server,app,main,api}*.py`).
Instead of running it, it returns an error telling the model to use
`launch_program` (detached, returns immediately), with a ready-to-use suggestion.

**Why:** observed live — the agent kept launching its translation server with
`execute_terminal_command`, which has a 20 s wall-clock limit. Each call blocked
the whole agent for 20 s (GPU idle, looked "stuck thinking"), then the timeout
killed the server, leaving orphaned processes. The system prompt already advised
`launch_program`, but the model forgot under load. This converts that soft
advice into a hard code guard the model cannot bypass — so it can never happen
again.

**Design:** new `_SERVER_PATTERNS` in `system_tools.py`, checked right after
`_DANGEROUS_PATTERNS`. `launch_program` is intentionally NOT subject to it (it
is the correct tool for servers). Patterns are specific (e.g. `python script.py`
is allowed; only `…server/app/main/api….py` is caught) to avoid false positives.

**Files:** `orchestrator/tools/system_tools.py`.

**Verified locally:** `py_compile` clean; 6/6 server commands blocked with the
launch_program hint, 5/5 normal commands (`pip list`, `python --version`,
`python script.py`, …) still run.

**To activate:** the UI's watch-mode reload picks it up immediately.

### Removed — dolphin-llama3:70b (tool-less, replaced by the coder)

**What:** `ollama rm dolphin-llama3:70b` and removed it from `MODEL_OPTIONS`
(UI + Telegram). It was redundant once `qwen3-coder-abliterated` (uncensored,
tool-capable) landed — dolphin is tool-LESS, Llama-3-era, 39 GB → CPU-offloaded.

**Note (shared blob):** deleting `dolphin-llama3:70b` alone freed ~0 GB because
it **shared its 40 GB weight blob** with `dolphin-unleashed` (built `FROM` the
same blob). After the user confirmed, `dolphin-unleashed` was also removed
(`ollama rm`) — which released the shared blob and freed **~40 GB** (disk 345 →
385 GB). Both dolphin models are now gone and removed from `MODEL_OPTIONS`.

**Remaining models:** `qwen3-coder-abliterated` (uncensored coder),
`supergemma4-uncensored`, `qwen3.6-uncensored`, `qwen3:32b`, `hermes3:8b`
(default), `qwen3:4b`, `gemma3:4b`, `nomic-embed-text`.

**Files:** `app.py`, `telegram_bot.py`. (The `dolphin` persona + `_NO_TOOL_MODELS`
entries are kept — harmless and ready if a dolphin model is re-added; `gemma`
still uses `_NO_TOOL_MODELS`.)

### Added — Uncensored coding model (Huihui-Qwen3-Coder abliterated) + download retry

**What:**
- Hardened `download_hf_model`: the download subprocess now retries up to 12×
  on a dropped stream (`hf_hub_download` resumes from the partial each time), so
  large downloads survive transient `RemoteProtocolError` disconnects.
- Added `Huihui-Qwen3-Coder-30B-A3B-Instruct-abliterated.i1-Q6_K.gguf` (25.1 GB,
  verified exact byte match + GGUF header) as the project's **uncensored coding
  model** (a hard requirement for the user's university project). Registered with
  Ollama and **named `qwen3-coder-abliterated`** so it auto-gets the
  `UNCENSORED_SYSTEM_PROMPT` persona (the name must contain `abliterated`, not
  just `uncensored`). Capabilities: **tools + completion** (no thinking — it's a
  pure Coder-Instruct, so no thinking/refusal-reassertion issue). Added to
  `MODEL_OPTIONS` (UI + Telegram). Verified: in both lists, persona resolves to
  UNCENSORED, `_model_supports_tools()` True (full coding agent).

**Why:** Qwen3-Coder-30B-A3B is the newest model purpose-built for agentic
coding + tool use, MoE (≈3 B active → fast), and Huihui's abliteration makes it
uncensored. **Q6_K** chosen deliberately — at Q4 the abliteration partially
returns (seen with qwen3-erotic), so a higher quant preserves the "uncensored"
requirement. Intended to replace `dolphin-llama3:70b` for coding (dolphin is
tool-LESS, Llama-3-era, and 39 GB → CPU-offloaded/slow — a poor coding agent).

The first attempt failed at ~3 GB (`peer closed connection`), which is what
motivated the retry hardening above.

**Files:** `orchestrator/tools/model_tools.py`.

**Verified locally:** `py_compile` clean; 27 tools register; download resumes
under the retry loop after a drop.

### Added — supergemma4-uncensored (Gemma-4 26B MoE) as the uncensored model

**What:** Downloaded `supergemma4-26b-uncensored-fast-v2-Q4_K_M.gguf` (16.8 GB,
verified exact byte match + GGUF header), registered as `supergemma4-uncensored`
(tools + thinking + completion), added to `MODEL_OPTIONS` (UI + Telegram), and
mapped `supergemma` → `UNCENSORED_SYSTEM_PROMPT` so it runs candid by default.

**Why:** replaces the weak qwen3-erotic. Gemma-4 26B-A4B (MoE, ~4 B active) is
coherent + fast, fits easily (16.8 GB), tool-capable, and the most-vetted of the
candidates the user compared (818 likes / ~99 k downloads). Downloaded +
registered via the new `download_hf_model` / `register_gguf_model` tools.

**Files:** `app.py`, `telegram_bot.py`, `orchestrator/agents/base_agent.py`.

**To activate:** restart the UI / bot, then select `supergemma4-uncensored`.

### Removed — qwen3-erotic (weak abliteration; freed ~34 GB)

**What:** Deleted `qwen3-erotic` from Ollama (`ollama rm`) and its 18.6 GB GGUF
folder, and removed it from `MODEL_OPTIONS` (UI + Telegram).

**Why:** behavioral testing showed it was actually MORE restricted than the
already-installed `qwen3.6-uncensored` — it softened an NSFW image prompt and
hard-refused a dual-use code request, while `qwen3.6-uncensored` complied. The
Ewere abliterated-erotic fine-tune is weak and Q4 brought refusals back; not
worth the disk. Replaced by `supergemma4-26b-uncensored` (downloading). The
`erotic`/`abliterated` persona + thinking-off rules are kept for future models.

**Files:** `app.py`, `telegram_bot.py`.

### Changed — Force thinking OFF for abliterated/erotic models

**What:** `OllamaClient._maybe_think` now sends `think=False` for models whose
name contains `erotic`/`abliterated` (`_FORCE_NO_THINK`), even though they
support thinking.

**Why:** the user found `qwen3-erotic` still acting censored. An abliterated
model's base reasoning channel tends to re-assert caution and talk itself into
softening/refusing; running it in direct (non-thinking) mode keeps it on-target.
Pairs with the `UNCENSORED_SYSTEM_PROMPT` persona change below.

**Files:** `orchestrator/models/ollama_client.py`.

**Verified locally:** `qwen3-erotic` → `think=False`; `qwen3:32b` → `think=True`
(unchanged); non-thinking models unaffected.

**Remaining lever (not done):** Q4_K_M quantization can itself partially undo
abliteration (the abliteration direction gets noisy at low bits) — if it's still
too restricted, a higher quant (Q6_K ≈ 25 GB, or Q5_K_M ≈ 21.7 GB) usually
behaves closer to what the model is known for. Downloadable via `download_hf_model`.

### Fixed — Abliterated models still refused (generic prompt steered them back)

**What:** Added a general `UNCENSORED_SYSTEM_PROMPT` and mapped model names
containing `erotic` or `abliterated` to it in `_MODEL_SYSTEM_PROMPTS`. So
`qwen3-erotic` (and any abliterated model) now gets a direct, no-refusal persona
instead of the generic "elite assistant" prompt.

**Why:** the user tested `qwen3-erotic` and found it still restricted. Same
lesson already documented for dolphin: the generic agent prompt steers a model
into polite-assistant behavior, so even an abliterated model drifts back toward
refusals/hedging. The Modelfile's own SYSTEM can't fix this because the agent
overrides the system prompt every run — so the per-model persona override is the
correct layer. (Targeted to `erotic`/`abliterated` only; `qwen3.6-uncensored`
and others are unchanged.)

**Design:** `UNCENSORED_SYSTEM_PROMPT` is a model-agnostic candid persona (local,
private, adult professional, no moralizing/refusing) — it removes the
assistant-style refusal steering; it is not a recipe for specific content, and
the model's own training does the rest. These models are tool-capable, so the
tool/grounding rules still apply.

**Files:** `orchestrator/agents/base_agent.py`.

**Verified locally:** `py_compile` clean; persona resolver maps `qwen3-erotic` →
UNCENSORED, `*abliterated*` → UNCENSORED, dolphin → DOLPHIN (unchanged),
`qwen3.6-uncensored`/`qwen3:32b`/`hermes3:8b` → DEFAULT (unchanged).

**To activate:** restart the UI / bot, then re-select `qwen3-erotic`.

**Fallback if still restricted:** this model also has a "thinking" channel
(`ENABLE_THINKING` on by default); if its reasoning still talks it into
hedging, try `ZERO_AGENT_THINK=0` (or a per-model thinking-off rule).

### Added — qwen3-erotic model (downloaded + registered via the new tools)

**What:** Downloaded `Qwen3-30B-A3B-abliterated-erotic.i1-Q4_K_M.gguf` (18.56 GB,
verified: exact byte match vs the HF API, valid GGUF header), registered it with
Ollama as `qwen3-erotic` via the new `register_gguf_model` tool, and added it to
the model picker (UI + Telegram).

**Why:** completes the download→use flow end-to-end and dogfoods the new model
tools (`download_hf_model` → `register_gguf_model`).

**Note:** Ollama imported it as `qwen3moe` and it advertises **tools + thinking +
completion**, so it runs as a full tool-capable agent model (gets the grounding
net, not the tool-less notice). Modelfile at the gguf's folder, `num_ctx 32768`.

**Files:** `app.py`, `telegram_bot.py` (added `qwen3-erotic` to `MODEL_OPTIONS`).

**To activate:** restart the UI / bot.

### Added — 11 new tools: model lifecycle, machine self-awareness, files, media, PDF

**What:** The agent went from 16 → **27 tools**, grouped around the role that
emerged in use — a local AI-ops manager. New tools:

- **Model lifecycle** (`orchestrator/tools/model_tools.py`, new):
  - `download_hf_model(repo_id, filename, dest_dir?)` — downloads exactly ONE
    file as a **cancellable background subprocess** (xet disabled for reliable
    HTTPS, no event-loop block, no 20 s terminal cap). Looks up the expected
    size for progress %. Directly fixes the failed download (whole-repo via the
    20 s terminal tool).
  - `register_gguf_model(gguf_path, model_name, num_ctx?, system?)` — writes a
    Modelfile and runs `ollama create` so a downloaded `.gguf` is usable.
  - `list_ollama_models()` — what's installed.
- **Machine self-awareness + jobs** (`orchestrator/tools/ops_tools.py`, new):
  - `system_resources()` — GPU VRAM (`nvidia-smi`), RAM (Win32 API, no psutil),
    disk free. So the agent checks space/VRAM BEFORE heavy actions.
  - `manage_jobs(action, job_id?)` — list all background jobs (downloads +
    ComfyUI) with progress; cancel a running download. (We had to kill a runaway
    download by hand — now the agent can.)
  - `move_path`, `copy_path`, `delete_path` — basic file ops (drive-root guard).
- **Media** (added to `media_tools.py`):
  - `list_media_assets()` — scan the local ComfyUI install for
    checkpoints/LoRAs/VAEs/diffusion_models + `*_api.json` workflows.
  - `analyze_image(image_path, question?, model?)` — describe/answer about an
    image via a local vision model (default `gemma3:4b`).
- **Docs** (added to `system_tools.py`): `read_pdf(filepath, max_chars?, pages?)`
  via `pypdf` (guarded import).

**Why:** the model-download saga showed the agent improvising with the wrong
tools because it had no right ones and no view of the machine (disk/VRAM/jobs).
These encapsulate the correct behavior and give it self-awareness. Consistent
with the roadmap: build capabilities INSIDE this architecture, no new framework.

**Design notes:**
- Downloads run as subprocesses (not threads) specifically so `manage_jobs` can
  cancel them by PID. Progress is read from the `.incomplete` file vs the
  expected size.
- `manage_jobs` lazily imports the ComfyUI `_JOBS` and the downloads registry to
  avoid an import cycle.
- `system_resources` is stdlib + `nvidia-smi` only — no psutil dependency
  (local-first). RAM via `GlobalMemoryStatusEx` (ctypes).
- The system prompt got a concise "Models & machine" block so the agent reaches
  for these (e.g. `download_hf_model`, not the terminal; `system_resources`
  before a big download).

**Files:** `orchestrator/tools/model_tools.py` *(new)*,
`orchestrator/tools/ops_tools.py` *(new)*, `orchestrator/tools/media_tools.py`,
`orchestrator/tools/system_tools.py`, `orchestrator/tools/__init__.py`,
`orchestrator/agents/base_agent.py` (prompt), `requirements.txt` (pypdf).

**Verified locally:** `py_compile` clean on all modules; registry lists 27 tools
with all 11 new ones present; live runs — `system_resources` (GPU 27 GB free /
disk 476 GB free), `list_ollama_models`, `list_media_assets` (real
checkpoints), `copy_path`/`move_path`/`delete_path` (+ drive-root guard
rejects `C:\`), `read_pdf` (blank PDF handled), and `analyze_image` correctly
described an astronaut-cat PNG via `gemma3:4b`.

**To activate:** restart the UI / bot.

## 2026-06-13

### Fixed — Copy button never appeared (wrong Chainlit selector)

**What:** The copy button targeted `.markdown-body`, which does NOT exist in the
Chainlit 2.11.1 frontend build, so no button was ever added. Switched to the
stable `[data-step-type="assistant_message"]` attribute (verified present in the
shipped frontend) via a `MSG_SELECTOR` constant.

**Why:** the user reported the button still wasn't showing. Grepping the
installed `chainlit/frontend/dist` confirmed `markdown-body` is absent and each
message step renders as `<div data-step-type="…" class="step py-2">`.

**Files:** `public/copy-button.js`.

**Verified locally:** `node --check` passes; selector replaced.

**To activate:** restart the UI **and hard-refresh the browser tab
(Ctrl+Shift+R)** — `custom_js` is loaded at page load, so an already-open tab
keeps the old script after a watch-mode reload.

### Changed — Default model qwen3:4b → hermes3:8b (tool-capable by default)

**What:** All four launchers now default `ZERO_AGENT_MODEL` to `hermes3:8b`
instead of `qwen3:4b`, so a fresh start uses a tool-capable model out of the box.

**Why:** the previous default (`qwen3:4b`) is fast but weak and answered factual
questions from memory — the source of the fabricated World Cup output. `hermes3:8b`
is still fast on the 5090 but is tool-capable and candid, a better all-round
default for an agent whose value is grounded answers.

**Files:** `START_ALL.bat`, `start_zero_agent.bat`, `START_TELEGRAM_ALL.bat`,
`start_telegram.bat` (still overridable via the env var / UI picker).

**Verified locally:** with `ZERO_AGENT_MODEL=hermes3:8b`, `config.DEFAULT_MODEL`,
`app.DEFAULT_MODEL` and `config.TELEGRAM_MODEL` all resolve to `hermes3:8b`; no
`qwen3:4b` default remains in any launcher.

**Important follow-up (observed in logs):** even Hermes answered "what games are
on today in the World Cup?" with `tool_calls=0` — it did NOT search, it answered
from memory. The bilingual grounding net now appends the "I didn't look this up"
warning to such answers, but to actually GROUND the answer the user should use
the 🔭 Research action (forces `deep_research`). A prompt change to push
tool-capable models to auto-search current-events questions is a candidate next
step (kept out for now to avoid over-triggering searches on every turn).

### Fixed — Grounding net missed Hebrew answers + Added response-time & copy button

**What:** Three changes after the user hit a fully fabricated **Hebrew** World
Cup answer ("two finals", invented matches, garbled text) that slipped past the
grounding net with no warning:
1. **Bilingual `_looks_factual`** — the heuristic only matched English signal
   words, so a Hebrew factual answer was never flagged. Added Hebrew signals
   (היום, מונדיאל, גמרים, ליגה, תוצאות, מחיר, מניה, מזג האוויר, חדשות, …).
   Hebrew has no left word-boundary (prefixes ב/ה/ל/ו/ש/כ/מ attach), so terms
   match as substrings, with trailing `\b` on short roots and explicit factual
   forms chosen to avoid false positives (מחר vs מחרוזת, היום vs היומי, and
   dropping גמר/תוצא which would hit לגמרי/כתוצאה — using גמרים/תוצאות instead).
2. **Response-time badge** — each UI answer now ends with `` `⏱️ 1.2s` ``,
   appended to the DISPLAYED message only (kept out of the distilled `answer`,
   so long-term memory stays clean).
3. **Per-message copy button** — a "📋 העתק" button on each rendered message,
   via a custom JS file using the Clipboard API (works on localhost = secure
   context). cl.Action callbacks run in Python and can't reach the clipboard, so
   this is done on the frontend.

**Why:** the Hebrew miss was a real correctness gap (the user works in Hebrew);
the two features were requested directly.

**Design:**
- `base_agent.py`: `_FACTUAL_SIGNALS` regex extended with a Hebrew alternation.
- `app.py`: `import time`; measure `time.perf_counter()` around `agent.run()` and
  append the badge to `answer_msg.content` only.
- `public/copy-button.js` *(new)* — MutationObserver adds a copy button to each
  `.markdown-body`; copies the text with our own button(s) stripped out.
- `.chainlit/config.toml`: `custom_js = "/public/copy-button.js"`.

**Files:** `orchestrator/agents/base_agent.py`, `app.py`,
`public/copy-button.js` *(new)*, `.chainlit/config.toml`.

**Verified locally:** `py_compile` clean; `_looks_factual` correct on 11 EN+HE
cases incl. the exact fabricated text (flagged) and tricky negatives
(מחרוזת / לגמרי / כתוצאה / יומן / code talk → not flagged); config parses with
`custom_js` set; `node --check` passes on the JS.

**To activate:** restart the UI. **Note:** the copy button targets Chainlit's
`.markdown-body` class — needs a quick visual check in the browser; if a future
Chainlit version renames that class, update the selector in `copy-button.js`.

**Caveat (model choice):** the grounding net only WARNS. The fabricated answer
also points to using `qwen3:4b` (weak default) for a factual query; for accurate
answers use `hermes3:8b` / `qwen3:32b` or the 🔭 Research action so the agent
actually searches instead of answering from memory.

### Fixed — Repeated "Translation file for he not found" log spam

**What:** Chainlit logged `Translation file for he not found. Using regional
variant he-IL.` on every UI action. Added `.chainlit/translations/he.json` (a
copy of the existing `he-IL.json`) so the bare `he` locale the browser requests
resolves directly and the warning stops.

**Why:** the browser sends locale `he`; only `he-IL.json` existed, so Chainlit
fell back (working, but noisy — one INFO line per action). Cosmetic only.

**Files:** `.chainlit/translations/he.json` *(new, = he-IL.json)*.

**Verified locally:** valid JSON; `he` now has its own file.

**To activate:** restart the UI (or let watch-mode reload).

### Added — Grounding net for tool-capable models (unverified-claim notice)

**What:** A tool-capable model (qwen3 family, hermes3) that answers a
current/factual-looking question **without calling any tool** now gets a soft,
deterministic notice appended: "I answered this without a web/tool lookup, so
any time-sensitive claim may be outdated — ask me to research it." It never
blocks or re-prompts, so there are no loops and no over-refusal — it just flags
that the claim wasn't looked up.

**Why:** ROADMAP item 3. The earlier fix handled tool-LESS models; this closes
the residual gap on tool-capable ones, which *can* search but may still answer a
factual question straight from training memory. Completes the anti-hallucination
arc: tool-less → refuse + warn; tool-capable but didn't look it up → warn.

**Design:**
- `base_agent.py`: `UNVERIFIED_NOTICE` + `_looks_factual()` (a conservative
  regex of current/factual signals — date markers, currency amounts,
  schedule/score/weather words; deliberately excludes generic "current" so
  ordinary coding answers are not flagged).
- New `_finalize_answer(answer, span, on_token)` centralizes both notices: it
  reads `span.tools` (empty ⇒ no tool was called this run) to decide. Tool-less
  ⇒ `NO_TOOLS_FOOTER`; tool-capable + no tool + looks factual ⇒
  `UNVERIFIED_NOTICE`; otherwise the answer is returned unchanged. The suffix is
  streamed via `on_token` (UI) and appended to the return value (Telegram), as
  before.

**Files:** `orchestrator/agents/base_agent.py` only.

**Verified locally:** `py_compile` clean; `_looks_factual` correct on 7 cases
(flags prices/schedules/weather/"latest"; does NOT flag code mentioning
"current"); `_finalize_answer` matrix — capable+no-tool+factual ⇒ notice,
capable+tool-used+factual ⇒ silent (grounded), capable+no-tool+non-factual ⇒
silent, tool-less ⇒ footer, hermes+no-tool+factual ⇒ notice.

**To activate:** restart the bot / UI.

### Added — Deterministic utility tools (datetime + calculator) & Hermes model

**What:** Two new always-correct tools and a tool-capable candid model:
- `get_current_datetime(tz)` — the real local/UTC date & time. Closes the
  "models invent today's date" gap (seen directly in the World Cup chat, where
  the date framing was unverified).
- `calculate(expression)` — a SAFE arithmetic evaluator (AST-based, no `eval`):
  whitelisted operators only, blocks function calls / names / attribute access,
  caps exponents. Returns the exact number so the model never does mental math
  on figures that must be right (prices, totals, %, probabilities).
- `hermes3:8b` added to the model lists (UI + Telegram). A Nous Hermes
  function-calling model: candid like dolphin **but tool-capable**, so factual
  questions get grounded via `deep_research` instead of fabricated/refused.

**Why:** follow-up to the anti-hallucination work. The real cure for the
"uncensored + accurate" use case is a tool-capable model plus deterministic
tools for anything that must be exact — not a smarter model alone.

**Design:**
- New module `orchestrator/tools/utility_tools.py`; registered in
  `orchestrator/tools/__init__.py`. Tool count 14 → **16**.
- Both tools are pure/deterministic, so they're cheap to call often. The
  calculator can never execute arbitrary code (AST whitelist).
- `hermes3:8b` slots into the existing `MODEL_OPTIONS` lists — no new dependency,
  no external framework (consistent with the roadmap Non-goals).

**Files:** `orchestrator/tools/utility_tools.py` *(new)*,
`orchestrator/tools/__init__.py`, `app.py`, `telegram_bot.py`.

**Verified locally:** `py_compile` clean; registry now lists 16 tools incl.
`get_current_datetime` + `calculate`; live checks — datetime returns the correct
date (2026-06-13), `(1250*1.17)/3 = 487.5`, `2**10 = 1024`, and the dangerous
cases are rejected (`__import__(...)` → unsupported element, `9**9**9` → exponent
too large, `100/0` → division by zero).

**To activate:** restart the bot / UI. For Hermes, `ollama pull hermes3:8b`
(pulled during this session) — then select it from the model picker.

**Hermes verified (2026-06-13):** `ollama show hermes3:8b` reports Capabilities
`completion` + **`tools`** (131k ctx), so it gets the full tool suite (not the
no-tools path). End-to-end agent test passed — Hermes called `calculate` for
"1234 * 5678" (→ 7006652) and `get_current_datetime` for "today's date"
(→ Saturday 13 June 2026), i.e. it grounds factual answers in tools instead of
guessing — exactly the "uncensored + accurate" goal.

### Fixed — Hallucinations on tool-less models (capability-aware grounding)

**What:** A tool-less model (dolphin / gemma — see `_NO_TOOL_MODELS`) no longer
gets the default prompt's impossible "use the web tools to verify" instruction.
Instead it receives `NO_TOOLS_NOTICE`: an honest statement that it has NO tools
and MUST refuse current/factual questions (events, dates, prices, stock quotes,
sports fixtures, news, stats, "latest"/"today") with a fixed line rather than
invent them — and must never fabricate source links or numbers. A deterministic
`NO_TOOLS_FOOTER` ("this model can't verify facts — use a tool-capable model")
is also appended to every tool-less reply as a guaranteed user-facing warning.

**Why:** review of the last chat (FIFA World Cup 2026 schedule) showed the root
cause of the worst hallucination. `dolphin-llama3:70b` produced a fully invented
schedule **with fake FIFA/CNN source links**, ignoring the user's explicit "do
not hallucinate" instruction. The cause is structural, not the model "not
listening": dolphin is in `_NO_TOOL_MODELS`, so `BaseAgent` strips all tools
(`tools=None`), yet the system prompt still ordered it to "use the web tools"
*and* the dolphin persona told it not to hedge — so with no way to verify, it
confidently fabricated. The fix removes that contradiction and adds a
deterministic warning that doesn't depend on the model complying. (For contrast,
`qwen3.6-uncensored` answered the same prompt correctly via `deep_research` — the
tool-capable path already grounds.)

**Design:**
- `base_agent.py`: new `NO_TOOLS_NOTICE` (prepended to the resolved system prompt
  in `run()` only when `_model_supports_tools()` is False, recomputed every run so
  switching back to a tool-capable model drops it cleanly) and `NO_TOOLS_FOOTER`.
- The footer is both streamed via `on_token` (the Chainlit UI renders the answer
  from streamed tokens and ignores the return value once it has content) and
  appended to the return value (Telegram uses the return value). No front-end
  both streams and uses the return, so it never doubles.
- Applies to both front-ends (UI + Telegram) since both drive `BaseAgent`.
- Tool-capable models (qwen3 family) are completely unchanged — no notice, no
  footer — so normal default (`qwen3:4b`) use is unaffected.

**Files:** `orchestrator/agents/base_agent.py` only.

**Verified locally:** `py_compile` clean; `_model_supports_tools()` →
`qwen3:32b`/`qwen3.6-uncensored` True (no notice/footer), `dolphin-llama3:70b`/
`gemma3:4b` False (get notice + footer).

**To activate:** restart the bot / UI.

**Caveat / next:** the notice is prompt-based, so a small model may still ignore
it — the footer is the deterministic guarantee. The real cure for the
"uncensored + accurate" use case is a **tool-capable** candid model (e.g. a
Nous **Hermes** function-calling model on Ollama) so factual questions can be
grounded instead of refused. Tracked in `ROADMAP.md` (new "Grounding &
anti-hallucination" item).

## 2026-06-12

### Changed — More agentic defaults (bigger context + more tool iterations)

**What:** Raised two limits so the agent behaves more like a coding agent on
multi-step tasks:
- `OLLAMA_NUM_CTX` 16384 → **32768** (more conversation/code held verbatim).
- `MAX_TOOL_ITERATIONS` 8 → **20** (longer read→edit→run→fix loops before the cap).

**Why:** the user asked which local models can work at "Claude Code level". The
model matters (tool-capable qwen3 family — `qwen3.6-uncensored` / `qwen3:32b` —
vs the tool-less dolphin/gemma), but the harness limits also capped how agentic
it could be. These two bumps lift that ceiling. Honest caveat recorded: local
4B–32B models give a capable *approximation*, not real Claude Code.

**Design/notes:**
- `MAX_TOOL_ITERATIONS` is only a cap — simple chats still finish in 1-2 steps;
  this just lets complex tasks go further.
- 32768 is safe for the qwen3 family on a 32 GB card; the 70B (already
  CPU-offloaded, and tool-less anyway) may crawl/OOM at 32k — lower
  `ZERO_AGENT_NUM_CTX` for it if needed.

**Files:** `orchestrator/config.py` + the four launchers (`START_ALL.bat`,
`start_zero_agent.bat`, `start_telegram.bat`, `START_TELEGRAM_ALL.bat`).
`ROADMAP.md` planned-item #1 marked done.

**Verified locally:** config loads with `MAX_TOOL_ITERATIONS=20`,
`OLLAMA_NUM_CTX=32768`.

**To activate:** restart the bot / UI.

**Next:** optionally pull a dedicated coder model (e.g. a `qwen*-coder`) for
stronger coding — large download, pending the user's go-ahead.

## 2026-06-11

### Fixed — "Cannot send a request, as the client has been closed" (UI)

**What:** After a chat ended or the Chainlit app hot-reloaded (`-w`), the next
turn could crash in `projects.recall` / memory recall with
`RuntimeError: Cannot send a request, as the client has been closed`.

**Why:** The memory + project stores are process-wide singletons that bound to
the **first session's** Ollama client. `@cl.on_chat_end` closes the session
client (`client.aclose()`), but the singleton stores kept using that now-closed
client for embeddings — so the next recall hit a closed client. (Latent
pre-existing bug; surfaced via the watch-mode reloads during this session.)

**Fix:** In `app.py` `_init_session`, the stores are now initialized with their
OWN long-lived embedding client (`get_store()` / `get_project_store()` with no
argument → each creates a dedicated `OllamaClient` once) instead of the
per-session client. The session client is still closed on chat end, but the
stores no longer depend on it. (The Telegram entry point already used a dedicated
embedding client, so it was unaffected.)

**Files:** `app.py` only.

**Verified locally:** compile + import OK; both stores hold a live dedicated
client.

**To activate:** restart the Chainlit UI.

### Fixed — HTTP 400 "System message must be at the beginning"

**What:** Intermittent `Ollama /api/chat 400: ... Jinja Exception: System message
must be at the beginning` even though the server was running.

**Why:** Some models' chat templates allow only ONE system message, at index 0.
The agent uses separate system messages for the persona + recalled memory
(`[[ZERO_MEMORY]]`) + project context (`[[ZERO_PROJECT]]`), so when memory/project
recall fired there were 2-3 system messages and those strict templates rejected
the request. It showed up "occasionally" — only when a memory/project block was
present (more likely as stored memories accumulated, and after the per-model
persona change always added the main prompt too).

**Fix:** `_merge_system_messages()` in `base_agent.py` collapses all system
messages into a single leading system message in the payload sent to the model
(`_stream_turn` uses it), while the conversation history keeps them separate so
each block is still refreshed independently every turn. Non-system messages keep
their order. Universal — a single leading system message is accepted by every
template.

**Files:** `orchestrator/agents/base_agent.py` only.

**Verified locally:** compile OK; merging 3 system messages yields exactly one
leading system message containing all parts, with conversation order preserved;
the no-system case passes through unchanged.

**To activate:** restart the bot / UI.

### Added — Per-model system prompts (dolphin gets a direct persona)

**What:** The agent can now use a different system prompt per model. The
"dolphin" family (incl. `dolphin-llama3:70b`, `dolphin-unleashed`) now gets a
direct, candid, low-refusal persona instead of the generic agent prompt; all
other models (qwen, gemma, qwen3.6) are unchanged.

**Why:** the user found `dolphin-llama3:70b` more restricted than
`qwen3.6-uncensored`. The cause is largely the system prompt — dolphin models
are steered heavily by it, and they were getting the generic "elite agent"
prompt, so Llama-3's base refusals showed through. (Also clarified that you
cannot "ask qwen3.6 to fine-tune" dolphin — fine-tuning is a separate training
process; this prompt change is the right, no-training fix.)

**Design:**
- `base_agent.py`: `DOLPHIN_SYSTEM_PROMPT` + an ordered `_MODEL_SYSTEM_PROMPTS`
  (name-substring → persona) and `_resolve_system_prompt(model, default)`.
  Easy to extend per model.
- `BaseAgent.run()` now resolves the persona for the **active** model and
  refreshes the main system prompt every run, so a mid-session model switch
  swaps the persona too. Memory/project blocks (system messages starting with
  `[[`) are preserved; only the main persona prompt is replaced.
- Side fix: the main system prompt is now always present even when a
  memory/project block exists (previously the presence of any system message
  could cause the main prompt to be skipped).
- Applies to both front-ends (UI + Telegram) since both use `BaseAgent`.

**Files:** `orchestrator/agents/base_agent.py` only.

**Verified locally:** compile OK; resolver maps dolphin* → dolphin persona and
everything else → default; message-rewrite keeps exactly one main prompt and
preserves the memory block.

**To activate:** restart the bot / UI, then select `dolphin-llama3:70b`.

### Planned — context & memory roadmap (no code yet)

**What:** Recorded a prioritized plan in `ROADMAP.md` (new "Planned — context &
memory" section): (1) raise the default `num_ctx`, (2) persist Telegram chat
history to disk like the UI, (3) rolling conversation summary to compress old
context instead of Ollama silently truncating it.

**Why:** the user asked how the agent remembers long conversations across
sessions (vs Gemini's huge context) and asked to capture the improvements for
future work. Investigation (read-only) confirmed: full transcript is sent each
turn, Ollama truncates past `num_ctx=16384`, the vector store persists *facts*
across sessions/front-ends, the UI restores transcripts from SQLite, and
Telegram history is in-memory only.

**Files:** `ROADMAP.md` (documentation only — no behavior change).

### Added — Telegram quick-action buttons (Research/Image/Remember modes)

**What:** Telegram now has a persistent reply keyboard with mode buttons —
🔭 Research, 🎨 Image, 🧠 Remember, 💬 Chat — the Telegram equivalent of the web
UI's composer buttons. Tap a mode, send a topic, and the chat operates in that
mode until switched: Research routes to `deep_research`, Image to ComfyUI,
Remember saves each message straight to long-term memory, Chat is normal.

**Why:** the user wanted the same easy buttons in Telegram as in the web UI.

**Design:**
- `Session` gained a `mode` field (chat|research|image|remember).
- `_mode_keyboard()` returns a persistent `ReplyKeyboardMarkup`; shown on
  `/start`, `/help`, and the new `/menu` command.
- `_handle_message` maps a tapped label (via `MODE_LABELS`) to the session mode;
  for normal messages it applies the mode — Remember saves directly (no model
  round-trip), Research/Image prepend a tool instruction to the prompt.
- `_run_agent_turn` now takes `prompt` (what the model sees) plus an optional
  `recall_text` (the raw topic) so memory/project recall + distillation stay
  clean even when a mode instruction is prepended.
- `/menu` added to the bot command list.

**Files:** `telegram_bot.py` only.

**Verified locally:** compile + import OK; reply keyboard, mode labels, `/menu`,
and the new `Session.mode` field all present.

**To activate:** restart the Telegram bot.

### Added — UI quick-action buttons (composer commands + starters)

**What:** The Chainlit web UI now has tap-to-use buttons so common actions don't
need to be typed as instructions. Three composer command buttons (Chainlit
"commands"): 🔭 **Research** (routes to `deep_research`), 🎨 **Image** (routes to
`run_comfyui_workflow`), 🧠 **Remember** (saves the typed text straight to
long-term memory). Plus four one-click **Starters** on the welcome screen.

**Why:** the user wanted easy buttons in the UI instead of phrasing a research /
image / save instruction every time.

**Design:**
- `CHAT_COMMANDS` (id/icon/description/button) registered via
  `cl.context.emitter.set_commands(...)` in `on_chat_start` and `on_chat_resume`.
- `on_message` reads `message.command`: `Remember` saves directly through the
  memory store (deterministic, no LLM round-trip) and returns; `Research`/`Image`
  prepend a clear instruction to the prompt so the agent reaches for the right
  tool. Plain messages are unchanged.
- `@cl.set_starters` provides the welcome-screen example prompts.

**Files:** `app.py` only. Requires Chainlit ≥ 2.x (installed: 2.11.1).

**Note:** Telegram already has its own model buttons; these are the web-UI
equivalent quick actions. **To activate:** restart the Chainlit UI.

**Verified locally:** `app.py` compiles and imports; commands defined with
`button=True`.

### Added — Deep web research (`deep_research` tool + SearXNG + cleaner fetches)

**What:** The agent can now do real, multi-source web research instead of only
reading search snippets. New tool `deep_research(query, max_sources)` runs a full
search → fetch → clean-extract → synthesize loop in a SINGLE tool call: it
searches, opens the top pages, extracts each page's clean article text, and
returns a consolidated, numbered, source-attributed corpus for the model to
synthesize and cross-reference. `fetch_webpage` now also returns clean article
text instead of raw HTML.

**Why:** the user observed the agent answering from stale training memory
("talking about 2023") instead of going to the web, and wanted high-quality
research / cross-referencing / analysis — all local, no paid APIs. Root causes:
(1) the model wasn't told to search for current info, and (2) `search_web` only
returned snippets, never full page content.

**Design decisions (confirmed with the user):** Tier 1 — a native research tool
built inside the existing architecture (consistent with the roadmap's "Phase 6
sub-agents: build the idea, not a framework"). SearXNG enabled (the user has
Docker). Researched current OSS options before building (see Sources below).

**Components:**
- **Tier 0 (prompt):** `base_agent.py` system prompt now mandates using the web
  tools for any current/uncertain fact and never presenting remembered
  dates/figures as current; points the model to `deep_research` for thorough work.
- **`orchestrator/tools/research_tools.py`** *(new)* — `deep_research` tool +
  `web_search` backend (SearXNG JSON API when reachable, automatic DuckDuckGo
  fallback) + concurrent fetch + `trafilatura` extraction. Per-source and
  timeout limits from config.
- **`fetch_webpage`** (`system_tools.py`) — extracts clean text via trafilatura
  for HTML pages, falls back to the raw body otherwise.
- **`config.py`** — `SEARXNG_HOST` (default `http://localhost:8888`),
  `RESEARCH_MAX_SOURCES` (5), `RESEARCH_PER_SOURCE_CHARS` (3500),
  `RESEARCH_FETCH_TIMEOUT` (12s).
- **SearXNG (optional, better search):** `docker-compose.searxng.yml`,
  `searxng/settings.yml` (JSON API on, limiter off), `START_SEARXNG.bat`. Runs
  on `http://localhost:8888`; the tool auto-detects it and falls back to DDG when
  it's down.
- **`trafilatura>=2.0`** added to `requirements.txt` (installed: 2.1.0).
- Registered via `orchestrator/tools/__init__.py`.

**Impact:** research queries are slower (multiple fetches + extraction + longer
synthesis) and want a stronger model (`qwen3:32b` / `qwen3.6`) and larger
`num_ctx`; the RTX 5090 handles this. All heavy lifting is in one tool call, so
it does not burn the 8-iteration cap. Fully local; no paid APIs.

**Verified locally:** compile OK, all imports OK, tool registers (now 14 tools),
and a live `deep_research` run via DuckDuckGo fetched + cleaned 3 sources
(~11k chars). SearXNG path verified after the user started the container —
`web_search` reported `ENGINE USED: SearXNG` with higher-quality results
(nvidia.com, Wikipedia, techpowerup); auto-falls back to DDG when it's down.

**To activate:** restart the bot / UI (the user runs it) to load the new tool.
Optional: start Docker Desktop, run `START_SEARXNG.bat`, then restart for the
better search backend.

**Sources reviewed:**
[local-deep-research](https://github.com/LearningCircuit/local-deep-research),
[langchain local-deep-researcher](https://github.com/langchain-ai/local-deep-researcher),
[SearXNG](https://searxng.org/),
[trafilatura](https://trafilatura.readthedocs.io/).

### Added — Telegram model picker (inline-keyboard buttons) + command menu

**What:** `/model` in Telegram now shows tap-to-select buttons (an inline
keyboard) for the available models, ticking the active one with ✅, instead of
requiring the user to type the model name. The bot's slash commands are also
registered via `setMyCommands` so they appear in Telegram's "/" menu.

**Why:** the user wanted a model chooser in Telegram like the web UI's dropdown,
and found typing `/model <name>` non-obvious.

**Design:**
- `_model_keyboard(active)` builds an inline keyboard (2 buttons/row) with
  `callback_data` `m:<model>`.
- New Bot API methods on `TelegramAPI`: `edit_message_text`,
  `answer_callback_query`, `set_my_commands`; `send_message` gained a
  `reply_markup` kwarg.
- `get_updates` now also requests `callback_query` updates; the poll loop
  dispatches them to the new `_handle_callback`, which authorizes the tapper,
  switches the chat's model, acks the tap, and edits the menu to show the new
  active model. Typing `/model <name>` still works (e.g. for a model not in the
  button list).
- `_handle_command` was refactored to send its replies directly (so it can
  attach the keyboard) instead of returning a string.

**Files:** `telegram_bot.py` only.

**Note:** `/forget-project` keeps working when typed but is not in the "/" menu
(a hyphen is not a valid registered command name).

**To activate:** restart the bot (the user runs it), since this changed code.

### Added — `START_TELEGRAM_ALL.bat` (one-click Telegram stack)

**What:** A one-click launcher that starts everything the Telegram path needs —
Ollama (required) + ComfyUI (for images) if not already running — then launches
the Telegram bot. Does NOT open the Chainlit web UI.

**Why:** the user asked whether they must run both `START_ALL.bat` (which also
opens the web UI) and `start_telegram.bat`. They don't — this gives a single
button for the Telegram-only path so Ollama gets started without the UI.

**Files:** `START_TELEGRAM_ALL.bat` *(new)*. Mirrors `START_ALL.bat` but calls
`start_telegram.bat` instead of the Chainlit UI.

**Note:** never run two bot instances against the same token at once (Telegram
allows a single `getUpdates` consumer — a second one causes 409 conflicts).

### Added — Telegram bridge (new entry point)

**What:** A third front-end, `telegram_bot.py`, that lets you talk to the same
Zero Agent from Telegram — same tools, same long-term memory, same projects.
The LLM still runs 100% locally on Ollama; only the chat transport goes through
Telegram's Bot API.

**Why:** the user asked to control the agent from their phone via Telegram.

**Design decisions (confirmed with the user):**
- **Raw `httpx` long-polling** against the Bot API — no `python-telegram-bot`,
  no webhooks, no new dependencies (httpx is already used). Stays self-contained
  and local-first.
- **Final answer + typing indicator** as the response style (no per-tool chatter;
  a refreshed "typing…" action while the agent works).
- **Shared memory + projects** with the Chainlit UI (same ChromaDB store, same
  active-project state, same auto-distillation of durable facts).
- **Security gate:** the agent can run shell commands and touch files, so the bot
  only serves numeric IDs in `TELEGRAM_ALLOWED_IDS`. Unknown senders just get
  told their own ID (so it can be added) and are otherwise ignored. An empty
  allow-list refuses everyone (safe default) but still reveals IDs for bootstrap.
- **Per-chat `Session`** (own Ollama client + agent + history) serialized by a
  per-chat `asyncio.Lock`; one task per message so a slow render never blocks
  polling or other chats.
- Generated images/videos (ComfyUI `[[MEDIA]]` markers) are sent as photos /
  documents after the text answer, mirroring the inline-image behavior in the UI.

**Commands:** `/start`, `/help`, `/reset`, `/model [name]`, `/projects`,
`/project [name]`, `/learn <text>`, `/forget-project <name>`, `/whoami`.

**Files:**
- `telegram_bot.py` *(new)* — the entry point (API client, sessions, commands,
  poll loop). Loads `zero-agent/.env` before importing config.
- `orchestrator/config.py` — new "Telegram bridge" section:
  `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_IDS`, `TELEGRAM_POLL_TIMEOUT`,
  `TELEGRAM_MODEL`, and a `_parse_int_set` helper.
- `start_telegram.bat` *(new)* — venv launcher for the bridge.
- `.env.example` *(new)* — documents the Telegram env vars; copy to `.env`.
- `ROADMAP.md` — noted Telegram as a shipped, additional entry point.
- `requirements.txt` — comment noting Telegram needs no new dependency.

**How to run:**
1. Create a bot with `@BotFather`, copy the token.
2. Copy `.env.example` → `.env`; paste the token into
   `ZERO_AGENT_TELEGRAM_TOKEN`. Leave `ZERO_AGENT_TELEGRAM_ALLOWED_IDS` empty
   for now.
3. Run `start_telegram.bat` (or `python telegram_bot.py`).
4. Message the bot; it replies with your Telegram ID. Paste that into
   `ZERO_AGENT_TELEGRAM_ALLOWED_IDS` and restart. You can now chat with the agent.

**Status:** code complete. Verified locally: `py_compile` clean, full module
import OK (all 13 tools register), message-splitting works, and a missing token
exits gracefully with a clear message. **Not yet smoke-tested against the live
Telegram API** — needs a real `@BotFather` token + a running Ollama.
