"""Stage 2 — rough cut from the transcript.

The core "cut tight" value: trim leading/trailing dead air and collapse long
silences between phrases. Optional filler-word removal (um/uh) drops those words
from the captions and, when they sit in a gap, from the cut too.

Output = a list of keep-ranges in ORIGINAL video time. Downstream stages remap
word timestamps onto the concatenated (post-cut) timeline via `build_timemap`.
"""
from __future__ import annotations

import difflib
import re

# Conservative default: only unambiguous non-words. "like"/"you know" are real
# words, so they stay unless --strip-filler-aggressive.
FILLER = {"um", "uh", "uhh", "umm", "uhm", "erm", "mm", "hmm", "mhm", "eh", "ah"}
FILLER_AGGRESSIVE = FILLER | {"like", "basically", "literally", "actually", "yknow"}


def _norm(word: str) -> str:
    return re.sub(r"[^a-z]", "", word.lower())


def _in_spans(t: float, spans: list[list[float]]) -> bool:
    return any(s <= t <= e for s, e in spans)


# --- punctuation/gap-aware seam padding (shared by every cut mode) -----------
# Fixed lead/trail padding slices a fragment off whichever word butts up against
# a cut — a clipped consonant, or a chopped word release. These place each cut on
# a phrase boundary instead: never cross into a neighbouring word at the start,
# keep a small tapered release at the end, and only breathe at a real sentence end.

_SENT_END = (".", "!", "?")


def breath_points(segments: list[dict], words: list[dict]) -> set[int]:
    """Word indices that END a sentence/clause — a natural place for a breath.

    Whisper's per-word punctuation is unreliable, but its SEGMENT boundaries track
    sentence/clause ends well, so the last word of each segment counts, plus any
    word whose token actually carries terminal punctuation.
    """
    ends: set[int] = set()
    for i, w in enumerate(words):
        if w["word"].rstrip().endswith(_SENT_END):
            ends.add(i)
    for s in segments or []:
        se = s.get("end")
        if se is None:
            continue
        best, bd = None, 1e9
        for i, w in enumerate(words):
            d = abs(w["end"] - se)
            if d < bd:
                best, bd = i, d
        if best is not None and bd < 0.15:
            ends.add(best)
    return ends


# How far past whisper's word-end we'll reach to a real silence point. Covers a
# fricative/plosive release (~0.1–0.25s) but not a long dropped pause.
_SILENCE_REACH = 0.30


def start_edge(words: list[dict], i: int, sil_ends: list[float] | None = None,
               bad_spans: list[list[float]] | None = None) -> float:
    """Left edge for a kept run starting at word i. Prefer the real point the
    audio picks up (end of the silence just before the word) so we open on speech,
    not a dead frame; else cap the lead to the silence before the word so we never
    cross into the previous (dropped) word.

    `bad_spans` (removed retakes/false-starts) set a hard floor: when a discarded
    take butts up right before this word, the "silence just before the word" lives
    INSIDE that dropped take, so snapping there would open on its trailing breath
    (the glitch). We never open earlier than the end of a removed take that ends
    within reach of the word."""
    ws = words[i]["start"]
    # Hard floor: don't open inside a removed take that ends just before this word.
    floor = 0.0
    for bs, be in (bad_spans or []):
        if ws - _SILENCE_REACH <= be <= ws + 0.02:
            floor = max(floor, be)
    if sil_ends:
        q = None
        for e in sil_ends:
            if e <= ws + 0.03:
                q = e
            else:
                break
        if q is not None and q >= ws - _SILENCE_REACH and q >= floor - 0.01:
            return round(max(0.0, q - 0.02), 3)
    prev_end = words[i - 1]["end"] if i > 0 else 0.0
    gap = ws - prev_end
    lead = min(0.03 if gap >= 0.18 else 0.02, max(0.0, gap))
    return round(max(floor, ws - lead, 0.0), 3)


def end_edge(words: list[dict], i: int, duration: float, breath: set[int],
             sil_starts: list[float] | None = None,
             bad_spans: list[list[float]] | None = None) -> float:
    """Right edge for a kept run ending at word i. Prefer the real point the audio
    goes quiet (whisper ends fricatives early, so its word-end clips the release);
    the blade then lands in true silence with the word intact. Fall back to a
    guessed trail only when no silence is near (a butt-up mid-take cut).

    `bad_spans` (removed retakes) unlock a bigger release: when a discarded take
    butts up right after this word (the speaker ran straight into a re-take, no
    pause — so there's no silence to snap to and the default 0.06s trail clips the
    word's fricative), we can safely reach into that dropped audio to let the final
    word finish. Capped so we take the release, not the next word's vowel."""
    we = words[i]["end"]
    for bs, be in (bad_spans or []):
        if bs <= we + 0.05 and be > we + 0.02:
            # cutting into discarded audio anyway → give the word its release
            return round(min(we + 0.12, be, duration), 3)
    if sil_starts:
        q = next((s for s in sil_starts if s >= we - 0.02), None)
        if q is not None and q <= we + _SILENCE_REACH:
            return round(min(q + 0.02, duration), 3)
    nxt = words[i + 1]["start"] if i + 1 < len(words) else duration
    gap = nxt - we
    if gap >= 0.04:
        sentence = i in breath or gap >= 0.18
        trail = min(0.07 if sentence else 0.025, gap)
    else:
        trail = 0.06  # butts up → keep the release, faded at the seam
    return round(min(we + trail, duration), 3)


# Words that, standing alone between two cuts, are just dangling connective tissue.
_CONNECTIVE = {"and", "so", "but", "or", "the", "a", "i", "it", "that", "this",
               "then", "yeah", "ok", "okay", "um", "uh", "like"}


def _run_tokens(run: list[dict]) -> list[str]:
    return [t for t in (_norm(w["word"]) for w in run) if t]


def _dedup_adjacent_runs(runs: list[list[dict]], thresh: float = 0.7) -> list[list[dict]]:
    """Drop a re-take that repeats the run right before it — keep the LATER take
    (usually the complete/cleaner one). Catches duplicate openings the LLM misses."""
    out: list[list[dict]] = []
    for run in runs:
        if out:
            r = difflib.SequenceMatcher(None, _run_tokens(out[-1]), _run_tokens(run)).ratio()
            if r >= thresh:
                out[-1] = run  # same take said twice → keep the later one
                continue
        out.append(run)
    return out


# Connectives a take trails off on ("...Claude code and") — trim from a run's END.
# Kept narrow: "so"/"and" are fine as run OPENERS, only dangling at the end.
# "the"/"a"/"i" added: when a take runs the last line straight into the next
# sentence with no pause, only that next sentence's first word survives (the rest
# is cut as silence), leaving a dangling "...over Messi. I" before a hard cut.
# These three are ~never real spoken sentence-enders, so trimming them is safe.
_TRAILING_DROP = {"and", "so", "but", "or", "um", "uh", "like", "the", "a", "i"}


def _trim_trailing_connectives(runs: list[list[dict]]) -> list[list[dict]]:
    """Drop a dangling connective a take trailed off on, so a run ends on a real
    word (and the cut lands after a clean phrase, not '...and')."""
    out: list[list[dict]] = []
    for run in runs:
        run = list(run)
        while len(run) > 1 and _norm(run[-1]["word"]) in _TRAILING_DROP:
            run.pop()
        if run:
            out.append(run)
    return out


def _drop_orphan_runs(runs: list[list[dict]]) -> list[list[dict]]:
    """Drop a run that is just one or two connective words stranded between cuts
    (a dangling 'and' / 'so') — it reads as a jerky double-cut, never as content."""
    out: list[list[dict]] = []
    for run in runs:
        toks = _run_tokens(run)
        if toks and len(toks) <= 2 and all(t in _CONNECTIVE for t in toks):
            continue
        out.append(run)
    return out


def merge_forward_seams(ranges: list[list[float]]) -> list[list[float]]:
    """Collapse a seam that would replay footage: when the next range starts
    inside the previous one AND still moves forward in the source (a continuous
    take split across cuts), merge them so the shared slice plays once. A range
    that jumps backward is a deliberate reorder and keeps its hard cut."""
    out: list[list[float]] = []
    for s, e in ranges:
        if out and out[-1][0] <= s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return out


def plan_cut(
    words: list[dict],
    duration: float,
    *,
    max_gap: float = 0.2,
    lead_pad: float = 0.05,
    trail_pad: float = 0.06,
    strip_filler: bool = False,
    aggressive_filler: bool = False,
    bad_spans: list[list[float]] | None = None,
    segments: list[dict] | None = None,
    sil_starts: list[float] | None = None,
    sil_ends: list[float] | None = None,
) -> dict:
    """Return {ranges, kept_words, kept_duration, removed_duration, cuts}.

    `bad_spans` are time-ranges flagged as retakes/false-starts/directions: any
    word whose midpoint falls inside one is dropped, and the silence logic then
    cuts the gap it leaves (a whole bad take is well over `max_gap`). `segments`
    (whisper's) feed punctuation-aware seam padding so cuts land on phrase
    boundaries and never clip the word that butts up against a dropped span.
    """
    filler_set = FILLER_AGGRESSIVE if aggressive_filler else FILLER
    bad_spans = bad_spans or []

    kept: list[dict] = []
    for w in words:
        if strip_filler and _norm(w["word"]) in filler_set:
            continue
        if _in_spans((w["start"] + w["end"]) / 2, bad_spans):
            continue
        kept.append(w)

    if not kept:
        # No speech detected → keep the whole clip, make no cuts.
        return {
            "ranges": [[0.0, round(duration, 3)]],
            "kept_words": [],
            "kept_duration": round(duration, 3),
            "removed_duration": 0.0,
            "cuts": 0,
        }

    # Group the kept words into continuous runs (split at any silence > max_gap),
    # then pad each run's OUTER edges punctuation/gap-aware against the FULL word
    # list — so a dropped word butting up against a cut is tapered, not clipped.
    breath = breath_points(segments or [], words)
    idx = {id(w): k for k, w in enumerate(words)}
    runs: list[list[dict]] = []
    run: list[dict] = [kept[0]]
    for w in kept[1:]:
        if w["start"] - run[-1]["end"] > max_gap:
            runs.append(run)
            run = [w]
        else:
            run.append(w)
    runs.append(run)

    # Deterministic cleanup of what the silence/bad-take pass leaves behind:
    # a repeated take (duplicate opening), a dangling trailing connective, and
    # connective-only islands.
    runs = _dedup_adjacent_runs(runs)
    runs = _trim_trailing_connectives(runs)
    runs = _drop_orphan_runs(runs)

    kept = [w for run in runs for w in run]  # reflect the drops in the caption words
    merged = merge_forward_seams([
        [start_edge(words, idx[id(run[0])], sil_ends, bad_spans),
         end_edge(words, idx[id(run[-1])], duration, breath, sil_starts, bad_spans)]
        for run in runs
    ])
    kept_duration = sum(e - s for s, e in merged)
    return {
        "ranges": merged,
        "kept_words": kept,
        "kept_duration": round(kept_duration, 3),
        "removed_duration": round(duration - kept_duration, 3),
        "cuts": len(merged) - 1,
    }


def ranges_from_words(
    words: list[dict],
    duration: float,
    *,
    max_gap: float = 0.2,
    lead_pad: float = 0.05,
    trail_pad: float = 0.06,
) -> list[list[float]]:
    """Merged keep-ranges for a CHRONOLOGICAL word list: open at the first word,
    cut whenever the gap to the next exceeds max_gap, pad each edge, merge overlaps.

    Assumes words are in time order (one continuous take). Script mode calls this
    per matched span, then concatenates the results in script order.
    """
    if not words:
        return []
    ranges: list[list[float]] = []
    start = max(0.0, words[0]["start"] - lead_pad)
    prev_end = words[0]["end"]
    for w in words[1:]:
        if w["start"] - prev_end > max_gap:
            ranges.append([round(start, 3), round(prev_end + trail_pad, 3)])
            start = max(0.0, w["start"] - lead_pad)
        prev_end = max(prev_end, w["end"])
    ranges.append([round(start, 3), round(min(prev_end + trail_pad, duration), 3)])

    merged: list[list[float]] = []
    for r in ranges:
        if merged and r[0] <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], r[1])
        else:
            merged.append(r)
    return merged


def build_timemap(ranges: list[list[float]], pre_gaps: list[float] | None = None):
    """Return a function mapping ORIGINAL time -> NEW (post-concat) time.

    `pre_gaps[k]` is silence deliberately INSERTED before range k (a cadence beat,
    e.g. before the CTA); it pushes that range and everything after it later so the
    burned captions stay in sync with the rendered hold. Returns None for a time
    that falls inside a removed gap.
    """
    pre_gaps = pre_gaps or [0.0] * len(ranges)
    offsets = []  # (orig_start, orig_end, new_start)
    acc = 0.0
    for k, (s, e) in enumerate(ranges):
        acc += pre_gaps[k]
        offsets.append((s, e, acc))
        acc += e - s

    def remap(t: float):
        for s, e, new_start in offsets:
            if s <= t <= e:
                return round(new_start + (t - s), 3)
        return None

    return remap


def drop_orphan_ranges(ranges: list[list[float]], words: list[dict], *,
                       max_dur: float = 0.9) -> tuple[list[list[float]], list[dict]]:
    """Drop a kept range that holds nothing but one or two connective words.

    `_drop_orphan_runs` above does this for runs, but only inside `plan_cut` — and
    the script-aware path never calls plan_cut, so on a scripted cut a stray "and"
    survived as its own 0.56s range. On screen that is a jerky double-cut, and in
    the captions it read as "and and this", which then needed a hand correction.
    Working on RANGES instead of runs means it applies whichever path built them.
    """
    kept: list[list[float]] = []
    dropped: list[dict] = []
    for a, b in ranges:
        toks = [_norm(w["word"]) for w in words
                if a - 0.02 <= (w["start"] + w["end"]) / 2 <= b + 0.02]
        toks = [t for t in toks if t]
        if toks and len(toks) <= 2 and (b - a) <= max_dur and all(t in _CONNECTIVE for t in toks):
            dropped.append({"range": [a, b], "words": toks})
            continue
        kept.append([a, b])
    return kept, dropped
