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

  3. and, added 2026-08-28, IS THIS ACTUALLY SILENCE OR IS IT JUST BREATH?

Not every dead-feeling stretch is silent. On a loud take the breaths, the
trailing "and...", and the drawn-out sentence endings all sit well above a fixed
noise floor, so they score as speech and no gap-finder will ever touch them. The
gate has to be measured against how loud the speaker actually is, not set as a
constant, and it has to be measured on the CUT audio, not the raw take. See
`breath_gaps` and BREATH_OFFSET_DB below for the numbers and the take behind them.
"""
from __future__ import annotations

import array
import math
import subprocess
from pathlib import Path

# -42dB over a 20ms window. Lower than detect_silence's -32dB because we want the
# quiet onset of a plosive ("P" in Premiere) to read as speech, not as silence.
# Still the default for `speech_runs`, which feeds `tighten` and must not treat a
# quiet onset as silence. Breath removal is a separate later pass, see `breath_gaps`.
NOISE_DB = -42.0
HOP = 0.02
MIN_RUN = 0.06          # ignore a blip shorter than this; it's a click, not a word

# --- the breath-level finding (2026-08-28, Matt's "fifty bucks" take) --------
# A FIXED dBFS floor cannot answer "is this dead air" because dead air is only
# dead RELATIVE to how loud the person is talking. On that take the speech level
# measured -19.3 dBFS, so every breath, every trailing "and...", and every drawn
# sentence ending sat 12dB ABOVE the -42dB floor and was scored as speech. The
# result: `interior_gaps` found nothing to cut, and 13 soft stretches totalling
# 4.92s survived into the render — 4 of them inside the first 20 seconds, which
# is exactly where a viewer feels drag. Matt's note, verbatim: "sometimes it's
# not true silence, sometimes it's just breath level".
#
# What actually worked, measured by hand on that file: a threshold at 30% of the
# speech AMPLITUDE, i.e. speech_level_dB - 10.5dB, which came out at -29.9 dBFS.
# Cutting the middle of each run with an 80ms pad either side removed 4.60s and
# left every consonant intact (verified by re-transcribing: no clipped words).
#
# So the threshold travels with the recording rather than being a constant.
#
# WHERE THIS HAS TO RUN, and the two ways I got it wrong first:
#
#   1. Changing the gate that feeds `speech_runs` does NOT work. `tighten` reaches
#      outward from every range to restore clipped tails, so a tighter gate just
#      gets handed back: at -37dB the render came out 67.0s against the 62.3s the
#      manual pass produced. The gate decides where blades LAND; it cannot decide
#      what survives inside a range.
#   2. Measuring the level on the whole SOURCE does not work either, because the
#      source is ~70% silence, so p60 lands in the noise band (-45.0 dBFS on this
#      take) instead of on speech. p60 is only meaningful once the silence is gone.
#
# So this runs LAST, over the ranges already chosen, and measures the level of the
# KEPT audio only — which is speech-dominated, so p60 lands on speech (-19.3 dBFS
# on the same take). That is the signal the 30%-of-amplitude figure was derived
# from and the only one it is valid against.
BREATH_OFFSET_DB = -10.5    # 30% of speech amplitude
BREATH_PAD = 0.08           # left either side so no consonant is ever clipped
BREATH_MIN = 0.25           # a shorter dip is a stop consonant, not a breath
WORD_MARGIN = 0.12          # whisper's word times drift; never cut this close to one


def speech_level_db(wav: Path, ranges: list[list[float]] | None = None,
                    *, hop: float = HOP) -> float:
    """p60 of the RMS envelope, over `ranges` only when given.

    p60 rather than the mean (dragged down by pauses) or the max (one plosive).
    Pass the kept ranges: on a raw take p60 is the room, not the voice.
    """
    env = [db for t, db in envelope(wav, hop=hop)
           if ranges is None or any(a <= t < b for a, b in ranges)]
    if not env:
        return NOISE_DB
    env.sort()
    return env[int(len(env) * 0.60)]


def breath_gaps(wav: Path, ranges: list[list[float]], *,
                words: list[dict] | None = None,
                offset: float = BREATH_OFFSET_DB, pad: float = BREATH_PAD,
                min_dur: float = BREATH_MIN, hop: float = HOP,
                word_margin: float = WORD_MARGIN
                ) -> tuple[list[list[float]], float]:
    """Breath-level dead air inside `ranges`. Returns (spans_to_remove, gate_db).

    Not silence. Breath, the trailing "and...", the drawn-out sentence ending: all
    of it sits above any fixed floor on a loud take and is scored as speech, which
    is how 4.92s of drag survived a clean-looking cut — 4 of those stretches inside
    the first 20 seconds, where a viewer feels it most.

    Each span is trimmed by `pad` either side so the blade never reaches a
    consonant.

    EXPERIMENTAL, off by default. Read this before switching it on.

    An energy gate cannot tell breath from speech that happens to be quiet. On the
    take this was built for it ate "and so that 50 bucks that I" — a real line,
    mumbled, sitting under the gate. Caught only by diffing the render against a
    control run with the pass disabled, which is the check to run every time.

    The obvious guard does not work either. Passing `words` and refusing any span
    that overlaps one sounds right, and it removes NOTHING: whisper stretches a
    single token across a pause (see this module's header), so its word spans
    already swallow the breaths. That run came out 74.1s against 68.6s unguarded
    and 62.3s for the hand pass. Both settings were measured on the same take.

    So: unguarded it can eat a quiet word, guarded it is inert. What actually
    shipped Matt's reel was running this over the RENDERED audio rather than the
    raw take — the render is loud enough that the mumbled line stays above the
    gate — then verifying by re-transcribing. Two encodes, but it is checkable.
    Separating breath from voiced speech properly needs a pitch or
    spectral-flatness test, which is a real build and is not here yet.
    """
    gate = speech_level_db(wav, ranges, hop=hop) + offset
    spans: list[list[float]] = []
    for a, b in ranges:
        run_start = None
        prev_t = a
        for t, db in envelope(wav, a, b, hop=hop):
            if db < gate:
                if run_start is None:
                    run_start = t
            else:
                if run_start is not None and prev_t - run_start >= min_dur:
                    spans.append([run_start, prev_t])
                run_start = None
            prev_t = t
        if run_start is not None and prev_t - run_start >= min_dur:
            spans.append([run_start, prev_t])
    keep = [[s + pad, e - pad] for s, e in spans if (e - s) - 2 * pad > 0.06]
    if words:
        keep = [g for g in keep
                if not any(w["start"] - word_margin < g[1] and w["end"] + word_margin > g[0]
                           for w in words)]
    return keep, gate


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
    """Contiguous spans that are above the noise floor — i.e. someone talking.

    Deliberately still a FIXED floor: this feeds `tighten`, which needs to see the
    quiet onset of a plosive as speech so it does not clip it. Breath removal is a
    separate, later pass — see `breath_gaps`.
    """
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


def _bisected(words: list[dict], t: float) -> dict | None:
    """The word, if any, that time `t` falls strictly inside."""
    for w in words:
        if w["start"] < t < w["end"]:
            return w
        if w["start"] > t:
            break
    return None


def tighten(ranges: list[list[float]], runs: list[tuple[float, float]], *,
            head: float = 0.10, tail: float = 0.16,
            bridge: float = 0.25, max_reach: float = 0.75,
            protect: list[list[float]] | None = None,
            words: list[dict] | None = None, word_grace: float = 0.35,
            ) -> tuple[list[list[float]], list[dict]]:
    """Snap every range to the speech it actually contains.

    Returns (ranges, report). A range holding no speech at all is DROPPED — on the
    reel this was written for, one 0.57s range contained silence and nothing else
    and had been shipping as a dead beat in the middle of the video.

    `tail` is deliberately larger than `head`: a word's energy dies before its
    articulation does, so a blade placed on the measured offset clips the release.
    An earlier build subtracted a flat 45ms from every range end and cut the tails
    off "DaVinci" and "timeline" — this is the correction for that.

    `words` (whisper's word times) is a second opinion used ONLY to finish a word the
    measured edge cuts through: a final consonant carries so little energy that the
    run can stop mid-articulation, and on Andrew's card video the blade landed 0.04s
    inside "market" and shipped "TCG mar-". Whisper's times drift, which is why the
    audio is measured in the first place, so the reach is capped at `word_grace` and
    can never cross into the neighbouring measured run.
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

        # Finish a word the measured edge cuts through, bounded by word_grace and by
        # the neighbouring runs so a drifting whisper time can never drag in the next
        # word's onset or the previous word's tail.
        word_fix = {}
        if words:
            wb = _bisected(words, nb)
            if wb is not None and wb["end"] > nb:
                limit = nb + word_grace
                nxt = next((r[0] for r in runs if r[0] >= reached - 1e-9), None)
                if nxt is not None:
                    limit = min(limit, nxt - 0.02)
                cand = round(min(wb["end"] + tail, limit), 3)
                if cand > nb:
                    word_fix["tail_word"] = [round(nb, 3), cand]
                    nb = cand
            wa = _bisected(words, na)
            if wa is not None and wa["start"] < na:
                limit = na - word_grace
                prev = next((r[1] for r in reversed(runs) if r[1] <= on + 1e-9), None)
                if prev is not None:
                    limit = max(limit, prev + 0.02)
                cand = round(max(wa["start"] - head, limit), 3)
                if cand < na:
                    word_fix["head_word"] = [round(na, 3), cand]
                    na = cand

        entry = {
            "range": [a, b], "action": "tightened", "new": [na, nb],
            "head_slop": round(on - a, 3),      # >0 = silence we were carrying
            "tail_clip": round(b - off, 3),     # <0 = we were cutting into a word
            "bridged": round(reached - off, 3),  # >0 = a word was finishing past the end
            **word_fix,                          # present = a bisected word was finished
        }
        # Both edges move independently, so two neighbours can end up owning the same
        # audio: a forward bridge that finishes a word runs past the next range's
        # start, while that range's own head snap pulls back before this end. Rendered,
        # the shared slice plays TWICE — a stutter mid-sentence, which is what shipped
        # on Andrew's card video ([133.68, 135.22] then [133.68, 136.92]). Neighbours
        # that overlap in source order are continuous speech by construction, so they
        # become one range. A deliberately REORDERED cut (the Drafter's) lands before
        # the previous range's start and is left alone: there the overlap is a seam
        # between two different beats, not a duplicated word.
        if out and out[-1][0] <= na < out[-1][1]:
            entry["action"] = "merged"
            entry["merged_into"] = list(out[-1])
            out[-1][1] = max(out[-1][1], nb)
        else:
            out.append([na, nb])
        report.append(entry)
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


# --- voicing (2026-09-07) ---------------------------------------------------
# `breath_gaps` above is a pure energy gate, and its own docstring says why that
# is not enough: "an energy gate cannot tell breath from speech that happens to
# be quiet", it ate a real mumbled line, and the proper answer "needs a pitch or
# spectral-flatness test, which is a real build and is not here yet". This is
# that build.
#
# Breath, room tone and the tail of an "s" are UNVOICED: no glottal pulse, so the
# waveform has no periodicity and its spectrum is close to flat. Voiced speech —
# every vowel, and therefore every mumbled word worth keeping — is periodic at
# the speaker's pitch. So instead of asking "is this quiet", ask "is anything in
# here periodic". A quiet vowel answers yes and survives; a loud breath answers
# no and goes.
#
# Measured on Matt's 2026-09-06 editor take. The energy gate alone flagged 9.76s
# across 19 dips in a 53.5s cut; the same file cut by hand came in at 43.0s with
# only 1.32s left under the gate, so the drag was real and the gate had found it.
# Of the 18 spans that survived padding, the voicing test cut 16 (all scoring
# 0.00) and SPARED 2: one on a range edge, and one at 31.6s scoring 1.00, which
# is a drawn-out voiced "music" that the energy gate would have bladed. That
# second one is the whole point — it is the same failure the old docstring
# recorded ("it ate 'and so that 50 bucks that I'"), caught automatically this
# time instead of by diffing two renders by hand.
#
# The first version of this test had NO pre-emphasis and scored room tone at
# 1.00 voiced, sparing 3.70s of pure silence: low-frequency rumble autocorrelates
# happily. Check that before trusting any number this function returns.
VOICED_MAX = 0.20       # a span may be cut only if under this fraction is voiced
EDGE_KEEP = 0.15        # never blade this close to a range edge; those belong to `tighten`
VOICED_CORR = 0.40      # normalised autocorrelation peak that counts as periodic
PITCH_LO_HZ = 70.0      # bottom of a male speaking range
PITCH_HI_HZ = 350.0     # top of a female speaking range


def voiced_fraction(wav: Path, start: float, end: float, *,
                    win: float = 0.04, corr_min: float = VOICED_CORR) -> float:
    """Fraction of `win`-sized windows in [start, end) that are periodic.

    Normalised autocorrelation over the lag band that corresponds to a human
    speaking pitch. Loud breath is aperiodic and scores ~0; a mumbled vowel is
    periodic and scores high even at 25dB below the speech level, which is
    exactly the case the energy gate gets wrong.
    """
    import numpy as np

    cmd = ["ffmpeg", "-v", "quiet", "-ss", f"{start:.3f}", "-to", f"{end:.3f}",
           "-i", str(wav), "-f", "s16le", "-ac", "1", "-ar", "16000", "-"]
    raw = subprocess.run(cmd, capture_output=True).stdout
    x = np.frombuffer(raw[: len(raw) // 2 * 2], dtype="<i2").astype(np.float64)
    # Pre-emphasis. Room tone, aircon and desk rumble are dominated by energy
    # under ~60Hz, and that rumble is periodic enough to autocorrelate: without
    # this filter the first measurement scored ROOM TONE at voiced=1.00 and the
    # test spared 3.70s of pure silence. A one-tap high-pass flattens it and it
    # scores 0.00, while a vowel's harmonics survive untouched.
    x = np.append(x[:1], x[1:] - 0.97 * x[:-1])

    n = int(16000 * win)
    if n < 8 or len(x) < n:
        return 0.0

    lo, hi = int(16000 / PITCH_HI_HZ), int(16000 / PITCH_LO_HZ)
    voiced = total = 0
    for i in range(0, len(x) - n + 1, n):
        w = x[i : i + n]
        w = w - w.mean()
        e0 = float(w @ w)
        total += 1
        if e0 < 1e-6:
            continue                      # true digital silence: not voiced
        ac = np.correlate(w, w, mode="full")[n - 1:]
        band = ac[lo : min(hi, len(ac))]
        if band.size and float(band.max()) / e0 >= corr_min:
            voiced += 1
    return voiced / total if total else 0.0


def unvoiced_gaps(wav: Path, ranges: list[list[float]], *,
                  offset: float = BREATH_OFFSET_DB, pad: float = BREATH_PAD,
                  min_dur: float = BREATH_MIN, hop: float = HOP,
                  voiced_max: float = VOICED_MAX, edge_keep: float = EDGE_KEEP
                  ) -> tuple[list[list[float]], float, list[list[float]]]:
    """Breath-level dead air that is also aperiodic. (cut, gate_db, kept_voiced).

    Two gates in series. The energy gate proposes; the voicing test disposes. A
    span the energy gate flagged but which turns out to be `voiced_max` or more
    periodic is a quiet WORD, and it is returned in the third element rather than
    cut, so a caller can print what it declined to touch.
    """
    proposed, gate = breath_gaps(wav, ranges, offset=offset, pad=pad,
                                 min_dur=min_dur, hop=hop)
    cut, spared = [], []
    for a, b in proposed:
        # A span touching a range edge is head/tail slop, which `tighten` already
        # owns and measures against the speech runs. Blading it here instead cost
        # the first three words of a render ("there's a free" vanished, caught by
        # the re-transcribe diff), because whisper's onset for the opening word
        # sits later than the audio actually starts.
        if any(a < ra + edge_keep or b > rb - edge_keep for ra, rb in ranges
               if a >= ra - 0.001 and b <= rb + 0.001):
            spared.append([a, b])
            continue
        (spared if voiced_fraction(wav, a, b) >= voiced_max else cut).append([a, b])
    return cut, gate, spared
