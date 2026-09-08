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
    // The open project (contract C4): the whole record GET /api/project
    // answers with, refreshed whenever the server says HEAD moved. It is the
    // one place the tab keeps "which clip, which commit, which rotation" now
    // that a reload has to bring all three back, and it is deliberately NOT
    // the live config: cfg() is still the truth for what is on screen, and
    // S.project.config is only read when the server hands us a new HEAD.
    project: null,
    // One of auto, 0, 90, 180 or 270, as strings, exactly as the server and
    // the engine spell them. Mirrors the open project's rotation; it is not
    // a browser preference any more (the old S.autorotate lived in
    // localStorage, invisible to an agent and to any render started outside
    // this tab, which is what made a CLI render come out sideways).
    rotation: "auto",
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
    // The picked rectangles (contract C7), each [x0, y0, x1, y1] as
    // fractions of its own picture (0 to 1). refCrop is drawn on the
    // reference, frameCrop on the frame in the viewer; null means whole
    // image, which is what a match did before this existed. matchCrops is
    // the whole per project bag as the server stores it, keyed by reference
    // name, so switching references brings each one's rectangle back.
    // pickRef and pickFrame are only which tool is switched on right now.
    refCrop: null,
    frameCrop: null,
    matchCrops: {},
    pickRef: false,
    pickFrame: false,
    viewMode: "after",
    beforeHold: false,
    // Where the wipe's split sits, as a percentage of the frame's width.
    // State, not an inline style read back off the handle, so leaving wipe
    // and coming back lands on the same split for the rest of the session
    // instead of snapping to the middle every time.
    splitPos: 50,
    mask: false,
    sheet: false,
    // The zoom used to live here as "fit" or "one". It is viewer-zoom.js's
    // now (contract E4: Fit, 100, 200, 400, plus, minus, wheel and pinch
    // around the pointer, drag to pan), and there is deliberately no copy of
    // it here: two places holding the same number is how a transform and a
    // layout come to disagree. Ask window.ViewerZoom.
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
    loopPreparing: false,
    // Proxy playback (job: live GPU viewer, mode 3). gpuPlaying is the third
    // "is something moving on screen" flag, deliberately NOT S.playing:
    // playing means a server encoded <video> is the picture, and mode 3's
    // picture is the GPU canvas, so setStageLayer has to land on "gpu" for
    // it. proxyPreparing covers the one ffmpeg pass per clip that has to
    // finish before anything can play; proxyClip/proxyReady say which clip
    // the prepared proxy belongs to, so selecting another clip (or changing
    // the preview width) cannot leave Play pointing at the previous one's
    // frames. proxyWarming is the background encode started on clip select
    // and is deliberately NOT part of "is playback active": pressing Play
    // while it runs joins that same job rather than being read as a stop.
    gpuPlaying: false,
    proxyPreparing: false,
    proxyWarming: false,
    proxyDesc: null,
    proxyReady: false
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
    var dims = clipDims(entry);
    return (dims && dims.width) || S.width;
  }

  /* What the open clip's file actually IS, as the server resolved it.
   *
   * The GPU preview cannot work this out for itself: convert.input "auto"
   * is answered from the file's transfer and primaries tags, which live in
   * the probe. The server already publishes its answer per clip in
   * /api/state, so this hands it to StudioLive.renderStill and the GPU asks
   * for the same technical cube the render would (see live.js
   * withResolvedInput). Empty when there is no state yet, which leaves the
   * old behaviour: "auto" means the Apple Log cubes.
   */
  function clipResolvedInput() {
    var entry = S.state && S.state.clips.filter(function (c) { return c.name === S.clip; })[0];
    return (entry && entry.source && entry.source.resolved_input) || "";
  }

  /* The frame size the graph actually sees at the project's rotation.
   *
   * The same rule the server uses (server.py _rotation_dims): `auto` is the
   * file's own tag honoured, every fixed rotation starts from the RAW frame
   * (the tag ignored), and a quarter turn swaps the two axes. /api/state also
   * reports this as entry.effective once a project is open, and that is used
   * when it is there; this is the fallback for the moment right after a
   * rotation change, before the next /api/state, and for a clip nobody has a
   * project for yet. */
  function clipDims(entry) {
    if (!entry) return null;
    if (entry.effective && entry.effective.rotation === S.rotation) return entry.effective;
    if (S.rotation === "auto") return entry.autorotate || entry.raw || null;
    var raw = entry.raw || entry.autorotate;
    if (!raw) return null;
    if (S.rotation === "90" || S.rotation === "270") {
      return { width: raw.height, height: raw.width };
    }
    return raw;
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

  /* One committed edit (a slider release, a checkbox, reset all, a pasted
   * JSON config). Contract C4: the publish below IS the commit now, so this
   * no longer keeps a stack of its own.
   *
   * There used to be a second, private undo history in this file (S.history,
   * S.future) sitting beside the server's. Two histories of the same thing
   * disagree the moment anybody else touches the project: an agent's commit
   * did not appear in the tab's stack, and the tab's undo silently rewound
   * past it. The project's commit tree is the only history now (Undo, Redo,
   * cmd+Z and the History panel all call /api/project/undo|redo), so this
   * publishes and nothing else. S.history and S.future stay as empty arrays
   * purely so older call sites that clear them are still harmless.
   *
   * The debounced PUT /api/grade autosave that used to fire from here is
   * gone with it: the commit is the save. The grades row is still written
   * server side on every commit, so the copy-from picker still fills. */
  function pushHistory() {
    var snap = snapshot();
    if (snap === lastCommitted) return;
    lastCommitted = snap;
    // Session publishing is a no-op with nothing loaded (see session.js), so
    // this costs nothing before the first clip is open.
    if (window.StudioSession) window.StudioSession.publish(cfg(), S.clip, S.time);
  }

  /* Put a config from the server (a checkout, an undo, a redo, a clip
   * switch, an outside agent's patch) on screen without publishing it back
   * out. Nothing here writes to the server: whatever handed us this config
   * is already the server's HEAD. */
  function applyServerConfig(config) {
    S.slots[S.active] = clone(config);
    // Contract G4: this config's own rotation (a checkout, an undo, a redo,
    // a copied grade) is what the control has to show now, not whatever it
    // showed a moment ago; see maybeAdoptConfigRotation's own comment for
    // when it does and does not move the control.
    maybeAdoptConfigRotation(config);
    lastCommitted = snapshot();
    syncSlotButtons();
    Panels.refresh(cfg(), S.defaults);
    markStageState();
    updateModified();
    scheduleRender(0);
  }

  // Undo and redo are the project's, not this tab's. The response carries the
  // new HEAD and its config, which applyProject puts on screen.
  var stepBusy = false;
  function projectStep(which) {
    if (stepBusy || !S.project || !S.project.open) return Promise.resolve();
    stepBusy = true;
    return api("/api/project/" + which, {
      method: "POST", headers: { "Content-Type": "application/json" },
      // Signed "studio", the same label StudioSession.publish uses for this
      // tab's own edits. Without it the server signs the write "cli", the
      // long poll hands this tab its own undo back as somebody else's
      // change, and the late re-apply lands on top of whatever was done in
      // between (measured: it wiped a window drag made right after an undo).
      body: JSON.stringify({ by: "studio" })
    }).then(function (proj) {
      applyProject(proj, { config: true });
      if (!proj.moved) toast(proj.note || ("nothing to " + which));
    }).catch(function (e) {
      toast(e.message || String(e), true);
    }).then(function () { stepBusy = false; });
  }
  function undo() { return projectStep("undo"); }
  function redo() { return projectStep("redo"); }

  /* Undo is possible when HEAD has a parent, redo when HEAD is not the tip of
   * its branch. Both facts ride along on every project response
   * (head_commit.parent and head_commit.is_tip), so this costs no extra
   * request. */
  function updateUndoButtons() {
    var head = S.project && S.project.open && S.project.head_commit;
    $("undoBtn").disabled = !(head && head.parent);
    $("redoBtn").disabled = !(head && head.is_tip === false);
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
    // The power window's on-picture shape editor (window-editor.js) draws
    // the selected layer's mask.window, so it has to redraw wherever the
    // config moved. This is that one place: every edit, undo, preset load,
    // clip switch and outside session patch already funnels through here, so
    // hooking it is what keeps the overlay and the layer's Window group's
    // sliders showing the same shape without either one polling the other.
    if (window.WindowEditor) window.WindowEditor.sync();
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
    // Mode 3 (proxy playback) is the same exception for the same reason: it
    // re-reads cfg() on every presented frame, so a knob change is already
    // on screen by the next frame and a still-frame fetch aimed at the
    // hidden #frameImg would be both wasted and wrong.
    if (S.looping || S.gpuPlaying) return;
    clearTimeout(renderTimer);
    renderTimer = setTimeout(doRender, delay === undefined ? 110 : delay);
  }

  function basePayload(extra) {
    var p = {
      clip: S.clip, time: S.time, width: S.width,
      rotation: S.rotation, config: cfg()
    };
    for (var k in (extra || {})) p[k] = extra[k];
    return p;
  }

  function doRender() {
    if (!S.clip) return;
    if (S.sheet) { renderSheet(); return; }
    // Frames 2 and 4 (contract E4): the picture on screen is the slot grid,
    // not the stage, so the slots are what a config change has to re-render.
    // frames.js renders each slot through the same GPU path this function
    // uses and falls back per slot to the same server route, then calls back
    // for the statistics and the scopes once slot 1 (the playhead) is up.
    if (framesActive()) { window.Frames.render(); return; }
    var t0 = performance.now();
    var mode = S.mask ? "mask" : "graded";

    // Bypass (job: compare). Before-only means the bypassed picture is the
    // ONLY picture on screen, because #stage.mode-before hides #afterLayer
    // outright. So the graded render is skipped here rather than paid for
    // and then hidden, and leaving bypass re-renders it, which on the GPU
    // path is a second pass over a source frame live.js already has cached:
    // no network request at all. The matte view is excluded because it is a
    // diagnostic of the selected layer's mask, not a picture that a bypass
    // comparison means anything against.
    if (mode === "graded" && effectiveMode() === MODE_BEFORE) {
      renderBypassOnly(t0);
      return;
    }

    // Every other before-showing mode (left/right, top/bottom, wipe) has the
    // bypassed picture on screen NEXT to the graded one, so it has to be
    // produced as well. On the GPU path that is gpuBypassStep, and it runs
    // before the graded render rather than after it because both present to
    // the same canvas: the bypass is copied off into #bypassCanvas and the
    // graded render then overwrites the GPU canvas the viewer shows.
    // Deciding this up front instead of inside the promise chain is what
    // keeps the "does the server still owe us a before frame" test at the
    // bottom of this function a plain synchronous one.
    var gpuBefore = mode === "graded" && S.gpu && StudioLive.available()
      && needsBefore(effectiveMode());

    // GPU preview (job: live GPU viewer, mode 1). Only the graded picture
    // itself goes through gpu.js; mask mode is a diagnostic view of the
    // selected layer's mask, not the grade the user is judging, and stays on
    // the server path unconditionally. Falls back to doRenderServer on ANY
    // rejection (an unsupported stage, a decode error, no GPU at all): the
    // server path is the one that matches the final render exactly, so a
    // GPU failure must never leave the viewer showing nothing.
    if (mode === "graded" && S.gpu && StudioLive.available()) {
      inflight++;
      gpuBypassStep(gpuBefore).then(function () {
        // A scrub started a proxy seek (see proxyPreview): let it land
        // first, so the accurate 16-bit still render is the LAST thing to
        // present rather than racing the 8-bit preview for the canvas.
        return whenProxySettled();
      }).then(function () {
        return StudioLive.renderStill({
          clip: S.clip, time: S.time, width: S.width, rotation: S.rotation,
          config: cfg(), sourceWidth: clipSourceWidth(),
          resolvedInput: clipResolvedInput()
        });
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
    // that second request is worth paying for. gpuBypassStep has already
    // taken that job whenever the GPU is driving, and pays no network for it.
    // Handing the layer back to the server frame here (rather than only in
    // the GPU path's own failure branch) is what keeps the matte view
    // honest: it forces the server path with the wipe still on, and without
    // this the layer would keep showing the last GPU bypass, which is a
    // different frame as soon as the time moves.
    if (needsBefore(effectiveMode()) && !gpuBefore) {
      setBypassSource("server");
      renderBefore();
    }
  }

  function doRenderServer(mode, t0) {
    inflight++;
    $("renderTime").textContent = "rendering...";
    // Matte mode (job: layers) shows the SELECTED layer's mask, not merely
    // "the first enabled one" (server.py's mask_preview_config's own
    // fallback, kept for a request that sends no mask_layer at all, such as
    // an old client or nothing selected yet).
    var extra = { mode: mode };
    if (mode === "mask" && window.Layers) {
      var maskIdx = window.Layers.getSelectedIndex();
      if (maskIdx >= 0) extra.mask_layer = maskIdx;
    }
    frameRequest("main", basePayload(extra))
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

  // t0 is passed only by the bypass-only path, where this frame is not a
  // second picture beside the graded one but the ONLY picture on screen, so
  // it also owes the viewer a render time and a set of measurements. Every
  // other caller passes nothing and gets exactly the old behaviour.
  function renderBefore(t0) {
    frameRequest("before", basePayload({
      mode: "flat", keep_exposure: $("beforeExposure").checked
    })).then(function (r) { return showBlob("before", $("beforeImg"), r); })
      .then(function (ok) {
        if (!ok || t0 === undefined) return;
        $("renderTime").textContent = Math.round(performance.now() - t0) + " ms";
        $("viewerMsg").classList.remove("on");
        if (S.scopesAuto) { refreshStats(); refreshScopes(); }
      })
      .catch(function (e) { showError(e); });
  }

  /* ---- bypass and the split wipe (job: compare) --------------------------
     Bypass here means the technical conversion and nothing creative: the
     engine defaults, with tone map, working space and output encode copied
     from the live config because those three ARE the conversion, not the
     grade. It is deliberately not the raw log image; a log frame next to a
     graded one compares the grade against an unwatchable picture instead of
     against the neutral one the grade started from. This mirrors
     server.py's flat_config exactly, function for function, so the GPU's
     bypassed picture and the server's mode=flat frame are the same picture
     rather than two nearby ones. */
  function bypassConfig() {
    // S.defaults is empty until GET /api/state lands. Returning null rather
    // than a half-built config makes every caller fall back to the server
    // path, which needs no client-side defaults at all.
    var live = cfg();
    if (!S.defaults || !S.defaults.convert || !live) return null;
    var flat = clone(S.defaults);
    // input is copied with the other three because it says what the source
    // IS, so a bypass that decoded it differently from the graded frame would
    // be two different pictures rather than a grade against its start. Guarded
    // with a fallback because a config saved before inputs existed has no
    // convert.input at all.
    ["tonemap", "working_space", "encode", "input"].forEach(function (k) {
      if (live.convert[k] !== undefined) flat.convert[k] = clone(live.convert[k]);
    });
    if ($("beforeExposure").checked) flat.convert.exposure = live.convert.exposure;
    return flat;
  }

  // What the viewer is showing right now, which is what the stats and the
  // scopes have to measure. With bypass on they must read the bypassed
  // picture: numbers taken from the graded config while the bypassed frame
  // is on screen describe a picture nobody can see, which is the one way a
  // scope can actively mislead rather than merely lag.
  function shownConfig() {
    if (!S.mask && effectiveMode() === MODE_BEFORE) {
      var b = bypassConfig();
      if (b) return b;
    }
    return cfg();
  }

  // Which element inside #beforeLayer is holding the bypassed picture. Same
  // shape as setStageLayer for the graded layer, and separate from it on
  // purpose: the graded picture can come from the server while the bypass
  // came from the GPU (a mid-render fallback does exactly that), so one
  // class cannot answer both questions.
  function setBypassSource(kind) {
    $("beforeLayer").classList.toggle("gpu", kind === "gpu");
  }

  // Grade the bypassed picture on the GPU and copy it off the shared canvas.
  // live.js presents to exactly one canvas, so the copy is what makes two
  // pictures possible at once: this runs first, #bypassCanvas keeps the
  // result, and the graded render then paints over the GPU canvas. gpu.js
  // creates its context with preserveDrawingBuffer, so drawImage off it is
  // defined rather than a race against the compositor. The source frame is
  // already in live.js's still cache (same clip, time and width as the
  // graded render), so this costs one more GPU pass and no network.
  function renderGpuBypass(bcfg) {
    return StudioLive.renderStill({
      clip: S.clip, time: S.time, width: S.width, rotation: S.rotation,
      config: bcfg, sourceWidth: clipSourceWidth(),
      resolvedInput: clipResolvedInput()
    }).then(function (r) {
      var src = $("gpuCanvas"), dst = $("bypassCanvas");
      if (dst.width !== src.width || dst.height !== src.height) {
        dst.width = src.width;
        dst.height = src.height;
      }
      dst.getContext("2d").drawImage(src, 0, 0);
      return r;
    });
  }

  // The bypass half of a split view. Resolves whatever happens: a bypass the
  // GPU cannot produce falls back to the server's own flat frame on its own
  // rather than taking the graded render down with it, because the graded
  // picture is the one the user is actually judging.
  function gpuBypassStep(want) {
    if (!want) return Promise.resolve();
    var bcfg = bypassConfig();
    if (!bcfg) { setBypassSource("server"); renderBefore(); return Promise.resolve(); }
    return renderGpuBypass(bcfg).then(function () {
      setBypassSource("gpu");
    }).catch(function () {
      setBypassSource("server");
      renderBefore();
    });
  }

  // Bypass on its own (before-only). The bypassed picture is the whole
  // viewer here, so this owns the badge, the render time and the
  // measurements that doRender's graded paths normally own.
  function renderBypassOnly(t0) {
    var bcfg = bypassConfig();
    if (bcfg && S.gpu && StudioLive.available()) {
      inflight++;
      renderGpuBypass(bcfg).then(function (r) {
        setStageLayer("gpu");
        setBypassSource("gpu");
        $("renderTime").textContent = Math.round(r.ms) + " ms (GPU bypass, "
          + Math.round(performance.now() - t0) + " ms incl. fetch)";
        $("viewerMsg").classList.remove("on");
        setRendererBadge("GPU (bypass)", r.grain);
        fitViewer();
        if (S.scopesAuto) { refreshStats(); refreshScopes(); }
      }).catch(function (e) {
        setStageLayer("still");
        setBypassSource("server");
        setRendererBadge("server bypass (GPU fallback: " + (e.message || e) + ")", false);
        renderBefore(t0);
      }).finally(function () { inflight--; });
    } else {
      setStageLayer("still");
      setBypassSource("server");
      setRendererBadge("server (bypass)", false);
      renderBefore(t0);
    }
  }

  // The wipe's split position, clamped so neither side can be dragged away
  // entirely: at 0 or 100 the viewer looks like a plain before-only or
  // after-only view with a stray handle in it, and the way back is not
  // obvious. Percentages, not pixels, because the same number drives both
  // #afterLayer's clip-path and the handle's own left, and those two are
  // measured against different boxes in pixels but the same box in percent.
  function setSplitPos(p) {
    if (!isFinite(p)) return S.splitPos;
    S.splitPos = Math.max(2, Math.min(98, p));
    $("stage").style.setProperty("--wipe", S.splitPos + "%");
    $("splitHandle").style.left = S.splitPos + "%";
    return S.splitPos;
  }

  function showError(e) {
    var m = $("viewerMsg");
    m.textContent = String(e.message || e);
    m.classList.add("on");
    $("renderTime").textContent = "error";
  }

  // shownConfig, not cfg: with bypass on the picture on screen is the
  // bypassed one, and a readout of the graded frame beside it would be a
  // measurement of something nobody can see.
  function refreshStats() {
    fetch("/api/stats", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(basePayload({ width: 640, config: shownConfig() }))
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
      postJSON("/api/scope", basePayload({
        width: 640, kind: kind, size: size, config: shownConfig()
      }), {
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
        clip: S.clip, time: t, width: 700, rotation: S.rotation, config: cfg()
      }).then(function (r) { return showBlob("sheet" + i, img, r); })
        .catch(function (e) { showError(e); });
    });
    $("renderTime").textContent = times.length + " frames";
  }

  /* ---- timeline -------------------------------------------------------- */

  /* The playhead belongs to the project (contract C4), so leaving the page at
   * 4.2 s and coming back has to land on 4.2 s. It is written on a 400 ms
   * debounce rather than per call because a scrub drag calls setTime on every
   * pointer move, and POST /api/project/time deliberately does NOT bump the
   * session revision, so this costs one small write and wakes nobody. */
  var timePushTimer = null;
  function pushProjectTime() {
    if (!S.project || !S.project.open || !S.clip) return;
    clearTimeout(timePushTimer);
    var wanted = S.time;
    timePushTimer = setTimeout(function () {
      timePushTimer = null;
      fetch("/api/project/time", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ time: wanted })
      }).then(function (r) {
        if (r.ok && S.project) S.project.time = wanted;
      }).catch(function () { /* the playhead is not worth a toast */ });
    }, 400);
  }

  /* opts.publish false means "this move came FROM the project", so writing it
   * back would be a pointless round trip (boot, a clip switch, an outside
   * agent's patch). Every real interaction leaves it on. */
  function setTime(t, opts) {
    // Every direct "jump to this time" interaction (scrub, step, playHead,
    // a thumbnail, a mark, selecting a clip) funnels through here, same as
    // scheduleRender is the one place that catches a config change during
    // Play. The live loop owns S.time while it runs (see its onFrame
    // callback below) and would just overwrite whatever this function sets
    // on its very next animation frame, so an explicit jump has to stop it
    // first or the scrub would silently snap back a few milliseconds later.
    if (S.looping || S.loopPreparing) stopLoopInternal();
    // Mode 3 owns S.time while it plays for the same reason the loop does,
    // so an explicit jump stops it rather than being overwritten a frame later.
    if (S.gpuPlaying) stopGpuPlayback(true);
    // Mode 1 (contract E3, "until Play is pressed again or the playhead is
    // scrubbed"): now that Play loops instead of stopping on its own, a
    // scrub is the other way this path ends.
    if (S.playing || S.playPreparing) stopPlayback();
    S.time = Math.max(0, Math.min(t, Math.max(0, S.duration - 1 / S.fps)));
    // Slot 1 of the multi frame viewer IS the playhead (contract E4), so it
    // is told here rather than keeping a copy that could disagree.
    if (window.Frames) window.Frames.playheadMoved(S.time);
    paintTime();
    // The proxy answers a scrub in the time of a seek instead of a decode,
    // which is what makes dragging the timeline feel like a player rather
    // than a series of stills. It is a PREVIEW: scheduleRender below still
    // asks for the exact 16-bit frame and overwrites it (see whenProxySettled).
    proxyPreview(S.time);
    scheduleRender();
    if (!opts || opts.publish !== false) pushProjectTime();
  }

  /* The one place a moved playhead is drawn. Four call sites used to write
     #timeLabel and #scrub.value and re-highlight the filmstrip by hand
     (setTime, and each of the three playback engines' per frame callbacks);
     the ruler owns all of that now (static/timeline.js), so they all come
     here instead and there is one definition of "the timeline shows this
     time". Cheap enough to call at frame rate on purpose: the label is a
     text write and the playhead itself is coalesced into one animation
     frame inside timeline.js. */
  function paintTime() {
    if (window.StudioTimeline) window.StudioTimeline.timeChanged();
  }

  /* One drag source definition for every place a timecode can be picked up:
     the timeline's own timecode readout, a mark flag on the ruler, and the
     filmstrip tiles (which frames.js and the harness still read a time off,
     even though the strip itself is no longer a pointer target: the ruler is
     one scrub surface now, see .tlstrip in style.css). The contact sheet's
     own cells cannot be one: it replaces the whole viewer while it is on
     (see #viewport.sheet in style.css), so there is no slot on screen to
     drop one onto. Its marks are the flags. */
  function makeTimeDraggable(el, t) {
    el.draggable = true;
    el.addEventListener("dragstart", function (ev) {
      if (!ev.dataTransfer) return;
      ev.dataTransfer.effectAllowed = "copy";
      ev.dataTransfer.setData("application/x-studio-time", String(t));
      ev.dataTransfer.setData("text/plain", String(t));
    });
  }

  /* Everything on the ruler that is drawn from the clip rather than from the
     playhead: the tick scale for this duration, the sixteen filmstrip tiles,
     the mark flags, the loop range. Called at exactly the two moments the
     open clip (and with it the duration, the fps and the rotation) changes.
     It used to be buildThumbs, which built the filmstrip and nothing else. */
  function rebuildTimeline() {
    if (window.StudioTimeline) window.StudioTimeline.clipChanged();
  }

  /* S.marks is still the only copy of the marked frames and the contact
     sheet still reads it unchanged (sheetTimes above). What changed is where
     they are drawn: they were a wrapping row of chips under the timeline bar
     and they are flags on the ruler now, at the time they mark, which is
     what timeline.js draws from here. */
  function drawMarks() {
    if (window.StudioTimeline) window.StudioTimeline.marksChanged();
  }

  /* Removing one mark, by its index in S.marks. It used to live inside the
     chip's own x handler; the flag on the ruler is built by timeline.js, so
     the splice has to be reachable from there, and the contact sheet has to
     be told either way because it may be showing the frame just removed. */
  function removeMark(i) {
    if (i < 0 || i >= S.marks.length) return;
    S.marks.splice(i, 1);
    drawMarks();
    if (S.sheet) renderSheet();
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
    // The two comparison buttons (job: compare) are views of the same view
    // mode the selector above shows, so they light up from it rather than
    // keeping any state of their own. Bypass reads the EFFECTIVE mode, so a
    // held space bar lights it too: it is describing what is on screen.
    // Wipe reads the latched mode, because a space-bar peek does not leave
    // wipe, and lighting a button for a mode you would return to on release
    // would say otherwise.
    $("bypassBtn").classList.toggle("active", mode === MODE_BEFORE);
    $("wipeBtn").classList.toggle("active", S.viewMode === MODE_WIPE);
    // Restores the session's split every time wipe comes back on screen,
    // and is harmless in every other mode (the handle is display: none and
    // nothing reads --wipe outside .mode-wipe).
    setSplitPos(S.splitPos);
    $("viewport").classList.toggle("sheet", S.sheet);
    // Frames 2 and 4 (contract E4). Same idiom as .sheet just above: one
    // class on #viewport swaps which box is the picture, so no view mode,
    // no wipe and no overlay had to learn that slots exist.
    $("viewport").classList.toggle("frames", framesActive());
    $("viewport").classList.toggle("withref", S.refShow && !!S.refName);
    fitViewer();
  }

  // Everything the viewer owes after the mode on screen changes, given the
  // mode that was on screen before it. Shared by the latched switch and the
  // space-bar hold, which are the same transition as far as the picture,
  // the stats and the scopes are concerned.
  function refreshForViewChange(was) {
    var now = effectiveMode();
    if (was === now) return;
    // Bypass in either direction goes through doRender: entering it skips
    // the graded render, leaving it has to put the graded picture back, and
    // both change which picture the stats and scopes are measuring. A split
    // mode with the GPU driving goes there too, because the GPU produces
    // the bypassed picture as part of the same render (gpuBypassStep).
    // Everything else only needs the server's flat frame, which is the
    // cheaper ask: a full doRender there would buy a second /api/frame for
    // a graded picture that is already on screen and has not changed.
    if (was === MODE_BEFORE || now === MODE_BEFORE
        || (needsBefore(now) && S.gpu && StudioLive.available())) {
      scheduleRender(0);
    } else if (needsBefore(now)) {
      renderBefore();
    }
  }

  function setViewMode(mode) {
    var was = effectiveMode();
    // Playback only ever shows in #afterLayer (see #stage.playing in
    // style.css); every other mode either hides that layer outright
    // (before-only) or splits the viewer in a way a single played video
    // was never built to share, so switching modes stops it rather than
    // leaving a video playing invisibly behind a mode change.
    stopAnyPlayback();
    // Same reasoning, same layer, for the GPU loop (#stage.gpu-live).
    if (S.looping || S.loopPreparing) stopLoop();
    S.viewMode = mode;
    applyViewerState();
    refreshForViewChange(was);
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
  // W is a two-state switch, not a step in Y's cycle: the point of a
  // dedicated key is that one press gets you into the wipe and the same
  // press gets you back out, from whatever mode you were in.
  function toggleWipe() {
    setViewMode(S.viewMode === MODE_WIPE ? MODE_AFTER : MODE_WIPE);
  }

  /* The zoom state lives in viewer-zoom.js (contract E4), which is the one
     writer of the transform on #stage and #framesGrid. This is the only
     question the layout has to ask it: at fit the picture elements carry a
     pixel maximum so they fit the box, and at any other zoom they are
     released to their natural size and the transform does the rest. */
  function zoomIsFit() {
    return !window.ViewerZoom || window.ViewerZoom.isFit();
  }

  function framesActive() {
    return !!(window.Frames && window.Frames.active());
  }

  function sizeLayer(img, wBox, hBox) {
    if (!zoomIsFit()) {
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

    // Frames 2 and 4 (contract E4) put a grid of slots on screen INSTEAD of
    // the stage (#viewport.frames in style.css), so the stage's own five
    // view modes have nothing to size here. Each slot gets its share of the
    // same box, through the same sizeLayer, so a slot's picture is fitted
    // exactly the way the single viewer's is.
    if (framesActive()) {
      var cells = window.Frames.cells();
      var cw = (availW - 4 * (cells.cols - 1)) / cells.cols;
      var ch = (availH - 4 * (cells.rows - 1)) / cells.rows;
      window.Frames.pictures().forEach(function (el) {
        sizeLayer(el, Math.max(40, cw), Math.max(40, ch));
      });
      zoomFollowedLayout();
      return;
    }

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
    // bypassCanvas is to the before half exactly what gpuCanvas is to the
    // after half, and it can be the picture in any before-showing mode, so
    // it needs the same half-share treatment beforeImg gets.
    sizeLayer($("bypassCanvas"), wBox, hBox);

    matchBeforeSize(mode);
    zoomFollowedLayout();
  }

  /* The layout just moved, so the zoom's own readout and its pan clamp are
     one measurement out of date. This is every case at once: a new frame at
     a different preview width, a sidebar dragged, a view mode switched, the
     frames grid turned on. Not a zoom change: viewer-zoom.js calls fitViewer
     itself when the zoom moves, and refresh() never changes the zoom, so the
     two cannot recurse into each other. */
  function zoomFollowedLayout() {
    if (window.ViewerZoom) window.ViewerZoom.refresh();
  }

  function matchBeforeSize(mode) {
    // Only the wipe overlay needs this: the bypassed picture sits inside an
    // absolutely positioned wrapper laid under the graded one, so percentage
    // sizing cannot reach the true frame size and it has to be told in
    // pixels. Left/Right and Top/Bottom already size both copies
    // independently in sizeLayer() above, from the same source dimensions,
    // so forcing a copy here would fight that instead of matching it.
    if (mode !== MODE_WIPE) return;
    // The LAYER, not #frameImg. On the GPU path frameImg is display: none
    // (see #stage.gpu-live in style.css) and measures zero, which used to
    // leave the bypassed copy unsized for exactly the renderer that now
    // produces both halves of the wipe. #afterLayer is sized by whichever
    // of its three children is visible, so it reads correctly on every path.
    var main = $("afterLayer");
    var w = main.clientWidth, h = main.clientHeight;
    if (!w) return;
    [$("beforeImg"), $("bypassCanvas")].forEach(function (el) {
      el.style.width = w + "px";
      el.style.height = h + "px";
    });
  }

  /* Fit and 100% used to be the whole of the zoom, and this function was it.
     viewer-zoom.js owns the state now (contract E4: 200%, 400%, plus and
     minus, wheel and pinch around the pointer, drag to pan), so this is one
     line into it and stays only because the F and 0 keys, and any older
     caller, name it. "one" is 100%, the name it has always had here. */
  function setZoom(mode) {
    if (!window.ViewerZoom) return;
    window.ViewerZoom.set(mode === "fit" ? "fit" : 1);
  }

  /* ---- presets --------------------------------------------------------- */

  function fillPresets(list) {
    var sel = $("presetSelect");
    var keep = sel.value;
    sel.innerHTML = "";
    // Two groups, because they behave differently and a flat list would hide
    // that: the shipped library in grade/presets is shared and read only (the
    // server answers 403 on delete), and "mine" is this account's own folder,
    // which is where every save lands. `library` comes from the server, so a
    // build where that flag is missing degrades to one "mine" group rather
    // than to a wrong claim about what is deletable.
    function group(label, rows) {
      if (!rows.length) return;
      var g = document.createElement("optgroup");
      g.label = label;
      rows.forEach(function (p) {
        var o = document.createElement("option");
        o.value = p.name;
        o.textContent = p.name + (p.look ? "  [" + p.look + "]" : "");
        o.title = p.comment || "";
        g.appendChild(o);
      });
      sel.appendChild(g);
    }
    group("library (read only)", list.filter(function (p) { return p.library; }));
    group("mine", list.filter(function (p) { return !p.library; }));
    if (keep && list.some(function (p) { return p.name === keep; })) sel.value = keep;
  }

  function loadPreset(name) {
    return api("/api/preset?name=" + encodeURIComponent(name)).then(function (j) {
      S.slots[S.active] = j.config;
      S.presetName = name;
      S.presetCfg = clone(j.config);
      // Contract G4: a preset with a real rotation moves the control to it;
      // one saved before rotation existed, or saved with auto, leaves the
      // control on whatever the project or the file tag already had it on.
      maybeAdoptConfigRotation(j.config);
      lastCommitted = snapshot();
      S.history.length = 0; S.future.length = 0;
      updateUndoButtons();
      Panels.refresh(cfg(), S.defaults);
      markStageState();
      // loadPreset resets lastCommitted directly instead of going through
      // pushHistory (loading a preset is not itself an undoable edit), so it
      // needs its own publish: an outside agent watching the session should
      // see a preset load too, not just the edits made on top of it.
      if (window.StudioSession) window.StudioSession.publish(cfg(), S.clip, S.time, "loaded preset " + name);
      // And for the same reason it needs its own autosave. Putting a preset
      // on a clip is a change to that clip's grade, and a user who loads a
      // look and then touches nothing else still expects it to be there when
      // they come back. grades.js ignores this while it is itself loading a
      // grade, so the boot sequence cannot save the starting preset over one.
      if (window.StudioGrades) window.StudioGrades.commit(S.clip, cfg());
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

  /* The other half of the session.js contract, and the single door every
   * config that did not come from a control in this page walks through: an
   * outside agent's `cinegrade session patch`, an Undo or a Redo, a checkout
   * or a fork from the History panel.
   *
   * It never publishes. Whatever produced this config is already the
   * server's HEAD, and re-publishing it would at best be a no-op commit and
   * at worst a race with whatever the user does next. session.js's own
   * `applying` flag guards the same thing from the other side.
   *
   * The rest of the project state (time, rotation, HEAD, the Undo and Redo
   * buttons) moves with the config: an agent that checked out an older
   * commit moved all of it, and showing the new picture under the old
   * commit id would be exactly the "who edited what" confusion this arc is
   * here to end. */
  window.applyExternalConfig = function (config, state) {
    if (config) applyServerConfig(config);
    if (state) {
      if (typeof state.time === "number" && Math.abs(state.time - S.time) > 1e-6
          && !playbackActive()) {
        setTime(state.time, { publish: false });
      }
      if (state.rotation && state.rotation !== S.rotation) {
        applyRotation(state.rotation);
      }
    }
    refreshProject();
    toast("config updated externally" + (state && state.by ? " (" + state.by + ")" : ""));
  };

  // grades.js has no toast of its own and should not grow one: this is the
  // page's single notification surface, borrowed rather than duplicated.
  window.studioToast = function (msg, bad) { toast(msg, bad); };

  /* The grades.js contract, all that is left of it after contract C4: the
   * copy-grade picker fetched another clip's saved grade and this puts it on
   * this clip. Clip switching does NOT come through here any more (a clip
   * switch is POST /api/project/open, see selectClip), so there is no per
   * clip undo stack to swap either: the history is the project's now.
   *
   * This one DOES publish, because copying a grade onto a clip is a real
   * change to that clip, and the server's own /api/grade/copy only writes
   * the grades row. The publish is what turns it into a commit you can
   * undo. */
  window.applyClipGrade = function (config, key, prevKey) {
    applyServerConfig(config ? config : S.defaults);
    if (window.StudioSession) {
      window.StudioSession.publish(cfg(), S.clip, S.time, "copied a grade from another clip");
    }
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

  // Renders list (contract E3, moved into the render dialog): two actions
  // per row rather than one whole-row click, since "open" (load the file
  // back in as the source clip, through the same /api/open the file browser
  // already uses) and "reveal" (Finder, this machine only) are both things
  // a finished render is for and neither should have to guess at the other.
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
      var openA = document.createElement("span");
      openA.className = "rowaction"; openA.textContent = "open";
      openA.title = "load this render into the viewer as the source clip";
      openA.addEventListener("click", function () {
        closeRenderDialog();
        openBrowseFile(r.path);
      });
      var revealA = document.createElement("span");
      revealA.className = "rowaction"; revealA.textContent = "reveal";
      revealA.title = "reveal in Finder (this machine only)";
      revealA.addEventListener("click", function () {
        postJSON("/api/reveal", { path: r.path });
      });
      row.appendChild(n); row.appendChild(sp); row.appendChild(x);
      row.appendChild(openA); row.appendChild(revealA);
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
        // A rectangle belongs to the reference it was drawn on, so switching
        // references swaps in that one's own rectangles (usually none) rather
        // than reusing fractions from a different picture, which would match
        // the wrong area with nothing on screen to say so.
        applyStoredCrops();
        host.querySelectorAll("img").forEach(function (o) { o.classList.remove("active"); });
        im.classList.add("active");
        applyViewerState();
      });
      host.appendChild(im);
    });
  }

  /* ---- the picked rectangles (contract C7) --------------------------------
     "i need to be able to use the rectangle mask tool to pick what i want
     before I use match reference" (founder, 2026-09-05).

     Two rectangles: one on the REFERENCE in this pane, one on the FRAME in
     the viewer. Each is [x0, y0, x1, y1] as fractions of its own picture
     (0 to 1, top left origin), which is what makes it survive a pane
     resize, a preview width change and a rotation: a fraction of the image
     means the same region at every size the image is ever drawn at.

     They live on the PROJECT, not in localStorage, keyed by the reference's
     own name under extras.match_crops:

         {"IMG_2570.PNG": {"ref": [..], "frame": [..]}}

     so they come back after a reload, follow the clip rather than the
     browser, and an agent can read exactly what the person picked out of
     GET /api/project without asking. Each reference gets one rectangle of
     each kind, which is the whole model: "match this part of this picture
     against this part of my shot".

     drawRefCropBox always measures refImg's OWN rendered rect, not
     refImgWrap's: refImgWrap centres the img with max-height/max-width:100%
     (style.css), so a reference whose aspect ratio does not match the wrap
     is letterboxed on one axis, and a box positioned against the wrap's box
     would drift off the picture on that axis.

     The frame rectangle is NOT drawn here: window-editor.js draws it into
     the SVG overlay it already owns, through WindowEditor.setPick, so it
     reuses that file's picture rect, pointer capture and resize observers
     instead of growing a second overlay with its own copy of all three.
     Nothing about the selected layer's power window is touched by picking,
     and switching the pick off puts the window overlay back as it was. */

  // Which edges each handle moves. Same table as window-editor.js's
  // PICK_EDGES, and it has to stay the same: the two rectangles are one
  // feature and a corner that resizes differently in the two panes would be
  // a bug the user feels before they can name it.
  var CROP_EDGES = {
    nw: { x0: 1, y0: 1 }, n: { y0: 1 }, ne: { x1: 1, y0: 1 },
    e: { x1: 1 }, se: { x1: 1, y1: 1 }, s: { y1: 1 },
    sw: { x0: 1, y1: 1 }, w: { x0: 1 }
  };
  // A little over the 1% of an axis the matcher refuses outright, so a
  // handle dragged past itself stops at something that can still be
  // measured rather than at an error.
  var MIN_CROP = 0.02;

  function clamp01(v) { return v < 0 ? 0 : (v > 1 ? 1 : v); }

  function normCrop(b) {
    var x0 = Math.min(b[0], b[2]), x1 = Math.max(b[0], b[2]);
    var y0 = Math.min(b[1], b[3]), y1 = Math.max(b[1], b[3]);
    var c;
    if (x1 - x0 < MIN_CROP) {
      c = (x0 + x1) / 2;
      x0 = clamp01(Math.min(c - MIN_CROP / 2, 1 - MIN_CROP)); x1 = x0 + MIN_CROP;
    }
    if (y1 - y0 < MIN_CROP) {
      c = (y0 + y1) / 2;
      y0 = clamp01(Math.min(c - MIN_CROP / 2, 1 - MIN_CROP)); y1 = y0 + MIN_CROP;
    }
    return [clamp01(x0), clamp01(y0), clamp01(x1), clamp01(y1)];
  }

  // Anything stored on the server has been through JSON and may have been
  // written by an agent, so it is checked rather than trusted: four finite
  // numbers or nothing.
  function validCrop(v) {
    return Array.isArray(v) && v.length === 4 && v.every(function (n) {
      return typeof n === "number" && isFinite(n);
    });
  }

  function round4(v) { return Math.round(v * 10000) / 10000; }

  function cropHandles() {
    var box = $("refCropBox");
    if (box.firstChild) return;
    Object.keys(CROP_EDGES).forEach(function (role) {
      var h = document.createElement("div");
      h.className = "crophandle";
      h.dataset.role = role;
      box.appendChild(h);
    });
  }

  function drawRefCropBox() {
    var box = $("refCropBox");
    var f = S.refCrop;
    if (!f) { box.classList.remove("on"); return; }
    var ir = $("refImg").getBoundingClientRect();
    var wr = $("refImgWrap").getBoundingClientRect();
    if (!ir.width || !ir.height) { box.classList.remove("on"); return; }
    var x = ir.left - wr.left + f[0] * ir.width;
    var y = ir.top - wr.top + f[1] * ir.height;
    var w = (f[2] - f[0]) * ir.width;
    var h = (f[3] - f[1]) * ir.height;
    box.style.left = x + "px";
    box.style.top = y + "px";
    box.style.width = w + "px";
    box.style.height = h + "px";
    cropHandles();
    var at = {
      nw: [0, 0], n: [w / 2, 0], ne: [w, 0], e: [w, h / 2],
      se: [w, h], s: [w / 2, h], sw: [0, h], w: [0, h / 2]
    };
    box.querySelectorAll(".crophandle").forEach(function (el) {
      var p = at[el.dataset.role];
      el.style.left = p[0] + "px";
      el.style.top = p[1] + "px";
    });
    box.classList.add("on");
  }

  function cropText(label, box) {
    if (!box) return label + " whole image";
    var pct = Math.round(100 * (box[2] - box[0]) * (box[3] - box[1]));
    return label + " " + box.map(function (v) { return v.toFixed(2); }).join(" ")
      + " (" + pct + "% of the image)";
  }

  function updateCropUi() {
    var has = !!S.refName;
    $("pickRefBtn").disabled = !has;
    $("pickFrameBtn").disabled = !has;
    $("clearRefCropBtn").disabled = !S.refCrop;
    $("clearFrameCropBtn").disabled = !S.frameCrop;
    ["pickRefBtn", "pickFrameBtn"].forEach(function (id, i) {
      var on = i === 0 ? S.pickRef : S.pickFrame;
      $(id).classList.toggle("active", !!on);
      $(id).setAttribute("aria-pressed", on ? "true" : "false");
    });
    $("refImgWrap").classList.toggle("picking", !!S.pickRef);
    $("matchCropReadout").textContent = has
      ? cropText("reference", S.refCrop) + "\n" + cropText("frame", S.frameCrop)
      : "";
  }

  /* Both rectangles for the current reference, written to the project in one
     call. Called once per drag, on release, never per pointermove: the live
     rectangle is already on screen, and a POST per frame of a drag would be
     a commit storm for something nobody is reading mid drag. */
  /* One named value on the open clip's project. Shared by the picked match
     rectangles (contract C7) and the frame slot times (contract E4), which
     want exactly the same write and the same recovery. `what` names the
     thing in the message a failure produces, so a toast still says which
     feature could not save. */
  function saveProjectExtra(name, value, what) {
    if (!S.clip) return Promise.resolve();
    var body = { clip: S.clip, name: name, value: value };
    return postJSON("/api/project/extra", body).then(function (r) {
      if (r.ok) return r;
      // A project row only exists once something has opened it. Opening this
      // clip's project is what the app does on a clip switch anyway, so this
      // creates it and retries rather than telling the user their rectangle
      // could not be saved for a reason they cannot act on.
      return postJSON("/api/project/open", { clip: S.clip })
        .then(function () { return postJSON("/api/project/extra", body); });
    }).then(function (r) {
      if (!r.ok) {
        return r.json().catch(function () { return {}; }).then(function (j) {
          throw new Error(j.error || ("HTTP " + r.status));
        });
      }
      return r;
    }).catch(function (e) {
      toast(what + " is on screen but was not saved: " + e.message, true);
    });
  }

  function saveMatchCrops() {
    if (!S.refName || !S.clip) return Promise.resolve();
    var entry = {};
    if (S.refCrop) entry.ref = S.refCrop.map(round4);
    if (S.frameCrop) entry.frame = S.frameCrop.map(round4);
    if (entry.ref || entry.frame) S.matchCrops[S.refName] = entry;
    else delete S.matchCrops[S.refName];
    return saveProjectExtra("match_crops", S.matchCrops, "the rectangle");
  }

  /* The stored rectangles for whichever reference is selected now. Called on
     boot, on a clip switch and whenever a different reference is clicked. */
  function applyStoredCrops() {
    var e = (S.refName && S.matchCrops[S.refName]) || {};
    S.refCrop = validCrop(e.ref) ? normCrop(e.ref.slice()) : null;
    S.frameCrop = validCrop(e.frame) ? normCrop(e.frame.slice()) : null;
    drawRefCropBox();
    if (S.pickFrame && window.WindowEditor) {
      window.WindowEditor.setPick({ crop: S.frameCrop, onChange: onFramePick });
    }
    updateCropUi();
  }

  /* One read of the clip's project extras, shared by everything that keeps
     something there: the picked match rectangles (contract C7) and the
     frame slot times (contract E4). One request rather than one per
     feature, because they all want the same document. */
  function loadProjectExtras() {
    if (!S.clip) return Promise.resolve();
    var clip = S.clip;
    return api("/api/project?clip=" + encodeURIComponent(clip))
      .then(function (p) { return (p && p.extras) || {}; })
      .catch(function () {
        // No project for this clip yet means nothing stored for it yet. Not
        // an error, and not worth a toast on every boot.
        return {};
      })
      .then(function (extras) {
        if (S.clip !== clip) return;         // a faster clip switch won
        var bag = extras.match_crops;
        S.matchCrops = (bag && typeof bag === "object" && !Array.isArray(bag))
          ? bag : {};
        applyStoredCrops();
        // secs (contract E3): a stored number restores that project's play
        // length, anything else (never saved, or saved as null for "all")
        // leaves the field on its own "all" placeholder.
        var secs = extras.play_secs;
        $("playDur").value = (typeof secs === "number" && isFinite(secs) && secs > 0)
          ? String(secs) : "";
        // That field is hidden now and the loop range on the ruler is what
        // shows its value, so the ruler has to be told a restored one landed.
        // rangeChanged, not clipChanged: the strip and the ruler were built
        // when the clip changed, and rebuilding them here would put another
        // 16 thumbnail requests in front of whatever the page is loading.
        if (window.StudioTimeline) window.StudioTimeline.rangeChanged();
        if (window.Frames) window.Frames.clipChanged(clip, extras);
      });
  }

  function setRefCrop(box, commit) {
    S.refCrop = box;
    drawRefCropBox();
    updateCropUi();
    if (commit) saveMatchCrops();
  }

  function onFramePick(box, commit) {
    S.frameCrop = box;
    updateCropUi();
    if (commit) saveMatchCrops();
  }

  function setPickFrame(on) {
    S.pickFrame = !!on && !!S.refName;
    if (window.WindowEditor) {
      if (S.pickFrame) {
        window.WindowEditor.setPick({ crop: S.frameCrop, onChange: onFramePick });
      } else {
        window.WindowEditor.setPick(null);
      }
    }
    updateCropUi();
  }

  // Given a mouse event and the reference image's current rendered rect,
  // the [x, y] fraction of the image under the cursor, clamped so a drag
  // that runs past the image edge (or off the whole window) still ends
  // exactly at that edge instead of producing an out of range fraction.
  function refPointFrac(ev, r) {
    return [clamp01((ev.clientX - r.left) / r.width),
            clamp01((ev.clientY - r.top) / r.height)];
  }

  /* One drag, either kind: a fresh rectangle dragged out on the picture, or
     one edge or corner moved by a handle. Both compute from the pointer's
     CURRENT position against the rectangle as it was at mousedown, never
     accumulated frame to frame, for the same reason window-editor.js does:
     an accumulated drag drifts and a dropped mousemove loses the difference
     for good. */
  function startRefDrag(ev, role) {
    var r = $("refImg").getBoundingClientRect();
    if (!r.width || !r.height) return;
    ev.preventDefault();
    var start = refPointFrac(ev, r);
    var from = S.refCrop ? S.refCrop.slice() : null;
    var moved = false;
    function at(e) {
      var p = refPointFrac(e, r);
      if (role === "new" || !from) {
        return normCrop([start[0], start[1], p[0], p[1]]);
      }
      var edges = CROP_EDGES[role] || {};
      var b = from.slice();
      if (edges.x0) b[0] = p[0];
      if (edges.x1) b[2] = p[0];
      if (edges.y0) b[1] = p[1];
      if (edges.y1) b[3] = p[1];
      return normCrop(b);
    }
    function move(e) { moved = true; setRefCrop(at(e), false); }
    function up(e) {
      document.removeEventListener("mousemove", move);
      document.removeEventListener("mouseup", up);
      // A plain click with no mousemove between down and up is not a
      // rectangle: whatever was drawn stays exactly as it was, and nothing
      // is written to the project.
      if (moved) setRefCrop(at(e), true);
    }
    document.addEventListener("mousemove", move);
    document.addEventListener("mouseup", up);
  }

  function bindRefCrop() {
    $("refImgWrap").addEventListener("mousedown", function (ev) {
      if (!S.refName || !S.pickRef) return;
      var role = (ev.target && ev.target.dataset && ev.target.dataset.role) || "new";
      startRefDrag(ev, role);
    });
    $("pickRefBtn").addEventListener("click", function () {
      S.pickRef = !S.pickRef && !!S.refName;
      updateCropUi();
    });
    $("pickFrameBtn").addEventListener("click", function () {
      setPickFrame(!S.pickFrame);
    });
    $("clearRefCropBtn").addEventListener("click", function () {
      setRefCrop(null, true);
    });
    $("clearFrameCropBtn").addEventListener("click", function () {
      S.frameCrop = null;
      if (S.pickFrame && window.WindowEditor) {
        window.WindowEditor.setPick({ crop: null, onChange: onFramePick });
      }
      updateCropUi();
      saveMatchCrops();
    });
  }

  /* ---- match reference ---------------------------------------------------
     Fits a transform that moves this frame's colour statistics toward the
     currently shown reference image; it does not extract or copy a LUT (a
     JPEG or PNG has none in it to extract). Full contract, every argument
     and failure mode: grade/tools/MATCH-REF-INTEGRATION.md.

     Method, strength and luma preserve are real controls now (matchMethod,
     matchStrength, matchLumaPreserve in the match panel), because
     match_ref.py actually takes all three (METHODS is "reinhard" or
     "histogram"). They only take effect on the next click of Match to
     reference, not live, so this still calls the endpoint once per click
     rather than refitting per drag step. Once a cube is baked, look.mix
     (already a slider in Parameters, schema.js) still gives a zero cost
     strength change on top of it. The three choices persist in
     localStorage under studio.match.* (bindMatchControls) so they survive
     a reload. */

  var MATCH_METHOD_DESC = {
    // Wording drawn from match_ref.py's own module docstring, not invented.
    reinhard: "Mean and standard deviation transfer per channel in Lab " +
      "space: a global affine move, robust and rarely catastrophic, but " +
      "the weaker of the two.",
    histogram: "Per channel cumulative histogram matching in display code: " +
      "monotonic per channel so it cannot invert a hue, stronger, and " +
      "slope limited and smoothed here so it does not posterise."
  };

  var MATCH_KEY = {
    method: "studio.match.method",
    strength: "studio.match.strength",
    luma: "studio.match.luma_preserve"
  };

  function bindMatchControls() {
    var methodSel = $("matchMethod");
    var strengthInp = $("matchStrength");
    var strengthVal = $("matchStrengthValue");
    var lumaChk = $("matchLumaPreserve");
    var descEl = $("matchMethodDesc");

    function paintDesc() { descEl.textContent = MATCH_METHOD_DESC[methodSel.value] || ""; }
    function paintStrength() { strengthVal.textContent = Number(strengthInp.value).toFixed(2); }

    var storedMethod = localStorage.getItem(MATCH_KEY.method);
    if (storedMethod === "reinhard" || storedMethod === "histogram") methodSel.value = storedMethod;
    var storedStrength = parseFloat(localStorage.getItem(MATCH_KEY.strength));
    if (!Number.isNaN(storedStrength)) {
      strengthInp.value = String(Math.min(1, Math.max(0, storedStrength)));
    }
    var storedLuma = localStorage.getItem(MATCH_KEY.luma);
    if (storedLuma === "0" || storedLuma === "1") lumaChk.checked = storedLuma === "1";

    paintDesc();
    paintStrength();

    methodSel.addEventListener("change", function () {
      localStorage.setItem(MATCH_KEY.method, methodSel.value);
      paintDesc();
    });
    strengthInp.addEventListener("input", function () {
      paintStrength();
      localStorage.setItem(MATCH_KEY.strength, strengthInp.value);
    });
    lumaChk.addEventListener("change", function () {
      localStorage.setItem(MATCH_KEY.luma, lumaChk.checked ? "1" : "0");
    });
  }

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
      rotation: S.rotation,
      config: cfg(),
      method: $("matchMethod").value,
      strength: parseFloat($("matchStrength").value),
      luma_preserve: $("matchLumaPreserve").checked
    };
    // The picked rectangles (contract C7), always both, always explicit.
    // A null is the tab saying "whole image, and do not fall back to what
    // the project has saved": the tab is the thing that wrote those saved
    // rectangles and is showing the user which ones are in force right now,
    // so the request must say exactly what the readout says. The server's
    // fall back to the stored rectangle is for callers with no screen (the
    // CLI, an agent), which send neither field.
    body.ref_crop = S.refCrop || null;
    body.frame_crop = S.frameCrop || null;

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

    /* Which part of which picture this cube was actually fitted from
       (contract C7), read back from the server's answer rather than from
       the controls, so it cannot claim a rectangle the fit did not use.
       result.crops.ref / .frame are null for a whole image match, and the
       *_source fields say whether the rectangle came from this request or
       from what the project had saved. */
    var crops = result.crops || {};
    var line = document.createElement("div");
    line.className = "mono muted";
    line.textContent = "measured: " + ["ref", "frame"].map(function (k) {
      var p = crops[k];
      var label = k === "ref" ? "reference" : "frame";
      if (!p || !p.box) return label + " whole image";
      var src = crops[k + "_source"] === "project" ? ", saved on the project" : "";
      return label + " " + p.box.map(function (v) { return v.toFixed(2); }).join(" ")
        + " (" + p.area_pct + "% of the image" + src + ")";
    }).join(", ");
    host.appendChild(line);

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

    // Same shape as match_ref.py's own print_report's method line: reads
    // back what the server actually ran (result.method/.strength/
    // .luma_preserve), not what the controls happened to show at click
    // time, so this cannot drift from the cube that was actually written.
    if (result.method) {
      var mi = document.createElement("div");
      mi.className = "mono muted";
      mi.textContent = "method " + result.method + " strength=" + result.strength +
        " luma_preserve=" + result.luma_preserve;
      host.appendChild(mi);
    }

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

  /* ---- project (contract C4) -------------------------------------------
     A project is one clip's whole state on the server: its rotation, its
     playhead, its loaded preset name, its extras and its commit tree. It is
     keyed by the clip's content, so it survives a rename, and it is shared by
     every account, so an agent and a person looking at the same clip see one
     history.

     This block is the tab's whole side of it, and the rule it follows is
     short: the server owns the project, this page reflects it. Every function
     here takes a project record the server just handed back and puts it on
     screen. None of them writes a config back out. */

  /* Put a project record on screen.
     opts.config  also load HEAD's config into the active slot (a clip switch,
                  an undo, a checkout). Left off for a plain refresh, where
                  the config on screen is already the one the user is editing
                  and replacing it would fight their typing.
     opts.select  also switch the viewer to the project's clip and put the
                  playhead where the project left it (boot and clip switch). */
  function applyProject(proj, opts) {
    opts = opts || {};
    if (!proj || !proj.open) { S.project = null; updateUndoButtons(); return; }
    var clipChanged = proj.name && proj.name !== S.clip;
    S.project = proj;
    if (proj.rotation && proj.rotation !== S.rotation) {
      applyRotation(proj.rotation, { redraw: !opts.select });
    } else {
      syncRotationButtons();
    }
    if (opts.select && proj.name) {
      if (clipChanged || !S.clip) {
        // The playhead the project was left at, set BEFORE showClip so its own
        // clamp to this clip's duration is the only thing that moves it.
        S.time = typeof proj.time === "number" ? proj.time : 0;
        showClip(proj.name);
      }
      if (!clipChanged && typeof proj.time === "number"
          && Math.abs(proj.time - S.time) > 1e-6 && !playbackActive()) {
        setTime(proj.time, { publish: false });
      }
    }
    if (opts.config && proj.config) applyServerConfig(proj.config);
    if (proj.preset) S.presetName = proj.preset;
    updateUndoButtons();
    updateHeadLabel();
  }

  /* Re read the open project WITHOUT touching the config on screen. Called
     whenever the server says the revision moved (our own commit or somebody
     else's), because a commit changes what Undo and Redo can do and which
     short id the page should be showing. One small JSON GET; the clip probe
     it reads is cached server side. */
  var projectRefreshing = false;
  function refreshProject() {
    if (projectRefreshing) return Promise.resolve(S.project);
    projectRefreshing = true;
    return api("/api/project").then(function (proj) {
      applyProject(proj, {});
      return proj;
    }).catch(function () {
      return S.project;
    }).then(function (p) { projectRefreshing = false; return p; });
  }

  // The short commit id in the save-state pill, so "is my work saved" has a
  // literal answer: the id of the commit the picture on screen is.
  function updateHeadLabel() {
    var el = $("gradeSaveState");
    if (!el) return;
    var head = S.project && S.project.open && S.project.head;
    el.textContent = head ? "committed " + head : "";
    el.style.visibility = head ? "visible" : "hidden";
    el.style.color = "var(--text-dim)";
    el.style.borderColor = "var(--text-dim)";
    el.title = head
      ? "Every committed change is a commit on this clip's project. This is the "
        + "one the picture on screen is. Undo and the History panel move it."
      : "";
  }

  /* ---- rotation --------------------------------------------------------- */

  // The five the engine, the server and the CLI all spell the same way.
  var ROTATIONS = ["auto", "0", "90", "180", "270"];

  // Put a rotation on screen. Does not talk to the server: setRotation below
  // does that and calls this with the answer, and applyProject calls it with
  // whatever the server already said.
  function applyRotation(value, opts) {
    opts = opts || {};
    var next = String(value || "auto");
    if (ROTATIONS.indexOf(next) < 0) next = "auto";
    var changed = next !== S.rotation;
    S.rotation = next;
    syncRotationButtons();
    if (!changed || opts.redraw === false) return;
    // Rotation changes the clip's own upright dimensions, which the loop's
    // decoded range and the proxy's encode were both made at, so anything
    // already moving has to stop rather than keep showing frames from the
    // orientation that was current when it was prepared.
    if (S.looping || S.loopPreparing) stopLoop();
    stopAnyPlayback();
    StudioLive.stopProxy();
    S.proxyReady = false;
    S.proxyDesc = null;
    var entry = S.state && S.state.clips.filter(function (c) { return c.name === S.clip; })[0];
    if (entry) drawClipInfo(entry);
    rebuildTimeline();
    warmProxy();
    scheduleRender(0);
  }

  // Contract G4: a loaded preset or grade that carries a real rotation
  // moves the control to it, because the config just replaced whatever was
  // on screen and the control has to describe what is actually about to
  // render. "auto" (a preset that never set one, or an old preset with no
  // rotation key at all, filled in by the server's own defaults merge)
  // leaves the control alone: the project's own rotation, or the file's
  // tag, keeps deciding, exactly as it did before this config carried an
  // opinion of its own.
  function maybeAdoptConfigRotation(config) {
    var r = config && config.rotation;
    if (!r) return;
    r = String(r);
    if (r === "auto" || ROTATIONS.indexOf(r) < 0) return;
    if (r !== S.rotation) applyRotation(r);
  }

  function syncRotationButtons() {
    var host = $("rotSeg");
    if (!host) return;
    var btns = host.querySelectorAll("button[data-rotation]");
    for (var i = 0; i < btns.length; i++) {
      var on = btns[i].getAttribute("data-rotation") === S.rotation;
      btns[i].classList.toggle("active", on);
      btns[i].setAttribute("aria-pressed", on ? "true" : "false");
    }
  }

  // The only writer of the project's rotation. The server answers with the
  // whole project record, so the picture, the dimensions readout and the
  // History panel all move off one response.
  //
  // Contract G4: rotation is part of a saved grade now, not just the
  // project's own separate field, so this also stamps config.rotation on
  // the live config and commits it exactly like a slider release or a
  // checkbox would (pushHistory, the same function every other control in
  // this file calls). setRotation is the only caller of pushHistory that
  // is not reachable from applyProject's own remote-sync path (a long poll
  // picking up somebody else's change never calls setRotation, only
  // applyRotation directly), so this cannot echo somebody else's rotation
  // back at them as a commit of our own.
  function setRotation(value) {
    if (!S.project || !S.project.open) {
      applyRotation(value);
      if (cfg()) { cfg().rotation = String(value); updateModified(); pushHistory(); }
      return Promise.resolve();
    }
    if (value === S.rotation) return Promise.resolve();
    return api("/api/project/rotation", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rotation: String(value), by: "studio" })
    }).then(function (proj) {
      applyProject(proj, {});
      if (cfg()) { cfg().rotation = String(value); updateModified(); pushHistory(); }
    }).catch(function (e) {
      toast(e.message || String(e), true);
      syncRotationButtons();
    });
  }

  function bindRotationControl() {
    var host = $("rotSeg");
    if (!host) return;
    host.addEventListener("click", function (ev) {
      var btn = ev.target.closest ? ev.target.closest("button[data-rotation]") : null;
      if (!btn) return;
      setRotation(btn.getAttribute("data-rotation"));
    });
    syncRotationButtons();
  }

  /* ---- clip ------------------------------------------------------------ */

  /* Opening a clip IS opening its project (contract C4). One POST does the
   * whole thing: the server resolves the clip to its content key, creates the
   * project on first sight (root commit from the old saved grade if there was
   * one, else the engine defaults), makes it this account's open project, and
   * answers with rotation, playhead, HEAD and HEAD's config.
   *
   * So the tab publishes NOTHING on a clip switch. The old path fetched the
   * saved grade, or stamped the engine defaults when there was none, and
   * published either one straight back out, which is how a clip switch could
   * land on top of an edit an agent had just made.
   *
   * Errors are shown rather than swallowed: a clip that has gone off disk is
   * the usual cause, and silently staying on the previous one would leave the
   * clip list and the picture disagreeing. */
  function selectClip(name) {
    if (!name) return Promise.resolve(null);
    return api("/api/project/open", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ clip: name, by: "studio" })
    }).then(function (proj) {
      applyProject(proj, { config: true, select: true });
      return proj;
    }).catch(function (e) {
      toast("could not open " + name + ": " + (e.message || e), true);
      return null;
    });
  }

  /* Everything that follows from "this clip is the one on screen now", with
   * no server call of its own. Split out of selectClip so applyProject can
   * run it for a project restored at boot as well as for a clip the user just
   * clicked. */
  function showClip(name) {
    S.clip = name;
    var entry = S.state.clips.filter(function (c) { return c.name === name; })[0];
    if (!entry) return;
    S.duration = entry.duration || 0;
    S.fps = entry.fps || 24;
    drawClipInfo(entry);
    // Redraws whichever folder is currently browsed so its row for this clip
    // (if it has one) picks up the .active highlight, mirroring what
    // fillClipList used to do for the old, unfiltered, footage-only list.
    // Guarded on browsePath because selectClip also runs at boot, before the
    // merged tab's first lazy load has populated S.browseDirs/browseFiles.
    if (S.browsePath) renderFolderClips();
    // The library list has its own row for this clip when it is showing the
    // folder the clip lives in; this moves that row's highlight, the same
    // thing renderFolderClips just did for the anywhere browser's list.
    if (window.StudioFiles) window.StudioFiles.clipChanged(name);
    rebuildTimeline();
    // The previous clip's proxy is a decoder plus tens of MB of video held
    // for a clip nobody is looking at any more; let it go before asking for
    // the next one. warmProxy then starts this clip's encode in the
    // background, so the first press of Play does not have to wait for it.
    StudioLive.stopProxy();
    S.proxyReady = false;
    S.proxyDesc = null;
    setTime(Math.min(S.time, Math.max(0, S.duration - 0.1)), { publish: false });
    warmProxy();
    scheduleRender(0);
    // grades.js keeps the copy-from picker, and the picker needs to know which
    // clip it must not offer to copy from. It no longer fetches or applies
    // anything on a clip switch: the project's HEAD is the grade now.
    if (window.StudioGrades) window.StudioGrades.clipChanged(name, entry.key);
    // The picked match rectangles (contract C7) and the frame slot times
    // (contract E4) belong to the clip's project, so they are re-read here
    // for the same reason the grade is: a rectangle drawn against the
    // previous shot, or a slot pointing 8 s into a 4 s clip, means nothing
    // on this one.
    loadProjectExtras();
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
    var cur = clipDims(entry) || entry.autorotate;
    kv("file", entry.name);
    kv("graph sees", cur.width + " x " + cur.height + "  (rotation " + S.rotation + ")");
    kv("auto", entry.autorotate.width + " x " + entry.autorotate.height);
    kv("no rotation", entry.raw.width + " x " + entry.raw.height);
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
      + "frame is sideways, pick another rotation above the clip list and look "
      + "again. It is saved on this clip's project, so a render started from "
      + "the command line comes out the same way up. Getting it wrong also "
      + "puts the blur and mask geometry on the wrong axis, so check it "
      + "before a long render.";
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

  // Small local twin of panels.js's useIcon: both just forward to
  // StudioIcons.render (studio/static/icons.js), which resolves the name
  // against window.HUGEICONS (studio/static/vendor/hugeicons.js). Kept as
  // its own function, not a shared export, for the same reason as before:
  // this file has no other reason to know panels.js exists.
  function browseIcon(name, cls) {
    return StudioIcons.render(name, cls);
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
  // know about shows up later; Folder01Icon is close enough a fallback for
  // any plain-directory shortcut. Values are @hugeicons/core-free-icons
  // export names (see studio/static/vendor/hugeicons.js).
  var QUICKJUMP_ICONS = { home: "Home03Icon", workspace: "Folder01Icon", footage: "PlaySquareIcon" };

  function renderQuickJumps(roots) {
    var host = $("browseQuick");
    host.innerHTML = "";
    (roots || []).forEach(function (r) {
      var key = (r.label || r.name || "").toLowerCase();
      var b = document.createElement("button");
      b.type = "button";
      b.className = "quickjump";
      b.appendChild(browseIcon(QUICKJUMP_ICONS[key] || "Folder01Icon", "rowicon"));
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
      row.appendChild(browseIcon("Folder01Icon", "rowicon"));
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
      row.appendChild(browseIcon("PlaySquareIcon", "rowicon"));
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

  /* Opening a LIBRARY item (contract E2): a clip in a folder of this account's
     own library, or one in somebody else's that a share reaches. files.js hands
     over {root, path} and never a path on disk; POST /api/project/open resolves
     it, checks the read permission and registers the file under a clip name.
     The clip list is refreshed BEFORE the project is applied because showClip
     reads that clip's duration and dimensions out of S.state.clips, and a file
     in a subfolder (or in somebody else's library) has never been in it. The
     error is rethrown rather than toasted here: files.js puts a refusal in the
     pane where the click happened as well as in a toast. */
  function openLibraryClip(root, path) {
    return api("/api/project/open", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ root: root, path: path, by: "studio" })
    }).then(function (proj) {
      return api("/api/clips").then(function (j) {
        S.state.clips = j.clips || S.state.clips;
        applyProject(proj, { config: true, select: true });
        toast("opened " + (proj.name || path));
        return proj;
      });
    });
  }

  /* The same refresh-then-select for a clip that has just been uploaded into a
     library folder: it is on the server now but not yet in this page's list. */
  function selectUploadedClip(name) {
    if (!name) return Promise.resolve(null);
    return api("/api/clips").then(function (j) {
      S.state.clips = j.clips || S.state.clips;
      return selectClip(name);
    }).catch(function () { return null; });
  }

  /* ---- upload ------------------------------------------------------------
     POST /api/upload is the raw file body, not a multipart form: server.py
     reads the socket itself. XHR rather than fetch because only XHR exposes
     upload.onprogress, which is what drives #uploadClipBtn's own label while
     a file is sending -- there is no separate progress element. */

  function uploadOneClip(file) {
    return new Promise(function (resolve, reject) {
      var xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/upload");
      xhr.setRequestHeader("Content-Type", "application/octet-stream");
      // Percent-encoded: an HTTP header value has to stay on one line and
      // plain ASCII, and a picked file's name is neither guaranteed.
      xhr.setRequestHeader("X-File-Name", encodeURIComponent(file.name));
      xhr.upload.onprogress = function (e) {
        if (e.lengthComputable && e.total) {
          $("uploadClipBtn").textContent = Math.round((e.loaded / e.total) * 100) + "%";
        }
      };
      xhr.onload = function () {
        var j = {};
        try { j = JSON.parse(xhr.responseText || "{}"); } catch (err) { /* non JSON error page */ }
        if (xhr.status >= 200 && xhr.status < 300) resolve(j);
        else reject(new Error(j.error || ("upload failed: HTTP " + xhr.status)));
      };
      xhr.onerror = function () { reject(new Error("upload failed: network error")); };
      xhr.send(file);
    });
  }

  // One file at a time, not parallel: FFMPEG_SLOTS on the server is already
  // shared with every scrub and thumbnail in the room (the ffprobe check
  // that runs after each upload lands takes one too), and one at a time
  // also keeps the button's percent readout one real number instead of an
  // average across several uploads in flight.
  function uploadClipFiles(fileList) {
    // Contract E2: one Upload button now serves two views. In a library root
    // the files pane owns the upload, because it knows which folder is on
    // screen and it draws a progress row per file with the server's own
    // refusal on it; in the anywhere browser this function is unchanged.
    if (window.StudioFiles && window.StudioFiles.handleUpload
        && window.StudioFiles.handleUpload(fileList)) return;
    var files = Array.prototype.slice.call(fileList);
    var btn = $("uploadClipBtn");
    btn.disabled = true;
    function next() {
      if (!files.length) {
        btn.disabled = false;
        btn.textContent = "Upload";
        return;
      }
      var file = files.shift();
      btn.textContent = "0%";
      uploadOneClip(file).then(function (j) {
        S.state.clips = j.clips;
        // Selects the clip just uploaded, same as opening one from the
        // browser does; with several files queued the last one to finish
        // is left selected.
        selectClip(j.name);
        toast("uploaded " + j.name);
        next();
      }).catch(function (err) {
        toast(file.name + ": " + (err.message || err), true);
        next();
      });
    }
    next();
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
    // A "fold" control (Tetra, Window, Key, Correct) is a group, not a field:
    // its own path/paths/wheels are all absent and the fields live one level
    // down in its own `controls` array. The walk below used to stop at that
    // first level, so it never recursed into a fold's controls and reported
    // every path inside one (slice.tetra.enabled, slice.tetra.r and friends
    // among them) as having no control at all, even though schema.js
    // declares each of them (a CHK and five "trio" controls for Tetra).
    function markControl(c) {
      if (c.path) covered[c.path.join(".")] = true;
      if (c.paths) c.paths.forEach(function (p) { covered[p.join(".")] = true; });
      if (c.wheels) c.wheels.forEach(function (w) { covered[w.path.join(".")] = true; });
      if (c.controls) c.controls.forEach(markControl);
    }
    SCHEMA.forEach(function (stage) {
      stage.controls.forEach(markControl);
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
    "<li><kbd>v</kbd> hold to see the ungraded frame, release to go back</li>",
    "<li><kbd>\\</kbd> toggle between after only and before only</li>",
    "<li><kbd>b</kbd> bypass: the technical conversion with none of the grade. ",
    "The stats and the scopes measure the bypassed picture while it is on screen</li>",
    "<li><kbd>w</kbd> split wipe: bypassed left of the handle, graded right</li>",
    "<li><kbd>y</kbd> cycle left/right, top/bottom and wipe, then back to after only</li>",
    "<li>In wipe mode, drag the blue handle to move the split. It stops 2 percent ",
    "from either edge so neither side can be dragged away entirely, and it stays ",
    "where you left it for the rest of the session</li>",
    "<li><kbd>1</kbd> <kbd>2</kbd> switch to grade slot A or B</li>",
    "<li><kbd>shift 1</kbd> <kbd>shift 2</kbd> copy the live grade into that slot</li>",
    "<li><kbd>f</kbd> fit &nbsp; <kbd>0</kbd> 100 percent</li>",
    "<li><kbd>r</kbd> show or hide the reference image beside the frame</li>",
    "<li><kbd>shift K</kbd> show the selected layer's mask</li>",
    "<li><kbd>c</kbd> contact sheet: this grade on four marked frames at once</li>",
    "<li><kbd>g</kbd> show or hide the scopes and statistics dock</li>",
    "</ul>",
    "<h4>Time</h4><ul>",
    "<li><kbd>space</kbd> or <kbd>p</kbd> play or pause from the playhead, with the ",
    "current grade, looping the range</li>",
    "<li><kbd>j</kbd> <kbd>k</kbd> <kbd>l</kbd> shuttle back, stop, shuttle forward. ",
    "Press <kbd>j</kbd> or <kbd>l</kbd> again for 2x, 4x, 8x</li>",
    "<li><kbd>,</kbd> <kbd>.</kbd> one frame back or forward &nbsp; ",
    "<kbd>shift ,</kbd> <kbd>shift .</kbd> ten frames</li>",
    "<li><kbd>home</kbd> <kbd>end</kbd> first or last frame</li>",
    "<li><kbd>m</kbd> mark this frame &nbsp; <kbd>[</kbd> <kbd>]</kbd> previous or next mark</li>",
    "<li><kbd>shift L</kbd> loop the range on the ruler, GPU-graded live</li>",
    "</ul>",
    "<h4>The timeline</h4><ul>",
    "<li>Press anywhere on the ruler to put the playhead there; drag to scrub. ",
    "The proxy frame follows the pointer, the full frame lands when you stop</li>",
    "<li>Arrow keys step a frame once the ruler has focus, <kbd>shift</kbd> ten; ",
    "a wheel or a trackpad swipe over it steps too</li>",
    "<li>Drag in the strip along the bottom to set the loop range Play repeats, ",
    "or drag its right hand handle to lengthen it. Drag it away to loop the ",
    "whole clip. Range does it from the playhead to the next mark</li>",
    "<li>Marks are flags on the ruler: click one to jump to it, or the small ",
    "circle above it to remove it. Drag a flag, or the timecode itself, onto a ",
    "frame slot to show that time there</li>",
    "</ul>",
    "<h4>Grade</h4><ul>",
    "<li><kbd>cmd Z</kbd> undo &nbsp; <kbd>shift cmd Z</kbd> redo</li>",
    "<li><kbd>cmd S</kbd> overwrite the loaded preset &nbsp; <kbd>shift cmd S</kbd> save as</li>",
    "<li><kbd>shift J</kbd> raw JSON view &nbsp; <kbd>?</kbd> this panel &nbsp; <kbd>esc</kbd> close</li>",
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

    bindRotationControl();

    /* session.js fires this on every revision this tab learns about, its own
     * commits included. A commit is what changes whether Undo and Redo can do
     * anything and which short id the page is standing on, so this is where
     * the project record is refreshed. It reads the project and never writes
     * it, and it deliberately does not touch the config: an outside config
     * arrives through applyExternalConfig, and this tab's own edits are
     * already on screen. */
    window.addEventListener("studio:session", function (ev) {
      var st = ev && ev.detail;
      var head = S.project && S.project.head;
      if (st && st.head && head && st.head === head && st.by === "studio") return;
      refreshProject();
    });

    $("previewWidth").addEventListener("change", function (e) {
      // Same reasoning: the loop's decoded frames are at the old width.
      if (S.looping || S.loopPreparing) stopLoop();
      // And the proxy was encoded at the old width, so it is now the wrong
      // size to grade next to the still path (which follows this select).
      stopAnyPlayback();
      S.width = parseInt(e.target.value, 10);
      warmProxy();
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

    // viewer. The zoom buttons (Fit, 100%, 200%, 400%, minus, plus) are
    // bound by viewer-zoom.js, which owns that state; binding them here too
    // would give one press two owners.
    $("viewMode").addEventListener("change", function (e) { setViewMode(e.target.value); });
    // Job: compare. Both buttons are the keyboard shortcuts' own functions,
    // so the button and the key can never drift apart.
    $("bypassBtn").addEventListener("click", toggleBeforeAfter);
    $("wipeBtn").addEventListener("click", toggleWipe);
    $("gpuToggle").addEventListener("click", function () {
      S.gpu = !S.gpu;
      $("gpuToggle").classList.toggle("active", S.gpu);
      // Turning the GPU on is also what makes proxy playback possible, so
      // this is the other moment worth starting that encode in the
      // background; turning it off stops a mode 3 playback that has just
      // lost the renderer it was drawing with.
      if (S.gpu) warmProxy(); else stopAnyPlayback();
      scheduleRender(0);
    });
    $("beforeExposure").addEventListener("change", function () {
      // No point re-rendering a before frame nobody is looking at. When the
      // bypassed picture is the whole viewer, or the GPU is the one making
      // it, it comes out of doRender rather than a flat frame request.
      if (!needsBefore(effectiveMode())) return;
      if (effectiveMode() === MODE_BEFORE || (S.gpu && StudioLive.available())) {
        scheduleRender(0);
      } else {
        renderBefore();
      }
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

    /* split handle (wipe mode only). Pointer events with a pointer capture,
       not mousedown plus document-level mousemove/mouseup listeners: the
       capture is what keeps a drag alive when the pointer leaves the handle
       (which it does immediately, since the handle is 14px wide and the
       drag is the full width of the frame) and what guarantees the matching
       release arrives even if the pointer ends up over another element or
       outside the window, so a drag cannot get stuck on. It also covers pen
       and touch for free, where the old mouse-only pair covered neither. */
    (function () {
      var handle = $("splitHandle");
      var dragging = false;
      function place(e) {
        // #afterLayer, not #frameImg: the graded picture can be any of three
        // elements and only the layer is the right box in all three cases.
        var r = $("afterLayer").getBoundingClientRect();
        if (!r.width) return;
        setSplitPos(((e.clientX - r.left) / r.width) * 100);
      }
      handle.addEventListener("pointerdown", function (ev) {
        ev.preventDefault();
        // A capture needs a live pointer with this id. A programmatically
        // dispatched PointerEvent (a UI test driving the handle) has no
        // such pointer and this throws, which must not take the drag down
        // with it, so the drag state is a flag of our own rather than a
        // read of hasPointerCapture: it is the same answer for a real
        // pointer and the only available one for a synthetic pointer.
        try { handle.setPointerCapture(ev.pointerId); } catch (e) { /* see above */ }
        dragging = true;
        handle.classList.add("dragging");
        place(ev);
      });
      handle.addEventListener("pointermove", function (ev) {
        if (dragging) place(ev);
      });
      function release(ev) {
        dragging = false;
        try {
          if (handle.hasPointerCapture(ev.pointerId)) handle.releasePointerCapture(ev.pointerId);
        } catch (e) { /* same as the capture above */ }
        handle.classList.remove("dragging");
      }
      handle.addEventListener("pointerup", release);
      handle.addEventListener("pointercancel", release);
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

    // timeline. The ruler itself (#scrub, its ticks, filmstrip, mark flags,
    // loop range and the pointer gesture over all of them) is
    // static/timeline.js, wired in boot() the same way frames.js is; these
    // are the buttons around it, which stay app.js's because each one is a
    // one line call into a function that already exists here.
    // Shift is ten frames, the step every editor's shifted arrow takes.
    $("stepBack").addEventListener("click", function (ev) {
      stopShuttle();
      setTime(S.time - (ev.shiftKey ? 10 : 1) / S.fps);
    });
    $("stepFwd").addEventListener("click", function (ev) {
      stopShuttle();
      setTime(S.time + (ev.shiftKey ? 10 : 1) / S.fps);
    });
    $("playHead").addEventListener("click", function () { stopShuttle(); setTime(0); });
    $("playBtn").addEventListener("click", function () {
      stopShuttle();
      if (playbackActive()) stopAnyPlayback(); else startPlayback();
    });
    // Persist per project (contract E3): "change" rather than "input", so
    // this writes once when the field is left rather than once a keystroke.
    $("playDur").addEventListener("change", function () {
      saveProjectExtra("play_secs", playDurationValue(), "the play length");
    });
    $("playerVideo").addEventListener("loadedmetadata", fitViewer);
    $("loopBtn").addEventListener("click", function () {
      if (S.looping || S.loopPreparing) stopLoop(); else startLoop();
    });
    // A quick way to point the loop at "here": the playhead to the next mark
    // after it, or two seconds if there is none, and pressing it again
    // clears the range back to the whole clip. The range itself is a band on
    // the ruler that can be dragged directly (timeline.js owns both the band
    // and this button's behaviour); this is the keyboard-free shortcut for
    // the common case, and what the old "Use marks" button did.
    $("loopMarkBtn").addEventListener("click", function () {
      if (window.StudioTimeline) window.StudioTimeline.toggleRange();
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
    $("renderDialogBtn").addEventListener("click", openRenderDialog);
    $("renderClose").addEventListener("click", closeRenderDialog);
    $("renderStart").addEventListener("input", updateRenderEstimate);
    $("renderDur").addEventListener("input", updateRenderEstimate);
    // Enter renders, Esc closes (contract E3): scoped to the dialog itself
    // so it fires before the global handler's blur-then-close Escape and
    // never reaches for a submit key inside the raw JSON textarea or any
    // other overlay's own fields.
    $("renderOverlay").addEventListener("keydown", function (ev) {
      if (ev.key === "Enter") { ev.preventDefault(); startRender(); }
      else if (ev.key === "Escape") { ev.preventDefault(); closeRenderDialog(); }
    });

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

    $("uploadClipBtn").addEventListener("click", function () { $("uploadClipFile").click(); });
    $("uploadClipFile").addEventListener("change", function (e) {
      var files = e.target.files;
      if (files && files.length) uploadClipFiles(files);
      e.target.value = "";
    });

    $("refShow").addEventListener("change", function (e) {
      S.refShow = e.target.checked; applyViewerState();
    });
    $("matchRefBtn").addEventListener("click", matchReference);
    bindRefCrop();
    // Starts the pick controls in the state the app is actually in: no
    // reference selected yet, so both tools are off and disabled and the
    // readout is empty rather than claiming a whole image match of nothing.
    updateCropUi();
    bindMatchControls();

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
      // V, not space: space is play and pause now (see onKey). Both cases of
      // the letter, because shift can be picked up or let go mid hold and
      // the peek must still end when the key does.
      if ((ev.key === "v" || ev.key === "V") && S.beforeHold) {
        var wasHeld = effectiveMode();
        S.beforeHold = false;
        applyViewerState();
        // Releasing the hold is a real transition now, not just a class
        // swap: the graded picture was never rendered while the bypass was
        // the whole viewer, so it has to be put back (on the GPU path from
        // the source frame already cached, with no network request), and
        // the stats and scopes have to come back with it.
        refreshForViewChange(wasHeld);
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

  /* Any explicit jump ends a shuttle: someone who just pressed "one frame
     back" is not shuttling any more. Kept here rather than inside
     timeline.js's own setTime path because the shuttle IS a stream of
     setTime calls, and a stopper inside that path would stop it every frame. */
  function stopShuttle() {
    if (window.StudioTimeline) window.StudioTimeline.shuttleStop();
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

  // This play session's server-stream <video> listeners (contract E3, "Play
  // loops until stopped"). The element is reused lap to lap the same way
  // #proxyVideo is in live.js, so the next lap (or a fresh Play press)
  // replaces this set instead of stacking a new one on top of it: without
  // that, every "ended" from lap N would still be live when lap N+1 ends,
  // and each lap after the first would start one more loop than the one
  // before it.
  var playAttached = null;

  function detachPlayListeners() {
    if (!playAttached) return;
    var a = playAttached;
    a.video.removeEventListener("playing", a.onPlaying);
    a.video.removeEventListener("timeupdate", a.onTime);
    a.video.removeEventListener("ended", a.onEnded);
    a.video.removeEventListener("error", a.onError);
    playAttached = null;
  }

  // null means the #playDur field's placeholder value, "all": the whole
  // clip rather than a fixed number of seconds. Anything else that will not
  // parse to a positive number (blank, or typed text) means the same thing,
  // on purpose: this is a plain text field, not a validated one, and a
  // half-typed value should fall back to "play the whole thing" rather than
  // silently becoming 5 the way it used to.
  function playDurationValue() {
    var v = parseFloat($("playDur").value);
    return (isFinite(v) && v > 0) ? v : null;
  }

  /* The loop range a press of Play uses, in clip time (contract E3):
   * "pressing Play plays from the playhead to the end of the range and
   * starts again ... the range is the whole clip by default".
   *
   * secs empty: the range is the whole clip (loopStart 0, loopEnd the
   * clip's duration), so the first lap plays from wherever the playhead
   * is to the end and every lap after that is the full clip from its
   * start. secs a number: the range is the bounded segment starting at
   * the playhead, which is today's segment-length behaviour, just looped
   * instead of played once.
   */
  function playLoopRange(startTime) {
    var secs = playDurationValue();
    if (secs == null) {
      return { loopStart: 0, loopEnd: S.duration || null, secs: null };
    }
    var end = S.duration ? Math.min(startTime + secs, S.duration) : startTime + secs;
    return { loopStart: startTime, loopEnd: end, secs: secs };
  }

  function syncPlayButton() {
    var btn = $("playBtn");
    btn.classList.toggle("active", playbackActive());
    // The button's own label is the "is this dead" signal the brief asks
    // for: idle says Play, a press that has not produced a frame yet says
    // so explicitly, and only an actually playing video says Pause. Mode 3
    // adds one more waiting state with a different cause, and says which:
    // "preparing" there is one ffmpeg pass over the whole clip, not a
    // fragment of stream, so it can take a while on a long clip and the
    // user is owed the difference.
    btn.textContent = (S.playing || S.gpuPlaying) ? "Pause"
      : (S.proxyPreparing ? "Preparing proxy..."
        : (S.playPreparing ? "Preparing..." : "Play"));
  }

  function stopPlayback(toastMsg) {
    if (!S.playing && !S.playPreparing) return;
    playToken++;                    // orphans any in-flight prepare()/video listeners
    detachPlayListeners();
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

  /* Which of the two playback engines a press of Play gets.
   *
   * Mode 3 (the GPU proxy) is the default whenever the GPU path is usable,
   * because it plays the whole clip, scrubs, and keeps every knob live while
   * it runs. The server stream below is the fallback for exactly one case:
   * no usable WebGL2 context, where there is no GPU to grade frames on and
   * the server has to both grade and encode.
   */
  function startPlayback() {
    if (!S.clip || playbackActive()) return;
    if (gpuPathAvailable()) { startGpuPlayback(); return; }
    startServerPlayback();
  }

  function startServerPlayback() {
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

    var startTime = S.time;
    var range = playLoopRange(startTime);
    var myToken = ++playToken;

    // Never a silent cap (contract E3): there is no separate "segment
    // budget" in this path to bound against in the first place, measured in
    // server.py's own _play_params (grep it) -- a single /api/play/prepare
    // request is bounded only by how much of the clip is left from `from`,
    // the same "remaining" clamp the 5s default was always subject to. So
    // "secs empty" asking for the rest of a long clip is one ordinary
    // request for a longer segment, not a new code path, and the existing
    // "preparing.../rendering..." status text is what says a bigger ffmpeg
    // pass is under way rather than doing it quietly.
    function segmentDuration(from) {
      if (range.secs != null) return range.secs;
      return Math.max(1 / (S.fps || 24), (range.loopEnd || 0) - from);
    }

    function playSegment(from) {
      detachPlayListeners();
      S.playPreparing = true;
      syncPlayButton();
      $("playStatus").textContent = "preparing...";
      var t0 = performance.now();
      var duration = segmentDuration(from);

      api("/api/play/prepare", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          clip: S.clip, time: from, duration: duration, width: S.width,
          rotation: S.rotation, config: cfg()
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
          paintTime();
        }
        function onEnded() {
          if (myToken !== playToken) return;
          // Loop (contract E3): the range's own start, not a stop. See
          // playLoopRange for what that start is in each case.
          playSegment(range.loopStart);
        }
        function onError() {
          if (myToken !== playToken) return;
          toast("playback failed to load", true);
          stopPlayback();
        }

        playAttached = { video: video, onPlaying: onPlaying, onTime: onTime,
                          onEnded: onEnded, onError: onError };
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

    playSegment(startTime);
  }

  /* ---- proxy playback (job: live GPU viewer, mode 3) --------------------
     Press Play with a working GPU and this is what runs. The server writes
     one small H.264 proxy of the whole clip once (POST /api/proxy/prepare),
     a hidden <video> decodes it, and live.js grades every frame the browser
     presents through the same chain the still viewer uses.

     What it buys over the two older paths: mode 2 (Loop) holds decoded
     frames in memory, so it is capped at a few seconds by a 700 MB budget;
     the server stream renders the whole chain on the CPU at about 2 fps and
     cannot be re-graded once encoded, so any knob change has to stop it.
     This path holds no frames at all (one video element, one texture), so a
     whole clip plays and scrubs, and a knob change lands on the next frame.

     What it costs: the proxy is 8-bit 4:2:0 where the still path is 16-bit.
     The difference is measured, not assumed, and stated in the Limits panel.

     proxyToken is the same idea as playToken above: a press of Play, a
     scrub and a clip change are all asynchronous and any of them can
     invalidate the others, so every callback checks the token it captured. */

  var proxyToken = 0;
  var proxySeekPromise = null;

  // "Something is playing or is about to", across both engines. One
  // predicate rather than four flags checked by hand at every call site,
  // because the failure mode of getting that wrong is two playback engines
  // fighting over the same layer.
  function playbackActive() {
    return S.playing || S.playPreparing || S.gpuPlaying || S.proxyPreparing;
  }

  function gpuPathAvailable() {
    return !!(S.gpu && StudioLive.available());
  }

  // What a prepared proxy is prepared FOR. The width is in here because the
  // proxy is encoded at the preview width, so changing that select makes the
  // existing proxy the wrong size to compare against the still path.
  function proxyDesc() {
    return S.clip + "|" + S.width + "|" + S.rotation;
  }

  function whenProxySettled() {
    return proxySeekPromise
      ? proxySeekPromise["catch"](function () { /* preview only */ })
      : Promise.resolve();
  }

  /* Make sure the proxy for the current clip and width exists and is
   * attached to the hidden video. Resolves immediately when it already is,
   * which is what makes pressing Play on an already-prepared clip start in
   * the time of a seek. */
  function ensureProxy(onProgress) {
    var desc = proxyDesc();
    if (S.proxyReady && S.proxyDesc === desc && StudioLive.proxyReady()) {
      return Promise.resolve(true);
    }
    S.proxyDesc = desc;
    S.proxyReady = false;
    return StudioLive.prepareProxy({
      clip: S.clip, width: S.width, rotation: S.rotation
    }, { onProgress: onProgress }).then(function (info) {
      if (proxyDesc() !== desc) {
        throw new Error("the clip changed while its proxy was preparing");
      }
      return StudioLive.attachProxy(info, {}).then(function () {
        if (proxyDesc() !== desc) {
          throw new Error("the clip changed while its proxy was loading");
        }
        S.proxyReady = true;
        return true;
      });
    });
  }

  /* Start the encode for the newly selected clip in the background, so the
   * first press of Play is instant instead of paying for a whole-clip ffmpeg
   * pass. Deliberately quiet: it writes progress into #playStatus and
   * nothing else, and it never blocks or disables the Play button, because
   * pressing Play mid-warm simply joins the same server side job. */
  function warmProxy() {
    if (!gpuPathAvailable() || !S.clip) return;
    if (S.proxyWarming || S.proxyPreparing || S.gpuPlaying) return;
    if (S.proxyReady && S.proxyDesc === proxyDesc()) return;
    var desc = proxyDesc();
    S.proxyWarming = true;
    ensureProxy(function (job) {
      if (proxyDesc() !== desc) return;
      var pct = job && job.progress ? Math.round(job.progress * 100) : 0;
      $("playStatus").textContent = "preparing playback proxy " + pct + "%";
    }).then(function () {
      S.proxyWarming = false;
      // The clip or the preview width changed while this encode was running,
      // so what just landed is not what the viewer is looking at now. The
      // change did try to warm and was turned away by the guard at the top of
      // this function (one warm at a time), and nothing else retries, so the
      // retry belongs here. Measured before this line existed: switching the
      // preview width inside the boot warm left the new width with no proxy
      // at all until Play was pressed, and Play then paid for the whole
      // encode. It cannot loop: the second pass warms the CURRENT desc, and
      // a warm whose desc still matches on completion stops here.
      if (proxyDesc() !== desc) { warmProxy(); return; }
      $("playStatus").textContent = "";
    })["catch"](function (e) {
      S.proxyWarming = false;
      if (proxyDesc() !== desc) { warmProxy(); return; }
      // Not a toast: nobody asked for this, it happened on clip select. The
      // failure only matters when Play is pressed, and that path reports it.
      $("playStatus").textContent = "proxy unavailable: " + (e.message || e);
    });
  }

  /* One graded frame from the proxy at this time, as instant feedback while
   * the timeline is being dragged. Returns without doing anything whenever
   * the proxy is not the right thing to show: another engine owns the
   * canvas, the view is split (the proxy only produces the graded half), or
   * the matte view is on (a diagnostic of the qualifier, not a picture). */
  function proxyPreview(t) {
    if (!gpuPathAvailable() || playbackActive() || S.looping) return;
    if (!S.proxyReady || S.proxyDesc !== proxyDesc() || !StudioLive.proxyReady()) return;
    if (S.mask || S.sheet || effectiveMode() !== MODE_AFTER) return;
    var token = ++proxyToken;
    proxySeekPromise = StudioLive.seekProxy(t, cfg, {
      sourceWidth: clipSourceWidth(),
      resolvedInput: clipResolvedInput()
    }).then(function (r) {
      if (token !== proxyToken || !r) return;
      setStageLayer("gpu");
      setRendererBadge("GPU proxy preview (8-bit)", !!cfg().grain.enabled);
    })["catch"](function () {
      // The still render scheduled alongside this is the real answer, so a
      // failed preview is not worth interrupting anyone over.
    });
    return proxySeekPromise;
  }

  function startGpuPlayback() {
    if (!S.clip || playbackActive()) return;
    if (S.looping || S.loopPreparing) stopLoop();
    // Same layer rule as every other playback path: mode 3 presents to
    // #gpuCanvas, which only #stage.gpu-live shows, and only after-only has
    // that layer to itself.
    if (S.viewMode !== MODE_AFTER) setViewMode(MODE_AFTER);

    // Loop range (contract E3), computed once from the playhead this press
    // started at: playProxy wraps back to range.loopStart on its own from
    // here on, so this call site never has to notice a lap ending.
    var range = playLoopRange(S.time);
    var token = ++proxyToken;
    var startedAt = performance.now();
    var warm = S.proxyReady && S.proxyDesc === proxyDesc();
    S.proxyPreparing = true;
    syncPlayButton();
    $("playStatus").textContent = warm ? "starting..." : "preparing playback proxy...";

    ensureProxy(function (job) {
      if (token !== proxyToken) return;
      var pct = job && job.progress ? Math.round(job.progress * 100) : 0;
      $("playStatus").textContent = "preparing playback proxy " + pct
        + "% (one ffmpeg pass for the whole clip, then playback is free)";
    }).then(function () {
      if (token !== proxyToken) return;
      S.proxyPreparing = false;
      S.gpuPlaying = true;
      syncPlayButton();
      setStageLayer("gpu");
      setRendererBadge("GPU playback (8-bit proxy)", !!cfg().grain.enabled);
      var lastFps = -1;
      // Start from where the playhead is, not from wherever the video was
      // left, so Play means "play from here" exactly as the old path did.
      StudioLive.seekProxy(S.time, null, {})["catch"](function () {})
        .then(function () {
          if (token !== proxyToken || !S.gpuPlaying) return;
          StudioLive.playProxy(cfg, {
            sourceWidth: clipSourceWidth(),
            resolvedInput: clipResolvedInput(),
            loop: true, loopStart: range.loopStart, loopEnd: range.loopEnd,
            onFrame: function (f) {
              if (token !== proxyToken) return;
              S.time = f.time;
              paintTime();
              if (f.measuredFps !== lastFps) {
                lastFps = f.measuredFps;
                $("playStatus").textContent = "playing " + f.measuredFps
                  + " fps, " + f.skipped + " skipped, " + f.dropped
                  + " dropped (first frame "
                  + Math.round(startedAt ? performance.now() - startedAt : 0) + " ms)";
              }
            },
            // Unreachable while loop: true above (live.js wraps instead of
            // calling this), kept for the one case it still means "stop":
            // opts.loop itself failing to resolve true inside live.js.
            onEnded: function () {
              if (token !== proxyToken) return;
              stopGpuPlayback();
            },
            onError: function (e) {
              if (token !== proxyToken) return;
              stopGpuPlayback();
              toast("GPU playback stopped: " + (e.message || e), true);
            }
          });
        });
    })["catch"](function (e) {
      if (token !== proxyToken) return;
      S.proxyPreparing = false;
      syncPlayButton();
      $("playStatus").textContent = "";
      // The GPU path could not be made to work for this clip, so fall all
      // the way back rather than leaving Play looking broken: the server
      // stream renders the same grade, just slower and without live knobs.
      toast((e.message || e) + " - falling back to the server stream", true);
      startServerPlayback();
    });
  }

  /* Stop mode 3 and hand the viewer back to the still path at the frame
   * that was last on screen. quiet=true is for setTime, which is already
   * moving the playhead itself and would otherwise be fought for it. */
  function stopGpuPlayback(quiet) {
    if (!S.gpuPlaying && !S.proxyPreparing) return;
    proxyToken++;
    var landAt = S.gpuPlaying ? StudioLive.proxyCurrentTime() : S.time;
    StudioLive.pauseProxy();
    S.gpuPlaying = false;
    S.proxyPreparing = false;
    // S.playing is false here, so this lands on gpu or still per the GPU
    // toggle, exactly as stopPlayback does for the server path.
    setStageLayer(S.gpu ? "gpu" : "still");
    syncPlayButton();
    $("playStatus").textContent = "";
    if (!quiet) setTime(landAt);
  }

  // Stops whichever engine is running, so no caller has to know which one
  // that was.
  function stopAnyPlayback(toastMsg) {
    if (S.playing || S.playPreparing) stopPlayback(toastMsg);
    if (S.gpuPlaying || S.proxyPreparing) stopGpuPlayback();
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
    stopAnyPlayback();
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
      width: S.width, rotation: S.rotation
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
        resolvedInput: clipResolvedInput(),
        onFrame: function (f) {
          S.time = f.time;
          paintTime();
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

  /* ---- render dialog (contract E3) --------------------------------------
     "the render settings should be a popup ... Render should go in the
     popup". One .overlay like JSON/Limits/Keys, so Esc already closes it
     (the global handler's ".overlay" sweep at the bottom of onKey) and the
     phone layout already fits it; only Enter-to-render is this dialog's
     own, since a plain text input has no other use for that key here. */

  function renderEstimateDuration() {
    var start = parseFloat($("renderStart").value);
    if (!isFinite(start) || start < 0) start = 0;
    var raw = ($("renderDur").value || "").trim();
    var secs = raw === "" ? NaN : parseFloat(raw);
    if (isFinite(secs) && secs > 0) return secs;
    return Math.max(0, (S.duration || 0) - start);      // empty: to the end
  }

  function updateRenderEstimate() {
    var el = $("renderEstimate");
    if (!el) return;
    var duration = renderEstimateDuration();
    var fps = S.fps || 24;
    var frames = Math.max(0, Math.round(duration * fps));
    el.textContent = "about " + frames + (frames === 1 ? " frame" : " frames")
      + " (" + duration.toFixed(2) + "s at " + fps.toFixed(2) + " fps)";
  }

  function openRenderDialog() {
    if (!S.clip) { toast("open a clip first", true); return; }
    // Defaults (contract E3): start at the playhead, secs empty (to the
    // end), width source, engine ffmpeg -- every time the dialog opens, so
    // it never carries a stale bound over from a previous render.
    $("renderStart").value = S.time.toFixed(2);
    $("renderDur").value = "";
    $("renderScale").value = "";
    $("renderEngine").value = "ffmpeg";
    updateRenderEstimate();
    $("renderOverlay").classList.add("on");
  }

  function closeRenderDialog() {
    $("renderOverlay").classList.remove("on");
  }

  function startRender() {
    var name = ($("renderName").value || "").trim();
    if (!name) { toast("give the render a name", true); return; }
    var body = {
      clip: S.clip, config: cfg(), rotation: S.rotation, name: name,
      start: parseFloat($("renderStart").value) || 0,
      // Empty means to the end of the clip: the server already renders with
      // no -t bound whenever duration is null (start_render and
      // start_gpu_render both read it the same way), so this is not a new
      // server behaviour, just the field's new default.
      duration: parseFloat($("renderDur").value) || null,
      scale: $("renderScale").value ? parseInt($("renderScale").value, 10) : null,
      // ffmpeg unless the user picked otherwise. The server dispatches on
      // this in start_render; every other field means the same to both.
      engine: ($("renderEngine") && $("renderEngine").value) || "ffmpeg"
    };
    api("/api/render", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    }).then(function (j) {
      toast("render started: " + j.job.label);
      closeRenderDialog();
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
      // Space is play and pause, which is the first key anyone tries on a
      // transport. It used to be "hold to peek at the ungraded frame"; that
      // moved to V, below, when the timeline was rebuilt.
      case " ":
        ev.preventDefault();
        stopShuttle();
        $("playBtn").click();
        break;
      // The peek, moved off space. Held rather than toggled: the keyup
      // handler below is what puts the graded frame back.
      case "v":
        // While a video is on screen this would mean showing beforeLayer
        // behind a still-playing video, so it stops playback instead of
        // holding, exactly as space used to.
        if (playbackActive()) { stopAnyPlayback(); break; }
        if (!S.beforeHold) {
          var wasHeld = effectiveMode();
          S.beforeHold = true;
          applyViewerState();
          // Same transition as the latched switch, so the same handler: the
          // hold has to move the stats and the scopes onto the bypassed
          // picture too, or they describe a frame that is no longer on
          // screen for as long as the key is down.
          refreshForViewChange(wasHeld);
        }
        break;
      case "p": stopShuttle(); $("playBtn").click(); break;
      /* J K L, the shuttle every editor has. Forward at 1x is the real
         playback engine; every other rate is one proxy seek per animation
         frame (timeline.js), because a <video> cannot play backwards and the
         proxy answers a seek far faster than a decode. Pressing J or L again
         doubles the rate to 8x.

         The three letters were already taken (j raw JSON, k mask, l loop),
         so those three moved to their own shifted key and nothing was lost:
         shift J, shift K, shift L still do exactly what j, k and l did, and
         all three also have a button on screen. */
      case "j": if (window.StudioTimeline) StudioTimeline.shuttle(-1); break;
      case "k": if (window.StudioTimeline) StudioTimeline.shuttle(0); break;
      case "l": if (window.StudioTimeline) StudioTimeline.shuttle(1); break;
      case "K": $("maskBtn").click(); break;
      case "L": $("loopBtn").click(); break;
      case "\\": toggleBeforeAfter(); break;
      case "b": $("bypassBtn").click(); break;
      case "w": $("wipeBtn").click(); break;
      case "y": cycleSplitModes(); break;
      case "1": setSlot("A"); break;
      case "2": setSlot("B"); break;
      case "!": S.slots.A = clone(cfg()); toast("stored in A"); break;
      case "@": S.slots.B = clone(cfg()); toast("stored in B"); break;
      case "f": setZoom("fit"); break;
      case "0": setZoom("one"); break;
      // One zoom step in and out (contract E4), the same step the plus and
      // minus buttons in the viewer bar take. Both spellings of each key,
      // because the shifted one is what a US keyboard actually sends.
      case "+": case "=":
        if (window.ViewerZoom) window.ViewerZoom.step(1); break;
      case "-": case "_":
        if (window.ViewerZoom) window.ViewerZoom.step(-1); break;
      case "r": $("refShow").checked = !$("refShow").checked;
        S.refShow = $("refShow").checked; applyViewerState(); break;
      case "c": $("sheetBtn").click(); break;
      case "g": {
        var d = $("dock");
        d.style.display = d.style.display === "none" ? "" : "none";
        setTimeout(fitViewer, 0);
        break;
      }
      case "m": addMark(); break;
      // One frame, or ten with shift: the step a shifted arrow takes in an
      // editor. It used to be a whole second, which is 24 frames on this
      // footage and lands nowhere in particular.
      // "<" and ">" are what a US keyboard actually sends for shift plus these
      // two keys, so the shifted step used to be unreachable: ev.key was
      // never "," or "." with shift held, and the ten frame (previously one
      // second) branch could not fire. Both spellings of each, the same way
      // the zoom keys already list "+" and "=".
      case ",": case "<":
        stopShuttle(); setTime(S.time - (ev.shiftKey ? 10 : 1) / S.fps); break;
      case ".": case ">":
        stopShuttle(); setTime(S.time + (ev.shiftKey ? 10 : 1) / S.fps); break;
      case "Home": ev.preventDefault(); stopShuttle(); setTime(0); break;
      case "End":
        ev.preventDefault(); stopShuttle();
        setTime(Math.max(0, S.duration - 1 / S.fps));
        break;
      case "[": {
        stopShuttle();
        var prev = S.marks.filter(function (t) { return t < S.time - 0.01; });
        if (prev.length) setTime(prev[prev.length - 1]);
        break;
      }
      case "]": {
        stopShuttle();
        var next = S.marks.filter(function (t) { return t > S.time + 0.01; });
        if (next.length) setTime(next[0]);
        break;
      }
      case "J": openJSON(); break;
      case "R": openRenderDialog(); break;
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
     VIEWER_ROWS/computeCellHeight below still sizes the viewer. Their cards
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
  // Only the viewer still needs a row COUNT for computeCellHeight's own math
  // below: scopes and timeline size to their real content in pixels (see
  // AUTOSIZE_CARDS/reflowAutosizeCards further down), not to a fixed share
  // of some assumed row total, so there is no longer one true "GRID_ROWS"
  // for the whole column the way there was before sizeToContent existed.
  var VIEWER_ROWS = 7;
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
  // (see avail/VIEWER_ROWS below) 24 is BIGGER than what avail leaves per row, so
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

  // The old formula was Math.floor((avail - (14 + 2) * GRID_MARGIN) / 14): a
  // fixed 14-row guess (viewer 7 + scopes 4 + timeline 3, the seed layout's
  // own row total) for a column where only the viewer's row count is
  // actually fixed -- scopes and timeline size to their real content in
  // pixels (see AUTOSIZE_CARDS/reflowAutosizeCards below), so their true row
  // count at any given cellHeight is whatever their fixed pixel content
  // happens to round up to, not a constant. Guessing 14 up front, and
  // separately guessing a (14 + 2) * GRID_MARGIN margin budget on top of it
  // that the vendored build never actually spends (read out of its own
  // _updateStyles/_updateContainerHeight: an item's box height is exactly
  // its row count times cellHeight -- `[gs-h="N"]{height: N*cellHeight}` --
  // GRID_MARGIN is only an inset the item's OWN content is pushed in BY,
  // shrinking what is visibly painted inside that box, never adding height
  // beyond it; confirmed live, viewer gs-h="7" measures exactly 7 *
  // cellHeight tall with zero margin term) -- both guesses being wrong in
  // the same direction is what opened the roughly 72px empty band this
  // fixes under the last card on a tall window: measured live at
  // 2560x1200, the old formula left a 74px gap below the timeline card even
  // on a load where the real row count landed on exactly 14 anyway.
  // Dropping just the margin-budget guess and leaving the 14-row guess in
  // place does not fix this in general, only relocates it: measured live,
  // it turned the 2560x1200 case into an 87px band instead (a *bigger* one)
  // by handing scopes/timeline a larger cellHeight, which is exactly enough
  // pixel headroom that their real, fixed pixel content now rounds up to
  // fewer rows than 14 assumed, so the column's real total row count no
  // longer matches 14 either.
  //
  // Genuinely fixing it means never guessing a row count for scopes/
  // timeline at all: measure their real pixel need directly
  // (autosizeCardOuterHeight below, the same three terms measureContentRows'
  // own r/h/n math resolves to for a settled card -- head height, natural
  // content height, two margin insets) and hand the viewer everything left
  // over in avail after that, divided by its own fixed VIEWER_ROWS. That
  // measurement needs scopes/timeline's REAL content to already exist, which
  // is not true yet at GridStack.init time: boot() deliberately lays the
  // grid out before the state fetch (see boot's own comment) so the widgets
  // are never visible unstyled while that fetch is in flight, which means
  // scopes/timeline are still just their near-empty static HTML skeleton the
  // first time anything could measure them. Measured live, using this real
  // math for GridStack.init's own bootstrap cellHeight read that empty
  // skeleton as the reserved figure and handed the viewer far too much of
  // the column, which nothing then clawed back once real content landed --
  // confirmed live, a 2560x1200 load ended up cutting the timeline off by
  // 363px instead of leaving a band under it. SEED_ROW_GUESS below is only
  // for that one bootstrap call, where nothing better is measurable yet;
  // computeCellHeight's own grids[colKey] check switches to the real math
  // the moment a grid object exists. watchAutosizeContentForCellHeight
  // (below) is what re-runs that real math once real content actually
  // lands: reflowAutosizeCards' own "change" event was tried first here and
  // does not work for this -- it only fires when scopes/timeline's ROW
  // COUNT moves, and a too-generous bootstrap cellHeight is exactly the
  // condition where real content arriving does NOT move that row count (the
  // oversized rows already had enough spare room to absorb the real content
  // without needing another one), so nothing re-fires it; confirmed live,
  // this genuinely got stuck at the wrong cellHeight forever, not just
  // slowly. Watching #dock/#timeline's own box directly (the same signal
  // AUTOSIZE_CARDS' own ResizeObserver in initAutosizeCards already uses,
  // just a second, independent observer instance rather than a change to
  // that one) reacts to the real pixel change directly, with no row
  // quantization to hide it behind. computeCellHeight's own shrink-until-it-
  // fits loop (below) is a second, separate correction for a second, separate
  // rounding problem: scopes/timeline still round their own pixel need up to
  // whole rows of whatever cellHeight this settles on (GridStack has no
  // other unit), and naively solving for cellHeight as if they could take
  // exactly their real pixel need provably overflows avail once that
  // rounding is applied -- confirmed live, a 2560x1200 load without the loop
  // undershot avail's own row math by exactly enough for scopes+timeline's
  // rounding-up to push the total 130px past the scrollport, a real
  // "needs a scroll" case on a window roomy enough that nothing about this
  // fix should have introduced one. The loop below only ever gives up a few
  // px to Math.floor, never a whole row, so what is left of any mismatch is
  // genuinely sub-pixel-row, not a blank band and not new overflow.
  var SEED_ROW_GUESS = 14; // viewer(7) + scopes(4) + timeline(3), bootstrap only, see above

  function autosizeCardOuterHeight(gsId, contentId) {
    var item = document.querySelector('#gridMid > .grid-stack-item[gs-id="' + gsId + '"]');
    var content = document.getElementById(contentId);
    if (!item || !content) return 0;
    var head = item.querySelector(".widgethead");
    var headH = head ? head.getBoundingClientRect().height : 0;
    return headH + content.getBoundingClientRect().height + 2 * GRID_MARGIN;
  }

  function computeCellHeight(colKey) {
    var host = $(colGridId(colKey));
    var avail = host ? host.clientHeight : 0;
    if (!grids[colKey]) {
      return Math.max(MIN_ROW_HEIGHT, Math.floor(avail / SEED_ROW_GUESS));
    }
    var scopesNeed = autosizeCardOuterHeight("scopes", "dock");
    var timelineNeed = autosizeCardOuterHeight("timeline", "timeline");
    var h = Math.max(MIN_ROW_HEIGHT, Math.floor((avail - scopesNeed - timelineNeed) / VIEWER_ROWS));
    // The line above sizes the viewer as if scopes/timeline could take up
    // exactly their real pixel need, but GridStack can only give them whole
    // rows of THIS cellHeight, rounded up (ceil, same as measureContentRows)
    // -- so their actual allocated pixel height at this h can run past what
    // was budgeted for them, past avail in total. Shrinking h one row-height
    // step at a time until the WHOLE-ROW total actually fits inside avail is
    // what turns that into sub-row flooring slack (at most a few px, from
    // Math.floor above) instead of overflow past the scrollport on a window
    // tall enough that MIN_ROW_HEIGHT was never the reason it stopped.
    while (h > MIN_ROW_HEIGHT) {
      var totalRows = VIEWER_ROWS + Math.ceil(scopesNeed / h) + Math.ceil(timelineNeed / h);
      if (totalRows * h <= avail) break;
      h--;
    }
    return h;
  }

  // Additive, not a replacement for AUTOSIZE_CARDS' own ResizeObserver in
  // initAutosizeCards below (a second, independent observer instance on the
  // same #dock/#timeline elements, not a change to that one): that observer
  // exists to keep scopes/timeline's OWN row count matched to their real
  // content; this one exists to keep the VIEWER's share matched to it too,
  // which needs the raw pixel signal (any real size change at all), not the
  // row-count-quantized one -- see the long comment on computeCellHeight for
  // why reflowAutosizeCards' own "change" event does not work for this.
  // Re-applying cellHeight only when it actually differs, and only reflowing
  // again when it does, is what keeps this quiet once real content stops
  // changing rather than re-triggering itself forever.
  function watchAutosizeContentForCellHeight() {
    if (!window.ResizeObserver) return;
    COLUMNS.forEach(function (colKey) {
      Object.keys(AUTOSIZE_CARDS).forEach(function (gsId) {
        var content = $(AUTOSIZE_CARDS[gsId]);
        if (!content) return;
        new ResizeObserver(function () {
          var grid = grids[colKey];
          if (!grid) return;
          var newH = computeCellHeight(colKey);
          if (newH !== grid.getCellHeight()) {
            grid.cellHeight(newH);
            reflowAutosizeCards();
          }
        }).observe(content);
      });
    });
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
    watchAutosizeContentForCellHeight();
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

  /* What a reload has to bring back (contract C4, the founder's words: "if i
   * reload it shouldnt break or reset the whole application").
   *
   * One GET decides everything. If this account has a project open, the page
   * comes back to exactly it: the same clip, the same playhead, the same
   * rotation and the same HEAD config, and it publishes NOTHING, so a reload
   * cannot create a commit or wake anybody else's long poll. If nothing is
   * open (a first ever visit, or a fresh data directory) it opens the first
   * clip, which is the ONE write boot is allowed to make and is what creates
   * that clip's project.
   *
   * The clip may be one opened from outside content/footage. Those live in
   * memory on the server (EXTERNAL_CLIPS), so a project that outlived a
   * server restart names a clip the current /api/state does not list; the
   * project record still carries its path, so it is re registered through
   * POST /api/open first and the clip list is refreshed from the answer.
   * Without that, reloading after a restart would silently drop the user on
   * a different clip, which is the exact complaint this contract is here to
   * answer. */
  function restoreOpenProject(state) {
    function fallback() {
      if (state.clips.length) return selectClip(state.clips[0].name);
      showError(new Error("no clips in content/footage"));
      return Promise.resolve(null);
    }
    return api("/api/project").then(function (proj) {
      if (!proj || !proj.open || !proj.name) return fallback();
      var listed = state.clips.some(function (c) { return c.name === proj.name; });
      if (listed) {
        applyProject(proj, { config: true, select: true });
        return proj;
      }
      if (!proj.path) return fallback();
      return api("/api/open", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: proj.path })
      }).then(function (j) {
        S.state.clips = j.clips;
        state.clips = j.clips;
        applyProject(proj, { config: true, select: true });
        return proj;
      }).catch(function () {
        toast("the clip this project is on is not reachable any more", true);
        return fallback();
      });
    }).catch(function () {
      // No project route at all (an older server): behave the way boot did
      // before this contract rather than showing an empty page.
      return fallback();
    });
  }

  function boot() {
    // Fills every static data-icon placeholder (browseUpBtn, the two
    // sidebar toggles, #themeToggle's sun/moon) with a real <svg> from the
    // vendored HugeIcons definitions. Before the state fetch, same reasoning
    // as initGrid just below: these placeholders are already in the DOM at
    // page load, not waiting on server data, so there is no reason to leave
    // them empty while that fetch is in flight. Panels.build and the
    // filesystem browser render their own icons directly through
    // StudioIcons.render, so this call never needs to run again.
    StudioIcons.mount(document);

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

      // Layers (contract C1) is handed the same {getConfig, onChange,
      // refreshPanels} shape WindowEditor gets just below, and for the same
      // reason: it never reaches into S, so it cannot become a second
      // source of truth for what a layer's fields are. This has to run
      // BEFORE Panels.build, not after (unlike WindowEditor): panels.js's
      // "layers" stage calls Layers.buildSection synchronously while it
      // builds the sidebar, so Layers needs api set before that happens.
      if (window.Layers) {
        window.Layers.init({
          getConfig: cfg,
          onChange: onParamChange,
          refreshPanels: function () { Panels.refresh(cfg(), S.defaults); }
        });
      }

      Panels.build($("params"), { onChange: onParamChange });
      Panels.refresh(cfg(), S.defaults);
      auditCoverage();

      // The shape editor is handed the same three things the panel uses and
      // nothing else: where the live config is, how to change it, and how to
      // repaint the widgets. It never reaches into S, so the overlay cannot
      // become a second source of truth for the window's numbers.
      if (window.WindowEditor) {
        window.WindowEditor.init({
          getConfig: cfg,
          onChange: onParamChange,
          refreshPanels: function () { Panels.refresh(cfg(), S.defaults); }
        });
      }

      // Zoom and the multi frame viewer (contract E4). Both get the same
      // shape every other module here gets: functions of app.js's, never a
      // reach into S, so neither can become a second source of truth for
      // the config, the playhead or the picture on screen.
      if (window.ViewerZoom) {
        window.ViewerZoom.init({ onLayout: fitViewer });
      }

      // The files pane (contract E2). Same shape as every other module here:
      // three functions of this file's, no reach into S, so the pane cannot
      // become a second source of truth for which clip is open.
      if (window.StudioFiles) {
        window.StudioFiles.init({
          openLibrary: openLibraryClip,
          selectClipByName: selectUploadedClip,
          getClipName: function () { return S.clip; },
          toast: toast
        });
      }
      /* The timeline (static/timeline.js): the ruler, the scrub gesture, the
         filmstrip, the mark flags and the loop range. Same shape as the
         Frames block below and for the same reason: it holds no state of its
         own, it is handed the handful of app.js functions it needs, so the
         playhead cannot end up with two owners. */
      if (window.StudioTimeline) {
        window.StudioTimeline.init({
          getClip: function () { return S.clip; },
          getTime: function () { return S.time; },
          getDuration: function () { return S.duration; },
          getFps: function () { return S.fps; },
          getRotation: function () { return S.rotation; },
          getMarks: function () { return S.marks; },
          removeMark: removeMark,
          setTime: function (t) { setTime(t); },
          makeTimeDraggable: makeTimeDraggable,
          playToggle: function () { $("playBtn").click(); },
          stopPlayback: function () { stopAnyPlayback(); },
          isPlaying: function () { return playbackActive() || S.looping; }
        });
      }
      if (window.Frames) {
        window.Frames.init({
          getClip: function () { return S.clip; },
          getTime: function () { return S.time; },
          getDuration: function () { return S.duration; },
          setTime: function (t) { setTime(t); },
          // The same test doRender makes before taking the GPU path, and
          // for the same reason: the matte view is a diagnostic the GPU
          // does not produce, so it stays on the server unconditionally.
          useGpu: function () { return !S.mask && S.gpu && StudioLive.available(); },
          renderStill: function (t) {
            return StudioLive.renderStill({
              clip: S.clip, time: t, width: S.width, rotation: S.rotation,
              config: cfg(), sourceWidth: clipSourceWidth(),
              resolvedInput: clipResolvedInput()
            });
          },
          gpuCanvas: function () { return $("gpuCanvas"); },
          serverFrame: function (i, t, img) {
            var channel = "slot" + i;
            return frameRequest(channel, basePayload({
              time: t, mode: S.mask ? "mask" : "graded"
            })).then(function (r) { return showBlob(channel, img, r); });
          },
          saveExtra: function (name, value) {
            return saveProjectExtra(name, value, "the frame times");
          },
          onCountChange: function () { applyViewerState(); },
          onLayout: fitViewer,
          // Scopes and statistics read slot 1, which is the playhead, which
          // is what S.time already is: these are the same two calls the
          // single viewer's render path makes.
          afterRender: function () {
            if (S.scopesAuto) { refreshStats(); refreshScopes(); }
          }
        });
      }

      fillPresets(state.presets);
      fillLooks(state.looks);
      fillRefs(state.refs);
      fillRenders(state.renders);
      drawMarks();
      syncSlotButtons();
      updateUndoButtons();
      bind();
      Panels.resizeCurves();

      restoreOpenProject(state);
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
