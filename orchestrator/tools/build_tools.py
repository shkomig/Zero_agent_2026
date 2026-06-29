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
import json
import logging
import os
import py_compile
import shutil
import socket
import subprocess
import sys
import time

import httpx

from orchestrator import config
from orchestrator.tools.registry import registry
from orchestrator.tools.system_tools import (
    _DANGEROUS_PATTERNS,
    _decode,
    _kill_process_tree,
)

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


# ===========================================================================
# Stage B — does it actually RUN / pass its tests? (not just "does it compile")
# ===========================================================================

def _module_available(name: str) -> bool:
    """True if an importable module is present in THIS interpreter (no import run)."""
    import importlib.util
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _python_test_files(d: str, limit: int = 500) -> list[str]:
    files: list[str] = []
    for root, dirs, fnames in os.walk(d):
        dirs[:] = [x for x in dirs if x not in _SKIP_DIRS]
        for fn in fnames:
            if (fn.startswith("test_") and fn.endswith(".py")) or fn.endswith("_test.py"):
                files.append(os.path.join(root, fn))
        if len(files) >= limit:
            break
    return files


def _node_test_script(d: str) -> str | None:
    """The package.json "test" script if it's a REAL one (not npm's placeholder)."""
    try:
        pkg = json.loads(open(os.path.join(d, "package.json"), encoding="utf-8").read())
    except (OSError, ValueError):
        return None
    script = (pkg.get("scripts") or {}).get("test", "")
    if not script or "no test specified" in script:
        return None  # the default `echo "Error: no test specified" && exit 1`
    return script


def _run_tests_for(d: str, kind: str, timeout: int) -> tuple[str, str]:
    if kind == "python":
        if not _python_test_files(d):
            return ("SKIPPED", "no Python test files (test_*.py / *_test.py) found.")
        if not _module_available("pytest"):
            return ("SKIPPED", "pytest is not installed in this interpreter — "
                    "`pip install pytest`, then re-run.")
        code, out = _run([sys.executable, "-m", "pytest", "-q"], d, timeout)
        tail = out.strip()[-1600:]
        if code == 0:
            return ("PASS", f"pytest: all tests passed.\n{tail}")
        if code == 5:  # pytest's "no tests collected"
            return ("SKIPPED", "pytest collected no tests.")
        if code == 124:
            return ("SKIPPED", f"pytest timed out (couldn't confirm).\n{tail}")
        return ("FAIL", f"pytest: tests failed (exit {code}):\n{tail}")
    if kind == "rust":
        return _check_with_tool(d, timeout, "cargo", ["cargo", "test", "--quiet"], "Rust (cargo test)")
    if kind == "go":
        return _check_with_tool(d, timeout, "go", ["go", "test", "./..."], "Go (go test)")
    if kind == "dotnet":
        return _check_with_tool(d, timeout, "dotnet", ["dotnet", "test", "--nologo"], ".NET (dotnet test)")
    if kind == "node":
        script = _node_test_script(d)
        if script is None:
            return ("SKIPPED", "Node: no real `test` script in package.json.")
        # Node's built-in test runner needs no dependencies — run it directly.
        if "node --test" in script:
            return _check_with_tool(d, timeout, "node", ["node", "--test"], "Node (node --test)")
        if not os.path.isdir(os.path.join(d, "node_modules")):
            return ("SKIPPED", "Node: dependencies not installed (no node_modules). Run `npm install` first.")
        return _check_with_tool(d, timeout, "npm", ["npm", "test", "--silent"], "Node (npm test)")
    if kind == "jvm":
        return ("SKIPPED", "JVM/Android (Gradle/Maven): tests not run here — needs the "
                "Gradle wrapper + SDK. Run `gradle test` with the toolchain installed.")
    return ("SKIPPED", f"no test runner for build system '{kind}'.")


@registry.register
def run_tests(project_dir: str) -> str:
    """Run a project's automated TEST SUITE and report PASS / FAIL / SKIPPED.

    Goes one step past verify_build: not just "does it compile" but "do its tests
    pass". Detects the test runner (pytest, cargo test, go test, npm test, dotnet
    test) and runs it safely (no network/dependency install; timeout + full
    process-tree kill so a hung test can't freeze the agent). Use it after a build
    passes; on FAIL read the failures, fix with write_file, and run_tests again.

    Returns a report starting with:
      * PASS    — the test suite ran and all tests passed.
      * FAIL    — tests failed; the failures follow (fix them).
      * SKIPPED — no tests found, or the runner/deps aren't available here (reason given).

    Args:
        project_dir: Absolute path to the project ROOT (holds the build/manifest file).
    """
    d = os.path.abspath(os.path.expanduser(project_dir))
    if not os.path.isdir(d):
        return f"FAIL: not a directory: {d}"
    kind = _detect(d)
    if kind is None:
        return f"SKIPPED: no recognized project in {d}, so no test runner to use."
    status, detail = _run_tests_for(d, kind, config.TEST_RUN_TIMEOUT)
    logger.info("run_tests [%s] %s: %s", kind, status, d)
    return f"{status} [{kind}] {d}\n{detail}"


def _free_port() -> int:
    """An OS-assigned free TCP port that is NOT one of the RESERVED_PORTS."""
    for _ in range(20):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", 0))
            port = int(s.getsockname()[1])
        finally:
            s.close()
        if port not in config.RESERVED_PORTS:
            return port
    return 0


def _normalize_start_command(cmd: str, port: int) -> tuple[str, int]:
    """Make a long-running web server safely smoke-runnable. Pure.

    Streamlit is the common trap: ``streamlit run app.py`` opens a BROWSER window on
    every launch and picks its own port, so a naive verify step hangs and spawns
    windows. Force it headless, bound to localhost, on a known port the probe can
    reach. Returns the (possibly) adjusted ``(command, port)``.
    """
    if "streamlit run" in cmd:
        if port == 0:
            port = 8501  # Streamlit's default
        if "--server.headless" not in cmd:
            cmd += " --server.headless true"
        if "--server.address" not in cmd:
            cmd += " --server.address 127.0.0.1"
        if "--server.port" not in cmd:
            cmd += f" --server.port {port}"
    return cmd, port


@registry.register
def smoke_run(project_dir: str, start_command: str = "", port: int = 0,
              probe_path: str = "/") -> str:
    """SMOKE-RUN a project: actually start it and confirm it comes up, then stop it.

    The gap past verify_build/run_tests: code can compile and still fail to boot
    (missing entry point, crash on startup, bad config). This starts the app, checks
    it's really alive, and ALWAYS kills it (and its whole process tree) when done —
    no leaked servers.

    Two modes, chosen automatically:
      * WEB (a port is involved): give `port` (or put `{port}` in start_command and a
        free one is chosen) — the app is launched and HTTP-probed at `probe_path`
        until it responds. PASS once it answers; FAIL if it never does or crashes first.
      * CLI/script (no port): the app is run briefly. Exit 0 → PASS (ran cleanly);
        a non-zero crash → FAIL with the output; still alive after the boot grace →
        PASS (a long-running process that started without crashing).

    If start_command is omitted, a static site (index.html) is auto-served; for
    anything else pass the exact command you'd use to run it. Placeholders `{port}`
    and `{dir}` in start_command are substituted. Reserved ports are refused.

    Returns a report starting with PASS, FAIL, or SKIPPED.

    Args:
        project_dir: Absolute path to the project ROOT to run in.
        start_command: How to start the app (e.g. 'python app.py --port {port}',
            'npm run dev'). Empty = auto-serve a static index.html.
        port: The port the app listens on (web mode). 0 = none / auto-pick for `{port}`.
        probe_path: HTTP path to probe in web mode (default '/').
    """
    d = os.path.abspath(os.path.expanduser(project_dir))
    if not os.path.isdir(d):
        return f"FAIL: not a directory: {d}"

    cmd = start_command.strip()
    if not cmd:
        if _detect(d) == "static" and _has(d, "index.html"):
            if port == 0:
                port = _free_port()
            cmd = f'"{sys.executable}" -m http.server {port}'
        else:
            return ("SKIPPED: no start_command given and the app isn't a static site — "
                    "pass start_command (e.g. 'python app.py --port {port}').")

    for pattern in _DANGEROUS_PATTERNS:
        if pattern.search(cmd):
            return f"FAIL: start_command blocked by safety policy: {cmd!r}"

    # Normalize known long-running web servers (e.g. Streamlit) to a headless,
    # fixed-port form so the probe can reach them and no browser window spawns.
    cmd, port = _normalize_start_command(cmd, port)

    if "{port}" in cmd and port == 0:
        port = _free_port()
    if port and port in config.RESERVED_PORTS:
        return (f"FAIL: refusing to smoke-run on reserved port {port} "
                f"(RESERVED_PORTS={config.RESERVED_PORTS}). Pick another port.")
    cmd = cmd.replace("{port}", str(port)).replace("{dir}", d)

    win = sys.platform == "win32"
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if win else 0
    try:
        proc = subprocess.Popen(  # noqa: S602 - intentional: run the built app
            cmd, cwd=d, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=creationflags, start_new_session=not win,
        )
    except OSError as exc:
        return f"FAIL: could not start {cmd!r}: {type(exc).__name__}: {exc}"

    def _finish(verdict: str, msg: str) -> str:
        _kill_process_tree(proc)
        try:
            out, _ = proc.communicate(timeout=5)
        except (subprocess.SubprocessError, OSError):
            out = b""
        tail = _decode(out).strip()[-1200:]
        logger.info("smoke_run %s: %s (%s)", verdict, d, cmd)
        body = f"{verdict} {d}\n{msg}"
        return body + (f"\n--- output ---\n{tail}" if tail else "")

    deadline = time.time() + config.SMOKE_RUN_TIMEOUT

    # WEB mode — poll HTTP until the server answers.
    if port:
        url = f"http://127.0.0.1:{port}{probe_path if probe_path.startswith('/') else '/' + probe_path}"
        while time.time() < deadline:
            if proc.poll() is not None:  # crashed before it could serve
                return _finish("FAIL", f"the app exited (code {proc.returncode}) before "
                               f"responding on port {port}.")
            try:
                resp = httpx.get(url, timeout=2.0)
                return _finish("PASS", f"server responded on {url} — HTTP {resp.status_code} "
                               f"(booted OK).")
            except httpx.HTTPError:
                time.sleep(0.5)  # not up yet — keep polling
        return _finish("FAIL", f"no HTTP response on {url} within "
                       f"{config.SMOKE_RUN_TIMEOUT}s (the app didn't come up).")

    # CLI/script mode — let it run briefly; verdict from exit code / staying alive.
    grace = min(config.SMOKE_BOOT_GRACE, config.SMOKE_RUN_TIMEOUT)
    end = time.time() + grace
    while time.time() < end:
        if proc.poll() is not None:
            code = proc.returncode
            if code == 0:
                return _finish("PASS", "ran and exited cleanly (exit 0).")
            return _finish("FAIL", f"crashed on startup (exit {code}).")
        time.sleep(0.3)
    return _finish("PASS", f"started and stayed alive for {grace}s without crashing.")
