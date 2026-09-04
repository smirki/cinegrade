# cinegrade regression suite

A golden-frame and invariant suite for the grading engine, built because two
real bugs shipped and were caught by luck:

- `look.mix` was declared and never read, and when it was implemented the blend
  was inverted, so `mix=0.0` produced output identical to `mix=1.0`.
- `lift` and `gain` went through ffmpeg's `colorlevels`, whose output points cap
  at 1.0, so any gain above 1.0 failed the render outright.

Both are the kind of bug a numeric assertion catches instantly and a human
eyeballing a frame does not. Output frames were also once silently tagged
`color_primaries=bt2020`, and a split-tone shadow push once inverted dark
saturated reds so a maroon car turned blue. Every one of those has a test here.

## Running it

```bash
cd content
.venv/bin/python grade/tests/run_tests.py                  # everything
.venv/bin/python grade/tests/run_tests.py -g color params  # some groups
.venv/bin/python grade/tests/run_tests.py -k mix -v        # by name, verbose
.venv/bin/python grade/tests/run_tests.py --list           # what exists
```

Plain python with the repo venv. There is no pytest in `content/.venv` and the
suite does not add one. numpy and colour-science are the only imports; scipy,
matplotlib and PIL are not installed, so pixels are read by piping ffmpeg
rawvideo into numpy and images are written by piping raw bytes back out.

The whole suite is roughly 100 ffmpeg renders and takes about two minutes. It
is fast because every frame is rendered at 320px wide: a render is dominated by
the ProRes decode, so a small frame costs the same as a large one, and the
engine is handed the small dimensions so its blurs, masks and grain plate scale
with it. Identical configs are rendered once and memoised.

Exit code is 0 only when nothing failed.

### Verbose output

By default a passing test prints one line. `-v` prints every assertion with the
numbers behind it, which is what you want when you are deciding whether a
change is a regression or an improvement.

## Updating goldens

```bash
.venv/bin/python grade/tests/run_tests.py -g golden --update
```

This rewrites the fingerprints in `goldens/` from whatever the engine currently
produces. It is the only thing that ever writes them, so a golden moving on its
own is always a regression.

Re-bless deliberately, not reflexively. A failing golden prints exactly what
moved and by how much:

```
FAILED  clipA_blockbuster: luma_mean 0.31057 -> 0.30157 (-0.00900)
FAILED  clipA_blockbuster: hue orange 36.378% -> 34.978% (-1.400 pts)
FAILED  clipA_blockbuster: thumbnail block (4,7) g 0.2095 -> 0.1785 (max delta 0.0310)
```

Read those numbers first and decide whether they are the change you intended.
If a look LUT or a preset was regenerated on purpose, they will be; if you were
refactoring the filter graph, they should not have moved at all.

A golden is a JSON fingerprint, not an image: per channel mean, median and
percentiles, mean saturation, hue family shares, a 12x12 block-mean thumbnail
and its hash. About 9KB each. The thumbnail is stored as numbers rather than
only as a hash so a failure can name the block that moved, which is the
difference between a report you can act on and one you re-bless out of
frustration.

## The groups

| group | what it covers |
| --- | --- |
| `color` | colour science anchors: the published Rec.709 values, the DWG working space, the two encodes, Apple Log's negative black, the contrast pivot |
| `golden` | six rendered frames, both clips, presets spanning CST-only, look, full FX and grain |
| `params` | every leaf key in `DEFAULTS`: it must change the picture, and change it in the direction its name implies |
| `look` | `look.mix`, pinned at the pixel level |
| `range` | clipping, pure black, pure white, dark saturated reds |
| `output` | the written file: bt709 tagging, codec, profile, crf, preset |
| `cli` | every subcommand runs and writes a valid, decodable output |

### color

Every expected number is lifted from the code or the docs, never chosen to make
a test pass. 18% grey lands at **0.3919** on the direct ACES path and at
**0.3360** in the DWG working space; the DWG two-LUT pair matches the direct
single LUT to within a lattice interpolation; gamma 2.4 encodes higher codes
than Rec.709-A at the same display linear; Apple Log code 0.0 decodes to
**-0.0564** and must clamp before the gamut matrix. Anchors are measured
through ffmpeg with the real `.cube` files, so a broken LUT or a wrong
interpolation mode fails here too, not only in the python that generated them.

The anchors are fed as 16-bit synthetic swatches of exact Apple Log code
values. Real footage cannot supply a known input, and 8 bits would quantise
0.3919 by as much as the tolerance being tested.

### params

Two assertions per parameter: it changes the frame at all, and it changes it
the right way. A parameter that does nothing is a failure, never a skip.

A coverage test walks `DEFAULTS` and fails if any leaf key is missing from the
table, so adding a parameter to the engine without testing it breaks the suite.

Choosing the measurement is most of the work. Some notes worth keeping:

- **Reference frames must switch the stage off explicitly.** Comparing a
  parameter against "the base config" is meaningless when the base already has
  the effect enabled.
- **A wider blur is measured by reach, not by energy.** A large halation sigma
  spreads the same light over so many pixels that none moves a whole 8-bit
  code, so the metric counts pixels touched at all.
- **Highlight clipping is measured against each working space's own white
  point.** Apple Log code 1.0 is 12.0 scene linear and tone maps to about
  0.992; DaVinci Intermediate code 1.0 is 100.0 scene linear and tone maps past
  1.0 onto a hard 255. Counting samples "at 255" compares two different
  ceilings and reverses the answer.
- **Lost detail is whole-pixel clipping.** One channel pinned while the other
  two still vary is what a saturated look does on purpose.

### Expected failures

A test marked expected-fail carries the correct assertion against a known
engine bug. It does not fail the run, and if it starts passing the suite
reports `XPASS` and fails, because the marker has become a lie and should be
removed along with the fix.

None are marked today. Two were, and both are now fixed in the engine, so the
markers came off and the assertions stayed:

- `params.fx_vignette_radius` - `build_fx` read only `fx.vignette.amount`, so
  `radius` never reached the ffmpeg `vignette` filter. It now rides in on the
  angle, and the shipped default of 0.85 makes that algebraically identical to
  the old expression, so no preset moved.
- `color.direct_honours_convert_encode` - `f_convert_out` built
  `AppleLog_to_Rec709_{tonemap}.cube` on the `direct` path, with no encode in
  the filename, so `convert.encode` could not reach it and the shipped default
  of `rec709a` silently rendered gamma 2.4. Per encode direct cubes fixed it.

Three more findings this suite raised were fixed the same way: the `direct`
contrast pivot (it held an output-side Rec.709 code, so contrast shifted
exposure), the missing `DWG_to_Rec709_none_*` cubes (`--tonemap none` could not
render on the default working space), and the `black_lift` / `highlight_rolloff`
curve (a 0.5 anchor drawn in a working space whose mid grey is 0.336, which
dragged the mid tones instead of only the ends).

This suite does not modify the engine to make itself pass; a failure is a
finding, and the engine is where it gets fixed.

## Housekeeping

Renders, synthetic swatch images and throwaway presets go in `_work/`, which is
deleted at the end of a run. `--keep-work` keeps it when you want to look at
something.

The engine caches baked secondary cubes and radial masks under `grade/luts/`,
keyed by their settings. The parameter sweep asks for dozens of one-off
combinations that no grade will ever use again, so anything a run creates there
is removed afterwards rather than left as tens of megabytes of litter in a
directory shared with real work. `--keep-work` suppresses that too.

## Adding a test

Case modules are `cases_*.py`, each exposing `register(suite)`. A test is a
function taking a `ctx`, and its assertions record their numbers whether they
pass or fail, which is what makes the report readable:

```python
def test_thing(ctx):
    img = H.render(H.CLIP_A, H.preset("natural"), H.TIME_A)
    ctx.expect_close("18% grey", H.stats(img)["luma_p50"], 0.42, 0.01)
```

For a new engine parameter, add a `P(...)` row to `CASES` in `cases_params.py`
rather than a new function; the coverage test will tell you if you forget.
