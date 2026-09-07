/* StudioLive: wires gpu.js into the main viewer.
 *
 * Three independent things live here, all built on the same StudioGPU
 * instance and the same canvas:
 *
 *   Mode 1, still frames: renderStill() fetches the normalised source pixels
 *   for one (clip, time, width) once and grades them again on every call, so
 *   a knob turn never touches the network, only the GPU.
 *
 *   Mode 2, the live loop: prepareLoop() decodes a whole time range in one
 *   request, startLoop() plays those frames back on requestAnimationFrame at
 *   the clip's real frame rate, re-grading every one through gpu.js as it
 *   goes so a knob dragged mid-loop shows up without a stall or a restart.
 *
 *   Mode 3, proxy playback: prepareProxy() asks the server for one small
 *   H.264 proxy of the whole clip, attachProxy() points a hidden <video> at
 *   it, and playProxy() grades every frame the browser presents. Unlike mode
 *   2 it holds no frames, so there is no memory cap and a whole clip plays
 *   and scrubs; unlike mode 1 the pixels are 8-bit 4:2:0, which is the one
 *   thing it gives up (measured, see limits.js).
 *
 * This file knows nothing about app.js's state object or its DOM ids: every
 * function takes plain parameters and a canvas, on purpose, so the app.js
 * wiring stays a thin adapter and this module stays testable (and reusable
 * from gpupreview.html-style pages) on its own.
 *
 * Depends on gpu.js being loaded first (StudioGPU global).
 */

(function (global) {
  "use strict";

  function now() {
    return (typeof performance !== "undefined" && performance.now)
      ? performance.now() : Date.now();
  }

  var inst = null;          // the StudioGPU instance, once init() succeeds
  var initReason = null;    // StudioGPU.lastError at init time, kept for reason()

  // What is currently sitting in the GPU's source texture, so both render
  // paths below can skip a re-upload (a real GPU cost, not just a network
  // one) when the pixels on the card already match what this call needs.
  // Tagged, not boolean, because the still path and the loop path share one
  // instance and one texture: switching between them has to invalidate
  // whichever path did NOT just upload, or it would keep rendering the
  // other mode's stale frame under a config that looks right but is not.
  var uploadedTag = null;

  function init(canvas, opts) {
    inst = StudioGPU.create(canvas, opts || {});
    initReason = StudioGPU.lastError;
    uploadedTag = null;
    return { ok: !!inst, reason: initReason };
  }

  function available() { return !!inst; }
  function reason() { return initReason; }
  function setDefaults(d) { StudioGPU.setDefaults(d); }

  /* Which stages this config would actually run on the GPU right now, split
   * into "fine" and "the caller should fall back to the server for this".
   *
   * Grain used to be a permanent, documented exception here: gpu.js had no
   * grain shader at all, and this comment used to justify that on ffmpeg's
   * noise filter supposedly seeding from the wall clock, which was already
   * measured false before that shader was written (see STAGE_NOTES.grain).
   * gpu.js now has a real grain pass and stageReport reports it exact, so
   * grain no longer needs (or gets) a special case in `blocking` below: it
   * simply never appears in rep.unsupported any more.
   *
   * grainOnly still comes back from here, but it now means something
   * narrower than "grain is on": the plate this preview fetches is one
   * frame's worth (ffmpeg's noise generator's own frame 0 for this exact
   * config and size, cached), not one plate per output frame. A single
   * still uses exactly one frame either way, which is why renderStill
   * below does not treat grainOnly as a caveat any more. renderProxyFrame
   * (the play loop, seeking, frame stepping) renders many frames from one
   * config and would show that same one plate on every one of them while a
   * real export's grain keeps changing, so it still surfaces grainOnly as
   * a caveat for the caller to warn about.
   */
  function checkStages(cfg) {
    var rep = StudioGPU.stageReport(cfg);
    var blocking = rep.unsupported;
    // The power window used to be forced into `blocking` here, because it was
    // in the ffmpeg engine only and gpu.js had no window stage to report as
    // unsupported: it would have graded the whole frame and shown a picture
    // that quietly ignored the shape the user just drew. The shader port
    // landed, and the stage report is the only authority again. Since the
    // single secondary became the layer stack the rows are one per layer
    // ("layer0", "layer1", ...), each carrying its own window, and the parity
    // harness measures the layer configs at both widths.
    return { report: rep, blocking: blocking, grainOnly: !!(cfg.grain && cfg.grain.enabled) };
  }

  function fetchJSON(url, opts) {
    return fetch(url, opts).then(function (r) {
      if (r.ok) return r;
      return r.json().catch(function () { return {}; }).then(function (j) {
        throw new Error(j.error || (url + ": " + r.status));
      });
    });
  }

  /* Rotation (contract C2) as a string: auto, 0, 90, 180 or 270.
   *
   * Every request this file makes carries it, and every cache key in this
   * file is built on it, because a rotation change makes the decoded source
   * a different picture at a different size: an entry keyed for one turn
   * must never be served to another. It used to be the boolean `autorotate`,
   * which could only say "honour the tag" or "ignore it".
   *
   * A caller that says nothing means `auto`, the one that reproduces the
   * old default. A caller that still passes the old boolean gets what it
   * used to mean, so an outside page (the parity harness) does not have to
   * move in the same commit. */
  function rotationOf(p) {
    if (p && p.rotation !== undefined && p.rotation !== null && p.rotation !== "") {
      return String(p.rotation);
    }
    if (p && p.autorotate === false) return "0";
    return "auto";
  }

  // ------------------------------------------------------------------
  // Mode 1: still frames
  // ------------------------------------------------------------------

  var stillCache = null; // { key, data, w, h }

  function stillKey(p) {
    return p.clip + "|" + (+p.time).toFixed(4) + "|" + p.width + "|" + rotationOf(p);
  }

  // The expensive half of a preview (a real ffmpeg decode) cached by
  // exactly the inputs that change it. A knob turn changes none of them, so
  // this is what makes "turning knobs must not refetch the source" true.
  function fetchStillSource(p) {
    var key = stillKey(p);
    if (stillCache && stillCache.key === key) return Promise.resolve(stillCache);
    var base = p.apiBase || "";
    return fetchJSON(base + "/api/source", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        clip: p.clip, time: p.time, width: p.width,
        rotation: rotationOf(p)
      })
    }).then(function (r) {
      var size = (r.headers.get("X-Frame-Size") || "0x0").split("x").map(Number);
      return r.arrayBuffer().then(function (buf) {
        stillCache = { key: key, data: new Uint16Array(buf), w: size[0], h: size[1] };
        return stillCache;
      });
    });
  }

  /* convert.input "auto" resolved to what the SERVER says this clip is.
   *
   * gpu.js cannot resolve "auto" on its own: that needs the file's transfer
   * and primaries tags, which live in the probe and never reach the render
   * loop. So the answer is carried in instead. GET /api/state already
   * publishes it per clip as clips[].source.resolved_input, computed by the
   * same cinegrade.resolve_input the render uses, so this is not a second
   * opinion about the file, it is the same one.
   *
   * Without this an HLG or PQ clip left on "auto" previewed through the Apple
   * Log cube while the server rendered it through the right one, which is the
   * gap contract G1 left open. With it the GPU asks for the cube the server
   * would, and a camera log clip (which can only ever be set by hand) behaves
   * the same way an explicitly set one does.
   *
   * Nothing changes for an Apple Log clip: its resolved_input IS "apple_log",
   * which names the same cubes "auto" already named and leaves the log stage
   * reported exact, so the human's GPU preview is the picture it was.
   *
   * The config is copied rather than edited: it belongs to the caller, who is
   * usually holding the live UI state object.
   */
  function withResolvedInput(config, resolved) {
    if (!resolved || !config || !config.convert) return config;
    if (config.convert.input && config.convert.input !== "auto") return config;
    var out = {}, k;
    for (k in config) {
      if (Object.prototype.hasOwnProperty.call(config, k)) out[k] = config[k];
    }
    out.convert = {};
    for (k in config.convert) {
      if (Object.prototype.hasOwnProperty.call(config.convert, k)) {
        out.convert[k] = config.convert[k];
      }
    }
    out.convert.input = resolved;
    return out;
  }

  /* Grade one still frame on the GPU and present it to the canvas passed to
   * init(). Rejects (never throws synchronously) when the GPU cannot be
   * trusted for this config, so a caller can always `.catch` into the
   * server path without a try/catch of its own.
   *
   * p: { clip, time, width, rotation, config, sourceWidth, apiBase,
   *      resolvedInput }
   * sourceWidth is the clip's own upright width (pre-downscale), needed to
   * compute the same pixelScale the server's scale_for_preview would use;
   * without it a reduced-size preview overstates every pixel-denominated FX.
   * resolvedInput is clips[].source.resolved_input for this clip; leaving it
   * out keeps the old behaviour, where "auto" means the Apple Log cubes.
   */
  function renderStill(p) {
    if (!inst) {
      return Promise.reject(new Error(
        "no GPU renderer available (" + (initReason || "not initialised") + ")"));
    }
    var cfg = withResolvedInput(p.config, p.resolvedInput);
    var check = checkStages(cfg);
    if (check.blocking.length) {
      var e = new Error("GPU preview cannot render this config: " + check.report.summary);
      e.stageReport = check.report;
      return Promise.reject(e);
    }
    return fetchStillSource(p).then(function (src) {
      var sourceWidth = p.sourceWidth || src.w;
      var factor = src.w / sourceWidth;
      var tag = "still:" + src.key;
      if (uploadedTag !== tag) {
        inst.setSource(src.data, src.w, src.h);
        uploadedTag = tag;
      }
      return inst.ready(cfg, { pixelScale: factor }).then(function () {
        var r = inst.render(cfg, { pixelScale: factor });
        return {
          ms: r.ms, passes: r.passes, width: r.width, height: r.height,
          // Not check.grainOnly: a still renders exactly one frame, which is
          // exactly what the grain plate this preview fetched covers, so
          // there is no caveat left to report here (see checkStages above).
          grain: false, report: check.report
        };
      });
    });
  }

  // ------------------------------------------------------------------
  // budget: what a loop range would cost, before asking for it
  // ------------------------------------------------------------------

  // Mirrors server.py's RANGE_MAX_BYTES so a client that wants to render its
  // own "how much can I have" message before ever asking the server can
  // (the number itself still comes from budget() below, which is the
  // authority; this constant exists only for callers that want to reason
  // about the cap without a round trip, e.g. disabling an input).
  var RANGE_MAX_BYTES = 700000000;

  function budget(p) {
    var base = p.apiBase || "";
    var qs = "clip=" + encodeURIComponent(p.clip)
      + "&width=" + encodeURIComponent(p.width)
      + "&rotation=" + encodeURIComponent(rotationOf(p));
    return fetchJSON(base + "/api/range/limit?" + qs).then(function (r) { return r.json(); });
  }

  // ------------------------------------------------------------------
  // Mode 2: the live loop
  // ------------------------------------------------------------------

  var loopBuf = null;    // { data, w, h, stride, count, fps, start, duration }
  var loopGen = 0;       // bumped on every prepareLoop, tags the upload cache
  var running = false;
  var busy = false;
  var raf = null;
  var lastIdx = -1;
  var fpsWindow = [];
  var loopT0 = 0;

  /* Decode a time range once. Checks the budget first (a cheap GET, no
   * decode) so an oversized request never starts a slow, doomed-to-fail
   * ffmpeg call: the plain-English refusal comes back before any real work
   * happens, not after minutes of decoding or a filled-up response buffer.
   *
   * p: { clip, time (range start), duration, width, rotation, apiBase }
   * Resolves { width, height, frames, fps, start, duration, loadMs }.
   */
  function prepareLoop(p) {
    var base = p.apiBase || "";
    var clip = p.clip, rotation = rotationOf(p);
    var width = p.width, start = p.time || 0, duration = p.duration;
    if (!(duration > 0)) return Promise.reject(new Error("loop range must be longer than 0s"));

    return budget({ clip: clip, width: width, rotation: rotation, apiBase: base })
      .then(function (b) {
        if (duration > b.max_seconds + 1e-6) {
          var msg = "That loop range is " + duration.toFixed(1) + "s at " + b.width
            + "px wide, over the " + Math.round(b.cap_bytes / 1e6) + " MB loop budget. "
            + "At " + b.width + "px wide you can loop up to " + b.max_seconds.toFixed(1)
            + "s at a time; drop the preview width to fit more time in the same budget.";
          var err = new Error(msg);
          err.budget = b;
          throw err;
        }
        var t0 = now();
        return fetchJSON(base + "/api/range", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            clip: clip, time: start, duration: duration, width: width, rotation: rotation
          })
        }).then(function (r) {
          var size = (r.headers.get("X-Frame-Size") || "0x0").split("x").map(Number);
          var count = parseInt(r.headers.get("X-Frame-Count") || "0", 10);
          var fps = parseFloat(r.headers.get("X-Fps") || "24");
          var rStart = parseFloat(r.headers.get("X-Range-Start") || String(start));
          var rDuration = parseFloat(r.headers.get("X-Range-Duration") || String(duration));
          return r.arrayBuffer().then(function (buf) {
            loopGen++;
            loopBuf = {
              data: new Uint16Array(buf), w: size[0], h: size[1], count: count,
              fps: fps, start: rStart, duration: rDuration, stride: size[0] * size[1] * 3
            };
            return {
              width: size[0], height: size[1], frames: count, fps: fps,
              start: rStart, duration: rDuration, loadMs: now() - t0
            };
          });
        });
      });
  }

  /* Start playing the last prepared range. getCfg is called fresh on every
   * rendered frame (not cached at start time), which is the entire
   * mechanism behind "a knob change applies on the very next frame": there
   * is no separate "apply" step to wire up, the loop is always grading
   * whatever getCfg() returns right now.
   *
   * opts: { sourceWidth, resolvedInput, onFrame(info), onError(err) }
   * Renders every animation frame the display asks for, not only when the
   * clip's own frame index advances, so a knob dragged between two source
   * frames still updates at display rate instead of waiting on the next
   * one; the source texture itself is only re-uploaded when the index
   * actually changes, which is the one part of this that is not free.
   *
   * resolvedInput is clips[].source.resolved_input for the clip in the
   * buffer, the same field renderStill takes: without it a clip left on
   * "auto" would loop through the Apple Log cubes while the server renders
   * it as whatever its tags say. Leaving it out keeps the old behaviour.
   */
  function startLoop(getCfg, opts) {
    if (!loopBuf) throw new Error("no loop range prepared");
    if (!inst) throw new Error("no GPU renderer available");
    opts = opts || {};
    var sourceWidth = opts.sourceWidth || loopBuf.w;
    var resolvedInput = opts.resolvedInput;
    var factor = loopBuf.w / sourceWidth;
    var gen = loopGen;
    running = true; busy = false; lastIdx = -1; fpsWindow.length = 0;
    loopT0 = now();

    function tick(nowMs) {
      if (!running) return;
      raf = requestAnimationFrame(tick);
      // A render already in flight (waiting on ready()'s LUT promise, which
      // is normally a resolved microtask but is a real fetch the first
      // time a look or layer config is seen) is left to finish rather
      // than starting a second one on top of it: that bounds this to one
      // outstanding render, which is what keeps a slow tick from queueing
      // up a backlog instead of just dropping a frame.
      if (busy) return;
      var elapsed = (nowMs - loopT0) / 1000;
      var idx = Math.floor(elapsed * loopBuf.fps) % loopBuf.count;
      var tag = "loop:" + gen + ":" + idx;
      if (uploadedTag !== tag) {
        var off = idx * loopBuf.stride;
        inst.setSource(loopBuf.data.subarray(off, off + loopBuf.stride), loopBuf.w, loopBuf.h);
        uploadedTag = tag;
        lastIdx = idx;
      }
      var cfg = withResolvedInput(getCfg(), resolvedInput);
      busy = true;
      inst.ready(cfg, { pixelScale: factor }).then(function () {
        if (!running || gen !== loopGen) { busy = false; return; }
        inst.render(cfg, { pixelScale: factor });
        busy = false;
        var t = now();
        fpsWindow.push(t);
        while (fpsWindow.length && t - fpsWindow[0] > 1000) fpsWindow.shift();
        if (opts.onFrame) {
          opts.onFrame({
            index: idx, count: loopBuf.count,
            time: loopBuf.start + idx / loopBuf.fps,
            measuredFps: fpsWindow.length
          });
        }
      }).catch(function (e) {
        busy = false;
        running = false;
        if (opts.onError) opts.onError(e);
      });
    }
    raf = requestAnimationFrame(tick);
  }

  function stopLoop() {
    running = false;
    if (raf) cancelAnimationFrame(raf);
    raf = null;
    busy = false;
  }

  function isLooping() { return running; }

  function loopInfo() {
    if (!loopBuf) return null;
    return {
      width: loopBuf.w, height: loopBuf.h, frames: loopBuf.count,
      fps: loopBuf.fps, start: loopBuf.start, duration: loopBuf.duration
    };
  }

  // Where playback currently is, in clip seconds, so a caller can hand the
  // ordinary still-frame timeline this number the moment the loop stops and
  // have the viewer, scopes and stats land exactly where the loop was.
  function loopCurrentTime() {
    if (!loopBuf || lastIdx < 0) return loopBuf ? loopBuf.start : 0;
    return loopBuf.start + lastIdx / loopBuf.fps;
  }

  // ------------------------------------------------------------------
  // Mode 3: proxy playback
  //
  // Mode 2 above is bounded by memory: it holds every frame of the range as
  // raw uint16 RGB, which is why it has a 700 MB budget and why a whole clip
  // does not fit. This mode removes the cap by never holding frames at all.
  // The server writes one small H.264 proxy per clip (POST /api/proxy/prepare,
  // see server.py's proxy section), a hidden <video> decodes it, and each
  // frame the browser presents is uploaded straight to the GPU source texture
  // and graded through the SAME chain the still path uses. Memory is one
  // video element plus one texture no matter how long the clip is.
  //
  // The trade is the pixel domain: the proxy is 8-bit 4:2:0 where the still
  // path is 16-bit. The server encodes it so that the browser's own YCbCr to
  // RGB conversion lands on the same numbers /api/source returns (that is
  // what the colour tags on the file are for), and the size of what is left
  // is measured, not assumed: see the honesty entry in limits.js.
  // ------------------------------------------------------------------

  var proxyInfoObj = null;   // the last /api/proxy/prepare answer, once ready
  var proxyVideo = null;     // the hidden <video>, created once and reused
  var proxyGen = 0;          // bumped on attach and on stop, tags callbacks
  var proxyUploads = 0;      // makes every video upload's tag unique
  var proxyRunning = false;
  var proxyBusy = false;
  var proxyRaf = null;       // set only on the rAF fallback path
  var proxyStats = null;
  var proxyEndedFn = null;   // the "ended" listener the current playProxy call
                              // owns, so a fresh call replaces it instead of
                              // stacking a second one on the same <video>
                              // (attachProxy is skipped on a warm proxy, so
                              // repeated Play presses on the same clip reuse
                              // one element and would otherwise pile up one
                              // listener per press)

  function proxyReady() { return !!(proxyInfoObj && proxyVideo); }

  function proxyInfo() {
    if (!proxyInfoObj) return null;
    var v = proxyVideo;
    return {
      key: proxyInfoObj.key, url: proxyInfoObj.url,
      width: proxyInfoObj.width, height: proxyInfoObj.height,
      duration: proxyInfoObj.duration, fps: proxyInfoObj.fps,
      range: proxyInfoObj.range, gop: proxyInfoObj.gop, crf: proxyInfoObj.crf,
      bytes: proxyInfoObj.bytes || 0,
      currentTime: v ? v.currentTime : 0,
      playing: proxyRunning
    };
  }

  // Where playback is, as a CLIP timecode (see proxyToClipTime), so a caller
  // can hand this to the still path and land on the same frame.
  function proxyCurrentTime() {
    return proxyVideo ? proxyToClipTime(proxyVideo.currentTime) : 0;
  }

  // The raw playhead of the video element itself, for measurement.
  function proxyVideoTime() {
    return proxyVideo ? proxyVideo.currentTime : 0;
  }

  /* Ask the server for this clip's proxy and wait until the file exists.
   *
   * prepare is idempotent and cheap: it answers "here is the URL" when the
   * encode is already on disk and "here is the job" when it is not, so
   * polling it is also how progress is reported. onProgress gets the job
   * dict, which is what the Play button's "preparing proxy" state reads.
   *
   * p: { clip, width, rotation, range, duration, apiBase, timeoutMs }
   */
  function prepareProxy(p, opts) {
    opts = opts || {};
    var base = p.apiBase || "";
    var body = {
      clip: p.clip, width: p.width || 960, rotation: rotationOf(p)
    };
    if (p.range) body.range = p.range;
    if (p.duration) body.duration = p.duration;
    var deadline = now() + (p.timeoutMs || 180000);

    function ask() {
      return fetchJSON(base + "/api/proxy/prepare", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body)
      }).then(function (r) { return r.json(); }).then(function (j) {
        if (j.ready) return j;
        if (j.job && j.job.status === "failed") {
          throw new Error("proxy encode failed: " + (j.job.message || "no reason given"));
        }
        if (now() > deadline) {
          throw new Error("the proxy for this clip is still encoding after "
            + Math.round((p.timeoutMs || 180000) / 1000) + "s; it is a job in the "
            + "jobs panel, so it is not lost, but playback will not start yet");
        }
        if (opts.onProgress) opts.onProgress(j.job || null);
        return new Promise(function (res) { setTimeout(res, 500); }).then(ask);
      });
    }
    return ask();
  }

  /* Point the hidden <video> at a prepared proxy and resolve once it has a
   * frame to show.
   *
   * The element is created here rather than in index.html because nothing
   * ever looks at it: it is a decoder, not a picture. It is still laid out
   * (1px, transparent) rather than display:none, because "is this element
   * being rendered" is what decides whether Chrome presents frames at all,
   * and a decoder that never presents never fires requestVideoFrameCallback.
   * Measured both ways in this headless Chrome and both worked, so this is
   * the conservative one of two working choices.
   */
  function attachProxy(info, opts) {
    opts = opts || {};
    proxyInfoObj = info;
    var base = opts.apiBase || "";
    if (!proxyVideo) {
      proxyVideo = document.createElement("video");
      proxyVideo.id = "proxyVideo";
      proxyVideo.muted = true;
      proxyVideo.defaultMuted = true;
      proxyVideo.playsInline = true;
      proxyVideo.setAttribute("playsinline", "");
      proxyVideo.setAttribute("aria-hidden", "true");
      proxyVideo.preload = "auto";
      proxyVideo.style.cssText = "position:absolute;left:0;top:0;width:1px;"
        + "height:1px;opacity:0;pointer-events:none;z-index:-1";
      document.body.appendChild(proxyVideo);
    }
    var gen = ++proxyGen;
    var url = base + info.url;
    if (proxyVideo.getAttribute("src") === url && proxyVideo.readyState >= 2) {
      return Promise.resolve(proxyVideo);
    }
    return new Promise(function (resolve, reject) {
      function done() { cleanup(); resolve(proxyVideo); }
      function fail() {
        cleanup();
        var err = proxyVideo.error;
        reject(new Error("the browser could not decode the proxy"
          + (err ? " (media error " + err.code + ")" : "")));
      }
      function cleanup() {
        proxyVideo.removeEventListener("loadeddata", done);
        proxyVideo.removeEventListener("error", fail);
      }
      proxyVideo.addEventListener("loadeddata", done);
      proxyVideo.addEventListener("error", fail);
      proxyVideo.src = url;
      proxyVideo.load();
      setTimeout(function () {
        if (gen !== proxyGen) return;
        if (proxyVideo.readyState >= 2) done();
      }, 8000);
    });
  }

  /* Upload the frame the video is showing right now into the GPU's source
   * texture, in place of the uint16 array setSource() takes.
   *
   * This reaches into the StudioGPU instance's own gl context and src slot on
   * purpose: gpu.js has no video path and this lane does not own that file.
   * The three pixelStorei calls are the whole reason a video frame can stand
   * in for /api/source at all. UNPACK_COLORSPACE_CONVERSION_WEBGL = NONE
   * stops Chrome from colour managing the frame into the canvas's space on
   * the way in (which would silently regrade every pixel before the shader
   * ever saw it), premultiply off keeps the numbers as decoded, and flip off
   * matches the row order setSource uploads with.
   */
  function uploadVideoFrame(v) {
    var gl = inst.gl;
    var w = v.videoWidth, h = v.videoHeight;
    if (!w || !h) return null;
    inst.G.scratch();
    gl.pixelStorei(gl.UNPACK_COLORSPACE_CONVERSION_WEBGL, gl.NONE);
    gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, false);
    gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, false);
    if (!inst.src || inst.src.w !== w || inst.src.h !== h) {
      if (inst.src) gl.deleteTexture(inst.src.tex);
      var tex = gl.createTexture();
      gl.bindTexture(gl.TEXTURE_2D, tex);
      // RGBA32F, same internal format setSource uses, so every shader
      // downstream reads exactly what it reads on the still path.
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA32F, gl.RGBA, gl.FLOAT, v);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
      inst.src = { tex: tex, w: w, h: h };
    } else {
      gl.bindTexture(gl.TEXTURE_2D, inst.src.tex);
      gl.texSubImage2D(gl.TEXTURE_2D, 0, 0, 0, gl.RGBA, gl.FLOAT, v);
    }
    // The still and loop paths both skip a re-upload when their tag still
    // matches; a video frame has to invalidate that or the next still render
    // would present a proxy frame under the still's config.
    uploadedTag = "proxy:" + (++proxyUploads);
    return inst.src;
  }

  /* Clip timecode to proxy playhead, and back.
   *
   * These two exist because the still path and a <video> element do not name
   * frames the same way, and the difference is a whole frame. Measured on
   * this build: ffmpeg's -ss returns the FIRST frame whose timestamp is at or
   * after the time given (-ss 0.5 gave frame 12, -ss 0.5208 gave frame 13),
   * while a video element at currentTime shows the frame whose interval
   * CONTAINS that time (0.5208 shows frame 12). Seeking the proxy to the
   * timecode the still path was given therefore shows the frame BEFORE the
   * one on the timeline: measured as a mean difference of 6.2 of 255 against
   * 2.6 when aligned, on the same clip, config and frame.
   *
   * clipToProxy lands in the MIDDLE of the wanted frame so no rounding at
   * either end can tip the browser into a neighbour. proxyToClip goes back a
   * quarter frame from the frame's own timestamp for the same reason in the
   * other direction: asking the server for exactly N/fps is a floating point
   * coin flip between frame N and frame N+1.
   */
  function proxyFps() {
    return (proxyInfoObj && proxyInfoObj.fps) || 24;
  }

  function clipToProxyTime(t) {
    var fps = proxyFps();
    var n = Math.ceil(t * fps - 1e-6);
    return Math.max(0, (n + 0.5) / fps);
  }

  function proxyToClipTime(c) {
    var fps = proxyFps();
    var n = Math.floor(c * fps + 1e-6);
    return Math.max(0, (n - 0.25) / fps);
  }

  function proxyFactor(opts) {
    var sourceWidth = (opts && opts.sourceWidth) || (proxyInfoObj && proxyInfoObj.source_width);
    var w = (proxyVideo && proxyVideo.videoWidth) || (proxyInfoObj && proxyInfoObj.width) || 1;
    if (!sourceWidth) return 1;
    return w / sourceWidth;
  }

  /* One graded frame from whatever the video is showing. Used by the play
   * loop, by seeking while paused and by frame stepping, so all three land
   * on identical pixels for the same time and config.
   *
   * opts.resolvedInput is the clip's clips[].source.resolved_input, exactly
   * as renderStill takes it: it is applied here rather than at the three
   * call sites so playProxy, seekProxy and stepProxy cannot drift apart on
   * which cube "auto" means. Leaving it out keeps the old behaviour, where
   * "auto" is the Apple Log cubes.
   */
  function renderProxyFrame(cfg, opts) {
    if (!inst) return Promise.reject(new Error("no GPU renderer available"));
    if (!proxyVideo) return Promise.reject(new Error("no proxy attached"));
    cfg = withResolvedInput(cfg, opts && opts.resolvedInput);
    var check = checkStages(cfg);
    if (check.blocking.length) {
      var e = new Error("GPU playback cannot render this config: " + check.report.summary);
      e.stageReport = check.report;
      return Promise.reject(e);
    }
    var factor = proxyFactor(opts);
    uploadVideoFrame(proxyVideo);
    return inst.ready(cfg, { pixelScale: factor }).then(function () {
      var r = inst.render(cfg, { pixelScale: factor });
      return {
        ms: r.ms, passes: r.passes, width: r.width, height: r.height,
        time: proxyVideo.currentTime, grain: check.grainOnly, report: check.report
      };
    });
  }

  function newProxyStats() {
    return {
      frames: 0,          // frames this renderer actually graded
      skipped: 0,         // frames the video presented that it did not grade
      dropped: 0,         // frames the DECODER dropped (playback quality)
      measuredFps: 0,
      lastPresented: 0,
      window: [],
      t0: now()
    };
  }

  function noteProxyFrame(meta) {
    var st = proxyStats;
    if (!st) return;
    st.frames++;
    if (meta && typeof meta.presentedFrames === "number") {
      if (st.lastPresented && meta.presentedFrames > st.lastPresented + 1) {
        st.skipped += meta.presentedFrames - st.lastPresented - 1;
      }
      st.lastPresented = meta.presentedFrames;
    }
    if (proxyVideo && proxyVideo.getVideoPlaybackQuality) {
      var q = proxyVideo.getVideoPlaybackQuality();
      if (q && typeof q.droppedVideoFrames === "number") st.dropped = q.droppedVideoFrames;
    }
    var t = now();
    st.window.push(t);
    while (st.window.length && t - st.window[0] > 1000) st.window.shift();
    st.measuredFps = st.window.length;
  }

  /* Play the attached proxy, grading every presented frame.
   *
   * getCfg is called fresh per frame for the same reason startLoop does it:
   * that, and nothing else, is what makes a knob change show up on the next
   * frame instead of needing playback restarted.
   *
   * opts.resolvedInput (clips[].source.resolved_input) rides through to
   * renderProxyFrame, which is where "auto" becomes a real input for every
   * proxy path at once; seekProxy and stepProxy take the same key.
   *
   * requestVideoFrameCallback is the right clock here because it fires once
   * per frame the compositor actually presents, so this renders exactly the
   * frames that exist rather than re-grading the same one several times at
   * display rate. Browsers without it fall back to requestAnimationFrame,
   * which costs redundant re-grades of an unchanged frame but is never
   * wrong; the fallback reports itself in stats().
   */
  /* opts.loop (contract E3, "Play loops until stopped"): when true, playback
   * wraps back to opts.loopStart (clip time, default 0) instead of stopping,
   * whether it reaches opts.loopEnd (bounded segment, the render dialog's
   * numeric secs case) or the proxy's own natural end (unbounded, secs
   * empty). Both paths land on the same wrap() so a short clip whose last
   * graded frame never quite reaches loopEnd still loops correctly off the
   * video element's own "ended" event. Without opts.loop this is the old
   * one-shot behaviour: onEnded fires once and nothing plays again. */
  function playProxy(getCfg, opts) {
    if (!inst) throw new Error("no GPU renderer available");
    if (!proxyVideo) throw new Error("no proxy attached");
    opts = opts || {};
    var gen = proxyGen;
    proxyRunning = true;
    proxyBusy = false;
    proxyStats = newProxyStats();
    proxyStats.mode = proxyVideo.requestVideoFrameCallback ? "rvfc" : "raf";
    proxyVideo.playbackRate = 1;

    var loopStart = Math.max(0, (opts.loop && opts.loopStart) || 0);
    var loopEnd = null;
    if (opts.loop) {
      var full = (proxyInfoObj && proxyInfoObj.duration) || null;
      loopEnd = (typeof opts.loopEnd === "number" && opts.loopEnd > loopStart)
        ? (full ? Math.min(opts.loopEnd, full) : opts.loopEnd)
        : full;
    }
    var loopFrame = 1 / proxyFps();

    function wrap() {
      // Same currentTime write a seek makes, deliberately not routed through
      // seekProxy: this runs from inside a still-playing element and a seek
      // continues playback on its own once it lands, no separate play() call
      // needed (the "ended" branch below is the one exception, since by the
      // time that event exists the element has actually stopped).
      try { proxyVideo.currentTime = clipToProxyTime(loopStart); } catch (e) { /* next frame retries */ }
    }

    function step(meta) {
      if (!proxyRunning || gen !== proxyGen) return;
      if (proxyBusy) return;
      var cfg = getCfg();
      proxyBusy = true;
      renderProxyFrame(cfg, opts).then(function (r) {
        proxyBusy = false;
        if (!proxyRunning || gen !== proxyGen) return;
        noteProxyFrame(meta);
        var clipTime = proxyToClipTime(r.time);
        if (opts.onFrame) {
          opts.onFrame({
            // The clip timecode of the frame just graded, not the video's raw
            // playhead: see proxyToClipTime. A caller that hands this straight
            // to a still render gets the same frame back.
            time: clipTime, videoTime: r.time, ms: r.ms,
            measuredFps: proxyStats.measuredFps,
            frames: proxyStats.frames,
            skipped: proxyStats.skipped,
            dropped: proxyStats.dropped,
            grain: r.grain
          });
        }
        if (loopEnd != null && clipTime >= loopEnd - loopFrame) wrap();
      })["catch"](function (e) {
        proxyBusy = false;
        proxyRunning = false;
        if (opts.onError) opts.onError(e);
      });
    }

    function vfc(nowMs, meta) {
      if (!proxyRunning || gen !== proxyGen) return;
      proxyVideo.requestVideoFrameCallback(vfc);
      step(meta);
    }
    function raf() {
      if (!proxyRunning || gen !== proxyGen) return;
      proxyRaf = requestAnimationFrame(raf);
      step(null);
    }

    // A fresh call replaces whatever "ended" listener the last one left
    // attached (see proxyEndedFn's own comment): without this, pressing Play
    // twice on a clip whose proxy is already warm stacks a second listener
    // on the same element, since ensureProxy skips attachProxy (the thing
    // that used to bump proxyGen and orphan the old one) on a warm proxy.
    if (proxyEndedFn) {
      proxyVideo.removeEventListener("ended", proxyEndedFn);
      proxyEndedFn = null;
    }
    // The end of the clip is not an error: without a loop it is a pause, so
    // the caller's UI would otherwise sit there saying "playing" over a
    // video that finished; with a loop it is not a pause at all, just the
    // point the range wraps.
    function onEnd() {
      if (gen !== proxyGen) { proxyVideo.removeEventListener("ended", onEnd); return; }
      if (opts.loop) {
        wrap();
        var rp = proxyVideo.play();
        if (rp && rp["catch"]) rp["catch"](function (e) { if (opts.onError) opts.onError(e); });
        return;
      }
      proxyVideo.removeEventListener("ended", onEnd);
      proxyEndedFn = null;
      proxyRunning = false;
      if (opts.onEnded) opts.onEnded();
    }
    proxyEndedFn = onEnd;
    proxyVideo.addEventListener("ended", onEnd);

    var p = proxyVideo.play();
    if (p && p["catch"]) {
      p["catch"](function (e) {
        proxyRunning = false;
        if (opts.onError) opts.onError(e);
      });
    }
    if (proxyVideo.requestVideoFrameCallback) proxyVideo.requestVideoFrameCallback(vfc);
    else proxyRaf = requestAnimationFrame(raf);
  }

  function pauseProxy() {
    proxyRunning = false;
    proxyBusy = false;
    if (proxyRaf) cancelAnimationFrame(proxyRaf);
    proxyRaf = null;
    if (proxyVideo) {
      proxyVideo.pause();
      // Stops a loop dead rather than leaving a listener that could still
      // fire "ended" and wrap the playhead after the user pressed stop:
      // pausing genuinely ends this play session, the next Play press starts
      // a fresh one with its own listener.
      if (proxyEndedFn) {
        proxyVideo.removeEventListener("ended", proxyEndedFn);
        proxyEndedFn = null;
      }
    }
  }

  function isProxyPlaying() { return proxyRunning; }

  /* Seek to a clip time and grade the frame that lands, without playing.
   *
   * The one frame is rendered from inside the video's own "seeked" event
   * (through one requestVideoFrameCallback where available), because that is
   * the first moment the element is showing the new frame rather than the
   * old one: uploading before it would grade the frame the user just left.
   */
  function seekProxy(t, getCfg, opts) {
    if (!proxyVideo) return Promise.reject(new Error("no proxy attached"));
    opts = opts || {};
    var gen = proxyGen;
    var dur = proxyVideo.duration || (proxyInfoObj && proxyInfoObj.duration) || 0;
    // t is a CLIP timecode, the same number the still path takes, and
    // clipToProxyTime is what makes those two agree on which frame that is.
    // opts.rawSeek is for the measurement tool, which needs to sweep the
    // playhead without this mapping in the way.
    var want = opts.rawSeek ? t : clipToProxyTime(t);
    var target = Math.max(0, dur ? Math.min(want, Math.max(0, dur - 1e-3)) : want);
    // The element is captured, not read from module state on each callback:
    // stopProxy() can drop the module's reference while a seek is still in
    // flight, and a callback that then touched a null proxyVideo would throw
    // inside an event handler where nothing is listening for it.
    var el = proxyVideo;
    return new Promise(function (resolve, reject) {
      var settled = false;
      function finish() {
        if (settled) return;
        settled = true;
        el.removeEventListener("seeked", onSeeked);
        // A different clip, or a re-attached proxy, took over while this
        // seek was in flight: there is no frame left to grade, so this
        // resolves with nothing rather than staying pending forever.
        // app.js awaits this promise before it draws the 16 bit still, so a
        // promise that never settles would freeze the viewer on whatever
        // frame it happened to be showing.
        if (gen !== proxyGen || !getCfg) { resolve(null); return; }
        // getCfg and renderProxyFrame both run caller code, and this runs
        // from an event callback where a throw goes nowhere: without this
        // catch the promise stays pending for the life of the page.
        // Measured, not imagined: a measurement script that passed a config
        // of null hung its first seek and never came back.
        try {
          renderProxyFrame(getCfg(), opts).then(resolve, reject);
        } catch (e) {
          reject(e);
        }
      }
      function onSeeked() {
        if (el.requestVideoFrameCallback) {
          el.requestVideoFrameCallback(function () { finish(); });
          // A paused element that is already showing the target frame may
          // never present another one, so the callback above is a best case
          // and not a guarantee; this is the floor under it.
          setTimeout(finish, 120);
        } else {
          finish();
        }
      }
      if (Math.abs(el.currentTime - target) < 1e-4 && el.readyState >= 2) {
        onSeeked();
        return;
      }
      el.addEventListener("seeked", onSeeked);
      try {
        el.currentTime = target;
      } catch (e) {
        el.removeEventListener("seeked", onSeeked);
        settled = true;
        reject(e);
      }
      setTimeout(function () {
        if (!settled) finish();
      }, 4000);
    });
  }

  // One frame forward or back from the frame the video is showing now.
  // Counted in whole frames of the proxy's own rate rather than added as
  // seconds, so repeated steps cannot accumulate a drift into a neighbour.
  function stepProxy(frames, getCfg, opts) {
    if (!proxyVideo) return Promise.reject(new Error("no proxy attached"));
    var fps = proxyFps();
    var n = Math.floor(proxyVideo.currentTime * fps + 1e-6) + (frames || 1);
    return seekProxy(Math.max(0, (n + 0.5) / fps), getCfg,
                     Object.assign({}, opts || {}, { rawSeek: true }));
  }

  // Frames per second this renderer achieved, what it skipped, and what the
  // decoder dropped underneath it. Null until something has played.
  function proxyPlaybackStats() {
    if (!proxyStats) return null;
    return {
      mode: proxyStats.mode, frames: proxyStats.frames,
      skipped: proxyStats.skipped, dropped: proxyStats.dropped,
      measuredFps: proxyStats.measuredFps,
      elapsedMs: now() - proxyStats.t0
    };
  }

  /* Stop playback and let go of the decoder. Removing the src is what
   * actually frees the video's own frame buffers; leaving it attached keeps
   * a decode pipeline alive for a clip nobody is watching.
   */
  function stopProxy() {
    pauseProxy();
    proxyGen++;
    if (proxyVideo) {
      proxyVideo.removeAttribute("src");
      proxyVideo.load();
    }
    proxyInfoObj = null;
  }

  global.StudioLive = {
    init: init,
    available: available,
    reason: reason,
    setDefaults: setDefaults,
    checkStages: checkStages,
    withResolvedInput: withResolvedInput,
    renderStill: renderStill,
    budget: budget,
    prepareLoop: prepareLoop,
    startLoop: startLoop,
    stopLoop: stopLoop,
    isLooping: isLooping,
    loopInfo: loopInfo,
    loopCurrentTime: loopCurrentTime,
    RANGE_MAX_BYTES: RANGE_MAX_BYTES,
    prepareProxy: prepareProxy,
    attachProxy: attachProxy,
    proxyReady: proxyReady,
    proxyInfo: proxyInfo,
    proxyCurrentTime: proxyCurrentTime,
    proxyVideoTime: proxyVideoTime,
    clipToProxyTime: clipToProxyTime,
    proxyToClipTime: proxyToClipTime,
    renderProxyFrame: renderProxyFrame,
    playProxy: playProxy,
    pauseProxy: pauseProxy,
    isProxyPlaying: isProxyPlaying,
    seekProxy: seekProxy,
    stepProxy: stepProxy,
    proxyPlaybackStats: proxyPlaybackStats,
    stopProxy: stopProxy
  };
})(typeof window !== "undefined" ? window : this);
