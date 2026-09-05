/* Layers: any number of masked correction layers (contract C1 in
 * plan/2026-09-04-studio-layers/PLAN.md), replacing the old single
 * "secondary" qualifier plus "window" pair.
 *
 * This file owns everything the SCHEMA "layers" stage (kind "array") needs
 * that a fixed, once-declared control list cannot express: how many layer
 * sections exist, add / remove / duplicate / move, which one is selected,
 * and the client side migration that shows an old secondary+window config
 * as one layer without writing anything back until the user changes
 * something. panels.js only knows to hand this stage's body over (see the
 * "kind === array" hook in its build()) and to call refresh() after every
 * config change; SCHEMA.itemControls (schema.js) is the per-layer control
 * template, with paths RELATIVE to one layer -- this file prefixes
 * ["layers", i] itself before ever touching the live config.
 *
 * Every write still goes through api.onChange (app.js's onParamChange),
 * the exact function every slider and checkbox in every other stage calls,
 * so undo, the per clip autosave and the session publish all cover a layer
 * edit the same way they cover anything else. The one thing this file adds
 * on top is a small LOCAL auto-enable (see autoEnableLocal): panels.js's
 * own auto-enable is keyed off a static path table built once from SCHEMA,
 * which cannot know about a dynamic ["layers", 3, "mask", "window", "cx"]
 * path for a layer that did not exist when the page loaded, so it never
 * fires for anything in here. This file's own version does the same job
 * (touching a group's controls while its own "enabled" flag is off turns
 * that flag on, so the effort is never lost by looking like nothing
 * happened) for the two group flags a layer actually has: mask.window and
 * mask.key, and for the layer's own top level "enabled".
 *
 * Rendering strategy: rebuild() tears down and rebuilds the whole layer
 * list from the live config every time it runs. That is only on a
 * COMMITTED change (a slider release, a checkbox, undo/redo, a preset load,
 * a clip switch, an outside session patch) -- see panels.js's refresh(),
 * which only ever runs then -- never on every drag frame, so this is a
 * "release cadence" cost, not a "60fps" one. A rename field is the one
 * control that would fight a rebuild-on-every-keystroke, which is why it
 * writes with commit=false on "input" (no rebuild, see onParamChange in
 * app.js) and only commits on blur/Enter. Selecting a layer and collapsing
 * one do NOT go through rebuild() at all: both just toggle a class on the
 * DOM nodes already on screen (see selectLayer/applySelectionClasses and
 * the header's own collapse toggle), which is what keeps clicking into a
 * layer to select it safe to do in the same gesture as starting a drag
 * inside it.
 */
(function (global) {
  "use strict";

  var api = null;                 // {getConfig, onChange, refreshPanels}, set by init()
  var stageSpec = null;           // the SCHEMA stage entry (itemControls lives here)
  var listEl = null;              // .layers-list, rebuilt every rebuild()
  var countEl = null;             // "N layer(s)" in the toolbar
  var noteEl = null;              // the "shown from the old fields" migration note
  var itemRoots = [];             // one .layer-item per current layer, index-matched
  var selectedIndex = -1;         // index into the CURRENT effective layers array, or -1
  var collapsedMap = {};          // index -> bool, UI-only, see the header note above
  var STYLE_ID = "layersInlineStyle";

  /* ---- layer identity (follow-up 2: the selected marker after an undo) ----
   * The selected layer is tracked by a stable client-side id, never by array
   * index and never written into the config. Ordinary edits (a slider, a
   * checkbox, a rename) mutate a layer object IN PLACE, so idMap's WeakMap
   * lookup is a same-object hit and costs nothing. An undo/redo goes through
   * app.js's restore(), which JSON.parses a whole new set of objects, so the
   * PREVIOUS layer at that array position is gone and a brand new object
   * takes its place: idMap has never seen that new object before, so
   * resolveIds falls back to content matching.
   *
   * That fallback cannot just compare against the MOST RECENT rebuild: an
   * undo of a plain field edit (a drag, a slider release) restores a layer
   * to whatever it looked like BEFORE that edit, which is not what the last
   * rebuild saw (the last rebuild saw the edited value). A pure reorder
   * (moveLayer) does not have this problem, because the objects themselves
   * are not replaced by a move, only an undo touches identity at all, and by
   * then the content is back to something this layer's id has already been
   * seen wearing. So sigHistory keeps EVERY signature ever recorded for each
   * id (not only the latest), and matching checks the whole set: whichever
   * id's history contains the restored layer's exact content wins it back,
   * whether that content was seen one rebuild ago or many edits ago. */
  var idMap = new WeakMap();      // live layer object -> stable id (fast path)
  var sigHistory = {};            // id -> {sig: true, ...}, every content shape that id has ever had
  var nextId = 1;
  var selectedId = null;          // identity of the selected layer, or null
  var currentIds = [];            // ids parallel to the layers array as of the last rebuild()

  /* ---- per-group fold state (follow-up 1: collapse by default) ------------
   * "<id>:<group>" -> bool (true = open). Absent means collapsed, which is
   * the default every fold starts at. Keyed by layer IDENTITY (not array
   * index) for the same reason selection is: a layer that moves keeps
   * whichever of its own groups the user had open, rather than swapping fold
   * states with whatever layer is now sitting at its old row. */
  var foldState = {};

  /* One layer, one string, whatever order its keys happen to be in.
   *
   * Plain JSON.stringify was not enough once undo became the project's undo
   * (contract C4): a restored layer now arrives from the server, and the
   * server stores every commit with its keys SORTED (projects.py's _canon,
   * which is also what the commit id is derived from). So the same layer,
   * byte for byte the same content, stringifies differently coming back than
   * it did going out, no signature in sigHistory matches, the layer is given
   * a brand new id, and the selection marker lands on nothing. Sorting here
   * makes the signature about the CONTENT only, which is what every comment
   * around it already claims it is. */
  function canonSig(v) {
    if (v === null || typeof v !== "object") {
      var s = JSON.stringify(v);          // undefined for a function or undefined
      return s === undefined ? "undefined" : s;
    }
    if (Array.isArray(v)) {
      return "[" + v.map(canonSig).join(",") + "]";
    }
    return "{" + Object.keys(v).sort().map(function (k) {
      return JSON.stringify(k) + ":" + canonSig(v[k]);
    }).join(",") + "}";
  }

  function layerSig(layer) {
    try { return canonSig(layer); } catch (e) { return null; }
  }

  // Resolves a stable id for every layer in the CURRENT effective array,
  // reusing idMap's fast path or sigHistory's content match, minting a fresh
  // id only when neither finds anything. Only ever called from rebuild() (so
  // every OTHER function can treat currentIds/selectedIndex as already
  // correct for whatever the live config currently holds) except
  // ensureLayerAndSelect's bounds check, which reads the cached result rather
  // than re-resolving.
  function resolveIds(layers) {
    var used = {};
    var ids = layers.map(function (layer) {
      if (idMap.has(layer)) {
        var known = idMap.get(layer);
        if (!used[known]) { used[known] = true; return known; }
      }
      var sig = layerSig(layer);
      var matchId = null;
      if (sig !== null) {
        for (var id in sigHistory) {
          if (!Object.prototype.hasOwnProperty.call(sigHistory, id)) continue;
          if (used[id]) continue;
          if (sigHistory[id][sig]) { matchId = id; break; }
        }
      }
      if (matchId !== null) {
        var reused = Number(matchId);
        idMap.set(layer, reused);
        used[reused] = true;
        return reused;
      }
      var fresh = nextId++;
      idMap.set(layer, fresh);
      used[fresh] = true;
      return fresh;
    });
    layers.forEach(function (layer, i) {
      var id = ids[i];
      var sig = layerSig(layer);
      if (sig === null) return;
      if (!sigHistory[id]) sigHistory[id] = {};
      sigHistory[id][sig] = true;
    });
    currentIds = ids;
    return ids;
  }

  function num(v, d) { v = Number(v); return isFinite(v) ? v : d; }
  function toNumber(v) { var n = parseFloat(v); return Number.isNaN(n) ? 0 : n; }

  /* ---- the layer template (contract C1's JSON block, verbatim) ---------- */

  function defaultWindow() {
    return { enabled: false, shape: "ellipse", cx: 0.5, cy: 0.5, w: 0.6, h: 0.6,
             rotation: 0.0, softness: 0.15, invert: false };
  }
  function defaultKey() {
    return { enabled: false, invert: false, hue_center: 30.0, hue_width: 40.0,
             hue_soft: 15.0, sat_low: 0.1, sat_high: 1.0, sat_soft: 0.1,
             lum_low: 0.0, lum_high: 1.0, lum_soft: 0.1 };
  }
  function defaultCorrect() {
    return { exposure: 0.0, contrast: 1.0, pivot: null, saturation: 1.0,
             temperature: 0.0, tint: 0.0, hue_shift: 0.0, sat_gain: 1.0,
             lum_gain: 1.0, offset: [0.0, 0.0, 0.0], blur: 0.0, strength: 1.0 };
  }
  function newLayer(name) {
    return {
      enabled: true, name: name, placement: "before_look",
      mask: { show: false, invert: false, window: defaultWindow(), key: defaultKey() },
      correct: defaultCorrect()
    };
  }

  /* ---- migration: an old secondary+window config shown as one layer ----
   * Delegates to StudioGPU.migrateLayers (studio/static/gpu.js), the same
   * rule gpu.js's own GPU preview reads a legacy config through: one rule in
   * JavaScript, not two written by hand in parallel (a second copy in
   * cinegrade.py is the Python side of the same rule, per contract C1).
   * gpu.js's version takes the whole cfg and returns a whole cfg with a real
   * `layers` array (dropping `secondary`/`window`); this file only ever
   * needs that array's first and only entry. Read-only: does not touch the
   * cfg it is given (see effectiveLayers/ensureArray below for the two ways
   * this gets used, one that writes and one that does not).
   *
   * Falls back to a bare default layer only if gpu.js is somehow not loaded
   * (script tag missing, load order broken): effectiveLayers is only ever
   * called with cfg.secondary or cfg.window actually present, so this path
   * should not be reachable in a normal boot, and a plain default layer is a
   * safer failure than a thrown error over trying to hand-reimplement the
   * mapping a second time here. */
  function migrateLegacy(cfg) {
    if (global.StudioGPU && global.StudioGPU.migrateLayers) {
      var migrated = global.StudioGPU.migrateLayers(cfg);
      if (migrated && Array.isArray(migrated.layers) && migrated.layers.length) {
        return migrated.layers[0];
      }
    }
    return newLayer("Layer 1");
  }

  // Read-only view for rendering: never mutates cfg, so opening the panel
  // on an old preset shows the migrated layer without saving anything.
  function effectiveLayers(cfg) {
    if (!cfg) return [];
    if (Array.isArray(cfg.layers)) return cfg.layers;
    if (cfg.secondary || cfg.window) return [migrateLegacy(cfg)];
    return [];
  }

  function isVirtual(cfg) {
    return !!cfg && !Array.isArray(cfg.layers) && !!(cfg.secondary || cfg.window);
  }

  // The ONE place migration is actually written back, and only ever called
  // from a real edit path (layerEmit, or a structural op below), never from
  // a plain render. Once this runs, cfg.layers is a real array and the old
  // keys are gone: from here on this clip's grade is the new shape, exactly
  // the "nothing is written back until you change something" rule.
  function ensureArray(cfg) {
    if (!Array.isArray(cfg.layers)) {
      var migrated = (cfg.secondary || cfg.window) ? [migrateLegacy(cfg)] : [];
      cfg.layers = migrated;
      delete cfg.secondary;
      delete cfg.window;
    }
    return cfg.layers;
  }

  /* ---- writes ------------------------------------------------------------ */

  // subPath is relative to the layer, e.g. ["mask","window","cx"] or
  // ["name"]. This is what window-editor.js calls too (as Layers.emit),
  // so a drag on the on-picture shape and a slider in this panel go
  // through the exact same auto-enable and commit path.
  function layerEmit(idx, subPath, value, commit) {
    var cfg = api.getConfig();
    ensureArray(cfg);
    autoEnableLocal(cfg, idx, subPath);
    api.onChange(["layers", idx].concat(subPath), value, commit);
  }

  // Mirrors panels.js's autoEnableParent, one level deeper: a layer has its
  // OWN "enabled" (turned on by touching anything else in it) and two
  // group flags, mask.window.enabled and mask.key.enabled (each turned on
  // by touching anything else in THAT group). Every auto-enable write goes
  // out with commit=false, same reasoning as panels.js's version: the real
  // edit's own commit, right after this returns, is what actually pushes
  // undo history, so a drag that auto-enables a layer and ends in a release
  // is one undo step, not two.
  function autoEnableLocal(cfg, idx, subPath) {
    var layer = cfg.layers && cfg.layers[idx];
    if (!layer) return;
    if (subPath.length === 1 && subPath[0] === "enabled") return; // the edit IS the toggle
    if (layer.enabled === false) {
      api.onChange(["layers", idx, "enabled"], true, false);
    }
    [["mask", "window"], ["mask", "key"]].forEach(function (grp) {
      if (subPath.length <= grp.length) return;
      for (var i = 0; i < grp.length; i++) {
        if (subPath[i] !== grp[i]) return;
      }
      if (subPath[grp.length] === "enabled") return; // the edit IS that group's own toggle
      var g = Panels.getPath(layer, grp);
      if (g && g.enabled === false) {
        api.onChange(["layers", idx].concat(grp, ["enabled"]), true, false);
      }
    });
  }

  function commitLayers(arr) {
    api.onChange(["layers"], arr, true);
  }

  function addLayer() {
    var cfg = api.getConfig();
    var arr = ensureArray(cfg).slice();
    var layer = newLayer("Layer " + (arr.length + 1));
    // A brand new object: register its id right away, before anything else
    // could possibly see it, so the generic "no match, mint a fresh id" path
    // in resolveIds is never even reached for it (see resolveIds' own
    // comment on why that matters).
    var id = nextId++;
    idMap.set(layer, id);
    arr.push(layer);
    // Set BEFORE commitLayers: onChange's commit path runs Panels.refresh
    // (hence this file's own rebuild()) synchronously, so the selection has
    // to already be right by the time that happens, not after.
    selectedId = id;
    selectedIndex = arr.length - 1;
    commitLayers(arr);
  }

  function removeLayer(idx) {
    var cfg = api.getConfig();
    var arr = ensureArray(cfg).slice();
    if (idx < 0 || idx >= arr.length) return;
    var removedId = currentIds[idx];
    arr.splice(idx, 1);
    if (selectedId === removedId) { selectedId = null; selectedIndex = -1; }
    commitLayers(arr);
  }

  function duplicateLayer(idx) {
    var cfg = api.getConfig();
    var arr = ensureArray(cfg).slice();
    if (idx < 0 || idx >= arr.length) return;
    var copy = JSON.parse(JSON.stringify(arr[idx]));
    copy.name = (copy.name || ("Layer " + (idx + 1))) + " copy";
    var id = nextId++;
    idMap.set(copy, id);
    arr.splice(idx + 1, 0, copy);
    selectedId = id;
    selectedIndex = idx + 1;
    commitLayers(arr);
  }

  // Selection follows the LAYER, not the row: with identity-based tracking
  // (selectedId, never an index) a swap needs no bookkeeping at all here.
  // The same two object references just change position in the array;
  // rebuild(), which commitLayers triggers synchronously, resolves
  // selectedIndex fresh from selectedId against wherever they land.
  function moveLayer(idx, dir) {
    var cfg = api.getConfig();
    var arr = ensureArray(cfg).slice();
    var j = idx + dir;
    if (idx < 0 || idx >= arr.length || j < 0 || j >= arr.length) return;
    var tmp = arr[idx]; arr[idx] = arr[j]; arr[j] = tmp;
    commitLayers(arr);
  }

  /* Window button / window-editor.js: creates a layer (and selects it) only
   * when none exists at all, per the direct request; otherwise it just
   * makes sure the current selection is a real index, same as clicking
   * a layer's own header would. selectedIndex is kept correct by rebuild()
   * on every commit (including one that happened from outside this file,
   * such as an outside session patch), so a plain bounds check here is
   * enough: no need to re-resolve identity from scratch. */
  function ensureLayerAndSelect() {
    var cfg = api.getConfig();
    var layers = effectiveLayers(cfg);
    if (!layers.length) {
      addLayer();
      return true;
    }
    if (selectedIndex < 0 || selectedIndex >= layers.length) selectLayer(layers.length - 1);
    return false;
  }

  /* ---- selection (no rebuild: see the file header) -----------------------
   * Tracked by the layer's stable id (selectedId), never by array index:
   * see the "layer identity" block of module vars above for why an index
   * alone goes stale across an undo. selectLayer(idx) itself still takes an
   * index because every caller (a click, ensureLayerAndSelect, the header
   * click handler) already has one from the DOM or from effectiveLayers(),
   * built from the SAME rebuild() pass that produced currentIds; it is
   * currentIds[idx], not idx itself, that is remembered. */

  function applySelectionClasses() {
    itemRoots.forEach(function (root, i) {
      if (root) root.classList.toggle("selected", i === selectedIndex);
    });
  }

  function selectLayer(idx) {
    if (idx === selectedIndex) return;
    selectedId = currentIds[idx] !== undefined ? currentIds[idx] : null;
    selectedIndex = idx;
    applySelectionClasses();
    // The on-picture shape editor draws the SELECTED layer's mask.window;
    // this is the one place selection changes without an onChange (which
    // would have reached window-editor.js through scheduleRender on its
    // own), so it has to call sync() itself.
    if (global.WindowEditor) global.WindowEditor.sync();
  }

  /* ---- per-layer control rendering ---------------------------------------
   * Mirrors panels.js's own makeControl for the handful of kinds SCHEMA's
   * layer itemControls actually use (slider, select, check, sub, pivot,
   * trio); it is not reused directly because panels.js's version is a
   * private function inside its own closure, and because every write here
   * has to go through layerEmit (idx aware, with the local auto-enable)
   * instead of panels.js's own emit. Built fresh every rebuild() with the
   * CURRENT value baked in, rather than kept around and repainted, which is
   * what "always rebuild on refresh" (see the file header) buys back: no
   * separate widgets bookkeeping array to keep in sync with which layers
   * still exist. */
  function subhead(text) {
    var el = document.createElement("div");
    el.className = "stagesub";
    el.textContent = text;
    return el;
  }

  // Tags the control's own root element with the dotted field path it
  // edits (e.g. "mask.window.enabled"), so a test can select an exact
  // control inside a layer without depending on DOM order (checkboxes in
  // particular repeat the same "ctl-switch" class six times over in one
  // layer body). Not used by anything at runtime, only by
  // studio/tests/specs/13-window-editor.mjs and 16-layers.mjs.
  function tagged(el, spec) {
    if (el && spec && spec.path) el.dataset.path = spec.path.join(".");
    return el;
  }

  /* ---- fold groups (follow-up 1: Window, Key and Correct collapse) --------
   * Mirrors panels.js's own "fold" kind (a plain <details>/<summary>, the
   * kind lane L2 added for Tetra in the Slice stage): a heading you click to
   * open, holding its own controls. layers.js needs its own copy rather than
   * calling panels.js's version because every write inside has to go through
   * layerEmit (idx aware, with the LOCAL auto-enable), and because the
   * summary text is dynamic here (Tetra's is a fixed label; a layer's
   * Window/Key summary is on/off and Correct's lists which fields moved). */

  var CORRECT_FIELDS = [
    ["exposure", "exposure"], ["contrast", "contrast"], ["pivot", "pivot"],
    ["saturation", "saturation"], ["temperature", "temperature"], ["tint", "tint"],
    ["hue_shift", "hue shift"], ["sat_gain", "sat gain"], ["lum_gain", "luma gain"],
    ["offset", "offset push"], ["blur", "blur"], ["strength", "strength"]
  ];

  function correctSummary(layer) {
    var c = (layer && layer.correct) || {};
    var d = defaultCorrect();
    var changed = [];
    CORRECT_FIELDS.forEach(function (pair) {
      if (!Panels.same(c[pair[0]], d[pair[0]])) changed.push(pair[1]);
    });
    return changed.length ? changed.join(", ") : "default";
  }

  function foldSummary(spec, layer) {
    if (spec.id === "window") {
      var w = (layer && layer.mask && layer.mask.window) || {};
      return spec.label + ": " + (w.enabled ? "on" : "off");
    }
    if (spec.id === "key") {
      var k = (layer && layer.mask && layer.mask.key) || {};
      return spec.label + ": " + (k.enabled ? "on" : "off");
    }
    if (spec.id === "correct") return spec.label + ": " + correctSummary(layer);
    return spec.label;
  }

  // Collapsed by default: absent from foldState reads as false. Keyed by
  // the layer's own stable id (see resolveIds) so a reordered layer keeps
  // whichever of its own groups were open, and by spec.id (not the label
  // string) so a relabel in schema.js could never silently reset every
  // fold back to collapsed.
  function makeFold(spec, idx, layer, defLayer) {
    var key = (currentIds[idx] !== undefined ? currentIds[idx] : "idx" + idx) + ":" + spec.id;
    var det = document.createElement("details");
    det.className = "layer-fold";
    det.dataset.fold = spec.id; // addressable by tests, e.g. [data-fold="window"]; not read at runtime
    det.open = !!foldState[key];
    var sum = document.createElement("summary");
    sum.className = "stagesub";
    sum.textContent = foldSummary(spec, layer);
    sum.style.cursor = "pointer";
    if (spec.title) sum.title = spec.title;
    det.appendChild(sum);
    det.addEventListener("toggle", function () { foldState[key] = det.open; });
    (spec.controls || []).forEach(function (sub) {
      var el = makeItemControl(sub, idx, layer, defLayer);
      if (el) det.appendChild(el);
    });
    return det;
  }

  function makeItemControl(spec, idx, layer, defLayer) {
    if (spec.kind === "sub") return subhead(spec.label);
    if (spec.kind === "fold") return makeFold(spec, idx, layer, defLayer);

    if (spec.kind === "slider") {
      var val = toNumber(Panels.getPath(layer, spec.path));
      var w = Ctl.slider({
        label: spec.label, unit: spec.unit, title: spec.title,
        min: spec.min, max: spec.max, step: spec.step, def: spec.def,
        bipolar: spec.bipolar, precision: spec.precision, value: val,
        onChange: function (v, c) { layerEmit(idx, spec.path, spec.integer ? Math.round(v) : v, c); }
      });
      w.markDirty(!Panels.same(Panels.getPath(layer, spec.path), Panels.getPath(defLayer, spec.path)));
      return tagged(w.el, spec);
    }

    if (spec.kind === "select") {
      var cur = Panels.getPath(layer, spec.path);
      var sw = Ctl.select({
        label: spec.label, title: spec.title, options: spec.options,
        value: cur === undefined ? (spec.options[0].value === undefined ? spec.options[0] : spec.options[0].value) : cur,
        onChange: function (v, c) { layerEmit(idx, spec.path, spec.numeric ? Number(v) : v, c); }
      });
      sw.markDirty(!Panels.same(Panels.getPath(layer, spec.path), Panels.getPath(defLayer, spec.path)));
      return tagged(sw.el, spec);
    }

    if (spec.kind === "check") {
      var cw = Ctl.check({
        label: spec.label, title: spec.title, desc: spec.title,
        value: !!Panels.getPath(layer, spec.path),
        onChange: function (v, c) { layerEmit(idx, spec.path, v, c); }
      });
      cw.markDirty(!Panels.same(Panels.getPath(layer, spec.path), Panels.getPath(defLayer, spec.path)));
      return tagged(cw.el, spec);
    }

    if (spec.kind === "pivot") {
      var pr = Ctl.row(spec.label, "Contrast pivot for this layer's own correction. "
        + "Auto uses the same mid grey primaries.pivot does.");
      var auto = document.createElement("input");
      auto.type = "checkbox";
      var lbl = document.createElement("span");
      lbl.className = "muted small"; lbl.textContent = "auto";
      var curPivot = Panels.getPath(layer, spec.path);
      var isAuto = curPivot === null || curPivot === undefined;
      var pf = Ctl.numField({
        value: isAuto ? 0.336 : curPivot, min: 0, max: 1, step: 0.002, precision: 3,
        onChange: function (v, c) { if (!auto.checked) layerEmit(idx, spec.path, v, c); }
      });
      auto.checked = isAuto;
      pf.el.style.opacity = isAuto ? 0.4 : 1;
      auto.addEventListener("change", function () {
        pf.el.style.opacity = auto.checked ? 0.4 : 1;
        layerEmit(idx, spec.path, auto.checked ? null : pf.get(), true);
      });
      pr.slot.appendChild(auto);
      pr.slot.appendChild(lbl);
      pr.slot.appendChild(pf.el);
      return tagged(pr.el, spec);
    }

    if (spec.kind === "trio") {
      var r = Ctl.row(spec.label, spec.title);
      var sw2 = document.createElement("i");
      sw2.style.cssText = "width:14px;height:14px;border:1px solid var(--line2);display:block;flex:none";
      var startVal = Panels.getPath(layer, spec.path);
      var vals = (Array.isArray(startVal) ? startVal : spec.def).slice();
      function swatch() {
        var bias = spec.swatchBias || 0;
        var c = vals.map(function (v) {
          return Math.max(0, Math.min(255, Math.round((v + bias) * 255)));
        });
        sw2.style.background = "rgb(" + c[0] + "," + c[1] + "," + c[2] + ")";
      }
      var fields = [];
      ["R", "G", "B"].forEach(function (ch, i) {
        var f = Ctl.numField({
          value: vals[i], min: spec.min, max: spec.max, step: spec.step, precision: spec.precision,
          onChange: function (v, c) { vals[i] = v; swatch(); layerEmit(idx, spec.path, vals.slice(), c); }
        });
        f.el.title = ch;
        f.el.style.width = "48px";
        fields.push(f);
        r.slot.appendChild(f.el);
      });
      r.slot.appendChild(sw2);
      r.label.style.cursor = "pointer";
      r.label.title = "double click to reset";
      r.label.addEventListener("dblclick", function () {
        vals = spec.def.slice(); swatch();
        fields.forEach(function (f, i) { f.set(vals[i]); });
        layerEmit(idx, spec.path, vals.slice(), true);
      });
      swatch();
      return tagged(r.el, spec);
    }

    return null;
  }

  /* ---- one layer's header + body ----------------------------------------- */

  function actionBtn(icon, title, disabled, fn) {
    var b = document.createElement("button");
    b.type = "button";
    b.className = "btn iconbtn";
    b.title = title;
    b.disabled = !!disabled;
    b.appendChild(global.StudioIcons.render(icon, "rowicon"));
    b.addEventListener("click", function (ev) {
      ev.stopPropagation();
      fn();
    });
    return b;
  }

  function buildLayerItem(layer, idx, total, defLayer) {
    var root = document.createElement("div");
    root.className = "layer-item" + (collapsedMap[idx] ? " collapsed" : "");
    root.dataset.layerIndex = String(idx);

    var head = document.createElement("div");
    head.className = "layer-head";

    var nameInput = document.createElement("input");
    nameInput.type = "text";
    nameInput.className = "layer-name-input";
    nameInput.value = layer.name || ("Layer " + (idx + 1));
    nameInput.spellcheck = false;
    nameInput.style.cssText = "flex:1 1 auto; min-width: 32px;";
    // "input" (every keystroke) writes with commit=false, so it neither
    // pushes undo history nor triggers Panels.refresh -- see the file
    // header's note on why a rename would otherwise fight rebuild(). Only
    // blur/Enter actually commits.
    nameInput.addEventListener("input", function () {
      layerEmit(idx, ["name"], nameInput.value, false);
    });
    function commitName() { layerEmit(idx, ["name"], nameInput.value, true); }
    nameInput.addEventListener("blur", commitName);
    nameInput.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter") { ev.preventDefault(); nameInput.blur(); }
    });

    var enableBox = document.createElement("input");
    enableBox.type = "checkbox";
    enableBox.className = "ctl-switch";
    enableBox.title = "layer enabled";
    enableBox.checked = !!layer.enabled;
    enableBox.addEventListener("change", function () {
      layerEmit(idx, ["enabled"], enableBox.checked, true);
    });

    var spacer = document.createElement("span");
    spacer.className = "spacer";

    var dupBtn = actionBtn("Copy01Icon", "Duplicate layer", false, function () { duplicateLayer(idx); });
    var upBtn = actionBtn("ArrowUp01Icon", "Move up", idx === 0, function () { moveLayer(idx, -1); });
    var downBtn = actionBtn("ArrowDown01Icon", "Move down", idx === total - 1, function () { moveLayer(idx, 1); });
    var rmBtn = actionBtn("Delete01Icon", "Remove layer", false, function () { removeLayer(idx); });
    var chevron = global.StudioIcons.render("ChevronDownIcon", "layer-chevron");

    head.appendChild(nameInput);
    head.appendChild(enableBox);
    head.appendChild(spacer);
    head.appendChild(dupBtn);
    head.appendChild(upBtn);
    head.appendChild(downBtn);
    head.appendChild(rmBtn);
    head.appendChild(chevron);

    // Collapse toggle: any click on the header that is NOT one of its own
    // interactive children (the rename field, the enabled switch, an
    // action button -- each of those already has its own handler and must
    // not also flip open/closed).
    head.addEventListener("click", function (ev) {
      if (ev.target.closest("input,button,select,textarea")) return;
      collapsedMap[idx] = !collapsedMap[idx];
      root.classList.toggle("collapsed", !!collapsedMap[idx]);
    });

    var body = document.createElement("div");
    body.className = "layer-body";
    (stageSpec.itemControls || []).forEach(function (spec) {
      var el = makeItemControl(spec, idx, layer, defLayer);
      if (el) body.appendChild(el);
    });

    root.appendChild(head);
    root.appendChild(body);

    // "Selecting a layer: clicking a layer's header or any control in it
    // makes it the selected layer" -- one delegated listener on the whole
    // item, mousedown (not click) so a drag that starts on a slider inside
    // this layer selects it before the drag itself begins. Does not
    // rebuild anything (see selectLayer), so it cannot step on the very
    // control the user is about to drag.
    root.addEventListener("mousedown", function () { selectLayer(idx); });

    return root;
  }

  /* ---- section build / refresh ------------------------------------------- */

  function injectStyleOnce() {
    if (document.getElementById(STYLE_ID)) return;
    var style = document.createElement("style");
    style.id = STYLE_ID;
    style.textContent = [
      ".layers-toolbar { display:flex; align-items:center; gap:8px; padding:2px 0 8px; }",
      ".layers-count { color: var(--text-dim); font-size: 10px; }",
      ".layers-note { color: var(--text-dim); font-size: 10px; padding: 0 0 8px; line-height: 1.35; }",
      ".layers-empty { color: var(--text-dim); font-size: 11px; padding: 6px 2px 2px; }",
      ".layers-list { display:flex; flex-direction:column; gap:8px; }",
      ".layer-item { border:1px solid var(--line2); border-radius: var(--radius-sm); overflow:hidden; }",
      ".layer-item.selected { border-color: var(--accent); }",
      ".layer-head { display:flex; align-items:center; gap:6px; padding:6px 8px; cursor:pointer; user-select:none; background: var(--raised); }",
      ".layer-item.selected .layer-head { background: var(--accent-wash); }",
      ".layer-name-input { border-color: transparent; background: transparent; }",
      ".layer-name-input:hover, .layer-name-input:focus { background: var(--panel2); border-color: var(--line2); }",
      ".layer-chevron { width:14px; height:14px; flex:none; color: var(--text-dim); transition: transform 160ms ease-out; }",
      ".layer-item.collapsed .layer-chevron { transform: rotate(-90deg); }",
      ".layer-item.collapsed .layer-body { display:none; }",
      ".layer-body { padding: 8px 10px 10px; }",
      ".layer-fold { margin: 6px 0 2px; }",
      ".layer-fold > summary { cursor: pointer; }",
      ".layer-fold > summary::marker, .layer-fold > summary::-webkit-details-marker { color: var(--text-dim); }",
      ".layer-fold[open] > summary { margin-bottom: 2px; }"
    ].join("\n");
    document.head.appendChild(style);
  }

  function rebuild() {
    if (!listEl || !api) return;
    var cfg = api.getConfig();
    if (!cfg) return;
    var layers = effectiveLayers(cfg);

    // Resolves currentIds fresh against whatever object identities the live
    // config holds RIGHT NOW (same objects as last time for an ordinary
    // edit, brand new ones after an undo/redo/restore or a virtual/legacy
    // config, which recomputes a fresh object on every call), then recovers
    // selectedIndex from selectedId rather than trusting whatever index used
    // to be selected. -1 (not just clamped to the last row) when the
    // selected layer no longer exists at all, e.g. it was removed.
    resolveIds(layers);
    selectedIndex = selectedId === null ? -1 : currentIds.indexOf(selectedId);

    if (noteEl) noteEl.style.display = (layers.length > 0 && isVirtual(cfg)) ? "" : "none";
    if (countEl) countEl.textContent = layers.length + (layers.length === 1 ? " layer" : " layers");

    listEl.innerHTML = "";
    itemRoots = [];
    var defLayer = newLayer("");

    if (!layers.length) {
      var empty = document.createElement("div");
      empty.className = "layers-empty";
      empty.textContent = "No layers yet. Add one below, or press the Window "
        + "button over the picture to start one.";
      listEl.appendChild(empty);
    } else {
      layers.forEach(function (layer, idx) {
        var root = buildLayerItem(layer, idx, layers.length, defLayer);
        itemRoots.push(root);
        listEl.appendChild(root);
      });
    }
    applySelectionClasses();
  }

  // Called once by panels.js's build(), right after the stage's note is
  // appended to `body` (see the array-kind hook there): this owns
  // everything under that note from here on.
  function buildSection(body, stage) {
    stageSpec = stage;
    injectStyleOnce();

    var wrap = document.createElement("div");
    wrap.className = "layers-wrap";

    var toolbar = document.createElement("div");
    toolbar.className = "layers-toolbar";
    var addBtn = document.createElement("button");
    addBtn.type = "button";
    addBtn.id = "layersAddBtn";
    addBtn.className = "btn";
    var addIcon = global.StudioIcons.render("Add01Icon", "rowicon");
    addIcon.style.cssText = "vertical-align:middle;margin-right:4px;";
    addBtn.appendChild(addIcon);
    addBtn.appendChild(document.createTextNode("Add layer"));
    addBtn.addEventListener("click", function () { addLayer(); });
    countEl = document.createElement("span");
    countEl.className = "layers-count";
    toolbar.appendChild(addBtn);
    toolbar.appendChild(countEl);

    noteEl = document.createElement("div");
    noteEl.className = "layers-note";
    noteEl.textContent = "Shown from the old secondary and window fields. "
      + "Nothing is written back until you change something in a layer "
      + "below.";

    listEl = document.createElement("div");
    listEl.className = "layers-list";

    wrap.appendChild(toolbar);
    wrap.appendChild(noteEl);
    wrap.appendChild(listEl);
    body.appendChild(wrap);

    rebuild();
  }

  // Called by panels.js's refresh() after every committed change. The cfg
  // and defaults arguments are the same live objects api.getConfig() would
  // return; rebuild() re-reads through api on purpose (one source of truth,
  // see the file header) rather than trusting whatever was handed in here.
  function refresh() {
    rebuild();
  }

  function init(opts) {
    api = opts || null;
  }

  global.Layers = {
    init: init,
    buildSection: buildSection,
    refresh: refresh,
    getSelectedIndex: function () { return selectedIndex; },
    getLayers: effectiveLayers,
    emit: layerEmit,
    selectLayer: selectLayer,
    ensureLayerAndSelect: ensureLayerAndSelect
  };
})(window);
