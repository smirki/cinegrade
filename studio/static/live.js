/* StudioLive: wires gpu.js into the main viewer.
 *
 * Two independent things live here, both built on the same StudioGPU
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
   * Grain is the one permanent, documented exception: ffmpeg's noise filter
   * seeds from the wall clock and does not reproduce itself between two
   * ffmpeg runs either, so there is no "correct" GPU grain to chase and
   * stageReport marks it unsupported forever. That is not a reason to bail
   * out of the GPU path the way a genuinely wrong stage would be; it is a
   * reason to say so in the UI and let the caller flip to the server render
   * to check. Anything else stageReport calls unsupported is a real
   * accuracy gap and has to fall all the way back.
   */
  function checkStages(cfg) {
    var rep = StudioGPU.stageReport(cfg);
    var blocking = rep.unsupported.filter(function (id) { return id !== "grain"; });
    // The power window exists in the ffmpeg engine only until its shader port
    // lands. gpu.js has no window stage at all, so it would not report one as
    // unsupported: it would simply grade the whole frame and show a picture
    // that quietly ignores the shape the user just drew. That is the exact
    // failure this function exists to prevent, so force the fall back.
    if (cfg && cfg.window && cfg.window.enabled && blocking.indexOf("window") < 0) {
      blocking = blocking.concat(["window"]);
    }
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

  // ------------------------------------------------------------------
  // Mode 1: still frames
  // ------------------------------------------------------------------

  var stillCache = null; // { key, data, w, h }

  function stillKey(p) {
    return p.clip + "|" + (+p.time).toFixed(4) + "|" + p.width + "|" + (p.autorotate !== false);
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
        autorotate: p.autorotate !== false
      })
    }).then(function (r) {
      var size = (r.headers.get("X-Frame-Size") || "0x0").split("x").map(Number);
      return r.arrayBuffer().then(function (buf) {
        stillCache = { key: key, data: new Uint16Array(buf), w: size[0], h: size[1] };
        return stillCache;
      });
    });
  }

  /* Grade one still frame on the GPU and present it to the canvas passed to
   * init(). Rejects (never throws synchronously) when the GPU cannot be
   * trusted for this config, so a caller can always `.catch` into the
   * server path without a try/catch of its own.
   *
   * p: { clip, time, width, autorotate, config, sourceWidth, apiBase }
   * sourceWidth is the clip's own upright width (pre-downscale), needed to
   * compute the same pixelScale the server's scale_for_preview would use;
   * without it a reduced-size preview overstates every pixel-denominated FX.
   */
  function renderStill(p) {
    if (!inst) {
      return Promise.reject(new Error(
        "no GPU renderer available (" + (initReason || "not initialised") + ")"));
    }
    var cfg = p.config;
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
          grain: check.grainOnly, report: check.report
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
      + "&rot=" + (p.autorotate !== false ? "1" : "0");
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
   * p: { clip, time (range start), duration, width, autorotate, apiBase }
   * Resolves { width, height, frames, fps, start, duration, loadMs }.
   */
  function prepareLoop(p) {
    var base = p.apiBase || "";
    var clip = p.clip, autorotate = p.autorotate !== false;
    var width = p.width, start = p.time || 0, duration = p.duration;
    if (!(duration > 0)) return Promise.reject(new Error("loop range must be longer than 0s"));

    return budget({ clip: clip, width: width, autorotate: autorotate, apiBase: base })
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
            clip: clip, time: start, duration: duration, width: width, autorotate: autorotate
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
   * opts: { sourceWidth, onFrame(info), onError(err) }
   * Renders every animation frame the display asks for, not only when the
   * clip's own frame index advances, so a knob dragged between two source
   * frames still updates at display rate instead of waiting on the next
   * one; the source texture itself is only re-uploaded when the index
   * actually changes, which is the one part of this that is not free.
   */
  function startLoop(getCfg, opts) {
    if (!loopBuf) throw new Error("no loop range prepared");
    if (!inst) throw new Error("no GPU renderer available");
    opts = opts || {};
    var sourceWidth = opts.sourceWidth || loopBuf.w;
    var factor = loopBuf.w / sourceWidth;
    var gen = loopGen;
    running = true; busy = false; lastIdx = -1; fpsWindow.length = 0;
    loopT0 = now();

    function tick(nowMs) {
      if (!running) return;
      raf = requestAnimationFrame(tick);
      // A render already in flight (waiting on ready()'s LUT promise, which
      // is normally a resolved microtask but is a real fetch the first
      // time a look or secondary config is seen) is left to finish rather
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
      var cfg = getCfg();
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

  global.StudioLive = {
    init: init,
    available: available,
    reason: reason,
    setDefaults: setDefaults,
    checkStages: checkStages,
    renderStill: renderStill,
    budget: budget,
    prepareLoop: prepareLoop,
    startLoop: startLoop,
    stopLoop: stopLoop,
    isLooping: isLooping,
    loopInfo: loopInfo,
    loopCurrentTime: loopCurrentTime,
    RANGE_MAX_BYTES: RANGE_MAX_BYTES
  };
})(typeof window !== "undefined" ? window : this);
