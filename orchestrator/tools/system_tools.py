"""Core system tools the agent can call to interact with the local machine.

Every tool here is registered against the shared ``registry`` and is built to
be *safe by default*: bounded output, timeouts, and a deny-list for obviously
destructive shell commands.
"""

from __future__ import annotations

import asyncio
import json
import locale
import os
import re
import signal
import subprocess
import sys
from pathlib import Path

import httpx

# trafilatura turns messy HTML into clean article text. Guarded so the agent
# still boots if it is missing (fetch_webpage then returns the raw body).
try:
    import trafilatura
except ImportError:  # pragma: no cover
    trafilatura = None  # type: ignore[assignment]

# pypdf turns a PDF into plain text for read_pdf. Guarded so the agent still
# boots if it's missing (read_pdf then returns a clear install hint).
try:
    import pypdf
except ImportError:  # pragma: no cover
    pypdf = None  # type: ignore[assignment]

# `duckduckgo-search` was renamed to `ddgs` (same DDGS().text() API). Prefer the
# new package if installed, fall back to the legacy one.
try:
    from ddgs import DDGS
except ImportError:  # pragma: no cover - depends on which package is installed
    from duckduckgo_search import DDGS

from orchestrator import config
from orchestrator.tools.registry import registry

# Patterns that should never be executed via the autonomous loop. This is a
# guard-rail for the PoC, NOT a complete sandbox. Tighten before granting the
# agent unsupervised access.
_DANGEROUS_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\brm\s+-rf\b",
        r"\brmdir\b.*\b/s\b",
        r"\bdel\b.*\b/s\b",
        r"\bformat\b",
        r"\bmkfs\b",
        r"\bshutdown\b",
        r"\breboot\b",
        r"\bRemove-Item\b.*-Recurse",
        r":\s*\)\s*\{.*\}\s*;.*:",  # fork bomb
        r"\b(curl|wget|iwr|Invoke-WebRequest)\b.*\|\s*(sh|bash|iex|Invoke-Expression)",
        r">\s*/dev/sd[a-z]",
        r"\bdiskpart\b",
    )
)

# Commands that start a LONG-RUNNING server/process. execute_terminal_command
# has a short wall-clock timeout, so running one of these here blocks the agent
# until it times out and then kills the server — the recurring "agent stuck,
# GPU idle" failure. These must go through launch_program (detached, returns
# immediately). Matched as a hard guard so the model can't make this mistake.
_SERVER_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\buvicorn\b",
        r"\bgunicorn\b",
        r"\bhypercorn\b",
        r"\bwaitress-serve\b",
        r"\bflask\s+run\b",
        r"\bstreamlit\s+run\b",
        r"\bchainlit\s+run\b",
        r"\bgradio\b",
        r"\bhttp\.server\b",
        r"\bmanage\.py\s+runserver\b",
        r"\bollama\s+serve\b",
        r"\bnpm\s+(run\s+)?(dev|start|serve|preview)\b",
        r"\b(pnpm|yarn|bun)\s+(run\s+)?(dev|start|serve)\b",
        r"\b(next|vite|nuxt|astro)\s+dev\b",
        # `python ...server.py` / `...app.py` / `...gui.py` / `...launcher.py` —
        # ad-hoc servers, GUIs, bots, and launchers all run forever and must be
        # detached. Broad keyword set so the model can't sneak a blocking process
        # (e.g. a Tkinter chat GUI or a local-LLM launcher) past the guard.
        r"\bpython[0-9.]*(?:\.exe)?\s+[^|&<>]*"
        r"(server|app|main|api|gui|ui|launch|launcher|bot|chat|serve|daemon"
        r"|worker|listen|watch|window|webui|web_ui)[^|&<>]*\.py\b",
        # START_*.bat / launch*.bat / run*.bat / serve*.cmd — launcher scripts
        # whose whole job is to start something long-running.
        r"\b(start|launch|run|serve)[^|&<>]*\.(bat|cmd|ps1)\b",
        r"\b[^|&<>]*(server|launcher|gui)[^|&<>]*\.(bat|cmd|ps1)\b",
    )
)


def _kill_process_tree(proc: "subprocess.Popen") -> None:
    """Kill a timed-out command AND all of its descendants, so a grandchild
    holding the output pipe can't block the agent forever."""
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        proc.kill()
    except OSError:
        pass


def _decode(raw: bytes | None) -> str:
    """Decode shell output bytes without ever raising.

    Windows console tools (dir, etc.) emit text in the OEM codepage, not UTF-8.
    We try UTF-8 first, then the system's preferred/OEM encodings, and finally
    fall back to a never-failing replacement decode so the agent loop survives
    any byte sequence (Hebrew, emoji, binary noise, ...).
    """
    if not raw:
        return ""

    candidates = ["utf-8"]
    # On Windows, the active OEM codepage (e.g. cp862/cp437 for Hebrew systems).
    oem = getattr(sys.stdout, "encoding", None) or locale.getpreferredencoding(False)
    if oem and oem.lower() not in candidates:
        candidates.append(oem)

    for enc in candidates:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    # Guaranteed to succeed: replace anything undecodable.
    return raw.decode("utf-8", errors="replace")


@registry.register
def read_file(filepath: str) -> str:
    """Read and return the UTF-8 text contents of a local file.

    Args:
        filepath: Path to the file to read (absolute or relative to the cwd).
    """
    path = Path(filepath).expanduser()
    if not path.exists():
        return f"Error: file not found: {path}"
    if not path.is_file():
        return f"Error: not a regular file: {path}"

    size = path.stat().st_size
    if size > config.READ_FILE_MAX_BYTES:
        return (
            f"Error: file is too large ({size} bytes, limit "
            f"{config.READ_FILE_MAX_BYTES}). Refusing to load it all into context."
        )

    text = path.read_text(encoding="utf-8", errors="replace")
    return f"--- contents of {path} ({size} bytes) ---\n{text}"


@registry.register
def execute_terminal_command(command: str) -> str:
    """Execute a shell command on the local machine and return its output.

    Runs with a wall-clock timeout and a deny-list for destructive commands.
    Captured stdout/stderr are truncated to a safe length.

    Args:
        command: The exact command line to execute in the system shell.
    """
    for pattern in _DANGEROUS_PATTERNS:
        if pattern.search(command):
            return (
                "Error: command blocked by safety policy "
                f"(matched dangerous pattern). Command was: {command!r}"
            )

    for pattern in _SERVER_PATTERNS:
        if pattern.search(command):
            return (
                "Error: this looks like a long-running server/process, which would "
                f"block this tool (it has a {config.TERMINAL_TIMEOUT:.0f}s limit) and "
                "then be killed. Use launch_program instead — it starts servers "
                "detached and returns immediately, so the agent never blocks.\n"
                f"Suggested: launch_program(command={command!r}). "
                f"(Matched server pattern in: {command!r}.)"
            )

    # Launch in its OWN process group/session so that on timeout we can kill the
    # WHOLE tree. subprocess.run(timeout=) only kills the direct child; a grandchild
    # (e.g. a PowerShell command that spawns wmic) keeps the stdout pipe open and
    # communicate() blocks FOREVER past the timeout — the "agent hung on step N,
    # GPU idle, model unloaded" failure. Raw bytes (no text=True): we decode safely.
    win = sys.platform == "win32"
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if win else 0
    try:
        proc = subprocess.Popen(  # noqa: S602 - intentional shell command
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=creationflags,
            start_new_session=not win,
        )
    except OSError as exc:
        return f"Error: could not run command {command!r}: {type(exc).__name__}: {exc}"

    try:
        raw_out, raw_err = proc.communicate(timeout=config.TERMINAL_TIMEOUT)
        returncode = proc.returncode
    except subprocess.TimeoutExpired:
        _kill_process_tree(proc)
        try:
            raw_out, raw_err = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            raw_out, raw_err = b"", b""
        return (
            f"Error: command timed out after {config.TERMINAL_TIMEOUT}s and was "
            f"killed along with its child processes: {command!r}. For anything "
            "long-running use launch_program instead."
        )

    stdout = _decode(raw_out).strip()
    stderr = _decode(raw_err).strip()

    parts = [f"exit_code: {returncode}"]
    if stdout:
        parts.append(f"stdout:\n{stdout}")
    if stderr:
        parts.append(f"stderr:\n{stderr}")
    output = "\n".join(parts)

    limit = config.TERMINAL_MAX_OUTPUT_CHARS
    if len(output) > limit:
        output = output[:limit] + f"\n... [truncated, {len(output) - limit} more chars]"
    return output


@registry.register
def launch_program(command: str, working_dir: str = "") -> str:
    """Start a long-running program or server in its OWN visible window.

    Use this (NOT execute_terminal_command) for anything that keeps running and
    does not return on its own — servers, .bat launchers, GUIs, etc. The process
    is started detached in a new console window and this call returns IMMEDIATELY
    without waiting, so it never freezes the agent.

    Examples: starting ComfyUI, a dev server, or any START_*.bat launcher.

    Args:
        command: The command/program/path to launch (e.g. a path to a .bat file).
        working_dir: Optional folder to start the program in. Defaults to the
            program's own directory if a file path is given, else the cwd.
    """
    for pattern in _DANGEROUS_PATTERNS:
        if pattern.search(command):
            return (
                "Error: command blocked by safety policy "
                f"(matched dangerous pattern). Command was: {command!r}"
            )

    # Resolve a sensible working directory: explicit > the file's own folder.
    cwd: str | None = None
    if working_dir.strip():
        wd = Path(working_dir).expanduser()
        if not wd.is_dir():
            return f"Error: working_dir is not a directory: {wd}"
        cwd = str(wd)
    else:
        candidate = Path(command.strip().strip('"')).expanduser()
        if candidate.exists() and candidate.parent.is_dir():
            cwd = str(candidate.parent)

    # On Windows, CREATE_NEW_CONSOLE opens a real, visible terminal window the
    # user can watch, and the child keeps running after this call returns.
    creationflags = 0
    if sys.platform == "win32":
        creationflags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)

    try:
        proc = subprocess.Popen(  # noqa: S602 - intentional detached launch
            command,
            shell=True,
            cwd=cwd,
            creationflags=creationflags,
            close_fds=True,
        )
    except OSError as exc:
        return f"Error: could not launch {command!r}: {type(exc).__name__}: {exc}"

    where = f" in {cwd}" if cwd else ""
    return (
        f"Launched in a new window{where} (pid={proc.pid}): {command!r}. "
        "It is running independently; this did not wait for it to finish."
    )


@registry.register
def write_file(filepath: str, content: str) -> str:
    """Write text content to a file (UTF-8), creating parent dirs as needed.

    Overwrites the file if it already exists.

    Args:
        filepath: Destination path for the file (absolute or relative to cwd).
        content: The text to write into the file.
    """
    path = Path(filepath).expanduser()
    if path.exists() and path.is_dir():
        return f"Error: path is a directory, not a file: {path}"

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return f"Wrote {len(content)} characters to {path}"


@registry.register
def list_directory(dir_path: str) -> str:
    """List the immediate contents of a directory as structured JSON.

    Cleaner and safer for the model to read than parsing raw `dir`/`ls` output.

    Args:
        dir_path: Path to the directory to list (absolute or relative to cwd).
    """
    path = Path(dir_path).expanduser()
    if not path.exists():
        return f"Error: directory not found: {path}"
    if not path.is_dir():
        return f"Error: not a directory: {path}"

    entries: list[dict[str, object]] = []
    for child in sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
        try:
            is_dir = child.is_dir()
            entries.append(
                {
                    "name": child.name,
                    "type": "directory" if is_dir else "file",
                    "size_bytes": None if is_dir else child.stat().st_size,
                }
            )
        except OSError:
            # Skip entries we can't stat (permissions, broken links, ...).
            continue

    return json.dumps({"path": str(path), "count": len(entries), "entries": entries})


@registry.register
async def fetch_webpage(url: str) -> str:
    """Fetch a URL via HTTP GET and return its text body (truncated if large).

    Args:
        url: The full URL to request (must start with http:// or https://).
    """
    if not re.match(r"^https?://", url, re.IGNORECASE):
        return "Error: url must start with http:// or https://"

    try:
        async with httpx.AsyncClient(
            timeout=config.FETCH_TIMEOUT, follow_redirects=True
        ) as client:
            resp = await client.get(url, headers={"User-Agent": "ZeroAgent/0.1"})
    except httpx.TimeoutException:
        return f"Error: request to {url} timed out after {config.FETCH_TIMEOUT}s"
    except httpx.HTTPError as exc:
        return f"Error fetching {url}: {type(exc).__name__}: {exc}"

    raw = resp.text
    content_type = resp.headers.get("content-type", "?")

    # For HTML pages, extract just the readable article text (drops nav, ads,
    # scripts) so the model sees content, not markup. Falls back to the raw body
    # if extraction isn't possible (non-HTML, extractor missing, or no match).
    body = raw
    clean = False
    if trafilatura is not None and "html" in content_type.lower():
        extracted = await asyncio.to_thread(
            trafilatura.extract, raw, include_comments=False, include_tables=True
        )
        if extracted and extracted.strip():
            body = extracted.strip()
            clean = True

    header = (
        f"GET {url} -> {resp.status_code} ({content_type})"
        f"{' [clean text]' if clean else ''}\n"
    )
    limit = config.FETCH_MAX_BYTES
    if len(body) > limit:
        body = body[:limit] + f"\n... [truncated, {len(body) - limit} more chars]"
    return header + body


@registry.register
def search_web(query: str, max_results: int = 5) -> str:
    """Search the web via DuckDuckGo and return ranked results.

    Use this to find current information, documentation, or facts the model does
    not already know. Returns a readable list of Title / URL / Snippet entries.

    Args:
        query: The search query.
        max_results: How many results to return (1-10).
    """
    max_results = max(1, min(max_results, 10))
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
    except Exception as exc:  # noqa: BLE001 - network/library errors -> the model
        return f"Error searching the web for {query!r}: {type(exc).__name__}: {exc}"

    if not results:
        return f"No web results found for {query!r}."

    lines = [f"Top {len(results)} web results for {query!r}:"]
    for i, item in enumerate(results, start=1):
        title = item.get("title", "(no title)")
        href = item.get("href", "")
        snippet = item.get("body", "").strip()
        lines.append(f"\n{i}. {title}\n   URL: {href}\n   {snippet}")
    return "\n".join(lines)


@registry.register
async def get_stock_price(symbol: str) -> str:
    """Get the LIVE price of a stock/ETF by its ticker symbol.

    Use this for any "what is the price of X" question instead of guessing or
    reading a number from a web-search snippet (which is often stale). Returns
    the real-time market price from Yahoo Finance. Never invent a price; if this
    returns an error, say the price could not be retrieved.

    Args:
        symbol: The ticker symbol, e.g. "NVDA", "AAPL", "MSFT".
    """
    sym = symbol.strip().upper()
    if not re.match(r"^[A-Z0-9.\-^=]{1,12}$", sym):
        return f"Error: '{symbol}' is not a valid ticker symbol."

    headers = {"User-Agent": "Mozilla/5.0"}
    hosts = ("query1.finance.yahoo.com", "query2.finance.yahoo.com")
    last_err = ""
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
        for host in hosts:
            url = f"https://{host}/v8/finance/chart/{sym}?interval=1d&range=1d"
            try:
                resp = await client.get(url, headers=headers)
            except httpx.HTTPError as exc:
                last_err = f"{type(exc).__name__}: {exc}"
                continue
            if resp.status_code != 200:
                last_err = f"HTTP {resp.status_code}"
                continue

            data = resp.json()
            result = (data.get("chart", {}).get("result") or [None])[0]
            if not result:
                err = data.get("chart", {}).get("error")
                return f"Error: no data for ticker {sym!r}. {err or 'Symbol may not exist.'}"

            meta = result.get("meta", {})
            price = meta.get("regularMarketPrice")
            if price is None:
                last_err = "no price field in response"
                continue

            currency = meta.get("currency", "")
            prev = meta.get("chartPreviousClose") or meta.get("previousClose")
            exch = meta.get("exchangeName", "")
            parts = [f"{sym}: {price} {currency}".rstrip()]
            if prev:
                change = price - prev
                pct = (change / prev * 100) if prev else 0
                sign = "+" if change >= 0 else ""
                parts.append(f"change {sign}{change:.2f} ({sign}{pct:.2f}%) vs prev close {prev}")
            if exch:
                parts.append(f"exchange {exch}")
            parts.append("source: Yahoo Finance (live)")
            return " | ".join(parts)

    return f"Error: could not retrieve a live price for {sym!r}. {last_err}".strip()


@registry.register
async def read_pdf(filepath: str, max_chars: int = 20000, pages: str = "") -> str:
    """Extract text from a local PDF file.

    Args:
        filepath: Absolute path to the .pdf file.
        max_chars: Cap on returned characters (protects the context window).
        pages: Optional page range like "1-5" or "3"; empty = all pages.
    """
    if pypdf is None:
        return "Error: pypdf is not installed. Run: pip install pypdf"
    path = Path(filepath.strip())
    if not path.exists():
        return f"Error: PDF not found: {path}"

    def _extract() -> str:
        reader = pypdf.PdfReader(str(path))
        n = len(reader.pages)
        lo, hi = 1, n
        if pages.strip():
            rng = pages.strip()
            if "-" in rng:
                a, _, b = rng.partition("-")
                lo, hi = int(a or 1), int(b or n)
            else:
                lo = hi = int(rng)
        lo, hi = max(1, lo), min(n, hi)
        chunks = []
        for i in range(lo - 1, hi):
            try:
                chunks.append(reader.pages[i].extract_text() or "")
            except Exception:  # noqa: BLE001 - skip a bad page, keep the rest
                continue
        text = "\n\n".join(chunks).strip()
        header = f"PDF: {path.name} ({n} pages, showing {lo}-{hi})\n\n"
        body = text[:max_chars]
        if len(text) > max_chars:
            body += f"\n\n… [truncated at {max_chars} chars of {len(text)}]"
        return header + (body or "(no extractable text — may be a scanned image PDF)")

    try:
        return await asyncio.to_thread(_extract)
    except Exception as exc:  # noqa: BLE001
        return f"Error reading PDF: {type(exc).__name__}: {exc}"
