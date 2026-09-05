/* Mobile mode for Fixxr Studio (contract C8,
 * plan/2026-09-05-studio-projects/PLAN.md).
 *
 * Direct request: "make a mobile mode too where those are buttons at the top
 * and the widgets are scrolling. the sidebars are basically new pages. On the
 * properties right sidebar, i should be able to see the preview while
 * scrolling (so preview floats, and scrolling properties/parameters widget/
 * panels should fade underneath it)".
 *
 * The layout itself is CSS (style.css's mobile block, keyed on the
 * data-mobile and data-mobilepage attributes static/sidebars.js paints on
 * <html> before first paint). This file is only the three things CSS cannot
 * do:
 *
 *   1. The page bar. #mobilebar's four buttons choose the page, and the two
 *      right sidebar ones (Grade, History) go through the REAL .paramtab
 *      button rather than setting data-paramtab themselves, so history.js's
 *      own click handler still runs and still refreshes the tree. There is
 *      one tab model, not a phone copy of it.
 *   2. GridStack off. The mobile CSS stacks the cards, but a stacked card
 *      whose drag handle still works can still be picked up and dropped into
 *      a layout that is then saved to localStorage and waiting on the desktop
 *      later. setStatic(true) is what actually stops that. Reversed on the
 *      way back out, so resizing a window across the breakpoint restores a
 *      normal, draggable desktop grid.
 *   3. The floating preview's picture. #mobilePreviewCanvas is a MIRROR of
 *      the viewer, never a second renderer: each tick it copies whichever of
 *      #frameImg, #gpuCanvas or #playerVideo the main viewer is showing right
 *      now (read from #stage's own classes, which app.js's setStageLayer is
 *      the single writer of) with one drawImage. It asks the server for
 *      nothing, reads no config and starts no GPU work, so a slider drag on a
 *      phone costs exactly what the same drag costs on a desktop.
 *
 * Nothing here writes grade, session or project state, and nothing here is
 * reachable at a desktop width: every entry point returns early unless
 * window.fixxrSidebars.isMobile() is true.
 */
(function (global) {
  "use strict";

  var doc = global.document;
  var root = doc.documentElement;

  function $(id) { return doc.getElementById(id); }

  function sidebars() { return global.fixxrSidebars || null; }

  function isMobile() {
    var s = sidebars();
    return s && s.isMobile ? !!s.isMobile() : root.getAttribute("data-mobile") === "on";
  }

  function currentPage() {
    var s = sidebars();
    return (s && s.mobilePage ? s.mobilePage() : root.getAttribute("data-mobilepage")) || "preview";
  }

  function currentTab() {
    return root.getAttribute("data-paramtab") === "history" ? "history" : "grade";
  }

  /* ---- the page bar ----------------------------------------------------- */

  function markActive() {
    var page = currentPage();
    var tab = currentTab();
    var btns = doc.querySelectorAll("#mobilebar .mobiletab");
    for (var i = 0; i < btns.length; i++) {
      var b = btns[i];
      var wantPage = b.getAttribute("data-mobilepage");
      var wantTab = b.getAttribute("data-paramtab");
      // A Grade or History button is active only when its page is showing AND
      // its tab is the one open in it: two buttons onto one page.
      var on = wantPage === page && (!wantTab || wantTab === tab);
      b.classList.toggle("active", on);
      b.setAttribute("aria-pressed", on ? "true" : "false");
    }
  }

  function goTo(page, tab) {
    if (tab) {
      // Click the real tab strip button so history.js's handler runs (it is
      // what refreshes the commit tree and re-lays the graph out); it calls
      // fixxrSidebars.setParamTab itself. Falling back to setting the
      // attribute directly only if that button is somehow absent.
      var real = doc.querySelector('.paramtab[data-paramtab="' + tab + '"]');
      if (real) real.click();
      else if (sidebars() && sidebars().setParamTab) sidebars().setParamTab(tab);
      else root.setAttribute("data-paramtab", tab);
    }
    if (sidebars() && sidebars().setMobilePage) sidebars().setMobilePage(page);
    else root.setAttribute("data-mobilepage", page);
    markActive();
    syncPreview();
  }

  function bindBar() {
    var btns = doc.querySelectorAll("#mobilebar .mobiletab");
    for (var i = 0; i < btns.length; i++) {
      btns[i].addEventListener("click", function (ev) {
        var b = ev.currentTarget;
        goTo(b.getAttribute("data-mobilepage"), b.getAttribute("data-paramtab"));
      });
    }
  }

  /* ---- GridStack: stacked cards must not still be draggable -------------- */

  var gridWanted = null;   // last state asked for, so a late grid still gets it

  function gridInstance() {
    var el = $("gridMid");
    return el && el.gridstack ? el.gridstack : null;
  }

  function applyGrid(makeStatic) {
    gridWanted = makeStatic;
    var g = gridInstance();
    if (!g) return false;
    try {
      g.setStatic(makeStatic);
    } catch (e) {
      // A vendored build that ever dropped setStatic must not take the page
      // down: the CSS half already stacks the cards either way.
      return false;
    }
    return true;
  }

  // app.js builds the grid inside boot(), which is async, so this file cannot
  // assume one exists when it loads. Poll briefly rather than reaching into
  // app.js for a hook: it is a handful of cheap property reads and it stops
  // as soon as the grid answers.
  function waitForGrid() {
    var tries = 0;
    var timer = global.setInterval(function () {
      tries += 1;
      if (gridInstance()) {
        if (gridWanted !== null) applyGrid(gridWanted);
        global.clearInterval(timer);
      } else if (tries > 150) {          // ~30s: the grid is never coming
        global.clearInterval(timer);
      }
    }, 200);
  }

  /* ---- the floating preview mirror --------------------------------------- */

  var rafId = 0;
  var lastPaint = 0;
  var MIN_INTERVAL_MS = 60;   // ~16 mirrored frames a second, plenty to read

  // Which element the main viewer is actually showing this instant. #stage's
  // two classes are written in exactly one place (setStageLayer in app.js),
  // so reading them here cannot disagree with what is on screen.
  function liveSource() {
    var stage = $("stage");
    if (!stage) return null;
    if (stage.classList.contains("playing")) {
      var v = $("playerVideo");
      if (v && v.videoWidth) return { el: v, w: v.videoWidth, h: v.videoHeight };
    }
    if (stage.classList.contains("gpu-live")) {
      var c = $("gpuCanvas");
      if (c && c.width && c.height) return { el: c, w: c.width, h: c.height };
    }
    var img = $("frameImg");
    if (img && img.naturalWidth) return { el: img, w: img.naturalWidth, h: img.naturalHeight };
    return null;
  }

  function paintPreview() {
    var box = $("mobilepreview");
    var canvas = $("mobilePreviewCanvas");
    if (!box || !canvas) return;
    var rect = box.getBoundingClientRect();
    if (rect.width < 2 || rect.height < 2) return;
    // Capped at 2, not the raw devicePixelRatio: this is a small reference
    // copy of a picture that is already being rendered at full size
    // elsewhere, and a phone reporting 3 would triple the readback for a
    // difference nobody can see at this size.
    var dpr = Math.min(global.devicePixelRatio || 1, 2);
    var W = Math.max(1, Math.round(rect.width * dpr));
    var H = Math.max(1, Math.round(rect.height * dpr));
    if (canvas.width !== W || canvas.height !== H) {
      canvas.width = W;
      canvas.height = H;
    }
    var ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.fillStyle = "#0e0e0e";
    ctx.fillRect(0, 0, W, H);
    var src = liveSource();
    if (!src) return;
    var scale = Math.min(W / src.w, H / src.h);
    var dw = Math.max(1, Math.round(src.w * scale));
    var dh = Math.max(1, Math.round(src.h * scale));
    try {
      ctx.drawImage(src.el, Math.round((W - dw) / 2), Math.round((H - dh) / 2), dw, dh);
    } catch (e) {
      // A frame that is mid decode throws rather than drawing; the next tick
      // gets it. Never let that reach the console: this spec's own boot check
      // fails the run on any console error.
    }
  }

  function tick(now) {
    rafId = global.requestAnimationFrame(tick);
    if (now - lastPaint < MIN_INTERVAL_MS) return;
    lastPaint = now;
    paintPreview();
  }

  function previewShouldRun() {
    return isMobile() && currentPage() === "params" && currentTab() === "grade";
  }

  function syncPreview() {
    if (previewShouldRun()) {
      if (!rafId) {
        lastPaint = 0;
        rafId = global.requestAnimationFrame(tick);
      }
      // One immediate paint so the picture is there on the first frame the
      // page is shown, not 60ms later.
      paintPreview();
    } else if (rafId) {
      global.cancelAnimationFrame(rafId);
      rafId = 0;
    }
  }

  /* ---- wiring ------------------------------------------------------------ */

  function syncAll() {
    applyGrid(isMobile());
    markActive();
    syncPreview();
  }

  function init() {
    bindBar();
    // sidebars.js fires this on <html> when the media query flips and on every
    // setMobilePage; both are cases where the grid, the active button and the
    // mirror all have to be re-decided.
    root.addEventListener("studio:mobile", syncAll);
    // The right sidebar's own tab strip is on screen on the params page too,
    // so a tap there has to move the bar's highlight and start or stop the
    // mirror. Watching the attribute covers every writer of it (the strip,
    // sidebars.js's pre-paint read, and this file) with one listener.
    if (global.MutationObserver) {
      new global.MutationObserver(syncAll).observe(root, {
        attributes: true,
        attributeFilter: ["data-paramtab", "data-mobile", "data-mobilepage"]
      });
    }
    global.addEventListener("resize", syncPreview);
    waitForGrid();
    syncAll();
  }

  if (doc.readyState === "loading") doc.addEventListener("DOMContentLoaded", init);
  else init();

  // Exposed for the harness (studio/tests/specs/22-mobile.mjs) and for a
  // person poking at the page in a console: a read only view plus the same
  // page switch the buttons perform, never a second way to set state.
  global.StudioMobile = {
    isMobile: isMobile,
    page: currentPage,
    goTo: goTo,
    paintPreview: paintPreview,
    previewRunning: function () { return !!rafId; }
  };
})(window);
