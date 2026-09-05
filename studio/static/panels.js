/* Builds the right-hand parameter column from SCHEMA.
 *
 * Nothing here holds state. The live config in app.js is the single source of
 * truth; these widgets read from it on refresh and write back through onChange.
 * Two-way widgets that keep their own copy are how a JSON edit and a slider end
 * up disagreeing about what the grade is. */

(function (global) {
  "use strict";

  function getPath(obj, path) {
    var cur = obj;
    for (var i = 0; i < path.length; i++) {
      if (cur === null || cur === undefined) return undefined;
      cur = cur[path[i]];
    }
    return cur;
  }

  function setPath(obj, path, value) {
    var cur = obj;
    for (var i = 0; i < path.length - 1; i++) {
      if (typeof cur[path[i]] !== "object" || cur[path[i]] === null) cur[path[i]] = {};
      cur = cur[path[i]];
    }
    cur[path[path.length - 1]] = value;
  }

  function same(a, b) {
    if (Array.isArray(a) && Array.isArray(b)) {
      return a.length === b.length && a.every(function (v, i) { return same(v, b[i]); });
    }
    if (typeof a === "number" && typeof b === "number") return Math.abs(a - b) < 1e-9;
    return a === b;
  }

  var widgets = [];          // {paths:[], set(cfg), dirty(cfg)}
  var host = null;
  var api = null;
  var curveWidget = null;
  var hueCurveWidget = null;
  var lutSelects = [];
  // The live config itself, cached from the last refresh() call. Panels
  // otherwise holds no state (see the file header) -- this is read-only,
  // just enough for emit() below to check a sibling "enabled" flag before
  // writing. refresh() runs after every commit and after every wholesale
  // config swap (preset load, undo/redo, slot switch, an outside session
  // patch), so this is never stale when a real edit reaches emit().
  var lastCfg = null;

  // One outline glyph per pipeline stage, keyed by stage.id (not stage
  // order, so reordering SCHEMA can never silently swap two icons). Values
  // are @hugeicons/core-free-icons export names, resolved by StudioIcons
  // (studio/static/icons.js) against window.HUGEICONS
  // (studio/static/vendor/hugeicons.js, vendored by studio/tools/icons/
  // build.mjs) -- no path data lives in this file.
  var STAGE_ICON = {
    convert: "RefreshCwIcon", primaries: "FilterHorizontalIcon", curves: "EaseCurveControlPointsIcon",
    hue_curves: "EaseCurveControlPointsIcon", slice: "ColorPickerIcon",
    layers: "Layers01Icon", look: "Film01Icon", fx: "SparklesIcon",
    grain: "ChartScatterIcon", detail: "FocusIcon", letterbox: "AspectRatioIcon",
    output: "Download01Icon"
  };
  function useIcon(name, cls) {
    return global.StudioIcons.render(name, cls);
  }

  function subhead(text) {
    // .stagesub, not .panelhead: .panelhead is also the overlay dialogs'
    // (Raw config, Limits, Keys) boxed header class, which this must NOT
    // pull in now that .stage itself is no longer a box (see style.css).
    var el = document.createElement("div");
    el.className = "stagesub";
    el.textContent = text;
    return el;
  }

  function note(text, cls) {
    var el = document.createElement("div");
    el.className = "note" + (cls ? " " + cls : "");
    el.textContent = text;
    return el;
  }

  function toNumber(v) { var n = parseFloat(v); return Number.isNaN(n) ? 0 : n; }

  function build(container, options) {
    host = container;
    api = options;
    widgets = [];
    lutSelects = [];
    curveWidget = null;
    hueCurveWidget = null;
    host.innerHTML = "";

    SCHEMA.forEach(function (stage) {
      var box = document.createElement("section");
      box.className = "stage";
      box.dataset.stage = stage.id;

      var head = document.createElement("div");
      head.className = "stagehead";
      head.appendChild(useIcon(STAGE_ICON[stage.id] || "RefreshCwIcon", "stagehead-icon"));
      var nm = document.createElement("span"); nm.className = "stagehead-name"; nm.textContent = stage.name;
      var sp = document.createElement("span"); sp.className = "spacer";
      head.appendChild(nm); head.appendChild(sp);
      head.appendChild(useIcon("ChevronDownIcon", "stagehead-chevron"));
      head.addEventListener("click", function () { box.classList.toggle("collapsed"); });

      var body = document.createElement("div");
      body.className = "stagebody";
      if (stage.note) body.appendChild(note(stage.note));

      stage.controls.forEach(function (spec) {
        var el = makeControl(spec);
        if (el) body.appendChild(el);
      });

      // An array-kind stage (currently only "layers") has no fixed control
      // list of its own: its length depends on config.layers, not on
      // anything SCHEMA can declare once. Its real body is built by
      // layers.js (studio/static/layers.js), which owns everything from
      // here down -- add/remove/duplicate/move, per-layer collapsing, the
      // selected-layer marker -- the same way window-editor.js owns the
      // on-picture shape overlay without panels.js knowing its internals.
      if (stage.kind === "array" && global.Layers) {
        global.Layers.buildSection(body, stage);
      }

      box.appendChild(head);
      box.appendChild(body);
      host.appendChild(box);
    });
  }

  // Every "enabled" checkbox in SCHEMA, keyed by its own dotted path, built
  // once. Covers both shapes the direct request called out with one lookup:
  // a stage-level toggle (["curves", "enabled"]) and an FX sub-group toggle
  // (["fx", "halation", "enabled"]) are both just a CHK control whose path
  // ends in "enabled" -- there is no need to know which shape a given
  // control belongs to, only whether ITS PARENT has one of these.
  var ENABLE_PATHS = {};
  SCHEMA.forEach(function (stage) {
    stage.controls.forEach(function (spec) {
      if (spec.kind === "check" && spec.path[spec.path.length - 1] === "enabled") {
        ENABLE_PATHS[spec.path.join(".")] = spec.path;
      }
    });
  });

  // Moving a control inside a disabled group should turn the group on
  // instead of visibly doing nothing, per the direct request. Every real
  // edit funnels through emit(), so this is the one place that catches all
  // of them the same way, stage-level and FX-sub-group level alike, just by
  // looking at the changed path's own parent.
  //
  // Skips entirely for a programmatic update (preset load, undo, reset, an
  // outside session patch): those replace cfg() wholesale and repaint
  // through each widget's own set(cfg), which never calls onChange (see
  // controls.js -- every widget's set() passes silent=true). emit() is only
  // ever reached from a real DOM event a user caused, so there is nothing
  // extra to guard here.
  //
  // Always forwarded to api.onChange with commit=false, regardless of the
  // real edit's own commit flag: that runs setPath/scheduleRender (so the
  // group turns on and the viewer updates right away, even mid-drag) but
  // skips pushHistory()/refresh() (only commit=true does that). The real
  // edit's own onChange call, right after this returns with its own real
  // commit flag, is what actually pushes undo history -- and by then the
  // enabled flag this already flipped is part of the same live cfg that
  // call snapshots. So a drag that auto-enables a group and ends in a
  // release is one undo step, not two, and undoing it puts the group back
  // to off along with the value.
  function autoEnableParent(path) {
    if (!lastCfg || path.length < 2) return;
    var leafKey = path.join(".");
    if (ENABLE_PATHS[leafKey]) return; // the edit IS the toggle: never re-enable what the user just turned off
    var enablePath = ENABLE_PATHS[path.slice(0, -1).concat("enabled").join(".")];
    if (!enablePath) return; // this control's group has no enabled toggle at all
    if (getPath(lastCfg, enablePath) === false) {
      api.onChange(enablePath, true, false);
      // api.onChange already flipped the live cfg and re-rendered; the one
      // thing it does NOT do on a commit=false call is repaint widgets (that
      // only happens on Panels.refresh(), which only runs on commit). Paint
      // this one checkbox by hand so it visibly ticks in the same instant
      // the group turns on, not only once the user releases the drag.
      var enableKey = enablePath.join(".");
      widgets.forEach(function (w) {
        if (w.paths.length === 1 && w.paths[0].join(".") === enableKey) w.set(lastCfg);
      });
    }
  }

  function emit(path, value, commit) {
    autoEnableParent(path);
    api.onChange(path, value, commit);
  }

  function makeControl(spec) {
    if (spec.kind === "sub") return subhead(spec.label);

    /* A collapsed subgroup: a heading you click to open, holding its own
     * controls. Used for Tetra, which is six RGB triplets nobody wants in
     * their face until they are reaching for it. <details> rather than a
     * hand rolled toggle so keyboard and find-in-page work for free. */
    if (spec.kind === "fold") {
      var det = document.createElement("details");
      var sum = document.createElement("summary");
      sum.className = "stagesub";
      sum.textContent = spec.label;
      sum.style.cursor = "pointer";
      if (spec.title) sum.title = spec.title;
      det.appendChild(sum);
      (spec.controls || []).forEach(function (sub) {
        var e2 = makeControl(sub);
        if (e2) det.appendChild(e2);
      });
      return det;
    }

    if (spec.kind === "slider") {
      var w = Ctl.slider({
        // Unit used to be baked into the label text ("temperature (stops)"),
        // which is also what truncated it in the old fixed-width label
        // column. Ctl.slider now shows it after the VALUE instead ("0.000
        // stops"), which is also just where Unsloth's own panel puts a unit.
        label: spec.label, unit: spec.unit,
        title: spec.title, min: spec.min, max: spec.max, step: spec.step,
        def: spec.def, bipolar: spec.bipolar, precision: spec.precision,
        value: spec.def,
        onChange: function (v, c) {
          emit(spec.path, spec.integer ? Math.round(v) : v, c);
        }
      });
      if (spec.inert) {
        w.el.appendChild(document.createTextNode(""));
        w.row.label.title = spec.inert;
        w.row.label.textContent = spec.label + " (inert)";
        w.row.label.style.color = "var(--warn)";
      }
      widgets.push({
        paths: [spec.path],
        set: function (cfg) { w.set(toNumber(getPath(cfg, spec.path))); },
        dirty: function (cfg, def) {
          w.markDirty(!same(getPath(cfg, spec.path), getPath(def, spec.path)));
        }
      });
      return w.el;
    }

    if (spec.kind === "select") {
      var sw = Ctl.select({
        label: spec.label, title: spec.title, options: spec.options,
        value: spec.options[0].value === undefined ? spec.options[0] : spec.options[0].value,
        onChange: function (v, c) { emit(spec.path, spec.numeric ? Number(v) : v, c); }
      });
      widgets.push({
        paths: [spec.path],
        set: function (cfg) { sw.set(getPath(cfg, spec.path)); },
        dirty: function (cfg, def) {
          sw.markDirty(!same(getPath(cfg, spec.path), getPath(def, spec.path)));
        },
        widget: sw, spec: spec
      });
      return sw.el;
    }

    if (spec.kind === "check") {
      var cw = Ctl.check({
        // desc: spec.title again -- Ctl.check decides for itself whether
        // that text becomes a hover tooltip or a permanent line under a
        // bolded label (only "matte in preset" currently has one; plain
        // "enabled"/"invert key" rows have no title and get neither).
        label: spec.label, title: spec.title, desc: spec.title, value: false,
        onChange: function (v, c) { emit(spec.path, v, c); }
      });
      widgets.push({
        paths: [spec.path],
        set: function (cfg) { cw.set(getPath(cfg, spec.path)); },
        dirty: function (cfg, def) {
          cw.markDirty(!same(getPath(cfg, spec.path), getPath(def, spec.path)));
        }
      });
      return cw.el;
    }

    if (spec.kind === "wheels") {
      var grid = document.createElement("div");
      grid.className = "wheelgrid";
      spec.wheels.forEach(function (ws) {
        var w = Ctl.wheel({
          name: ws.name, value: [ws.def, ws.def, ws.def], def: ws.def,
          scale: ws.scale, min: ws.min, max: ws.max,
          masterMin: ws.masterMin, masterMax: ws.masterMax,
          step: ws.step, precision: ws.precision,
          onChange: function (v, c) { emit(ws.path, v, c); }
        });
        widgets.push({
          paths: [ws.path],
          set: function (cfg) {
            var v = getPath(cfg, ws.path);
            w.set(Array.isArray(v) ? v : [v, v, v]);
          },
          dirty: function () {}
        });
        grid.appendChild(w.el);
      });
      var wrap = document.createElement("div");
      wrap.appendChild(grid);
      wrap.appendChild(note("Puck tints, the bar under each wheel moves all "
        + "three channels together. Double click a wheel to reset it. Numbers "
        + "are the literal R, G and B values written to the preset."));
      return wrap;
    }

    if (spec.kind === "trio") {
      var r = Ctl.row(spec.label, spec.title);
      var sw2 = document.createElement("i");
      sw2.style.cssText = "width:14px;height:14px;border:1px solid var(--line2);display:block;flex:none";
      var fields = [];
      var vals = (spec.def || [0, 0, 0]).slice();
      function swatch() {
        var bias = spec.swatchBias || 0;
        var c = vals.map(function (v) {
          return Math.max(0, Math.min(255, Math.round((v + bias) * 255)));
        });
        sw2.style.background = "rgb(" + c[0] + "," + c[1] + "," + c[2] + ")";
      }
      ["R", "G", "B"].forEach(function (ch, i) {
        var f = Ctl.numField({
          value: vals[i], min: spec.min, max: spec.max, step: spec.step,
          precision: spec.precision,
          onChange: function (v, c) { vals[i] = v; swatch(); emit(spec.path, vals.slice(), c); }
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
        emit(spec.path, vals.slice(), true);
      });
      swatch();
      widgets.push({
        paths: [spec.path],
        set: function (cfg) {
          var v = getPath(cfg, spec.path) || spec.def;
          vals = v.slice();
          fields.forEach(function (f, i) { f.set(vals[i]); });
          swatch();
        },
        dirty: function (cfg, def) {
          r.el.classList.toggle("dirty",
            !same(getPath(cfg, spec.path), getPath(def, spec.path)));
        }
      });
      return r.el;
    }

    if (spec.kind === "pivot") {
      var pr = Ctl.row(spec.label, "Contrast pivot. Auto uses the real mid grey "
        + "of the working space (0.336 in dwg, 0.392 direct), which is what "
        + "keeps contrast from also shifting exposure.");
      var auto = document.createElement("input");
      auto.type = "checkbox";
      var lbl = document.createElement("span");
      lbl.className = "muted small"; lbl.textContent = "auto";
      var pf = Ctl.numField({
        value: 0.336, min: 0, max: 1, step: 0.002, precision: 3,
        onChange: function (v, c) { if (!auto.checked) emit(spec.path, v, c); }
      });
      auto.addEventListener("change", function () {
        emit(spec.path, auto.checked ? null : pf.get(), true);
      });
      pr.slot.appendChild(auto);
      pr.slot.appendChild(lbl);
      pr.slot.appendChild(pf.el);
      widgets.push({
        paths: [spec.path],
        set: function (cfg) {
          var v = getPath(cfg, spec.path);
          auto.checked = (v === null || v === undefined);
          pf.el.style.opacity = auto.checked ? 0.4 : 1;
          if (v !== null && v !== undefined) pf.set(v);
        },
        dirty: function (cfg, def) {
          pr.el.classList.toggle("dirty",
            !same(getPath(cfg, spec.path), getPath(def, spec.path)));
        }
      });
      return pr.el;
    }

    if (spec.kind === "lut") {
      var lr = Ctl.select({
        label: spec.label, options: [{ value: "", label: "(none)" }], value: "",
        onChange: function (v, c) { emit(spec.path, v === "" ? null : v, c); }
      });
      lutSelects.push(lr);
      widgets.push({
        paths: [spec.path],
        set: function (cfg) {
          var v = getPath(cfg, spec.path);
          lr.set(v === null || v === undefined ? "" : v);
        },
        dirty: function (cfg, def) {
          lr.markDirty(!same(getPath(cfg, spec.path), getPath(def, spec.path)));
        }
      });
      return lr.el;
    }

    if (spec.kind === "curves") {
      var wrap2 = document.createElement("div");
      wrap2.className = "curvewrap";
      var tabs = document.createElement("div");
      tabs.className = "curvetabs";
      var canvas = document.createElement("canvas");
      canvas.id = "curveCanvas";
      var live = { master: [[0, 0], [1, 1]], r: [[0, 0], [1, 1]],
                   g: [[0, 0], [1, 1]], b: [[0, 0], [1, 1]] };

      var ed = Ctl.curveEditor({
        canvas: canvas, value: live,
        onChange: function (curves, commit) {
          [["master", 0], ["r", 1], ["g", 2], ["b", 3]].forEach(function (pair) {
            emit(spec.paths[pair[1]], curves[pair[0]].map(function (p) {
              return [p[0], p[1]];
            }), commit);
          });
        }
      });
      curveWidget = ed;

      [["master", "Y"], ["r", "R"], ["g", "G"], ["b", "B"]].forEach(function (pair) {
        var b = document.createElement("button");
        b.className = "btn" + (pair[0] === "master" ? " active" : "");
        b.textContent = pair[1];
        b.addEventListener("click", function () {
          tabs.querySelectorAll(".btn").forEach(function (x) { x.classList.remove("active"); });
          b.classList.add("active");
          ed.setChannel(pair[0]);
        });
        tabs.appendChild(b);
      });
      var resetBtn = document.createElement("button");
      resetBtn.className = "btn";
      resetBtn.textContent = "reset all";
      resetBtn.addEventListener("click", function () { ed.resetAll(); });
      tabs.appendChild(resetBtn);

      wrap2.appendChild(tabs);
      wrap2.appendChild(canvas);
      var hint = document.createElement("div");
      hint.className = "curvehint";
      hint.textContent = "click to add a point, drag to move, alt or right click "
        + "to delete, double click the graph to reset this channel.";
      wrap2.appendChild(hint);

      widgets.push({
        paths: spec.paths,
        set: function (cfg) {
          var keys = ["master", "r", "g", "b"];
          var next = {};
          keys.forEach(function (k, i) {
            var pts = getPath(cfg, spec.paths[i]) || [[0, 0], [1, 1]];
            next[k] = pts.map(function (p) { return [p[0], p[1]]; });
          });
          live = next;
          ed.set(live);
        },
        dirty: function () {}
      });
      return wrap2;
    }

    /* The hue curve editor. Its own widget (studio/static/huecurve.js), not
     * Ctl.curveEditor: the neutral is a flat line rather than the diagonal,
     * the three hue axes are periodic, and y is an offset or a multiplier on
     * its own scale rather than an output level in [0, 1]. spec.paths is in
     * HueCurve.KEYS order, which is what maps a tab back to a config path. */
    if (spec.kind === "huecurves") {
      var hwrap = document.createElement("div");
      hwrap.className = "curvewrap";
      var htabs = document.createElement("div");
      htabs.className = "curvetabs";
      var hcanvas = document.createElement("canvas");
      hcanvas.id = "hueCurveCanvas";
      var hlive = {};
      global.HueCurve.KEYS.forEach(function (k) { hlive[k] = []; });

      var hed = global.HueCurve.editor({
        canvas: hcanvas, value: hlive,
        onChange: function (key, pts, commit) {
          var i = global.HueCurve.KEYS.indexOf(key);
          emit(spec.paths[i], pts.map(function (p) { return [p[0], p[1]]; }), commit);
        }
      });
      hueCurveWidget = hed;

      global.HueCurve.KEYS.forEach(function (k, i) {
        var b2 = document.createElement("button");
        b2.className = "btn" + (i === 0 ? " active" : "");
        b2.textContent = global.HueCurve.AXES[k].tab;
        b2.title = global.HueCurve.AXES[k].name + ": y is "
          + global.HueCurve.AXES[k].yLabel;
        b2.dataset.curve = k;
        b2.addEventListener("click", function () {
          htabs.querySelectorAll(".btn").forEach(function (x) { x.classList.remove("active"); });
          b2.classList.add("active");
          hed.setChannel(k);
        });
        htabs.appendChild(b2);
      });
      var hreset = document.createElement("button");
      hreset.className = "btn";
      hreset.textContent = "reset all";
      hreset.addEventListener("click", function () {
        global.HueCurve.KEYS.forEach(function (k, i) {
          hlive[k] = [];
          emit(spec.paths[i], [], true);
        });
        hed.set(hlive);
      });
      htabs.appendChild(hreset);

      hwrap.appendChild(htabs);
      hwrap.appendChild(hcanvas);
      var hhint = document.createElement("div");
      hhint.className = "curvehint";
      hhint.textContent = "click to add a point, drag to move, alt or right "
        + "click to delete any point, double click the graph to clear this "
        + "curve back to the identity. The olive line is the neutral.";
      hwrap.appendChild(hhint);

      widgets.push({
        paths: spec.paths,
        set: function (cfg) {
          var next = {};
          global.HueCurve.KEYS.forEach(function (k, i) {
            var pts = getPath(cfg, spec.paths[i]) || [];
            next[k] = pts.map(function (p) { return [p[0], p[1]]; });
          });
          hlive = next;
          hed.set(hlive);
        },
        dirty: function () {}
      });
      return hwrap;
    }

    return null;
  }

  function refresh(cfg, defaults) {
    lastCfg = cfg;
    widgets.forEach(function (w) {
      w.set(cfg);
      if (w.dirty) w.dirty(cfg, defaults);
    });
    // The encode control has no effect on the direct path, so grey it out
    // rather than let it look like a setting that is being ignored.
    widgets.forEach(function (w) {
      if (w.spec && w.spec.path && w.spec.path.join(".") === "convert.encode") {
        w.widget.setDisabled(cfg.convert.working_space === "direct");
      }
    });
    SCHEMA.forEach(function (stage) {
      if (!stage.enable) return;
      var box = host.querySelector('[data-stage="' + stage.id + '"]');
      if (box) box.classList.toggle("off", !getPath(cfg, stage.enable));
    });
    // Every refresh (a commit, an undo/redo, a preset load, a clip switch, an
    // outside session patch) may have changed config.layers itself, not just
    // a value inside it -- an add, remove, duplicate or reorder replaces the
    // whole array (see layers.js's commitLayers), which the plain widgets
    // loop above has no entry for at all. Layers.refresh reads the current
    // array fresh every time rather than diffing against what it last drew.
    if (global.Layers) global.Layers.refresh(cfg, defaults);
  }

  function setLooks(names) {
    var opts = [{ value: "", label: "(none)" }].concat(names.map(function (n) {
      return { value: n, label: n };
    }));
    lutSelects.forEach(function (s) {
      var keep = s.input.value;
      s.setOptions(opts, keep);
    });
  }

  function resizeCurves() {
    if (curveWidget) curveWidget.resize();
    if (hueCurveWidget) hueCurveWidget.resize();
  }

  function collapseAll(on) {
    host.querySelectorAll(".stage").forEach(function (s) {
      s.classList.toggle("collapsed", !!on);
    });
  }

  global.Panels = {
    // emit is exported for one caller: the on-picture shape editor
    // (window-editor.js), which is a control for the same config the panel
    // shows and therefore has to take the same route into it, auto-enable
    // included. Anything else that edits the config should go through
    // app.js's onParamChange directly.
    build: build, refresh: refresh, setLooks: setLooks, emit: emit,
    getPath: getPath, setPath: setPath, same: same,
    resizeCurves: resizeCurves, collapseAll: collapseAll
  };
})(window);
