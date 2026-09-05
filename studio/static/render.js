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

    // width/height explicit: this worker calls ready() before setSource16
    // ever runs (the loop below fetches the first frame after ready()
    // resolves), so this.src is still null and the grain plate prefetch in
    // ready() has nothing to fall back to unless told the size directly.
    var opts = { pixelScale: plan.pixelScale, width: plan.width, height: plan.height };
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
        inst.setSource16(got.data, plan.width, plan.height);
        inst.render(plan.config, { pixelScale: plan.pixelScale, want16: true });
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
