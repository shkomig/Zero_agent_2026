# Contributing to Zero Agent

Thanks for your interest! Zero Agent is a **100% local** AI agent — contributions
that keep it local, simple, and reliable are very welcome.

## Principles

- **Local-first.** No cloud services, paid APIs, or external orchestrators
  (LangChain/CrewAI/AutoGen). We adopt *ideas*, not frameworks.
- **Additive.** After every change the agent still boots and works.
- **Reliability over features.** Anti-hallucination, calibration, and safety
  (HITL) matter more than another tool.

## Dev setup

```bash
git clone https://github.com/shkomig/Zero_agent_2026.git
cd Zero_agent_2026
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
ollama serve && ollama pull qwen3:32b
```

Run the UI with `chainlit run app.py` (or `START_ALL.bat` on Windows).

## Before you open a PR

- **Run the eval harness:** `python test_agent.py` (fast component checks) and,
  for behavioural changes, `python test_agent.py --live --model=qwen3-coder-30b`.
  Don't regress it.
- Match the surrounding code style; keep functions documented (Google-style
  docstrings — the tool registry turns them into schemas).
- Adding a tool? See the "Extending: adding a tool" section in
  [orchestrator/README.md](orchestrator/README.md). Gate destructive tools behind
  `config.HITL_TOOLS`.
- Document user-facing changes in [CHANGELOG.md](CHANGELOG.md).

## Reporting issues

Include your OS, Python version, the model in use, and the relevant log line
(every run has a `trace=` id). Never paste secrets or private chat content.

By contributing you agree your work is licensed under the [MIT License](LICENSE).
