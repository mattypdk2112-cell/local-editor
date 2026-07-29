"""Stage 1.25 — the auto-story DRAFTER (the brain that writes the story.txt).

The cutter (`scriptmatch`) can turn any `story.txt` into a tight, in-order cut.
Until now a human wrote that story.txt by hand from the raw ramble. This writes
it automatically: it splits the raw transcript into real spoken SEGMENTS, hands
the numbered segments to a model, and the model returns the segment indices to
keep and the order to play them in. The output story is assembled from those
segments' ACTUAL words, so it is real spoken content by construction, never
invented — which is exactly what the deterministic cutter needs to align to.

Design mirrors badtakes/scriptmatch: the model returns indices, correctness is
enforced in plain code (valid/distinct indices, hook forced first, CTA last). So
the worst a bad answer can do is pick a weak order, never put words in your mouth.

Which model does the thinking is `llm.py`'s problem — by default the Claude Code
already on the machine, so there is no key to set up.
"""
from __future__ import annotations

import re
from pathlib import Path

from . import llm

DEFAULT_OR_MODEL = llm.DEFAULT_OR_MODEL

# Condensed format library (from the ig-reel-script-writer skill). The drafter
# picks whichever fits the raw footage — the niche-specific version comes from
# the voice/winners grounding passed in, not from hard-coded rules.
FORMAT_MENU = """FORMATS to choose from (pick the ONE the raw footage actually supports):
STORY: founder / win / lost / lesson / transformation / challenge / big-goal.
EDUCATIONAL: tutorial / comparison / mythbust / dos-vs-donts / single-tip / before-after / challenge.
NARRATIVE SHAPE: linear · non-linear (open on the result, then how) · in-media-res \
(drop mid-scene) · circular (end where it began, new meaning) · hero's-journey · quest.
FRAMEWORK: Root-Cause Loop (open on the loss/tension, keep conflict live, land the \
lesson LAST, close the loop by echoing a hook word) is the default for any personal \
story; Educational (no arc) for how-to/comparison."""

PROMPT = """You are a world-class short-form video editor cutting a talking-head reel.

Below is a RAW transcript split into numbered SEGMENTS. The creator freestyled: \
false starts, several takes of the same line, throat-clears, tangents, and \
directions to themselves. Your job is to SELECT and REORDER segments into the \
tightest, highest-retention reel possible — using ONLY the segments given.

{format_menu}

RULES (hard):
- Return SEGMENT INDICES ONLY. Never write or paraphrase text — you may only pick \
segments that exist. The words that get spoken are exactly the segments you list.
- HOOK: choose the single strongest opening segment — a surprising line, a loss, or \
live tension the viewer must resolve. NEVER open on a throat-clear, a "so", a setup, \
or a segment whose payoff is guessable. The lesson/point must NOT be in the hook.
- Drop: retakes (keep only the cleanest/last take of a repeated line), tangents, \
filler segments, self-directions, and anything that doesn't move the one story forward.
- ORDER for retention: hook → escalate → land the payoff/lesson LAST.
- Tighter is better. A few strong beats beat many weak ones. Do not keep a segment \
just because it was said.
- If a CTA / comment-keyword segment exists, it goes LAST.
- Write in the creator's register (see VOICE) — but only by CHOOSING segments that \
sound like them, never by rewriting.

Return ONLY JSON in this exact shape:
{{"format": "<the format you chose>", "hook": <segment index>, \
"order": [<segment index>, ...], "cta": <segment index or null>, \
"reason": "<one short line: why this cut>"}}
The "order" array is the full reel start-to-finish and MUST begin with "hook"."""


# Optional. Drop a craft.md in the repo root with your own house rules and it gets
# prepended here; without one the built-in format menu above does the job.
CRAFT_INTRO = (
    "CRAFT REFERENCE — use these principles to judge which single segment is the "
    "strongest hook and how to order the rest for retention. You SELECT existing "
    "segments only; you never write. Ignore anything about authoring/writing NEW "
    "text (hook generators, output formats) — that is not your job.\n\n"
)

def load_craft(project_root) -> str | None:
    """Optional craft brief at <repo>/craft.md — house rules for how a reel should be
    shaped. Absent by default, and the built-in format menu below covers it, so this
    returns None rather than hard-failing. Drop your own file in to steer the drafter."""
    try:
        p = Path(project_root) / "craft.md"
        return p.read_text(encoding="utf-8").strip() if p.exists() else None
    except OSError:
        return None


def resolve_provider(project_root, prefer_model: str = DEFAULT_OR_MODEL) -> dict | None:
    """Who does the thinking. See llm.resolve_backend — Claude Code by default."""
    return llm.resolve_backend(project_root, prefer_model=prefer_model)


def _mk_segment(words: list[dict], idxs: list[int], i: int) -> dict:
    a, b = idxs[0], idxs[-1]
    text = re.sub(r"\s+", " ", " ".join(w["word"] for w in words[a:b + 1])).strip()
    return {"i": i, "a": a, "b": b,
            "start": round(float(words[a]["start"]), 3),
            "end": round(float(words[b]["end"]), 3),
            "text": text}


def segment_transcript(words: list[dict], *, gap: float = 0.5, max_words: int = 18,
                       min_words: int = 3) -> list[dict]:
    """Split words into candidate spoken segments, broken on real pauses (whisper
    punctuation is unreliable) or sentence-final punctuation, capped so a pause-free
    retake-run still splits. Sub-`min_words` fragments are then merged into a
    neighbour so the drafter is only ever offered whole phrases (never "And" / "What"
    on their own). These are the ONLY units the drafter may select from."""
    groups: list[list[int]] = []
    cur: list[int] = []
    for idx, w in enumerate(words):
        if cur:
            prev = cur[-1]
            if (float(w["start"]) - float(words[prev]["end"]) > gap) or len(cur) >= max_words:
                groups.append(cur)
                cur = []
        cur.append(idx)
        if re.search(r"[.?!]$", str(w["word"]).strip()):
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)

    # Coalesce tiny fragments (contiguous word runs) into the previous group, or
    # the next if it's the first — a fragment plus its neighbour is one real phrase.
    merged: list[list[int]] = []
    for g in groups:
        if merged and len(g) < min_words:
            merged[-1].extend(g)
        else:
            merged.append(g)
    if len(merged) >= 2 and len(merged[0]) < min_words:  # first group still tiny
        merged[1] = merged[0] + merged[1]
        merged.pop(0)

    return [_mk_segment(words, g, i) for i, g in enumerate(merged) if g]


def _render_segments(segs: list[dict]) -> str:
    return "\n".join(f"[{s['i']}] {s['text']}" for s in segs)


def _build_prompt(segs, voice, winners, target_seconds, craft=None, brief=None, examples=None) -> str:
    menu = (CRAFT_INTRO + craft.strip()) if craft else FORMAT_MENU
    parts = [PROMPT.format(format_menu=menu)]
    if voice:
        parts.append(f"\nVOICE (write in this register — pick segments that sound like this):\n{voice.strip()[:4000]}")
    if winners:
        parts.append(f"\nWHAT WINS in this niche (shapes the format/hook choice):\n{winners.strip()[:4000]}")
    if examples:
        parts.append(
            "\nAPPROVED CUTS from this creator (their real taste — learn what they keep, "
            f"drop, and how they order; match this judgment):\n{examples.strip()[:4000]}")
    if brief:
        parts.append(
            "\nCREATOR'S DIRECTION (honour this — it overrides the default cut where they "
            f"conflict, but you STILL only select real segments, never write):\n{brief.strip()[:1000]}")
    if target_seconds:
        parts.append(f"\nTARGET LENGTH: about {target_seconds:.0f} seconds — cut to roughly that.")
    parts.append(f"\nSEGMENTS:\n{_render_segments(segs)}")
    return "\n".join(parts)


def draft_story(
    transcript: dict,
    *,
    provider: dict,
    voice: str | None = None,
    winners: str | None = None,
    target_seconds: float | None = None,
    craft: str | None = None,
    brief: str | None = None,
    examples: str | None = None,
) -> dict:
    """Draft a story from the raw transcript using the resolved provider.

    Returns {story_lines, segments, order, format, reason, provider, model, usage}.
    story_lines is the ordered list of REAL spoken beats to write into a story.txt.
    """
    words = transcript.get("words") or []
    segs = segment_transcript(words)
    if not segs:
        return {"story_lines": [], "segments": [], "order": [], "format": "",
                "reason": "no speech", "provider": provider["name"], "model": provider["model"], "usage": {}}

    prompt = _build_prompt(segs, voice, winners, target_seconds, craft=craft, brief=brief, examples=examples)
    parsed, usage = llm.complete_json(provider, prompt, timeout=300.0)

    order = assemble_order(parsed, len(segs))
    return {
        "story_lines": [segs[i]["text"] for i in order],
        "segments": segs,
        "order": order,
        "format": str(parsed.get("format", "")).strip(),
        "reason": str(parsed.get("reason", "")).strip(),
        "provider": provider["name"],
        "model": provider["model"],
        "usage": usage,
    }


def assemble_order(parsed: dict, n_segs: int) -> list[int]:
    """Enforce the selection in code: valid distinct indices, hook forced first,
    CTA forced last. The model can only reorder real segments — it cannot invent."""
    def valid(x) -> bool:
        return isinstance(x, int) and 0 <= x < n_segs

    hook = parsed.get("hook")
    cta = parsed.get("cta")

    order: list[int] = []
    seen: set[int] = set()
    for x in (parsed.get("order") or []):
        if valid(x) and x not in seen:
            order.append(x)
            seen.add(x)

    if valid(hook):
        if hook in seen:
            order.remove(hook)
        order.insert(0, hook)
        seen.add(hook)

    if valid(cta):
        if cta in seen:
            order.remove(cta)
        order.append(cta)
        seen.add(cta)

    return order


def estimate_cost(usage: dict, provider_name: str) -> float:
    """What that draft cost. Priced per backend over in llm.py."""
    return llm.estimate_cost(usage, provider_name)
