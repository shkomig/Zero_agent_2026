# Zero Agent — Architecture

A visual map of how the system fits together. Open this file's **Markdown
Preview** in VS Code (or view on GitHub) to render the Mermaid diagrams.

> **Key idea:** the model is just an interchangeable *brain*. The memory &
> learning layer, the tools, and the voice I/O all wrap **every** model — so your
> standing instructions, remembered facts, and lessons apply no matter which
> model is active. The preferences are the **agent's**, not the model's.

---

## High-level components

```mermaid
flowchart TB
    subgraph FE["Front-ends — same agent, same memory"]
        UI["Chainlit UI<br/>app.py"]
        TG["Telegram bot<br/>telegram_bot.py"]
        CLI["CLI<br/>main.py"]
    end

    subgraph VOICE["Voice — local, offline, torch-free"]
        STT["🎤 Speech-in<br/>stt.py · faster-whisper"]
        TTS["🔊 Speech-out<br/>tts.py · kokoro-onnx"]
    end

    subgraph CORE["Agent core"]
        AGENT["BaseAgent · base_agent.py<br/>tool-calling loop · streaming<br/>tool-call fallback · self-verify"]
    end

    subgraph WRAP["Memory & learning layer — model-independent"]
        MEM["auto_memory<br/>facts + standing rules"]
        LES["lessons<br/>learn from failures"]
        SUM["summarizer<br/>rolling summary"]
        VER["verify<br/>self-check"]
        PROJ["projects<br/>knowledge bases"]
    end

    subgraph MODELS["Models — interchangeable brains"]
        OLL["OllamaClient<br/>ollama_client.py"]
        SRV["Ollama server<br/>qwen3:32b · hermes3<br/>qwen3-coder-abliterated · …"]
    end

    subgraph TOOLS["Tools — registry"]
        T1["files · terminal · ops · models · utility"]
        T2["deep_research · web · ComfyUI · vision"]
    end

    subgraph STORE["Persistent storage — local disk"]
        CHROMA[("ChromaDB<br/>memory · lessons · projects")]
        SQLITE[("SQLite<br/>UI chat history")]
        TGJSON[("JSON<br/>Telegram history")]
    end

    UI <--> STT
    UI <--> TTS
    UI --> AGENT
    TG --> AGENT
    CLI --> AGENT

    AGENT --> WRAP
    AGENT --> OLL --> SRV
    AGENT --> TOOLS

    WRAP --> CHROMA
    UI --> SQLITE
    TG --> TGJSON
```

---

## What happens in one turn

```mermaid
sequenceDiagram
    participant U as You
    participant FE as Front-end
    participant W as Memory layer
    participant A as BaseAgent
    participant M as Model (Ollama)
    participant T as Tools

    U->>FE: message (typed, or 🎤 voice → STT)
    FE->>W: recall facts + standing rules + lessons
    W-->>A: inject as system context (every turn)
    FE->>A: run(prompt, history)
    loop tool-calling loop (no time/step cap — ⏹ stop is manual)
        A->>M: chat (streamed)
        M-->>A: tool call  -or-  final answer
        A->>T: execute tool
        T-->>A: result
    end
    A->>A: self-verify (only if tools were used)
    A-->>FE: final answer (streamed live)
    FE->>W: distill facts + standing rules (background)
    FE-->>U: answer (text + 🔊 TTS if enabled)
```

---

## Why the model is "just a brain"

```mermaid
flowchart LR
    RULES["Your standing instructions<br/>(always cite sources, be concise, …)"]
    FACTS["Remembered facts about you"]
    LESS["Lessons from past failures"]

    RULES --> INJECT
    FACTS --> INJECT
    LESS --> INJECT
    INJECT["Injected into the prompt<br/>EVERY turn"] --> BRAIN

    subgraph BRAIN["Whichever model is active"]
        M1["qwen3:32b"]
        M2["hermes3:8b"]
        M3["qwen3-coder-abliterated"]
        M4["… any model"]
    end
```

The grey box swaps freely; everything feeding into it stays the same. That is why
feedback you give under one model is honoured by **all** of them — only how
*precisely* a model follows the instructions varies with the model's quality.

---

## Module map (where things live)

| Area | Files |
|------|-------|
| Front-ends | `app.py` (Chainlit UI), `telegram_bot.py`, `main.py` (CLI) |
| Agent core | `orchestrator/agents/base_agent.py` |
| Model client | `orchestrator/models/ollama_client.py` |
| Voice | `orchestrator/stt.py` (in), `orchestrator/tts.py` (out) |
| Memory & learning | `orchestrator/auto_memory.py`, `lessons.py`, `summarizer.py`, `verify.py`, `projects.py` |
| Vector store | `orchestrator/memory/store.py` (ChromaDB) |
| Tools | `orchestrator/tools/` (registry + `system/media/model/ops/utility` tools) |
| Persistence | `orchestrator/persistence.py` (SQLite), `telegram_history.py` (JSON) |
| Config | `orchestrator/config.py` (all settings, env-overridable) |

See [ROADMAP.md](ROADMAP.md) for status and [CHANGELOG.md](CHANGELOG.md) for the
dated history of changes.
