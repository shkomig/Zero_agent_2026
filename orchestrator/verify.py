"""Self-verification — the Reflexion-style "evaluate" step (learn-layer phase 2).

After the agent says it's done, a strict reviewer checks whether the task was
ACTUALLY accomplished/verified or merely CLAIMED done — the recurring failure
where the agent reports "success" / "it works" without testing the real output
(e.g. a translation server that returns garbage but answers HTTP 200). If the
result looks unverified, the caller gives the agent ONE corrective iteration to
actually test and fix it before the answer is returned.

Runs on the local Ollama server like everything else. Designed to never block:
any verifier failure is treated as "OK" so a hiccup can't stall a reply.
"""

from __future__ import annotations

import logging

from orchestrator.models.ollama_client import OllamaClient, OllamaError

logger = logging.getLogger("zero_agent.verify")

_VERIFY_PROMPT = (
    "You are a STRICT QA reviewer. A user asked an agent to do a task; below are "
    "the request and the agent's final answer. Decide whether the task was "
    "ACTUALLY accomplished and VERIFIED, or merely CLAIMED done.\n"
    "Mark it FAIL if you see any of:\n"
    "- claims success ('done', 'it works', 'success') WITHOUT testing the real "
    "output;\n"
    "- built/started something (server, script, file, system) but never verified "
    "it produces the CORRECT result;\n"
    "- relied on a tool returning 'success' without checking the content is right.\n"
    "If the task is genuinely complete AND verified, OR it was a simple "
    "question/chat that needs no verification, respond with exactly: OK\n"
    "Otherwise respond with exactly: FAIL: <one short sentence on what to actually "
    "test or fix>\n\n"
    "User request: {user}\n"
    "Tools used this run: {tools}\n"
    "Agent's final answer:\n{answer}"
)


async def verify_answer(
    client: OllamaClient, user_msg: str, answer: str, tools_used: list[str]
) -> tuple[bool, str]:
    """Return ``(ok, critique)``.

    ``ok`` is True when the task is complete/verified or trivial; False with a
    one-line ``critique`` when it looks merely claimed-done. Never raises — a
    verifier failure returns ``(True, "")`` so it can't block the reply.
    """
    try:
        resp = await client.chat(
            [
                {
                    "role": "user",
                    "content": _VERIFY_PROMPT.format(
                        user=(user_msg or "").strip()[:1500],
                        tools=", ".join(tools_used) or "(none)",
                        answer=(answer or "").strip()[:2000],
                    ),
                }
            ],
            options={"temperature": 0.0},
        )
    except OllamaError:
        logger.exception("verify chat failed; treating as OK")
        return True, ""

    raw = (resp.message.content or "").strip()
    if raw.upper().startswith("FAIL"):
        critique = raw.split(":", 1)[1].strip() if ":" in raw else ""
        return False, critique or "the result was not actually verified"
    return True, ""


_FAITHFULNESS_PROMPT = (
    "You are a STRICT fact-checker auditing a research answer for FAITHFULNESS and "
    "CALIBRATION (how well its confidence matches the evidence). Below are SOURCES "
    "(the raw text the agent actually retrieved) and the agent's ANSWER.\n"
    "Mark FAIL if the answer:\n"
    "- states facts, figures, dates, names, or quotes NOT found in the sources;\n"
    "- presents training-memory knowledge as if it came from the sources;\n"
    "- CALIBRATION: states a contested or quantitative claim with high confidence "
    "when the sources are thin, single-source, or disagree — confidence must match "
    "the evidence; a key claim resting on only ONE source should be hedged as such;\n"
    "- attaches numeric confidence/probabilities the sources don't justify, or "
    "presents a SECONDARY source (blog/forum/SEO/news) as if it were established/"
    "primary fact without saying so.\n"
    "If every material claim is supported by the sources AND its confidence is "
    "calibrated to them (or is clearly labelled as the agent's own inference/"
    "uncertainty / single-source), respond with exactly: OK\n"
    "Otherwise respond with exactly: FAIL: <the specific unsupported or "
    "over-confident claim(s)>\n\n"
    "SOURCES:\n{sources}\n\nANSWER:\n{answer}"
)


async def check_faithfulness(
    client: OllamaClient,
    answer: str,
    sources: str,
    *,
    max_source_chars: int = 6000,
) -> tuple[bool, str]:
    """Return ``(ok, critique)`` for whether ``answer`` is grounded in ``sources``.

    Used on research turns: a reviewer sees ONLY the drafted answer and the raw
    retrieved source text and flags claims the sources don't support (the
    "confident but unsourced / false-precision" failure). Never raises — any
    failure returns ``(True, "")`` so it can't block a reply.
    """
    sources = (sources or "").strip()
    answer = (answer or "").strip()
    if not sources or not answer:
        return True, ""
    try:
        resp = await client.chat(
            [
                {
                    "role": "user",
                    "content": _FAITHFULNESS_PROMPT.format(
                        sources=sources[:max_source_chars],
                        answer=answer[:2500],
                    ),
                }
            ],
            options={"temperature": 0.0},
        )
    except OllamaError:
        logger.exception("faithfulness check failed; treating as OK")
        return True, ""

    raw = (resp.message.content or "").strip()
    if raw.upper().startswith("FAIL"):
        critique = raw.split(":", 1)[1].strip() if ":" in raw else ""
        return False, critique or "some claims are not supported by the retrieved sources"
    return True, ""
