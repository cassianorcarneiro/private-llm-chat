"""Web research pipeline: SearXNG -> fetch -> extract -> chunk -> rank -> context.

Search-engine snippets are too short to ground an answer, so the top pages are
fetched and their main text extracted. The chunks are then ranked against the
queries so that only the most relevant passages consume the (scarce) context
window. Ranking uses a local embedding model and falls back to lexical overlap
if that model is not installed.
"""

import asyncio
import logging
import math
import re
from collections import Counter

import httpx
import trafilatura

from .config import settings
from .ollama_client import ollama

log = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) private-llm-chat/1.0"
_WORD = re.compile(r"\w{3,}", re.UNICODE)


async def searxng_search(query: str, limit: int) -> list[dict]:
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(
            f"{settings.searxng_url.rstrip('/')}/search",
            params={"q": query, "format": "json", "safesearch": 0},
        )
        r.raise_for_status()
        results = r.json().get("results", [])
    out = []
    for item in results:
        if item.get("url"):
            out.append(
                {"url": item["url"], "title": item.get("title", ""), "snippet": item.get("content", "")}
            )
        if len(out) >= limit:
            break
    return out


async def fetch_text(client: httpx.AsyncClient, url: str) -> str | None:
    """Download a page (size-capped) and extract its main text."""
    try:
        async with client.stream("GET", url, follow_redirects=True) as r:
            if r.status_code != 200:
                return None
            ctype = r.headers.get("content-type", "")
            if "html" not in ctype and "text/plain" not in ctype:
                return None
            buf = bytearray()
            async for chunk in r.aiter_bytes():
                buf.extend(chunk)
                if len(buf) > settings.fetch_max_bytes:
                    break
            encoding = r.encoding or "utf-8"
        html = buf.decode(encoding, errors="replace")
        if "text/plain" in ctype:
            return html
        # trafilatura is CPU-bound; keep it off the event loop.
        return await asyncio.to_thread(
            trafilatura.extract, html, include_comments=False, include_tables=True
        )
    except Exception as exc:  # network errors, decoding errors, extractor errors
        log.info("fetch failed for %s: %s", url, exc)
        return None


def chunk_text(text: str, size: int = 900, overlap: int = 150) -> list[str]:
    text = re.sub(r"[ \t]+", " ", text).strip()
    chunks, start = [], 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            cut = text.rfind(" ", start + size // 2, end)
            if cut > start:
                end = cut
        chunks.append(text[start:end].strip())
        if end >= len(text):
            break
        nxt = max(end - overlap, start + 1)
        # Start the next chunk on a word boundary.
        space = text.find(" ", nxt, end)
        start = space + 1 if space != -1 else nxt
    return [c for c in chunks if len(c) > 80]


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _lexical(query: str, text: str) -> float:
    q = set(w.lower() for w in _WORD.findall(query))
    if not q:
        return 0.0
    t = Counter(w.lower() for w in _WORD.findall(text))
    return sum(1 for w in q if t[w]) / len(q)


async def rank_chunks(queries: list[str], chunks: list[dict]) -> tuple[list[dict], str]:
    """Rank chunks by max similarity to any query.

    Chunks scoring below a fraction of the best score are dropped. A relative
    cut is used because absolute cosine ranges differ between embedding models.
    """
    if not chunks:
        return [], "none"
    try:
        inputs = [settings.embed_query_prefix + q for q in queries] + [
            settings.embed_doc_prefix + c["text"] for c in chunks
        ]
        vectors = await ollama.embed(settings.embed_model, inputs)
        qv, cv = vectors[: len(queries)], vectors[len(queries):]
        scores = [max(_cosine(q, c) for q in qv) for c in cv]
        method = "embedding"
    except Exception as exc:
        log.warning("embedding rank failed (%s); using lexical ranking", exc)
        scores = [max(_lexical(q, c["text"]) for q in queries) for c in chunks]
        method = "lexical"
    order = sorted(range(len(chunks)), key=lambda i: scores[i], reverse=True)
    best = scores[order[0]]
    cutoff = best * settings.search_min_relative_score if best > 0 else 0.0
    return [chunks[i] for i in order if scores[i] >= cutoff], method


async def web_research(queries: list[str], max_chars: int) -> tuple[str, list[dict], str]:
    """Return (context block, numbered sources, ranking method)."""
    raw = await asyncio.gather(
        *(searxng_search(q, settings.search_max_results) for q in queries), return_exceptions=True
    )
    lists = [r for r in raw if isinstance(r, list)]
    if not lists:
        raise RuntimeError(f"SearXNG unavailable: {raw[0]}")

    # Round-robin merge so every query contributes its best hits.
    hits, seen = [], set()
    for i in range(max(len(lst) for lst in lists)):
        for lst in lists:
            if i < len(lst) and lst[i]["url"] not in seen:
                seen.add(lst[i]["url"])
                hits.append(lst[i])
    if not hits:
        return "", [], "none"

    to_fetch = hits[: settings.search_fetch_pages]
    async with httpx.AsyncClient(
        timeout=settings.fetch_timeout_s, headers={"User-Agent": USER_AGENT}
    ) as client:
        texts = await asyncio.gather(*(fetch_text(client, h["url"]) for h in to_fetch))

    chunks: list[dict] = []
    for hit, text in zip(to_fetch, texts):
        if text:
            for c in chunk_text(text)[:12]:
                chunks.append({"url": hit["url"], "title": hit["title"], "text": c})
        elif hit["snippet"]:
            chunks.append({"url": hit["url"], "title": hit["title"], "text": hit["snippet"]})
    for hit in hits[settings.search_fetch_pages:]:
        if hit["snippet"]:
            chunks.append({"url": hit["url"], "title": hit["title"], "text": hit["snippet"]})
    chunks = chunks[:80]

    ranked, method = await rank_chunks(queries, chunks)

    # Select top chunks under a character budget, at most 2 per page for diversity.
    selected, per_url, total = [], Counter(), 0
    for c in ranked:
        if per_url[c["url"]] >= 2 or total + len(c["text"]) > max_chars:
            continue
        selected.append(c)
        per_url[c["url"]] += 1
        total += len(c["text"])
        if len(selected) >= settings.search_top_chunks:
            break

    index: dict[str, int] = {}
    sources: list[dict] = []
    blocks: list[str] = []
    for c in selected:
        if c["url"] not in index:
            index[c["url"]] = len(sources) + 1
            sources.append({"n": index[c["url"]], "url": c["url"], "title": c["title"] or c["url"]})
        n = index[c["url"]]
        blocks.append(f"[{n}] {c['title']}\nURL: {c['url']}\n{c['text']}")
    return "\n\n---\n\n".join(blocks), sources, method
