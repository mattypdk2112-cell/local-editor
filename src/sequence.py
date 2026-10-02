"""Build a whole reel from a recipe: join clips, then add text, sound and an end card.

  sections  pieces (talking clips + b-roll, in/out, gain, optional squeeze) -> "<name> sections.mp4"
            + "<name> cuts.json" (where every piece starts on the timeline)
  finish    the joined body -> text stamps, your sound effects, your music, end card,
            optional captions -> "<name> vN.mp4"

Anchors. Every time in the finish half can be written against the timeline instead of raw
seconds, so re-trimming a piece moves its text and sounds with it:
  12.5              seconds
  "walk"            start of the piece labelled walk
  "walk+2.76"       2.76s after it starts ("walk-0.4" works too)
  "walk.end"        where it ends
  "end"             end of the body (start of the end card)
  "word:five days"  where that phrase is spoken (the body is transcribed once, then cached)

Music and sound effects are files you bring. Nothing is bundled. See recipes/README.md.
"""
from __future__ import annotations

import array
import json
import math
import re
import shutil
import subprocess
from pathlib import Path

from .util import dump_json, probe, proxy_for, run

HERE = Path(__file__).resolve().parent.parent
MONO_LOSS_DB = 6.0      # a stereo sound that loses more than this folded to mono is phase-cancelling
SFX_PEAK_DB = -24.0     # default peak for an effect with no level set
FADE_MS = 150
ANIMS = ("fade", "slide", "pop", "none")


# --- recipe --------------------------------------------------------------------------

def load(recipe_path: Path, out: Path | None = None) -> dict:
    r = json.loads(recipe_path.read_text())
    r["_dir"] = recipe_path.resolve().parent
    r.setdefault("size", [1080, 1920])
    r.setdefault("fps", 30)
    r.setdefault("name", recipe_path.stem)
    out = out or Path(r.get("out", r["_dir"])).expanduser()
    r["_out"] = out if out.is_absolute() else r["_dir"] / out
    r["_out"].mkdir(parents=True, exist_ok=True)
    r["_work"] = r["_out"] / f".{r['name']} work"
    r["_work"].mkdir(parents=True, exist_ok=True)
    if not r.get("pieces"):
        raise ValueError(f"{recipe_path.name}: a recipe needs a \"pieces\" list")
    return r


def _path(r: dict, p: str, base: str | None = None) -> Path:
    q = Path(p).expanduser()
    if q.is_absolute():
        return q
    root = Path(r[base]).expanduser() if base and r.get(base) else r["_dir"]
    if not root.is_absolute():
        root = r["_dir"] / root
    return root / q


def _ffmpeg(args: list, *, cwd: Path | None = None) -> None:
    run(["ffmpeg", "-nostdin", "-v", "error", "-y", *[str(a) for a in args]], cwd=cwd)


def _streams(path: Path) -> tuple[float, float]:
    """(video duration, audio duration). A container duration can hide a long audio track."""
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,duration",
                          "-of", "json", str(path)], capture_output=True, text=True).stdout
    d = {s["codec_type"]: float(s.get("duration", 0)) for s in json.loads(out)["streams"]}
    return d.get("video", 0.0), d.get("audio", 0.0)


def _fps(r: dict) -> tuple[int, int]:
    f = str(r["fps"])
    return tuple(int(x) for x in f.split("/")) if "/" in f else (int(float(f)), 1)


# --- step 1: sections -----------------------------------------------------------------

SQUEEZE = {"floor_db": -38.0, "min_gap": 0.06, "keep": 0.04}


def _energy(src: Path, a: float, b: float) -> list[float]:
    """Loudness in dB per 20 ms window over [a, b]."""
    sr = 16000
    raw = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{a:.3f}", "-t", f"{b - a:.3f}",
                          "-i", str(src), "-vn", "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"],
                         capture_output=True).stdout
    x = array.array("h", raw[: len(raw) // 2 * 2])
    win = int(sr * 0.02)
    return [20 * math.log10(math.sqrt(sum(v * v for v in x[k:k + win]) / win) / 32768 + 1e-9)
            for k in range(0, len(x) - win + 1, win)]


def _squeeze(src: Path, a: float, b: float, cfg: dict) -> list[tuple[float, float]]:
    """Split [a, b] at every pause and return the spoken runs, back to back. Each side of a
    pause is walked to the true silence dip (capped at 0.10 s) so soft word endings and
    onsets stay with their word and only the air between them goes."""
    keep, gap = cfg["keep"], cfg["min_gap"]
    db = _energy(src, a, b)
    n = len(db)
    quiet = [d <= cfg["floor_db"] for d in db]
    need = max(1, round(gap / 0.02))
    cuts, k = [], 0
    while k < n:
        if quiet[k]:
            j = k
            while j < n and quiet[j]:
                j += 1
            if j - k >= need and k > 0 and j < n:          # interior pauses only
                ts = k
                while ts < j and ts - k < 5 and db[ts] > -44:
                    ts += 1
                on = j
                while on > ts and j - on < 5 and db[on - 1] > -44:
                    on -= 1
                cs, ce = a + ts * 0.02 + keep, a + on * 0.02 - cfg.get("lead", 0.06)
                if ce - cs >= 0.04:
                    cuts.append((cs, ce))
            k = j
        else:
            k += 1
    out, cur = [], a
    for cs, ce in cuts:
        out.append((cur, cs))
        cur = ce
    out.append((cur, b))
    return out or [(a, b)]


def _src(r: dict, p: dict) -> Path:
    src = _path(r, p["file"], "footage_dir")
    if not src.exists():
        raise FileNotFoundError(f"piece {p.get('label', '?')}: {src} not found")
    if r.get("proxy", True) and p.get("proxy", True):
        W, H = r["size"]
        src = proxy_for(src, W, H)
    return src


def _expand(r: dict) -> list[dict]:
    """Recipe pieces -> pieces actually cut. A talk piece with squeeze on becomes several
    sub-pieces with the same label (merged back into one on the timeline). Recipe
    "squeeze": true turns it on for every talk piece; piece "squeeze": false opts out;
    piece "tail": 0.3 keeps a beat after its last word."""
    rs = r.get("squeeze")
    out = []
    for p in r["pieces"]:
        ps = p.get("squeeze", rs if p.get("kind", "talk") == "talk" else None)
        if not ps or p.get("out") is None:
            out.append(p)
            continue
        cfg = {**SQUEEZE, **(ps if isinstance(ps, dict) else {})}
        spans = _squeeze(_src(r, p), float(p.get("in", 0.0)), float(p["out"]), cfg)
        if p.get("tail"):
            spans[-1] = (spans[-1][0], min(float(p["out"]), spans[-1][1] + float(p["tail"])))
        before = float(p["out"]) - float(p.get("in", 0.0))
        print(f"  squeeze {p.get('label', '?')}: {before:.2f}s -> {sum(e - s for s, e in spans):.2f}s "
              f"in {len(spans)} runs")
        for s, e in spans:
            out.append({**p, "in": round(s, 3), "out": round(e, 3), "squeeze": False})
    return out


def sections(r: dict) -> Path:
    W, H = r["size"]
    fn, fd = _fps(r)
    inputs: list = []
    graph: list[str] = []
    labels: list[str] = []
    timeline: list[dict] = []
    t = 0.0
    for i, p in enumerate(_expand(r)):
        src = _src(r, p)
        info = probe(src)
        a = float(p.get("in", 0.0))
        b = float(p["out"]) if p.get("out") is not None else info["duration"]
        # Whole frames only, rounded up, so picture and sound end on the same frame.
        frames = max(1, math.ceil((b - a) * fn / fd - 1e-6))
        dur = frames * fd / fn
        inputs += ["-ss", f"{a:.6f}", "-t", f"{dur + 1.0:.3f}", "-i", str(src)]
        extra = f",{p['vf']}" if p.get("vf") else ""
        # Fill the frame then crop, so a 16:9 clip and a 9:16 clip both land at the recipe size
        # with no black bars. x_pct / y_pct move the crop (0 = left/top, 50 = centre).
        cx = float(p.get("x_pct", 50)) / 100
        cy = float(p.get("y_pct", 50)) / 100
        graph.append(f"[{i}:v]setpts=PTS-STARTPTS,scale={W}:{H}:force_original_aspect_ratio=increase,"
                     f"crop={W}:{H}:(iw-{W})*{cx:.3f}:(ih-{H})*{cy:.3f}{extra},"
                     f"fps={fn}/{fd},tpad=stop_mode=clone:stop=2,trim=end_frame={frames},"
                     f"setpts=PTS-STARTPTS,format=yuv420p,setsar=1[v{i}]")
        # Every piece's audio is padded then cut to exactly its picture length.
        if info["has_audio"]:
            graph.append(f"[{i}:a]asetpts=PTS-STARTPTS,volume={float(p.get('gain_db', 0))}dB,"
                         f"aresample=48000,aformat=channel_layouts=stereo,apad,atrim=0:{dur}[a{i}]")
        else:
            graph.append(f"anullsrc=r=48000:cl=stereo,atrim=0:{dur}[a{i}]")
        labels.append(f"[v{i}][a{i}]")
        label = p.get("label", f"piece{i}")
        if timeline and timeline[-1]["label"] == label:   # squeezed sub-pieces stay one piece
            timeline[-1]["end"] = round(t + dur, 3)
        else:
            timeline.append({"label": label, "kind": p.get("kind", "talk"),
                             "start": round(t, 3), "end": round(t + dur, 3)})
        t += dur
    graph.append(f"{''.join(labels)}concat=n={len(labels)}:v=1:a=1[v][a]")
    fg = r["_work"] / "sections_fg.txt"
    fg.write_text(";\n".join(graph))
    joined = r["_out"] / f"{r['name']} sections.mp4"
    _ffmpeg([*inputs, "-filter_complex_script", fg, "-map", "[v]", "-map", "[a]",
             "-c:v", "libx264", "-crf", "17", "-preset", "fast", "-c:a", "aac", "-b:a", "192k", joined])
    v, au = _streams(joined)
    if abs(v - au) > 0.1:
        raise RuntimeError(f"sections: picture {v:.2f}s but audio {au:.2f}s")
    dump_json(r["_out"] / f"{r['name']} cuts.json", {"duration": round(t, 3), "pieces": timeline})
    for s in timeline:
        print(f"  {s['start']:7.2f}  {s['label']} ({s['kind']})")
    print(f"  sections: {t:.2f}s -> {joined}")
    return joined


# --- step 2: finish -------------------------------------------------------------------

class Timeline:
    def __init__(self, r: dict, body: Path, cuts: dict):
        self.r, self.body, self.cuts = r, body, cuts
        self.pieces = {p["label"]: p for p in cuts["pieces"]}
        self.end = probe(body)["duration"]
        self._words: list[dict] | None = None
        drift = self.end - cuts["duration"]
        if abs(drift) > 0.1:
            print(f"  WARNING: body is {self.end:.2f}s but cuts.json says {cuts['duration']:.2f}s "
                  f"({drift:+.2f}s). Anchors after the change will be off; re-run sections.")

    def words(self) -> list[dict]:
        if self._words is None:
            from .transcribe import transcribe
            proj = self.r["_work"] / "body-transcript"
            proj.mkdir(exist_ok=True)
            cache = proj / f"words-{self.body.stat().st_size}.json"
            if cache.exists():
                self._words = json.loads(cache.read_text())
            else:
                self._words = transcribe(self.body, proj, vocab=self.r.get("vocab"))["words"]
                cache.write_text(json.dumps(self._words))
        return self._words

    def at(self, anchor) -> float:
        if isinstance(anchor, (int, float)):
            return float(anchor)
        s = str(anchor).strip()
        if s.startswith("word:"):
            return self._word(s[5:])
        m = re.fullmatch(r"(.+?)([+-]\d+(?:\.\d+)?)?", s)
        base, off = m.group(1), float(m.group(2) or 0)
        if base == "end":
            return self.end + off
        if base.endswith(".end") and base[:-4] in self.pieces:
            return self.pieces[base[:-4]]["end"] + off
        if base in self.pieces:
            return self.pieces[base]["start"] + off
        raise KeyError(f"anchor {anchor!r}: no piece called {base!r} (have {list(self.pieces)})")

    def _word(self, phrase: str) -> float:
        norm = lambda w: re.sub(r"[^a-z0-9']", "", w.lower())
        want = [norm(w) for w in phrase.split()]
        ws = self.words()
        toks = [norm(w["word"]) for w in ws]
        for i in range(len(toks) - len(want) + 1):
            if toks[i:i + len(want)] == want:
                return float(ws[i]["start"])
        raise KeyError(f"word anchor {phrase!r} not found in the transcript")

    def many(self, spec) -> list[float]:
        """A list of anchors, or a group: 'cuts' (every piece start after the first),
        'kind:broll' (every b-roll start), 'stamps' (every stamp's start)."""
        if isinstance(spec, list):
            return [self.at(a) for a in spec]
        if spec == "cuts":
            return [p["start"] for p in self.cuts["pieces"][1:]]
        if isinstance(spec, str) and spec.startswith("kind:"):
            return [p["start"] for p in self.cuts["pieces"] if p["kind"] == spec[5:]]
        if spec == "stamps":
            return [max(0.05, self.at(s["at"])) for s in self.r.get("stamps", [])]
        return [self.at(spec)]


def _levels(path: Path) -> tuple[float, float]:
    """(peak dBFS of the loudest channel, peak dBFS once folded to mono)."""
    def peak(af: list[str]) -> float:
        err = subprocess.run(["ffmpeg", "-nostdin", "-i", str(path), *af, "-af", "volumedetect",
                              "-f", "null", "-"], capture_output=True, text=True).stderr
        m = re.search(r"max_volume: (-?[\d.]+) dB", err)
        return float(m.group(1)) if m else -91.0
    return peak([]), peak(["-ac", "1"])


def _ts(t: float) -> str:
    t = max(0.0, t)
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def _anim(kind: str, x: int, y: int, H: int, fin: int, fout: int) -> str:
    """Simple entrances only. fade: fades in and out. slide: rises a few pixels into place.
    pop: grows from 85% to full size. none: hard on, hard off."""
    if kind not in ANIMS:
        raise ValueError(f"animation {kind!r}: use one of {', '.join(ANIMS)}")
    if kind == "none":
        return f"\\an8\\pos({x},{y})"
    fad = f"\\fad({fin},{fout})"
    if kind == "slide":
        return f"\\an8\\move({x},{y + round(H * 0.025)},{x},{y},0,260){fad}"
    if kind == "pop":
        return f"\\an8\\pos({x},{y})\\fscx85\\fscy85\\t(0,180,\\fscx100\\fscy100){fad}"
    return f"\\an8\\pos({x},{y}){fad}"


def _esc(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", "\\N")


def _ass(r: dict, tl: Timeline, card: dict | None, card_secs: float, only_card: bool = False) -> str:
    W, H = r["size"]
    font = r.get("font", "Helvetica Neue")
    st = r.get("stamp_style", {})
    size = round(H * st.get("size_pct", 3.5) / 100)
    top = round(H * st.get("y_pct", 12) / 100)
    anim = st.get("anim", "fade")
    lines = ["[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}", "[V4+ Styles]",
             "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
             "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
             "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
             f"Style: T,{font},{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,1,0,0,0,100,100,1,0,1,"
             f"{st.get('outline', 2)},{st.get('shadow', 1)},8,40,40,{top},1"]
    if card:
        cs = round(H * card.get("size_pct", 4.5) / 100)
        lines.append(f"Style: E,{font},{cs},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,1,0,0,0,"
                     f"100,100,0,0,1,0,0,8,40,40,0,1")
    lines += ["[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
    stamps = [] if only_card else r.get("stamps", [])
    for i, s in enumerate(stamps):
        a = tl.at(s["at"])
        b = tl.at(s["until"]) if "until" in s else (tl.at(stamps[i + 1]["at"]) if i + 1 < len(stamps) else tl.end)
        fin = 0 if a <= 0.01 else FADE_MS
        fout = 0 if b >= tl.end - 0.01 else FADE_MS
        tag = _anim(s.get("anim", anim), W // 2, top, H, fin, fout)
        lines.append(f"Dialogue: 0,{_ts(a)},{_ts(b)},T,,0,0,0,,{{{tag}}}{_esc(s['text'])}")
    if card:
        cy = round(H * card.get("y_pct", 45) / 100)
        tag = _anim(card.get("anim", "fade"), W // 2, cy, H, 300, 0)
        lines.append(f"Dialogue: 0,{_ts(tl.end)},{_ts(tl.end + card_secs)},E,,0,0,0,,{{{tag}}}{_esc(card['text'])}")
    return "\n".join(lines) + "\n"


def _card(r: dict, tl: Timeline, card: dict, csecs: float) -> Path:
    """The end card as its own short clip: a plain colour, its text, silence."""
    W, H = r["size"]
    fn, fd = _fps(r)
    work = r["_work"]
    (work / "card.ass").write_text(_ass(r, tl, card, csecs, only_card=True))
    bg = card.get("color", "black").lstrip("#")
    bg = f"0x{bg}" if re.fullmatch(r"[0-9a-fA-F]{6}", bg) else bg
    _ffmpeg(["-f", "lavfi", "-i", f"color=c={bg}:s={W}x{H}:r={fn}/{fd}:d={csecs}",
             "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", f"{csecs:.3f}",
             # The card's text is timed from the end of the body, so shift into that time,
             # burn the text, then shift back to zero.
             "-vf", f"setpts=PTS+{tl.end:.3f}/TB,subtitles=card.ass,setpts=PTS-STARTPTS,format=yuv420p,setsar=1",
             "-c:v", "libx264", "-crf", "17", "-preset", "medium", "-c:a", "aac", "-b:a", "192k", "card.mp4"],
            cwd=work)
    return work / "card.mp4"


def finish(r: dict, body: Path, version: str | None = None) -> Path:
    cuts_path = r["_out"] / f"{r['name']} cuts.json"
    if not cuts_path.exists():
        raise FileNotFoundError(f"{cuts_path} missing: run the sections step first")
    tl = Timeline(r, body, json.loads(cuts_path.read_text()))
    W, H = r["size"]
    fn, fd = _fps(r)
    work = r["_work"]
    card = r.get("end_card")
    csecs = float(card.get("secs", 2.0)) if card else 0.0

    # The body (text, sounds, music) is built and captioned on its own; the end card is joined
    # on afterwards, so a caption can never spill onto it.
    (work / "overlay.ass").write_text(_ass(r, tl, None, 0.0))
    inputs: list = ["-i", str(body)]
    graph = [f"[0:v]scale={W}:{H}:flags=lanczos,fps={fn}/{fd},tpad=stop_mode=clone:stop=2,"
             f"trim=0:{tl.end:.3f},format=yuv420p,setsar=1,subtitles=overlay.ass[v]",
             f"[0:a]aresample=48000,aformat=channel_layouts=stereo,apad,atrim=0:{tl.end:.3f}[base]"]
    mix = ["[base]"]
    n = 1
    report = []

    for fx in r.get("sfx", []):
        f = _path(r, fx["sound"], "sfx_dir")
        if not f.exists():
            raise FileNotFoundError(f"sound effect {f} not found")
        peak, mono = _levels(f)
        fold = ""
        if peak - mono > MONO_LOSS_DB:
            # Out-of-phase stereo vanishes on a phone speaker. Use one channel instead.
            fold = "pan=stereo|c0=c0|c1=c0,"
            print(f"  sfx {f.name}: loses {peak - mono:.1f} dB in mono, using the left channel only")
        target = float(fx.get("peak_db", SFX_PEAK_DB))
        gain = target - peak
        lead = float(fx.get("lead", 0.0))
        for t in tl.many(fx["at"]):
            ms = max(0, round((t - lead) * 1000))
            inputs += ["-i", str(f)]
            graph.append(f"[{n}:a]{fold}aresample=48000,aformat=channel_layouts=stereo,"
                         f"volume={gain:.2f}dB,adelay={ms}|{ms}[s{n}]")
            mix.append(f"[s{n}]")
            report.append(f"{t:6.2f}s  {f.name}  peak {target:.0f} dBFS")
            n += 1

    mu = r.get("music")
    if mu:
        mf = _path(r, mu["file"], "music_dir")
        if not mf.exists():
            raise FileNotFoundError(f"music {mf} not found")
        start = float(mu.get("start", 0.0))
        if "land" in mu:
            # "land the song's drop at 32s on the cut to the street": solve for where the song starts.
            start = float(mu["land"]["song_at"]) - tl.at(mu["land"]["on"])
        fo = float(mu.get("fade_out", 0.6))
        # Music stops at the end card so the loop restarts clean.
        inputs += ["-ss", f"{max(0.0, start):.3f}", "-i", str(mf)]
        graph.append(f"[{n}:a]aresample=48000,aformat=channel_layouts=stereo,atrim=0:{tl.end:.3f},"
                     f"afade=t=in:d={mu.get('fade_in', 0.5)},afade=t=out:st={tl.end - fo:.3f}:d={fo},"
                     f"volume={float(mu.get('gain_db', -18))}dB,apad[m]")
        mix.append("[m]")
        report.append(f"music {mf.name} from {start:.2f}s at {mu.get('gain_db', -18)} dB")
        n += 1

    graph.append(f"{''.join(mix)}amix=inputs={len(mix)}:normalize=0:duration=first,atrim=0:{tl.end:.3f}[a]")
    (work / "finish_fg.txt").write_text(";\n".join(graph))
    master = work / f"{r['name']} master.mp4"
    _ffmpeg([*inputs, "-filter_complex_script", "finish_fg.txt", "-map", "[v]", "-map", "[a]",
             "-c:v", "libx264", "-crf", "17", "-preset", "medium", "-c:a", "aac", "-b:a", "192k",
             str(master)], cwd=work)
    for line in report:
        print("  " + line)

    version = version or _next_version(r)
    final = r["_out"] / f"{r['name']} {version}.mp4"
    cap = r.get("captions")
    if cap:
        cap = cap if isinstance(cap, dict) else {}
        cmd = [str(HERE / "edit"), str(master), "--no-cut", "--no-bad-takes", "--captions", "--no-open",
               "--name", f"{r['name']}-{version}".replace(" ", "-"), "--out-dir", str(work)]
        if r.get("vocab"):
            cmd += ["--vocab", *r["vocab"]]
        if cap.get("fix"):
            cmd += ["--fix", *cap["fix"]]
        out = subprocess.run(cmd, capture_output=True, text=True)
        m = re.search(r"exported: (.+\.mp4)", out.stdout)
        if out.returncode or not m:
            raise RuntimeError(f"captions failed:\n{out.stdout[-1500:]}\n{out.stderr[-1500:]}")
        master = Path(m.group(1))
    if card:
        cardf = _card(r, tl, card, csecs)
        join = (f"[0:v]fps={fn}/{fd},format=yuv420p,setsar=1[v0];[1:v]fps={fn}/{fd},format=yuv420p,setsar=1[v1];"
                "[0:a]aresample=48000,aformat=channel_layouts=stereo[a0];"
                "[1:a]aresample=48000,aformat=channel_layouts=stereo[a1];"
                "[v0][a0][v1][a1]concat=n=2:v=1:a=1[v][a]")
        _ffmpeg(["-i", master, "-i", cardf, "-filter_complex", join, "-map", "[v]", "-map", "[a]",
                 "-c:v", "libx264", "-crf", "17", "-preset", "medium", "-c:a", "aac", "-b:a", "192k", final])
    else:
        shutil.move(master, final)
    v, au = _streams(final)
    print(f"  finish: {final.name}  picture {v:.2f}s, audio {au:.2f}s")
    return final


def _next_version(r: dict) -> str:
    taken = [int(m.group(1)) for p in r["_out"].glob(f"{r['name']} v*.mp4")
             if (m := re.fullmatch(re.escape(r["name"]) + r" v(\d+)\.mp4", p.name))]
    return f"v{max(taken, default=0) + 1}"
