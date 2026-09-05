// Keeps the open page and the server's copy of the live config in step, so an
// outside agent running `cinegrade session patch` moves the picture on screen.
//
// Split out of app.js so this can be worked on without touching the layout, and
// so the coupling is one small documented contract instead of a tangle. app.js
// only has to do two things:
//
//   window.StudioSession.publish(config, clip, time, message)   after every local change
//   window.applyExternalConfig = function (config) {}  to accept an outside one
//
// Both are optional. If app.js defines neither, this file does nothing except
// keep a long poll open, and nothing breaks.
(function (global) {
  "use strict";

  var lastRev = 0;
  // Set while an incoming change is being applied. Without it, applying an
  // outside patch would fire app.js's own change handler, which would publish
  // it straight back and bump the revision again, and the two sides would
  // ping pong forever.
  var applying = false;
  var stopped = false;
  // The last refusal this tab showed, so one refused edit is one message
  // rather than one per keystroke. Cleared by the next accepted publish.
  var lastRefusal = null;

  // One hook any other file can listen for instead of polling on its own
  // timer (contract C3, the History panel): fired on every revision this tab
  // learns about, whether that came from ITS OWN publish just below (a local
  // commit) or from the long poll further down (somebody else's commit, a
  // checkout, an undo, a redo, a fork, a rotation change). Carries the same
  // state GET /api/session already answers with (config, project, head,
  // rotation, branch, by), so a listener can read what moved without a
  // second fetch, though the History panel just uses it as a cue to refetch
  // /api/project/log for the full tree. Deliberately NOT gated on the
  // `by !== "studio"` echo filter below: that filter is about whether THIS
  // tab's picture should redraw from an outside config, which is a different
  // question from "did the project's history just grow".
  function dispatchState(state) {
    global.dispatchEvent(new CustomEvent("studio:session", { detail: state }));
  }

  function publish(config, clip, time, message) {
    if (applying || stopped) { return; }
    var body = { config: config, by: "studio" };
    if (clip !== undefined && clip !== null) { body.clip = clip; }
    if (time !== undefined && time !== null) { body.time = time; }
    // Optional: the server already generates a description for a plain edit
    // ("N changes: ..."), so only send one when the caller has something more
    // useful to say, such as app.js naming a preset load as its own commit.
    if (message) { body.message = message; }
    // replace, not merge: the page is the source of truth for its own state, so
    // a key the user cleared has to actually disappear rather than survive as a
    // leftover from the previous revision.
    body.replace = true;
    fetch("/api/session", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    }).then(function (r) { return r.json(); })
      .then(function (s) {
        if (s && s.rev) { lastRev = s.rev; lastRefusal = null; dispatchState(s); return; }
        // A session write is a COMMIT (contract E1), so somebody holding this
        // clip through a viewer grant is refused here with one plain sentence.
        // Before that rule existed this branch could only be reached by a
        // server that had gone away mid edit, and staying quiet was right;
        // now the commonest way to reach it is a refusal, and an edit that is
        // silently not being kept is the worst possible thing to be quiet
        // about. Repeats are dropped because publish() runs on every change
        // and a slider drag would otherwise be forty identical toasts.
        if (s && s.error && s.error !== lastRefusal) {
          lastRefusal = s.error;
          if (typeof global.studioToast === "function") { global.studioToast(s.error, true); }
        }
      })
      .catch(function () { /* the server going away is not worth a toast */ });
  }

  function announce(state) {
    if (typeof global.applyExternalConfig !== "function") { return; }
    applying = true;
    try {
      global.applyExternalConfig(state.config, state);
    } finally {
      // Cleared on a later turn of the event loop because app.js's own change
      // handlers may be queued rather than synchronous, and clearing too early
      // would let them echo the change back.
      setTimeout(function () { applying = false; }, 0);
    }
  }

  function poll() {
    if (stopped) { return; }
    fetch("/api/session/wait?since=" + lastRev)
      .then(function (r) {
        // 401 means the session is gone: signed out in another tab, expired, or
        // the server came back with logins on. auth.js sees the same 401 and
        // navigates to the login page; retrying here on the 2 second timer below
        // would hammer a server that has already said no, for as long as the tab
        // stays open. Stop instead and let the navigation happen.
        if (r.status === 401) { stopped = true; return null; }
        return r.json();
      })
      .then(function (state) {
        if (stopped) { return; }
        if (state && state.rev > lastRev) {
          lastRev = state.rev;
          dispatchState(state);
          // Only an edit from somewhere else should be pushed back into the
          // page. Our own publish comes back with by "studio" and applying it
          // would fight whatever the user has typed since.
          if (state.by !== "studio" && state.config) { announce(state); }
        }
        poll();
      })
      .catch(function () {
        // The long poll ends on any server restart. Retry on a slow timer
        // rather than spinning, so a stopped server does not become a busy loop.
        setTimeout(poll, 2000);
      });
  }

  global.StudioSession = {
    publish: publish,
    revision: function () { return lastRev; },
    stop: function () { stopped = true; }
  };

  poll();
}(window));
