"""Stage 1 — transcribe.

Extract 16k mono audio, run faster-whisper with word-level timestamps, emit a
flat word list + segment text. No PyTorch: faster-whisper (CTranslate2) only.
"""
from __future__ import annotations

import re
from pathlib import Path

from .util import run

# --- one model per process (2026-09-07) --------------------------------------
# A single run loads the speech model four separate times: the source pass, the
# stretched-word repair, and once per verify pass over the assembled cut. Each
# load costs ~20s on an M1, so ~80s of a ~9 minute run is spent building the same
# object again. It is stateless for our purposes, so build it once and hand the
# same one out.
_MODELS: dict[str, object] = {}


def get_model(model_size: str = "small"):
    from faster_whisper import WhisperModel

    if model_size not in _MODELS:
        _MODELS[model_size] = WhisperModel(model_size, device="cpu", compute_type="int8")
    return _MODELS[model_size]



def extract_audio(video: Path, out_wav: Path) -> Path:
    run([
        "ffmpeg", "-y", "-i", str(video),
        "-vn", "-ac", "1", "-ar", "16000",
        "-c:a", "pcm_s16le", str(out_wav),
    ])
    return out_wav


def transcribe(video: Path, project: Path, *, model_size: str = "small", language: str = "en",
               vocab: list[str] | None = None) -> dict:
    """Return {words: [{word,start,end,prob}], text, segments}.

    `vocab` = proper nouns / brand / CTA words the decoder keeps mangling on quiet
    audio (e.g. YAMAL heard as "your mile", a full report as "four report"). We
    pass them as whisper's `initial_prompt` so the model is primed to spell them
    right — critical before captions are ever turned on.
    """
    from faster_whisper import WhisperModel  # imported lazily; lives only in the venv

    wav = extract_audio(video, project / "audio.wav")

    print(f"  loading faster-whisper '{model_size}' (int8, cpu)…")
    model = WhisperModel(model_size, device="cpu", compute_type="int8")

    terms = [t.strip() for t in (vocab or []) if t and t.strip()]
    initial_prompt = ("Glossary: " + ", ".join(terms) + ".") if terms else None
    if initial_prompt:
        print(f"  vocab hint: {', '.join(terms)}")

    print("  transcribing…")
    segments, info = model.transcribe(
        str(wav),
        language=language,
        word_timestamps=True,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 300},
        beam_size=5,
        initial_prompt=initial_prompt,
    )

    words: list[dict] = []
    seg_out: list[dict] = []
    for seg in segments:
        seg_out.append({"start": round(seg.start, 3), "end": round(seg.end, 3), "text": seg.text.strip()})
        for w in (seg.words or []):
            token = w.word.strip()
            if not token:
                continue
            words.append({
                "word": token,
                "start": round(w.start, 3),
                "end": round(w.end, 3),
                "prob": round(getattr(w, "probability", 0.0), 3),
            })

    result = {
        "language": info.language,
        "duration": round(info.duration, 3),
        "text": " ".join(s["text"] for s in seg_out).strip(),
        "segments": seg_out,
        "words": words,
    }
    print(f"  {len(words)} words, {len(seg_out)} segments transcribed")
    return result


def transcribe_cut(video: Path, project: Path, *, model_size: str = "small",
                   language: str = "en", vocab: list[str] | None = None) -> dict:
    """Transcribe an ALREADY-ASSEMBLED cut. Word times are output times.

    Captioning from the source transcript needs a remap (`captions.build_timemap`),
    and that remap keys off whisper's LOGGED word start. Whisper's timestamps drift
    from the real onset by up to ~0.8s on quiet audio, so once ranges are tightened
    to the waveform a word can be plainly audible inside a kept range while its
    logged start sits outside it — and the remap silently drops it. That is how a
    finished reel shipped missing "Premiere" and an entire sentence.

    Transcribing the cut removes the remap, and therefore the whole failure mode.
    It also means brand names are re-decoded against what actually ships, instead
    of inheriting "Capcut"/"quad-code" from the source pass and burning them in.

    VAD is off here: the cut has already had its silence removed, so VAD's only
    remaining effect would be to fuse words across the joins.

    `vocab` is an initial_prompt, and whisper does not treat it as a spelling hint
    only — it tidies what it hears to match. Primed with Matt's glossary it decoded
    "There's a free open source editor" from audio that says "There's a free
    there's a free open source editor" (measured 2026-09-07, same file and model,
    vocab the only variable: 0 repeats with it, 2 without). So pass vocab when you
    are about to BURN the words in, and pass None when you are checking whether the
    cut is clean. Those are different jobs and they want different settings.
    """
    wav = project / "cut_audio.wav"
    run(["ffmpeg", "-y", "-v", "error", "-i", str(video),
         "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)])

    model = get_model(model_size)
    terms = [t.strip() for t in (vocab or []) if t and t.strip()]
    segments, _ = model.transcribe(
        str(wav), language=language, word_timestamps=True,
        vad_filter=False, condition_on_previous_text=False, beam_size=5,
        initial_prompt=("Glossary: " + ", ".join(terms) + ".") if terms else None)

    words: list[dict] = []
    seg_out: list[dict] = []
    for seg in segments:
        seg_out.append({"start": round(seg.start, 3), "end": round(seg.end, 3),
                        "text": seg.text.strip()})
        for w in (seg.words or []):
            token = w.word.strip()
            if token:
                words.append({"word": token, "start": round(float(w.start), 3),
                              "end": round(float(w.end), 3),
                              "prob": round(float(w.probability), 3)})
    return {"words": words, "segments": seg_out,
            "text": " ".join(s["text"] for s in seg_out)}


def apply_fixes(words: list[dict], pairs: list[str]) -> tuple[list[dict], list[str]]:
    """Rewrite mis-heard PHRASES in a cut transcript. Returns (words, applied).

    Captions are burned in, so a word whisper got wrong is unfixable once rendered.
    `--vocab` primes the decoder but does not guarantee it — on this project it
    still produced "made with code code" for "made with Claude Code", and "How do I
    have to open" for "I don't have to open".

    Matching is phrase-level, not word-level, because the failures are: a brand
    name only reads as wrong in context ("code" alone is a real word), and the
    replacement often has a different word count than what was heard. The matched
    span's time range is re-divided across the replacement words, so caption timing
    stays aligned no matter how the counts differ.

    Each pair is "heard=actual", compared case- and punctuation-insensitively.
    """
    def norm(s: str) -> str:
        return re.sub(r"[^a-z0-9' ]", "", s.lower()).strip()

    rules = []
    for p in pairs:
        if "=" not in p:
            continue
        heard, actual = p.split("=", 1)
        heard_toks = norm(heard).split()
        if heard_toks:
            rules.append((heard_toks, actual.strip().split()))
    if not rules:
        return words, []

    out: list[dict] = []
    applied: list[str] = []
    i = 0
    while i < len(words):
        for heard_toks, actual_toks in rules:
            n = len(heard_toks)
            if i + n > len(words):
                continue
            window = [norm(w["word"]) for w in words[i : i + n]]
            if window != heard_toks:
                continue
            start = words[i]["start"]
            end = words[i + n - 1]["end"]
            step = (end - start) / max(1, len(actual_toks))
            for k, tok in enumerate(actual_toks):
                out.append({"word": tok,
                            "start": round(start + k * step, 3),
                            "end": round(start + (k + 1) * step, 3),
                            "prob": 1.0})
            applied.append(f"{' '.join(heard_toks)} -> {' '.join(actual_toks)}")
            i += n
            break
        else:
            out.append(words[i])
            i += 1
    return out, applied


# --- transcript repair (2026-09-07) -----------------------------------------
# A word cannot take 1.6 seconds to say when it is four letters long. When whisper
# logs one that does, it has not slowed down — it has SWALLOWED something, usually
# a repeated attempt, and emitted the region as a single stretched token.
#
# Measured on Matt's 2026-09-06 editor take: 425 words, median duration 0.24s, p90
# 0.52s, and 14 words (3%) over 0.85s — every one of them a short function word
# ("and", "I", "on", "or", "so", "open"). Those 14 are not a curiosity, they are
# where every bad cut in that reel came from. "open" logged 34.13-35.75 is the
# 1.68s of a previous take that shipped as "There's a free there's a free open
# source editor"; "music" logged at 117.96 is why the wrong take of the music line
# was chosen; "I" at 40.86 is the "but I / I took one" false start.
#
# Re-transcribing just that window, on its own, returns the truth: isolated,
# 32.5-39.5 decodes cleanly and puts the good take's first word at 34.84 rather
# than 33.16. Whisper is accurate on a short window and unreliable across a long
# one, so the repair is to ask it again, narrowly.
STRETCHED_S = 0.85          # a word logged longer than this is hiding something
REPAIR_PAD = 0.25           # context either side so the window decodes in context


def _norm_tok(word: str) -> str:
    return re.sub(r"[^a-z0-9']", "", word.lower())


def stretched_words(words: list[dict], *, limit: float = STRETCHED_S) -> list[int]:
    """Indices of words whose logged duration is not physically plausible."""
    return [i for i, w in enumerate(words) if (w["end"] - w["start"]) > limit]


def repair_stretched(words: list[dict], wav: Path, *, model_size: str = "small",
                     language: str = "en", limit: float = STRETCHED_S,
                     pad: float = REPAIR_PAD) -> tuple[list[dict], list[dict]]:
    """Re-decode each stretched word's window on its own. (words, report).

    No initial_prompt and no VAD: this pass exists to hear what is really in the
    audio, and a glossary makes whisper tidy a repeat away (see `transcribe_cut`).
    """
    bad = stretched_words(words, limit=limit)
    if not bad:
        return words, []

    model = get_model(model_size)
    out = list(words)
    report: list[dict] = []
    for i in reversed(bad):                       # right to left: indices stay valid
        w = out[i]
        a = max(0.0, w["start"] - pad)
        b = w["end"] + pad
        seg_words = _decode_window(model, wav, a, b, language)
        inner = [x for x in seg_words
                 if x["start"] >= w["start"] - pad / 2 and x["end"] <= w["end"] + pad / 2]
        # The re-decode must still contain the word it is splitting. A stretched
        # token hides EXTRA words around the one whisper logged, so the original
        # has to survive; when it does not, the window was quiet and the decoder
        # invented something. That is how "for watching." — a YouTube sign-off
        # that is nowhere in the audio — replaced 'on' at 27.25s and entered the
        # transcript. Measured 2026-09-07.
        target = _norm_tok(w["word"])
        if len(inner) > 1 and target and target not in {_norm_tok(x["word"]) for x in inner}:
            report.append({"at": round(w["start"], 2), "was": w["word"],
                           "dur": round(w["end"] - w["start"], 2),
                           "now": " ".join(x["word"] for x in inner),
                           "rejected": True})
            continue
        if len(inner) > 1:
            report.append({"at": round(w["start"], 2), "was": w["word"],
                           "dur": round(w["end"] - w["start"], 2),
                           "now": " ".join(x["word"] for x in inner)})
            out[i:i + 1] = inner
    return out, list(reversed(report))


def _decode_window(model, wav: Path, a: float, b: float, language: str) -> list[dict]:
    """Whisper on [a, b) of `wav`, returned on the ORIGINAL timeline."""
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
        clip = Path(tf.name)
    run(["ffmpeg", "-y", "-v", "error", "-ss", f"{a:.3f}", "-to", f"{b:.3f}",
         "-i", str(wav), "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(clip)])
    # beam 5, deliberately. beam 1 is 31% faster on the whole file (61.5s against
    # 88.9s, measured) but these windows are short and quiet, which is exactly
    # where a greedy decode hallucinates: at beam 1 the window around 'on' at
    # 27.25s came back as "for watching.", a YouTube sign-off that is nowhere in
    # the audio, and it went into the transcript. The repair is cached now, so it
    # is paid once per project and the speed is not worth the risk.
    segments, _ = model.transcribe(str(clip), language=language, word_timestamps=True,
                                   vad_filter=False, beam_size=5,
                                   condition_on_previous_text=False)
    out = []
    for seg in segments:
        for x in (seg.words or []):
            tok = x.word.strip()
            if tok:
                out.append({"word": tok, "start": round(a + float(x.start), 3),
                            "end": round(a + float(x.end), 3),
                            "prob": round(float(x.probability or 0), 3)})
    clip.unlink(missing_ok=True)
    return out
