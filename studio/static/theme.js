// Theme boot + toggle. Loaded as a blocking classic script in <head>, before any CSS,
// so the very first frame already has the right data-theme attribute (see the comment
// in index.html next to the <script> tag for why this can't be deferred).
//
// Storage key holds the string "dark" or "light". Anything else (unset, corrupted,
// an old value from a future scheme) falls back to "dark", which is the documented
// default: dark stays default even though light is fully supported.
(function () {
  "use strict";
  var KEY = "fixxr-studio-theme";

  function readStored() {
    try {
      var v = window.localStorage.getItem(KEY);
      return v === "light" ? "light" : "dark";
    } catch (e) {
      // localStorage can throw in private-browsing/sandboxed contexts; degrade to
      // the default instead of breaking page load.
      return "dark";
    }
  }

  function apply(theme) {
    var root = document.documentElement;
    if (theme === "light") {
      root.setAttribute("data-theme", "light");
    } else {
      root.removeAttribute("data-theme");
    }
    // Tells the browser which theme to use for its own UI (form control chrome,
    // default scrollbar arrows in browsers that draw them, etc.) so those match
    // ours instead of always assuming dark.
    root.style.colorScheme = theme === "light" ? "light" : "dark";
  }

  var current = readStored();
  apply(current);

  function toggle() {
    current = current === "light" ? "dark" : "light";
    apply(current);
    try {
      window.localStorage.setItem(KEY, current);
    } catch (e) {
      // Non-fatal: theme just will not persist across reloads this session.
    }
  }

  // Exposed for console/debugging use; app.js does not need to call this.
  window.fixxrTheme = { get: function () { return current; }, toggle: toggle, apply: apply };

  document.addEventListener("DOMContentLoaded", function () {
    var btn = document.getElementById("themeToggle");
    if (btn) btn.addEventListener("click", toggle);
  });
})();
