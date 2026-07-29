"""Where speech ACTUALLY is, measured off the waveform.

`util.detect_silence` answers "is this stretch below -32dB for 50ms", which is
enough to snap a blade into a quiet spot. It is not enough to answer the two
questions that decide whether a cut feels tight:

  1. where does this line really start and end?
  2. is there dead air sitting INSIDE the range I kept?

Whisper cannot answer either. Its word timestamps drift from the real onset (on
one reel "Premiere" was logged at 17.56 and is audible at 18.33) and it happily
stretches a single token across a pause, hiding the pause inside a "word". So a
range planned purely off word times ships with head slop, or clips a consonant
tail, or both.

This module measures an RMS envelope directly and reports speech runs. Ranges
are then snapped to those runs, which is deterministic, needs no model, and is
the difference between a cut that breathes and one that drags.
"""
from __future__ import annotations

import array
import math
import subprocess
from pathlib import Path

# -42dB over a 20ms window. Lower than detect_silence's -32dB because we want the
# quiet onset of a plosive ("P" in Premiere) to read as speech, not as silence.
NOISE_DB = -42.0
HOP = 0.02
MIN_RUN = 0.06          # ignore a blip shorter than this; it's a click, not a word


def envelope(wav: Path, start: float = 0.0, end: float | None = None,
             *, hop: float = HOP) -> list[tuple[float, float]]:
    """[(time, dBFS)] at `hop` resolution over [start, end)."""
    cmd = ["ffmpeg", "-v", "quiet"]
    if start:
        cmd += ["-ss", f"{start:.3f}"]
    if end is not None:
        cmd += ["-to", f"{end:.3f}"]
    cmd += ["-i", str(wav), "-f", "s16le", "-ac", "1", "-ar", "16000", "-"]
    raw = subprocess.run(cmd, capture_output=True).stdout
    samples = array.array("h")
    samples.frombytes(raw[: len(raw) // 2 * 2])

    n = int(16000 * hop)
    out: list[tuple[float, float]] = []
    for i in range(0, max(0, len(samples) - n), n):
        window = samples[i : i + n]
        rms = math.sqrt(sum(s * s for s in window) / len(window)) + 1e-9
        out.append((start + i / 16000.0, 20 * math.log10(rms / 32768.0)))
    return out


def speech_runs(wav: Path, start: float = 0.0, end: float | None = None,
                *, noise_db: float = NOISE_DB, min_run: float = MIN_RUN,
                hop: float = HOP) -> list[tuple[float, float]]:
    """Contiguous spans that are above the noise floor — i.e. someone talking."""
    runs: list[tuple[float, float]] = []
    open_at: float | None = None
    last = start
    for t, db in envelope(wav, start, end, hop=hop):
        loud = db > noise_db
        if loud and open_at is None:
            open_at = t
        elif not loud and open_at is not None:
            if t - open_at >= min_run:
                runs.append((open_at, t))
            open_at = None
        last = t
    if open_at is not None and last - open_at >= min_run:
        runs.append((open_at, last))
    return runs


def _overlapping(runs: list[tuple[float, float]], a: float, b: float):
    return [r for r in runs if r[1] > a and r[0] < b]


def tighten(ranges: list[list[float]], runs: list[tuple[float, float]], *,
            head: float = 0.10, tail: float = 0.16,
            bridge: float = 0.25, max_reach: float = 0.75,
            protect: list[list[float]] | None = None,
            ) -> tuple[list[list[float]], list[dict]]:
    """Snap every range to the speech it actually contains.

    Returns (ranges, report). A range holding no speech at all is DROPPED — on the
    reel this was written for, one 0.57s range contained silence and nothing else
    and had been shipping as a dead beat in the middle of the video.

    `tail` is deliberately larger than `head`: a word's energy dies before its
    articulation does, so a blade placed on the measured offset clips the release.
    An earlier build subtracted a flat 45ms from every range end and cut the tails
    off "DaVinci" and "timeline" — this is the correction for that.
    """
    protect = protect or []
    out: list[list[float]] = []
    report: list[dict] = []
    for a, b in ranges:
        inside = _overlapping(runs, a, b)
        if not inside:
            report.append({"range": [a, b], "action": "dropped", "reason": "no speech"})
            continue
        on, off = inside[0][0], inside[-1][1]

        # Snapping to speech INSIDE the range is not enough on its own: if the range
        # ends part-way through a word, the speech that finishes it lives outside and
        # is never seen. "Claude Code" is three fragments (0.14s / 0.12s / 0.18s) with
        # 0.18s and 0.02s between them, and a range ending on the first one shipped as
        # "made with common editor" — the payoff line, gone. So keep reaching forward
        # while the next run is close enough to be the same phrase.
        reached = off
        i = runs.index(inside[-1])
        while (i + 1 < len(runs)
               and runs[i + 1][0] - reached <= bridge
               and runs[i + 1][1] - off <= max_reach
               and not any(p0 < runs[i + 1][1] and runs[i + 1][0] < p1
                           for p0, p1 in protect)):
            i += 1
            reached = runs[i][1]

        na, nb = round(on - head, 3), round(reached + tail, 3)
        report.append({
            "range": [a, b], "action": "tightened", "new": [na, nb],
            "head_slop": round(on - a, 3),      # >0 = silence we were carrying
            "tail_clip": round(b - off, 3),     # <0 = we were cutting into a word
            "bridged": round(reached - off, 3),  # >0 = a word was finishing past the end
        })
        out.append([na, nb])
    return out, report


def interior_gaps(a: float, b: float, runs: list[tuple[float, float]], *,
                  min_gap: float = 0.34, keep: float = 0.10) -> list[tuple[float, float]]:
    """Dead air INSIDE [a, b] that is safe to remove, with a margin either side.

    Only the silence between two measured speech runs is ever returned, so a cut
    can never land inside a word — which is what made the previous carve drop word
    onsets and, with them, whole captions.
    """
    inside = _overlapping(runs, a, b)
    gaps: list[tuple[float, float]] = []
    for i in range(len(inside) - 1):
        g0, g1 = inside[i][1], inside[i + 1][0]
        # `min_gap` is the pause we're willing to leave alone, and `keep` is breathing
        # room held back either side. Testing the gap against min_gap + 2*keep meant a
        # 0.54s pause survived a 0.34s limit, so the threshold had to be met twice
        # over. The gap itself must exceed min_gap; the margins only have to leave
        # enough behind to be worth a cut.
        if g1 - g0 > min_gap and (g1 - g0) - 2 * keep > 0.10:
            gaps.append((round(g0 + keep, 3), round(g1 - keep, 3)))
    return gaps


def subtract(ranges: list[list[float]], spans: list[list[float]]) -> list[list[float]]:
    """Remove `spans` from `ranges`, splitting a range that a span lands inside.

    Retake spans have to be applied HERE rather than only as `plan_cut(bad_spans=)`,
    because the script-aware path builds its own ranges and never calls plan_cut —
    so spans handed to plan_cut alone were silently ignored on exactly the cuts that
    matter most. Subtracting after the fact works whichever path produced the cut.
    """
    if not spans:
        return ranges
    out: list[list[float]] = []
    for a, b in ranges:
        pieces = [[a, b]]
        for s0, s1 in spans:
            nxt: list[list[float]] = []
            for p0, p1 in pieces:
                if s1 <= p0 or s0 >= p1:        # no overlap
                    nxt.append([p0, p1])
                    continue
                if s0 > p0:
                    nxt.append([p0, min(s0, p1)])
                if s1 < p1:
                    nxt.append([max(s1, p0), p1])
            pieces = nxt
        out += [[round(p0, 3), round(p1, 3)] for p0, p1 in pieces if p1 - p0 > 0.12]
    return out


def carve(a: float, b: float, gaps: list[tuple[float, float]]) -> list[list[float]]:
    """Split [a, b] around `gaps`, dropping slivers too short to be worth a cut."""
    out: list[list[float]] = []
    x = a
    for g0, g1 in gaps:
        g0, g1 = max(g0, a), min(g1, b)
        if g1 <= g0 or g0 <= x:
            continue
        out.append([round(x, 3), round(g0, 3)])
        x = g1
    if b - x > 0.05:
        out.append([round(x, 3), round(b, 3)])
    return [r for r in out if r[1] - r[0] > 0.10]
