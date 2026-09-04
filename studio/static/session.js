// Keeps the open page and the server's copy of the live config in step, so an
// outside agent running `cinegrade session patch` moves the picture on screen.
//
// Split out of app.js so this can be worked on without touching the layout, and
// so the coupling is one small documented contract instead of a tangle. app.js
// only has to do two things:
//
//   window.StudioSession.publish(config, clip, time)   after every local change
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

  function publish(config, clip, time) {
    if (applying || stopped) { return; }
    var body = { config: config, by: "studio" };
    if (clip !== undefined && clip !== null) { body.clip = clip; }
    if (time !== undefined && time !== null) { body.time = time; }
    // replace, not merge: the page is the source of truth for its own state, so
    // a key the user cleared has to actually disappear rather than survive as a
    // leftover from the previous revision.
    body.replace = true;
    fetch("/api/session", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    }).then(function (r) { return r.json(); })
      .then(function (s) { if (s && s.rev) { lastRev = s.rev; } })
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
