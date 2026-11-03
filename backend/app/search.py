"""Web research pipeline: SearXNG -> fetch -> extract -> chunk -> rank -> context.

Search-engine snippets are too short to ground an answer, so the top pages are
fetched and their main text extracted. The chunks are then ranked against the
queries so that only the most relevant passages consume the (scarce) context
window. Ranking uses a local embedding model and falls back to lexical overlap
if that model is not installed.

Semantic similarity measures relevance, not reliability: an SEO article titled
"Latest version of X" matches the question better than the project's own
release page. Two signals counter that:

- A small authority prior in the ranking, for primary sources.
- Publication dates in each passage header, so the model can resolve conflicts
  between sources in favor of the most recent one.
"""

import asyncio
import logging
import math
import re
from collections import Counter
from urllib.parse import urlparse

import httpx
import trafilatura

from .config import settings
from .ollama_client import ollama

log = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) private-llm-chat/1.0"
_WORD = re.compile(r"\w{3,}", re.UNICODE)
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")
# Second-level labels of two-part public suffixes (com.br, co.uk, ...).
_SLD = {"com", "co", "org", "net", "gov", "edu", "ac", "or", "ne"}


# --- Source metadata -------------------------------------------------------------


def host_of(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def site_name(host: str) -> str:
    """Registrable label of a host: docs.ollama.com -> ollama, foo.com.br -> foo."""
    labels = host.split(".")
    if len(labels) >= 3 and labels[-2] in _SLD and len(labels[-1]) == 2:
        return labels[-3]
    return labels[-2] if len(labels) >= 2 else host


def is_primary(url: str, queries: list[str]) -> bool:
    """Heuristic for "primary source".

    True for code/package/paper hosts in settings.search_primary_domains, and for
    sites whose registrable name equals a query term (ollama.com for "ollama ...").
    The second rule targets a project's own site; it requires an exact label match,
    so look-alikes such as ollama-guide.net do not qualify.
    """
    host = host_of(url)
    if not host:
        return False
    for domain in settings.search_primary_domains:
        if host == domain or host.endswith("." + domain):
            return True
    name = site_name(host)
    terms = {w.lower() for q in queries for w in _WORD.findall(q)}
    return len(name) >= 4 and name in terms


def normalize_date(value) -> str | None:
    if not value:
        return None
    match = _DATE.match(str(value))
    return match.group(0) if match else None


# --- Search and fetch --------------------------------------------------------------


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
                {
                    "url": item["url"],
                    "title": item.get("title", ""),
                    "snippet": item.get("content", ""),
                    "date": normalize_date(item.get("publishedDate")),
                }
            )
        if len(out) >= limit:
            break
    return out


def _extract(html: str) -> tuple[str | None, str | None]:
    doc = trafilatura.bare_extraction(
        html, with_metadata=True, include_comments=False, include_tables=True
    )
    if doc is None:
        return None, None
    return doc.text, normalize_date(doc.date)


async def fetch_page(client: httpx.AsyncClient, url: str) -> tuple[str | None, str | None]:
    """Download a page (size-capped). Return (main text, publication date)."""
    try:
        async with client.stream("GET", url, follow_redirects=True) as r:
            if r.status_code != 200:
                return None, None
            ctype = r.headers.get("content-type", "")
            if "html" not in ctype and "text/plain" not in ctype:
                return None, None
            buf = bytearray()
            async for chunk in r.aiter_bytes():
                buf.extend(chunk)
                if len(buf) > settings.fetch_max_bytes:
                    break
            encoding = r.encoding or "utf-8"
        html = buf.decode(encoding, errors="replace")
        if "text/plain" in ctype:
            return html, None
        # trafilatura is CPU-bound; keep it off the event loop.
        return await asyncio.to_thread(_extract, html)
    except Exception as exc:  # network errors, decoding errors, extractor errors
        log.info("fetch failed for %s: %s", url, exc)
        return None, None


# --- Chunking and ranking ------------------------------------------------------------


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
    """Rank chunks by max similarity to any query, times an authority prior.

    The prior is multiplicative, (1 + boost) for primary sources, so it only
    reorders passages of comparable relevance. An irrelevant passage from a
    primary site still ranks low.

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
    boost = 1.0 + settings.search_authority_boost
    scores = [s * boost if c["primary"] else s for s, c in zip(scores, chunks)]
    order = sorted(range(len(chunks)), key=lambda i: scores[i], reverse=True)
    best = scores[order[0]]
    cutoff = best * settings.search_min_relative_score if best > 0 else 0.0
    return [chunks[i] for i in order if scores[i] >= cutoff], method


# --- Pipeline ------------------------------------------------------------------------


def _header(n: int, c: dict) -> str:
    parts = [f"[{n}] {c['title'] or c['host']}", f"site: {c['host']}"]
    parts.append(f"published: {c['date']}" if c["date"] else "published: unknown")
    if c["primary"]:
        parts.append("primary source")
    return " | ".join(parts) + f"\nURL: {c['url']}"


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

    for h in hits:
        h["host"] = host_of(h["url"])
        h["primary"] = is_primary(h["url"], queries)

    # Fetch the top hits, plus up to two primary sources further down the list.
    # Engines often rank SEO pages above a project's own release page.
    to_fetch = hits[: settings.search_fetch_pages]
    extra = [h for h in hits[settings.search_fetch_pages:] if h["primary"]][:2]
    to_fetch += extra
    async with httpx.AsyncClient(
        timeout=settings.fetch_timeout_s, headers={"User-Agent": USER_AGENT}
    ) as client:
        pages = await asyncio.gather(*(fetch_page(client, h["url"]) for h in to_fetch))

    def meta(h: dict, date: str | None = None) -> dict:
        return {
            "url": h["url"],
            "title": h["title"],
            "host": h["host"],
            "primary": h["primary"],
            "date": date or h["date"],
        }

    chunks: list[dict] = []
    fetched = set()
    for hit, (text, date) in zip(to_fetch, pages):
        fetched.add(hit["url"])
        if text:
            for c in chunk_text(text)[:12]:
                chunks.append({**meta(hit, date), "text": c})
        elif hit["snippet"]:
            chunks.append({**meta(hit), "text": hit["snippet"]})
    for hit in hits:
        if hit["url"] not in fetched and hit["snippet"]:
            chunks.append({**meta(hit), "text": hit["snippet"]})
    chunks = chunks[:90]

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
            sources.append(
                {
                    "n": index[c["url"]],
                    "url": c["url"],
                    "title": c["title"] or c["url"],
                    "date": c["date"],
                    "primary": c["primary"],
                }
            )
        blocks.append(_header(index[c["url"]], c) + "\n" + c["text"])
    return "\n\n---\n\n".join(blocks), sources, method
