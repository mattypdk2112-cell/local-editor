"""One LLM backend for the whole editor.

Two of the flags need a model to think: `--bad-takes` (which of these takes is the
keeper) and `--draft` (put my own sentences in the best order). Everything else in
this repo is pure ffmpeg and whisper and never leaves the machine.

The default brain is the Claude Code you already installed to set this up. No API
key, no signup, no second subscription. If you have an OpenRouter or Gemini key
exported, that wins instead, because setting one is an explicit choice.

Every backend here does the same job: take a prompt, return parsed JSON plus a
usage dict. Callers never care which one answered.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import httpx

GEMINI_BASE = "https://generativelanguage.googleapis.com"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

OPENROUTER_KEYS = ("OPENROUTER_API_KEY", "OPEN_ROUTER", "OPENROUTER_KEY", "OPENROUTER")
GEMINI_KEYS = ("GEMINI_API_KEY", "GEMINI_KEY")

DEFAULT_OR_MODEL = "anthropic/claude-sonnet-5"
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
# Sonnet, not whatever the user's interactive session happens to be set to. This is
# a text-reordering job; Opus would spend their plan limits for no better answer.
DEFAULT_CLAUDE_MODEL = "sonnet"

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)

# Claude Code is an agent, not a completion endpoint. Left alone it will try a tool
# call first and then hand back the answer wrapped in an apology and a code fence.
# This tells it to behave like an API. _extract_json still assumes it won't.
JSON_ONLY_SYSTEM = (
    "You are a JSON API. Reply with one raw JSON object and absolutely nothing else: "
    "no preamble, no explanation, no markdown code fences. Do not use any tools. "
    "Everything you need is in the message."
)


def load_env_key(name: str, *, project_root) -> str | None:
    """os.environ first, then a bare parse of <repo>/.env (no dependency)."""
    if os.getenv(name):
        return os.getenv(name)
    env_file = Path(project_root) / ".env"
    if not env_file.exists():
        return None
    try:
        lines = env_file.read_text().splitlines()
    except OSError:
        return None
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() == name:
            return v.strip().strip('"').strip("'")
    return None


def claude_cli() -> str | None:
    """Path to the Claude Code binary, or None if it isn't installed."""
    return shutil.which("claude")


def resolve_backend(project_root, *, prefer_model: str | None = None) -> dict | None:
    """Pick who does the thinking. An explicit key beats the bundled default.

    Returns {name, key?, model} or None when there is nothing to call.
    """
    for name in OPENROUTER_KEYS:
        k = load_env_key(name, project_root=project_root)
        if k:
            return {"name": "openrouter", "key": k,
                    "model": prefer_model or DEFAULT_OR_MODEL}
    for name in GEMINI_KEYS:
        k = load_env_key(name, project_root=project_root)
        if k:
            return {"name": "gemini", "key": k, "model": DEFAULT_GEMINI_MODEL}
    if claude_cli():
        return {"name": "claude_cli", "model": DEFAULT_CLAUDE_MODEL}
    return None


def no_backend_message() -> str:
    """One sentence, no traceback, when nothing can answer."""
    return ("this flag needs a model to think and none is available. Install Claude Code "
            "(claude.com/claude-code) and this works with no key, or export a "
            "GEMINI_API_KEY / OPENROUTER_API_KEY")


def describe(backend: dict) -> str:
    if backend["name"] == "claude_cli":
        return f"claude code ({backend['model']})"
    return f"{backend['name']} ({backend['model']})"


def _extract_json(text: str) -> dict:
    """Get the JSON object out of a model reply, however it chose to dress it up.

    Tried in order: the whole thing, whatever is inside a code fence, then the
    outermost braces. Models wrap answers in prose and fences no matter how firmly
    the prompt says not to, and a whole edit should not be lost to a stray sentence.
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("model returned an empty reply")

    candidates = [text]
    fenced = _FENCE_RE.search(text)
    if fenced:
        candidates.append(fenced.group(1).strip())
    a, b = text.find("{"), text.rfind("}")
    if a != -1 and b > a:
        candidates.append(text[a:b + 1])

    for c in candidates:
        try:
            parsed = json.loads(c)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise ValueError(f"no JSON object in the reply: {text[:160]}")


def _call_claude_cli(prompt: str, *, model: str, timeout: float) -> tuple[dict, dict]:
    """Run the local Claude Code in headless mode.

    The prompt goes in on stdin, not argv, because transcripts are far longer than a
    command line is allowed to be. MCP servers are switched off because this is a
    text transform and loading someone's whole toolbelt only makes it slower.
    """
    cmd = [
        claude_cli() or "claude", "-p",
        "--output-format", "json",
        "--model", model,
        "--append-system-prompt", JSON_ONLY_SYSTEM,
        "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
    ]
    try:
        proc = subprocess.run(cmd, input=prompt, capture_output=True,
                              text=True, timeout=timeout)
    except FileNotFoundError:
        raise RuntimeError("claude code is not on PATH") from None
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"claude code did not answer within {int(timeout)}s") from None
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:200]
        raise RuntimeError(f"claude code exited {proc.returncode}: {detail}")

    try:
        env = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"claude code returned no JSON: {proc.stdout[:200]}") from None
    if env.get("is_error"):
        raise RuntimeError(f"claude code errored: {str(env.get('result'))[:200]}")

    usage = env.get("usage", {}) or {}
    usage["_cost_usd"] = env.get("total_cost_usd")
    return _extract_json(env.get("result", "")), usage


def _call_openrouter(prompt: str, *, api_key: str, model: str,
                     timeout: float) -> tuple[dict, dict]:
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
        "temperature": 0.4,
    }
    with httpx.Client(timeout=httpx.Timeout(timeout, connect=15.0)) as c:
        r = c.post(OPENROUTER_URL, headers={"Authorization": f"Bearer {api_key}"}, json=body)
        if r.status_code >= 300:
            raise RuntimeError(f"openrouter {r.status_code}: {r.text[:200]}")
        data = r.json()
    return _extract_json(data["choices"][0]["message"]["content"]), data.get("usage", {}) or {}


def _call_gemini(prompt: str, *, api_key: str, model: str,
                 timeout: float, temperature: float = 0.4) -> tuple[dict, dict]:
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"response_mime_type": "application/json",
                             "temperature": temperature},
    }
    with httpx.Client(timeout=httpx.Timeout(timeout, connect=15.0)) as c:
        r = c.post(f"{GEMINI_BASE}/v1beta/models/{model}:generateContent",
                   params={"key": api_key}, json=body)
        if r.status_code >= 300:
            raise RuntimeError(f"gemini {r.status_code}: {r.text[:200]}")
        data = r.json()
    return (_extract_json(data["candidates"][0]["content"]["parts"][0]["text"]),
            data.get("usageMetadata", {}) or {})


def complete_json_retry(backend: dict, prompt: str, *, tries: int = 2,
                        **kw) -> tuple[dict, dict]:
    """`complete_json`, but a timeout gets one more go before we give up.

    The take-selection call is the difference between a 1:06 cut and a 1:43 one
    full of retakes, and on 2026-09-07 it timed out once at 300s and the run
    quietly continued with the worse cut. A single retry is cheap; silently
    shipping the degraded edit is not.
    """
    last = None
    for attempt in range(1, tries + 1):
        try:
            return complete_json(backend, prompt, **kw)
        except RuntimeError as e:
            last = e
            if "did not answer within" not in str(e) or attempt == tries:
                raise
            print(f"  {e} — retrying ({attempt + 1}/{tries})")
    raise last


def complete_json(backend: dict, prompt: str, *, timeout: float = 600.0,
                  temperature: float = 0.4) -> tuple[dict, dict]:
    """Send a prompt, get back (parsed JSON, usage). Raises RuntimeError on failure."""
    name = backend["name"]
    if name == "claude_cli":
        return _call_claude_cli(prompt, model=backend["model"], timeout=timeout)
    if name == "openrouter":
        return _call_openrouter(prompt, api_key=backend["key"], model=backend["model"],
                                timeout=timeout)
    if name == "gemini":
        return _call_gemini(prompt, api_key=backend["key"], model=backend["model"],
                            timeout=timeout, temperature=temperature)
    raise RuntimeError(f"unknown backend: {name}")


def estimate_cost(usage: dict, backend_name: str) -> float:
    """Out-of-pocket dollars for this call, which is what a user actually wants to know.

    Zero for Claude Code: it runs on the plan they already pay for, so surfacing the
    API-rate equivalent it reports (~$0.25 a pass, mostly system-prompt overhead) would
    read as a bill that never arrives. The APIs below are billed per call, so those
    are priced for real.
    """
    if backend_name == "claude_cli":
        return 0.0
    if backend_name == "openrouter":  # Claude Sonnet 5 list: ~$3/1M in, $15/1M out
        pt = int(usage.get("prompt_tokens") or 0)
        ct = int(usage.get("completion_tokens") or 0)
        return round(pt * 3.0e-6 + ct * 15.0e-6, 6)
    # gemini-2.5-flash text rates: $0.30/1M in, $2.50/1M out (incl thinking)
    pt = int(usage.get("promptTokenCount") or 0)
    ct = int(usage.get("candidatesTokenCount") or 0)
    tt = int(usage.get("thoughtsTokenCount") or 0)
    return round(pt * 0.30e-6 + (ct + tt) * 2.50e-6, 6)
