/* Viewer zoom and pan (contract E4).

   Founder, 2026-09-05: "have more zoom controls for the frontend and for the
   agent."

   One transform, on one element. Everything the viewer can show (the still
   #frameImg, the GPU canvas, the played video, the bypassed copy in a split
   view, the power window overlay, the picked match rectangle, and the two or
   four slot grid in frames.js) lives inside either #stage or #framesGrid, so
   scaling that one box scales all of it together and nothing can drift out
   of register with the picture underneath it. Transforming the picture
   elements one by one was the alternative and it is the bug: the overlay is
   a sibling of the picture, not a child, so it would have needed its own
   copy of the same maths and the two copies would disagree the first time
   one of them was changed.

   Two modes, one meaning:

     fit    the picture is sized by app.js's fitViewer (max-width and
            max-height in px on each picture element) and the transform is
            identity. This is exactly what the viewer did before this file
            existed, byte for byte, so nothing about the default view moved.
     zoom   the picture elements are released to their natural size (the
            preview render's own pixels) and this file's transform does the
            rest: translate for the pan, scale for the zoom.

   The percentage is therefore "of the preview's own pixels": at 100% one
   pixel of the 960px (or 1280, or 1920) preview render is one CSS pixel on
   screen. It is deliberately NOT a percentage of the source: the preview
   width select in the viewer bar is what decides how many pixels there are
   to look at, and a label that quietly meant something else at each setting
   would be worse than no label.

   Anchoring is measured, not derived. To keep the point under the pointer
   still while the scale changes, this reads the content's rect before, sets
   the new scale, reads the rect again and corrects the pan by the
   difference. That survives everything a derived formula would have to know
   about and could get wrong: flex centring, a container that overflows on
   both sides, a picture whose natural size changed between the two frames,
   and the fit-to-zoom transition where the element's own layout size
   changes at the same moment as the transform.
*/
(function (global) {
  "use strict";

  var MIN_SCALE = 0.02;
  var MAX_SCALE = 16;
  /* Each press of + or - is one of these steps. sqrt(2) rather than 2 so a
     press is a visible move without skipping the useful range between 100%
     and 200% in one go. */
  var STEP = Math.SQRT2;

  var api = null;                 /* {onLayout, onZoom} from app.js */
  var mode = "fit";               /* "fit" or "zoom" */
  var scale = 1;                  /* the transform scale while mode is zoom */
  var panX = 0, panY = 0;
  var spaceHeld = false;
  var pan = null;                 /* the live drag */
  var pointers = {};              /* pointerId -> {x, y}, for pinch */
  var pinch = null;

  function byId(id) { return document.getElementById(id); }
  function clamp(v, lo, hi) { return v < lo ? lo : (v > hi ? hi : v); }

  /* Whichever box is on screen right now. The frames grid replaces the
     stage rather than sitting inside it (see #viewport.frames in style.css),
     so the transform has to follow the swap. */
  function content() {
    var grid = byId("framesGrid");
    if (grid && grid.offsetParent !== null) return grid;
    return byId("stage");
  }

  /* The picture element the percentage is measured against: the same three
     candidates, in the same order, window-editor.js resolves. An <img> with
     no decoded picture lays out as its alt text, which is a real rect of a
     meaningless size, so naturalWidth is what tells "showing a frame" from
     "showing nothing yet". */
  function pictureEl() {
    var host = content();
    if (!host) return null;
    var ids = ["frameImg", "gpuCanvas", "playerVideo"];
    for (var i = 0; i < ids.length; i++) {
      var n = byId(ids[i]);
      if (!n || !host.contains(n)) continue;
      if (n.tagName === "IMG" && !n.naturalWidth) continue;
      var r = n.getBoundingClientRect();
      if (r.width > 1 && r.height > 1) return n;
    }
    /* The frames grid has no #frameImg: its slots each hold their own
       picture, so the first one that has decoded something is the measure. */
    var slots = host.querySelectorAll(".fimg, .fcanvas");
    for (var j = 0; j < slots.length; j++) {
      var s = slots[j];
      if (s.tagName === "IMG" && !s.naturalWidth) continue;
      var sr = s.getBoundingClientRect();
      if (sr.width > 1 && sr.height > 1) return s;
    }
    return null;
  }

  function naturalWidth(node) {
    if (!node) return 0;
    if (node.tagName === "IMG") return node.naturalWidth || 0;
    if (node.tagName === "CANVAS") return node.width || 0;
    if (node.tagName === "VIDEO") return node.videoWidth || 0;
    return 0;
  }

  /* What the picture is at on screen right now, as a multiple of its own
     pixels, whatever mode we are in. In zoom mode that is the transform's
     scale by construction; in fit mode it is whatever fitViewer's max-width
     resolved to, which is the number the label has to show and the number a
     wheel zoom out of fit has to continue from. */
  function shownScale() {
    if (mode === "zoom") return scale;
    var node = pictureEl();
    var nat = naturalWidth(node);
    if (!node || !nat) return 1;
    return node.getBoundingClientRect().width / nat;
  }

  function percent() { return Math.round(shownScale() * 100); }

  function transformText() {
    if (mode === "fit") return "";
    return "translate(" + panX.toFixed(2) + "px, " + panY.toFixed(2) + "px) "
      + "scale(" + scale.toFixed(6) + ")";
  }

  /* One writer for the transform, and it writes it on BOTH boxes rather
     than only the one showing: a mode change (frames on, frames off) must
     not leave a stale transform on the box that just went away, or it comes
     back wrong the next time it is shown. */
  function paint() {
    var t = transformText();
    ["stage", "framesGrid"].forEach(function (id) {
      var el = byId(id);
      if (el) el.style.transform = t;
    });
    var vp = byId("viewport");
    if (vp) {
      vp.classList.toggle("zoom", mode !== "fit");
      vp.classList.toggle("panning", !!pan);
      vp.classList.toggle("grab", spaceHeld || (mode !== "fit"));
    }
    var label = byId("zoomLabel");
    if (label) {
      label.textContent = mode === "fit" ? ("fit " + percent() + "%")
        : (percent() + "%");
    }
    ["fitBtn", "oneToOneBtn", "zoom200Btn", "zoom400Btn"].forEach(function (id, i) {
      var b = byId(id);
      if (!b) return;
      var want = i === 0 ? (mode === "fit")
        : (mode === "zoom" && Math.abs(scale - [0, 1, 2, 4][i]) < 0.001);
      b.classList.toggle("active", want);
    });
  }

  /* Keeps the picture inside the viewport: centred on an axis it is smaller
     than, and edge to edge on an axis it is larger than. Measured against
     the real rects rather than computed from the scale, for the same reason
     the anchoring is measured. */
  function clampPan() {
    if (mode === "fit") { panX = 0; panY = 0; return; }
    var vp = byId("viewport");
    var host = content();
    if (!vp || !host) return;
    var v = vp.getBoundingClientRect();
    var c = host.getBoundingClientRect();
    if (c.width <= v.width) panX -= (c.left + c.width / 2) - (v.left + v.width / 2);
    else if (c.left > v.left) panX -= c.left - v.left;
    else if (c.right < v.right) panX += v.right - c.right;
    if (c.height <= v.height) panY -= (c.top + c.height / 2) - (v.top + v.height / 2);
    else if (c.top > v.top) panY -= c.top - v.top;
    else if (c.bottom < v.bottom) panY += v.bottom - c.bottom;
  }

  /* The one place the mode and the scale change. anchor is a client point to
     hold still (a pointer, a pinch centre); without one the viewport's own
     centre is held, which is what a button press should do.

     onLayout is app.js's fitViewer: leaving or entering fit changes whether
     the picture elements carry a pixel maximum at all, so the layout has to
     be redone between the two measurements, not after them. */
  function applyScale(next, anchor) {
    var host = content();
    var before = host ? host.getBoundingClientRect() : null;
    var ax = anchor ? anchor.x : null, ay = anchor ? anchor.y : null;
    if (!anchor) {
      var vp = byId("viewport");
      if (vp) {
        var v = vp.getBoundingClientRect();
        ax = v.left + v.width / 2; ay = v.top + v.height / 2;
      }
    }
    var fx = 0.5, fy = 0.5;
    if (before && before.width > 1 && before.height > 1 && ax !== null) {
      fx = (ax - before.left) / before.width;
      fy = (ay - before.top) / before.height;
    }

    if (next === "fit") {
      mode = "fit"; scale = 1; panX = 0; panY = 0;
    } else {
      mode = "zoom";
      scale = clamp(next, MIN_SCALE, MAX_SCALE);
    }
    if (api && api.onLayout) api.onLayout();
    paint();

    var host2 = content();
    if (mode === "zoom" && ax !== null && host2) {
      var after = host2.getBoundingClientRect();
      if (after.width > 1 && after.height > 1) {
        panX += ax - (after.left + fx * after.width);
        panY += ay - (after.top + fy * after.height);
      }
    }
    clampPan();
    paint();
    if (api && api.onZoom) api.onZoom(mode, shownScale());
  }

  function zoomBy(factor, anchor) {
    applyScale(shownScale() * factor, anchor);
  }

  /* ---- input ------------------------------------------------------------- */

  function pointFrom(ev) { return { x: ev.clientX, y: ev.clientY }; }

  function onWheel(ev) {
    /* ctrl or cmd only. A plain wheel over the viewer is not a zoom: the
       viewer sits inside a scrolling column (see the widget grid), and
       stealing the wheel there would make the page impossible to scroll
       with the pointer over the picture. Chrome delivers a trackpad or
       touchscreen pinch as a wheel event with ctrlKey set, so pinch to zoom
       arrives here too. */
    if (!(ev.ctrlKey || ev.metaKey)) return;
    if (!panTargetOk(ev)) return;
    ev.preventDefault();
    var factor = Math.exp(-ev.deltaY * 0.0035);
    zoomBy(clamp(factor, 0.2, 5), pointFrom(ev));
  }

  /* Everything inside #viewport that owns its own pointer already. The
     reference pane is the big one: it lives inside the viewport (see
     #viewport.withref in style.css) and its own rectangle drag would
     otherwise pan the picture behind it. window-editor.js stops propagation
     on every grip and on its picking catcher, so those never reach here at
     all; the rest are listed because they do. */
  var NOT_PAN = "#refPane, #sheet, #splitHandle, .crophandle, button, select, input, label";

  function panTargetOk(ev) {
    var t = ev.target;
    if (!t || !t.closest) return true;
    return !t.closest(NOT_PAN);
  }

  function onPointerDown(ev) {
    if (ev.button !== undefined && ev.button !== 0) return;
    pointers[ev.pointerId] = pointFrom(ev);
    if (Object.keys(pointers).length === 2) { startPinch(); return; }
    if (mode === "fit" && !spaceHeld) return;
    if (!panTargetOk(ev)) return;
    pan = { id: ev.pointerId, x: ev.clientX, y: ev.clientY, px: panX, py: panY, moved: false };
    paint();
  }

  function onPointerMove(ev) {
    if (pointers[ev.pointerId]) pointers[ev.pointerId] = pointFrom(ev);
    if (pinch) { movePinch(); return; }
    if (!pan || pan.id !== ev.pointerId) return;
    var dx = ev.clientX - pan.x, dy = ev.clientY - pan.y;
    if (!pan.moved && Math.abs(dx) < 3 && Math.abs(dy) < 3) return;
    /* Only once the pointer really moved: a press that turns out to be a
       click on a slot button must stay a click. */
    if (!pan.moved) {
      pan.moved = true;
      try { byId("viewport").setPointerCapture(ev.pointerId); } catch (e) { /* synthetic pointer */ }
    }
    ev.preventDefault();
    panX = pan.px + dx;
    panY = pan.py + dy;
    clampPan();
    paint();
  }

  function onPointerUp(ev) {
    delete pointers[ev.pointerId];
    if (pinch && Object.keys(pointers).length < 2) pinch = null;
    if (pan && pan.id === ev.pointerId) {
      try { byId("viewport").releasePointerCapture(ev.pointerId); } catch (e) { /* never captured */ }
      pan = null;
      paint();
    }
  }

  function pinchState() {
    var ids = Object.keys(pointers);
    var a = pointers[ids[0]], b = pointers[ids[1]];
    var dx = a.x - b.x, dy = a.y - b.y;
    return {
      dist: Math.max(1, Math.sqrt(dx * dx + dy * dy)),
      centre: { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 }
    };
  }

  function startPinch() {
    pan = null;
    var s = pinchState();
    pinch = { dist: s.dist, scale: shownScale() };
    paint();
  }

  function movePinch() {
    var s = pinchState();
    applyScale(pinch.scale * (s.dist / pinch.dist), s.centre);
  }

  function onDoubleClick(ev) {
    if (!panTargetOk(ev)) return;
    ev.preventDefault();
    if (mode === "fit") applyScale(1, pointFrom(ev));
    else applyScale("fit");
  }

  /* Space is already app.js's "hold to peek at the ungraded frame", and it
     stays that. This only adds the second meaning the contract asks for:
     while it is held, a drag pans even at fit. Its own listeners, so
     app.js's handler is untouched and neither can swallow the other. */
  function onKeyDown(ev) {
    if (ev.key !== " " || spaceHeld) return;
    if (isTyping(ev.target)) return;
    spaceHeld = true;
    paint();
  }

  function onKeyUp(ev) {
    if (ev.key !== " ") return;
    spaceHeld = false;
    paint();
  }

  function isTyping(el) {
    if (!el || !el.tagName) return false;
    var t = el.tagName.toLowerCase();
    return t === "input" || t === "textarea" || t === "select" || el.isContentEditable;
  }

  /* ---- wiring ------------------------------------------------------------ */

  function bind() {
    var vp = byId("viewport");
    if (!vp) return;
    vp.addEventListener("wheel", onWheel, { passive: false });
    vp.addEventListener("pointerdown", onPointerDown);
    vp.addEventListener("dblclick", onDoubleClick);
    /* On window, not on the viewport: a pan that leaves the picture (or the
       page) must still end, and must still be following the pointer while
       it is outside. */
    window.addEventListener("pointermove", onPointerMove, { passive: false });
    window.addEventListener("pointerup", onPointerUp);
    window.addEventListener("pointercancel", onPointerUp);
    window.addEventListener("keydown", onKeyDown);
    window.addEventListener("keyup", onKeyUp);
    window.addEventListener("blur", function () { spaceHeld = false; pan = null; paint(); });

    bindButton("fitBtn", "fit");
    bindButton("oneToOneBtn", 1);
    bindButton("zoom200Btn", 2);
    bindButton("zoom400Btn", 4);
    var out = byId("zoomOutBtn");
    if (out) out.addEventListener("click", function () { zoomBy(1 / STEP, null); });
    var into = byId("zoomInBtn");
    if (into) into.addEventListener("click", function () { zoomBy(STEP, null); });
  }

  function bindButton(id, value) {
    var b = byId(id);
    if (b) b.addEventListener("click", function () { applyScale(value, null); });
  }

  function init(opts) {
    api = opts || null;
    bind();
    applyScale("fit");
  }

  global.ViewerZoom = {
    init: init,
    /* set("fit") or set(1.5): the same entry point the buttons use, so a
       caller and a button cannot take different routes to the same state. */
    set: function (v, anchor) { applyScale(v, anchor || null); },
    step: function (dir) { zoomBy(dir > 0 ? STEP : 1 / STEP, null); },
    toggle: function () { applyScale(mode === "fit" ? 1 : "fit", null); },
    /* Re-measure and re-clamp without changing the zoom: the layout moved
       (a sidebar dragged, a new frame with different dimensions, the frames
       grid switched on). */
    refresh: function () { clampPan(); paint(); },
    /* How big the picture is on screen as a multiple of its own pixels: the
       number the label shows and the number a wheel zoom continues from. It
       is NOT 1 at fit (the picture is fitted down to the viewport), so it is
       not the number an overlay divides by. */
    scale: function () { return shownScale(); },
    /* The one number window-editor.js needs: the CSS transform actually on
       the content box, which is exactly 1 at fit. Its geometry is computed
       from getBoundingClientRect differences, which are in SCREEN pixels,
       while the SVG it draws into is inside the transformed box and measures
       in untransformed ones; dividing by this is what keeps the two in
       register, and dividing by 1 at fit leaves the pre-zoom behaviour
       untouched. */
    stageScale: function () { return mode === "zoom" ? scale : 1; },
    isFit: function () { return mode === "fit"; },
    percent: percent
  };
})(window);
