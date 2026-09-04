# Using a cinegrade look in free DaVinci Resolve

Free Resolve cannot run the second half of this engine. It has no third-party
OFX support, so Dehancer and everything like it are out, and the ResolveFX that
would replace them (Film Grain, Glow, Halation, Film Look Creator) are Studio
features. What it can do is apply a 3D LUT, and the colour half of a cinegrade
config collapses into exactly one of those with no loss worth arguing about.

So the split is:

| | where it runs |
| --- | --- |
| exposure, white balance, conversion, primaries, curves, secondary, look | a `.cube` you drop into free Resolve |
| halation, bloom, radial blur, RGB split, vignette, grain, soften, sharpen | here, in cinegrade, or not at all |

`tools/bake_lut.py` writes the cube and then proves it by rendering the same
frame both ways. Every number in this document came out of that check on real
4K Apple Log footage from `footage/`.

---

## 1. Bake the cube

```bash
cd content
# one LUT that does everything, for footage still in Apple Log
.venv/bin/python grade/tools/bake_lut.py --preset natural --domain applelog

# a LUT that goes after a conversion, for a Rec.709 timeline
.venv/bin/python grade/tools/bake_lut.py --preset natural --domain rec709
```

Output lands in `grade/luts/export/`. The tool prints, loudly, which kind it
wrote and where it belongs, and it lists every stage of the preset it could
**not** put in the file.

Other flags: `--config path.json` for a config that is not a named preset,
`--size` to override the grid, `--strict` to exit non-zero when the preset has
spatial stages, `--no-verify` to skip the render check, `--verify-clip` and
`--verify-time` to check against a different frame.

---

## 2. Which domain, and why it matters so much

This is the one thing to get right. Both files look plausible in the LUT
browser and only one of them is correct for your node tree.

### `--domain applelog`, the combination LUT

The cube expects **raw Apple Log** and does the log to display conversion
itself.

```
[ Clip ]---> [ Node 1 ]
              3D LUT: natural_applelog_65.cube
              nothing else on this node, nothing before it
```

- Project Settings > Color Management > Color Science must be **DaVinci YRGB**,
  not DaVinci YRGB Color Managed. In Color Managed mode Resolve has already
  transformed the clip before your first node sees it, and this LUT would then
  be converting Rec.709 as though it were log.
- There must be no Color Space Transform in front of it.
- Clip Attributes > Data Levels: Apple Log ProRes off the Blackmagic Camera app
  is **Video** levels. cinegrade expands that to full range before the LUT, so
  Resolve has to be handing the node full-range data too. Auto is normally
  right; if the image looks contrastier and more clipped in Resolve than it
  does here, this is the first thing to check.

### `--domain rec709`, the look LUT

The cube expects footage **already converted to Rec.709** and only carries the
grade.

```
[ Clip ]---> [ Node 1 ]---------------------> [ Node 2 ]
              3D LUT:                          3D LUT:
              CST_AppleLog_to_Rec709_*.cube    natural_rec709_65.cube
```

`bake_lut.py --domain rec709` writes that conversion cube next to the look and
tells you its name. The name carries the tone map and the transfer, because the
look cube was built assuming exactly that conversion ran in front of it: most
presets pair with `CST_AppleLog_to_Rec709_aces_rec709a.cube`, but
`golden_haze` uses the filmic tone map and pairs with
`CST_AppleLog_to_Rec709_filmic_rec709a.cube`. Pairing a look with the wrong
conversion puts you back in the same failure class as picking the wrong domain,
just smaller. Use it rather than a Resolve Color Space Transform node
when you want the result to match what cinegrade renders: a Resolve CST does
the same job but with Blackmagic's tone mapping curve, not the ACES fit this
engine uses, so the two diverge in the highlights. A Resolve CST node still
produces a perfectly usable image, it just is not the same image.

If you do use a CST node, set it to Input `Apple Log` / `Rec.2020`, Output
`Rec.709` / `Gamma 2.4` (or `Rec.709-A` to match the engine's default
`convert.encode`), and turn tone mapping on.

### What going wrong actually costs

Measured, not estimated, on the `natural` preset against a 4K frame:

| mistake | worst pixel | mean error |
| --- | --- | --- |
| correct pairing | 0.57 of 255 | 0.06 |
| applelog LUT placed after a conversion | 129 of 255 | 40 |
| rec709 LUT dropped straight on log | 71 of 255 | 34 |

A mean of 40 code values is not subtle, but it is not obviously *broken*
either. It reads as a washed out, oddly saturated grade, which is exactly the
failure people spend an evening trying to fix in the primaries.

---

## 3. Installing the LUT so Resolve can see it

macOS:

```
/Library/Application Support/Blackmagic Design/DaVinci Resolve/LUT/
```

Windows is `C:\ProgramData\Blackmagic Design\DaVinci Resolve\Support\LUT`,
Linux is usually `/opt/resolve/LUT`. If in doubt, do not guess: Project
Settings > Color Management > Lookup Tables has an **Open LUT Folder** button
that opens the right one for your install.

Put the cubes in a subfolder so they show up as their own category rather than
scattered through the built-ins:

```bash
sudo mkdir -p "/Library/Application Support/Blackmagic Design/DaVinci Resolve/LUT/cinegrade"
sudo cp content/grade/luts/export/*.cube \
  "/Library/Application Support/Blackmagic Design/DaVinci Resolve/LUT/cinegrade/"
```

Then, in Resolve, either click **Update Lists** in Project Settings > Color
Management > Lookup Tables, or right-click in the LUT browser on the Color page
and choose **Refresh**. Resolve caches the list at startup, so a LUT copied in
while Resolve is running will not appear until you do one of those.

To apply: right-click a node > **LUTs** > cinegrade > the file, or drag it from
the LUT browser onto the node.

### Set the interpolation to Tetrahedral

Project Settings > Color Management > **3D Lookup Table Interpolation** >
Tetrahedral. Resolve ships with Trilinear selected, and every number in this
document was measured with tetrahedral, which is what cinegrade's own LUT stage
uses.

The difference is real. Same cube, same frame, `natural` preset:

| interpolation | worst pixel |
| --- | --- |
| Tetrahedral | 0.57 of 255 |
| Trilinear | 2.34 of 255 |

Four times the error for a setting you change once per project.

---

## 4. How close the cube gets

`bake_lut.py` renders one frame through the full cinegrade chain with the
spatial stages switched off, renders the same frame through nothing but the
baked cube, and diffs them. Results on `footage/A001_09011336_C002.MOV` at
two seconds, in 8-bit code values out of 255:

| preset | applelog (65) | rec709 (65) |
| --- | --- | --- |
| `flat` | 0.32 | 0.37 (33 grid) |
| `natural` | 0.32 | 0.25 |
| `cinekit` | 0.38 | 0.28 |
| `premium` | 0.52 | 0.26 |
| `forest` | 0.34 | 0.25 |
| `golden_haze` | 0.40 | 0.25 |
| `commercial` | 0.30 | 0.25 |
| `film_portrait` | 0.32 | 0.25 |
| `clean` | 0.46 | 0.33 |
| `punch` | 0.59 | 0.35 |
| `silverblue` | 0.76 | 0.28 |
| `interior` | **1.17** | 0.25 |
| `film_tungsten` | **1.46** | 0.25 |
| `reels` | **1.90** | 0.55 |
| `cinematic` | **2.03** | 0.80 |
| `blue_hour` | **2.99** | 0.48 |
| `blockbuster` | **4.28** | 0.64 |
| `blockbuster_max` | **6.59** | 0.57 |

The figure is the 99.9th percentile of the per-channel difference, so "0.32"
means 999 pixels in every thousand are within a third of one 8-bit step. Bold
entries are the ones the tool refuses to pass.

Two things worth understanding from that table.

**Every rec709 bake is accurate. Some applelog bakes are not.** A combination
LUT samples on a grid that is evenly spaced in *log*, but the looks and the
HSL secondary are 33-point cubes authored evenly spaced in *Rec.709*, and the
log to display curve is steepest through the mid tones. Around 18% grey, one
step of a 65-point log grid covers more display range than one step of a
33-point Rec.709 grid, so any kink in the look falls between two bake nodes
and gets straightened out. A Rec.709 domain bake samples in the look's own
space, where its nodes line up, which is why it wins by five to ten times on
exactly the presets that use a loud look. Nothing about the bake can fix that.
It is a property of the two grids.

**The maximum is not the number to judge on.** The worst single pixel on a real
frame is usually a few hundred pixels sitting exactly where one output channel
clips at 1.0, a hard corner no interpolation can follow. On `premium` against a
different frame the worst pixel is 14 code values off while the mean is 0.05
and only 278 pixels out of 8.3 million miss by more than 4. That is a speckle
in a blown highlight, not a wrong grade. The tool reports both so you can tell
the difference.

### When a preset does not bake cleanly

In order of preference:

1. **Bake `--domain rec709` instead** and put the conversion on node 1. This is
   the fix for every failing preset in the table above.
2. **Do the primaries in Resolve and bake a look-only cube.** Write a config
   with the look you want and nothing else, and the tool will give you a
   33-point Rec.709 cube. `blockbuster` reduced to look-only bakes at a 99.9th
   percentile of 0.41 against 4.28 for the full combination LUT.
3. **Render the clip with cinegrade** and hand Resolve the finished file. Zero
   bake error, and you get the FX as well.

Raising `--size` also helps (the error roughly halves from 65 to 129) but
Blackmagic documents 17, 33 and 65 as the sizes Resolve writes, and nothing I
could find confirms it loads anything larger. Treat a 129-point cube as
untested rather than as a solution, and note that it is a 55 MB text file.

---

## 5. What free Resolve genuinely cannot do

Take this as accurate for the free version in the 18 to 20 range. Blackmagic
moves individual effects between the tiers between releases, so if a specific
one matters to you, check it in your own install rather than trusting a list.

**Third-party OFX plugins do not load at all.** This is the hard one. Dehancer,
FilmConvert, Sapphire, Boris and everything else in that category are Studio
only. Most of the "cinematic film look" tutorials are built on Dehancer, and
none of that is reproducible in free Resolve by any route. Specifically:

- **Dehancer halation.** Dehancer models the real thing, light passing through
  the emulsion, bouncing off the film base and scattering back into the red
  sensitive layer. It is a threshold, a blur radius and a red-orange tint
  applied to isolated highlights.
- **Dehancer bloom.** Wider, weaker, more neutral than halation, a lens and
  emulsion diffusion effect rather than a film base one.
- **Dehancer grain.** Real scanned grain plates with a physical grain size,
  which is why it survives a downscale to 1080p when per-pixel noise does not.

**ResolveFX Film Grain, Glow, Halation and Film Look Creator are Studio.** The
first-party replacements for the above are behind the same paywall, which is
what leaves free Resolve with no route to any of it.

**Also Studio only, and worth knowing:** noise reduction (temporal and
spatial), the Neural Engine features including Magic Mask, and the higher
resolution and frame rate ceilings. Free Resolve is capped at 4K UHD output.

**Free Resolve does have** the Color Space Transform node, DaVinci Wide Gamut
as a timeline space, all the primaries and colour wheels, custom and soft
curves, HSL/RGB/luma qualifiers, power windows with the point tracker,
3D LUT application at any node, and the scopes.

### What that means for these presets

Of the eighteen shipped presets, exactly one (`flat`, which is the conversion
and nothing else) bakes completely. The other seventeen all have optical stages
that no cube can hold. For `cinekit` that means the cube gives you the colour
and free Resolve gives you nothing at all for `fx.halation`, `fx.bloom`,
`fx.rgb_split`, `fx.radial_blur`, `fx.vignette` or `grain`. `bake_lut.py` lists
the exact set for whichever preset you baked, both on screen and in the cube's
header comments.

Partial substitutes that free Resolve can actually do:

- **Vignette**: a circular power window on its own node, inverted, softened,
  with the gain pulled down. This is a real replacement, not an approximation.
- **Halation and bloom**: you can fake the shape with a node tree that keys
  highlights with a luma qualifier, blurs the key, tints it and composites it
  back. It is fiddly, it is not what Dehancer does, and it costs more time than
  rendering the clip through cinegrade.
- **Grain**: there is no honest substitute. A grain overlay clip in composite
  mode is the usual advice and it is not the same thing.
- **Sharpen and soften**: free Resolve's blur and sharpen palette does this.

---

## 6. The workflow this is actually for

Free Resolve is better than cinegrade at everything that needs eyes and a
mouse: shot matching, tracked windows, a qualifier you pull by dragging on the
picture, a curve you nudge while watching the scopes. cinegrade is better at
everything that is a repeatable calculation, and it is the only one of the two
that can do the optical effects at all without a Studio licence.

So use both, in this order:

1. **Pick the look here.** `./content/cinegrade compare IN.MOV --presets
   natural,cinekit,premium --time 8 --open`. One image, every candidate.
2. **Bake it `--domain rec709`** from a config with the look and nothing else,
   so the cube is purely creative and stays out of the way of your primaries.
3. **Grade in free Resolve.** Node 1 the conversion, node 2 your own balance
   and primaries, node 3 windows and qualifiers, node 4 the baked look cube.
   Setting the look last means the wheels on node 2 behave the way you expect,
   because you are correcting the picture rather than correcting the look.
4. **Bring the FX back here**, and know that this step has a rough edge.

### The FX-only pass, honestly

cinegrade has no "effects only" mode. Every `render` runs the conversion:
`f_convert_out` always applies a technical LUT, and no preset value turns it
off. So you cannot point `cinegrade render` at a Rec.709 export from Resolve
and get just the halation and grain. It would convert an already-converted
file and the picture would be wrong.

The graph itself is fine, it is only the CLI that has no entry point for it.
This runs, and was tested on a Rec.709 ProRes file:

```python
import sys, subprocess
sys.path.insert(0, "grade")
import cinegrade

cfg = cinegrade.load_preset("cinekit")     # only the fx/detail/grain half is used
src = "from_resolve.mov"                   # already Rec.709
info = cinegrade.probe(src)

segs = ["[0:v]format=gbrp16le[fx0]"]
fxs, cur, _ = cinegrade.build_fx(cfg, info, "fx0", 0)
segs += fxs
segs.append(f"[{cur}]" + ",".join(cinegrade.f_detail(cfg)
                                 + ["format=yuv422p10le"]) + "[vout]")
graph = ";".join(segs)

args = cinegrade.ffmpeg_inputs(src, cfg, info)
if cfg["fx"]["radial_blur"]["enabled"]:
    graph = "[1:v]format=gbrp16le,setsar=1[mask];" + graph
if cfg["grain"]["enabled"]:
    i = 2 if cfg["fx"]["radial_blur"]["enabled"] else 1
    graph = graph.replace("[vout]", "[pre]") + (
        f";[{i}:v]scale={info['width']}:{info['height']}:flags=bilinear,"
        f"format=gbrp16le,setsar=1[gp]"
        f";[pre][gp]blend=all_mode=overlay:all_opacity="
        f"{cfg['grain'].get('opacity', 0.5):.3f}[vout]")

# The grain plate is an endless lavfi source, so the output MUST be bounded or
# ffmpeg writes until the disk fills. This is not optional.
bound = (["-frames:v", str(info["nb_frames"])] if info.get("nb_frames")
         else ["-t", str(info["duration"])])

subprocess.run(args + ["-filter_complex", graph, "-map", "[vout]"] + bound
               + ["-c:v", "prores_ks", "-profile:v", "3", "out.mov"],
               check=True)
```

That is a workaround, not a feature. If the round trip matters, the engine
wants a real `fx` subcommand and a `convert.enabled` flag; until then, treat
the snippet as the thing that works rather than as the thing that should exist.

> Related bug, found while testing this and worth knowing before you start a
> long render: `cinegrade render` on any preset with `grain.enabled` and no
> `--duration` does not terminate either, for the same reason. The grain input
> is an endless `lavfi` source and nothing bounds the output. Left running for
> 90 seconds on a 12 frame clip it produced a 2.6 GB file. Until that is fixed
> in the engine, always pass `-t` when rendering a grain preset.

### The simpler answer

If none of the above appeals: render the whole clip with cinegrade and use
Resolve only to cut. No bake error, no domain confusion, no missing effects,
no round trip. For a clip that does not need shot-by-shot attention it is
strictly fewer steps, and it is the workflow this engine was built for. The
baked cube exists for the case where you genuinely want to grade by hand and
still start from a look that was already decided here.

---

## 7. Checklist before you blame the LUT

- Colour science is DaVinci YRGB, not Color Managed. (applelog domain)
- Nothing sits in front of the LUT node. (applelog domain)
- The conversion is on node 1 and the look on node 2. (rec709 domain)
- 3D Lookup Table Interpolation is Tetrahedral.
- Clip Attributes > Data Levels matches the file (Video for this camera's
  ProRes).
- The timeline colour space and output are Rec.709, not something managed.
- You clicked Update Lists after copying the file in.
- The cube's own header says what it expects. Open it in a text editor: the
  first 25 lines record the config, the domain, the node position, the date and
  every stage that was left out.
