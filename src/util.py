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
