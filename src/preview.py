"""Render ONE frame of the finished look, so it can be judged before committing
to a full encode.

Cutting is cheap to judge from a log line ("39.56s, zero silences over 0.35s").
The overlay is not — caption size, title placement, whether a card collides with
something in the shot are all things you have to LOOK at. Re-encoding a whole reel
for each of those decisions wastes minutes per round, and the reel this was built
for went through three rounds of nudging one number.
"""
from __future__ import annotations

import subprocess
from pathlib import Path


def frame(video: Path, at: float, out: Path, *, chain: str | None = None,
          cwd: Path | None = None) -> Path:
    """Write a single still from `video` at `at` seconds, with `chain` applied.

    `-ss` goes AFTER `-i` deliberately. Seeking on the INPUT rebases timestamps to
    zero, so libass renders the frame as though it were t=0 — any overlay with a
    fade-in comes out part-way through it, and a title timed to appear later does
    not appear at all. Output seeking costs a decode of the (already short) cut and
    is the only way the still matches what actually ships.
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(video), "-ss", f"{at:.3f}"]
    if chain:
        cmd += ["-filter_complex" if "[" in chain else "-vf", chain]
    cmd += ["-frames:v", "1", str(out)]
    subprocess.run(cmd, cwd=str(cwd) if cwd else None, check=True)
    return out
