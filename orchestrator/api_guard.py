"""Public-API preservation guard for file MODIFICATIONS.

When the Worker rewrites an existing code file, it can silently DROP a public
function/method that the rest of the project depends on — the file still exists and
compiles, so RealityVerifier passes, yet another module now crashes at runtime
(observed: a rewrite deleted ``PromptGenerator.get_available_templates`` and the app
died with ``AttributeError``). This is a deterministic, LLM-free check: parse the
BEFORE and AFTER source, and flag any PUBLIC (non-underscore) top-level function,
class, or class method that existed before the edit but is gone after it.

Pure and best-effort: if either side doesn't parse (a syntax error is already caught
elsewhere), it returns no findings rather than guessing.
"""

from __future__ import annotations

import ast
import logging

logger = logging.getLogger("zero_agent.api_guard")


def _public_symbols(source: str) -> set[str]:
    """The public API surface of ``source``: module-level ``func``/``Class`` names and
    ``Class.method`` names, excluding anything starting with ``_``. Empty on a parse
    error (we can't compare what we can't parse)."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return set()
    out: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("_"):
                out.add(node.name)
        elif isinstance(node, ast.ClassDef):
            if node.name.startswith("_"):
                continue
            out.add(node.name)
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    # __init__ etc. are dunder; only flag genuinely public methods.
                    if not item.name.startswith("_"):
                        out.add(f"{node.name}.{item.name}")
    return out


def dropped_public_symbols(before: str, after: str) -> list[str]:
    """Public symbols present in ``before`` but MISSING from ``after`` (sorted).

    Empty when nothing was dropped, when either side fails to parse, or when either
    side is empty (a brand-new file has no "before" API to preserve).
    """
    if not before or not after:
        return []
    # If the AFTER doesn't parse we cannot compare — and an empty symbol set would
    # falsely look like "everything was dropped". A syntax error is already caught by
    # the per-file compile check, so just decline to judge here.
    try:
        ast.parse(after)
    except (SyntaxError, ValueError):
        return []
    before_syms = _public_symbols(before)
    after_syms = _public_symbols(after)
    if not before_syms:
        return []
    return sorted(before_syms - after_syms)
