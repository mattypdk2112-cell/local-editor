"""Title card + CTA card as an ASS overlay.

What this replaces: a pass that sprayed 2-4 word CAPS fragments across the top of
the frame, timed to the audio ("5 MINUTE RAMBLE" / "40 SECOND REEL"). It read as
a second set of captions, echoing words the real captions already carried, and it
never told the viewer what the thing on screen actually IS.

The shape here comes from the gemini visual pass over 224 reels in the AI-tooling
niche (`reels.written_hook`, `analysis.gemini.on_screen_text`). The accounts that
win there do the same thing every time — NAME THE TOOL, then STATE THE BENEFIT:

    @nick_saraev  671k  "Claude Code Skills"
    @nick_saraev  544k  "NOW SHOWING FABLE 5"
    @nick_saraev  358k  "Claude Design" + "Completely Free" + "No Limits, No Paywalls"
    @nick_saraev  331k  "CLAUDE CODE" + "UNLIMITED" + "COMPLETELY FREE"
    @nick_saraev  781k  "You can now run" + "Completely Free Forever"
    @nateherkai   183k  "Claude Code"
    @nateherkai   247k  "Claude just killed web designers."

Not one of them is a transcript fragment. The card names the product, holds for a
few seconds while the hook lands, then gets out of the way.

The CTA card is the other half: over the closing ask, captions come OFF and a
single card carries it, because two things competing for the eye at the moment
you want an action costs you the action.
"""
from __future__ import annotations

from pathlib import Path

from .fontmetrics import FontMetrics, find_font_file

ASSETS = Path(__file__).resolve().parent.parent / "assets"
CLAUDE_MARK = ASSETS / "claude-mark.png"

# ASS colours are BGR, not RGB.
WHITE = "FFFFFF"
AMBER = "3CB1F5"        # #F5B13C


# The Title/Credit styles render BOLD with Spacing=0.5, but FontMetrics loads the
# REGULAR face and measures bare advances. Laying out against that under-measures
# the text, and a centred title then spills onto the mark on its left.
_ASS_SPACING = 0.5   # matches the Spacing field in the Style lines
_BOLD_WIDEN = 1.04   # bold is a few percent wider than regular; estimate, not measured

def _rendered_width(fm, text: str, size: float) -> float:
    """Width as ASS will actually draw it: bold, with per-character spacing."""
    return fm.text_width(text, size) * _BOLD_WIDEN + _ASS_SPACING * max(0, len(text) - 1)


def _tc(t: float) -> str:
    h, r = divmod(max(0.0, t), 3600)
    m, s = divmod(r, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def _rounded_rect(w: float, h: float, r: float) -> str:
    k = r * 0.5523
    f = lambda v: f"{v:.0f}"  # noqa: E731 - ASS drawing coords are integers
    return (f"m {f(r)} 0 l {f(w-r)} 0 b {f(w-r+k)} 0 {f(w)} {f(r-k)} {f(w)} {f(r)} "
            f"l {f(w)} {f(h-r)} b {f(w)} {f(h-r+k)} {f(w-r+k)} {f(h)} {f(w-r)} {f(h)} "
            f"l {f(r)} {f(h)} b {f(r-k)} {f(h)} 0 {f(h-r+k)} 0 {f(h-r)} "
            f"l 0 {f(r)} b 0 {f(r-k)} {f(r-k)} 0 {f(r)} 0")


def build(
    width: int,
    height: int,
    duration: float,
    *,
    title: str,
    variant: str = "badge",
    strike_at: float = 1.25,
    credit: str | None = None,
    benefit: str | None = None,
    cta_keyword: str | None = None,
    cta_line: str | None = None,
    cta_start: float | None = None,
    font: str = "Helvetica Neue",
    accent: str = AMBER,
    title_out: float = 3.0,
    benefit_in: float = 0.0,
    mark: Path | None = CLAUDE_MARK,
    scale: float = 1.0,
    title_y_pct: float = 11.2,
    cta_y_pct: float = 70.0,
) -> tuple[str, dict | None]:
    """Return (ass_text, mark_placement).

    `mark_placement` is None when there's no logo, else the geometry the caller
    needs to overlay the PNG — ASS can't composite an image, so the mark rides a
    separate ffmpeg `overlay` and needs its own fade/enable window or it outlives
    the text it belongs to.
    """
    fm = FontMetrics(find_font_file(font))
    # Everything is a fraction of frame HEIGHT so the card holds its proportions on
    # any format. `scale` moves title, benefit and mark together — they read as one
    # object, so sizing them independently makes the card look assembled by accident.
    t_size = round(height * 0.0436 * scale)   # 84 on a 1920-tall frame at scale 1
    b_size = round(height * 0.0309 * scale)   # 59
    mark_px = round(height * 0.0478 * scale)  # 92

    top_y = round(height * title_y_pct / 100.0)
    # The accent line is part of the card, not an event in it. Staggering it in
    # made the first second read as something assembling itself on screen; at
    # benefit_in=0 the whole card is simply THERE on frame one. A non-zero
    # benefit_in still fades, for the case where the line is a genuine reveal.
    b_fade = 0 if benefit_in <= 0.01 else 220
    ev: list[str] = []
    mark_x = mark_y = None
    mark_size = mark_px

    def dlg(a, b, style, text, layer=2):
        ev.append(f"Dialogue: {layer},{_tc(a)},{_tc(b)},{style},,0,0,0,,{text}")

    # Line 2 of the `plate` card, declared here because the Style lines below need it
    # too. 0.70 of line 1, picked by eye from a side-by-side still.
    plate_l2 = round(t_size * 0.70)

    if variant == "badge":
        # A pill naming the product. The default, and what A ships.
        tw = _rendered_width(fm, title, t_size)
        gap = round(mark_px * 0.26)
        group = (mark_px + gap + tw) if mark else tw
        x0 = (width - group) / 2.0
        text_cx = x0 + (mark_px + gap if mark else 0) + tw / 2.0

        pad_x, pad_y = round(t_size * 0.55), round(t_size * 0.32)
        plate_w, plate_h = group + pad_x * 2, mark_px + pad_y * 2
        plate_x = (width - plate_w) / 2.0
        text_cy = top_y + plate_h / 2.0

        plate = ("{\\an7\\pos(%.0f,%.0f)\\1c&H1A1512&\\1a&H30&\\bord0\\shad0\\fad(0,260)\\p1}"
                 "%s{\\p0}" % (plate_x, top_y, _rounded_rect(plate_w, plate_h, plate_h / 2)))
        dlg(0, title_out, "Plate", plate, layer=0)
        dlg(0, title_out, "Title",
            "{\\an5\\pos(%.0f,%.0f)\\fad(0,260)}%s" % (text_cx, text_cy, title))
        if benefit:
            by = top_y + plate_h + round(t_size * 0.55)
            dlg(benefit_in, title_out, "Benefit",
                "{\\an8\\pos(%.0f,%.0f)\\fad(%d,260)}%s"
                % (width / 2, by, b_fade, benefit))
        mark_x, mark_y = int(round(x0)), int(round(top_y + pad_y))

    elif variant == "native":
        # Instagram's own "Classic" text look: white, no box, a soft shadow so it holds
        # on a dark room AND a bright screen. title_y_pct here is the BOTTOM of the
        # text block, so it can sit just above whatever is in the shot.
        lines = [x for x in (title, benefit) if x]
        dlg(0, title_out, "Native",
            "{\\an2\\pos(%.0f,%.0f)\\fad(0,260)}%s" % (width / 2, top_y, "\\N".join(lines)))
    elif variant == "plate":
        # Line 1 is BLACK text on an opaque WHITE rounded plate, line 2 is WHITE text on
        # an opaque BLACK plate underneath, smaller and centred under line 1.
        l1_size = t_size
        # Shrink both lines to fit 90% of the frame width, or a long hook on a 9:16
        # frame runs off both edges.
        _w = _rendered_width(fm, title, l1_size) + round(l1_size * 0.60)
        if _w > width * 0.90:
            _k = width * 0.90 / _w
            l1_size = t_size = round(l1_size * _k)   # t_size drives the PlateText style
            plate_l2 = round(plate_l2 * _k)
        l2_size = plate_l2
        # Tight padding is what makes the words fill their plate: cap height is ~70%
        # of the em, so generous padding leaves small-looking text in a big box.
        pad_x1, pad_y1 = round(l1_size * 0.30), round(l1_size * 0.07)
        pad_x2, pad_y2 = round(l2_size * 0.42), round(l2_size * 0.09)

        w1 = _rendered_width(fm, title, l1_size)
        p1_w, p1_h = w1 + pad_x1 * 2, l1_size + pad_y1 * 2
        p1_x = (width - p1_w) / 2.0
        y1 = top_y

        dlg(0, title_out, "PlateW",
            "{\\an7\\pos(%.0f,%.0f)\\1c&HFFFFFF&\\1a&H00&\\bord0\\shad0\\fad(0,260)\\p1}%s{\\p0}"
            % (p1_x, y1, _rounded_rect(p1_w, p1_h, p1_h * 0.22)), layer=0)
        dlg(0, title_out, "PlateText",
            "{\\an5\\pos(%.0f,%.0f)\\fad(0,260)}%s"
            % (p1_x + p1_w / 2.0, y1 + p1_h / 2.0, title), layer=1)

        if benefit:
            w2 = _rendered_width(fm, benefit, l2_size)
            p2_w, p2_h = w2 + pad_x2 * 2, l2_size + pad_y2 * 2
            p2_x = p1_x + (p1_w - p2_w) / 2.0
            y2 = y1 + p1_h + round(l1_size * 0.18)
            dlg(benefit_in, title_out, "PlateB",
                "{\\an7\\pos(%.0f,%.0f)\\1c&H000000&\\1a&H00&\\bord0\\shad0\\fad(%d,260)\\p1}%s{\\p0}"
                % (p2_x, y2, b_fade, _rounded_rect(p2_w, p2_h, p2_h * 0.26)), layer=0)
            dlg(benefit_in, title_out, "PlateBText",
                "{\\an5\\pos(%.0f,%.0f)\\fad(%d,260)}%s"
                % (p2_x + p2_w / 2.0, y2 + p2_h / 2.0, b_fade, benefit), layer=1)

    else:
        # headline / problem: no pill. Two stacked lines carry the claim, and the
        # product name drops to a small credit row underneath — so the frame reads
        # as a statement rather than a badge. Same block, different rhetoric:
        #   headline  the claim lands whole and holds
        #   problem   line 1 is the pain and gets STRUCK, then the fix arrives
        l1_size, l2_size = t_size, round(t_size * 0.78)
        c_size = round(t_size * 0.46)
        mark_size = round(c_size * 1.25)

        y1 = top_y
        y2 = y1 + round(l1_size * 1.20)
        yc = y2 + round(l2_size * 1.36)

        if variant == "problem":
            dlg(0, strike_at, "L1", "{\\an8\\pos(%.0f,%.0f)\\fad(0,0)}%s" % (width / 2, y1, title))
            dlg(strike_at, title_out, "L1",
                "{\\an8\\pos(%.0f,%.0f)\\s1\\alpha&H55&\\fad(90,260)}%s" % (width / 2, y1, title))
            if benefit:
                dlg(benefit_in, title_out, "L2",
                    "{\\an8\\pos(%.0f,%.0f)\\fad(%d,260)}%s"
                    % (width / 2, y2, b_fade, benefit))
        else:
            dlg(0, title_out, "L1", "{\\an8\\pos(%.0f,%.0f)\\fad(0,260)}%s" % (width / 2, y1, title))
            if benefit:
                dlg(benefit_in, title_out, "L2",
                    "{\\an8\\pos(%.0f,%.0f)\\fad(%d,260)}%s"
                    % (width / 2, y2, b_fade, benefit))

        # "" or "none" means DRAW NO CREDIT (it used to fall back to a default line).
        cred = "" if (credit or "").strip().lower() in ("", "none", "off") else credit
        cw = _rendered_width(fm, cred, c_size) if cred else 0
        gap = round(mark_size * 0.30)
        group = (mark_size + gap + cw) if mark else cw
        x0 = (width - group) / 2.0
        if cred:
            dlg(0, title_out, "Credit",
                "{\\an7\\pos(%.0f,%.0f)\\fad(0,260)}%s"
                % (x0 + (mark_size + gap if mark else 0), yc, cred))
        mark_x = int(round(x0))
        mark_y = int(round(yc - (mark_size - c_size) / 2.0))

    if cta_keyword and cta_start is not None:
        cy = height * cta_y_pct / 100.0
        dlg(cta_start, duration, "CTAbig",
            '{\\an5\\pos(%.0f,%.0f)\\fad(160,0)}Comment “%s”'
            % (width / 2, cy, cta_keyword))
        if cta_line:
            # Offset off frame height, NOT off t_size: scaling the title up must not
            # drag the CTA card around with it. They are separate moments.
            dlg(cta_start + 0.12, duration, "CTAsub",
                "{\\an8\\pos(%.0f,%.0f)\\fad(200,0)}%s"
                % (width / 2, cy + round(height * 0.0323), cta_line))

    styles = [
        f"Style: Plate,{font},{t_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,"
        "0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1",
        f"Style: Title,{font},{t_size},&H00{WHITE},&H00{WHITE},&H00000000,&H90000000,"
        "1,0,0,0,100,100,0.5,0,1,0,2,5,0,0,0,1",
        f"Style: Benefit,{font},{b_size},&H00{accent},&H00{accent},&H00000000,&HA0000000,"
        "1,0,0,0,100,100,0.3,0,1,0,3,8,0,0,0,1",
        f"Style: L1,{font},{round(height * 0.0436 * scale)},&H00{WHITE},&H00{WHITE},"
        "&H00000000,&HB0000000,1,0,0,0,100,100,0.5,0,1,0,4,8,0,0,0,1",
        f"Style: L2,{font},{round(height * 0.0340 * scale)},&H00{accent},&H00{accent},"
        "&H00000000,&HB0000000,1,0,0,0,100,100,0.4,0,1,0,4,8,0,0,0,1",
        f"Style: Credit,{font},{round(height * 0.0201 * scale)},&H00D8D8D8,&H00D8D8D8,"
        "&H00000000,&HA0000000,1,0,0,0,100,100,1.2,0,1,0,3,7,0,0,0,1",
        f"Style: PlateW,{font},{t_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,"
        "0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1",
        f"Style: PlateText,{font},{t_size},&H00000000,&H00000000,&H00000000,&H00000000,"
        "1,0,0,0,100,100,0.2,0,1,0,0,5,0,0,0,1",
        f"Style: PlateB,{font},{plate_l2},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,"
        "0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1",
        f"Style: PlateBText,{font},{plate_l2},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,"
        "1,0,0,0,100,100,0.2,0,1,0,0,5,0,0,0,1",
        f"Style: Native,{font},{round(height * 0.0305 * scale)},&H00FFFFFF,&H00FFFFFF,&H00000000,&H90000000,"
        "0,0,0,0,100,100,0,0,1,0,3,2,0,0,0,1",
        f"Style: CTAbig,{font},{round(height * 0.0479)},&H00{WHITE},&H00{WHITE},"
        "&H00000000,&HA0000000,1,0,0,0,100,100,0,0,1,0,5,5,0,0,0,1",
        f"Style: CTAsub,{font},{round(height * 0.024)},&H00{accent},&H00{accent},"
        "&H00000000,&HA0000000,1,0,0,0,100,100,0.3,0,1,0,4,8,0,0,0,1",
    ]
    ass = (f"[Script Info]\nScriptType: v4.00+\nPlayResX: {width}\nPlayResY: {height}\n"
           "WrapStyle: 2\nScaledBorderAndShadow: yes\n\n[V4+ Styles]\n"
           "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
           "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
           "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
           + "\n".join(styles)
           + "\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, "
             "MarginV, Effect, Text\n" + "\n".join(ev) + "\n")

    placement = None
    if mark and Path(mark).exists() and mark_x is not None:
        placement = {"path": str(mark), "size": mark_size,
                     "x": mark_x, "y": mark_y, "out": title_out}
    return ass, placement


def stage_mark(placement: dict, project: Path) -> str:
    """Copy the mark into `project` and return its bare filename.

    ffmpeg's `movie=` splits its options on ":", so any absolute path containing a
    colon is read as a truncated filename plus junk options — and this repo lives
    under `Claude:VSCode`. Referencing the file by name with cwd=project sidesteps
    it entirely, which is the same reason `subtitles=captions.ass` is passed bare.
    """
    import shutil
    dest = project / "mark.png"
    shutil.copyfile(placement["path"], dest)
    return dest.name


def overlay_filter(placement: dict, name: str = "mark.png") -> str:
    """ffmpeg filter fragment that composites the mark and retires it with the text.

    `name` must be relative to the ffmpeg working directory — see stage_mark.
    """
    p = placement
    return (f"movie={name},scale={p['size']}:{p['size']},format=rgba,"
            f"fade=out:st={p['out'] - 0.26:.2f}:d=0.26:alpha=1[mk];"
            f"[vin][mk]overlay={p['x']}:{p['y']}:enable='lt(t,{p['out']:.2f})'")
