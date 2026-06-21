"""Knowledge-graph tool — run graphify on ANY local project.

Lets the agent turn a folder of code (and optionally docs/images) into a
navigable knowledge graph: `graph.html` (interactive), `GRAPH_REPORT.md` (god
nodes / communities / suggested questions), and `graph.json`. 100% local — code
mode is pure AST (no LLM); full mode drives the project's OWN local Ollama via
graphify's built-in `ollama` backend (no cloud, no API keys).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

from orchestrator.tools.registry import registry

logger = logging.getLogger("zero_agent.graph_tools")

# Interpreters likely to have `graphify` installed, tried in order. The first
# that can `import graphify` wins; result is cached for the process.
_PY_CANDIDATES = (
    r"C:\AI\Miniforge3\python.exe",
    sys.executable,
)
_GRAPHIFY_PY: str | None = None

# graphify writes its output into a folder inside the project. Newer versions use
# `_docs_and_graph`; older ones used `graphify-out`. Prefer whichever already
# exists; otherwise default to the current name so a fresh build lands there.
_OUT_NAMES = ("_docs_and_graph", "graphify-out")


def _out_dir(proj: Path) -> Path:
    for name in _OUT_NAMES:
        if (proj / name).is_dir():
            return proj / name
    return proj / _OUT_NAMES[0]


def _find_graphify_python() -> str | None:
    global _GRAPHIFY_PY
    if _GRAPHIFY_PY:
        return _GRAPHIFY_PY
    candidates = list(_PY_CANDIDATES)
    which = shutil.which("python")
    if which:
        candidates.append(which)
    for cand in candidates:
        if cand and Path(cand).exists():
            try:
                r = subprocess.run(
                    [cand, "-c", "import graphify"],
                    capture_output=True, timeout=25,
                )
                if r.returncode == 0:
                    _GRAPHIFY_PY = cand
                    return cand
            except (OSError, subprocess.SubprocessError):
                continue
    return None


@registry.register
def graphify_project(path: str, mode: str = "code", timeout_s: int = 0) -> str:
    """Build a navigable knowledge graph of a LOCAL project folder with graphify.

    Produces graph.html (interactive), GRAPH_REPORT.md (god nodes, communities,
    suggested questions), and graph.json inside ``<path>/_docs_and_graph/``. Runs
    100% locally. Use this to map the architecture of THIS project or any other
    local project on the machine. Call this whenever the user asks to "update the
    graph / docs of folder X", "graphify X", or "refresh the knowledge graph of
    X" — ``path`` is that folder; ``update`` is incremental (only changed files).

    Args:
        path: Absolute path to the project folder to analyze.
        mode: "code" (fast, AST only, no LLM — default and recommended) or
            "full" (also extracts docs/papers/images via the local Ollama
            backend — thorough but much slower and model-limited).
        timeout_s: Max seconds to allow (0 = sensible default: 180 for code,
            1200 for full). If it times out, partial output may still be written.
    """
    proj = Path(path).expanduser()
    if not proj.is_dir():
        return f"Error: not a directory: {proj}"

    py = _find_graphify_python()
    if not py:
        return (
            "Error: graphify is not installed in any reachable Python. Install it "
            "with `pip install graphifyy` (then it is importable as `graphify`)."
        )

    env = dict(os.environ)
    mode = (mode or "code").lower()
    if mode == "full":
        # graphify's built-in OpenAI-compatible Ollama backend — fully local.
        env.setdefault("OLLAMA_BASE_URL", "http://localhost:11434/v1")
        env.setdefault("OLLAMA_MODEL", "qwen3-coder-30b")
        cmds = [
            [py, "-m", "graphify", "extract", str(proj), "--backend", "ollama"],
            [py, "-m", "graphify", "cluster-only", str(proj), "--backend", "ollama"],
        ]
        timeout = timeout_s or 1200
    else:
        cmds = [[py, "-m", "graphify", "update", str(proj)]]
        timeout = timeout_s or 180

    captured: list[str] = []
    for cmd in cmds:
        logger.info("graphify_project: %s", " ".join(cmd))
        try:
            r = subprocess.run(
                cmd, cwd=str(proj), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=timeout, env=env,
            )
        except subprocess.TimeoutExpired:
            return (
                f"graphify ({mode}) timed out after {timeout}s on {proj}. "
                "Full/semantic mode on a local model is slow — try mode='code', a "
                "smaller folder, or a larger timeout_s. Partial output may exist in "
                f"{_out_dir(proj)}."
            )
        except OSError as exc:
            return f"Error launching graphify: {type(exc).__name__}: {exc}"
        captured.append((r.stdout or "").strip())
        if r.returncode != 0:
            return (
                f"graphify ({mode}) failed (exit {r.returncode}):\n"
                + (r.stderr or r.stdout or "")[-900:]
            )

    outdir = _out_dir(proj)
    html = outdir / "graph.html"
    if not html.exists():
        return (
            f"graphify ran but no graph.html was produced in {outdir}. Output:\n"
            + "\n".join(captured)[-900:]
        )
    summary = "\n".join(captured)[-1200:]
    return (
        f"Knowledge graph built for {proj} (mode={mode}).\n"
        f"  Interactive graph : {html}\n"
        f"  Report            : {outdir / 'GRAPH_REPORT.md'}\n"
        f"  Raw graph         : {outdir / 'graph.json'}\n\n"
        f"{summary}"
    )
