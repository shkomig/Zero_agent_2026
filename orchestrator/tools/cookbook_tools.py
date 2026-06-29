"""Model Cookbook tools — curated model catalog + one-click Ollama install.

Two tools exposed to the agent:
  cookbook_list    — browse the catalog (filter by category or VRAM)
  cookbook_install — pull a model from the catalog with ollama pull

The /cookbook slash command in app.py calls cookbook_list() directly for
a nicely formatted UI response.
"""

from __future__ import annotations

import subprocess
import sys

from orchestrator import cookbook
from orchestrator.tools.registry import registry


@registry.register
def cookbook_list(
    category: str = "",
    max_vram_gb: float = 0,
    model_id: str = "",
) -> str:
    """Browse the curated model catalog.  Use this when the user asks which
    model to use, what models are available, or which model fits their GPU.

    Returns a formatted table of models with VRAM requirements, capabilities,
    and use-case recommendations.  Optionally filter by category or VRAM.

    Categories: general, coding, fast, uncensored, vision, embedding

    Args:
        category:    Filter by category name (empty = all categories).
        max_vram_gb: Show only models that fit within this many GB of VRAM
                     (0 = no filter, show everything).
        model_id:    Show detailed info for one specific model by ID.
    """
    if model_id:
        entry = cookbook.get(model_id)
        if entry is None:
            ids = ", ".join(e.id for e in cookbook.CATALOG)
            return f"Model '{model_id}' not found in cookbook.\nAvailable IDs:\n{ids}"
        caps = " · ".join(entry.capabilities) if entry.capabilities else "—"
        lines = [
            f"## {entry.name}  (`{entry.id}`)",
            f"**Category:** {cookbook.CATEGORIES.get(entry.category, entry.category)}",
            f"**VRAM:** {entry.vram_gb} GB  |  **Download:** {entry.size_gb} GB",
            f"**Capabilities:** {caps}",
            f"**Description:** {entry.description}",
        ]
        if entry.recommended_for:
            lines.append(f"**Best for:** {entry.recommended_for}")
        if entry.notes:
            lines.append(f"**Notes:** {entry.notes}")
        lines.append(f"\nInstall: `ollama pull {entry.id}`  or call `cookbook_install('{entry.id}')`")
        return "\n".join(lines)

    # Apply filters
    entries = cookbook.all_models()
    if category:
        cat = category.lower().strip()
        entries = [e for e in entries if e.category == cat]
        if not entries:
            valid = ", ".join(cookbook.CATEGORIES)
            return f"No models in category '{category}'. Valid: {valid}"
    if max_vram_gb > 0:
        entries = [e for e in entries if e.vram_gb <= max_vram_gb]
        if not entries:
            return f"No models fit within {max_vram_gb} GB VRAM."

    header = "# Zero Agent — Model Cookbook\n"
    if category or max_vram_gb:
        filters: list[str] = []
        if category:
            filters.append(f"category={category}")
        if max_vram_gb:
            filters.append(f"VRAM≤{max_vram_gb} GB")
        header += f"_Filtered: {', '.join(filters)}_\n"

    table = cookbook.format_table(entries, show_notes=True)
    footer = (
        "\n---\n"
        "**Install any model:** `cookbook_install(model_id='<id>')`\n"
        "**Filter by VRAM:** `cookbook_list(max_vram_gb=16)`\n"
        "**Filter by use:** `cookbook_list(category='coding')`"
    )
    return header + table + footer


@registry.register
def cookbook_install(model_id: str, timeout_s: int = 600) -> str:
    """Pull a model from the Model Cookbook with Ollama.

    Runs ``ollama pull <model_id>`` and streams the final status line.
    The model_id must be a valid Ollama tag (e.g. "qwen3:32b", "nomic-embed-text").
    It does NOT have to be in the cookbook — any Ollama-compatible tag works.

    Args:
        model_id:  Ollama model tag to pull, e.g. "qwen3:14b".
        timeout_s: Max seconds to wait (default 600 — large models take time).
    """
    if not model_id or not model_id.strip():
        return "Error: model_id is required."

    mid = model_id.strip()

    # Check if it's in the cookbook and show a note
    entry = cookbook.get(mid)
    note = ""
    if entry:
        note = f"({entry.name} — {entry.size_gb} GB download)\n"

    try:
        r = subprocess.run(
            ["ollama", "pull", mid],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
        )
    except FileNotFoundError:
        return "Error: 'ollama' not found in PATH. Is Ollama installed and running?"
    except subprocess.TimeoutExpired:
        return (
            f"Timed out after {timeout_s}s pulling '{mid}'. "
            "The model may still be downloading in the background — check 'ollama list'."
        )

    output = (r.stdout or "").strip() + ("\n" + (r.stderr or "").strip()).rstrip()
    output = output.strip()

    if r.returncode == 0:
        return (
            f"Successfully pulled '{mid}'. {note}\n"
            + (output[-600:] if len(output) > 600 else output)
        )
    return (
        f"Failed to pull '{mid}' (exit {r.returncode}):\n"
        + output[-600:]
    )
