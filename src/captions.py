"""Stage 3 — animated captions (word-level karaoke, ASS/libass).

Words are remapped onto the post-cut timeline, grouped into short phrases, and
written as ASS Dialogue events with per-word `\\k` karaoke tags so each word
lights up in the accent colour as it's spoken. Short-form reels style: big,
bold, centred lower-third, heavy outline.
"""
from __future__ import annotations

import re

from .roughcut import build_timemap
from .fontmetrics import FontMetrics, find_font_file

# Highlight-box geometry, as multiples of CAP HEIGHT. Measured off a client's own
# reel (box 277x82 around a 43px-cap word on a 1080x1920 frame): pad_x/cap = .35,
# pad_y/cap = .44, corner radius ~= .20.
BOX_PAD_X = 0.22
BOX_PAD_Y = 0.43
BOX_RADIUS = 0.26


def _rounded_rect(w: float, h: float, r: float) -> str:
    """ASS vector path for a rounded rectangle anchored at its top-left."""
    r = max(0.0, min(r, w / 2, h / 2))
    k = r * 0.5523  # circular bezier handle
    f = lambda v: f"{v:.0f}"  # noqa: E731 - ASS drawing coords are integers
    return (
        f"m {f(r)} 0 "
        f"l {f(w - r)} 0 "
        f"b {f(w - r + k)} 0 {f(w)} {f(r - k)} {f(w)} {f(r)} "
        f"l {f(w)} {f(h - r)} "
        f"b {f(w)} {f(h - r + k)} {f(w - r + k)} {f(h)} {f(w - r)} {f(h)} "
        f"l {f(r)} {f(h)} "
        f"b {f(r - k)} {f(h)} 0 {f(h - r + k)} 0 {f(h - r)} "
        f"l 0 {f(r)} "
        f"b 0 {f(r - k)} {f(r - k)} 0 {f(r)} 0"
    )


def _hex_to_ass(hex_rgb: str, alpha: str = "00") -> str:
    h = hex_rgb.lstrip("#")
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{alpha}{b}{g}{r}".upper()


def _ass_time(t: float) -> str:
    cs = int(round(max(0.0, t) * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _clean(word: str, uppercase: bool, strip_punct: bool = False) -> str:
    word = word.replace("{", "").replace("}", "").replace("\\", "")
    if strip_punct:
        # Trailing/leading sentence punctuation only — an apostrophe inside a word
        # ("don't", "it's") has to survive or the caption reads as a typo.
        word = re.sub(r"^[\"'“”‘’(\[]+|[.,!?;:\"'“”‘’)\]]+$", "", word)
    return word.upper() if uppercase else word


def _group_lines(words: list[dict], words_per_line: int, phrase_gap: float,
                 max_line_dur: float | None = None,
                 break_at: list[float] | None = None):
    """Split words (with new_start/new_end) into short caption lines.

    `max_line_dur` breaks a line early once it has been on screen that long, so the
    line length tracks DELIVERY rather than a fixed count: fast phrases fill the
    3-word cap, a slow or emphasised word lands on its own. That's the 1-3 word
    variation a hand-editor produces, without randomness.

    `break_at` are cut boundaries on the output timeline. A caption line must never
    straddle a hard cut: the removed silence takes the pause that would have ended
    the sentence with it, so the sentence-end test can't see the join.
    """
    cuts = sorted(break_at or [])
    lines: list[list[dict]] = []
    cur: list[dict] = []
    for w in words:
        if cur:
            gap = w["new_start"] - cur[-1]["new_end"]
            ends_sentence = bool(re.search(r"[.!?]$", cur[-1]["word"]))
            too_long = (max_line_dur is not None
                        and w["new_end"] - cur[0]["new_start"] > max_line_dur)
            # A word whose timestamp is stretched ACROSS the join still belongs to the
            # new line, so the test reaches to the end of that word, not its start.
            spans_cut = any(cur[-1]["new_end"] - 0.10 <= c <= w["new_end"] for c in cuts)
            if (len(cur) >= words_per_line or gap > phrase_gap or ends_sentence
                    or too_long or spans_cut):
                lines.append(cur)
                cur = []
        cur.append(w)
    if cur:
        lines.append(cur)
    return lines


def build_ass(
    words: list[dict],
    ranges: list[list[float]],
    width: int,
    height: int,
    *,
    accent: str = "FFFFFF",
    color: str = "FFFFFF",
    highlight_on: bool = True,
    font: str = "Arial",
    uppercase: bool = True,
    strip_punct: bool = False,
    words_per_line: int = 3,
    max_line_dur: float | None = None,
    break_at: list[float] | None = None,
    phrase_gap: float = 0.5,
    pre_gaps: list[float] | None = None,
    size: float = 10.0,
    stroke: float = 30.0,
    shadow_pct: float = 30.0,
    bg: str | None = None,
    y_pct: float = 16.0,
) -> str:
    """`size`/`stroke`/`shadow_pct` are CapCut-style dials, not pixels — a client reads
    their style off CapCut, so the numbers they hand us have to mean something here.
    They're calibrated so the previous hard-coded defaults (5.2% of frame height, a
    0.09em outline, a 0.03em shadow) are exactly size=10 / stroke=30 / shadow=30.

    `bg` (RRGGBB) switches on a CapCut-style highlight BOX behind the spoken word —
    Andrew's style, and what Gemini reads off his own reels ("green box highlight
    around captions", "white captions with green highlights for keywords"). ASS has
    no per-word box tag, so it's drawn as two layers: layer 0 renders the line with
    an opaque-box border whose colour is switched on per word (transparent on the
    rest) and its glyph fill hidden, layer 1 draws the stroked text on top. Both
    layers share font/size/margins, so the glyphs land identically.
    """
    remap = build_timemap(ranges, pre_gaps)

    # Remap each kept word onto the new timeline; drop only words that fall
    # inside a removed gap. faster-whisper sometimes emits zero-duration words
    # (e.g. "most people" spoken with no gap) — clamp those to a minimum visible
    # span instead of dropping them, or the word vanishes from the captions.
    mapped: list[dict] = []
    for w in words:
        ns = remap(w["start"])
        if ns is None:
            continue
        ne = remap(w["end"])
        if ne is None:
            ne = ns
        ne = max(ne, ns + 0.08)
        mapped.append({"word": w["word"], "new_start": ns, "new_end": ne})

    # CapCut dials -> pixels, anchored to frame HEIGHT. Calibrated so the rendered cap
    # height is 2.24% of frame height — measured off a real client reel with
    # tools/caption_proportions.py. Anchoring to min(width,height) instead made landscape
    # captions 1.8x oversized, because on a wide frame the short edge IS the height.
    fontsize = max(18, round(height * 0.0486 * (max(1.0, size) / 10.0)))
    # Stroke calibrated against a real CapCut export at dial 60, whose outline measures
    # 1.079% of frame width. Proportion, not pixels — that is the only thing that
    # transfers between a screenshot, a reel and a landscape render.
    outline = max(1, round(fontsize * 0.00207 * max(0.0, stroke)))
    shadow = max(0, round(fontsize * 0.001 * max(0.0, shadow_pct)))
    # The box has to clear the stroke or it's invisible: a heavy outline (stroke 60 =
    # .18em) is wider than a fixed .12em pad, so the black stroke ate the highlight.
    # Pad = stroke + a visible margin, which is the green frame you see on Andrew's reels.
    box_pad = outline + max(4, round(fontsize * 0.09))
    # Vertical placement, as a % of frame height from the bottom to the caption
    # baseline. 16 = the lower third we've always used; a client whose own reels sit
    # mid-frame (Andrew's measure 46%) sets it per-org.
    margin_v = round(height * max(2.0, min(90.0, y_pct)) / 100.0)
    margin_h = round(width * 0.06)

    # A Black/Heavy face already carries the weight; asking libass to synthesise bold
    # on top of it smears the letterforms.
    bold = 0 if re.search(r"\b(black|heavy|extrabold|ultra)\b", font, re.I) else 1

    if bg:
        # Box style: the word colour lives in the box, so the text stays flat.
        primary = secondary = _hex_to_ass(color)
    elif highlight_on:
        primary = _hex_to_ass(accent)          # spoken word pops to the highlight colour
        secondary = _hex_to_ass(color, "55")   # upcoming words: dimmed base colour
    else:
        primary = secondary = _hex_to_ass(color)  # static — no karaoke pop
    black = _hex_to_ass("000000")

    fmt = ("Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
           "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
           "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding")
    styles = [
        f"Style: Pop,{font},{fontsize},{primary},{secondary},{black},{black},{bold},0,0,0,"
        f"100,100,0,0,1,{outline},{shadow},2,{margin_h},{margin_h},{margin_v},1"
    ]
    # Drawing the highlight ourselves needs the font's own metrics. If the face can't be
    # found on this machine we fall back to BorderStyle=3 — a square box padded off the
    # whole line box. Looser and un-rounded, but never a missing highlight.
    metrics = None
    if bg:
        try:
            path = find_font_file(font)
            metrics = FontMetrics(path) if path else None
        except Exception:  # noqa: BLE001 - an unparseable face just means the fallback box
            metrics = None

    if bg and metrics is None:
        # BorderStyle 3 turns OutlineColour into an opaque box behind the glyphs; the
        # per-word colour/alpha is then switched inline with \3c/\3a. Shadow 0 so the
        # box doesn't cast its own — the text layer owns the shadow.
        styles.append(
            f"Style: Box,{font},{fontsize},{_hex_to_ass(color)},{_hex_to_ass(color)},"
            f"{_hex_to_ass(bg)},{black},{bold},0,0,0,100,100,0,0,3,{box_pad},0,2,"
            f"{margin_h},{margin_h},{margin_v},1"
        )
    elif bg:
        # Vector box: no border/shadow of its own, filled with the highlight colour.
        styles.append(
            f"Style: Box,{font},{fontsize},{_hex_to_ass(bg)},{_hex_to_ass(bg)},"
            f"{_hex_to_ass(bg)},{black},0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1"
        )

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
{fmt}
{chr(10).join(styles)}

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    lines = _group_lines(mapped, words_per_line, phrase_gap, max_line_dur, break_at)
    events: list[str] = []
    for i, line in enumerate(lines):
        start = line[0]["new_start"]
        # Clamp end to the next line's start so captions never stack.
        end = line[-1]["new_end"] + 0.25
        if i + 1 < len(lines):
            end = min(end, lines[i + 1][0]["new_start"])
        if end <= start:
            end = start + 0.20

        shown = [_clean(w["word"], uppercase, strip_punct) for w in line]
        if not any(s for s in shown):
            continue

        if bg and metrics is not None:
            # We place every word ourselves so the box and the glyphs come from the SAME
            # measurement and can never drift apart.
            ppu = metrics.px_per_unit(fontsize)
            cap_px = metrics.cap_height * ppu
            space_w = metrics.advance(" ") * ppu
            widths = [metrics.text_width(s, fontsize) for s in shown]
            total = sum(widths) + space_w * (len(shown) - 1)
            pen = (width - total) / 2.0
            # \an5 centres a word's LINE box on \pos, so back out where the baseline lands.
            baseline = height - margin_v
            cy = baseline + fontsize / 2.0 - metrics.win_ascent * ppu
            pad_x, pad_y = BOX_PAD_X * cap_px, BOX_PAD_Y * cap_px
            radius = BOX_RADIUS * cap_px

            # A CapCut shadow is BLURRED; ASS's built-in Shadow is a hard offset copy of
            # the glyph, which is why ours read as a harder edge than the reference. Cast
            # it ourselves instead: a blurred black copy on its own layer under the text,
            # with the style's own shadow suppressed (\shad0).
            blur = max(0.0, shadow_pct / 12.0)
            for j, w in enumerate(line):
                cx = pen + widths[j] / 2.0
                if shadow > 0:
                    events.append(
                        f"Dialogue: 1,{_ass_time(start)},{_ass_time(end)},Pop,,0,0,0,,"
                        f"{{\\an5\\pos({cx + shadow:.0f},{cy + shadow:.0f})\\1c&H000000&\\3c&H000000&"
                        f"\\shad0\\blur{blur:.1f}\\1a&H40&\\3a&H40&\\fad(70,70)}}{shown[j]}"
                    )
                events.append(
                    f"Dialogue: 2,{_ass_time(start)},{_ass_time(end)},Pop,,0,0,0,,"
                    f"{{\\an5\\pos({cx:.0f},{cy:.0f})\\shad0\\fad(70,70)}}{shown[j]}"
                )
                w_start = w["new_start"] if j else start
                w_end = line[j + 1]["new_start"] if j + 1 < len(line) else end
                if w_end > w_start:
                    bx = cx - widths[j] / 2.0 - pad_x
                    by = baseline - cap_px - pad_y
                    bw = widths[j] + 2 * pad_x
                    bh = cap_px + 2 * pad_y
                    events.append(
                        f"Dialogue: 0,{_ass_time(w_start)},{_ass_time(w_end)},Box,,0,0,0,,"
                        f"{{\\an7\\pos({bx:.0f},{by:.0f})\\p1}}{_rounded_rect(bw, bh, radius)}{{\\p0}}"
                    )
                pen += widths[j] + space_w
            continue

        if bg:
            # Layer 1: the words, held for the whole line (the box carries the timing).
            events.append(
                f"Dialogue: 1,{_ass_time(start)},{_ass_time(end)},Pop,,0,0,0,,"
                + "{\\fad(70,70)}" + " ".join(shown)
            )
            # Layer 0: one event per word, the whole line re-rendered with the box
            # opaque on that word only and the glyph fill hidden (\1a) so the text
            # layer above is what you actually read.
            for j, w in enumerate(line):
                w_start = w["new_start"] if j else start
                w_end = line[j + 1]["new_start"] if j + 1 < len(line) else end
                if w_end <= w_start:
                    continue
                parts = [
                    ("{\\3a&H00&}" if k == j else "{\\3a&HFF&}") + word
                    for k, word in enumerate(shown)
                ]
                events.append(
                    f"Dialogue: 0,{_ass_time(w_start)},{_ass_time(w_end)},Box,,0,0,0,,"
                    + "{\\1a&HFF&\\4a&HFF&}" + " ".join(parts)
                )
            continue

        parts = []
        for j, w in enumerate(line):
            nxt = line[j + 1]["new_start"] if j + 1 < len(line) else w["new_end"]
            k_cs = max(1, round((nxt - w["new_start"]) * 100))
            parts.append(f"{{\\k{k_cs}}}{shown[j]}")
        text = "{\\fad(70,70)}" + " ".join(parts)
        events.append(
            f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Pop,,0,0,0,,{text}"
        )

    return header + "\n".join(events) + "\n"
