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
