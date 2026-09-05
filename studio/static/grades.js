/* Per clip grades: what is left of contract C3 after contract C4 took the
 * saving over.
 *
 * The history: the studio used to hold one grade for the whole session, so a
 * night interior grade followed you onto a beach shot silently. Contract C3
 * gave every clip its own grade, saved with a debounced PUT /api/grade, and
 * this file was the client half of it.
 *
 * Contract C4 replaced that. A clip's grade is now its project's HEAD commit
 * (studio/projects.py): opening a clip is POST /api/project/open and a
 * committed edit is POST /api/session, both of which the server records as
 * commits. So this file no longer fetches, applies or saves a grade. The old
 * `grades` table is still there and is still written by PUT /api/grade (the
 * CLI, a restore from backup), which is why the picker below reads it as well
 * as the projects. Two things are left, and both are real:
 *
 *   the copy-from picker  "take the grade off that clip and put it on this
 *                          one", which is a genuine action with no other home
 *   #gradeSaveState       the element in the Clips panelhead. app.js writes it
 *                          (it shows the short id of the commit on screen);
 *                          this file only puts it there.
 *
 * app.js provides window.applyClipGrade(config) to put a copied grade into the
 * active slot, and calls StudioGrades.clipChanged(name, key) when the clip
 * changes so the picker knows which clip it must not offer. commit() and
 * isPending() survive as no-ops so older call sites and the test harness keep
 * working without pretending a save is in flight.
 */
(function (global) {
  "use strict";

  var current = { clip: null, key: null };
  var knownKeys = {};          // clip keys that have a saved grade
  var mounted = false;
  var els = {};

  function json(res) {
    if (!res.ok) {
      return res.json().catch(function () { return {}; }).then(function (body) {
        throw new Error(body.error || ("HTTP " + res.status));
      });
    }
    return res.json();
  }

  /* ---- the commit indicator --------------------------------------------- */

  // Created here, written by app.js (updateHeadLabel). It used to say saved /
  // saving / unsaved / not saved for the debounced autosave; with the commit
  // as the save there is no in flight window to report, so what it shows now
  // is which commit the picture on screen is.
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
    dot.style.visibility = "hidden";
    refreshCopyList();
  }

  /* ---- the copy picker -------------------------------------------------- */

  /* Which clips have a grade to copy FROM.
   *
   * Two sources, merged, because the answer moved house halfway through this
   * arc. GET /api/grades lists the old per account `grades` table, which is
   * still the right answer for anything graded before contract C4 or saved
   * from the CLI. But a clip graded in the studio today has a PROJECT and no
   * row in that table (a commit does not write one), so asking only the old
   * table would show "no other graded clips yet" the day after this shipped.
   * The second source asks each listed clip whether a project exists for it.
   *
   * Capped at PROBE_MAX clips: this is a dropdown, the browser can be sitting
   * in a folder of hundreds, and one small JSON GET each is fine for a few
   * dozen and rude for a few hundred. The clip probe behind it is cached
   * server side, so this is cheap after the first clip listing. */
  var PROBE_MAX = 24;

  function projectGrades(known) {
    return fetch("/api/clips").then(json).then(function (body) {
      var clips = (body && body.clips) || [];
      var todo = clips.filter(function (c) {
        return c && c.name && c.key && !known[c.key] && c.key !== current.key;
      }).slice(0, PROBE_MAX);
      return Promise.all(todo.map(function (c) {
        return fetch("/api/project?clip=" + encodeURIComponent(c.name))
          .then(json)
          .then(function (p) {
            return (p && p.open && p.key)
              ? { clip_key: p.key, clip_name: p.name || c.name }
              : null;
          })
          .catch(function () { return null; });
      }));
    }).then(function (found) {
      return found.filter(Boolean);
    }).catch(function () { return []; });
  }

  function refreshCopyList() {
    if (!els.select) { return Promise.resolve(); }
    return fetch("/api/grades").then(json).then(function (body) {
      var saved = (body && body.grades) || [];
      var known = {};
      saved.forEach(function (r) { known[r.clip_key] = true; });
      return projectGrades(known).then(function (extra) {
        extra.forEach(function (r) { known[r.clip_key] = true; });
        return saved.concat(extra);
      });
    }).then(function (rows) {
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

  /* Read the source clip's grade and put it on this one.
   *
   * GET /api/grade?clip=<key> rather than POST /api/grade/copy: that route
   * reads the source out of the old `grades` table and errors with "that clip
   * has no saved grade to copy" for anything graded since contract C4, which
   * is now most things. The plain GET answers with the project's HEAD when
   * there is a project and falls back to the old row when there is not, which
   * is exactly the "whatever that clip's grade currently is" this button
   * means. app.js then puts it on screen AND publishes it, and that publish
   * is what makes the copy a commit on THIS clip rather than a change with no
   * history behind it. */
  function copyFromSelected() {
    if (!els.select || !els.select.value || !current.clip) { return; }
    var from = els.select.value;
    els.button.disabled = true;
    fetch("/api/grade?clip=" + encodeURIComponent(from)).then(json).then(function (body) {
      if (!body || !body.exists || !body.config) {
        throw new Error("that clip has no grade to copy");
      }
      if (typeof global.applyClipGrade === "function") {
        global.applyClipGrade(body.config, body.key, current.key);
      }
      knownKeys[current.key] = true;
      refreshCopyList();
      if (typeof global.studioToast === "function") {
        global.studioToast("copied the grade onto " + current.clip);
      }
    }).catch(function (err) {
      if (typeof global.studioToast === "function") {
        global.studioToast("could not copy that grade: " + (err.message || err), true);
      }
    }).then(function () { els.button.disabled = false; });
  }

  /* ---- which clip is open ----------------------------------------------- */

  /* Called from app.js's showClip. It used to fetch this clip's saved grade
   * and push it into the page; that is POST /api/project/open's job now, and
   * doing it here as well is exactly how a clip switch could republish a
   * stale config on top of an agent's edit. All that is left is bookkeeping
   * for the picker: which clip we are on, so it is not offered as a source
   * for itself, and a refresh of the list.
   *
   * The key comes from the caller (/api/state carries it per clip) rather
   * than from a round trip of this file's own. */
  function clipChanged(name, key) {
    mount();
    if (!name) { return Promise.resolve({ key: null }); }
    current.clip = name;
    current.key = key || null;
    return refreshCopyList().then(function () { return { key: current.key }; });
  }

  /* ---- retired, kept callable ------------------------------------------- */

  // The debounced PUT /api/grade autosave (contract C3) is gone: a committed
  // edit is a commit on the project now (contract C4), and the server writes
  // the grades row itself as part of it. These three keep their names so
  // call sites elsewhere, and the test harness, do not have to care.
  function commit() { /* the commit is the save */ }
  function flush() { return Promise.resolve(); }
  function ifNoSavedGrade(fn) { if (typeof fn === "function") { fn(); } }

  global.StudioGrades = {
    clipChanged: clipChanged,
    commit: commit,
    flush: flush,
    ifNoSavedGrade: ifNoSavedGrade,
    refreshCopyList: refreshCopyList,
    currentKey: function () { return current.key; },
    // Always false now. It reported whether the autosave debounce was still
    // running; there is no debounce left, so there is never a save in flight
    // for a spec to wait on.
    isPending: function () { return false; }
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", mount);
  } else {
    mount();
  }
}(window));
