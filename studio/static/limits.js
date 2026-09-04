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
     "The engine half is built and shipped: one ellipse or rectangle, with " +
     "centre, extent, rotation, feather and invert, gating the secondary " +
     "through a geq baked matte and maskedmerge, which is the same technique " +
     "the radial blur ramp already used. It has sliders in the Window panel " +
     "and it renders. What is still missing is the drawing part: there is no " +
     "way to drag the shape on the picture, no handles for the extent or the " +
     "rotation, and no softness ring, so a window is positioned by numbers " +
     "rather than by eye. The GPU shader port is missing with it, which is " +
     "why an enabled window drops the viewer back to the server render. Both " +
     "are the next wave of work. See the three window entries below for what " +
     "the shipped half does and does not do."],
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
     "checked, not just built. " +
     "<span id=\"parityClaim\">Parity numbers are read from the last harness " +
     "run when this panel opens.</span> " +
     "Some of it is honestly not " +
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
    ["Bypass shows the technical conversion, not the camera's log image.",
     "works, by definition",
     "Bypass is the engine defaults with the tone map, working space and output " +
     "encode copied from the live config, because those three are the conversion " +
     "and not the grade. It is the same picture the server's flat mode has always " +
     "produced: measured on the first frame of A001_09011336_C002 with the cinematic " +
     "preset loaded, the GPU's bypassed picture reads a mean luma of 90.80 of 255 " +
     "and the server's flat frame of the same frame reads 90.96, against 126.68 " +
     "for the graded picture. It is deliberately NOT the raw log frame: a log " +
     "image next to a graded one compares the grade against an unwatchable " +
     "picture rather than against the neutral conversion it started from. If the " +
     "log frame is what you want, this control will not give it to you."],
    ["The split wipe costs a second render per frame on the server path.",
     "works, at a cost",
     "With the wipe on, every frame change is two POST /api/frame calls instead " +
     "of one (measured 2 against a baseline of 1 for the same scrub with the wipe " +
     "off), because the graded picture and the bypassed picture are two separate " +
     "server renders. On the GPU path it is free of network entirely: entering " +
     "the wipe cost 0 /api/frame and 0 /api/source, and a frame change with the " +
     "wipe on cost 1 /api/source (the same source decode the graded render needs " +
     "anyway) and no extra request, because the bypassed picture is a second GPU " +
     "pass over the source frame already on the card. Toggling bypass on and " +
     "back off on the GPU path cost 0 requests of either kind."],
    ["The stats and the scopes follow bypass only when bypass is the whole picture.",
     "partially works",
     "With bypass on (before only) they measure the bypassed picture: the readout " +
     "moved from mean 8-bit 126.1 to 90.8 and back on the same frame. In the wipe, " +
     "left/right and top/bottom modes they keep measuring the GRADED picture, " +
     "because two pictures are on screen and there is one set of numbers. That is " +
     "a choice, not an oversight (the graded picture is the one being judged), but " +
     "it does mean a scope read in a split view describes only half of what you " +
     "are looking at."],
    ["Passwords cross the network in the clear.",
     "real, unfixable from inside this program",
     "The built in server is Python's http.server and speaks plain HTTP only. " +
     "With logins on, the name and password posted to /api/auth/login and the " +
     "session cookie sent back on every request afterwards are readable by " +
     "anyone who can see the traffic. Nothing inside this codebase can change " +
     "that: the fix is a TLS reverse proxy (nginx, Caddy) in front of it, " +
     "started then with --behind-https-proxy so the cookie is also marked " +
     "Secure. On 127.0.0.1 the traffic never leaves the machine, so this only " +
     "bites on a network deployment. The server refuses to bind a non loopback " +
     "address at all unless logins are on and an account exists, but it cannot " +
     "tell whether a proxy is really in front of it."],
    ["The sign in rate limiter forgets everything on restart.",
     "in memory only",
     "Five failed attempts per name and twenty per client address in a five " +
     "minute window return 429. Those counters are a dict in this one process: " +
     "restarting the server clears them, and two servers behind a load " +
     "balancer would each count separately. It raises the cost of guessing a " +
     "password through a browser; it is not a defence against an attacker who " +
     "can restart the clock. What does the real work is the hashing: scrypt at " +
     "n=32768, r=8, p=1 costs 32 MB and measured 52 ms to hash and 70 ms to " +
     "verify on this machine, so a single guess costs about 0.07 seconds of " +
     "CPU even with the limiter switched off."],
    ["Agent tokens never expire.",
     "by design, and the cost is real",
     "A token created from the user menu is 43 characters and stays valid " +
     "until somebody deletes it. That is deliberate, so an agent running for " +
     "days does not die halfway, but it means a leaked token is a permanent " +
     "key until it is revoked by hand. The server stores only a SHA-256 of it, " +
     "so a stolen database gives up no live tokens, and no endpoint can show " +
     "an existing token again. There is no expiry field and no rotation."],
    ["Presets, looks and the live session are shared between accounts.",
     "accounts do not isolate work yet",
     "Logins gate who can reach the studio; they do not yet separate what each " +
     "account sees. Saved presets, imported look cubes, the render output " +
     "folder, the frame cache and the one live session config are all a single " +
     "shared set, so two people signed in at once are grading the same state " +
     "and can overwrite each other's presets. Only the file browser is per " +
     "account: with logins on it is confined to content/footage plus that " +
     "account's own folder. Per user grades and presets are the next wave of " +
     "work, not a setting that can be turned on today."],
    ["The server itself has not been hardened for hostile traffic.",
     "unaudited",
     "This is Python's ThreadingHTTPServer, which the standard library's own " +
     "documentation says is not recommended for production. There is no " +
     "request size cap, no connection cap, no slow request timeout, and no " +
     "durable log of failed sign ins; one thread is spawned per connection. " +
     "Logins keep out somebody who has no account. They are not a defence " +
     "against somebody deliberately attacking the process, and nothing here " +
     "has been fuzzed or load tested against bad input."],
    ["Histogram match can fail its own health check on a shot where reinhard passes.",
     "documented trade of the per channel design, not a bug",
     "Measured on the same clip, reference, crop, strength (1.0) and " +
     "luma_preserve (true): A001_09011832_C003.MOV at 8s against IMG_2570.PNG. " +
     "reinhard passed (probes_ok true, skin probe hue shift 6.3 degrees, mid " +
     "grey channel spread 0.028). histogram failed the identical check " +
     "(probes_ok false): the skin probe's R>=G>=B order broke (in [0.55, 0.40, " +
     "0.32] out [0.53, 0.34, 0.38], plausible false) and mid grey read out at a " +
     "channel spread of 0.146, both flagged as a neutral cast. This is the " +
     "trade match_ref.py's own module docstring names: histogram is monotonic " +
     "per channel, so no single channel can invert on its own, but nothing " +
     "ties the three channels to each other the way reinhard's affine move in " +
     "the decorrelated Lab space does, so the cross channel relationships the " +
     "probes check (a grey card staying grey, skin keeping its channel order) " +
     "can still break even though no single channel posterised. The endpoint " +
     "refuses to apply a result like this on its own (result.ok false); it " +
     "does not silently ship it."],
    ["The power window gates the secondary and nothing else.",
     "works, and only there",
     "The window wraps the qualifier cube and only the qualifier cube: the " +
     "graph splits just before it, grades one branch and merges the two back " +
     "through the matte. Primaries, curves, the look LUT, every FX and grain " +
     "still cover the whole frame, so a window cannot be used to darken one " +
     "corner or to soften one face. With the secondary switched off the " +
     "window drops out of the graph entirely (no filter, no extra ffmpeg " +
     "input) rather than sitting there doing nothing visible."],
    ["One power window, and it does not move.",
     "works, within those limits",
     "There is one window in the config, so two shapes cannot be combined, " +
     "subtracted from each other, or given different corrections. It is also " +
     "fixed for the length of the shot: nothing tracks a subject and nothing " +
     "keyframes the shape, for the same reason the tracking entry above " +
     "gives, which is that every frame is an independent ffmpeg graph with " +
     "no state carried between frames. On a moving subject the window has to " +
     "be drawn wide enough to hold the subject for the whole range."],
    ["The window matte is 8 bit, like the radial blur ramp.",
     "works, at 256 levels",
     "The matte is baked once by a geq expression into an 8 bit grey PNG and " +
     "read back as an ordinary ffmpeg input, so the correction is mixed in " +
     "256 steps, not continuously. That was measured rather than assumed: the " +
     "ffmpeg expression and the numpy reference implementation agree exactly " +
     "(max difference 0 of 255 across ten shape and size combinations at " +
     "1920x1080 and 320x568). The scaling into the 16 bit merge is exact too, " +
     "but only because the matte is routed through gray16le on the way in. " +
     "Straight to 16 bit planar RGB, which is what the radial ramp does, " +
     "ffmpeg expands 8 bits with a left shift, so matte code 255 arrives as " +
     "65280 of 65535 and a fully open window applies 99.61% of the " +
     "correction: measured at a mean 0.186 of 255 on 18.6% of pixels before " +
     "that hop was added, and exactly 0 after it."],
    ["A power window switches the GPU preview off.",
     "known preview limit, until the shader port lands",
     "The window is in the ffmpeg engine only. gpu.js has no window stage at " +
     "all, so it would not even report it as unsupported: it would grade the " +
     "whole frame and show a picture that quietly ignores the shape you just " +
     "drew, which is the worst kind of preview error. The viewer therefore " +
     "treats an enabled window as blocking and falls back to the server " +
     "render for as long as it is on. Everything still works, it is just the " +
     "slower path. The shader port is the next wave of work."],
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
    ["The GPU preview of the black lift and highlight rolloff curve",
     "drew a different curve from the render",
     "The toe and the shoulder are written as explicit points and handed to " +
     "ffmpeg's pchip spline. The engine spells the half you are NOT touching " +
     "out as six points sitting on y=x; gpu.js was spelling it out as a single " +
     "endpoint. pchip takes every node's slope from its neighbouring secants " +
     "and weights them by the neighbouring interval widths, so that one lone " +
     "endpoint made the untouched half one enormous interval and let the " +
     "graded half bend it. Measured by pushing a 65536 entry 16 bit ramp " +
     "through ffmpeg 8.1.1 and diffing the transfer: at black_lift 0.08 the " +
     "GPU table was 278 of 65535 away from ffmpeg at x = 0.678 (1.08 code " +
     "values at 8 bit), and at highlight_rolloff 0.35 on the direct path it " +
     "was 1191 of 65535 (4.63 at 8 bit). It is now within 1 of 65535 on all " +
     "18 slider combinations tested, which is the float versus integer " +
     "rounding tie. In the full parity sweep prim_black_lift went from FAILED " +
     "(max 2 of 255, 3.15 percent of pixels off by more than 1) to EXACT (max " +
     "1, 0.00 percent), and the four presets that inherit it (film_portrait, " +
     "interior, punch, reels) went from FAILED to CLOSE, with pixels off by " +
     "more than 1 falling from 1.77 to 0.21 percent (film_portrait), 1.94 to " +
     "0.25 (interior), 2.35 to 0.40 (punch) and 2.67 to 0.35 (reels). Sweep " +
     "totals moved from 150 EXACT, 24 CLOSE, 10 FAILED to 152 EXACT, 32 " +
     "CLOSE, 0 FAILED of 184."],
    ["The curve editor's drawn line", "was not the curve that rendered",
     "controls.js took the two end slopes of its pchip spline from the plain " +
     "end secant. ffmpeg uses scipy's _edge_case formula there instead. On the " +
     "S curve 0/0 0.25/0.18 0.75/0.82 1/1, measured against ffmpeg 8.1.1 on a " +
     "65536 entry ramp, the drawn line was 454 of 65535 away from the render " +
     "at x = 0.0803, which is 1.77 code values at 8 bit. A two point curve was " +
     "wrong in a second way: ffmpeg extrapolates that straight line past both " +
     "ends and the editor drew it flat. Both are fixed, and the drawn line now " +
     "matches ffmpeg within 1 of 65535 on all five curves tested."],
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

  /* The GPU parity claim is printed from studio/tools/parity-results.json, the
   * file the harness in parity.html POSTs back after a run, rather than typed
   * in by hand. A hand typed count is stale the moment the harness runs again,
   * and this panel exists precisely so nobody has to take a claim on trust.
   * If the report cannot be read the panel says so instead of claiming a
   * number. */
  function parityClaimText(rep) {
    if (!rep || !rep.counts || !rep.runCount) {
      // The route answers 200 with {} when the file is absent, so an empty
      // object means "never run here", not "zero comparisons failed".
      return "No parity numbers to show: studio/tools/parity-results.json has "
           + "not been written on this server, so nothing is being claimed "
           + "here. Run studio/static/parity.html to generate it.";
    }
    var c = rep.counts || {};
    var total = rep.runCount || 0;
    var when = String(rep.generated || "").slice(0, 10) || "an unrecorded date";
    var failedIds = [], seen = {};
    (rep.rows || []).forEach(function (r) {
      if (r.verdict === "FAILED" && !seen[r.id]) { seen[r.id] = 1; failedIds.push(r.id); }
    });
    var s = "Parity against ffmpeg over " + total + " comparisons ("
          + (rep.configCount || 0) + " configurations at "
          + ((rep.widths || []).join(" and ") || "one") + " wide), measured on "
          + when + ": " + (c.EXACT || 0) + " EXACT, " + (c.CLOSE || 0)
          + " CLOSE, " + (c.FAILED || 0) + " FAILED.";
    if (failedIds.length) {
      s += " Still failing: " + failedIds.join(", ") + ".";
    }
    return s;
  }

  /* Fill the placeholder after the dialog body has been written. app.js sets
   * limitsBody.innerHTML synchronously in its own click handler, so this runs
   * on a document level listener and defers by a tick, which makes it correct
   * whichever order the two scripts registered in. */
  function refreshParityClaim() {
    var node = document.getElementById("parityClaim");
    if (!node) return;
    fetch("/api/parity/report", { cache: "no-store" }).then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    }).then(function (rep) {
      node.textContent = parityClaimText(rep);
    })["catch"](function (e) {
      node.textContent = "No parity numbers to show: GET /api/parity/report "
        + "failed (" + e.message + "), so nothing is being claimed here. Run "
        + "studio/static/parity.html to regenerate the report.";
    });
  }

  document.addEventListener("click", function (e) {
    var t = e.target;
    while (t && t !== document) {
      if (t.id === "limitsBtn") { setTimeout(refreshParityClaim, 0); return; }
      t = t.parentNode;
    }
  });

  global.refreshParityClaim = refreshParityClaim;

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
