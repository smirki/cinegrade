# Match Reference: how a UI should call it

`grade/tools/match_ref.py` takes a reference image plus one source frame and
writes a look `.cube` that moves the source's colour toward the reference. It
is the honest version of Resolve's Shot Match: it infers a transform from two
colour distributions, it does not recover the reference's grade. Everything
below is the contract; the reasoning lives in the tool's own docstrings.

## Call it in process (preferred)

```python
import sys
sys.path.insert(0, "<repo>/content/grade/tools")
from match_ref import match_reference, MatchError

result = match_reference(
    ref="content/refs/IMG_2570.PNG",       # required, any still ffmpeg reads
    clip="content/footage/A001_09011832_C003.MOV",
    time=20.0,                             # seconds into the clip
    autorotate=False,                      # False == cinegrade's --no-autorotate
    preset="cinekit",                      # the preset the shot is graded with
    method="reinhard",                     # or "histogram"
    strength=0.75,                         # 0 to 1, baked into the cube
    luma_preserve=True,                    # colour only, leave exposure alone
)
```

One call builds one LUT with one method. For a two-up comparison, call it
twice and use the returned `distance` to label them.

Equivalent CLI, same defaults:

```bash
content/.venv/bin/python content/grade/tools/match_ref.py match \
  --ref content/refs/IMG_2570.PNG \
  --clip content/footage/A001_09011832_C003.MOV --time 20 --no-autorotate \
  --preset cinekit --method reinhard --strength 0.75 --luma-preserve \
  --report /tmp/match.json
```

`match_ref.py detect IMG.PNG ...` prints the auto detected crop for one or
more references and writes nothing. Use it to render the crop rectangle in a
preview before the user commits.

## Arguments a button actually needs

| Argument | Default | What it does |
| --- | --- | --- |
| `ref` | required | Reference image. iPhone screenshots are handled: P3 vs sRGB is read from the PNG's own tagging, and the phone UI is cropped off. |
| `clip` + `time` | none | The source. Rendered through the engine to the exact node where the look LUT sits. |
| `still` | none | Use instead of `clip` when you already have a Rec.709 still from `cinegrade still`. |
| `preset` | engine defaults | Must be the preset the shot will actually be graded with. A LUT fitted through one CST is wrong under another. |
| `autorotate` | `True` | `False` for clips whose display matrix lies (see `cinegrade orient`). |
| `method` | `"reinhard"` | `reinhard` (safe) or `histogram` (stronger, can overshoot). |
| `strength` | `1.0` | Blend toward identity, baked in. 0.6 to 0.8 is the usable range. |
| `luma_preserve` | `False` | Match colour without moving exposure. On by default is a reasonable UI choice. |
| `crop` / `crop_frac` | auto | `"x,y,w,h"` in pixels or in fractions, to override the detected region. |
| `name` | derived | Output stem. Derived name is `match_<ref>_<source>_<method>[_lp]`. |
| `out_dir` | `grade/luts/looks` | Where the cube lands. |
| `size` | `33` | Cube size. Matches the other looks; 33 is plenty for these transforms. |
| `width` | `960` | Analysis width for the source frame. Lower is faster, statistics barely move. |
| `verify` | `True` | Apply the cube back through ffmpeg and measure it. Leave it on. |

## What comes back

A plain dict, JSON serialisable:

```
ok                bool   false means do not offer this LUT to the user
lut               str    absolute path to the written .cube
name              str    stem, which is what cinegrade's look.lut wants
method, strength, luma_preserve, preset, encode, reference, source
ref_gamut         "p3" | "srgb", read from the reference file itself
crop              {x, y, w, h, frac:[x0,y0,x1,y1], area_pct, source, notes[]}
hue_divergence    float  0 to 1, source vs reference content difference
distance.before / .after   {luma, sat, hue, total, colour}
distance.gain_pct / .gain_colour_pct   percent closer, negative means worse
stats.reference / .source_before / .source_after
                  {luma_pct[5], mean_sat, families{...}, clip_low_pct,
                   clip_high_pct, tonal_density[3], ...}
lut_health        {neutral_monotonic, neutral_flat, black_point, white_point,
                   probes{maroon, skin, grey18, sky, foliage}, probes_ok}
method_info       the fit itself (gains, or the per channel curve limits)
warnings          list[str], already written in plain English for display
elapsed_s         float
```

The cube is written even when `ok` is false, so a UI can show the numbers and
still refuse the result. Show `warnings` verbatim; they are the product.

## Wiring the result into the engine

The file lands at `content/grade/luts/looks/<name>.cube`. Set
`look.lut = result["name"]` and `look.mix` to taste. The cube's domain is
Rec.709 display code, so it goes in the LOOK slot and nowhere else.

`strength` and `look.mix` are the same axis and they multiply: a cube baked at
`strength=0.8` shown at `mix=0.5` is a 0.4 match. Pick one as the user facing
control. The sane split is to bake at 1.0 and let the user drag `look.mix`,
which needs no rebuild, and to use `strength` only when handing someone a
fixed conservative file.

## Timing

Measured on this machine, 4K ProRes source, 33 cube, `width=960`:

| Step | Cost |
| --- | --- |
| decode and crop the reference | 0.4 to 0.9 s (12 MP screenshot) |
| render the source frame through the engine | 0.6 to 1.5 s |
| fit and write the cube | under 0.1 s |
| verify (apply the cube back, measure) | 0.4 to 0.6 s |
| **total per call** | **1.4 to 3.0 s** |

Nothing is cached between calls, so a two method comparison costs twice that.
If the UI wants both, run them concurrently or reuse one rendered still via
the `still` argument.

## Failure modes

1. `MatchError` (and `cinegrade.GradeError`) are raised for user fixable
   problems and carry a message meant to be shown: reference not found, an
   unknown preset, a `time` past the end of the clip, a crop smaller than
   16 px, ffmpeg failing. Catch both, show the string.
2. `result["ok"] is False` means a health check failed: the maroon or skin
   probe changed dominant channel, a probe hit a hard 0 or 1, or the LUT's
   neutral ramp is not monotonic or goes flat. Treat as "do not apply".
3. `ok` true with a non empty `warnings` is the normal case for a hard match.
   The one that matters most is `hue_divergence > 0.35`, which means the
   reference and the shot are different content and the match will drag the
   frame toward whatever the reference happens to contain. Surface it next to
   the preview, not in a log.
4. A negative `distance.gain_colour_pct` means the match moved the frame away
   from the reference. That has only been observed with `method="histogram"`
   on mismatched content. Offer `reinhard` as the fallback.
