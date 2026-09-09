/* Masks: the SAM component stack, picking on the picture, tracking, and the
 * two display modes (contract C1 for the model, C4 for the routes; lane M6 of
 * plan/2026-09-08-studio-masks/PLAN.md).
 *
 * What this file is
 * -----------------
 * A layer's mask used to be one power window times one colour key. Contract
 * C1 replaces that with a STACK of components combined by add, subtract and
 * intersect, where a component is a window, a colour key, a luminance key, or
 * a `matte`: a tracked SAM 3.1 selection stored on disk as one grey PNG per
 * frame. This file is the whole user side of that: the stack editor, the add
 * menu, picking a subject on the picture, starting and watching a track, the
 * finesse controls, and the overlay tint in the viewer.
 *
 * What this file is NOT
 * ---------------------
 * It is not a second definition of the mask. Every number it writes goes into
 * the config through `Layers.emit`, the same per layer emit a slider in the
 * Window group calls, so undo, the per clip autosave, the session publish and
 * the re-render all cover a mask edit exactly the way they cover a slider
 * drag. The mask is then rendered by the two engines that already exist:
 * cinegrade.py for a real render and gpu.js for the preview. Nothing here
 * computes a matte for the picture on screen except the overlay tint, which is
 * a diagnostic drawn OVER that picture and never a substitute for it.
 *
 * The model is not realtime (design rules, section 2 of the plan)
 * --------------------------------------------------------------
 * Nothing here waits on the model. Segmenting one frame is a request with a
 * busy state; tracking a clip is a background job with progress, a measured
 * rate, and cancel. A layer whose matte is still being tracked keeps working:
 * the engines hold the nearest written frame, so the correction is static past
 * the tip of the track, which is what the "static until tracked" badge says.
 * When the job finishes the panel refreshes and the same matte id is suddenly
 * complete, so the layer swaps to the tracked matte without a click.
 *
 * The overlay tint (acceptance A1)
 * --------------------------------
 * `#maskOverlay` is a canvas inside #stage. It reads
 * GET /api/matte/<id>/frame?time=&width=, the same route gpu.js reads, tints
 * the grey with the theme's accent and draws it over whatever the viewer is
 * showing (a still, the GPU canvas, or a playing video). It follows the
 * playhead on an animation frame loop that only runs while the overlay is on,
 * so it moves under BOTH playback engines without either one needing a hook.
 * Design rule 10 is implemented the same way gpu.js implements it: an LRU of
 * decoded frames, a prefetch along the play direction, and a frame that is not
 * there yet reuses the last one and says "matte lagging" in the status line
 * instead of stalling the picture.
 *
 * The black and white matte view is NOT drawn here at all: it is the layer's
 * own `mask.show`, which both engines already render (gpu.js draws a component
 * stack's combined matte as grey, cinegrade does the same through
 * maskedmerge), so it plays in sync with the picture for free and it is the
 * exact combined matte rather than this file's approximation of it.
 */
(function (global) {
  "use strict";

  /* ---- module state ------------------------------------------------------
   * api is the same {getConfig, onChange, refreshPanels, ...} shape every
   * other module in this folder is handed by app.js's boot(): functions of
   * app.js's, never a reach into its S, so this file cannot become a second
   * source of truth for the clip, the playhead or the config. */
  var api = null;

  var STYLE_ID = "masksInlineStyle";

  /* The overlay asks the server for the matte at this width and tints what
   * comes back. Small on purpose: the tint is a diagnostic over the picture,
   * not the picture, and the one per frame pixel pass that turns grey into
   * "accent with alpha" is the only real cost in the playback loop. 384 wide
   * is about a quarter of a megapixel on this footage, which is a few
   * milliseconds once per NEW frame (never once per animation frame, see the
   * cache below). */
  var OVERLAY_WIDTH = 384;
  /* Same numbers gpu.js uses for the same job (M3 section 4), so the two
   * caches behave alike and a person reading one can predict the other. */
  var CACHE_MAX = 48;
  var PREFETCH = 8;
  /* More matte components than this on one layer and the overlay stops
   * compositing them: past a handful the tint says nothing anyway, and the
   * matte view is the exact answer. */
  var OVERLAY_MAX_MATTES = 4;

  /* How far the two blur controls may be dragged, as a fraction of frame
   * width. Round 3 finding 83: the engines have clamped feather and
   * finesse.blur to a tenth of frame width since round 2 finding 52, in all
   * three implementations, and this panel went on offering 0.25 and 0.2. Past
   * the cap the slider moved, the saved grade changed, and the picture did
   * not: the sliders' last two thirds were inert.
   *
   * Read off gpu.js rather than typed again, so there is one number in the
   * browser and it is the one the preview actually clamps to; the literal is
   * only the fallback for a page that somehow loaded this file without that
   * one. The maxima are the cap, and a value SAVED above it (any grade
   * written before round 2) is shown clamped, because the number in the panel
   * has to be the number the picture was made with. */
  var BLUR_MAX = (global.StudioGPU && global.StudioGPU.mask
                  && typeof global.StudioGPU.mask.BLUR_MAX === "number")
    ? global.StudioGPU.mask.BLUR_MAX : 0.10;
  var BLUR_CAP_NOTE = " Capped at " + BLUR_MAX.toFixed(2)
    + " of the frame width: past that the engines clamp and the picture stops "
    + "changing.";

  var display = { mode: "off", layer: -1 };   // "off" | "overlay" | "matte"

  var service = { state: "checking", health: null, error: "", startCmd: "" };
  var jobs = {};              // job id -> the last /api/mask/jobs/<id> answer
  var watching = {};          // job id -> true while it is queued or running
  var matteInfo = {};         // matte id -> the last /api/matte/<id> answer
  var matteInfoWanted = {};   // matte id -> true, so one fetch per id in flight
  var clipRot = {};           // clip name -> the file's own rotation tag, as a string
  var clipRotWanted = false;  // one /api/clips fetch at a time, for the tag above
  var pollTimer = null;
  var sections = [];          // one entry per built layer section, for refresh

  var pick = null;            // the live pick session, see startPick()
  var thumbs = [];            // the live component thumbnails, see drawThumb

  /* The overlay's own decoded frame cache (design rule 10). Keyed
   * "<matte id>:<width>:<frame index>", each value a canvas already tinted, so
   * a render is one drawImage. `order` is the LRU. */
  var frames = {};
  var frameOrder = [];
  var frameWanted = {};
  var lastFrame = {};         // matte id -> the last frame index actually drawn
  var lagging = false;
  var playDir = 1;
  var lastTime = null;
  var rafId = null;
  var scratch = null;         // one reusable 2D canvas for the tint pass

  function $(id) { return document.getElementById(id); }
  function num(v, d) { v = Number(v); return isFinite(v) ? v : d; }
  function clamp(v, lo, hi) { return v < lo ? lo : (v > hi ? hi : v); }

  /* ---- http --------------------------------------------------------------
   * app.js's own api() throws away the status code, and this file needs it:
   * a 503 from a mask route is "the service is not running" plus the command
   * that starts it, which is a different thing to show than a 400. */
  function req(path, opts) {
    return fetch(path, opts).then(function (r) {
      if (!r.ok) {
        return r.json().catch(function () { return {}; }).then(function (j) {
          var e = new Error(j.error || r.statusText || ("HTTP " + r.status));
          e.status = r.status;
          throw e;
        });
      }
      return r.json();
    });
  }

  function post(path, body) {
    return req(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });
  }

  function toast(msg, bad) {
    if (api && api.toast) api.toast(msg, bad);
  }

  /* ---- reading and writing a layer's mask -------------------------------- */

  function layersOf() {
    var cfg = api && api.getConfig ? api.getConfig() : null;
    if (!cfg || !global.Layers) return [];
    return global.Layers.getLayers(cfg);
  }

  function layerAt(idx) {
    var arr = layersOf();
    return (idx >= 0 && idx < arr.length) ? arr[idx] : null;
  }

  function componentsOf(layer) {
    var list = (layer && layer.mask && layer.mask.components) || [];
    return Array.isArray(list) ? list : [];
  }

  // One write of the whole array, so a reorder, a delete and a field edit are
  // all one undo step and all go through the same auto-enable as a slider.
  function writeComponents(idx, arr, commit) {
    if (!global.Layers || !global.Layers.emit) return;
    global.Layers.emit(idx, ["mask", "components"], arr, commit !== false);
  }

  function cloneComponents(layer) {
    return JSON.parse(JSON.stringify(componentsOf(layer)));
  }

  function writeField(idx, j, path, value, commit) {
    var layer = layerAt(idx);
    if (!layer) return;
    var arr = cloneComponents(layer);
    if (!arr[j]) return;
    var node = arr[j];
    for (var i = 0; i < path.length - 1; i++) {
      if (node[path[i]] === undefined || node[path[i]] === null) node[path[i]] = {};
      node = node[path[i]];
    }
    node[path[path.length - 1]] = value;
    writeComponents(idx, arr, commit);
  }

  function writeFinesse(idx, field, value, commit) {
    if (!global.Layers || !global.Layers.emit) return;
    global.Layers.emit(idx, ["mask", "finesse", field], value, commit !== false);
  }

  /* ---- the component templates (the add menu's own table) -----------------
   *
   * `kind` is what the menu item carries in data-mask-add-kind, and it is the
   * only place a menu entry turns into a component. A SAM entry is a matte
   * component whose recipe is a text prompt (portable: the same recipe applied
   * to another clip queues that clip's own track, per design rule 5); the last
   * five are the keys and windows the studio already had, written as
   * components so they combine with the SAM ones through the same three ops.
   *
   * `track: true` means adding it also queues its track straight away, which
   * is design rule 9 ("start early"): the model is slow, so the useful thing
   * to do with a text selection is to get it running while the grade happens.
   * `pick: true` means the opposite: the recipe is pixel specific, so the
   * component starts in `needs_pick` and the viewer goes into pick mode. */
  var ADD_KINDS = [
    { kind: "subject", group: "Select", label: "Select subject", text: "subject", track: true,
      title: "SAM 3.1 finds the subject of the frame and tracks it through the clip." },
    { kind: "sky", group: "Select", label: "Sky", text: "sky", track: true,
      title: "Every sky region SAM finds, tracked through the clip." },
    { kind: "background", group: "Select", label: "Background", text: "background", track: true,
      title: "The background as a phrase. If the detector finds none, add Select "
           + "subject instead and switch its invert on, which is the same selection "
           + "the other way round." },
    { kind: "person", group: "People and parts", label: "People", text: "person", track: true },
    { kind: "face", group: "People and parts", label: "Face", text: "face", track: true },
    { kind: "hair", group: "People and parts", label: "Hair", text: "hair", track: true },
    { kind: "lips", group: "People and parts", label: "Lips", text: "lips", track: true },
    { kind: "eyes", group: "People and parts", label: "Eyes", text: "eyes", track: true },
    { kind: "teeth", group: "People and parts", label: "Teeth", text: "teeth", track: true },
    { kind: "clothes", group: "People and parts", label: "Clothes", text: "clothes", track: true },
    { kind: "object", group: "Point at it", label: "Object (click or box)", pick: true,
      title: "Click the thing on the picture, alt click anything to exclude, or drag "
           + "a box round it. SAM answers with candidates and you pick one." },
    { kind: "text", group: "Point at it", label: "Text phrase", ask: true, track: true,
      title: "Any phrase SAM 3.1 knows: \"red car\", \"left hand\", \"window frame\"." },
    { kind: "colour", group: "Keys and shapes", label: "Colour range", type: "key",
      title: "The hue, saturation and luminance qualifier the studio already had, "
           + "as a component so it combines with the rest of the stack." },
    { kind: "luma", group: "Keys and shapes", label: "Luminance range", type: "luma",
      title: "The same qualifier with hue and saturation opened all the way: a "
           + "luminance key." },
    { kind: "linear", group: "Keys and shapes", label: "Linear gradient", type: "window",
      shape: "linear", title: "A gradient across the frame, aimed by its angle." },
    { kind: "radial", group: "Keys and shapes", label: "Radial gradient", type: "window",
      shape: "ellipse", title: "The power window's ellipse, with its own softness." },
    { kind: "rect", group: "Keys and shapes", label: "Rectangle", type: "window",
      shape: "rect", title: "The power window's rectangle." }
  ];

  function kindByName(kind) {
    for (var i = 0; i < ADD_KINDS.length; i++) {
      if (ADD_KINDS[i].kind === kind) return ADD_KINDS[i];
    }
    return null;
  }

  var nextCid = 1;
  function freshId() { return "c" + (nextCid++) + "_" + Math.random().toString(36).slice(2, 6); }

  /* The defaults every component is written against: gpu.js's own
   * COMPONENT_DEFAULTS when it is loaded (one definition, mirrored from
   * cinegrade's MASK_COMPONENT_DEFAULTS), and a hand written copy of the same
   * fields only if it somehow is not. */
  function componentDefaults() {
    if (global.StudioGPU && global.StudioGPU.mask && global.StudioGPU.mask.COMPONENT_DEFAULTS) {
      return JSON.parse(JSON.stringify(global.StudioGPU.mask.COMPONENT_DEFAULTS));
    }
    return {
      id: "", type: "window", op: "add", enabled: true, invert: false, feather: 0.0,
      window: { enabled: true, shape: "ellipse", cx: 0.5, cy: 0.5, w: 0.6, h: 0.6,
                rotation: 0.0, softness: 0.15, invert: false },
      key: { enabled: true, invert: false, hue_center: 30.0, hue_width: 40.0,
             hue_soft: 15.0, sat_low: 0.1, sat_high: 1.0, sat_soft: 0.1,
             lum_low: 0.0, lum_high: 1.0, lum_soft: 0.1 },
      matte: { id: "", recipe: {} }
    };
  }

  /* One component, built from a menu entry. Only the fields that differ from
   * the defaults are written, so the preset a layer saves stays small and a
   * component reads as what somebody actually chose. */
  function makeComponent(entry, phrase) {
    var comp = { id: freshId(), op: "add", enabled: true, invert: false, feather: 0.0 };
    if (entry.type === "key" || entry.type === "luma") {
      comp.type = entry.type;
      comp.name = entry.label;
      var d = componentDefaults();
      comp.key = d.key;
      return comp;
    }
    if (entry.type === "window") {
      comp.type = "window";
      comp.name = entry.label;
      var dw = componentDefaults().window;
      dw.shape = entry.shape;
      if (entry.shape === "linear") {
        /* The linear gradient's own numbers: a transition a third of the frame
         * wide across the middle, aimed at the top, with a straight ramp.
         * `angle` is written explicitly because the engine falls back to
         * `rotation` when it is absent (M2's formula), and a gradient whose
         * aim silently came from a rotation field is a confusing default. */
        dw.cx = 0.5; dw.cy = 0.5; dw.w = 0.33; dw.angle = 0; dw.softness = 1.0;
      }
      comp.window = dw;
      return comp;
    }
    comp.type = "matte";
    comp.name = phrase || entry.label;
    comp.matte = { id: "", recipe: {} };
    if (entry.pick) {
      comp.matte.needs_pick = true;
    } else {
      comp.matte.recipe = { prompts: { text: [phrase || entry.text] }, select: "all", steady: null };
    }
    return comp;
  }

  /* ---- what state one component is in ------------------------------------
   *
   * The plan's section 2 states, plus the two the UI has to say on its own:
   * `static` is "the matte exists but the track has not finished, so the
   * engines are holding the nearest written frame" (the plan's "static until
   * tracked"), and `none` is a window or key component, which has no matte and
   * no job at all. Everything else comes from the server: the matte's own
   * state from GET /api/matte/<id>, which the SAM service keeps current, and
   * the job's from GET /api/mask/jobs/<id>. */
  function recipeOf(comp) {
    return ((comp || {}).matte || {}).recipe || {};
  }

  function componentState(comp) {
    if (!comp || comp.type !== "matte") return { state: "none" };
    var ref = comp.matte || {};
    if (ref.needs_pick) return { state: "needs_pick" };
    var id = String(ref.id || "");
    if (!id) {
      /* No matte id yet. With a recipe already on it that means the track
       * call is in flight (adding a text selection queues it at once), which
       * is a queued state and NOT "needs a pick": telling somebody to go
       * click on the picture when the thing is already starting is worse than
       * saying nothing. Without a recipe there is genuinely nothing to track.
       */
      return (recipeOf(comp).prompts) ? { state: "queued" } : { state: "needs_pick" };
    }
    wantMatte(id);   // one fetch per id, guarded; a repeat build is free
    var info = matteInfo[id];
    var jobId = ref.job_id ? String(ref.job_id) : "";
    var job = jobId ? jobs[jobId] : null;
    var out = { state: "queued", matte: id, info: info, job: job };
    if (info && info.unreachable && jobLive(job)) {
      /* The record could not be read AND a job is running: the service
       * rewrites that record as frames land, so a failed read here is a race
       * with the writer, not news. The job's own state is the authority and
       * the read is retried on the next poll; putting the read's error on the
       * row would be a red line about nothing, once a second, for the whole
       * length of the track. */
      out.state = String(job.state === "queued" ? "queued" : "running");
    } else if (info) {
      out.state = String(info.state || "queued");
      if (info.error) out.reason = String(info.error);
      /* Stale is the studio's own word for "this matte was made for something
       * else": another rotation, or another clip. The server can also say
       * stale itself, which is why the check is after reading its state and
       * only ever tightens it. */
      var rot = api && api.getRotation ? String(api.getRotation()) : null;
      var clip = api && api.getClip ? String(api.getClip()) : null;
      var madeAt = resolveRot(info.rotation, info.clip || clip);
      var viewAt = rot === null ? null : resolveRot(rot, clip);
      if (madeAt !== null && viewAt !== null && madeAt !== viewAt) {
        out.state = "stale";
        out.reason = "made at rotation " + info.rotation + ", the viewer is at " + rot;
      } else if (clip && info.clip && String(info.clip) !== clip) {
        out.state = "stale";
        out.reason = "made for " + info.clip;
      }
    } else if (job) {
      out.state = String(job.state || "queued");
    }
    if (out.state === "running" || out.state === "partial") out.holding = true;
    if (jobLive(job)) out.job = job;
    /* Which record carries the reason depends on the ending, so this reads
     * both and prefers the matte's (out.reason is set from info.error above).
     * A job that FAILED carries the only copy of why (the service's own
     * refusal), and the matte it left is `partial` with no error on it: a
     * matte cancelled at frame 20 is that shape. A job that was CANCELLED
     * before it wrote anything is the other way round since tooling gap 26:
     * the matte is `cancelled` and carries the reason, and the job view
     * deliberately reports no error, because a cancel is not a failure to
     * report. */
    if (job && !jobLive(job) && job.error && !out.reason) out.reason = String(job.error);
    return out;
  }

  var STATE_WORDS = {
    queued: "queued", running: "tracking", done: "tracked", failed: "failed",
    partial: "partial", stale: "stale", needs_pick: "needs a pick",
    cancelled: "cancelled", none: ""
  };

  function stateLabel(st) {
    if (st.holding) return "static until tracked";
    return STATE_WORDS[st.state] !== undefined ? STATE_WORDS[st.state] : st.state;
  }

  /* ---- the service and the jobs ------------------------------------------ */

  function pollNow() {
    return req("/api/mask/status").then(function (j) {
      var h = j.service || {};
      if (h.ok === false) {
        service.state = "down";
        service.error = String(h.error || "the mask service is not answering");
        service.startCmd = startCommandFrom(service.error);
      } else {
        service.state = "ok";
        service.health = h;
        service.error = "";
      }
      return j;
    }).catch(function (e) {
      service.state = "down";
      service.error = e.message || String(e);
      service.startCmd = startCommandFrom(service.error);
    }).then(function () {
      return pollJobs();
    }).then(function () {
      refreshAll();
    });
  }

  /* The 503's own message always carries the command that starts the service
   * (studio/sam_client.py's START_COMMAND). Pulling it out rather than
   * hard coding one here means the panel shows whatever that file says, so the
   * instruction cannot drift away from the code that prints it. */
  function startCommandFrom(message) {
    /* Stops at the first "(" on purpose: sam_client.py's START_COMMAND puts a
     * parenthetical after the command ("drop --stub once weights are
     * installed"), and what goes in the copyable box has to be the command
     * alone, because that is what gets pasted into a terminal. */
    var m = /((?:uv run|uvx|python[0-9.]*)[^\n(]*server\.py[^\n(]*)/.exec(String(message || ""));
    return m ? m[1].trim() : "";
  }

  /* A matte record that could not be read is retried on the next poll rather
   * than remembered as broken. Reading one is a race with the service writing
   * it (the record is rewritten as frames land), so a single failed read says
   * almost nothing, and caching that failure would leave a red line on a row
   * whose matte is perfectly fine a second later. */
  function retryUnreachable() {
    Object.keys(matteInfo).forEach(function (id) {
      if (matteInfo[id] && matteInfo[id].unreachable) wantMatte(id, true);
    });
  }

  function pollJobs() {
    retryUnreachable();
    var ids = Object.keys(watching);
    if (!ids.length) return Promise.resolve();
    return Promise.all(ids.map(function (id) {
      return req("/api/mask/jobs/" + encodeURIComponent(id)).then(function (j) {
        jobs[id] = j;
        if (!jobLive(j)) {
          delete watching[id];
          (j.matte_ids || []).forEach(function (m) { forgetMatte(m); });
          /* The swap the plan asks for, with no click: the matte id on the
           * component never changed, it just finished, so re-reading it and
           * re-rendering is the whole of it. */
          if (api && api.rerender) api.rerender();
        }
        (j.matte_ids || []).forEach(function (m) { wantMatte(m, true); });
      }).catch(function () { delete watching[id]; });
    }));
  }

  function schedulePoll() {
    if (pollTimer) return;
    pollTimer = setInterval(function () {
      var live = Object.keys(watching).length > 0;
      var need = live || service.state !== "ok" || sections.length > 0;
      if (!need) return;
      pollNow();
    }, 1200);
  }

  function wantMatte(id, force) {
    id = String(id || "");
    if (!id) return;
    if (!force && (matteInfo[id] || matteInfoWanted[id])) return;
    matteInfoWanted[id] = true;
    req("/api/matte/" + encodeURIComponent(id)).then(function (j) {
      matteInfo[id] = j;
    }).catch(function (e) {
      /* The matte's own record could not be read. That is not the same as
       * "the track failed": while a job is live the job's state is the better
       * answer, so this is marked as unreachable rather than failed and
       * componentState below decides which of the two to believe. */
      /* 404 is the one failure that is final: the matte store has never heard
       * of this id, so the recipe on this component points at nothing and
       * asking again every second would not change that. Anything else (a
       * read racing the writer, the server restarting) is retried. */
      matteInfo[id] = { matte_id: id, state: "failed",
                        unreachable: e.status !== 404,
                        error: e.status === 404
                          ? "this matte is not in the store any more: track it again"
                          : (e.message || String(e)) };
    }).then(function () {
      delete matteInfoWanted[id];
      refreshAll();
    });
  }

  /* ---- what "auto" means when two rotations are compared -----------------
   *
   * "auto" is not a rotation. It is "whatever this file's own tag says", so
   * it cannot be compared with a rotation somebody picked until it has been
   * resolved to that tag. Comparing the word with a number calls every matte
   * made at the studio's default stale the moment a person clicks the
   * rotation the file was already at, which is the one case where the picture
   * did not move at all. The tag comes from /api/clips (rotation_tag), read
   * once and kept. */
  function wantClipRot() {
    if (clipRotWanted) return;
    clipRotWanted = true;
    req("/api/clips").then(function (r) {
      (r.clips || []).forEach(function (c) {
        if (!c || !c.name) return;
        var tag = (c.rotation_tag === undefined || c.rotation_tag === null || c.rotation_tag === "")
          ? c.rotation : c.rotation_tag;
        clipRot[c.name] = (tag === undefined || tag === null) ? "" : String(parseInt(tag, 10) || 0);
      });
      refreshAll(true);
    }).catch(function () { /* no tag: the comparison below simply stands down */ });
  }

  /* null means "not knowable yet", and the caller treats that as "say
   * nothing" rather than "stale": a guess here would put a red badge on a
   * matte that is perfectly current. */
  function resolveRot(v, clipName) {
    var sv = (v === undefined || v === null || v === "") ? "auto" : String(v);
    if (sv !== "auto") return sv;
    if (!clipName) return null;
    wantClipRot();
    var tag = clipRot[clipName];
    return (tag === undefined || tag === "") ? null : String(tag);
  }

  function forgetMatte(id) {
    id = String(id || "");
    delete matteInfo[id];
    // Its frames are stale too: a done matte has frames a partial one did not.
    Object.keys(frames).forEach(function (k) {
      if (k.indexOf(id + ":") === 0) { delete frames[k]; }
    });
    frameOrder = frameOrder.filter(function (k) { return k.indexOf(id + ":") !== 0; });
    delete lastFrame[id];
    /* The GRADED picture holds its own copy of the same frames, and it has to
     * be told too. This panel's overlay cache and gpu.js's matte cache are two
     * caches of one store: a re-track writes different pixels under the same
     * id and frame numbers, and a matte finishing turns a partial index into a
     * full one, so a renderer that was never told would keep serving the old
     * pixels (and the old, short frame count) for the rest of the session. It
     * used to be told nothing at all, which is why a re-tracked matte looked
     * right in the overlay and stayed wrong in the picture. */
    if (global.StudioLive && global.StudioLive.invalidateMatte) {
      global.StudioLive.invalidateMatte(id);
    }
    wantMatte(id, true);
  }

  /* ---- tracking ---------------------------------------------------------- */

  function trackBody(extra) {
    var body = {
      clip: api.getClip(),
      rotation: api.getRotation()
    };
    for (var k in (extra || {})) body[k] = extra[k];
    return body;
  }

  /* Where a component is NOW, by its own id.
   *
   * A track is a network round trip, and while it is in flight the rows can be
   * reordered, added to or deleted from. An array index is a POSITION, not a
   * component, so stamping a finished track back at the position it started
   * from attaches one subject's matte to whatever row happens to sit there
   * now. Components carry an id (makeComponent writes one), so that is what is
   * followed here. A component with no id at all (a preset written before ids
   * existed) falls back to its position, which is the old behaviour and the
   * best that can be done for it. Returns -1 when the component is gone. */
  function componentIndexById(arr, cid, fallback) {
    if (cid) {
      for (var i = 0; i < arr.length; i++) {
        if (arr[i] && arr[i].id === cid) return i;
      }
      return -1;
    }
    return arr[fallback] ? fallback : -1;
  }

  /* Starts (or re-starts) the track for one component and stamps whatever came
   * back onto that component. `job_id` is remembered on the component's matte
   * ref so a reload of the panel still knows which job to watch; the engines
   * ignore it (it is inside `matte`, which they only read `id` and `recipe`
   * from), and it is what makes the progress row survive a rebuild. */
  function trackComponent(idx, j, opts) {
    var layer = layerAt(idx);
    var comp = componentsOf(layer)[j];
    if (!comp || comp.type !== "matte") return Promise.resolve();
    var cid = comp.id || "";
    var ref = comp.matte || {};
    var recipe = ref.recipe || {};
    var body = trackBody({ steady: (opts && opts.steady) || ref.steady || null });
    if (opts && opts.pick_id) {
      body.pick_id = opts.pick_id;
      body.select = opts.select || "all";
    } else if (recipe.prompts) {
      body.prompts = recipe.prompts;
      if (recipe.select) body.select = recipe.select;
    } else {
      toast("this component has no prompt to track: pick it on the picture first", true);
      return Promise.resolve();
    }
    setRowBusy(idx, j, true);
    return post("/api/mask/track", body).then(function (r) {
      var mattes = r.mattes || [];
      if (!mattes.length) throw new Error("the track started but named no matte");
      var arr = cloneComponents(layerAt(idx));
      var at = componentIndexById(arr, cid, j);
      if (at < 0 || !arr[at] || arr[at].type !== "matte") {
        /* The component was deleted (or stopped being a matte) while the track
         * ran. The matte itself is on disk and the job is real, so this is not
         * an error to shout about, but there is nothing left to stamp it onto,
         * and stamping it on the row that took that position would be the bug
         * this whole helper exists to stop. */
        setRowBusy(idx, j, false);
        return;
      }
      arr[at].matte = {
        id: mattes[0].matte_id,
        recipe: mattes[0].recipe || recipe,
        job_id: r.job_id || null
      };
      if (opts && opts.name) arr[at].name = opts.name;
      /* "choose one or all": every instance past the first becomes its own
       * component, added below this one with op add, so a person can then
       * subtract or feather each one separately. */
      for (var k = 1; k < mattes.length; k++) {
        var extra = JSON.parse(JSON.stringify(arr[at]));
        extra.id = freshId();
        extra.name = (mattes[k].label || arr[at].name || "matte") + " " + (k + 1);
        extra.matte = { id: mattes[k].matte_id, recipe: mattes[k].recipe || recipe,
                        job_id: r.job_id || null };
        arr.splice(at + k, 0, extra);
      }
      writeComponents(idx, arr, true);
      if (r.job_id) { watching[r.job_id] = true; jobs[r.job_id] = { id: r.job_id, state: "queued" }; }
      mattes.forEach(function (m) { forgetMatte(m.matte_id); });
      pollNow();
    }).catch(function (e) {
      if (e.status === 503) {
        service.state = "down";
        service.error = e.message;
        service.startCmd = startCommandFrom(e.message);
        refreshAll();
      } else {
        toast(e.message || String(e), true);
      }
      setRowBusy(idx, j, false);
    });
  }

  /* The studio's own generic job cancel, not a mask specific route: a mask
   * track IS a studio Job, and the mask poller in server.py turns the
   * cancelled status into a cancel call on the SAM service. Whatever frames
   * the track already wrote stay on disk as a partial matte. */
  function cancelJob(jobId) {
    return post("/api/job/cancel", { id: jobId })
      .then(function () { pollNow(); })
      .catch(function (e) { toast(e.message || String(e), true); });
  }

  /* GET /api/mask/jobs/<id> only ever says running, done, failed or
   * cancelled: waiting for the service's own queue is `running` with a
   * queue_position, so "queued" is read off that rather than off the state. */
  function jobLive(job) {
    return !!(job && (job.state === "running" || job.state === "queued"));
  }

  function jobQueued(job) {
    return !!(job && job.state !== "done" && num(job.queue_position, 0) > 0);
  }

  /* ---- picking on the picture -------------------------------------------- */

  function startPick(idx, j) {
    // compId is the component this pick belongs to, followed by id rather than
    // by row position for the whole life of the session (see choosePick).
    pick = { layer: idx, comp: j,
             compId: ((componentsOf(layerAt(idx))[j] || {}).id) || "",
             points: [], boxes: [], busy: false,
             candidates: null, pickId: null, error: "" };
    if (global.WindowEditor && global.WindowEditor.setMaskPick) {
      /* window-editor.js owns the drawing and appends to the two arrays it
       * is handed (they are THIS object's own arrays, so there is one copy of
       * the prompt, not two); the callbacks are only "something changed". */
      global.WindowEditor.setMaskPick({
        points: pick.points, boxes: pick.boxes,
        onPoint: function () { refreshAll(); },
        onBox: function () { refreshAll(); }
      });
    }
    setStatus();
    refreshAll();
  }

  function stopPick() {
    pick = null;
    if (global.WindowEditor && global.WindowEditor.setMaskPick) {
      global.WindowEditor.setMaskPick(null);
    }
    setStatus();
    refreshAll();
  }

  function runSegment() {
    if (!pick) return;
    if (!pick.points.length && !pick.boxes.length) {
      toast("click the subject on the picture first", true);
      return;
    }
    /* THIS session, captured before the request goes out.
     *
     * `pick` is one module level slot that startPick reassigns wholesale, so
     * `if (!pick)` tests that SOME pick is live, not that it is the one this
     * response belongs to. Pick on component A, press Find, press Done, pick on
     * component B, and A's late answer used to pass that guard and overwrite
     * B's pickId and candidates: choosePick then tracked B with A's pick and
     * attached a matte of the wrong subject with no error anywhere. Identity,
     * not nullness. */
    var session = pick;
    session.busy = true;
    session.error = "";
    refreshAll();
    post("/api/mask/segment", {
      clip: api.getClip(), time: api.getTime(), rotation: api.getRotation(),
      prompts: { text: [], points: session.points, boxes: session.boxes },
      max_instances: 8
    }).then(function (r) {
      if (pick !== session) return;
      session.busy = false;
      session.pickId = r.pick_id;
      session.candidates = r.instances || [];
      if (!session.candidates.length) {
        session.error = "SAM found nothing there. Try another point.";
      }
      refreshAll();
    }).catch(function (e) {
      /* The service being down is a fact about the machine, not about this
       * session, so it is recorded whichever pick is live now; the session's
       * own error line is not, for the reason above. */
      if (e.status === 503) {
        service.state = "down";
        service.error = e.message;
        service.startCmd = startCommandFrom(e.message);
      }
      if (pick !== session) { refreshAll(); return; }
      session.busy = false;
      session.error = e.message || String(e);
      refreshAll();
    });
  }

  function choosePick(select, label) {
    if (!pick || !pick.pickId) return;
    /* Read the target off the session that owns this pick id, before stopPick
     * clears it, and find the component by ITS OWN ID rather than by the
     * position it had when the pick started. A pick session lasts as long as
     * the user takes to click, press Find and press Done, and the rows can be
     * reordered or deleted in that time (see componentIndexById). */
    var session = pick;
    var arr = componentsOf(layerAt(session.layer));
    var at = componentIndexById(arr, session.compId, session.comp);
    if (at < 0 || !arr[at] || arr[at].type !== "matte") {
      toast("the component this pick belongs to is gone, so there is nothing "
            + "to track: add it again and pick once more", true);
      stopPick();
      return;
    }
    trackComponent(session.layer, at,
                   { pick_id: session.pickId, select: select, name: label });
    stopPick();
  }

  /* ---- the overlay tint --------------------------------------------------
   * Everything from here to setStatus() is the viewer overlay: the frame
   * cache, the tint pass, the prefetch and the draw loop. */

  function accentRGB() {
    var v = getComputedStyle(document.documentElement).getPropertyValue("--accent").trim();
    var m = /^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(v);
    if (m) return [parseInt(m[1], 16), parseInt(m[2], 16), parseInt(m[3], 16)];
    return [69, 161, 253];     // --accent's own value, if the variable is unreadable
  }

  function frameKey(id, index) { return id + ":" + OVERLAY_WIDTH + ":" + index; }

  function lruTouch(key) {
    var at = frameOrder.indexOf(key);
    if (at >= 0) frameOrder.splice(at, 1);
    frameOrder.push(key);
    while (frameOrder.length > CACHE_MAX) {
      var drop = frameOrder.shift();
      delete frames[drop];
    }
  }

  /* index = round(time * fps), clamped, exactly as C2 and M2's own
   * frame_index do. With no index for this matte yet (the info fetch has not
   * landed, or the route failed) the caller keys on the time instead and
   * prefetch is switched off, which is the same fallback gpu.js documents. */
  function frameIndexOf(id, t) {
    var info = matteInfo[id];
    if (!info || !info.fps) return null;
    var frames_ = num(info.frames, 0) || num(info.total_frames, 0);
    var i = Math.round(num(t, 0) * num(info.fps, 24));
    return clamp(i, 0, Math.max(0, frames_ - 1));
  }

  function tintInto(bitmap, rgb) {
    var w = bitmap.width, h = bitmap.height;
    if (!scratch) scratch = document.createElement("canvas");
    scratch.width = w; scratch.height = h;
    var sctx = scratch.getContext("2d", { willReadFrequently: true });
    sctx.clearRect(0, 0, w, h);
    sctx.drawImage(bitmap, 0, 0);
    var img = sctx.getImageData(0, 0, w, h);
    var d = img.data;
    /* One pass, grey to "accent with the matte as alpha". Done once per NEW
     * matte frame and cached, never once per animation frame: at 24 fps the
     * playback loop below is a single drawImage of the canvas this produces. */
    for (var i = 0; i < d.length; i += 4) {
      var g = d[i];
      d[i] = rgb[0]; d[i + 1] = rgb[1]; d[i + 2] = rgb[2];
      d[i + 3] = g;
    }
    var out = document.createElement("canvas");
    out.width = w; out.height = h;
    out.getContext("2d").putImageData(img, 0, 0);
    return out;
  }

  function fetchFrame(id, index, t) {
    var key = frameKey(id, index === null ? ("t" + num(t, 0).toFixed(3)) : index);
    if (frames[key] || frameWanted[key]) return;
    frameWanted[key] = true;
    var url = "/api/matte/" + encodeURIComponent(id) + "/frame?time="
      + encodeURIComponent(num(t, 0).toFixed(4)) + "&width=" + OVERLAY_WIDTH;
    fetch(url).then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.blob();
    }).then(function (b) {
      return createImageBitmap(b);
    }).then(function (bm) {
      frames[key] = tintInto(bm, accentRGB());
      lruTouch(key);
      if (bm.close) bm.close();
      delete frameWanted[key];
      redrawThumbs(id);
    }).catch(function () {
      delete frameWanted[key];
    });
  }

  // Read ahead of the playhead along the play direction, fire and forget:
  // nothing in the grading loop waits on it (design rule 1).
  function prefetch(id, index) {
    var info = matteInfo[id];
    if (index === null || !info || !info.fps) return;
    var total = num(info.frames, 0) || num(info.total_frames, 0);
    for (var n = 1; n <= PREFETCH; n++) {
      var i = index + n * playDir;
      if (i < 0 || i >= total) break;
      fetchFrame(id, i, i / num(info.fps, 24));
    }
  }

  function overlayMattes() {
    var layer = layerAt(display.layer);
    if (!layer) return [];
    var out = [];
    componentsOf(layer).forEach(function (c) {
      if (!c || c.type !== "matte" || c.enabled === false) return;
      var id = String((c.matte || {}).id || "");
      if (id && out.indexOf(id) < 0 && out.length < OVERLAY_MAX_MATTES) out.push(id);
    });
    return out;
  }

  function pictureBox() {
    if (global.WindowEditor && global.WindowEditor.pictureBox) {
      return global.WindowEditor.pictureBox();
    }
    return null;
  }

  function drawOverlay() {
    var cv = $("maskOverlay");
    if (!cv) return;
    var on = display.mode === "overlay";
    var ids = on ? overlayMattes() : [];
    var box = ids.length ? pictureBox() : null;
    if (!box) {
      cv.dataset.on = "false";
      cv.style.display = "none";
      if (lagging) { lagging = false; setStatus(); }
      return;
    }
    cv.dataset.on = "true";
    cv.style.display = "block";
    cv.style.left = box.x.toFixed(2) + "px";
    cv.style.top = box.y.toFixed(2) + "px";
    cv.style.width = box.w.toFixed(2) + "px";
    cv.style.height = box.h.toFixed(2) + "px";

    var t = api.getTime ? num(api.getTime(), 0) : 0;
    if (lastTime !== null && Math.abs(t - lastTime) > 1e-6) playDir = t >= lastTime ? 1 : -1;
    lastTime = t;

    var pxw = Math.max(1, Math.round(box.w));
    var pxh = Math.max(1, Math.round(box.h));
    if (cv.width !== pxw || cv.height !== pxh) { cv.width = pxw; cv.height = pxh; }
    var ctx = cv.getContext("2d");
    ctx.clearRect(0, 0, pxw, pxh);

    var lag = false, servedFrame = null, servedId = null;
    ids.forEach(function (id) {
      var index = frameIndexOf(id, t);
      if (index === null) wantMatte(id);
      var key = frameKey(id, index === null ? ("t" + t.toFixed(3)) : index);
      var canvas = frames[key];
      if (!canvas) {
        fetchFrame(id, index, t);
        /* Design rule 10: a frame that is not decoded yet does not stall the
         * picture. The last one decoded for this matte is drawn instead and
         * the status line says so. */
        var prev = lastFrame[id];
        if (prev !== undefined) {
          canvas = frames[frameKey(id, prev)];
        }
        lag = true;
      } else {
        lastFrame[id] = index === null ? ("t" + t.toFixed(3)) : index;
        lruTouch(key);
      }
      if (canvas) {
        ctx.globalAlpha = 0.55;
        ctx.drawImage(canvas, 0, 0, pxw, pxh);
        ctx.globalAlpha = 1;
        servedFrame = lastFrame[id];
        servedId = id;
      }
      prefetch(id, index);
    });
    cv.dataset.matte = servedId || "";
    cv.dataset.frame = servedFrame === null || servedFrame === undefined ? "" : String(servedFrame);
    cv.dataset.lagging = lag ? "true" : "false";
    if (lag !== lagging) { lagging = lag; setStatus(); }
  }

  function loop() {
    rafId = null;
    if (display.mode === "overlay") {
      drawOverlay();
      rafId = requestAnimationFrame(loop);
    } else if (display.mode === "matte") {
      // No canvas to draw here (see setDisplay's own comment: the matte
      // view is mask.show, rendered by gpu.js or cinegrade themselves).
      // Polled anyway, at the same rate the overlay redraws itself, so
      // gpu.js's own "matte lagging" flag (StudioLive.lastMattesReport,
      // integration lane item 3) shows up in the status line while it is
      // true and clears again once the GPU's own cache has caught up,
      // instead of only ever updating on the next unrelated re-render.
      setStatus();
      rafId = requestAnimationFrame(loop);
    }
  }

  function startLoop() {
    if (rafId === null) rafId = requestAnimationFrame(loop);
  }

  function stopLoop() {
    if (rafId !== null) { cancelAnimationFrame(rafId); rafId = null; }
    var cv = $("maskOverlay");
    if (cv) { cv.dataset.on = "false"; cv.style.display = "none"; }
  }

  /* gpu.js keeps its own per-matte cache report (id -> {state, lagging,
   * ...}) for the black and white matte VIEW (mask.show, a real config
   * field both engines render), which is a different cache from this
   * file's own overlay-tint one above. StudioLive forwards the most recent
   * one from either a still render or a playing proxy frame; true here
   * means at least one matte component on the picture right now is being
   * held on its last decoded frame while a newer one is still on the way. */
  function gpuMattesLagging() {
    var live = global.StudioLive;
    if (!live || typeof live.lastMattesReport !== "function") return false;
    var report = live.lastMattesReport();
    if (!report) return false;
    for (var id in report) {
      if (Object.prototype.hasOwnProperty.call(report, id) && report[id]
          && report[id].lagging) return true;
    }
    return false;
  }

  /* ---- the status line in the viewer bar ---------------------------------- */

  function setStatus() {
    var el = $("maskStatus");
    if (!el) return;
    var state = "off", text = "";
    if (service.state === "down") {
      state = "down";
      text = "mask service not running";
    } else if (pick) {
      state = "picking";
      text = "click the subject, alt click to exclude, drag a box";
    } else if (display.mode === "overlay" && lagging) {
      state = "lagging";
      text = "matte lagging";
    } else if (display.mode === "overlay") {
      state = "overlay";
      var ids = overlayMattes();
      text = ids.length ? ("mask overlay, " + ids.length + (ids.length === 1 ? " matte" : " mattes"))
                        : "mask overlay: no tracked matte on this layer";
    } else if (display.mode === "matte" && gpuMattesLagging()) {
      state = "lagging";
      text = "matte lagging";
    } else if (display.mode === "matte") {
      state = "matte";
      text = "matte view";
    }
    el.dataset.maskStatus = state;
    el.textContent = text;
  }

  /* ---- display modes ------------------------------------------------------
   * off / overlay tint / black and white matte. The matte view is the layer's
   * own mask.show, which is a real config field both engines already render,
   * so switching to it is a config edit like any other rather than a viewer
   * flag this file would have to teach two engines about. */
  function setDisplay(idx, mode) {
    var was = display.mode, wasLayer = display.layer;
    if (was === "matte" && (mode !== "matte" || wasLayer !== idx)) {
      var prev = layerAt(wasLayer);
      if (prev && prev.mask && prev.mask.show) {
        global.Layers.emit(wasLayer, ["mask", "show"], false, true);
      }
    }
    display.mode = mode;
    display.layer = idx;
    if (mode === "matte") {
      global.Layers.emit(idx, ["mask", "show"], true, true);
    }
    // "matte" mode has nothing of this file's own to draw every frame (the
    // picture itself IS the matte, rendered by gpu.js or cinegrade), but it
    // still polls gpuMattesLagging() at the same rate so "matte lagging"
    // shows up and clears on gpu.js's own schedule during playback rather
    // than only on the next unrelated re-render (loop() above no-ops the
    // draw for this mode, it only calls setStatus()).
    if (mode === "overlay" || mode === "matte") startLoop(); else stopLoop();
    setStatus();
    refreshAll();
    if (api && api.rerender) api.rerender();
  }

  /* ---- small DOM helpers -------------------------------------------------- */

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }

  function btn(label, title, fn, cls) {
    var b = el("button", "btn" + (cls ? " " + cls : ""), label);
    b.type = "button";
    if (title) b.title = title;
    b.addEventListener("click", function (ev) { ev.stopPropagation(); fn(ev); });
    return b;
  }

  function iconBtn(icon, title, disabled, fn) {
    var b = el("button", "btn iconbtn");
    b.type = "button";
    b.title = title;
    b.disabled = !!disabled;
    if (global.StudioIcons) b.appendChild(global.StudioIcons.render(icon, "rowicon"));
    else b.textContent = title;
    b.addEventListener("click", function (ev) { ev.stopPropagation(); fn(); });
    return b;
  }

  function named(node, attr) {
    node.setAttribute(attr, "");
    return node;
  }

  function setRowBusy(idx, j, on) {
    var root = $("maskSection-" + idx);
    if (!root) return;
    var row = root.querySelector('[data-mask-comp="' + j + '"]');
    if (row) row.classList.toggle("busy", !!on);
  }

  /* ---- the component row's thumbnail --------------------------------------
   * A matte draws its own frame at the playhead; a window draws its shape; a
   * key draws its hue band. Small, cheap, and never a second renderer: the
   * matte thumbnail reuses the same cached tinted frames the overlay decodes.
   */
  function drawThumb(cv, comp) {
    var w = cv.width, h = cv.height;
    var ctx = cv.getContext("2d");
    ctx.clearRect(0, 0, w, h);
    var line = getComputedStyle(document.documentElement).getPropertyValue("--line2").trim() || "#3d3d3d";
    var accent = getComputedStyle(document.documentElement).getPropertyValue("--accent").trim() || "#45a1fd";
    /* Black first, so the thumbnail reads as a little picture of the matte
     * rather than as a coloured pill sitting next to two real switches. */
    ctx.fillStyle = "#000";
    ctx.fillRect(0, 0, w, h);
    ctx.strokeStyle = line;
    ctx.strokeRect(0.5, 0.5, w - 1, h - 1);
    if (!comp) return;
    if (comp.type === "matte") {
      var id = String((comp.matte || {}).id || "");
      if (!id) return;
      var t = api.getTime ? num(api.getTime(), 0) : 0;
      var index = frameIndexOf(id, t);
      var key = frameKey(id, index === null ? ("t" + t.toFixed(3)) : index);
      var c = frames[key] || (lastFrame[id] !== undefined ? frames[frameKey(id, lastFrame[id])] : null);
      if (c) {
        /* Drawn back to grey. The cached frame is the accent tinted one the
         * overlay uses (one decode serves both), but a 40x26 patch of solid
         * accent next to two real switches reads as a third switch, and what
         * this is meant to say is "here is the matte", which is a black and
         * white thing. */
        ctx.filter = "grayscale(1) brightness(1.5)";
        ctx.drawImage(c, 1, 1, w - 2, h - 2);
        ctx.filter = "none";
      } else {
        fetchFrame(id, index, t);
      }
      return;
    }
    if (comp.type === "key" || comp.type === "luma") {
      var k = comp.key || {};
      var hue = num(k.hue_center, 30);
      for (var x = 1; x < w - 1; x++) {
        var f = (x - 1) / Math.max(1, w - 3);
        ctx.fillStyle = comp.type === "luma"
          ? "hsl(0, 0%, " + Math.round(f * 100) + "%)"
          : "hsl(" + hue + ", " + Math.round(30 + f * 60) + "%, 50%)";
        ctx.fillRect(x, 1, 1, h - 2);
      }
      return;
    }
    var win = comp.window || {};
    ctx.strokeStyle = accent;
    ctx.lineWidth = 1;
    if (win.shape === "rect") {
      ctx.strokeRect(w * 0.22, h * 0.24, w * 0.56, h * 0.52);
    } else if (win.shape === "linear") {
      var grad = ctx.createLinearGradient(0, 1, 0, h - 1);
      grad.addColorStop(0, accent);
      grad.addColorStop(1, "transparent");
      ctx.fillStyle = grad;
      ctx.fillRect(1, 1, w - 2, h - 2);
    } else {
      ctx.beginPath();
      ctx.ellipse(w / 2, h / 2, w * 0.28, h * 0.3, 0, 0, Math.PI * 2);
      ctx.stroke();
    }
  }

  function redrawThumbs(matteId) {
    thumbs.forEach(function (t) {
      if (!t.cv.isConnected) return;
      var c = t.comp || {};
      if (c.type === "matte" && String((c.matte || {}).id || "") !== String(matteId)) return;
      drawThumb(t.cv, c);
    });
  }

  /* ---- the add menu ------------------------------------------------------- */

  function buildAddMenu(idx, anchor) {
    var menu = el("div", "mask-menu");
    menu.setAttribute("data-mask-addmenu", "");
    var groups = [];
    ADD_KINDS.forEach(function (entry) {
      if (groups.indexOf(entry.group) < 0) groups.push(entry.group);
    });
    groups.forEach(function (g) {
      menu.appendChild(el("div", "mask-menu-head", g));
      ADD_KINDS.filter(function (e) { return e.group === g; }).forEach(function (entry) {
        if (entry.ask) {
          var wrap = el("div", "mask-menu-ask");
          var input = el("input");
          input.type = "text";
          input.placeholder = entry.label;
          input.title = entry.title || "";
          input.setAttribute("data-mask-add-phrase", "");
          var go = btn("Add", entry.title || "", function () {
            var phrase = String(input.value || "").trim();
            if (!phrase) { input.focus(); return; }
            addComponent(idx, entry, phrase);
            closeMenu();
          });
          go.setAttribute("data-mask-add-kind", entry.kind);
          input.addEventListener("keydown", function (ev) {
            if (ev.key === "Enter") { ev.preventDefault(); go.click(); }
          });
          wrap.appendChild(input);
          wrap.appendChild(go);
          menu.appendChild(wrap);
          return;
        }
        var item = el("button", "mask-menu-item", entry.label);
        item.type = "button";
        item.setAttribute("data-mask-add-kind", entry.kind);
        if (entry.title) item.title = entry.title;
        item.addEventListener("click", function (ev) {
          ev.stopPropagation();
          addComponent(idx, entry, null);
          closeMenu();
        });
        menu.appendChild(item);
      });
    });
    anchor.appendChild(menu);
    return menu;
  }

  var openMenu = null;
  function closeMenu() {
    if (openMenu) { openMenu.classList.remove("open"); openMenu = null; }
  }
  document.addEventListener("click", function () { closeMenu(); });

  function addComponent(idx, entry, phrase) {
    var layer = layerAt(idx);
    if (!layer) return;
    var arr = cloneComponents(layer);
    var comp = makeComponent(entry, phrase);
    arr.push(comp);
    var j = arr.length - 1;
    writeComponents(idx, arr, true);
    if (entry.pick) { startPick(idx, j); return; }
    /* Design rule 9, written into the UI: queue the track the moment the
     * selection is named, because the model is slow and the grade happens
     * while it runs. */
    if (entry.track) trackComponent(idx, j, {});
  }

  /* ---- one component row --------------------------------------------------- */

  function buildRow(idx, j, comp, total) {
    var st = componentState(comp);
    var row = el("div", "mask-comp");
    row.setAttribute("data-mask-comp", String(j));
    row.setAttribute("data-mask-comp-type", String(comp.type || "window"));
    row.setAttribute("data-mask-state", st.state);

    var head = el("div", "mask-comp-head");

    var thumb = document.createElement("canvas");
    thumb.className = "mask-thumb";
    thumb.width = 40; thumb.height = 26;
    thumb.setAttribute("data-mask-thumb", "");
    /* Registered so a matte frame landing later can repaint just this canvas.
     * A rebuild of the whole panel for one 40x26 thumbnail would be absurd,
     * and doing nothing would leave every matte row grey until the next real
     * change. Cleared wholesale on the next rebuild (beginRebuild). */
    thumbs.push({ cv: thumb, comp: comp });
    drawThumb(thumb, comp);
    head.appendChild(thumb);

    var name = el("input", "mask-comp-name");
    name.type = "text";
    name.value = String(comp.name || defaultName(comp));
    name.spellcheck = false;
    name.setAttribute("data-mask-comp-name", "");
    name.title = "What this component is called in the stack. Only a label: the "
      + "selection itself is the recipe below it.";
    name.addEventListener("input", function () { writeField(idx, j, ["name"], name.value, false); });
    name.addEventListener("blur", function () { writeField(idx, j, ["name"], name.value, true); });
    name.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter") { ev.preventDefault(); name.blur(); }
    });
    head.appendChild(name);

    head.appendChild(el("span", "spacer"));
    /* Named as well as titled: an icon button has no text for a test to find,
     * and the checkpoint promises these three names to whoever writes one. */
    head.appendChild(named(iconBtn("ArrowUp01Icon", "Move up", j === 0,
      function () { move(idx, j, -1); }), "data-mask-up"));
    head.appendChild(named(iconBtn("ArrowDown01Icon", "Move down", j === total - 1,
      function () { move(idx, j, 1); }), "data-mask-down"));
    head.appendChild(named(iconBtn("Delete01Icon", "Remove this component", false,
      function () { remove(idx, j); }), "data-mask-remove"));
    row.appendChild(head);

    var ctrls = el("div", "mask-comp-ctrls");

    /* On this row rather than beside the name: the sidebar is about 250px
     * wide, and a thumbnail, a name field, a type word and three icon buttons
     * on one line leaves the name two characters of room. */
    var type = el("span", "mask-comp-type mono muted", typeWord(comp));
    type.setAttribute("data-mask-comp-type-label", "");
    ctrls.appendChild(type);

    var op = document.createElement("select");
    op.className = "mask-op";
    op.setAttribute("data-mask-op", "");
    op.title = "How this component combines with everything above it: add is the "
      + "larger of the two, intersect multiplies them, subtract takes this one away.";
    [["add", "add"], ["subtract", "subtract"], ["intersect", "intersect"]].forEach(function (o) {
      var opt = document.createElement("option");
      opt.value = o[0]; opt.textContent = o[1];
      op.appendChild(opt);
    });
    op.value = String(comp.op || "add");
    op.addEventListener("change", function () { writeField(idx, j, ["op"], op.value, true); });
    ctrls.appendChild(op);

    ctrls.appendChild(toggle("invert", "invert", !!comp.invert,
      "Flips THIS component before it combines, which is not the same as inverting the finished matte.",
      function (v) { writeField(idx, j, ["invert"], v, true); }, "data-mask-invert"));
    ctrls.appendChild(toggle("on", "on", comp.enabled !== false,
      "Switch this component off without deleting it.",
      function (v) { writeField(idx, j, ["enabled"], v, true); }, "data-mask-enabled"));

    var badge = el("span", "mask-badge", stateLabel(st));
    badge.setAttribute("data-mask-badge", "");
    badge.dataset.state = st.state;
    if (st.state !== "none") ctrls.appendChild(badge);
    /* A second badge, never a replacement for the state one: a matte can be
     * cleanly `done` and still have jumped onto the wrong thing halfway
     * through (checkpoint gap 18). Count only; the reasons and the first
     * suspect time are in the matte block below. */
    var sq = (st.info || {}).quality || null;
    if (sq && num(sq.suspect_count, 0) > 0) {
      var flag = el("span", "mask-badge", sq.suspect_count + " suspect");
      flag.setAttribute("data-mask-suspect-badge", "");
      flag.dataset.state = "suspect";
      flag.title = "Frames where the tracked area jumped, the overlap with "
        + "the frame before dropped, or the area went to zero inside the "
        + "span. First at " + sq.first_suspect_time + "s.";
      ctrls.appendChild(flag);
    }
    row.appendChild(ctrls);

    /* Feather is per component (C1 puts it there, not in finesse), so it lives
     * in the row it belongs to rather than in the finesse fold below. */
    var feather = Ctl.slider({
      label: "feather", min: 0, max: BLUR_MAX, step: 0.001, def: 0, precision: 3,
      value: Math.min(num(comp.feather, 0), BLUR_MAX),
      title: "A gaussian on this component alone, as a fraction of the frame "
           + "width." + BLUR_CAP_NOTE,
      onChange: function (v, c) { writeField(idx, j, ["feather"], v, c); }
    });
    feather.el.setAttribute("data-mask-feather", "");
    row.appendChild(feather.el);

    if (comp.type === "matte") row.appendChild(buildMatteBlock(idx, j, comp, st));
    else row.appendChild(buildShapeBlock(idx, j, comp));
    return row;
  }

  function toggle(id, label, value, title, fn, attr) {
    var wrap = el("label", "mask-toggle");
    wrap.title = title;
    var box = document.createElement("input");
    box.type = "checkbox";
    box.className = "ctl-switch";
    box.checked = !!value;
    box.setAttribute(attr, "");
    box.addEventListener("change", function () { fn(box.checked); });
    wrap.appendChild(box);
    wrap.appendChild(el("span", "mask-toggle-label", label));
    return wrap;
  }

  function typeWord(comp) {
    if (comp.type === "matte") return "SAM matte";
    if (comp.type === "key") return "colour range";
    if (comp.type === "luma") return "luminance";
    var s = (comp.window || {}).shape;
    return s === "linear" ? "gradient" : (s === "rect" ? "rectangle" : "radial");
  }

  function defaultName(comp) {
    if (comp.type === "matte") {
      var r = ((comp.matte || {}).recipe || {}).prompts || {};
      if (r.text && r.text.length) return r.text.join(", ");
      return "picked object";
    }
    return typeWord(comp);
  }

  /* The SAM half of a component row: the recipe in words, the state, the job's
   * progress with a measured rate, the service's own window and memory, and
   * the four buttons (pick, track, cancel, re-track). */
  function buildMatteBlock(idx, j, comp, st) {
    var box = el("div", "mask-matte");
    var ref = comp.matte || {};
    var recipe = ref.recipe || {};
    var prompts = recipe.prompts || {};

    var line = el("div", "mask-line");
    var what = [];
    if (prompts.text && prompts.text.length) what.push("text: " + prompts.text.join(", "));
    if (prompts.points && prompts.points.length) what.push(prompts.points.length + " point"
      + (prompts.points.length === 1 ? "" : "s"));
    if (prompts.boxes && prompts.boxes.length) what.push(prompts.boxes.length + " box"
      + (prompts.boxes.length === 1 ? "" : "es"));
    line.appendChild(el("span", "mono muted", what.length ? what.join(", ") : "no prompt yet"));
    box.appendChild(line);

    if (st.state === "needs_pick") {
      var need = el("div", "mask-line mask-needs");
      need.appendChild(el("span", "", "This selection is made of points and boxes on the "
        + "picture, so it has to be picked on this clip."));
      box.appendChild(need);
    }

    if (st.reason) {
      var why = el("div", "mask-line mask-reason", st.reason);
      why.setAttribute("data-mask-reason", "");
      box.appendChild(why);
    }

    /* How far a matte that is NOT being tracked right now actually got. The
     * engines hold the nearest written frame past the tip (M2's own
     * fallback), so "20 of 120" is the difference between a correction that
     * tracks and one that stops moving a fifth of the way in, and it is the
     * one number that says which. */
    var info = st.info;
    if (info && !jobLive(st.job) && info.is_partial) {
      var held = el("div", "mask-line mono muted",
        "tracked to " + num(info.done_frames, 0) + " of " + num(info.total_frames, 0)
        + " frames, held past there");
      held.setAttribute("data-mask-held", "");
      box.appendChild(held);
    }

    /* The span the matte really answers for, and the plain statement that it
     * is frozen outside it (checkpoint gaps 8 and 10). A DONE matte freezes
     * past its own window exactly like a running one, which is easy to read
     * as "this matte is valid for the whole clip" when nothing says
     * otherwise. */
    var span = info ? info.span : null;
    if (span && span.end_frame) {
      var sp = el("div", "mask-line mono muted",
        "span " + span.start_s + "s to " + span.end_s + "s, frozen outside it");
      sp.setAttribute("data-mask-span", "");
      sp.title = "Outside this window the matte holds its nearest written "
        + "frame: the correction still applies, it just stops moving.";
      box.appendChild(sp);
    }

    /* Per frame quality flags (checkpoint gap 18): the "face" matte that lost
     * the face, grabbed a tree and then tracked the whole person read as a
     * clean `done` everywhere in this panel before this line existed. */
    var q = info ? info.quality : null;
    if (q && num(q.suspect_count, 0) > 0) {
      var sus = el("div", "mask-line mask-reason",
        q.suspect_count + " suspect frames, first at " + q.first_suspect_time
        + "s: check `mask show " + String(info.matte_id || "") + " --strip` "
        + "before grading on this");
      sus.setAttribute("data-mask-suspect", "");
      sus.title = "A frame is suspect when its tracked area jumps by more "
        + "than " + ((q.thresholds || {}).area_jump) + " of the frame before "
        + "it, its overlap with that frame falls under "
        + ((q.thresholds || {}).min_iou) + ", or its area is zero inside the "
        + "span.";
      box.appendChild(sus);
    }

    var job = st.job;
    if (jobLive(job)) {
      var prog = el("div", "mask-line mask-progress");
      prog.setAttribute("data-mask-progress", "");
      var bar = el("span", "mask-bar");
      var fill = el("i");
      fill.style.width = Math.round(num(job.progress, 0) * 100) + "%";
      bar.appendChild(fill);
      prog.appendChild(bar);
      var done = num(job.done_frames, 0), total = num(job.total_frames, 0);
      /* The measured rate, not the clip's fps: on the real model those two are
       * 0.06 and 24 and confusing them is how a UI promises a track in five
       * seconds. GET /api/mask/jobs/<id> does not carry rate_fps yet (M5's own
       * open item), so it is computed from the frames done and the elapsed
       * seconds the job DOES carry, and it is labelled as measured. */
      var elapsed = num(job.elapsed_s, 0);
      var rate = (job.rate_fps !== undefined && job.rate_fps !== null)
        ? num(job.rate_fps, 0)
        : (elapsed > 0 ? done / elapsed : 0);
      var bits = [done + " of " + total + " frames"];
      if (rate > 0) {
        bits.push(rate.toFixed(rate < 1 ? 2 : 1) + " frames/s measured");
        if (total > done) {
          var left = (total - done) / rate;
          bits.push(left > 90 ? (Math.round(left / 60) + " min left") : (Math.round(left) + " s left"));
        }
      }
      if (jobQueued(job)) bits.unshift("queued, " + job.queue_position + " ahead of it");
      var txt = el("span", "mono muted", bits.join(", "));
      txt.setAttribute("data-mask-progress-text", "");
      prog.appendChild(txt);
      box.appendChild(prog);

      var wnote = serviceWindowNote();
      if (wnote) {
        var wl = el("div", "mask-line mono muted", wnote);
        wl.setAttribute("data-mask-window-note", "");
        box.appendChild(wl);
      }
    }

    var acts = el("div", "mask-line mask-acts");
    var pickBtn = btn("Pick on the picture", "Click the subject in the viewer, alt click to "
      + "exclude, or drag a box round it.", function () { startPick(idx, j); });
    pickBtn.setAttribute("data-mask-repick", "");
    acts.appendChild(pickBtn);

    var canTrack = !!(prompts.text && prompts.text.length);
    var trackBtn = btn(st.state === "done" ? "Track again" : "Track",
      "Track this selection through the clip in the background. Nothing in the "
      + "grading loop waits on it.", function () { trackComponent(idx, j, {}); });
    trackBtn.setAttribute("data-mask-track", "");
    trackBtn.disabled = !canTrack || service.state === "down";
    acts.appendChild(trackBtn);

    if (jobLive(job)) {
      var cancel = btn("Cancel", "Stop this track. Whatever frames it already wrote stay, "
        + "as a partial matte.", function () { cancelJob(job.id); });
      cancel.setAttribute("data-mask-cancel", "");
      acts.appendChild(cancel);
    }

    var steady = document.createElement("input");
    steady.type = "number";
    steady.min = "1"; steady.max = "31"; steady.step = "2";
    steady.className = "mask-steady";
    steady.value = String(num(ref.steady, 1));
    steady.title = "Steady: temporal smoothing over this many frames, centred on the "
      + "frame, so the matte does not lag the picture. 1 is off.";
    steady.setAttribute("data-mask-steady", "");
    steady.addEventListener("change", function () {
      var v = clamp(Math.round(Number(steady.value) || 1), 1, 31);
      steady.value = String(v);
      writeField(idx, j, ["matte", "steady"], v, true);
    });
    var slab = el("label", "mask-steady-wrap");
    slab.appendChild(el("span", "muted small", "steady"));
    slab.appendChild(steady);
    acts.appendChild(slab);

    box.appendChild(acts);
    return box;
  }

  function serviceWindowNote() {
    var h = service.health;
    if (!h) return "";
    var bits = [];
    if (h.window && h.window.frames) {
      bits.push("service window " + h.window.start + " to " + h.window.end
        + " (" + h.window.frames + " frames)");
    }
    if (h.memory && h.memory.rss_mb) {
      bits.push(Math.round(h.memory.rss_mb) + " MB resident"
        + (h.memory.peak_rss_mb ? ", peak " + Math.round(h.memory.peak_rss_mb) + " MB" : ""));
    }
    if (h.queue && h.queue.queued) bits.push(h.queue.queued + " queued behind it");
    return bits.join(", ");
  }

  /* A window or key component's own controls. Deliberately the few numbers
   * that make the shape usable from here; the shape itself is dragged on the
   * picture with the Window button, exactly as a legacy window is. */
  function buildShapeBlock(idx, j, comp) {
    var box = el("div", "mask-shape");
    function slider(label, path, min, max, step, def, precision, title) {
      var cur = path.reduce(function (o, k) { return (o || {})[k]; }, comp);
      var w = Ctl.slider({
        label: label, min: min, max: max, step: step, def: def, precision: precision,
        value: num(cur, def), title: title,
        onChange: function (v, c) { writeField(idx, j, path, v, c); }
      });
      w.el.setAttribute("data-mask-field", path.join("."));
      box.appendChild(w.el);
    }
    if (comp.type === "window") {
      var shape = (comp.window || {}).shape;
      slider("centre x", ["window", "cx"], 0, 1, 0.002, 0.5, 3);
      slider("centre y", ["window", "cy"], 0, 1, 0.002, 0.5, 3);
      if (shape === "linear") {
        slider("width", ["window", "w"], 0.01, 2, 0.002, 0.33, 3,
          "How wide the transition is, as a fraction of the frame width.");
        slider("angle", ["window", "angle"], -180, 180, 1, 0, 0,
          "0 selects the top of the frame, 90 the right, clockwise on screen.");
        slider("softness", ["window", "softness"], 0.05, 8, 0.05, 1, 2,
          "The ease: 1 is a straight ramp, 3 eases both ends, 0.05 is nearly a hard edge.");
      } else {
        slider("width", ["window", "w"], 0.02, 2, 0.002, 0.6, 3);
        slider("height", ["window", "h"], 0.02, 2, 0.002, 0.6, 3);
        slider("rotation", ["window", "rotation"], -180, 180, 1, 0, 0);
        slider("softness", ["window", "softness"], 0, 1, 0.005, 0.15, 3);
      }
      return box;
    }
    if (comp.type === "key") {
      slider("hue centre", ["key", "hue_center"], 0, 360, 1, 30, 0);
      slider("hue width", ["key", "hue_width"], 0, 180, 1, 40, 0);
      slider("hue soft", ["key", "hue_soft"], 0, 90, 1, 15, 0);
      slider("sat low", ["key", "sat_low"], 0, 1, 0.005, 0.1, 3);
      slider("sat high", ["key", "sat_high"], 0, 1, 0.005, 1, 3);
    }
    slider("luma low", ["key", "lum_low"], 0, 1, 0.005, 0, 3);
    slider("luma high", ["key", "lum_high"], 0, 1, 0.005, 1, 3);
    slider("luma soft", ["key", "lum_soft"], 0, 1, 0.005, 0.1, 3);
    return box;
  }

  /* ---- structural edits ---------------------------------------------------- */

  function move(idx, j, dir) {
    var layer = layerAt(idx);
    var arr = cloneComponents(layer);
    var k = j + dir;
    if (j < 0 || j >= arr.length || k < 0 || k >= arr.length) return;
    var tmp = arr[j]; arr[j] = arr[k]; arr[k] = tmp;
    writeComponents(idx, arr, true);
  }

  function remove(idx, j) {
    var layer = layerAt(idx);
    var arr = cloneComponents(layer);
    if (j < 0 || j >= arr.length) return;
    arr.splice(j, 1);
    writeComponents(idx, arr, true);
  }

  /* ---- the pick panel -------------------------------------------------------- */

  function buildPickPanel(idx) {
    var box = el("div", "mask-pick");
    box.setAttribute("data-mask-pickpanel", "");
    var hint = el("div", "mask-line mask-pick-hint",
      "Click the subject on the picture. Alt click anything that should be left out. "
      + "Drag a box round it if a click is not enough.");
    hint.setAttribute("data-mask-pick-hint", "");
    box.appendChild(hint);

    var counts = el("div", "mask-line mono muted",
      pick.points.length + " point" + (pick.points.length === 1 ? "" : "s")
      + ", " + pick.boxes.length + " box" + (pick.boxes.length === 1 ? "" : "es"));
    counts.setAttribute("data-mask-pick-points", "");
    box.appendChild(counts);

    var acts = el("div", "mask-line mask-acts");
    /* Collecting points works with the service down (they are just marks on
     * the picture), asking what is under them does not, so only this button
     * goes dead: whatever was already clicked is still there when the
     * service comes back. */
    var down = service.state === "down";
    var find = btn(pick.busy ? "Finding..." : "Find",
      down ? "The mask service is not running. Start it and press Retry above."
           : "Ask SAM what is under those points.",
      function () { runSegment(); });
    find.setAttribute("data-mask-pick-run", "");
    find.disabled = !!pick.busy || down;
    acts.appendChild(find);
    var clear = btn("Clear", "Forget the points and boxes.", function () {
      pick.points.length = 0; pick.boxes.length = 0; pick.candidates = null;
      if (global.WindowEditor && global.WindowEditor.setMaskPick) {
        global.WindowEditor.setMaskPick({ points: pick.points, boxes: pick.boxes,
          onPoint: function () { refreshAll(); },
          onBox: function () { refreshAll(); } });
      }
      refreshAll();
    });
    clear.setAttribute("data-mask-pick-clear", "");
    acts.appendChild(clear);
    var stop = btn("Done", "Leave pick mode.", function () { stopPick(); });
    stop.setAttribute("data-mask-pick-cancel", "");
    acts.appendChild(stop);
    box.appendChild(acts);

    if (pick.error) {
      box.appendChild(el("div", "mask-line mask-reason", pick.error));
    }

    if (pick.candidates) {
      var list = el("div", "mask-cands");
      list.setAttribute("data-mask-candidates", "");
      pick.candidates.forEach(function (inst) {
        var chip = el("button", "mask-cand");
        chip.type = "button";
        chip.setAttribute("data-mask-candidate", String(inst.id));
        var img = document.createElement("img");
        img.src = inst.overlay;
        img.alt = "candidate " + inst.id;
        img.draggable = false;
        chip.appendChild(img);
        var cap = el("span", "mono",
          (inst.label ? inst.label + " " : "") + Math.round(num(inst.score, 0) * 100) + "%");
        chip.appendChild(cap);
        chip.title = "score " + num(inst.score, 0).toFixed(3)
          + ", area " + (num(inst.area, 0) * 100).toFixed(1) + "% of the frame";
        chip.addEventListener("click", function (ev) {
          ev.stopPropagation();
          choosePick([inst.id], inst.label || null);
        });
        list.appendChild(chip);
      });
      if (pick.candidates.length > 1) {
        var all = el("button", "mask-cand mask-cand-all", "use all "
          + pick.candidates.length);
        all.type = "button";
        all.setAttribute("data-mask-candidate-all", "");
        all.title = "Track every candidate: each one becomes its own component, added "
          + "with op add so they can be feathered and subtracted separately.";
        all.addEventListener("click", function (ev) {
          ev.stopPropagation();
          choosePick("all", null);
        });
        list.appendChild(all);
      }
      box.appendChild(list);
    }
    return box;
  }

  /* ---- the whole section for one layer --------------------------------------- */

  function buildSection(idx, layer) {
    injectStyleOnce();
    var root = el("div", "masksec");
    root.id = "maskSection-" + idx;
    root.setAttribute("data-mask-layer", String(idx));

    var head = el("div", "mask-head");
    /* "Mask stack", not "Mask": the schema's own Mask group (matte in preset,
     * invert mask, and the legacy Window and Key folds) is further down the
     * same body, and two headings both called Mask would be a puzzle. */
    head.appendChild(el("span", "stagesub", "Mask stack"));
    head.appendChild(el("span", "spacer"));

    /* Display modes. off / overlay tint / black and white matte, as three
     * pressed buttons rather than a select, because they are switched
     * constantly while looking at a matte. */
    var modes = el("div", "mask-display");
    [["off", "Off", "No mask diagnostic over the picture."],
     ["overlay", "Overlay", "Tint the picture where this layer's tracked mattes are open. "
       + "Follows the playhead, so it moves with the subject during playback."],
     ["matte", "Matte", "Show the combined matte itself, black and white. This writes the "
       + "layer's own mask.show, which both engines render, so it plays in sync with the "
       + "picture and it is the exact matte rather than the overlay's approximation."]
    ].forEach(function (m) {
      var b = btn(m[1], m[2], function () { setDisplay(idx, m[0]); }, "toggle");
      b.setAttribute("data-mask-display-mode", m[0]);
      /* Off is the state of the viewer, not of a layer, so it reads as on
       * for every layer that is not the one being displayed. */
      var on = m[0] === "off"
        ? (display.mode === "off" || display.layer !== idx)
        : (display.layer === idx && display.mode === m[0]);
      b.classList.toggle("active", on);
      b.setAttribute("aria-pressed", on ? "true" : "false");
      modes.appendChild(b);
    });
    modes.setAttribute("data-mask-display", "");
    head.appendChild(modes);
    root.appendChild(head);

    root.appendChild(buildServiceLine());

    var list = el("div", "mask-comps");
    list.setAttribute("data-mask-components", "");
    var comps = componentsOf(layer);
    if (!comps.length) {
      list.appendChild(el("div", "mask-empty", "No mask components. Add one below: a SAM "
        + "selection that tracks through the clip, or one of the keys and shapes this "
        + "studio already had."));
    } else {
      comps.forEach(function (c, j) { list.appendChild(buildRow(idx, j, c || {}, comps.length)); });
    }
    root.appendChild(list);

    if (pick && pick.layer === idx) root.appendChild(buildPickPanel(idx));

    var addWrap = el("div", "mask-addwrap");
    var addBtn = btn("Add mask", "Add a component to this layer's mask stack.", function (ev) {
      ev.stopPropagation();
      var menu = addWrap.querySelector("[data-mask-addmenu]");
      if (!menu) menu = buildAddMenu(idx, addWrap);
      var wasOpen = menu.classList.contains("open");
      closeMenu();
      if (!wasOpen) { menu.classList.add("open"); openMenu = menu; }
    });
    addBtn.id = "maskAddBtn-" + idx;
    addBtn.setAttribute("data-mask-add", "");
    addWrap.appendChild(addBtn);
    root.appendChild(addWrap);

    if (comps.length) root.appendChild(buildFinesse(idx, layer));

    var note = noteFor(layer);
    if (note) {
      var n = el("div", "mask-note", note);
      n.setAttribute("data-mask-note", "");
      root.appendChild(n);
    }

    sections.push({ idx: idx, root: root });
    return root;
  }

  function noteFor(layer) {
    var comps = componentsOf(layer);
    if (!comps.length) return "";
    var mattes = 0, others = 0, finesse = false;
    comps.forEach(function (c) {
      if (!c || c.enabled === false) return;
      if (c.type === "matte") mattes++; else others++;
      if (String(c.op || "add") !== "add") others++;
    });
    var f = (layer.mask && layer.mask.finesse) || {};
    ["blur", "grow", "clean_black", "clean_white"].forEach(function (k) {
      if (Math.abs(num(f[k], 0)) > 1e-9) finesse = true;
    });
    if (!mattes) {
      return "The overlay tint only draws tracked mattes, so this stack has nothing to "
           + "tint. Use the Matte view to see it.";
    }
    if (others || finesse) {
      return "The overlay tint draws the tracked mattes only. This stack also has keys, "
           + "shapes, ops other than add, or finesse, so the Matte view is the exact "
           + "combined matte and the overlay is the quick one.";
    }
    return "";
  }

  function buildServiceLine() {
    var line = el("div", "mask-service");
    line.setAttribute("data-mask-service", "");
    line.setAttribute("data-mask-service-state", service.state);
    if (service.state === "ok") {
      var h = service.health || {};
      var q = h.queue || {};
      var bits = ["mask service: " + (h.backend || "?")];
      if (h.model) bits.push(h.model);
      if (q.running) bits.push("1 running");
      if (q.queued) bits.push(q.queued + " queued");
      if (h.memory && h.memory.rss_mb) bits.push(Math.round(h.memory.rss_mb) + " MB");
      line.appendChild(el("span", "mono muted", bits.join(", ")));
      return line;
    }
    if (service.state === "checking") {
      line.appendChild(el("span", "mono muted", "checking the mask service..."));
      return line;
    }
    line.appendChild(el("span", "mask-service-bad", "mask service not running"));
    if (service.startCmd) {
      var cmd = el("code", "mask-cmd", service.startCmd);
      cmd.setAttribute("data-mask-start-cmd", "");
      cmd.title = "Run this from the studio's own folder, then press Retry.";
      line.appendChild(cmd);
    } else if (service.error) {
      line.appendChild(el("span", "mono muted", service.error));
    }
    var retry = btn("Retry", "Ask the service again.", function () { pollNow(); });
    retry.setAttribute("data-mask-retry", "");
    line.appendChild(retry);
    return line;
  }

  function buildFinesse(idx, layer) {
    var f = (layer.mask && layer.mask.finesse) || {};
    var det = document.createElement("details");
    det.className = "layer-fold mask-finesse";
    det.setAttribute("data-mask-finesse", "");
    var sum = el("summary", "stagesub", "Finesse: " + finesseSummary(f));
    sum.style.cursor = "pointer";
    sum.title = "DaVinci's matte finesse, on the COMBINED matte: clean first, then grow "
      + "or shrink, then blur. Feather is per component, in each row above.";
    det.appendChild(sum);
    [["blur", "blur", 0, BLUR_MAX, 0.001, 0, 3,
      "A gaussian on the finished matte, as a fraction of the frame width."
      + BLUR_CAP_NOTE],
     ["grow", "grow / shrink", -0.03, 0.03, 0.0005, 0, 4,
      "Positive grows the matte, negative shrinks it. One pixel of radius is one pass, capped at 32."],
     ["clean_black", "clean black", 0, 1, 0.005, 0, 3,
      "Push everything below this to 0, with a soft knee."],
     ["clean_white", "clean white", 0, 1, 0.005, 0, 3,
      "Push everything above 1 minus this to 1, with a soft knee."]
    ].forEach(function (spec) {
      /* blur only: a saved value above the cap is DISPLAYED clamped, because
       * the engines clamp it and the panel must not show a number the picture
       * was not made with. Not done for the others, because their maxima are
       * the panel's own comfortable range and not a limit the engines apply:
       * grow keeps clamping in passes at render time, so pinning its readout
       * at 0.03 would be the same lie the other way round. */
      var saved = num(f[spec[0]], spec[5]);
      if (spec[0] === "blur") saved = Math.min(saved, BLUR_MAX);
      var w = Ctl.slider({
        label: spec[1], min: spec[2], max: spec[3], step: spec[4], def: spec[5],
        precision: spec[6], title: spec[7], value: saved,
        bipolar: spec[0] === "grow",
        onChange: function (v, c) { writeFinesse(idx, spec[0], v, c); }
      });
      w.el.setAttribute("data-mask-finesse-field", spec[0]);
      det.appendChild(w.el);
    });
    return det;
  }

  function finesseSummary(f) {
    var on = [];
    if (Math.abs(num(f.blur, 0)) > 1e-9) on.push("blur");
    if (Math.abs(num(f.grow, 0)) > 1e-9) on.push(num(f.grow, 0) > 0 ? "grow" : "shrink");
    if (Math.abs(num(f.clean_black, 0)) > 1e-9) on.push("clean black");
    if (Math.abs(num(f.clean_white, 0)) > 1e-9) on.push("clean white");
    return on.length ? on.join(", ") : "none";
  }

  /* ---- refresh --------------------------------------------------------------
   * layers.js rebuilds the whole layer list on every committed change, and it
   * calls buildSection again as part of that, so the normal path needs nothing
   * here. refreshAll is for the changes that do NOT go through the config: a
   * poll landing, a pick answering, the service coming back. It asks layers.js
   * to rebuild rather than patching the DOM in place, which is the same
   * "always rebuild on a committed change" rule layers.js already follows and
   * the reason there is no widget bookkeeping in this file at all. */
  var refreshPending = false;

  /* Everything the panel draws that does NOT come from the config, boiled
   * down to a string. A poll that lands with all of these the same has
   * nothing to redraw, and redrawing anyway would be actively wrong: the
   * rebuild throws the DOM away, so an open add menu would shut and a
   * half typed name would lose its caret, once every 1.2 seconds, forever. */
  function pollSignature() {
    var bits = [service.state, service.error];
    Object.keys(jobs).sort().forEach(function (k) {
      var j = jobs[k] || {};
      bits.push(k + ":" + j.state + ":" + j.done_frames + ":" + j.total_frames
        + ":" + j.queue_position + ":" + j.error);
    });
    Object.keys(matteInfo).sort().forEach(function (k) {
      var m = matteInfo[k] || {};
      bits.push(k + ":" + m.state + ":" + m.done_frames + ":" + m.error);
    });
    if (pick) {
      bits.push("pick:" + pick.layer + ":" + pick.comp + ":" + pick.busy + ":"
        + pick.points.length + ":" + pick.boxes.length + ":" + pick.error + ":"
        + (pick.candidates ? pick.candidates.length : "-"));
    }
    var h = service.health || {};
    bits.push((h.memory && h.memory.rss_mb) + ":" + (h.queue && h.queue.queued));
    bits.push("display:" + display.mode + ":" + display.layer);
    /* The clip and the rotation are not in the config, so a change to either
     * one never reaches the panel through Panels.refresh. They belong here
     * because they decide whether a matte is STALE: a matte tracked at 270 is
     * the wrong shape for a picture now turned to 180, and saying so is the
     * difference between a correction that has quietly stopped matching and a
     * badge that tells you why. */
    bits.push("view:" + (api && api.getClip ? api.getClip() : "")
      + ":" + (api && api.getRotation ? api.getRotation() : ""));
    return bits.join("|");
  }

  var lastSignature = null;

  function refreshAll(force) {
    if (refreshPending || !api || !api.refreshPanels) return;
    var sig = pollSignature();
    if (!force && sig === lastSignature) { setStatus(); return; }
    lastSignature = sig;
    refreshPending = true;
    requestAnimationFrame(function () {
      refreshPending = false;
      /* Never while the panel has something half done in it: a rebuild takes
       * the caret out of a name being typed and shuts an open add menu. The
       * change is not lost, it lands on the next state change or the next
       * interaction, because lastSignature was already moved on. */
      var a = document.activeElement;
      if (a && a.closest && a.closest(".masksec")
          && (a.tagName === "INPUT" || a.tagName === "SELECT")) { setStatus(); return; }
      if (openMenu) { setStatus(); return; }
      api.refreshPanels();
      setStatus();
    });
  }

  /* The one entry point app.js calls whenever the config or the playhead may
   * have moved (scheduleRender), plus layers.js after a rebuild. */
  function sync() {
    if (display.mode === "overlay") startLoop();
    else drawOverlay();
    setStatus();
    /* Cheap: refreshAll only rebuilds when pollSignature actually moved, and
     * the two things this catches (a clip switch, a rotation change) move it.
     * Without this a matte would go on claiming to be tracked for a picture
     * it no longer fits, because neither of those is a config field and
     * neither goes through Panels.refresh. */
    refreshAll();
  }

  function injectStyleOnce() {
    if (document.getElementById(STYLE_ID)) return;
    // Everything visual is in style.css; this only exists so the panel still
    // reads correctly if that file is served from an older cache.
    var s = document.createElement("style");
    s.id = STYLE_ID;
    s.textContent = ".masksec { margin: 6px 0 10px; }";
    document.head.appendChild(s);
  }

  function init(opts) {
    api = opts || null;
    pollNow();
    schedulePoll();
    setStatus();
  }

  global.Masks = {
    init: init,
    buildSection: buildSection,
    sync: sync,
    // layers.js calls this before rebuilding, so a stale section list cannot
    // grow without bound across rebuilds.
    beginRebuild: function () { sections = []; thumbs = []; },
    usesComponents: function (layer) { return componentsOf(layer).length > 0; },
    displayMode: function () { return display.mode; },
    setDisplay: setDisplay,
    picking: function () { return !!pick; },
    // For a spec that wants the facts without reading the DOM.
    state: function () {
      return { service: service.state, display: display.mode, lagging: lagging,
               jobs: Object.keys(watching).length };
    }
  };
})(window);
