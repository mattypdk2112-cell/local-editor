"""Reframe the export: crop to a target aspect, then scale to a target height.

The editor used to ship whatever the camera shot. A 4K 16:9 phone take went out as
4K 16:9, so a reel that wanted to be 4:3 got cropped by hand afterwards and the
burned-in captions were positioned for the wrong frame.

Order matters and it is the whole reason this lives in one place: the crop and
scale run BEFORE `subtitles=`, and `plan()` hands back the post-scale dimensions so
the .ass is authored against the frame it will actually land on. Build the captions
against the source size and every margin is off by the crop ratio.
"""
from __future__ import annotations


def _even(n: float) -> int:
    """h264 with yuv420p needs even dimensions, and an odd one fails at encode time."""
    return max(2, int(round(n / 2.0)) * 2)


def parse_aspect(spec: str) -> float:
    """'4:3' -> 1.3333. Also accepts '4x3' and a bare float."""
    s = str(spec).strip().lower().replace("x", ":")
    if ":" in s:
        w, _, h = s.partition(":")
        wf, hf = float(w), float(h)
        if wf <= 0 or hf <= 0:
            raise ValueError(f"aspect must be positive, got {spec!r}")
        return wf / hf
    v = float(s)
    if v <= 0:
        raise ValueError(f"aspect must be positive, got {spec!r}")
    return v


def plan(src_w: int, src_h: int, *, aspect: str | None = None,
         crop_x: int | None = None, out_height: int | None = None) -> dict:
    """Return {chain, width, height} for the reframe.

    `chain` is an ffmpeg filter fragment (no leading/trailing comma), empty when
    nothing needs doing. `width`/`height` are the dimensions the captions must be
    authored against.

    `crop_x` is an offset in SOURCE pixels from the left edge; None centres it. It
    is clamped into range rather than rejected, because "push me right" is a nudge
    the caller makes by eye and an off-by-a-few-pixels ask should not be an error.
    """
    cw, ch = int(src_w), int(src_h)
    parts: list[str] = []

    if aspect:
        target = parse_aspect(aspect)
        if src_w / src_h > target:
            cw, ch = _even(src_h * target), _even(src_h)      # letterbox-free: trim width
        else:
            cw, ch = _even(src_w), _even(src_w / target)      # trim height
        cw, ch = min(cw, src_w), min(ch, src_h)
        x = (src_w - cw) // 2 if crop_x is None else int(crop_x)
        x = max(0, min(x, src_w - cw))
        y = (src_h - ch) // 2
        parts.append(f"crop={cw}:{ch}:{x}:{y}")
    elif crop_x is not None:
        raise ValueError("--crop-x needs --aspect; there is nothing to offset without a crop")

    ow, oh = cw, ch
    if out_height and out_height < ch:
        oh = _even(out_height)
        ow = _even(cw * (oh / ch))
        parts.append(f"scale={ow}:{oh}")

    return {"chain": ",".join(parts), "width": ow, "height": oh}
