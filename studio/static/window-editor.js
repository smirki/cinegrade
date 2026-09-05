/* Power window shape editor: the window drawn and dragged on the picture
   itself, the way a colourist works, rather than only through the sliders in
   a layer's Window group.

   Contract C1 (layers) moved the window from the config's own top level
   ("window") into the SELECTED layer's mask.window (see selectedLayer/win
   below); this file draws whichever layer layers.js currently says is
   selected, never a config-wide singleton.

   Every edit here goes out through global.Layers.emit, the per-layer
   equivalent of the emit every slider and checkbox in a layer's Window
   group also calls (see layers.js). That is deliberate and it is the whole
   design: emit runs the local auto-enable (a drag on a window that is
   switched off switches it on, and ticks the checkbox, exactly as a slider
   drag does) and then hands the change to app.js's onParamChange, which
   owns the live config, the undo history, the session publish and the
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

  /* The picked rectangle's own constants (contract C7). MIN_PICK is a little
     over the 1% of the image the matcher refuses outright, so a handle
     dragged past itself stops at something that can still be measured
     instead of at an error. */
  var MIN_PICK = 0.02;
  var PICK_EDGES = {
    "pick-nw": { x0: 1, y0: 1 }, "pick-n": { y0: 1 }, "pick-ne": { x1: 1, y0: 1 },
    "pick-e": { x1: 1 }, "pick-se": { x1: 1, y1: 1 }, "pick-s": { y1: 1 },
    "pick-sw": { x0: 1, y1: 1 }, "pick-w": { x0: 1 }
  };

  var api = null;
  var svg = null;
  var grp = null;
  var pgrp = null;          /* the picked rectangle's group, untransformed */
  var pick = null;          /* {frac: [x0,y0,x1,y1] or null, onChange: fn} */
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
     patch from an agent) draws the default shape instead of NaN.

     Contract C1 (layers) moved this from the config's own top level
     ("window") to the SELECTED layer's mask.window: the overlay always
     draws whichever layer layers.js says is selected (the last one clicked,
     or the one a fresh Window button press created -- see
     ensureSelectedLayer below), never a config-wide singleton any more.
     global.Layers.getLayers(cfg) is the read-only, migration-aware view
     (an old secondary+window config with no real "layers" array yet still
     resolves to one virtual layer here), so this draws correctly even
     before the user has touched anything that would materialise it for
     real. */
  function selectedLayer() {
    var cfg = api && api.getConfig ? api.getConfig() : null;
    if (!cfg || !global.Layers) return null;
    var layers = global.Layers.getLayers(cfg);
    var idx = global.Layers.getSelectedIndex();
    if (idx < 0 || idx >= layers.length) return null;
    return layers[idx];
  }

  function win() {
    var layer = selectedLayer();
    var w = (layer && layer.mask && layer.mask.window) || {};
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

  /* The viewer's zoom (contract E4, viewer-zoom.js) is one CSS transform on
     #stage, and this SVG is INSIDE #stage, so the transform is already
     applied to everything drawn here. That is what makes the overlay follow
     a zoom and a pan for free, and it is also why every measurement below
     has to be divided by it: getBoundingClientRect answers in screen pixels
     (scaled), while the SVG's own user units are the unscaled ones. Read
     from viewer-zoom.js rather than parsed back out of the transform, so
     there is one definition of the number; 1 when that file is not loaded,
     which is exactly the pre-zoom behaviour.

     ViewerZoom.stageScale(), NOT ViewerZoom.scale(): the second one is how
     big the picture is on screen as a multiple of its own pixels, which at
     fit is the fit ratio (about 0.49 for this footage in a 1440x1500
     window) and has nothing to do with the transform. Dividing by that at
     fit drew every grip at twice its distance from the corner, which is the
     bug specs 13 and 16 caught. */
  function stageScale() {
    var s = (global.ViewerZoom && global.ViewerZoom.stageScale)
      ? Number(global.ViewerZoom.stageScale()) : 1;
    return (isFinite(s) && s > 0) ? s : 1;
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
    var z = stageScale();
    var w = win();
    var pw = pic.width / z, ph = pic.height / z;
    return {
      cfg: w,
      pic: pic,
      z: z,
      ox: (pic.left - sr.left) / z, oy: (pic.top - sr.top) / z,
      pw: pw, ph: ph,
      cxp: w.cx * pw, cyp: w.cy * ph,
      ax: Math.max(w.w * pw / 2, 1),
      ay: Math.max(w.h * ph / 2, 1),
      rot: w.rotation, soft: w.softness, shape: w.shape
    };
  }

  /* Client point to the shape's own rotated frame, the same two lines the
     matte uses (ux = dx cos r + dy sin r, uy = dy cos r - dx sin r), so the
     feather drag reads the same distance d the engine will. The division by
     g.z is the zoom, see geometry() above: the pointer arrives in screen
     pixels and everything it is compared against is in unscaled ones. */
  function localPoint(g, clientX, clientY) {
    var dx = (clientX - g.pic.left) / g.z - g.cxp;
    var dy = (clientY - g.pic.top) / g.z - g.cyp;
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

     global.Layers.emit rather than onChange directly: it is the per-layer
     equivalent of panels.js's own emit (the function every OTHER control in
     a layer calls), and it is where the local auto-enable lives (touching
     mask.window while it is off turns it on). Going round it would leave a
     drag on a switched-off window doing nothing visible, which is the exact
     behaviour panels.js was changed to stop for every other stage, and
     layers.js's own version exists so a dynamic ["layers", i, ...] path
     gets the same treatment (see autoEnableLocal in layers.js: panels.js's
     own auto-enable table is built once from SCHEMA and cannot know about a
     layer index at all). Falls back to a raw onChange (no auto-enable) only
     if layers.js somehow is not loaded, so a drag is never silently a
     no-op. */
  function emit(pairs, commit) {
    if (!pairs || !pairs.length) return;
    var idx = global.Layers ? global.Layers.getSelectedIndex() : -1;
    if (idx < 0) return; // nothing selected: sync() would not have drawn a shape to drag in the first place
    for (var i = 0; i < pairs.length; i++) {
      var commitThis = !!commit && i === pairs.length - 1;
      if (global.Layers && global.Layers.emit) {
        global.Layers.emit(idx, ["mask", "window", pairs[i][0]], pairs[i][1], commitThis);
      } else if (api && api.onChange) {
        api.onChange(["layers", idx, "mask", "window", pairs[i][0]], pairs[i][1], commitThis);
      }
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

    /* The picked rectangle (contract C7). A second thing this overlay can
       draw: not the selected layer's power window but a plain rectangle
       saying which part of the frame "Match to reference" should measure.
       It lives in this file rather than in an overlay of its own because
       everything it needs is already solved here: the PICTURE's rendered
       rect (which is not the stage's in wipe mode), the pointer capture
       that keeps a drag alive once it leaves a 5px grip, and the observers
       that reposition on a resize, a zoom or a new frame.

       It is its own untransformed group: grp carries the window's
       translate and rotate, and a crop rectangle is neither translated nor
       rotated. It writes nothing into the config either, it hands the
       rectangle to whoever called setPick, so a layer's window cannot be
       disturbed by picking and leaving pick mode puts back exactly what
       was on screen before it. */
    pgrp = mk("g", "pick-grp", svg);
    /* The full stage catcher, so a drag can START anywhere on the picture
       and draw a new rectangle. Only takes the pointer while picking (see
       #windowOverlay.picking .pick-catch in style.css), so outside pick
       mode the overlay is as click-through as it always was. */
    el.pickCatch = mk("rect", "pick-catch", pgrp);
    el.pickCatch.setAttribute("x", "0");
    el.pickCatch.setAttribute("y", "0");
    el.pickCatch.setAttribute("width", "100%");
    el.pickCatch.setAttribute("height", "100%");
    el.pickBox = mk("rect", "pick-box", pgrp);
    el.pickGrips = [];
    Object.keys(PICK_EDGES).forEach(function (role) {
      var c = mk("circle", "pick-grip", pgrp);
      c.setAttribute("r", "5");
      c.dataset.role = role;
      bindHandle(c, role);
      el.pickGrips.push(c);
    });
    el.pickCatch.addEventListener("pointerdown", function (ev) {
      if (pick) onDown("pick-new", el.pickCatch, ev);
    });

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
        // "If no layer exists, the Window button creates one and selects
        // it" (contract C1). Only on the way ON: switching the button back
        // off never removes a layer, it only hides the overlay again (see
        // sync()'s own "on" test below, which also checks window.enabled).
        if (forced && global.Layers) global.Layers.ensureLayerAndSelect();
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
      pairs: null,
      /* Picking (contract C7) takes the same drag lifecycle and the same
         pointer capture and answers with a rectangle instead of window
         fields. startBox is the rectangle as it was at pointerdown, so a
         handle drag is computed against it rather than accumulated frame to
         frame, exactly like every branch of compute() below. */
      pick: role.indexOf("pick") === 0,
      anchor: pickPoint(g, ev),
      startBox: (pick && pick.frac) ? pick.frac.slice() : null,
      box: null
    };
    svg.classList.add("dragging");
    if (svg.focus) svg.focus();
  }

  /* The pointer as [x, y] fractions of the picture, clamped so a drag that
     leaves the picture (or the window) still ends exactly at its edge. */
  function pickPoint(g, ev) {
    return [clamp((ev.clientX - g.pic.left) / (g.z * g.pw), 0, 1),
            clamp((ev.clientY - g.pic.top) / (g.z * g.ph), 0, 1)];
  }

  function normBox(b) {
    var x0 = Math.min(b[0], b[2]), x1 = Math.max(b[0], b[2]);
    var y0 = Math.min(b[1], b[3]), y1 = Math.max(b[1], b[3]);
    var c;
    if (x1 - x0 < MIN_PICK) {
      c = (x0 + x1) / 2;
      x0 = clamp(c - MIN_PICK / 2, 0, 1 - MIN_PICK); x1 = x0 + MIN_PICK;
    }
    if (y1 - y0 < MIN_PICK) {
      c = (y0 + y1) / 2;
      y0 = clamp(c - MIN_PICK / 2, 0, 1 - MIN_PICK); y1 = y0 + MIN_PICK;
    }
    return [x0, y0, x1, y1];
  }

  function computePick(ev) {
    var g = drag.g;
    var p = pickPoint(g, ev);
    if (drag.role === "pick-new" || !drag.startBox) {
      return normBox([drag.anchor[0], drag.anchor[1], p[0], p[1]]);
    }
    var edges = PICK_EDGES[drag.role] || {};
    var b = drag.startBox.slice();
    if (edges.x0) b[0] = p[0];
    if (edges.x1) b[2] = p[0];
    if (edges.y0) b[1] = p[1];
    if (edges.y1) b[3] = p[1];
    return normBox(b);
  }

  function pickChanged(box, commit) {
    if (!pick) return;
    pick.frac = box;
    sync();
    if (pick.onChange) pick.onChange(box ? box.slice() : null, !!commit);
  }

  function onMove(ev) {
    if (!drag || ev.pointerId !== drag.id) return;
    ev.preventDefault();
    if (drag.pick) {
      var box = computePick(ev);
      if (!box) return;
      drag.box = box;
      pickChanged(box, false);
      return;
    }
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
    if (drag.pick) {
      /* Same rule as the window below: a press with no movement is not an
         edit, so a plain click on the picture leaves whatever rectangle was
         already drawn exactly where it was. */
      var box = drag.box ? (computePick(ev) || drag.box) : null;
      drag = null;
      svg.classList.remove("dragging");
      if (box) pickChanged(box, true);
      else sync();
      return;
    }
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
      var cx = ((ev.clientX - g.pic.left) / g.z - drag.grabDx) / g.pw;
      var cy = ((ev.clientY - g.pic.top) / g.z - drag.grabDy) / g.ph;
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
    var step = (ev.shiftKey ? 10 : 1) / g.z;   /* screen px, so the zoom divides it */
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

  /* The picked rectangle, drawn in the picture's own pixels: a plain
     outline and eight grips, no wash and no fill. The wash the window uses
     is a momentary "this is what is inside" hint on a shape being placed;
     a crop rectangle sits on screen for as long as the user is matching,
     and a permanent tint over a picture being judged for colour is the one
     thing this overlay must never do (see the note at the top of the
     #windowOverlay block in style.css). */
  function drawPick(g) {
    var b = pick.frac;
    show(el.pickBox, !!b);
    el.pickGrips.forEach(function (c) { show(c, !!b); });
    if (!b) return;
    var x = g.ox + b[0] * g.pw, y = g.oy + b[1] * g.ph;
    var w = (b[2] - b[0]) * g.pw, h = (b[3] - b[1]) * g.ph;
    el.pickBox.setAttribute("x", x.toFixed(2));
    el.pickBox.setAttribute("y", y.toFixed(2));
    el.pickBox.setAttribute("width", Math.max(w, 1).toFixed(2));
    el.pickBox.setAttribute("height", Math.max(h, 1).toFixed(2));
    var at = {
      "pick-nw": [x, y], "pick-n": [x + w / 2, y], "pick-ne": [x + w, y],
      "pick-e": [x + w, y + h / 2], "pick-se": [x + w, y + h],
      "pick-s": [x + w / 2, y + h], "pick-sw": [x, y + h],
      "pick-w": [x, y + h / 2]
    };
    el.pickGrips.forEach(function (c) {
      var p = at[c.dataset.role];
      move(c, p[0].toFixed(2), p[1].toFixed(2));
    });
  }

  /* The one entry point app.js calls whenever the config may have moved:
     scheduleRender, which every config change from anywhere funnels through
     (a slider, an undo, a preset, a clip switch, an outside session patch).
     The observers above call it for layout and view mode. */
  function sync() {
    if (!svg) return;
    svg.classList.toggle("picking", !!pick);
    if (pick) {
      /* Pick mode owns the overlay for as long as it is on. The window's
         own state (forced, and the selected layer's mask.window) is not
         touched here, so switching the pick off falls straight through to
         the branch below and draws exactly what was there before. */
      var stage0 = byId("stage");
      var busy = !!(stage0 && stage0.classList.contains("playing"));
      var gp = busy ? null : geometry();
      svg.classList.toggle("on", !!gp);
      if (gp) drawPick(gp);
      return;
    }
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

  /* Pick mode on and off (contract C7), the whole of this file's public
     surface for it.

       setPick({crop: [x0,y0,x1,y1] or null, onChange: fn})   turn it on
       setPick(null)                                          turn it off

     onChange(box, committed) fires on every pointermove with committed
     false and once on release with true, the same shape the window's own
     drag uses, so the caller can preview live and save once. Turning it off
     redraws the window overlay as it was: nothing about the layer was
     touched while picking. */
  function setPick(opts) {
    build();
    if (!opts) {
      pick = null;
    } else {
      pick = {
        frac: opts.crop ? normBox(opts.crop.slice()) : null,
        onChange: opts.onChange || (pick && pick.onChange) || null
      };
    }
    sync();
  }

  function getPick() {
    return (pick && pick.frac) ? pick.frac.slice() : null;
  }

  global.WindowEditor = {
    init: init, sync: sync, setPick: setPick, getPick: getPick,
    picking: function () { return !!pick; }
  };
})(window);
