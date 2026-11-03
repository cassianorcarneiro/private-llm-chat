"""HTTP API and static frontend."""

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select

from . import prompts
from .catalog import catalog, looks_like_embedding
from .config import settings
from .db import Conversation, Message, ModelEntry, SessionLocal, init_db, new_id, utcnow
from .graph import chat_graph
from .memory import est_tokens, prepare_history
from .ollama_client import ollama, think_off

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per request is noise
log = logging.getLogger("app")

# Strong references to fire-and-forget tasks (asyncio keeps only weak ones).
_background: set[asyncio.Task] = set()


def spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)
    return task


@asynccontextmanager
async def lifespan(_: FastAPI):
    await init_db()
    await catalog.start()
    yield
    await catalog.stop()
    await ollama.close()


app = FastAPI(title="local-ai-chat", lifespan=lifespan)

SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# --- Health ---------------------------------------------------------------------


@app.get("/api/health")
async def health():
    status = {"ollama": False, "searxng": False}
    try:
        await ollama.tags()
        status["ollama"] = True
    except Exception:
        pass
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{settings.searxng_url.rstrip('/')}/healthz")
            status["searxng"] = r.status_code == 200
    except Exception:
        pass
    return status


# --- Conversations ----------------------------------------------------------------


def _conv_dict(c: Conversation) -> dict:
    return {"id": c.id, "title": c.title, "model": c.model, "updated_at": c.updated_at.isoformat()}


def _msg_dict(m: Message) -> dict:
    return {
        "id": m.id,
        "role": m.role,
        "content": m.content,
        "thinking": m.thinking,
        "model": m.model,
        "sources": m.sources,
        "created_at": m.created_at.isoformat(),
    }


@app.get("/api/conversations")
async def list_conversations():
    async with SessionLocal() as s:
        rows = await s.scalars(select(Conversation).order_by(Conversation.updated_at.desc()))
        return [_conv_dict(c) for c in rows.all()]


@app.get("/api/conversations/{conv_id}")
async def get_conversation(conv_id: str):
    async with SessionLocal() as s:
        conv = await s.get(Conversation, conv_id)
        if conv is None:
            raise HTTPException(404, "conversation not found")
        msgs = await s.scalars(
            select(Message).where(Message.conversation_id == conv_id).order_by(Message.seq)
        )
        return {**_conv_dict(conv), "messages": [_msg_dict(m) for m in msgs.all()]}


class RenameRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)


@app.patch("/api/conversations/{conv_id}")
async def rename_conversation(conv_id: str, req: RenameRequest):
    async with SessionLocal() as s:
        conv = await s.get(Conversation, conv_id)
        if conv is None:
            raise HTTPException(404, "conversation not found")
        conv.title = req.title.strip()
        await s.commit()
        return _conv_dict(conv)


@app.delete("/api/conversations/{conv_id}", status_code=204)
async def delete_conversation(conv_id: str):
    async with SessionLocal() as s:
        # Explicit child delete: does not depend on the DB enforcing ON DELETE CASCADE.
        await s.execute(delete(Message).where(Message.conversation_id == conv_id))
        await s.execute(delete(Conversation).where(Conversation.id == conv_id))
        await s.commit()


# --- Models -----------------------------------------------------------------------


@app.get("/api/models")
async def list_models():
    try:
        tags = await ollama.tags()
    except Exception as exc:
        raise HTTPException(502, f"Ollama unreachable: {exc}")
    async with SessionLocal() as s:
        entries = {e.name: e for e in (await s.scalars(select(ModelEntry))).all()}
    out = []
    for tag in sorted(tags, key=lambda t: t["name"]):
        e = entries.get(tag["name"])
        details = tag.get("details") or {}
        caps = list(e.capabilities) if e and e.capabilities else []
        out.append(
            {
                "name": tag["name"],
                "size": tag.get("size", 0),
                "family": (e.family if e and e.family else details.get("family", "")),
                "parameter_size": (
                    e.parameter_size if e and e.parameter_size else details.get("parameter_size", "")
                ),
                "quantization": (
                    e.quantization if e and e.quantization else details.get("quantization_level", "")
                ),
                "context_length": e.context_length if e else None,
                "capabilities": caps,
                "chat": ("completion" in caps)
                if caps
                else not looks_like_embedding(tag["name"], details),
                "description": e.description if e else "",
                "description_source": e.description_source if e else "",
                "status": e.status if e else "pending",
                "error": e.error if e else "",
            }
        )
    return out


class PullRequest(BaseModel):
    name: str = Field(min_length=1, max_length=300)


@app.post("/api/models/pull")
async def pull_model(req: PullRequest):
    name = req.name.strip()

    async def stream():
        succeeded = False
        try:
            async for ev in ollama.pull(name):
                if "error" in ev:
                    yield sse("error", {"message": ev["error"]})
                    return
                yield sse("progress", ev)
                if ev.get("status") == "success":
                    succeeded = True
        except Exception as exc:
            yield sse("error", {"message": str(exc)})
            return
        if succeeded:
            spawn(catalog.reconcile())
            yield sse("done", {"name": name})

    return StreamingResponse(stream(), media_type="text/event-stream", headers=SSE_HEADERS)


@app.post("/api/models/{name:path}/describe", status_code=202)
async def describe_model(name: str):
    await catalog.mark_pending(name)
    spawn(catalog.describe(name))
    return {"name": name, "status": "pending"}


@app.delete("/api/models/{name:path}", status_code=204)
async def delete_model(name: str):
    try:
        await ollama.delete(name)
    except Exception as exc:
        raise HTTPException(502, str(exc))
    spawn(catalog.reconcile())


# --- Chat -------------------------------------------------------------------------


class ChatRequest(BaseModel):
    conversation_id: str | None = None
    model: str = Field(min_length=1, max_length=300)
    content: str = Field(min_length=1, max_length=200_000)
    web_search: bool = False


async def _model_info(name: str) -> tuple[list[str], int | None]:
    async with SessionLocal() as s:
        entry = await s.get(ModelEntry, name)
        if entry is None:
            return [], None
        return list(entry.capabilities or []), entry.context_length


async def _persist_assistant(conv_id, msg_id, seq, content, thinking, model, sources) -> None:
    try:
        async with SessionLocal() as s:
            if await s.get(Conversation, conv_id) is None:
                return  # deleted while streaming
            s.add(
                Message(
                    id=msg_id,
                    conversation_id=conv_id,
                    seq=seq,
                    role="assistant",
                    content=content,
                    thinking=thinking,
                    model=model,
                    sources=sources or None,
                )
            )
            conv = await s.get(Conversation, conv_id)
            conv.updated_at = utcnow()
            await s.commit()
    except Exception:
        log.exception("failed to persist assistant message")


async def _generate_title(model: str, caps: list[str], user: str, answer: str) -> str | None:
    try:
        raw = await ollama.chat(
            model,
            [
                {"role": "system", "content": prompts.TITLE_SYSTEM},
                {"role": "user", "content": f"User: {user[:1500]}\n\nAssistant: {answer[:1500]}"},
            ],
            options={"temperature": 0.3, "num_ctx": 4096},
            think=think_off(caps),
        )
    except Exception as exc:
        log.info("title generation failed: %s", exc)
        return None
    title = raw.strip().splitlines()[0].strip().strip("\"'#*").strip() if raw.strip() else ""
    return title[:80] or None


@app.post("/api/chat")
async def chat(req: ChatRequest):
    caps, model_ctx = await _model_info(req.model)
    num_ctx = min(settings.default_num_ctx, model_ctx or settings.default_num_ctx)

    async with SessionLocal() as s:
        if req.conversation_id:
            conv = await s.get(Conversation, req.conversation_id)
            if conv is None:
                raise HTTPException(404, "conversation not found")
            is_new = False
        else:
            title = " ".join(req.content.split())[:60] or "New chat"
            conv = Conversation(title=title)
            s.add(conv)
            is_new = True
        prior = list(
            (
                await s.scalars(
                    select(Message).where(Message.conversation_id == conv.id).order_by(Message.seq)
                )
            ).all()
        )
        next_seq = (
            await s.scalar(
                select(func.coalesce(func.max(Message.seq), -1)).where(
                    Message.conversation_id == conv.id
                )
            )
        ) + 1
        user_msg = Message(conversation_id=conv.id, seq=next_seq, role="user", content=req.content)
        s.add(user_msg)
        conv.model = req.model
        conv.updated_at = utcnow()
        await s.commit()
        conv_id, summary, summary_upto = conv.id, conv.summary, conv.summary_upto

    assistant_id = new_id()
    assistant_seq = next_seq + 1

    async def stream():
        answer: list[str] = []
        thinking: list[str] = []
        sources: list[dict] = []
        persisted = False
        yield sse(
            "meta", {"conversation_id": conv_id, "user_message_id": user_msg.id, "is_new": is_new}
        )
        try:
            system = prompts.SYSTEM.format(today=prompts.today_str())
            budget = (
                num_ctx
                - settings.response_reserve_tokens
                - est_tokens(system)
                - est_tokens(req.content)
                - (settings.web_context_tokens if req.web_search else 0)
            )
            if prior and sum(est_tokens(m.content) for m in prior) > max(budget, 512):
                yield sse("status", {"text": "Summarizing earlier messages…"})
            history, new_summary, new_upto, changed = await prepare_history(
                prior=prior,
                summary=summary,
                summary_upto=summary_upto,
                budget_tokens=max(budget, 512),
                model=req.model,
                caps=caps,
            )
            if changed:
                async with SessionLocal() as s:
                    c = await s.get(Conversation, conv_id)
                    if c is not None:
                        c.summary, c.summary_upto = new_summary, new_upto
                        await s.commit()
            if new_summary:
                system += "\n\n" + prompts.SUMMARY_BLOCK.format(summary=new_summary)

            state = {
                "model": req.model,
                "capabilities": caps,
                "num_ctx": num_ctx,
                "web_search": req.web_search,
                "system": system,
                "history": history,
                "user_input": req.content,
            }
            async for ev in chat_graph.astream(state, stream_mode="custom"):
                kind = ev.get("type")
                if kind == "token":
                    answer.append(ev["text"])
                elif kind == "thinking":
                    thinking.append(ev["text"])
                elif kind == "sources":
                    sources = ev["sources"]
                yield sse(kind, ev)

            await _persist_assistant(
                conv_id, assistant_id, assistant_seq, "".join(answer), "".join(thinking),
                req.model, sources,
            )
            persisted = True

            title = None
            if is_new and answer:
                title = await _generate_title(req.model, caps, req.content, "".join(answer))
                if title:
                    async with SessionLocal() as s:
                        c = await s.get(Conversation, conv_id)
                        if c is not None:
                            c.title = title
                            await s.commit()
            yield sse("done", {"message_id": assistant_id, "title": title})
        except Exception as exc:
            log.exception("chat failed")
            yield sse("error", {"message": str(exc)})
        finally:
            # Client disconnect (stop button) cancels this generator. The partial
            # answer is saved from a detached task, because further awaits inside
            # a cancelled scope would be cancelled as well.
            if not persisted and (answer or thinking):
                spawn(
                    _persist_assistant(
                        conv_id, assistant_id, assistant_seq, "".join(answer),
                        "".join(thinking), req.model, sources,
                    )
                )

    return StreamingResponse(stream(), media_type="text/event-stream", headers=SSE_HEADERS)


# --- Frontend ---------------------------------------------------------------------

_static = Path(settings.static_dir)
if _static.is_dir():
    app.mount("/", StaticFiles(directory=_static, html=True), name="static")
