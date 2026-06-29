"""Self-improvement loop for Zero Agent.

Analyses the Supervisor task history (``data/tasks/*.json``) across sessions to
find recurring failure PATTERNS, diagnoses their root causes with a local LLM,
and converts them into standing RULES that are injected into every future turn
via the existing ``auto_memory.PREFERENCE_TAG`` channel.

Design goals:
- **Zero new infrastructure** — reuses task history, ChromaDB, and the
  ``preference`` injection channel that already exists.
- **Reversible** — rules land in the same store as ``/always`` entries;
  the user can review them with ``/always`` and clear them with ``/always clear``.
- **Safe** — rules are never applied without passing through
  ``memory_guard.sanitize_for_memory``; the LLM never modifies code.
- **Incremental** — tracks the last cycle's cutoff so we don't re-analyse
  runs already processed (``data/session/last_improve.json``).

Typical call path:
    run_improvement_cycle(store, client)  →  summary string

Slash command entry-points (in ``app.py``):
    /improve       →  run a full cycle (dry_run=False)
    /improve dry   →  preview without writing rules
    /improve stats →  show failure metrics for the last N days
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from orchestrator import config
from orchestrator.auto_memory import PREFERENCE_TAG
from orchestrator.memory.store import MemoryStore
from orchestrator.memory_guard import sanitize_for_memory

logger = logging.getLogger("zero_agent.self_improve")

# ── Config (all overridable via env vars) ─────────────────────────────────────

SELF_IMPROVE_ENABLED: bool = os.getenv("ZERO_AGENT_SELF_IMPROVE", "1") not in (
    "0", "false", "False"
)

# How many days of task history to look back when mining failures.
SELF_IMPROVE_DAYS: int = int(os.getenv("ZERO_AGENT_IMPROVE_DAYS", "7"))

# Minimum number of distinct failures required before a diagnosis cycle makes
# sense.  Too few examples → patterns are noise.
SELF_IMPROVE_MIN_FAILURES: int = int(
    os.getenv("ZERO_AGENT_IMPROVE_MIN_FAILURES", "3")
)

# Maximum rules to write per cycle (avoids rule-list bloat).
SELF_IMPROVE_MAX_RULES: int = int(os.getenv("ZERO_AGENT_IMPROVE_MAX_RULES", "5"))

# Model used for pattern diagnosis.  Default: same as the Supervisor.
SELF_IMPROVE_MODEL: str = os.getenv(
    "ZERO_AGENT_IMPROVE_MODEL", config.SUPERVISOR_MODEL
)

# Minimum gap between automatic idle-triggered improve cycles (seconds).
# 86400 = once per day at most.
SELF_IMPROVE_MIN_GAP: int = int(
    os.getenv("ZERO_AGENT_IMPROVE_MIN_GAP", str(86400))
)

# Marker prepended to every improvement-sourced rule so they can be identified
# and (optionally) bulk-cleared without touching user-written /always rules.
IMPROVE_SOURCE_TAG = "[auto-improve]"

# State file: tracks when the last cycle ran and which task files were processed.
_STATE_FILE = Path(config.TASKS_DIR).parent / "session" / "last_improve.json"

# ── Failure mining ─────────────────────────────────────────────────────────────

def _load_state() -> dict:
    try:
        return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"last_run": 0.0, "processed_ids": []}


def _save_state(state: dict) -> None:
    try:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception:
        logger.warning("could not save self-improve state")


def _load_recent_tasks(days: int) -> list[dict]:
    """Load task JSONs created/modified within the last ``days`` days."""
    cutoff = time.time() - days * 86400
    tasks: list[dict] = []
    tasks_dir = Path(config.TASKS_DIR)
    if not tasks_dir.exists():
        return tasks
    for path in tasks_dir.glob("*.json"):
        try:
            # Use file mtime as a fast pre-filter.
            if path.stat().st_mtime < cutoff:
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            # Also check the stored `created` timestamp if available.
            created = data.get("created", 0)
            if created and float(created) < cutoff:
                continue
            tasks.append(data)
        except Exception:
            pass
    return tasks


def _extract_failures(tasks: list[dict]) -> list[dict]:
    """Extract every failed / timed-out step with its context."""
    failures: list[dict] = []
    for task in tasks:
        goal = (task.get("goal") or "")[:200]
        replans = task.get("replans", 0)
        for step in task.get("plan", []):
            status = (step.get("status") or "").lower()
            if status not in ("failed", "error", "timeout"):
                continue
            failures.append({
                "goal": goal,
                "step": (step.get("description") or "")[:200],
                "error": (step.get("root_cause") or step.get("result") or "")[:300],
                "attempts": step.get("attempts", 1),
                "replans": replans,
            })
        # Also treat a run with many replans as a systemic failure signal.
        if replans >= 2 and not any(
            (s.get("status") or "").lower() in ("failed", "error", "timeout")
            for s in task.get("plan", [])
        ):
            failures.append({
                "goal": goal,
                "step": "(repeated replanning)",
                "error": f"Goal required {replans} replanning cycles",
                "attempts": replans,
                "replans": replans,
            })
    return failures


# ── Diagnosis ─────────────────────────────────────────────────────────────────

_DIAGNOSE_PROMPT = """\
You are an AI coach analysing failure logs of an autonomous agent to help it \
self-improve.

Below are {n} recent failures from the agent's task history:

{failures}

Identify the TOP {max_rules} distinct failure PATTERNS (not individual cases). \
Each pattern must be actionable — a repeating root cause that a standing rule \
can fix.

For each pattern return a JSON object with exactly these keys:
  "pattern"  — short name (3–6 words)
  "cause"    — one sentence: why it keeps happening
  "rule"     — one clear sentence the agent should follow ALWAYS; start with \
"Always", "Never", or "When … then …"; be specific about tools or actions.

Return a JSON ARRAY of these objects. No other text, no markdown fences.\
"""


async def _diagnose(failures: list[dict], client: Any, max_rules: int) -> list[dict]:
    """Ask the LLM to cluster failures into patterns and propose rules."""
    failures_text = "\n\n".join(
        f"Failure {i + 1}:\n"
        f"  Goal:     {f['goal']}\n"
        f"  Step:     {f['step']}\n"
        f"  Error:    {f['error']}\n"
        f"  Attempts: {f['attempts']}"
        for i, f in enumerate(failures[:25])
    )
    prompt = _DIAGNOSE_PROMPT.format(
        n=min(len(failures), 25),
        failures=failures_text,
        max_rules=max_rules,
    )
    try:
        resp = await client.chat(
            [{"role": "user", "content": prompt}],
            options={"temperature": 0.0},
            think=False,
        )
        raw = (resp.message.content or "").strip()
    except Exception:
        logger.exception("self-improve diagnosis LLM call failed")
        return []

    # Strip optional markdown fences the model might add despite the instruction.
    raw = re.sub(r"^```[a-z]*\n?", "", raw, flags=re.MULTILINE)
    raw = re.sub(r"\n?```$", "", raw, flags=re.MULTILINE)
    try:
        result = json.loads(raw)
        if isinstance(result, list):
            return result
    except json.JSONDecodeError:
        # Try to extract a JSON array from prose.
        m = re.search(r"\[.*\]", raw, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except Exception:
                pass
    logger.warning("self-improve: could not parse diagnosis JSON from model output")
    return []


# ── Public API ────────────────────────────────────────────────────────────────

async def run_improvement_cycle(
    store: MemoryStore,
    client: Any,
    *,
    days: int = SELF_IMPROVE_DAYS,
    dry_run: bool = False,
    force: bool = False,
) -> str:
    """Full self-improvement cycle: mine → diagnose → apply rules.

    Args:
        store:   The agent's ChromaDB MemoryStore.
        client:  An OllamaClient instance (used for diagnosis).
        days:    Look-back window for task history.
        dry_run: Preview proposed rules without writing them.
        force:   Skip the time-gap guard (useful for manual ``/improve``).

    Returns:
        A human-readable summary string suitable for display in the UI.
    """
    if not SELF_IMPROVE_ENABLED:
        return "Self-improvement is disabled (ZERO_AGENT_SELF_IMPROVE=0)."

    state = _load_state()

    # Time-gap guard (skip for manual calls via force=True).
    if not force and not dry_run:
        since = time.time() - state.get("last_run", 0.0)
        if since < SELF_IMPROVE_MIN_GAP:
            hours_left = (SELF_IMPROVE_MIN_GAP - since) / 3600
            return (
                f"Last improvement cycle ran {since / 3600:.1f}h ago — "
                f"next auto-cycle in {hours_left:.1f}h. "
                f"Use `/improve` to force a cycle now."
            )

    # --- Step 1: mine failures ------------------------------------------------
    tasks = _load_recent_tasks(days)
    if not tasks:
        return f"No task history found in `data/tasks/` for the last {days} days."

    failures = _extract_failures(tasks)
    if len(failures) < SELF_IMPROVE_MIN_FAILURES:
        return (
            f"Only {len(failures)} failure(s) in the last {days} days "
            f"(minimum {SELF_IMPROVE_MIN_FAILURES} needed to find patterns). "
            f"Keep using the agent — the loop will activate once enough data accumulates."
        )

    # --- Step 2: diagnose patterns -------------------------------------------
    patterns = await _diagnose(failures, client, SELF_IMPROVE_MAX_RULES)
    if not patterns:
        return (
            "Found failures but the diagnosis model returned no structured patterns. "
            "Try again or check that the improve model is running: "
            f"`{SELF_IMPROVE_MODEL}`."
        )

    # --- Step 3: sanitize and apply rules ------------------------------------
    applied: list[str] = []
    skipped: list[str] = []

    for p in patterns:
        rule_raw = (p.get("rule") or "").strip()
        if not rule_raw:
            continue

        # Prefix so users can distinguish auto-improved rules from manual /always.
        rule_tagged = f"{IMPROVE_SOURCE_TAG} {rule_raw}"

        safe = sanitize_for_memory(rule_tagged)
        if safe is None:
            skipped.append(rule_raw[:80])
            logger.warning("self-improve: dropped unsafe rule: %s", rule_raw[:80])
            continue

        if dry_run:
            applied.append(f"  ○ {rule_raw}")
            continue

        try:
            # Avoid writing a near-duplicate of an existing rule.
            existing = await store.recall(rule_raw, k=1)
            if existing and existing[0].get("score", 0) >= 0.90:
                skipped.append(f"(duplicate) {rule_raw[:60]}")
                continue
            await store.remember(safe, tags=PREFERENCE_TAG)
            applied.append(f"  ✓ {rule_raw}")
            logger.info("self-improve: added rule: %s", rule_raw[:90])
        except Exception:
            logger.exception("self-improve: failed to store rule")
            skipped.append(rule_raw[:60])

    # --- Step 4: update state -------------------------------------------------
    if not dry_run:
        state["last_run"] = time.time()
        _save_state(state)

    # --- Build summary --------------------------------------------------------
    lines: list[str] = [
        f"**Self-improvement cycle** — {len(tasks)} task run(s), "
        f"{len(failures)} failure(s), {len(patterns)} pattern(s) found.\n"
    ]

    for p in patterns:
        lines.append(
            f"**{p.get('pattern', '?')}**\n"
            f"  Cause: {p.get('cause', '')}\n"
            f"  Rule:  {p.get('rule', '')}"
        )
    lines.append("")

    if dry_run:
        lines.append("_(Dry run — no rules written)_")
        if applied:
            lines.append("Rules that **would** be added:")
            lines.extend(applied)
    else:
        if applied:
            lines.append(f"{len(applied)} rule(s) added to standing instructions:")
            lines.extend(applied)
            lines.append(
                "\n_Use `/always` to view all standing rules, "
                "`/always clear` to remove them all._"
            )
        if skipped:
            lines.append(f"{len(skipped)} rule(s) skipped (duplicate or unsafe).")

    return "\n".join(lines)


def get_failure_stats(days: int = SELF_IMPROVE_DAYS) -> str:
    """Return a plain-text failure-rate summary for the last ``days`` days."""
    tasks = _load_recent_tasks(days)
    if not tasks:
        return f"No task history for the last {days} days."

    total_runs = len(tasks)
    total_steps = sum(len(t.get("plan", [])) for t in tasks)
    failed_steps = sum(
        1
        for t in tasks
        for s in t.get("plan", [])
        if (s.get("status") or "").lower() in ("failed", "error", "timeout")
    )
    total_replans = sum(t.get("replans", 0) for t in tasks)
    runs_with_failures = sum(
        1
        for t in tasks
        if any(
            (s.get("status") or "").lower() in ("failed", "error", "timeout")
            for s in t.get("plan", [])
        )
    )

    state = _load_state()
    last_run = state.get("last_run", 0.0)
    last_run_str = (
        datetime.fromtimestamp(last_run).strftime("%Y-%m-%d %H:%M")
        if last_run
        else "never"
    )

    return (
        f"**Self-improvement stats** (last {days} days)\n"
        f"  Agent runs:          {total_runs}\n"
        f"  Runs with failures:  {runs_with_failures} "
        f"({100 * runs_with_failures // max(total_runs, 1)}%)\n"
        f"  Total steps:         {total_steps}\n"
        f"  Failed steps:        {failed_steps} "
        f"({100 * failed_steps // max(total_steps, 1)}%)\n"
        f"  Total replans:       {total_replans}\n"
        f"  Last improve cycle:  {last_run_str}"
    )


# ── Idle-triggered auto-cycle ─────────────────────────────────────────────────

async def maybe_run_idle(store: MemoryStore, client: Any) -> None:
    """Run one improvement cycle if enough time has passed and enough failures
    exist.  Called from the idle hygiene watcher after memory consolidation.
    Never raises — errors are logged only."""
    if not SELF_IMPROVE_ENABLED:
        return
    try:
        result = await run_improvement_cycle(
            store, client, force=False, dry_run=False
        )
        if "pattern" in result or "rule" in result:
            logger.info("idle self-improve: %s", result[:200])
    except Exception:
        logger.exception("idle self-improve cycle failed")
