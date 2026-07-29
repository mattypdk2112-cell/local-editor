"""Stage 1.5 — semantic bad-take detection (optional, --bad-takes).

Silence-cutting can't catch a bad take the speaker talked straight through: a
false start, a line re-recorded until they nailed it, or a spoken direction
("get rid of that", "let me redo that"). This reads the transcript with a model
and returns the time-spans to remove, keeping only the clean final performance.

Which model does the reading is `llm.py`'s problem, not this file's. By default
it's the Claude Code already on the machine, so there is no key to set up. No-op
with a clear message when nothing is available, so the flag never hard-fails a run.
"""
from __future__ import annotations

import re

from . import llm
from .llm import load_env_key  # noqa: F401  (re-exported: callers still import it here)

PROMPT = """You are a video editor's assistant. Below is a raw talking-head transcript with \
EVERY WORD numbered as `index:word`. The speaker recorded in ONE take with mistakes: false \
starts, lines re-recorded until they got them right, stumbles/restarts, and spoken directions \
to themselves or the editor (e.g. "get rid of that", "cut that", "let me redo that", "wait we \
do that again", "start again", "scratch that", "wait no").

Identify the WORD RANGES to REMOVE so only the clean, final performance remains.

Rules:
- Remove false starts and abandoned sentences.
- When a line is repeated (a re-take), KEEP the last/cleanest version, remove the earlier attempts.
- Remove spoken directions / meta-commentary that are not part of the actual script — INCLUDING \
when they are only the first few words of an otherwise-good sentence (e.g. a line that starts \
"wait, we do that again, comment gap and..." → remove just the "wait, we do that again" words, \
keep "comment gap and...").
- Be conservative: if words are unique, on-script content, KEEP them. When unsure, KEEP.
- Never remove more than necessary. Imperfect-but-real content stays; only clear retakes, false \
starts, and spoken directions get removed.

Ranges are INCLUSIVE word indices. Return ONLY JSON in this exact shape:
{"remove": [{"from": <int>, "to": <int>, "reason": "<short reason>"}]}

Numbered transcript:
"""


def _render_words(words: list[dict], per_line: int = 10) -> str:
    lines = []
    for i in range(0, len(words), per_line):
        chunk = words[i:i + per_line]
        lines.append(" ".join(f"{i + j}:{w['word']}" for j, w in enumerate(chunk)))
    return "\n".join(lines)


def _norm(word: str) -> str:
    return re.sub(r"[^a-z0-9]", "", word.lower())


def _line_bounds(words: list[dict], idx: int, *, gap: float = 0.45, max_words: int = 22) -> tuple[int, int]:
    """The spoken 'line' around words[idx], bounded by pauses (gaps > `gap`).

    Whisper punctuation is unreliable, so we find phrase breaks by timing, not
    full stops. Capped at max_words so a pause-free run can't protect the world.
    """
    start = idx
    while start > 0 and words[start]["start"] - words[start - 1]["end"] <= gap and idx - start < max_words:
        start -= 1
    end = idx
    while end < len(words) - 1 and words[end + 1]["start"] - words[end]["end"] <= gap and end - idx < max_words:
        end += 1
    return (start, end)


def first_line_word_span(words: list[dict]) -> tuple[int, int] | None:
    """The hook = the first spoken line (word 0 to the first real pause)."""
    if not words:
        return None
    return _line_bounds(words, 0)


def cta_word_spans(words: list[dict], cta_terms: list[str]) -> list[tuple[int, int]]:
    """Protect the LAST occurrence of a CTA term (the final delivery), as its line.

    Only the last one: earlier occurrences are retakes we WANT the bad-take pass
    to remove. Protecting all of them would keep every retake.
    """
    norms = [_norm(w["word"]) for w in words]
    last_i = -1
    for term in cta_terms:
        toks = [_norm(t) for t in term.split() if _norm(t)]
        if not toks:
            continue
        L = len(toks)
        for i in range(len(norms) - L + 1):
            if norms[i:i + L] == toks and i > last_i:
                last_i = i
    return [_line_bounds(words, last_i)] if last_i >= 0 else []


def _subtract_protected(a: int, b: int, protected: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Sub-ranges of [a,b] (inclusive) not covered by any protected span."""
    blocked = set()
    for ps, pe in protected:
        blocked.update(range(max(a, ps), min(b, pe) + 1))
    pieces: list[tuple[int, int]] = []
    s = None
    for i in range(a, b + 1):
        if i in blocked:
            if s is not None:
                pieces.append((s, i - 1))
                s = None
        elif s is None:
            s = i
    if s is not None:
        pieces.append((s, b))
    return pieces


def detect_bad_takes(
    transcript: dict,
    *,
    backend: dict,
    protect_spans: list[tuple[int, int]] | None = None,
) -> dict:
    """Return {spans: [[start,end],...], removed: [{from,to,reason,text}], usage: {...}}.

    Works on WORD ranges (inclusive), so it can drop a leaked direction that is
    only the prefix of an otherwise-good sentence, and stays unambiguous even
    when a phrase repeats across re-takes. `protect_spans` (hook + CTA word
    ranges) are carved out of every removal so the AI can never cut them.
    """
    words = transcript.get("words") or []
    if not words:
        return {"spans": [], "removed": [], "usage": {}}
    protect_spans = protect_spans or []

    # Low temperature: picking the keeper take is a judgement call, not a creative one.
    parsed, usage = llm.complete_json(
        backend, PROMPT + _render_words(words), timeout=300.0, temperature=0.2,
    )

    n = len(words)
    removed: list[dict] = []
    spans: list[list[float]] = []
    for item in parsed.get("remove", []):
        a, b = item.get("from"), item.get("to")
        if not isinstance(a, int) or not isinstance(b, int):
            continue
        a, b = max(0, min(a, b)), min(n - 1, max(a, b))
        if a > b:
            continue
        reason = item.get("reason", "")
        pieces = _subtract_protected(a, b, protect_spans)
        if not pieces:
            full = " ".join(w["word"] for w in words[a:b + 1])
            print(f"  ↩ kept protected (hook/CTA): {full[:55].strip()!r}")
            continue
        if pieces != [(a, b)]:
            print(f"  ↩ trimmed removal to protect hook/CTA: {reason}")
        for pa, pb in pieces:
            phrase = " ".join(w["word"] for w in words[pa:pb + 1])
            removed.append({"from": pa, "to": pb, "reason": reason, "text": phrase})
            spans.append([float(words[pa]["start"]), float(words[pb]["end"])])

    return {"spans": spans, "removed": removed, "usage": usage}


def estimate_cost(usage: dict, backend_name: str = "gemini") -> float:
    """What that pass cost. Priced per backend over in llm.py."""
    return llm.estimate_cost(usage, backend_name)
