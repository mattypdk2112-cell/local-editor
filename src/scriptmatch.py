"""Stage 1.5b — script-aware, framework-grounded cutting.

You write a script before filming, then stutter / do retakes / freestyle on
camera. This lines the raw transcript up against that script: for each scripted
line it keeps the best take, in script order, drops the retakes and directions,
and guarantees the hook and CTA survive. The heavy lift (mapping script lines to
transcript word spans) is an LLM call; correctness is enforced afterwards in
plain code (difflib verify, overlap resolution, hook/CTA force-keep, a coverage
gate that falls back rather than ship a butchered cut).

Reuses badtakes' Gemini/httpx pattern and roughcut.ranges_from_words. The
assemble filtergraph concatenates ranges in the order given, so handing it
script-ordered ranges reorders the footage for free.
"""
from __future__ import annotations

import difflib
import json
import re
from pathlib import Path

import httpx

from . import badtakes
from .roughcut import (
    FILLER,
    FILLER_AGGRESSIVE,
    breath_points,
    end_edge,
    merge_forward_seams,
    ranges_from_words,
    start_edge,
)

GEMINI_BASE = "https://generativelanguage.googleapis.com"
DEFAULT_MODEL = "gemini-2.5-flash"

PROMPT = """You are aligning raw talking-head footage to the script the creator meant to say.

Below is (1) the intended SCRIPT as numbered lines, then (2) the RAW TRANSCRIPT with every \
word numbered `index:word`. The creator stutters, does several takes of a line, freestyles, \
and talks to himself ("get rid of that", "wait do that again").

For EACH script line, return the single best CONTINUOUS word-index span in the transcript that \
delivers it. Rules:
- When a line was recorded several times, keep the LAST clean take.
- The spoken version can paraphrase the script (he ad-libs) - match on MEANING, not exact words.
- Return from=null, to=null if a line was never actually delivered.
- Spans must not overlap. Leave false starts, retakes, and self-directions OUT (just don't include them).
- The FIRST line (the hook) and any line with the call-to-action / comment keyword MUST be returned \
if they were said at all.

Return ONLY JSON in this exact shape:
{"matches": [{"line": <int>, "from": <int|null>, "to": <int|null>, "note": "<short>"}]}
"""


def flatten_script(data) -> list[str]:
    """Flatten a content.script value into ordered spoken lines.

    Handles the shapes it's actually stored in:
      - a flat list of strings
      - {"script": [...]}
      - the structured card shape {hook:{verbal,...}, body:[...], landing, cta}
        → order: hook.verbal, body[], landing, cta (what the creator says, in order)
    """
    if isinstance(data, str):
        return [ln.strip() for ln in data.splitlines() if ln.strip()]
    if isinstance(data, list):
        return [str(x).strip() for x in data if str(x).strip()]
    if isinstance(data, dict):
        if isinstance(data.get("script"), (list, dict)):
            return flatten_script(data["script"])
        lines: list[str] = []
        hook = data.get("hook")
        if isinstance(hook, dict):
            if hook.get("verbal"):
                lines.append(str(hook["verbal"]).strip())
        elif hook:
            lines.append(str(hook).strip())
        for key in ("body", "value", "beats"):
            v = data.get(key)
            if isinstance(v, list):
                lines += [str(x).strip() for x in v if str(x).strip()]
            elif v:
                lines.append(str(v).strip())
        for key in ("landing", "cta"):
            if data.get(key):
                lines.append(str(data[key]).strip())
        return [ln for ln in lines if ln]
    return []


def load_script(path: Path) -> list[str]:
    """Ordered spoken lines from a .txt (one beat per line) or .json script file."""
    text = path.read_text()
    if path.suffix.lower() == ".json":
        return flatten_script(json.loads(text))
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def _tokens(s: str) -> list[str]:
    # Strip apostrophes BEFORE splitting so a script's "I'm"/"it's" tokenizes the
    # same way the transcript side does (badtakes._norm drops all non-alnum, so a
    # word becomes "im"/"its"). Without this the script splits "I'm" into i+m, the
    # match scores higher WITHOUT it, and the opening word gets clipped off the hook.
    return re.findall(r"[a-z0-9]+", re.sub(r"['’]", "", s.lower()))


def _verify_span(script_line: str, words: list[dict], a: int, b: int) -> float:
    """Loose token-overlap ratio between a script line and transcript words[a:b]."""
    st = _tokens(script_line)
    sp = _tokens(" ".join(w["word"] for w in words[a:b + 1]))
    if not st:
        return 0.0
    return difflib.SequenceMatcher(None, st, sp).ratio()


def _best_window(script_line: str, words: list[dict]) -> tuple[float, tuple[int, int] | None]:
    """Search the transcript for the window best matching a script line (fallback)."""
    st = _tokens(script_line)
    L = len(st)
    if L == 0 or not words:
        return (0.0, None)
    toks = [badtakes._norm(w["word"]) for w in words]
    best_r, best_span = 0.0, None
    for wlen in range(max(1, L - 3), L + 4):
        for i in range(0, len(words) - wlen + 1):
            sp = [t for t in toks[i:i + wlen] if t]
            r = difflib.SequenceMatcher(None, st, sp).ratio()
            if r > best_r:
                best_r, best_span = r, (i, i + wlen - 1)
    return (best_r, best_span)


def align_script(transcript: dict, script_lines: list[str], *, api_key: str, model: str = DEFAULT_MODEL) -> dict:
    """Ask the LLM to map each script line to a transcript word span."""
    words = transcript.get("words") or []
    if not words or not script_lines:
        return {"matches": [], "usage": {}}

    numbered_script = "\n".join(f"[{i}] {ln}" for i, ln in enumerate(script_lines))
    prompt = f"{PROMPT}\nSCRIPT:\n{numbered_script}\n\nTRANSCRIPT:\n{badtakes._render_words(words)}"
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"response_mime_type": "application/json", "temperature": 0.2},
    }
    with httpx.Client(timeout=httpx.Timeout(120.0, connect=15.0)) as c:
        r = c.post(
            f"{GEMINI_BASE}/v1beta/models/{model}:generateContent",
            params={"key": api_key}, json=body,
        )
        if r.status_code >= 300:
            raise RuntimeError(f"gemini {r.status_code}: {r.text[:200]}")
        data = r.json()

    usage = data.get("usageMetadata", {}) or {}
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    text = re.sub(r"^```(?:json)?|```$", "", text.strip()).strip()
    return {"matches": json.loads(text).get("matches", []), "usage": usage}


def align_script_local(transcript: dict, script_lines: list[str]) -> dict:
    """Deterministic alignment — NO LLM. For each script line, pick the transcript
    window with the best token-overlap (difflib `_best_window`). Same input always
    yields the same matches, so the cut is reproducible run-to-run.

    Line-to-line overlaps are resolved downstream in `build_script_cut` (earlier
    script line wins). For a line the creator said several times this naturally
    lands on the fullest/cleanest take (highest overlap), dropping the retries —
    and any spoken content NOT in the script (tangents, false starts, asides) is
    simply never matched, so it falls away.
    """
    words = transcript.get("words") or []
    matches: list[dict] = []
    for i, line in enumerate(script_lines):
        r, span = _best_window(line, words)
        if span and r > 0:
            matches.append({"line": i, "from": span[0], "to": span[1], "note": f"local {r:.2f}"})
        else:
            matches.append({"line": i, "from": None, "to": None, "note": "no match"})
    return {"matches": matches, "usage": {}}


def _span_has_cta(words: list[dict], a: int, b: int, cta_terms: list[str]) -> bool:
    norms = [badtakes._norm(w["word"]) for w in words[a:b + 1]]
    for term in cta_terms:
        toks = [badtakes._norm(t) for t in term.split() if badtakes._norm(t)]
        if toks and any(norms[i:i + len(toks)] == toks for i in range(len(norms) - len(toks) + 1)):
            return True
    return False


def build_script_cut(
    transcript: dict,
    script_lines: list[str],
    matches: list[dict],
    duration: float,
    *,
    max_gap: float = 0.2,
    lead_pad: float = 0.05,
    trail_pad: float = 0.06,
    strip_filler: bool = False,
    aggressive_filler: bool = False,
    protect_hook: bool = True,
    cta_terms: list[str] | None = None,
    min_keep_ratio: float = 0.4,
    verify_threshold: float = 0.4,
    cta_beat: float = 0.0,  # off: a frozen-frame beat reads as a paused video; needs live "settling" footage
    sil_starts: list[float] | None = None,
    sil_ends: list[float] | None = None,
) -> dict:
    """Turn LLM matches into a `plan_cut`-shaped dict with ranges in SCRIPT order.

    Sets `fallback=True` (leaving the caller to fall back) if too few lines could
    be placed confidently.
    """
    words = transcript.get("words") or []
    n = len(words)
    cta_terms = cta_terms or []
    filler_set = FILLER_AGGRESSIVE if aggressive_filler else FILLER

    # 1 — verify each proposed span; re-search when the LLM span reads wrong.
    kept: list[tuple[int, int, int]] = []  # (line_index, a, b)
    for m in matches:
        li, a, b = m.get("line"), m.get("from"), m.get("to")
        if not isinstance(li, int) or li < 0 or li >= len(script_lines):
            continue
        if not isinstance(a, int) or not isinstance(b, int):
            continue
        a, b = max(0, min(a, b)), min(n - 1, max(a, b))
        if a > b:
            continue
        ratio = _verify_span(script_lines[li], words, a, b)
        if ratio < verify_threshold:
            br, bspan = _best_window(script_lines[li], words)
            if bspan and br > ratio:
                ratio, (a, b) = br, bspan
            if ratio < verify_threshold:
                continue
        kept.append((li, a, b))

    matched_lines = {li for li, _, _ in kept}

    # 2 — force-keep the hook and the CTA even if the LLM missed them.
    if protect_hook and 0 not in matched_lines:
        h = badtakes.first_line_word_span(words)
        if h:
            kept.append((0, h[0], h[1]))
    if cta_terms and not any(_span_has_cta(words, a, b, cta_terms) for _, a, b in kept):
        cs = badtakes.cta_word_spans(words, cta_terms)
        if cs:
            kept.append((len(script_lines), cs[0][0], cs[0][1]))  # CTA sorts last

    # 3 — order by script line, resolve word-index overlaps (earlier line wins).
    kept.sort(key=lambda s: (s[0], s[1]))
    used: list[tuple[int, int]] = []
    ordered: list[tuple[int, int]] = []
    for _, a, b in kept:
        for pa, pb in badtakes._subtract_protected(a, b, used):
            ordered.append((pa, pb))
            used.append((pa, pb))

    # 4 — build ranges per span, concatenate in script order.
    # Inner splits (a same-take pause inside one matched line) keep the default
    # pad; the true OUTER edges of each span are re-padded punctuation/gap-aware
    # so cuts land on phrase boundaries, never clip a word, and only breathe where
    # the speaker actually paused.
    breath = breath_points(transcript.get("segments") or [], words)
    idx = {id(w): k for k, w in enumerate(words)}
    all_ranges: list[list[float]] = []
    kept_words: list[dict] = []
    cta_start: float | None = None  # output-range start of the CTA, for the setup beat
    for a, b in ordered:
        span_words = [w for w in words[a:b + 1]
                      if not (strip_filler and badtakes._norm(w["word"]) in filler_set)]
        if not span_words:
            continue
        sub = ranges_from_words(span_words, duration,
                                max_gap=max_gap, lead_pad=lead_pad, trail_pad=trail_pad)
        first_i = idx[id(span_words[0])]
        last_i = idx[id(span_words[-1])]
        sub[0][0] = start_edge(words, first_i, sil_ends)
        sub[-1][1] = end_edge(words, last_i, duration, breath, sil_starts)
        # Keep each span's edges INSIDE its own words. The words just outside the
        # span belong to a different beat or were dropped, so a silence-snap that
        # reaches past them pulls a stray word onto the seam (e.g. "...Malaysia I"
        # then a hard cut to "I actually lived" = a doubled-"I" stutter). Clamp to
        # the neighbouring word boundaries so the seam never bleeds.
        if first_i > 0:
            sub[0][0] = max(sub[0][0], round(words[first_i - 1]["end"], 3))
        if last_i + 1 < len(words):
            sub[-1][1] = min(sub[-1][1], round(words[last_i + 1]["start"], 3))
        if cta_terms and cta_start is None and _span_has_cta(words, a, b, cta_terms):
            cta_start = sub[0][0]
        all_ranges.extend(sub)
        kept_words.extend(span_words)

    # A continuous take the LLM split across two lines leaves the release of one
    # range overlapping the next; merge those so the shared slice plays once.
    all_ranges = merge_forward_seams(all_ranges)

    # Cadence: give the CTA a setup beat — a held pause before the payoff — unless
    # it opens the video (nothing to pause after) or already follows a real gap.
    pre_gaps = [0.0] * len(all_ranges)
    if cta_start is not None:
        for i, (s, _e) in enumerate(all_ranges):
            if i > 0 and abs(s - cta_start) < 1e-6:
                pre_gaps[i] = cta_beat
                break

    coverage = len(matched_lines) / max(1, len(script_lines))
    kept_duration = sum(e - s for s, e in all_ranges)
    return {
        "ranges": all_ranges,
        "pre_gaps": pre_gaps,
        "kept_words": kept_words,
        "kept_duration": round(kept_duration, 3),
        "removed_duration": round(duration - kept_duration, 3),
        "cuts": max(0, len(all_ranges) - 1),
        "matched": len(matched_lines),
        "total": len(script_lines),
        "coverage": round(coverage, 2),
        "fallback": coverage < min_keep_ratio,
    }


def estimate_cost(usage: dict) -> float:
    return badtakes.estimate_cost(usage)
