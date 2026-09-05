/* The two and four frame viewer (contract E4).

   Founder, 2026-09-05: "for the preview window I should be able to put 4
   frames. I should be able to drag and drop from the preview timeline into
   the preview preview."

   So: a Frames control in the viewer bar with 1, 2 and 4; each slot holds
   its own timecode, graded with the live config, and a thumbnail dragged
   from the filmstrip (or a mark chip from the timeline) onto a slot sets
   that slot's time. Slot 1 is always the playhead, so the scopes, the
   statistics, the power window and everything else that reads "the frame"
   still read the frame the rest of the app is about.

   Why a grid beside #stage rather than a rebuild of #stage: the stage
   carries five before/after view modes, the wipe with its clip-path and
   handle, and the window overlay, and a slot shares none of that. The
   contact sheet already established the pattern of a second box that
   #viewport swaps in (see #viewport.sheet in style.css), and following it
   means no view mode, no wipe and no overlay had to learn about slots.

   Rendering: each slot goes through the GPU when the viewer is on the GPU
   path, exactly like the single viewer, and falls back per slot to a server
   /api/frame render on any rejection. live.js presents to one shared
   canvas, so a slot's picture is copied off it with drawImage the same way
   app.js's renderGpuBypass copies the bypassed frame off it. The slots are
   rendered one after another rather than all at once: four parallel server
   renders would queue behind each other in the server's ffmpeg semaphore
   anyway, and doing them in order means the first slot (the playhead, the
   one being judged) is on screen first.
*/
(function (global) {
  "use strict";

  var COUNTS = [1, 2, 4];
  var MAX_SLOTS = 4;
  var EXTRA = "frame_slots";      /* the project extra these times live in */
  var PERSIST_MS = 250;           /* one write per burst of changes, see persist() */

  var api = null;
  var count = 1;
  var times = [0, 0, 0, 0];
  var slots = [];                 /* {root, img, canvas, tag, btn} */
  var gen = 0;                    /* cancels a render run the state moved past */
  var clip = null;
  var restoring = false;          /* reading the project must not write it back */

  function byId(id) { return document.getElementById(id); }
  function num(v, d) { v = Number(v); return isFinite(v) ? v : d; }

  /* ---- the slots --------------------------------------------------------- */

  function build() {
    var host = byId("framesGrid");
    if (!host || slots.length) return;
    for (var i = 0; i < MAX_SLOTS; i++) slots.push(makeSlot(host, i));
  }

  function makeSlot(host, i) {
    var root = document.createElement("div");
    root.className = "fslot";
    root.dataset.slot = String(i);

    var img = document.createElement("img");
    img.className = "fimg";
    img.alt = "frame " + (i + 1);
    img.draggable = false;
    var canvas = document.createElement("canvas");
    canvas.className = "fcanvas";

    var tag = document.createElement("b");
    tag.className = "ftime";

    /* Per slot, not one shared control: "make THIS one the playhead frame"
       is the move, and a slot is where the answer to "which one" already
       is. Slot 1 has none, because slot 1 IS the playhead and a button that
       does nothing would only raise the question. */
    var btn = null;
    if (i > 0) {
      btn = document.createElement("button");
      btn.className = "btn fset";
      btn.type = "button";
      btn.textContent = "playhead";
      btn.title = "Set this frame to the playhead's time";
      btn.addEventListener("click", function (ev) {
        ev.preventDefault();
        ev.stopPropagation();
        setSlotTime(i, api && api.getTime ? api.getTime() : 0, true);
      });
    }

    root.appendChild(img);
    root.appendChild(canvas);
    root.appendChild(tag);
    if (btn) root.appendChild(btn);

    /* Clicking a slot moves the playhead there, the same thing clicking a
       filmstrip thumbnail or a mark chip does. Slot 1 is already the
       playhead, so it is the one slot where that would be a no-op. */
    root.addEventListener("click", function () {
      if (i === 0 || !api || !api.setTime) return;
      api.setTime(times[i]);
    });

    bindDrop(root, i);
    host.appendChild(root);
    return { root: root, img: img, canvas: canvas, tag: tag, btn: btn };
  }

  /* The drop half of the founder's "drag and drop from the preview timeline
     into the preview preview". The drag half is in app.js, on the filmstrip
     thumbnails and the mark chips, and it carries the time in a MIME type of
     our own so a file dragged in from the desktop (contract E2's upload)
     cannot be mistaken for a timecode. text/plain carries the same number,
     for anything that can only read that. */
  function bindDrop(root, i) {
    root.addEventListener("dragover", function (ev) {
      if (!carriesTime(ev)) return;
      ev.preventDefault();
      ev.dataTransfer.dropEffect = "copy";
      root.classList.add("drop");
    });
    root.addEventListener("dragleave", function () { root.classList.remove("drop"); });
    root.addEventListener("drop", function (ev) {
      var t = readTime(ev);
      root.classList.remove("drop");
      if (t === null) return;
      ev.preventDefault();
      /* A drop on slot 1 moves the playhead, because slot 1 is the
         playhead: setting it to anything else would be a lie the next
         scrub would silently correct. */
      if (i === 0) { if (api && api.setTime) api.setTime(t); return; }
      setSlotTime(i, t, true);
    });
  }

  var TIME_TYPE = "application/x-studio-time";

  /* dragover is not allowed to read a drag's data, only its type list, so
     "will this drop mean anything" and "what does it say" are two different
     questions and this is the first one. */
  function carriesTime(ev) {
    var dt = ev.dataTransfer;
    if (!dt) return false;
    var types = dt.types ? Array.prototype.slice.call(dt.types) : [];
    return types.indexOf(TIME_TYPE) >= 0;
  }

  function readTime(ev) {
    var dt = ev.dataTransfer;
    if (!dt) return null;
    var raw = "";
    try { raw = dt.getData(TIME_TYPE); } catch (e) { raw = ""; }
    if (!raw) {
      try { raw = dt.getData("text/plain"); } catch (e2) { raw = ""; }
    }
    var t = Number(raw);
    return (raw !== "" && isFinite(t) && t >= 0) ? t : null;
  }

  /* ---- state ------------------------------------------------------------- */

  function clampTime(t) {
    var d = api && api.getDuration ? api.getDuration() : 0;
    if (!isFinite(t) || t < 0) t = 0;
    if (d > 0) t = Math.min(t, Math.max(0, d - 0.01));
    return Math.round(t * 1000) / 1000;
  }

  function setSlotTime(i, t, save) {
    if (i <= 0 || i >= MAX_SLOTS) return;
    times[i] = clampTime(t);
    paintTags();
    renderSlot(i, ++gen);
    if (save) persist();
  }

  function setCount(n) {
    n = COUNTS.indexOf(Number(n)) >= 0 ? Number(n) : 1;
    var was = count;
    count = n;
    var sel = byId("frameCount");
    if (sel && Number(sel.value) !== n) sel.value = String(n);
    var host = byId("framesGrid");
    if (host) {
      host.classList.remove("n1", "n2", "n4");
      host.classList.add("n" + n);
    }
    slots.forEach(function (s, i) { s.root.classList.toggle("off", i >= n); });
    slots.forEach(function (s, i) { s.root.classList.toggle("head", i === 0); });
    if (api && api.onCountChange) api.onCountChange(n, was);
    paintTags();
    if (n > 1) render();
    if (was !== n && !restoring) persist();
  }

  /* Every slot after the first starts somewhere sensible rather than on the
     playhead four times over: a viewer showing the same frame four times
     answers no question. Quarters of the clip is the same spacing the
     contact sheet's own default times use. */
  function seedTimes() {
    var d = api && api.getDuration ? api.getDuration() : 0;
    var head = api && api.getTime ? api.getTime() : 0;
    times[0] = clampTime(head);
    for (var i = 1; i < MAX_SLOTS; i++) {
      if (times[i] > 0) continue;
      times[i] = clampTime(d ? (d * [0, 0.25, 0.5, 0.75][i]) : head);
    }
  }

  /* "head" rather than "playhead" in the badge: a slot is a quarter of the
     viewer at Frames 4, and the longer word wrapped onto a second line over
     the picture. The whole word is in the slot's tooltip. */
  function paintTags() {
    slots.forEach(function (s, i) {
      s.tag.textContent = (i + 1) + ": " + times[i].toFixed(2) + "s"
        + (i === 0 ? " head" : "");
      s.root.title = i === 0
        ? "Slot 1 is the playhead. The scopes and the statistics read this one"
        : "Drag a filmstrip thumbnail or a mark onto this slot to set its time";
    });
  }

  function playheadMoved(t) {
    times[0] = clampTime(t);
    paintTags();
  }

  /* ---- rendering --------------------------------------------------------- */

  function active() { return count > 1; }

  function render() {
    if (!active() || !api) return Promise.resolve();
    if (!api.getClip || !api.getClip()) return Promise.resolve();
    times[0] = clampTime(api.getTime ? api.getTime() : 0);
    paintTags();
    var mine = ++gen;
    var chain = Promise.resolve();
    for (var i = 0; i < count; i++) {
      chain = chain.then(makeStep(i, mine));
    }
    return chain.then(function () {
      /* The scopes and the statistics read slot 1, which is the playhead,
         which is what every other reader of "the frame" in this app already
         means. Told once, after the slots are on screen, so four renders
         cost one measurement rather than four. */
      if (gen === mine && api.afterRender) api.afterRender();
    });
  }

  function makeStep(i, mine) {
    return function () {
      if (gen !== mine) return null;
      return renderSlot(i, mine);
    };
  }

  function renderSlot(i, mine) {
    if (!active() || i >= count || !api) return Promise.resolve();
    var slot = slots[i];
    var t = times[i];
    if (api.useGpu && api.useGpu()) {
      return api.renderStill(t).then(function () {
        if (gen !== mine) return;
        var src = api.gpuCanvas();
        if (!src || !src.width) throw new Error("the GPU produced no picture");
        if (slot.canvas.width !== src.width || slot.canvas.height !== src.height) {
          slot.canvas.width = src.width;
          slot.canvas.height = src.height;
        }
        slot.canvas.getContext("2d").drawImage(src, 0, 0);
        slot.root.classList.add("gpu");
        slot.root.classList.remove("failed");
        if (api.onLayout) api.onLayout();
      }).catch(function () {
        if (gen !== mine) return;
        return serverSlot(i, mine);
      });
    }
    return serverSlot(i, mine);
  }

  function serverSlot(i, mine) {
    var slot = slots[i];
    return api.serverFrame(i, times[i], slot.img).then(function () {
      if (gen !== mine) return;
      slot.root.classList.remove("gpu", "failed");
      if (api.onLayout) api.onLayout();
    }).catch(function () {
      if (gen !== mine) return;
      slot.root.classList.add("failed");
    });
  }

  /* ---- the project extra -------------------------------------------------- */

  /* One write per burst, not one per change. Two drops in the same moment
     (the spec does exactly that, and so does a person dropping two
     thumbnails quickly) were otherwise two POSTs in flight at once, and the
     extra is a read, modify, write on the server: whichever landed last won,
     which was not always the one sent last. That lost the second slot's time
     across a reload. The pending write is also dropped if the clip changed
     under it, so the old clip's times can never be saved onto the new one. */
  var persistTimer = null;
  var persistFor = null;

  function persist() {
    if (!api || !api.saveExtra || !clip) return;
    persistFor = clip;
    if (persistTimer) clearTimeout(persistTimer);
    persistTimer = setTimeout(function () {
      persistTimer = null;
      if (!api || !api.saveExtra || !clip || clip !== persistFor) return;
      api.saveExtra(EXTRA, { count: count, times: times.slice() });
    }, PERSIST_MS);
  }

  /* Called on a clip switch and at boot. Slot 1 is deliberately NOT restored
     from here: it is the playhead, and the playhead is already the project's
     own (contract C4 of the projects arc), so reading it twice from two
     places is how the two would come to disagree. */
  function restore(value) {
    var v = (value && typeof value === "object") ? value : {};
    var list = Array.isArray(v.times) ? v.times : [];
    restoring = true;
    try {
      for (var i = 1; i < MAX_SLOTS; i++) {
        times[i] = list[i] === undefined ? 0 : clampTime(num(list[i], 0));
      }
      seedTimes();
      setCount(COUNTS.indexOf(Number(v.count)) >= 0 ? Number(v.count) : count);
    } finally {
      restoring = false;
    }
    paintTags();
    if (active()) render();
  }

  function clipChanged(name, extras) {
    clip = name || null;
    times = [0, 0, 0, 0];
    restore(extras && extras[EXTRA]);
  }

  /* ---- wiring ------------------------------------------------------------- */

  function init(opts) {
    api = opts || null;
    build();
    var sel = byId("frameCount");
    if (sel) {
      sel.addEventListener("change", function () { setCount(sel.value); });
    }
    seedTimes();
    paintTags();
  }

  global.Frames = {
    init: init,
    active: active,
    count: function () { return count; },
    setCount: setCount,
    times: function () { return times.slice(0, count); },
    slotTime: function (i) { return times[i]; },
    setSlotTime: function (i, t) { setSlotTime(i, t, true); },
    playheadMoved: playheadMoved,
    render: render,
    clipChanged: clipChanged,
    /* Each visible slot's picture elements, so app.js's fitViewer can size
       them the same way it sizes the single viewer's. */
    pictures: function () {
      var out = [];
      for (var i = 0; i < count; i++) out.push(slots[i].img, slots[i].canvas);
      return out;
    },
    cells: function () {
      return { cols: count === 1 ? 1 : 2, rows: count === 4 ? 2 : 1 };
    }
  };
})(window);
