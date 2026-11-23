"""./report: summarise usage-report.jsonl, the local log every ./edit and ./reel run writes."""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

LOG = Path(__file__).resolve().parent.parent / "usage-report.jsonl"


def main() -> int:
    if not LOG.exists():
        print("No runs logged yet. Run ./edit first.")
        return 0
    lines = [ln for ln in LOG.read_text().splitlines() if ln.strip()]
    if sys.argv[1:] == ["--raw"]:
        print("\n".join(lines))
        return 0
    runs = []
    for ln in lines:
        try:
            runs.append(json.loads(ln))
        except Exception:
            pass
    if not runs:
        print("The log is empty.")
        return 0
    ok = [r for r in runs if r.get("ok")]
    tot = [r["total_s"] for r in runs if isinstance(r.get("total_s"), (int, float))]
    stages: dict[str, list[float]] = defaultdict(list)
    for r in ok:
        for k, v in (r.get("stages") or {}).items():
            stages[k].append(v)
    flags: dict[str, int] = defaultdict(int)
    for r in runs:
        for f in r.get("flags") or []:
            flags[f] += 1
    first, last = runs[0], runs[-1]
    print("Local editor usage report (copy everything below to Matt)")
    print("-" * 56)
    print(f"runs: {len(runs)} ({len(ok)} worked, {len(runs) - len(ok)} failed)")
    print(f"from {first.get('ts', '?')} to {last.get('ts', '?')}")
    print(f"editor version: {last.get('version', '?')}   os: {last.get('os', '?')}   python: {last.get('python', '?')}")
    if tot:
        print(f"time per run: average {sum(tot) / len(tot):.1f}s, longest {max(tot):.1f}s")
    if stages:
        print("average seconds per stage (runs that worked):")
        for k, v in sorted(stages.items(), key=lambda kv: -sum(kv[1]) / len(kv[1])):
            print(f"  {k:<12} {sum(v) / len(v):6.1f}s  ({len(v)} runs)")
    if flags:
        print("flags used: " + ", ".join(f"{k} x{n}" for k, n in sorted(flags.items(), key=lambda kv: -kv[1])))
    print("last 5 runs:")
    for r in runs[-5:]:
        size = f"{r.get('input_width')}x{r.get('input_height')}" if r.get("input_width") else "?"
        dur = r.get("input_duration_s")
        print(f"  {r.get('ts', '?')}  {r.get('tool', '?'):<4} {'ok  ' if r.get('ok') else 'FAIL'} "
              f"{r.get('total_s', '?')}s  input {size} {dur if dur is not None else '?'}s")
    fails = [r for r in runs if not r.get("ok") and r.get("error")]
    if fails:
        f = fails[-1]
        print(f"last crash ({f.get('ts', '?')}, version {f.get('version', '?')}):")
        print("  " + str(f.get("error")).replace("\n", "\n  "))
        for t in f.get("traceback") or []:
            print("    " + t)
    print("-" * 56)
    print("Nothing in here is footage, transcript text, a file name or a path.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
