"""Async wrapper around the local Ollama HTTP API (/api/chat).

Talks to Ollama's native chat endpoint, which is OpenAI-compatible for tool
calling: pass a list of tool schemas and the assistant message comes back with
a populated ``tool_calls`` array when the model decides to act.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator

import httpx
from pydantic import BaseModel, ConfigDict, Field

from orchestrator import config

logger = logging.getLogger("zero_agent.ollama")


class OllamaError(RuntimeError):
    """Raised when the Ollama server returns an error response."""


class OllamaUnavailable(OllamaError):
    """Raised when the Ollama server cannot be reached after all retries."""


class OllamaToolCallError(OllamaError):
    """Raised when the model emitted a malformed native tool call.

    This is a DETERMINISTIC failure (the model produced tool-call JSON the server
    couldn't parse, e.g. qwen3-coder's truncated arguments), not a transient
    server hiccup — so retrying the identical request is pointless. It's a
    distinct type so the agent can react by retrying the turn WITHOUT tools
    (text mode), where the model's tool call can be parsed from its text instead.
    """


def _is_tool_call_error(body: str) -> bool:
    """True if a 5xx body indicates a malformed tool call (not a real server fault)."""
    low = body.lower()
    return (
        "tool call" in low
        or "tool_call" in low
        or "end of json input" in low
        or ("invalid" in low and "json" in low)
    )


class ToolCallFunction(BaseModel):
    """The ``function`` payload inside a single tool call."""

    model_config = ConfigDict(extra="allow")

    name: str
    # Ollama returns arguments as an object; some models emit a JSON string.
    arguments: dict[str, Any] | str = Field(default_factory=dict)


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="allow")

    function: ToolCallFunction


class ChatMessage(BaseModel):
    """A single assistant/user/tool/system message."""

    model_config = ConfigDict(extra="allow")

    role: str
    content: str = ""
    tool_calls: list[ToolCall] | None = None


class ChatResponse(BaseModel):
    """Parsed response from POST /api/chat (stream=False)."""

    model_config = ConfigDict(extra="allow")

    model: str
    message: ChatMessage
    done: bool = True


class OllamaClient:
    """Thin async client for a local Ollama server.

    Usage::

        async with OllamaClient(model="qwen3:32b") as client:
            resp = await client.chat(messages, tools=tool_schemas)
    """

    # Shared model -> capability set, fetched once from /api/show. Lets us detect
    # tools/thinking/vision from GROUND TRUTH instead of fragile name substrings
    # (the source of the gemma-vs-supergemma tools bug and the qwen3-coder thinking
    # 400). Name heuristics remain only as a fallback when /api/show is unavailable.
    _CAPS_CACHE: "dict[str, set[str]]" = {}

    def __init__(
        self,
        *,
        host: str = config.OLLAMA_HOST,
        model: str = config.DEFAULT_MODEL,
        timeout: float = config.REQUEST_TIMEOUT,
        max_retries: int = config.OLLAMA_MAX_RETRIES,
        retry_backoff: float = config.OLLAMA_RETRY_BACKOFF,
        num_ctx: int = config.OLLAMA_NUM_CTX,
        num_predict: int = config.OLLAMA_NUM_PREDICT,
        think: bool = config.ENABLE_THINKING,
    ) -> None:
        self.base_url = host.rstrip("/")
        self.model = model
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff
        self.num_ctx = num_ctx
        self.num_predict = num_predict
        self.think = think
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(timeout, read=400.0))

    async def __aenter__(self) -> "OllamaClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def health_check(self) -> bool:
        """Return True if the Ollama server answers, False otherwise.

        Used at startup so the UI can show a clear "Ollama is down" message
        instead of failing on the first user message.
        """
        try:
            resp = await self._client.get(f"{self.base_url}/api/tags", timeout=5.0)
            return resp.status_code < 400
        except httpx.HTTPError:
            return False

    async def ensure_capabilities(self) -> None:
        """Fetch the active model's capabilities from /api/show, once, into the
        shared cache. Failures are swallowed — callers fall back to the name
        heuristic. Safe to call on every request (cheap cache hit after the first).
        """
        name = self.model or ""
        if not name or name in self._CAPS_CACHE:
            return
        try:
            resp = await self._client.post(
                f"{self.base_url}/api/show", json={"model": name}, timeout=15.0
            )
            if resp.status_code < 400:
                caps = resp.json().get("capabilities") or []
                self._CAPS_CACHE[name] = {str(c).lower() for c in caps}
        except (httpx.HTTPError, ValueError, KeyError):
            pass  # leave uncached → name-heuristic fallback in the readers

    def cached_capabilities(self) -> "set[str] | None":
        """The active model's capability set if known (else None → use heuristic)."""
        return self._CAPS_CACHE.get(self.model or "")

    def _with_num_ctx(self, options: "dict[str, Any] | None") -> dict[str, Any]:
        """Return options with ``num_ctx`` and ``num_predict`` filled in.

        Ollama defaults many models to an 8192-token window, which silently
        truncates long chats and drops the system prompt. We always request our
        configured window unless the caller explicitly overrides it. We also cap
        the generated length (``num_predict``) so a reasoning model can't run
        away with a 50k-token reply. Caller-supplied values always win.
        """
        merged = dict(options or {})
        merged.setdefault("num_ctx", self.num_ctx)
        if self.num_predict and self.num_predict > 0:
            merged.setdefault("num_predict", self.num_predict)
        return merged

    # Model families that support a separate "thinking" channel. Sending the
    # ``think`` flag to other models makes Ollama 400 ("does not support
    # thinking"), so we only include it for these.
    _THINKING_MODELS = ("qwen3", "qwen2.5", "deepseek-r1")

    # Substrings that OVERRIDE the above to non-thinking, even though they match a
    # thinking family. ``qwen3-coder`` matches "qwen3" but the coder variants do
    # NOT expose a thinking channel — sending ``think`` (True OR False) 400s with
    # "does not support thinking". Verified via `ollama show` (2026-06-19).
    _NO_THINK_OVERRIDE = ("coder",)

    # Models where we FORCE thinking off even though they support it. An
    # abliterated/uncensored model's base reasoning often re-asserts caution and
    # talks itself into refusing/softening, so non-thinking mode keeps it direct.
    _FORCE_NO_THINK = ("erotic", "abliterated")

    def _supports_thinking(self) -> bool:
        # Ground truth from /api/show when available; else the name heuristic.
        caps = self.cached_capabilities()
        if caps is not None:
            return "thinking" in caps
        name = (self.model or "").lower()
        if any(marker in name for marker in self._NO_THINK_OVERRIDE):
            return False
        return any(marker in name for marker in self._THINKING_MODELS)

    def _maybe_think(self, payload: dict[str, Any]) -> None:
        """Set the ``think`` flag for models that support it.

        For ``_FORCE_NO_THINK`` models we send ``think=False`` explicitly (rather
        than omitting it) so Ollama runs them in direct, non-reasoning mode.
        """
        if not self._supports_thinking():
            return
        name = (self.model or "").lower()
        if any(marker in name for marker in self._FORCE_NO_THINK):
            payload["think"] = False
        else:
            payload["think"] = self.think

    async def _backoff(self, attempt: int, exc: Exception) -> None:
        """Sleep with exponential backoff before the next retry, and log it."""
        delay = self.retry_backoff * (2 ** (attempt - 1))
        logger.warning(
            "ollama call failed (attempt %d/%d): %s — retrying in %.1fs",
            attempt,
            self.max_retries,
            exc,
            delay,
        )
        await asyncio.sleep(delay)

    async def list_models(self) -> list[str]:
        """Return the tags of all locally available models."""
        resp = await self._client.get(f"{self.base_url}/api/tags")
        resp.raise_for_status()
        data = resp.json()
        return [m["name"] for m in data.get("models", [])]

    async def resolve_model(self, preferred: str | None = None) -> str | None:
        """Return ``preferred`` if installed, else the first available model.

        Sets ``self.model`` to the resolved value. Returns ``None`` if Ollama is
        unreachable or no models are installed.
        """
        preferred = preferred or self.model
        try:
            installed = await self.list_models()
        except Exception:  # noqa: BLE001 - caller decides how to report
            return None
        if not installed:
            return None
        self.model = preferred if preferred in installed else installed[0]
        await self.ensure_capabilities()  # pre-warm caps for the resolved model
        return self.model

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        options: dict[str, Any] | None = None,
    ) -> ChatResponse:
        """Send a (non-streaming) chat completion request.

        Args:
            messages: OpenAI-style message dicts.
            tools: Ollama/OpenAI tool schemas (see ToolRegistry.schemas()).
            options: Model runtime options (temperature, num_ctx, ...).

        Returns:
            A validated :class:`ChatResponse`.
        """
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
        }
        await self.ensure_capabilities()
        self._maybe_think(payload)
        if tools:
            payload["tools"] = tools
        payload["options"] = self._with_num_ctx(options)

        last_exc: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = await self._client.post(
                    f"{self.base_url}/api/chat", json=payload
                )
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt < self.max_retries:
                    await self._backoff(attempt, exc)
                    continue
                raise OllamaUnavailable(
                    f"Ollama unreachable at {self.base_url} after "
                    f"{self.max_retries} attempts: {type(exc).__name__}"
                ) from exc

            if resp.status_code >= 500 and attempt < self.max_retries:
                # Transient server error (e.g. model still loading) — retry.
                await self._backoff(attempt, RuntimeError(f"HTTP {resp.status_code}"))
                continue
            if resp.status_code >= 400:
                # 4xx is a real client error (e.g. "model does not support
                # tools"); surface it instead of retrying.
                detail = resp.text
                try:
                    detail = resp.json().get("error", detail)
                except Exception:  # noqa: BLE001
                    pass
                raise OllamaError(f"Ollama /api/chat {resp.status_code}: {detail}")
            return ChatResponse.model_validate(resp.json())

        # Loop exhausted on 5xx without returning/raising above.
        raise OllamaUnavailable(
            f"Ollama returned repeated server errors at {self.base_url}: {last_exc}"
        )

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        options: dict[str, Any] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream a chat completion, yielding raw NDJSON chunks from Ollama.

        Each yielded chunk is a dict like
        ``{"message": {"content": "...delta...", "tool_calls": [...]}, "done": ...}``
        where ``content`` is the *incremental* token(s) for that chunk. Tool
        calls (when the model decides to act) arrive in one of the chunks.
        """
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": True,
        }
        await self.ensure_capabilities()
        self._maybe_think(payload)
        if tools:
            payload["tools"] = tools
        payload["options"] = self._with_num_ctx(options)

        for attempt in range(1, self.max_retries + 1):
            started = False
            try:
                async with self._client.stream(
                    "POST", f"{self.base_url}/api/chat", json=payload
                ) as resp:
                    if resp.status_code >= 400:
                        body = (await resp.aread()).decode("utf-8", errors="replace")
                        try:
                            body = json.loads(body).get("error", body)
                        except Exception:  # noqa: BLE001
                            pass
                        # Malformed tool call = deterministic; don't burn retries,
                        # raise a distinct error so the agent can retry text-mode.
                        if resp.status_code >= 500 and _is_tool_call_error(body):
                            raise OllamaToolCallError(
                                f"Ollama /api/chat {resp.status_code} "
                                f"(malformed tool call): {body[:300]}"
                            )
                        if resp.status_code >= 500 and attempt < self.max_retries:
                            await self._backoff(
                                attempt, RuntimeError(f"HTTP {resp.status_code}")
                            )
                            continue
                        raise OllamaError(
                            f"Ollama /api/chat {resp.status_code}: {body}"
                        )

                    async for line in resp.aiter_lines():
                        if line.strip():
                            started = True
                            yield json.loads(line)
                return
            except httpx.HTTPError as exc:
                if started:
                    # Connection dropped mid-stream: we've already emitted
                    # partial output, so retrying would duplicate it.
                    raise OllamaUnavailable(
                        f"Ollama stream interrupted: {type(exc).__name__}"
                    ) from exc
                if attempt < self.max_retries:
                    await self._backoff(attempt, exc)
                    continue
                raise OllamaUnavailable(
                    f"Ollama unreachable at {self.base_url} after "
                    f"{self.max_retries} attempts: {type(exc).__name__}"
                ) from exc

    async def embed(
        self, texts: list[str], *, model: str | None = None
    ) -> list[list[float]]:
        """Return embedding vectors for ``texts`` via POST /api/embed.

        Used by the vector-memory subsystem to turn notes and queries into
        vectors. Retries transient failures like :meth:`chat`.

        Args:
            texts: One or more strings to embed.
            model: Embedding model tag; defaults to config.EMBED_MODEL.

        Returns:
            A list of float vectors, one per input string.
        """
        payload = {"model": model or config.EMBED_MODEL, "input": texts}

        for attempt in range(1, self.max_retries + 1):
            try:
                resp = await self._client.post(
                    f"{self.base_url}/api/embed", json=payload
                )
            except httpx.HTTPError as exc:
                if attempt < self.max_retries:
                    await self._backoff(attempt, exc)
                    continue
                raise OllamaUnavailable(
                    f"Ollama unreachable at {self.base_url} after "
                    f"{self.max_retries} attempts: {type(exc).__name__}"
                ) from exc

            if resp.status_code >= 500 and attempt < self.max_retries:
                await self._backoff(attempt, RuntimeError(f"HTTP {resp.status_code}"))
                continue
            if resp.status_code >= 400:
                detail = resp.text
                try:
                    detail = resp.json().get("error", detail)
                except Exception:  # noqa: BLE001
                    pass
                raise OllamaError(f"Ollama /api/embed {resp.status_code}: {detail}")

            return resp.json().get("embeddings", [])

        raise OllamaUnavailable(
            f"Ollama returned repeated server errors at {self.base_url}"
        )
