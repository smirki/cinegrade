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
     "No alpha, no blend modes, no blending two clips together. Every graph " +
     "has one picture input. The layer stack is not a contradiction of this: " +
     "its layers are serial corrections applied to the same single picture " +
     "and merged back through a grey matte, not separate images composited " +
     "over one another. There is nothing to composite because there is " +
     "nothing to composite with."],
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
     "several are baked into LUT files (every layer in the stack, and the " +
     "technical CSTs), which sendcmd cannot animate. Animating a LUT backed " +
     "control means rebuilding the cube per frame, and the stack multiplies " +
     "that: one cube per layer per frame, or two for the one layer shape " +
     "that needs two branches (an inverted mask with a window AND a key)."],
    ["Drawn power windows.",
     "The engine half is built and shipped: one ellipse or rectangle, with " +
     "centre, extent, rotation, feather and invert, gating the layer it " +
     "belongs to " +
     "through a geq baked matte and maskedmerge, which is the same technique " +
     "the radial blur ramp already used. It has sliders in a layer's Window " +
     "group and it renders. The drawing part has landed too: the shape is dragged " +
     "on the picture itself, with a centre grip, four extent handles on the " +
     "rotated axes, a rotation grip on a stem and a draggable feather ring, " +
     "so a window no longer has to be positioned by numbers alone. The GPU " +
     "shader port has landed with it, so an enabled window no longer drops " +
     "the viewer back to the server render; see the fixed entries for the " +
     "measured parity and for what the drawing half does not show. What is " +
     "still missing from the phrase 'drawn power windows' as a colourist " +
     "means it is everything the window entries below list: one shape per " +
     "layer, no union or difference between shapes inside a layer, and no " +
     "tracking. The last clause of this sentence used to read 'and the gate " +
     "only reaches the secondary', which is no longer true: a window now " +
     "belongs to a layer and gates that layer's whole correction."],
    ["Several look slots.",
     "The several secondaries half of this entry has shipped, and it shipped " +
     "the way the entry predicted: the single secondary and its single " +
     "window became the layer stack, any number of masked corrections, each " +
     "baked to its own 33 cube and inserted as its own lut3d node, with the " +
     "bake cached on the correction and qualifier settings rather than on " +
     "one fixed filename. See the layer entries in the next section for what " +
     "that costs and what it still cannot do. Several LOOK slots has shipped " +
     "too, but not the way this predicted: not a second node in series, lut1 " +
     "feeding into lut2, but a parallel blend, two looks read from the same " +
     "pre-look signal and combined by balance and mix2 rather than stacked. " +
     "See the FIXED entry below, 'Look now has two slots, but they blend in " +
     "parallel, not a stack.', for the measurements."],
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
     "Still deliberately not, but for a different and narrower reason now. " +
     "gpu.js has a real grain shader (see the interactive preview entry " +
     "above) and it measures exact against ffmpeg on a rendered still. This " +
     "GPU render path (render_gpu.py, the one that produces the actual " +
     "output file) still routes grain to ffmpeg regardless: _gpu_config " +
     "forces grain off in the config it hands to the browser, and when the " +
     "source config had grain on it takes detail and sharpen with it, " +
     "because ffmpeg's own graph applies those two after the grain blend " +
     "and the point of this path is to produce the same file. The reason is " +
     "not that gpu.js cannot render grain any more; it is that the plate " +
     "gpu.js fetches is one frame's worth (cached per config and size), not " +
     "one plate per output frame the way ffmpeg's noise filter genuinely " +
     "advances across a real multi frame export. Wiring gpu.js's grain into " +
     "a many frame render would mean advancing that plate once per output " +
     "frame and proving it stays byte identical to ffmpeg's own advancing " +
     "plate across a whole clip, not just on frame 0 of a still, which is " +
     "separate work nobody has done. So a grain-on GPU render is honestly " +
     "still a hybrid, not a GPU render, for that reason now, not for a " +
     "missing shader."],
    ["Grade versions, and a reference still per clip.",
     "The storage half is already there and is the easy half: per clip grades " +
     "sit in grades(user_id, clip_key, clip_name, config_json, updated_at) " +
     "with a primary key on (user_id, clip_key), so exactly one row per clip " +
     "per account. Versions mean dropping that uniqueness and adding a label " +
     "and a created_at; a still means writing one JPEG beside the row. The " +
     "actual work is the interface: a version list, a way to compare two of " +
     "them, and a rule for which version a clip loads on select."],
    ["Reordering layers is buttons, not a drag.",
     "Each layer in the Layers panel has a Move up and a Move down button; " +
     "there is no drag handle. Moving a layer N rows costs N clicks, where a " +
     "drag would be one gesture; studio/tests/specs/16-layers.mjs moves a " +
     "two layer list by one row with one button click, undoable in one " +
     "#undoBtn press same as any other edit here. A real drag would need a " +
     "pointer capture on the row itself instead of a fixed button, the same " +
     "job gridstack-all.js already does for the dock widgets, so the shape " +
     "of the fix is known, it is just not what shipped here."],
    ["Apple Log 2 input curve.",
     "not shipped: Apple's own document could not be fetched",
     "Today's Apple Log decode (f_convert_in/f_convert_out baking " +
     "AppleLog_to_DWG.cube etc. via grade/tools/make_cst.py, which calls " +
     "colour-science's log_decoding_AppleLogProfile / " +
     "log_encoding_AppleLogProfile) has no Apple Log 2 counterpart, and the " +
     "installed colour-science 0.4.7 has no AppleLog2 model either. The rule " +
     "for adding one was that only Apple's own published document counts as " +
     "a source for the transfer function formula, the primaries " +
     "chromaticities and sample values, never a third party page quoting " +
     "constants. Searched: developer.apple.com's own Apple Log 2 page " +
     "(avfoundation/avcapturecolorspace/applelog2), which is real but prose " +
     "only, \"an Apple defined Log curve\" with no formula or numbers; " +
     "Apple's Final Cut Camera 2.0 newsroom announcement, which confirms " +
     "Apple Log 2 shoots on iPhone 17 Pro but gives no spec; " +
     "developer.apple.com's downloads search for the white paper, which " +
     "redirects to an idmsa.apple.com Apple ID sign-in (no credentials for " +
     "that, and using any if they existed would not make it a public " +
     "source); guessed direct PDF URLs following the existing Apple Log " +
     "white paper's naming pattern, which all redirect to " +
     "developer.apple.com/unauthorized/; and web and site-restricted " +
     "searches for the CoreMedia AppleLog2 transfer/colour constants, which " +
     "turn up only generic constant-name descriptions, never the curve " +
     "itself. The only pages with actual numbers were a Threads post and a " +
     "couple of colour grading blogs, both explicitly disqualified by the " +
     "sourcing rule above. Nothing was invented to fill the gap: no " +
     "apple_log2 option exists in convert.input, no code claims to decode " +
     "it, and no clip shot on it exists in footage/ to test against even if " +
     "constants were guessed (footage/ holds exactly the two clips this " +
     "whole suite already uses, A001_09011336_C002.MOV and " +
     "A001_09011832_C003.MOV, checked directly, neither Apple Log 2)."],
    ["Denoise on the GPU path.",
     "Deliberately not. prep.denoise is ffmpeg's hqdn3d, which has no GPU " +
     "port here, so turning it on reports \"unsupported\" in stageReport " +
     "and the whole still falls back to a server render. Grain used to be " +
     "the one exception to that: stageReport called it unsupported too, " +
     "but checkStages exempted it from forcing a fallback and the GPU " +
     "quietly rendered without the effect instead of dropping to the " +
     "server. Grain has since been ported and stageReport now calls it " +
     "exact, so that exemption is gone; see the possible entry on grain, " +
     "detail and letterbox above. denoise has no equivalent shortcut: " +
     "hqdn3d itself is not the slow part: rendering the same still five " +
     "times each way at 1920 wide with spatial and temporal both at 0.5 " +
     "gave a median of 0.513s with denoise on against 0.515s off, so the " +
     "filter adds no measured time of its own. The real cost of turning it " +
     "on is the one every server fallback pays, a network round trip " +
     "against a local recompute: measured elsewhere in this panel at about " +
     "0.40s through the server against about 4ms on the GPU (see the GPU " +
     "accelerated preview entry). That gap, not hqdn3d, is what a user " +
     "actually feels when the enabled toggle is switched on."],
    ["mid_detail is one radius, not Resolve's multi-scale midtone tool.",
     "Resolve's midtone detail tool blends several blur radii at once, each " +
     "with its own weight, to push texture at more than one scale " +
     "simultaneously. This is one gaussian (sigma 2 percent of the frame " +
     "width) and one blend, so it is one scale of local contrast, not " +
     "several. Measured on real footage at 1920 wide, after the clamp fix in " +
     "the fixed list below: mid_detail 1.0 moves the whole frame by a mean of " +
     "6.7 of 255 and -1.0 by 6.8, so about the same amount either way. The " +
     "7.6 that stood here before came from the build whose blend wrapped, " +
     "which on the identical frame reported 8.8 for 1.0 against the same 6.8 " +
     "for -1.0. A genuinely sharp edge already in the shot swings far more " +
     "than that at that one edge, which is the trade a single radius makes " +
     "and a multi-scale tool would not."],
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
    ["Temporal denoise cannot show properly on a still.",
     "works, but a still is the wrong test",
     "hqdn3d's temporal term needs motion between frames to do anything real; " +
     "a still is one frame shown twice, so there is nothing for it to compare. " +
     "It is not a perfect no op there either: rendering the same still at 1920 " +
     "wide with spatial 0 and temporal 0 versus temporal 1 measured a max " +
     "difference of 4.15 of 255 and a mean of 0.95, which is hqdn3d's own " +
     "rounding on its first frame (confirmed with a bare ffmpeg hqdn3d run " +
     "outside this engine, not a bug introduced here), not a directional " +
     "effect: the signed difference was 42 percent positive, 47 percent " +
     "negative, 11 percent exactly zero. On an actual 12 frame render the " +
     "same comparison at frame 6 measured a mean of 2.52 of 255 and a max of " +
     "24.45, several times the still's residual, which is temporal denoise " +
     "doing real work once there is motion to use."],
    ["The layer key's luma softness does nothing at the default window.",
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
    ["A mask gates its own layer and nothing else.",
     "works, and only there",
     "A layer's mask (its window, its colour key, or the two multiplied " +
     "together) wraps that layer's correction and only that correction: the " +
     "graph splits just before the layer, grades one branch and merges the " +
     "two back through the matte. Primaries, curves, the look LUT, every FX " +
     "and grain still cover the whole frame, and no mask anywhere can gate " +
     "them. What did change is what sits inside the mask. It used to be the " +
     "colour qualifier alone, so a window could only ever retint a keyed " +
     "colour, and this entry used to end by saying a window could not be " +
     "used to darken one corner or soften one face. A layer carries " +
     "exposure, contrast, temperature, tint, hue shift, saturation, " +
     "luminance, an offset and a blur, so both of those are now ordinary " +
     "uses of a window. With a layer switched off it drops out of the graph " +
     "entirely (no cube, no matte, no extra ffmpeg input) rather than " +
     "sitting there doing nothing visible, and a stack with no enabled " +
     "layers in it renders byte identical to no stack at all, which the " +
     "engine suite asserts on a real frame."],
    ["One shape per layer, and it does not move.",
     "works, within those limits",
     "Two shapes cannot be combined or subtracted INSIDE one layer: a layer " +
     "has one window, one colour key, and multiplies them. Two shapes with " +
     "two different corrections is what the stack is for, and two shapes " +
     "sharing one correction can be had by giving two layers the same " +
     "settings, but there is no union or difference operator, so a shape " +
     "with a bite out of it can only be approximated by stacking an inverted " +
     "layer on top. Every window is also fixed for the length of the shot: " +
     "nothing tracks a subject and nothing keyframes the shape, for the same " +
     "reason the tracking entry above gives, which is that every frame is an " +
     "independent graph on either render engine, with no state carried " +
     "between frames. On a moving subject the window has to " +
     "be drawn wide enough to hold the subject for the whole range."],
    ["Layers are serial, not parallel.",
     "works, in one order only",
     "The stack is a chain, not a mix. Each layer takes the previous layer's " +
     "output as its input and hands its own output on, so two layers over " +
     "the same pixels compound rather than blend. There is no opacity " +
     "mixing between sibling layers, no blend mode, and no way to average " +
     "two corrections: a layer's strength is the merge between its own " +
     "corrected branch and its own input, not a weight against its " +
     "neighbours. Order therefore matters, and that is asserted rather than " +
     "assumed (the engine test layers.two_layers_apply_in_order renders the " +
     "same two layers in both orders and requires the pictures to differ). " +
     "The two placement points, before and after the look LUT, are the only " +
     "structural choice available; within each one the array order is the " +
     "render order, so re-ordering the list is the only way to re-order the " +
     "maths."],
    ["Each active layer costs about the same again.",
     "works, and the cost is per layer",
     "Measured by studio/tools/layer_cost_bench.py through the ffmpeg " +
     "engine: 48 frames of A001_09011832_C003.MOV rendered to 1920x3414 " +
     "(1920 wide from a vertical 4K source, so about 3.2 times the pixels " +
     "of a 1920x1080 frame), libx264 crf 18 preset medium, each case " +
     "rendered twice with only the second run timed so this is render time " +
     "and not bake time, every layer carrying a power window and a blur. " +
     "Two runs of the same bench, the second while two other agents were " +
     "using the machine. No layers: 6.07s and 6.07s. One layer: 8.61s and " +
     "9.30s. Two: 11.61s and 11.87s. Four: 17.69s and 22.45s. So the cost " +
     "is 2.9 to 4.1 seconds per layer, or roughly 60 to 85ms per layer per " +
     "frame at that size, and the spread between the two runs is machine " +
     "load rather than anything in the grade. What does not vary is the " +
     "structure the timing follows: the graph goes from 2 lut3d nodes, 0 " +
     "gblur and 0 maskedmerge at no layers to 6, 4 and 4 at four layers, " +
     "exactly one of each per layer, and nothing amortises across layers. " +
     "Four masked layers is therefore roughly a three times longer render " +
     "than none, and nothing in the code caps the count: forty layers will " +
     "be accepted and will cost forty layers."],
    ["A layer's exposure, contrast, temperature and tint are display side.",
     "works, different domain to primaries",
     "The stack runs after CST OUT, so a layer always sees display referred " +
     "Rec.709 code and never the log or working signal that primaries sees. " +
     "That changes two formulas rather than two controls. A stop is a code " +
     "multiply of 2 to the power (stops / 2.4) instead of an offset on the " +
     "log curve (an offset in log IS a linear gain, and after CST OUT there " +
     "is no log curve left to offset); this is exactly the branch the engine " +
     "already takes for convert.exposure when the source is Rec.709, and the " +
     "layer reads the same constant rather than restating it, measured " +
     "within 4.9e-7 across the whole 33 cube and at a median 1.15541 against " +
     "a predicted 1.155353 on a real frame. Contrast pivots on 0.4587, the " +
     "Rec.709 mid grey, where primaries pivots on the mid grey of whatever " +
     "working space it is in, which is 0.3360 on the DaVinci Wide Gamut " +
     "path. The same number typed into the two nodes is therefore the same " +
     "intent and not the same arithmetic, and a layer set to match a " +
     "primaries move has to be dialled by eye."],
    ["A layer's blur is a sigma quoted at 1920 wide.",
     "works, and it scales with the frame",
     "The blur field is a gaussian sigma in pixels AT 1920 wide, scaled by " +
     "the actual frame width on both engines, which is the same convention " +
     "the radial blur sigma already uses. Without it a grade dialled on the " +
     "640 preview would render three times sharper at delivery size. It is " +
     "resolved once from the probed width, in layer_blur_sigma, rather than " +
     "in the preview scaler, so the preview and the render read the same " +
     "field and there is no second place to keep in step. The blur runs on " +
     "the corrected branch only, before the matte merge, so it stays inside " +
     "the mask: a blur under a window softens inside the shape and leaves " +
     "every pixel outside it byte identical, which the engine suite asserts " +
     "(layers.blur_under_a_window_stays_inside_it)."],
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
     "layer stack, grain, detail, fx, or which look is loaded, " +
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
     "masked correction (one secondary, which is the shape the config had " +
     "when this was measured; it is one layer now): max 21.4, mean 1.49 to " +
     "1.60, 49.6 to 54.0% over 1, 7.96 to " +
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
    ["Hue curves and Color Slice are a 33 point cube, not a per pixel operation.",
     "known approximation, quantisation measured",
     "Both sections are pure functions of one pixel's RGB, so the whole of Hue " +
     "vs Hue, Hue vs Sat, Hue vs Lum, Lum vs Sat, Sat vs Sat, the seven hue " +
     "vectors and Tetra collapse into a single 33 point .cube that ffmpeg " +
     "reads with lut3d=interp=tetrahedral and the GPU reads with the same " +
     "tetrahedral maths. That is what makes the two engines agree and what " +
     "keeps the cost flat no matter how many of the controls are on, and the " +
     "price is that a colour between two lattice points is interpolated " +
     "rather than computed. Measured on the hardest case for a hue tool, a " +
     "smooth 360 step sweep right around the wheel, comparing the cube " +
     "through real ffmpeg against the exact numpy maths, in 8 bit code " +
     "values: a 60 degree rotation of the red vector, max 1.627 and mean " +
     "0.012 at S=0.8 V=0.8 and max 1.302 mean 0.017 at S=0.45 V=0.6; a 2x " +
     "saturation push on cyan, max 1.868 and max 0.251; a two point Hue vs " +
     "Hue curve, max 1.529 and max 2.216; a two point Hue vs Sat curve, max " +
     "0.798 and max 0.144; all five curves plus two vectors plus global " +
     "density plus Tetra at once, max 1.152 mean 0.154 and max 1.679 mean " +
     "0.067. So the worst single pixel anywhere in that set is 2.216 of 255 " +
     "and the typical pixel is under a tenth of a code. It really is the " +
     "cube size rather than anything else: the same worst case sweep baked " +
     "at three sizes gives max 4.222 mean 0.232 at 17 points, max 2.216 " +
     "mean 0.057 at 33, and max 0.689 mean 0.015 at 65. 33 is the size the " +
     "rest of the engine already uses. If a banded hue ramp ever shows up, " +
     "the fix is one number in grade/slice.py, at 8x the bake cost."],
    ["This is not Resolve's Color Slice, and Tetra here is not the Tetra DCTL.",
     "different tool, never compared",
     "The names are borrowed because they say what the controls are for, and " +
     "that is the whole of the relationship. No comparison against DaVinci " +
     "Resolve or against any Tetra DCTL was run, because neither is " +
     "installed on this machine, so any claim that a setting here matches a " +
     "setting there would be invented. What is known and deliberate: this " +
     "version runs in display referred Rec.709 after the curves stage, not " +
     "in a wide working space, so a vector's reach is defined on the " +
     "displayed hue rather than on scene linear chroma. A pixel's weight for " +
     "a vector is a raised cosine of its hue distance from that vector's " +
     "centre, zero at 60 degrees and scaled by its own saturation; the six " +
     "chromatic weights sum to exactly 1 at every hue, which is a property " +
     "worth having and not one anybody promised about the original. Density " +
     "is L' = L * (1 - density * S * weight) on HSV V. Tetra moves the six " +
     "cube corners with black and white pinned and interpolates the inside " +
     "tetrahedrally, and it has no black or white corner control at all. " +
     "Treat the two as tools that do a similar job, not as a port."],
    ["Per vector density and the global density are not the same strength.",
     "known asymmetry, measured",
     "Both follow the contract's formula, and the contract makes them " +
     "different: the global control is L * (1 - density * S), while a " +
     "vector's is L * (1 - density * S * weight) and that weight already has " +
     "the pixel's saturation in it, so a vector carries saturation twice and " +
     "the global carries it once. The consequence is real and was measured " +
     "rather than reasoned about. On clip A at 640 wide, mean luma 0.37790 " +
     "at defaults: setting all six vector densities to 1, which covers the " +
     "whole hue wheel, gives 0.36421, a drop of 0.01369, while setting the " +
     "global density to 1 gives 0.31376, a drop of 0.06414. The global " +
     "control is therefore 4.68x stronger at the same number even though the " +
     "two cover the same pixels. One vector on its own, red at 1, moves it " +
     "by 0.00548. This was left as the contract wrote it rather than " +
     "quietly normalised, because changing it would move every grade saved " +
     "against the shipped behaviour; the sliders say so in their tooltips."],
    ["The skin vector is aimed at one measured colour, not at your subject.",
     "known limit, centre measured",
     "Its centre is 20.8696 degrees, which is not a taste call: it is the hue " +
     "of the engine's own skin swatch, grade/tools/match_ref.py " +
     "PROBES['skin'] = (0.55, 0.40, 0.32), the colour match_ref already " +
     "refuses to let a match break. Checked against real footage rather than " +
     "trusted on its own. Grading both test clips at defaults at 640 wide " +
     "and taking every pixel whose chroma sits within 0.06 of that probe " +
     "with brightness normalised out: 31780 such pixels on clip A with a " +
     "circular mean hue of 16.159 degrees, and 10006 on clip B at 12.894 " +
     "degrees, with 100.0 percent of both inside the vector's 60 degree " +
     "support. What that does not establish is skin in general. Two clips " +
     "of the same production are not a survey of skin tones, and a subject " +
     "far from this probe, or under a strong colour cast, will sit off " +
     "centre in the vector and get less of the move than the slider implies. " +
     "The skin vector also overlaps red and yellow on purpose, so it stacks " +
     "with them rather than replacing them."],
    ["Look now has two slots, but they blend in parallel, not a stack.",
     "works, not what \"several look slots\" above promised",
     "The POSSIBLE entry above predicted a second look slot would be " +
     "\"a second node\", lut1 feeding into lut2 in series. What shipped " +
     "instead is A = lerp(base, lut1(base), mix), B = lerp(base, " +
     "lut2(base), mix2), out = lerp(A, B, balance): both slots read the " +
     "same pre-look signal and never see each other's output, so this is a " +
     "blend of two looks, not a stack of two. The compatibility gate is " +
     "exact rather than approximate: with lut2 unset, 0 of 255 max " +
     "difference against today's single-slot output on both test clips, " +
     "even with balance and mix2 pushed away from their defaults " +
     "(grade/tests/cases_look.py " +
     "test_lut2_null_is_byte_identical_to_today), and balance=1.0 with " +
     "mix2=1.0 is 0 of 255 max difference against lut2 applied alone " +
     "(test_balance_one_is_lut2_alone). At balance=0.5 the output matches " +
     "the pixel average of the two branches to within 0.50 of 65535 on " +
     "both clips, read back at 16 bit rather than the usual 8 " +
     "(test_balance_half_is_the_average_of_the_two_branches). The GPU " +
     "shader port renders the same parallel blend: the studio's own " +
     "GPU-vs-ffmpeg parity harness reports both new rows " +
     "(look_two_slots_balance_half, look_two_slots_balance_full_mix2) as " +
     "EXACT at both preview widths it checks, 640 and 1280."],
    ["The zoom percentage counts preview pixels, not camera pixels.",
     "works, but not what 100% usually means",
     "The viewer's picture is a preview render, so \"100%\" is one preview " +
     "pixel per screen pixel and not one sensor pixel per screen pixel. " +
     "Measured on the test clip in a 1440x900 window: the preview is 960 " +
     "wide, Fit shows it 185 px wide and the label reads 19%, and 200% is " +
     "1920 px wide on screen from a source that is 2160 px wide after " +
     "rotation. So zooming past about 225% here is enlarging preview pixels, " +
     "not revealing detail. Raise the preview width if you need to judge " +
     "fine detail at a high zoom."],
    ["The power window and the pick rectangle are not drawn on 2 or 4 frames.",
     "known limit, on purpose",
     "The overlay lives inside #stage, and the frames grid replaces #stage " +
     "entirely (#viewport.frames #stage is display none in style.css), so " +
     "with Frames on 2 or 4 there is no window overlay and no pick " +
     "rectangle on any slot. Switch back to Frames 1 to drag a window or " +
     "pick a rectangle; the grade being shown in the slots is the same one " +
     "either way. Drawing four overlays would need four independent " +
     "geometries, which is a bigger change than this contract."],
    ["The contact sheet's own cells cannot be dragged onto a frame slot.",
     "known limit, measured",
     "The contact sheet takes over the whole viewer, so while it is open " +
     "there are no slots on screen to drop onto. The two drag sources that " +
     "do work are the filmstrip thumbnails and the mark chips on the " +
     "timeline, and the mark chips ARE the contact sheet's marks, so every " +
     "marked time is still reachable by drag, just from the timeline rather " +
     "than from the sheet."],
    ["A region render costs a whole frame render at the zoom width.",
     "works, more expensive than it looks",
     "Every spatial stage (power windows, the radial ramp, the vignette, " +
     "the grain plate) is sized from the whole frame, so a region has to be " +
     "cropped AFTER the grade or those stages would re-centre on the patch. " +
     "Both the CLI and POST /api/frame therefore render the full frame at " +
     "the zoomed width and then crop: a 480 wide request for the middle " +
     "half at zoom 4 renders the whole frame at 1920 and hands back 960 px " +
     "of it. The one thing that caps the cost is the source: no region " +
     "renders wider than its own pixels (measured on the test clip, 2160 " +
     "wide after rotation, a middle half at zoom 999 comes back 1080 px " +
     "wide, not wider)."],
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
     "of 255 could make one visible, and it would be one code. The single " +
     "secondary and single window this entry describes have since become the " +
     "layer stack, and the same merge now runs once per layer. The old " +
     "configs were not thrown away to get there: they are migrated on read " +
     "into one layer, and the seven window and secondary cases quoted above " +
     "are still run in their pre-layers spelling, so they now measure the " +
     "migration as well as the shader. All seven are still EXACT at both " +
     "widths, worst channel 1 code of 255, and eight new layer cases (window " +
     "only, key only, both, two stacked, after the look, a blur under a " +
     "window, an inverted mask, and a global correction with no mask) are " +
     "EXACT at both widths too."],
    ["A power window could only be positioned by typing numbers.",
     "was seven sliders, now it is dragged on the picture",
     "The window had centre, extent, rotation, softness and invert as " +
     "sliders in a layer's Window group and nothing on the picture, so placing a " +
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
    ["The selected layer marker can end up on the wrong layer after an undo.",
     "tracked by identity now, not by array position",
     "Was one plain integer, the index into config.layers, kept outside the " +
     "config on purpose (the engine has no idea which layer is selected, and " +
     "undo/redo snapshot only the config). Move up and Move down used to " +
     "update that index by hand so the marker followed the LAYER across a " +
     "reorder, but undo restores the config by JSON.parsing a whole new set " +
     "of objects (app.js's restore()), so the index was left naming whatever " +
     "landed on that row instead of the layer the user actually meant. Fixed " +
     "by giving every layer a stable client-side id (a WeakMap keyed on the " +
     "live layer object, re-associated after undo by matching a layer's own " +
     "serialised content, name included, against what the previous render " +
     "saw, since the object itself is a new one) and tracking the SELECTED " +
     "id rather than an index; moveLayer needs no index bookkeeping at all " +
     "any more, since the id stays with the object wherever it moves. " +
     "Measured directly in studio/tests/specs/16-layers.mjs: select a layer " +
     "named Sky Fix at index 1, Move up puts it at index 0 and the marker " +
     "correctly follows it there, then one #undoBtn press restores the two " +
     "layer order (Sky Fix back at index 1) and the marker now follows it " +
     "there too, reading Sky Fix rather than the other layer."],
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
     "done: grain is ported to the interactive preview and to this parity " +
     "harness (live.js, parity.js), but not to the full multi frame GPU " +
     "render, render_gpu.py, which still routes it to ffmpeg on purpose " +
     "(see the possible entry on grain, detail and letterbox on the GPU " +
     "path). This entry used to say grain can never match because ffmpeg's " +
     "noise filter seeds itself from the clock and does not reproduce " +
     "between runs. That was a prediction, not a measurement, and it was " +
     "wrong on this machine: rendering the same single frame twice through " +
     "cinegrade.py with grain on (cinematic preset, -frames:v 1, " +
     "A001_09011336_C002.MOV at 2.0s) gave byte identical output both " +
     "times (max absolute difference 0), and the bare ffmpeg noise filter " +
     "on its own gives byte identical output across two separate process " +
     "runs a few seconds apart too, while still varying frame to frame " +
     "inside one run, which is real temporal noise, not a frozen plate. " +
     "ffmpeg's default seed of -1 (\"unset\") is not actually drawn from " +
     "the wall clock on this build: it is reproducible, and a GPU grain " +
     "stage has since been written on the strength of that measurement. " +
     "gpu.js overlay-blends against a plate the server renders " +
     "(/api/grain/plate, a slice of the same build_graph text ffmpeg's own " +
     "grain block runs, so the two cannot drift apart) and applies the " +
     "same response=film weight when that field is set. parity.js's four " +
     "grain rows (defaults, the 35mm stock, response=film, color=1) " +
     "measured exact against ffmpeg at both 640 and 1280 wide: mean " +
     "channel difference 0.006 of 255, max 1, 0 percent of channels over " +
     "1. That is for a rendered STILL only: the plate is one frame's " +
     "worth (ffmpeg's noise generator's own frame 0 for that config and " +
     "size, cached), not one plate per output frame, so a playing or " +
     "looping preview reuses that one plate on every frame while a real " +
     "export's grain keeps changing, and #rendererBadge still says so " +
     "whenever grain is on outside a still render. The 8 bit yuv lane is " +
     "CLOSE rather than EXACT for an unrelated reason, swscale dithering: " +
     "worst case 2 of 255 on 0.0009 percent of channels. It used to run only " +
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
    ["mid_detail against ffmpeg", "was 255 codes out on rare pixels, now exact",
     "The engine and this GPU port used to disagree by the full 255 on 0.06 " +
     "percent of channel samples at mid_detail 0.5 and 1.10 percent at 1.0, " +
     "and the first explanation written here (floating point rounding landing " +
     "on opposite sides of a clip boundary) was wrong. ffmpeg's blend filter " +
     "does not clip an out of range all_expr result at all: it casts the " +
     "float to the plane's integer type, so the value wraps modulo 65536. " +
     "Measured directly on this build (ffmpeg 8.1.1) with a 16 bit ramp and " +
     "all_expr 'A*3-B*2': where the arithmetic says -44 ffmpeg returns 65492, " +
     "where it says 130269 ffmpeg returns 64733. On the parity clip at 640 " +
     "wide with mid_detail 1.0, 24140 of 2184960 channel samples (1.10 " +
     "percent, exactly the share the parity row reported as differing) leave " +
     "the range: 8517 above 65535 and 15623 below 0, every one of them " +
     "wrapping to the opposite end. Pixel (70,216) green computes -242, came " +
     "back from ffmpeg as 65294 (255 of 255 after the 8 bit hop) and from the " +
     "GPU as 0. The earlier note read the wrap as a clip because doubling " +
     "white, 65535 * 2 = 131070, wraps to 65534, which is indistinguishable " +
     "from a clip to white. The fix is one clip(...,0,65535) around the same " +
     "blend expression in f_mid_detail_segment, so both engines clamp at the " +
     "same point: after the whole expression, in the working range. All six " +
     "mid_detail parity rows (0.5, 1.0 and -0.5, at 640 and 1280 wide) now " +
     "measure EXACT: max 1 code value, 0 percent of channel samples off by " +
     "more than 1, mean 0.0056 to 0.0060 of 255. Negative mid_detail measured " +
     "exact before and after, which is what an overshoot only wrap predicts."],
    ["Loading a preset, then Saving As or Overwriting a different one",
     "leaked the wrong comment",
     "The founder saw it directly: a preset saved as Manav_1_2 with no " +
     "typed comment still carried the comment of nature_cinema, a preset " +
     "loaded earlier, so the file described a different grade. Two bugs, " +
     "both in server.py. read_preset() ran the file through full_config() " +
     "to fill in every field the file omits, which correctly reached inside " +
     "_comment too (deep_merge keeps any key not in DEFAULTS as is), so a " +
     "loaded preset's comment rode along into the live config that becomes " +
     "the starting point for every future save. write_preset(), asked to " +
     "keep an existing comment when Save As or Overwrite is given a blank " +
     "one, read that 'existing' comment off the live config's leftover " +
     "_comment instead of off the actual target file on disk, so whichever " +
     "preset was loaded most recently kept describing itself no matter what " +
     "was actually being saved over. read_preset() now strips _comment " +
     "before the config reaches the page. write_preset() now reads the " +
     "kept-if-blank comment from the target file itself, keyed by the name " +
     "being written, never from the live config. Measured with a real " +
     "browser driving the actual UI (studio/tests/specs/20-presets.mjs): " +
     "loading cinematic (its own comment: 'Loud teal/orange...') then Save " +
     "As with an empty comment field now writes a file with no _comment key " +
     "at all, and Overwrite on a library preset with a blank comment now " +
     "keeps only that preset's own prior comment, never a loaded one's."],
    ["Load only replaced the fields a preset happened to set",
     "now fully replaces the config",
     "Not a defect found in the Load code path itself (loadPreset() already " +
     "did S.slots[S.active] = j.config, a full replace, and server.py's " +
     "read_preset() already ran the file through full_config() before this " +
     "fix, so it always returned every field, layers and the second look " +
     "slot included). This entry records what the audit actually verified " +
     "with a real browser round trip end to end, because the founder's own " +
     "words ('doesnt overwrite or LOAD the full settings') named Load as a " +
     "suspect alongside the comment bug above. Measured: loading a preset " +
     "with layers and a second look LUT, saving that as p5_full, loading a " +
     "plain preset (layers back to [], look.lut2 back to null), then " +
     "loading p5_full again restores the layer array and look.lut2 exactly, " +
     "matching full_config(p5_full) field for field. The one real gap was " +
     "in the test harness, not the product: a reload right after Load could " +
     "read the config before boot()'s own per clip grade restore had " +
     "settled, making a correct Load look like a no op. Fixed in the spec " +
     "with the same StudioGrades.isPending()/#gradeSaveState poll specs 12 " +
     "and 16 already use instead of a fixed sleep."],
    ["Loading a preset left no record of what happened",
     "now commits \"loaded preset NAME\"",
     "loadPreset()'s call to StudioSession.publish() carried no message, so " +
     "the project history recorded a generic 'N changes: ...' line " +
     "indistinguishable from an ordinary edit, with no way to tell from the " +
     "log alone that a preset load, not a hand grade, put the picture where " +
     "it is. session.js's publish() now takes an optional fourth message " +
     "argument, sent only when the caller has one, and loadPreset() passes " +
     "'loaded preset ' + name. Server support for this already existed " +
     "(live_set() in server.py already read payload.message and threaded it " +
     "into PROJECTS.commit(), and projects.py's own commit() docstring " +
     "already named 'loaded preset nature_cinema' as the intended example), " +
     "so this was a one line client gap, not a server change. Measured with " +
     "a real browser: loading flat now posts {message: \"loaded preset " +
     "flat\"} to POST /api/session, and GET /api/project/log grows by " +
     "exactly one commit whose message is \"loaded preset flat\"."],
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
