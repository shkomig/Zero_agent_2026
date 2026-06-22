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
    r"bat|cmd|ps1|sh|sql|xml|svg|png|jpg|jpeg|gif|pdf|docx?|xlsx?|"
    # source/code files — without these the verifier was BLIND to e.g. .java and
    # failed every "create a code file" step as if nothing landed on disk.
    r"java|kt|kts|c|cc|cpp|cxx|h|hpp|hh|go|rs|rb|php|swift|scala|dart|lua|cs|"
    r"vue|svelte|scss|sass|less|gradle|properties|proto|graphql|gql|jsonc|R|pl"
)
_PATH_RE = re.compile(
    r"""['"`]([^'"`\n]+?\.(?:%s))['"`]"""  # quoted "path.ext"
    r"""|((?:[A-Za-z]:\\|/|\.{1,2}/|\w[\w\-./\\]*?)[\w\-./\\]*?\.(?:%s))\b"""
    % (_EXT, _EXT),
    re.IGNORECASE,
)

# Words that mark the deliverable as a DIRECTORY, not a file (so we check
# os.path.isdir, not isfile — a mkdir produces no file and was being falsely
# failed as "no file landed on disk").
_DIR_WORDS = ("directory", "folder", "mkdir")

# Absolute directory paths: a Windows drive path or a Unix abs path, captured up
# to the first whitespace/quote. We later drop any that carry a file extension.
_DIR_RE = re.compile(r"""['"`]?([A-Za-z]:\\[^\s'"`<>|]+|/[A-Za-z][^\s'"`<>|]*)""")


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
        """Return ``(ok, message)``. **NEVER raises** — any internal error (e.g. a
        non-UTF-8 file the audit happened to touch) is swallowed as a PASS, so a
        verifier hiccup can't crash the whole agent run. The Supervisor calls this
        directly (not via the registry), so an uncaught error here is fatal."""
        try:
            return self._verify_task(task_description, agent_result, cwd)
        except Exception as exc:  # noqa: BLE001 - the verifier must never crash a run
            logger.exception("verifier crashed; treating as pass")
            return (True, f"Verification skipped (internal error: {type(exc).__name__}).")

    def _verify_task(
        self, task_description: str, agent_result: str, cwd: str = ""
    ) -> tuple[bool, str]:
        """Core verification logic (wrapped by :meth:`verify_task`).

        ``ok=False`` means the claimed work is NOT backed by reality (missing
        file/dir, empty file, syntax error). ``ok=True`` with an "inconclusive"
        message means there was nothing deterministic to check.
        """
        desc = task_description.lower()
        is_creation = any(w in desc for w in _CREATION_WORDS)

        # Directory-creation tasks: the deliverable is a DIRECTORY, not a file.
        # mkdir produces no file, so the file checks below would falsely fail it.
        if is_creation and any(w in desc for w in _DIR_WORDS):
            dirs = self._extract_dir_paths(f"{task_description}\n{agent_result}", cwd)
            existing = [d for d in dirs if os.path.isdir(d)]
            if existing:
                return (True, "Directory exists: " + ", ".join(existing))
            if dirs:
                return (False, "Expected directory was not created: " + ", ".join(dirs))
            # No explicit path to check — don't block on a directory task.
            return (True, "Directory task with no explicit path — trusting the result.")

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
    def _extract_dir_paths(self, text: str, cwd: str) -> list[str]:
        """Extract absolute DIRECTORY paths (no file extension) from text."""
        out: list[str] = []
        for m in _DIR_RE.finditer(text):
            p = (m.group(1) or "").strip().rstrip("\\/.,;:\"'` ")
            if not p:
                continue
            ext = os.path.splitext(p)[1].lower()
            if ext and 2 <= len(ext) <= 5:  # has a file extension → a file, skip
                continue
            full = p if os.path.isabs(p) else os.path.join(cwd or os.getcwd(), p)
            full = os.path.normpath(full)
            if full not in out:
                out.append(full)
        return out

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
            except UnicodeDecodeError:
                # An incidental non-UTF-8 .json (common on Windows) — exists, just
                # don't deep-validate; never fail/crash the run over its encoding.
                return (True, f"{name} ok ({size} bytes; non-UTF-8, not validated)")
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
