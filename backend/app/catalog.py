"""Model catalog: keeps a description for every model installed in Ollama.

Detection is level-triggered (a reconciliation loop diffs /api/tags against the
catalog) rather than edge-triggered (a hook on our own pull endpoint). Ollama has
no webhooks, and models pulled from the CLI must be described too.

Descriptions are grounded: /api/show supplies objective metadata and the model's
public page supplies the author's claims. A local LLM only condenses them.
Asking an LLM to describe a model from its own weights would hallucinate,
because most models are newer than the describer's training data.
"""

import asyncio
import logging
import re
from contextlib import suppress

import httpx
import trafilatura
from sqlalchemy import select

from . import prompts
from .config import settings
from .db import ModelEntry, SessionLocal, utcnow
from .ollama_client import ollama, think_off

log = logging.getLogger(__name__)

_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def looks_like_embedding(name: str, details: dict) -> bool:
    family = (details.get("family") or "").lower()
    return "embed" in name.lower() or family in {"bert", "nomic-bert", "xlm-roberta"}


def model_card_url(name: str) -> str:
    if name.startswith(("hf.co/", "huggingface.co/")):
        repo = name.split("/", 1)[1].split(":")[0]
        return f"https://huggingface.co/{repo}"
    base = name.split(":")[0]
    return f"https://ollama.com/{base}" if "/" in base else f"https://ollama.com/library/{base}"


async def fetch_model_card(name: str) -> tuple[str, str]:
    url = model_card_url(name)
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            r = await client.get(url, headers={"User-Agent": "private-llm-chat/1.0"})
            r.raise_for_status()
        text = await asyncio.to_thread(trafilatura.extract, r.text, include_tables=True) or ""
        return text[:6000], url
    except Exception as exc:
        log.info("model card fetch failed for %s: %s", name, exc)
        return "", ""


def clean_description(raw: str) -> str:
    text = _THINK.sub("", raw).strip().strip('"').strip()
    text = re.sub(r"\s+", " ", text)
    return text[:400]


class Catalog:
    def __init__(self) -> None:
        # One lock: descriptions run on the GPU and must not overlap.
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if settings.catalog_enabled:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task

    async def _loop(self) -> None:
        while True:
            try:
                await self.reconcile()
            except Exception as exc:
                log.warning("catalog reconcile failed: %s", exc)
            await asyncio.sleep(settings.catalog_interval_s)

    async def reconcile(self) -> None:
        async with self._lock:
            tags = {t["name"]: t for t in await ollama.tags()}
            todo: list[str] = []
            async with SessionLocal() as s:
                entries = {e.name: e for e in (await s.scalars(select(ModelEntry))).all()}
                for name in entries.keys() - tags.keys():
                    await s.delete(entries[name])
                for name, tag in tags.items():
                    entry = entries.get(name)
                    if entry is None:
                        entry = ModelEntry(name=name, status="pending")
                        s.add(entry)
                    entry.size_bytes = int(tag.get("size") or 0)
                    if entry.status == "pending" or entry.digest != tag.get("digest", ""):
                        todo.append(name)
                await s.commit()
            for name in todo:
                await self._describe(name, tags[name].get("digest", ""))

    async def describe(self, name: str) -> None:
        """Force a new description (manual retry from the UI)."""
        async with self._lock:
            tags = {t["name"]: t for t in await ollama.tags()}
            if name not in tags:
                raise KeyError(name)
            await self._describe(name, tags[name].get("digest", ""))

    async def mark_pending(self, name: str) -> None:
        await self._update(name, status="pending", error="")

    async def _update(self, name: str, **fields) -> None:
        async with SessionLocal() as s:
            entry = await s.get(ModelEntry, name)
            if entry is None:
                entry = ModelEntry(name=name)
                s.add(entry)
            for key, value in fields.items():
                setattr(entry, key, value)
            entry.updated_at = utcnow()
            await s.commit()

    async def _caps(self, name: str) -> list[str]:
        async with SessionLocal() as s:
            entry = await s.get(ModelEntry, name)
            return list(entry.capabilities or []) if entry else []

    async def _pick_describer(self, name: str, caps: list[str]) -> tuple[str, list[str]]:
        # Prefer an explicit choice, then a model already in VRAM (no swap),
        # then the model being described itself.
        if settings.describer_model:
            return settings.describer_model, await self._caps(settings.describer_model)
        with suppress(Exception):
            for m in await ollama.ps():
                loaded = m.get("name") or m.get("model")
                loaded_caps = await self._caps(loaded)
                if "completion" in loaded_caps:
                    return loaded, loaded_caps
        return name, caps

    async def _describe(self, name: str, digest: str) -> None:
        await self._update(name, status="describing", error="")
        try:
            info = await ollama.show(name)
            details = info.get("details") or {}
            model_info = info.get("model_info") or {}
            ctx = next(
                (
                    int(v)
                    for k, v in model_info.items()
                    if k.endswith(".context_length") and isinstance(v, (int, float))
                ),
                None,
            )
            caps = info.get("capabilities") or (
                ["embedding"] if looks_like_embedding(name, details) else ["completion"]
            )
            facts = {
                "family": details.get("family", ""),
                "parameter_size": details.get("parameter_size", ""),
                "quantization": details.get("quantization_level", ""),
                "context_length": ctx,
                "capabilities": caps,
            }
            await self._update(name, digest=digest, **facts)

            if "completion" not in caps:
                desc = (
                    f"Embedding model ({facts['parameter_size'] or 'size unknown'}). "
                    "Used for semantic search and reranking, not for chat."
                )
                await self._update(name, description=desc, description_source="metadata", status="ready")
                return

            card, source = ("", "")
            if settings.catalog_fetch_remote:
                card, source = await fetch_model_card(name)

            describer, dcaps = await self._pick_describer(name, caps)
            raw = await ollama.chat(
                describer,
                [
                    {"role": "system", "content": prompts.DESCRIBER_SYSTEM},
                    {
                        "role": "user",
                        "content": prompts.DESCRIBER_USER.format(
                            name=name,
                            family=facts["family"] or "?",
                            params=facts["parameter_size"] or "?",
                            quant=facts["quantization"] or "?",
                            ctx=ctx or "?",
                            caps=", ".join(caps),
                            source=source or "not available",
                            card=card or "(no page text available)",
                            language=settings.description_language,
                        ),
                    },
                ],
                options={"temperature": 0.2, "num_ctx": 8192},
                think=think_off(dcaps),
            )
            await self._update(
                name,
                description=clean_description(raw),
                description_source=source or "metadata",
                status="ready",
            )
        except Exception as exc:
            log.warning("describe failed for %s: %s", name, exc)
            await self._update(name, status="error", error=str(exc)[:500])


catalog = Catalog()
