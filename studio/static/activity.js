/* The library activity feed (contract E2), at the foot of the History tab.
 *
 * The founder's ask this belongs to: "basically google drive like share so then
 * when people make updates it shows up in history etc." Two different kinds of
 * update turn out to be hiding in that sentence, and they live in two different
 * places on purpose:
 *
 *   a GRADE changed   that is a commit, it is in the graph above this panel,
 *                     with its author, and it has been since contract C3.
 *   a FILE changed    uploaded, folder made, renamed, moved, trashed, restored,
 *                     shared, unshared. That is this feed.
 *
 * Writing grade commits into this list as well would make it a worse copy of the
 * panel three centimetres above it, so library.py deliberately does not record
 * them and this file deliberately does not ask for them.
 *
 * Why this is not in history.js: that file owns the commit graph and is being
 * worked on by another lane. This panel shares exactly one thing with it, the
 * `studio:session` event session.js dispatches, and shares no state at all, so
 * neither can break the other. It also listens for `studio:library`, which
 * files.js dispatches after every write it makes, so an upload shows up here
 * without waiting for a session revision that may never come.
 */
(function (global) {
  "use strict";

  var LIMIT = 40;
  var REFRESH_MS = 900;          // the shortest gap between two fetches
  var lastFetch = 0;
  var timer = null;
  var mounted = false;

  function byId(id) { return document.getElementById(id); }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) { n.className = cls; }
    if (text !== undefined && text !== null) { n.textContent = text; }
    return n;
  }

  /* Same stable per name hue history.js gives an author's avatar, computed the
     same way (a small string hash into eight hues), so one person is the same
     colour in the graph above and in this list below without either file
     importing anything from the other. */
  function hueOf(name) {
    var s = String(name || "");
    var h = 0;
    for (var i = 0; i < s.length; i++) { h = (h * 31 + s.charCodeAt(i)) % 360; }
    return h;
  }

  function initials(name) {
    var parts = String(name || "?").trim().split(/[\s._-]+/).filter(Boolean);
    if (!parts.length) { return "?"; }
    if (parts.length === 1) { return parts[0].slice(0, 2).toUpperCase(); }
    return (parts[0][0] + parts[1][0]).toUpperCase();
  }

  function ago(ts) {
    var secs = Math.max(0, (Date.now() / 1000) - (Number(ts) || 0));
    if (secs < 90) { return "now"; }
    if (secs < 5400) { return Math.round(secs / 60) + "m"; }
    if (secs < 129600) { return Math.round(secs / 3600) + "h"; }
    return Math.round(secs / 86400) + "d";
  }

  /* One sentence per event, in the past tense, naming the thing rather than the
     route. `detail` carries whatever the event had that a sentence needs (who a
     share went to, where a move landed), and anything missing degrades to the
     shorter sentence rather than to "undefined". */
  function sentence(row) {
    var d = row.detail || {};
    var name = row.name || row.path || "something";
    switch (row.kind) {
      case "upload": return "uploaded " + name;
      case "mkdir": return "made the folder " + name;
      case "rename": return "renamed " + (d.was || d.from || "something") + " to " + name;
      case "move": return "moved " + name + " into " + (d.to || "the top of the library");
      case "trash": return "moved " + name + " to the trash";
      case "restore": return "put " + name + " back";
      case "share": return "shared " + name + " with " + (d.target || "somebody")
        + " as " + (d.role || "viewer");
      case "unshare": return "took " + (d.target || "somebody") + " off " + name;
      default: return row.kind + " " + name;
    }
  }

  function render(rows) {
    var host = byId("activityList");
    if (!host) { return; }
    host.innerHTML = "";
    if (!rows.length) {
      host.appendChild(el("div", "note", "Nothing has happened to the files yet. "
        + "Uploads, folders, renames, moves and shares show up here."));
      return;
    }
    rows.forEach(function (row) {
      var node = el("div", "activityrow");
      var av = el("div", "havatar", initials(row.actor));
      av.style.setProperty("--hue", String(hueOf(row.actor)));
      av.title = row.actor || "";
      node.appendChild(av);
      var text = el("div", "atext");
      var line = el("div", "aname");
      line.appendChild(el("span", "aactor", row.actor || "somebody"));
      line.appendChild(document.createTextNode(" " + sentence(row)));
      text.appendChild(line);
      if (!row.mine && row.owner) {
        text.appendChild(el("div", "fsub", "in " + row.owner + "'s files"));
      }
      node.appendChild(text);
      node.appendChild(el("span", "awhen", ago(row.ts)));
      node.title = (row.path || "") + " " + new Date((row.ts || 0) * 1000).toLocaleString();
      host.appendChild(node);
    });
  }

  function fetchFeed() {
    lastFetch = Date.now();
    return fetch("/api/activity?limit=" + LIMIT)
      .then(function (r) { return r.ok ? r.json() : { activity: [] }; })
      .then(function (j) { render((j && j.activity) || []); })
      .catch(function () { /* a server that is not there is app.js's message to give */ });
  }

  /* Coalesced: a burst of five uploads dispatches five studio:library events
     and a slider drag dispatches a studio:session event per revision, and this
     panel is worth exactly one fetch for all of them. */
  function schedule() {
    if (timer) { return; }
    var wait = Math.max(0, REFRESH_MS - (Date.now() - lastFetch));
    timer = global.setTimeout(function () {
      timer = null;
      fetchFeed();
    }, wait);
  }

  function mount() {
    if (mounted) { return; }
    if (!byId("activityList")) { return; }
    mounted = true;
    global.addEventListener("studio:session", schedule);
    global.addEventListener("studio:library", schedule);
    fetchFeed();
  }

  global.StudioActivity = { refresh: fetchFeed, schedule: schedule };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", mount);
  } else {
    mount();
  }
}(window));
