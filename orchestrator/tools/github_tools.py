"""GitHub tools — trending repos, repo info, no API key required.

Uses GitHub's public HTML trending page (scraping with trafilatura) and
the public REST API (unauthenticated, 60 req/h). Optionally set
GITHUB_TOKEN in .env for higher rate limits (5000 req/h).
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone

import httpx

from orchestrator.tools.registry import registry

logger = logging.getLogger("zero_agent.github_tools")

_GH_API = "https://api.github.com"
_TRENDING_URL = "https://github.com/trending/{language}?since={period}"

_CATEGORIES = {
    "ai": ["machine-learning", "deep-learning", "llm", "ai", "gpt", "neural", "transformer",
            "artificial-intelligence", "nlp", "diffusion", "agent", "langchain", "rag"],
    "all": [],
}


def _gh_headers() -> dict[str, str]:
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


async def _fetch_trending_api(language: str = "", period: str = "daily") -> list[dict]:
    """Parse GitHub trending via the public REST search API (no scraping).

    GitHub removed the official Trending API, but the search API with
    'stars' sort + date filter is a good proxy for 'trending today'.
    """
    # "pushed:>YYYY-MM-DD" filters to repos active today/this week.
    from datetime import timedelta
    days = {"daily": 1, "weekly": 7, "monthly": 30}.get(period, 1)
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")

    params: dict = {
        "q": f"pushed:>{since}" + (f" language:{language}" if language else ""),
        "sort": "stars",
        "order": "desc",
        "per_page": 25,
    }
    try:
        async with httpx.AsyncClient(timeout=20, headers=_gh_headers()) as client:
            r = await client.get(f"{_GH_API}/search/repositories", params=params)
            r.raise_for_status()
            data = r.json()
            return data.get("items", [])
    except Exception as exc:
        logger.warning("github_tools: API error: %s", exc)
        return []


def _is_ai_related(repo: dict) -> bool:
    """Heuristic: does this repo look AI/ML/LLM-related?"""
    text = " ".join([
        repo.get("name", ""),
        repo.get("description", "") or "",
        " ".join(repo.get("topics", [])),
    ]).lower()
    return any(kw in text for kw in _CATEGORIES["ai"])


def _format_repo(repo: dict, rank: int) -> str:
    name = repo.get("full_name", "?")
    desc = (repo.get("description") or "").strip()[:150]
    stars = repo.get("stargazers_count", 0)
    lang = repo.get("language") or "—"
    topics = ", ".join(repo.get("topics", [])[:5]) or "—"
    url = repo.get("html_url", "")
    today_stars = repo.get("_today_stars", "")
    today_str = f"  ⭐ +{today_stars} today" if today_stars else ""
    return (
        f"**{rank}. {name}** ({lang})\n"
        f"  {desc}\n"
        f"  ⭐ {stars:,} total stars{today_str}\n"
        f"  Topics: {topics}\n"
        f"  🔗 {url}"
    )


@registry.register
async def get_github_trending(
    category: str = "ai",
    language: str = "",
    period: str = "daily",
    limit: int = 15,
) -> str:
    """Fetch today's trending GitHub repositories.

    Searches by recency + stars. Optionally filter to AI/ML repos or a
    specific programming language. Returns a formatted digest.

    Args:
        category: "ai" (default) — only AI/ML/LLM/agent repos;
                  "all" — every category.
        language: Programming language filter, e.g. "python", "typescript".
                  Empty = all languages.
        period:   "daily" (default), "weekly", or "monthly".
        limit:    Max repos to return (default 15).
    """
    repos = await _fetch_trending_api(language=language, period=period)

    if not repos:
        return (
            "Could not fetch GitHub trending data (API rate limit or network issue).\n"
            "Try again in a few minutes or set GITHUB_TOKEN in .env for higher limits."
        )

    if category == "ai":
        filtered = [r for r in repos if _is_ai_related(r)]
        if not filtered:
            filtered = repos  # fallback: show all if no AI matches
        cat_label = "AI / ML / LLM"
    else:
        filtered = repos
        cat_label = "All categories"

    filtered = filtered[:limit]
    period_label = {"daily": "היום", "weekly": "השבוע", "monthly": "החודש"}.get(period, period)
    lang_label = f" ({language})" if language else ""

    lines = [
        f"## 🔥 GitHub Trending {period_label} — {cat_label}{lang_label}\n",
        f"*{len(filtered)} repositories · {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC*\n",
    ]
    for i, repo in enumerate(filtered, 1):
        lines.append(_format_repo(repo, i))
        lines.append("")

    lines.append(
        "---\n💡 *לפרטים נוספים על פרויקט ספציפי, שאל: \"ספר לי יותר על [שם הrepo]\"*"
    )
    return "\n".join(lines)


@registry.register
async def get_github_repo_info(repo: str) -> str:
    """Get detailed information about a specific GitHub repository.

    Args:
        repo: Repository in "owner/name" format, e.g. "openai/whisper".
              Also accepts a full GitHub URL.
    """
    # Extract owner/name from URL if needed
    repo = repo.strip()
    if "github.com/" in repo:
        m = re.search(r"github\.com/([^/]+/[^/\s?#]+)", repo)
        if m:
            repo = m.group(1)

    if "/" not in repo:
        return f"Invalid repo format '{repo}'. Use 'owner/name', e.g. 'openai/whisper'."

    try:
        async with httpx.AsyncClient(timeout=15, headers=_gh_headers()) as client:
            r = await client.get(f"{_GH_API}/repos/{repo}")
            if r.status_code == 404:
                return f"Repository '{repo}' not found on GitHub."
            r.raise_for_status()
            d = r.json()

            # Also fetch README (first 600 chars)
            readme_text = ""
            try:
                rr = await client.get(f"{_GH_API}/repos/{repo}/readme")
                if rr.status_code == 200:
                    import base64
                    content = rr.json().get("content", "")
                    readme_raw = base64.b64decode(content).decode("utf-8", errors="replace")
                    # Strip markdown headings/badges, keep prose
                    prose = re.sub(r"!\[.*?\]\(.*?\)", "", readme_raw)  # images
                    prose = re.sub(r"\[.*?\]\(.*?\)", lambda m: m.group(0).split("](")[0][1:], prose)
                    prose = re.sub(r"<[^>]+>", "", prose)
                    prose = prose.strip()[:600]
                    readme_text = f"\n\n**README (תחילת):**\n{prose}…"
            except Exception:
                pass

    except Exception as exc:
        return f"Error fetching '{repo}': {type(exc).__name__}: {exc}"

    stars = d.get("stargazers_count", 0)
    forks = d.get("forks_count", 0)
    issues = d.get("open_issues_count", 0)
    lang = d.get("language") or "—"
    topics = ", ".join(d.get("topics", [])[:8]) or "—"
    license_name = (d.get("license") or {}).get("name", "—")
    pushed = (d.get("pushed_at") or "")[:10]
    desc = (d.get("description") or "—").strip()

    return (
        f"## {d['full_name']}\n"
        f"**{desc}**\n\n"
        f"⭐ {stars:,} stars  |  🍴 {forks:,} forks  |  🐛 {issues} open issues\n"
        f"Language: {lang}  |  License: {license_name}  |  Last push: {pushed}\n"
        f"Topics: {topics}\n"
        f"🔗 {d['html_url']}"
        f"{readme_text}"
    )
