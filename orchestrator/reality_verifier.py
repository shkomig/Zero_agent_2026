"""Reality verification — "code as a judge", not "text as a judge".

The single-agent loop's only check was ``verify_answer``: it asked the model
whether its OWN answer looked plausible. A model that hallucinates "Created the
website successfully" will happily also say "yes, that looks correct" — so text
self-review cannot catch a fabricated action. This module checks the WORLD
instead: did the file actually land on disk? Is it non-empty? Does the Python
compile? Does the JSON parse? Those are facts, not opinions.

Deterministic checks run first (free, reliable, no hallucination). Only when the
task type is fuzzy and there is no file evidence does the Supervisor fall back to
an LLM judge — and even then it is given the filesystem evidence to ground on.
"""

from __future__ import annotations

import json
import logging
import os
import py_compile
import re
import tempfile

logger = logging.getLogger("zero_agent.verifier")

# Words in a task description that imply a file/artifact should now exist.
_CREATION_WORDS = (
    "create",
    "write",
    "save",
    "build",
    "generate",
    "make",
    "implement",
    "add",
    "produce",
)

# Pull candidate file paths out of free text: quoted paths and bare tokens that
# end in a known file extension. Deliberately broad — we then test on disk.
_EXT = (
    r"py|js|ts|tsx|jsx|html|htm|css|json|md|txt|csv|yaml|yml|toml|ini|cfg|"
    r"bat|cmd|ps1|sh|sql|xml|svg|png|jpg|jpeg|gif|pdf|docx?|xlsx?"
)
_PATH_RE = re.compile(
    r"""['"`]([^'"`\n]+?\.(?:%s))['"`]"""  # quoted "path.ext"
    r"""|((?:[A-Za-z]:\\|/|\.{1,2}/|\w[\w\-./\\]*?)[\w\-./\\]*?\.(?:%s))\b"""
    % (_EXT, _EXT),
    re.IGNORECASE,
)


class RealityVerifier:
    """Verify that a Worker's claimed result actually happened on disk."""

    def __init__(self, registry=None) -> None:
        # registry kept for optional tool-based checks (e.g. run a build); the
        # core file/syntax checks use the stdlib directly for reliability.
        self.registry = registry

    # --- public API ---------------------------------------------------------
    def verify_task(
        self, task_description: str, agent_result: str, cwd: str = ""
    ) -> tuple[bool, str]:
        """Return ``(ok, message)``. Never raises.

        ``ok=False`` means the claimed work is NOT backed by reality (missing
        file, empty file, syntax error). ``ok=True`` with an "inconclusive"
        message means there was nothing deterministic to check — the Supervisor
        may then choose an LLM judge.
        """
        desc = task_description.lower()
        is_creation = any(w in desc for w in _CREATION_WORDS)

        candidates = self._extract_paths(f"{task_description}\n{agent_result}")
        resolved = self._resolve(candidates, cwd)

        # Creation task but we found NO file reference anywhere → the worker very
        # likely only narrated. That's the classic failure we must catch.
        if is_creation and not resolved:
            return (
                False,
                "Task implies creating a file, but no file path was found in the "
                "result and nothing verifiable landed on disk. The worker may have "
                "only described the action instead of calling write_file.",
            )

        if not resolved:
            return (True, "No file artifacts to verify for this task type.")

        existing = [p for p in resolved if os.path.isfile(p)]
        missing = [p for p in resolved if not os.path.isfile(p)]

        if is_creation and not existing:
            return (
                False,
                "None of the expected files exist on disk: "
                + ", ".join(missing),
            )

        problems: list[str] = []
        verified: list[str] = []
        for path in existing:
            ok, note = self._check_file(path)
            (verified if ok else problems).append(note)

        if problems:
            return (False, "File checks failed:\n  - " + "\n  - ".join(problems))

        evidence = "; ".join(verified) if verified else "files present"
        if missing:
            evidence += f" (note: not found: {', '.join(missing)})"
        return (True, f"Reality check passed: {evidence}")

    # --- internals ----------------------------------------------------------
    def _extract_paths(self, text: str) -> list[str]:
        seen: list[str] = []
        for m in _PATH_RE.finditer(text):
            p = (m.group(1) or m.group(2) or "").strip().strip("'\"`")
            # Skip obvious noise (urls, bare extensions, dotfiles like ".py").
            if not p or p.startswith(("http://", "https://")) or p.lstrip(".") == "":
                continue
            if p not in seen:
                seen.append(p)
        return seen

    def _resolve(self, candidates: list[str], cwd: str) -> list[str]:
        out: list[str] = []
        for c in candidates:
            c = c.replace("/", os.sep) if os.sep == "\\" else c
            p = c if os.path.isabs(c) else os.path.join(cwd or os.getcwd(), c)
            p = os.path.normpath(p)
            if p not in out:
                out.append(p)
        return out

    def _check_file(self, path: str) -> tuple[bool, str]:
        name = os.path.basename(path)
        try:
            size = os.path.getsize(path)
        except OSError as exc:
            return (False, f"{name}: cannot stat ({exc})")
        if size == 0:
            return (False, f"{name}: exists but is EMPTY (0 bytes)")

        ext = os.path.splitext(path)[1].lower()
        if ext == ".py":
            try:
                py_compile.compile(path, doraise=True, quiet=1)
            except py_compile.PyCompileError as exc:
                return (False, f"{name}: Python syntax error ({exc.msg.strip()})")
            except OSError as exc:
                return (False, f"{name}: cannot read for compile ({exc})")
        elif ext == ".json":
            try:
                with open(path, encoding="utf-8") as f:
                    json.load(f)
            except (OSError, json.JSONDecodeError) as exc:
                return (False, f"{name}: invalid JSON ({exc})")
        return (True, f"{name} ok ({size} bytes)")


# Sanity self-test when run directly: python -m orchestrator.reality_verifier
if __name__ == "__main__":  # pragma: no cover
    v = RealityVerifier()
    d = tempfile.mkdtemp()
    good = os.path.join(d, "index.html")
    with open(good, "w", encoding="utf-8") as f:
        f.write("<!doctype html><h1>hi</h1>")
    bad_py = os.path.join(d, "broken.py")
    with open(bad_py, "w", encoding="utf-8") as f:
        f.write("def x(:\n")  # syntax error

    print(v.verify_task("Create index.html", f"wrote {good}", d))
    print(v.verify_task("Write broken.py", f"saved {bad_py}", d))
    print(v.verify_task("Create config.json", "I created config.json", d))  # missing
    print(v.verify_task("Explain how recursion works", "Recursion is...", d))  # n/a
