"""Scheduler tools — let Zero schedule tasks for later execution.

Zero can schedule any prompt to run at a specific time or on a recurring cron
pattern. Scheduled tasks are persisted to disk and survive restarts.

Examples Zero can handle:
    "תזמן כל בוקר ב-09:00 לתת לי חדשות ספורט"
    "הרץ את הפרק 42 הלילה ב-22:00"
    "כל שני ב-08:30 שלח לי סיכום שוק ההון"
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta

from orchestrator.tools.registry import registry
from orchestrator import scheduler as _sched

logger = logging.getLogger("zero_agent.scheduler_tools")


def _parse_time_expression(time_expr: str) -> tuple[str, str | None, datetime | None]:
    """Parse a natural-language-ish time expression into (type, cron, run_at).

    Accepts:
        "כל בוקר 09:00"       → cron  "0 9 * * *"
        "כל יום 22:00"        → cron  "0 22 * * *"
        "כל שני 08:30"        → cron  "30 8 * * 1"
        "כל ראשון 07:00"      → cron  "0 7 * * 0"
        "כל שישי 14:30"       → cron  "30 14 * * 5"
        "כל שבת 10:00"        → cron  "0 10 * * 6"
        "כל שעה"              → cron  "0 * * * *"
        "כל 30 דקות"          → cron  "*/30 * * * *"
        "היום 20:00"          → once  (today at 20:00 local)
        "מחר 08:00"           → once  (tomorrow at 08:00 local)
        "2026-07-01 09:00"    → once  (specific date)
        "* * * * *" (raw cron)→ cron  (passed through)
    Returns (type, cron_expr_or_None, run_at_or_None).
    """
    expr = time_expr.strip()

    # Raw cron passthrough: 5 fields separated by spaces where first field is
    # a digit, *, or /
    parts = expr.split()
    if len(parts) == 5 and all(p[0] in "0123456789*/" for p in parts):
        return "cron", expr, None

    # Hebrew day-of-week map
    _dow = {"ראשון": "0", "שני": "1", "שלישי": "2", "רביעי": "3",
            "חמישי": "4", "שישי": "5", "שבת": "6"}

    lower = expr.lower().replace(":", " ")

    # "כל X דקות"
    if "דקות" in lower or "דקה" in lower:
        for tok in parts:
            if tok.isdigit():
                return "cron", f"*/{tok} * * * *", None

    # "כל שעה"
    if "שעה" in lower or "שעות" in lower:
        return "cron", "0 * * * *", None

    # "כל <day> HH:MM" or "כל <day>"
    for heb, dow_num in _dow.items():
        if heb in expr:
            hh, mm = _extract_hhmm(expr)
            return "cron", f"{mm} {hh} * * {dow_num}", None

    # "כל בוקר" / "כל יום" / "כל ערב" with optional time
    hh, mm = _extract_hhmm(expr)
    if "בוקר" in lower:
        hh = hh if hh != "9" else "9"
        return "cron", f"{mm} {hh} * * *", None
    if "ערב" in lower:
        hh = hh if hh != "9" else "20"
        return "cron", f"{mm} {hh} * * *", None
    if "יום" in lower or "daily" in lower:
        return "cron", f"{mm} {hh} * * *", None

    # "היום HH:MM"
    if "היום" in lower or "today" in lower:
        hh_i, mm_i = int(hh), int(mm)
        now = datetime.now()
        run_at = now.replace(hour=hh_i, minute=mm_i, second=0, microsecond=0)
        if run_at < now:
            run_at += timedelta(days=1)
        return "once", None, run_at

    # "מחר HH:MM"
    if "מחר" in lower or "tomorrow" in lower:
        hh_i, mm_i = int(hh), int(mm)
        run_at = (datetime.now() + timedelta(days=1)).replace(
            hour=hh_i, minute=mm_i, second=0, microsecond=0)
        return "once", None, run_at

    # ISO date "YYYY-MM-DD HH:MM"
    try:
        run_at = datetime.strptime(expr[:16], "%Y-%m-%d %H:%M")
        return "once", None, run_at
    except ValueError:
        pass

    # Fallback: treat as cron with the raw string
    return "cron", expr, None


def _extract_hhmm(expr: str) -> tuple[str, str]:
    """Extract HH MM from a string. Returns ("9", "0") as default."""
    import re
    m = re.search(r"(\d{1,2})[:\s](\d{2})", expr)
    if m:
        return m.group(1), m.group(2)
    m2 = re.search(r"\b(\d{1,2})\b", expr)
    if m2:
        return m2.group(1), "0"
    return "9", "0"


# ── Registered tools ──────────────────────────────────────────────────────────

@registry.register
async def schedule_task(
    task_prompt: str,
    schedule: str,
    label: str = "",
) -> str:
    """Schedule a Zero Agent task to run at a specific time or on a recurring schedule.

    Zero will run `task_prompt` through the full agent pipeline (web search, tools,
    analysis — everything) at the scheduled time and broadcast the result to the UI.

    Args:
        task_prompt: The exact prompt Zero should run at the scheduled time.
            E.g. "חפש חדשות ספורט של היום וסכם בעברית".
        schedule: When to run. Accepts natural Hebrew or English expressions:
            - "כל בוקר 09:00"       → daily at 09:00
            - "כל שני 08:30"        → every Monday at 08:30
            - "היום 22:00"          → today at 22:00 (one-time)
            - "מחר 08:00"           → tomorrow at 08:00 (one-time)
            - "כל 30 דקות"         → every 30 minutes
            - "0 9 * * 1-5"        → raw cron (weekdays 09:00)
        label: Short human-readable name for this task (shown in /scheduled list).
    """
    try:
        kind, cron_expr, run_at = _parse_time_expression(schedule)
    except Exception as exc:
        return f"Error parsing schedule '{schedule}': {exc}. Try formats like 'כל בוקר 09:00' or 'היום 22:00'."

    try:
        if kind == "cron" and cron_expr:
            task_id = _sched.schedule_cron(task_prompt, cron_expr, label=label)
            human = f"recurring ({cron_expr})"
        elif kind == "once" and run_at:
            task_id = _sched.schedule_once(task_prompt, run_at, label=label)
            human = f"once at {run_at.strftime('%Y-%m-%d %H:%M')}"
        else:
            return f"Could not parse schedule '{schedule}'. Try 'כל בוקר 09:00' or 'מחר 22:00'."
    except Exception as exc:
        return f"Error creating scheduled task: {exc}"

    return (
        f"✅ Task scheduled — id: `{task_id}`\n"
        f"Schedule: {human}\n"
        f"Label: {label or task_prompt[:50]}\n"
        f"Prompt: {task_prompt[:100]}\n"
        "Use list_scheduled_tasks() or /scheduled to see all tasks."
    )


@registry.register
async def list_scheduled_tasks() -> str:
    """List all active scheduled tasks with their next run time.

    Shows task id, label, schedule type, cron expression or run time,
    and the next scheduled execution time.
    """
    tasks = _sched.list_tasks()
    if not tasks:
        return "No scheduled tasks. Use schedule_task() to add one."

    lines = ["**Scheduled Tasks**\n"]
    for t in tasks:
        status = "✅ active" if t.get("active") else "⚠️ not in scheduler"
        schedule = t.get("cron") or t.get("run_at", "?")
        next_run = t.get("next_run", "?")
        last = t.get("last_fired") or "never"
        lines.append(
            f"• `{t['id']}` — {t.get('label','(no label)')}\n"
            f"  Schedule: {schedule} | Next: {next_run} | Last ran: {last} | {status}\n"
            f"  Prompt: {t.get('prompt','')[:80]}"
        )
    return "\n".join(lines)


@registry.register
async def cancel_scheduled_task(task_id: str) -> str:
    """Cancel and remove a scheduled task by its id.

    Args:
        task_id: The task id shown in list_scheduled_tasks() or /scheduled.
    """
    removed = _sched.cancel_task(task_id.strip())
    if removed:
        return f"✅ Task `{task_id}` cancelled and removed."
    return f"Task `{task_id}` not found. Check list_scheduled_tasks() for valid ids."
