"""Task scheduler — run Zero Agent tasks at a scheduled time or on a cron pattern.

Uses APScheduler (AsyncIOScheduler) with a JSON persistence file so schedules
survive restarts. When a task fires, it runs through the normal BaseAgent pipeline
and broadcasts the result to all connected Chainlit WebSocket clients.

Lifecycle:
    scheduler.start()  — called once in app.py on_chat_start / lifespan
    scheduler.shutdown()  — called on app shutdown

Storage: data/scheduler/tasks.json  (append-only log + active task index)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

logger = logging.getLogger("zero_agent.scheduler")

_DATA_DIR = Path(__file__).parent.parent / "data" / "scheduler"
_TASKS_FILE = _DATA_DIR / "tasks.json"

# Will be set by app.py after the agent/broadcast are available.
_run_task_fn: Callable[[str], Any] | None = None

_scheduler: AsyncIOScheduler | None = None


# ── Persistence ───────────────────────────────────────────────────────────────

def _load_tasks() -> dict[str, dict]:
    if _TASKS_FILE.exists():
        try:
            return json.loads(_TASKS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_tasks(tasks: dict[str, dict]) -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    _TASKS_FILE.write_text(json.dumps(tasks, ensure_ascii=False, indent=2), encoding="utf-8")


# ── Core ──────────────────────────────────────────────────────────────────────

def get_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler(timezone="Asia/Jerusalem")
    return _scheduler


def set_task_runner(fn: Callable[[str], Any]) -> None:
    """Inject the async function that runs a prompt through Zero Agent.

    Called from app.py so the scheduler module doesn't import app-level things.
    fn signature: async def run(prompt: str) -> None
    """
    global _run_task_fn
    _run_task_fn = fn


async def _fire_task(task_id: str, prompt: str) -> None:
    """Callback invoked by APScheduler when a task fires."""
    logger.info("scheduler firing task %s: %r", task_id, prompt[:80])
    tasks = _load_tasks()
    model = ""
    if task_id in tasks:
        tasks[task_id]["last_fired"] = datetime.now(timezone.utc).isoformat()
        model = tasks[task_id].get("model", "")
        _save_tasks(tasks)

    if _run_task_fn is not None:
        try:
            import inspect
            sig = inspect.signature(_run_task_fn)
            if "model" in sig.parameters:
                await _run_task_fn(f"[Scheduled task {task_id}] {prompt}", model=model)
            else:
                await _run_task_fn(f"[Scheduled task {task_id}] {prompt}")
        except Exception:
            logger.exception("scheduled task %s raised an exception", task_id)
    else:
        logger.warning("task %s fired but no task runner is set", task_id)


# ── Public API ────────────────────────────────────────────────────────────────

def schedule_once(prompt: str, run_at: datetime, label: str = "") -> str:
    """Schedule a task to run once at a specific datetime. Returns the task id."""
    sched = get_scheduler()
    task_id = uuid.uuid4().hex[:8]
    sched.add_job(
        _fire_task,
        trigger=DateTrigger(run_date=run_at),
        args=[task_id, prompt],
        id=task_id,
        name=label or prompt[:40],
        replace_existing=True,
        misfire_grace_time=300,
    )
    tasks = _load_tasks()
    tasks[task_id] = {
        "id": task_id,
        "prompt": prompt,
        "label": label or prompt[:40],
        "type": "once",
        "run_at": run_at.isoformat(),
        "cron": None,
        "created": datetime.now(timezone.utc).isoformat(),
        "last_fired": None,
    }
    _save_tasks(tasks)
    logger.info("scheduled once task %s at %s", task_id, run_at)
    return task_id


def schedule_cron(prompt: str, cron_expr: str, label: str = "") -> str:
    """Schedule a recurring task using a cron expression. Returns the task id.

    cron_expr examples:
        "0 9 * * *"    — every day at 09:00
        "0 22 * * 1-5" — weekdays at 22:00
        "*/30 * * * *" — every 30 minutes
    """
    sched = get_scheduler()
    task_id = uuid.uuid4().hex[:8]
    minute, hour, day, month, day_of_week = (cron_expr.split() + ["*"] * 5)[:5]
    trigger = CronTrigger(
        minute=minute, hour=hour, day=day, month=month,
        day_of_week=day_of_week, timezone="Asia/Jerusalem",
    )
    sched.add_job(
        _fire_task,
        trigger=trigger,
        args=[task_id, prompt],
        id=task_id,
        name=label or prompt[:40],
        replace_existing=True,
        misfire_grace_time=300,
    )
    tasks = _load_tasks()
    tasks[task_id] = {
        "id": task_id,
        "prompt": prompt,
        "label": label or prompt[:40],
        "type": "cron",
        "run_at": None,
        "cron": cron_expr,
        "created": datetime.now(timezone.utc).isoformat(),
        "last_fired": None,
    }
    _save_tasks(tasks)
    logger.info("scheduled cron task %s expr=%s", task_id, cron_expr)
    return task_id


def cancel_task(task_id: str) -> bool:
    """Cancel a scheduled task. Returns True if found and removed."""
    sched = get_scheduler()
    job = sched.get_job(task_id)
    if job:
        job.remove()
    tasks = _load_tasks()
    if task_id in tasks:
        del tasks[task_id]
        _save_tasks(tasks)
        return True
    return bool(job)


def list_tasks() -> list[dict]:
    """Return all active scheduled tasks."""
    tasks = _load_tasks()
    sched = get_scheduler()
    result = []
    for tid, t in tasks.items():
        job = sched.get_job(tid)
        next_run = None
        if job and job.next_run_time:
            next_run = job.next_run_time.isoformat()
        result.append({**t, "next_run": next_run, "active": job is not None})
    return result


def restore_from_disk() -> int:
    """Re-register all persisted tasks into APScheduler after a restart."""
    tasks = _load_tasks()
    restored = 0
    sched = get_scheduler()
    now = datetime.now(timezone.utc)
    for tid, t in tasks.items():
        if sched.get_job(tid):
            continue  # already registered
        try:
            if t["type"] == "cron" and t.get("cron"):
                minute, hour, day, month, dow = (t["cron"].split() + ["*"] * 5)[:5]
                trigger = CronTrigger(
                    minute=minute, hour=hour, day=day, month=month,
                    day_of_week=dow, timezone="Asia/Jerusalem",
                )
                sched.add_job(
                    _fire_task, trigger=trigger, args=[tid, t["prompt"]],
                    id=tid, name=t.get("label", ""), replace_existing=True,
                    misfire_grace_time=300,
                )
                restored += 1
            elif t["type"] == "once" and t.get("run_at"):
                run_at = datetime.fromisoformat(t["run_at"])
                if run_at > now:
                    sched.add_job(
                        _fire_task, trigger=DateTrigger(run_date=run_at),
                        args=[tid, t["prompt"]], id=tid, name=t.get("label", ""),
                        replace_existing=True, misfire_grace_time=300,
                    )
                    restored += 1
                else:
                    # One-time task already past — remove from store
                    del tasks[tid]
        except Exception:
            logger.exception("failed to restore task %s", tid)
    _save_tasks({k: v for k, v in tasks.items() if k in [j.id for j in sched.get_jobs()]
                 or tasks[k]["type"] == "cron"})
    logger.info("scheduler restored %d tasks from disk", restored)
    return restored


def start() -> None:
    """Start the scheduler and restore persisted tasks."""
    sched = get_scheduler()
    if not sched.running:
        sched.start()
        restore_from_disk()
        logger.info("AsyncIOScheduler started")


def shutdown() -> None:
    sched = get_scheduler()
    if sched.running:
        sched.shutdown(wait=False)
