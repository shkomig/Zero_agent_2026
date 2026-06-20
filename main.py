"""Milestone 1 test harness for the Zero Agent tool-calling loop.

Run it directly:

    python main.py
    python main.py "Run 'dir' in the terminal and summarize the output"

It auto-detects an installed Ollama model (preferring ZERO_AGENT_MODEL /
qwen3:32b) so the loop works on whatever you have pulled today.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from orchestrator import config
from orchestrator import observability as obs
from orchestrator.agents.base_agent import BaseAgent
from orchestrator.models.ollama_client import OllamaClient

# Tool-call models like qwen3 / deepseek-r1 / gemma3 work; tiny base models may not.
from orchestrator.tools import registry  # noqa: F401  (registers system tools)

obs.configure_logging()
log = logging.getLogger("zero_agent.main")


async def pick_model(client: OllamaClient) -> str | None:
    """Return the configured model if installed, else fall back to any model."""
    try:
        installed = await client.list_models()
    except Exception as exc:  # noqa: BLE001
        log.error("Could not reach Ollama at %s (%s).", config.OLLAMA_HOST, exc)
        log.error("Is the server running?  ->  ollama serve")
        return None

    if not installed:
        log.error("No models installed. Pull one, e.g.:  ollama pull qwen3:32b")
        return None

    if config.DEFAULT_MODEL in installed:
        return config.DEFAULT_MODEL

    fallback = installed[0]
    log.warning(
        "Model %r not found. Falling back to %r. Installed: %s",
        config.DEFAULT_MODEL,
        fallback,
        ", ".join(installed),
    )
    log.warning("For the real agent:  ollama pull qwen3:32b")
    return fallback


async def main() -> int:
    prompts = (
        [" ".join(sys.argv[1:])]
        if len(sys.argv) > 1
        else [
            "Read the file 'requirements.txt' in the current directory and tell me "
            "which packages it lists.",
            "Run the command 'dir' (or 'ls' on Unix) in the terminal and report how "
            "many entries are in the current directory.",
        ]
    )

    async with OllamaClient() as client:
        model = await pick_model(client)
        if model is None:
            return 1
        client.model = model

        agent = BaseAgent(client, registry)
        log.info("Zero Agent ready | model=%s | tools=%s", model, registry.names())

        for prompt in prompts:
            print("\n" + "=" * 70)
            print(f"USER: {prompt}")
            print("=" * 70)
            answer = await agent.run(prompt)
            print(f"\nAGENT: {answer}\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
