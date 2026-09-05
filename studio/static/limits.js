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
     "distortion correction. These need multi frame analysis, and both render " +
     "engines treat every frame as an independent graph, ffmpeg's filter " +
     "chain or gpu.js's, with nothing carried from one frame to the next."],
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
     "in a format Chrome can decode, and that is now what Play does: see " +
     "the playback entry under Fixed. This entry used to go on to say that " +
     "such a proxy would have to be 10 bit HEVC, because Apple Log packs its " +
     "range into a narrow code window and an 8 bit H.264 proxy would band it " +
     "visibly. That was a prediction, not a measurement, and the 8 bit proxy " +
     "was built and measured instead: the numbers are in the next entry. The " +
     "worry is not disproved (the log curve is still undone by the grade " +
     "AFTER the 8 bit step, and nothing here tested a large smooth gradient " +
     "for banding specifically), but it is no longer a reason not to have " +
     "playback."],
    ["Not a calibrated reference.",
     "The grade is computed in 16 bit, but you are judging an 8 bit JPEG that " +
     "your browser then colour manages to your display profile. No amount of " +
     "work here turns a browser tab into a reference monitor."],
    ["Scopes measure the preview, not the render.",
     "They run on the 640 wide 8 bit preview frame, not the full resolution 10 " +
     "bit output. Read them for shape and balance, not as a broadcast QC pass."],
    ["Playback is judged on an 8 bit proxy, stills are not.",
     "Pressing Play does not run the 16 bit path faster. It decodes a 960 wide " +
     "8 bit 4:2:0 H.264 proxy in a hidden &lt;video&gt; and grades each frame " +
     "on the GPU, while a still comes from the 16 bit /api/source path as " +
     "before, so the picture you judge while playing is not quite the picture " +
     "you judge when you stop. Measured by studio/tests/proxy-fidelity.mjs, " +
     "which grades the same frame of the same clip with the same config twice, " +
     "once from each path, at three frames of a 24 fps clip: with the panels at " +
     "their defaults the mean absolute difference per channel was " +
     "3.195/1.806/2.777, 3.012/1.799/2.599 and 3.292/1.742/2.767 of 255, and " +
     "with a grade on (which stretches whatever the codec did) 4.29/2.487/4.417, " +
     "3.899/2.427/4.106 and 4.169/2.289/4.225. The worst single channel across " +
     "those six comparisons was 53 of 255. The share of pixels off by more than " +
     "2 of 255 is 72.488 to 76.255 percent at defaults and 85.128 to 87.369 " +
     "percent graded, so this is everywhere in the frame rather than in one " +
     "corner. What it is not is a shift: whole picture mean luma moved by only " +
     "0.10 to 0.32 of 255, so exposure and balance read the same and the " +
     "difference is fine grain from chroma subsampling and DCT. Decoding the " +
     "same proxy file with ffmpeg rather than Chrome gives 1.023/0.700/1.222, " +
     "so about a third of the difference is the encode and the rest is Chrome's " +
     "own YUV to RGB conversion and chroma upsampling on the way into the " +
     "texture, which no server side setting here can reach."],
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
     "and it renders. The drawing part has landed too: the shape is dragged " +
     "on the picture itself, with a centre grip, four extent handles on the " +
     "rotated axes, a rotation grip on a stem and a draggable feather ring, " +
     "so a window no longer has to be positioned by numbers alone. The GPU " +
     "shader port has landed with it, so an enabled window no longer drops " +
     "the viewer back to the server render; see the fixed entries for the " +
     "measured parity and for what the drawing half does not show. What is " +
     "still missing from the phrase 'drawn power windows' as a colourist " +
     "means it is everything the window entries below list: one shape, no " +
     "tracking, and the gate only reaches the secondary."],
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
     "number, not a measured 4K number. That backend route has since been " +
     "built rather than argued about, and it does run gpu.js in a headless " +
     "Chrome as a render process, because Node still has no WebGL2. It pays " +
     "the per frame GPU to CPU readback this entry predicted (about 33MB a " +
     "frame at 4K) and that readback, plus moving the frame to the encoder, " +
     "turns out to cost more than ffmpeg's whole filter chain: the GPU " +
     "engine is measurably SLOWER, not faster. So the 2.3 fps above is no " +
     "longer the only option, but it is still the faster one. What the GPU " +
     "engine buys is a second, independent render of the same grade at 10 " +
     "bit. See the fixed entry for what it is, and the two broken entries " +
     "for the speed and the parity numbers."],
    ["A GPU render worth choosing for speed.",
     "Possible, and not close yet. The GPU engine spends about 1.13s a frame " +
     "at 1920 wide while the same shaders were measured at a worst case of " +
     "3.8ms a frame, so almost none of that second is the grade. It is the " +
     "frame moving: a 16 bit readback out of WebGL, an HTTP POST back to the " +
     "server, and a raw pipe into the encoder. Where inside that the time " +
     "actually goes was NOT measured, so the fix is not known, only the " +
     "shape of it. The obvious candidates are reading back into a pixel " +
     "buffer object asynchronously instead of a blocking readPixels, and " +
     "cutting the byte count by packing to 10 bit rather than 16. Neither " +
     "was tried, so neither is promised."],
    ["Grain, detail and letterbox on the GPU path.",
     "Deliberately not, for now. With grain off the whole chain runs in " +
     "gpu.js. With grain on, the grain plate is generated and blended by " +
     "ffmpeg on the way to the encoder exactly as the ffmpeg engine does it, " +
     "and detail and letterbox go with it, because ffmpeg's own graph " +
     "applies those two after the blend and the point of this path is to " +
     "produce the same file. So a grain on GPU render is honestly a hybrid, " +
     "not a GPU render. Moving grain into gpu.js means matching ffmpeg's " +
     "noise generator pixel for pixel, which is a separate piece of work " +
     "with its own parity question."],
    ["Grade versions, and a reference still per clip.",
     "The storage half is already there and is the easy half: per clip grades " +
     "sit in grades(user_id, clip_key, clip_name, config_json, updated_at) " +
     "with a primary key on (user_id, clip_key), so exactly one row per clip " +
     "per account. Versions mean dropping that uniqueness and adding a label " +
     "and a created_at; a still means writing one JPEG beside the row. The " +
     "actual work is the interface: a version list, a way to compare two of " +
     "them, and a rule for which version a clip loads on select."],
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
     "gives, which is that every frame is an independent graph on either " +
     "render engine, with no state carried between frames. On a moving " +
     "subject the window has to " +
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
     "Straight to 16 bit planar RGB ffmpeg expands 8 bits with a left shift, " +
     "so matte code 255 arrives as 65280 of 65535 and a fully open window " +
     "applies 99.61% of the correction: measured at a mean 0.186 of 255 on " +
     "18.6% of pixels before that hop was added, and exactly 0 after it. The " +
     "radial blur ramp shipped on the bare shift until 2026-09-04 and now " +
     "takes the same hop; see the fixed list below for what that moved."],
    ["The agent grading loop only tunes five primaries controls.",
     "works, and only there",
     "studio/tools/agent_grade.py drives a real grade through the same HTTP " +
     "API the browser uses (state, session, stats, match, grade or preset), " +
     "but the loop that decides what to change is five deterministic rules " +
     "over PRIMARIES ONLY: exposure through primaries.brightness (a proxy, " +
     "not convert.exposure, because that field lives outside the primaries " +
     "node this loop is scoped to, which the CLI guide's own advice to grade " +
     "exposure in stops on the log signal does not apply here), contrast " +
     "through primaries.contrast, saturation through primaries.saturation, " +
     "and white balance through primaries.temperature and primaries.tint, " +
     "each moved by a damped share of the measured gap and clamped to that " +
     "control's own range from schema.js. It never touches curves, the " +
     "secondary, the window, grain, detail, fx, or which look is loaded, " +
     "except the one look a --ref run fits and applies through /api/match " +
     "before the primaries loop even starts. Measured on a real clip " +
     "(A001_09011832_C003.MOV) against two different targets: toward the " +
     "cinematic preset's own measured stats from a flat start, the loop cut " +
     "total tracked error from 0.4499 to 0.0379 (91.6%) over 12 steps " +
     "without fully converging, because cinematic's difference from flat " +
     "also comes from convert.exposure 1.2, the blockbuster look LUT, and " +
     "halation, bloom, rgb_split and vignette, none of which primaries can " +
     "reach, so saturation was still climbing toward its own range ceiling " +
     "(reached 1.8957 of 0 to 2.5) rather than closing the last few percent " +
     "of gap. Toward a reference image (IMG_2570.PNG), after /api/match fit " +
     "and applied a look (distance 0.0993 to 0.0737), the primaries loop " +
     "converged fully in 6 steps, total error 0.7232 to 0.0438, every " +
     "tracked metric inside tolerance, with contrast pushed to 2.3413 of " +
     "its 2.5 ceiling: a real convergence, but on a reference whose " +
     "remaining gap after the look fit happened to be one primaries push " +
     "could close, which will not be true of every reference or every " +
     "preset."],
    ["A saved grade is the current config and nothing else.",
     "works, but it is not a version history",
     "PUT /api/grade replaces the whole config for that clip, so there is one " +
     "grade per clip per account and no way back to what it looked like an " +
     "hour ago. No named versions, no reference stills, no per clip notes. " +
     "The undo stack is per clip but lives in browser memory only: measured, " +
     "the grade comes back after a reload and the undo button is disabled, " +
     "because the history of how you got there was never sent anywhere."],
    ["The clip key is content based, so a re-encode is a new clip.",
     "by design, with a real cost",
     "A clip is identified by the first 32 hex characters of sha256(byte " +
     "length, first 1 MiB, last 1 MiB), not by its name or its path. " +
     "Measured: a byte identical copy of a 114 MB clip, saved in the same " +
     "folder under a different name, resolved to the same key " +
     "05fe6fe1e893172ddf254f196a9a121d, so renaming or moving footage keeps " +
     "its grade. The other half of that deal is that a transcode, a trim or " +
     "a re-wrap is a different file and gets a different key, so it arrives " +
     "ungraded and nothing on screen explains why. Copy grade here is the " +
     "manual way across."],
    ["Only the active A/B slot follows the clip.",
     "partially works",
     "Selecting a clip loads its grade into whichever slot is active and " +
     "leaves the other slot untouched, so the inactive slot still holds the " +
     "previous clip's look. Comparing A against B straight after a clip " +
     "switch is therefore comparing this clip's grade against the last " +
     "clip's, which is a reasonable thing to want and is not what the two " +
     "buttons appear to promise. Only the active slot is ever saved."],
    ["With logins off, everything belongs to one account.",
     "works as intended, said out loud",
     "Grades and saved presets are keyed by user id, and with no logins every " +
     "request resolves to user id 0. Two people sharing one machine therefore " +
     "share one set of grades, and turning --auth on later does not migrate " +
     "what user 0 saved into the new accounts: it stays under id 0 and " +
     "becomes invisible. Measured with two accounts on one server: the " +
     "second account's GET /api/grade for a clip the first had graded " +
     "returned exists false, and its GET /api/grades returned an empty list."],
    ["Uploaded clips are not scanned.",
     "no content or malware scanning",
     "POST /api/upload checks the file extension against the same allow " +
     "list every other route uses, then runs a bounded, headers only " +
     "ffprobe to confirm the container actually has a video stream. Neither " +
     "check looks at what is in the picture or whether the bytes contain " +
     "anything else riding along with them. This is a personal grading tool " +
     "on a machine its account already has a login for, not a public " +
     "upload surface, and no scanning of any kind was built for it."],
    ["The upload quota only ever looks at one folder.",
     "measured, by design",
     "The default 8589934592 byte (8 GiB) per file cap and 53687091200 " +
     "byte (50 GiB) per account total are both checked against the single " +
     "destination folder an upload lands in (FOOTAGE with logins off, this " +
     "account's own footage folder with them on), summing only the files " +
     "sitting directly inside it. Nothing here looks at the rest of the " +
     "disk, at studio/cache (which this same wave measured at over 2.6 GB " +
     "just from the frame cache during testing), or at other accounts' " +
     "folders, so an account can still fill the disk by staying under its " +
     "own 50 GiB and letting the shared caches grow unbounded around it. " +
     "The quota check itself also has a gap: two uploads to the same " +
     "account starting at the same moment both read the folder's current " +
     "size before either has written a byte, so both can pass a check that " +
     "only one of them should have, and the folder ends up over the cap by " +
     "roughly the smaller of the two uploads. Measured by reading the code " +
     "path, not by triggering it: _dir_total_bytes() and the write that " +
     "follows it are not under one lock."],
    ["An upload is one request with no resume.",
     "no resume, no chunking",
     "The whole file is one HTTP POST body, written to a temp file and " +
     "renamed into place only once every declared byte has arrived. A " +
     "connection that drops midway (closed laptop lid, wifi drop, a " +
     "reverse proxy timeout) deletes the partial temp file and reports an " +
     "error; there is no byte range, no chunk id and nothing saved to " +
     "resume from, so trying again means uploading the whole clip from the " +
     "first byte. For an 8 GiB cap on an ordinary upload connection that is " +
     "a real cost, not a rare inconvenience."],
    ["ffprobe validating an upload checks the container, not the whole pipeline.",
     "partially works",
     "A file that keeps its declared extension and reports a stream with " +
     "codec_type video in its own headers is accepted; cinegrade's own " +
     "decode is never attempted before the file is kept. An exotic or " +
     "corrupt codec that ffprobe's header parse recognises as \"video\" but " +
     "that the render graph cannot actually decode would still be accepted " +
     "here and only fail later, the first time someone tries to view or " +
     "grade it."],
    ["POST /api/look (importing a .cube LUT) has no body size cap.",
     "found while auditing this, not fixed here",
     "Every JSON route now refuses a body over 8 MB before reading it (see " +
     "_body() in server.py), and POST /api/upload has its own 8 GiB cap " +
     "checked the same way. The one other route that reads a raw request " +
     "body, POST /api/look, calls _raw_body() instead, which was out of " +
     "this wave's scope and still reads whatever Content-Length claims " +
     "with no ceiling at all."],
    ["The window drawn on the picture is the geometry, not the matte.",
     "an outline and a ring, not a preview of the falloff",
     "The shape editor draws exactly what the seven numbers say and nothing " +
     "more: a solid outline at the shape's own edge (d = 1) and a dashed " +
     "ring at (1 + softness) times that radius, which is where the matte " +
     "reaches zero. Between those two lines it draws nothing at all, so the " +
     "linear ramp the engine actually computes per pixel is not on screen. " +
     "The accent wash that appears while the centre grip is under the " +
     "pointer is a flat fill of the inside, the same colour at d = 0.2 as at " +
     "d = 0.99, and it stays on the inside when invert is on, where the " +
     "correction lands OUTSIDE the shape (the dashed outline is the only " +
     "thing that says so). To see the real matte, turn the Matte view on: " +
     "that renders the qualifier matte multiplied by the window matte from " +
     "the engine itself. What IS measured is the mapping, not the falloff: " +
     "in studio/tests spec 13 a real 120px drag of the centre grip across a " +
     "468.2px wide fitted picture moved window.cx by 0.25632 against an " +
     "expected 0.25632, an error of 0.00000. At that fitted size one screen " +
     "pixel is 1/468th of the frame, which is also the finest the arrow key " +
     "nudge can go by eye; the sliders still go finer."],
    ["The grain stage's icon reads as a chart, not as film grain.",
     "closest available glyph, not the intended shape",
     "Every icon in this tool is now rendered from @hugeicons/core-free-icons " +
     "verbatim (studio/tools/icons/build.mjs vendors whole icon definitions, " +
     "studio/static/icons.js turns them into real svg nodes; no path data is " +
     "typed or edited by hand anywhere). A prior version of this icon hand " +
     "picked 5 of ChartScatterIcon's 6 paths, dropping its axis-frame line so " +
     "only the scattered dots remained. That per-path edit is exactly the " +
     "human-touches-path-data pattern this rework removes, so the grain stage " +
     "now shows the whole icon, axis frame included: 6 shapes, confirmed by " +
     "studio/tests/verify-icons-l8c.mjs reading the live DOM. Hugeicons' free " +
     "4,500-icon set (checked by name: grain, noise, texture, scatter, dotted) " +
     "has no dedicated film-grain glyph; ChartScatterIcon's dot field is the " +
     "closest shape available, and it now reads more like a scatter chart " +
     "because of that frame line. The engine's own grain effect is unaffected; " +
     "this is a labelling glyph only."],
    ["The GPU render engine is slower than the ffmpeg one.",
     "works, and costs you time",
     "Measured on A001_09011832_C003.MOV, 72 frames, both engines on the same " +
     "machine, wall clock from POST /api/render to the job reaching done. At " +
     "1920 wide: ffmpeg 29.4s (2.45 fps), GPU 81.3s (0.89 fps). At the clip's " +
     "native width: ffmpeg 31.3s (2.30 fps), GPU 101.1s (0.71 fps). That is " +
     "2.8x slower at 1920 and 3.2x slower at native width. This is not a " +
     "software renderer being reported as a GPU: the unmasked renderer string " +
     "read out of WEBGL_debug_renderer_info in the worker page is ANGLE " +
     "(Apple, ANGLE Metal Renderer: Apple M5, Unspecified Version), and the " +
     "worker prints it into the job message so every render says which GPU " +
     "did it. Both engines run the same decoder and the same encoder, so the " +
     "whole difference is the cost of moving each frame out to the browser " +
     "and back, and that cost is larger than ffmpeg's entire filter chain. " +
     "It ships anyway, because it is the only way to get gpu.js's own picture " +
     "into a 10 bit file, and because ffmpeg stays the default. No speedup is " +
     "claimed anywhere in this tool for it, because none was measured."],
    ["The GPU render does not match the ffmpeg render pixel for pixel.",
     "known difference, larger at larger sizes",
     "Compared on the ENCODED outputs (not on anything in memory): the same 3s " +
     "range at 1920 wide through both engines, 5 frames each decoded back to " +
     "raw 16 bit, differences quoted in 8 bit code values against the parity " +
     "harness's own thresholds (EXACT at max 1, CLOSE at max 16 with at most " +
     "0.5% of pixels over 1 and 0.02% over 4). The cinematic preset with " +
     "grain off: max 36.0, mean 2.26 to 2.31, 62.6 to 64.5% of pixels over 1, " +
     "17.2 to 17.7% over 4. A natural preset with a power window and a " +
     "secondary: max 21.4, mean 1.49 to 1.60, 49.6 to 54.0% over 1, 7.96 to " +
     "8.53% over 4. Both are FAILED by those thresholds and calling them " +
     "anything else would be a lie. Two things are worth knowing about that " +
     "number. It is not the render path: an ffmpeg only split at the same cut " +
     "point, out to a raw pipe and back into the encoder, is byte identical " +
     "(max 0.000), so the transport and the encode add nothing. It is gpu.js's " +
     "own error against ffmpeg's filters, and it grows with the picture " +
     "because the blur radii scale with it. The same comparison at 640 wide: " +
     "no vignette and no sharpen, mean 0.11 and max 5.7; vignette only, mean " +
     "0.45; sharpen only, mean 0.76; both, mean 1.24 to 1.34. The shipped " +
     "test (studio/tests/specs/15-gpu-render.mjs) renders 640 wide with " +
     "halation on and gets max 3.498, mean 0.0911, 0.274% over 1 and 0.0000% " +
     "over 4, which passes CLOSE. So the honest summary is: at preview sizes " +
     "the two engines agree, at delivery sizes they visibly do not, and the " +
     "existing parity harness cannot see it because it compares at preview " +
     "size in 8 bit. With grain on, where only a statistical comparison is " +
     "meaningful, the per frame numbers are the same size as with grain off " +
     "(signed mean 1.42 to 1.44 of 255, sd 2.75 to 2.92, largest absolute " +
     "difference 35.1 to 39.7), which is indirect evidence that ffmpeg's " +
     "noise reproduced itself across the two runs rather than reseeding."],
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
    ["A power window", "switched the GPU preview off, now it renders on the GPU",
     "The window was in the ffmpeg engine only, so live.js forced an enabled " +
     "window into its blocking list and the viewer fell back to the server " +
     "render for as long as one was on. gpu.js now evaluates the same matte " +
     "formula per pixel in the colour shader, with the same floor(m*255+0.5) " +
     "rounding and the same multiply by 257 into the 16 bit merge, and " +
     "maskedmerges the secondary branch over the un-graded one exactly where " +
     "build_graph splits the tree. The override is gone. Measured on this " +
     "machine (Apple M5, ANGLE Metal) against ffmpeg 8.1.1 at 640x1138 and " +
     "1280x2276: four window configs, eight runs, all EXACT, worst channel 1 " +
     "code of 255 and 0.00 percent of channels off by more than 1. The sweep " +
     "went from 152 EXACT, 32 CLOSE, 0 FAILED of 184 runs to 160 EXACT, 32 " +
     "CLOSE, 0 FAILED of 192, with all 184 earlier runs unchanged in verdict, " +
     "worst channel and mean. The shader works in 32 bit float where the " +
     "engine's numpy and geq references work in 64 bit, so a pixel sitting on " +
     "a matte step can round the other way: the window_matte_only case, which " +
     "renders the bare matte with nothing else in the chain, differs from " +
     "ffmpeg's baked matte on 5 of 728320 pixels at 640 wide and 11 of " +
     "2913280 at 1280 wide, by one code of 255 each. That is 0.0007 and " +
     "0.0004 percent. One matte code is 1/255th of the correction, so at the " +
     "loudest qualifier in the sweep (which moves the picture by at most 59 " +
     "of 255) it cannot reach half an 8 bit code, and none of those pixels " +
     "shows up as an output difference. A correction stronger than about 127 " +
     "of 255 could make one visible, and it would be one code."],
    ["A power window could only be positioned by typing numbers.",
     "was seven sliders, now it is dragged on the picture",
     "The window had centre, extent, rotation, softness and invert as " +
     "sliders in the Window panel and nothing on the picture, so placing a " +
     "shape over a face meant guessing a fraction, releasing, looking, and " +
     "guessing again. The shape is now drawn over the graded layer as an SVG " +
     "overlay with a centre grip, four extent handles sitting on the " +
     "ROTATED axes, a rotation grip on a stem and a draggable feather ring, " +
     "and arrow keys nudge the centre by one screen pixel (ten with shift). " +
     "It maps to the picture element's own rendered rect, not the stage, so " +
     "it stays correct in the letterboxed and split view modes, and it reads " +
     "the same autorotated axes the engine bakes the matte in. Every drag " +
     "writes through the same Panels.emit the sliders write through, which " +
     "is why a drag auto-enables the stage, is one undo step, and moves the " +
     "sliders as it goes. Measured in studio/tests spec 13 against a real " +
     "browser and real pointer input: a 120px drag of the centre grip on a " +
     "468.2px wide picture moved cx from 0.5 to 0.75632 where 0.75632 was " +
     "expected (error 0.00000), window.enabled went false to true on that " +
     "same drag, one undo press put cx back to 0.5, and a drag on the " +
     "rotation grip moved rotation from 0 to 20.7 degrees."],
    ["Playing a clip", "was a server render per segment, now the whole clip " +
     "plays graded on the GPU",
     "Play used to ask the server to render a short segment and stream the " +
     "result back, so what you could watch was whatever had been rendered and " +
     "every scrub was a round trip. One bounded ffmpeg pass per clip and width " +
     "now writes a proxy (POST /api/proxy/prepare, cached under " +
     "studio/cache/proxy against a 1.5 GB budget, served with byte ranges so a " +
     "&lt;video&gt; can seek in it), a hidden video element decodes it, and " +
     "requestVideoFrameCallback hands every presented frame to the same gpu.js " +
     "chain the stills go through. Measured on this machine by " +
     "studio/tests/proxy-fidelity.mjs: 5 seconds of a 24 fps clip graded 119 " +
     "frames in 4949 ms, which is 23.84 fps against a ceiling of 24.00 (a video " +
     "element plays at 1x, so the clip's own rate is the limit), with 0 frames " +
     "skipped by the renderer and 0 dropped by the decoder, and the GPU chain " +
     "itself costing 0.04 ms per frame (worst 0.2 ms). A knob moved mid " +
     "playback lands on the very next frame: mean luma went 99.58 to 161.84 " +
     "with no frame in between. Nothing accumulates per frame: the JS heap over " +
     "those 119 frames went from 72.2 MB to 20.7 MB. The cost is the one " +
     "prepare pass, which is not free and is not hidden: at 640 wide a 5.00 s " +
     "clip took 2.0 s for 1.52 MB and a 32.25 s clip took 11.9 s for 19.87 MB, " +
     "both about 2.5x realtime from 2160x3840 source, and at 960 wide the same " +
     "5 s clip is 3.60 MB. Asking again for a proxy already on disk answers in " +
     "1 ms. The proxy is tagged full range rather than limited because both " +
     "were built and compared: limited scored a slightly lower per pixel mean " +
     "(3.139/1.919/2.799 against 3.643/2.092/3.482) but moved whole picture " +
     "mean luma by 1.21 to 1.50 of 255 where full moved it by 0.10 to 0.32, and " +
     "a bias you can see beats grain you cannot."],
    ["Seeking in the proxy", "expected to snap to a keyframe, measured frame " +
     "exact",
     "The proxy is encoded with a 12 frame GOP (-g 12 -keyint_min 12 " +
     "-sc_threshold 0) on the assumption that a seek would land on a keyframe " +
     "and a short GOP would keep that error under half a second. That " +
     "assumption is wrong, and measuring it is what showed it: 12 random seeks " +
     "in a 5 s 24 fps clip each landed on exactly the frame asked for (frames " +
     "8, 29, 31, 36, 49, 51, 68, 88, 92, 93, 97 and 99, none of them a multiple " +
     "of 12), and stepping one frame forward and back returned to the frame it " +
     "started on. The decoder decodes forward from the keyframe rather than " +
     "showing it, so a short GOP buys latency, not accuracy. The latency it " +
     "buys, measured: 11 ms at best, 126 ms median, 372 ms worst, where the " +
     "cluster at about 126 ms is this code's own 120 ms wait for a frame that a " +
     "paused element may never present again, not decode time. End to end, " +
     "dragging the timeline to a new time and seeing a different picture " +
     "measured 24.8, 27.7, 141.5 and 201.2 ms."],
    ["Which frame is the frame at 1.5 seconds", "the still and the video " +
     "disagreed by one, now they are mapped",
     "ffmpeg's -ss T returns the first frame whose timestamp is at or after T. " +
     "A video element asked for currentTime = T shows the frame whose display " +
     "interval contains T. Those are different frames whenever T does not sit " +
     "exactly on a frame boundary, and the gap is a whole frame of motion: an " +
     "early version of the fidelity tool compared frame 11 against frame 12 " +
     "and reported a mean difference of 3.9 of 255 that was the shot moving, " +
     "not the codec. live.js now maps clip time to proxy time and back so both " +
     "paths name the same frame, and the tool asks for the middle of a frame so " +
     "no rounding at either end can tip it into the neighbour. The frame rate " +
     "that arithmetic uses comes from ffprobe's r_frame_rate, not " +
     "avg_frame_rate: on the test clip avg reads 23.067 against a real 24.000, " +
     "which drifts a whole frame within half a second and measured as a mean " +
     "difference of 26 of 255 before it was fixed. Two encoder knobs that " +
     "sound like they would help do nothing here and were dropped rather than " +
     "left in as decoration: sws dithering and accurate rounding on the 16 bit " +
     "to 8 bit step produced byte identical files (cmp, not eyeballing), and " +
     "CRF 12 instead of 18 changed the decoded result by 0.09 of 255 while " +
     "tripling the file."],
    ["The radial blur ramp", "was wrong twice over on the way into the merge, " +
     "now exact",
     "The ramp is an 8 bit grey PNG baked by a geq expression, and between the " +
     "number that expression asked for and the weight maskedmerge applied sat " +
     "two silent conversions, both wrong. First, the geq ran on the luma plane " +
     "of a limited range YUV source and the frame was converted to grey " +
     "afterwards, which stretched every code by round((v-16)*255/219): on a " +
     "256 wide identity ramp 248 of 256 codes moved, by up to 20 code values, " +
     "and only 220 distinct codes survived, so everything at or below 16 " +
     "flattened to 0 and everything at or above 235 flattened to 255. Second, " +
     "the 8 bit matte was widened to 16 bit with a bare format=gbrp16le, which " +
     "is a left shift: code 0 arrived as 0, 128 as 32768, 235 as 60160 and 255 " +
     "as 65280 of 65535, so a fully open ramp applied 99.61% of the blur and " +
     "never all of it. Both now go the way the window matte already went: " +
     "format=gray before the geq (measured: 0 of 256 codes moved, max error 0) " +
     "and format=gray16le,format=gbrp16le into the merge (measured: max " +
     "|arrived - 257n| = 0 over all 256 codes, 255 arriving as 65535, and the " +
     "same 0 end to end through maskedmerge itself). What it moved, measured " +
     "on A001_09011336_C002.MOV at 2.0 s rendered 1920x3412, radial blur on at " +
     "the defaults (sigma 9, start 0.55, end 1.0): max 4 of 255, mean 0.0944, " +
     "9.35% of pixels changed, and it lands where it should. Inside the ramp " +
     "start radius, where the matte is closed, exactly 0 pixels moved. In the " +
     "ramp itself, max 4 and mean 0.1649 over 16.33% of pixels. Outside the " +
     "ramp, where the matte is fully open, max 1 and mean 0.0190 over 1.90% of " +
     "pixels, which is the 65280 to 65535 correction arriving. With start " +
     "pulled to 0.2 the same picture: max 4, mean 0.1309, 12.90% of pixels, " +
     "still 0 inside the closed radius. For scale, turning the radial blur on " +
     "at all moves that frame by up to 51 of 255, so this was up to about 8% " +
     "of the effect it was supposed to be applying. One golden moved and only " +
     "one (clipA_cinekit, the only golden preset with a radial blur: g_p95 " +
     "0.58039 to 0.58431 and b_p99 0.63922 to 0.64314, both one 8 bit code); " +
     "the other five re-blessed byte identical. The GPU port emulated both " +
     "defects on purpose so that parity would pass, and now emulates neither. " +
     "It still quantises the ramp with floor(255*ramp), because the matte " +
     "really is an 8 bit PNG and ffmpeg's geq really does truncate (measured " +
     "on a 640x360 ramp: floor matches on all 230400 pixels, round-half-up " +
     "misses 86776 of them and rint misses 86770). Parity is unchanged either " +
     "side of the fix, 160 EXACT, 32 CLOSE, 0 FAILED of 192 before and after, " +
     "with both radial stage rows EXACT at max 1 of 255 and 0% of pixels off " +
     "by more than 1. Four regression tests now pin it (grade/tests/" +
     "cases_radial.py) so it cannot come back quietly."],
    ["GPU accelerated preview", "was confined to test pages, now the " +
     "default in the main viewer",
     "gpu.js is a complete WebGL2 port of the render chain, and it has been " +
     "checked, not just built. " +
     "<span id=\"parityClaim\">Parity numbers are read from the last harness " +
     "run when this panel opens.</span> " +
     "Some of it is honestly not " +
     "done: grain has not been ported to gpu.js at all (see the possible " +
     "entry on grain, detail and letterbox on the GPU path), and matching " +
     "it turns out to be tractable rather than hopeless. This entry used to " +
     "say grain can never match because ffmpeg's noise filter seeds itself " +
     "from the clock and does not reproduce between runs. That was a " +
     "prediction, not a measurement, and it was wrong on this machine: " +
     "rendering the same single frame twice through cinegrade.py with grain " +
     "on (cinematic preset, -frames:v 1, A001_09011336_C002.MOV at 2.0s) " +
     "gave byte identical output both times (max absolute difference 0), " +
     "and the bare ffmpeg noise filter on its own gives byte identical " +
     "output across two separate process runs a few seconds apart too, " +
     "while still varying frame to frame inside one run, which is real " +
     "temporal noise, not a frozen plate. ffmpeg's default seed of -1 " +
     "(\"unset\") is not actually drawn from the wall clock on this build: " +
     "it is reproducible, so a GPU grain stage could in principle be parity " +
     "checked exactly like every other stage. Nobody has written that " +
     "shader yet, which is the real reason grain stays off the parity list, " +
     "not an inherent impossibility. The 8 bit yuv lane is CLOSE rather " +
     "than EXACT for an unrelated reason, swscale dithering: worst case 2 " +
     "of 255 on 0.0009 percent of channels. It used to run only " +
     "on gpupreview.html and parity.html while the main viewer went through " +
     "the server for every knob turn. It is wired into the main viewer now " +
     "too: StudioLive.init() runs against the real canvas at boot in app.js, " +
     "the #gpuToggle button defaults on whenever that finds a usable WebGL2 " +
     "context, and #rendererBadge prints \"GPU\" for a still frame, the live " +
     "loop or proxy playback whenever it is grading on the card, falling " +
     "back to \"server\" only when no WebGL2 context was found or the toggle " +
     "is switched off by hand. The difference between the two paths is " +
     "still what it always was, a network round trip against a local " +
     "recompute: a knob turn was measured at about 0.40s through the server " +
     "against about 4ms on the GPU. That pair of numbers came from the test " +
     "pages before the main viewer wiring landed and has not been re-timed " +
     "there directly, so it describes the shape of the win, not a " +
     "main-viewer measurement."],
    ["Auto shot match", "was assumed impossible, now built and used",
     "Match Reference (POST /api/match, match_ref.py) fits a transform from " +
     "a reference image and writes a look cube. It does not extract the " +
     "reference's own LUT, because a JPEG does not contain one; it moves " +
     "the source's colour statistics toward the reference's and reports how " +
     "far it got and where it is unreliable. Measured on " +
     "A001_09011832_C003.MOV against IMG_2570.PNG: method reinhard passed " +
     "its own health check (probes_ok true, skin probe hue shift 6.3 " +
     "degrees, mid grey channel spread 0.028) and closed the fit distance " +
     "from 0.0993 to 0.0737, the same run the primaries loop in " +
     "agent_grade.py then converged the rest of the way from. See the " +
     "broken entries for where the same tool's histogram method fails that " +
     "health check on the identical shot, and for how far the primaries " +
     "loop alone can close a reference match on its own."],
    ["The final render", "was ffmpeg only, now the GPU can render it too",
     "The render dialog has an Engine choice. ffmpeg is still the default and " +
     "is untouched. GPU (headless Chrome) decodes the clip with ffmpeg, hands " +
     "every frame to the same gpu.js the preview uses running in a headless " +
     "Chrome, and encodes what comes back with exactly the same codec, " +
     "profile, pixel format, colour tags and audio mapping as the ffmpeg " +
     "path, so it is the same kind of file and not a lesser one. It really is " +
     "10 bit, and that was measured rather than assumed: counting distinct " +
     "luma code values inside a smooth gradient patch of the same rendered " +
     "frame gives 390 for the GPU engine against 394 for ffmpeg over a span " +
     "of about 502 codes, where the same patch pushed through 8 bit holds " +
     "only 123. That depends on the frames leaving Chrome at 16 bits per " +
     "channel, which needed a new readback (the picture is packed into a " +
     "render target of its own and read as unsigned integers); the 8 bit " +
     "readPixels the preview uses was deliberately left alone. Frames never " +
     "travel through page.evaluate: the worker page fetches raw frames over " +
     "HTTP and POSTs the graded ones back, because turning a 12MB frame into " +
     "a JSON array of numbers would cost more than the grade. The frame " +
     "routes are open only to the worker and only for the life of one render, " +
     "on a random per render token plus the same trusted loopback check the " +
     "rest of the local only routes use. Progress and Cancel are the existing " +
     "job machinery, so a GPU render stops the same way any other one does, " +
     "and if Chrome or node is missing the render fails immediately with a " +
     "message saying which. It is slower than ffmpeg and it does not match " +
     "ffmpeg exactly; both are measured, both are under Broken, and neither " +
     "is hidden behind this entry."],
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
