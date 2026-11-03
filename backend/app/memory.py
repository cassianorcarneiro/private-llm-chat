"""Context-window management: sliding window plus a rolling summary.

The newest messages that fit the token budget are sent verbatim. Older ones are
folded into a running summary, which is updated incrementally (old summary +
newly evicted messages) so the cost per turn stays bounded.
"""

from .db import Message
from .ollama_client import ollama, think_off
from . import prompts


def est_tokens(text: str) -> int:
    # ~3 chars/token is conservative for mixed English/Portuguese and code.
    # Overestimating is the safe direction: it never overflows num_ctx.
    return len(text) // 3 + 4


async def summarize(model: str, caps: list[str], previous: str, messages: list[Message]) -> str:
    transcript = "\n\n".join(f"{m.role.upper()}: {m.content[:4000]}" for m in messages)
    user = f"Previous summary:\n{previous or '(none)'}\n\nNew messages:\n{transcript}"
    out = await ollama.chat(
        model,
        [{"role": "system", "content": prompts.SUMMARIZER_SYSTEM}, {"role": "user", "content": user}],
        options={"temperature": 0.2, "num_ctx": 8192},
        think=think_off(caps),
    )
    return out.strip()


async def prepare_history(
    *,
    prior: list[Message],
    summary: str,
    summary_upto: int,
    budget_tokens: int,
    model: str,
    caps: list[str],
) -> tuple[list[dict], str, int, bool]:
    """Return (ollama messages, summary, summary_upto, summary_changed)."""
    used = est_tokens(summary) if summary else 0
    first = len(prior)
    for i in range(len(prior) - 1, -1, -1):
        cost = est_tokens(prior[i].content)
        if used + cost > budget_tokens:
            break
        used += cost
        first = i
    # Start the window on a user turn. Some chat templates reject a history
    # that opens with an assistant message after the system prompt.
    while first < len(prior) and prior[first].role != "user":
        first += 1

    changed = False
    if first > summary_upto:
        summary = await summarize(model, caps, summary, prior[summary_upto:first])
        summary_upto = first
        changed = True

    start = max(first, summary_upto)
    history = [
        {"role": m.role, "content": m.content}
        for m in prior[start:]
        if m.role in ("user", "assistant") and m.content
    ]
    return history, summary, summary_upto, changed
