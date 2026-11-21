"""Opt-in usage reports. Off unless you typed a name into ./setup.

What one report holds (and nothing else): your tester name, the time, the editor's
git version, your OS, which flag NAMES you used (never their values), the input's
length and frame size, how long each stage took, whether it worked, and on a crash
the error text and the last few traceback lines with every path and filename
scrubbed out. Never footage, transcript text, file paths or filenames.

The send runs in a detached background process with a 4 second timeout, so a slow
or dead network can never slow down or break an edit.

Switch it off: delete .usage-reports.json, or set LOCAL_EDITOR_REPORTS=0.
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
LAST = ROOT / "projects" / ".last-usage-report.json"
ENDPOINT = "https://kssffxnusvvexzoqsiev.supabase.co/functions/v1/local-editor-report"
TIMEOUT = 4.0

_run: dict | None = None
_stage: tuple[str, float] | None = None


def _config() -> dict | None:
    if os.environ.get("LOCAL_EDITOR_REPORTS", "").strip() in ("0", "off", "no", "false"):
        return None
    try:
        cfg = json.loads(CONFIG.read_text())
    except Exception:
        return None
    name = str(cfg.get("name") or "").strip()
    if not cfg.get("enabled") or not name:
        return None
    return {"name": name[:40]}


def _version() -> str:
    try:
        return subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, timeout=2).stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _flags(argv: list[str]) -> list[str]:
    # Flag names only. A value after a flag can be a path or a caption fix, so it is dropped.
    return sorted({a.split("=", 1)[0] for a in argv if a.startswith("--")})[:60]


_PATH = re.compile(r"""(?:~|/|[A-Za-z]:\\)[^\s'",;:()\[\]]*""")
_FILE = re.compile(r"""[^\s'",;:()\[\]/]+\.(?:mp4|mov|m4v|mkv|avi|mxf|wav|mp3|m4a|aac|flac|json|txt|srt|ass|png|jpe?g|heic)\b""", re.I)


def scrub(text: str) -> str:
    """Drop anything that looks like a path, a filename, or a quoted string."""
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
    cfg = _config()
    if not cfg:
        return
    _run = {"run_id": str(uuid.uuid4()), "tester": cfg["name"], "tool": tool,
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
    """Close the run and hand the report to a background sender. Never raises."""
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
        p = subprocess.Popen([sys.executable, "-I", str(Path(__file__)), "--send"],
                             stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        p.stdin.write(json.dumps(run).encode())
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
        LAST.write_text(json.dumps({"sent": json.loads(body), "result": status}, indent=1))
    except Exception:
        pass


if __name__ == "__main__" and sys.argv[1:] == ["--send"]:
    _send()
