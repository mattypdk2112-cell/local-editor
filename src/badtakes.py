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
from pathlib import Path

from . import llm

ROOT = Path(__file__).resolve().parent
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


def first_line_word_span(words: list[dict], *, overlap: float = 0.6
                         ) -> tuple[int, int] | None:
    """The hook, protected on its LAST take rather than its first.

    This used to return word 0 to the first pause, which is the first ATTEMPT at
    the hook, and on any real take that is a false start. The bad-take pass would
    correctly flag "four earlier false-start attempts at the opening line", this
    guard would veto the removal, and the render shipped with the hook said twice:
    "There's a free open source editor on github, there's a free open source
    editor that cuts your entire reels on your laptop." Measured 2026-09-07 on a
    fresh clone.

    `cta_word_spans` below already had this right, and says why in its own
    docstring: protect the last delivery, because the earlier ones are the
    retakes you are trying to remove. Same rule, same reason, applied to the hook.

    Takes are never word-identical, so the match is on token overlap rather than
    equality: the last line that shares `overlap` of the opening line's words is
    the one that survives.
    """
    if not words:
        return None
    a, b = _line_bounds(words, 0)
    toks = {_norm(w["word"]) for w in words[a:b + 1]}
    toks.discard("")
    if len(toks) < 3:
        return a, b

    best = (a, b)
    i = b + 1
    while i < len(words):
        la, lb = _line_bounds(words, i)
        line = {_norm(w["word"]) for w in words[la:lb + 1]}
        line.discard("")
        if line and len(toks & line) / len(toks) >= overlap:
            best = (la, lb)
        i = lb + 1
    return best


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
    # Walk every backend, not just the best one. Claude Code being INSTALLED is
    # not the same as being logged in: on a headless box the binary was found,
    # picked, and answered "Not logged in - Please run /login", which took the
    # whole edit down instead of using the key sitting next to it.
    chain = llm.resolve_backends(ROOT.parent) if backend is None else [backend]
    parsed, usage, backend = llm.complete_json_any(
        chain, PROMPT + _render_words(words), timeout=300.0, temperature=0.2,
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


def stutters(words: list[dict], *, min_len: int = 1, max_len: int = 8,
             gap: float = 1.2) -> list[dict]:
    """Back-to-back repeats in an ASSEMBLED cut. [{start, end, text, n}].

    This is a check on the render, not on the source, and it exists because the
    source transcript cannot be trusted to reveal a repeated take. On Matt's
    2026-09-06 editor take whisper logged the hook's first word at 33.16s; the
    clean take actually starts at 34.84s, and it hid the 1.68s of the PREVIOUS
    attempt inside a single 1.62s-long token ("open"). A range planned off those
    word times shipped "There's a free there's a free open source editor" and
    neither the retake sweep nor --strip-filler saw it, because both read the
    same lying transcript.

    Re-transcribing the finished file and looking for an n-gram immediately
    followed by itself catches that class outright, whatever produced it: a bad
    script match, a hand-picked range, or a retake the LLM pass missed.
    """
    norms = [_norm(w["word"]) for w in words]
    out: list[dict] = []
    i = 0
    while i < len(norms):
        hit = None
        for n in range(max_len, min_len - 1, -1):
            a, b = norms[i:i + n], norms[i + n:i + 2 * n]
            if len(b) < n or a != b or not all(a):
                continue
            # A deliberate repeat for emphasis lands immediately; a stray take has
            # a beat of air between the two halves. Both are worth showing, so the
            # gap only decides nothing here — it is reported so the caller can see.
            hit = n
            break
        if hit:
            span = words[i:i + 2 * hit]
            out.append({"start": round(float(span[0]["start"]), 2),
                        "end": round(float(span[-1]["end"]), 2),
                        # where the SECOND copy begins. This is the repair point:
                        # everything before it is the take that should not be here.
                        "mid": round(float(span[hit]["start"]), 2),
                        "text": " ".join(w["word"].strip() for w in span),
                        "n": hit})
            i += 2 * hit
        else:
            i += 1
    return out


def map_to_source(ranges: list[list[float]], a: float, b: float) -> list[list[float]]:
    """Interval [a, b) on the CUT timeline -> the source spans it is made of."""
    out, acc = [], 0.0
    for rs, re_ in ranges:
        d = re_ - rs
        lo, hi = max(a, acc), min(b, acc + d)
        if hi > lo:
            out.append([rs + (lo - acc), rs + (hi - acc)])
        acc += d
    return out


def repeat_spans(ranges: list[list[float]], reps: list[dict], *,
                 head_window: float = 0.60, pad: float = 0.04,
                 deliberate_n: int = 3
                 ) -> tuple[list[list[float]], list[str]]:
    """The CUT-timeline spans to delete: the FIRST copy of each repeat.

    Reporting a repeat is not fixing one. Every repeat that reaches a render is
    the same shape — one copy too many — so the repair is one operation: map
    [start, mid) from the cut timeline back onto the source ranges and subtract
    it. Doing it in cut time is what makes this general. The three failures on
    Matt's 2026-09-06 take all fall out of it:

      * head of a range   "There's a free | there's a free open source editor"
                          (whisper put the hook 1.68s early, so the range opened
                          on the tail of the previous attempt)
      * across a seam     "...half a second and" + "and Every um"
      * a false start     "but I" + "I took one and rebuilt it"

    A repeat sitting well inside a single range with no seam near it is the
    speaker actually saying it twice, which is a script decision, so that one is
    reported and left alone.
    """
    seams, acc = [], 0.0
    for a, b in ranges:
        seams.append(acc)
        acc += b - a
    seams.append(acc)

    drop: list[list[float]] = []
    notes: list[str] = []
    for rep in reps:
        first = [rep["start"], max(rep["start"], rep["mid"] - pad)]
        near_seam = any(abs(s - rep["start"]) <= head_window
                        or first[0] <= s <= rep["mid"] for s in seams)
        if not near_seam and rep["n"] < deliberate_n:
            # Short doubles away from a join are how people talk ("that that",
            # "and and"). Say nothing about a single word; mention a pair.
            if rep["n"] >= 2:
                notes.append(f"repeat at {rep['start']:.2f}s ({rep['text']!r}) is mid-range "
                             f"and short — reads as speech, left alone")
            continue
        # A LONG repeat is never deliberate. The first version of this function
        # spared anything mid-range on the theory that a cut artefact always
        # lands on a join, which is only true once a script has picked the takes.
        # On a raw ramble both attempts sit inside one kept range, and the rule
        # shipped "you don't have to be experienced with coding" twice in a row,
        # eight words, into a finished render. Measured 2026-09-07 on a fresh
        # clone cutting a 3:44 take with no script.
        if first[1] - first[0] < 0.05:
            continue
        drop.append(first)
        notes.append(f"dropped the first copy of {rep['text']!r} "
                     f"({first[1] - first[0]:.2f}s at {rep['start']:.2f}s in the cut)")

    return drop, notes


def _subtract(ranges: list[list[float]], spans: list[list[float]]) -> list[list[float]]:
    out = [list(r) for r in ranges]
    for sa, sb in sorted(spans):
        nxt = []
        for a, b in out:
            if sb <= a or sa >= b:
                nxt.append([a, b])
                continue
            if sa > a:
                nxt.append([a, min(sa, b)])
            if sb < b:
                nxt.append([max(sb, a), b])
        out = nxt
    return [[round(a, 3), round(b, 3)] for a, b in out if b - a > 0]
