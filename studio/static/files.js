/* The files pane (contract E2): folders and clips on the SERVER, per account,
 * shared to people and teams the way a drive is.
 *
 * The founder's ask this answers, in their own words: "fix the ui and layout of
 * the file explorer and also make sure the layout works for all platforms" and
 * "if you drag and drop a file (or upload) it should upload into the server's
 * folders so multiple users can use this (all files are independent to them)
 * but they can share it to other people (files, folders) create folders as
 * well. but basically google drive like share so then when people make updates
 * it shows up in history etc."
 *
 * The whole server side is lane L1's studio/library.py; this file adds no rules
 * of its own. It reads GET /api/library and draws it, and every write is one of
 * the six routes that module owns. Two things follow from that and are worth
 * saying out loud:
 *
 *   - There is no permission logic in here. A refusal arrives as a 403 with one
 *     plain sentence and is shown as that sentence, never guessed at in advance
 *     and never swallowed. Buttons the listing says this account cannot use
 *     (`can_edit` false) are simply not drawn, which is a courtesy, not the
 *     check.
 *   - Paths are always the server's own relative paths. This file never builds
 *     one, never joins one with a separator it invented, and never sees a path
 *     on the machine.
 *
 * Two views, one pane. `data-filesroot` on .filespane says which:
 *
 *   a library root  mine | shared | team:<id> | user:<id> | trash
 *                   drawn here, one list, folders first then clips.
 *   thismac         the anywhere browser exactly as it was (app.js's browse*
 *                   functions, #browseCrumbs/#browseQuick/#browseDirs/
 *                   #browseClips). Offered only when the server runs without
 *                   logins, because with logins on an account has no business
 *                   walking the disk. Nothing in here touches it beyond
 *                   flipping that one attribute.
 *
 * app.js hands this file a small api object during boot(), the same shape
 * window-editor.js, layers.js and frames.js get: functions of app.js's, never a
 * reach into its state, so this file cannot become a second source of truth for
 * which clip is open.
 */
(function (global) {
  "use strict";

  var ROOT_KEY = "studio.files.root.v1";
  var PATH_KEY = "studio.files.path.v1";
  var LAYOUT_KEY = "studio.files.layout.v1";
  var SORT_KEY = "studio.files.sort.v1";
  var THISMAC = "thismac";

  var api = null;                    // set by init(), app.js's own functions
  var authOn = false;                // the server is running with logins
  var me = null;                     // {id, name, role} from /api/auth/me
  var people = { users: [], teams: [], roles: ["viewer", "editor"] };
  // "mine" to match index.html's own data-filesroot, so the pane is never one
  // view according to the CSS and the other according to this file, not even
  // for the few milliseconds before /api/auth/me answers.
  var root = "mine";                 // where we are looking
  var path = "";                     // the folder inside it
  var listing = null;                // the last GET /api/library answer
  var layout = "list";               // list | grid
  var sortBy = "name";               // name | new | size
  var loadedOnce = false;            // the pane has been opened at least once
  var ready = false;                 // /api/auth/me has answered
  var wantLoad = false;              // the pane was opened before it answered
  var pending = 0;                   // uploads still in flight
  var dragging = null;               // the row being dragged, if any
  // Every navigation gets a number, and only the newest one is allowed to
  // paint. Listing a folder can take a while (the server probes each video for
  // its duration), so a person who opens the pane and immediately switches
  // root or walks into a folder has a request in flight for a place they have
  // already left. Without this, that late answer lands and yanks them back.
  var nav = 0;

  function byId(id) { return document.getElementById(id); }

  function pane() { return document.querySelector(".filespane"); }

  function toast(msg, bad) {
    if (api && api.toast) { api.toast(msg, bad); return; }
    if (typeof global.studioToast === "function") { global.studioToast(msg, bad); }
  }

  function store(key, value) {
    try { global.localStorage.setItem(key, value); } catch (e) { /* private mode */ }
  }

  function read(key) {
    try { return global.localStorage.getItem(key); } catch (e) { return null; }
  }

  /* One fetch shape for every call in this file. A refusal from library.py is
   * a non 2xx with {"error": "one plain sentence"}; that sentence is what a
   * caller sees, because the whole point of the server writing sentences was
   * that somebody would show them. */
  function call(url, options) {
    return fetch(url, options || {}).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        if (!r.ok) { throw new Error(j.error || ("HTTP " + r.status)); }
        return j;
      });
    });
  }

  function post(url, body) {
    return call(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {})
    });
  }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) { n.className = cls; }
    if (text !== undefined && text !== null) { n.textContent = text; }
    return n;
  }

  function note(text) {
    var n = el("div", "note", text);
    return n;
  }

  function icon(name, cls) {
    try { return global.StudioIcons.render(name, cls); } catch (e) { return el("span", cls); }
  }

  function isLibrary() { return root !== THISMAC; }

  function canEdit() {
    return !!(listing && listing.can_edit) && root !== "shared"
      && root.indexOf("team:") !== 0 && root !== "trash";
  }

  /* ---- small formatters --------------------------------------------------- */

  function clock(seconds) {
    var s = Math.max(0, Math.round(Number(seconds) || 0));
    var m = Math.floor(s / 60);
    var rest = s % 60;
    return m + ":" + (rest < 10 ? "0" : "") + rest;
  }

  function megabytes(bytes) {
    var n = Number(bytes) || 0;
    if (n >= 1e9) { return (n / 1e9).toFixed(1) + " GB"; }
    return Math.max(1, Math.round(n / 1e6)) + " MB";
  }

  function ago(ts) {
    var secs = Math.max(0, (Date.now() / 1000) - (Number(ts) || 0));
    if (secs < 90) { return "just now"; }
    if (secs < 5400) { return Math.round(secs / 60) + "m ago"; }
    if (secs < 129600) { return Math.round(secs / 3600) + "h ago"; }
    return Math.round(secs / 86400) + "d ago";
  }

  /* ---- the root switcher --------------------------------------------------
     My files, Shared with me, one chip per team this account is in, Trash, and
     This Mac. With logins off there is one account and one org, so there is
     nobody to share with and no team to be in: those chips are not drawn at
     all rather than drawn and disabled, because an empty "Shared with me" on a
     single user machine is a question, not a feature. This Mac is the opposite
     way round for the same reason: it exists precisely when there are no
     accounts to confine. */

  function rootChips() {
    var out = [{ id: "mine", label: "My files" }];
    if (authOn) {
      out.push({ id: "shared", label: "Shared with me" });
      (people.teams || []).forEach(function (t) {
        if (t.mine) { out.push({ id: "team:" + t.id, label: t.name }); }
      });
    }
    out.push({ id: "trash", label: "Trash" });
    if (!authOn) { out.push({ id: THISMAC, label: "This Mac" }); }
    return out;
  }

  function renderRoots() {
    var host = byId("filesRoots");
    if (!host) { return; }
    host.innerHTML = "";
    rootChips().forEach(function (chip) {
      var b = el("button", "filesroot", chip.label);
      b.type = "button";
      b.setAttribute("role", "tab");
      b.dataset.root = chip.id;
      // A user root reached by walking into a share keeps "Shared with me"
      // lit, because that is the door it was entered through and the chip is
      // how a person walks back out of it.
      var lit = chip.id === root
        || (chip.id === "shared" && root.indexOf("user:") === 0);
      if (lit) {
        b.classList.add("active");
        b.setAttribute("aria-selected", "true");
      }
      b.addEventListener("click", function () { go(chip.id, ""); });
      // A row dropped on a root chip is not a move: the destination is a
      // different owner's tree, and library.py refuses a cross owner move on
      // purpose. Only This Mac and the trash are meaningless targets too, so
      // no chip takes a drop and none of them pretend to.
      host.appendChild(b);
    });
  }

  /* ---- navigation --------------------------------------------------------- */

  function go(nextRoot, nextPath) {
    root = nextRoot || "mine";
    path = nextPath || "";
    store(ROOT_KEY, root);
    store(PATH_KEY, path);
    var p = pane();
    if (p) { p.dataset.filesroot = root; }
    // Anything already in flight was asked about the place we just left.
    nav += 1;
    renderRoots();
    syncTools();
    if (!isLibrary()) {
      // This Mac: app.js owns everything below this line. Its own lazy first
      // load already ran (or runs on the tab's first open), so there is
      // nothing to fetch here.
      clearError();
      return;
    }
    return refresh();
  }

  function refresh() {
    if (!isLibrary()) { return Promise.resolve(null); }
    var seq = (nav += 1);
    var url = "/api/library?root=" + encodeURIComponent(root)
      + "&path=" + encodeURIComponent(path);
    return call(url).then(function (j) {
      if (seq !== nav) { return null; }   // a newer navigation has already won
      listing = j;
      // The server decides what the root actually was: walking into a share
      // from the "shared" index arrives at user:<owner>, and a listing that
      // says so is what keeps the breadcrumb and the chips honest.
      if (j.root && j.root !== root) {
        root = j.root;
        store(ROOT_KEY, root);
        var p = pane();
        if (p) { p.dataset.filesroot = root; }
        renderRoots();
      }
      path = j.path || "";
      store(PATH_KEY, path);
      clearError();
      if (j.error) { showError(j.error); }
      renderCrumbs();
      renderList();
      syncTools();
      return j;
    }).catch(function (err) {
      if (seq !== nav) { return null; }
      showError(err.message || String(err));
      // A path that has gone (renamed in Finder, or a share taken away) must
      // not leave the pane stuck on it for the rest of the session.
      if (path) { path = ""; store(PATH_KEY, ""); }
      renderCrumbs();
      return null;
    });
  }

  function showError(msg) {
    var host = byId("filesError");
    if (host) { host.textContent = msg || ""; }
  }

  function clearError() { showError(""); }

  /* ---- breadcrumb ---------------------------------------------------------
     Straight from the listing's own `crumbs`, never rebuilt from the path
     string: inside somebody else's library the walk starts at the grant, not
     at their library root, and only the server knows where that is. Each
     segment is also a drop target, which is the contract's "move a row onto a
     breadcrumb segment" and in practice the only way to move something UP a
     level without a second window. */

  function renderCrumbs() {
    var host = byId("filesCrumbs");
    if (!host) { return; }
    host.innerHTML = "";
    var crumbs = (listing && listing.crumbs) || [{ name: "My files", root: root, path: "" }];
    crumbs.forEach(function (c, i) {
      var last = i === crumbs.length - 1;
      var b = el("button", "crumb" + (last ? " current" : ""), c.name);
      b.type = "button";
      b.title = c.path || c.name;
      if (last) {
        b.disabled = true;
      } else {
        b.addEventListener("click", function () { go(c.root, c.path); });
      }
      // Even the current segment takes a drop: dragging a row onto "you are
      // here" is a no-op the server refuses cheaply, and refusing to draw the
      // target on the last crumb would make the whole row feel arbitrary.
      dropTargetForMove(b, c.root, c.path);
      host.appendChild(b);
      if (!last) {
        var sep = el("span", "crumbsep", "/");
        sep.setAttribute("aria-hidden", "true");
        host.appendChild(sep);
      }
    });
  }

  /* ---- the list -----------------------------------------------------------
     One list, folders first then clips, exactly as the contract asks. Grid
     mode is the same rows re-flowed by one class on the host, so there is one
     renderer and one set of handlers rather than two that drift apart. */

  function sorted(rows) {
    var out = rows.slice();
    if (sortBy === "new") {
      out.sort(function (a, b) { return (b.mtime || 0) - (a.mtime || 0); });
    } else if (sortBy === "size") {
      out.sort(function (a, b) { return (b.bytes || 0) - (a.bytes || 0); });
    } else {
      out.sort(function (a, b) {
        return String(a.name || "").toLowerCase()
          .localeCompare(String(b.name || "").toLowerCase());
      });
    }
    return out;
  }

  function renderList() {
    var host = byId("filesList");
    if (!host) { return; }
    host.innerHTML = "";
    host.classList.toggle("grid", layout === "grid");
    if (!listing) { return; }
    var folders = sorted(listing.folders || []);
    var files = sorted(listing.files || []);
    if (!folders.length && !files.length) {
      host.appendChild(note(emptyLine()));
      return;
    }
    folders.forEach(function (row) { host.appendChild(makeRow(row, "folder")); });
    files.forEach(function (row) { host.appendChild(makeRow(row, "file")); });
  }

  function emptyLine() {
    if (root === "trash") { return "The trash is empty."; }
    if (root === "shared") { return "Nobody has shared anything with you yet."; }
    if (root.indexOf("team:") === 0) { return "Nothing has been shared with this team yet."; }
    if (listing && listing.error) { return "Nothing to show here."; }
    return "This folder is empty. Upload a clip or make a folder.";
  }

  function makeRow(row, kind) {
    var node = el("div", "filerow");
    node.dataset.kind = kind;
    node.dataset.path = row.path || "";
    node.dataset.root = row.root || root;
    node.title = row.name || "";

    if (kind === "file" && !row.missing) {
      var img = document.createElement("img");
      img.className = "fthumb";
      img.alt = "";
      img.loading = "lazy";
      img.src = "/api/thumb?root=" + encodeURIComponent(row.root || root)
        + "&path=" + encodeURIComponent(row.path || "") + "&w=96&t=0";
      // A clip ffprobe or ffmpeg cannot read still gets a row: the glyph
      // replaces the picture rather than the row disappearing, because a file
      // that cannot be previewed is exactly the one somebody needs to find.
      img.addEventListener("error", function () {
        if (img.parentNode) { img.parentNode.replaceChild(icon("PlaySquareIcon", "rowicon"), img); }
      });
      node.appendChild(img);
    } else {
      node.appendChild(icon(kind === "folder" ? "Folder01Icon" : "PlaySquareIcon", "rowicon"));
    }

    var meta = el("div", "fmeta");
    meta.appendChild(el("div", "fname", row.name || row.path || ""));
    var sub = el("div", "fsub");
    if (root === "trash") {
      sub.appendChild(el("span", null, "was " + (row.orig || row.name)));
      sub.appendChild(el("span", null, ago(row.trashed_at)));
    } else if (kind === "folder") {
      sub.appendChild(el("span", null, (row.count || 0) + (row.count === 1 ? " item" : " items")));
    } else {
      if (row.duration) { sub.appendChild(el("span", null, clock(row.duration))); }
      sub.appendChild(el("span", null, megabytes(row.bytes)));
      if (row.project && row.project.head) {
        var head = el("span", null, row.project.head);
        head.title = "this clip has a project; HEAD is " + row.project.head;
        sub.appendChild(head);
      }
    }
    if (row.owner && row.owner_id !== (me && me.id)) {
      sub.appendChild(el("span", null, row.owner));
    }
    meta.appendChild(sub);
    node.appendChild(meta);

    if (row.missing) {
      node.appendChild(el("span", "fbadge warn", "missing"));
    } else if (row.shared_with_me) {
      node.appendChild(el("span", "fbadge", row.role || "shared"));
    } else if (row.shared_by_me) {
      node.appendChild(el("span", "fbadge", "shared"));
    }

    node.appendChild(rowActions(row, kind, node));

    if (row.missing) {
      node.classList.add("missing");
    } else {
      node.addEventListener("click", function (ev) {
        if (ev.target.closest && ev.target.closest(".faction")) { return; }
        if (ev.target.tagName === "INPUT") { return; }
        openRow(row, kind);
      });
    }

    if (kind === "file" && api && api.getClipName && row.name === api.getClipName()) {
      node.classList.add("active");
    }

    // Drag to move: only inside a tree this account may write to, and only
    // for a real path (a `shared` index row has no folder of its own to be
    // moved out of).
    if (canEdit() && !row.missing && root !== "trash") {
      node.draggable = true;
      node.addEventListener("dragstart", function (ev) {
        dragging = { root: row.root || root, path: row.path, name: row.name };
        node.classList.add("dragging");
        try {
          ev.dataTransfer.setData("application/x-studio-file", JSON.stringify(dragging));
          ev.dataTransfer.effectAllowed = "move";
        } catch (e) { /* some browsers refuse a custom type; the fallback is `dragging` */ }
      });
      node.addEventListener("dragend", function () {
        node.classList.remove("dragging");
        dragging = null;
      });
    }
    if (kind === "folder" && !row.missing) {
      dropTargetForMove(node, row.root || root, row.path);
      bindFolderFileDrop(node, row.path);
    }
    return node;
  }

  function rowActions(row, kind, node) {
    var acts = el("div", "facts");
    function add(label, title, fn) {
      var b = el("button", "faction", label);
      b.type = "button";
      b.title = title;
      b.addEventListener("click", function (ev) {
        ev.stopPropagation();
        fn();
      });
      acts.appendChild(b);
      return b;
    }
    if (root === "trash") {
      add("Restore", "Put this back where it came from", function () { restore(row); });
      return acts;
    }
    if (!row.missing) {
      add("Open", kind === "folder" ? "Go into this folder" : "Load this clip",
        function () { openRow(row, kind); });
    }
    // Renaming, sharing and trashing are the owner's, so they are drawn on a
    // row in a tree this account can write to and nowhere else. The server
    // refuses either way; this only avoids offering a button whose one job is
    // to produce a refusal.
    if (canEdit() && !row.missing) {
      add("Rename", "Give this a different name", function () { beginRename(row, node); });
    }
    if (authOn && root === "mine") {
      add("Share", "Give somebody in your org access to this",
        function () { openShare(row, kind); });
    }
    if (canEdit()) {
      add("Trash", "Move this to the trash (nothing is deleted)",
        function () { trash(row); });
    }
    return acts;
  }

  function openRow(row, kind) {
    if (row.missing) {
      toast(row.name + " is not where the share says it is any more", true);
      return;
    }
    if (kind === "folder") {
      go(row.root || root, row.path);
      return;
    }
    if (root === "trash") {
      toast("restore " + row.name + " before opening it", true);
      return;
    }
    if (!api || !api.openLibrary) { return; }
    api.openLibrary(row.root || root, row.path).catch(function (err) {
      showError(err.message || String(err));
      toast(err.message || String(err), true);
    });
  }

  /* ---- writes -------------------------------------------------------------
     Every one of these is one route on library.py and one refresh afterwards.
     `studio:library` is dispatched so the activity feed repaints without this
     file knowing that feed exists. */

  function afterWrite(message) {
    global.dispatchEvent(new CustomEvent("studio:library", { detail: { root: root, path: path } }));
    if (message) { toast(message); }
    return refresh();
  }

  function trash(row) {
    return post("/api/library/trash", { root: row.root || root, path: row.path })
      .then(function (j) {
        var shares = j && j.shares ? " (" + j.shares + " share(s) removed with it)" : "";
        return afterWrite("moved " + row.name + " to the trash" + shares);
      })
      .catch(function (err) { showError(err.message || String(err)); toast(err.message || String(err), true); });
  }

  function restore(row) {
    return post("/api/library/restore", { path: row.path })
      .then(function (j) { return afterWrite("put " + (j.name || row.name) + " back"); })
      .catch(function (err) { showError(err.message || String(err)); toast(err.message || String(err), true); });
  }

  function move(fromRoot, fromPath, toRoot, toPath) {
    if (!fromPath && fromPath !== "") { return Promise.resolve(); }
    if (fromRoot !== toRoot) {
      toast("a file can only be moved inside one person's library", true);
      return Promise.resolve();
    }
    // Dropping a folder on itself, or a row on the folder it is already in,
    // is what a slightly missed drag looks like. Say nothing and do nothing.
    var parent = fromPath.indexOf("/") >= 0 ? fromPath.slice(0, fromPath.lastIndexOf("/")) : "";
    if (fromPath === toPath || parent === toPath) { return Promise.resolve(); }
    return post("/api/library/move", { root: fromRoot, path: fromPath, to: toPath })
      .then(function (j) { return afterWrite("moved " + (j.name || "it") + " into " + (toPath || "the top")); })
      .catch(function (err) { showError(err.message || String(err)); toast(err.message || String(err), true); });
  }

  /* A folder row or a breadcrumb segment that a dragged row can be let go on.
     dragover has to preventDefault or the browser refuses the drop outright;
     the type test is on dataTransfer.types because getData is not readable
     during a drag (Chrome's protected mode), which is also why `dragging`
     above is kept as the real payload and the DataTransfer copy is a
     courtesy for anything outside this page. */
  function dropTargetForMove(node, targetRoot, targetPath) {
    node.addEventListener("dragover", function (ev) {
      var types = (ev.dataTransfer && ev.dataTransfer.types) || [];
      var ours = dragging || Array.prototype.indexOf.call(types, "application/x-studio-file") >= 0;
      if (!ours) { return; }
      ev.preventDefault();
      ev.stopPropagation();
      ev.dataTransfer.dropEffect = "move";
      node.classList.add("dropinto");
    });
    node.addEventListener("dragleave", function () { node.classList.remove("dropinto"); });
    node.addEventListener("drop", function (ev) {
      node.classList.remove("dropinto");
      var payload = dragging;
      if (!payload) {
        try { payload = JSON.parse(ev.dataTransfer.getData("application/x-studio-file") || "null"); }
        catch (e) { payload = null; }
      }
      if (!payload) { return; }
      ev.preventDefault();
      ev.stopPropagation();
      dragging = null;
      move(payload.root, payload.path, targetRoot, targetPath || "");
    });
  }

  /* Rename in place: the name becomes an input inside its own row. A prompt()
     was the other option and is worse in three ways at once -- it cannot be
     styled, it blocks the page, and it is unreachable to a test harness or an
     agent driving the page. Enter commits, Escape and blur cancel. */
  function beginRename(row, node) {
    var nameEl = node.querySelector(".fname");
    if (!nameEl || node.querySelector(".frename")) { return; }
    var input = document.createElement("input");
    input.type = "text";
    input.className = "frename";
    input.value = row.name || "";
    input.setAttribute("aria-label", "New name");
    nameEl.textContent = "";
    nameEl.appendChild(input);
    input.focus();
    input.select();
    var done = false;
    function cancel() {
      if (done) { return; }
      done = true;
      renderList();
    }
    function commit() {
      if (done) { return; }
      var next = input.value.trim();
      if (!next || next === row.name) { cancel(); return; }
      done = true;
      post("/api/library/rename", { root: row.root || root, path: row.path, name: next })
        .then(function (j) { return afterWrite("renamed to " + (j.name || next)); })
        .catch(function (err) {
          showError(err.message || String(err));
          toast(err.message || String(err), true);
          renderList();
        });
    }
    input.addEventListener("keydown", function (ev) {
      ev.stopPropagation();               // the app's single key shortcuts
      if (ev.key === "Enter") { ev.preventDefault(); commit(); }
      else if (ev.key === "Escape") { ev.preventDefault(); cancel(); }
    });
    input.addEventListener("blur", cancel);
    input.addEventListener("click", function (ev) { ev.stopPropagation(); });
  }

  /* New folder is the same idea one row higher: an input at the top of the
     list rather than a dialog, so making three folders in a row is three
     names and three Enters. */
  function beginNewFolder() {
    if (!canEdit()) { toast("you cannot make a folder here", true); return; }
    var host = byId("filesList");
    if (!host || host.querySelector(".fnewfolder")) { return; }
    var wrap = el("div", "filerow fnewfolder");
    wrap.appendChild(icon("Folder01Icon", "rowicon"));
    var input = document.createElement("input");
    input.type = "text";
    input.className = "frename";
    input.placeholder = "New folder name";
    input.setAttribute("aria-label", "New folder name");
    wrap.appendChild(input);
    host.insertBefore(wrap, host.firstChild);
    input.focus();
    var done = false;
    function cancel() { if (!done) { done = true; renderList(); } }
    function commit() {
      if (done) { return; }
      var name = input.value.trim();
      if (!name) { cancel(); return; }
      done = true;
      post("/api/library/mkdir", { root: root, path: path, name: name })
        .then(function (j) { return afterWrite("made " + (j.name || name)); })
        .catch(function (err) {
          showError(err.message || String(err));
          toast(err.message || String(err), true);
          renderList();
        });
    }
    input.addEventListener("keydown", function (ev) {
      ev.stopPropagation();
      if (ev.key === "Enter") { ev.preventDefault(); commit(); }
      else if (ev.key === "Escape") { ev.preventDefault(); cancel(); }
    });
    input.addEventListener("blur", cancel);
  }

  /* ---- uploads ------------------------------------------------------------
     One file at a time, for the reason app.js's own upload already gives: the
     server's ffmpeg slots are shared with every scrub and thumbnail in the
     room, and a queue of one keeps each percentage a real number rather than
     an average of several.

     What is new here is that nothing is silent. Every file gets its own row
     with its own bar, and a file the server refuses (too big, over the quota,
     not a video extension, not something ffprobe can read) KEEPS its row with
     the server's own sentence on it until it is dismissed. That is the whole
     of the founder's "surfaced as messages, never silent". */

  function uploadRow(name) {
    var host = byId("filesUploads");
    var node = el("div", "uploadrow");
    node.appendChild(el("span", "uname", name));
    var bar = el("span", "ubar");
    var fill = el("i");
    fill.style.width = "0%";
    bar.appendChild(fill);
    node.appendChild(bar);
    var pct = el("span", "upct", "0%");
    node.appendChild(pct);
    if (host) { host.appendChild(node); }
    return {
      node: node,
      progress: function (fraction) {
        var n = Math.max(0, Math.min(100, Math.round(fraction * 100)));
        fill.style.width = n + "%";
        pct.textContent = n + "%";
      },
      done: function () { if (node.parentNode) { node.parentNode.removeChild(node); } },
      failed: function (message) {
        node.classList.add("failed");
        node.querySelector(".uname").textContent = name + ": " + message;
        pct.textContent = "";
        var x = el("button", "udismiss", "Dismiss");
        x.type = "button";
        x.addEventListener("click", function () {
          if (node.parentNode) { node.parentNode.removeChild(node); }
        });
        node.appendChild(x);
      }
    };
  }

  function uploadOne(file, row) {
    return new Promise(function (resolve, reject) {
      var xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/upload?root=" + encodeURIComponent(root)
        + "&path=" + encodeURIComponent(path));
      xhr.setRequestHeader("Content-Type", "application/octet-stream");
      // Percent encoded for the same reason app.js does it: a header value has
      // to stay on one line and plain ASCII, and a file name is neither.
      xhr.setRequestHeader("X-File-Name", encodeURIComponent(file.name));
      xhr.upload.onprogress = function (e) {
        if (e.lengthComputable && e.total) {
          row.progress(e.loaded / e.total);
          setButtonLabel(Math.round((e.loaded / e.total) * 100) + "%");
        }
      };
      xhr.onload = function () {
        var j = {};
        try { j = JSON.parse(xhr.responseText || "{}"); } catch (err) { /* an error page, not JSON */ }
        if (xhr.status >= 200 && xhr.status < 300) { resolve(j); }
        else { reject(new Error(j.error || ("upload failed: HTTP " + xhr.status))); }
      };
      xhr.onerror = function () { reject(new Error("upload failed: the connection dropped")); };
      xhr.send(file);
    });
  }

  /* #uploadClipBtn's own label is driven here as well as by app.js, because it
     is one button serving both views and anything waiting on it (a person, the
     test harness, an agent) has to be able to read "is an upload in flight"
     off it in either. */
  function setButtonLabel(text) {
    var btn = byId("uploadClipBtn");
    if (btn) { btn.textContent = text; }
  }

  function uploadFiles(fileList) {
    var files = Array.prototype.slice.call(fileList || []);
    if (!files.length) { return Promise.resolve(); }
    if (!canEdit()) {
      toast("you cannot put files in this folder", true);
      return Promise.resolve();
    }
    var btn = byId("uploadClipBtn");
    if (btn) { btn.disabled = true; }
    pending += files.length;
    var lastName = null;
    function next() {
      if (!files.length) {
        // The button goes back to "Upload" LAST, after the list has been
        // redrawn and the clip that just landed has been selected: it is the
        // one readable "am I still working" signal this pane has, and anything
        // waiting on it (a person, the harness, an agent) would otherwise read
        // "done" while the Source panel still described the previous clip.
        function finish() {
          if (btn) { btn.disabled = false; }
          setButtonLabel("Upload");
        }
        return refresh().then(function () {
          global.dispatchEvent(new CustomEvent("studio:library", { detail: { root: root, path: path } }));
          if (lastName && api && api.selectClipByName) { return api.selectClipByName(lastName); }
          return null;
        }).then(finish, finish);
      }
      var file = files.shift();
      var row = uploadRow(file.name);
      setButtonLabel("0%");
      return uploadOne(file, row).then(function (j) {
        pending -= 1;
        row.done();
        lastName = j.name || lastName;
        toast("uploaded " + (j.name || file.name));
        return next();
      }, function (err) {
        pending -= 1;
        row.failed(err.message || String(err));
        toast(file.name + ": " + (err.message || err), true);
        return next();
      });
    }
    return next();
  }

  /* Drag and drop from the desktop, anywhere on the page. The contract asks
     for the list, a folder row and the page as a whole, and the page as a
     whole is the superset: a drop on a folder row is handled by that row's own
     listener first (it stops propagation), and everything else lands in the
     folder on screen. Every dragover has to preventDefault or Chrome opens the
     file in the tab instead, which is the old behaviour this replaces. */
  function carriesFiles(ev) {
    var types = (ev.dataTransfer && ev.dataTransfer.types) || [];
    return Array.prototype.indexOf.call(types, "Files") >= 0;
  }

  function bindPageDrop() {
    var depth = 0;
    document.addEventListener("dragenter", function (ev) {
      if (!carriesFiles(ev) || !isLibrary()) { return; }
      depth += 1;
      var p = pane();
      if (p && canEdit()) { p.classList.add("dropping"); }
    });
    document.addEventListener("dragover", function (ev) {
      if (!carriesFiles(ev) || !isLibrary()) { return; }
      ev.preventDefault();
      if (ev.dataTransfer) { ev.dataTransfer.dropEffect = "copy"; }
    });
    document.addEventListener("dragleave", function (ev) {
      if (!carriesFiles(ev)) { return; }
      depth = Math.max(0, depth - 1);
      if (!depth) {
        var p = pane();
        if (p) { p.classList.remove("dropping"); }
      }
    });
    document.addEventListener("drop", function (ev) {
      var p = pane();
      if (p) { p.classList.remove("dropping"); }
      depth = 0;
      if (!carriesFiles(ev) || !isLibrary()) { return; }
      ev.preventDefault();
      var files = (ev.dataTransfer && ev.dataTransfer.files) || [];
      if (!files.length) { return; }
      showFilesPane();
      uploadFiles(files);
    });
  }

  // A drop somewhere else on the page has to bring the pane it landed in into
  // view, or the progress rows are written to a tab nobody is looking at.
  function showFilesPane() {
    var tab = document.querySelector('.railtab[data-rail="browse"]');
    if (tab && !tab.classList.contains("active")) { tab.click(); }
  }

  /* A folder row takes a file drop too: the file goes into THAT folder, not
     the one on screen. Wired here rather than in makeRow so the two drop
     meanings (a row being moved, a desktop file being uploaded) sit side by
     side and can be read together. */
  function bindFolderFileDrop(node, targetPath) {
    node.addEventListener("drop", function (ev) {
      if (!carriesFiles(ev)) { return; }
      ev.preventDefault();
      ev.stopPropagation();
      var p = pane();
      if (p) { p.classList.remove("dropping"); }
      var files = (ev.dataTransfer && ev.dataTransfer.files) || [];
      if (!files.length) { return; }
      var here = path;
      path = targetPath;
      uploadFiles(files).then(function () {
        // Land where the person was, not where the files went: they dropped
        // on a folder from this one, so this one is still the folder they are
        // reading.
        if (path !== here) { go(root, here); }
      });
    });
    node.addEventListener("dragover", function (ev) {
      if (!carriesFiles(ev)) { return; }
      ev.preventDefault();
      ev.stopPropagation();
      node.classList.add("dropinto");
    });
  }

  /* ---- the share dialog ---------------------------------------------------
     Who has this, what they can do with it, and how to give it to somebody
     else. The people and teams come from GET /api/people (names and ids only:
     there is nothing else in that database worth handing out). The note at the
     foot is the same two sentences the README's role table says, because a
     share dialog that does not say what the two roles mean is a share dialog
     that gets used wrong once and then not at all. */

  var shareState = { path: "", kind: "folder", name: "" };

  function shareOverlay() {
    var overlay = byId("shareOverlay");
    if (overlay) { return overlay; }
    overlay = el("div");
    overlay.id = "shareOverlay";
    var box = el("div", "sharebox");
    var head = el("div", "panelhead");
    head.id = "shareHead";
    head.textContent = "Share";
    box.appendChild(head);
    var body = el("div", "sharebody");
    body.id = "shareBody";
    box.appendChild(body);
    var foot = el("div", "sharefoot");
    var close = el("button", "btn", "Close");
    close.type = "button";
    close.id = "shareCloseBtn";
    close.addEventListener("click", closeShare);
    foot.appendChild(el("span", "spacer"));
    foot.appendChild(close);
    box.appendChild(foot);
    overlay.appendChild(box);
    overlay.addEventListener("click", function (ev) {
      if (ev.target === overlay) { closeShare(); }
    });
    document.body.appendChild(overlay);
    return overlay;
  }

  function closeShare() {
    var overlay = byId("shareOverlay");
    if (overlay) { overlay.classList.remove("on"); }
    document.removeEventListener("keydown", shareKeys, true);
  }

  function shareKeys(ev) {
    if (ev.key === "Escape") { ev.stopPropagation(); closeShare(); }
  }

  function openShare(row, kind) {
    shareState = { path: row.path, kind: kind, name: row.name };
    var overlay = shareOverlay();
    overlay.classList.add("on");
    document.addEventListener("keydown", shareKeys, true);
    byId("shareHead").textContent = "Share " + (row.name || "this");
    drawShare();
  }

  function drawShare() {
    var body = byId("shareBody");
    if (!body) { return; }
    body.innerHTML = "";
    body.appendChild(note("Loading."));
    Promise.all([
      call("/api/library/share?path=" + encodeURIComponent(shareState.path)),
      loadPeople()
    ]).then(function (both) {
      var info = both[0];
      body.innerHTML = "";

      var grants = info.grants || [];
      body.appendChild(el("div", "panelhead", grants.length ? "Who has it" : "Nobody has it yet"));
      grants.forEach(function (g) {
        var r = el("div", "sharerow");
        r.dataset.shareId = String(g.id);
        r.appendChild(el("span", "sname", g.target + (g.target_kind === "team" ? " (team)" : "")));
        var sel = document.createElement("select");
        sel.className = "sharerole";
        (people.roles || ["viewer", "editor"]).forEach(function (role) {
          var o = document.createElement("option");
          o.value = role;
          o.textContent = role;
          if (role === g.role) { o.selected = true; }
          sel.appendChild(o);
        });
        sel.addEventListener("change", function () {
          post("/api/library/share", {
            path: shareState.path, kind: shareState.kind,
            target_kind: g.target_kind, target_id: g.target_id, role: sel.value
          }).then(function () {
            toast(g.target + " is now " + sel.value);
            afterWrite();
            drawShare();
          }).catch(function (err) { toast(err.message || String(err), true); });
        });
        r.appendChild(sel);
        var rm = el("button", "faction", "Remove");
        rm.type = "button";
        rm.addEventListener("click", function () {
          post("/api/library/unshare", { share_id: g.id }).then(function () {
            toast("took " + g.target + " off " + shareState.name);
            afterWrite();
            drawShare();
          }).catch(function (err) { toast(err.message || String(err), true); });
        });
        r.appendChild(rm);
        body.appendChild(r);
      });

      (info.inherited || []).forEach(function (g) {
        body.appendChild(note(g.target + " already reaches this as " + g.role
          + " through the folder " + (g.name || g.path) + "."));
      });

      body.appendChild(el("div", "panelhead", "Give it to somebody"));
      var add = el("div", "shareadd");
      var who = document.createElement("select");
      who.id = "shareTarget";
      var options = [];
      (people.users || []).forEach(function (u) {
        if (u.me) { return; }
        options.push({ value: "user:" + u.id, label: u.name });
      });
      (people.teams || []).forEach(function (t) {
        options.push({ value: "team:" + t.id, label: t.name + " (team, " + t.members + ")" });
      });
      if (!options.length) {
        body.appendChild(note("There is nobody else in your org yet. Accounts and "
          + "teams are made from a terminal on the machine running the studio."));
      } else {
        options.forEach(function (o) {
          var opt = document.createElement("option");
          opt.value = o.value;
          opt.textContent = o.label;
          who.appendChild(opt);
        });
        add.appendChild(who);
        var role = document.createElement("select");
        role.id = "shareRole";
        (people.roles || ["viewer", "editor"]).forEach(function (r) {
          var opt = document.createElement("option");
          opt.value = r;
          opt.textContent = r;
          role.appendChild(opt);
        });
        add.appendChild(role);
        var btn = el("button", "btn", "Share");
        btn.type = "button";
        btn.id = "shareAddBtn";
        btn.addEventListener("click", function () {
          var parts = who.value.split(":");
          btn.disabled = true;
          post("/api/library/share", {
            path: shareState.path, kind: shareState.kind,
            target_kind: parts[0], target_id: Number(parts[1]), role: role.value
          }).then(function (g) {
            btn.disabled = false;
            toast("shared " + shareState.name + " with " + g.target + " as " + g.role);
            afterWrite();
            drawShare();
          }).catch(function (err) {
            btn.disabled = false;
            toast(err.message || String(err), true);
            body.appendChild(note(err.message || String(err)));
          });
        });
        add.appendChild(btn);
        body.appendChild(add);
      }

      var n = el("div", "sharenote");
      n.textContent = "A viewer can open this, look at it, scrub it and read its "
        + "history, and cannot change a grade. An editor can do all of that and "
        + "commit grades, upload into this folder, and rename and move what is "
        + "inside it. Only you can share it or throw it away. A share on a "
        + "folder covers everything under it.";
      body.appendChild(n);
    }).catch(function (err) {
      body.innerHTML = "";
      body.appendChild(note(err.message || String(err)));
    });
  }

  function loadPeople() {
    if (people.loaded) { return Promise.resolve(people); }
    return call("/api/people").then(function (j) {
      people = {
        users: j.users || [], teams: j.teams || [],
        roles: j.roles || ["viewer", "editor"], loaded: true
      };
      return people;
    }).catch(function () {
      people.loaded = true;
      return people;
    });
  }

  /* ---- toolbar ------------------------------------------------------------ */

  function syncTools() {
    var newFolder = byId("filesNewFolder");
    if (newFolder) { newFolder.disabled = !canEdit(); }
    var upload = byId("uploadClipBtn");
    if (upload) {
      upload.title = isLibrary()
        ? "Bring a video file from this computer into this folder"
        : "Bring a video file from this computer into footage";
      // In a root that is only an index of shares, or in the trash, there is
      // no folder for a file to land in.
      upload.disabled = isLibrary() && listing !== null && !canEdit();
    }
    var lay = byId("filesLayout");
    if (lay) { lay.textContent = layout === "grid" ? "List" : "Grid"; }
  }

  function bindTools() {
    var newFolder = byId("filesNewFolder");
    if (newFolder) { newFolder.addEventListener("click", beginNewFolder); }
    var lay = byId("filesLayout");
    if (lay) {
      lay.addEventListener("click", function () {
        layout = layout === "grid" ? "list" : "grid";
        store(LAYOUT_KEY, layout);
        syncTools();
        renderList();
      });
    }
    var sortSel = byId("filesSort");
    if (sortSel) {
      sortSel.value = sortBy;
      sortSel.addEventListener("change", function () {
        sortBy = sortSel.value || "name";
        store(SORT_KEY, sortBy);
        renderList();
      });
    }
    var tab = document.querySelector('.railtab[data-rail="browse"]');
    if (tab) { tab.addEventListener("click", maybeLoad); }
  }

  /* The pane's first listing is lazy, the same way the anywhere browser's
     always was: opening the studio should not cost an ffprobe of every clip in
     a folder nobody has looked at. The `ready` guard is the part worth stating:
     which roots exist at all depends on /api/auth/me, so a click that lands
     before that answer arrives records the wish and init() honours it, rather
     than latching `loadedOnce` on a root this file has not decided yet. */
  function maybeLoad() {
    if (!ready) { wantLoad = true; return; }
    if (loadedOnce) { return; }
    loadedOnce = true;
    if (isLibrary()) { return refresh(); }
    return null;
  }

  /* ---- wiring ------------------------------------------------------------- */

  function init(opts) {
    api = opts || null;
    layout = read(LAYOUT_KEY) === "grid" ? "grid" : "list";
    sortBy = read(SORT_KEY) || "name";
    bindTools();
    bindPageDrop();
    // Who we are decides which roots exist at all, so it is the first thing
    // asked and everything else waits for the answer. /api/auth/me answers the
    // same shape with logins off (the built in local admin), so there is no
    // "is auth on" branch anywhere else in this file.
    return fetch("/api/auth/me").then(function (r) { return r.json(); }).then(function (info) {
      authOn = !!(info && info.auth_required);
      me = (info && info.user) || null;
      return authOn ? loadPeople() : null;
    }).catch(function () { /* an unreachable server is app.js's message to give */ })
      .then(function () {
        var saved = read(ROOT_KEY);
        var chips = rootChips().map(function (c) { return c.id; });
        // A saved user:<id> root is a share that may since have been taken
        // away; it is not a chip, so it is accepted only when it looks like one
        // and the listing below is what actually decides.
        var start = (saved && (chips.indexOf(saved) >= 0 || saved.indexOf("user:") === 0))
          ? saved : "mine";
        root = start;
        path = read(PATH_KEY) || "";
        var p = pane();
        if (p) { p.dataset.filesroot = root; }
        renderRoots();
        syncTools();
        ready = true;
        var tab = document.querySelector('.railtab[data-rail="browse"]');
        if (wantLoad || (tab && tab.classList.contains("active"))) { return maybeLoad(); }
        return null;
      });
  }

  global.StudioFiles = {
    init: init,
    /* app.js's own upload handler calls this first. Returning true means this
       file took the files: in a library root the destination is the folder on
       screen and the progress rows are drawn here. Returning false leaves the
       old anywhere browser upload exactly as it was. */
    handleUpload: function (fileList) {
      if (!isLibrary()) { return false; }
      uploadFiles(fileList);
      return true;
    },
    clipChanged: function () { if (isLibrary() && listing) { renderList(); } },
    refresh: refresh,
    go: go,
    root: function () { return root; },
    path: function () { return path; },
    authOn: function () { return authOn; },
    pending: function () { return pending; }
  };
}(window));
