"""Persistent task state for the Supervisor-Worker loop.

The single-agent loop kept the whole plan, every tool result, and all history in
ONE growing conversation — which overflows a local model's context window after a
few steps and makes it "forget" the goal. The TaskManager fixes that by keeping
the plan + working memory as a small JSON file on disk. The Worker then receives a
short, focused context per atomic task instead of the entire transcript, and a
multi-step goal survives a crash/restart (resume from `data/tasks/<run>.json`).

This module is pure state — no LLM calls, no I/O beyond the JSON file. The
Supervisor drives it; the Worker (BaseAgent) only reads the context string.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from enum import Enum

from pydantic import BaseModel, Field

from orchestrator import config


class TaskStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


class Task(BaseModel):
    """One atomic step in the plan."""

    id: str
    description: str
    status: TaskStatus = TaskStatus.PENDING
    result: str | None = None
    #: Root-cause analysis written by the verifier/supervisor on failure — fed
    #: back into the next attempt so the Worker learns instead of repeating.
    root_cause: str | None = None
    attempts: int = 0


class WorkingMemory(BaseModel):
    """The small, always-current "where am I" scratchpad — NOT the transcript."""

    cwd: str = ""
    current_focus: str | None = None
    #: Durable lessons distilled from failures during THIS run.
    lessons_learned: list[str] = Field(default_factory=list)
    #: Free-form facts the plan produced that later steps need (paths, names…).
    artifacts: dict[str, str] = Field(default_factory=dict)


class TaskState(BaseModel):
    run_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    goal: str = ""
    plan: list[Task] = Field(default_factory=list)
    memory: WorkingMemory = Field(default_factory=WorkingMemory)
    replans: int = 0
    created: float = Field(default_factory=time.time)
    updated: float = Field(default_factory=time.time)


class TaskManager:
    """Owns one :class:`TaskState`, persisted to ``data/tasks/<run_id>.json``."""

    def __init__(self, state_file: str | None = None, *, cwd: str = "") -> None:
        os.makedirs(config.TASKS_DIR, exist_ok=True)
        self.state_file = state_file or ""
        self.state = self._load_state()
        if cwd:
            self.state.memory.cwd = cwd

    # --- persistence --------------------------------------------------------
    def _load_state(self) -> TaskState:
        if self.state_file and os.path.exists(self.state_file):
            try:
                with open(self.state_file, encoding="utf-8") as f:
                    return TaskState(**json.load(f))
            except (OSError, json.JSONDecodeError, ValueError):
                pass  # corrupt/partial state → start fresh rather than crash
        return TaskState()

    def save(self) -> None:
        self.state.updated = time.time()
        if not self.state_file:
            self.state_file = os.path.join(
                config.TASKS_DIR, f"{self.state.run_id}.json"
            )
        with open(self.state_file, "w", encoding="utf-8") as f:
            f.write(self.state.model_dump_json(indent=2))

    # --- planning -----------------------------------------------------------
    def set_plan(self, goal: str, plan_tasks: list[str]) -> None:
        self.state.goal = goal
        self.state.plan = [
            Task(id=str(i + 1), description=desc.strip())
            for i, desc in enumerate(plan_tasks)
            if desc and desc.strip()
        ]
        self.save()

    def replan_remaining(self, new_tasks: list[str]) -> None:
        """Replace only the not-yet-completed steps (after a failure) and keep a
        stable id space so completed work is never re-run."""
        kept = [t for t in self.state.plan if t.status == TaskStatus.COMPLETED]
        start = len(kept)
        fresh = [
            Task(id=str(start + i + 1), description=desc.strip())
            for i, desc in enumerate(new_tasks)
            if desc and desc.strip()
        ]
        self.state.plan = kept + fresh
        self.state.replans += 1
        self.save()

    # --- execution flow -----------------------------------------------------
    def get_next_task(self) -> Task | None:
        for task in self.state.plan:
            if task.status == TaskStatus.PENDING:
                task.status = TaskStatus.IN_PROGRESS
                task.attempts += 1
                self.state.memory.current_focus = task.description
                self.save()
                return task
        return None

    def mark_completed(self, task_id: str, result: str) -> None:
        for task in self.state.plan:
            if task.id == task_id:
                task.status = TaskStatus.COMPLETED
                task.result = result
                break
        self.state.memory.current_focus = None
        self.save()

    def mark_failed(self, task_id: str, root_cause: str) -> None:
        for task in self.state.plan:
            if task.id == task_id:
                task.status = TaskStatus.FAILED
                task.root_cause = root_cause
                self.state.memory.lessons_learned.append(
                    f"Task {task_id} ('{task.description}') failed: {root_cause}"
                )
                break
        self.save()

    def reset_task_to_pending(self, task_id: str) -> None:
        """Send a failed/in-progress task back to the queue for a retry."""
        for task in self.state.plan:
            if task.id == task_id:
                task.status = TaskStatus.PENDING
                break
        self.save()

    def record_artifact(self, key: str, value: str) -> None:
        self.state.memory.artifacts[key] = value
        self.save()

    # --- queries ------------------------------------------------------------
    def is_done(self) -> bool:
        """True when no task is still pending/in-progress (success OR exhausted)."""
        return all(
            t.status in (TaskStatus.COMPLETED, TaskStatus.FAILED)
            for t in self.state.plan
        )

    def all_succeeded(self) -> bool:
        return bool(self.state.plan) and all(
            t.status == TaskStatus.COMPLETED for t in self.state.plan
        )

    def get_context_for_worker(self) -> str:
        """A SHORT briefing for the Worker — the whole point of this class.

        Instead of the full transcript, the Worker gets the goal, where to work,
        what's already done, any artifacts it needs, and lessons from failures.
        """
        completed = [
            f"  [done] {t.description}"
            for t in self.state.plan
            if t.status == TaskStatus.COMPLETED
        ]
        lines = [f"GOAL: {self.state.goal}"]
        if self.state.memory.cwd:
            lines.append(f"Working directory: {self.state.memory.cwd}")
        if completed:
            lines.append("Already completed:\n" + "\n".join(completed))
        if self.state.memory.artifacts:
            arts = ", ".join(
                f"{k}={v}" for k, v in self.state.memory.artifacts.items()
            )
            lines.append(f"Known artifacts: {arts}")
        if self.state.memory.lessons_learned:
            lines.append(
                "Lessons from earlier failures (do NOT repeat):\n"
                + "\n".join(f"  - {x}" for x in self.state.memory.lessons_learned[-5:])
            )
        return "\n".join(lines)
