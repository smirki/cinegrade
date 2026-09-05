/* Per clip grades: the client half of contract C3.
 *
 * The studio used to hold one grade for the whole session. Selecting a
 * different clip kept whatever look was on screen, so a night interior grade
 * followed you onto a beach shot silently. Now every clip owns its grade, every
 * account owns its own copy, and switching clips loads the right one.
 *
 * Split out of app.js the same way session.js is, and for the same reason: the
 * coupling is a small written contract instead of a tangle. app.js provides
 *
 *   window.applyClipGrade(config, key, prevKey)   load a config into the
 *                                                 active slot, swap the undo
 *                                                 stack, refresh the panels.
 *                                                 A null config means "the
 *                                                 engine defaults", so this
 *                                                 file never carries its own
 *                                                 copy of them.
 *
 * and calls into here at exactly three points:
 *
 *   StudioGrades.clipChanged(name)     from selectClip
 *   StudioGrades.commit(name, config)  from pushHistory and loadPreset
 *   StudioGrades.ifNoSavedGrade(fn)    from boot, so the "start on flat"
 *                                      default does not stamp over a grade
 *                                      that was just loaded from the server
 *
 * If app.js defines none of that, this file mounts its indicator and does
 * nothing else, and nothing breaks.
 *
 * The saving rule: a save is a PUT of the WHOLE config, debounced 600 ms after
 * the last committed change. Committed means a slider release or a checkbox,
 * not every pixel of a drag (app.js already draws that line for the undo
 * stack, and this reuses it), so a drag costs one request, not two hundred.
 */
(function (global) {
  "use strict";

  var DEBOUNCE_MS = 600;

  var current = { clip: null, key: null };
  // Held up while a fetched grade is being pushed into the page. Without it a
  // load could look like an edit and be saved straight back, which at best is
  // a pointless request and at worst overwrites a newer grade with an older
  // one during a slow response.
  //
  // A COUNTER, not a boolean, and that is not fussiness: two holds overlap at
  // boot (the first clip's grade being applied, and the starting preset being
  // loaded on top of it when there is none). With a boolean the inner hold's
  // release cleared the outer one's too, and the boot preset was saved as if
  // the user had graded the clip. Measured before the fix: one PUT per boot.
  var holds = 0;
  var timer = null;
  var pending = null;
  var initial = null;          // the first clip's load, awaited by boot
  var lastSavedJSON = null;    // what the server is believed to hold
  var knownKeys = {};          // clip keys that have a saved grade
  var mounted = false;
  var els = {};

  function hold() { holds += 1; }
  // Released a turn later, for the same reason session.js clears its own flag
  // late: app.js's handlers can be queued rather than synchronous, and
  // releasing too early lets a load echo back out as a save.
  function releaseHold() {
    setTimeout(function () { holds = holds > 0 ? holds - 1 : 0; }, 0);
  }
  function held() { return holds > 0; }

  function json(res) {
    if (!res.ok) {
      return res.json().catch(function () { return {}; }).then(function (body) {
        throw new Error(body.error || ("HTTP " + res.status));
      });
    }
    return res.json();
  }

  /* ---- the saved indicator --------------------------------------------- */

  // Four states, and the wording is deliberately literal. "unsaved" means the
  // debounce timer is still running, "saving" means a PUT is in flight,
  // "saved" means the server acknowledged it, "error" means it did not and
  // the grade on screen is NOT on the server. A single silent dot would hide
  // the one case that matters.
  var STATES = {
    saved:   { text: "saved",   colour: "var(--text-dim)",
               title: "This clip's grade is saved to your account." },
    saving:  { text: "saving",  colour: "var(--accent)",
               title: "Writing this clip's grade to your account." },
    unsaved: { text: "unsaved", colour: "var(--warn)",
               title: "Edited. It saves automatically a moment after you stop." },
    error:   { text: "not saved", colour: "var(--danger)",
               title: "The last save failed, so this grade is only in this tab." },
    idle:    { text: "", colour: "var(--text-dim)", title: "" }
  };

  function setState(name, detail) {
    var s = STATES[name] || STATES.idle;
    if (!els.dot) { return; }
    els.dot.textContent = s.text;
    els.dot.style.color = s.colour;
    els.dot.style.borderColor = s.colour;
    els.dot.style.visibility = s.text ? "visible" : "hidden";
    els.dot.title = s.title + (detail ? "  " + detail : "");
  }

  function mount() {
    if (mounted) { return; }
    var head = document.querySelector(".browsebottom .panelhead");
    var list = document.getElementById("browseClips");
    if (!head || !list) { return; }          // not this page (login, parity)
    mounted = true;

    var dot = document.createElement("span");
    dot.id = "gradeSaveState";
    dot.className = "dot on";
    els.dot = dot;
    var spacer = head.querySelector(".spacer");
    if (spacer) { head.insertBefore(dot, spacer); } else { head.appendChild(dot); }

    var label = document.createElement("div");
    label.className = "panelhead";
    label.textContent = "Grade";

    var row = document.createElement("div");
    row.className = "row wrap";
    row.id = "gradeCopyRow";

    var sel = document.createElement("select");
    sel.id = "gradeCopyFrom";
    sel.title = "Clips you have already graded";
    var btn = document.createElement("button");
    btn.id = "gradeCopyBtn";
    btn.className = "btn";
    btn.type = "button";
    btn.textContent = "Copy grade here";
    btn.title = "Replace this clip's grade with the grade saved on the clip "
      + "chosen on the left";
    row.appendChild(sel);
    row.appendChild(btn);

    list.parentNode.insertBefore(label, list.nextSibling);
    label.parentNode.insertBefore(row, label.nextSibling);
    els.select = sel;
    els.button = btn;
    btn.addEventListener("click", copyFromSelected);
    setState("idle");
    refreshCopyList();
  }

  /* ---- the copy picker -------------------------------------------------- */

  function refreshCopyList() {
    if (!els.select) { return Promise.resolve(); }
    return fetch("/api/grades").then(json).then(function (body) {
      var rows = (body && body.grades) || [];
      knownKeys = {};
      rows.forEach(function (r) { knownKeys[r.clip_key] = true; });
      var keep = els.select.value;
      els.select.innerHTML = "";
      var others = rows.filter(function (r) { return r.clip_key !== current.key; });
      if (!others.length) {
        var none = document.createElement("option");
        none.value = "";
        none.textContent = "no other graded clips yet";
        els.select.appendChild(none);
        els.select.disabled = true;
        els.button.disabled = true;
        return;
      }
      others.forEach(function (r) {
        var o = document.createElement("option");
        o.value = r.clip_key;
        o.textContent = r.clip_name || r.clip_key;
        els.select.appendChild(o);
      });
      els.select.disabled = false;
      els.button.disabled = false;
      if (keep && others.some(function (r) { return r.clip_key === keep; })) {
        els.select.value = keep;
      }
    }).catch(function () { /* a picker that cannot fill is not worth a toast */ });
  }

  function copyFromSelected() {
    if (!els.select || !els.select.value || !current.clip) { return; }
    var from = els.select.value;
    setState("saving");
    fetch("/api/grade/copy", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ from: from, to: current.clip })
    }).then(json).then(function (body) {
      apply(body.config, body.key, current.key);
      current.key = body.key;
      lastSavedJSON = JSON.stringify(body.config);
      knownKeys[body.key] = true;
      setState("saved");
      refreshCopyList();
      if (typeof global.studioToast === "function") {
        global.studioToast("copied the grade onto " + current.clip);
      }
    }).catch(function (err) {
      setState("error", err.message || "");
    });
  }

  /* ---- loading a clip's grade ------------------------------------------- */

  function apply(config, key, prevKey) {
    if (typeof global.applyClipGrade !== "function") { return; }
    hold();
    try {
      global.applyClipGrade(config, key, prevKey);
    } finally {
      releaseHold();
    }
  }

  function clipChanged(name) {
    mount();
    if (!name) { return Promise.resolve({ exists: false }); }
    // A pending autosave belongs to the clip that is on its way out, so it is
    // flushed now rather than dropped: leaving a clip must not lose the edit
    // that was still inside the debounce window.
    flush();
    var prevKey = current.key;
    current.clip = name;
    setState("saving");
    var p = fetch("/api/grade?clip=" + encodeURIComponent(name))
      .then(json)
      .then(function (body) {
        current.key = body.key || null;
        if (body.exists && body.config) {
          apply(body.config, body.key, prevKey);
          lastSavedJSON = JSON.stringify(body.config);
          setState("saved");
        } else {
          // No grade for this clip: the engine defaults, not whatever the
          // previous clip happened to be showing. That is the whole point.
          // A null config is the agreed way to ask for them, so this file
          // does not need its own copy of what the defaults are.
          apply(null, body.key, prevKey);
          lastSavedJSON = null;
          setState("idle");
        }
        refreshCopyList();
        return { exists: !!body.exists, key: body.key };
      })
      .catch(function (err) {
        setState("error", err.message || "");
        return { exists: false, key: null };
      });
    if (initial === null) { initial = p; }
    return p;
  }

  /* ---- saving ----------------------------------------------------------- */

  function put(clip, config) {
    var body = JSON.stringify(config);
    setState("saving");
    return fetch("/api/grade", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ clip: clip, config: config })
    }).then(json).then(function (res) {
      // Only trust the acknowledgement for the clip that is still open. A save
      // that lands after the user moved on must not relabel the new clip.
      if (current.clip === clip) {
        current.key = res.key || current.key;
        lastSavedJSON = body;
        setState("saved");
      }
      if (res.key && !knownKeys[res.key]) {
        knownKeys[res.key] = true;
        refreshCopyList();
      }
    }).catch(function (err) {
      if (current.clip === clip) { setState("error", err.message || ""); }
    });
  }

  function commit(clip, config) {
    mount();
    if (held() || !clip || !config) { return; }
    var body = JSON.stringify(config);
    if (body === lastSavedJSON) { return; }   // nothing actually moved
    pending = { clip: clip, config: JSON.parse(body) };
    setState("unsaved");
    if (timer) { clearTimeout(timer); }
    timer = setTimeout(function () {
      timer = null;
      var job = pending;
      pending = null;
      if (job) { put(job.clip, job.config); }
    }, DEBOUNCE_MS);
  }

  function flush() {
    if (timer) { clearTimeout(timer); timer = null; }
    var job = pending;
    pending = null;
    if (job) { return put(job.clip, job.config); }
    return Promise.resolve();
  }

  /* ---- boot ordering ---------------------------------------------------- */

  // boot() in app.js loads the "flat" preset as a starting point. That is the
  // right default for a clip nobody has graded and exactly the wrong thing for
  // one that has a saved grade, so the default only runs once the first
  // clip's grade has come back and said there is none.
  //
  // It also runs with a hold in place, so the starting preset is NOT saved:
  // opening the studio is not the user grading anything, and a boot that wrote
  // a row would fill the copy-from picker with clips nobody has touched and
  // make "this clip has no grade yet" a state that never occurs again.
  function ifNoSavedGrade(fn) {
    function guarded() {
      hold();
      var out;
      try {
        out = fn();
      } finally {
        if (out && typeof out.then === "function") {
          out.then(releaseHold, releaseHold);
        } else {
          releaseHold();
        }
      }
    }
    if (initial === null) { guarded(); return; }
    initial.then(function (r) { if (!r || !r.exists) { guarded(); } })
           .catch(function () { guarded(); });
  }

  global.StudioGrades = {
    clipChanged: clipChanged,
    commit: commit,
    flush: flush,
    ifNoSavedGrade: ifNoSavedGrade,
    refreshCopyList: refreshCopyList,
    currentKey: function () { return current.key; },
    // Exposed for the UI test harness: the debounce means "did it save" is a
    // question about a timer, and a spec should be able to ask rather than
    // sleep and hope.
    isPending: function () { return !!(timer || pending); }
  };

  // A last chance to write: closing the tab inside the debounce window would
  // otherwise lose the edit. keepalive lets the request outlive the page.
  global.addEventListener("beforeunload", function () {
    if (!pending) { return; }
    var job = pending;
    pending = null;
    if (timer) { clearTimeout(timer); timer = null; }
    try {
      fetch("/api/grade", {
        method: "PUT", keepalive: true,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ clip: job.clip, config: job.config })
      });
    } catch (e) { /* the page is going away; nothing to report to */ }
  });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", mount);
  } else {
    mount();
  }
}(window));
