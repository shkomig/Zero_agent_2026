"""Deep web-research tool: search -> fetch -> clean-extract -> synthesize.

Where ``search_web`` returns only snippets, ``deep_research`` actually *reads*
several pages: it searches (SearXNG if available, else DuckDuckGo), fetches the
top results concurrently, extracts the clean article text with trafilatura, and
returns a consolidated, source-attributed corpus in ONE tool call. The model
then synthesizes and cross-references the sources.

Everything is local + free: no paid APIs. SearXNG (a self-hosted metasearch
engine) is used when reachable for better, rate-limit-free results, with an
automatic DuckDuckGo fallback so the tool always works.
"""

from __future__ import annotations

import asyncio
import logging
import xml.etree.ElementTree as ET

import httpx

from orchestrator import config
from orchestrator.tools.registry import registry

# `ddgs` is the renamed `duckduckgo-search` (same DDGS().text() API).
try:
    from ddgs import DDGS
except ImportError:  # pragma: no cover
    from duckduckgo_search import DDGS

# trafilatura turns messy HTML into clean article text. Guarded so the agent
# still boots if the dependency is somehow missing (the tool degrades to
# returning search snippets instead of full text).
try:
    import trafilatura
except ImportError:  # pragma: no cover
    trafilatura = None  # type: ignore[assignment]

logger = logging.getLogger("zero_agent.research")

# Wikimedia (and most APIs) BLOCK browser-spoofing UAs ("Mozilla/5.0 (compatible;…)")
# with 403. Their User-Agent policy requires a descriptive bot UA with real contact
# info (project URL + email). This is the compliant form.
_UA = (
    "ZeroAgent/0.1 (https://github.com/shkomig/Zero_agent_2026; "
    "haiatia132@gmail.com) httpx"
)

# --- Source-quality tiering (lightweight, local, deterministic) --------------
# A domain-based provenance signal so the model can CALIBRATE: weight PRIMARY
# evidence over SECONDARY blogs/forums instead of inventing its own credibility
# labels. This is the local, framework-free version of the journal-reputation
# scoring that mature research tools use. Not a truth oracle — just provenance.
_PRIMARY_TLDS = (".gov", ".edu", ".mil", ".int")
_PRIMARY_HOSTS = (
    "arxiv.org", "doi.org", "ncbi.nlm.nih.gov", "pubmed", "who.int", "un.org",
    "oecd.org", "worldbank.org", "imf.org", "europa.eu", "nature.com",
    "science.org", "sciencedirect.com", "ieee.org", "acm.org", "nist.gov",
    "sec.gov", "nasa.gov", "wikipedia.org", "semanticscholar.org",
)
_REPUTABLE_HOSTS = (
    "reuters.com", "apnews.com", "bbc.co", "bbc.com", "nytimes.com", "wsj.com",
    "ft.com", "economist.com", "theguardian.com", "bloomberg.com", "npr.org",
    "scientificamerican.com", "nationalgeographic.com", "technologyreview.com",
    "arstechnica.com", "ieeexplore.ieee.org",
)
_SECONDARY_HOSTS = (
    "medium.com", "substack.com", "reddit.com", "quora.com", "blogspot",
    "wordpress.com", "tumblr", "facebook.com", "twitter.com", "x.com",
    "linkedin.com", "ycombinator.com", "stackexchange.com", "fandom.com",
)


def _source_tier(url: str) -> str:
    """Classify a URL's domain into a provenance tier for calibration."""
    from urllib.parse import urlparse

    host = (urlparse(url).hostname or "").lower()
    if not host:
        return "UNRATED"
    if host.endswith(_PRIMARY_TLDS) or any(h in host for h in _PRIMARY_HOSTS):
        return "PRIMARY"
    if any(h in host for h in _REPUTABLE_HOSTS):
        return "REPUTABLE"
    if any(h in host for h in _SECONDARY_HOSTS):
        return "SECONDARY"
    return "UNRATED"


async def _searxng_search(query: str, n: int) -> list[dict[str, str]]:
    """Query a self-hosted SearXNG instance via its JSON API.

    Raises on any failure so the caller can fall back to DuckDuckGo.
    """
    base = config.SEARXNG_HOST.rstrip("/")
    params = {"q": query, "format": "json"}
    async with httpx.AsyncClient(timeout=8.0) as http:
        resp = await http.get(f"{base}/search", params=params)
        resp.raise_for_status()
        data = resp.json()
    out: list[dict[str, str]] = []
    for item in (data.get("results") or [])[:n]:
        url = item.get("url", "")
        if url:
            out.append(
                {
                    "title": item.get("title", ""),
                    "url": url,
                    "content": item.get("content", ""),
                }
            )
    return out


def _ddg_search(query: str, n: int) -> list[dict[str, str]]:
    """Query DuckDuckGo (sync; run in a worker thread by the caller)."""
    out: list[dict[str, str]] = []
    with DDGS() as ddgs:
        for item in ddgs.text(query, max_results=n):
            url = item.get("href", "")
            if url:
                out.append(
                    {
                        "title": item.get("title", ""),
                        "url": url,
                        "content": item.get("body", ""),
                    }
                )
    return out


async def web_search(query: str, n: int) -> tuple[list[dict[str, str]], str]:
    """Search the web, preferring SearXNG and falling back to DuckDuckGo.

    Returns ``(results, engine_name)``. Each result is ``{title, url, content}``.
    """
    if config.SEARXNG_HOST.strip():
        try:
            results = await _searxng_search(query, n)
            if results:
                return results, "SearXNG"
        except Exception as exc:  # noqa: BLE001 - any failure => fall back
            logger.debug("SearXNG unavailable (%s); using DuckDuckGo", exc)
    try:
        results = await asyncio.to_thread(_ddg_search, query, n)
    except Exception as exc:  # noqa: BLE001
        logger.warning("DuckDuckGo search failed: %s", exc)
        return [], "DuckDuckGo"
    return results, "DuckDuckGo"


async def _fetch_and_extract(http: httpx.AsyncClient, url: str) -> str | None:
    """Fetch a URL and return its clean article text, or ``None`` on failure."""
    try:
        resp = await http.get(url, headers={"User-Agent": _UA})
    except httpx.HTTPError as exc:
        logger.debug("fetch failed for %s: %s", url, exc)
        return None
    if resp.status_code != 200 or not resp.text:
        return None
    if trafilatura is None:
        return None
    # trafilatura.extract is CPU-bound and synchronous — keep it off the loop.
    try:
        text = await asyncio.to_thread(
            trafilatura.extract,
            resp.text,
            include_comments=False,
            include_tables=True,
        )
    except Exception as exc:  # noqa: BLE001 - never let one page sink the run
        logger.debug("extract failed for %s: %s", url, exc)
        return None
    return (text or "").strip() or None


@registry.register
async def deep_research(query: str, max_sources: int = 0) -> str:
    """Research a topic in depth by reading several web pages, not just snippets.

    Use this (instead of search_web) when the user wants thorough/current
    information, a comparison, fact-checking, or analysis that needs real
    sources. It searches the web, opens the top pages, extracts their full
    article text, and returns a consolidated, numbered corpus. After it returns,
    synthesize a clear answer, cross-reference the sources, note any
    disagreements, and cite sources by their [number] and URL.

    Args:
        query: The research question or topic, in natural language.
        max_sources: How many pages to read (1-10). 0 uses the configured default.
    """
    n = max_sources or config.RESEARCH_MAX_SOURCES
    n = max(1, min(n, 10))

    # Over-fetch search hits a bit: some pages fail to fetch/extract, and we
    # de-duplicate by URL before reading.
    results, engine = await web_search(query, n * 2)
    if not results:
        return f"No web results found for {query!r}. (Searched via {engine}.)"

    seen: set[str] = set()
    picks: list[dict[str, str]] = []
    for r in results:
        if r["url"] not in seen:
            seen.add(r["url"])
            picks.append(r)
        if len(picks) >= n:
            break

    async with httpx.AsyncClient(
        timeout=config.RESEARCH_FETCH_TIMEOUT, follow_redirects=True
    ) as http:
        texts = await asyncio.gather(*(_fetch_and_extract(http, r["url"]) for r in picks))

    blocks: list[str] = []
    for r, text in zip(picks, texts):
        # Fall back to the search snippet if a page couldn't be read in full.
        body = (text or r.get("content") or "").strip()
        if not body:
            continue
        body = body[: config.RESEARCH_PER_SOURCE_CHARS]
        idx = len(blocks) + 1
        title = r.get("title") or "(untitled)"
        tier = _source_tier(r["url"])
        blocks.append(f"[{idx}] ({tier}) {title}\nURL: {r['url']}\n{body}")

    if not blocks:
        return (
            f"Found results for {query!r} via {engine}, but could not extract "
            "readable content from any source. Try rephrasing the query."
        )

    tiers = ", ".join(sorted({b.split("(", 1)[1].split(")", 1)[0] for b in blocks}))
    header = (
        f"Deep research on: {query!r}\n"
        f"Search engine: {engine}. Read {len(blocks)} source(s). Tiers present: {tiers}.\n"
        "Each source is tagged with a provenance tier: PRIMARY (gov/edu/official/"
        "journals/arXiv/Wikipedia), REPUTABLE (established news/orgs), SECONDARY "
        "(blogs/forums/social), UNRATED (unknown).\n"
        "Now synthesize a clear, well-structured answer and CALIBRATE to provenance: "
        "lead with PRIMARY/REPUTABLE evidence; treat SECONDARY/UNRATED claims as "
        "weak and say so; if a key figure rests only on a SECONDARY source or a "
        "single source, hedge it explicitly. Cross-reference, flag disagreements, "
        "do NOT attach numeric confidence a primary source doesn't justify, and "
        "cite every claim by [number] and URL.\n"
        "=== SOURCES ==="
    )
    logger.info("deep_research %r via %s: %d sources read", query[:60], engine, len(blocks))
    return header + "\n\n" + "\n\n".join(blocks)


@registry.register
async def search_wikipedia(query: str, max_results: int = 4) -> str:
    """Look up a topic on Wikipedia — a PRIMARY, authoritative reference for
    established facts, definitions, history, people, places, and events.

    Prefer this over web blogs/news for settled facts. Returns the intro extract
    of the top matching articles; cite by [number] and URL.

    Args:
        query: What to look up, in natural language.
        max_results: How many articles to return (1-6).
    """
    n = max(1, min(max_results or 4, 6))
    api = "https://en.wikipedia.org/w/api.php"
    try:
        async with httpx.AsyncClient(timeout=10.0, headers={"User-Agent": _UA}) as http:
            sr = await http.get(api, params={
                "action": "query", "list": "search", "srsearch": query,
                "srlimit": n, "format": "json",
            })
            sr.raise_for_status()
            hits = sr.json().get("query", {}).get("search") or []
            if not hits:
                return f"No Wikipedia articles found for {query!r}."
            titles = [h["title"] for h in hits]
            ex = await http.get(api, params={
                "action": "query", "prop": "extracts", "exintro": 1,
                "explaintext": 1, "redirects": 1, "titles": "|".join(titles),
                "format": "json",
            })
            ex.raise_for_status()
            pages = ex.json().get("query", {}).get("pages", {})
    except Exception as exc:  # noqa: BLE001 - never break a turn
        logger.warning("wikipedia lookup failed: %s", exc)
        return f"Wikipedia lookup failed ({type(exc).__name__}). Try deep_research instead."

    by_title = {p.get("title"): p for p in pages.values()}
    blocks: list[str] = []
    for t in titles:  # preserve relevance order
        p = by_title.get(t)
        extract = ((p.get("extract") if p else "") or "").strip()
        if not extract:
            continue
        extract = extract[: config.RESEARCH_PER_SOURCE_CHARS]
        url = "https://en.wikipedia.org/wiki/" + t.replace(" ", "_")
        blocks.append(f"[{len(blocks) + 1}] {t} (Wikipedia)\nURL: {url}\n{extract}")
    if not blocks:
        return f"No readable Wikipedia extracts for {query!r}."
    header = (
        f"Wikipedia results for {query!r} ({len(blocks)} article(s)). "
        "Authoritative reference — cite by [number] and URL.\n=== SOURCES ==="
    )
    logger.info("search_wikipedia %r: %d articles", query[:60], len(blocks))
    return header + "\n\n" + "\n\n".join(blocks)


@registry.register
async def search_arxiv(query: str, max_results: int = 5) -> str:
    """Search arXiv for scientific/technical papers — PRIMARY academic sources.

    Use this for scientific, ML/AI, physics, math, or quantitative claims instead
    of blogs. Returns title, authors, date, abstract, and link; cite by [number]
    and URL.

    Args:
        query: Topic or keywords.
        max_results: How many papers (1-10).
    """
    n = max(1, min(max_results or 5, 10))
    api = "https://export.arxiv.org/api/query"
    try:
        async with httpx.AsyncClient(
            timeout=12.0, headers={"User-Agent": _UA}, follow_redirects=True
        ) as http:
            r = await http.get(api, params={
                "search_query": f"all:{query}", "start": 0,
                "max_results": n, "sortBy": "relevance",
            })
            r.raise_for_status()
            xml_text = r.text
    except Exception as exc:  # noqa: BLE001
        logger.warning("arxiv lookup failed: %s", exc)
        return f"arXiv lookup failed ({type(exc).__name__})."

    ns = {"a": "http://www.w3.org/2005/Atom"}
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return "arXiv returned an unparseable response."

    blocks: list[str] = []
    for e in root.findall("a:entry", ns):
        title = (e.findtext("a:title", default="", namespaces=ns) or "").strip().replace("\n", " ")
        summary = (e.findtext("a:summary", default="", namespaces=ns) or "").strip().replace("\n", " ")
        link = (e.findtext("a:id", default="", namespaces=ns) or "").strip()
        published = (e.findtext("a:published", default="", namespaces=ns) or "")[:10]
        authors = ", ".join(
            a.findtext("a:name", default="", namespaces=ns) or "" for a in e.findall("a:author", ns)
        )[:200]
        if not title:
            continue
        summary = summary[: config.RESEARCH_PER_SOURCE_CHARS]
        blocks.append(
            f"[{len(blocks) + 1}] {title} ({published})\nAuthors: {authors}\n"
            f"URL: {link}\n{summary}"
        )
    if not blocks:
        return f"No arXiv papers found for {query!r}."
    header = (
        f"arXiv results for {query!r} ({len(blocks)} paper(s)). "
        "Primary academic sources — cite by [number] and URL.\n=== SOURCES ==="
    )
    logger.info("search_arxiv %r: %d papers", query[:60], len(blocks))
    return header + "\n\n" + "\n\n".join(blocks)


@registry.register
async def search_sec_edgar(query: str, forms: str = "", max_results: int = 5) -> str:
    """Search SEC EDGAR full-text — PRIMARY financial filings from US public
    companies (10-K annual, 10-Q quarterly, 8-K events, etc.).

    Authoritative for company financials, risk factors, and official disclosures
    — prefer over finance blogs/news for hard numbers. Returns matching filings
    with links; open a URL (e.g. with fetch_webpage) to read one. Cite by
    [number] and URL.

    Args:
        query: Company, topic, or phrase (e.g. "NVIDIA data center revenue").
        forms: Optional filing-type filter, e.g. "10-K" or "10-K,10-Q". Empty=all.
        max_results: How many filings (1-10).
    """
    n = max(1, min(max_results or 5, 10))
    params: dict[str, str] = {"q": query}
    if forms.strip():
        params["forms"] = forms.strip()
    ua = "ZeroAgent/0.1 (local research; contact local@example.com)"
    hits: list = []
    try:
        async with httpx.AsyncClient(
            timeout=12.0, headers={"User-Agent": ua}, follow_redirects=True
        ) as http:
            # SEC's full-text endpoint occasionally returns a transient 500 — retry.
            for attempt in range(3):
                r = await http.get("https://efts.sec.gov/LATEST/search-index", params=params)
                if r.status_code >= 500 and attempt < 2:
                    await asyncio.sleep(0.8 * (attempt + 1))
                    continue
                r.raise_for_status()
                hits = r.json().get("hits", {}).get("hits", [])
                break
    except Exception as exc:  # noqa: BLE001
        logger.warning("sec edgar lookup failed: %s", exc)
        return f"SEC EDGAR lookup failed ({type(exc).__name__}). It can be flaky — try again."

    blocks: list[str] = []
    for h in hits[:n]:
        src = h.get("_source", {})
        names = "; ".join(src.get("display_names") or []) or "(unknown filer)"
        form = src.get("form") or src.get("file_type") or "?"
        date = src.get("file_date") or ""
        adsh = src.get("adsh") or ""
        ciks = src.get("ciks") or []
        _id = h.get("_id", "")
        doc = _id.split(":", 1)[1] if ":" in _id else ""
        url = ""
        if ciks and adsh and doc:
            try:
                url = (
                    f"https://www.sec.gov/Archives/edgar/data/{int(ciks[0])}/"
                    f"{adsh.replace('-', '')}/{doc}"
                )
            except (ValueError, TypeError):
                url = ""
        blocks.append(f"[{len(blocks) + 1}] {names} — {form} ({date})\nURL: {url}")
    if not blocks:
        return f"No SEC filings found for {query!r}."
    header = (
        f"SEC EDGAR filings for {query!r} ({len(blocks)} result(s)). "
        "PRIMARY financial disclosures — open a URL to read the filing; "
        "cite by [number] and URL.\n=== SOURCES ==="
    )
    logger.info("search_sec_edgar %r: %d filings", query[:60], len(blocks))
    return header + "\n\n" + "\n\n".join(blocks)


@registry.register
async def search_hacker_news(query: str, max_results: int = 5) -> str:
    """Search Hacker News discussions — community sentiment and shared links on
    tech, AI, startups, and software.

    SECONDARY source (opinions/discussion, NOT authoritative) — good for what
    practitioners are actually saying and for surfacing primary links people
    share. Cite by [number] and URL.

    Args:
        query: Topic or keywords.
        max_results: How many stories (1-10).
    """
    n = max(1, min(max_results or 5, 10))
    try:
        async with httpx.AsyncClient(timeout=10.0, headers={"User-Agent": _UA}) as http:
            r = await http.get(
                "https://hn.algolia.com/api/v1/search",
                params={"query": query, "tags": "story", "hitsPerPage": n},
            )
            r.raise_for_status()
            hits = r.json().get("hits", [])
    except Exception as exc:  # noqa: BLE001
        logger.warning("hacker news lookup failed: %s", exc)
        return f"Hacker News lookup failed ({type(exc).__name__})."

    blocks: list[str] = []
    for h in hits[:n]:
        title = (h.get("title") or "").strip()
        if not title:
            continue
        oid = h.get("objectID", "")
        ext = h.get("url") or ""
        pts = h.get("points", 0)
        nc = h.get("num_comments", 0)
        date = (h.get("created_at") or "")[:10]
        line = (
            f"[{len(blocks) + 1}] {title} ({date}, {pts} pts, {nc} comments)\n"
            f"HN thread: https://news.ycombinator.com/item?id={oid}"
        )
        if ext:
            line += f"\nLink: {ext}"
        blocks.append(line)
    if not blocks:
        return f"No Hacker News stories found for {query!r}."
    header = (
        f"Hacker News results for {query!r} ({len(blocks)} story/stories). "
        "Community discussion (SECONDARY — opinions/sentiment); "
        "cite by [number] and URL.\n=== SOURCES ==="
    )
    logger.info("search_hacker_news %r: %d stories", query[:60], len(blocks))
    return header + "\n\n" + "\n\n".join(blocks)
