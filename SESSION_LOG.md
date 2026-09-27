# SESSION_LOG

## 2026-09-27: public repo made ready for a friend's scripted edits

**Goal:** a friend clones this and uses it for two jobs: (1) he has a script plus a ~5 min raw
clip and wants the best take of each line cut together, (2) no script, find the best parts.
Both already existed (`--script`, `--draft`) but the README never mentioned `--script`.

### Changed (uncommitted work from this session, now committed)
- `setup`: if Homebrew is missing, it downloads a static ffmpeg + ffprobe into `~/.local/bin`
  (martin-riedl.de latest release). Homebrew needs an admin password, which stalls a Claude Code
  install. Tested with a clean HOME and a bare PATH: worked.
- `src/llm.py`: `claude_cli()` also finds Claude Code inside the Claude desktop app
  (`~/Library/Application Support/Claude/claude-code/<ver>/claude.app/Contents/MacOS/claude`).
  Before this a desktop-app-only user silently got no retake cutter. Tested.
- `setup`: no longer warns "Claude Code isn't installed" to desktop-app users.
- `edit.py`: a missing `--script` file is now an error (it used to silently do a normal cut).
- `src/assemble.py`: ported the private repo's audio-overhang fix (`aresample,apad` + `-shortest`).
  The private repo's "fixed floor" carve fix does NOT apply: public speech.py has no adaptive floor.
- `README.md`: rewritten around the two jobs, corrected defaults (retake cutter is ON by default),
  documented `story.txt` re-run loop, `--height`, `--no-normalize`, no Premiere/CapCut timeline export.

### NOT verified
The end-to-end `--script` test on a real 6:18 take (`/Volumes/My SSDIH/Miscellaneous/Tlek /C2286.MP4`,
the Lyra poker ramble, test script in the session scratchpad) was killed mid-transcribe. The Mac was
at load 32 on 8 cores from a parallel Remotion render, so there is also no honest runtime number yet.

### Open loops / next step
1. Re-run the script-match test on an idle machine from a fresh clone:
   `./edit <raw> --script script.txt --height 720 --no-open`. Check coverage, watch the output, time it.
   Test `--draft --target 45` on the same clip.
2. Only after both pass: write the friend a short how-to message (install prompt for Claude Code,
   the two commands, how to ask Claude to fix a cut by editing script.txt / story.txt).
3. Unknown: is the friend on Mac or Windows? Windows is unsupported (WSL only). Ask Matt.

## 2026-09-28: runs natively on Windows, script cut verified on the real take

The friend is on **Windows**, so this was tested on Matt's Windows 10 PC, not the Mac.

**Built / changed:**
- `setup.cmd` / `setup.ps1` / `edit.cmd`: native Windows launchers. ffmpeg from gyan.dev into
  `~\.local\bin`, Python via `py` or winget, no admin prompt. `PYTHONUTF8=1` set in `edit.cmd`.
- PATH `os.pathsep`, Windows font dirs, `os.startfile`, `claude.exe` lookup.
- `util.filter_script_flag()`: ffmpeg 9 removed `-filter_complex_script`, which crashed every render.
  Mac setup also downloads "latest", so Mac users would have hit it too.
- Script cut fixes in `scriptmatch.py` + `edit.py` (commit 6ee30aa): the script vetoes retake spans that
  delete script words, ties go to the later take, a match extends past a mid-line stumble.

**Verified:** `edit.cmd <Lyra C2076, 6:18 4K> --script <36 lines from FINAL.mp4> --height 720`.
36/36 lines, in order, no repeats, 94.9s (hand edit 101s), picture = audio within 2ms.
v1 had lost a whole beat and "there was more to", v2 said "Nah, I'm playing" twice, v3 is clean.
Runtime on this PC: first run ~13 min (transcribe + Claude retake pass + 4K encode), re-runs ~8-10 min.

**Not verified:** `--draft --target 45`. Killed by Claude Code for low system memory (16GB box,
nothing wrong with the command). Re-run with other apps closed.

**Next:** draft test, push, then the how-to for the friend.
