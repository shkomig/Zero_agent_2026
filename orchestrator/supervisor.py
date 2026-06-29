"""The Supervisor — the manager half of the Supervisor-Worker architecture.

Flow:  plan the goal  ->  for each atomic step: run the Worker, verify reality,
and on failure RETRY (with the root cause fed back) or REPLAN the remaining steps
-- never the naive "halt on first failure".

Why this breaks the old ceiling:
  * The Worker (BaseAgent) gets a SMALL focused context per step (from the
    TaskManager) instead of an ever-growing transcript, so a local model stays
    coherent across many steps.
  * Success is judged by reality (RealityVerifier: does the file exist / compile /
    parse?), not by the model's own "looks good to me".
  * State lives on disk, so a long goal survives a crash.

The Supervisor/Planner can run a strong reasoning model (qwen3:32b) while the
Worker runs the fast Qwen3-Coder MoE — the right model for each role.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from collections.abc import Awaitable, Callable

from orchestrator import api_guard, config, verify
from orchestrator.agents.base_agent import BaseAgent
from orchestrator.models.ollama_client import OllamaClient, OllamaError
from orchestrator.reality_verifier import RealityVerifier
from orchestrator.task_manager import TaskManager

logger = logging.getLogger("zero_agent.supervisor")

#: How many times a single step is retried (with the failure fed back) before
#: the Supervisor escalates to replanning the remaining steps.
MAX_TASK_ATTEMPTS = 2

PLANNER_PROMPT = (
    "You are a task PLANNER for an autonomous AI coding agent running locally on "
    "Windows. Break the user's goal into a short sequence of small, concrete, "
    "individually-verifiable steps. Each step should be ONE atomic "
    "action a worker can finish and that can be checked on disk (e.g. 'Create "
    "C:\\\\projects\\\\site\\\\index.html with the page markup'). Prefer absolute "
    "paths. Do NOT include vague steps like 'plan' or 'think'.\n"
    "EXISTING vs NEW: if the working directory ALREADY CONTAINS a project (source "
    "files are listed below), this is an AUDIT/UPGRADE of existing code — READ and "
    "MODIFY those files in place. Do NOT scaffold, do NOT call create_project, and do "
    "NOT invent files like main.py / test_main.py that are not already there; work "
    "with the real files that exist. When you modify an existing file, change ONLY what "
    "the goal asks and PRESERVE every other existing function/class — do not drop public "
    "methods other code may call. End an existing-project change with a step that "
    "smoke_runs the app and/or run_tests on the project to confirm nothing else broke. "
    "ONLY scaffold a brand-NEW project when the working directory is EMPTY.\n"
    "VERIFYING A WEB/SERVER APP (Streamlit, FastAPI/uvicorn, Flask, Node): NEVER make a "
    "step that just runs the server (e.g. 'run streamlit run app.py' or 'uvicorn ...') — "
    "a server BLOCKS forever and (Streamlit) opens a browser, so the step hangs. Instead "
    "the verify step must use the smoke_run tool with the start_command and port, which "
    "starts it, HTTP-probes it, and ALWAYS kills it — e.g. smoke_run(project_dir, "
    "start_command='streamlit run app.py', port=8501) or "
    "start_command='python -m uvicorn main:app --port {port}'. Phrase the step as "
    "'smoke_run the app (start_command=..., port=...) to confirm it boots'.\n"
    "BUILD A COMPLETE, RUNNABLE PROJECT (new projects only): when the goal is to build "
    "a NEW app/project in an EMPTY directory, "
    "make the FIRST step 'Scaffold the project with create_project (kind=python/node/"
    "static/android-kotlin)' — it lays down a correct, immediately-buildable skeleton "
    "(and the real binary Gradle wrapper for Android when gradle is installed). Then "
    "plan EVERY remaining essential file so it actually builds/runs — the entry "
    "point/source, the manifest/config, AND the build files — not a partial scaffold; "
    "finish with a step that checks the project is complete and consistent. Use each platform's "
    "MODERN default language: Kotlin for Android (NOT Java), Swift for iOS. For an "
    "Android app, plan at least these files: AndroidManifest.xml, MainActivity.kt, "
    "app/build.gradle (with the Kotlin plugin), the root build.gradle, "
    "settings.gradle, and the res/ layout + values. "
    f"Never put a project's dev/server on a RESERVED port ({config.RESERVED_PORTS}); "
    "choose a free one (e.g. 8501, 5173, 3000, 8080). "
    "Make the LAST step 'Verify the project builds, runs, and passes tests; fix any "
    "errors' — the worker calls verify_build (compiles?), then smoke_run (actually "
    "starts up?) and run_tests (tests pass?) on the project root, and fixes reported "
    "failures until they pass (SKIPPED is acceptable when a toolchain/SDK is absent).\n"
    "Respond with ONLY a JSON array of strings, nothing else. "
    'Example: ["Create index.html", "Add styles.css", "Open index.html to verify"].'
)

REPLAN_PROMPT = (
    "You are re-planning for an autonomous AI coding agent. A step just FAILED "
    "verification. Given the goal, what already succeeded, and the failure reason, "
    "produce a REVISED JSON array of the REMAINING steps (fix the failed work, "
    "then continue). Respond with ONLY a JSON array of strings."
)

SYNTH_PROMPT = (
    "You are writing the FINAL DELIVERABLE of a completed multi-step task. Using "
    "the GOAL and the step results below as your evidence, produce exactly what the "
    "goal asked for — a complete report / analysis / answer, NOT a status update or "
    "a list of steps. Follow any structure or sections the goal specified (e.g. "
    "Executive Summary, Findings, Risks). Base every claim on the step evidence; "
    "where the evidence is thin or missing, say so honestly rather than inventing. "
    "Be concrete, organized, and self-contained."
)

# (kind, payload) — optional progress hook so a UI/CLI can render live status.
EventCallback = Callable[[str, str], Awaitable[None]]

# Absolute Windows (C:\...) or POSIX (/...) paths named in a goal. Used to detect a
# target directory the user wants the agent to operate IN, so the run adopts it as
# its working directory instead of the default scratch workspace — the fix for
# "worker wrote to C:\Real\Project but the verifier checked the scratch dir".
_ABS_PATH_RE = re.compile(r"""[A-Za-z]:\\[^\s"'<>|?*\n]+|/(?:[^\s"'<>|?*\n/]+/?)+""")


def _infer_working_dir(goal: str) -> str | None:
    """Return the most specific EXISTING directory named in ``goal``, or ``None``.

    A goal like "audit the codebase at C:\\Vs-Pro\\…\\Engineered-prompt" must run IN
    that directory, not in the scratch workspace — otherwise the verifier checks the
    wrong place and fails every real write. We take absolute paths from the goal,
    map a named FILE to its parent, and pick the longest one that exists on disk.
    """
    candidates: list[str] = []
    for m in _ABS_PATH_RE.finditer(goal or ""):
        raw = m.group(0).rstrip(" .,:;\"')]}")
        if os.path.isdir(raw):
            candidates.append(os.path.normpath(raw))
        elif os.path.isfile(raw):
            candidates.append(os.path.normpath(os.path.dirname(raw)))
    if not candidates:
        return None
    # Most specific (deepest) existing directory wins.
    return max(candidates, key=len)


class Supervisor:
    def __init__(
        self,
        worker: BaseAgent,
        registry,
        *,
        planner_client: OllamaClient | None = None,
        cwd: str = "",
        on_event: EventCallback | None = None,
        on_tool_start=None,
        on_tool_end=None,
        on_approval=None,
    ) -> None:
        self.worker = worker
        self.registry = registry
        # Forwarded to the Worker's run() so a UI can render each tool call live
        # (the same collapsible-step callbacks the normal chat path uses).
        self.on_tool_start = on_tool_start
        self.on_tool_end = on_tool_end
        # HITL approver — gates destructive Worker tools even in autonomous runs.
        self.on_approval = on_approval
        # Planner uses the reasoning model; defaults to its own client so it can
        # differ from the Worker's model without one clobbering the other.
        self.planner = planner_client or OllamaClient(model=config.SUPERVISOR_MODEL)
        # Planning is a STRUCTURED JSON call — force thinking OFF. On a thinking
        # model (qwen3) the reasoning tokens consumed the whole num_predict budget
        # and left message.content EMPTY, so no plan could be parsed.
        self.planner.think = False
        self.verifier = RealityVerifier(registry)
        self.task_manager = TaskManager(cwd=cwd or "")
        self.on_event = on_event
        # Per-RUN baseline of a code file's PRE-AGENT content (first-touch-wins), for the
        # API-preservation guard. Reset at the start of each execute_plan.
        self._run_baseline: dict[str, str] = {}

    async def _emit(self, kind: str, payload: str) -> None:
        logger.info("supervisor %s: %s", kind, payload)
        if self.on_event:
            try:
                await self.on_event(kind, payload)
            except Exception:  # noqa: BLE001 - a UI hook must never break the run
                logger.exception("on_event hook failed")

    # --- planning -----------------------------------------------------------
    @staticmethod
    def _parse_json_array(text: str) -> list[str] | None:
        """Extract a JSON array of strings from a possibly-noisy model reply.

        Weak local models often ramble before (or instead of) the JSON, so we
        don't trust a single greedy regex: we scan every ``[`` and return the
        LAST one that parses as a list of non-empty strings (the final answer
        usually comes after the rambling).
        """
        if not text:
            return None
        cleaned = re.sub(r"```[a-zA-Z]*", "", text).replace("```", "")

        def as_steps(data: object) -> list[str] | None:
            if isinstance(data, list):
                items = [str(x).strip() for x in data if str(x).strip()]
                return items or None
            return None

        best: list[str] | None = None
        for start in (m.start() for m in re.finditer(r"\[", cleaned)):
            depth = 0
            for i in range(start, len(cleaned)):
                if cleaned[i] == "[":
                    depth += 1
                elif cleaned[i] == "]":
                    depth -= 1
                    if depth == 0:
                        try:
                            steps = as_steps(json.loads(cleaned[start : i + 1]))
                        except json.JSONDecodeError:
                            steps = None
                        if steps:
                            best = steps  # keep scanning; prefer the last valid one
                        break
        if best:
            return best

        # Fallback: the model returned a NUMBERED or BULLETED list instead of JSON
        # (common when a goal is long/complex). Salvage step lines so a one-off
        # format slip doesn't abort the whole run.
        lines: list[str] = []
        for ln in cleaned.splitlines():
            m = re.match(r"""\s*(?:\d+[.)]|[-*•])\s+["']?(.+?)["']?,?\s*$""", ln)
            if m and len(m.group(1).strip()) > 3:
                lines.append(m.group(1).strip())
        return lines or None

    async def _ask_planner(self, system: str, user: str) -> list[str] | None:
        try:
            resp = await self.planner.chat(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                tools=None,
            )
        except OllamaError as exc:
            await self._emit("error", f"planner call failed: {exc}")
            return None
        return self._parse_json_array(resp.message.content)

    def _planning_context(self) -> str:
        """A grounding preamble for the planner: WHERE to work and WHAT is already
        there. Lets the planner tell "audit an existing project" (modify in place)
        from "build a new one" (scaffold) instead of always scaffolding."""
        cwd = self.task_manager.state.memory.cwd
        if not cwd:
            return ""
        lines = [f"WORKING DIRECTORY: {cwd}"]
        try:
            entries = sorted(os.listdir(cwd)) if os.path.isdir(cwd) else []
        except OSError:
            entries = []
        # Ignore noise dirs so an existing project isn't mistaken for empty.
        meaningful = [e for e in entries if e not in {
            ".git", ".venv", "__pycache__", ".pytest_cache", ".idea", "node_modules"
        }]
        if meaningful:
            shown = ", ".join(meaningful[:25])
            lines.append(
                "This directory ALREADY CONTAINS a project (existing entries): "
                f"{shown}. Treat this as an EXISTING codebase — read and MODIFY these "
                "files in place; do NOT scaffold or create main.py/test_main.py that "
                "aren't already here. Use absolute paths under this directory."
            )
        else:
            lines.append(
                "This directory is EMPTY — a brand-new project; scaffold it first."
            )
        return "\n".join(lines) + "\n\n"

    async def plan_goal(self, user_goal: str) -> bool:
        await self._emit("planning", user_goal)
        ctx = self._planning_context()
        plan = await self._ask_planner(PLANNER_PROMPT, f"{ctx}GOAL: {user_goal}")
        if not plan:
            # One retry — a cold-loaded model or a one-off non-JSON reply usually
            # succeeds the second time. Add an explicit format nudge.
            await self._emit("planning", "retrying plan…")
            plan = await self._ask_planner(
                PLANNER_PROMPT
                + '\nReturn ONLY a JSON array, e.g. ["Step 1", "Step 2"]. No prose.',
                f"{ctx}GOAL: {user_goal}\n\nBreak this into 3-8 concrete, ordered steps.",
            )
        if not plan:
            await self._emit("error", "could not produce a valid plan")
            return False
        self.task_manager.set_plan(user_goal, plan)
        await self._emit(
            "plan_ready",
            "\n".join(f"{i+1}. {s}" for i, s in enumerate(plan)),
        )
        return True

    # --- execution ----------------------------------------------------------
    async def execute_plan(self) -> str:
        replans = 0
        self._run_baseline = {}  # fresh API baseline per run
        while not self.task_manager.is_done():
            task = self.task_manager.get_next_task()
            if task is None:
                break

            await self._emit("task_start", f"[{task.id}] {task.description}")
            context = self.task_manager.get_context_for_worker()
            retry_hint = ""
            if task.root_cause:
                retry_hint = (
                    f"\n\nThis step FAILED before. Reason: {task.root_cause}\n"
                    "Fix that specifically this time."
                )
            worker_prompt = (
                f"TASK: {task.description}{retry_hint}\n\n"
                "Execute ONLY this task, fully, by calling the tools. Do not "
                "describe what you would do — actually do it.\n"
                "If this task creates a file, call write_file with the EXACT target "
                "path from the task, copied character-for-character — do NOT rename, "
                "shorten, or drop any folder in the path."
            )

            # Capture the paths the worker actually writes to, so we can detect and
            # repair a garbled path: weak local models often mangle a long path
            # (e.g. drop a folder), write the file to the wrong place, then "verify"
            # their own wrong path — leaving nothing at the path the plan expected.
            written: list[str] = []
            # Snapshot a code file's PRE-AGENT content the FIRST time we're about to
            # write it THIS RUN, so we can flag a rewrite that silently drops public API
            # the project depends on (observed: a deleted public method → app crash). The
            # baseline is per-RUN and first-touch-wins (``self._run_baseline``): a file
            # the agent CREATED this run is stored as "" (no prior API), so renaming a
            # function in a brand-new file across retries is NOT a false "dropped API".
            # Captured at tool-START, before the write replaces the file.

            async def _capture_start(name, args, _orig=self.on_tool_start):
                if name == "write_file" and isinstance(args, dict):
                    fp = args.get("filepath") or args.get("path")
                    if fp:
                        written.append(str(fp))
                        path = str(fp)
                        if path.endswith(".py") and path not in self._run_baseline:
                            try:
                                self._run_baseline[path] = (
                                    open(path, encoding="utf-8", errors="replace").read()
                                    if os.path.isfile(path) else ""
                                )
                            except OSError:
                                self._run_baseline[path] = ""
                if _orig is not None:
                    return await _orig(name, args)
                return None

            try:
                result = await self.worker.run(
                    worker_prompt,
                    history=[],
                    injected_context=context,
                    on_tool_start=_capture_start,
                    on_tool_end=self.on_tool_end,
                    on_approval=self.on_approval,
                )
            except OllamaError as exc:
                result = f"(worker error: {exc})"

            await self._heal_garbled_paths(task.description, written)

            ok, msg = self.verifier.verify_task(
                task.description, result, self.task_manager.state.memory.cwd
            )

            # API-preservation guard (deterministic, no LLM): a rewrite of an existing
            # file must not silently DROP public functions/methods the rest of the
            # project depends on (the file still compiles, so reality passes — but a
            # caller now crashes at runtime).
            if ok and written:
                dropped = self._check_api_preserved(written)
                if dropped:
                    ok = False
                    msg = (
                        "the edit removed existing public API the rest of the project "
                        f"may depend on: {', '.join(dropped)}. Re-add them unchanged "
                        "(only this task's specific change was requested) unless the "
                        "task explicitly asked to remove them."
                    )
                    await self._emit("api_regression", f"[{task.id}] dropped: {', '.join(dropped)}")

            # Intent check: reality confirmed the file exists/compiles — now make sure
            # it actually DOES what the step asked. Catches a shallow/wrong deliverable
            # that compiles fine (the "looks done, is broken" failure). Only on steps
            # that produced a file; never blocks on a judge hiccup.
            if ok and written and config.SUPERVISOR_SEMANTIC_CHECK:
                sem_ok, critique = await self._semantic_check(task.description, result, written)
                if not sem_ok:
                    ok = False
                    msg = f"intent not met: {critique}"
                    await self._emit("intent_fail", f"[{task.id}] {critique}")

            if ok:
                await self._emit("task_done", f"[{task.id}] {msg}")
                self.task_manager.mark_completed(task.id, result)
                continue

            # --- failure handling: retry, then replan, then halt ------------
            await self._emit("task_failed", f"[{task.id}] {msg}")
            self.task_manager.mark_failed(task.id, msg)

            if task.attempts < MAX_TASK_ATTEMPTS:
                await self._emit("retry", f"[{task.id}] attempt {task.attempts + 1}")
                self.task_manager.reset_task_to_pending(task.id)
                continue

            if replans < config.SUPERVISOR_MAX_REPLANS:
                replans += 1
                await self._emit("replan", f"#{replans} after [{task.id}] failed")
                new_plan = await self._ask_planner(
                    REPLAN_PROMPT,
                    f"GOAL: {self.task_manager.state.goal}\n"
                    f"COMPLETED: {[t.description for t in self.task_manager.state.plan if t.status.value == 'completed']}\n"
                    f"FAILED STEP: {task.description}\n"
                    f"FAILURE REASON: {msg}",
                )
                if new_plan:
                    self.task_manager.replan_remaining(new_plan)
                    continue
                await self._emit("error", "replan produced no valid steps")

            return (
                f"Plan halted. Step {task.id} ('{task.description}') failed "
                f"verification after {task.attempts} attempts and "
                f"{replans} replan(s): {msg}"
            )

        if self.task_manager.all_succeeded():
            return "All tasks completed and verified against reality."
        done = sum(
            1
            for t in self.task_manager.state.plan
            if t.status.value == "completed"
        )
        return (
            f"Finished with partial success: {done}/{len(self.task_manager.state.plan)} "
            "steps verified. See task state for details."
        )

    async def _heal_garbled_paths(self, task_description: str, written: list[str]) -> None:
        """Repair a worker that wrote a file to a MANGLED path.

        Weak local models often can't reproduce a long path verbatim — they drop a
        folder or alter a name, so the file lands next to (not at) the path the plan
        asked for, and verification then fails forever. Here: for each file the task
        expected, if it's missing but the worker DID write exactly one file with the
        same basename (the garbled twin), move it to the intended path. Deterministic,
        no model cooperation needed. Best-effort and never raises.
        """
        if not written:
            return
        cwd = self.task_manager.state.memory.cwd
        expected = self.verifier._resolve(
            self.verifier._extract_paths(task_description), cwd
        )
        landed = [w for w in written if os.path.isfile(w)]
        for exp in expected:
            if os.path.isfile(exp):
                continue  # the worker got it right
            base = os.path.basename(exp).lower()
            twins = [w for w in landed if os.path.basename(w).lower() == base]
            if len(twins) != 1 or os.path.normpath(twins[0]) == os.path.normpath(exp):
                continue
            try:
                os.makedirs(os.path.dirname(exp) or ".", exist_ok=True)
                shutil.move(twins[0], exp)
                await self._emit(
                    "path_fixed",
                    f"worker wrote to a garbled path; moved {twins[0]} → {exp}",
                )
            except OSError as exc:
                logger.warning("path heal failed (%s → %s): %s", twins[0], exp, exc)

    def _check_api_preserved(self, written: list[str]) -> list[str]:
        """Return public symbols a modification DROPPED vs each file's PRE-AGENT
        baseline (``self._run_baseline``). Only files that existed before the agent
        touched them (non-empty baseline) are checked — a brand-new file has no prior
        API to preserve. Deterministic; best-effort (never raises)."""
        dropped: list[str] = []
        for path in dict.fromkeys(written):
            before = self._run_baseline.get(path)
            if not before or not os.path.isfile(path):
                continue
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    after = f.read()
            except OSError:
                continue
            dropped.extend(api_guard.dropped_public_symbols(before, after))
        return sorted(set(dropped))

    async def _semantic_check(
        self, task_description: str, result: str, written: list[str]
    ) -> tuple[bool, str]:
        """Judge whether a step's deliverable fulfils its INTENT (not just compiles).

        Builds evidence from the worker's result PLUS the actual content of the file(s)
        it wrote — the reviewer must see what landed on disk, not the worker's own
        description of it — then asks the acceptance reviewer. Never raises."""
        # Reality already confirmed these files exist AND compile — say so, so the
        # judge rules on INTENT and never re-flags "truncated/incomplete".
        evidence = (
            "(Note: every file below already PASSED an existence + syntax/compile check, "
            "so it is NOT truncated — judge only whether it fulfils the step's intent.)\n\n"
            + (result or "").strip()
        )
        for path in list(dict.fromkeys(written))[:2]:
            try:
                if os.path.isfile(path):
                    # Read the WHOLE file (capped): a 1.8k window cut off the end of a
                    # 2.1k file and made a COMPLETE file look "truncated" → false FAIL.
                    with open(path, encoding="utf-8", errors="replace") as f:
                        body = f.read(8000)
                    evidence += f"\n\n--- file written: {path} ---\n{body}"
            except OSError:
                continue
        try:
            return await verify.check_task_completion(
                self.planner, task_description, evidence, max_chars=16000
            )
        except Exception:  # noqa: BLE001 - a judging hiccup must never block a step
            logger.exception("semantic check crashed; treating as pass")
            return True, ""

    async def _synthesize_deliverable(self, goal: str) -> str:
        """Write the FINAL deliverable from the completed step results — the actual
        report/answer the goal asked for, not just a "steps done" status line.

        This is what turns an audit/analysis/research goal into the report the user
        wanted (the step results hold the evidence; here we synthesize them)."""
        done = [
            (t.description, (t.result or "").strip())
            for t in self.task_manager.state.plan
            if t.status.value == "completed" and (t.result or "").strip()
        ]
        if not done:
            return ""
        evidence = "\n\n".join(f"### Step: {d}\n{r[:1800]}" for d, r in done)
        evidence = evidence[: config.RESEARCH_SYNTH_MAX_CHARS]
        try:
            resp = await self.planner.chat(
                [
                    {"role": "system", "content": SYNTH_PROMPT},
                    {"role": "user", "content":
                        f"GOAL:\n{goal}\n\nWORK COMPLETED — step results (your evidence):\n{evidence}"},
                ],
                tools=None,
            )
        except OllamaError as exc:
            await self._emit("error", f"final synthesis failed: {exc}")
            return ""
        return (resp.message.content or "").strip()

    async def run_goal(self, user_goal: str) -> str:
        """Plan → execute → SYNTHESIZE the final deliverable. Returns the report
        (or a status string if there was nothing to synthesize)."""
        # If the goal names an existing target directory, operate THERE — not in the
        # default scratch workspace. This is the fix for the verifier checking the
        # wrong directory (every real write looked "missing" → endless replanning).
        target = _infer_working_dir(user_goal)
        if target and os.path.normpath(target) != os.path.normpath(
            self.task_manager.state.memory.cwd or ""
        ):
            self.task_manager.set_cwd(target)
            await self._emit("workdir", f"operating in the directory named by the goal: {target}")
        if not await self.plan_goal(user_goal):
            return (
                "⚠️ I couldn't break this into a plan — the planner model returned an "
                "unparseable response (this can happen on a cold start or with a very "
                "long goal). Please try again, or shorten/scope the goal."
            )
        status = await self.execute_plan()
        await self._emit("synthesizing", "writing the final deliverable")
        report = await self._synthesize_deliverable(user_goal)
        if not report:
            return status
        # Prepend a one-line note if the run only partially succeeded.
        if not self.task_manager.all_succeeded():
            return f"_⚠️ {status}_\n\n{report}"
        return report
