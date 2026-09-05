// History panel (contract C3, studio projects arc): the git style tree for
// the open project, in the right sidebar's History tab (Grade | History,
// index.html, style.css). One file, no dependency on app.js's internals
// (S, cfg(), Panels...): everything it needs is either its own fetch to the
// C1 routes (studio/projects.py through studio/server.py) or the two small
// hooks session.js already exposes for exactly this:
//
//   window.applyExternalConfig(config, state)   to put a checked out / undone
//                                                / redone / forked config on
//                                                screen, same path an outside
//                                                agent's edit already takes
//   the "studio:session" window event           to learn a revision moved,
//                                                without polling on a timer
//                                                of its own (see session.js)
//
// Tab switching itself is not this file's own invention either: which pane
// is showing lives on <html data-paramtab="grade|history">, set pre paint by
// static/sidebars.js the same way data-sidebar-left/right already is, so a
// reload never flashes the Grade pane before switching to a History tab
// somebody left open (see the CSS in style.css's "right sidebar tab strip"
// section). This file only ever flips that one attribute; it never toggles
// a class on the panes itself.
(function (global) {
  "use strict";

  var els = {};
  var state = {
    log: null,            // last GET /api/project/log body
    proj: null,            // last GET /api/project body
    whoami: null,          // GET /api/whoami, cached: who "you" are
    lastHead: null,        // previous head_short, to notice it actually moved
    expanded: {},           // commit id -> bool, kept across rerenders
    loading: false,
    pending: false
  };
  var pendingToastBy = null;
  var refreshTimer = null;

  /* ---- http -------------------------------------------------------------
     Same shape as app.js's own api(): fetch, throw the server's own message
     on a non 2xx response, otherwise hand back the parsed JSON. Kept as a
     private copy rather than reaching into app.js's closure: this file is
     meant to work even if app.js never loaded at all (session.js's own
     header comment makes the same promise about itself). */
  function api(path, opts) {
    return fetch(path, opts).then(function (r) {
      if (!r.ok) {
        return r.json().catch(function () { return { error: r.statusText }; })
          .then(function (j) { throw new Error(j.error || r.statusText); });
      }
      return r.json();
    });
  }

  function postJSON(path, body) {
    return api(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {})
    });
  }

  /* ---- identity and colour -----------------------------------------------
     Three things a chip can be: you, another person, or an agent. The server
     never tags an author with a role, it is just a string (the account name,
     "studio" from this tab with logins off, "cli" or "agent:<name>" from the
     CLI, "server" for a migrated root commit) -- see studio/projects.py's
     "Author" comment and _author() in server.py. So the classification is a
     naming convention read here, the same convention the CLI and the plan
     both document: agent:<name>, cli and the root's own "server" are not
     people; "studio" is what THIS tab (any logged out tab) signs as, so with
     logins off that is the only way to know a row is "you" versus somebody
     else on a different machine using the same anonymous account; with
     logins on the account name is compared to whoami's own user instead. */
  function isAgent(author) {
    var a = String(author || "");
    return a === "cli" || a === "server" || /^agent:/i.test(a);
  }

  function isYou(author) {
    var who = state.whoami;
    if (who && who.auth && who.user) { return author === who.user; }
    return author === "studio";
  }

  function hashStr(s) {
    var h = 0;
    for (var i = 0; i < s.length; i++) { h = ((h << 5) - h + s.charCodeAt(i)) | 0; }
    return Math.abs(h);
  }

  // Two hue ranges (a "person" family, an "agent" family) so the shape
  // (round pill versus a tag's smaller radius, see .hchip/.hchip-agent in
  // style.css) is not the only signal; a stable hash keeps one author the
  // same colour across a reload without a lookup table anywhere.
  function hueFor(author) {
    var h = hashStr(String(author || ""));
    return isAgent(author) ? (265 + (h % 55)) : (155 + (h % 60));
  }

  function buildChip(author) {
    var span = document.createElement("span");
    var agent = isAgent(author);
    var you = isYou(author);
    span.className = "hchip" + (agent ? " hchip-agent" : " hchip-person") + (you ? " hchip-you" : "");
    span.style.setProperty("--hue", String(hueFor(author)));
    span.textContent = author || "unknown";
    span.title = you ? "you" : (agent ? "agent" : "person");
    return span;
  }

  function renderLegend() {
    if (!els.legend) return;
    els.legend.innerHTML = "";
    [
      { label: "you", cls: "hchip-person hchip-you", hue: 205 },
      { label: "other people", cls: "hchip-person", hue: 205 },
      { label: "agents", cls: "hchip-agent", hue: 280 }
    ].forEach(function (it) {
      var wrap = document.createElement("span");
      wrap.className = "row";
      var chip = document.createElement("span");
      chip.className = "hchip " + it.cls;
      chip.style.setProperty("--hue", String(it.hue));
      chip.textContent = "  ";
      wrap.appendChild(chip);
      wrap.appendChild(document.createTextNode(" " + it.label));
      els.legend.appendChild(wrap);
    });
  }

  /* ---- time --------------------------------------------------------------
     A short relative label in the row ("3m ago"), the full local time on
     hover -- exactly the split the plan asks for, spelled with "to"/"ago"
     rather than any dash. */
  function relativeTime(ts) {
    if (!ts) return "";
    var diff = (Date.now() / 1000) - ts;
    if (diff < 0) diff = 0;
    if (diff < 5) return "just now";
    if (diff < 60) return Math.floor(diff) + "s ago";
    if (diff < 3600) return Math.floor(diff / 60) + "m ago";
    if (diff < 86400) return Math.floor(diff / 3600) + "h ago";
    if (diff < 86400 * 30) return Math.floor(diff / 86400) + "d ago";
    return new Date(ts * 1000).toLocaleDateString();
  }

  function absoluteTime(ts) {
    return ts ? new Date(ts * 1000).toLocaleString() : "";
  }

  function fmtVal(v) {
    if (v === null || v === undefined) return "none";
    if (typeof v === "boolean") return v ? "on" : "off";
    if (typeof v === "number") return String(Math.round(v * 1000) / 1000);
    if (Array.isArray(v)) return "[" + v.map(fmtVal).join(", ") + "]";
    if (typeof v === "object") return JSON.stringify(v);
    return String(v);
  }

  /* ---- DOM ----------------------------------------------------------- */

  function cacheEls() {
    els.key = document.getElementById("histKey");
    els.clip = document.getElementById("histClip");
    els.branchSelect = document.getElementById("histBranch");
    els.rotation = document.getElementById("histRotation");
    els.head = document.getElementById("histHead");
    els.undoBtn = document.getElementById("histUndoBtn");
    els.redoBtn = document.getElementById("histRedoBtn");
    els.note = document.getElementById("histNote");
    els.body = document.getElementById("historyBody");
    els.graph = document.getElementById("historyGraph");
    els.rows = document.getElementById("historyRows");
    els.legend = document.getElementById("historyLegend");
  }

  function setNote(msg) {
    if (els.note) els.note.textContent = msg || "";
  }

  function findCommitByShort(log, shortId) {
    var list = (log && log.commits) || [];
    for (var i = 0; i < list.length; i++) { if (list[i].short === shortId) return list[i]; }
    return null;
  }

  function shortenKey(key) { return String(key || "").slice(0, 10); }

  function renderHeader(log, proj) {
    els.key.textContent = log.key ? shortenKey(log.key) : "no project";
    els.clip.textContent = log.name || "";
    fillBranchSelect(log.branches, log.branch);
    els.rotation.textContent = "rotation " + ((proj && proj.rotation) || "auto");
    els.head.textContent = log.head_short || "";
    var headRow = findCommitByShort(log, log.head_short);
    els.undoBtn.disabled = !headRow || !headRow.parent;
    els.redoBtn.disabled = !headRow || !!headRow.is_tip;
  }

  function fillBranchSelect(branches, current) {
    var sel = els.branchSelect;
    sel.innerHTML = "";
    (branches || []).forEach(function (b) {
      var opt = document.createElement("option");
      opt.value = b.tip || "";
      opt.textContent = b.name + " (" + (b.commits || 0) + ")";
      if (b.name === current) opt.selected = true;
      sel.appendChild(opt);
    });
  }

  function renderEmpty(msg) {
    state.log = null;
    els.key.textContent = "no project";
    els.clip.textContent = "";
    els.branchSelect.innerHTML = "";
    els.rotation.textContent = "";
    els.head.textContent = "";
    els.undoBtn.disabled = true;
    els.redoBtn.disabled = true;
    els.rows.innerHTML = "";
    var div = document.createElement("div");
    div.className = "historyempty";
    div.textContent = msg || "no project is open yet";
    els.rows.appendChild(div);
    els.graph.setAttribute("width", "0");
    els.graph.setAttribute("height", "0");
  }

  function renderError(msg) {
    els.rows.innerHTML = "";
    var div = document.createElement("div");
    div.className = "historyerr";
    div.textContent = "could not load history: " + msg;
    els.rows.appendChild(div);
    els.graph.setAttribute("width", "0");
    els.graph.setAttribute("height", "0");
  }

  function buildChangeLine(ch) {
    var div = document.createElement("div");
    var label = document.createElement("span");
    label.className = "hchange-label";
    label.textContent = ch.label + ": ";
    div.appendChild(label);
    div.appendChild(document.createTextNode(fmtVal(ch.old) + " to " + fmtVal(ch.new)));
    return div;
  }

  function toggleExpand(id, row) {
    state.expanded[id] = !state.expanded[id];
    row.classList.toggle("expanded", !!state.expanded[id]);
    relayoutGraph();
  }

  function buildRow(c) {
    var row = document.createElement("div");
    row.className = "historyrow" + (c.is_head ? " head" : "") + (state.expanded[c.id] ? " expanded" : "");
    row.dataset.commit = c.id;
    row.tabIndex = 0;

    // Two lines, not one: a commit's message is the answer to "what changed"
    // (the founder's own framing: "like if i move the hsl slider it should be
    // like hsl slider moved"), so it gets a line of its own with nothing
    // competing for width, prominent text, one line, ellipsis when long. The
    // id/chip/tag/time/actions are all flex:none (actions stay layout space
    // even hidden, for the hover fade) and on a narrow sidebar their combined
    // width alone can exceed it, so sharing a line with the message starved
    // it to zero width; a metadata line above the message never competes
    // with it for space at all.
    var meta = document.createElement("div");
    meta.className = "historyrow-meta";

    var id = document.createElement("span");
    id.className = "hid mono";
    id.textContent = c.short;
    meta.appendChild(id);

    meta.appendChild(buildChip(c.author));

    if (c.is_tip) {
      var tag = document.createElement("span");
      tag.className = "htag";
      tag.textContent = c.branch;
      tag.title = c.branch + " (tip)";
      meta.appendChild(tag);
    }

    var time = document.createElement("span");
    time.className = "htime mono";
    time.textContent = relativeTime(c.ts);
    time.title = absoluteTime(c.ts);
    meta.appendChild(time);

    var actions = document.createElement("span");
    actions.className = "historyrow-actions";
    var gotoBtn = document.createElement("button");
    gotoBtn.type = "button"; gotoBtn.className = "btn"; gotoBtn.textContent = "Go here";
    gotoBtn.addEventListener("click", function (ev) { ev.stopPropagation(); doGoto(c.id); });
    var forkBtn = document.createElement("button");
    forkBtn.type = "button"; forkBtn.className = "btn"; forkBtn.textContent = "Fork from here";
    forkBtn.addEventListener("click", function (ev) { ev.stopPropagation(); doFork(c.id); });
    actions.appendChild(gotoBtn);
    actions.appendChild(forkBtn);
    meta.appendChild(actions);

    row.appendChild(meta);

    var msgLine = document.createElement("div");
    msgLine.className = "historyrow-msgline";
    var msg = document.createElement("span");
    msg.className = "hmsg";
    msg.textContent = c.message || "";
    msg.title = c.message || ""; // the full text on hover even before expanding
    msgLine.appendChild(msg);
    row.appendChild(msgLine);

    var changes = document.createElement("div");
    changes.className = "historyrow-changes";
    var list = c.changes || [];
    if (list.length) {
      list.forEach(function (ch) { changes.appendChild(buildChangeLine(ch)); });
    } else {
      var none = document.createElement("div");
      none.textContent = "no field level changes recorded for this commit";
      changes.appendChild(none);
    }
    row.appendChild(changes);

    row.addEventListener("click", function (ev) {
      if (ev.target && ev.target.closest && ev.target.closest("button")) return;
      toggleExpand(c.id, row);
    });
    row.addEventListener("keydown", function (ev) {
      if (ev.target !== row) return;
      if (ev.key === "Enter" || ev.key === " " || ev.key === "Spacebar") {
        ev.preventDefault();
        toggleExpand(c.id, row);
      }
    });

    return row;
  }

  function renderRows(log) {
    var host = els.rows;
    host.innerHTML = "";
    var commits = log.commits || [];
    if (!commits.length) {
      var empty = document.createElement("div");
      empty.className = "historyempty";
      empty.textContent = "this project has no commits yet";
      host.appendChild(empty);
      return;
    }
    commits.forEach(function (c) { host.appendChild(buildRow(c)); });
  }

  /* ---- the graph column ---------------------------------------------------
     One SVG for the whole visible list, not one per row: dots and edges are
     positioned from each row's OWN measured centre (getBoundingClientRect),
     not an assumed fixed row height, so an expanded row's extra height never
     throws the lines out of alignment with the rows beside them -- toggling
     a row just calls this again. Lanes are branches, oldest branch first
     (log.branches is already created-ascending, so "main" is always lane 0);
     a straight line is a same branch parent, a curve is a fork point (the
     first commit on a branch, whose parent lives in another lane). Only
     produces anything while this pane is actually laid out (display:flex,
     not display:none): a hidden subtree measures everything at zero, which
     the early return below treats as "nothing to draw yet", corrected by
     relayoutGraph() being called again from the tab switch handler. */
  function laneIndex(branches, name) {
    for (var i = 0; i < branches.length; i++) { if (branches[i].name === name) return i; }
    return 0;
  }

  var NS = "http://www.w3.org/2000/svg";
  var LANE_W = 14;

  function buildGraph(log) {
    var svg = els.graph;
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    if (!log || !log.commits || !log.commits.length) {
      svg.setAttribute("width", "0"); svg.setAttribute("height", "0");
      return;
    }
    var rowNodes = els.rows.children;
    var n = Math.min(rowNodes.length, log.commits.length);
    if (!n) { svg.setAttribute("width", "0"); svg.setAttribute("height", "0"); return; }

    var byId = {}, indexById = {};
    for (var i = 0; i < log.commits.length; i++) {
      byId[log.commits[i].id] = log.commits[i];
      indexById[log.commits[i].id] = i;
    }

    var branches = log.branches && log.branches.length ? log.branches : [{ name: log.branch || "main" }];
    var width = branches.length * LANE_W + 10;

    var top = els.rows.getBoundingClientRect().top;
    var centres = [];
    for (i = 0; i < n; i++) {
      var r = rowNodes[i].getBoundingClientRect();
      centres.push((r.top - top) + (r.height / 2));
    }
    var height = els.rows.scrollHeight;
    if (!height) { svg.setAttribute("width", "0"); svg.setAttribute("height", "0"); return; }

    svg.setAttribute("width", String(width));
    svg.setAttribute("height", String(Math.ceil(height)));
    svg.setAttribute("viewBox", "0 0 " + width + " " + Math.ceil(height));

    function laneX(branchName) { return laneIndex(branches, branchName) * LANE_W + (LANE_W / 2) + 4; }

    var c, edge, x1, y1, x2, y2, parent, pj, midY;
    for (i = 0; i < n; i++) {
      c = log.commits[i];
      if (!c.parent) continue;
      pj = indexById[c.parent];
      if (pj === undefined || pj >= n) continue; // parent below the fetched/rendered window
      parent = byId[c.parent];
      x1 = laneX(c.branch); y1 = centres[i];
      x2 = laneX(parent.branch); y2 = centres[pj];
      if (parent.branch === c.branch) {
        edge = document.createElementNS(NS, "line");
        edge.setAttribute("x1", x1); edge.setAttribute("y1", y1);
        edge.setAttribute("x2", x2); edge.setAttribute("y2", y2);
        edge.setAttribute("class", "hedge");
      } else {
        midY = (y1 + y2) / 2;
        edge = document.createElementNS(NS, "path");
        edge.setAttribute("d", "M " + x1 + " " + y1 + " C " + x1 + " " + midY + ", " + x2 + " " + midY + ", " + x2 + " " + y2);
        edge.setAttribute("class", "hedge fork");
      }
      svg.appendChild(edge);
    }
    for (i = 0; i < n; i++) {
      c = log.commits[i];
      var dot = document.createElementNS(NS, "circle");
      dot.setAttribute("cx", String(laneX(c.branch)));
      dot.setAttribute("cy", String(centres[i]));
      dot.setAttribute("r", c.is_head ? "5" : "4");
      dot.setAttribute("class", "hnode" + (c.is_head ? " head" : ""));
      svg.appendChild(dot);
    }
  }

  function relayoutGraph() {
    if (!state.log) return;
    buildGraph(state.log);
    // A second pass on the next frame: right after switching the tab on,
    // the attribute flip and this call can land in the same synchronous
    // turn as the pane's display:none -> flex change, and some engines have
    // not settled layout for getBoundingClientRect yet at that exact point.
    global.requestAnimationFrame(function () { buildGraph(state.log); });
  }

  /* ---- mutating actions --------------------------------------------------
     Every one of these signs itself "studio", the same literal label
     StudioSession.publish (session.js) uses for this tab's own grade edits,
     so the resulting live state's `by` is "studio" here too -- consistent
     with how this file later tells "my own action" apart from "somebody
     else's" for the toast in onSessionEvent. */
  function applyProjectConfig(proj) {
    if (proj && proj.config && typeof global.applyExternalConfig === "function") {
      var by = (proj.head_commit && proj.head_commit.author) || "studio";
      global.applyExternalConfig(proj.config, { by: by });
    }
  }

  function doMutate(url, body) {
    var payload = { by: "studio" };
    if (body) { for (var k in body) { if (body.hasOwnProperty(k)) payload[k] = body[k]; } }
    return postJSON(url, payload).then(function (proj) {
      applyProjectConfig(proj);
      if (proj && Object.prototype.hasOwnProperty.call(proj, "moved")) {
        setNote(proj.moved ? "" : (proj.note || "no move"));
      } else {
        setNote("");
      }
      refresh();
      return proj;
    }).catch(function (err) {
      setNote(String((err && err.message) || err));
      throw err;
    });
  }

  function doUndo() { doMutate("/api/project/undo").catch(function () { /* note already set */ }); }
  function doRedo() { doMutate("/api/project/redo").catch(function () { /* note already set */ }); }
  function doGoto(commitId) { doMutate("/api/project/checkout", { commit: commitId }).catch(function () {}); }
  function doFork(commitId) {
    var name = prompt("Name this branch (letters, digits, dot, dash, underscore; leave blank to auto name it)", "");
    if (name === null) return; // cancelled
    var body = {};
    if (commitId) body.commit = commitId;
    var trimmed = name.trim();
    if (trimmed) body.name = trimmed;
    doMutate("/api/project/fork", body).catch(function () {});
  }

  function onBranchChange() {
    var tip = els.branchSelect.value;
    if (tip) doGoto(tip);
  }

  /* ---- fetch + render loop ------------------------------------------- */

  function onData(log, proj) {
    var newHead = log.head_short;
    var toastCandidate = pendingToastBy;
    pendingToastBy = null;
    if (state.lastHead !== null && newHead !== state.lastHead && toastCandidate && global.studioToast) {
      var row = findCommitByShort(log, newHead);
      global.studioToast(toastCandidate + ": " + (row ? row.message : "the project moved"));
    }
    state.lastHead = newHead;
    state.log = log;
    state.proj = proj;
    renderHeader(log, proj);
    var scrollEl = els.body;
    var atScroll = scrollEl ? scrollEl.scrollTop : 0;
    renderRows(log);
    if (scrollEl) scrollEl.scrollTop = atScroll;
    relayoutGraph();
    renderLegend();
  }

  function refresh() {
    if (state.loading) { state.pending = true; return; }
    state.loading = true;
    api("/api/project").then(function (proj) {
      if (!proj || !proj.open) {
        state.lastHead = null;
        renderEmpty(proj && proj.note);
        renderLegend();
        return null;
      }
      return api("/api/project/log?limit=2000").then(function (log) { onData(log, proj); });
    }).catch(function (err) {
      renderError(String((err && err.message) || err));
    }).finally(function () {
      state.loading = false;
      if (state.pending) { state.pending = false; refresh(); }
    });
  }

  function scheduleRefresh() {
    if (refreshTimer) return;
    refreshTimer = setTimeout(function () { refreshTimer = null; refresh(); }, 120);
  }

  // The one hook session.js exposes for this (see the file header comment
  // and session.js's own dispatchState): fired on every revision this tab
  // learns about, whatever caused it. Never trust the event's own detail as
  // the whole truth (it is the live mirror, not the tree), always refetch.
  function onSessionEvent(ev) {
    var detail = ev && ev.detail;
    if (detail && detail.by && detail.by !== "studio") {
      pendingToastBy = detail.by;
    }
    scheduleRefresh();
  }

  /* ---- tab strip ------------------------------------------------------- */

  function bindTabs() {
    var tabs = document.querySelectorAll(".paramtab");
    for (var i = 0; i < tabs.length; i++) {
      tabs[i].addEventListener("click", function (ev) {
        var tab = ev.currentTarget.getAttribute("data-paramtab");
        if (global.fixxrSidebars && global.fixxrSidebars.setParamTab) {
          global.fixxrSidebars.setParamTab(tab);
        } else {
          document.documentElement.setAttribute("data-paramtab", tab);
        }
        if (tab === "history") { refresh(); relayoutGraph(); }
      });
    }
  }

  function fetchWhoami() {
    return api("/api/whoami")
      .then(function (w) { state.whoami = w; })
      .catch(function () { state.whoami = { auth: false }; });
  }

  function init() {
    cacheEls();
    if (!els.rows || !els.graph) return; // markup missing: nothing to mount onto
    bindTabs();
    els.undoBtn.addEventListener("click", doUndo);
    els.redoBtn.addEventListener("click", doRedo);
    els.branchSelect.addEventListener("change", onBranchChange);
    global.addEventListener("studio:session", onSessionEvent);
    fetchWhoami().then(refresh);
  }

  init();
}(window));
