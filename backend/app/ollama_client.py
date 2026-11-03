"""Thin async client for the Ollama HTTP API (native endpoints, not /v1)."""

import json
from contextlib import aclosing
from typing import Any, AsyncIterator

import httpx

from .config import settings


class OllamaError(RuntimeError):
    pass


class OllamaClient:
    def __init__(self, base_url: str) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            # Long read timeout: a cold model load plus a long answer can take minutes.
            timeout=httpx.Timeout(connect=5.0, read=900.0, write=60.0, pool=10.0),
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _json(self, method: str, path: str, **kw: Any) -> dict:
        r = await self._client.request(method, path, **kw)
        if r.status_code >= 400:
            raise OllamaError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else {}

    async def _stream(self, path: str, payload: dict) -> AsyncIterator[dict]:
        async with self._client.stream("POST", path, json=payload) as r:
            if r.status_code >= 400:
                body = (await r.aread()).decode(errors="replace")
                raise OllamaError(f"POST {path} -> {r.status_code}: {body[:300]}")
            async for line in r.aiter_lines():
                if not line.strip():
                    continue
                data = json.loads(line)
                yield data

    # --- Model management ------------------------------------------------------
    async def tags(self) -> list[dict]:
        return (await self._json("GET", "/api/tags")).get("models", [])

    async def ps(self) -> list[dict]:
        return (await self._json("GET", "/api/ps")).get("models", [])

    async def show(self, name: str) -> dict:
        return await self._json("POST", "/api/show", json={"model": name})

    async def unload(self, name: str) -> None:
        """Evict a model from VRAM now.

        A generate request with no prompt and keep_alive=0 is Ollama's documented
        unload call; `ollama stop` sends the same request. If the model is serving
        another request, Ollama unloads it when that request finishes.
        """
        await self._json("POST", "/api/generate", json={"model": name, "keep_alive": 0})

    async def delete(self, name: str) -> None:
        await self._json("DELETE", "/api/delete", json={"model": name})

    async def pull(self, name: str) -> AsyncIterator[dict]:
        async for ev in self._stream("/api/pull", {"model": name, "stream": True}):
            yield ev

    # --- Inference -------------------------------------------------------------
    @staticmethod
    def _chat_payload(model, messages, options, stream, fmt=None, think=None, keep_alive=None):
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": stream,
            "options": options or {},
            "keep_alive": keep_alive or settings.keep_alive,
        }
        if fmt is not None:
            payload["format"] = fmt
        if think is not None:
            payload["think"] = think
        return payload

    async def chat(self, model, messages, options=None, fmt=None, think=None) -> str:
        data = await self._json(
            "POST", "/api/chat", json=self._chat_payload(model, messages, options, False, fmt, think)
        )
        if "error" in data:
            raise OllamaError(data["error"])
        return (data.get("message") or {}).get("content", "")

    async def chat_stream(self, model, messages, options=None, think=None) -> AsyncIterator[dict]:
        payload = self._chat_payload(model, messages, options, True, None, think)
        # Close the inner stream explicitly when the consumer stops early.
        async with aclosing(self._stream("/api/chat", payload)) as stream:
            async for chunk in stream:
                if "error" in chunk:
                    raise OllamaError(chunk["error"])
                yield chunk

    async def embed(self, model: str, inputs: list[str]) -> list[list[float]]:
        data = await self._json("POST", "/api/embed", json={"model": model, "input": inputs})
        return data["embeddings"]


ollama = OllamaClient(settings.ollama_base_url)


def think_off(capabilities: list[str]) -> bool | None:
    """Disable reasoning traces for auxiliary calls (planning, titles, summaries).

    Only sent to models that declare the capability; other models may reject it.
    """
    return False if "thinking" in (capabilities or []) else None
