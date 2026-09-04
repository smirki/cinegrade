/* Control primitives for Fixxr Studio.
 *
 * Every control reports twice: continuously while the gesture is live, so the
 * viewer can update, and once more with commit=true when the pointer is
 * released. Undo history is written on commit only, otherwise dragging one
 * slider across the panel would push a hundred entries and undo would become
 * useless. */

(function (global) {
  "use strict";

  function clamp(v, lo, hi) { return v < lo ? lo : (v > hi ? hi : v); }

  function fmt(v, precision) {
    if (v === null || v === undefined || Number.isNaN(v)) return "--";
    return Number(v).toFixed(precision);
  }

  /* ---- drag-scrub numeric field ---------------------------------------- */

  function numField(opts) {
    var el = document.createElement("div");
    // `plain: true` is Ctl.slider's own value-on-the-label-line (see
    // style.css .ctl-value): same widget, same edit/drag behaviour, just no
    // boxed pill around it. Trio/pivot/wheel fields stay .numfield (boxed),
    // per the direct request that those adjacent-to-the-wheels readouts
    // stay visually as they were.
    el.className = opts.plain ? "ctl-value" : "numfield";
    el.tabIndex = 0;
    var value = opts.value;
    var precision = opts.precision === undefined ? 3 : opts.precision;
    var step = opts.step || 0.01;
    var min = opts.min === undefined ? -Infinity : opts.min;
    var max = opts.max === undefined ? Infinity : opts.max;
    // Display-only suffix (a unit, e.g. " stops"): parseFloat stops at the
    // first non-numeric character, so it never has to be stripped back out
    // again for editing or for the commit read in endEdit below.
    var suffix = opts.suffix || "";
    var editing = false;

    function paint() { if (!editing) el.textContent = fmt(value, precision) + suffix; }

    function set(v, silent) {
      value = clamp(v, min, max);
      paint();
      if (!silent) opts.onChange(value, false);
    }

    function beginEdit() {
      if (editing) return;
      editing = true;
      el.classList.add("editing");
      el.contentEditable = "true";
      el.textContent = fmt(value, precision);
      var r = document.createRange();
      r.selectNodeContents(el);
      var s = window.getSelection();
      s.removeAllRanges();
      s.addRange(r);
      el.focus();
    }

    el.addEventListener("mousedown", function (ev) {
      if (editing || ev.button !== 0) return;
      ev.preventDefault();
      var x0 = ev.clientX, v0 = value, moved = false;
      function move(e) {
        // Shift is the fine gear and cmd/alt the coarse one, the same
        // convention every NLE uses for a scrubbable field.
        var mult = e.shiftKey ? 0.1 : (e.metaKey || e.altKey ? 10 : 1);
        var dx = e.clientX - x0;
        if (Math.abs(dx) > 2) moved = true;
        set(v0 + dx * step * mult);
      }
      function up() {
        document.removeEventListener("mousemove", move);
        document.removeEventListener("mouseup", up);
        document.body.style.cursor = "";
        if (moved) {
          opts.onChange(value, true);
        } else {
          // A mousedown that never moved is a plain click: on the boxed
          // .numfield that used to just do nothing (dblclick was the only
          // way in); on the plain-text .ctl-value there is no boxed
          // affordance left to hint "double click me", so a single click
          // has to be the edit gesture directly. Kept for both variants
          // rather than branched on opts.plain: a lone click doing nothing
          // on the boxed field was never a deliberate feature, just what
          // was left when dblclick was the only entry point.
          beginEdit();
        }
      }
      document.addEventListener("mousemove", move);
      document.addEventListener("mouseup", up);
      document.body.style.cursor = "ew-resize";
    });

    el.addEventListener("dblclick", beginEdit);

    function endEdit(apply) {
      if (!editing) return;
      editing = false;
      el.contentEditable = "false";
      el.classList.remove("editing");
      if (apply) {
        var n = parseFloat(el.textContent);
        if (!Number.isNaN(n)) { set(n, true); opts.onChange(value, true); }
      }
      paint();
    }
    el.addEventListener("keydown", function (ev) {
      if (!editing) {
        if (ev.key === "ArrowUp" || ev.key === "ArrowDown") {
          ev.preventDefault();
          var mult = ev.shiftKey ? 0.1 : (ev.metaKey || ev.altKey ? 10 : 1);
          set(value + (ev.key === "ArrowUp" ? 1 : -1) * step * mult);
          opts.onChange(value, true);
        }
        return;
      }
      if (ev.key === "Enter") { ev.preventDefault(); endEdit(true); el.blur(); }
      if (ev.key === "Escape") { ev.preventDefault(); endEdit(false); el.blur(); }
    });
    el.addEventListener("blur", function () { endEdit(true); });

    paint();
    return { el: el, set: function (v) { set(v, true); }, get: function () { return value; } };
  }

  /* ---- slider track ---------------------------------------------------- */

  function track(opts) {
    var el = document.createElement("div");
    el.className = "track";
    var rail = document.createElement("i"); rail.className = "rail";
    var fill = document.createElement("i"); fill.className = "fill";
    var knob = document.createElement("i"); knob.className = "knob";
    el.appendChild(rail); el.appendChild(fill); el.appendChild(knob);

    var min = opts.min, max = opts.max, value = opts.value;
    // A bipolar parameter gets a visible detent at its neutral value and its
    // fill grows out of that point, so "no move" is readable at a glance
    // instead of having to read the number.
    var origin = opts.bipolar ? opts.def : min;
    if (opts.bipolar) {
      var d = document.createElement("i");
      d.className = "detent";
      d.style.left = pct(origin) + "%";
      el.appendChild(d);
    }

    function pct(v) { return clamp((v - min) / (max - min), 0, 1) * 100; }

    function paint() {
      var p = pct(value), o = pct(origin);
      // clamp(), not a bare percent: the knob is a centered 14px circle (see
      // .track .knob in style.css), so at p=0 or p=100 a plain "left:p%" pushes
      // its far half past the track's own edge -- same edge-overhang class of
      // bug as the grid blowout documented on .stage, just at the single-element
      // level. Insetting by half the knob's width keeps it a full circle inside
      // the track at both extremes instead of getting clipped by the parent's
      // overflow:hidden.
      knob.style.left = "clamp(7px, " + p + "%, calc(100% - 7px))";
      fill.style.left = Math.min(p, o) + "%";
      fill.style.width = Math.abs(p - o) + "%";
    }

    function set(v, silent) {
      value = clamp(v, min, max);
      paint();
      if (!silent) opts.onChange(value, false);
    }

    function fromX(clientX) {
      var r = el.getBoundingClientRect();
      return min + clamp((clientX - r.left) / r.width, 0, 1) * (max - min);
    }

    el.addEventListener("mousedown", function (ev) {
      if (ev.button !== 0) return;
      ev.preventDefault();
      var fine = ev.shiftKey;
      var x0 = ev.clientX, v0 = value;
      if (!fine) set(fromX(ev.clientX));
      function move(e) {
        if (e.shiftKey || fine) {
          var r = el.getBoundingClientRect();
          set(v0 + ((e.clientX - x0) / r.width) * (max - min) * 0.15);
        } else {
          set(fromX(e.clientX));
        }
      }
      function up() {
        document.removeEventListener("mousemove", move);
        document.removeEventListener("mouseup", up);
        opts.onChange(value, true);
      }
      document.addEventListener("mousemove", move);
      document.addEventListener("mouseup", up);
    });

    el.addEventListener("dblclick", function (ev) {
      ev.preventDefault();
      set(opts.def);
      opts.onChange(value, true);
    });

    paint();
    return { el: el, set: function (v) { set(v, true); } };
  }

  /* ---- composed rows --------------------------------------------------- */

  function row(labelText, title) {
    var el = document.createElement("div");
    el.className = "ctl";
    var lab = document.createElement("label");
    lab.className = "ctl-label";
    lab.textContent = labelText;
    if (title) lab.title = title;
    var slot = document.createElement("div");
    slot.className = "slot";
    el.appendChild(lab); el.appendChild(slot);
    return { el: el, label: lab, slot: slot };
  }

  /* Two-line row (UNSLOTH-TOKENS.md's Configuration panel shape): line 1 is
   * label-left / value-right, line 2 is the full-width track. Built by hand
   * rather than through row() above, because row()'s single-line
   * label+slot shape has nowhere for a second line to go without every
   * OTHER row() caller (select/trio/pivot) also picking up a wrapper div
   * they do not want. */
  function slider(opts) {
    var el = document.createElement("div");
    el.className = "ctl ctl-slider";

    var head = document.createElement("div");
    head.className = "ctl-head";
    var lab = document.createElement("label");
    lab.className = "ctl-label";
    lab.textContent = opts.label;
    head.appendChild(lab);

    var num, trk;
    num = numField({
      value: opts.value, min: opts.min, max: opts.max, step: opts.step,
      precision: opts.precision, plain: true,
      suffix: opts.unit ? " " + opts.unit : "",
      onChange: function (v, c) { if (trk) trk.set(v); opts.onChange(v, c); }
    });
    head.appendChild(num.el);

    trk = track({
      min: opts.min, max: opts.max, value: opts.value, def: opts.def,
      bipolar: opts.bipolar,
      onChange: function (v, c) { if (num) num.set(v); opts.onChange(v, c); }
    });

    el.appendChild(head);
    el.appendChild(trk.el);

    // Reset lives on the label, same gesture as before: the value is now a
    // click target in its own right (click to edit), so double clicking IT
    // would be ambiguous with entering edit mode. The label and the track
    // both still carry the old double-click-to-reset.
    lab.title = (opts.title ? opts.title + "\n" : "") +
      "double click to reset to " + fmt(opts.def, opts.precision) + (opts.unit ? " " + opts.unit : "");
    lab.style.cursor = "pointer";
    lab.addEventListener("dblclick", function () {
      if (num) num.set(opts.def);
      if (trk) trk.set(opts.def);
      opts.onChange(opts.def, true);
    });

    return {
      el: el, row: { el: el, label: lab, slot: head },
      set: function (v) { if (num) num.set(v); if (trk) trk.set(v); },
      markDirty: function (on) { el.classList.toggle("dirty", !!on); }
    };
  }

  function select(opts) {
    var r = row(opts.label, opts.title);
    var sel = document.createElement("select");
    sel.style.flex = "1 1 auto";
    (opts.options || []).forEach(function (o) {
      var op = document.createElement("option");
      op.value = String(o.value === undefined ? o : o.value);
      op.textContent = o.label === undefined ? String(o) : o.label;
      sel.appendChild(op);
    });
    sel.value = String(opts.value);
    sel.addEventListener("change", function () { opts.onChange(sel.value, true); });
    r.slot.appendChild(sel);
    return {
      el: r.el, input: sel,
      set: function (v) { sel.value = v === null ? "" : String(v); },
      setOptions: function (list, keep) {
        sel.innerHTML = "";
        list.forEach(function (o) {
          var op = document.createElement("option");
          op.value = String(o.value === undefined ? o : o.value);
          op.textContent = o.label === undefined ? String(o) : o.label;
          sel.appendChild(op);
        });
        if (keep !== undefined) sel.value = keep === null ? "" : String(keep);
      },
      setDisabled: function (on) { sel.disabled = !!on; },
      markDirty: function (on) { r.el.classList.toggle("dirty", !!on); }
    };
  }

  /* Boolean row: a pill toggle switch on the right, per the direct request.
   * `opts.desc`, when present (schema passes its `title` text through as
   * both), is shown as a permanent grey line under a bolded label instead
   * of only living in a hover tooltip -- "where a boolean has an
   * explanation, the label is bold ... a grey description sits under it". */
  function check(opts) {
    var el = document.createElement("div");
    el.className = "ctl ctl-check";

    var textWrap = document.createElement("div");
    textWrap.className = "ctl-check-text";
    var lab = document.createElement("label");
    lab.className = "ctl-label";
    lab.textContent = opts.label;
    // Tooltip only when there is no on-screen description already saying
    // the same thing -- otherwise the hover would just repeat the line
    // sitting right underneath it.
    if (opts.title && !opts.desc) lab.title = opts.title;
    textWrap.appendChild(lab);
    if (opts.desc) {
      el.classList.add("has-desc");
      var d = document.createElement("div");
      d.className = "ctl-check-desc";
      d.textContent = opts.desc;
      textWrap.appendChild(d);
    }

    var box = document.createElement("input");
    box.type = "checkbox";
    box.className = "ctl-switch";
    box.checked = !!opts.value;
    box.addEventListener("change", function () { opts.onChange(box.checked, true); });

    // Clicking anywhere in the label/description also flips the switch (the
    // same reach a native <label for> gives a checkbox), without an id
    // round trip through every caller. Guarded against the switch itself so
    // a direct click there is not double-toggled by this handler too.
    textWrap.addEventListener("click", function (ev) {
      if (ev.target === box) return;
      box.checked = !box.checked;
      box.dispatchEvent(new Event("change"));
    });

    el.appendChild(textWrap);
    el.appendChild(box);

    return {
      el: el, input: box,
      set: function (v) { box.checked = !!v; },
      markDirty: function (on) { el.classList.toggle("dirty", !!on); }
    };
  }

  /* ---- colour wheel ---------------------------------------------------- */

  // Channel directions on the wheel, in degrees measured from the top going
  // clockwise. Three unit vectors 120 degrees apart sum to zero, which is what
  // makes a puck move a pure hue push: the three channel offsets always cancel,
  // so the wheel tints without changing overall level. The bar underneath is
  // the level control, kept separate for exactly that reason.
  var DIRS = { r: 90, g: 210, b: 330 };

  function dirVec(deg) {
    var a = deg * Math.PI / 180;
    return [Math.cos(a), Math.sin(a)];
  }
  var DV = { r: dirVec(DIRS.r), g: dirVec(DIRS.g), b: dirVec(DIRS.b) };

  function puckToResidual(x, y, scale) {
    return {
      r: scale * (x * DV.r[0] + y * DV.r[1]),
      g: scale * (x * DV.g[0] + y * DV.g[1]),
      b: scale * (x * DV.b[0] + y * DV.b[1])
    };
  }

  function residualToPuck(res, scale) {
    // Inverse of the above. For three unit vectors 120 apart the outer product
    // sum is 1.5 * I, hence the 2/3.
    var x = (2 / 3) * (res.r * DV.r[0] + res.g * DV.g[0] + res.b * DV.b[0]) / scale;
    var y = (2 / 3) * (res.r * DV.r[1] + res.g * DV.g[1] + res.b * DV.b[1]) / scale;
    return [x, y];
  }

  var wheelBitmap = null;
  function wheelImage(size) {
    if (wheelBitmap && wheelBitmap.width === size) return wheelBitmap;
    var c = document.createElement("canvas");
    c.width = c.height = size;
    var ctx = c.getContext("2d");
    var img = ctx.createImageData(size, size);
    var rad = size / 2;
    for (var py = 0; py < size; py++) {
      for (var px = 0; px < size; px++) {
        var x = (px - rad + 0.5) / rad;
        var y = -(py - rad + 0.5) / rad;
        var d = Math.sqrt(x * x + y * y);
        var i = (py * size + px) * 4;
        if (d > 1.0) { img.data[i + 3] = 0; continue; }
        // Paint the disc with exactly the offsets the puck would apply, so the
        // wheel is a picture of its own behaviour rather than a generic hue
        // circle that happens to sit next to it.
        var res = puckToResidual(x, y, 1.0);
        img.data[i] = clamp(128 + res.r * 190, 0, 255);
        img.data[i + 1] = clamp(128 + res.g * 190, 0, 255);
        img.data[i + 2] = clamp(128 + res.b * 190, 0, 255);
        img.data[i + 3] = d > 0.985 ? 255 * (1 - (d - 0.985) / 0.015) : 255;
      }
    }
    ctx.putImageData(img, 0, 0);
    wheelBitmap = c;
    return c;
  }

  /* opts: name, value [r,g,b], def (scalar neutral), scale, masterMin,
     masterMax, masterStep, precision, onChange(triplet, commit) */
  function wheel(opts) {
    var size = 92;
    var wrap = document.createElement("div");
    wrap.className = "wheel";

    var head = document.createElement("div");
    head.className = "wname";
    var nm = document.createElement("span"); nm.textContent = opts.name;
    var rst = document.createElement("u"); rst.textContent = "reset";
    head.appendChild(nm); head.appendChild(rst);

    var canvas = document.createElement("canvas");
    canvas.width = canvas.height = size;
    canvas.style.width = canvas.style.height = size + "px";
    var ctx = canvas.getContext("2d");

    var value = opts.value.slice();
    var neutral = opts.def;

    function master() { return (value[0] + value[1] + value[2]) / 3; }
    function residual() {
      var m = master();
      return { r: value[0] - m, g: value[1] - m, b: value[2] - m };
    }

    function paint() {
      ctx.clearRect(0, 0, size, size);
      ctx.drawImage(wheelImage(size), 0, 0);
      var p = residualToPuck(residual(), opts.scale);
      var d = Math.sqrt(p[0] * p[0] + p[1] * p[1]);
      if (d > 1) { p[0] /= d; p[1] /= d; }
      var cx = size / 2 + p[0] * size / 2;
      var cy = size / 2 - p[1] * size / 2;
      ctx.strokeStyle = "rgba(0,0,0,0.8)";
      ctx.lineWidth = 3;
      ctx.beginPath(); ctx.arc(cx, cy, 4.5, 0, 6.2832); ctx.stroke();
      ctx.strokeStyle = "#ffffff";
      ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.arc(cx, cy, 4.5, 0, 6.2832); ctx.stroke();
      // crosshair at neutral, so an off-centre puck is obvious
      ctx.strokeStyle = "rgba(255,255,255,0.30)";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(size / 2 - 4, size / 2); ctx.lineTo(size / 2 + 4, size / 2);
      ctx.moveTo(size / 2, size / 2 - 4); ctx.lineTo(size / 2, size / 2 + 4);
      ctx.stroke();
    }

    function applyPuck(x, y, commit) {
      var d = Math.sqrt(x * x + y * y);
      if (d > 1) { x /= d; y /= d; }
      var res = puckToResidual(x, y, opts.scale);
      var m = master();
      value = [m + res.r, m + res.g, m + res.b];
      sync(commit);
    }

    function setMaster(m, commit) {
      var res = residual();
      value = [m + res.r, m + res.g, m + res.b];
      sync(commit);
    }

    var fields = [];
    function sync(commit) {
      paint();
      for (var i = 0; i < 3; i++) fields[i].set(value[i]);
      mtrack.set(master());
      opts.onChange(value.slice(), !!commit);
    }

    canvas.addEventListener("mousedown", function (ev) {
      ev.preventDefault();
      function at(e) {
        var r = canvas.getBoundingClientRect();
        var x = (e.clientX - r.left - r.width / 2) / (r.width / 2);
        var y = -(e.clientY - r.top - r.height / 2) / (r.height / 2);
        // shift halves the travel for a fine push
        if (e.shiftKey) { x *= 0.35; y *= 0.35; }
        return [x, y];
      }
      var p = at(ev); applyPuck(p[0], p[1], false);
      function move(e) { var q = at(e); applyPuck(q[0], q[1], false); }
      function up() {
        document.removeEventListener("mousemove", move);
        document.removeEventListener("mouseup", up);
        opts.onChange(value.slice(), true);
      }
      document.addEventListener("mousemove", move);
      document.addEventListener("mouseup", up);
    });

    function reset() {
      value = [neutral, neutral, neutral];
      sync(true);
    }
    canvas.addEventListener("dblclick", function (ev) { ev.preventDefault(); reset(); });
    rst.addEventListener("click", reset);

    var mtrack = track({
      min: opts.masterMin, max: opts.masterMax, value: master(), def: neutral,
      bipolar: true,
      onChange: function (v, c) { setMaster(v, c); }
    });
    mtrack.el.title = "master: moves all three channels together";

    var vals = document.createElement("div");
    vals.className = "wvals";
    ["r", "g", "b"].forEach(function (ch, i) {
      var f = numField({
        value: value[i], step: opts.step, precision: opts.precision,
        min: opts.min, max: opts.max,
        onChange: function (v, c) { value[i] = v; paint(); mtrack.set(master()); opts.onChange(value.slice(), c); }
      });
      f.el.title = ch.toUpperCase();
      fields.push(f);
      vals.appendChild(f.el);
    });

    wrap.appendChild(head);
    wrap.appendChild(canvas);
    wrap.appendChild(mtrack.el);
    wrap.appendChild(vals);
    paint();

    return {
      el: wrap,
      set: function (v) {
        value = Array.isArray(v) ? v.slice() : [v, v, v];
        paint();
        for (var i = 0; i < 3; i++) fields[i].set(value[i]);
        mtrack.set(master());
      }
    };
  }

  /* ---- curve editor ---------------------------------------------------- */

  // Fritsch and Carlson monotone cubic, which is what ffmpeg's curves filter
  // calls pchip. Drawing the same spline the render uses means the line on
  // screen is the transfer function, not an artist's impression of it.
  function pchipEval(pts, xs) {
    var n = pts.length;
    if (n < 2) return xs.map(function () { return 0; });
    var h = [], d = [];
    for (var i = 0; i < n - 1; i++) {
      h.push(Math.max(1e-9, pts[i + 1][0] - pts[i][0]));
      d.push((pts[i + 1][1] - pts[i][1]) / h[i]);
    }
    var m = new Array(n);
    m[0] = d[0];
    m[n - 1] = d[n - 2];
    for (var k = 1; k < n - 1; k++) {
      if (d[k - 1] * d[k] <= 0) { m[k] = 0; continue; }
      var w1 = 2 * h[k] + h[k - 1], w2 = h[k] + 2 * h[k - 1];
      m[k] = (w1 + w2) / (w1 / d[k - 1] + w2 / d[k]);
    }
    return xs.map(function (x) {
      if (x <= pts[0][0]) return pts[0][1];
      if (x >= pts[n - 1][0]) return pts[n - 1][1];
      var i = 0;
      while (i < n - 2 && x > pts[i + 1][0]) i++;
      var t = (x - pts[i][0]) / h[i], t2 = t * t, t3 = t2 * t;
      return (2 * t3 - 3 * t2 + 1) * pts[i][1]
        + (t3 - 2 * t2 + t) * h[i] * m[i]
        + (-2 * t3 + 3 * t2) * pts[i + 1][1]
        + (t3 - t2) * h[i] * m[i + 1];
    });
  }

  var CHANNEL_INK = { master: "#e0e0e0", r: "#d05a5a", g: "#5aad5a", b: "#5a8ad0" };

  function curveEditor(opts) {
    var canvas = opts.canvas;
    var ctx = canvas.getContext("2d");
    var channel = "master";
    var curves = opts.value;                  // {master:[[x,y]..], r:[], g:[], b:[]}
    var dragIndex = -1;
    var W = 0, H = 0;

    function size() {
      var r = canvas.getBoundingClientRect();
      var dpr = window.devicePixelRatio || 1;
      W = Math.max(120, Math.round(r.width));
      H = Math.max(120, Math.round(r.width * 0.78));
      canvas.style.height = H + "px";
      canvas.width = W * dpr;
      canvas.height = H * dpr;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }

    function toPx(p) { return [p[0] * W, (1 - p[1]) * H]; }
    function toVal(px, py) { return [clamp(px / W, 0, 1), clamp(1 - py / H, 0, 1)]; }

    function draw() {
      ctx.clearRect(0, 0, W, H);
      ctx.fillStyle = "#141414";
      ctx.fillRect(0, 0, W, H);

      ctx.strokeStyle = "#2a2a2a";
      ctx.lineWidth = 1;
      for (var i = 1; i < 4; i++) {
        ctx.beginPath();
        ctx.moveTo(Math.round(W * i / 4) + 0.5, 0);
        ctx.lineTo(Math.round(W * i / 4) + 0.5, H);
        ctx.moveTo(0, Math.round(H * i / 4) + 0.5);
        ctx.lineTo(W, Math.round(H * i / 4) + 0.5);
        ctx.stroke();
      }
      // Rec.709 mid grey after the aces CST lands at 0.392, not 0.5. Marking it
      // stops the user pulling on 0.5 thinking that is the mid tone.
      ctx.strokeStyle = "#4a4a2a";
      ctx.beginPath();
      ctx.moveTo(Math.round(W * 0.392) + 0.5, 0);
      ctx.lineTo(Math.round(W * 0.392) + 0.5, H);
      ctx.stroke();
      ctx.strokeStyle = "#242424";
      ctx.beginPath(); ctx.moveTo(0, H); ctx.lineTo(W, 0); ctx.stroke();

      ["master", "r", "g", "b"].forEach(function (ch) {
        var pts = curves[ch];
        var flat = pts.every(function (p) { return Math.abs(p[0] - p[1]) < 1e-6; });
        if (ch !== channel && flat) return;
        var xs = [];
        for (var x = 0; x <= W; x += 2) xs.push(x / W);
        var ys = pchipEval(pts, xs);
        ctx.strokeStyle = CHANNEL_INK[ch];
        ctx.globalAlpha = ch === channel ? 1 : 0.35;
        ctx.lineWidth = ch === channel ? 1.6 : 1;
        ctx.beginPath();
        for (var i = 0; i < xs.length; i++) {
          var px = xs[i] * W, py = (1 - clamp(ys[i], 0, 1)) * H;
          if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
        }
        ctx.stroke();
        ctx.globalAlpha = 1;
      });

      curves[channel].forEach(function (p, i) {
        var q = toPx(p);
        ctx.fillStyle = i === dragIndex ? "#ffffff" : CHANNEL_INK[channel];
        ctx.strokeStyle = "#000";
        ctx.lineWidth = 1;
        ctx.beginPath(); ctx.arc(q[0], q[1], 3.5, 0, 6.2832);
        ctx.fill(); ctx.stroke();
      });
    }

    function nearest(px, py) {
      var best = -1, bd = 1e9;
      curves[channel].forEach(function (p, i) {
        var q = toPx(p);
        var d = Math.hypot(q[0] - px, q[1] - py);
        if (d < bd) { bd = d; best = i; }
      });
      return bd < 10 ? best : -1;
    }

    function localPos(ev) {
      var r = canvas.getBoundingClientRect();
      return [ev.clientX - r.left, ev.clientY - r.top];
    }

    canvas.addEventListener("mousedown", function (ev) {
      ev.preventDefault();
      var p = localPos(ev);
      var idx = nearest(p[0], p[1]);
      var pts = curves[channel];

      if ((ev.altKey || ev.button === 2) && idx > 0 && idx < pts.length - 1) {
        pts.splice(idx, 1);
        draw(); opts.onChange(curves, true);
        return;
      }
      if (idx < 0) {
        if (ev.button !== 0) return;
        var v = toVal(p[0], p[1]);
        var at = 0;
        while (at < pts.length && pts[at][0] < v[0]) at++;
        pts.splice(at, 0, v);
        idx = at;
      }
      dragIndex = idx;
      draw();

      function move(e) {
        var q = localPos(e);
        var v = toVal(q[0], q[1]);
        var arr = curves[channel];
        // The two ends stay pinned in x: they are the black and white points,
        // and letting them slide inward would leave the curve undefined at the
        // edges of the range ffmpeg evaluates.
        if (dragIndex === 0) v[0] = 0;
        else if (dragIndex === arr.length - 1) v[0] = 1;
        else {
          v[0] = clamp(v[0], arr[dragIndex - 1][0] + 0.004,
            arr[dragIndex + 1][0] - 0.004);
        }
        arr[dragIndex] = v;
        draw();
        opts.onChange(curves, false);
      }
      function up() {
        document.removeEventListener("mousemove", move);
        document.removeEventListener("mouseup", up);
        dragIndex = -1;
        draw();
        opts.onChange(curves, true);
      }
      document.addEventListener("mousemove", move);
      document.addEventListener("mouseup", up);
    });

    canvas.addEventListener("contextmenu", function (ev) { ev.preventDefault(); });
    canvas.addEventListener("dblclick", function (ev) {
      ev.preventDefault();
      curves[channel] = [[0, 0], [1, 1]];
      draw();
      opts.onChange(curves, true);
    });

    // Any layout change that resizes the canvas's CSS box has to re-run
    // size() and draw(), not just a window resize: an accordion opening or
    // closing, that 200ms grid-template-rows animation as it plays and
    // settles, a column splitter drag, a GridStack widget resize. None of
    // those fire a window "resize" event, so canvas.width/height (the
    // backing store, in device pixels, set the last time size() ran) goes
    // stale against canvas.getBoundingClientRect() (the CSS box, now a
    // different size). The browser then stretches the old bitmap to fit
    // the new box, which is the warping the user sees, and toVal()/toPx()
    // keep dividing by the stale W/H, so every click lands on the wrong
    // point. A ResizeObserver on the canvas itself catches all of the
    // above the same way, with no knowledge of which one caused it,
    // current or future.
    //
    // The observer fires on every rendered frame while the accordion's
    // 200ms transition is still animating, so the redraw is deferred to
    // requestAnimationFrame and coalesced (never more than one pending
    // redraw in flight) instead of run synchronously per notification.
    // That also means the very last notification, once the transition has
    // actually settled, is the one that lands: a mid-animation size is
    // never left as the final state, and a cheap canvas redraw (a couple
    // hundred spline samples) never runs more than once per frame.
    var resizePending = false;
    function onLayoutChange() {
      if (resizePending) return;
      resizePending = true;
      requestAnimationFrame(function () {
        resizePending = false;
        size();
        draw();
      });
    }

    size(); draw();
    // Superseded by the ResizeObserver above: a plain window resize is only
    // one of the many things that changes the canvas's box, and the
    // observer already catches it along with everything the old listener
    // missed (accordion, splitter, GridStack). Kept only as a fallback for
    // a browser without ResizeObserver, where a window resize is at least
    // better than nothing.
    if (typeof ResizeObserver !== "undefined") {
      new ResizeObserver(onLayoutChange).observe(canvas);
    } else {
      window.addEventListener("resize", onLayoutChange);
    }

    return {
      draw: draw,
      resize: function () { size(); draw(); },
      setChannel: function (ch) { channel = ch; draw(); },
      getChannel: function () { return channel; },
      set: function (v) { curves = v; draw(); },
      resetAll: function () {
        ["master", "r", "g", "b"].forEach(function (ch) { curves[ch] = [[0, 0], [1, 1]]; });
        draw(); opts.onChange(curves, true);
      }
    };
  }

  global.Ctl = {
    clamp: clamp, fmt: fmt,
    numField: numField, track: track, row: row,
    slider: slider, select: select, check: check,
    wheel: wheel, curveEditor: curveEditor, pchipEval: pchipEval
  };
})(window);
