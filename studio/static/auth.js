// Client side of the studio's login (contract C2).
//
// Loaded as a blocking classic script in <head>, before every script that
// talks to the API, for one reason: it replaces window.fetch, and a wrapper
// installed after the first request has already gone out is a wrapper with a
// hole in it.
//
// Three jobs.
//
//   1. Any 401 from a /api/ URL means the session is gone (signed out in
//      another tab, expired, or the server restarted with logins on). The
//      page is then showing stale data it can no longer refresh, so this
//      does a full navigation to /login.html rather than letting every panel
//      quietly fail one by one.
//   2. window.StudioAuth: me(), logout(), createToken(). Small on purpose;
//      the rest of the app does not need to know auth exists.
//   3. The user button in the right sidebar: who you are, sign out, and
//      create an agent token. The token is shown exactly once, because the
//      server stores only a hash of it and genuinely cannot show it again.
//
// With logins off, /api/auth/me answers with the built in "local" admin and
// auth_required false; the button then stays hidden and nothing else in this
// file ever runs. That is what makes auth-off a zero change for local use.
(function (global) {
  "use strict";

  var LOGIN_URL = "/login.html";
  var navigating = false;

  // Captured before anything else can wrap it, and bound so callers that
  // pass it around do not lose `this`.
  var nativeFetch = global.fetch.bind(global);

  function urlOf(input) {
    if (typeof input === "string") { return input; }
    if (input && typeof input.url === "string") { return input.url; }   // Request
    if (input && typeof input.href === "string") { return input.href; } // URL
    return "";
  }

  function isApi(input) {
    var u = urlOf(input);
    if (u.indexOf("/api/") === 0) { return true; }
    // Absolute URLs too, but only same origin ones: a 401 from somebody
    // else's server is not this app's session.
    if (/^https?:\/\//i.test(u)) {
      try {
        var parsed = new URL(u, global.location.href);
        return parsed.origin === global.location.origin
            && parsed.pathname.indexOf("/api/") === 0;
      } catch (e) {
        return false;
      }
    }
    return false;
  }

  function toLogin() {
    if (navigating) { return; }
    navigating = true;
    if (global.StudioSession && typeof global.StudioSession.stop === "function") {
      global.StudioSession.stop();
    }
    global.location.href = LOGIN_URL;
  }

  global.fetch = function (input, init) {
    return nativeFetch(input, init).then(function (res) {
      if (res && res.status === 401 && isApi(input)) { toLogin(); }
      return res;
    });
  };

  function api(path, options) {
    // nativeFetch, not the wrapper: these calls decide for themselves what a
    // 401 means. me() on a page with logins off must not bounce anybody.
    return nativeFetch(path, options || {}).then(function (res) {
      return res.json().catch(function () { return {}; })
        .then(function (data) { return { status: res.status, data: data }; });
    });
  }

  function me() {
    return api("/api/auth/me").then(function (r) {
      return r.status === 200 ? r.data : null;
    });
  }

  function logout() {
    return api("/api/auth/logout", { method: "POST" }).then(function () {
      toLogin();
    });
  }

  function createToken(label) {
    return api("/api/auth/token", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ label: label || "agent" })
    }).then(function (r) {
      if (r.status !== 200) {
        throw new Error(r.data && r.data.error ? r.data.error
                                               : "could not create a token");
      }
      return r.data.token;
    });
  }

  global.StudioAuth = { me: me, logout: logout, createToken: createToken };

  // ---- UI ---------------------------------------------------------------

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) { n.className = cls; }
    if (text !== undefined) { n.textContent = text; }
    return n;
  }

  // login.css carries both the login page and these two widgets. It is
  // linked from here rather than from index.html because the brief for this
  // lane allows exactly one script tag and one button in that shared file,
  // and a runtime <link> costs the same one request either way.
  function ensureStyles() {
    if (document.querySelector('link[href="/login.css"]')) { return; }
    var link = document.createElement("link");
    link.rel = "stylesheet";
    link.href = "/login.css";
    document.head.appendChild(link);
  }

  function tokenDialog(token) {
    var overlay = el("div");
    overlay.id = "studioTokenOverlay";
    var box = el("div", "box");
    box.appendChild(el("h2", null, "Agent token"));
    box.appendChild(el("p", null,
      "Copy this now. The server keeps only a hash of it, so this is the "
      + "only time it can be shown. Send it as an Authorization: Bearer "
      + "header; it does not expire, and deleting it is how it is revoked."));
    var value = el("textarea");
    value.id = "studioTokenValue";
    value.readOnly = true;
    value.rows = 3;
    value.value = token;
    box.appendChild(value);
    var actions = el("div");
    actions.id = "studioTokenActions";
    var copy = el("button", "btn accent", "Copy");
    var close = el("button", "btn", "Close");
    actions.appendChild(copy);
    actions.appendChild(close);
    box.appendChild(actions);
    overlay.appendChild(box);
    document.body.appendChild(overlay);
    overlay.classList.add("on");
    value.focus();
    value.select();

    copy.addEventListener("click", function () {
      // select() plus execCommand is the fallback on purpose: the async
      // clipboard API is unavailable on a plain http:// origin in Chrome,
      // which is exactly how this tool is served.
      value.focus();
      value.select();
      var done = false;
      if (global.navigator && global.navigator.clipboard) {
        global.navigator.clipboard.writeText(token).then(function () {
          copy.textContent = "Copied";
        }, function () { /* fall through to the message below */ });
        done = true;
      }
      if (!done) {
        try {
          done = document.execCommand("copy");
        } catch (e) {
          done = false;
        }
        copy.textContent = done ? "Copied" : "Press cmd C";
      }
    });

    function dismiss() {
      overlay.remove();
      document.removeEventListener("keydown", onKey);
    }
    function onKey(ev) { if (ev.key === "Escape") { dismiss(); } }
    close.addEventListener("click", dismiss);
    overlay.addEventListener("click", function (ev) {
      if (ev.target === overlay) { dismiss(); }
    });
    document.addEventListener("keydown", onKey);
  }

  function mountUserButton(user) {
    var btn = document.getElementById("userBtn");
    if (!btn) { return; }
    ensureStyles();
    btn.hidden = false;
    btn.textContent = user.name;
    btn.title = "Signed in as " + user.name + " (" + user.role + ")";

    // The menu is built here rather than in index.html so this lane's edit to
    // that shared file stays at one button and one script tag.
    var wrap = el("span");
    wrap.id = "studioUserWrap";
    btn.parentNode.insertBefore(wrap, btn);
    wrap.appendChild(btn);

    var menu = el("div");
    menu.id = "studioUserMenu";
    var who = el("div", "who");
    who.appendChild(document.createTextNode("signed in as "));
    var strong = el("b", null, user.name);
    who.appendChild(strong);
    who.appendChild(document.createTextNode(" (" + user.role + ")"));
    menu.appendChild(who);

    var mkToken = el("button", null, "Create agent token");
    var signOut = el("button", null, "Sign out");
    menu.appendChild(mkToken);
    menu.appendChild(signOut);
    wrap.appendChild(menu);

    function close() {
      menu.classList.remove("on");
      document.removeEventListener("click", onDocClick, true);
    }
    function onDocClick(ev) {
      if (!wrap.contains(ev.target)) { close(); }
    }
    btn.addEventListener("click", function () {
      if (menu.classList.contains("on")) { close(); return; }
      menu.classList.add("on");
      document.addEventListener("click", onDocClick, true);
    });

    mkToken.addEventListener("click", function () {
      close();
      mkToken.disabled = true;
      createToken("studio ui").then(function (token) {
        mkToken.disabled = false;
        tokenDialog(token);
      }, function (err) {
        mkToken.disabled = false;
        if (typeof global.toast === "function") {
          global.toast(err.message);
        } else {
          global.alert(err.message);
        }
      });
    });

    signOut.addEventListener("click", function () {
      close();
      logout();
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    me().then(function (info) {
      // Logins off: leave the button hidden and stop. Nothing above this
      // point has changed anything the page does.
      if (!info || !info.auth_required || !info.user) { return; }
      mountUserButton(info.user);
    });
  });
}(window));
