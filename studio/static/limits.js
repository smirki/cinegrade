// The Limits panel content.
//
// Split out of app.js so the honest account of what this tool can and cannot do
// is editable without touching the application logic, and so the two can be
// worked on at the same time without colliding.
//
// The three groups are the point. "Cannot" and "not built yet" are very
// different answers to a user asking whether something is possible, and the old
// panel ran them together as one undifferentiated list of limitations, which
// made solvable gaps look like dead ends. Every claim below was measured on
// this machine against this build; the numbers are real, not estimates.
(function (global) {
  "use strict";

  var NOT_POSSIBLE = [
    ["No editing.",
     "One clip, one range, one output. There are no cuts, no multi clip " +
     "timeline, no transitions and no titles. Titles are not merely missing: " +
     "this ffmpeg build has no drawtext filter at all, so there is nothing to " +
     "call. An edit list is a different program, not a setting."],
    ["No audio work.",
     "Audio is copied through and re encoded to 256k AAC. Nothing is levelled, " +
     "filtered, ducked or synced. A colour tool has no business owning the mix."],
    ["No motion tools.",
     "No stabilisation, no optical flow, no retime, no motion blur, no lens " +
     "distortion correction. These need multi frame analysis, and this engine " +
     "renders each frame as an independent ffmpeg graph."],
    ["No tracking.",
     "Nothing can follow a subject across a shot. Same reason: a tracker needs " +
     "to carry state from frame to frame, and there is no place in a stateless " +
     "per frame filter graph to keep it."],
    ["No compositing.",
     "No alpha, no layers, no blending two clips together. Every graph has one " +
     "picture input."],
    ["No video preview of the source in Chrome.",
     "Chrome cannot decode Apple ProRes at all, whether in a &lt;video&gt; " +
     "element or through WebCodecs, so a browser side preview of the source video is " +
     "blocked by the decoder, not by anything the grade does. macOS Safari " +
     "can decode it, through VideoToolbox, so this is a Chrome limit rather " +
     "than a hardware or codec limit in general. Short of using Safari, the " +
     "only way to get a playable source into Chrome is a server made proxy " +
     "in a format Chrome can decode, and that proxy would have to be 10 bit " +
     "(HEVC): Apple Log packs its range into a narrow code window, and an 8 " +
     "bit H.264 proxy would band it visibly. Streaming the already graded " +
     "result instead sidesteps all of this, because a finished Rec.709 " +
     "image is exactly what 8 bit H.264 is for."],
    ["Not a calibrated reference.",
     "The grade is computed in 16 bit, but you are judging an 8 bit JPEG that " +
     "your browser then colour manages to your display profile. No amount of " +
     "work here turns a browser tab into a reference monitor."],
    ["Scopes measure the preview, not the render.",
     "They run on the 640 wide 8 bit preview frame, not the full resolution 10 " +
     "bit output. Read them for shape and balance, not as a broadcast QC pass."],
  ];

  var POSSIBLE = [
    ["Keyframes over time.",
     "Genuinely possible and demonstrated: ffmpeg's sendcmd filter can drive a " +
     "filter parameter from a timestamped script, and a real exposure ramp was " +
     "built this way as a test. The work is not the ramp, it is that every " +
     "parameter currently becomes a baked constant in a filter string, and " +
     "several are baked into LUT files (the secondary and the technical CSTs), " +
     "which sendcmd cannot animate. Animating a LUT backed control means " +
     "rebuilding the cube per frame."],
    ["Drawn power windows.",
     "Possible and demonstrated: a shape drawn with geq and composited with " +
     "maskedmerge produced a correctly masked blur (measured 0.55 against 8.02 " +
     "mean absolute laplacian inside versus outside the shape). The radial mask " +
     "already in the engine is exactly this technique, just with one fixed " +
     "shape. The missing part is a UI to draw and edit the shape and a way to " +
     "combine it with the existing qualifier, not any engine capability."],
    ["Several secondaries, or several look slots.",
     "Straightforward. A secondary is baked to a 33 cube and inserted as one " +
     "lut3d node, so a second one is a second node. The only real cost is that " +
     "each bake is a file write, so a chain of them needs a cache keyed on the " +
     "qualifier settings rather than one fixed filename."],
    ["Node reordering.",
     "Partly. The chain is assembled by string concatenation in build_graph, so " +
     "moving a stage is easy mechanically. What constrains it is that stages " +
     "have domains: curves and the look LUT are defined in Rec.709 display code " +
     "and are meaningless before CST OUT, and primaries are defined in the " +
     "working space. A free ordering would need each node to declare its domain " +
     "and the graph to insert conversions, which is a real design, not a switch."],
    ["HDR output.",
     "Possible. It needs new technical LUTs targeting PQ or HLG (make_cst.py " +
     "already takes a tone map and an encode, so this is mostly new encoder " +
     "entries), an output codec that carries the metadata, and a preview path " +
     "that does not lie, which is the hard part on an SDR browser."],
    ["Hardware accelerated final render.",
     "VideoToolbox specifically is barely worth it, and measured rather than " +
     "assumed. On a full render: decode alone 0.60s, decode plus the full " +
     "grade to null 15.24s, with the current prores_ks encode 18.30s, with " +
     "prores_videotoolbox 15.35s. So hardware encoding saves about 16 " +
     "percent. Hardware decoding makes it WORSE, 24.05s, because the frames " +
     "then have to come back off the GPU for CPU filters. That said, the " +
     "filters really are the cost: at 4K, 120 frames, the full cinematic " +
     "grade runs 52.4s (2.3 fps) against 1.54s (78 fps) to decode alone, so " +
     "about 50 of those 52 seconds are filters, and that is exactly where a " +
     "different route wins. gpu.js runs the same filter chain on the GPU " +
     "instead of the CPU. Measured in Chrome on this machine's Apple M5 " +
     "through Metal, at 1920x1080 the cinematic chain (128 GPU passes) ran a " +
     "worst case of 3.8ms per frame across twelve runs. (The median came " +
     "back at 0.1ms; that number is not trustworthy, because gl.finish() " +
     "does not reliably drain the queue on Metal, so the worst case is the " +
     "one to quote.) Scaling the 1080p figure by pixel count puts a 4K GPU " +
     "pass near 16ms a frame, an extrapolation from the measured 1080p " +
     "number, not a measured 4K number. A backend render on the GPU would " +
     "still pay a per frame GPU to CPU readback (about 33MB a frame at 4K) " +
     "to hand pixels to the encoder, and running gpu.js on the server means " +
     "running headless Chrome as a render process, because Node has no " +
     "WebGL2."],
    ["GPU accelerated preview exists, but only on test pages.",
     "gpu.js is a complete WebGL2 port of the render chain, and it has been " +
     "checked, not just built: parity against ffmpeg across 184 comparisons " +
     "came back 152 EXACT, 32 CLOSE, 0 FAILED. Two of those are honestly not " +
     "fixable: grain can never match, because ffmpeg's noise filter seeds " +
     "from the clock and does not reproduce itself run to run, and the 8 " +
     "bit yuv lane is CLOSE rather than EXACT because of swscale dithering, " +
     "worst case 4 of 255 on 0.43 percent of channels. It currently only " +
     "runs on gpupreview.html and parity.html. The main viewer still goes " +
     "through the server for every knob turn, which is the difference " +
     "between a knob costing 0.40s and costing about 4ms."],
    ["Real time playback.",
     "Measured end to end today, and it works. One ffmpeg pass at preview " +
     "size (4K Apple Log ProRes decode, scale to 960, the full cinematic " +
     "preset with every FX: halation, bloom, radial blur, rgb split, " +
     "vignette, grain, sharpen, then h264_videotoolbox encode to a playable " +
     "mp4) ran 120 frames (5 seconds of footage) in 3.32s, 36 fps, 1.5x " +
     "realtime. Decode and scale alone, no grade and no encode, ran the " +
     "same 120 frames in 2.17s, 55 fps. So at preview size the grade is not " +
     "the bottleneck, decoding is: if the grade cost nothing you would go " +
     "from 36 fps to 55 fps and stop. Realtime graded playback at preview " +
     "size is available today with the existing pipeline, it just is not " +
     "wired to a play button yet."],
    ["Auto shot match.",
     "Already built, and it is why this line moved out of the impossible group. " +
     "Match Reference fits a transform from a reference image and writes a look " +
     "cube. It does not extract the reference's LUT, because a JPEG does not " +
     "contain one; it moves the source's colour statistics toward the " +
     "reference's, and it reports how far it got and where it is unreliable."],
  ];

  var BROKEN = [
    ["RGB split lands on whole pixels only.",
     "partially works",
     "ffmpeg's rgbashift takes an integer pixel offset, so the amount is rounded " +
     "and anything under 0.5 does nothing at all. This one is inherent to the " +
     "filter, not a bug in the wiring."],
    ["RGB split reads stronger in the preview than it will render.",
     "known preview error",
     "The honestly scaled amount at a 960 preview of a 4K source rounds to zero, " +
     "which made the slider look dead while the render still split the channels. " +
     "The preview is now forced to at least one pixel, so a small amount is " +
     "overstated here by up to 4x. Overstating a real effect was judged the " +
     "lesser error against hiding it."],
    ["Grain is coarser in the preview than in the render.",
     "known preview error",
     "Grain size is an integer plate divisor that bottoms out at 1, so at " +
     "quarter scale a size of 3 becomes 1."],
    ["FX sizes are absolute pixels, so an unscaled preview overstates them.",
     "known preview error",
     "Halation sigma 46 and bloom sigma 140 are pixel counts, not relative " +
     "sizes, so at quarter resolution they cover four times as much of the " +
     "frame, and an unscaled preview shows far more bloom than the render " +
     "will produce. The engine has scale_for_preview for exactly this, and " +
     "anything that renders at a reduced size must route through it. Hand " +
     "building an ffmpeg command that bypassed it today produced a preview " +
     "that was visibly overcooked next to the real render. A preview that " +
     "lies is worse than no preview."],
    ["Sharpen is very subtle even at maximum.",
     "works, weakly",
     "At the top of the slider it moves the frame by a mean of 0.35 of 255 " +
     "(max 2). The direction is correct and unsharp is doing what it is asked; " +
     "the fixed 5 pixel matrix is simply small relative to a 4K frame. It also " +
     "reads stronger in the preview for the same reason: 5 pixels is a larger " +
     "share of a 960 wide frame."],
    ["The secondary's luma softness does nothing at the default window.",
     "works, but not at the defaults",
     "lum_low is 0 and lum_high is 1 out of the box, so there is no luma " +
     "boundary for softness to soften. Narrow the window and it works normally " +
     "(keying moved 32.3 to 35.1 percent in a test). Structurally a no op at " +
     "the shipped defaults rather than broken."],
    ["Vibrance does not protect saturated colour, despite the name.",
     "works as a saturation control, label was wrong",
     "Measured against a plain saturation multiply matched to the same overall " +
     "strength, vibrance put 11.5% of its gain on flat colour and the plain " +
     "multiply put 11.7%, so it is not doing the thing the name promises. It " +
     "still adds saturation and it is not inert. Seven shipped presets use it " +
     "at 0.1 to 0.22, so changing the filter would move looks that were already " +
     "approved; the tooltip was corrected instead. ffmpeg's alternate formula " +
     "does bias toward flat colour more, and is available if the look is ever " +
     "re-approved."],
    ["Brightness is not as flat an offset as the name suggests.",
     "partially works",
     "It is added before the tone map, so the highlights get compressed more " +
     "than the shadows: at +0.15 the 5th percentile moved +56, the median +97 " +
     "and the 95th only +23 code values. It is a real control, just not a " +
     "uniform lift."],
    ["The studio preview and the CLI render differ very slightly on some presets.",
     "known preview error",
     "On presets that combine a vignette with sharpen and no grain, a minority " +
     "of pixels differ by up to 4 to 6 of 255, mean well under 1. The cause is " +
     "ffmpeg choosing a YUV conversion matrix for vignette and unsharp based on " +
     "metadata inherited across the whole graph, which differs between a live " +
     "decode and a cached source frame handed over a pipe. Closing it fully " +
     "would mean changing what the CLI has always rendered, which was not worth " +
     "the trade."],
  ];

  var FIXED = [
    ["fx.vignette.radius", "was inert, now works",
     "Stored in every preset and never read: radius 0.2, 0.85 and 1.6 rendered " +
     "byte identical. It now scales ffmpeg's vignette angle, which is exact " +
     "rather than an approximation because the vignette factor is cos(angle * d) " +
     "to the fourth, so scaling the distance and scaling the angle are the same " +
     "operation. 0.85 reproduces the old fixed falloff, so no existing preset " +
     "moved."],
    ["convert.encode on the direct path", "was ignored, now works",
     "The direct path loaded one LUT with no encode in its name, which " +
     "make_cst.py bakes with its own default of gamma24. So the control was not " +
     "just inert, the UI said rec709a while the picture was gamma24. Per encode " +
     "cubes fixed it, and the gamma24 ones are byte identical to the old files."],
    ["halation and bloom strength", "was capped, now goes further",
     "Strength was only a blend opacity, which ffmpeg refuses above 1.0, and a " +
     "wide gaussian drops peak amplitude in proportion to how far it spreads, so " +
     "even the maximum was faint. Above 1.0 the extra now drives the glow " +
     "layer's amplitude instead. At or below 1.0 the output is byte identical, " +
     "so no shipped preset changed."],
    ["primaries.saturation above about 2.08", "used to fail the render",
     "One saturation matrix exceeded ffmpeg's coefficient limit and errored out, " +
     "so the top of the slider crashed instead of saturating. It is now split " +
     "across several passes. The split is exact rather than an approximation: " +
     "the matrix composes, so N passes of the Nth root equal one impossible pass."],
    ["The contrast pivot on the direct working space", "was the wrong number",
     "Contrast pivoted on 0.3919, a Rec.709 output code, while primaries on that " +
     "path actually operate on raw Apple Log where mid grey is 0.4883. Contrast " +
     "1.6 therefore dragged 18 percent grey by 33 of 255, which is the exact " +
     "thing a pivot exists to prevent."],
    ["black_lift and highlight_rolloff", "moved the midtones",
     "Both were drawn as one three point spline anchored at 0.5, inside a " +
     "working space where mid grey is 0.336, so the anchor sat on a highlight " +
     "and the curve dragged the midtones with it: black_lift 0.10 pushed grey " +
     "+6.0 of 255 and highlight_rolloff 0.35 pushed it +8.2. They are now a toe " +
     "and a shoulder that each fall off to nothing at mid grey, meeting it with " +
     "a slope of exactly 1 so there is no kink and no midtone contrast change. " +
     "The half of the range you are not touching is pinned to identity with its " +
     "own anchor points, because ffmpeg's spline otherwise takes its shape from " +
     "the far end and drifts: that drift was 20 of 255 at the top of the " +
     "black_lift slider and is now 2. Tracking the same pixels across a real " +
     "frame, black_lift +0.15 now lifts the darkest 5% by +10.0 of 255 while " +
     "mid grey moves +0.29 and the brightest 5% move -0.00."],
    ["tonemap none on the default working space", "would not render",
     "The option was offered by the CLI and the UI but the cube it asks for was " +
     "never generated, so choosing it killed the render. The rebuild loop now " +
     "covers every tonemap and encode combination on both paths."],
    ["Renders with grain never finished", "hung until killed",
     "The grain plate was an unbounded lavfi source and the blend waited for " +
     "every input to reach EOF, so a 12 frame clip wrote gigabytes and ran " +
     "forever unless a duration was passed. The plate is now bounded by the " +
     "clip's own duration and the blend ends with the picture."],
    ["Ordinary Rec.709 footage was silently wrecked", "produced garbage, no error",
     "Loading a normal mp4 applied a log to display conversion to a picture that " +
     "never had a log curve: median luma fell 0.332 to 0.238 and saturation rose " +
     "0.33 to 0.59, giving neon colour and blown skin, with no error at any " +
     "point. There is now a rec709 working space that grades in place, and a " +
     "mismatch between the source's tag and the chosen space is refused with a " +
     "message saying which mode to use."],
  ];

  function rows(items) {
    return items.map(function (it) {
      return "<li><b>" + it[0] + "</b> " + it[it.length - 1] + "</li>";
    }).join("");
  }

  function verdictRows(items) {
    return items.map(function (it) {
      return "<li><b>" + it[0] + "</b> <i>(" + it[1] + ")</i> " + it[2] + "</li>";
    }).join("");
  }

  global.LIMITS_HTML = [
    "<p>Three different answers to \"can this tool do X\", kept apart on ",
    "purpose. Everything here was measured against this build, not assumed ",
    "from reading the code.</p>",

    "<h4>1. Not possible here</h4>",
    "<p>Real limits of what this program is. These are not on a roadmap; they ",
    "would be a different tool.</p><ul>", rows(NOT_POSSIBLE), "</ul>",

    "<h4>2. Possible, not built yet</h4>",
    "<p>Each of these was checked rather than guessed at, and the note says ",
    "what the actual work is.</p><ul>", rows(POSSIBLE), "</ul>",

    "<h4>3. Broken, inert, or honestly imperfect</h4>",
    "<p>What is still wrong or approximate, stated plainly.</p><ul>",
    verdictRows(BROKEN), "</ul>",

    "<h4>Recently fixed, and what was actually wrong</h4>",
    "<p>These were all dead or damaging controls found by measuring every ",
    "parameter with the effect off and on. They are listed because a control ",
    "that used to lie is worth knowing about if you graded anything with it.</p>",
    "<ul>", verdictRows(FIXED), "</ul>",

    "<h4>Effects that depend on the shot</h4>",
    "<p>Halation and bloom are threshold gated screen blends, so they need real ",
    "blown highlights to do anything. On a clip with bright windows against a ",
    "dark interior, strength 1 moves 10.9 percent of pixels; on flat golden hour ",
    "footage with no bright source, the same setting moves 0.46 percent and even ",
    "strength 8 barely registers. That is the effect working correctly on ",
    "unsuitable material, not a fault. Radial blur needs edge detail at the ",
    "radius it affects. Vignette is a pure radial multiply and behaves the same ",
    "on any shot.</p>",
  ].join("");
}(window));
