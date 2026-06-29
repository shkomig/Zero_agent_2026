"""Session-context tools: let Zero persist and recall its working task state.

Three tools the agent calls explicitly:
  update_task_state — write the current task/step to session_state.json
  log_task_action   — append one completion entry to the task journal
  get_task_status   — read back the current session state (for a /status command)

The read+inject side (what happens at session START) is handled by session_context.py
and wired in app.py — these tools are purely the WRITE side.
"""

from __future__ import annotations

from orchestrator import session_context as sc
from orchestrator.tools.registry import registry


@registry.register
async def update_task_state(
    task: str,
    step: str = "",
    next_step: str = "",
    notes: str = "",
) -> str:
    """Persist the current task and step so Zero remembers context across sessions.

    Call this when you start a significant new task, complete a phase, or switch
    focus — so the next session picks up exactly where this one left off.

    Args:
        task: Short description of the overall task / project (e.g. "CyberLab Phase 3").
        step: The current step being worked on (e.g. "VMnet network ranges config").
        next_step: The next pending step after this one completes.
        notes: Any important notes, blockers, or decisions worth preserving.
    """
    sc.save_session_state(task=task, step=step, next_step=next_step, notes=notes)
    parts = [f"Task context saved: **{task}**"]
    if step:
        parts.append(f"Current step: {step}")
    if next_step:
        parts.append(f"Next step: {next_step}")
    return "\n".join(parts)


@registry.register
async def log_task_action(action: str) -> str:
    """Append a completed-action entry to the persistent task journal.

    Call this after completing a meaningful step (file written, phase done,
    decision made). The last 20 entries are shown at the start of every session.

    Args:
        action: One-line description of what was done, e.g.
                "CyberLab Phase 2: VM_Design.md written to C:\\Labs\\CyberLab\\Architecture\\ ✓"
    """
    sc.append_journal(action)
    return f"Logged: {action}"


@registry.register
async def get_task_status() -> str:
    """Return the current session state and the last 10 journal entries.

    Use this at the start of a resumed task to orient yourself on where things
    stand before taking action.
    """
    state = sc.load_session_state()
    journal = sc._load_journal_tail(10)

    lines: list[str] = []

    if state.get("active_task"):
        lines.append(f"**Active task:** {state['active_task']}")
        if state.get("current_step"):
            lines.append(f"**Current step:** {state['current_step']}")
        if state.get("next_step"):
            lines.append(f"**Next step:** {state['next_step']}")
        if state.get("notes"):
            lines.append(f"**Notes:** {state['notes']}")
        if state.get("updated_at"):
            lines.append(f"**Last updated:** {state['updated_at']}")
    else:
        lines.append("No active task recorded.")

    if journal:
        lines.append("\n**Recent actions:**")
        lines.extend(f"  {e}" for e in journal)

    return "\n".join(lines) if lines else "No session context found."
