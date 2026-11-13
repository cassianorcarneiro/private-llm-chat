"""Answer pipeline as a LangGraph state machine.

    START -> plan --(queries?)--> research -> evaluate --(weak, rounds left)--> research
                  \\-------------(none)---------------\\--(ok / out of rounds)--> generate -> END

The evaluate node checks the retrieved passages (coverage by an LLM judge,
trust by source tier). When they are weak it sends refined queries back to
research, up to settings.search_max_rounds rounds in total.

LangGraph is used for what it adds over plain code: typed shared state,
conditional edges, and a streaming channel (`get_stream_writer`) that lets every
node emit progress events (status, sources, tokens) to the HTTP layer.

The planner runs on the *selected* chat model. It is already resident in VRAM,
so planning costs no model swap. On a single 16 GB GPU a separate router model
would force an unload/reload on every turn.
"""

import json
import logging
from contextlib import aclosing
from typing import TypedDict

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph

from . import prompts
from .config import settings
from .loopguard import find_loop
from .memory import est_tokens
from .ollama_client import ollama, think_off
from .search import build_context, gather, merge_pool, select

log = logging.getLogger(__name__)

# Run the repetition check after this many new characters. It costs ~0.1 ms.
LOOP_CHECK_EVERY = 48


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
    # Evidence loop
    rounds: int
    tried: list[str]
    pool: list[dict]
    selected: list[dict]
    retry: bool
    failed: bool
    evidence: dict



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
    rnd = state.get("rounds", 0) + 1
    queries = state["queries"]
    prefix = "Searching again" if rnd > 1 else "Searching"
    write({"type": "status", "text": f"{prefix}: " + " · ".join(queries)})
    pool = state.get("pool", [])
    tried = state.get("tried", []) + [q for q in queries if q not in state.get("tried", [])]
    try:
        new = await gather(queries, skip_urls={c["url"] for c in pool})
        pool = merge_pool(pool, new)
        # Rank against every query tried so far, so round 2 refines round 1.
        selected, method = await select(tried, pool, settings.web_context_tokens * 3)
    except Exception as exc:
        log.warning("web research failed: %s", exc)
        write({"type": "status", "text": f"Web search failed: {exc}"})
        if rnd > 1:  # keep what round 1 found
            return {"rounds": rnd, "failed": True}
        return {"web_context": "", "sources": [], "selected": [], "rounds": rnd, "failed": True}
    context, sources = build_context(selected)
    write({"type": "sources", "sources": sources})
    if sources:
        write({"type": "status", "text": f"Reading {len(sources)} sources ({method} ranking)…"})
    else:
        write({"type": "status", "text": "No usable web results."})
    return {
        "web_context": context,
        "sources": sources,
        "selected": selected,
        "pool": pool,
        "tried": tried,
        "rounds": rnd,
        "failed": False,
    }


async def judge(state: ChatState) -> dict | None:
    """Ask the chat model whether the passages cover the question.

    Returns None on any failure. The caller then treats the evidence as
    sufficient, i.e. the pipeline behaves as it did before this check existed.
    A broken judge must not block answers.
    """
    passages = "\n\n".join(
        f"[{i}] {c['host']} ({c['tier']}, {c['date'] or 'no date'}): {c['text'][:350]}"
        for i, c in enumerate(state.get("selected", []), 1)
    )
    try:
        raw = await ollama.chat(
            state["model"],
            [
                {"role": "system", "content": prompts.EVALUATOR_SYSTEM.format(today=prompts.today_str())},
                {
                    "role": "user",
                    "content": prompts.EVALUATOR_USER.format(
                        question=state["user_input"],
                        tried="\n".join(f"- {q}" for q in state.get("tried", [])),
                        passages=passages or "(none)",
                    ),
                },
            ],
            options={"temperature": 0, "num_ctx": state["num_ctx"]},
            fmt=prompts.EVAL_SCHEMA,
            think=think_off(state.get("capabilities", [])),
        )
        data = json.loads(raw)
        return {
            "sufficient": bool(data.get("sufficient")),
            "missing": str(data.get("missing", ""))[:300],
            "queries": [q.strip() for q in data.get("queries", []) if isinstance(q, str) and q.strip()],
        }
    except Exception as exc:
        log.warning("evidence judge failed (%s); assuming sufficient", exc)
        return None


async def evaluate(state: ChatState) -> dict:
    """Judge the retrieved evidence; decide between another round and generation."""
    write = get_stream_writer()
    sources = state.get("sources", [])
    selected = state.get("selected", [])
    trusted = any(c["tier"] == "trusted" for c in selected)

    verdict = None
    if settings.search_verify and selected:
        write({"type": "status", "text": "Checking whether the sources answer the question…"})
        verdict = await judge(state)
    sufficient = verdict["sufficient"] if verdict else bool(selected) or not settings.search_verify
    trust_gap = settings.search_require_trusted and not trusted
    weak = not sufficient or trust_gap or (not selected and bool(state.get("queries")))

    rnd = state.get("rounds", 1)
    if weak and rnd < settings.search_max_rounds:
        tried = state.get("tried", [])
        fresh = [q for q in (verdict or {}).get("queries", []) if q not in tried][:2]
        if not fresh and trust_gap and tried:
            fresh = [f"{tried[0]} official site documentation"]
        if not fresh and not selected:
            fresh = [q for q in [state["user_input"][:200]] if q not in tried]
        if fresh:
            return {"queries": fresh, "retry": True}

    problems = []
    if not sufficient:
        missing = (verdict or {}).get("missing", "")
        problems.append(
            "The retrieved passages may not fully answer the question"
            + (f" (missing: {missing})." if missing else ".")
        )
    if trust_gap and selected:
        problems.append("None of the sources is a primary or otherwise trusted source.")
    evidence = {
        "sufficient": sufficient,
        "trusted": trusted,
        "rounds": rnd,
        "missing": (verdict or {}).get("missing", ""),
        "problems": problems,
    }
    if sources:
        write({"type": "evidence", **evidence})
    return {"retry": False, "evidence": evidence}


def after_research(state: ChatState) -> str:
    return "generate" if state.get("failed") else "evaluate"


def after_evaluate(state: ChatState) -> str:
    return "research" if state.get("retry") else "generate"


async def generate(state: ChatState) -> dict:
    write = get_stream_writer()
    system = state["system"]
    if state.get("web_context"):
        system += "\n\n" + prompts.WEB_CONTEXT.format(context=state["web_context"])
        problems = (state.get("evidence") or {}).get("problems", [])
        if problems:
            system += "\n\n" + prompts.EVIDENCE_NOTE.format(problem=" ".join(problems))
    messages = [
        {"role": "system", "content": system},
        *state.get("history", []),
        {"role": "user", "content": state["user_input"]},
    ]
    # Cap the output at the room left in the context window. Without a cap,
    # Ollama keeps generating after the window is full by discarding old tokens
    # ("context shift"). A model that has started to loop then never stops.
    # est_tokens overestimates, so the cap errs on the short side.
    prompt_tokens = sum(est_tokens(m["content"]) for m in messages)
    options = {
        "num_ctx": state["num_ctx"],
        "num_predict": max(512, state["num_ctx"] - prompt_tokens),
    }

    answer = ""
    checked = 0
    # aclosing() closes the HTTP stream as soon as we leave the loop, which makes
    # Ollama stop generating. Without it, closing waits for garbage collection.
    async with aclosing(ollama.chat_stream(state["model"], messages, options=options)) as stream:
        async for chunk in stream:
            msg = chunk.get("message") or {}
            if msg.get("thinking"):
                write({"type": "thinking", "text": msg["thinking"]})
            if msg.get("content"):
                answer += msg["content"]
                write({"type": "token", "text": msg["content"]})
                if len(answer) - checked >= LOOP_CHECK_EVERY:
                    checked = len(answer)
                    keep = find_loop(answer)
                    if keep is not None:
                        log.warning(
                            "repetition loop from %s: stopped at %d chars, kept %d",
                            state["model"], len(answer), keep,
                        )
                        write({"type": "loop", "keep": keep})
                        break
            if chunk.get("done"):
                write(
                    {
                        "type": "stats",
                        "prompt_tokens": chunk.get("prompt_eval_count"),
                        "output_tokens": chunk.get("eval_count"),
                        "eval_duration_ns": chunk.get("eval_duration"),
                    }
                )
                if chunk.get("done_reason") == "length":
                    write(
                        {
                            "type": "notice",
                            "text": "The answer was cut at the context limit. "
                            "Start a new chat or raise DEFAULT_NUM_CTX.",
                        }
                    )
    return {}


def build_graph():
    g = StateGraph(ChatState)
    g.add_node("plan", plan)
    g.add_node("research", research)
    g.add_node("evaluate", evaluate)
    g.add_node("generate", generate)
    g.add_edge(START, "plan")
    g.add_conditional_edges("plan", after_plan, {"research": "research", "generate": "generate"})
    g.add_conditional_edges(
        "research", after_research, {"evaluate": "evaluate", "generate": "generate"}
    )
    g.add_conditional_edges(
        "evaluate", after_evaluate, {"research": "research", "generate": "generate"}
    )
    g.add_edge("generate", END)
    return g.compile()


chat_graph = build_graph()
