/* The timeline: ruler, scrub, filmstrip, mark flags, loop range, transport.
 *
 * Founder's words: "improve the timeline, scrub, feel, ux of the timeline?
 * like it should be wayyyy better than what it is. Clean."
 *
 * What this replaces: a native <input type="range" id="scrub"> with 1000
 * steps, a second row of "from"/"to" text boxes for the GPU loop, a "secs"
 * text box for the play length, a horizontally scrolling filmstrip that had
 * nothing to do with the bar above it, and a wrapping row of mark chips
 * underneath. Every number that used to be typed into a box is now a thing on
 * the ruler that can be seen and dragged.
 *
 * What it deliberately does NOT do: own any state. The playhead is still
 * S.time in app.js and moves only through app.js's setTime (which is what
 * stops playback, previews the proxy frame, schedules the real render and
 * saves the playhead on the project). The play length is still the value of
 * #playDur, saved as extras.play_secs by app.js's own change listener. The
 * GPU loop range is still #loopStart/#loopEnd, read by app.js's loopDuration.
 * This file paints those values and writes them back through the same
 * handlers a person typing into the old boxes went through, so the server
 * side contract and both playback engines are untouched.
 *
 * The loop range's IN point is the playhead, because that is what the engine
 * has always done: playLoopRange(startTime) in app.js returns
 * [playhead, playhead + secs] when secs is set, and the whole clip when it is
 * not. The band on the ruler is therefore drawn hanging off the playhead line
 * rather than floating free, which is the honest picture of "loop the next
 * N seconds from here", and dragging its out handle is what sets N.
 *
 * Geometry: everything that changes rarely (ticks, filmstrip, mark flags, the
 * range band) is positioned in percent, so a resize costs nothing but a tick
 * rebuild. The one thing that moves every frame during playback (the
 * playhead, and the hover line under the pointer) is moved with a transform
 * on a cached track width, coalesced into one requestAnimationFrame, so a
 * playing clip never triggers layout from here.
 */
(function (global) {
  "use strict";

  var doc = global.document;
  function $(id) { return doc.getElementById(id); }

  var api = null;              // app.js's own functions, handed over by init()
  var els = {};
  var W = 0;                   // cached track width in px
  var rect = null;             // cached track rect, invalidated on scroll/resize
  var headRaf = 0;
  var drag = null;             // the live pointer gesture, if any
  var shuttleState = null;     // { rate, raf, last }
  var lastStep = -1;           // the tick step the ruler was last built at
  var STRIP_TILES = 16;
  var stripSig = "";           // clip, duration and rotation the strip shows

  /* ---- small readers over app.js's state -------------------------------- */

  function duration() { return (api && api.getDuration()) || 0; }
  function fps() { return (api && api.getFps()) || 24; }
  function time() { return (api && api.getTime()) || 0; }
  function marks() { return (api && api.getMarks()) || []; }
  function lastFrameTime() {
    var d = duration();
    return d > 0 ? Math.max(0, d - 1 / fps()) : 0;
  }
  function clampTime(t) {
    if (!isFinite(t)) return 0;
    return Math.max(0, Math.min(t, lastFrameTime()));
  }
  // The timeline addresses frames, not milliseconds: a scrub that lands
  // between two frames asks the server for a frame it will round to anyway,
  // and then the label and the picture disagree by a fraction.
  function snapFrame(t) {
    var f = fps();
    return f > 0 ? Math.round(t * f) / f : t;
  }
  function pct(t) {
    var d = duration();
    return d > 0 ? Math.max(0, Math.min(100, (t / d) * 100)) : 0;
  }

  /* The play length, in seconds, or null for "the whole clip". Same rule as
     app.js's playDurationValue: anything that will not parse to a positive
     number means the whole clip, so a half-typed value is never a range. */
  function secs() {
    var v = parseFloat(($("playDur") || {}).value);
    return (isFinite(v) && v > 0) ? v : null;
  }

  function rangeEnd() {
    var s = secs();
    if (s == null) return null;
    var d = duration();
    return d > 0 ? Math.min(time() + s, d) : time() + s;
  }

  /* ---- writing back through app.js's own handlers ----------------------- */

  /* The play length. `commit` fires the same "change" event a person leaving
     the old text box fired, which is what app.js saves as extras.play_secs;
     a drag therefore repaints on every move and saves once, on release. */
  function setSecs(value, commit) {
    var el = $("playDur");
    if (!el) return;
    el.value = value == null ? "" : String(+value.toFixed(2));
    if (commit) el.dispatchEvent(new Event("change", { bubbles: true }));
    paintRange();
    syncLoopFields();
  }

  /* The GPU loop (mode 2) reads these two fields directly in app.js's
     loopDuration(), so the visible range has to keep them true. With no
     range set, this holds the two seconds from the playhead that the old
     "Use marks" button produced by default, rather than the whole clip:
     mode 2 decodes its range into memory and has a budget, so "no range
     means everything" would turn pressing Loop into a refusal. */
  function syncLoopFields() {
    var a = $("loopStart"), b = $("loopEnd");
    if (!a || !b) return;
    var t = time();
    var end = rangeEnd();
    if (end == null) {
      var d = duration();
      end = d > 0 ? Math.min(d, t + 2) : t + 2;
    }
    a.value = t.toFixed(2);
    b.value = end.toFixed(2);
  }

  /* ---- geometry --------------------------------------------------------- */

  function trackRect() {
    if (!rect && els.track) rect = els.track.getBoundingClientRect();
    return rect || { left: 0, width: 0 };
  }
  function invalidateRect() { rect = null; }

  function timeAtX(clientX) {
    var r = trackRect();
    var f = r.width > 0 ? (clientX - r.left) / r.width : 0;
    return clampTime(snapFrame(f * duration()));
  }

  /* ---- painting --------------------------------------------------------- */

  function fmtClock(t) {
    if (t >= 60) {
      var m = Math.floor(t / 60);
      var s = t - m * 60;
      return m + ":" + (s < 10 ? "0" : "") + s.toFixed(2);
    }
    return t.toFixed(2) + "s";
  }

  function paintHead() {
    headRaf = 0;
    var t = time();
    var d = duration();
    var x = d > 0 ? (t / d) * W : 0;
    if (els.head) els.head.style.transform = "translateX(" + x.toFixed(2) + "px)";
    if (els.track) {
      els.track.setAttribute("aria-valuenow", t.toFixed(3));
      els.track.setAttribute("aria-valuetext", fmtClock(t));
    }
  }

  function highlightTile() {
    if (!els.strip) return;
    var tiles = els.strip.children;
    var d = duration();
    var idx = d > 0 ? Math.min(STRIP_TILES - 1, Math.floor((time() / d) * STRIP_TILES)) : 0;
    for (var i = 0; i < tiles.length; i++) {
      tiles[i].classList.toggle("active", i === idx);
    }
  }

  /* The one paint every mover calls: a scrub, a frame step, and all three
     playback engines' per frame callbacks. The label is written straight
     through (a text write, no layout), the playhead is coalesced into one
     animation frame however many times a second this is called, and the
     things that only make sense when nothing is playing are skipped while
     something is. */
  function timeChanged() {
    var t = time();
    if (els.time) els.time.textContent = t.toFixed(2) + "s";
    if (els.frame) els.frame.textContent = "f" + Math.round(t * fps());
    highlightTile();
    if (!headRaf) headRaf = global.requestAnimationFrame(paintHead);
    // The range hangs off the playhead, but the range a lap of playback is
    // running was fixed when Play was pressed: moving the band under a
    // playing clip would draw a range that is not the one being played.
    if (!(api && api.isPlaying && api.isPlaying())) {
      paintRange();
      syncLoopFields();
    }
    paintMarkStates();
  }

  /* Ticks. The step is chosen so a label has room to breathe at the current
     width, from a ladder of round numbers people actually read time in, and
     the ruler is only rebuilt when that choice or the clip changes. */
  var TICK_STEPS = [0.1, 0.25, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600];
  function chooseStep(d, w) {
    for (var i = 0; i < TICK_STEPS.length; i++) {
      if (w * (TICK_STEPS[i] / d) >= 62) return TICK_STEPS[i];
    }
    return TICK_STEPS[TICK_STEPS.length - 1];
  }
  function tickLabel(t, step) {
    if (duration() >= 60) {
      var m = Math.floor(t / 60);
      var s = Math.round(t - m * 60);
      return m + ":" + (s < 10 ? "0" : "") + s;
    }
    return (step < 1 ? t.toFixed(1) : String(Math.round(t))) + "s";
  }
  function buildRuler(force) {
    if (!els.ruler) return;
    var d = duration();
    if (!(d > 0) || W < 40) { els.ruler.innerHTML = ""; lastStep = -1; return; }
    var step = chooseStep(d, W);
    if (!force && step === lastStep) return;
    lastStep = step;
    var html = [];
    var minor = step / 5;
    var minorPx = W * (minor / d);
    for (var t = 0; t <= d + 1e-6; t += minor) {
      var at = Math.round(t / minor) * minor;      // kills float drift on the label
      var major = Math.abs(at / step - Math.round(at / step)) < 1e-6;
      if (!major && minorPx < 7) continue;
      html.push('<i class="tltick' + (major ? " major" : "") + '" style="left:'
        + pct(at).toFixed(4) + '%"></i>');
      if (major) {
        html.push('<u class="tllabel" style="left:' + pct(at).toFixed(4) + '%">'
          + tickLabel(at, step) + "</u>");
      }
    }
    els.ruler.innerHTML = html.join("");
  }

  /* The filmstrip, laid out under the ruler on the same time axis: tile i
     covers exactly the i-th sixteenth of the clip, so a picture is always
     directly under the moment it belongs to. It is not a pointer target
     (style.css gives it pointer-events: none): the whole ruler is one scrub
     surface, and a filmstrip that swallowed a press would put a dead stripe
     across the middle of it. The tiles stay real drag sources for a frame
     slot (contract E4) through the dragstart handler app.js attaches. */
  function buildStrip() {
    if (!els.strip) return;
    var clip = api && api.getClip();
    var d = duration();
    // Each tile is a real decode on the server the first time it is asked
    // for, so a strip that already shows this clip at this length and this
    // rotation is left alone rather than re-requested. Without this, any
    // second call in the same clip switch costs 16 more queued decodes in
    // front of whatever else the page is loading.
    var sig = [clip, d, (api && api.getRotation && api.getRotation()) || "auto"].join("|");
    if (sig === stripSig && els.strip.childElementCount === STRIP_TILES) {
      highlightTile();
      return;
    }
    stripSig = sig;
    els.strip.innerHTML = "";
    if (!clip || !(d > 0)) { stripSig = ""; return; }
    for (var i = 0; i < STRIP_TILES; i++) {
      var t = (d * i) / STRIP_TILES;
      var im = doc.createElement("img");
      im.dataset.t = String(t);
      im.alt = "";
      im.title = t.toFixed(2) + "s";
      im.src = "/api/thumb?clip=" + encodeURIComponent(clip)
        + "&t=" + t.toFixed(3) + "&w=94&rotation="
        + encodeURIComponent((api.getRotation && api.getRotation()) || "auto");
      if (api.makeTimeDraggable) api.makeTimeDraggable(im, t);
      els.strip.appendChild(im);
    }
    highlightTile();
  }

  /* Marks are flags on the ruler now, not chips in a row under it. Each one
     is two hit targets: the flag itself jumps to that frame, and the small
     circle above it (revealed on hover, over the tick labels, where there is
     already empty space) removes it. S.marks in app.js is still the only
     copy, and the contact sheet still reads it unchanged. */
  function drawMarks() {
    if (!els.marks) return;
    var host = els.marks;
    host.innerHTML = "";
    var list = marks();
    for (var i = 0; i < list.length; i++) {
      (function (t, index) {
        var wrap = doc.createElement("div");
        wrap.className = "tlmark";
        wrap.style.left = pct(t) + "%";
        wrap.dataset.t = String(t);

        var kill = doc.createElement("button");
        kill.type = "button";
        kill.className = "tlmarkkill";
        kill.title = "Remove the mark at " + t.toFixed(2) + "s";
        kill.textContent = "x";
        kill.addEventListener("click", function (ev) {
          ev.stopPropagation();
          ev.preventDefault();
          if (api && api.removeMark) api.removeMark(index);
        });
        kill.addEventListener("pointerdown", function (ev) { ev.stopPropagation(); });

        var flag = doc.createElement("span");
        flag.className = "tlmarkflag";
        flag.title = t.toFixed(2) + "s. Click to jump, drag onto a frame slot";
        flag.addEventListener("click", function (ev) {
          ev.stopPropagation();
          if (api && api.setTime) api.setTime(t);
        });
        // The flag owns its own press so a click on it is a jump, not the
        // start of a scrub that would land a frame or two away from the mark.
        flag.addEventListener("pointerdown", function (ev) { ev.stopPropagation(); });
        if (api && api.makeTimeDraggable) api.makeTimeDraggable(flag, t);

        wrap.appendChild(kill);
        wrap.appendChild(flag);
        host.appendChild(wrap);
      }(list[i], i));
    }
    paintMarkStates();
    if (els.clear) els.clear.classList.toggle("tlhidden", list.length === 0);
  }

  function paintMarkStates() {
    if (!els.marks) return;
    var kids = els.marks.children;
    var t = time();
    for (var i = 0; i < kids.length; i++) {
      kids[i].classList.toggle("at", Math.abs(parseFloat(kids[i].dataset.t) - t) < 0.01);
    }
  }

  function paintRange() {
    if (!els.band || !els.out) return;
    var d = duration();
    var s = secs();
    if (!(d > 0)) {
      els.band.style.left = "0%";
      els.band.style.width = "0%";
      return;
    }
    if (s == null) {
      els.range.classList.add("all");
      els.band.style.left = "0%";
      els.band.style.width = "100%";
      els.out.style.left = "100%";
      if (els.bandLabel) els.bandLabel.textContent = "whole clip";
      return;
    }
    els.range.classList.remove("all");
    var start = time();
    var end = rangeEnd();
    els.band.style.left = pct(start) + "%";
    els.band.style.width = Math.max(0, pct(end) - pct(start)) + "%";
    els.out.style.left = pct(end) + "%";
    if (els.bandLabel) els.bandLabel.textContent = (end - start).toFixed(1) + "s loop";
  }

  function paintDuration() {
    var d = duration();
    if (els.dur) els.dur.textContent = fmtClock(d);
    if (els.track) {
      els.track.setAttribute("aria-valuemax", d.toFixed(3));
      els.track.classList.toggle("empty", !(d > 0));
    }
  }

  /* ---- the scrub gesture ------------------------------------------------- */

  /* Direct: the pointer down lands the playhead where it was pressed, the
     drag follows the pointer for as long as it is held (pointer capture, so
     leaving the element or the window does not drop it), and every position
     is snapped to a frame. app.js's setTime does the rest: the proxy answers
     with a frame in the time of a seek, and the real 16-bit render lands on
     its own debounce once the pointer stops, which is what "the full frame
     on release" is. */
  function beginScrub(ev) {
    if (!api || !(duration() > 0)) return;
    if (ev.button !== undefined && ev.button !== 0) return;
    invalidateRect();
    shuttleStop();
    drag = { kind: "scrub", id: ev.pointerId };
    els.track.classList.add("dragging");
    try { els.track.setPointerCapture(ev.pointerId); } catch (e) { /* mouse without capture still works */ }
    ev.preventDefault();
    // Focus so the arrow keys step frames straight after a click, without
    // the column jumping to bring the track into view.
    try { els.track.focus({ preventScroll: true }); } catch (e) { els.track.focus(); }
    api.setTime(timeAtX(ev.clientX));
  }

  function beginRange(ev) {
    if (!api || !(duration() > 0)) return;
    if (ev.button !== undefined && ev.button !== 0) return;
    invalidateRect();
    shuttleStop();
    var onHandle = ev.target === els.out;
    var start = onHandle ? time() : timeAtX(ev.clientX);
    drag = { kind: "range", id: ev.pointerId, start: start, moved: false, had: secs() };
    els.range.classList.add("dragging");
    try { els.range.setPointerCapture(ev.pointerId); } catch (e) { /* as above */ }
    ev.preventDefault();
    ev.stopPropagation();
    // Pressing in the empty lane sets the in point, which IS the playhead;
    // pressing the out handle leaves the playhead alone and only resizes.
    if (!onHandle) api.setTime(start);
  }

  function onMove(ev) {
    if (!drag) return;
    if (drag.kind === "scrub") {
      api.setTime(timeAtX(ev.clientX));
      return;
    }
    var t = timeAtX(ev.clientX);
    var len = t - drag.start;
    drag.moved = true;
    // Dragged back past the in point: the range is gone and Play is back to
    // the whole clip, which is the same thing an empty secs box always meant.
    setSecs(len < 1 / fps() ? null : len, false);
  }

  function endDrag(ev) {
    if (!drag) return;
    var d = drag;
    drag = null;
    els.track.classList.remove("dragging");
    els.range.classList.remove("dragging");
    try {
      if (d.kind === "scrub") els.track.releasePointerCapture(d.id);
      else els.range.releasePointerCapture(d.id);
    } catch (e) { /* already released with the pointer */ }
    if (d.kind === "range") {
      // A press with no drag in the empty lane is a scrub, not a range edit:
      // put back whatever the range was.
      if (!d.moved) setSecs(d.had, false);
      else setSecs(secs(), true);           // one save per gesture, on release
    }
    if (ev && ev.type === "pointercancel") return;
  }

  function onHover(ev) {
    if (!els.hover || drag) return;
    var r = trackRect();
    if (!(r.width > 0) || !(duration() > 0)) return;
    var x = Math.max(0, Math.min(r.width, ev.clientX - r.left));
    els.hover.style.transform = "translateX(" + x.toFixed(1) + "px)";
    els.track.classList.add("hovering");
    if (els.bubble) {
      els.bubble.textContent = fmtClock(clampTime(snapFrame((x / r.width) * duration())));
      // Keep the bubble inside the card at both ends rather than letting the
      // card's own overflow: hidden clip it.
      var shift = Math.max(30, Math.min(r.width - 30, x)) - x;
      els.bubble.style.transform = "translateX(calc(-50% + " + shift.toFixed(1) + "px))";
    }
  }

  /* ---- keyboard on the track --------------------------------------------- */

  function onTrackKey(ev) {
    if (!api) return;
    var f = 1 / fps();
    var many = ev.shiftKey ? 10 : 1;
    switch (ev.key) {
      case "ArrowLeft": case "ArrowDown": api.setTime(time() - f * many); break;
      case "ArrowRight": case "ArrowUp": api.setTime(time() + f * many); break;
      case "Home": api.setTime(0); break;
      case "End": api.setTime(lastFrameTime()); break;
      // A second at a time, the coarse step between a frame and an end.
      case "PageDown": api.setTime(time() - 1); break;
      case "PageUp": api.setTime(time() + 1); break;
      default: return;    // every other key belongs to app.js's own handler
    }
    // Only the keys this handler actually took: a shuttle must survive J or
    // L arriving while the ruler happens to have focus.
    shuttleStop();
    ev.preventDefault();
    ev.stopPropagation();
  }

  /* ---- shuttle (J K L) ---------------------------------------------------
     Reverse playback has no engine behind it: the proxy is a <video> and a
     video cannot play backwards. So shuttle is built on exactly the same
     path a drag is, one seek per animation frame at the chosen rate, which
     is the proxy's own strength (a seek, not a decode). Forward at 1x is the
     real engine instead, because that plays at the clip's own frame rate
     with none of the seeking. */
  function shuttleStop(quiet) {
    if (shuttleState) {
      global.cancelAnimationFrame(shuttleState.raf);
      shuttleState = null;
      if (!quiet && els.status) els.status.textContent = "";
    }
  }

  function shuttleTick(ts) {
    if (!shuttleState) return;
    var dt = Math.min(0.1, (ts - shuttleState.last) / 1000);
    shuttleState.last = ts;
    var t = time() + shuttleState.rate * dt;
    var last = lastFrameTime();
    if (t > last) t = 0;                     // wrap, the same way playback loops
    if (t < 0) t = last;
    api.setTime(t);
    shuttleState.raf = global.requestAnimationFrame(shuttleTick);
  }

  function shuttleAt(rate) {
    if (!api || !(duration() > 0)) return;
    if (api.stopPlayback) api.stopPlayback();
    if (!shuttleState) shuttleState = { rate: rate, raf: 0, last: global.performance.now() };
    else shuttleState.rate = rate;
    if (els.status) els.status.textContent = "shuttle " + (rate > 0 ? "" : "-") + Math.abs(rate) + "x";
    global.cancelAnimationFrame(shuttleState.raf);
    shuttleState.raf = global.requestAnimationFrame(shuttleTick);
  }

  /* L: play, then 2x, then 4x, then 8x. J: the same in reverse. K: stop. */
  function shuttle(dir) {
    if (!api) return;
    if (dir === 0) {
      shuttleStop();
      if (api.stopPlayback) api.stopPlayback();
      return;
    }
    var playing = !!(api.isPlaying && api.isPlaying());
    var rate = shuttleState ? shuttleState.rate : 0;
    if (dir > 0) {
      if (shuttleState && rate > 0) { shuttleAt(Math.min(8, rate * 2)); return; }
      if (playing) { shuttleAt(2); return; }
      shuttleStop();
      if (api.playToggle) api.playToggle();
      return;
    }
    if (shuttleState && rate < 0) { shuttleAt(Math.max(-8, rate * 2)); return; }
    shuttleAt(-1);
  }

  /* ---- wiring ------------------------------------------------------------ */

  function bind() {
    els.track.addEventListener("pointerdown", beginScrub);
    els.track.addEventListener("pointermove", function (ev) {
      if (drag && drag.kind === "scrub") onMove(ev); else onHover(ev);
    });
    els.track.addEventListener("pointerup", endDrag);
    els.track.addEventListener("pointercancel", endDrag);
    els.track.addEventListener("pointerleave", function () {
      if (!drag) els.track.classList.remove("hovering");
    });
    els.track.addEventListener("keydown", onTrackKey);
    // A wheel over the ruler steps frames, the way it does over a clip in
    // every editor. Not passive: this one gesture is ours, and the column
    // behind it would otherwise scroll at the same time.
    els.track.addEventListener("wheel", function (ev) {
      if (!api || !(duration() > 0)) return;
      var d = Math.abs(ev.deltaX) > Math.abs(ev.deltaY) ? ev.deltaX : ev.deltaY;
      if (!d) return;
      ev.preventDefault();
      shuttleStop();
      api.setTime(time() + (d > 0 ? 1 : -1) * (ev.shiftKey ? 10 : 1) / fps());
    }, { passive: false });

    els.range.addEventListener("pointerdown", beginRange);
    els.range.addEventListener("pointermove", function (ev) {
      if (drag && drag.kind === "range") onMove(ev);
    });
    els.range.addEventListener("pointerup", endDrag);
    els.range.addEventListener("pointercancel", endDrag);

    if (els.end) {
      els.end.addEventListener("click", function () {
        shuttleStop();
        api.setTime(lastFrameTime());
      });
    }
    // The playhead's own timecode is a drag source for a frame slot, so the
    // filmstrip staying out of the pointer's way costs contract E4 nothing.
    if (els.clock && api.makeTimeDraggable) {
      els.clock.draggable = true;
      els.clock.addEventListener("dragstart", function (ev) {
        if (!ev.dataTransfer) return;
        ev.dataTransfer.effectAllowed = "copy";
        ev.dataTransfer.setData("application/x-studio-time", String(time()));
        ev.dataTransfer.setData("text/plain", String(time()));
      });
    }

    if (global.ResizeObserver) {
      new global.ResizeObserver(function () {
        W = els.track.clientWidth;
        invalidateRect();
        buildRuler(false);
        if (!headRaf) headRaf = global.requestAnimationFrame(paintHead);
      }).observe(els.track);
    }
    // The card scrolls inside the middle column, so a cached rect goes stale
    // without the element itself changing size.
    global.addEventListener("scroll", invalidateRect, true);
    global.addEventListener("resize", invalidateRect);
  }

  /* Called from app.js's boot(), the same way frames.js and files.js are
     handed their api object: everything this file needs from app.js arrives
     here, and nothing here reaches back into app.js's closure. */
  function init(hooks) {
    api = hooks;
    els = {
      track: $("scrub"), ruler: $("tlRuler"), strip: $("thumbs"), marks: $("marks"),
      range: $("tlRange"), band: $("tlBand"), bandLabel: $("tlBandLabel"), out: $("tlOut"),
      head: $("tlHead"), hover: $("tlHover"), bubble: $("tlBubble"),
      time: $("timeLabel"), frame: $("tlFrame"), dur: $("tlDur"), clock: $("tlClock"),
      end: $("tlEnd"), clear: $("clearMarksBtn"), status: $("playStatus")
    };
    if (!els.track) return;
    W = els.track.clientWidth;
    bind();
    clipChanged();
  }

  /* A new clip, a new duration, a new rotation, or a restored play length:
     everything that is drawn from the clip rather than from the playhead. */
  function clipChanged() {
    if (!api) return;
    paintDuration();
    buildRuler(true);
    buildStrip();
    drawMarks();
    paintRange();
    syncLoopFields();
    timeChanged();
  }

  /* A play length that arrived from somewhere other than the ruler: the
     saved one a clip switch restores. Only the band and the two GPU loop
     fields, deliberately: clipChanged has already rebuilt the strip by the
     time the extras come back, and asking for its 16 thumbnails a second
     time on every clip switch is 16 more decodes queued in front of
     everything else the page is waiting on. */
  function rangeChanged() {
    if (!api) return;
    paintRange();
    syncLoopFields();
  }

  /* The Range button: from the playhead to the next mark after it, or two
     seconds if there is none, and pressing it again clears the range. This
     is the old "Use marks" button's job, plus the clear the old two text
     boxes had no room for. */
  function toggleRange() {
    if (!api) return;
    if (secs() != null) { setSecs(null, true); return; }
    var t = time();
    var after = marks().filter(function (m) { return m > t + 0.01; })
      .sort(function (a, b) { return a - b; });
    var end = after.length ? after[0] : Math.min(duration() || t + 2, t + 2);
    var len = end - t;
    setSecs(len > 1 / fps() ? len : null, true);
  }

  global.StudioTimeline = {
    init: init,
    // Called by app.js whenever the playhead moves, from a scrub, a step, a
    // mark, a clip switch, or any of the three playback engines' per frame
    // callbacks.
    timeChanged: timeChanged,
    clipChanged: clipChanged,
    rangeChanged: rangeChanged,
    marksChanged: drawMarks,
    buildStrip: buildStrip,
    toggleRange: toggleRange,
    shuttle: shuttle,
    shuttleStop: shuttleStop,
    // Read only views, for the harness and for a person in a console. time()
    // is app.js's own S.time unrounded, which is the only way to check "one
    // frame" against a frame that is 0.0417s long.
    time: time,
    secs: secs,
    rangeEnd: rangeEnd,
    timeAtX: timeAtX,
    shuttleRate: function () { return shuttleState ? shuttleState.rate : 0; }
  };
})(window);
