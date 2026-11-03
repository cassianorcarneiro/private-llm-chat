"""Answer pipeline as a LangGraph state machine.

    START -> plan --(queries?)--> research -> generate -> END
                  \\-------------(none)------^

LangGraph is used for what it adds over plain code: typed shared state,
conditional edges, and a streaming channel (`get_stream_writer`) that lets every
node emit progress events (status, sources, tokens) to the HTTP layer.

The planner runs on the *selected* chat model. It is already resident in VRAM,
so planning costs no model swap. On a single 16 GB GPU a separate router model
would force an unload/reload on every turn.
"""

import json
import logging
from typing import TypedDict

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph

from . import prompts
from .config import settings
from .ollama_client import ollama, think_off
from .search import web_research

log = logging.getLogger(__name__)


class ChatState(TypedDict, total=False):
    model: str
    capabilities: list[str]
    num_ctx: int
    web_search: bool
    system: str
    history: list[dict]
    user_input: str
    queries: list[str]
    web_context: str
    sources: list[dict]


async def plan(state: ChatState) -> dict:
    if not state.get("web_search"):
        return {"queries": []}
    write = get_stream_writer()
    write({"type": "status", "text": "Deciding whether to search the web…"})
    recent = state.get("history", [])[-6:]
    convo = "\n".join(f"{m['role']}: {m['content'][:1500]}" for m in recent) or "(none)"
    try:
        raw = await ollama.chat(
            state["model"],
            [
                {"role": "system", "content": prompts.PLANNER_SYSTEM.format(today=prompts.today_str())},
                {
                    "role": "user",
                    "content": prompts.PLANNER_USER.format(
                        conversation=convo, message=state["user_input"]
                    ),
                },
            ],
            options={"temperature": 0, "num_ctx": state["num_ctx"]},
            fmt=prompts.PLAN_SCHEMA,
            think=think_off(state.get("capabilities", [])),
        )
        data = json.loads(raw)
        queries = []
        if data.get("needs_search"):
            queries = [q.strip() for q in data.get("queries", []) if isinstance(q, str) and q.strip()]
        queries = queries[:3]
    except Exception as exc:
        # The user explicitly enabled search: fall back to the raw message.
        log.warning("planner failed (%s); searching the raw message", exc)
        queries = [state["user_input"][:200]]
    if not queries:
        write({"type": "status", "text": "No web search needed."})
    return {"queries": queries}


def after_plan(state: ChatState) -> str:
    return "research" if state.get("queries") else "generate"


async def research(state: ChatState) -> dict:
    write = get_stream_writer()
    write({"type": "status", "text": "Searching: " + " · ".join(state["queries"])})
    try:
        context, sources, method = await web_research(
            state["queries"], settings.web_context_tokens * 3
        )
    except Exception as exc:
        log.warning("web research failed: %s", exc)
        write({"type": "status", "text": f"Web search failed: {exc}"})
        return {"web_context": "", "sources": []}
    write({"type": "sources", "sources": sources})
    if sources:
        write({"type": "status", "text": f"Reading {len(sources)} sources ({method} ranking)…"})
    else:
        write({"type": "status", "text": "No usable web results."})
    return {"web_context": context, "sources": sources}


async def generate(state: ChatState) -> dict:
    write = get_stream_writer()
    system = state["system"]
    if state.get("web_context"):
        system += "\n\n" + prompts.WEB_CONTEXT.format(context=state["web_context"])
    messages = [
        {"role": "system", "content": system},
        *state.get("history", []),
        {"role": "user", "content": state["user_input"]},
    ]
    async for chunk in ollama.chat_stream(
        state["model"], messages, options={"num_ctx": state["num_ctx"]}
    ):
        msg = chunk.get("message") or {}
        if msg.get("thinking"):
            write({"type": "thinking", "text": msg["thinking"]})
        if msg.get("content"):
            write({"type": "token", "text": msg["content"]})
        if chunk.get("done"):
            write(
                {
                    "type": "stats",
                    "prompt_tokens": chunk.get("prompt_eval_count"),
                    "output_tokens": chunk.get("eval_count"),
                    "eval_duration_ns": chunk.get("eval_duration"),
                }
            )
    return {}


def build_graph():
    g = StateGraph(ChatState)
    g.add_node("plan", plan)
    g.add_node("research", research)
    g.add_node("generate", generate)
    g.add_edge(START, "plan")
    g.add_conditional_edges("plan", after_plan, {"research": "research", "generate": "generate"})
    g.add_edge("research", "generate")
    g.add_edge("generate", END)
    return g.compile()


chat_graph = build_graph()
