#!/usr/bin/env python3
"""Local AI video editor — v1 (#63).

raw footage → rough cut (faster-whisper) → animated karaoke captions →
background music (-23dB) → exported MP4. Fully local, no cloud, no API keys.

Run it through the venv (has faster-whisper):
    ./edit <input.mp4> [options]           # launcher sets PATH + venv python
or directly:
    .venv/bin/python edit.py <input.mp4> [options]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src import assemble as assemble_mod  # noqa: E402
from src import badtakes as badtakes_mod  # noqa: E402
from src import captions as captions_mod  # noqa: E402
from src import drafter as drafter_mod  # noqa: E402
from src import llm as llm_mod  # noqa: E402
from src import preview as preview_mod  # noqa: E402
from src import retakes as retakes_mod  # noqa: E402
from src import roughcut as roughcut_mod  # noqa: E402
from src import speech as speech_mod  # noqa: E402
from src import scriptmatch as scriptmatch_mod  # noqa: E402
from src import titlecard as titlecard_mod  # noqa: E402
from src import transcribe as transcribe_mod  # noqa: E402
from src import geometry as geometry_mod  # noqa: E402
from src import util as util_mod  # noqa: E402
from src.util import (merge_overlaps, close_intraword_gaps, boundary_report, detect_silence, dump_json, fmt_secs, load_json,  # noqa: E402
                      next_versioned_path, probe)

ROOT = Path(__file__).resolve().parent


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Local AI video editor (v1)")
    p.add_argument("input", type=Path, help="raw footage (mp4/mov)")
    p.add_argument("--profile", default=None,
                   help="load defaults from profiles/<name>.json (or a path). Every reel\n                        this look ships on needs ~15 identical flags; typing them each\n                        time is how two reels quietly stop matching. Explicit flags win")
    p.add_argument("--music", type=Path, default=None, help="background track (mixed at -23dB)")
    p.add_argument("--model", default="small", help="faster-whisper size: tiny/base/small/medium")
    p.add_argument("--fresh", action="store_true", help="ignore any cached transcript, re-run whisper")
    p.add_argument("--no-repair-transcript", action="store_true",
                   help="skip re-decoding words whose logged duration is impossible")
    p.add_argument("--breath-trim", action=argparse.BooleanOptionalAction, default=True,
                   help="also cut breath-level dead air, the pauses that sit ABOVE the silence\n"
                        "                        floor because the take is loud. Guarded by a voicing test so a\n"
                        "                        quiet WORD is never bladed. (default: on)")
    p.add_argument("--breath-offset", type=float, default=speech_mod.BREATH_OFFSET_DB,
                   help="breath gate, dB below the cut's speech level (default -10.5)")
    p.add_argument("--verify", action=argparse.BooleanOptionalAction, default=True,
                   help="re-transcribe the assembled audio and remove repeats the SOURCE\n"
                        "                        transcript hid. (default: on)")
    p.add_argument("--no-repair", action="store_true",
                   help="report what --verify finds but do not re-cut to remove it")
    p.add_argument("--max-gap", type=float, default=0.2, help="cut silences longer than this (s)")
    p.add_argument("--words-per-line", type=int, default=3, help="caption words shown per line")
    p.add_argument("--accent", default="FFFFFF", help="caption spoken-word/highlight colour (RRGGBB hex, default white)")
    p.add_argument("--caption-color", default="FFFFFF", help="caption base text colour (RRGGBB hex)")
    p.add_argument("--caption-font", default="Arial", help="caption font family (must be installed on the render machine)")
    p.add_argument("--no-highlight", action="store_true", help="disable the karaoke word pop (static captions)")
    # CapCut-style dials, so a client's own caption spec transfers 1:1. 10/30/30 == the
    # previous hard-coded look; see captions.build_ass.
    p.add_argument("--caption-size", type=float, default=10.0, help="caption size dial (CapCut-style, 10 = baseline)")
    p.add_argument("--caption-stroke", type=float, default=30.0, help="caption stroke/outline 0-100 (CapCut-style)")
    p.add_argument("--caption-shadow", type=float, default=30.0, help="caption shadow 0-100 (CapCut-style)")
    p.add_argument("--caption-y", type=float, default=16.0,
                   help="caption baseline height, %% of frame height from the bottom (16 = lower third)")
    p.add_argument("--caption-x", type=float, default=None,
                   help="caption LEFT edge, %% of frame width. Omit for centred (the default)")
    p.add_argument("--aspect", default=None,
                   help="reframe the export to this ratio, e.g. 4:3. Crops, never pads")
    p.add_argument("--crop-x", type=int, default=None,
                   help="crop offset in SOURCE pixels from the left (needs --aspect). Omit to centre")
    p.add_argument("--height", type=int, default=None,
                   help="cap the export height, e.g. 1080. Only ever downscales")
    p.add_argument("--caption-bg", default=None,
                   help="highlight BOX colour behind the spoken word (RRGGBB); omit for coloured-text highlighting")
    p.add_argument("--strip-filler", action="store_true", help="drop um/uh from captions")
    p.add_argument("--aggressive-filler", action="store_true", help="also drop like/basically/etc")
    p.add_argument("--bad-takes", action="store_true",
                   help="also cut retakes/false-starts/spoken directions (thinks with the "
                        "Claude Code you already have — no API key)")
    p.add_argument("--script", type=Path, default=None,
                   help="intended script (.txt one beat per line, or .json) — align footage to it")
    p.add_argument("--deterministic", action="store_true",
                   help="(default) align --script with pure string-matching, no LLM (same input = same cut)")
    p.add_argument("--llm-align", action="store_true",
                   help="use the Gemini aligner for --script instead of the default deterministic one")
    p.add_argument("--draft", action="store_true",
                   help="reorder your own spoken sentences into the tightest story, then cut "
                        "to it (no API key — uses your Claude Code)")
    p.add_argument("--draft-model", default=llm_mod.DEFAULT_OR_MODEL,
                   help="only used when an OPENROUTER_API_KEY is set (default Claude Sonnet 5)")
    p.add_argument("--voice", type=Path, default=None,
                   help="voice/brand doc to ground the Drafter (writes in your register)")
    p.add_argument("--target", type=float, default=None,
                   help="target reel length in seconds (hint to the Drafter)")
    p.add_argument("--brief", default=None,
                   help="the creator's direction for THIS cut, fed to the Drafter "
                        "(e.g. --brief 'keep the dad story, punchier open')")
    p.add_argument("--script-model", default="gemini-2.5-flash", help="model for script alignment")
    p.add_argument("--cta", nargs="*", default=[],
                   help="CTA keyword/phrase to NEVER cut, e.g. --cta 'comment gap'")
    p.add_argument("--protect-hook", action=argparse.BooleanOptionalAction, default=True,
                   help="never cut the first spoken line (the hook)")
    p.add_argument("--no-uppercase", action="store_true", help="keep caption case as spoken")
    p.add_argument("--strip-punct", action=argparse.BooleanOptionalAction, default=True,
                   help="drop sentence punctuation from captions (default: on)")
    p.add_argument("--caption-max-dur", type=float, default=0.85,
                   help="break a caption line after this long on screen, so line length\n                        tracks delivery and emphasised words land alone (0 = off)")
    p.add_argument("--caption-cut", action=argparse.BooleanOptionalAction, default=True,
                   help="caption the assembled cut instead of remapping the source\n                        transcript — costs one extra encode, cannot drop a word")
    p.add_argument("--title", default=None,
                   help="persistent title card naming the tool, e.g. --title 'Claude Code Editor'")
    p.add_argument("--title-benefit", default=None,
                   help="benefit line under the title, e.g. --title-benefit 'completely free'")
    p.add_argument("--title-variant", default="badge",
                   choices=["badge", "headline", "problem"],
                   help="card treatment. badge = pill naming the product (default);\n                        headline = two stacked lines, product drops to a credit row;\n                        problem = line 1 is the pain and gets struck, then the fix lands")
    p.add_argument("--title-credit", default=None,
                   help="small product line under a headline/problem card "
                        "(default: the --title text)")
    p.add_argument("--title-y", type=float, default=11.2,
                   help="title block top, %% of frame height. Sits relative to the SHOT:\n                        the card should tuck just above whatever the subject is at "
                        "(a laptop lid, a monitor), so a tighter frame wants a lower number")
    p.add_argument("--title-secs", type=float, default=3.0,
                   help="how long the title card holds before it fades (default 3)")
    p.add_argument("--cta-card", default=None,
                   help="keyword for the closing card, e.g. --cta-card EDITOR. Captions are\n                        suppressed over it so nothing competes with the ask")
    p.add_argument("--cta-card-line", default="for the full breakdown",
                   help="second line under the CTA keyword")
    p.add_argument("--fix", nargs="*", default=[], metavar="HEARD=ACTUAL",
                   help="correct a mis-heard phrase before it burns into the captions,\n                        e.g. --fix 'code code=Claude Code'. Phrase-level, so the\n                        replacement may have a different word count")
    p.add_argument("--preview-at", type=float, default=None,
                   help="render ONE frame at this many seconds into the cut and stop, so the\n                        overlay can be reviewed before paying for a full encode. Re-run\n                        without the flag to render for real — the cut is reused, not rebuilt")
    p.add_argument("--preview-dir", type=Path, default=Path.home() / "Desktop",
                   help="where the preview still lands (default: Desktop)")
    p.add_argument("--retakes", action=argparse.BooleanOptionalAction, default=True,
                   help="re-decode stretched tokens to find takes whisper collapsed")
    p.add_argument("--tighten", action=argparse.BooleanOptionalAction, default=True,
                   help="snap every cut range to measured speech (kills head slop,\n                        restores clipped tails, drops silent ranges)")
    p.add_argument("--captions", action=argparse.BooleanOptionalAction, default=False,
                   help="burn in animated karaoke captions (default: OFF — a wrong or "
                        "uncommon word gets baked into the video with no way to fix it, so "
                        "add/fix captions in your editor instead)")
    p.add_argument("--no-cut", action="store_true", help="keep full length, don't trim silences")
    p.add_argument("--vocab", nargs="*", default=[],
                   help="proper nouns / brand / CTA words to prime whisper (e.g. --vocab YAMAL Messi Claude)")
    p.add_argument("--no-normalize", action="store_true",
                   help="skip loudness normalization on export (leave the raw level)")
    p.add_argument("--name", default=None, help="project name (default: <stem>-<timestamp>)")
    p.add_argument("--out-dir", type=Path, default=None,
                   help="where the finished MP4 lands (default: ~/Downloads)")
    p.add_argument("--fast", action="store_true",
                   help="encode on Apple's hardware media engine (h264_videotoolbox): ~30s for a 4K "
                        "clip vs minutes on CPU. Slightly softer, much faster")
    p.add_argument("--beside", action="store_true",
                   help="put the finished MP4 next to the input file instead of ~/Downloads")
    p.add_argument("--dry-run", action="store_true",
                   help="plan the cut, print the resulting transcript and any suspect cut\n"
                        "                        boundaries, then STOP. No encode. Seconds instead of minutes")
    p.add_argument("--no-open", action="store_true", help="don't auto-open the result")
    return p.parse_args()


def _apply_profile(args: argparse.Namespace, argv: list[str]) -> argparse.Namespace:
    """Fill unset options from a profile file. Anything given on the CLI wins.

    "Given on the CLI" is decided by scanning argv, not by comparing against the
    parser default — otherwise passing a value that happens to equal the default
    would be silently overridden by the profile.
    """
    if not args.profile:
        return args
    path = Path(args.profile)
    if not path.exists():
        path = ROOT / "profiles" / f"{args.profile}.json"
    if not path.exists():
        sys.exit(f"edit: no such profile '{args.profile}' (looked in profiles/)")

    explicit = {a.lstrip("-").replace("-", "_") for a in argv if a.startswith("--")}
    data = json.loads(path.read_text())

    # Accumulating options MERGE; scalars are overridden. Replacing was wrong and
    # shipped two finals with "code code" and "these sentences" burned into them:
    # passing one --fix for a per-reel mishearing silently discarded every standing
    # brand correction in the profile. A reel-specific fix is an ADDITION to the
    # house list, never a replacement for it.
    MERGE = {"fix", "vocab", "cta"}

    applied, merged = [], []
    for key, value in data.items():
        if key.startswith("_") or not hasattr(args, key):
            continue
        if key in explicit:
            if key in MERGE and isinstance(value, list):
                have = list(getattr(args, key) or [])
                extra = [v for v in value if v not in have]
                if extra:
                    setattr(args, key, have + extra)
                    merged.append(f"{key}+{len(extra)}")
            continue
        setattr(args, key, value)
        applied.append(key)
    note = ", ".join(sorted(applied))
    if merged:
        note += "  (merged: " + ", ".join(sorted(merged)) + ")"
    print(f"  profile {path.stem}: {note}")
    return args


def main() -> int:
    args = _apply_profile(parse_args(), sys.argv[1:])
    _costs: list[float] = []  # per-run LLM spend (draft + align + bad-takes) for the cost meter
    video = args.input.expanduser().resolve()
    if not video.exists():
        print(f"error: input not found: {video}", file=sys.stderr)
        return 1
    if args.music and not args.music.expanduser().exists():
        print(f"error: music not found: {args.music}", file=sys.stderr)
        return 1

    stem = args.name or f"{video.stem}-{datetime.now():%Y%m%d-%H%M%S}"
    project = ROOT / "projects" / stem
    project.mkdir(parents=True, exist_ok=True)
    print(f"\n▶ project: {project.relative_to(ROOT)}")

    info = probe(video)
    print(f"  source: {info['width']}x{info['height']}  {fmt_secs(info['duration'])}  "
          f"{info['fps']}fps  audio={'yes' if info['has_audio'] else 'NO'}")
    geo = geometry_mod.plan(info["width"], info["height"], aspect=args.aspect,
                            crop_x=args.crop_x, out_height=args.height)
    if geo["chain"]:
        print(f"  reframe: {geo['chain']}  ->  {geo['width']}x{geo['height']}")
    if not info["has_audio"]:
        print("error: no audio stream — nothing to transcribe or cut on.", file=sys.stderr)
        return 1

    # 0 — resolve the script source.
    script_lines: list[str] = []
    if args.script:
        script_path = args.script.expanduser().resolve()
        if script_path.exists():
            script_lines = scriptmatch_mod.load_script(script_path)
        else:
            print(f"  script not found: {script_path} — ignoring.")

    # 1 — transcribe (cache per project so the iterate loop doesn't re-run whisper)
    print("\n[1/4] transcribe")
    cached = project / "transcript.json"
    if cached.exists() and not args.fresh:
        import json as _json
        transcript = _json.loads(cached.read_text())
        print(f"  reusing cached transcript ({len(transcript.get('words', []))} words) — --fresh to redo")
    else:
        transcript = transcribe_mod.transcribe(video, project, model_size=args.model, vocab=args.vocab)
        dump_json(cached, transcript)

    # Real audio silence points — cuts snap to these (not whisper's word ends) so
    # fricative releases never clip and every blade lands in true silence.
    sil_starts: list[float] = []
    sil_ends: list[float] = []
    wav = project / "audio.wav"

    # The source transcript is not trustworthy where whisper stretched a token. A
    # four-letter word cannot take 1.6s to say; when one is logged that long it has
    # swallowed something, almost always a repeated attempt. Re-decoding just those
    # windows, on their own, gets the real words back — and it is what stops a range
    # being planned on top of a take that should not be in the cut.
    if not args.no_repair_transcript:
        fixed, rep = transcribe_mod.repair_stretched(
            transcript["words"], wav, model_size=args.model)
        for r in rep:
            print(f"  transcript repair {r['at']:.2f}s: {r['was']!r} ({r['dur']}s) "
                  f"was really {r['now']!r}")
        if rep:
            transcript["words"] = fixed
            print(f"  {len(rep)} stretched word(s) re-decoded, "
                  f"{len(fixed)} words now")

    if not args.no_cut and wav.exists():
        sil_starts, sil_ends = detect_silence(wav)
        print(f"  {len(sil_starts)} audio silence points (cuts snap to these)")

    # 1.25 — DRAFT the story from the raw transcript (the brain writes story.txt).
    # Only when no explicit --script was given; --draft then feeds 1.5a.
    if args.draft and not script_lines and not args.no_cut:
        print("\n[+] draft — writing the story from the raw transcript")
        prov = drafter_mod.resolve_provider(ROOT, prefer_model=args.draft_model)
        if not prov:
            print("  " + llm_mod.no_backend_message() + ". Skipping the draft.")
        else:
            try:
                vpath = args.voice.expanduser() if args.voice else None
                voice_txt = vpath.read_text() if vpath and vpath.exists() else None
                craft_txt = drafter_mod.load_craft(ROOT)
                if args.brief:
                    print(f"  direction: {args.brief.strip()[:70]}")
                res = drafter_mod.draft_story(
                    transcript, provider=prov, voice=voice_txt, winners=None,
                    target_seconds=args.target, craft=craft_txt, brief=args.brief,
                    examples=None,
                )
                if res["story_lines"]:
                    script_lines = res["story_lines"]
                    story_path = project / "story.txt"
                    story_path.write_text("\n".join(script_lines) + "\n")
                    # persist the raw→story draft so an approved cut can become a
                    # few-shot example for this creator (the worker sends it on complete).
                    dump_json(project / "draft.json", {
                        "segments": [s["text"] for s in res.get("segments", [])],
                        "order": res.get("order", []),
                        "format": res.get("format", ""),
                        "story_lines": script_lines,
                        "brief": args.brief or "",
                    })
                    cost = drafter_mod.estimate_cost(res["usage"], res["provider"])
                    _costs.append(cost)
                    print(f"  {res['provider']} ({res['model']}) · format: {res['format']} · "
                          f"{len(script_lines)} beats" + (f" · ~${cost:.4f}" if cost else ""))
                    if res["reason"]:
                        print(f"  why: {res['reason']}")
                    print(f"  drafted story → {story_path}")
                    for ln in script_lines:
                        print(f"    · {ln[:80]}")
                    print("  (edit that story.txt and re-run with --script to refine)")
                else:
                    print("  draft produced no beats — falling back to plain cut.")
            except Exception as e:  # never let the draft crash a run
                print(f"  draft failed ({e}) — falling back to plain cut.")

    # 1.5a — script-aware cutting (preferred when a script is given)
    cut = None
    if script_lines and not args.no_cut:
        try:
            # Deterministic string-matching is the DEFAULT — reproducible, no cost,
            # and it was more reliable than the LLM on unscripted footage. Opt into
            # the model only with --llm-align (and only if a Gemini key is present).
            key = (llm_mod.load_env_key("GEMINI_API_KEY", project_root=ROOT)
                   or llm_mod.load_env_key("GEMINI_KEY", project_root=ROOT))
            if args.llm_align and key:
                print("\n[+] script-aware cutting (LLM, --llm-align)")
                al = scriptmatch_mod.align_script(transcript, script_lines,
                                                  api_key=key, model=args.script_model)
                cost = scriptmatch_mod.estimate_cost(al["usage"])
            else:
                if args.llm_align and not key:
                    print("\n[+] script-aware cutting (deterministic — no GEMINI_API_KEY)")
                else:
                    print("\n[+] script-aware cutting (deterministic, no LLM)")
                al = scriptmatch_mod.align_script_local(transcript, script_lines)
                cost = 0.0
            _costs.append(cost)
            sc = scriptmatch_mod.build_script_cut(
                transcript, script_lines, al["matches"], info["duration"],
                max_gap=args.max_gap,
                strip_filler=args.strip_filler or args.aggressive_filler,
                aggressive_filler=args.aggressive_filler,
                protect_hook=args.protect_hook, cta_terms=args.cta,
                sil_starts=sil_starts, sil_ends=sil_ends,
            )
            print(f"  matched {sc['matched']}/{sc['total']} script lines "
                  f"(coverage {sc['coverage']})" + (f" · ~${cost:.4f}" if cost else ""))
            dump_json(project / "script_match.json",
                      {"matches": al["matches"], "coverage": sc["coverage"]})
            if sc["fallback"]:
                print(f"  low coverage ({sc['coverage']}) — falling back to bad-takes/plain.")
            else:
                cut = sc
        except Exception as e:  # any align/parse failure → fall back, never crash a run
            print(f"  script align failed ({e}) — falling back.")

    cut_was_scripted = cut is not None

    # 1.5b — bad-take detection. Runs on BOTH paths: a scripted cut keeps one
    # contiguous span per beat, so any false start INSIDE a beat rides along and
    # only this pass can see it.
    bad_spans: list[list[float]] = []
    bt_cache = project / "bad_takes.json"
    if args.bad_takes and not args.no_cut and bt_cache.exists() and not args.fresh:
        import json as _json
        cachedbt = _json.loads(bt_cache.read_text())
        bad_spans = cachedbt.get("spans", [])
        print(f"\n[+] bad-take detection — reusing cached {len(bad_spans)} span(s) "
              f"(deterministic; --fresh to redo)")
    elif args.bad_takes and not args.no_cut:
        backend = llm_mod.resolve_backend(ROOT)
        if not backend:
            print("\n[+] bad-take detection")
            print("  " + llm_mod.no_backend_message() + ". Skipping the bad-take pass.")
        else:
            print(f"\n[+] bad-take detection · {llm_mod.describe(backend)}")
            protect = []
            if args.protect_hook:
                hook = badtakes_mod.first_line_word_span(transcript["words"])
                if hook:
                    protect.append(hook)
            protect += badtakes_mod.cta_word_spans(transcript["words"], args.cta)
            try:
                bt = badtakes_mod.detect_bad_takes(
                    transcript, backend=backend, protect_spans=protect,
                )
            except Exception as e:  # a flaky model call must never lose the whole cut
                print(f"  bad-take pass failed ({e}). Continuing with the plain cut.")
                bt = None
            if bt:
                bad_spans = bt["spans"]
                dump_json(project / "bad_takes.json",
                          {"removed": bt["removed"], "spans": bad_spans})
                for r in bt["removed"]:
                    print(f"  ✂ {r['reason']}: \"{r['text'][:70].strip()}\"")
                cost = badtakes_mod.estimate_cost(bt["usage"], backend["name"])
                _costs.append(cost)
                print(f"  flagged {len(bad_spans)} bad-take span(s)"
                      + (f" · ~${cost:.4f}" if cost else ""))

    # 1.75 — RETAKES whisper hid. A repeated take that the decoder collapsed into
    # one stretched token is invisible to every text-based check, including the
    # LLM pass above, because the text it reads is already clean. This goes back to
    # the audio. Free, deterministic, no API key. See src/retakes.py.
    retake_spans: list[list[float]] = []
    if args.retakes and not args.no_cut and wav.exists():
        print("\n[1.75] retake sweep")
        rt = retakes_mod.find(wav, transcript["words"], model_size=args.model,
                              vocab=args.vocab)
        if rt["spans"]:
            for a, b in rt["spans"]:
                said = " ".join(w["word"] for w in rt["words"] if a <= w["start"] < b)
                print(f"  drop {a:.2f}-{b:.2f} ({b - a:.2f}s) — {said[:70]}")
            bad_spans = (bad_spans or []) + rt["spans"]
            retake_spans = rt["spans"]
            print(f"  {len(rt['spans'])} repeated take(s) · {rt['removed']:.1f}s")
        else:
            print(f"  none ({len(rt['windows'])} window(s) checked)")

    # 2 — rough cut (unless script-aware cutting already produced a cut)
    print("\n[2/4] rough cut")
    if cut is None:
        if args.no_cut:
            cut = {"ranges": [[0.0, round(info["duration"], 3)]], "kept_words": transcript["words"],
                   "kept_duration": round(info["duration"], 3), "removed_duration": 0.0, "cuts": 0}
        else:
            cut = roughcut_mod.plan_cut(
                transcript["words"], info["duration"],
                max_gap=args.max_gap,
                strip_filler=args.strip_filler or args.aggressive_filler,
                aggressive_filler=args.aggressive_filler,
                bad_spans=bad_spans,
                segments=transcript.get("segments"),
                sil_starts=sil_starts, sil_ends=sil_ends,
            )
    # 2.25 — apply the retake spans. plan_cut consumes `bad_spans`, but the
    # script-aware path above never calls plan_cut, so on a scripted cut the spans
    # would otherwise be found and then thrown away. Subtracting from the finished
    # ranges works on every path; tighten (next) then snaps the new edges to real
    # speech, which also repairs whisper's sloppy span boundaries.
    scripted_drop = [sp for sp in (bad_spans or []) if sp not in retake_spans] if cut_was_scripted else []
    if scripted_drop:
        retake_spans = retake_spans + scripted_drop
    if retake_spans:
        before = cut["kept_duration"]
        cut["ranges"] = speech_mod.subtract(cut["ranges"], retake_spans)
        cut["kept_duration"] = round(sum(b - a for a, b in cut["ranges"]), 3)
        print(f"  retakes removed: {before:.2f}s -> {cut['kept_duration']:.2f}s")

    # 2.5 — TIGHTEN every range against the waveform. plan_cut places blades using
    # whisper's word times, which drift from the real onset and can sit inside a
    # word. Snapping to measured speech removes the head slop that makes a cut drag
    # (0.6-0.9s per line on the reel this was built for) and restores the consonant
    # tails that were being clipped. Also drops any range holding no speech at all.
    if args.tighten and not args.no_cut and wav.exists():
        runs = speech_mod.speech_runs(wav)
        # `protect` = the retake spans just removed. Without it the forward reach
        # could bridge straight back into a bad take whose first words sit within
        # a breath of the previous range's end.
        tight, report = speech_mod.tighten(cut["ranges"], runs, protect=retake_spans)
        if tight:
            slop = sum(r["head_slop"] for r in report if r["action"] == "tightened")
            clip = [r for r in report if r.get("tail_clip", 0) < 0]
            dropped = [r for r in report if r["action"] == "dropped"]
            before = cut["kept_duration"]
            cut["ranges"] = tight
            cut["kept_duration"] = round(sum(b - a for a, b in tight), 3)
            cut["cuts"] = max(0, len(tight) - 1)
            bridged = [r for r in report if r.get("bridged", 0) > 0.01]
            print(f"  tightened to the waveform: {before:.2f}s -> {cut['kept_duration']:.2f}s "
                  f"({slop:.2f}s head slop, {len(clip)} clipped tail(s) restored, "
                  f"{len(bridged)} word(s) finished past the cut, "
                  f"{len(dropped)} silent range(s) dropped)")
            dump_json(project / "tighten.json", {"report": report})

        # Tighten moves EDGES, and a restored tail can run past where the next range
        # already begins. concat replays that shared audio, heard as a stutter the
        # speaker never said. Merge before anything downstream sees it.
        merged, overlaps = merge_overlaps(cut["ranges"])
        if overlaps:
            cut["ranges"] = merged
            cut["kept_duration"] = round(sum(b - a for a, b in merged), 3)
            cut["cuts"] = max(0, len(merged) - 1)
            print(f"  merged {overlaps} overlapping range(s) -> "
                  f"{cut['kept_duration']:.2f}s (each would have played twice)")

        # A gap can also land mid-WORD without the ranges overlapping, which chops a
        # syllable and replays the rest: "describe" -> "descr...ibe".
        joined, njoin = close_intraword_gaps(cut["ranges"], transcript["words"])
        if njoin:
            cut["ranges"] = joined
            cut["kept_duration"] = round(sum(b - a for a, b in joined), 3)
            cut["cuts"] = max(0, len(joined) - 1)
            print(f"  closed {njoin} cut(s) landing inside a word -> "
                  f"{cut['kept_duration']:.2f}s")

            # tighten only moves EDGES. A pause sitting inside a kept range survives
            # it — and survives plan_cut too on the script-aware path, which never
            # calls plan_cut. Carve those out against the same measured runs, never
            # cutting inside a word.
            carved: list[list[float]] = []
            for a, b in cut["ranges"]:
                gaps = speech_mod.interior_gaps(a, b, runs, min_gap=args.max_gap + 0.14)
                carved += speech_mod.carve(a, b, gaps) if gaps else [[a, b]]
            if len(carved) > len(cut["ranges"]):
                held = cut["kept_duration"]
                cut["ranges"] = carved
                cut["kept_duration"] = round(sum(b - a for a, b in carved), 3)
                cut["cuts"] = max(0, len(carved) - 1)
                print(f"  interior dead air carved: {held:.2f}s -> {cut['kept_duration']:.2f}s "
                      f"({len(carved) - len(tight)} extra cut(s))")

            # A range holding only a stray "and" is a jerky double-cut on screen and
            # reads as a stutter in the captions. plan_cut already drops these, but
            # the script-aware path never calls it — so do it on the ranges.
            kept, orphans = roughcut_mod.drop_orphan_ranges(cut["ranges"], transcript["words"])
            if orphans:
                cut["ranges"] = kept
                cut["kept_duration"] = round(sum(b - a for a, b in kept), 3)
                cut["cuts"] = max(0, len(kept) - 1)
                for o in orphans:
                    print(f"  dropped orphan range {o['range'][0]:.2f}-{o['range'][1]:.2f} "
                          f"— only {' '.join(o['words'])!r}")

    dump_json(project / "cut.json", {k: v for k, v in cut.items() if k != "kept_words"})
    print(f"  {cut['cuts']} cut(s) · kept {fmt_secs(cut['kept_duration'])} · "
          f"removed {fmt_secs(cut['removed_duration'])} of dead air/filler")

    # Cut boundaries on the OUTPUT timeline, so a caption line never straddles a
    # hard cut. Removing the silence at a join takes the pause that would have
    # ended the sentence with it, so the sentence-end test cannot see the join.
    seams, acc = [], 0.0
    for a, b in cut["ranges"][:-1]:
        acc += b - a
        seams.append(round(acc, 3))
    # The CTA is the final range. When a CTA card is on, captions stop there so the
    # ask is the only thing reading on screen.
    cta_at = seams[-1] if (seams and args.cta_card) else None

    def write_captions(words, ranges, w, h):
        ass = captions_mod.build_ass(
            words, ranges, w, h,
            accent=args.accent, color=args.caption_color, highlight_on=not args.no_highlight,
            font=args.caption_font, uppercase=not args.no_uppercase,
            strip_punct=args.strip_punct, words_per_line=args.words_per_line,
            max_line_dur=args.caption_max_dur, break_at=seams,
            pre_gaps=cut.get("pre_gaps"),
            size=args.caption_size, stroke=args.caption_stroke,
            shadow_pct=args.caption_shadow, bg=args.caption_bg, y_pct=args.caption_y,
            x_pct=args.caption_x,
        )
        path = project / "captions.ass"
        path.write_text(ass)
        return path

    # 3 — captions. Two routes:
    #   caption_cut (default) — assemble clean FIRST, transcribe that file, caption
    #     it. No remap exists, so no word can be dropped by a drifting timestamp,
    #     and brand names are decoded from what actually ships.
    #   legacy — caption from the source transcript through build_timemap.
    captions_path = None
    caption_from_cut = args.captions and args.caption_cut
    if args.captions and not caption_from_cut:
        print("\n[3/4] captions")
        captions_path = write_captions(cut["kept_words"], cut["ranges"],
                                       geo["width"], geo["height"])
        print(f"  wrote {captions_path.name}")
    elif caption_from_cut:
        print("\n[3/4] captions — deferred until the cut exists (--caption-cut)")
    else:
        print("\n[3/4] captions — skipped")

    # 4 — assemble
    # Cut is final here. Check the EDGES before paying for an encode: "20/20 matched,
    # coverage 1.0" only means the text was found, not that the cut sounds clean.
    issues = boundary_report(cut["ranges"], transcript["words"])
    if issues:
        print(f"\n[!] {len(issues)} cut(s) land mid-speech and may be heard as a glitch:")
        for it in issues[:10]:
            print(f"    {it['kind']:5} {it['at']:8.2f}s  only {it['gap']:.2f}s silence | {it['context']}")
        if len(issues) > 10:
            print(f"    ... and {len(issues) - 10} more")
        print("    Fix by moving that beat's text to start/end on a real pause.")

    if args.dry_run:
        kept = [w for w in transcript["words"]
                if any(a <= w["start"] < b for a, b in cut["ranges"])]
        print(f"\n[dry-run] {cut['kept_duration']:.2f}s, {len(cut['ranges'])} range(s), "
              f"{len(kept)} words. Nothing encoded.\n")
        print(" ".join(w["word"] for w in kept))
        print()
        return 0

    # MEASURE THE AUDIO THAT WILL SHIP, BEFORE PAYING FOR A VIDEO ENCODE.
    # Two things can only be known from the assembled cut, never from the plan:
    #
    #   1. Repeats. The source transcript is not evidence about the cut. Whisper
    #      hides a repeated take inside a stretched token — on one take it logged
    #      the opening word 1.68s early and swallowed the previous attempt in a
    #      single 1.62s "word" — so every pass upstream reads the same lie and
    #      reports the cut clean while it opens "There's a free there's a free".
    #   2. Breath-level dead air. The gate is a fraction of how loud the speaker
    #      is, and "how loud" is only true after loudnorm. Measured over the very
    #      same ranges, the raw take put the gate at -44.1 dBFS where the
    #      normalised cut puts it at -29.4, and the pass found 0.24s instead of
    #      the 9.76s that was really in there.
    #
    # An AUDIO-ONLY concat answers both in seconds, so the video is encoded once
    # instead of encoded, measured and encoded again.
    if (args.verify or args.breath_trim) and not args.no_cut and cut["ranges"] and wav.exists():
        print("\n[2.95] measuring the assembled audio")
        proxy = project / "cut_proxy.wav"
        fg = []
        for i, (ra, rb) in enumerate(cut["ranges"]):
            fg.append(f"[0:a]atrim=start={ra}:end={rb},asetpts=PTS-STARTPTS[p{i}];")
        fg.append("".join(f"[p{i}]" for i in range(len(cut["ranges"])))
                  + f"concat=n={len(cut['ranges'])}:v=0:a=1[c];")
        fg.append("[c]loudnorm=I=-14:TP=-1.5:LRA=11[out]"
                  if not args.no_normalize else "[c]anull[out]")
        (project / "proxy_fg.txt").write_text("\n".join(fg))
        util_mod.run(["ffmpeg", "-y", "-v", "error", "-i", str(wav),
                      "-filter_complex_script", str(project / "proxy_fg.txt"),
                      "-map", "[out]", "-ac", "1", "-ar", "16000",
                      "-c:a", "pcm_s16le", str(proxy)])

        cut_ranges, acc = [], 0.0
        for ra, rb in cut["ranges"]:
            cut_ranges.append([acc, acc + (rb - ra)])
            acc += rb - ra

        drop = []
        if args.breath_trim:
            spans, gate, spared = speech_mod.unvoiced_gaps(proxy, cut_ranges,
                                                           offset=args.breath_offset)
            drop += spans
            print(f"  breath-level dead air: {sum(y - x for x, y in spans):.2f}s "
                  f"in {len(spans)} stretch(es), gate {gate:.1f} dBFS")
            if spared:
                print(f"  left {sum(y - x for x, y in spared):.2f}s alone — {len(spared)} "
                      f"stretch(es) under the gate but voiced or on a join")

        if args.verify:
            # NO vocab here, deliberately. The glossary is an initial_prompt and
            # whisper obeys it by TIDYING what it hears: primed with a glossary it
            # decoded "There's a free open source editor" from audio that says
            # "There's a free there's a free open source editor", and this pass
            # reported the cut clean. Same file, same model, vocab the only
            # variable: 0 repeats with it, 2 without.
            vtx = transcribe_mod.transcribe_cut(
                proxy, project, model_size=args.model, vocab=None)
            reps = badtakes_mod.stutters(vtx["words"])
            if not reps:
                print("  no repeats in the assembled audio")
            for r in reps:
                print(f"  repeat at {r['start']:.2f}s: {r['text']!r}")
            if reps:
                spans, notes = badtakes_mod.repeat_spans(cut["ranges"], reps)
                drop += spans
                for n in notes:
                    print(f"  {n}")

        if drop and not args.no_repair:
            src_spans = []
            for x, y in drop:
                src_spans += badtakes_mod.map_to_source(cut["ranges"], x, y)
            fixed = badtakes_mod._subtract(cut["ranges"], src_spans)
            fixed = [r for r in fixed if r[1] - r[0] >= 0.12]
            fixed, orph = roughcut_mod.drop_orphan_ranges(fixed, transcript["words"])
            for o in orph:
                print(f"  dropped orphan range {o['range'][0]:.2f}-{o['range'][1]:.2f} "
                      f"— only {' '.join(o['words'])!r}")
            held = cut["kept_duration"]
            cut["ranges"] = fixed
            cut["kept_duration"] = round(sum(y - x for x, y in fixed), 3)
            cut["cuts"] = max(0, len(fixed) - 1)
            print(f"  {held:.2f}s -> {cut['kept_duration']:.2f}s before the encode")

    print("\n[4/4] assemble")
    music = args.music.expanduser().resolve() if args.music else None
    # Lands in ~/Downloads unless told otherwise. Not beside the input: defaulting to
    # video.parent auto-versioned an "<stem> edited v<N>.mp4" next to the source on
    # every run, and 11 of them piled up beside one Desktop file in a single session
    # before anyone noticed. The full project (transcript, cut plan, captions) still
    # lives under projects/<name>/ either way.
    if args.out_dir:
        out_dir = args.out_dir.expanduser().resolve()
    elif args.beside:
        out_dir = video.parent
    else:
        out_dir = Path.home() / "Downloads"
    out_dir.mkdir(parents=True, exist_ok=True)
    export_path = next_versioned_path(out_dir, video.stem)
    # The trim/concat runs over the FULL source and is the slowest step here, so it
    # is skipped when output.mp4 already matches the plan. That is what makes the
    # preview -> approve -> render loop cheap: the second run only re-burns.
    existing = project / "output.mp4"
    reusable = (existing.exists() and not args.fresh
                and abs(probe(existing)["duration"] - cut["kept_duration"]) < 0.25)
    if reusable:
        print(f"  reusing output.mp4 ({fmt_secs(cut['kept_duration'])}) — the cut is unchanged")
        result = {"project_out": existing, "exported_out": existing}
    else:
        result = assemble_mod.assemble(
            video, project, cut["ranges"],
            music=music, captions=captions_path, export_path=export_path,
            pre_gaps=cut.get("pre_gaps"), normalize=not args.no_normalize,
            fast=args.fast, geom=geo["chain"],
        )

    if caption_from_cut:
        print("\n[3/4] captions — transcribing the cut")
        # Reuse a cut transcript that's already here unless the cut changed length.
        # Captions get BURNED IN, so a word whisper got wrong is unfixable after the
        # fact — this is the escape hatch: edit cut_transcript.json by hand, re-run,
        # and the corrected word is what ships. --fresh forces a re-decode.
        cached_cut = project / "cut_transcript.json"
        cut_tx = None
        if cached_cut.exists() and not args.fresh:
            prev = load_json(cached_cut)
            if prev.get("words") and abs(prev.get("cut_duration", -1)
                                         - cut["kept_duration"]) < 0.05:
                cut_tx = prev
                print(f"  reusing cut_transcript.json ({len(prev['words'])} words) "
                      "— edit it by hand to fix a burned-in word; --fresh to re-decode")
        if cut_tx is None:
            cut_tx = transcribe_mod.transcribe_cut(
                result["project_out"], project, model_size=args.model, vocab=args.vocab)
            cut_tx["cut_duration"] = cut["kept_duration"]
            dump_json(cached_cut, cut_tx)
        if args.fix:
            cut_tx["words"], applied = transcribe_mod.apply_fixes(cut_tx["words"], args.fix)
            # whisper re-decodes the CUT each render, so a pattern written against a
            # previous decode silently stops matching. Say so rather than ship the
            # wrong word burned into the pixels.
            hit = {a.split(" -> ")[0].strip().lower() for a in applied}
            missed = [f for f in args.fix
                      if "=" in f and f.split("=", 1)[0].strip().lower() not in hit]
            if missed:
                print(f"  [!] {len(missed)} fix(es) matched nothing this run: "
                      + ", ".join(repr(m) for m in missed[:6]))
            for a in applied:
                print(f"  fixed: {a}")
        print(f"  {len(cut_tx['words'])} words: "
              + " ".join(w["word"] for w in cut_tx["words"])[:110] + "…")
        cut_dur = probe(result["project_out"])["duration"]
        # A word belongs to the CTA if its MIDPOINT is past the boundary. Testing
        # the start against a fixed epsilon breaks the moment the cut length shifts:
        # a 0.56s change moved "Comment" 0.02s to the wrong side of it and leaked a
        # stray caption word over the CTA card.
        body = ([w for w in cut_tx["words"]
                 if (w["start"] + w["end"]) / 2 < cta_at]
                if cta_at else cut_tx["words"])
        captions_path = write_captions(body, [[0.0, cut_dur]],
                                       geo["width"], geo["height"])

        # Title + CTA card share this encode rather than adding another pass.
        extra = None
        if args.title or args.cta_card:
            ass, mark = titlecard_mod.build(
                geo["width"], geo["height"], cut_dur,
                title=args.title or "", benefit=args.title_benefit,
                cta_keyword=args.cta_card, cta_line=args.cta_card_line,
                cta_start=cta_at, title_out=args.title_secs,
                variant=args.title_variant, credit=args.title_credit,
                title_y_pct=args.title_y, font=args.caption_font)
            (project / "title.ass").write_text(ass)
            extra = "subtitles=title.ass"
            if mark:
                name = titlecard_mod.stage_mark(mark, project)
                extra += "[vin];" + titlecard_mod.overlay_filter(mark, name)
            print(f"  title card: {args.title or args.cta_card}")

        # Preview: same filter chain, one frame, then stop. Re-running without the
        # flag reuses output.mp4 and cut_transcript.json, so approving costs only
        # the burn — nothing above this line is computed twice.
        if args.preview_at is not None:
            chain = f"subtitles={captions_path.name}"
            if extra:
                chain += extra if extra.lstrip().startswith(("[", ";")) else "," + extra
            still = args.preview_dir.expanduser() / f"PREVIEW-{project.name}.png"
            preview_mod.frame(result["project_out"], args.preview_at, still,
                              chain=chain, cwd=project)
            print(f"\n👁  preview only — nothing rendered\n   {still}\n"
                  f"   re-run without --preview-at to render for real")
            return 0

        result = assemble_mod.burn(result["project_out"], captions_path,
                                   export_path=export_path, fast=args.fast, extra=extra)
        print(f"  burned {captions_path.name}")

    out_info = probe(result["project_out"])
    print(f"\n✅ done — {fmt_secs(out_info['duration'])} (from {fmt_secs(info['duration'])})")
    print(f"   project : {result['project_out']}")
    print(f"   exported: {result['exported_out']}")

    total = sum(_costs)
    if total:
        print(f"   thinking: ~${total:.4f}")

    if not args.no_open:
        subprocess.run(["open", str(result["exported_out"])])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
