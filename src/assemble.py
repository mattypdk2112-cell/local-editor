"""Stage 4 — assemble the final MP4.

One ffmpeg pass: trim + concat the keep-ranges (video and audio), burn the
karaoke captions, mix background music at -23dB, export H.264 MP4. The filter
graph is written to a file so paths with odd characters never hit the shell, and
ffmpeg runs with cwd = project dir so the caption file is a bare relative name.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from .util import run


def _filtergraph(
    ranges: list[list[float]], has_music: bool, has_captions: bool,
    pre_gaps: list[float] | None = None, normalize: bool = True,
    geom: str = "",
) -> str:
    # Short audio fade at each segment edge so the concat seams don't click
    # (a hard splice on a non-zero waveform pops) and the small word-release the
    # cutter keeps at butt-up seams tapers instead of reading as a fragment. 45ms
    # is inaudible as a fade; duration is preserved so A/V + captions stay in sync.
    fade = 0.045
    # Pad rather than trust the caller's length. The loop below indexes pre_gaps[i+1]
    # while guarding on len(ranges), so any producer that hands back fewer gaps than
    # ranges took the whole assemble down with an IndexError. The script-aware cutter
    # does exactly that (30 ranges, 27 gaps), and a missing gap just means "no hold".
    pre_gaps = list(pre_gaps or [])
    if len(pre_gaps) < len(ranges):
        pre_gaps += [0.0] * (len(ranges) - len(pre_gaps))
    lines: list[str] = []
    labels: list[str] = []
    for i, (s, e) in enumerate(ranges):
        # A cadence beat requested BEFORE the next segment (e.g. before the CTA)
        # is rendered as a hold on THIS segment's tail: freeze its last frame and
        # pad silence, so the speaker settles for a beat before the payoff cuts in.
        hold = pre_gaps[i + 1] if i + 1 < len(ranges) else 0.0
        vline = f"[0:v]trim=start={s}:end={e},setpts=PTS-STARTPTS"
        if hold > 0:
            vline += f",tpad=stop_mode=clone:stop_duration={hold:.3f}"
        lines.append(vline + f"[v{i}];")
        fo = max(0.0, (e - s) - fade)
        aline = (
            f"[0:a]atrim=start={s}:end={e},asetpts=PTS-STARTPTS,"
            f"afade=t=in:st=0:d={fade},afade=t=out:st={fo:.3f}:d={fade}"
        )
        if hold > 0:
            aline += f",apad=pad_dur={hold:.3f}"
        lines.append(aline + f"[a{i}];")
        labels.append(f"[v{i}][a{i}]")
    n = len(ranges)
    lines.append(f"{''.join(labels)}concat=n={n}:v=1:a=1[vcat][acat];")

    # Reframe BEFORE the subtitles filter. The .ass is authored against the
    # post-crop frame (see geometry.plan), so burning first and cropping after
    # would shift every caption by the crop ratio.
    pre = f"{geom}," if geom else ""
    if has_captions:
        lines.append(f"[vcat]{pre}subtitles=captions.ass[vout];")
    elif geom:
        lines.append(f"[vcat]{geom}[vout];")
    else:
        lines.append("[vcat]null[vout];")

    # Mix music (if any), then loudness-normalize to a social target (~-14 LUFS)
    # so quiet, distant raw audio comes out at a consistent, punchy level instead
    # of the -40s dB it was shot at. loudnorm preserves duration, so A/V + captions
    # stay in sync. --no-normalize skips it (leave the raw level for CapCut).
    if has_music:
        lines.append("[1:a]volume=-23dB,aresample=async=1[bg];")
        src = "[acat][bg]amix=inputs=2:duration=first:normalize=0"
    else:
        src = "[acat]anull"
    if normalize:
        src += ",loudnorm=I=-14:TP=-1.5:LRA=11"
    lines.append(src + "[aout]")

    return "\n".join(lines) + "\n"


def assemble(
    video: Path,
    project: Path,
    ranges: list[list[float]],
    *,
    music: Path | None = None,
    captions: Path | None = None,
    export_path: Path | None = None,
    pre_gaps: list[float] | None = None,
    normalize: bool = True,
    fast: bool = False,
    geom: str = "",
) -> dict:
    graph_file = project / "filtergraph.txt"
    graph_file.write_text(
        _filtergraph(ranges, music is not None, captions is not None, pre_gaps, normalize,
                     geom=geom)
    )

    out = project / "output.mp4"
    cmd: list[str] = ["ffmpeg", "-y", "-i", str(video)]
    if music is not None:
        cmd += ["-stream_loop", "-1", "-i", str(music)]
    # fast = Apple's hardware encoder (h264_videotoolbox): a 4K clip that takes
    # minutes on the CPU (libx264) drops to ~30s on the media engine. Slightly
    # softer at a given size, but Instagram re-encodes the upload anyway. Default
    # is libx264 quality; reach for --fast when you are iterating on a cut.
    vcodec = (
        ["-c:v", "h264_videotoolbox", "-b:v", "20M"]
        if fast else
        ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"]
    )
    cmd += [
        "-filter_complex_script", "filtergraph.txt",
        "-map", "[vout]", "-map", "[aout]",
        *vcodec,
        "-pix_fmt", "yuv420p", "-profile:v", "high",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(out),
    ]
    # cwd = project so `subtitles=captions.ass` resolves without path escaping.
    run(cmd, cwd=project)

    if export_path is None:
        export_path = Path.home() / "Downloads" / "output-edited.mp4"
    export_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(out, export_path)
    return {"project_out": out, "exported_out": export_path}


def burn(video: Path, captions: Path, *, export_path: Path | None = None,
         fast: bool = False, extra: str | None = None) -> dict:
    """Burn a subtitle file (and optionally more filters) onto a finished cut.

    Used by the caption-the-cut route: the cut has to EXIST before it can be
    transcribed, so captions arrive after assemble() has already run. One extra
    encode, against the short cut rather than the long source — cheaper than
    re-running the trim/concat graph over the whole take.

    `extra` is appended to the filter chain, which is where a title card or a logo
    overlay goes so it shares this single encode instead of adding another.
    """
    out = video.parent / "captioned.mp4"
    chain = f"subtitles={captions.name}"
    if extra:
        # `extra` may open its own labelled branch (a logo overlay needs a second
        # source), so it declares the separator itself rather than assuming a comma.
        chain += extra if extra.lstrip().startswith(("[", ";")) else "," + extra
    vcodec = (["-c:v", "h264_videotoolbox", "-b:v", "20M"] if fast else
              ["-c:v", "libx264", "-preset", "medium", "-crf", "19"])
    run(["ffmpeg", "-y", "-i", str(video), "-vf", chain, *vcodec,
         "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart", str(out)],
        cwd=video.parent)   # cwd so `subtitles=` resolves without path escaping

    if export_path is None:
        export_path = Path.home() / "Downloads" / "output-edited.mp4"
    export_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(out, export_path)
    return {"project_out": out, "exported_out": export_path}
