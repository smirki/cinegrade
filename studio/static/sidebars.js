// Sidebar collapsed-state and width boot (job: app shell restructure, then
// resizable sidebars). Loaded as a blocking classic script in <head>, before
// any CSS, same reasoning as theme.js right next to it there: it sets
// data-sidebar-left/right AND the two --sidebar-*-w custom properties on
// <html> synchronously so the very first paint already has each sidebar's
// collapsed-or-expanded state and its dragged-or-default width. Doing either
// of those from app.js instead, after the DOM and CSS are already up, would
// show a flash (open-then-closed, or default-width-then-resized) on every
// reload for anyone who has ever collapsed or resized a sidebar -- exactly
// the problem theme.js's own comment describes for the theme attribute.
//
// Two independent keys per axis, not one: the left (sources) and right
// (parameters + utilities) sidebars collapse from #maintoolbar's two separate
// toggle buttons and resize from their own separate drag handles (see
// initSidebarResizers in app.js), so their stored state has to be separate
// too.
(function () {
  "use strict";
  var COLLAPSE_KEYS = { left: "fixxr-studio-sidebar-left", right: "fixxr-studio-sidebar-right" };
  var collapsed = { left: false, right: false };

  function readCollapsed(side) {
    try {
      return window.localStorage.getItem(COLLAPSE_KEYS[side]) === "collapsed";
    } catch (e) {
      // localStorage can throw in private-browsing/sandboxed contexts; degrade to
      // the default (expanded) instead of breaking page load.
      return false;
    }
  }

  function applyCollapsed(side, isCollapsed) {
    var root = document.documentElement;
    var attr = "data-sidebar-" + side;
    if (isCollapsed) root.setAttribute(attr, "collapsed");
    else root.removeAttribute(attr);
  }

  collapsed.left = readCollapsed("left");
  collapsed.right = readCollapsed("right");
  applyCollapsed("left", collapsed.left);
  applyCollapsed("right", collapsed.right);

  function toggle(side) {
    if (side !== "left" && side !== "right") return collapsed[side];
    collapsed[side] = !collapsed[side];
    applyCollapsed(side, collapsed[side]);
    try {
      window.localStorage.setItem(COLLAPSE_KEYS[side], collapsed[side] ? "collapsed" : "open");
    } catch (e) {
      // Non-fatal: the collapsed state just will not persist across reloads.
    }
    return collapsed[side];
  }

  // ---- width: a drag-resizable px width per sidebar, same persistence
  // technique as collapse above (read + paint before first CSS, write on
  // change). Painted as a CSS custom property rather than a direct width so
  // one write here drives both the outer collapsing box AND the inner fixed-
  // width content wrapper each sidebar has (#leftrail, #paramsdock -- see
  // style.css's app-shell section for why the inner wrapper has to stay a
  // steady width while the outer one animates to/from 0 on collapse).
  //
  // Defaults and clamps roughly follow research/unsloth/studio/frontend's own
  // hooks/use-sidebar-width.ts (SIDEBAR_WIDTH_DEFAULT/MIN/MAX): a persisted
  // pixel preference, clamped to an absolute [min, max] range on every read
  // and write. Unsloth also clamps a second time to a fraction of the
  // viewport width so one panel can never eat most of a narrow window; this
  // file does not repeat that second clamp because nothing here runs before
  // window.innerWidth is available the way this pre-CSS script needs to run
  // before layout -- app.js's own resize handling (computeCellHeight and
  // friends) already has to cope with a short/narrow window elsewhere, and a
  // sidebar pinned at its MAX (480px) on the narrowest window this tool
  // targets (1280px) still leaves #mainBg comfortably over half the window.
  var WIDTH_KEYS = { left: "fixxr-studio-sidebar-left-w", right: "fixxr-studio-sidebar-right-w" };
  // left: the sources rail (refs/LUTs/jobs/clips tab strip + lists) --
  // unchanged from the fixed 260px it always rendered at.
  // right: parameters (the whole grade accordion) plus the utility strip --
  // wider than the old 190px utilities-only sidebar because it now carries
  // the control panel that used to be its own ~24%-of-#grid GridStack
  // column (roughly 240-350px across the widths this tool is used at).
  var WIDTH_DEFAULT = { left: 260, right: 300 };
  var WIDTH_MIN = { left: 200, right: 240 };
  var WIDTH_MAX = { left: 460, right: 480 };
  var width = { left: 0, right: 0 };

  function clampWidth(side, px) {
    if (typeof px !== "number" || !isFinite(px)) px = WIDTH_DEFAULT[side];
    return Math.max(WIDTH_MIN[side], Math.min(WIDTH_MAX[side], Math.round(px)));
  }

  function readWidth(side) {
    try {
      var raw = window.localStorage.getItem(WIDTH_KEYS[side]);
      if (raw === null) return WIDTH_DEFAULT[side];
      return clampWidth(side, parseFloat(raw));
    } catch (e) {
      return WIDTH_DEFAULT[side];
    }
  }

  function paintWidth(side, px) {
    document.documentElement.style.setProperty("--sidebar-" + side + "-w", px + "px");
  }

  width.left = readWidth("left");
  width.right = readWidth("right");
  paintWidth("left", width.left);
  paintWidth("right", width.right);

  // Live-preview a width mid-drag without persisting it: initSidebarResizers
  // in app.js calls this on every pointermove, and setWidth (below) only on
  // pointerup, the same split live-paint/persist-on-release app.js's old
  // column splitter used (see saveColumnWidths there).
  function applyWidth(side, px) {
    if (side !== "left" && side !== "right") return width[side];
    var clamped = clampWidth(side, px);
    width[side] = clamped;
    paintWidth(side, clamped);
    return clamped;
  }

  function setWidth(side, px) {
    var clamped = applyWidth(side, px);
    try {
      window.localStorage.setItem(WIDTH_KEYS[side], String(clamped));
    } catch (e) {
      // Non-fatal: the width just will not persist across reloads.
    }
    return clamped;
  }

  function resetWidth(side) {
    return setWidth(side, WIDTH_DEFAULT[side]);
  }

  // Exposed for app.js's toggle-button and drag-handle handlers, same
  // pattern as window.fixxrTheme in theme.js.
  window.fixxrSidebars = {
    isCollapsed: function (side) { return !!collapsed[side]; },
    toggle: toggle,
    getWidth: function (side) { return width[side]; },
    clampWidth: clampWidth,
    applyWidth: applyWidth,
    setWidth: setWidth,
    resetWidth: resetWidth,
    WIDTH_MIN: WIDTH_MIN,
    WIDTH_MAX: WIDTH_MAX,
    WIDTH_DEFAULT: WIDTH_DEFAULT
  };

  // ---- right sidebar tab strip (contract C3, History panel): Grade |
  // History. Same pre-paint, no-flash technique as collapsed/width above --
  // read and paint a data-paramtab attribute on <html> before any CSS or
  // layout runs, so a reload never flashes the Grade pane before switching
  // to a History tab somebody left open (see style.css's
  // html[data-paramtab=...] rules and static/history.js, which is the only
  // file that calls setParamTab). A plain global preference, not per
  // project: which pane is open is a UI choice, not project state.
  var PARAMTAB_KEY = "fixxr-studio-paramtab";
  var paramTab = "grade";

  function readParamTab() {
    try {
      return window.localStorage.getItem(PARAMTAB_KEY) === "history" ? "history" : "grade";
    } catch (e) {
      return "grade";
    }
  }

  function applyParamTab(tab) {
    document.documentElement.setAttribute("data-paramtab", tab);
  }

  paramTab = readParamTab();
  applyParamTab(paramTab);

  function setParamTab(tab) {
    paramTab = tab === "history" ? "history" : "grade";
    applyParamTab(paramTab);
    try {
      window.localStorage.setItem(PARAMTAB_KEY, paramTab);
    } catch (e) {
      // Non-fatal: the tab just will not persist across reloads.
    }
    return paramTab;
  }

  window.fixxrSidebars.paramTab = function () { return paramTab; };
  window.fixxrSidebars.setParamTab = setParamTab;

  // ---- mobile mode (contract C8) -----------------------------------------
  //
  // Direct request: "make a mobile mode too where those are buttons at the top
  // and the widgets are scrolling. the sidebars are basically new pages."
  //
  // Two attributes on <html>, both written here for the same reason the three
  // above are: this file is a blocking script in <head>, so the very first
  // paint already knows whether this is a phone and which page was left open.
  // Deciding either of those from app.js instead would render the desktop
  // three-column shell first and snap to the phone layout a frame later.
  //
  // data-mobile="on"    : this viewport gets the one-page-at-a-time layout.
  // data-mobilepage=... : which page that is, "clips" | "preview" | "params".
  //
  // "params" is one page carrying BOTH right sidebar tabs; which of them is
  // showing is still data-paramtab, exactly as on desktop, so the Grade and
  // History buttons in the mobile bar are two buttons onto one page rather
  // than a second, parallel notion of the same state. Both attributes are
  // painted on every viewport, phone or not, so nothing has to be re-written
  // when the window is resized across the breakpoint; only data-mobile
  // decides whether the mobile rules in style.css apply at all.
  //
  // The query is the one contract C8 names: under 900px wide, or a coarse
  // pointer (a finger, not a mouse) on anything under 1100px. The second half
  // is what catches a tablet held in landscape, which is wider than a phone
  // but has no hover and no precise pointer.
  var MOBILE_QUERY = "(max-width: 899px), (pointer: coarse) and (max-width: 1100px)";
  var MOBILE_PAGE_KEY = "fixxr-studio-mobilepage";
  var MOBILE_PAGES = { clips: true, preview: true, params: true };
  var mobilePage = "preview";
  var mobileOn = false;
  var mobileMq = null;

  function readMobilePage() {
    try {
      var raw = window.localStorage.getItem(MOBILE_PAGE_KEY);
      return MOBILE_PAGES[raw] ? raw : "preview";
    } catch (e) {
      return "preview";
    }
  }

  function applyMobilePage(page) {
    document.documentElement.setAttribute("data-mobilepage", page);
  }

  function applyMobile(on) {
    var root = document.documentElement;
    if (on) root.setAttribute("data-mobile", "on");
    else root.removeAttribute("data-mobile");
  }

  function announceMobile() {
    // Fired on <html> rather than window so a listener registered before the
    // body exists still hears it, and dispatched only after both attributes
    // are already painted, so a handler reading the DOM sees the new state.
    try {
      document.documentElement.dispatchEvent(new CustomEvent("studio:mobile", {
        bubbles: true,
        detail: { mobile: mobileOn, page: mobilePage }
      }));
    } catch (e) {
      // CustomEvent is available everywhere this tool runs; a failure here
      // must not take the boot script down with it.
    }
  }

  function setMobilePage(page) {
    mobilePage = MOBILE_PAGES[page] ? page : "preview";
    applyMobilePage(mobilePage);
    try {
      window.localStorage.setItem(MOBILE_PAGE_KEY, mobilePage);
    } catch (e) {
      // Non-fatal: the page just will not persist across reloads.
    }
    announceMobile();
    return mobilePage;
  }

  mobilePage = readMobilePage();
  applyMobilePage(mobilePage);
  try {
    mobileMq = window.matchMedia(MOBILE_QUERY);
    mobileOn = !!mobileMq.matches;
  } catch (e) {
    mobileOn = false;
  }
  applyMobile(mobileOn);

  if (mobileMq && mobileMq.addEventListener) {
    // Live, not read once: the harness drives this by resizing one page from
    // a desktop viewport to a phone one and back, and a person dragging a
    // window narrow should get the same thing.
    mobileMq.addEventListener("change", function (ev) {
      mobileOn = !!ev.matches;
      applyMobile(mobileOn);
      announceMobile();
    });
  }

  window.fixxrSidebars.isMobile = function () { return mobileOn; };
  window.fixxrSidebars.mobilePage = function () { return mobilePage; };
  window.fixxrSidebars.setMobilePage = setMobilePage;
})();
