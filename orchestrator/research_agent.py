"""Iterative deep-research agent — search → assess gaps → search more → synthesize.

A single `deep_research` call gives ONE round of sources. Hard questions need
more: this runs MULTIPLE rounds (the "iterate-until-confident" pattern), so the
answer rests on broader, cross-checked, provenance-tiered evidence instead of the
first page of results. The model only PLANS (decompose), ASSESSES gaps, and
SYNTHESIZES; retrieval is done by the existing research tools. Calibration is
enforced in the synthesis prompt and a faithfulness pass — the project's
research-reliability goal (DeepWeb-Bench style: evidence-matched confidence).

Local + framework-free: just the Ollama client + the tool registry + verify.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from orchestrator import config, verify
from orchestrator.models.ollama_client import OllamaError
from orchestrator.supervisor import Supervisor  # reuse the robust JSON-array parser

logger = logging.getLogger("zero_agent.research_agent")

_DECOMPOSE_PROMPT = (
    "You are a research planner. Break the user's question into 2-4 focused, "
    "search-ready SUB-QUERIES that together cover the evidence needed to answer it "
    "(definitions, key facts, opposing views, numbers, recent developments). "
    "Respond with ONLY a JSON array of short query strings, nothing else."
)

_ASSESS_PROMPT = (
    "You are a research editor checking COVERAGE. Given the QUESTION and the "
    "EVIDENCE gathered so far, decide what is still missing to answer it well — "
    "unaddressed sub-claims, unresolved contradictions, missing primary/quantitative "
    "evidence, or stale data needing a recency check. Respond with ONLY a JSON array "
    "of up to 3 NEW follow-up search queries to fill those gaps, or an empty array "
    "[] if the evidence is already sufficient. No prose."
)

_SYNTH_PROMPT = (
    "You are a meticulous research analyst. Using ONLY the SOURCES below, write a "
    "clear, well-structured answer to the QUESTION.\n"
    "RULES (calibration matters):\n"
    "- Each source is tagged with a provenance tier (PRIMARY / REPUTABLE / "
    "SECONDARY / UNRATED). LEAD with PRIMARY/REPUTABLE evidence; treat SECONDARY/"
    "UNRATED as weak and SAY SO.\n"
    "- A key figure resting on a single source, or only on SECONDARY/UNRATED "
    "sources, MUST be hedged explicitly ('per one secondary source…').\n"
    "- Do NOT state facts, numbers, dates, or quotes not present in the sources, "
    "and do NOT attach a numeric confidence/probability the sources don't justify.\n"
    "- Cross-reference, flag disagreements between sources, and cite every claim by "
    "its source URL.\n"
    "- End with a short, honest 'Confidence & gaps' note (what is well-evidenced vs "
    "thin/uncertain).\n"
)

EventCallback = Callable[[str, str], Awaitable[None]]


class ResearchAgent:
    def __init__(self, client, registry, *, on_event: EventCallback | None = None) -> None:
        self.client = client
        self.registry = registry
        self.on_event = on_event
        # Decompose/assess are structured JSON calls — thinking OFF for clean output.
        self.client.think = False
        self.max_rounds = max(1, config.RESEARCH_MAX_ROUNDS)
        self.max_subqueries = max(1, config.RESEARCH_MAX_SUBQUERIES)

    async def _emit(self, kind: str, payload: str) -> None:
        logger.info("research %s: %s", kind, payload[:100])
        if self.on_event:
            try:
                await self.on_event(kind, payload)
            except Exception:  # noqa: BLE001 - a UI hook must never break the run
                logger.exception("on_event hook failed")

    async def _ask_json(self, system: str, user: str) -> list[str] | None:
        try:
            resp = await self.client.chat(
                [{"role": "system", "content": system},
                 {"role": "user", "content": user}],
                tools=None,
                options={"temperature": 0.0},
            )
        except OllamaError as exc:
            await self._emit("error", f"planner call failed: {exc}")
            return None
        return Supervisor._parse_json_array(resp.message.content)

    async def investigate(self, question: str) -> str:
        await self._emit("planning", question)
        sub_queries = await self._ask_json(_DECOMPOSE_PROMPT, f"QUESTION: {question}")
        if not sub_queries:
            sub_queries = [question]
        sub_queries = sub_queries[: self.max_subqueries]
        await self._emit("plan_ready", "\n".join(f"{i+1}. {q}" for i, q in enumerate(sub_queries)))

        corpus: list[str] = []
        asked: set[str] = set()
        for rnd in range(1, self.max_rounds + 1):
            pending = [q for q in sub_queries if q.strip().lower() not in asked][: self.max_subqueries]
            if not pending:
                break
            for q in pending:
                asked.add(q.strip().lower())
                await self._emit("query", q)
                try:
                    res = await self.registry.execute("deep_research", {"query": q})
                except Exception as exc:  # noqa: BLE001
                    res = f"(search failed: {exc})"
                corpus.append(f"### Sub-query: {q}\n{res}")
                await self._emit("query_done", q)

            if rnd >= self.max_rounds:
                break
            joined = "\n\n".join(corpus)[: config.RESEARCH_SYNTH_MAX_CHARS]
            gaps = await self._ask_json(_ASSESS_PROMPT, f"QUESTION: {question}\n\nEVIDENCE:\n{joined}")
            if not gaps:
                await self._emit("coverage_ok", f"round {rnd}: evidence sufficient")
                break
            await self._emit("gaps", f"round {rnd}: {len(gaps)} follow-up(s)")
            sub_queries = gaps

        await self._emit("synthesizing", f"{len(corpus)} source-set(s)")
        sources_text = "\n\n".join(corpus)[: config.RESEARCH_SYNTH_MAX_CHARS]
        answer = await self._synthesize(question, sources_text)

        # Faithfulness + calibration pass; one corrective re-synthesis on FAIL.
        ok, critique = await verify.check_faithfulness(self.client, answer, sources_text)
        if not ok:
            await self._emit("recalibrating", critique[:120])
            answer = await self._synthesize(question, sources_text, fix=critique)
        await self._emit("done", "")
        return answer

    async def _synthesize(self, question: str, sources: str, *, fix: str = "") -> str:
        user = f"QUESTION: {question}\n\nSOURCES:\n{sources}"
        if fix:
            user += (
                f"\n\nA reviewer flagged this faithfulness/calibration problem in your "
                f"previous draft — FIX it (remove/hedge unsupported or over-confident "
                f"claims, lean on PRIMARY sources): {fix}"
            )
        try:
            resp = await self.client.chat(
                [{"role": "system", "content": _SYNTH_PROMPT},
                 {"role": "user", "content": user}],
                tools=None,
            )
        except OllamaError as exc:
            return f"⚠️ Research synthesis failed (Ollama): {exc}"
        return (resp.message.content or "").strip() or "(no synthesis produced)"
