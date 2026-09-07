"""Shared helpers: PATH setup, ffmpeg/ffprobe wrappers, video probing."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

# Tooling was installed no-sudo into ~/.local/bin last session (ffmpeg/ffprobe
# 8.1.2, node 22). Make sure that's on PATH for every subprocess we spawn.
LOCAL_BIN = Path.home() / ".local" / "bin"


def _env() -> dict:
    env = os.environ.copy()
    env["PATH"] = f"{LOCAL_BIN}:{env.get('PATH', '')}"
    return env


def run(cmd: list[str], *, cwd: Path | None = None, quiet: bool = False) -> subprocess.CompletedProcess:
    """Run a command with ~/.local/bin on PATH. Raise with stderr on failure."""
    if not quiet:
        print("  $", " ".join(str(c) for c in cmd[:6]), "…" if len(cmd) > 6 else "")
    proc = subprocess.run(
        [str(c) for c in cmd],
        cwd=str(cwd) if cwd else None,
        env=_env(),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-20:])
        raise RuntimeError(f"command failed ({proc.returncode}): {' '.join(map(str, cmd))}\n{tail}")
    return proc


def probe(video: Path) -> dict:
    """Return {duration, width, height, has_audio, fps} for a video file."""
    proc = run(
        [
            "ffprobe", "-v", "error",
            "-print_format", "json",
            "-show_format", "-show_streams",
            str(video),
        ],
        quiet=True,
    )
    data = json.loads(proc.stdout)
    v = next((s for s in data["streams"] if s.get("codec_type") == "video"), None)
    a = next((s for s in data["streams"] if s.get("codec_type") == "audio"), None)
    if v is None:
        raise RuntimeError(f"no video stream in {video}")

    def _fps(stream) -> float:
        raw = stream.get("avg_frame_rate") or stream.get("r_frame_rate") or "30/1"
        num, _, den = raw.partition("/")
        try:
            return float(num) / float(den) if float(den) else 30.0
        except (ValueError, ZeroDivisionError):
            return 30.0

    return {
        "duration": float(data["format"]["duration"]),
        "width": int(v["width"]),
        "height": int(v["height"]),
        "has_audio": a is not None,
        "fps": round(_fps(v), 3),
    }


def detect_silence(wav: Path, *, noise_db: float = -32.0, min_dur: float = 0.05) -> tuple[list[float], list[float]]:
    """Return (silence_starts, silence_ends) from ffmpeg silencedetect, sorted.

    Cuts snap to these real audio boundaries instead of whisper's word timestamps
    (which end fricative/plosive words early), so a word's full release always
    plays and the blade always lands in true silence. Deterministic: the same
    audio yields the same points on every run.
    """
    proc = run(
        ["ffmpeg", "-hide_banner", "-i", str(wav),
         "-af", f"silencedetect=noise={noise_db}dB:d={min_dur}", "-f", "null", "-"],
        quiet=True,
    )
    text = proc.stderr  # silencedetect logs to stderr
    starts = [float(m.group(1)) for m in re.finditer(r"silence_start:\s*([0-9.]+)", text)]
    ends = [float(m.group(1)) for m in re.finditer(r"silence_end:\s*([0-9.]+)", text)]
    return sorted(starts), sorted(ends)


def load_json(path: Path) -> dict:
    return json.loads(path.read_text())


def dump_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=2))


def fmt_secs(s: float) -> str:
    m, sec = divmod(max(0.0, s), 60)
    return f"{int(m)}:{sec:05.2f}"


def next_versioned_path(out_dir: Path, stem: str, *, suffix: str = "edited", ext: str = "mp4") -> Path:
    """Return the next '<stem> <suffix> v<N>.<ext>' in out_dir (N = max existing + 1).

    Never overwrites: each render bumps the version. Anchored regex so 'v10' is not
    read as 'v1', and a gap (v1, v3 present) still returns max+1.
    """
    pat = re.compile(rf"^{re.escape(stem)} {re.escape(suffix)} v(\d+)\.{re.escape(ext)}$")
    max_n = 0
    if out_dir.exists():
        for p in out_dir.iterdir():
            m = pat.match(p.name)
            if m:
                max_n = max(max_n, int(m.group(1)))
    return out_dir / f"{stem} {suffix} v{max_n + 1}.{ext}"


def merge_overlaps(ranges: list[list[float]], *, eps: float = 0.001) -> tuple[list[list[float]], int]:
    """Collapse overlapping/touching ranges. Returns (merged, overlaps_found).

    Concat plays every range in order, so two ranges that overlap in SOURCE time
    play the shared audio twice — the viewer hears "chilling on the couch chilling
    on the couch" from a single clean take. Nothing upstream produced the stutter;
    the tighten pass extended a tail past where the next range already started.

    Merging rather than clipping because an overlap means the two ranges are one
    continuous piece of speech: joining them keeps every word and removes the seam,
    where clipping the earlier tail would re-introduce the clipped word tighten had
    just restored.
    """
    if not ranges:
        return [], 0
    ordered = sorted([[float(a), float(b)] for a, b in ranges], key=lambda r: r[0])
    merged = [ordered[0]]
    overlaps = 0
    for start, end in ordered[1:]:
        prev = merged[-1]
        if start < prev[1] - eps:
            overlaps += 1
            prev[1] = max(prev[1], end)
        elif start <= prev[1] + eps:
            prev[1] = max(prev[1], end)      # touching: join, no double-play
        else:
            merged.append([start, end])
    return [[round(a, 3), round(b, 3)] for a, b in merged], overlaps


def close_intraword_gaps(ranges: list[list[float]], words: list[dict],
                         *, pad: float = 0.02) -> tuple[list[list[float]], int]:
    """Join ranges whose gap falls INSIDE a spoken word. Returns (ranges, joins).

    Cutting mid-word chops a syllable and replays the rest after the join, which is
    heard as a glitch rather than an edit: "describe" spanning 98.16-99.28 with a cut
    at 98.40 and a resume at 98.70 comes out "descr...ibe".

    merge_overlaps cannot see this because the ranges do not overlap — there is a
    real gap, it just happens to land in the middle of a word. Word boundaries are
    the only thing that makes a cut inaudible, so the gap is closed rather than moved.
    """
    if not ranges or not words:
        return ranges, 0
    out = [list(ranges[0])]
    joins = 0
    for start, end in ranges[1:]:
        prev = out[-1]
        gap_a, gap_b = prev[1], start
        inside = any(w["start"] + pad < gap_a and w["end"] - pad > gap_b for w in words)
        if inside:
            prev[1] = max(prev[1], end)
            joins += 1
        else:
            out.append([start, end])
    return [[round(a, 3), round(b, 3)] for a, b in out], joins


def boundary_report(ranges: list[list[float]], words: list[dict],
                    *, min_silence: float = 0.20) -> list[dict]:
    """Flag cuts that land in the middle of continuous speech.

    A cut is inaudible when there is real silence either side of it. A cut with a
    speaker mid-flow on both sides is heard as a glitch, a stutter or a truncated
    word, and it is the single most common defect in a scripted cut — the matcher
    reports "20/20 matched, coverage 1.0" because it found the TEXT, which says
    nothing about whether the resulting EDGE is clean.

    Returns one dict per suspect edge: {kind, at, gap, context}. Empty means every
    boundary sits on silence.
    """
    if not ranges or not words:
        return []
    issues: list[dict] = []

    def silence_before(t: float) -> float:
        prev = [w for w in words if w["end"] <= t + 0.01]
        nxt = [w for w in words if w["start"] >= t - 0.01]
        if not prev or not nxt:
            return 99.0
        return max(0.0, nxt[0]["start"] - prev[-1]["end"])

    def near(t: float, span: float = 1.4) -> str:
        return " ".join(w["word"] for w in words if t - span <= w["start"] <= t + span)

    for i, (a, b) in enumerate(ranges):
        for kind, t in (("start", a), ("end", b)):
            if i == 0 and kind == "start":
                continue
            if i == len(ranges) - 1 and kind == "end":
                continue
            gap = silence_before(t)
            if gap < min_silence:
                issues.append({"kind": kind, "index": i, "at": round(t, 2),
                               "gap": round(gap, 2), "context": near(t)[:70]})
    return issues


def snap_to_pause(ranges: list[list[float]], words: list[dict], *,
                  min_silence: float = 0.20, max_shift: float = 1.20,
                  restore_shift: float = 2.50
                  ) -> tuple[list[list[float]], list[str]]:
    """Move edges that `boundary_report` flags onto real silence. (ranges, notes).

    Detecting a mid-speech cut and only printing a warning about it is not a fix.
    Measured 2026-09-07: the take-selection pass correctly removed "All you have
    to do" but its span ended one word short, the range then opened on the
    orphaned "do", and the render said "To do, you don't have to use Terminal".
    The tool printed "1 cut(s) land mid-speech" and encoded it anyway.

    Both edges are moved INWARD — a start goes later, an end goes earlier — so
    the repair always removes the fragment rather than restoring more of the take
    around it. `max_shift` stops it eating a real line when a whole passage was
    said without a breath.
    """
    if not ranges or not words:
        return [list(r) for r in ranges], []

    starts = sorted(w["start"] for w in words)
    ends = sorted(w["end"] for w in words)

    def pause_after(t: float) -> float | None:
        """First word start at/after `t` that opens a run (real quiet before it)."""
        for s in starts:
            if s < t - 0.01:
                continue
            prev = [e for e in ends if e <= s + 0.01]
            if not prev or s - prev[-1] >= min_silence:
                return s
        return None

    def run_start_before(t: float) -> float | None:
        """Start of the continuous run that `t` falls inside.

        Not "the previous pause": the previous word END followed by silence can be
        seconds away (7.85s on the take this was built for), while the run the
        blade is sitting inside began 0.88s back. Restoring to the run start is
        what gives the sentence back whole.
        """
        best = None
        for s in starts:
            if s > t + 0.01:
                break
            prev = [e for e in ends if e <= s + 0.01]
            if not prev or s - prev[-1] >= min_silence:
                best = s
        return best

    def pause_before(t: float) -> float | None:
        """Last word end at/before `t` that closes a run (real quiet after it)."""
        for e in reversed(ends):
            if e > t + 0.01:
                continue
            nxt = [s for s in starts if s >= e - 0.01]
            if not nxt or nxt[0] - e >= min_silence:
                return e
        return None

    def run_end_after(t: float) -> float | None:
        """End of the continuous run that `t` falls inside."""
        for e in ends:
            if e < t - 0.01:
                continue
            nxt = [s for s in starts if s >= e - 0.01]
            if not nxt or nxt[0] - e >= min_silence:
                return e
        return None

    out = [list(r) for r in ranges]
    notes: list[str] = []
    for iss in boundary_report(ranges, words, min_silence=min_silence):
        i = iss.get("index")
        if i is None:
            continue
        a, b = out[i]
        if iss["kind"] == "start":
            t = pause_after(a)
            if t is not None and 0 < t - a <= max_shift and t < b - 0.20:
                notes.append(f"snapped range {i} start {a:.2f} → {t:.2f} "
                             f"(+{t - a:.2f}s) off mid-speech")
                out[i][0] = round(t, 3)
                continue
            # No pause ahead within reach: the speaker ran the whole thing
            # together, so there is no blade here at all and cutting into it can
            # only leave a fragment. Give the words back instead. Measured
            # 2026-09-07: "All you have to do you don't have to use terminal" is
            # one continuous breath, the take-selection pass called the first half
            # an abandoned false start, and the render said "To do, you don't have
            # to use Terminal". Restored, it reads as the sentence he actually
            # said. If restoring really does re-expose a duplicate take, the
            # repeat check on the assembled audio removes it downstream.
            t = run_start_before(a)
            if t is not None and 0 < a - t <= restore_shift:
                notes.append(f"no blade at range {i} start {a:.2f} — restored to "
                             f"{t:.2f} (+{a - t:.2f}s) rather than leave a fragment")
                out[i][0] = round(t, 3)
        else:
            t = pause_before(b)
            if t is not None and 0 < b - t <= max_shift and t > a + 0.20:
                notes.append(f"snapped range {i} end {b:.2f} → {t:.2f} "
                             f"(-{b - t:.2f}s) off mid-speech")
                out[i][1] = round(t, 3)
                continue
            t = run_end_after(b)
            if t is not None and 0 < t - b <= restore_shift:
                notes.append(f"no blade at range {i} end {b:.2f} — restored to "
                             f"{t:.2f} (+{t - b:.2f}s) rather than leave a fragment")
                out[i][1] = round(t, 3)
    # Restoring can push a range into its neighbour; merge any overlap.
    out.sort()
    merged: list[list[float]] = []
    for a, b in out:
        if merged and a <= merged[-1][1] + 0.01:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return merged, notes
