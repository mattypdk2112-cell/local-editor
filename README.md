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

`./setup` installs ffmpeg if you don't have it, builds an isolated Python environment,
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

## The five flags worth knowing

Stack them however you like. Everything is off by default.

| flag | what it does |
|---|---|
| `--captions` | Burns in word-by-word karaoke captions |
| `--strip-filler` | Drops every um and uh, from the audio and the captions |
| `--bad-takes` | Cuts your retakes, false starts, and the bits where you talk to the camera about the shot |
| `--music beat.mp3` | Lays a track underneath, mixed low so your voice sits on top |
| `--draft` | You filmed a ramble with no script. It reads the transcript and reorders your real sentences into the tightest version of what you already said |

A normal night looks like this:

```bash
./edit ~/Desktop/raw.mp4 --bad-takes --strip-filler
```

**Captions in your colours:** `--accent FF3D71` takes a plain hex with no hash,
`--words-per-line 2` sets how many show at once, `--no-uppercase` keeps your casing.

Captions are off by default on purpose. Speech recognition occasionally mishears an
unusual word, and once it is burned into the pixels you cannot fix it. Read the
transcript first, then burn. If it mishears something, `--fix 'heard=actual'` corrects
it before it goes in.

Run `./edit --help` for the rest. There are a lot of dials, you will not need most of them.

## About those two AI flags

`--bad-takes` and `--draft` are the only parts that need a model to think. Everything
else is pure ffmpeg and speech recognition running on your machine.

They use the Claude Code you already installed. No API key, no signup, nothing extra to
pay for. If you have `GEMINI_API_KEY` or `OPENROUTER_API_KEY` exported, those are used
instead. If you have none of the three, the two flags tell you so and skip, and the rest
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
- Tell you if the hook works. That is a data question, not an editing one.
- Colour grade, add motion graphics, or anything past cuts and captions.
- Run on Windows. Mac and Linux only.

## If it breaks

Run `./setup` again first, it fixes most things.

**"ffmpeg is not installed"**. On a Mac, `brew install ffmpeg`. If you don't have
Homebrew, get it at [brew.sh](https://brew.sh).

**"need Python 3.9 or newer"**. On a Mac, `xcode-select --install`.

**The captions say the wrong word**. Speech recognition misheard it. Fix it with
`--fix 'what it heard=what you said'`, or run `--model medium` for a slower, more
accurate pass.

**It cut something it shouldn't have**. `--max-gap 1.0` cuts less aggressively.
`--no-cut` skips cutting entirely.

Still broken? Reply to the DM you got this from and tell me what happened. I read all
of them.

## Licence

MIT. Do what you like with it.
