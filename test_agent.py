"""Zero Agent — eval / regression harness (ROADMAP Tier 2 #4).

Golden tasks + behavioural assertions, so a prompt/tool tweak can't silently
break another behaviour. Run before pushing any core change.

    python test_agent.py            # fast COMPONENT checks only (no Ollama)
    python test_agent.py --live     # also run LIVE golden tasks (needs Ollama)
    python test_agent.py --live --model=qwen3:32b   # pick the live model

Exit code is non-zero if any check fails, so it can gate a commit hook / CI.
The component tier is deterministic and instant; the live tier exercises the
real agent loop and is intentionally lenient (behavioural, not exact-match,
since a local LLM is non-deterministic).
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile

from orchestrator import config
from orchestrator.agents.base_agent import BaseAgent, _fence_untrusted
from orchestrator.models.ollama_client import OllamaClient
from orchestrator.reality_verifier import RealityVerifier
from orchestrator.supervisor import Supervisor
from orchestrator.tools import registry  # noqa: F401 - populates the tool registry

_PASS = 0
_FAIL = 0


def check(name: str, cond: object, detail: str = "") -> None:
    global _PASS, _FAIL
    ok = bool(cond)
    if ok:
        _PASS += 1
    else:
        _FAIL += 1
    mark = "PASS" if ok else "FAIL"
    line = f"  [{mark}] {name}"
    if not ok and detail:
        line += f"  -> {detail}"
    print(line)


# --------------------------------------------------------------------------- #
# Tier 1 — component regression checks (deterministic, no Ollama)             #
# --------------------------------------------------------------------------- #
async def component_checks() -> None:
    print("\n[1] Component checks (deterministic, no Ollama)")

    # OLLAMA_HOST normalization (the 0.0.0.0 bug)
    check(
        "host 0.0.0.0 -> loopback",
        config._normalize_ollama_host("0.0.0.0") == "http://127.0.0.1:11434",
    )
    check(
        "host bare name gets scheme+port",
        config._normalize_ollama_host("localhost") == "http://localhost:11434",
    )
    check(
        "host LAN ip preserved",
        config._normalize_ollama_host("192.168.1.5:11434") == "http://192.168.1.5:11434",
    )

    # RealityVerifier — the anti-hallucination core
    v = RealityVerifier()
    d = tempfile.mkdtemp()
    good = os.path.join(d, "ok.html")
    with open(good, "w", encoding="utf-8") as f:
        f.write("<!doctype html><h1>hi</h1>")
    ok, _ = v.verify_task("Create ok.html", f"wrote {good}", d)
    check("verifier passes a real file", ok)
    ok, _ = v.verify_task("Create missing.json", "I created missing.json", d)
    check("verifier catches a missing file (hallucinated success)", not ok)
    bad = os.path.join(d, "broken.py")
    with open(bad, "w", encoding="utf-8") as f:
        f.write("def x(:\n")
    ok, _ = v.verify_task("Write broken.py", f"saved {bad}", d)
    check("verifier catches a Python syntax error", not ok)
    # Directory tasks check the DIR, not a file (regression: the audit-task halt).
    ok, _ = v.verify_task(f"Create the directory {d} with permissions", "done",
                          cwd=r"C:\unrelated\workspace")
    check("verifier passes a real directory (not demanding a file)", ok)
    ok, _ = v.verify_task(f"Create the directory {os.path.join(d, 'ghostdir')}", "done", d)
    check("verifier catches a missing directory", not ok)
    # Code files must be SEEN by the verifier (regression: .java/.go/etc. were not in
    # the extension list, so every "create a code file" step falsely failed as
    # "nothing landed on disk", breaking real app-building goals).
    java = os.path.join(d, "MainActivity.java")
    with open(java, "w", encoding="utf-8") as f:
        f.write("package com.example;\npublic class MainActivity {}\n")
    ok, _ = v.verify_task("Create MainActivity.java with the activity code", f"wrote {java}", d)
    check("verifier recognizes a .java code file (not blind to code)", ok)
    ok, _ = v.verify_task("Create Server.go with the handler", "I created Server.go", d)
    check("verifier catches a missing .go file (code path extraction works)", not ok)

    # Injection-defense fence
    check(
        "untrusted web output is fenced",
        _fence_untrusted("fetch_webpage", "some page text").startswith("[UNTRUSTED"),
    )
    check(
        "constructive write_file is NOT fenced",
        not _fence_untrusted("write_file", "Wrote 10 chars").startswith("[UNTRUSTED"),
    )
    check(
        "error strings are NOT fenced",
        not _fence_untrusted("fetch_webpage", "Error: 404").startswith("[UNTRUSTED"),
    )

    # HITL approval gate
    agent = BaseAgent(OllamaClient(model="qwen3:32b"), registry)
    agent._on_approval = None
    check("HITL: safe tool never gated (no approver)", await agent._approve_tool("read_file", {}))

    async def deny(_n, _a):
        return False

    agent._on_approval = deny
    check("HITL: destructive tool denied when user denies", not await agent._approve_tool("delete_path", {}))
    check("HITL: non-gated tool still allowed under deny", await agent._approve_tool("read_file", {}))

    async def allow(_n, _a):
        return True

    agent._on_approval = allow
    check("HITL: destructive tool allowed when user approves", await agent._approve_tool("execute_terminal_command", {}))

    # Supervisor planner JSON parser (robust to rambling models)
    P = Supervisor._parse_json_array
    check("planner parses fenced JSON array", P('```json\n["a","b"]\n```') == ["a", "b"])
    check("planner parses array after rambling", P('<think>hm</think> ["x","y"]') == ["x", "y"])
    check("planner rejects non-JSON", P("there is no plan here") is None)


# --------------------------------------------------------------------------- #
# Tier 2 — live golden tasks (real agent loop, needs Ollama)                  #
# --------------------------------------------------------------------------- #
async def live_tasks(model: str) -> None:
    print(f"\n[2] Live golden tasks (model={model}, needs Ollama)")
    client = OllamaClient(model=model)
    resolved = await client.resolve_model(model)
    if resolved is None:
        check("Ollama reachable + model installed", False, f"could not resolve {model}")
        await client.aclose()
        return
    agent = BaseAgent(client, registry)

    import re

    calls: list[str] = []
    tool_results: dict[str, str] = {}

    async def on_tool_start(name, _args):
        calls.append(name)
        return name

    async def on_tool_end(handle, result):
        tool_results[str(handle)] = result  # handle is the tool name we returned

    # Golden 1 — determinism: a date question must use the tool, not memory.
    calls.clear()
    await agent.run("What is today's date? Use your tools to be sure.",
                    on_tool_start=on_tool_start, on_tool_end=on_tool_end)
    check("date question calls get_current_datetime", "get_current_datetime" in calls, f"tools={calls}")

    # Golden 2 — exact math via the calculator (no guessing). We check the TOOL's
    # result (the deterministic part), not the model's prose, which may reformat
    # the number in LaTeX/commas and is fragile to match.
    calls.clear()
    tool_results.clear()
    await agent.run("Compute 18475 * 392 exactly using your calculator tool.",
                    on_tool_start=on_tool_start, on_tool_end=on_tool_end)
    check("math question calls calculate", "calculate" in calls, f"tools={calls}")
    calc_digits = re.sub(r"\D", "", tool_results.get("calculate", ""))
    check("calculate returns the correct value (7242200)", "7242200" in calc_digits,
          f"result={tool_results.get('calculate', '')[:80]!r}")

    # Golden 3 — reality: a create-file task actually lands on disk.
    d = tempfile.mkdtemp()
    target = os.path.join(d, "hello.txt")
    calls.clear()
    await agent.run(f"Create a text file at {target} whose content is exactly: Hello Eval. "
                    "Use the write_file tool.",
                    on_tool_start=on_tool_start, on_tool_end=on_tool_end)
    check("file task actually created the file on disk", os.path.exists(target), f"path={target}")
    if os.path.exists(target):
        with open(target, encoding="utf-8") as f:
            body = f.read()
        check("created file has the expected content", "Hello Eval" in body, f"body={body[:80]!r}")

    # Golden 4 — research (Retrieval + grounding): a factual question must use a
    # source tool and arrive at the right answer (not fabricate from memory).
    research_tools = {"search_wikipedia", "deep_research", "search_web",
                      "search_arxiv", "fetch_webpage"}
    calls.clear()
    ans = await agent.run("Who wrote the novel '1984'? Look it up with a primary "
                          "source (search_wikipedia) and cite it.",
                          on_tool_start=on_tool_start, on_tool_end=on_tool_end)
    check("research task used a source tool (not memory)",
          any(t in research_tools for t in calls), f"tools={calls}")
    check("research answer is grounded/correct (Orwell)", "orwell" in ans.lower(),
          f"ans={ans[:140]}")

    await client.aclose()


async def main() -> None:
    live = "--live" in sys.argv
    model = "hermes3:8b"
    for a in sys.argv:
        if a.startswith("--model="):
            model = a.split("=", 1)[1]

    print("Zero Agent — eval / regression harness")
    await component_checks()
    if live:
        try:
            await live_tasks(model)
        except Exception as exc:  # noqa: BLE001
            check("live tasks completed without error", False, f"{type(exc).__name__}: {exc}")
    else:
        print("\n[2] Live golden tasks SKIPPED (pass --live to run them).")

    print(f"\n=== {_PASS} passed, {_FAIL} failed ===")
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    asyncio.run(main())
