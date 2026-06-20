# Zero Agent — Model Guide

Which local model to use for what. Switch any time from the settings panel (⚙️)
in the UI or `/model` in Telegram. The agent's memory, standing instructions, and
tools are **the same for every model** — the model is just the interchangeable
brain (see [ARCHITECTURE.md](ARCHITECTURE.md)).

## At a glance

| Model | Size | Strength | Use it for | Avoid for |
|-------|------|----------|------------|-----------|
| **`qwen3:32b`** | ~20 GB | 🥇 Best **agentic reasoning** — plans, picks the right tools, cites | **Default for anything serious**: research, analysis, **agentic coding**, multi-step tasks | When you need a fast reply (it's a big model) |
| **`hermes3:8b`** | ~5 GB | Fast, conversational, stable tool-calling | Everyday chat, light-to-medium tasks, quick turnaround | Deep multi-step analysis |
| **`qwen3-coder-abliterated`** | ~20 GB | **Raw, uncensored code generation** (university requirement) | "Write me function X" — **one-shot code**, code chat | ❌ The **agentic loop** (it narrates, picks wrong tools, is slow — see below) |
| **`qwen3.6-uncensored`** | ~28 GB | Good reasoning, uncensored | Analysis / NSFW-safety testing that needs both logic and freedom | Very heavy (~90% VRAM), slow |
| **`supergemma4-uncensored`** | medium | Uncensored, balanced | General content / safety testing | Less reliable tool-calling |
| **`gemma3:4b`** | ~3 GB | **Vision** | Behind `analyze_image` — reading images | Complex text |

## Rule of thumb

- **Serious task / code / research** → `qwen3:32b`
- **Quick chat** → `hermes3:8b`
- **Uncensored code (one-shot)** → `qwen3-coder-abliterated` (NOT in the agentic loop!)
- **NSFW testing with reasoning** → `qwen3.6-uncensored`

## The key lesson (2026-06-15)

**"A code model" ≠ "a model for agentic coding tasks."**
`qwen3-coder-abliterated` is excellent at *producing code*, but poor at *driving
tools*: on a complex "build a module" task it narrated a plan instead of writing
files, called `deep_research` + `list_media_assets` (irrelevant), and was very
slow — it had to be stopped manually. For build/automation tasks use `qwen3:32b`;
keep `qwen3-coder-abliterated` for "write me the code for X".

## Notes & tuning

- **One model loaded at a time** (`OLLAMA_MAX_LOADED_MODELS=1`) for VRAM safety on
  the 32 GB card — switching models swaps VRAM, so expect a load pause on switch.
- The 28 GB uncensored models sit near the VRAM ceiling; `num_ctx` is 16384.
- Defaults: UI/CLI start on `qwen3:32b`-class; the memory **distiller** uses
  `qwen3:4b` (cheap background work). Override via `ZERO_AGENT_*` env vars
  (see `orchestrator/config.py`).
