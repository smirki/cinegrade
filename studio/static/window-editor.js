/* Power window shape editor: the window drawn and dragged on the picture
   itself, the way a colourist works, rather than only through the seven
   sliders in the Window panel.

   Every edit here goes out through Panels.emit, which is the SAME function
   every slider and checkbox in the Window panel calls. That is deliberate
   and it is the whole design: emit runs the auto-enable (a drag on a window
   that is switched off switches it on, and ticks the checkbox, exactly as a
   slider drag does) and then hands the change to app.js's onParamChange,
   which owns the live config, the undo history, the session publish and the
   re-render. This file therefore holds no config state of its own and never
   writes cfg() directly: it reads the live config on every redraw and draws
   what it finds, so a slider move, an undo, a preset load, a clip switch and
   a drag on the picture all land in the same place and cannot disagree.

   Coordinates: every window parameter is a fraction of the GRADED frame
   (contract C4), and the graded frame is exactly what the viewer shows.
   server.py probes the clip with autorotate on unless it is turned off and
   decodes the source frame the same way, then bakes the matte at the PREVIEW
   dimensions from that same probe (_grade_inputs passes pinfo, whose width
   and height are the preview's), so the matte's axes are the axes of the
   picture on screen at whatever preview width is selected. The overlay maps
   fractions onto the rendered rect of whichever element currently holds the
   picture (#frameImg, #gpuCanvas or #playerVideo), never onto #stage and
   never onto #afterLayer: in wipe mode the layer is stretched over the whole
   stage and only the picture element inside it has the letterboxed rect the
   fractions actually refer to. */
(function (global) {
  "use strict";

  var NS = "http://www.w3.org/2000/svg";
  var STEM = 28;            /* px from the shape's top edge to the rotation grip */
  var MIN_EXTENT = 0.02;    /* w and h clamp, fractions of the frame */
  var MAX_EXTENT = 2;
  /* In DOM order, which is also the order the three possible picture
     elements take precedence in: a still, the GPU canvas, a playing video.
     Only one of them is ever visible at once (setStageLayer in app.js is
     the single writer of the classes that decide which) and the hidden ones
     measure zero, so the first non-empty rect is the picture. */
  var PICTURE_IDS = ["frameImg", "gpuCanvas", "playerVideo"];

  var api = null;
  var svg = null;
  var grp = null;
  var el = {};
  var drag = null;
  var forced = false;       /* the toolbar button: draw before the window is on */
  var btn = null;
  var refreshPending = false;

  function byId(id) { return document.getElementById(id); }
  function clamp(v, lo, hi) { return v < lo ? lo : (v > hi ? hi : v); }
  function num(v, d) { v = Number(v); return isFinite(v) ? v : d; }

  function mk(tag, cls, parent) {
    var node = document.createElementNS(NS, tag);
    if (cls) node.setAttribute("class", cls);
    if (parent) parent.appendChild(node);
    return node;
  }

  /* The window block as this file needs it: every field defaulted, so a
     config saved before the window stage existed (or a half written session
     patch from an agent) draws the default shape instead of NaN. */
  function win() {
    var cfg = api && api.getConfig ? api.getConfig() : null;
    var w = (cfg && cfg.window) || {};
    return {
      enabled: !!w.enabled,
      shape: String(w.shape) === "rect" ? "rect" : "ellipse",
      cx: num(w.cx, 0.5), cy: num(w.cy, 0.5),
      w: num(w.w, 0.6), h: num(w.h, 0.6),
      rotation: num(w.rotation, 0),
      softness: Math.max(0, num(w.softness, 0.15)),
      invert: !!w.invert
    };
  }

  function pictureRect() {
    for (var i = 0; i < PICTURE_IDS.length; i++) {
      var node = byId(PICTURE_IDS[i]);
      if (!node) continue;
      /* An <img> with no src still lays out as its alt text, which is a real
         rect of the wrong size. naturalWidth is 0 until a picture actually
         decoded, so this is the one cheap test that tells "showing a frame"
         from "showing nothing yet". */
      if (node.tagName === "IMG" && !node.naturalWidth) continue;
      var r = node.getBoundingClientRect();
      if (r.width > 1 && r.height > 1) return r;
    }
    return null;
  }

  /* Everything the drawing and the drag maths need, resolved to px against
     the picture's own rect, plus the stage offset the SVG is positioned in.
     The half axis clamp mirrors grade/cinegrade.py _window_geometry, where a
     zero width window is one pixel rather than a divide by zero. */
  function geometry() {
    var stage = byId("stage");
    var pic = pictureRect();
    if (!stage || !pic) return null;
    var sr = stage.getBoundingClientRect();
    var w = win();
    return {
      cfg: w,
      pic: pic,
      ox: pic.left - sr.left, oy: pic.top - sr.top,
      pw: pic.width, ph: pic.height,
      cxp: w.cx * pic.width, cyp: w.cy * pic.height,
      ax: Math.max(w.w * pic.width / 2, 1),
      ay: Math.max(w.h * pic.height / 2, 1),
      rot: w.rotation, soft: w.softness, shape: w.shape
    };
  }

  /* Client point to the shape's own rotated frame, the same two lines the
     matte uses (ux = dx cos r + dy sin r, uy = dy cos r - dx sin r), so the
     feather drag reads the same distance d the engine will. */
  function localPoint(g, clientX, clientY) {
    var dx = (clientX - g.pic.left) - g.cxp;
    var dy = (clientY - g.pic.top) - g.cyp;
    var r = g.rot * Math.PI / 180;
    var c = Math.cos(r), s = Math.sin(r);
    return { dx: dx, dy: dy, ux: dx * c + dy * s, uy: dy * c - dx * s };
  }

  function shapeDistance(g, p) {
    if (g.shape === "rect") return Math.max(Math.abs(p.ux) / g.ax, Math.abs(p.uy) / g.ay);
    return Math.sqrt((p.ux / g.ax) * (p.ux / g.ax) + (p.uy / g.ay) * (p.uy / g.ay));
  }

  /* One drag is one undo step. Every intermediate write goes out with
     commit false and only the LAST field of the release carries commit
     true, because onParamChange pushes history on each committed call and a
     centre move writes two fields. app.js's pushHistory pushes the PREVIOUS
     committed snapshot, so that one commit restores the whole drag,
     including the auto-enable that started it.

     Panels.emit rather than onChange directly: emit is the function the
     panel's own controls call, and it is where the auto-enable lives. Going
     round it would leave a drag on a switched-off window doing nothing
     visible, which is the exact behaviour panels.js was changed to stop. */
  function emit(pairs, commit) {
    if (!pairs || !pairs.length) return;
    var send = (global.Panels && global.Panels.emit)
      ? global.Panels.emit
      : (api && api.onChange);
    if (!send) return;
    for (var i = 0; i < pairs.length; i++) {
      send(["window", pairs[i][0]], pairs[i][1], !!commit && i === pairs.length - 1);
    }
  }

  /* Mid drag the panel's sliders are stale: onParamChange only repaints
     widgets on a committed change, so without this the numbers under the
     Window heading would sit still until the mouse came up. One repaint per
     animation frame is the same budget the viewer's own preview gets, and
     it is the same call app.js makes on every commit, so nothing new can go
     wrong in it. */
  function refreshPanelsSoon() {
    if (refreshPending || !api || !api.refreshPanels) return;
    refreshPending = true;
    requestAnimationFrame(function () {
      refreshPending = false;
      api.refreshPanels();
    });
  }

  /* ---- the overlay itself ------------------------------------------------ */

  function build() {
    var stage = byId("stage");
    if (!stage || svg) return;

    svg = document.createElementNS(NS, "svg");
    svg.setAttribute("id", "windowOverlay");
    svg.setAttribute("tabindex", "0");
    svg.setAttribute("aria-label", "power window shape editor");
    grp = mk("g", "win-grp", svg);

    /* Both shapes exist at all times and one of the pair is hidden, rather
       than replacing the node when the panel's shape select changes: a swap
       would drop the element a pointer capture is attached to mid drag. */
    el.washE = mk("ellipse", "win-wash", grp);
    el.washR = mk("rect", "win-wash", grp);
    el.featherE = mk("ellipse", "win-feather", grp);
    el.featherR = mk("rect", "win-feather", grp);
    el.featherHitE = mk("ellipse", "win-hit", grp);
    el.featherHitR = mk("rect", "win-hit", grp);
    el.outlineE = mk("ellipse", "win-outline", grp);
    el.outlineR = mk("rect", "win-outline", grp);

    el.stem = mk("line", "win-stem", grp);
    el.rot = mk("circle", "win-grip win-rot", grp);
    el.rot.setAttribute("r", "6");

    el.hw = [];
    ["wpos", "wneg", "hpos", "hneg"].forEach(function (role) {
      var c = mk("circle", "win-grip win-extent", grp);
      c.setAttribute("r", "5");
      c.dataset.role = role;
      el.hw.push(c);
    });

    el.crossH = mk("line", "win-cross", grp);
    el.crossV = mk("line", "win-cross", grp);
    el.centreRing = mk("circle", "win-grip win-centre", grp);
    el.centreRing.setAttribute("r", "9");

    bindHandle(el.centreRing, "centre");
    el.hw.forEach(function (c) { bindHandle(c, c.dataset.role); });
    bindHandle(el.rot, "rot");
    bindHandle(el.featherHitE, "soft");
    bindHandle(el.featherHitR, "soft");

    el.centreRing.addEventListener("pointerenter", function () { svg.classList.add("hot"); });
    el.centreRing.addEventListener("pointerleave", function () {
      if (!drag || drag.role !== "centre") svg.classList.remove("hot");
    });

    svg.addEventListener("keydown", onKey);
    stage.appendChild(svg);

    /* The move and the release listen on window, not on the grip. With a
       successful setPointerCapture the events retarget to the grip and then
       bubble here anyway, so this sees each one exactly once; without one
       (an old browser, or a synthetic pointer with no live pointer behind
       it) this is the only listener that still sees a drag that has left
       the grip. One listener, both cases, no double fire. */
    global.addEventListener("pointermove", onMove);
    global.addEventListener("pointerup", onUp);
    global.addEventListener("pointercancel", onUp);

    btn = byId("windowBtn");
    if (btn) {
      btn.addEventListener("click", function () {
        forced = !forced;
        sync();
        if (forced && svg && svg.focus) svg.focus();
      });
    }
  }

  /* Nothing polls. Config changes arrive through app.js calling sync() from
     scheduleRender, which every config edit funnels through. Layout changes
     do not go through there, so they are observed instead: the picture's
     rendered rect moves on a viewport resize, a zoom change, a preview
     width change and a new frame loading (all of which resize one of these
     boxes), and the class list on #stage is what decides the view mode and
     whether playback owns the layer. */
  function observe() {
    var stage = byId("stage");
    if (!stage) return;
    if (global.ResizeObserver) {
      var ro = new ResizeObserver(function () { sync(); });
      ro.observe(stage);
      PICTURE_IDS.forEach(function (id) {
        var node = byId(id);
        if (node) ro.observe(node);
      });
    }
    if (global.MutationObserver) {
      new MutationObserver(function () { sync(); })
        .observe(stage, { attributes: true, attributeFilter: ["class", "style"] });
    }
  }

  /* ---- drag -------------------------------------------------------------- */

  function bindHandle(node, role) {
    node.addEventListener("pointerdown", function (ev) { onDown(role, node, ev); });
  }

  function onDown(role, node, ev) {
    var g = geometry();
    if (!g) return;
    ev.preventDefault();
    ev.stopPropagation();
    /* Same reasoning as the wipe handle in app.js: the capture is what keeps
       the drag alive once the pointer leaves a 9px grip. A synthetic
       PointerEvent with no live pointer behind it throws here, which must
       not take the drag down with it, and the window level listeners above
       are what make that survivable. The drag state is therefore a flag of
       our own, never a read of hasPointerCapture. */
    try { node.setPointerCapture(ev.pointerId); } catch (e) { /* see above */ }
    var p = localPoint(g, ev.clientX, ev.clientY);
    drag = {
      role: role, node: node, id: ev.pointerId, g: g,
      grabDx: p.dx, grabDy: p.dy,
      startAngle: Math.atan2(p.dy, p.dx),
      startRot: g.rot,
      startSoft: g.soft,
      startD: shapeDistance(g, p),
      axOffset: g.ax - Math.abs(p.ux),
      ayOffset: g.ay - Math.abs(p.uy),
      pairs: null
    };
    svg.classList.add("dragging");
    if (svg.focus) svg.focus();
  }

  function onMove(ev) {
    if (!drag || ev.pointerId !== drag.id) return;
    ev.preventDefault();
    var pairs = compute(ev);
    if (!pairs) return;
    drag.pairs = pairs;
    emit(pairs, false);
    refreshPanelsSoon();
  }

  function onUp(ev) {
    if (!drag || ev.pointerId !== drag.id) return;
    var node = drag.node;
    try {
      if (node.hasPointerCapture(ev.pointerId)) node.releasePointerCapture(ev.pointerId);
    } catch (e) { /* same as the capture above */ }
    var pairs = compute(ev) || drag.pairs;
    var moved = !!drag.pairs;
    drag = null;
    svg.classList.remove("dragging");
    /* A press with no movement is not an edit: nothing is committed, so it
       cannot push an undo step that restores the state it is already in. */
    if (moved && pairs) emit(pairs, true);
    sync();
  }

  /* The drag maths, one branch per grip. Every branch returns the fields to
     write as [path leaf, value] pairs, computed from the pointer's CURRENT
     position against the geometry captured at pointerdown, never
     accumulated frame to frame: an accumulated drag drifts, and a dropped
     pointermove would lose the difference for good. */
  function compute(ev) {
    var g = drag.g;
    var role = drag.role;
    if (role === "centre") {
      var cx = ((ev.clientX - g.pic.left) - drag.grabDx) / g.pw;
      var cy = ((ev.clientY - g.pic.top) - drag.grabDy) / g.ph;
      return [["cx", clamp(cx, 0, 1)], ["cy", clamp(cy, 0, 1)]];
    }
    var p = localPoint(g, ev.clientX, ev.clientY);
    if (role === "wpos" || role === "wneg") {
      var ax = Math.abs(p.ux) + drag.axOffset;
      return [["w", clamp((2 * ax) / g.pw, MIN_EXTENT, MAX_EXTENT)]];
    }
    if (role === "hpos" || role === "hneg") {
      var ay = Math.abs(p.uy) + drag.ayOffset;
      return [["h", clamp((2 * ay) / g.ph, MIN_EXTENT, MAX_EXTENT)]];
    }
    if (role === "rot") {
      var deg = drag.startRot + (Math.atan2(p.dy, p.dx) - drag.startAngle) * 180 / Math.PI;
      /* The slider's own range is -180 to 180 and the matte only ever sees
         cos and sin of this, so wrapping here keeps the panel and the
         overlay showing the same number for the same shape. */
      while (deg > 180) deg -= 360;
      while (deg < -180) deg += 360;
      return [["rotation", Math.round(deg * 10) / 10]];
    }
    if (role === "soft") {
      /* d is the matte's own distance, so releasing the ring under the
         pointer puts the feather's outer edge exactly there. */
      var soft = drag.startSoft + (shapeDistance(g, p) - drag.startD);
      return [["softness", clamp(soft, 0, 1)]];
    }
    return null;
  }

  function onKey(ev) {
    if (ev.key === "Escape") {
      svg.blur();
      ev.preventDefault();
      return;
    }
    var dx = 0, dy = 0;
    if (ev.key === "ArrowLeft") dx = -1;
    else if (ev.key === "ArrowRight") dx = 1;
    else if (ev.key === "ArrowUp") dy = -1;
    else if (ev.key === "ArrowDown") dy = 1;
    else return;
    var g = geometry();
    if (!g) return;
    ev.preventDefault();
    /* 1 screen pixel, 10 with shift. Screen pixels, not frame pixels: the
       nudge is there to line the shape up with something the eye can see at
       the size it is being seen at, and a fraction of the rendered rect is
       what that means. */
    var step = ev.shiftKey ? 10 : 1;
    var pairs = [];
    if (dx) pairs.push(["cx", clamp(g.cfg.cx + (dx * step) / g.pw, 0, 1)]);
    if (dy) pairs.push(["cy", clamp(g.cfg.cy + (dy * step) / g.ph, 0, 1)]);
    emit(pairs, true);
    sync();
  }

  /* ---- draw -------------------------------------------------------------- */

  function setEllipse(node, rx, ry) {
    node.setAttribute("cx", "0"); node.setAttribute("cy", "0");
    node.setAttribute("rx", String(rx)); node.setAttribute("ry", String(ry));
  }
  function setRect(node, rx, ry) {
    node.setAttribute("x", String(-rx)); node.setAttribute("y", String(-ry));
    node.setAttribute("width", String(2 * rx)); node.setAttribute("height", String(2 * ry));
  }
  function show(node, on) { node.setAttribute("display", on ? "inline" : "none"); }
  function move(node, x, y) { node.setAttribute("cx", String(x)); node.setAttribute("cy", String(y)); }
  function line(node, x1, y1, x2, y2) {
    node.setAttribute("x1", String(x1)); node.setAttribute("y1", String(y1));
    node.setAttribute("x2", String(x2)); node.setAttribute("y2", String(y2));
  }

  function draw(g) {
    var isRect = g.shape === "rect";
    var fx = g.ax * (1 + g.soft), fy = g.ay * (1 + g.soft);

    grp.setAttribute("transform",
      "translate(" + (g.ox + g.cxp).toFixed(2) + " " + (g.oy + g.cyp).toFixed(2) + ") "
      + "rotate(" + g.rot.toFixed(3) + ")");

    setEllipse(el.outlineE, g.ax, g.ay); setRect(el.outlineR, g.ax, g.ay);
    setEllipse(el.washE, g.ax, g.ay); setRect(el.washR, g.ax, g.ay);
    setEllipse(el.featherE, fx, fy); setRect(el.featherR, fx, fy);
    setEllipse(el.featherHitE, fx, fy); setRect(el.featherHitR, fx, fy);
    show(el.outlineE, !isRect); show(el.outlineR, isRect);
    show(el.washE, !isRect); show(el.washR, isRect);
    show(el.featherHitE, !isRect); show(el.featherHitR, isRect);
    /* Softness 0 is a hard edge with no partial pixels at all (the matte's
       own special case), so the ring sits exactly on the outline and drawing
       it there would only put a dashed line on top of a solid one. The
       invisible hit ring stays live at that radius so the feather can still
       be pulled out of zero by dragging, which is the one thing hiding the
       whole ring would take away. */
    var hasFeather = g.soft > 0;
    show(el.featherE, !isRect && hasFeather);
    show(el.featherR, isRect && hasFeather);

    move(el.hw[0], g.ax, 0);
    move(el.hw[1], -g.ax, 0);
    move(el.hw[2], 0, g.ay);
    move(el.hw[3], 0, -g.ay);

    line(el.stem, 0, -g.ay, 0, -(g.ay + STEM));
    move(el.rot, 0, -(g.ay + STEM));

    move(el.centreRing, 0, 0);
    line(el.crossH, -13, 0, 13, 0);
    line(el.crossV, 0, -13, 0, 13);

    svg.classList.toggle("inverted", !!g.cfg.invert);
  }

  /* The one entry point app.js calls whenever the config may have moved:
     scheduleRender, which every config change from anywhere funnels through
     (a slider, an undo, a preset, a clip switch, an outside session patch).
     The observers above call it for layout and view mode. */
  function sync() {
    if (!svg) return;
    var w = win();
    var stage = byId("stage");
    /* Playback owns the layer while it runs and the overlay would be drawn
       over a moving picture it cannot follow (there is no tracking), so it
       goes away for the duration. setStageLayer in app.js is the only writer
       of this class; this only ever reads it. */
    var playing = !!(stage && stage.classList.contains("playing"));
    var g = (!playing && (w.enabled || forced)) ? geometry() : null;
    var on = !!g;
    svg.classList.toggle("on", on);
    if (btn) {
      /* The button is its own switch, not a readout of the overlay: it is
         what lets a user draw a window that is still switched off, and it
         has to stay pressable back to off afterwards. */
      btn.classList.toggle("active", forced);
      btn.setAttribute("aria-pressed", forced ? "true" : "false");
    }
    if (!on) {
      svg.classList.remove("hot");
      return;
    }
    draw(g);
  }

  function init(opts) {
    api = opts || null;
    build();
    observe();
    sync();
  }

  global.WindowEditor = { init: init, sync: sync };
})(window);
