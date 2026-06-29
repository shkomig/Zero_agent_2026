"""Deep Research Engine — parallel multi-angle research with synthesis.

Runs multiple research angles concurrently (academic, industry, GitHub, news,
Wikipedia) and synthesizes into a structured Hebrew report with sources and
confidence score. Integrates with Projects for persistent memory.

Usage (from app.py):
    result = await run_deep_research(question, client, project_name)
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("zero_agent.deep_research")

# ── Research angles ────────────────────────────────────────────────────────────

_ANGLES: list[dict[str, Any]] = [
    {
        "key": "academic",
        "label": "📚 אקדמי",
        "suffix": "research paper study site:arxiv.org OR site:scholar.google.com 2025 2026",
        "tool": "web_search",
    },
    {
        "key": "industry",
        "label": "🏢 תעשייה",
        "suffix": "industry analysis trends market 2026",
        "tool": "web_search",
    },
    {
        "key": "github",
        "label": "💻 קוד פתוח",
        "suffix": "open source implementation github",
        "tool": "web_search",
    },
    {
        "key": "news",
        "label": "📰 חדשות",
        "suffix": "latest news developments announcement 2026",
        "tool": "web_search",
    },
    {
        "key": "wikipedia",
        "label": "📖 רקע",
        "tool": "wikipedia",
    },
]

_SYNTH_PROMPT = """\
אתה חוקר בכיר. קיבלת תוצאות ממחקר מקבילי ממספר מקורות על: "{question}"

{combined}

כתוב דוח מחקר מובנה בעברית:

## ממצא מרכזי
(2-3 משפטים — התובנה המרכזית)

## לפי מקור
(בולט לכל זווית: מה מצאנו, עם ציון מקורות ספציפיים)

## מסקנות
(3-5 תובנות מעשיות)

## רמת ביטחון: X%
(הסבר קצר מה מוודא טוב ומה לא ברור)

## מקורות
(רשימת קישורים/מקורות שהוזכרו)

היה ספציפי. הימנע מהכללות. ציין מקורות inline."""


# ── Core engine ────────────────────────────────────────────────────────────────

async def _run_angle(angle: dict[str, Any], question: str) -> dict[str, Any]:
    """Run a single research angle. Returns {key, label, text, error}."""
    key = angle["key"]
    label = angle["label"]

    try:
        if angle["tool"] == "wikipedia":
            from orchestrator.tools.research_tools import search_wikipedia
            text = await search_wikipedia(question, max_results=3)

        else:  # web_search
            from orchestrator.tools.research_tools import web_search
            q = f"{question} {angle['suffix']}"
            results, _source = await web_search(q, n=5)
            lines = []
            for r in results:
                lines.append(f"• **{r.get('title','')}** ({r.get('source','')})\n  {r.get('body','')}")
                if r.get("href"):
                    lines.append(f"  🔗 {r['href']}")
            text = "\n".join(lines) if lines else "אין תוצאות."

        return {"key": key, "label": label, "text": text, "error": None}

    except Exception as exc:
        logger.warning("deep_research angle '%s' failed: %s", key, exc)
        return {"key": key, "label": label, "text": "", "error": str(exc)}


async def run_deep_research(
    question: str,
    client: Any,
    project_name: str = "",
    angles: list[str] | None = None,
) -> dict[str, Any]:
    """Run parallel multi-angle research and synthesize into a Hebrew report.

    Args:
        question:     The research question.
        client:       OllamaClient instance (used for synthesis).
        project_name: If set, saves the report to this project in ChromaDB.
        angles:       Subset of angle keys to run. Default: all 5.

    Returns a dict with: question, report (str), sources_by_angle, elapsed, saved.
    """
    started = datetime.now(timezone.utc)

    active_angles = [
        a for a in _ANGLES
        if angles is None or a["key"] in angles
    ]

    # Run all angles in parallel
    angle_results = await asyncio.gather(
        *[_run_angle(a, question) for a in active_angles]
    )

    # Build synthesis input
    sections: list[str] = []
    sources_by_angle: dict[str, str] = {}
    for r in angle_results:
        if r["error"] or not r["text"].strip():
            continue
        sections.append(f"### {r['label']}\n{r['text']}")
        sources_by_angle[r["key"]] = r["text"]

    if not sections:
        return {
            "question": question,
            "report": "❌ לא נמצאו תוצאות ממחקר — בדוק חיבור לאינטרנט.",
            "sources_by_angle": {},
            "elapsed": 0,
            "saved": False,
            "timestamp": started.isoformat(),
        }

    combined = "\n\n".join(sections)
    synth_prompt = _SYNTH_PROMPT.format(question=question, combined=combined)

    # Synthesize
    messages = [{"role": "user", "content": synth_prompt}]
    resp = await client.chat(messages, stream=False)
    report = (resp.get("content") or "").strip()

    elapsed = round((datetime.now(timezone.utc) - started).total_seconds(), 1)

    # Save to project
    saved = False
    if project_name and report:
        try:
            from orchestrator.projects import get_project_store
            store = get_project_store(client)
            doc = (
                f"# מחקר: {question}\n"
                f"תאריך: {started.strftime('%Y-%m-%d')}\n\n"
                f"{report}"
            )
            await store.add_knowledge(project_name, doc)
            saved = True
            logger.info("deep_research: saved to project '%s'", project_name)
        except Exception as exc:
            logger.warning("deep_research: failed to save to project: %s", exc)

    return {
        "question": question,
        "report": report,
        "sources_by_angle": sources_by_angle,
        "elapsed": elapsed,
        "saved": saved,
        "project": project_name,
        "timestamp": started.isoformat(),
    }
