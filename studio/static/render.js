/* The GPU final render worker page.
 *
 * Loaded in a headless Chrome by studio/tools/render_worker.mjs, driven by
 * studio/render_gpu.py. The loop is deliberately plain HTTP rather than
 * anything that crosses the DevTools protocol: CDP carries binary as base64
 * inside JSON, so a 50 MB 4K frame through page.evaluate would cost more than
 * the grade does. Frames come down over fetch as raw bytes and go back up the
 * same way.
 *
 * Ordering is by explicit frame index, never by arrival: the server writes
 * frame n to the encoder only when frame n has come back, so overlapping
 * requests cannot reorder the picture.
 *
 * Everything this page fetches is authorised by the Authorization header the
 * worker sets on the browser, which carries the token minted for this one
 * render. There is no token in this file and none in the URL.
 */
(function () {
  "use strict";

  var statusEl = document.getElementById("status");
  var info = {
    renderer: "", frames: 0, ms: 0, fps: 0, width: 0, height: 0,
    readback: "", error: ""
  };
  window.__renderInfo = info;

  function say(text) {
    statusEl.textContent = text;
    // The worker relays console output into the job log, so this is how a
    // render that stalls says where it stalled.
    if (window.console && console.log) console.log("[render] " + text);
  }

  function finish(state, message) {
    if (message) info.error = message;
    document.body.dataset.done = state;
    say(state === "1" ? "done" : ("error: " + message));
  }

  function post(url, body, headers) {
    return fetch(url, { method: "POST", body: body, headers: headers || {} });
  }

  function fetchPlan() {
    return fetch("/api/render/gpu/plan").then(function (r) {
      if (!r.ok) return r.text().then(function (t) { throw new Error("plan: " + t); });
      return r.json();
    });
  }

  /* One source frame, or null when the decoder has no more.
   *
   * 204 is the end of the stream, and it is the ONLY end of stream signal:
   * the server hands frames out under a lock, so a 204 means the decode
   * finished and nothing is left, never "not ready yet". */
  function fetchFrame() {
    return fetch("/api/render/gpu/next").then(function (r) {
      if (r.status === 204) return null;
      if (!r.ok) return r.text().then(function (t) { throw new Error("next: " + t); });
      var index = parseInt(r.headers.get("X-Frame-Index") || "-1", 10);
      return r.arrayBuffer().then(function (buf) {
        return { index: index, data: new Uint16Array(buf) };
      });
    });
  }

  function sendFrame(index, out) {
    return post("/api/render/gpu/frame?i=" + index, out.data,
                { "Content-Type": "application/octet-stream",
                  "X-Frame-Format": out.format })
      .then(function (r) {
        if (!r.ok) return r.text().then(function (t) { throw new Error("frame " + index + ": " + t); });
        return true;
      });
  }

  /* Every matte id this config's component stacks name.
   *
   * Needed because a matte component is the one part of the config whose
   * pixels depend on WHEN the frame is, so a render that cannot work out the
   * time of a frame must refuse rather than grade every frame against matte
   * frame 0 and hand back a file with a still mask on a moving subject. */
  function configMatteIds(config) {
    var ids = [];
    StudioGPU.configLayers(StudioGPU.fullConfig(config)).forEach(function (L) {
      if (!L || !L.mask || !StudioGPU.mask.usesComponents(L.mask)) return;
      StudioGPU.mask.components(L.mask).forEach(function (c) {
        var id = c && c.type === "matte" && c.matte && c.matte.id;
        if (id && ids.indexOf(id) < 0) ids.push(id);
      });
    });
    return ids;
  }

  /* The matte rows of a render's report that did not get the frame they
   * asked for, as sentences. Empty when every matte served its own frame. */
  function lagging(mattes) {
    var out = [];
    Object.keys(mattes || {}).forEach(function (id) {
      var st = mattes[id];
      if (!st || !st.lagging) return;
      out.push(id + " (" + st.state + ") wanted frame "
        + (st.want === null ? "?" : st.want)
        + (st.empty ? " and has nothing decoded"
                    : ", served frame " + st.got));
    });
    return out;
  }

  function rendererString(gl) {
    try {
      var dbg = gl.getExtension("WEBGL_debug_renderer_info");
      if (dbg) return String(gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL));
    } catch (e) { /* fall through to the masked string */ }
    return String(gl.getParameter(gl.RENDERER));
  }

  function run(plan) {
    var canvas = document.getElementById("gl");
    StudioGPU.setDefaults(plan.defaults);
    var inst = StudioGPU.create(canvas, { apiBase: "" });
    if (!inst) throw new Error("no WebGL2: " + StudioGPU.lastError);
    // No present pass: nothing is looking at this canvas, and at 4K the copy
    // would cost a full frame of bandwidth per frame for a picture nobody
    // sees. render() skips it when there is no canvas.
    inst.canvas = null;
    info.renderer = rendererString(inst.gl);
    info.width = plan.width;
    info.height = plan.height;
    if (!inst.readOutput || !inst.setSource16) {
      throw new Error("this gpu.js has no final render readback (readOutput)");
    }
    if (window.console && console.log) {
      console.log(JSON.stringify({ renderer: info.renderer,
                                   size: plan.width + "x" + plan.height,
                                   expected: plan.expected }));
    }

    /* The clip time of a frame, from the plan's own timebase.
     *
     * X-Frame-Index counts from 0 at the START of the render range (the
     * decoder is seeded with -ss), so the clip time of frame i is
     * start + i/fps, which is the number the matte store is indexed by (C2).
     * A plan without fps cannot say when any frame is, and a matte component
     * would then read frame 0 for the whole file: that is refused out loud
     * here instead of shipped as a silently wrong render. */
    var fps = +plan.fps || 0;
    var startAt = +plan.start || 0;
    var matteIds = configMatteIds(plan.config);
    if (matteIds.length && !(fps > 0)) {
      throw new Error(
        "this render's plan carries no frame rate, so the time of each frame "
        + "is unknown, and this config uses tracked matte "
        + (matteIds.length === 1 ? "component " : "components ")
        + matteIds.join(", ") + ", whose pixels change with time. Refusing "
        + "rather than rendering every frame against the matte's first frame. "
        + "The GPU render plan needs \"fps\" and \"start\" (studio/render_gpu.py).");
    }
    function frameTime(index) {
      return fps > 0 ? startAt + index / fps : startAt;
    }

    // width/height explicit: this worker calls ready() before setSource16
    // ever runs (the loop below fetches the first frame after ready()
    // resolves), so this.src is still null and the grain plate prefetch in
    // ready() has nothing to fall back to unless told the size directly.
    var opts = { pixelScale: plan.pixelScale, width: plan.width,
                 height: plan.height, time: frameTime(0) };
    var maxPosts = Math.max(1, Math.min(3, plan.window || 2));

    return inst.ready(plan.config, opts).then(async function () {
      var posts = new Set();
      var t0 = 0;
      var next = fetchFrame();

      while (true) {
        var got = await next;
        if (!got) break;
        next = fetchFrame();                   // decode overlaps the grade
        if (!t0) t0 = performance.now();
        var t = frameTime(got.index);
        inst.setSource16(got.data, plan.width, plan.height);
        /* A FINAL render waits for the matte frame; only the preview is
         * allowed to run on ahead of it (design rule 10 is about not stalling
         * the picture a user is watching). A file is written once, so a frame
         * graded against a matte frame that had not arrived yet would be a
         * permanently wrong file. Resolves off the cache when the read ahead
         * has already fetched it, which after the first frame it has. */
        if (matteIds.length) await inst.matteReady(plan.config, t);
        var r = inst.render(plan.config, { pixelScale: plan.pixelScale,
                                           want16: true, time: t });
        var lag = lagging(r.mattes);
        if (lag.length) {
          throw new Error("frame " + got.index + " at " + t.toFixed(3)
            + "s could not get the matte frame it needs: " + lag.join("; ")
            + ". Track the whole range (or shorten the render range) and try "
            + "again; a still matte on a moving subject will not be written.");
        }
        var out = inst.readOutput();
        info.readback = out.format;
        // let, not var: the callback below closes over THIS iteration's
        // promise. With var it would delete whichever promise the last
        // iteration happened to leave in the variable and the window would
        // stop bounding anything.
        let p = sendFrame(got.index, out).then(function () { posts.delete(p); });
        posts.add(p);
        info.frames++;
        if (info.frames % 5 === 0) {
          info.ms = performance.now() - t0;
          info.fps = info.frames / (info.ms / 1000);
          say("frame " + info.frames + " of " + plan.expected
              + " (" + info.fps.toFixed(2) + " fps)");
        }
        // The window that keeps decode, grade and encode overlapping without
        // letting finished frames pile up in memory: at 4K each one is 50 MB.
        while (posts.size >= maxPosts) await Promise.race(Array.from(posts));
      }

      await Promise.all(Array.from(posts));
      info.ms = t0 ? performance.now() - t0 : 0;
      info.fps = info.ms ? info.frames / (info.ms / 1000) : 0;
    });
  }

  say("fetching the plan");
  fetchPlan().then(run).then(function () {
    finish("1", "");
  }).catch(function (err) {
    finish("error", (err && err.message) || String(err));
    if (window.console && console.log) {
      console.log(JSON.stringify({ error: (err && err.message) || String(err) }));
    }
  });
})();
