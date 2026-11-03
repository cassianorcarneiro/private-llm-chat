"""Detect degenerate repetition loops in streamed model output.

Small models sometimes fall into a loop: they copy a block they just wrote,
over and over, until the context window is full. Sampling penalties do not
reliably prevent this. Ollama's default repeat_last_n is 64 tokens, so a loop
whose period is longer than that is never penalized.

The check is model-independent: it looks only at the text.
"""

import re

_DIGITS = re.compile(r"\d")

# A masked loop (digits ignored) must have a long period. Loops that increment
# a counter, such as an echoed "[85] ... [86] ..." source list, repeat whole
# blocks. Short lines that differ only by a number, such as "case 1: ...",
# "case 2: ...", are normal code and must not match.
_MASKED_MIN_PERIOD = 200
_SNAP_LIMIT = 80


def _scan(
    view: str, window: int, repeats: int, min_distinct: int, min_period: int
) -> tuple[int, int] | None:
    """Look for a periodic ending in `view`. Return (start, period) or None."""
    probe = view[-window:]
    if len(set(probe)) < min_distinct:
        return None

    starts: list[int] = []
    i = view.find(probe)
    while i != -1:
        starts.append(i)
        i = view.find(probe, i + 1)
    if len(starts) < repeats:
        return None

    last = starts[-repeats:]
    gaps = {b - a for a, b in zip(last, last[1:])}
    if len(gaps) != 1:
        return None
    period = gaps.pop()
    if period < min_period:
        return None

    # Walk back to where the periodic stretch begins.
    start = last[0]
    while start > 0 and view[start - 1] == view[start - 1 + period]:
        start -= 1
    return start, period


def _snap_to_line_end(text: str, cut: int) -> int:
    """Move a cut forward to the end of its line, if that is close.

    The periodic stretch can extend a few characters back into the text before
    the loop when both happen to end alike (for example "\\n\\n"). That leaves
    the cut a few characters short of the end of the first copy.
    """
    if cut <= 0 or cut >= len(text) or text[cut - 1] == "\n":
        return cut
    nl = text.find("\n", cut, cut + _SNAP_LIMIT)
    return cut if nl == -1 else nl + 1


def find_loop(
    text: str,
    *,
    window: int = 160,
    repeats: int = 4,
    scan: int = 8000,
    min_distinct: int = 12,
) -> int | None:
    """Return how many characters of `text` to keep if it ends in a loop, else None.

    A loop is a stretch of text that repeats with a fixed period, with the last
    `window` characters occurring at least `repeats` times at equal spacing.
    The returned length keeps exactly one copy of the repeated block.

    Guards against false positives:
    - The probe needs at least `min_distinct` distinct characters. This rejects
      separators, "ha ha ha", and tables of bare numbers.
    - The occurrences must be equally spaced, not just frequent.
    - Repeating a long block exactly three times, as a user might ask for,
      does not trigger: four occurrences of the probe need three full periods
      plus the probe itself.

    Two passes run in this order:
    1. Digits masked, long periods only: catches loops that increment a counter.
       Masking maps each digit to "0", so lengths and indices are unchanged.
    2. Exact text, any period: catches short, identical loops.
    """
    if len(text) < window * 2:
        return None
    base = max(0, len(text) - scan)
    tail = text[base:]

    for view, min_period in (
        (_DIGITS.sub("0", tail), _MASKED_MIN_PERIOD),
        (tail, 1),
    ):
        found = _scan(view, window, repeats, min_distinct, min_period)
        if found is not None:
            start, period = found
            return _snap_to_line_end(text, base + start + period)
    return None
