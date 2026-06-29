"""Sealed Docker sandbox — run a command the agent doesn't fully trust WITHOUT
letting it touch the host (ROADMAP Tier-1 #2, the last security piece).

`execute_terminal_command` runs on the real Windows machine on purpose — the agent
operates the host (starts ComfyUI, manages models, builds projects). This tool is
the ADDITIVE, opt-in alternative for the risky case: code pulled from a fetched
page, an untested build/install script, anything where "what if it's hostile?"
matters. It runs inside a one-shot, locked-down container:

  * no network by default (`--network none`) — can't exfiltrate or phone home,
  * capped memory / cpu / pids — can't fork-bomb or exhaust the box,
  * ALL Linux capabilities dropped + `no-new-privileges` — can't escalate,
  * `--rm` one-shot — nothing persists,
  * only an explicit work_dir (or a scratch dir) is bind-mounted at /work — the
    rest of the host filesystem is invisible.

Safe (the flags are not optional), honest (Docker daemon down → a clear message,
not a crash), and never hangs (timeout + `docker kill` of the named container).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import uuid

from orchestrator import config
from orchestrator.tools.registry import registry
from orchestrator.tools.system_tools import _DANGEROUS_PATTERNS, _decode

logger = logging.getLogger("zero_agent.sandbox")


def _docker_available() -> tuple[bool, str]:
    """(usable, reason). Checks the CLI is installed AND the daemon answers."""
    if shutil.which("docker") is None:
        return (False, "the 'docker' CLI is not installed.")
    try:
        proc = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return (False, f"could not run docker: {exc}")
    if proc.returncode != 0:
        return (False, "the Docker daemon isn't running — start Docker Desktop and retry.")
    return (True, _decode(proc.stdout).strip())


def _build_docker_cmd(command: str, name: str, work_dir: str) -> list[str]:
    """The exact, locked-down `docker run` argv. Pure (no side effects) so the
    safety flags can be asserted in tests."""
    return [
        "docker", "run", "--rm", "--name", name,
        "--network", config.SANDBOX_NETWORK,
        "--memory", config.SANDBOX_MEMORY,
        "--cpus", config.SANDBOX_CPUS,
        "--pids-limit", str(config.SANDBOX_PIDS),
        "--security-opt", "no-new-privileges",
        "--cap-drop", "ALL",
        "-v", f"{work_dir}:/work",
        "-w", "/work",
        config.SANDBOX_IMAGE,
        "sh", "-c", command,
    ]


@registry.register
def execute_in_sandbox(command: str, work_dir: str = "") -> str:
    """Run a shell command inside a SEALED Docker container (isolated from the host).

    Use this instead of execute_terminal_command when you don't fully trust the
    command — e.g. code or a build/install script that came from a fetched web page,
    PDF, or untrusted file. It runs with NO network, dropped privileges, and capped
    resources, and nothing on the host (outside the mounted work_dir) is reachable.
    Use execute_terminal_command (the host) for trusted local operations the agent
    is meant to perform on the real machine.

    Returns the command's exit code + output, or a clear message if Docker isn't
    available (daemon not running / not installed).

    Args:
        command: The shell command to run inside the container (POSIX sh).
        work_dir: Optional host folder to bind-mount at /work (read-write) so the
            command can read/produce files. Defaults to a scratch sandbox dir.
    """
    for pattern in _DANGEROUS_PATTERNS:
        if pattern.search(command):
            # Even sandboxed, refuse the obviously-destructive forms (defense in depth).
            return f"Error: command blocked by safety policy (matched dangerous pattern): {command!r}"

    usable, info = _docker_available()
    if not usable:
        return (f"SKIPPED: cannot sandbox — {info} "
                "(the command was NOT run). Start Docker, or use execute_terminal_command "
                "if you trust this command on the host.")

    wd = os.path.abspath(os.path.expanduser(work_dir)) if work_dir.strip() else config.SANDBOX_WORKDIR
    try:
        os.makedirs(wd, exist_ok=True)
    except OSError as exc:
        return f"Error: cannot prepare work_dir {wd}: {exc}"
    if not os.path.isdir(wd):
        return f"Error: work_dir is not a directory: {wd}"

    name = f"zero-sbx-{uuid.uuid4().hex[:12]}"
    cmd = _build_docker_cmd(command, name, wd)
    win = sys.platform == "win32"
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if win else 0
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=creationflags, start_new_session=not win,
        )
    except OSError as exc:
        return f"Error: could not start the sandbox container: {exc}"

    try:
        out, _ = proc.communicate(timeout=config.SANDBOX_TIMEOUT)
        code = proc.returncode
    except subprocess.TimeoutExpired:
        # Killing the docker CLI doesn't stop the container — kill it by name.
        subprocess.run(["docker", "kill", name], capture_output=True, timeout=15)
        try:
            out, _ = proc.communicate(timeout=5)
        except (subprocess.SubprocessError, OSError):
            out = b""
        body = _decode(out).strip()[-1500:]
        return (f"Error: sandboxed command timed out after {config.SANDBOX_TIMEOUT}s "
                f"and the container was killed: {command!r}\n{body}")

    body = _decode(out).strip()
    limit = getattr(config, "TERMINAL_MAX_OUTPUT_CHARS", 8000)
    if len(body) > limit:
        body = body[:limit] + f"\n... [truncated, {len(body) - limit} more chars]"
    logger.info("execute_in_sandbox exit=%s (%s)", code, command[:80])
    header = (f"[sandbox: image={config.SANDBOX_IMAGE} network={config.SANDBOX_NETWORK} "
              f"mem={config.SANDBOX_MEMORY}] exit_code: {code}")
    return f"{header}\n{body}" if body else header
