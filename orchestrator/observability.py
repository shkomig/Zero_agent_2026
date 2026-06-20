"""Observability for the Zero Agent: structured logging, trace IDs, metrics.

Phase 1 of the roadmap. Everything here is additive and dependency-free (stdlib
only). A single :func:`configure_logging` call wires up formatting; every log
record emitted during a run automatically carries the active ``trace_id`` so
lines from one user message can be correlated even when tools interleave.

Usage::

    from orchestrator import observability as obs

    obs.configure_logging()                 # once, at startup
    with obs.run_span() as span:            # once per agent run
        logger.info("doing work")           # line carries span.trace_id
        span.record_tool("read_file", 0.012, ok=True)
    # span.summary() -> dict of metrics for the final log line
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

from orchestrator import config

# --- Trace context ----------------------------------------------------------
# The id of the in-flight run. "-" when no run is active (e.g. startup logs).
_trace_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "zero_agent_trace_id", default="-"
)


def current_trace_id() -> str:
    """Return the trace id of the active run, or ``"-"`` if none."""
    return _trace_id.get()


class _TraceFilter(logging.Filter):
    """Inject the active ``trace_id`` onto every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = _trace_id.get()
        return True


class _KeyValueFormatter(logging.Formatter):
    """Human-readable ``key=value`` formatter with the trace id up front."""

    def format(self, record: logging.LogRecord) -> str:
        ts = time.strftime("%H:%M:%S", time.localtime(record.created))
        trace = getattr(record, "trace_id", "-")
        base = (
            f"{ts} {record.levelname:<5} trace={trace} "
            f"{record.name} | {record.getMessage()}"
        )
        if record.exc_info:
            base = f"{base}\n{self.formatException(record.exc_info)}"
        return base


class _JsonFormatter(logging.Formatter):
    """One JSON object per line — for piping into a log aggregator."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created)),
            "level": record.levelname,
            "trace": getattr(record, "trace_id", "-"),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


_configured = False


def configure_logging(force: bool = False) -> None:
    """Configure root logging for the agent. Safe to call more than once.

    Honors:
      - ``ZERO_AGENT_LOG_LEVEL`` (default ``INFO``)
      - ``ZERO_AGENT_LOG_JSON``  (``1`` for JSON lines, else key=value)
      - ``ZERO_AGENT_LOG_FILE``  (optional path for an extra file sink)
    """
    global _configured
    if _configured and not force:
        return

    level_name = config.LOG_LEVEL.upper()
    level = getattr(logging, level_name, logging.INFO)
    formatter: logging.Formatter = (
        _JsonFormatter() if config.LOG_JSON else _KeyValueFormatter()
    )
    trace_filter = _TraceFilter()

    root = logging.getLogger("zero_agent")
    root.setLevel(level)
    root.handlers.clear()

    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    stream.addFilter(trace_filter)
    root.addHandler(stream)

    if config.LOG_FILE:
        try:
            file_handler = logging.FileHandler(config.LOG_FILE, encoding="utf-8")
            file_handler.setFormatter(formatter)
            file_handler.addFilter(trace_filter)
            root.addHandler(file_handler)
        except OSError:
            root.warning("could not open log file %s", config.LOG_FILE)

    # Don't double-log through the root logger's default handler.
    root.propagate = False
    _configured = True


# --- Per-run metrics --------------------------------------------------------
@dataclass
class ToolStat:
    name: str
    elapsed: float
    ok: bool


@dataclass
class RunSpan:
    """Collects metrics for a single agent run and exposes a summary."""

    trace_id: str
    started: float = field(default_factory=time.perf_counter)
    steps: int = 0
    tokens: int = 0
    tools: list[ToolStat] = field(default_factory=list)

    def record_tool(self, name: str, elapsed: float, ok: bool) -> None:
        self.tools.append(ToolStat(name=name, elapsed=elapsed, ok=ok))

    def add_tokens(self, n: int) -> None:
        self.tokens += n

    @property
    def elapsed(self) -> float:
        return time.perf_counter() - self.started

    def summary(self) -> dict[str, Any]:
        ok = sum(1 for t in self.tools if t.ok)
        failed = len(self.tools) - ok
        return {
            "trace": self.trace_id,
            "steps": self.steps,
            "tool_calls": len(self.tools),
            "tools_failed": failed,
            "tokens": self.tokens,
            "elapsed_s": round(self.elapsed, 2),
        }


@contextmanager
def run_span() -> Iterator[RunSpan]:
    """Open a trace-scoped run: sets a fresh ``trace_id`` for the duration."""
    trace = uuid.uuid4().hex[:8]
    token = _trace_id.set(trace)
    span = RunSpan(trace_id=trace)
    try:
        yield span
    finally:
        _trace_id.reset(token)
