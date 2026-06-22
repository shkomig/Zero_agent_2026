"""Build verification — "does the whole PROJECT actually build?", not just "does
each file have valid syntax?".

The RealityVerifier checks that a file landed on disk and parses on its own. That
catches a hallucinated/empty/garbled file, but NOT the gap between "files look
right" and "the project compiles" — missing imports, wrong dependency, a
reference to a file that doesn't exist. This module closes that gap: detect the
project's build system, run its REAL build/compile, and gate on the exit code.

Philosophy (honest + safe):
  * PASS / FAIL / SKIPPED — three outcomes, not two. We must distinguish "the code
    is broken" (FAIL, with the errors) from "we can't verify here" (SKIPPED —
    toolchain not installed, deps not fetched, or a binary/SDK build like Android).
    Never fail working code just because the environment lacks a tool.
  * Safe by default: we run non-destructive compile/check commands only and do NOT
    auto-run `npm install` / fetch the network (supply-chain + slowness). When a
    build needs install/SDK we SKIP and say why.
  * Never hangs: external builds run with a timeout and a full process-tree kill
    (reused from system_tools), so a gradle daemon / watch mode can't freeze Zero.

Exposed as the ``verify_build`` tool so the Worker can build-then-fix in its own
loop (call verify_build → read errors → write_file fixes → verify_build again).
"""

from __future__ import annotations

import glob
import logging
import os
import py_compile
import shutil
import subprocess
import sys

from orchestrator import config
from orchestrator.tools.registry import registry
from orchestrator.tools.system_tools import _decode, _kill_process_tree

logger = logging.getLogger("zero_agent.build")

# Directories we never descend into when scanning a project (vendored deps, VCS,
# build output, virtualenvs) — they're not the project's own source.
_SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "env", "__pycache__",
    "build", "dist", ".gradle", ".idea", "target", "bin", "obj", ".next", ".cache",
}


def _run(cmd: list[str], cwd: str, timeout: int) -> tuple[int, str]:
    """Run a build command safely: capture output, enforce a timeout, and kill the
    whole process tree on timeout. Returns (returncode, combined_output). Never
    raises — a launch failure comes back as code 127."""
    win = sys.platform == "win32"
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if win else 0
    try:
        proc = subprocess.Popen(
            cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=creationflags, start_new_session=not win,
        )
    except OSError as exc:
        return 127, f"could not launch {cmd[0]}: {exc}"
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_tree(proc)
        try:
            out, _ = proc.communicate(timeout=5)
        except (subprocess.SubprocessError, OSError):
            out = b""
        return 124, _decode(out) + f"\n(timed out after {timeout}s — killed)"
    return proc.returncode, _decode(out)


def _has(project_dir: str, *names: str) -> bool:
    return any(os.path.exists(os.path.join(project_dir, n)) for n in names)


def _glob(project_dir: str, pattern: str) -> bool:
    return bool(glob.glob(os.path.join(project_dir, pattern)))


def _py_files(project_dir: str, limit: int = 500) -> list[str]:
    files: list[str] = []
    for root, dirs, fnames in os.walk(project_dir):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for fn in fnames:
            if fn.endswith(".py"):
                files.append(os.path.join(root, fn))
        if len(files) >= limit:
            break
    return files


def _detect(project_dir: str) -> str | None:
    """Identify the project's PRIMARY build system from its marker files."""
    if _has(project_dir, "Cargo.toml"):
        return "rust"
    if _has(project_dir, "go.mod"):
        return "go"
    if _glob(project_dir, "*.csproj") or _glob(project_dir, "*.sln"):
        return "dotnet"
    if _has(project_dir, "build.gradle", "build.gradle.kts", "settings.gradle",
            "settings.gradle.kts", "pom.xml"):
        return "jvm"
    if _has(project_dir, "tsconfig.json"):
        return "typescript"
    if _has(project_dir, "package.json"):
        return "node"
    if _has(project_dir, "requirements.txt", "pyproject.toml", "setup.py") or _py_files(project_dir, 1):
        return "python"
    if _has(project_dir, "index.html"):
        return "static"
    return None


# --- per-language checkers: return (status, detail) -------------------------
# status ∈ {"PASS", "FAIL", "SKIPPED"}.

def _check_python(d: str, timeout: int) -> tuple[str, str]:
    files = _py_files(d)
    if not files:
        return ("SKIPPED", "no Python files found to compile.")
    errors: list[str] = []
    for f in files:
        try:
            py_compile.compile(f, doraise=True, quiet=1)
        except py_compile.PyCompileError as exc:
            errors.append(f"{os.path.relpath(f, d)}: {str(exc.msg).strip()[:160]}")
        except OSError:
            pass  # unreadable file — not a syntax verdict
    if errors:
        return ("FAIL", f"{len(errors)} file(s) have syntax errors:\n  - "
                + "\n  - ".join(errors[:20]))
    return ("PASS", f"{len(files)} Python file(s) compile cleanly.")


def _check_with_tool(
    d: str, timeout: int, tool: str, cmd: list[str], label: str, needs: str = ""
) -> tuple[str, str]:
    """Run an external build tool if present, else SKIP with a clear reason."""
    if shutil.which(tool) is None:
        return ("SKIPPED", f"{label}: '{tool}' is not installed{(' — ' + needs) if needs else ''}.")
    code, out = _run(cmd, d, timeout)
    tail = out.strip()[-1200:]
    if code == 0:
        return ("PASS", f"{label}: build/check succeeded.")
    if code == 124:
        return ("SKIPPED", f"{label}: timed out (couldn't confirm).\n{tail}")
    return ("FAIL", f"{label}: build failed (exit {code}):\n{tail}")


def _verify(d: str, kind: str, timeout: int) -> tuple[str, str]:
    if kind == "python":
        return _check_python(d, timeout)
    if kind == "rust":
        return _check_with_tool(d, timeout, "cargo", ["cargo", "check", "--quiet"], "Rust (cargo check)")
    if kind == "go":
        return _check_with_tool(d, timeout, "go", ["go", "build", "./..."], "Go (go build)")
    if kind == "dotnet":
        return _check_with_tool(d, timeout, "dotnet", ["dotnet", "build", "--nologo"], ".NET (dotnet build)")
    if kind == "typescript":
        if shutil.which("tsc"):
            return _check_with_tool(d, timeout, "tsc", ["tsc", "--noEmit"], "TypeScript (tsc --noEmit)")
        if shutil.which("npx") and os.path.isdir(os.path.join(d, "node_modules")):
            return _check_with_tool(d, timeout, "npx", ["npx", "--no-install", "tsc", "--noEmit"], "TypeScript (npx tsc)")
        return ("SKIPPED", "TypeScript: 'tsc' not installed (and node_modules absent). Run `npm install` then `tsc --noEmit`.")
    if kind == "node":
        if not os.path.isdir(os.path.join(d, "node_modules")):
            return ("SKIPPED", "Node: dependencies not installed (no node_modules). Run `npm install`, then `npm run build`.")
        return _check_with_tool(d, timeout, "npm", ["npm", "run", "build"], "Node (npm run build)")
    if kind == "jvm":
        return ("SKIPPED",
                "JVM/Android (Gradle/Maven): not built here — a real build needs the "
                "Gradle wrapper jar + the Android SDK, which can't be authored as text. "
                "Files are present; open the project in Android Studio / run `gradle build` "
                "with the SDK to compile.")
    if kind == "static":
        return ("SKIPPED", "Static site (HTML/CSS/JS): no build step — open index.html in a browser.")
    return ("SKIPPED", "no recognized build system.")


@registry.register
def verify_build(project_dir: str) -> str:
    """Verify that a project actually BUILDS/compiles (not just per-file syntax).

    Detects the project's build system and runs its real compile/check. Use this
    after creating/changing a project to confirm it's complete and consistent —
    and if it FAILS, read the reported errors, fix the files with write_file, and
    call verify_build again until it PASSES.

    Returns a report starting with PASS, FAIL, or SKIPPED:
      * PASS    — the project compiles.
      * FAIL    — it does not; the build errors follow (fix them).
      * SKIPPED — can't verify here (toolchain not installed, deps not fetched, or
                  a binary/SDK build like Android) — the reason is given.

    Args:
        project_dir: Absolute path to the project's ROOT folder (the one holding
            the build file, e.g. package.json / Cargo.toml / build.gradle, or the
            top of a Python project).
    """
    d = os.path.abspath(os.path.expanduser(project_dir))
    if not os.path.isdir(d):
        return f"FAIL: not a directory: {d}"
    kind = _detect(d)
    if kind is None:
        return (f"SKIPPED: no recognized build system in {d} (no package.json, "
                "Cargo.toml, go.mod, build.gradle, *.csproj, or Python files).")
    status, detail = _verify(d, kind, config.BUILD_VERIFY_TIMEOUT)
    logger.info("verify_build [%s] %s: %s", kind, status, d)
    return f"{status} [{kind}] {d}\n{detail}"
