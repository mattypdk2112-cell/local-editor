"""Build a multi-clip reel from a recipe. See recipes/README.md.

  ./reel recipe.json sections                  join the pieces -> "<name> sections.mp4" + cuts.json
  ./reel recipe.json finish                    text, sounds, music, end card -> "<name> vN.mp4"
  ./reel recipe.json finish --body graded.mp4  finish a file you colour-graded in between
  ./reel recipe.json all                       both steps in one go
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from src import sequence


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("recipe", type=Path)
    p.add_argument("step", choices=["sections", "finish", "all"])
    p.add_argument("--body", type=Path, default=None,
                   help="the file to finish (default: the sections output)")
    p.add_argument("--version", default=None, help="e.g. v3 (default: next free number)")
    p.add_argument("--out", type=Path, default=None, help="write here instead of the recipe's out folder")
    a = p.parse_args()
    r = sequence.load(a.recipe, out=a.out)
    joined = r["_out"] / f"{r['name']} sections.mp4"
    if a.step in ("sections", "all"):
        print("[sections]")
        joined = sequence.sections(r)
    if a.step in ("finish", "all"):
        print("[finish]")
        sequence.finish(r, a.body or joined, a.version)
    return 0


if __name__ == "__main__":
    sys.exit(main())
