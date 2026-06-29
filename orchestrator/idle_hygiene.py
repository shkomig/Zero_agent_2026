"""Idle-triggered memory hygiene (ROADMAP Tier-2 #3, the idle-trigger sub-step).

`/hygiene` is manual; this runs the same consolidation pass AUTOMATICALLY when the
machine is actually unused, so memory stays tidy without the user remembering to ask.
"Idle" is REAL OS input idle — time since the last keyboard/mouse event — read via
Win32 ``GetLastInputInfo``. On any non-Windows platform (or if the call fails) the
trigger self-disables; nothing else changes.

The gating decision is a PURE function (`_should_run`) so it's tested without a clock
or an OS. The watcher is a single background asyncio task started once from the UI;
it never raises into the app and is rate-limited so it won't churn while you're away.
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import sys
import time
from typing import Any

from orchestrator import config, memory_hygiene, self_improve

logger = logging.getLogger("zero_agent.idle_hygiene")


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def idle_seconds() -> float | None:
    """Seconds since the last keyboard/mouse input, or ``None`` if unsupported.

    Windows only (``GetLastInputInfo`` + ``GetTickCount64``). Returns ``None`` on
    other platforms or on any API failure — the caller treats ``None`` as "can't
    tell → don't trigger", so the feature simply stays dormant.
    """
    if sys.platform != "win32":
        return None
    try:
        info = _LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        ctypes.windll.kernel32.GetTickCount64.restype = ctypes.c_ulonglong
        now_ms = ctypes.windll.kernel32.GetTickCount64()
        return max(0.0, (now_ms - info.dwTime) / 1000.0)
    except Exception:  # noqa: BLE001 - never let a probe crash the watcher
        logger.exception("idle_seconds probe failed")
        return None


def _should_run(
    idle: float | None, last_run: float, now: float, idle_threshold: int, min_gap: int
) -> bool:
    """Pure gate: run only when the machine is idle past the threshold AND we haven't
    run in the last ``min_gap`` seconds. ``idle is None`` (unsupported) → never run."""
    if idle is None:
        return False
    if idle < idle_threshold:
        return False
    if now - last_run < min_gap:
        return False
    return True


_task: asyncio.Task | None = None


async def _watch(store: Any, judge: memory_hygiene.ContradictionJudge, client: Any = None) -> None:
    """Poll the idle time; when idle, run memory consolidation + self-improvement."""
    last_run = 0.0
    while True:
        await asyncio.sleep(max(5, config.MEMORY_IDLE_CHECK_INTERVAL))
        try:
            now = time.time()
            if not _should_run(
                idle_seconds(), last_run, now,
                config.MEMORY_IDLE_SECONDS, config.MEMORY_IDLE_MIN_GAP,
            ):
                continue
            r = await asyncio.to_thread(memory_hygiene.consolidate, store, judge=judge)
            last_run = now
            removed = (
                r.get("removed_duplicates", 0)
                + r.get("removed_expired", 0)
                + r.get("removed_contradictions", 0)
            )
            if removed:
                logger.info(
                    "idle hygiene: examined=%d dedup=%d decayed=%d contradictions=%d remaining=%d",
                    r.get("examined", 0), r.get("removed_duplicates", 0),
                    r.get("removed_expired", 0), r.get("removed_contradictions", 0),
                    r.get("remaining", 0),
                )
            # Self-improvement pass — runs after memory is clean; client may be
            # None on first start (before a chat session provides one).
            if client is not None:
                await self_improve.maybe_run_idle(store, client)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the watcher must outlive any single hiccup
            logger.exception("idle hygiene watcher iteration failed")


def start_idle_watcher(store: Any, client: Any = None) -> asyncio.Task | None:
    """Start the background idle-hygiene watcher ONCE. Returns the task, or ``None``
    when disabled by config or unsupported on this platform (so callers can no-op).

    Safe to call repeatedly (e.g. on every chat start) — it won't spawn a second
    watcher. Requires a running event loop (called from the async UI). The judge uses
    the dedicated `MEMORY_CONTRADICTION_MODEL`, not the active chat model."""
    global _task
    if not config.MEMORY_IDLE_TRIGGER:
        return None
    if _task is not None and not _task.done():
        return _task
    if idle_seconds() is None:
        logger.info("idle hygiene disabled (no OS idle support on this platform)")
        return None
    judge = memory_hygiene.make_contradiction_judge()
    _task = asyncio.create_task(_watch(store, judge, client=client))
    logger.info(
        "idle hygiene watcher started (idle>=%ds, every %ds, min gap %ds)",
        config.MEMORY_IDLE_SECONDS, config.MEMORY_IDLE_CHECK_INTERVAL,
        config.MEMORY_IDLE_MIN_GAP,
    )
    return _task
