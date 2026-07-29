"""Stage 1 — transcribe.

Extract 16k mono audio, run faster-whisper with word-level timestamps, emit a
flat word list + segment text. No PyTorch: faster-whisper (CTranslate2) only.
"""
from __future__ import annotations

import re
from pathlib import Path

from .util import run


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
    """
    from faster_whisper import WhisperModel

    wav = project / "cut_audio.wav"
    run(["ffmpeg", "-y", "-v", "error", "-i", str(video),
         "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)])

    model = WhisperModel(model_size, device="cpu", compute_type="int8")
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
