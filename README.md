# The Local Editor

A 5 minute ramble goes in. A 40 second reel comes out.

It listens to your video word by word, cuts every silence longer than half a second,
drops your ums and false starts, and exports a finished MP4. Your footage never leaves
your laptop. No website, no login, no subscription.

Made by [Matt Pham](https://instagram.com/mattphxm) at Converter Studios.

---

## Install

You need a Mac or Linux. On Windows, use WSL.

```bash
git clone https://github.com/mattypdk2112-cell/local-editor.git ~/local-editor
cd ~/local-editor
./setup
```

`./setup` installs ffmpeg if you don't have it (no Homebrew or password needed), builds an isolated Python environment,
and downloads the speech model. It takes a couple of minutes and you only do it once.
It is safe to run again if something looks wrong.

If you'd rather not touch a terminal, open [Claude Code](https://claude.com/claude-code)
and paste this:

> Clone https://github.com/mattypdk2112-cell/local-editor into ~/local-editor and set it
> up so I can run it. Install anything it needs first, including ffmpeg and Python if
> they are missing. Then cut ~/Desktop/raw.mp4 for me.

## Use it

```bash
cd ~/local-editor
./edit ~/Desktop/raw.mp4
```

The finished file lands in `~/Downloads` and opens by itself. The first run is slower
because it downloads the speech model once.

## Build a whole reel from several clips

`./edit` cuts one clip. `./reel` joins several (talking clips, screen recordings, b-roll) and adds
text with simple animations, your own sound effects and music, an end card and captions, all from
one small recipe file:

```bash
./reel recipes/example.json all
```

Everything is in [recipes/README.md](recipes/README.md). No music or sound effects are included,
bring your own.

## The two jobs it does

**1. You have a script.** You filmed it, stumbled, did five takes of the hook, and now you
have a 5 minute raw clip. Put the script in a text file, one line per beat:

```
What happens when you ask a work question at a poker table?
You get absolutely destroyed.
Yesterday I was at Lyra, a tech startup.
```

Then:

```bash
./edit ~/Desktop/raw.mp4 --script ~/Desktop/script.txt
```

For every script line it finds the best take you said, keeps them in script order, and drops
the retakes and the bits in between. The line does not have to match word for word, a close
paraphrase is fine. If fewer than 40% of the lines match, it says so and falls back to a plain
cut rather than ship a butchered one.

**2. You have no script.** You rambled. Let it pick the best parts:

```bash
./edit ~/Desktop/raw.mp4 --draft --target 45 --brief "open on the poker line, keep the Eddie bit"
```

`--target` is the length you want in seconds. `--brief` is optional, plain English direction.
It can only pick and reorder sentences you actually said, it cannot write new ones. It saves
what it picked as `projects/<name>/story.txt`. Don't like the order? Edit that file and re-run
with `--script` pointing at it.

Either way, the first run transcribes the clip (the slow part). Every re-run after that
reuses the transcript, so fixing the cut is fast: change a line in `script.txt`, run it again.

## Flags worth knowing

| flag | what it does |
|---|---|
| `--script script.txt` | Cut to your script (job 1 above) |
| `--draft` | Pick the best parts of a ramble (job 2 above) |
| `--captions` | Burns in word-by-word karaoke captions. Off by default |
| `--strip-filler` | Drops every um and uh, from the audio and the captions |
| `--no-bad-takes` | Turns off the retake cutter, which is on by default |
| `--music beat.mp3` | Lays a track underneath, mixed low so your voice sits on top |
| `--height 1080` | Exports smaller than a 4K source. Faster, and plenty for a reel |
| `--no-normalize` | Keeps your raw audio level, for when you finish the mix in Premiere or CapCut |
| `--drop 37.4-38.4` | Cuts out a stretch of the source by hand (seconds, from `projects/<name>/transcript.json`). For a stumble the automatic passes missed. Takes several |
| `--pin 12.5` | Protects a moment in the source (a reaction, a reveal) from trimming and tells you where it landed in the cut |
| `--noise-db -38` | The silence floor for finding speech, in dBFS (decibels below full scale). Default -42. Raise it on a noisy room |
| `--title "TEXT"` | A title card at the start. Works with captions on or off. `--title-variant` picks the look: badge, headline, problem, plate, native |
| `--no-proxy` | Works from the original file even when it is 4K or HEVC (see Proxies below) |

**Captions in your colours:** `--accent FF3D71` takes a plain hex with no hash,
`--words-per-line 2` sets how many show at once, `--no-uppercase` keeps your casing.

Captions are off by default on purpose. Speech recognition occasionally mishears an
unusual word, and once it is burned into the pixels you cannot fix it. Read the
transcript first, then burn. If it mishears something, `--fix 'heard=actual'` corrects
it before it goes in.

Run `./edit --help` for the rest. There are a lot of dials, you will not need most of them.

## Proxies

Every stage reads the whole clip from the top, and 1440p-or-larger footage, or HEVC,
ProRes, AV1 and VP9 files (what iPhones and most cameras record), decode slowly. For
those the editor makes one 1080p H.264 copy (the proxy) the first time, keeps it in a
`.proxies` folder next to your footage, and cuts from that. The second run reuses it.

The finished video is then 1080p, which is plenty for a reel. To cut from the original
at full size, pass `--no-proxy`; to force a proxy on a smaller file, pass `--proxy`.
Delete the `.proxies` folder whenever you like, it is rebuilt on demand.

Phone footage shot in HDR (HLG) is converted to normal colour automatically, so it does
not come out grey.

## About those two AI flags

The retake cutter and `--draft` are the only parts that need a model to think. Everything
else is pure ffmpeg and speech recognition running on your machine.

They use the Claude Code you already installed, including the one inside the Claude desktop
app. No API key, no signup, nothing extra to pay for. If you have `GEMINI_API_KEY` or `OPENROUTER_API_KEY` exported, those are used
instead. If you have none of the three, those two passes tell you so and skip, and the rest
of the edit runs normally.

`--draft` can only ever pick and reorder sentences you actually said. It cannot write
new ones. That is enforced in code, not asked for in a prompt.

## Where things go

- Finished video: `~/Downloads`. Use `--out-dir` to change it, or `--beside` to drop it
  next to the input.
- Working files: `projects/<name>/` inside the repo. Transcript, cut plan, captions and
  the raw render. Re-running reuses the transcript, so the second pass is fast. Delete
  the folder or pass `--fresh` to start clean.

## What it doesn't do

- Write the script. It edits the take, it doesn't think of it.
- Hand you a Premiere or CapCut timeline. It exports a finished MP4 you can fine-tune from.
- Tell you if the hook works. That is a data question, not an editing one.
- Colour grade, add motion graphics, or anything past cuts and captions.
- Run on Windows. Mac and Linux only.

## If it breaks

Run `./setup` again first, it fixes most things.

**"ffmpeg is not installed"**. Run `./setup` again, it downloads one into `~/.local/bin`.

**"need Python 3.9 or newer"**. On a Mac, `xcode-select --install`.

**The captions say the wrong word**. Speech recognition misheard it. Fix it with
`--fix 'what it heard=what you said'`, or run `--model medium` for a slower, more
accurate pass.

**It cut something it shouldn't have**. `--max-gap 1.0` cuts less aggressively.
`--no-cut` skips cutting entirely.

Still broken? Reply to the DM you got this from and tell me what happened. I read all
of them.

## Usage reports

Every `./edit` and `./reel` run adds one line to `usage-report.jsonl` in this folder.
That file always exists once you have run the editor, it is ignored by git, and it
never leaves your machine on its own. `./report` prints it as a short summary you can
copy and send to Matt if you want to (`./report --raw` shows the lines as stored).

Each line holds exactly these fields:

- `tool`: edit or reel
- `run_id`: a random id, so a run is never counted twice
- `ts`: when the run started (UTC)
- `version`: the editor's git commit
- `os`, `python`: for example `Darwin 25.6.0 arm64`, `3.9.6`
- `flags`: the flag names you used, like `--captions`. Never their values
- `input_duration_s`, `input_width`, `input_height`: length and frame size of the input
- `output_duration_s`, `clips`: length of the result, and how many clips a reel used
- `stages`, `total_s`: seconds per stage (transcribe, roughcut, assemble...) and in total
- `ok`, `exit_code`: whether it worked
- `error`, `traceback`: only on a crash. The error message with every path, filename,
  argument value and quoted text replaced by `<path>`, `<file>`, `<hidden>` or `<text>`,
  and the last 8 lines of the editor's own code it passed through
  (like `src/assemble.py:120 in assemble`)

Never your footage, transcript text, file paths or filenames.

**Nothing is sent without an invite code.** `./setup` asks once: "Got an invite code
from Matt? Paste it, or press Enter to skip." Matt hands codes out personally. With a
code, each line is also sent to him from a background process with a 4 second timeout,
so it never slows down or breaks an edit, and the copy that was sent is kept at
`projects/.last-usage-report.json`. Without one, nothing ever leaves your laptop.
There are no notifications or alerts on either side.

**Stop sending:** `rm .usage-reports.json` (`./setup` asks again next time), or run with
`LOCAL_EDITOR_REPORTS=0`.
**Delete the log:** `rm usage-report.jsonl`. It starts fresh on the next run.

## Licence

MIT. Do what you like with it.
