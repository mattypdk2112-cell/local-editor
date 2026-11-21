"""Usage log. Every run appends one line to usage-report.jsonl in the repo folder.

That file never leaves your machine on its own. ./report prints it in a readable
form you can copy to Matt. Only if you pasted an invite code from Matt into
./setup is the same line also sent to him.

What one line holds (and nothing else): the time, the editor's git version, your
OS, which flag NAMES you used (never their values), the input's length and frame
size, how long each stage took, whether it worked, and on a crash the error text
and the last few traceback lines with every path and filename scrubbed out. Never
footage, transcript text, file paths or filenames.

A send runs in a detached background process with a 4 second timeout, so a slow
or dead network can never slow down or break an edit.

Stop sending: delete .usage-reports.json, or set LOCAL_EDITOR_REPORTS=0.
Clear the log: rm usage-report.jsonl
"""
from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import sys
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / ".usage-reports.json"
LOG = ROOT / "usage-report.jsonl"
LAST = ROOT / "projects" / ".last-usage-report.json"
ENDPOINT = "https://kssffxnusvvexzoqsiev.supabase.co/functions/v1/local-editor-report"
TIMEOUT = 4.0

_run: dict | None = None
_stage: tuple[str, float] | None = None


def _invite() -> str | None:
    """The invite code from ./setup, or None. Without one nothing is ever sent."""
    if os.environ.get("LOCAL_EDITOR_REPORTS", "").strip() in ("0", "off", "no", "false"):
        return None
    try:
        code = str(json.loads(CONFIG.read_text()).get("invite") or "").strip()
    except Exception:
        return None
    return code[:80] if re.fullmatch(r"[A-Za-z0-9_-]{8,80}", code) else None


def _version() -> str:
    try:
        return subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, timeout=2).stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _flags(argv: list[str]) -> list[str]:
    # Flag names only. A value after a flag can be a path or a caption fix, so it is dropped.
    return sorted({a.split("=", 1)[0] for a in argv if a.startswith("--")})[:60]


_EXT = r"(?:mp4|mov|m4v|mkv|avi|mxf|wav|mp3|m4a|aac|flac|json|txt|srt|ass|png|jpe?g|heic)"
# A path to a media file may hold spaces ("/Users/x/Desktop/My Client take 2.mp4"), so
# that form is matched first, up to its extension.
_MEDIA_PATH = re.compile(r"""(?:~|/)[^\n'"]*?\.""" + _EXT + r"\b", re.I)
_PATH = re.compile(r"""(?:~|/|[A-Za-z]:\\)[^\s'",;:()\[\]]*""")
_FILE = re.compile(r"""[^\s'",;:()\[\]/]+\.""" + _EXT + r"\b", re.I)
_hidden: set[str] = set()


def hide(*values) -> None:
    """Never send these strings (argv values, recipe file names), whole or as a name or stem."""
    for v in values:
        try:
            v = str(v).strip()
            if len(v) < 3:
                continue
            q = Path(v).expanduser()
            for x in {v, str(q), str(q.resolve()), q.name, q.stem}:
                if len(x) >= 3:
                    _hidden.add(x)
        except Exception:
            pass


def scrub(text: str) -> str:
    """Drop anything that looks like a path, a filename, or a quoted string."""
    for v in sorted(_hidden, key=len, reverse=True):
        text = text.replace(v, "<hidden>")
    text = _MEDIA_PATH.sub("<path>", text)
    text = _PATH.sub("<path>", text)
    text = _FILE.sub("<file>", text)
    text = re.sub(r"'[^']{2,}'|\"[^\"]{2,}\"", "<text>", text)
    for v in {os.path.expanduser("~"), os.environ.get("USER", "")}:
        if v and len(v) > 2:
            text = text.replace(v, "<user>")
    return text


def _tb_lines(exc: BaseException) -> list[str]:
    """Last 8 frames as 'src/module.py:123 in func'. Repo source names only, never yours."""
    out = []
    for fr in traceback.extract_tb(exc.__traceback__)[-8:]:
        try:
            where = Path(fr.filename).resolve().relative_to(ROOT).as_posix()
        except Exception:
            where = "<outside editor>"
        out.append(f"{where}:{fr.lineno} in {fr.name}")
    return out


def start(tool: str, argv: list[str]) -> None:
    global _run
    hide(*[a.split("=", 1)[1] if a.startswith("--") else a
           for a in argv if not a.startswith("--") or "=" in a])
    _run = {"run_id": str(uuid.uuid4()), "tool": tool,
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "version": _version(),
            "os": f"{platform.system()} {platform.release()} {platform.machine()}",
            "python": platform.python_version(), "flags": _flags(argv), "stages": {},
            "input_duration_s": None, "input_width": None, "input_height": None,
            "output_duration_s": None, "_t0": time.monotonic()}


def note(**kv) -> None:
    if _run is not None:
        for k, v in kv.items():
            if k in ("input_duration_s", "input_width", "input_height", "output_duration_s", "clips"):
                _run[k] = round(float(v), 2) if isinstance(v, float) else v


def stage(name: str) -> None:
    """Mark the start of a stage; the previous one ends here."""
    global _stage
    if _run is None:
        return
    now = time.monotonic()
    if _stage:
        prev, t = _stage
        _run["stages"][prev] = round(_run["stages"].get(prev, 0) + now - t, 2)
    _stage = (name, now) if name else None


def finish(exit_code: int, exc: BaseException | None = None) -> None:
    """Close the run, append it to the local log, and send it only with an invite. Never raises."""
    global _run
    try:
        if _run is None:
            return
        stage("")
        run, _run = _run, None
        run["total_s"] = round(time.monotonic() - run.pop("_t0"), 2)
        run["ok"] = exc is None and exit_code == 0
        run["exit_code"] = int(exit_code)
        run["error"] = None
        run["traceback"] = []
        if exc is not None and not isinstance(exc, KeyboardInterrupt):
            run["error"] = scrub(f"{type(exc).__name__}: {exc}")[:400]
            run["traceback"] = _tb_lines(exc)
        elif isinstance(exc, KeyboardInterrupt):
            run["error"] = "KeyboardInterrupt"
        try:
            with LOG.open("a") as f:
                f.write(json.dumps(run) + "\n")
        except Exception:
            pass
        code = _invite()
        if not code:
            return
        p = subprocess.Popen([sys.executable, "-I", str(Path(__file__)), "--send"],
                             stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        p.stdin.write(json.dumps({"invite": code, **run}).encode())
        p.stdin.close()
    except Exception:
        pass


def wrap(tool: str, main) -> int:
    """Run main() with a report around it. The edit's own result and errors pass through."""
    try:
        start(tool, sys.argv[1:])
    except Exception:
        pass
    try:
        code = main()
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        finish(code)
        raise
    except BaseException as e:
        finish(1, e)
        raise
    finish(code or 0)
    return code


def _send() -> None:
    import urllib.request
    body = sys.stdin.buffer.read()
    shown = {k: v for k, v in json.loads(body).items() if k != "invite"}
    status: dict = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    try:
        req = urllib.request.Request(ENDPOINT, data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            status["http"] = r.status
            status["reply"] = r.read(2000).decode("utf-8", "replace")
    except Exception as e:
        status["error"] = type(e).__name__
    try:
        LAST.parent.mkdir(parents=True, exist_ok=True)
        LAST.write_text(json.dumps({"sent": shown, "result": status}, indent=1))
    except Exception:
        pass


if __name__ == "__main__" and sys.argv[1:] == ["--send"]:
    _send()
