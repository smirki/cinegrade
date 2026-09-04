/* Fixxr Studio application.
 *
 * One rule holds the whole thing together: the config object is the truth and
 * every panel is a view of it. A parameter change writes to the config, then a
 * single refresh pushes the config back out to every widget and asks the server
 * for a new frame. Widgets never talk to each other. */

(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  var CLIENT = "studio-" + Math.random().toString(36).slice(2, 8);
  var GEN = {};

  var S = {
    state: null,
    defaults: {},
    clip: null,
    autorotate: true,
    time: 0,
    duration: 0,
    fps: 24,
    width: 960,
    slots: { A: null, B: null },
    active: "A",
    presetName: "",
    presetCfg: null,
    marks: [],
    scopes: { waveform: true, parade: true, vectorscope: true, histogram: false },
    scopesAuto: true,
    refName: null,
    refShow: false,
    // The match region drawn on the reference, as [x, y, w, h] fractions of
    // the reference's own size (0 to 1, same shape match_reference's
    // crop_frac wants). null means no box: the auto content detector picks
    // the region, same as before this existed.
    refCropFrac: null,
    viewMode: "after",
    beforeHold: false,
    mask: false,
    sheet: false,
    zoom: "fit",
    history: [],
    future: [],
    blobs: {},
    // Render naming (job 4): a fixed token per preset load, not a fresh one
    // per keystroke, so the field does not visibly churn while a slider is
    // being dragged. renderNameAuto is false the moment the user types in
    // the field by hand, and only loadPreset turns it back on, so a name
    // they chose on purpose is never silently overwritten.
    renderStamp: Math.floor(Date.now() / 1000 % 100000),
    renderNameAuto: true,
    // Filesystem browser (job: file browser, merged Clips/Browse tab).
    // browsePath is null until the tab is opened once, which is also what
    // tells the lazy loader whether it has already run this session.
    // browseDirs/browseFiles/browseError cache the last successful /api/browse
    // response so the two lists can be redrawn (a keyboard cursor move, a
    // clip finishing loading) without a round trip. browseCursor is the
    // keyboard-navigable index into dirs-then-files, -1 meaning nothing is
    // under the cursor yet.
    browsePath: null,
    browseParent: "",
    browseDirs: [],
    browseFiles: [],
    browseError: "",
    browseCursor: -1,
    // Playback (job: play button). playing is true only once the <video>
    // element has actually started painting frames (the "playing" event);
    // playPreparing covers the gap between pressing Play and that, which is
    // exactly the window the honesty requirement in the brief is about ("if
    // the first fragment has not arrived yet, say so"). playSegStart is the
    // clip timecode the video's own currentTime=0 corresponds to, since a
    // played segment always starts at the playhead, not at the clip's 0.
    playing: false,
    playPreparing: false,
    playSegStart: 0,
    // GPU preview (job: live GPU viewer). gpu is the user's on/off choice
    // for mode 1 (still frames); gpuOk is whether StudioLive.init() found a
    // usable WebGL2 context at all, set once at boot and never again, and is
    // what the toggle itself is disabled against when false. looping and
    // loopPreparing are mode 2's own pair, deliberately shaped like
    // playing/playPreparing above: same "idle / preparing / running" story,
    // just driven by gpu.js frame by frame instead of a streamed video.
    gpu: false,
    gpuOk: false,
    looping: false,
    loopPreparing: false
  };

  // The five before/after view modes (job 3). Kept as constants rather than
  // string literals scattered through the file so a typo in one place fails
  // loudly instead of silently landing on the default mode.
  var MODE_AFTER = "after", MODE_BEFORE = "before", MODE_LR = "lr",
      MODE_TB = "tb", MODE_WIPE = "wipe";
  var MODE_LABELS = {};
  MODE_LABELS[MODE_AFTER] = "after"; MODE_LABELS[MODE_BEFORE] = "before";
  MODE_LABELS[MODE_LR] = "left / right"; MODE_LABELS[MODE_TB] = "top / bottom";
  MODE_LABELS[MODE_WIPE] = "wipe";
  // Y's cycle order. Finding the current mode's index and stepping to the
  // next one handles every starting point in one rule: starting from a mode
  // that is not in this list at all (before-only) gives indexOf -1, and
  // -1 + 1 = 0 lands on the first entry, which is exactly "start the split
  // cycle" -- the same behaviour as starting from AFTER at the end of it.
  var SPLIT_CYCLE = [MODE_LR, MODE_TB, MODE_WIPE, MODE_AFTER];

  function cfg() { return S.slots[S.active]; }
  function clone(o) { return JSON.parse(JSON.stringify(o)); }

  // The clip's own upright width, the number every pixel-denominated FX
  // (halation and bloom sigma above all) is scaled relative to. gpu.js's
  // pixelScale option needs this, same as gpupreview.html's sourceWidth()
  // does, so a reduced-width GPU preview scales its glow the same way the
  // server's scale_for_preview does rather than showing it four times too
  // wide at a quarter-size preview.
  function clipSourceWidth() {
    var entry = S.state && S.state.clips.filter(function (c) { return c.name === S.clip; })[0];
    if (!entry) return S.width;
    var dims = S.autorotate ? entry.autorotate : entry.raw;
    return (dims && dims.width) || S.width;
  }

  // The one place that writes #rendererBadge, so "which renderer produced
  // the picture on screen" can never drift from what doRender/the loop
  // actually did. grainWarn is the one documented case where the GPU
  // picture is representative, not identical (see checkStages in live.js).
  function setRendererBadge(label, grainWarn) {
    var el = $("rendererBadge");
    el.textContent = label + (grainWarn
      ? " (grain: representative only, not identical to the server render)" : "");
    el.classList.toggle("warn", !!grainWarn);
  }

  // A hold of the space bar always previews before-only without touching
  // the latched mode, so releasing it puts you back exactly where you were.
  function effectiveMode() { return S.beforeHold ? MODE_BEFORE : S.viewMode; }
  // Only after-only never shows the before frame, so this is also the
  // switch for "is it worth asking the server for one" (see doRender).
  function needsBefore(mode) { return mode !== MODE_AFTER; }

  function deepEqual(a, b) {
    if (a === b) return true;
    if (typeof a !== typeof b) return false;
    if (a === null || b === null) return a === b;
    if (Array.isArray(a) !== Array.isArray(b)) return false;
    if (typeof a === "number" && typeof b === "number") return Math.abs(a - b) < 1e-9;
    if (typeof a !== "object") return a === b;
    var ka = Object.keys(a), kb = Object.keys(b);
    if (ka.length !== kb.length) return false;
    for (var i = 0; i < ka.length; i++) {
      if (!(ka[i] in b)) return false;
      if (!deepEqual(a[ka[i]], b[ka[i]])) return false;
    }
    return true;
  }

  /* ---- toast ----------------------------------------------------------- */

  var toastTimer = null;
  function toast(msg, bad) {
    var t = $("toast");
    t.textContent = msg;
    t.className = "on" + (bad ? " bad" : "");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { t.className = ""; }, bad ? 6000 : 2600);
  }

  /* ---- http ------------------------------------------------------------ */

  function api(path, opts) {
    return fetch(path, opts).then(function (r) {
      if (!r.ok) {
        return r.json().catch(function () { return { error: r.statusText }; })
          .then(function (j) { throw new Error(j.error || r.statusText); });
      }
      return r.json();
    });
  }

  function postJSON(path, body, extraHeaders) {
    var headers = { "Content-Type": "application/json" };
    for (var k in (extraHeaders || {})) headers[k] = extraHeaders[k];
    return fetch(path, { method: "POST", headers: headers, body: JSON.stringify(body) });
  }

  function frameRequest(channel, payload) {
    GEN[channel] = (GEN[channel] || 0) + 1;
    return postJSON("/api/frame", payload, {
      "X-Studio-Client": CLIENT + ":" + channel,
      "X-Studio-Gen": String(GEN[channel])
    });
  }

  function showBlob(channel, imgEl, response) {
    if (response.status === 409) return Promise.resolve(false);
    if (!response.ok) {
      return response.json().catch(function () { return {}; }).then(function (j) {
        throw new Error(j.error || ("frame failed: " + response.status));
      });
    }
    return response.blob().then(function (b) {
      var url = URL.createObjectURL(b);
      if (S.blobs[channel]) URL.revokeObjectURL(S.blobs[channel]);
      S.blobs[channel] = url;
      imgEl.src = url;
      return true;
    });
  }

  /* ---- config edits ---------------------------------------------------- */

  function snapshot() {
    return JSON.stringify({ A: S.slots.A, B: S.slots.B, active: S.active });
  }

  var lastCommitted = null;
  function pushHistory() {
    var snap = snapshot();
    if (snap === lastCommitted) return;
    S.history.push(lastCommitted === null ? snap : lastCommitted);
    lastCommitted = snap;
    if (S.history.length > 150) S.history.shift();
    S.future.length = 0;
    updateUndoButtons();
    // Every committed edit (a slider release, reset all, a pasted JSON
    // config) is a real change to the live grade, so this is the one place
    // that covers all of them for session.js: an outside agent polling
    // /api/session sees the same config the page just settled on. Session
    // publishing is a no-op with nothing loaded (see session.js), so this
    // costs nothing when no outside agent is attached.
    if (window.StudioSession) window.StudioSession.publish(cfg(), S.clip, S.time);
  }

  function restore(snap) {
    var o = JSON.parse(snap);
    S.slots.A = o.A; S.slots.B = o.B; S.active = o.active;
    lastCommitted = snap;
    syncSlotButtons();
    Panels.refresh(cfg(), S.defaults);
    updateModified();
    scheduleRender();
    updateUndoButtons();
  }

  function undo() {
    if (!S.history.length) return;
    S.future.push(snapshot());
    restore(S.history.pop());
  }
  function redo() {
    if (!S.future.length) return;
    S.history.push(snapshot());
    restore(S.future.pop());
  }
  function updateUndoButtons() {
    $("undoBtn").disabled = !S.history.length;
    $("redoBtn").disabled = !S.future.length;
  }

  function onParamChange(path, value, commit) {
    Panels.setPath(cfg(), path, value);
    updateModified();
    markStageState();
    scheduleRender();
    if (commit) {
      pushHistory();
      Panels.refresh(cfg(), S.defaults);
    }
  }

  function markStageState() {
    SCHEMA.forEach(function (stage) {
      if (!stage.enable) return;
      var box = document.querySelector('#params [data-stage="' + stage.id + '"]');
      if (box) box.classList.toggle("off", !Panels.getPath(cfg(), stage.enable));
    });
  }

  function updateModified() {
    var dirty = S.presetCfg ? !deepEqual(cfg(), S.presetCfg) : true;
    $("modifiedDot").classList.toggle("on", dirty);
    refreshRenderName(dirty);
  }

  // job 4: a render's filename is the only record of what is actually in it
  // once the job finishes, so it has to describe the live config, not just
  // whichever preset happened to load it last. sanitizeForFilename mirrors
  // the server's own allow-list (letters, digits, dot, dash, underscore) so
  // an odd preset name (spaces, punctuation, a save-as typed free-form)
  // cannot produce a name the server would reject.
  function sanitizeForFilename(s) {
    return String(s || "").replace(/[^A-Za-z0-9.\-_]/g, "-");
  }

  function autoRenderName(dirty) {
    var base = sanitizeForFilename(S.presetName || "custom");
    return base + (dirty ? "_mod" : "") + "_" + S.renderStamp;
  }

  function refreshRenderName(dirty) {
    if (!S.renderNameAuto) return;
    if (dirty === undefined) dirty = S.presetCfg ? !deepEqual(cfg(), S.presetCfg) : true;
    $("renderName").value = autoRenderName(dirty);
  }

  /* ---- rendering ------------------------------------------------------- */

  var renderTimer = null;
  var inflight = 0;

  function scheduleRender(delay) {
    // Every codepath that changes the config or the playhead (a slider
    // drag, a preset load, undo/redo, an outside session patch, setTime)
    // already funnels through here before asking the server for a fresh
    // still, which is what makes this the one place that has to catch
    // "a grade parameter changed while playing" -- there is no second path
    // that edits cfg() or S.time without also calling this. A stale stream
    // is stopped rather than left running, because a preview that keeps
    // showing the OLD grade after a knob moved is a preview that lies.
    if (S.playing || S.playPreparing) {
      stopPlayback("grade changed, playback stopped - press play again");
    }
    // The live loop (job: live GPU viewer, mode 2) is the one exception to
    // the rule in the comment above: it is not a second path that edits
    // cfg() without calling this, it is the path that makes a config edit
    // during playback NOT stop anything. The loop's own rAF tick already
    // reads cfg() fresh on every frame (see startLoop in live.js), so the
    // still-frame fetch this function would otherwise schedule is not just
    // unneeded here, it would be wrong: it targets frameImg, which is
    // hidden and not what is on screen while #stage.gpu-live is showing
    // gpuCanvas instead.
    if (S.looping) return;
    clearTimeout(renderTimer);
    renderTimer = setTimeout(doRender, delay === undefined ? 110 : delay);
  }

  function basePayload(extra) {
    var p = {
      clip: S.clip, time: S.time, width: S.width,
      autorotate: S.autorotate, config: cfg()
    };
    for (var k in (extra || {})) p[k] = extra[k];
    return p;
  }

  function doRender() {
    if (!S.clip) return;
    if (S.sheet) { renderSheet(); return; }
    var t0 = performance.now();
    var mode = S.mask ? "mask" : "graded";

    // GPU preview (job: live GPU viewer, mode 1). Only the graded picture
    // itself goes through gpu.js; mask mode is a diagnostic view of the
    // secondary qualifier, not the grade the user is judging, and stays on
    // the server path unconditionally. Falls back to doRenderServer on ANY
    // rejection (an unsupported stage, a decode error, no GPU at all): the
    // server path is the one that matches the final render exactly, so a
    // GPU failure must never leave the viewer showing nothing.
    if (mode === "graded" && S.gpu && StudioLive.available()) {
      inflight++;
      StudioLive.renderStill({
        clip: S.clip, time: S.time, width: S.width, autorotate: S.autorotate,
        config: cfg(), sourceWidth: clipSourceWidth()
      }).then(function (r) {
        setStageLayer("gpu");
        $("renderTime").textContent = Math.round(r.ms) + " ms (GPU, "
          + Math.round(performance.now() - t0) + " ms incl. fetch)";
        $("viewerMsg").classList.remove("on");
        setRendererBadge("GPU", r.grain);
        fitViewer();
        if (S.scopesAuto) { refreshStats(); refreshScopes(); }
      }).catch(function (e) {
        setStageLayer("still");
        setRendererBadge("server (GPU fallback: " + (e.message || e) + ")", false);
        doRenderServer(mode, t0);
      }).finally(function () { inflight--; });
    } else {
      setStageLayer("still");
      if (mode === "graded") setRendererBadge("server", false);
      doRenderServer(mode, t0);
    }

    // Only after-only can go without the before frame; every other mode has
    // it on screen somewhere, so this is the one place that decides whether
    // that second request is worth paying for.
    if (needsBefore(effectiveMode())) renderBefore();
  }

  function doRenderServer(mode, t0) {
    inflight++;
    $("renderTime").textContent = "rendering...";
    frameRequest("main", basePayload({ mode: mode }))
      .then(function (r) { return showBlob("main", $("frameImg"), r); })
      .then(function (ok) {
        if (ok) {
          $("renderTime").textContent = Math.round(performance.now() - t0) + " ms";
          $("viewerMsg").classList.remove("on");
          if (S.scopesAuto) { refreshStats(); refreshScopes(); }
        }
      })
      .catch(function (e) { showError(e); })
      .finally(function () { inflight--; });
  }

  function renderBefore() {
    frameRequest("before", basePayload({
      mode: "flat", keep_exposure: $("beforeExposure").checked
    })).then(function (r) { return showBlob("before", $("beforeImg"), r); })
      .catch(function (e) { showError(e); });
  }

  function showError(e) {
    var m = $("viewerMsg");
    m.textContent = String(e.message || e);
    m.classList.add("on");
    $("renderTime").textContent = "error";
  }

  function refreshStats() {
    fetch("/api/stats", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(basePayload({ width: 640 }))
    }).then(function (r) { return r.json(); })
      .then(function (j) { if (j.stats) drawStats(j.stats, j.size); })
      .catch(function () { /* stats are a nicety, never block the viewer */ });
  }

  function refreshScopes() {
    var host = $("scopes");
    var kinds = Object.keys(S.scopes).filter(function (k) { return S.scopes[k]; });
    Array.prototype.slice.call(host.children).forEach(function (c) {
      if (kinds.indexOf(c.dataset.kind) < 0) c.remove();
    });
    kinds.forEach(function (kind) {
      var box = host.querySelector('[data-kind="' + kind + '"]');
      if (!box) {
        box = document.createElement("div");
        box.className = "scopebox";
        box.dataset.kind = kind;
        var img = document.createElement("img");
        var lab = document.createElement("b");
        lab.textContent = kind;
        box.appendChild(img); box.appendChild(lab);
        host.appendChild(box);
      }
      var size = kind === "vectorscope" ? 210 : 300;
      GEN["scope_" + kind] = (GEN["scope_" + kind] || 0) + 1;
      postJSON("/api/scope", basePayload({ width: 640, kind: kind, size: size }), {
        "X-Studio-Client": CLIENT + ":scope_" + kind,
        "X-Studio-Gen": String(GEN["scope_" + kind])
      }).then(function (r) {
        return showBlob("scope_" + kind, box.querySelector("img"), r);
      }).catch(function () { /* leave the last good scope on screen */ });
    });
  }

  /* ---- statistics readout ---------------------------------------------- */

  function statRow(label, value, cls) {
    var d = document.createElement("div");
    d.className = "statrow" + (cls ? " " + cls : "");
    var e = document.createElement("em"); e.textContent = label;
    var s = document.createElement("span"); s.textContent = value;
    d.appendChild(e); d.appendChild(s);
    return d;
  }

  function group(title, rows) {
    var g = document.createElement("div");
    g.className = "statgroup";
    var b = document.createElement("b"); b.textContent = title;
    g.appendChild(b);
    rows.forEach(function (r) { g.appendChild(r); });
    return g;
  }

  function drawStats(st, size) {
    var host = $("stats");
    host.innerHTML = "";
    var L = st.luma;
    var midCls = (L.mean8 < 95 || L.mean8 > 140) ? "hot" : "";

    host.appendChild(group("luma percentiles", [
      statRow("p5", L.p5.toFixed(4)),
      statRow("p25", L.p25.toFixed(4)),
      statRow("p50 median", L.p50.toFixed(4)),
      statRow("p75", L.p75.toFixed(4)),
      statRow("p95", L.p95.toFixed(4)),
      statRow("mean", L.mean.toFixed(4)),
      statRow("mean 8-bit", L.mean8.toFixed(1), midCls)
    ]));

    var cb = st.clipped.black, cw = st.clipped.white;
    host.appendChild(group("clipping", [
      statRow("black %", cb.toFixed(3), cb > 2 ? "bad" : (cb > 0.5 ? "hot" : "")),
      statRow("white %", cw.toFixed(3), cw > 2 ? "bad" : (cw > 0.5 ? "hot" : "")),
      statRow("luma min", L.min.toFixed(4)),
      statRow("luma max", L.max.toFixed(4))
    ]));

    host.appendChild(group("saturation", [
      statRow("mean (all px)", st.saturation.mean.toFixed(4)),
      statRow("mean (coloured)", st.saturation.mean_coloured.toFixed(4)),
      statRow("p95", st.saturation.p95.toFixed(4))
    ]));

    var f = st.families;
    host.appendChild(group("colour families %", [
      statRow("warm", f.warm.toFixed(2)),
      statRow("green", f.green.toFixed(2)),
      statRow("cool", f.cool.toFixed(2)),
      statRow("magenta", f.magenta.toFixed(2)),
      statRow("neutral", f.neutral.toFixed(2))
    ]));

    host.appendChild(group("channel means", [
      statRow("R", st.channels.r.toFixed(4)),
      statRow("G", st.channels.g.toFixed(4)),
      statRow("B", st.channels.b.toFixed(4))
    ]));

    var d = st.definitions;
    $("statsNote").textContent =
      "measured on the graded frame at " + size[0] + "x" + size[1] + ", 8-bit. "
      + "coloured means saturation >= " + d.sat_floor
      + "; warm " + d.families.warm[0] + " to " + d.families.warm[1] + " deg, "
      + "green " + d.families.green[0] + " to " + d.families.green[1] + ", "
      + "cool " + d.families.cool[0] + " to " + d.families.cool[1] + "; "
      + "clipped black <= " + d.clip_black_code + "/255, white >= "
      + d.clip_white_code + "/255. Normal exposure is mean 8-bit 95 to 140.";
  }

  /* ---- contact sheet --------------------------------------------------- */

  function sheetTimes() {
    if (S.marks.length >= 2) return S.marks.slice(0, 4);
    var d = S.duration || 1;
    return [0.1, 0.35, 0.6, 0.85].map(function (f) { return +(d * f).toFixed(2); });
  }

  function renderSheet() {
    var host = $("sheet");
    var times = sheetTimes();
    host.innerHTML = "";
    times.forEach(function (t, i) {
      var cell = document.createElement("div");
      cell.className = "sheetcell";
      var img = document.createElement("img");
      var tag = document.createElement("b");
      tag.textContent = t.toFixed(2) + "s";
      cell.appendChild(img); cell.appendChild(tag);
      cell.addEventListener("click", function () { setTime(t); });
      host.appendChild(cell);
      frameRequest("sheet" + i, {
        clip: S.clip, time: t, width: 700, autorotate: S.autorotate, config: cfg()
      }).then(function (r) { return showBlob("sheet" + i, img, r); })
        .catch(function (e) { showError(e); });
    });
    $("renderTime").textContent = times.length + " frames";
  }

  /* ---- timeline -------------------------------------------------------- */

  function setTime(t) {
    // Every direct "jump to this time" interaction (scrub, step, playHead,
    // a thumbnail, a mark, selecting a clip) funnels through here, same as
    // scheduleRender is the one place that catches a config change during
    // Play. The live loop owns S.time while it runs (see its onFrame
    // callback below) and would just overwrite whatever this function sets
    // on its very next animation frame, so an explicit jump has to stop it
    // first or the scrub would silently snap back a few milliseconds later.
    if (S.looping || S.loopPreparing) stopLoopInternal();
    S.time = Math.max(0, Math.min(t, Math.max(0, S.duration - 1 / S.fps)));
    $("timeLabel").textContent = S.time.toFixed(2) + "s";
    $("scrub").value = String(Math.round(S.duration ? (S.time / S.duration) * 1000 : 0));
    highlightThumb();
    scheduleRender();
  }

  function highlightThumb() {
    var imgs = $("thumbs").querySelectorAll("img");
    var best = -1, bd = 1e9;
    imgs.forEach(function (im, i) {
      var d = Math.abs(parseFloat(im.dataset.t) - S.time);
      if (d < bd) { bd = d; best = i; }
    });
    imgs.forEach(function (im, i) { im.classList.toggle("active", i === best); });
  }

  function buildThumbs() {
    var host = $("thumbs");
    host.innerHTML = "";
    if (!S.clip || !S.duration) return;
    var n = 16;
    for (var i = 0; i < n; i++) {
      var t = (S.duration * i) / n;
      var im = document.createElement("img");
      im.dataset.t = String(t);
      im.title = t.toFixed(2) + "s";
      im.src = "/api/thumb?clip=" + encodeURIComponent(S.clip)
        + "&t=" + t.toFixed(3) + "&w=94&rot=" + (S.autorotate ? "1" : "0");
      im.addEventListener("click", function (ev) {
        setTime(parseFloat(ev.target.dataset.t));
      });
      host.appendChild(im);
    }
    highlightThumb();
  }

  function drawMarks() {
    var host = $("marks");
    host.innerHTML = "";
    S.marks.forEach(function (t, i) {
      var chip = document.createElement("span");
      chip.className = "markchip" + (Math.abs(t - S.time) < 0.01 ? " active" : "");
      chip.textContent = t.toFixed(2) + "s";
      var kill = document.createElement("span");
      kill.className = "kill"; kill.textContent = "x";
      kill.addEventListener("click", function (ev) {
        ev.stopPropagation();
        S.marks.splice(i, 1);
        drawMarks();
        if (S.sheet) renderSheet();
      });
      chip.appendChild(kill);
      chip.addEventListener("click", function () { setTime(t); });
      host.appendChild(chip);
    });
    if (!S.marks.length) {
      var hint = document.createElement("span");
      hint.className = "muted small";
      hint.textContent = "no marks. Mark (M) stores a frame; the contact sheet "
        + "checks one grade against up to four of them at once.";
      host.appendChild(hint);
    }
  }

  /* ---- viewer ---------------------------------------------------------- */

  var STAGE_MODE_CLASSES = ["mode-after", "mode-before", "mode-lr", "mode-tb", "mode-wipe"];

  // The one place that writes #stage.playing / #stage.gpu-live. Both used to
  // be toggled directly wherever they were relevant (doRender, startLoop,
  // stopPlayback...) which is how a plain GPU still render and a later press
  // of Play ended up with BOTH classes on #stage at once: nothing had ever
  // cleared the still render's gpu-live when Play started, since that was a
  // different codepath entirely. Two classes present together is not a
  // state the CSS was written to handle (#stage.gpu-live #gpuCanvas and
  // #stage.playing #playerVideo both say display: block, and both fire),
  // so the canvas and the video painted on top of each other.
  // Routing every caller through here instead makes the invalid combination
  // unable to occur: "want" names the layer the caller is asking for, but
  // playback always wins over it, because a user who pressed Play wants to
  // watch the video, not have a still GPU render steal the frame back under
  // them. "still" (the server-rendered #frameImg) is the implicit third
  // state and needs no class of its own: it is just what shows once neither
  // "playing" nor "gpu-live" is present.
  function setStageLayer(want) {
    var layer = S.playing ? "video" : want;
    var stage = $("stage");
    stage.classList.toggle("playing", layer === "video");
    stage.classList.toggle("gpu-live", layer === "gpu");
  }

  function applyViewerState() {
    var stage = $("stage");
    var mode = effectiveMode();
    STAGE_MODE_CLASSES.forEach(function (c) { stage.classList.remove(c); });
    stage.classList.add("mode-" + mode);
    // The selector always reflects the latched mode, never a transient
    // space-bar hold, so releasing space never looks like it silently
    // changed what you had chosen. The mode label next to it does track the
    // hold, on purpose, because it is describing what is on screen right now.
    $("viewMode").value = S.viewMode;
    $("modeLabel").textContent = MODE_LABELS[mode];
    $("maskBtn").classList.toggle("active", S.mask);
    $("sheetBtn").classList.toggle("active", S.sheet);
    $("viewport").classList.toggle("sheet", S.sheet);
    $("viewport").classList.toggle("withref", S.refShow && !!S.refName);
    fitViewer();
  }

  function setViewMode(mode) {
    // Playback only ever shows in #afterLayer (see #stage.playing in
    // style.css); every other mode either hides that layer outright
    // (before-only) or splits the viewer in a way a single played video
    // was never built to share, so switching modes stops it rather than
    // leaving a video playing invisibly behind a mode change.
    if (S.playing || S.playPreparing) stopPlayback();
    // Same reasoning, same layer, for the GPU loop (#stage.gpu-live).
    if (S.looping || S.loopPreparing) stopLoop();
    S.viewMode = mode;
    applyViewerState();
    if (needsBefore(effectiveMode())) renderBefore();
  }

  // \\ only ever flips between the two solo modes. From any split mode this
  // lands on before-only, same as Lightroom's \\ does regardless of what its
  // Y state was.
  function toggleBeforeAfter() {
    setViewMode(S.viewMode === MODE_BEFORE ? MODE_AFTER : MODE_BEFORE);
  }
  function cycleSplitModes() {
    var i = SPLIT_CYCLE.indexOf(S.viewMode);
    setViewMode(SPLIT_CYCLE[(i + 1) % SPLIT_CYCLE.length]);
  }

  function sizeLayer(img, wBox, hBox) {
    if (S.zoom !== "fit") {
      img.style.maxWidth = "none";
      img.style.maxHeight = "none";
    } else {
      img.style.maxWidth = wBox + "px";
      img.style.maxHeight = hBox + "px";
    }
    // A leftover pixel size from matchBeforeSize's wipe-mode case would
    // otherwise win over these max- properties the next time this image is
    // the one on a normal (non-wipe) layout.
    img.style.width = "";
    img.style.height = "";
  }

  function fitViewer() {
    var vp = $("viewport");
    var mode = effectiveMode();
    var box = vp.getBoundingClientRect();
    var refW = (S.refShow && S.refName) ? $("refPane").offsetWidth + 8 : 0;
    var availW = Math.max(80, box.width - refW - 4);
    var availH = Math.max(80, box.height - 4);

    // Left/Right and Top/Bottom each show a COMPLETE copy of the frame, not
    // a clip, so each one only gets to claim half the viewer's box before it
    // is handed to the image; otherwise a fit-mode pair would be sized as if
    // each half owned the whole viewer and together spill past its edge.
    var wBox = availW, hBox = availH;
    if (mode === MODE_LR) wBox = (availW - 1) / 2;   // 1px for the divider border
    if (mode === MODE_TB) hBox = (availH - 1) / 2;

    sizeLayer($("frameImg"), wBox, hBox);
    sizeLayer($("beforeImg"), wBox, hBox);
    // playerVideo only ever shows in after-only (setViewMode stops playback
    // before leaving it), so wBox/hBox is always the full box for it in
    // practice. gpuCanvas is not the same: doRender does not check view
    // mode before trying the GPU path, so a GPU still can be the picture in
    // the "after" half of Left/Right, Top/Bottom or Wipe too, and there it
    // does need the LR/TB half-share computed above, same as frameImg gets.
    // Sizing both here on every call is harmless either way, since
    // setStageLayer is what decides whether either one is actually visible.
    sizeLayer($("playerVideo"), wBox, hBox);
    sizeLayer($("gpuCanvas"), wBox, hBox);

    var beforeLayer = $("beforeLayer");
    if (mode !== MODE_WIPE) {
      // The wipe handle leaves an inline percentage width on this wrapper so
      // the split position sticks around if you flip back to wipe later
      // (matching the original behaviour). Left/Right and Top/Bottom lay
      // this wrapper out as a normal flex item, and an inline width would
      // set its flex-basis and fight that, so it has to be cleared in every
      // mode except the one that actually uses it.
      beforeLayer.style.width = "";
    }

    matchBeforeSize(mode);
  }

  function matchBeforeSize(mode) {
    // Only the wipe overlay needs this: beforeImg sits inside a
    // width-clipped, absolutely positioned wrapper, so percentage sizing
    // cannot reach the true frame size and it has to be told in pixels.
    // Left/Right and Top/Bottom already size both copies independently in
    // sizeLayer() above, from the same source dimensions, so forcing a copy
    // here would fight that instead of matching it.
    if (mode !== MODE_WIPE) return;
    var main = $("frameImg"), before = $("beforeImg");
    if (!main.clientWidth) return;
    before.style.width = main.clientWidth + "px";
    before.style.height = main.clientHeight + "px";
  }

  function setZoom(mode) {
    S.zoom = mode;
    $("viewport").classList.toggle("zoom", mode !== "fit");
    $("zoomLabel").textContent = mode === "fit"
      ? "fit" : ("100% of the " + S.width + "px preview");
    fitViewer();
  }

  /* ---- presets --------------------------------------------------------- */

  function fillPresets(list) {
    var sel = $("presetSelect");
    var keep = sel.value;
    sel.innerHTML = "";
    list.forEach(function (p) {
      var o = document.createElement("option");
      o.value = p.name;
      o.textContent = p.name + (p.look ? "  [" + p.look + "]" : "");
      o.title = p.comment || "";
      sel.appendChild(o);
    });
    if (keep && list.some(function (p) { return p.name === keep; })) sel.value = keep;
  }

  function loadPreset(name) {
    return api("/api/preset?name=" + encodeURIComponent(name)).then(function (j) {
      S.slots[S.active] = j.config;
      S.presetName = name;
      S.presetCfg = clone(j.config);
      lastCommitted = snapshot();
      S.history.length = 0; S.future.length = 0;
      updateUndoButtons();
      Panels.refresh(cfg(), S.defaults);
      markStageState();
      // loadPreset resets lastCommitted directly instead of going through
      // pushHistory (loading a preset is not itself an undoable edit), so it
      // needs its own publish: an outside agent watching the session should
      // see a preset load too, not just the edits made on top of it.
      if (window.StudioSession) window.StudioSession.publish(cfg(), S.clip, S.time);
      // A fresh preset load is the one point where overwriting the render
      // name field is always correct (it is, by definition, unmodified at
      // this instant) and where a hand-typed name stops being protected:
      // you just asked to load different grade, so a name describing the
      // previous one would be actively wrong.
      S.renderNameAuto = true;
      S.renderStamp = Math.floor(Date.now() / 1000 % 100000);
      updateModified();
      scheduleRender(0);
      toast("loaded preset " + name);
    });
  }

  // The other half of the session.js contract: an outside agent (the
  // cinegrade CLI) patched the live config on the server, and this is how
  // that reaches the open page. Treated exactly like loadPreset above minus
  // the network fetch: same slot assignment, same pushHistory so undo covers
  // an outside edit like a local one, same panel refresh and re-render.
  // session.js itself guards against publishing this straight back out as a
  // fresh local change (see its `applying` flag), so no loop guard is needed
  // here. Kept to the small version the direct request asked for: no
  // dedicated attribution UI, just a toast naming who made the change.
  window.applyExternalConfig = function (config, state) {
    S.slots[S.active] = config;
    pushHistory();
    Panels.refresh(cfg(), S.defaults);
    markStageState(); updateModified(); scheduleRender(0);
    toast("config updated externally" + (state && state.by ? " (" + state.by + ")" : ""));
  };

  function savePreset(name, comment) {
    return api("/api/preset", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name, config: cfg(), comment: comment || "" })
    }).then(function (j) {
      fillPresets(j.presets);
      $("presetSelect").value = name;
      S.presetName = name;
      S.presetCfg = clone(cfg());
      updateModified();
      toast("saved " + j.saved);
    });
  }

  /* ---- looks ----------------------------------------------------------- */

  function fillLooks(list) {
    Panels.setLooks(list.map(function (l) { return l.name; }));
    Panels.refresh(cfg(), S.defaults);
    var host = $("lutList");
    host.innerHTML = "";
    list.forEach(function (l) {
      var row = document.createElement("div");
      row.className = "lutrow";
      if (cfg() && cfg().look.lut === l.name) row.classList.add("active");
      var n = document.createElement("span"); n.textContent = l.name;
      var sp = document.createElement("span"); sp.className = "spacer";
      var x = document.createElement("span");
      x.className = "x";
      x.textContent = (l.size || "?") + "^3" + (l.generated ? "" : " ext");
      row.appendChild(n); row.appendChild(sp); row.appendChild(x);
      row.title = (l.generated
        ? "generated by tools/make_looks.py"
        : "imported, not generated here") + "  " + Math.round(l.bytes / 1024) + " kB";
      row.addEventListener("click", function () {
        onParamChange(["look", "lut"], l.name, true);
        fillLooks(list);
      });
      host.appendChild(row);
    });
  }

  function fillRenders(list) {
    var host = $("renderList");
    host.innerHTML = "";
    list.slice(0, 20).forEach(function (r) {
      var row = document.createElement("div");
      row.className = "lutrow";
      var n = document.createElement("span"); n.textContent = r.name;
      var sp = document.createElement("span"); sp.className = "spacer";
      var x = document.createElement("span");
      x.className = "x"; x.textContent = (r.bytes / 1e6).toFixed(0) + " MB";
      row.appendChild(n); row.appendChild(sp); row.appendChild(x);
      row.title = "click to reveal in Finder";
      row.addEventListener("click", function () {
        postJSON("/api/reveal", { path: r.path });
      });
      host.appendChild(row);
    });
  }

  /* ---- refs ------------------------------------------------------------ */

  function fillRefs(list) {
    var host = $("refList");
    host.innerHTML = "";
    list.forEach(function (r) {
      var im = document.createElement("img");
      im.src = "/api/ref?name=" + encodeURIComponent(r.name) + "&w=200";
      im.title = r.name;
      im.addEventListener("click", function () {
        S.refName = r.name;
        S.refShow = true;
        $("refShow").checked = true;
        $("refImg").src = "/api/ref?name=" + encodeURIComponent(r.name) + "&w=1100";
        // A box drawn on the old picture is meaningless on the new one, and
        // silently reusing its fractions would match the wrong area without
        // any sign anything changed.
        setRefCropFrac(null);
        host.querySelectorAll("img").forEach(function (o) { o.classList.remove("active"); });
        im.classList.add("active");
        applyViewerState();
      });
      host.appendChild(im);
    });
  }

  /* ---- ref crop box ------------------------------------------------------
     Lets the user drag a box on the reference to say which part of it
     "Match to reference" should measure, instead of always falling back to
     the auto content detector. S.refCropFrac holds it as [x, y, w, h]
     fractions of the reference's own size (0 to 1); that is resize safe on
     its own, since a fraction of the image means the same thing whatever
     size the pane is currently drawn at, and is exactly the shape
     match_reference's crop_frac argument wants server side.

     drawRefCropBox always measures refImg's OWN rendered rect, not
     refImgWrap's: refImgWrap centres the img with max-height/max-width:100%
     (style.css), so a reference whose aspect ratio does not match the wrap
     is letterboxed on one axis, and a box positioned against the wrap's box
     would drift off the picture on that axis. */

  function drawRefCropBox() {
    var box = $("refCropBox");
    var f = S.refCropFrac;
    if (!f) { box.classList.remove("on"); return; }
    var ir = $("refImg").getBoundingClientRect();
    var wr = $("refImgWrap").getBoundingClientRect();
    if (!ir.width || !ir.height) { box.classList.remove("on"); return; }
    box.style.left = (ir.left - wr.left + f[0] * ir.width) + "px";
    box.style.top = (ir.top - wr.top + f[1] * ir.height) + "px";
    box.style.width = (f[2] * ir.width) + "px";
    box.style.height = (f[3] * ir.height) + "px";
    box.classList.add("on");
  }

  function setRefCropFrac(frac) {
    S.refCropFrac = frac;
    $("clearRefCropBtn").disabled = !frac;
    drawRefCropBox();
  }

  // Drag start and end both go through this: given a mouse event and the
  // reference image's current rendered rect, it is the [x, y] fraction of
  // the image under the cursor, clamped so a drag that runs past the image
  // edge (or off the whole window) still ends exactly at that edge instead
  // of producing an out of range fraction.
  function refPointFrac(ev, r) {
    return [
      Math.max(0, Math.min(1, (ev.clientX - r.left) / r.width)),
      Math.max(0, Math.min(1, (ev.clientY - r.top) / r.height))
    ];
  }

  function bindRefCrop() {
    $("refImgWrap").addEventListener("mousedown", function (ev) {
      if (!S.refName) return;
      var r = $("refImg").getBoundingClientRect();
      if (!r.width || !r.height) return;
      ev.preventDefault();
      var start = refPointFrac(ev, r);
      function move(e) {
        var p = refPointFrac(e, r);
        var x0 = Math.min(start[0], p[0]), x1 = Math.max(start[0], p[0]);
        var y0 = Math.min(start[1], p[1]), y1 = Math.max(start[1], p[1]);
        setRefCropFrac([x0, y0, x1 - x0, y1 - y0]);
      }
      function up() {
        document.removeEventListener("mousemove", move);
        document.removeEventListener("mouseup", up);
      }
      // A plain click with no mousemove between down and up never calls
      // move(), so S.refCropFrac (and whatever box was already drawn) is
      // left exactly as it was: a click is not a zero size box.
      document.addEventListener("mousemove", move);
      document.addEventListener("mouseup", up);
    });
    $("clearRefCropBtn").addEventListener("click", function () { setRefCropFrac(null); });
  }

  /* ---- match reference ---------------------------------------------------
     Fits a transform that moves this frame's colour statistics toward the
     currently shown reference image; it does not extract or copy a LUT (a
     JPEG or PNG has none in it to extract). Full contract, every argument
     and failure mode: grade/tools/MATCH-REF-INTEGRATION.md.

     Strength is deliberately NOT a control here: the cube POST /api/match
     writes is baked at a fixed strength, and look.mix (already a slider in
     Parameters, schema.js) is the same axis and needs no network round trip
     to move. Re-fitting on every strength change would be a 1.4 to 3 second
     wait per drag step for no reason, so this only ever calls the endpoint
     once per click and leaves strength to the existing mix slider. */

  var matchBusy = false;

  function matchReference() {
    if (matchBusy || !S.refName || !S.clip) return;
    matchBusy = true;
    $("matchRefBtn").disabled = true;
    $("matchRefStatus").textContent = "matching (1 to 3 seconds)...";
    $("matchRefResult").innerHTML = "";

    var body = {
      ref: S.refName,
      clip: S.clip,
      time: S.time,
      autorotate: S.autorotate,
      config: cfg(),
      method: "reinhard",
      luma_preserve: true
    };
    // Only sent when the user drew a box: server.py turns auto_crop off the
    // moment crop_frac is present, so a box always wins over the auto
    // content detector rather than the two stacking or racing.
    if (S.refCropFrac) body.crop_frac = S.refCropFrac.join(",");

    api("/api/match", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    }).then(function (result) {
      renderMatchResult(result);
      // Only a healthy result ever touches the live grade: result.ok false
      // is the endpoint's own "do not apply this" verdict (a probe hit a
      // hard 0/1, or the neutral ramp is not monotonic), and applying it
      // anyway would be silently grading through a LUT its own health check
      // rejected.
      if (result.ok) onParamChange(["look", "lut"], result.name, true);
      // The response carries the refreshed LUT list already (server side
      // note: "so you can replace your list without a second request"), so
      // this reuses fillLooks exactly as a second /api/looks fetch would,
      // just without the round trip.
      if (result.looks) fillLooks(result.looks);
    }).catch(function (err) {
      var p = document.createElement("div");
      p.className = "matchwarn";
      // A SyntaxError here is fetch's own response.json() rejecting the
      // body, not the endpoint's {"error": "..."} path (that path is
      // already valid JSON by construction). This used to fire reliably: a
      // probe's hue_shift_deg on a near neutral (grey18) computed as Python
      // float("nan"), which json.dumps wrote as the bare token NaN, valid to
      // Python's own json module but not to the JSON spec. Fixed server side
      // (match_ref.py now reports an undefined hue shift as null, and
      // server.py dumps every response with allow_nan=False so a future
      // non-finite value becomes a named 500 instead of invalid JSON), so
      // this branch is a regression guard now rather than the expected
      // path: kept because it costs nothing and still turns "cryptic parser
      // text" into "this is a server bug, not a config problem" for
      // whatever unlikely cause trips it next.
      p.textContent = (err instanceof SyntaxError)
        ? "the server's response was not valid JSON, a backend bug outside this page: " + err.message
        : err.message;
      $("matchRefResult").appendChild(p);
    }).then(function () {
      matchBusy = false;
      $("matchRefBtn").disabled = false;
      $("matchRefStatus").textContent = "";
    });
  }

  function renderMatchResult(result) {
    var host = $("matchRefResult");
    host.innerHTML = "";

    var head = document.createElement("div");
    head.className = "matchhead " + (result.ok ? "ok" : "bad");
    head.textContent = result.ok ? "applied: " + result.name : "not applied: failed its own health check";
    host.appendChild(head);

    // The one warning called out by name as worth surfacing prominently:
    // above 0.35 the reference and this shot are different enough content
    // that the match is dragging the frame toward whatever the reference
    // happens to contain, not toward "this shot, colour corrected".
    if (typeof result.hue_divergence === "number" && result.hue_divergence > 0.35) {
      var hd = document.createElement("div");
      hd.className = "matchwarn";
      hd.textContent = "hue divergence " + result.hue_divergence.toFixed(2) +
        " (over 0.35): reference and shot look like different content, expect the match to pull toward the reference's own colours";
      host.appendChild(hd);
    }

    // Every other warning, verbatim: the direct request was explicit that
    // these are "the most useful part of the output" and must not be
    // summarised or swallowed.
    (result.warnings || []).forEach(function (w) {
      var wl = document.createElement("div");
      wl.className = "matchwarn";
      wl.textContent = w;
      host.appendChild(wl);
    });

    // Same shape as match_ref.py's own print_report, so the region a drawn
    // box produced (source "explicit-fraction") reads the same way the CLI
    // would report it, and a user who drew a box can see the fit actually
    // used it instead of falling back to the auto detector.
    if (result.crop) {
      var cr = result.crop;
      var c = document.createElement("div");
      c.className = "mono muted";
      c.textContent = "crop " + cr.x + "," + cr.y + " " + cr.w + "x" + cr.h +
        " = v" + cr.frac[1].toFixed(3) + "-" + cr.frac[3].toFixed(3) +
        " h" + cr.frac[0].toFixed(3) + "-" + cr.frac[2].toFixed(3) +
        " [" + cr.source + "]";
      host.appendChild(c);
    }

    if (result.distance && typeof result.distance.gain_pct === "number") {
      var d = document.createElement("div");
      d.className = "mono muted";
      d.textContent = result.distance.gain_pct.toFixed(1) + "% closer to the reference"
        + (typeof result.elapsed_s === "number" ? ", " + result.elapsed_s.toFixed(2) + "s" : "");
      host.appendChild(d);
    }
  }

  /* ---- jobs ------------------------------------------------------------ */

  var jobTimer = null;
  function pollJobs() {
    api("/api/jobs").then(function (j) {
      var host = $("jobList");
      host.innerHTML = "";
      j.jobs.forEach(function (job) {
        var row = document.createElement("div");
        row.className = "jobrow " + job.status;
        var top = document.createElement("div");
        top.className = "row";
        var nm = document.createElement("span");
        nm.textContent = job.label;
        var sp = document.createElement("span"); sp.className = "spacer";
        var st = document.createElement("span");
        st.className = "mono muted";
        st.textContent = job.status === "running"
          ? Math.round(job.progress * 100) + "%" : job.status;
        top.appendChild(nm); top.appendChild(sp); top.appendChild(st);
        if (job.status === "running") {
          var c = document.createElement("button");
          c.className = "btn danger"; c.textContent = "cancel";
          c.addEventListener("click", function () {
            postJSON("/api/job/cancel", { id: job.id }).then(pollJobs);
          });
          top.appendChild(c);
        }
        var bar = document.createElement("div");
        bar.className = "jobbar";
        var i = document.createElement("i");
        i.style.width = (job.progress * 100) + "%";
        bar.appendChild(i);
        var msg = document.createElement("div");
        msg.className = "muted small mono";
        msg.textContent = job.message || "";
        row.appendChild(top); row.appendChild(bar); row.appendChild(msg);
        host.appendChild(row);
      });
      fillRenders(j.renders);
      var running = j.jobs.some(function (x) { return x.status === "running"; });
      clearTimeout(jobTimer);
      jobTimer = setTimeout(pollJobs, running ? 900 : 5000);
    }).catch(function () {
      clearTimeout(jobTimer);
      jobTimer = setTimeout(pollJobs, 5000);
    });
  }

  /* ---- clip ------------------------------------------------------------ */

  function rotKey(name) { return "studio.rot." + name; }

  function selectClip(name) {
    S.clip = name;
    var entry = S.state.clips.filter(function (c) { return c.name === name; })[0];
    if (!entry) return;
    var stored = localStorage.getItem(rotKey(name));
    S.autorotate = stored === null ? true : stored === "1";
    S.duration = entry.duration || 0;
    S.fps = entry.fps || 24;
    $("rotBtn").classList.toggle("active", S.autorotate);
    $("rotBtn").textContent = S.autorotate ? "autorotate" : "no-autorotate";
    drawClipInfo(entry);
    // Redraws whichever folder is currently browsed so its row for this clip
    // (if it has one) picks up the .active highlight, mirroring what
    // fillClipList used to do for the old, unfiltered, footage-only list.
    // Guarded on browsePath because selectClip also runs at boot, before the
    // merged tab's first lazy load has populated S.browseDirs/browseFiles.
    if (S.browsePath) renderFolderClips();
    buildThumbs();
    setTime(Math.min(S.time, Math.max(0, S.duration - 0.1)));
    scheduleRender(0);
  }

  function drawClipInfo(entry) {
    var host = $("clipInfo");
    host.innerHTML = "";
    function kv(k, v) {
      var d = document.createElement("div");
      var a = document.createElement("em"); a.textContent = k; a.style.fontStyle = "normal";
      var b = document.createElement("span"); b.textContent = v;
      d.appendChild(a); d.appendChild(b);
      host.appendChild(d);
    }
    var cur = S.autorotate ? entry.autorotate : entry.raw;
    kv("file", entry.name);
    kv("graph sees", cur.width + " x " + cur.height);
    kv("autorotate", entry.autorotate.width + " x " + entry.autorotate.height);
    kv("no-autorotate", entry.raw.width + " x " + entry.raw.height);
    kv("rotation tag", entry.rotation + " deg");
    kv("duration", (entry.duration || 0).toFixed(2) + " s");
    kv("fps", (entry.fps || 0).toFixed(3));
    kv("codec", entry.codec + " " + (entry.profile || ""));
    kv("pix fmt", entry.pix_fmt);
    kv("range", entry.color_range);
    kv("matrix", entry.color_space);
    kv("size", (entry.bytes / 1e6).toFixed(0) + " MB");
    var n = document.createElement("div");
    n.className = "note warn";
    n.textContent = "Rotation metadata on this footage is not reliable. If the "
      + "frame is sideways, flip the autorotate button in the top bar and look "
      + "again. Getting it wrong also puts the blur and mask geometry on the "
      + "wrong axis, so check it before a long render.";
    host.appendChild(n);
  }

  /* ---- filesystem browser (merged Clips/Browse tab) ----------------------
     One tab now: navigate anywhere on disk up top, see the clips actually
     sitting in wherever that landed underneath, selecting one loads it into
     the viewer the same way the old standalone Clips tab did. The endpoints
     are GET /api/browse?path=<abs> -> {path, parent, dirs, files, roots,
     error} and POST /api/open {path} -> {clip, clips}; both live in
     server.py, which this file does not touch beyond that contract. */

  var BROWSE_PATH_KEY = "studio.browsepath.v1";
  var SVGNS = "http://www.w3.org/2000/svg";
  var XLINKNS = "http://www.w3.org/1999/xlink";

  // Small local twin of panels.js's useIcon: that one is scoped inside its
  // own closure and not exported, and duplicating six lines here is cheaper
  // than threading a shared export through a module that otherwise has no
  // reason to know this file exists.
  function browseIcon(name, cls) {
    var svg = document.createElementNS(SVGNS, "svg");
    svg.setAttribute("class", cls);
    svg.setAttribute("aria-hidden", "true");
    var use = document.createElementNS(SVGNS, "use");
    use.setAttribute("href", "#" + name);
    use.setAttributeNS(XLINKNS, "xlink:href", "#" + name); // older Safari
    svg.appendChild(use);
    return svg;
  }

  function emptyNote(text) {
    var el = document.createElement("div");
    el.className = "note";
    el.textContent = text;
    return el;
  }

  // Best-effort parent directory of an absolute path string. Every path this
  // file works with came back from server.py, which resolves everything
  // through pathlib on this same Mac, so a plain POSIX split is safe without
  // pulling in a path library for one string op.
  function dirName(p) {
    var trimmed = (p || "").replace(/\/+$/, "");
    var i = trimmed.lastIndexOf("/");
    return i > 0 ? trimmed.slice(0, i) : "/";
  }

  // Where the clip already on screen lives, so the merged tab can open there
  // instead of at the filesystem root (direct request: "it should start from
  // where u currently are not the computer root"). Null if nothing is loaded
  // yet, which only happens if content/footage itself is empty.
  function currentClipFolder() {
    var entry = S.state && S.state.clips
      && S.state.clips.filter(function (c) { return c.name === S.clip; })[0];
    return (entry && entry.path) ? dirName(entry.path) : null;
  }

  // Candidate starting folders in priority order: the folder the user was
  // last in (localStorage, so reloading the studio does not reset
  // navigation), then the loaded clip's folder, then null -- which
  // loadBrowseDir below passes straight through to /api/browse, and
  // server.py's browse_dir defaults an empty path to content/footage. Tried
  // one at a time by openInitialBrowseDir, so a saved folder that got
  // deleted or lost its permissions between sessions falls through quietly
  // to the next candidate instead of greeting the user with an error on a
  // fresh start.
  function initialBrowseCandidates() {
    var out = [];
    var saved = localStorage.getItem(BROWSE_PATH_KEY);
    if (saved) out.push(saved);
    var clipFolder = currentClipFolder();
    if (clipFolder) out.push(clipFolder);
    out.push(null);
    return out;
  }

  function openInitialBrowseDir() {
    var candidates = initialBrowseCandidates();
    (function tryNext() {
      var path = candidates.shift();
      api("/api/browse" + (path ? "?path=" + encodeURIComponent(path) : "")).then(function (j) {
        // An unreadable saved/derived folder still resolves (server.py
        // returns its own path with dirs/files empty and `error` set) rather
        // than rejecting, so this is the check that actually drives the
        // fallback chain on that path, not just network/400 failures below.
        if (j.error && candidates.length) { tryNext(); return; }
        applyBrowseResult(j);
      }).catch(function (err) {
        if (candidates.length) tryNext();
        else $("browseError").textContent = err.message || String(err);
      });
    })();
  }

  // Shared render step for both the initial (fallback-chain) load and every
  // ordinary navigation afterwards: cache the response, persist where we
  // landed, and redraw everything that depends on it.
  function applyBrowseResult(j) {
    S.browsePath = j.path;
    S.browseParent = j.parent || "";
    S.browseDirs = j.dirs || [];
    S.browseFiles = j.files || [];
    S.browseError = j.error || "";
    S.browseCursor = -1;
    localStorage.setItem(BROWSE_PATH_KEY, j.path);
    $("browseError").textContent = S.browseError;
    $("browseUpBtn").disabled = !S.browseParent;
    renderCrumbs(j.path);
    renderQuickJumps(j.roots);
    renderBrowseDirs();
    renderFolderClips();
  }

  // Ordinary navigation: clicking a folder row, a breadcrumb segment, the up
  // button, a quick jump, or Enter on a keyboard-selected folder. Errors
  // (a permission-protected folder is the real case) are shown, not
  // swallowed, unlike the initial-load fallback chain above.
  function loadBrowseDir(path) {
    api("/api/browse" + (path ? "?path=" + encodeURIComponent(path) : "")).then(function (j) {
      applyBrowseResult(j);
    }).catch(function (err) {
      $("browseError").textContent = err.message || String(err);
    });
  }

  // Every ancestor of the current folder as a clickable segment, built fresh
  // from the path string on each navigation. The first segment is always
  // "/", so jumping to root (and from there anywhere else reachable by
  // folder) never depends on how deep browsing happened to start.
  function renderCrumbs(path) {
    var host = $("browseCrumbs");
    host.innerHTML = "";
    var parts = (path || "/").split("/").filter(Boolean);
    function addCrumb(label, full, isLast) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "crumb" + (isLast ? " current" : "");
      b.textContent = label;
      b.title = full;
      if (isLast) {
        b.disabled = true;
      } else {
        b.addEventListener("click", function () { loadBrowseDir(full); });
      }
      host.appendChild(b);
      if (!isLast) {
        var sep = document.createElement("span");
        sep.className = "crumbsep";
        sep.textContent = "/";
        sep.setAttribute("aria-hidden", "true");
        host.appendChild(sep);
      }
    }
    addCrumb("/", "/", parts.length === 0);
    var acc = "";
    parts.forEach(function (part, i) {
      acc += "/" + part;
      addCrumb(part, acc, i === parts.length - 1);
    });
  }

  // Three quiet quick jumps (server.py's browse_roots), icon plus label
  // rather than the old permanent row of buttons. Keyed on the server's
  // `label` so an icon still gets picked even if a root this file does not
  // know about shows up later; icon-folder is close enough a fallback for
  // any plain-directory shortcut.
  var QUICKJUMP_ICONS = { home: "icon-home", workspace: "icon-folder", footage: "icon-video" };

  function renderQuickJumps(roots) {
    var host = $("browseQuick");
    host.innerHTML = "";
    (roots || []).forEach(function (r) {
      var key = (r.label || r.name || "").toLowerCase();
      var b = document.createElement("button");
      b.type = "button";
      b.className = "quickjump";
      b.appendChild(browseIcon(QUICKJUMP_ICONS[key] || "icon-folder", "rowicon"));
      var lbl = document.createElement("span"); lbl.textContent = r.label || r.name;
      b.appendChild(lbl);
      b.title = r.path;
      b.addEventListener("click", function () { loadBrowseDir(r.path); });
      host.appendChild(b);
    });
  }

  // Top half: the subfolders of S.browsePath. Reads from the S.browseDirs
  // cache rather than taking a fresh response, so a keyboard cursor move can
  // redraw the highlight without a round trip.
  function renderBrowseDirs() {
    var host = $("browseDirs");
    host.innerHTML = "";
    if (!S.browsePath) return;
    if (!S.browseDirs.length) {
      if (!S.browseError) host.appendChild(emptyNote("No subfolders here."));
      return;
    }
    S.browseDirs.forEach(function (d, i) {
      var row = document.createElement("div");
      row.className = "lutrow";
      if (i === S.browseCursor) row.classList.add("cursor");
      row.appendChild(browseIcon("icon-folder", "rowicon"));
      var n = document.createElement("span"); n.textContent = d.name;
      var sp = document.createElement("span"); sp.className = "spacer";
      row.appendChild(n); row.appendChild(sp);
      row.title = d.path;
      row.addEventListener("click", function () { loadBrowseDir(d.path); });
      host.appendChild(row);
    });
  }

  // Bottom half: the video files sitting in S.browsePath -- what used to be
  // the Clips tab's own hardcoded content/footage list, now scoped to
  // whatever folder navigation landed on. Deliberately shows name and size
  // only, not duration: getting duration means probing the file (ffprobe),
  // and probing every video in a folder just for being looked at would turn
  // opening a folder full of large clips into a stall. The full readout
  // (including duration) appears in the Source panel below once a clip is
  // actually selected, same as before.
  function renderFolderClips() {
    var host = $("browseClips");
    host.innerHTML = "";
    if (!S.browsePath) return;
    if (!S.browseFiles.length) {
      if (!S.browseError) host.appendChild(emptyNote("No video files in this folder."));
      return;
    }
    var loaded = S.state && S.state.clips.filter(function (c) { return c.name === S.clip; })[0];
    S.browseFiles.forEach(function (f, i) {
      var row = document.createElement("div");
      row.className = "lutrow";
      var cursorIdx = S.browseDirs.length + i;
      if (cursorIdx === S.browseCursor) row.classList.add("cursor");
      if (loaded && loaded.path === f.path) row.classList.add("active");
      row.appendChild(browseIcon("icon-video", "rowicon"));
      var n = document.createElement("span"); n.textContent = f.name;
      var sp = document.createElement("span"); sp.className = "spacer";
      var x = document.createElement("span");
      x.className = "x"; x.textContent = ((f.bytes || 0) / 1e6).toFixed(0) + " MB";
      row.appendChild(n); row.appendChild(sp); row.appendChild(x);
      row.title = "click to load as the source clip: " + f.path;
      row.addEventListener("click", function () { openBrowseFile(f.path); });
      host.appendChild(row);
    });
  }

  function openBrowseFile(path) {
    $("browseError").textContent = "";
    api("/api/open", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: path })
    }).then(function (j) {
      S.state.clips = j.clips;
      // selectClip redraws the clip list itself (to move the .active row),
      // so there is nothing left to do here beyond confirming it happened;
      // there is no separate tab left to jump back to now that Clips and
      // Browse are the same pane.
      selectClip(j.clip.name);
      toast("opened " + j.clip.name);
    }).catch(function (err) {
      $("browseError").textContent = err.message || String(err);
    });
  }

  // True only while the merged tab is the one on screen, so ArrowUp/Down/
  // Enter (bound globally in onKey below, next to the rest of the app's
  // single-key shortcuts) do not hijack those keys everywhere else.
  function browsePaneActive() {
    var pane = document.querySelector('.railpane[data-rail="browse"]');
    return !!pane && pane.classList.contains("active");
  }

  // Keyboard cursor over dirs-then-files (top half first, then bottom half,
  // the same order they read top to bottom on screen). Re-renders both
  // lists on every press: they are short enough (a folder's own subfolders
  // and clips, not the whole disk) that this costs nothing worth avoiding,
  // and it is the same "just redraw the list" approach fillLooks/fillRenders
  // already use elsewhere in this file.
  function browseKeyNav(key) {
    var total = S.browseDirs.length + S.browseFiles.length;
    if (!total) return;
    if (key === "ArrowDown") {
      S.browseCursor = S.browseCursor < 0 ? 0 : Math.min(S.browseCursor + 1, total - 1);
    } else if (key === "ArrowUp") {
      S.browseCursor = S.browseCursor <= 0 ? 0 : S.browseCursor - 1;
    } else if (key === "Enter") {
      if (S.browseCursor < 0) return;
      if (S.browseCursor < S.browseDirs.length) {
        loadBrowseDir(S.browseDirs[S.browseCursor].path);
      } else {
        var f = S.browseFiles[S.browseCursor - S.browseDirs.length];
        if (f) openBrowseFile(f.path);
      }
      return;
    } else {
      return;
    }
    renderBrowseDirs();
    renderFolderClips();
    var el = document.querySelector(".railpane.browsepane .lutrow.cursor");
    if (el) el.scrollIntoView({ block: "nearest" });
  }

  /* ---- coverage audit -------------------------------------------------- */

  function auditCoverage() {
    var covered = {};
    SCHEMA.forEach(function (stage) {
      stage.controls.forEach(function (c) {
        if (c.path) covered[c.path.join(".")] = true;
        if (c.paths) c.paths.forEach(function (p) { covered[p.join(".")] = true; });
        if (c.wheels) c.wheels.forEach(function (w) { covered[w.path.join(".")] = true; });
      });
    });
    var missing = [];
    (function walk(obj, prefix) {
      Object.keys(obj).forEach(function (k) {
        var path = prefix ? prefix + "." + k : k;
        var v = obj[k];
        if (v !== null && typeof v === "object" && !Array.isArray(v)) {
          walk(v, path);
        } else if (!covered[path]) {
          missing.push(path);
        }
      });
    })(S.defaults, "");
    if (missing.length) {
      console.warn("Fixxr Studio: engine parameters with no control:", missing);
      toast("no control for: " + missing.join(", "), true);
    } else {
      console.log("Fixxr Studio: all " + Object.keys(covered).length
        + " engine parameters have controls.");
    }
  }

  /* ---- overlays -------------------------------------------------------- */

  function openJSON() {
    $("jsonText").value = JSON.stringify(cfg(), null, 2);
    $("jsonError").textContent = "";
    // The diff preview is computed here rather than asked for, so opening this
    // panel never writes a file the user did not ask for. It mirrors what the
    // server strips on save: presets are partial overrides on the defaults.
    $("jsonDiff").textContent = JSON.stringify(localDiff(cfg(), S.defaults), null, 2);
    $("jsonOverlay").classList.add("on");
  }

  function localDiff(c, d) {
    var out = {};
    Object.keys(c).forEach(function (k) {
      if (k.charAt(0) === "_") { out[k] = c[k]; return; }
      if (!(k in d)) { out[k] = c[k]; return; }
      var a = c[k], b = d[k];
      if (a !== null && typeof a === "object" && !Array.isArray(a)
        && b !== null && typeof b === "object" && !Array.isArray(b)) {
        var sub = localDiff(a, b);
        if (Object.keys(sub).length) out[k] = sub;
      } else if (!deepEqual(a, b)) {
        out[k] = a;
      }
    });
    return out;
  }

  var HELP_HTML = [
    "<h4>Viewer</h4><ul>",
    "<li><kbd>space</kbd> hold to see the ungraded frame, release to go back</li>",
    "<li><kbd>\\</kbd> toggle between after only and before only</li>",
    "<li><kbd>y</kbd> cycle left/right, top/bottom and wipe, then back to after only</li>",
    "<li>In wipe mode, drag the blue handle to move the split</li>",
    "<li><kbd>1</kbd> <kbd>2</kbd> switch to grade slot A or B</li>",
    "<li><kbd>shift 1</kbd> <kbd>shift 2</kbd> copy the live grade into that slot</li>",
    "<li><kbd>f</kbd> fit &nbsp; <kbd>0</kbd> 100 percent</li>",
    "<li><kbd>r</kbd> show or hide the reference image beside the frame</li>",
    "<li><kbd>k</kbd> show the secondary qualifier matte</li>",
    "<li><kbd>c</kbd> contact sheet: this grade on four marked frames at once</li>",
    "<li><kbd>g</kbd> show or hide the scopes and statistics dock</li>",
    "</ul>",
    "<h4>Time</h4><ul>",
    "<li><kbd>,</kbd> <kbd>.</kbd> one frame back or forward</li>",
    "<li><kbd>shift ,</kbd> <kbd>shift .</kbd> one second back or forward</li>",
    "<li><kbd>m</kbd> mark this frame &nbsp; <kbd>[</kbd> <kbd>]</kbd> previous or next mark</li>",
    "<li><kbd>p</kbd> play or pause from the current playhead, with the current grade</li>",
    "<li><kbd>l</kbd> loop the range set below the timeline, GPU-graded live</li>",
    "</ul>",
    "<h4>Grade</h4><ul>",
    "<li><kbd>cmd Z</kbd> undo &nbsp; <kbd>shift cmd Z</kbd> redo</li>",
    "<li><kbd>cmd S</kbd> overwrite the loaded preset &nbsp; <kbd>shift cmd S</kbd> save as</li>",
    "<li><kbd>j</kbd> raw JSON view &nbsp; <kbd>?</kbd> this panel &nbsp; <kbd>esc</kbd> close</li>",
    "</ul>",
    "<h4>Controls</h4><ul>",
    "<li>Drag any number left or right to scrub it. <kbd>shift</kbd> is fine, ",
    "<kbd>cmd</kbd> or <kbd>alt</kbd> is coarse.</li>",
    "<li>Double click a number to type an exact value, <kbd>enter</kbd> to commit, ",
    "<kbd>esc</kbd> to cancel.</li>",
    "<li>Double click a control's <b>label</b> or its <b>slider track</b> to reset it ",
    "to the default. The number field's double click is taken by typing.</li>",
    "<li>Bipolar sliders have a tick at their neutral value and fill outward from it.</li>",
    "<li>Colour wheels: drag the puck to tint, use the bar underneath for level, ",
    "double click the wheel to reset. <kbd>shift</kbd> drags at a third of the travel.</li>",
    "<li>Curves: click to add a point, drag to move, <kbd>alt</kbd> or right click a ",
    "point to delete it, double click the graph to reset that channel. The two end ",
    "points are pinned in x.</li>",
    "</ul>"
  ].join("");

  /* ---- wiring ---------------------------------------------------------- */

  // Sidebar collapse/expand (job: app shell restructure). The actual
  // collapsed/expanded state lives on <html> (data-sidebar-left/right),
  // owned by static/sidebars.js so it can be set before first paint on
  // every load, not just after this click handler has ever run once; this
  // only calls into it and keeps the button's own aria-expanded in sync so
  // a screen reader announces the sidebar's current state correctly.
  function bindSidebarToggle(btnId, side) {
    var btn = $(btnId);
    if (!btn) return;
    function sync() {
      btn.setAttribute("aria-expanded", window.fixxrSidebars.isCollapsed(side) ? "false" : "true");
    }
    btn.addEventListener("click", function () {
      window.fixxrSidebars.toggle(side);
      sync();
    });
    sync();
  }

  function bind() {
    bindSidebarToggle("sidebarLeftToggle", "left");
    bindSidebarToggle("sidebarRightToggle", "right");

    $("rotBtn").addEventListener("click", function () {
      // Rotation changes the clip's own upright dimensions, which the loop's
      // decoded range was fetched at, so a stale loop has to stop rather
      // than keep showing frames from the orientation that was current when
      // it was prepared.
      if (S.looping || S.loopPreparing) stopLoop();
      S.autorotate = !S.autorotate;
      localStorage.setItem(rotKey(S.clip), S.autorotate ? "1" : "0");
      $("rotBtn").classList.toggle("active", S.autorotate);
      $("rotBtn").textContent = S.autorotate ? "autorotate" : "no-autorotate";
      var entry = S.state.clips.filter(function (c) { return c.name === S.clip; })[0];
      if (entry) drawClipInfo(entry);
      buildThumbs();
      scheduleRender(0);
    });

    $("previewWidth").addEventListener("change", function (e) {
      // Same reasoning: the loop's decoded frames are at the old width.
      if (S.looping || S.loopPreparing) stopLoop();
      S.width = parseInt(e.target.value, 10);
      scheduleRender(0);
    });

    $("loadPresetBtn").addEventListener("click", function () {
      loadPreset($("presetSelect").value).catch(function (e) { toast(e.message, true); });
    });
    $("presetSelect").addEventListener("dblclick", function () {
      loadPreset($("presetSelect").value).catch(function (e) { toast(e.message, true); });
    });
    $("savePresetBtn").addEventListener("click", function () {
      var name = S.presetName || $("presetSelect").value;
      if (!name) { toast("no preset loaded, use Save as", true); return; }
      savePreset(name, "").catch(function (e) { toast(e.message, true); });
    });
    $("saveAsBtn").addEventListener("click", function () {
      var name = prompt("Save preset as", (S.presetName || "grade") + "_2");
      if (!name) return;
      var comment = prompt("One line description (optional)", "") || "";
      savePreset(name.trim(), comment).catch(function (e) { toast(e.message, true); });
    });
    $("deletePresetBtn").addEventListener("click", function () {
      var name = $("presetSelect").value;
      if (!name || !confirm("Delete preset " + name + "? This cannot be undone.")) return;
      api("/api/preset?name=" + encodeURIComponent(name), { method: "DELETE" })
        .then(function (j) { fillPresets(j.presets); toast("deleted " + name); })
        .catch(function (e) { toast(e.message, true); });
    });

    $("undoBtn").addEventListener("click", undo);
    $("redoBtn").addEventListener("click", redo);
    $("resetAllBtn").addEventListener("click", function () {
      if (!confirm("Reset every parameter to the engine defaults?")) return;
      S.slots[S.active] = clone(S.defaults);
      pushHistory();
      Panels.refresh(cfg(), S.defaults);
      markStageState(); updateModified(); scheduleRender(0);
    });
    // A second, separate reset: this one puts the three columns and every
    // card inside them back where they started, not the grade. Kept as its
    // own button (see #resetLayoutBtn in index.html) rather than folded
    // into resetAllBtn above, because "start this grade over" and "put my
    // panels back" are two different regrets a user can have independently
    // of each other.
    $("resetLayoutBtn").addEventListener("click", resetGridLayout);

    // viewer
    $("fitBtn").addEventListener("click", function () { setZoom("fit"); });
    $("oneToOneBtn").addEventListener("click", function () { setZoom("one"); });
    $("viewMode").addEventListener("change", function (e) { setViewMode(e.target.value); });
    $("gpuToggle").addEventListener("click", function () {
      S.gpu = !S.gpu;
      $("gpuToggle").classList.toggle("active", S.gpu);
      scheduleRender(0);
    });
    $("beforeExposure").addEventListener("change", function () {
      // No point re-rendering a before frame nobody is looking at.
      if (needsBefore(effectiveMode())) renderBefore();
    });
    $("maskBtn").addEventListener("click", function () {
      S.mask = !S.mask; applyViewerState(); scheduleRender(0);
    });
    $("sheetBtn").addEventListener("click", function () {
      S.sheet = !S.sheet; applyViewerState();
      if (S.sheet) renderSheet(); else scheduleRender(0);
    });
    $("slotA").addEventListener("click", function () { setSlot("A"); });
    $("slotB").addEventListener("click", function () { setSlot("B"); });
    $("storeSlot").addEventListener("click", function () {
      var other = S.active === "A" ? "B" : "A";
      S.slots[other] = clone(cfg());
      pushHistory();
      toast("copied grade into slot " + other);
    });

    // split handle (wipe mode only)
    (function () {
      var handle = $("splitHandle");
      handle.addEventListener("mousedown", function (ev) {
        ev.preventDefault();
        function move(e) {
          var r = $("frameImg").getBoundingClientRect();
          var p = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width));
          $("beforeLayer").style.width = (p * 100) + "%";
          handle.style.left = (p * 100) + "%";
        }
        function up() {
          document.removeEventListener("mousemove", move);
          document.removeEventListener("mouseup", up);
        }
        document.addEventListener("mousemove", move);
        document.addEventListener("mouseup", up);
      });
    })();

    $("frameImg").addEventListener("load", fitViewer);
    // refImg loading a new picture changes its own rendered rect (a portrait
    // reference sizes very differently from a landscape one), so any box
    // left over from before this load needs repositioning against it too.
    // fillRefs already clears the box on a genuinely new reference; this
    // covers the frac staying valid but the on-screen rect having moved.
    $("refImg").addEventListener("load", drawRefCropBox);
    window.addEventListener("resize", function () {
      fitViewer(); Panels.resizeCurves(); drawRefCropBox();
    });
    // The window "resize" event only fires for the outer window changing
    // size, but #viewport's own box can change for reasons that never touch
    // the window: the viewerbar wrapping to a second line because a mode
    // name is longer than the last one, the dock or timeline reflowing, the
    // ref pane opening. Any of those leaves a stale fit (an old max-height
    // computed against a taller viewport) unless something re-measures, so
    // #viewport is watched directly rather than trying to enumerate every
    // sibling that could push on it. #refPane is a fixed fraction of
    // #viewport (style.css), so the same observer covers a stale crop box
    // for the same reasons.
    if (window.ResizeObserver) {
      new ResizeObserver(function () { fitViewer(); drawRefCropBox(); }).observe($("viewport"));
    }

    // scopes
    document.querySelectorAll("[data-scope]").forEach(function (b) {
      b.addEventListener("click", function () {
        var k = b.dataset.scope;
        S.scopes[k] = !S.scopes[k];
        b.classList.toggle("active", S.scopes[k]);
        refreshScopes();
      });
    });
    $("scopesRefresh").addEventListener("click", function () {
      refreshScopes(); refreshStats();
    });
    $("scopesAuto").addEventListener("click", function () {
      S.scopesAuto = !S.scopesAuto;
      $("scopesAuto").classList.toggle("active", S.scopesAuto);
    });

    // timeline
    $("scrub").addEventListener("input", function (e) {
      setTime((parseInt(e.target.value, 10) / 1000) * S.duration);
    });
    $("stepBack").addEventListener("click", function () { setTime(S.time - 1 / S.fps); });
    $("stepFwd").addEventListener("click", function () { setTime(S.time + 1 / S.fps); });
    $("playHead").addEventListener("click", function () { setTime(0); });
    $("playBtn").addEventListener("click", function () {
      if (S.playing || S.playPreparing) stopPlayback(); else startPlayback();
    });
    $("playerVideo").addEventListener("loadedmetadata", fitViewer);
    $("loopBtn").addEventListener("click", function () {
      if (S.looping || S.loopPreparing) stopLoop(); else startLoop();
    });
    // A quick way to point the loop at "here": the playhead to two seconds
    // past it, clamped to the clip, or the playhead to the next mark after
    // it if one exists. Typing exact numbers into the fields directly
    // always works too; this just saves doing that for the common case.
    $("loopMarkBtn").addEventListener("click", function () {
      var start = S.time;
      var after = S.marks.filter(function (m) { return m > start + 0.01; }).sort(function (a, b) { return a - b; });
      var end = after.length ? after[0] : Math.min(S.duration || start + 2, start + 2);
      $("loopStart").value = start.toFixed(2);
      $("loopEnd").value = end.toFixed(2);
    });
    $("markBtn").addEventListener("click", addMark);
    $("clearMarksBtn").addEventListener("click", function () {
      S.marks = []; drawMarks(); if (S.sheet) renderSheet();
    });
    $("renderBtn").addEventListener("click", startRender);
    // .value = "" from our own code never fires "input" (only real typing
    // and paste do), so this only trips when the user actually edits the
    // field, which is exactly when their name should stop being overwritten.
    $("renderName").addEventListener("input", function () { S.renderNameAuto = false; });

    // rail tabs
    document.querySelectorAll(".railtab").forEach(function (t) {
      t.addEventListener("click", function () {
        document.querySelectorAll(".railtab").forEach(function (x) { x.classList.remove("active"); });
        document.querySelectorAll(".railpane").forEach(function (x) { x.classList.remove("active"); });
        t.classList.add("active");
        document.querySelector('.railpane[data-rail="' + t.dataset.rail + '"]').classList.add("active");
        // Lazy: the filesystem browser only fetches on the first visit to
        // its tab, not at boot, so a session that never opens it never
        // makes the call. openInitialBrowseDir works out where that first
        // visit should land (last folder used, else the loaded clip's
        // folder, else content/footage) rather than always starting fresh.
        if (t.dataset.rail === "browse" && !S.browsePath) openInitialBrowseDir();
      });
    });

    $("browseUpBtn").addEventListener("click", function () {
      if (S.browseParent) loadBrowseDir(S.browseParent);
    });

    $("refShow").addEventListener("change", function (e) {
      S.refShow = e.target.checked; applyViewerState();
    });
    $("matchRefBtn").addEventListener("click", matchReference);
    bindRefCrop();

    // LUTs
    $("importLutBtn").addEventListener("click", function () { $("lutFile").click(); });
    $("lutFile").addEventListener("change", function (e) {
      var file = e.target.files[0];
      if (!file) return;
      var name = prompt("Name for this LUT", file.name.replace(/\.cube$/i, ""));
      if (!name) return;
      file.text().then(function (text) {
        return fetch("/api/look?name=" + encodeURIComponent(name), {
          method: "POST", headers: { "Content-Type": "text/plain" }, body: text
        });
      }).then(function (r) { return r.json(); }).then(function (j) {
        if (j.error) throw new Error(j.error);
        fillLooks(j.looks);
        toast("imported " + j.imported + " (" + j.size + "-cube)");
      }).catch(function (err) { toast(err.message, true); });
      e.target.value = "";
    });
    $("downloadLutBtn").addEventListener("click", function () {
      var lut = cfg().look.lut;
      if (!lut) { toast("no look LUT selected", true); return; }
      window.location.href = "/api/look?name=" + encodeURIComponent(lut);
    });
    $("rebuildLooksBtn").addEventListener("click", function () {
      postJSON("/api/rebuild", { which: "looks" }).then(pollJobs);
      toast("rebuilding look LUTs");
    });
    $("rebuildCstBtn").addEventListener("click", function () {
      postJSON("/api/rebuild", { which: "cst" }).then(pollJobs);
      toast("rebuilding technical LUTs, this takes a while");
    });
    $("clearCacheBtn").addEventListener("click", function () {
      api("/api/cache/clear", { method: "POST" }).then(function (j) {
        toast("cleared " + j.cleared + " cached files");
        scheduleRender(0);
      });
    });

    // overlays
    $("jsonBtn").addEventListener("click", openJSON);
    $("jsonClose").addEventListener("click", function () { $("jsonOverlay").classList.remove("on"); });
    $("jsonCopy").addEventListener("click", function () {
      navigator.clipboard.writeText($("jsonText").value).then(function () { toast("copied"); });
    });
    $("jsonApply").addEventListener("click", function () {
      try {
        var parsed = JSON.parse($("jsonText").value);
        if (typeof parsed !== "object" || Array.isArray(parsed) || parsed === null) {
          throw new Error("the config must be a JSON object");
        }
        ["convert", "primaries", "look", "fx", "grain"].forEach(function (k) {
          if (!(k in parsed)) throw new Error("missing required section: " + k);
        });
        S.slots[S.active] = parsed;
        pushHistory();
        Panels.refresh(cfg(), S.defaults);
        markStageState(); updateModified(); scheduleRender(0);
        $("jsonError").textContent = "";
        $("jsonOverlay").classList.remove("on");
        toast("config applied");
      } catch (err) {
        $("jsonError").textContent = err.message;
      }
    });
    $("limitsBtn").addEventListener("click", function () {
      $("limitsBody").innerHTML = LIMITS_HTML;
      $("limitsOverlay").classList.add("on");
    });
    $("limitsClose").addEventListener("click", function () { $("limitsOverlay").classList.remove("on"); });
    $("helpBtn").addEventListener("click", function () {
      $("helpBody").innerHTML = HELP_HTML;
      $("helpOverlay").classList.add("on");
    });
    $("helpClose").addEventListener("click", function () { $("helpOverlay").classList.remove("on"); });

    document.addEventListener("keydown", onKey);
    document.addEventListener("keyup", function (ev) {
      if (ev.code === "Space" && S.beforeHold) {
        S.beforeHold = false; applyViewerState();
      }
    });
  }

  function setSlot(which) {
    S.active = which;
    syncSlotButtons();
    Panels.refresh(cfg(), S.defaults);
    markStageState(); updateModified(); scheduleRender(0);
  }
  function syncSlotButtons() {
    $("slotA").classList.toggle("active", S.active === "A");
    $("slotB").classList.toggle("active", S.active === "B");
  }

  function addMark() {
    var t = +S.time.toFixed(2);
    if (S.marks.indexOf(t) < 0) S.marks.push(t);
    S.marks.sort(function (a, b) { return a - b; });
    drawMarks();
    if (S.sheet) renderSheet();
  }

  /* ---- playback ---------------------------------------------------------
     Press play, watch the clip move with the current grade applied. Two
     network steps per press: POST /api/play/prepare resolves the request
     into a cache key (and tells the caller whether it is already rendered),
     then the <video> element's own src points straight at GET
     /api/play/stream?key=... so the browser drives the fetch, not this
     file -- native progressive playback of a chunked fragmented-mp4
     response is what makes "starts after the first fragment" a browser
     built-in instead of something this code has to implement.

     playToken exists because a press of Play is asynchronous (prepare(),
     then the video's own "playing" event) and anything can invalidate it
     before either of those callbacks fires: another press, a parameter
     change (via scheduleRender/stopPlayback above), a view mode switch.
     Every callback checks its own captured token against the current one
     and bails if a newer action has superseded it, which is what stops a
     slow, stale prepare() response from starting a video nobody asked for
     any more. */

  var playToken = 0;

  function playDurationValue() {
    var v = parseFloat($("playDur").value);
    return (isFinite(v) && v > 0) ? v : 5;
  }

  function syncPlayButton() {
    var btn = $("playBtn");
    btn.classList.toggle("active", S.playing || S.playPreparing);
    // The button's own label is the "is this dead" signal the brief asks
    // for: idle says Play, a press that has not produced a frame yet says
    // so explicitly, and only an actually playing video says Pause.
    btn.textContent = S.playing ? "Pause" : (S.playPreparing ? "Preparing..." : "Play");
  }

  function stopPlayback(toastMsg) {
    if (!S.playing && !S.playPreparing) return;
    playToken++;                    // orphans any in-flight prepare()/video listeners
    var video = $("playerVideo");
    video.pause();
    // Actually aborts the underlying fetch (a paused native <video> does
    // not otherwise stop downloading), which matters server side: it is
    // what turns the server's next socket write into a BrokenPipeError, the
    // signal server.py's _play_stream uses to stop feeding a dead
    // connection while it keeps draining ffmpeg into the segment cache.
    video.removeAttribute("src");
    video.load();
    S.playing = false;
    S.playPreparing = false;
    // S.playing is already false, so this lands on whichever of gpu/still
    // the GPU toggle currently calls for; scheduleRender (still to come, in
    // every caller of stopPlayback) replaces it with a fresh render shortly,
    // this just keeps the viewer honest for the gap in between.
    setStageLayer(S.gpu ? "gpu" : "still");
    syncPlayButton();
    $("playStatus").textContent = "";
    if (toastMsg) toast(toastMsg);
  }

  function startPlayback() {
    if (!S.clip || S.playing || S.playPreparing) return;
    // Mutual exclusion with the GPU loop: both only ever show in
    // #afterLayer, and both are "the" picture on screen, so pressing Play
    // stops a running loop rather than fighting it for the same layer. The
    // explicit check here covers the case setViewMode's own guard (below)
    // cannot: the view is already after-only, so setViewMode never runs.
    if (S.looping || S.loopPreparing) stopLoop();
    // Playback only ever shows in #afterLayer; forcing after-only here
    // rather than refusing to play in another mode means Play always does
    // something, which matches "press play, watch the clip move".
    if (S.viewMode !== MODE_AFTER) setViewMode(MODE_AFTER);

    var duration = playDurationValue();
    var startTime = S.time;
    var myToken = ++playToken;
    S.playPreparing = true;
    syncPlayButton();
    $("playStatus").textContent = "preparing...";
    var t0 = performance.now();

    api("/api/play/prepare", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        clip: S.clip, time: startTime, duration: duration, width: S.width,
        autorotate: S.autorotate, config: cfg()
      })
    }).then(function (j) {
      if (myToken !== playToken) return;
      var video = $("playerVideo");
      S.playSegStart = j.start;
      $("playStatus").textContent = j.cached ? "cached, starting..." : "rendering...";

      function onPlaying() {
        if (myToken !== playToken) return;
        S.playing = true;
        S.playPreparing = false;
        setStageLayer("video");
        syncPlayButton();
        $("playStatus").textContent = "playing (first frame "
          + Math.round(performance.now() - t0) + " ms)";
      }
      function onTime() {
        // Guarded on S.playing, not just the token: "timeupdate" keeps
        // firing on a paused/ended element, and without this a stray event
        // after stopPlayback() could still write to S.time.
        if (myToken !== playToken || !S.playing) return;
        var t = S.playSegStart + video.currentTime;
        S.time = t;
        $("timeLabel").textContent = t.toFixed(2) + "s";
        $("scrub").value = String(Math.round(S.duration ? (t / S.duration) * 1000 : 0));
        highlightThumb();
      }
      function onEnded() {
        if (myToken !== playToken) return;
        var landAt = S.playSegStart + (video.currentTime || duration);
        stopPlayback();
        // Back to the ordinary still-frame path at exactly where playback
        // stopped, so the viewer, scopes and stats all agree with the
        // timeline the instant the video disappears.
        setTime(landAt);
      }
      function onError() {
        if (myToken !== playToken) return;
        toast("playback failed to load", true);
        stopPlayback();
      }

      video.addEventListener("playing", onPlaying, { once: true });
      video.addEventListener("timeupdate", onTime);
      video.addEventListener("ended", onEnded, { once: true });
      video.addEventListener("error", onError, { once: true });

      video.src = "/api/play/stream?key=" + encodeURIComponent(j.key);
      video.play().catch(function (e) {
        if (myToken !== playToken) return;
        toast(e.message, true);
        stopPlayback();
      });
    }).catch(function (e) {
      if (myToken !== playToken) return;
      S.playPreparing = false;
      syncPlayButton();
      $("playStatus").textContent = "";
      toast(e.message, true);
    });
  }

  /* ---- live loop (job: live GPU viewer, mode 2) -------------------------
     Press Loop, and the range between #loopStart and #loopEnd decodes once
     (POST /api/range) and then plays back on the GPU forever, re-grading
     every frame from whatever cfg() returns right now (see startLoop in
     live.js). This is deliberately not built on top of the Play feature
     above: Play streams one server-encoded pass through and stops; a config
     change during it has to stop playback because the video already
     streaming cannot be re-graded. The loop never re-encodes anything after
     the initial decode, so a config change is just a different draw call on
     the next animation frame, not a reason to stop. */

  function loopDuration() {
    var start = parseFloat($("loopStart").value);
    var end = parseFloat($("loopEnd").value);
    return { start: start, end: end, duration: end - start };
  }

  function syncLoopButton() {
    var btn = $("loopBtn");
    btn.classList.toggle("active", S.looping || S.loopPreparing);
    btn.textContent = S.looping ? "Stop loop" : (S.loopPreparing ? "Preparing..." : "Loop");
  }

  // The internal half of stopping: tears down the GPU loop and the DOM
  // state that says it is running, but does not touch the timeline. Split
  // from stopLoop() below so onError (the loop cannot just leave a broken
  // render on screen) does not also have to duplicate the "land the
  // playhead where it stopped" behaviour that a normal user-pressed stop
  // wants.
  function stopLoopInternal() {
    StudioLive.stopLoop();
    S.looping = false;
    S.loopPreparing = false;
    // Back to the server still until the caller's next render lands (stopLoop
    // below always follows this with setTime, which schedules one); the
    // canvas would otherwise keep showing its last loop frame with nothing
    // left driving it.
    setStageLayer("still");
    syncLoopButton();
    $("loopStatus").textContent = "";
    setRendererBadge(S.gpu ? "GPU" : "server", false);
  }

  function stopLoop() {
    if (!S.looping && !S.loopPreparing) return;
    var landAt = StudioLive.loopCurrentTime();
    stopLoopInternal();
    // Back to the ordinary still-frame path at exactly where the loop
    // stopped, same contract Play's onEnded honours above: the viewer,
    // scopes and stats all agree with the timeline the instant it stops.
    setTime(landAt);
  }

  function startLoop() {
    if (!S.clip || S.looping || S.loopPreparing) return;
    if (!StudioLive.available()) {
      toast("no GPU renderer available: " + StudioLive.reason(), true);
      return;
    }
    if (S.playing || S.playPreparing) stopPlayback();
    var range = loopDuration();
    if (!isFinite(range.start) || !isFinite(range.end) || range.duration <= 0) {
      toast("loop range needs an end after its start", true);
      return;
    }
    if (S.viewMode !== MODE_AFTER) setViewMode(MODE_AFTER);

    S.loopPreparing = true;
    syncLoopButton();
    $("loopBudget").textContent = "";
    $("loopStatus").textContent = "preparing... (decoding "
      + range.duration.toFixed(1) + "s, a couple of seconds)";
    var sourceWidth = clipSourceWidth();

    StudioLive.prepareLoop({
      clip: S.clip, time: range.start, duration: range.duration,
      width: S.width, autorotate: S.autorotate
    }).then(function (info) {
      S.loopPreparing = false;
      if (!S.clip) return;
      S.looping = true;
      setStageLayer("gpu");
      syncLoopButton();
      setRendererBadge("GPU loop", !!cfg().grain.enabled);
      var lastFps = 0;
      StudioLive.startLoop(cfg, {
        sourceWidth: sourceWidth,
        onFrame: function (f) {
          S.time = f.time;
          $("timeLabel").textContent = f.time.toFixed(2) + "s";
          $("scrub").value = String(Math.round(S.duration ? (f.time / S.duration) * 1000 : 0));
          highlightThumb();
          setRendererBadge("GPU loop", !!cfg().grain.enabled);
          if (f.measuredFps !== lastFps) {
            lastFps = f.measuredFps;
            $("loopStatus").textContent = info.frames + " frames at "
              + info.fps.toFixed(2) + " fps source, loaded in "
              + Math.round(info.loadMs) + " ms, rendering " + f.measuredFps + " fps";
          }
        },
        onError: function (e) {
          stopLoopInternal();
          showError(e);
          toast("GPU loop stopped: " + (e.message || e), true);
        }
      });
    }).catch(function (e) {
      S.loopPreparing = false;
      syncLoopButton();
      $("loopStatus").textContent = "";
      // The budget refusal (StudioLive.prepareLoop's own preflight, or the
      // server's) is the plain-English message itself; show it as-is rather
      // than wrapping it, since it already says how many seconds fit at
      // this width and that a narrower preview buys more.
      toast(e.message || String(e), true);
      if (e.budget) {
        $("loopBudget").textContent = "at " + e.budget.width + "px wide, up to "
          + e.budget.max_seconds.toFixed(1) + "s fits the "
          + Math.round(e.budget.cap_bytes / 1e6) + " MB loop budget";
      }
    });
  }

  function startRender() {
    var name = ($("renderName").value || "").trim();
    if (!name) { toast("give the render a name", true); return; }
    var body = {
      clip: S.clip, config: cfg(), autorotate: S.autorotate, name: name,
      start: parseFloat($("renderStart").value) || 0,
      duration: parseFloat($("renderDur").value) || null,
      scale: $("renderScale").value ? parseInt($("renderScale").value, 10) : null
    };
    api("/api/render", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    }).then(function (j) {
      toast("render started: " + j.job.label);
      document.querySelector('.railtab[data-rail="jobs"]').click();
      pollJobs();
    }).catch(function (e) { toast(e.message, true); });
  }

  function typing(ev) {
    var t = ev.target;
    return t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA"
      || t.tagName === "SELECT" || t.isContentEditable);
  }

  function onKey(ev) {
    if (typing(ev)) {
      if (ev.key === "Escape") ev.target.blur();
      return;
    }
    // Merged Clips/Browse tab (job: file browser): ArrowUp/Down move the
    // keyboard cursor through dirs-then-files, Enter opens a folder or loads
    // a clip. Gated on browsePaneActive() so these three keys only mean
    // anything while that tab is actually on screen, and left alone (not
    // even preventDefault'd) everywhere else, same as every other rail tab.
    if ((ev.key === "ArrowUp" || ev.key === "ArrowDown" || ev.key === "Enter") && browsePaneActive()) {
      ev.preventDefault();
      browseKeyNav(ev.key);
      return;
    }
    var meta = ev.metaKey || ev.ctrlKey;

    if (meta && ev.key.toLowerCase() === "z") {
      ev.preventDefault();
      if (ev.shiftKey) redo(); else undo();
      return;
    }
    if (meta && ev.key.toLowerCase() === "s") {
      ev.preventDefault();
      if (ev.shiftKey) $("saveAsBtn").click(); else $("savePresetBtn").click();
      return;
    }
    if (meta) return;

    switch (ev.key) {
      case " ":
        ev.preventDefault();
        // Space already means "hold to peek at the ungraded frame"; while a
        // video is on screen that would mean showing beforeLayer behind a
        // still-playing video, so space stops playback instead of holding.
        if (S.playing || S.playPreparing) { stopPlayback(); break; }
        if (!S.beforeHold) { S.beforeHold = true; applyViewerState(); renderBefore(); }
        break;
      case "p": $("playBtn").click(); break;
      case "l": $("loopBtn").click(); break;
      case "\\": toggleBeforeAfter(); break;
      case "y": cycleSplitModes(); break;
      case "1": setSlot("A"); break;
      case "2": setSlot("B"); break;
      case "!": S.slots.A = clone(cfg()); toast("stored in A"); break;
      case "@": S.slots.B = clone(cfg()); toast("stored in B"); break;
      case "f": setZoom("fit"); break;
      case "0": setZoom("one"); break;
      case "r": $("refShow").checked = !$("refShow").checked;
        S.refShow = $("refShow").checked; applyViewerState(); break;
      case "k": $("maskBtn").click(); break;
      case "c": $("sheetBtn").click(); break;
      case "g": {
        var d = $("dock");
        d.style.display = d.style.display === "none" ? "" : "none";
        setTimeout(fitViewer, 0);
        break;
      }
      case "m": addMark(); break;
      case ",": setTime(S.time - (ev.shiftKey ? 1 : 1 / S.fps)); break;
      case ".": setTime(S.time + (ev.shiftKey ? 1 : 1 / S.fps)); break;
      case "[": {
        var prev = S.marks.filter(function (t) { return t < S.time - 0.01; });
        if (prev.length) setTime(prev[prev.length - 1]);
        break;
      }
      case "]": {
        var next = S.marks.filter(function (t) { return t > S.time + 0.01; });
        if (next.length) setTime(next[0]);
        break;
      }
      case "j": openJSON(); break;
      case "?": $("helpBtn").click(); break;
      case "Escape":
        document.querySelectorAll(".overlay").forEach(function (o) { o.classList.remove("on"); });
        break;
      default: return;
    }
  }

  /* ---- gridstack docking shell: one column, two independently resizable
     sidebars around it -----------------------------------------------------
     Second direct request, superseding the free-floating widget grid this
     used to be: "there should be 3 main columns so if i move the size of a
     column it should update multiple separate cards in another column".
     Narrowed twice more since: "one main sidebar (the left) and main
     background" promoted "sources" out of GridStack into #sidebarLeft, then
     "the utilities and the parameter should be combined and thats the
     sidebar that opens and closes" promoted "parameters" out of GridStack
     into #sidebarRight, combined there with the utility strip. What is left
     inside #grid is exactly one column, #colMid, holding the viewer, scopes
     and timeline cards stacked vertically -- there is no longer a second
     GridStack column for a splitter to divide, and no rightPct/colFlexBasis
     math to keep two columns' widths in step with a shared 100%.

     "i need to be able to resize each sidebar" moved the resize job outward
     instead of removing it: each sidebar drags its own width from its own
     inner edge now (initSidebarResizers below, window.fixxrSidebars in
     sidebars.js), the same "widen one, narrow main" relationship the old
     colsplit gave the two GridStack columns, just against a sidebar instead
     of a second column.

     "the wigets on main should be long solid wise so when i scroll even on
     them it scrolls the widget page so i dont have to scroll inside tiny
     widgets" is the other half of what used to live here: scopes and
     timeline no longer have a user-draggable height at all (gs-no-resize in
     the HTML) and no longer get squeezed to fit a fixed row budget the way
     GRID_ROWS/computeCellHeight below still sizes the viewer. Their cards
     grow to their own natural content height instead, via GridStack's own
     sizeToContent feature (initAutosizeCards below, and the long comment on
     .autosize in style.css, just above .widgethead, for exactly what it
     measures); .gridcol > .grid-stack's own overflow: auto (style.css) is
     what then scrolls the whole column to reach whichever card does not fit
     a short window, on a plain mouse wheel over any card, the same "one
     scrollport for the page" role it already had before this change.

     This section owns exactly what neither GridStack nor the two sidebar
     drag handles give you for free: the column's default card layout, a
     cellHeight that fills the column's actual height (still real for the
     viewer, whose height stays row-count based), and localStorage
     persistence for both the card layout and each sidebar's width, with a
     reset action that puts all of it back. */

  // Still an array-of-column-keys, even with exactly one entry: saveGridLayout/
  // loadSavedGridLayout/resetGridLayout/initGrid all key their work off this
  // list rather than the literal string "mid", so a future column would be a
  // one-line addition here plus the matching markup, not a second code path
  // through every function below.
  var COLUMNS = ["mid"];

  // Mirrors the gs-x/gs-y/gs-w/gs-h already on each .grid-stack-item in
  // index.html. Duplicated rather than read back off the DOM for the same
  // reason as before: the reset action needs a value to reset TO that a
  // user's own dragging (or a sizeToContent measurement -- see
  // initAutosizeCards) can never have overwritten. scopes/timeline's h here
  // is only their SEED row count before the first sizeToContent measurement
  // lands on load/reset; it is not a floor or a target the way viewer's h
  // still is.
  var GRID_DEFAULT_LAYOUT = {
    mid: [
      { id: "viewer", x: 0, y: 0, w: 1, h: 7 },
      { id: "scopes", x: 0, y: 7, w: 1, h: 4 },
      { id: "timeline", x: 0, y: 11, w: 1, h: 3 }
    ]
  };
  var GRID_ROWS = 14; // viewer(7) + scopes(4) + timeline(3), the seed layout's own row total
  var GRID_MARGIN = 4; // the same 4px spacing grid the rest of the app uses
  // v2: per-column shape, not one flat array. Two now-unused keys from
  // earlier shapes are simply never read rather than migrated: "left" (from
  // when sources was still a GridStack column) and, as of this change,
  // "right" (from when parameters was) -- an old saved blob carrying either
  // just quietly loses that key, the same way GRID_DEFAULT_LAYOUT above no
  // longer has one to load it into.
  //
  // v3: bumped again, same discard-not-migrate treatment, for a reason that
  // is not visible in this file's own current code: a v2 blob saved by the
  // OLD initAutosizeCards can carry sizeToContent: true, resizeToContentParent:
  // ".autosize" and minH: 2 on its "scopes"/"timeline" entries (grid.save()
  // snapshots whatever GridStack set on the node, and that used to include
  // those). grid.load() applies a loaded entry's fields onto the live node
  // object, so an old blob would silently re-arm GridStack's OWN sizeToContent
  // machinery on top of this file's replacement for it (see the long comment
  // above AUTOSIZE_CARDS for why that machinery is what crashes). Bumping the
  // key means every saved-before-this-change layout is discarded once,
  // exactly like the "left"/"right" keys above, rather than carrying that
  // stale baggage forward into a grid.load() call this file does not control
  // the contents of.
  var GRID_STORAGE_KEY = "studio.gridlayout.v3";

  var grids = {}; // column key -> GridStack instance

  function colGridId(colKey) {
    return "grid" + colKey.charAt(0).toUpperCase() + colKey.slice(1);
  }

  // The floor a row is ever allowed to shrink to. This used to be 24, which is
  // not a floor in practice: on anything shorter than about an 850px window
  // (see avail/GRID_ROWS below) 24 is BIGGER than what avail leaves per row, so
  // Math.max(24, h) picked h every time and cellHeight kept falling as the
  // window got shorter, with nothing to stop it. That is what squeezed the
  // timeline card down to a sliver too short to show its own toolbar, and
  // because the column's total content height was ALWAYS shrunk to exactly
  // fit whatever height was available, .gridcol > .grid-stack's own
  // scrollHeight never exceeded its clientHeight either, so there was nothing
  // to scroll to -- the card was simply gone.
  //
  // What this floor still governs, now that scopes and timeline size to
  // their own content instead of a row budget (see initAutosizeCards): the
  // viewer, still the one card whose height is plain row-count times
  // cellHeight, same as it always was. 54 keeps a short window's viewer
  // readable rather than shrinking it to a sliver the way the old unfloored
  // math did; it no longer has to also keep scopes/timeline clear of a fixed
  // min-height the way it did when this comment was first written, because
  // gs-no-resize plus sizeToContent means neither of those two cards can be
  // squeezed by cellHeight math at all anymore.
  //
  // Math.max still runs the OTHER direction too: on a tall window avail/rows
  // comes out well above 54, and the larger of the two wins, so the viewer
  // still grows to fill spare height exactly as before. The floor only ever
  // engages on a short window, and when it does, the column's natural-height
  // content genuinely exceeds the column's own box, which is what finally
  // gives `.gridcol > .grid-stack`'s overflow: auto (style.css) real content
  // to scroll rather than a box it already fit.
  var MIN_ROW_HEIGHT = 54;

  function computeCellHeight(colKey) {
    var host = $(colGridId(colKey));
    var avail = host ? host.clientHeight : 0;
    var h = Math.floor((avail - (GRID_ROWS + 2) * GRID_MARGIN) / GRID_ROWS);
    return Math.max(MIN_ROW_HEIGHT, h);
  }

  function saveGridLayout() {
    try {
      var out = {};
      COLUMNS.forEach(function (k) { if (grids[k]) out[k] = grids[k].save(false); });
      // false: do not also snapshot each card's innerHTML. The panels are
      // rebuilt from server state on every load regardless, so saving
      // content here would only bloat localStorage with a copy nothing
      // reads back.
      localStorage.setItem(GRID_STORAGE_KEY, JSON.stringify(out));
    } catch (e) {
      // Private-mode Safari and a full quota both throw here; the layout
      // just stops persisting across reloads, nothing else in the app breaks.
    }
  }

  function loadSavedGridLayout() {
    try {
      var raw = localStorage.getItem(GRID_STORAGE_KEY);
      return raw ? JSON.parse(raw) : null;
    } catch (e) {
      return null;
    }
  }

  // #dock (scopes+stats) and #timeline are gs-no-resize in the HTML and
  // wrapped in .autosize there (see .autosize's own long comment in
  // style.css for exactly what that wrapper measures): this file sizes
  // their cards itself, rather than turning on GridStack's own
  // sizeToContent feature for them.
  //
  // That is a deliberate workaround, not the first design: sizeToContent
  // was tried first (grid.update(el, {sizeToContent:true,
  // resizeToContentParent:".autosize", minH:2}) plus grid.resizeToContent())
  // and reproduced GridStack's own measurement correctly, but applying a
  // new height goes through the vendored build's moveNode(), which calls
  // _fixCollisions() to push any node it now overlaps out of the way.
  // "scopes" sits between "viewer" above it and "timeline" below it in one
  // float:true column, and resizing it that way sent moveNode and
  // _fixCollisions into an infinite mutual recursion that never converges
  // (confirmed live: a RangeError: Maximum call stack size exceeded on
  // every page load, every time; wrapping the calls in
  // grid.batchUpdate()/grid.batchUpdate(false) does not help either, the
  // recursion happens inside one moveNode call, not across the repack a
  // batch only defers). That is a bug in the vendored GridStack build's
  // collision engine, not something this file's own options can avoid
  // while still calling moveNode for a middle item in a stack.
  //
  // grid.load() does not have that problem: it replaces the whole node set
  // in one non-incremental pass (confirmed live), so there is nothing left
  // to collide with by the time GridStack lays the new set out. So this
  // measures each autosize card's real content height using the exact same
  // formula GridStack's own resizeToContent() uses internally (read out of
  // the vendored source and reproduced in measureContentRows below), then
  // calls grid.load() once with viewer's own position untouched (it is
  // still a plain row-count card a user can drag-resize, see initGrid's
  // resizable option) and scopes/timeline stacked directly beneath it with
  // no gap.
  //
  // gs-id (the GridStack card) and its actual content element's id are
  // different strings for the scopes card specifically -- gs-id="scopes" is
  // the widget, #dock is the content inside it, #scopes is a DIFFERENT,
  // nested element further in (the row of scope images inside #dockbody) --
  // so this maps card id to content id explicitly rather than assuming they
  // match the way they happen to for timeline.
  var AUTOSIZE_CARDS = { scopes: "dock", timeline: "timeline" };

  // Same math as the vendored build's own GridStack.prototype.resizeToContent,
  // traced out of gridstack-all.js: s is the pixel height of one row
  // (margin included, the "true" arg), r is the card's own chrome above
  // .autosize (just .widgethead here), l is the content height GridStack
  // believes the card currently has, h is the content's real natural
  // height (getBoundingClientRect, not clientHeight, so a shrunk parent
  // never clips the reading), and a is the row count that would fit h. The
  // 2-row floor mirrors the old minH: 2 this file used to pass to GridStack
  // directly.
  function measureContentRows(grid, cardId) {
    var el = grid.el.querySelector('[gs-id="' + cardId + '"]');
    var node = el && el.gridstackNode;
    if (!el || !node || !el.clientHeight) return node ? node.h : null;
    var s = grid.getCellHeight(true);
    if (!s) return node.h;
    var wrap = el.querySelector(".autosize");
    var content = wrap ? wrap.firstElementChild : null;
    if (!wrap || !content) return node.h;
    var n = node.h ? node.h * s : el.clientHeight;
    var r = el.clientHeight - wrap.clientHeight;
    var l = node.h ? node.h * s - r : wrap.clientHeight;
    var h = content.getBoundingClientRect().height || l;
    if (l === h) return node.h;
    n += h - l;
    return Math.max(2, Math.ceil(n / s));
  }

  // Re-measures scopes/timeline and, only if a row count or position
  // actually changed, applies the whole mid-column layout in one
  // grid.load() call -- see the long comment above AUTOSIZE_CARDS for why
  // this does not call grid.resizeToContent()/moveNode() per card instead.
  // Safe to call as often as needed (window resize, a card's own content
  // changing, Reset Layout): the no-op check keeps an unrelated resize tick
  // from re-triggering grid's own "change" event (and the localStorage
  // write it causes) when nothing about the layout actually moved.
  function reflowAutosizeCards() {
    var grid = grids.mid;
    if (!grid) return;
    var viewerEl = grid.el.querySelector('[gs-id="viewer"]');
    var scopesEl = grid.el.querySelector('[gs-id="scopes"]');
    var timelineEl = grid.el.querySelector('[gs-id="timeline"]');
    var viewer = viewerEl && viewerEl.gridstackNode;
    var scopes = scopesEl && scopesEl.gridstackNode;
    var timeline = timelineEl && timelineEl.gridstackNode;
    if (!viewer || !scopes || !timeline) return;
    var scopesH = measureContentRows(grid, "scopes") || scopes.h;
    var timelineH = measureContentRows(grid, "timeline") || timeline.h;
    var scopesY = viewer.y + viewer.h;
    var timelineY = scopesY + scopesH;
    if (scopesH === scopes.h && scopesY === scopes.y && timelineH === timeline.h && timelineY === timeline.y) {
      return;
    }
    grid.load([
      { id: "viewer", x: viewer.x, y: viewer.y, w: viewer.w, h: viewer.h },
      { id: "scopes", x: scopes.x, y: scopesY, w: scopes.w, h: scopesH },
      { id: "timeline", x: timeline.x, y: timelineY, w: timeline.w, h: timelineH }
    ]);
  }

  // Wires the standing watchers, once: a ResizeObserver per autosize card's
  // own content element. GridStack's own grid-level ResizeObserver (armed
  // automatically by sizeToContent) exists to catch the GRID CONTAINER's
  // box changing -- a window resize, a sidebar drag narrowing #mainBg --
  // but this file is not using that feature (see above), and covers that
  // same case itself via the plain window "resize" listener in initGrid.
  // What neither of those covers is the content growing or shrinking with
  // the container's own box untouched: #marks (inside #timeline) wrapping
  // onto a second row as marks pile up, or a scope toggled off/on. This
  // ResizeObserver watches the actual content element (#dock/#timeline, not
  // .autosize) for exactly that gap. It cannot retrigger itself: #dock and
  // #timeline are plain block children of .autosize, not stretched to fill
  // it, so their own box only changes size when THEIR content changes, not
  // when reflowAutosizeCards resizes the card around them.
  function initAutosizeCards() {
    var grid = grids.mid;
    if (!grid) return;
    reflowAutosizeCards();
    if (!window.ResizeObserver) return;
    Object.keys(AUTOSIZE_CARDS).forEach(function (gsId) {
      var content = $(AUTOSIZE_CARDS[gsId]);
      if (!content) return;
      new ResizeObserver(reflowAutosizeCards).observe(content);
    });
  }

  function resetGridLayout() {
    try {
      localStorage.removeItem(GRID_STORAGE_KEY);
    } catch (e) { /* see saveGridLayout */ }
    COLUMNS.forEach(function (k) { if (grids[k]) grids[k].load(GRID_DEFAULT_LAYOUT[k]); });
    reflowAutosizeCards();
    window.fixxrSidebars.resetWidth("left");
    window.fixxrSidebars.resetWidth("right");
    toast("layout reset to defaults");
  }

  function initGrid() {
    var saved = loadSavedGridLayout();
    COLUMNS.forEach(function (colKey) {
      var g = GridStack.init({
        column: 1, // one vertical lane: width is the column's job, not a card's
        cellHeight: computeCellHeight(colKey),
        margin: GRID_MARGIN,
        // Shorthand for draggable.handle, confirmed against the vendored
        // build's own option-merge code: without this the whole card body
        // is the drag target, which would fight every slider, LUT row and
        // scope click inside it.
        handle: ".widgethead",
        // Only the bottom edge, and only meaningful for the viewer now:
        // scopes/timeline are gs-no-resize in the HTML (their height comes
        // from reflowAutosizeCards instead, see the long comment above
        // AUTOSIZE_CARDS), so this handle simply never renders for those
        // two cards' own drag interaction, GridStack reads gs-no-resize per
        // node before this grid-level default ever applies.
        resizable: { handles: "s" },
        // Cards stay exactly where they are put; nothing they did not ask
        // to move reflows to fill a gap.
        float: true,
        disableOneColumnMode: true
      }, "#" + colGridId(colKey));
      grids[colKey] = g;
      if (saved && saved[colKey] && saved[colKey].length) g.load(saved[colKey]);
      g.on("change", saveGridLayout);
    });

    initAutosizeCards();
    initSidebarResizers();

    // The viewer's row-count height depends on cellHeight, which is a
    // function of the column's own available height, so this recomputes it
    // on every window resize same as before. It also reflows scopes/
    // timeline here: cellHeight changing changes how many rows their SAME
    // pixel content height converts to, even though nothing about their
    // content itself moved (a case the content ResizeObservers in
    // initAutosizeCards do not see, since #dock/#timeline's own box does
    // not change on a plain window resize). A sidebar drag narrowing
    // #mainBg is not a window resize and does not come through here at all
    // -- it is covered separately, by those same content ResizeObservers,
    // since narrowing .autosize narrows #dock/#timeline directly and either
    // can reflow onto more rows (#marks in particular) as a result.
    window.addEventListener("resize", function () {
      COLUMNS.forEach(function (k) { if (grids[k]) grids[k].cellHeight(computeCellHeight(k)); });
      reflowAutosizeCards();
    });
  }

  // Each sidebar's own drag handle (.sidebar-resize, one per sidebar, on its
  // inner edge -- see index.html and the app shell section of style.css)
  // replaces the old two-column .colsplit: window.fixxrSidebars (sidebars.js)
  // already owns clamping, the live CSS custom property paint and
  // localStorage persistence (the same pre-CSS boot script that keeps a
  // sidebar's collapsed state flash-free on reload does the same for its
  // width), so this only supplies the pointer plumbing -- mirroring the
  // deleted initColumnSplitters's own live-paint-during-drag,
  // persist-on-release split (applyWidth mid-drag, setWidth on release).
  function initSidebarResizers() {
    document.querySelectorAll(".sidebar-resize").forEach(function (handle) {
      var side = handle.dataset.resizeSide;
      if (side !== "left" && side !== "right") return;
      handle.addEventListener("mousedown", function (ev) {
        // A collapsed sidebar has no width to drag; style.css already keeps
        // the handle out of the 0-width box via overflow: hidden, this is
        // the behavioural half of that same contract.
        if (window.fixxrSidebars.isCollapsed(side)) return;
        ev.preventDefault();
        var startX = ev.clientX;
        var startWidth = window.fixxrSidebars.getWidth(side);
        var liveWidth = startWidth;
        handle.classList.add("dragging");

        function move(e) {
          // Left sidebar's handle sits on its own right edge, so dragging
          // right widens it. Right sidebar's handle sits on its LEFT edge,
          // so dragging right narrows it instead -- the sign flips between
          // the two for the same reason the old splitter's one deltaPct
          // meant opposite things for colMid vs colRight.
          var deltaX = e.clientX - startX;
          var target = side === "left" ? startWidth + deltaX : startWidth - deltaX;
          liveWidth = window.fixxrSidebars.applyWidth(side, target);
        }
        function up() {
          document.removeEventListener("mousemove", move);
          document.removeEventListener("mouseup", up);
          handle.classList.remove("dragging");
          window.fixxrSidebars.setWidth(side, liveWidth);
        }
        document.addEventListener("mousemove", move);
        document.addEventListener("mouseup", up);
      });
    });
  }

  /* ---- boot ------------------------------------------------------------ */

  function boot() {
    // Laid out before the state fetch below, not after: the grid does not
    // depend on server data, and starting it immediately is what stops the
    // five widgets from ever being visible in their raw, GridStack-less,
    // stacked-on-top-of-each-other state while that fetch is in flight.
    initGrid();

    api("/api/state").then(function (state) {
      S.state = state;
      S.defaults = state.defaults;
      S.slots.A = clone(state.defaults);
      S.slots.B = clone(state.defaults);
      lastCommitted = snapshot();

      // GPU preview (job: live GPU viewer). Tried once, here, against the
      // real canvas: StudioGPU.create never throws for a missing feature,
      // it returns null with a reason, which is exactly what lets this be
      // an on/off decision made up front rather than a try/catch around
      // every render. Defaults ON when available: re-grading on the GPU
      // instead of round-tripping to the server is the entire point of
      // wiring gpu.js into this viewer, so the fallback is the exception,
      // not the starting state.
      var live = StudioLive.init($("gpuCanvas"), { apiBase: "" });
      StudioLive.setDefaults(state.defaults);
      S.gpuOk = live.ok;
      S.gpu = live.ok;
      $("gpuToggle").classList.toggle("active", S.gpu);
      if (!live.ok) {
        $("gpuToggle").disabled = true;
        $("gpuToggle").title = "GPU preview unavailable: " + live.reason;
        $("loopBtn").disabled = true;
        $("loopBtn").title = "GPU preview unavailable: " + live.reason;
        setRendererBadge("server (no GPU: " + live.reason + ")", false);
      }

      Panels.build($("params"), { onChange: onParamChange });
      Panels.refresh(cfg(), S.defaults);
      auditCoverage();

      fillPresets(state.presets);
      fillLooks(state.looks);
      fillRefs(state.refs);
      fillRenders(state.renders);
      drawMarks();
      syncSlotButtons();
      updateUndoButtons();
      bind();
      Panels.resizeCurves();

      if (state.clips.length) {
        selectClip(state.clips[0].name);
      } else {
        showError(new Error("no clips in content/footage"));
      }

      // Start on the honest conversion rather than a look, the same way the
      // grading guide says to: look at the flat frame first, then decide. This
      // actually loads it, so the modified indicator means something from the
      // first change onward.
      if (state.presets.some(function (p) { return p.name === "flat"; })) {
        $("presetSelect").value = "flat";
        loadPreset("flat").catch(function () {});
      }
      pollJobs();
      applyViewerState();
      setZoom("fit");
    }).catch(function (e) {
      document.body.insertAdjacentHTML("afterbegin",
        '<pre style="color:#c96;padding:16px">could not reach the studio server: '
        + e.message + "</pre>");
    });
  }

  boot();
})();
