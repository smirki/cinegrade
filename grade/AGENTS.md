# cinegrade: agent operating guide

A headless color grading pipeline for Apple Log footage. Read this before
grading anything. Run every command through the wrapper `./content/cinegrade`.

## The node tree

Stages run in this fixed order. Each is optional.

```
LOG        decode ProRes, expand tv -> full range, BT.2020 matrix.
           exposure AND white balance applied here as log-domain offsets
CST IN     Apple Log / BT.2020 -> DaVinci Intermediate / DaVinci Wide Gamut
PRIMARIES  lift-gamma-gain, contrast, saturation, vibrance, curves
           (runs inside DWG, where there is headroom)
CST OUT    DaVinci Wide Gamut -> Rec.709, tone map applied here
LOOK       creative .cube LUT, at a real opacity (look.mix)
FX         halation -> bloom -> radial blur -> RGB split -> vignette
DETAIL     soften, then sharpen
GRAIN      film grain
OUT        ProRes 422 HQ or H.264, retagged Rec.709
```

Set `convert.working_space` to `direct` to collapse the two CSTs into one
Apple-Log-to-Rec.709 LUT. `dwg` is the default and is what you want: primaries
run before the tone map, so a contrast or exposure push still gets rolled off
instead of clipping.

The published figures for that (`direct` clipping 0.88% of the frame against
`dwg`'s 0.72% on a +1.2 stop, 1.45 contrast push) no longer reproduce, and the
reason is worth knowing. They were measured while two separate bugs were live:
`convert.encode` could not reach the `direct` path, so that side was rendering
gamma24 while `dwg` rendered rec709a, and the `direct` contrast pivot was an
output-side code value rather than the Apple Log grey the stage actually
operates on, so contrast was dragging the whole image up. With both fixed and
both paths on the same encode, that gentle push clips 0.755% either way, a
difference of four parts per million. The headroom difference is real (DWG holds
100.0 scene linear at code 1.0 against Apple Log's 12.0) but it takes a harder
push to see: at +2.8 stops and contrast 1.8, `direct` loses 45.6% of the frame
to its white ceiling against `dwg`'s 45.2%.

There is a third option, `rec709`, for footage that is NOT camera log: it skips
both CSTs and grades in place. Use it for an ordinary mp4, or to bring a Rec.709
export back for FX and grain only. Choosing an Apple Log space for a bt709
tagged source is refused with a message rather than rendered, because it used to
silently ruin the picture (median luma 0.332 to 0.238, saturation 0.33 to 0.59).

## Commands

```bash
./content/cinegrade orient  IN.MOV --time 8 --open          # do this first
./content/cinegrade compare IN.MOV --presets flat,clean,natural --time 8 --open
./content/cinegrade compare IN.MOV --looks kodak2383,warm_film --time 2
./content/cinegrade still   IN.MOV -p cinekit --time 2 --width 900 -o f.png
./content/cinegrade scopes  IN.MOV -p cinekit --time 2 -o s.png
./content/cinegrade stats   IN.MOV -p cinekit --time 2
./content/cinegrade render  IN.MOV -p cinekit -o out.mov
```

## How to actually grade

1. `compare` first, always. One image with every candidate beats N renders.
2. `stats` to check you have not crushed or blown anything. `YMIN` near 0 with
   a large pixel count there means clipped blacks. `YMAX` pinned at 255 means
   clipped highlights. `YAVG` outside roughly 95 to 140 means the frame is
   mis-exposed, not stylised.
3. `scopes` when a color cast is suspected. On the vectorscope, skin tones must
   sit along the diagonal skin line. If the cloud drifts off it, fix
   `temperature` and `tint` in PRIMARIES, not in the LOOK LUT.
4. `render` only once the still is approved. A 5 second 4K clip with the full
   FX chain takes about 45 seconds.

## Picking a look

The full list, with a one line description and the measured mean saturation of
each look on two real frames, is the table in `README.md`. Read it before
guessing: the numbers are like-for-like, so "quieter than natural" or "as loud
as blockbuster without the hue steering" is a lookup rather than an opinion.

Looks 14 to 20 (`film_portrait`, `film_tungsten`, `commercial`, `blue_hour`,
`interior`, `punch`, `silverblue`) were each built to a stated numeric target
and verified two ways before being called done: seven probe colours checked for
hue family, channel ranking, saturation pinning and channels driven to zero,
then the whole thing measured on a 4K golden hour exterior and a soft interior
selfie. The target, the measured result and the traps found on the way are in
each look's docstring in `tools/make_looks.py`. If you change one of them,
re-run that verification rather than looking at the frame and deciding.

Two results from that work are worth knowing before you build another look:

- **A saturation target cannot be met on two different scenes at once.** A cool
  cast subtracts chroma from a green exterior and adds it to a warm interior,
  because chroma is distance from grey and the cast runs with one and against
  the other. `film_tungsten` measures -16% on one frame and +16% on the other
  from a single setting.
- **Grain is not colour neutral even when the grain plate is.** The overlay
  blend moves channels either side of 0.5 in opposite directions, so the same
  grey noise value stretches the channel differences it lands on. Measured on
  `silverblue` it took a near-monochrome frame from 0.07 saturation to 0.11.
  That is why the new presets ship with grain off.

## Check orientation before any long render

```bash
./content/cinegrade orient IN.MOV --time 8 --open
```

Some clips carry a display-matrix rotation whose frame content is **already**
upright, so honouring the tag rotates it into being sideways. Both cases exist
in this footage set: one clip's +90 tag was correct, another's -90 tag was not.
Trusting the tag either way is a coin flip, so look at the two-up and pass
`--no-autorotate` when the right-hand frame is the upright one.

Getting this wrong is expensive twice over: the render is unusable, and the
blur and mask dimensions are computed from the rotated size, so the FX geometry
is wrong too.

## Match the effects to the source, or they subtract

Every optical effect trades apparent sharpness for character. That trade is
only worth making when the source has sharpness to spend. Measure first:

```bash
ffmpeg -i IN.MOV -vf "format=gray,split=2[a][b];[b]gblur=sigma=2[bb];\
  [a][bb]blend=all_mode=difference,signalstats,metadata=mode=print:file=-" \
  -frames:v 1 -f null -
```

`YAVG` from that is a rough detail-energy figure. Below about 0.5 on a 4K
frame the source is soft (a phone front camera, heavy noise reduction, a
low-light shot), and you should use the `clean` preset: sharpen rather than
soften, and leave radial blur, vignette, bloom and grain off.

Grain is the trap. It raises the detail metric while lowering actual quality,
because it adds high-frequency noise on top of a soft image, which is exactly
what cheap footage looks like. On the test clip, grain supplied 55% of the
graded frame's high-frequency energy. If you add grain, verify with the source
sharp enough to carry it, and check the metric with grain disabled.

The same logic applies upstream: a look LUT cannot invent colour separation
that the scene never had. Flat frontal light on a beige interior has no
shadow-side modelling and no colour contrast, so every look will read as a
tint over a dull frame. That is a shooting problem, not a grading one.

## Rules that will bite you

- **Never apply a look LUT to raw log.** The CONVERT stage must run first.
  Looks are authored in Rec.709 and will produce mud on log input.
- **Exposure belongs in `convert.exposure`, in stops.** It is applied as an
  offset on the log signal before tone mapping, which is what actually
  recovers highlights. Brightening in PRIMARIES instead just stretches
  already tone-mapped values and clips the top end.
- **Tone map choice is a real decision.** `aces` gives filmic highlight
  roll-off and is the default. `filmic` is gentler and keeps more midtone
  latitude. `none` clips anything over diffuse white; use it only when
  matching a reference that was itself made without tone mapping.
- **Do not stack a combination LUT on top of the CST.** See
  `luts/looks/README.md`.
- Vignette lowers `YAVG`. A drop of 10 to 15 after enabling it is expected and
  is not a mis-exposure.
- **Pick the right output gamma.** `convert.encode` is `rec709a` by default,
  which is right for grading on a Mac display. Switch it to `gamma24` for a
  calibrated external monitor. Grading against the wrong one is why a grade
  can look correct in the app and washed out in QuickTime.
- **A look LUT at full strength is almost always too strong.** `look.mix`
  around 0.6 to 0.8 is the normal working state, not 1.0. This is real
  blending: `mix` 0.0 is pixel-identical to no look, 1.0 to the full look.
- **`look.mix` does not apply to combination LUTs.** Those bake the log
  conversion in, so dialling the opacity down also dials down the conversion
  and the image goes wrong rather than subtle. Use `mix` only on Rec.709
  creative LUTs.
- White balance is deliberately not in the PRIMARIES stage. It is applied in
  the LOG stage, because on a log signal a per-channel offset is exactly a
  per-channel linear gain. That makes it the equivalent of Resolve's "set the
  node to linear gamma and use the gain wheel" technique, applied where the
  sensor's own gain would have acted.

## Editing a preset

Presets are JSON in `presets/`, deep-merged over the defaults in
`cinegrade.py`. Only override what you change:

```json
{
  "convert":   {"working_space": "dwg", "tonemap": "aces",
                "encode": "rec709a", "exposure": 0.3},
  "primaries": {"temperature": 0.04, "tint": 0.0, "contrast": 1.05,
                "saturation": 1.0, "vibrance": 0.1,
                "black_lift": 0.01, "highlight_rolloff": 0.04},
  "look":      {"lut": "warm_film", "mix": 0.7},
  "fx":        {"halation": {"enabled": true, "strength": 0.5}},
  "detail":    {"soften": 0.0, "sharpen": 0.6},
  "grain":     {"enabled": true, "strength": 5}
}
```

Units: `exposure`, `temperature` and `tint` are in **stops**. `temperature`
positive is warmer, `tint` positive is greener.

## Rebuilding LUTs

They are gitignored and generated, so rebuild after a fresh clone:

```bash
cd content
.venv/bin/python grade/tools/make_cst.py --stage in --size 65 \
  --out grade/luts/technical/AppleLog_to_DWG.cube
# Every tonemap the CLI advertises, on both working spaces, at both encodes.
# The full product matters: a combination with no cube on disk is a render
# that dies at load time, and `none` on the DWG path used to be exactly that.
for tm in aces filmic none; do
  .venv/bin/python grade/tools/make_cst.py --stage direct --tonemap $tm --size 65 \
    --out grade/luts/technical/AppleLog_to_Rec709_$tm.cube
  for enc in rec709a gamma24; do
    .venv/bin/python grade/tools/make_cst.py --stage direct --tonemap $tm --encode $enc \
      --size 65 --out grade/luts/technical/AppleLog_to_Rec709_${tm}_${enc}.cube
    .venv/bin/python grade/tools/make_cst.py --stage out --tonemap $tm --encode $enc \
      --size 65 --out grade/luts/technical/DWG_to_Rec709_${tm}_${enc}.cube
  done
done
.venv/bin/python grade/tools/make_looks.py
```

`make_cst.py` prints anchor checks on every run. An 18% grey card must land
near **0.39** on the `aces` path. If it does not, the CST is wrong and every
grade downstream inherits the error.
