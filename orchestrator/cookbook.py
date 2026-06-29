"""Model Cookbook — curated catalog of local AI models for Zero Agent.

Hardware-aware: filters by available VRAM, groups by use-case, and surfaces
the right model for the job. Backed by the CATALOG below; no network calls,
no API keys.  Add entries here as you pull new models.

One-click install: every entry has an ollama_tag; call cookbook_install() to
pull it with Ollama.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class CookbookEntry:
    id: str                              # unique short ID (also the ollama_tag)
    name: str                            # display name
    category: str                        # general | coding | fast | uncensored | vision | embedding
    vram_gb: float                       # minimum VRAM needed (GB)
    size_gb: float                       # approximate download size (GB)
    description: str
    capabilities: list[str] = field(default_factory=list)  # tools | thinking | vision | multilingual
    recommended_for: str = ""
    notes: str = ""


# ---------------------------------------------------------------------------
# The Catalog
# Edit freely — add new models as you pull them with Ollama.
# ---------------------------------------------------------------------------
CATALOG: list[CookbookEntry] = [
    # ── General purpose ──────────────────────────────────────────────────────
    CookbookEntry(
        id="qwen3:32b",
        name="Qwen3 32B",
        category="general",
        vram_gb=20,
        size_gb=19.8,
        description="Best all-round local model. Strong reasoning, Hebrew, tools, and thinking.",
        capabilities=["tools", "thinking", "multilingual"],
        recommended_for="Agent planning, complex tasks, Hebrew conversations",
        notes="Default supervisor/planner. Thinking mode gives clean final answers.",
    ),
    CookbookEntry(
        id="qwen3:14b",
        name="Qwen3 14B",
        category="general",
        vram_gb=10,
        size_gb=9.3,
        description="Good middle ground — smarter than 4B, faster than 32B.",
        capabilities=["tools", "thinking", "multilingual"],
        recommended_for="Everyday tasks on machines with 12-16 GB VRAM",
    ),
    CookbookEntry(
        id="qwen3:4b",
        name="Qwen3 4B",
        category="fast",
        vram_gb=3,
        size_gb=2.6,
        description="Ultra-fast for everyday chat. Low VRAM, great quality for its size.",
        capabilities=["tools", "thinking", "multilingual"],
        recommended_for="Quick answers, memory distillation, voice replies",
        notes="Zero's default fast model. Ideal for TTS-paired voice mode.",
    ),
    CookbookEntry(
        id="gemma3:27b",
        name="Gemma 3 27B",
        category="general",
        vram_gb=17,
        size_gb=16.9,
        description="Google's flagship local model. Strong at long documents and reasoning.",
        capabilities=["tools", "vision", "multilingual"],
        recommended_for="Long-context tasks, document analysis, vision",
    ),
    CookbookEntry(
        id="gemma3:4b",
        name="Gemma 3 4B",
        category="fast",
        vram_gb=3,
        size_gb=2.5,
        description="Fast, good quality. Alternative to Qwen3:4b for voice/quick tasks.",
        capabilities=["tools", "vision"],
        recommended_for="Fast replies, image understanding on low VRAM",
    ),
    CookbookEntry(
        id="phi4",
        name="Phi-4 14B",
        category="general",
        vram_gb=9,
        size_gb=8.8,
        description="Microsoft's Phi-4. Punches above its weight on reasoning tasks.",
        capabilities=["tools"],
        recommended_for="Reasoning, STEM, structured output",
    ),

    # ── Coding ───────────────────────────────────────────────────────────────
    CookbookEntry(
        id="qwen3-coder-30b",
        name="Qwen3-Coder 30B",
        category="coding",
        vram_gb=19,
        size_gb=18.5,
        description="Best local coding model. MoE architecture (~3B active params) = fast.",
        capabilities=["tools", "thinking"],
        recommended_for="Code generation, debugging, multi-file edits",
        notes="Zero's default worker model for /agent tasks.",
    ),
    CookbookEntry(
        id="qwopus-coder",
        name="Qwopus3.6 27B Coder",
        category="coding",
        vram_gb=17,
        size_gb=16.5,
        description="Qwen3.6 + Opus merge. MTP speculative decoding = 1.4-2.2x faster inference.",
        capabilities=["tools", "thinking"],
        recommended_for="Code generation with speed boost via MTP",
        notes="Current Zero supervisor/worker default. Validated 27/27 on eval harness.",
    ),
    CookbookEntry(
        id="deepseek-coder-v2:16b",
        name="DeepSeek Coder V2 16B",
        category="coding",
        vram_gb=10,
        size_gb=9.1,
        description="Strong at code, especially Python/Go/Rust. MoE, efficient.",
        capabilities=["tools"],
        recommended_for="Code when VRAM is tight (10 GB)",
    ),
    CookbookEntry(
        id="codellama:34b",
        name="CodeLlama 34B",
        category="coding",
        vram_gb=20,
        size_gb=19.0,
        description="Meta's dedicated coding model. Strong fill-in-the-middle.",
        capabilities=["tools"],
        recommended_for="Code completion, fill-in-the-middle, legacy codebases",
    ),

    # ── Uncensored ───────────────────────────────────────────────────────────
    CookbookEntry(
        id="qwen3.6-uncensored",
        name="Qwen3.6 Uncensored",
        category="uncensored",
        vram_gb=17,
        size_gb=16.5,
        description="Uncensored Qwen3.6 with vision and tools. For AI-safety/NSFW research.",
        capabilities=["tools", "thinking", "vision", "uncensored"],
        recommended_for="University AI-safety research, NSFW content testing",
        notes="Abliterated model — no refusals. Use responsibly.",
    ),
    CookbookEntry(
        id="qwen3-coder-abliterated",
        name="Qwen3-Coder Abliterated",
        category="uncensored",
        vram_gb=19,
        size_gb=18.5,
        description="Uncensored coding variant of Qwen3-Coder.",
        capabilities=["tools", "thinking", "uncensored"],
        recommended_for="Unrestricted code generation research",
    ),
    CookbookEntry(
        id="supergemma4-uncensored",
        name="SuperGemma4 Uncensored",
        category="uncensored",
        vram_gb=10,
        size_gb=9.5,
        description="Uncensored Gemma 4 variant. Good general uncensored model.",
        capabilities=["tools", "uncensored"],
        recommended_for="General uncensored tasks, research",
    ),
    CookbookEntry(
        id="hermes3:8b",
        name="Nous Hermes 3 8B",
        category="uncensored",
        vram_gb=5,
        size_gb=4.9,
        description="Function-calling specialist. Uncensored + accurate + grounded.",
        capabilities=["tools", "uncensored"],
        recommended_for="Tool-calling accuracy, grounded answers without refusals",
        notes="Validated on Zero eval harness for tool calls.",
    ),

    # ── Vision ───────────────────────────────────────────────────────────────
    CookbookEntry(
        id="llava:34b",
        name="LLaVA 34B",
        category="vision",
        vram_gb=20,
        size_gb=19.7,
        description="Best local vision model for detailed image analysis.",
        capabilities=["vision"],
        recommended_for="Detailed image description, OCR, visual QA",
    ),
    CookbookEntry(
        id="minicpm-v",
        name="MiniCPM-V",
        category="vision",
        vram_gb=6,
        size_gb=5.5,
        description="Compact vision model. Good at documents, screenshots, charts.",
        capabilities=["vision"],
        recommended_for="Screenshots, documents, charts on limited VRAM",
    ),
    CookbookEntry(
        id="llava:7b",
        name="LLaVA 7B",
        category="vision",
        vram_gb=5,
        size_gb=4.7,
        description="Fast vision model. Less accurate than 34B but much faster.",
        capabilities=["vision"],
        recommended_for="Quick image descriptions, low-latency vision",
    ),

    # ── Embedding ─────────────────────────────────────────────────────────────
    CookbookEntry(
        id="nomic-embed-text",
        name="Nomic Embed Text",
        category="embedding",
        vram_gb=0.5,
        size_gb=0.3,
        description="Zero's default embedding model. Fast, 768-dim, excellent for ChromaDB.",
        capabilities=["embedding"],
        recommended_for="ChromaDB memory, semantic search, RAG",
        notes="Default EMBED_MODEL in config. Pull this first on any new machine.",
    ),
    CookbookEntry(
        id="mxbai-embed-large",
        name="MXBai Embed Large",
        category="embedding",
        vram_gb=0.5,
        size_gb=0.7,
        description="Higher-quality embeddings (1024-dim). Better recall at cost of size.",
        capabilities=["embedding"],
        recommended_for="High-quality RAG retrieval when accuracy > speed",
    ),
]

# Category display order + labels
CATEGORIES: dict[str, str] = {
    "general":    "General purpose",
    "coding":     "Coding",
    "fast":       "Fast / lightweight",
    "uncensored": "Uncensored / research",
    "vision":     "Vision (image input)",
    "embedding":  "Embedding (for memory/RAG)",
}


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

def all_models() -> list[CookbookEntry]:
    return list(CATALOG)


def by_category(category: str) -> list[CookbookEntry]:
    cat = category.lower().strip()
    return [e for e in CATALOG if e.category == cat]


def for_vram(max_vram_gb: float) -> list[CookbookEntry]:
    return [e for e in CATALOG if e.vram_gb <= max_vram_gb]


def get(model_id: str) -> CookbookEntry | None:
    mid = model_id.strip().lower()
    return next((e for e in CATALOG if e.id.lower() == mid), None)


def format_table(
    entries: list[CookbookEntry],
    *,
    show_notes: bool = False,
    group_by_category: bool = True,
) -> str:
    if not entries:
        return "No models match your criteria."

    def _row(e: CookbookEntry) -> str:
        caps = " · ".join(e.capabilities) if e.capabilities else "—"
        line = (
            f"  **{e.name}** (`{e.id}`)\n"
            f"    VRAM: {e.vram_gb} GB  |  Size: {e.size_gb} GB  |  {caps}\n"
            f"    {e.description}"
        )
        if e.recommended_for:
            line += f"\n    ✓ {e.recommended_for}"
        if show_notes and e.notes:
            line += f"\n    💡 {e.notes}"
        return line

    if not group_by_category:
        return "\n\n".join(_row(e) for e in entries)

    # Group by category
    groups: dict[str, list[CookbookEntry]] = {}
    for e in entries:
        groups.setdefault(e.category, []).append(e)

    lines: list[str] = []
    for cat_key in CATEGORIES:
        group = groups.get(cat_key, [])
        if not group:
            continue
        lines.append(f"\n### {CATEGORIES[cat_key]}")
        lines.extend(_row(e) for e in group)

    # Any categories not in the canonical order
    for cat_key, group in groups.items():
        if cat_key not in CATEGORIES:
            lines.append(f"\n### {cat_key.title()}")
            lines.extend(_row(e) for e in group)

    return "\n\n".join(lines)


def recommendation_for_vram(vram_gb: float) -> str:
    """Return a short opinionated recommendation based on available VRAM."""
    if vram_gb >= 30:
        return (
            "32+ GB: You can run qwen3:32b (best reasoning) + qwen3-coder-30b or qwopus-coder "
            "(best code) side-by-side — but OLLAMA_MAX_LOADED_MODELS=1 prevents co-loading "
            "to avoid OOM. Recommended: keep qwopus-coder as supervisor+worker for /agent tasks."
        )
    if vram_gb >= 20:
        return (
            "20-32 GB: qwen3:32b or qwopus-coder fits. Pick qwopus-coder for coding tasks "
            "(faster via MTP); qwen3:32b for general reasoning."
        )
    if vram_gb >= 10:
        return (
            "10-20 GB: qwen3:14b or deepseek-coder-v2:16b. Both run well and support tools."
        )
    if vram_gb >= 5:
        return (
            "5-10 GB: hermes3:8b (tools + uncensored) or qwen3:4b (fast, thinking). "
            "Both fit comfortably and handle everyday tasks."
        )
    return (
        "< 5 GB: qwen3:4b (2.6 GB) or gemma3:4b (2.5 GB). Fast, capable for their size."
    )
