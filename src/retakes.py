"""Find the takes whisper hides.

The failure this exists to stop: a reel shipped with the sentence "it reads back
what I said and reorders the sentences" spoken THREE times in a row. Nothing in
the pipeline saw it. `transcript.json` held one clean copy of the line, because
whisper had collapsed all three attempts into a single token:

    431.83-435.59 (3.76s)  'reorders'
    436.07-439.55 (3.48s)  'so'

`roughcut._dedup_adjacent_runs` compares word sequences, and the sequence looked
perfect, so it fired on nothing. The silence carve only removes silence, so it
left the repeated speech in and trimmed the pauses between the takes — which made
it WORSE, not better: three takes back to back with no breath.

Two things cause the collapse, both defaults:

  vad_filter=True                 whisper never sees the pauses between attempts
  condition_on_previous_text=True the decoder actively suppresses a repeat of
                                  text it just emitted

So the detector cannot be smarter pattern-matching over the same transcript. It
has to go back to the audio. A stretched token is the tell: real speech runs
about 0.09s per character, and a word logged at 10x that is not a word, it is a
pause or a swallowed retake wearing a word's timestamp. Those windows get
re-decoded in isolation with both defaults off, which exposes what was said, and
repeated n-grams in the result mark the earlier attempts for removal.
"""
from __future__ import annotations

import re
from pathlib import Path

# Seconds of audio per character of transcribed text. Measured across a 9:37
# talking-head take: median 0.089, p95 0.31. 0.55 is ~6x the median, so it flags
# only tokens that are mostly silence or mostly un-transcribed speech.
SEC_PER_CHAR = 0.55
MIN_SUSPECT = 0.90       # never flag a token shorter than this, whatever the ratio
# The per-char allowance has to be capped or a LONG word hides the most audio:
# 'reorders' is 8 characters, so a pure ratio gives it 4.4s of rope and the 3.76s
# token covering two whole retakes reads as normal. No single word is articulated
# for over ~1.1s, so that is the ceiling regardless of spelling.
MAX_WORD = 1.10


def _norm(word: str) -> str:
    return re.sub(r"[^a-z0-9']", "", word.lower())


def stretched_words(words: list[dict], *, sec_per_char: float = SEC_PER_CHAR,
                    min_suspect: float = MIN_SUSPECT) -> list[dict]:
    """Tokens whose duration is far too long for their text — where takes hide."""
    out = []
    for w in words:
        text = _norm(w["word"])
        if not text:
            continue
        dur = w["end"] - w["start"]
        if dur < min_suspect:
            continue
        expected = min(len(text) * sec_per_char, MAX_WORD)
        if dur > expected:
            out.append({**w, "expected": round(expected, 2), "actual": round(dur, 2)})
    return out


def suspect_windows(words: list[dict], *, pad: float = 1.5,
                    merge_within: float = 1.0) -> list[tuple[float, float]]:
    """Merged [start, end] windows worth re-decoding at higher fidelity.

    `pad` is generous on purpose. At 0.5s the window opened mid-way through the
    first attempt and whisper re-collapsed the repeat it was supposed to expose;
    at 1.5s the same audio decodes as two clearly separate takes. The decoder
    needs to hear a take begin to notice the next one begins again.
    """
    spans = [(w["start"] - pad, w["end"] + pad) for w in stretched_words(words)]
    spans.sort()
    merged: list[list[float]] = []
    for a, b in spans:
        if merged and a - merged[-1][1] <= merge_within:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [(round(max(0.0, a), 3), round(b, 3)) for a, b in merged]


def redecode(wav: Path, windows: list[tuple[float, float]], *,
             model_size: str = "small", language: str = "en",
             vocab: list[str] | None = None) -> list[dict]:
    """Re-transcribe each window in isolation with the collapsing defaults OFF.

    Isolation matters as much as the flags: given the whole file, the decoder
    still carries context across the window boundary and re-suppresses the repeat.
    """
    import subprocess
    import tempfile

    from faster_whisper import WhisperModel

    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    terms = [t.strip() for t in (vocab or []) if t and t.strip()]
    prompt = ("Glossary: " + ", ".join(terms) + ".") if terms else None

    words: list[dict] = []
    with tempfile.TemporaryDirectory() as tmp:
        for i, (a, b) in enumerate(windows):
            clip = Path(tmp) / f"w{i}.wav"
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{a:.3f}",
                            "-to", f"{b:.3f}", "-i", str(wav), str(clip)], check=True)
            segments, _ = model.transcribe(
                str(clip), language=language, word_timestamps=True,
                vad_filter=False, condition_on_previous_text=False,
                beam_size=5, initial_prompt=prompt)
            for seg in segments:
                for w in (seg.words or []):
                    token = w.word.strip()
                    if token:
                        # floats, not numpy scalars — these end up in cut.json
                        words.append({"word": token,
                                      "start": round(float(a + w.start), 3),
                                      "end": round(float(a + w.end), 3),
                                      "prob": round(float(w.probability), 3)})
    return words


def repeated_spans(words: list[dict], *, n: int = 4, gap: float = 3.0
                   ) -> list[list[float]]:
    """Spans covering every attempt at a line EXCEPT the last one.

    Matches on an n-gram of normalised words: when the same opening n-gram starts
    more than once inside `gap` seconds, every attempt but the final one is a
    retake. Keeping the LAST is the right default — a speaker restarts because the
    previous attempt broke, so the final one is the complete thought.
    """
    toks = [(_norm(w["word"]), w) for w in words]
    toks = [(t, w) for t, w in toks if t]
    if len(toks) < n:
        return []

    starts: dict[tuple[str, ...], list[int]] = {}
    for i in range(len(toks) - n + 1):
        key = tuple(t for t, _ in toks[i : i + n])
        starts.setdefault(key, []).append(i)

    spans: list[list[float]] = []
    for key, idxs in starts.items():
        if len(idxs) < 2:
            continue
        # Cluster occurrences that sit close together in time; a phrase legitimately
        # repeated a minute later is a callback, not a retake.
        cluster = [idxs[0]]
        for i in idxs[1:]:
            if toks[i][1]["start"] - toks[cluster[-1]][1]["start"] <= gap * n:
                cluster.append(i)
            else:
                if len(cluster) > 1:
                    spans += _spans_for(cluster, toks)
                cluster = [i]
        if len(cluster) > 1:
            spans += _spans_for(cluster, toks)

    return _merge(spans)


def _spans_for(cluster: list[int], toks) -> list[list[float]]:
    """Every attempt but the last becomes a removable span."""
    out = []
    for a, b in zip(cluster, cluster[1:]):
        out.append([toks[a][1]["start"], toks[b][1]["start"]])
    return out


def _merge(spans: list[list[float]]) -> list[list[float]]:
    if not spans:
        return []
    spans = sorted(spans)
    out = [spans[0][:]]
    for a, b in spans[1:]:
        if a <= out[-1][1] + 0.05:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [[round(float(a), 3), round(float(b), 3)] for a, b in out]


def find(wav: Path, words: list[dict], *, model_size: str = "small",
         vocab: list[str] | None = None) -> dict:
    """Full pass: flag stretched tokens, re-decode them, return retake spans.

    Returns {windows, words, spans, removed} — `spans` drop straight into
    `roughcut.plan_cut(bad_spans=...)`, which is where the LLM bad-take detector's
    output already goes. No API key, no cost, deterministic.
    """
    windows = suspect_windows(words)
    if not windows:
        return {"windows": [], "words": [], "spans": [], "removed": 0.0}
    fine = redecode(wav, windows, model_size=model_size, vocab=vocab)
    spans = repeated_spans(fine)
    return {"windows": windows, "words": fine, "spans": spans,
            "removed": round(sum(b - a for a, b in spans), 3)}
