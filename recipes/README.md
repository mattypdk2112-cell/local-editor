# Recipes: build a whole reel from one file

`./edit` cuts one talking-head clip. `./reel` builds a reel from several: talking clips, screen
recordings, b-roll, with text on top, your own sounds and music, and an end card.

```bash
./reel recipes/example.json all
```

Copy `example.json`, point it at your own files, and run it. Two steps run in order:

1. **sections** joins the pieces into `<name> sections.mp4` and writes `<name> cuts.json`, a list
   of where every piece starts.
2. **finish** adds text, sounds, music, the end card and captions, and writes `<name> v1.mp4`.
   Run it again and you get `v2`, so earlier versions are never overwritten.

Want to colour-grade between the two? Run `sections`, grade the file in any app, then
`./reel recipe.json finish --body graded.mp4`.

## The recipe, field by field

| Field | What it does |
|---|---|
| `name` | Used in the output file names |
| `out` | Folder the finished files go to |
| `footage_dir` | Where your clips live, so pieces can just say `"talk.mov"` |
| `size`, `fps` | Output size and frame rate. Default 1080x1920 (vertical) at 30 |
| `pieces` | The clips, in order. Each has `file`, `in` and `out` in seconds, and a `label` |
| `squeeze` | `true` removes the pauses and breaths inside every talking piece |
| `stamps` | Text on screen. `at` says when it appears, `until` when it goes |
| `stamp_style` | `size_pct` and `y_pct` (percent of the frame height), `anim` |
| `sfx` | Your sound effects. `sound` is a file, `at` is when, `peak_db` is how loud |
| `music` | Your song. `gain_db` sets the level (default -18). `land` lines a moment of the song up with a cut |
| `end_card` | A closing card: `text`, `secs`, `color`, `anim` |
| `captions` | `true` burns in word-by-word captions with the same captioner `./edit` uses |

### Piece options

- `kind`: `"talk"` (default) or `"broll"`. Squeeze only touches talk pieces.
- `gain_db`: make one piece louder or quieter.
- `x_pct`, `y_pct`: move the crop when a clip is reframed (0 = left/top, 50 = centre, 100 = right/bottom).
- `tail`: keep a short beat after the last word, in seconds.
- `proxy: false`: cut from the original file. By default a big file (4K) gets a smaller cached copy
  first, in a `.proxies` folder next to it, so cutting stays fast.

### Anchors

Any `at`, `until` or `on` can be seconds, or a place on the timeline. Re-trim a piece and
everything anchored to it moves with it.

| Anchor | Means |
|---|---|
| `12.5` | 12.5 seconds in |
| `"demo"` | where the piece labelled demo starts |
| `"demo+1.2"` / `"demo-0.4"` | 1.2s after it starts / 0.4s before |
| `"demo.end"` | where it ends |
| `"end"` | the end of the body, where the end card starts |
| `"word:five days"` | where that phrase is spoken (needs speech in the body) |

For sound effects, `at` can also be a group: `"cuts"` (every cut), `"kind:broll"` (every b-roll
piece) or `"stamps"` (every time text appears).

### Animations

Kept simple on purpose. Set `anim` on `stamp_style` for all text, or on one stamp or the end card.

| `anim` | Looks like |
|---|---|
| `fade` (default) | fades in and out |
| `slide` | rises a few pixels into place |
| `pop` | grows from 85% to full size |
| `none` | hard on, hard off |

### Music

No music ships with this. Use a file you have the rights to.

`"land": {"song_at": 32.0, "on": "demo"}` starts the song so that its 32-second mark, usually the
drop, hits exactly when the demo piece starts. The music fades out before the end card, so the reel
loops cleanly.

## What it won't do

No motion graphics, no templates, no music or sound library. It joins, times and labels your clips.
The taste is still yours.
