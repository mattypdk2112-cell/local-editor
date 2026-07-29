"""Minimal TrueType metrics reader — just enough to lay captions out ourselves.

The caption highlight box has to hug the LETTERS (cap height + a small pad, rounded
corners), which ASS's BorderStyle=3 can't do: it boxes the whole line box, ascender
and descender included, square-cornered. Measured against a real client reel that's
54px of vertical padding where the reference has 19.

So the box is drawn as an ASS vector instead, which means we need each word's width
and the font's cap height ourselves. Only `head`, `OS/2`, `hhea`, `hmtx` and `cmap`
(format 4/12) are parsed — no font library is installed on the render machine and
this avoids adding one.

libass sizes a face by its WINDOWS metrics: an ASS Fontsize of N renders the
usWinAscent+usWinDescent span at N pixels, NOT the em square. Verified against a
rendered frame — Montserrat Black at Fontsize 100 measures a 46px cap height, and
capHeight / (winAscent + winDescent) * 100 predicts 46.6.
"""
from __future__ import annotations

import struct
from pathlib import Path

FONT_DIRS = [
    Path.home() / "Library/Fonts",
    Path("/Library/Fonts"),
    Path("/System/Library/Fonts"),
    Path("/System/Library/Fonts/Supplemental"),
]


def _tables(data: bytes) -> dict[str, tuple[int, int]]:
    if data[:4] == b"ttcf":
        off = struct.unpack(">I", data[12:16])[0]
    else:
        off = 0
    num = struct.unpack(">H", data[off + 4:off + 6])[0]
    out = {}
    for i in range(num):
        e = off + 12 + 16 * i
        tag = data[e:e + 4].decode("latin1")
        start, length = struct.unpack(">II", data[e + 8:e + 16])
        out[tag] = (start, length)
    return out


def _family_names(data: bytes, tabs: dict) -> set[str]:
    if "name" not in tabs:
        return set()
    off = tabs["name"][0]
    _, count, so = struct.unpack(">HHH", data[off:off + 6])
    names = set()
    for i in range(count):
        r = off + 6 + 12 * i
        pid, _eid, _lid, nid, ln, o = struct.unpack(">HHHHHH", data[r:r + 12])
        if nid not in (1, 4, 16):
            continue
        raw = data[off + so + o:off + so + o + ln]
        try:
            names.add((raw.decode("utf-16-be") if pid == 3 else raw.decode("latin1")).strip())
        except Exception:  # noqa: BLE001 - a malformed name record just isn't a match
            continue
    return names


def find_font_file(family: str) -> Path | None:
    """First font file whose family/full name matches, case-insensitively."""
    want = family.strip().lower()
    for d in FONT_DIRS:
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*")):
            if p.suffix.lower() not in (".ttf", ".otf", ".ttc"):
                continue
            try:
                data = p.read_bytes()
                if {n.lower() for n in _family_names(data, _tables(data))} & {want}:
                    return p
            except Exception:  # noqa: BLE001 - unreadable/exotic font, keep looking
                continue
    return None


class FontMetrics:
    def __init__(self, path: Path):
        data = path.read_bytes()
        t = _tables(data)
        ho = t["head"][0]
        self.upem = struct.unpack(">H", data[ho + 18:ho + 20])[0] or 1000

        oo = t["OS/2"][0]
        self.win_ascent, self.win_descent = struct.unpack(">HH", data[oo + 74:oo + 78])
        version = struct.unpack(">H", data[oo:oo + 2])[0]
        # sCapHeight only exists from OS/2 v2; older faces get the usual approximation.
        cap = struct.unpack(">h", data[oo + 88:oo + 90])[0] if version >= 2 else 0
        self.cap_height = cap if cap > 0 else round(self.upem * 0.70)

        eo = t["hhea"][0]
        num_h = struct.unpack(">H", data[eo + 34:eo + 36])[0]
        mo = t["hmtx"][0]
        self.advances = [struct.unpack(">H", data[mo + 4 * i:mo + 4 * i + 2])[0] for i in range(num_h)]

        self.cmap = self._cmap(data, t["cmap"][0])

    @staticmethod
    def _cmap(data: bytes, off: int) -> dict[int, int]:
        n = struct.unpack(">H", data[off + 2:off + 4])[0]
        best = None
        for i in range(n):
            pid, eid, sub = struct.unpack(">HHI", data[off + 4 + 8 * i:off + 12 + 8 * i])
            fmt = struct.unpack(">H", data[off + sub:off + sub + 2])[0]
            if fmt in (4, 12) and (pid, eid) in ((3, 1), (3, 10), (0, 3), (0, 4), (0, 6)):
                # Prefer full-range format 12 when a face offers both.
                if best is None or fmt == 12:
                    best = (off + sub, fmt)
        if not best:
            return {}
        base, fmt = best
        m: dict[int, int] = {}
        if fmt == 12:
            ngroups = struct.unpack(">I", data[base + 12:base + 16])[0]
            for g in range(ngroups):
                s, e, gid = struct.unpack(">III", data[base + 16 + 12 * g:base + 28 + 12 * g])
                for c in range(s, min(e, s + 1000) + 1):
                    m[c] = gid + (c - s)
            return m
        seg2 = struct.unpack(">H", data[base + 6:base + 8])[0]
        seg = seg2 // 2
        ends = struct.unpack(f">{seg}H", data[base + 14:base + 14 + seg2])
        so = base + 16 + seg2
        starts = struct.unpack(f">{seg}H", data[so:so + seg2])
        do = so + seg2
        deltas = struct.unpack(f">{seg}h", data[do:do + seg2])
        ro = do + seg2
        ranges = struct.unpack(f">{seg}H", data[ro:ro + seg2])
        for i in range(seg):
            for c in range(starts[i], min(ends[i], 0xFFFF) + 1):
                if ranges[i] == 0:
                    g = (c + deltas[i]) & 0xFFFF
                else:
                    gi = ro + 2 * i + ranges[i] + 2 * (c - starts[i])
                    if gi + 2 > len(data):
                        continue
                    g = struct.unpack(">H", data[gi:gi + 2])[0]
                    if g:
                        g = (g + deltas[i]) & 0xFFFF
                if g:
                    m[c] = g
        return m

    def advance(self, ch: str) -> int:
        g = self.cmap.get(ord(ch))
        if g is None:
            g = self.cmap.get(ord("?"), 0)
        return self.advances[g] if g < len(self.advances) else self.advances[-1]

    # --- the two numbers the caption layout actually needs -------------------
    def px_per_unit(self, ass_fontsize: float) -> float:
        """libass sizes by usWinAscent+usWinDescent, not the em square."""
        span = self.win_ascent + self.win_descent
        return ass_fontsize / span if span else ass_fontsize / self.upem

    def text_width(self, text: str, ass_fontsize: float) -> float:
        ppu = self.px_per_unit(ass_fontsize)
        return sum(self.advance(c) for c in text) * ppu
