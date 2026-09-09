#!/usr/bin/env node
/* Fixxr Studio UI test harness.
 *
 * Spawns the real studio/server.py on a random local port, drives the real
 * static front end in a real (headless) Chrome through puppeteer-core, and
 * runs every spec in studio/tests/specs against that one live session. No
 * mocking: every check is a real DOM read after a real fetch, click, drag
 * or wheel event went through the actual browser input pipeline.
 *
 * Usage: npm test   (from studio/tests), or node run.mjs directly.
 */

import puppeteer from "puppeteer-core";
import { spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { pickPort, waitForHttp200, sleep, renderTable, pruneHarnessCache } from "./lib/util.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const CONTENT_DIR = path.resolve(HERE, "..", ".."); // studio/tests -> studio -> content
const PYTHON = path.join(CONTENT_DIR, ".venv", "bin", "python");
const CHROME_PATH = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const DEFAULT_VIEWPORT = { width: 1440, height: 900 };
// Round 1 finding 20: this harness picked freely from 20000-60000 with no
// exclusions at all, and that range contains two ports a real server was
// found on. The list of ports nothing here may ever bind lives in ONE file,
// studio/tests/forbidden-ports.json, read by both python suites
// (studio/tests/py/ports.py, grade/tests/ports.py) and, through
// lib/util.mjs's pickPort(), by every node harness here. Round 2 finding 58
// moved the picker itself into lib/util.mjs so parity-gate.mjs shares it
// rather than keeping its own unguarded one liner.

// Lane M7's own addition: the real SAM masking service (contract C3), always
// started in --stub mode (no weights, synthetic drifting ellipses, the
// project's own uv-managed interpreter) so the masks specs drive the real
// studio/server.py -> sam/server.py round trip rather than a fake standing
// in for either half. --no-model-lock skips the machine-wide
// /tmp/fixxr-sam-model.lock (a test-only flag sam/server.py documents for
// exactly this): the founder's own real SAM service, if one happens to be
// running, is never touched or blocked by this harness. --stub-delay-ms
// gives a track real wall-clock time per frame, the same reason M7's Python
// suite uses it, so a spec polling queued/running actually has a window to
// observe those states rather than always landing on "done" first read.
const SAM_PYTHON = path.join(CONTENT_DIR, "sam", ".venv", "bin", "python");
const SAM_SERVER = path.join(CONTENT_DIR, "sam", "server.py");
// The panel never restricts a track's own start/end (design: a person names
// a selection and it tracks the whole clip), so the delay times the WHOLE
// fixture clip's frame count, not a slice of it: kept small so a real
// queued/running/done cycle stays observable without a spec waiting tens of
// seconds for it.
const SAM_STUB_DELAY_MS = "60";

const SPEC_FILES = [
  "01-input-probe.mjs",
  "02-boot.mjs",
  "03-panels.mjs",
  "04-sidebar-toggles.mjs",
  "05-sidebar-drag.mjs",
  "06-widget-scroll.mjs",
  "07-scopes-row.mjs",
  "08-gpu-preview.mjs",
  "09-undo.mjs",
  "10-theme.mjs",
  "11-reload-clean.mjs",
  "12-per-clip-grades.mjs",
  "13-window-editor.mjs",
  "14-proxy-playback.mjs",
  "15-gpu-render.mjs",
  "16-layers.mjs",
  "17-slice.mjs",
  "18-history.mjs",
  "19-reload-state.mjs",
  "20-presets.mjs",
  "21-match-pick.mjs",
  "22-mobile.mjs",
  "23-files.mjs",
  "24-playback-render.mjs",
  "25-viewer-zoom-frames.mjs",
  /* 26 was reserved for the timeline arc while the two arcs ran side by side
   * (M6/M7's own briefs both said so, which is why masks starts at 27); the
   * timeline branch filled it, and this list is the merge of the two. It runs
   * before the masks specs simply because it is numbered before them: it
   * needs no SAM service and leaves the playhead wherever spec 25 left it. */
  "26-timeline.mjs",
  "27-masks-panel.mjs",
  "28-masks-pick-and-track.mjs",
  "29-masks-fixture-states.mjs",
  /* 30 is acceptance A1's GPU half (a tracked matte moving in the graded
   * picture and in playback) and 31 wires in studio/tests/mask-stack-ref.mjs,
   * the mask arithmetic reference, which until now no runner invoked at all.
   *
   * 30 runs AFTER 29 on purpose even though 29 stops the SAM stub: it needs no
   * SAM service, only a matte on disk and the GPU, and 29's own note says
   * nothing numbered after it needs the stub alive. */
  "30-gpu-matte-time.mjs",
  "31-mask-stack-ref.mjs",
  /* 32 is A1's other half: the FILE a GPU final render writes. It runs last
   * because it is the most expensive spec here (a real render through the
   * headless worker) and because it needs nothing any later spec sets up. */
  "32-gpu-render-matte-frame.mjs",
];

/* Chasing one failing spec through a whole run costs minutes of GPU work, so
 * SPECS=12,23 narrows the run to those numbered specs. Unset (every normal run
 * and anything anyone calls a result) means the full list above, in order.
 * A narrowed run is a debugging aid only: several specs read state an earlier
 * one set up, so a subset can pass or fail differently from the real run. */
const ONLY = (process.env.SPECS || "").split(",").map((s) => s.trim()).filter(Boolean);
const SPECS_TO_RUN = ONLY.length
  ? SPEC_FILES.filter((f) => ONLY.some((n) => f.indexOf(n.length < 2 ? "0" + n : n) === 0))
  : SPEC_FILES;

/* Each spec starts from the same known right sidebar tab, the same way it
 * starts from the same known viewport.
 *
 * The tab is remembered in localStorage (fixxr-studio-paramtab, sidebars.js)
 * and painted on <html> before first paint, so it survives every reload for
 * the rest of the run. 18-history leaves it on History by design (that is
 * what it is testing), and every later spec that clicks a control inside
 * #params then finds that pane display: none and puppeteer refuses the click
 * with "Node is either not clickable or not an Element". Resetting it here,
 * once per spec, is the same kind of leak guard as the viewport reset rather
 * than a change to any spec's own behaviour. */
async function resetParamTab(page) {
  try {
    await page.evaluate(() => {
      try { window.localStorage.setItem("fixxr-studio-paramtab", "grade"); } catch (e) { /* private mode */ }
      document.documentElement.setAttribute("data-paramtab", "grade");
    });
  } catch (err) { /* no page yet, or navigating: the next spec resets it too */ }
}

/* The one place every spec waits for "the app finished its first boot":
 * Panels.build() only runs after boot()'s /api/state fetch resolves, and it
 * is the last synchronous step before selectClip() kicks off the async
 * frame/thumbnail fetches, so a section.stage existing inside #params is a
 * reliable, cheap signal that boot got at least that far. Used for the
 * initial load and for every later reload (sidebar-drag, reload-clean). */
export async function waitForBootComplete(page, timeoutMs) {
  await page.waitForFunction(() => {
    var host = document.getElementById("params");
    return !!(host && host.querySelector("section.stage"));
  }, { timeout: timeoutMs || 20000 });
}

/* Registered once, before the first navigation, with evaluateOnNewDocument
 * so it re-installs itself on every future navigation in this page's life
 * (a reload included) and, being a window-capture listener, sees every one
 * of these event types before any in-page script gets a chance to call
 * stopPropagation on it. This is the harness's answer to the open question
 * in the plan: does synthetic input from puppeteer's own page.mouse API
 * (as opposed to someone's hand-rolled raw CDP calls) actually reach page
 * JavaScript in this headless Chrome. */
function installInputProbe(page) {
  return page.evaluateOnNewDocument(() => {
    window.__inputProbe = { mousedown: 0, mouseup: 0, mousemove: 0, wheel: 0, pointerdown: 0, pointerup: 0 };
    Object.keys(window.__inputProbe).forEach(function (type) {
      window.addEventListener(type, function () {
        window.__inputProbe[type] += 1;
      }, { capture: true, passive: true });
    });
  });
}

function pushLog(buf, chunk) {
  buf.push(chunk.toString());
  if (buf.length > 1000) buf.shift();
}

/* Per clip grades (contract C3) are real, persistent, per account data in
 * studio/data/studio.db, and this harness drives the real server, so it is
 * writing into whatever the person running it has actually graded.
 *
 * That cuts both ways and both of them matter. A spec that changes the config
 * now SAVES it, so the next run boots with the previous run's leftovers and a
 * spec that asserts a starting value fails for a reason that is nowhere in its
 * own code (measured: window.enabled left true by an earlier run made the
 * window-editor spec fail on its first assertion). And a run that simply
 * scribbles over somebody's grades is not an acceptable price for a test.
 *
 * So the run takes the grades away before the first page load and puts them
 * back at the end: every spec starts from "no clip has a grade", and the
 * account's own work is exactly where it was. Addressing by clip_key rather
 * than by name on the way back is deliberate: a grade can exist for a clip
 * that is not in the current footage folder, and the key is what identifies
 * it either way. */
async function takeGradesAside(baseUrl) {
  const saved = [];
  let rows = [];
  try {
    const list = await fetch(baseUrl + "/api/grades").then((r) => r.json());
    rows = (list && list.grades) || [];
  } catch (err) {
    return saved;                       // no grade API: nothing to protect
  }
  for (const row of rows) {
    const q = "?clip=" + encodeURIComponent(row.clip_key);
    try {
      const one = await fetch(baseUrl + "/api/grade" + q).then((r) => r.json());
      if (one && one.exists) {
        saved.push({ key: row.clip_key, name: row.clip_name, config: one.config });
      }
      await fetch(baseUrl + "/api/grade" + q, { method: "DELETE" });
    } catch (err) { /* leave that one alone rather than half clearing it */ }
  }
  return saved;
}

async function putGradesBack(baseUrl, saved) {
  // Clear first: the run itself will have saved grades of its own (a spec
  // that clicks a control now writes one), and leaving those behind would
  // mean "restored" quietly meant "restored, plus whatever the tests did".
  await takeGradesAside(baseUrl);
  for (const g of saved) {
    try {
      await fetch(baseUrl + "/api/grade", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ clip: g.key, clip_name: g.name, config: g.config }),
      });
    } catch (err) { /* best effort: the server may already be gone */ }
  }
}

async function main() {
  const port = await pickPort();
  const baseUrl = "http://127.0.0.1:" + port;
  console.log("[run] port " + port);

  // The SAM masking service (contract C3), real, --stub, its own data dir,
  // started before studio/server.py so --sam-url points at something
  // already listening rather than something studio has to retry into
  // existing. pickPort is called again rather than reused: the two
  // servers must never be told to share one port, and a second independent
  // call (which binds and releases before returning) is how every other
  // free port in this harness is chosen too. pickPort also refuses every
  // port on the shared forbidden list above (the founder's live studio, the
  // SAM service's own default, the reserved pair, the agent seat's studio
  // and the two ports a real server was found squatting inside this very
  // 20000-60000 range), so neither server here can land on one.
  const samPort = await pickPort();
  const samBase = "http://127.0.0.1:" + samPort;
  const samDataDir = fs.mkdtempSync(path.join(os.tmpdir(), "fixxr-sam-test-"));
  console.log("[run] SAM stub port " + samPort + ", data dir " + samDataDir);
  const samLog = { stdout: [], stderr: [] };
  const samServer = spawn(SAM_PYTHON, [SAM_SERVER, "--port", String(samPort),
    "--data-dir", samDataDir, "--stub", "--no-model-lock",
    "--stub-delay-ms", SAM_STUB_DELAY_MS], {
    cwd: CONTENT_DIR,
    stdio: ["ignore", "pipe", "pipe"],
  });
  samServer.stdout.on("data", (d) => pushLog(samLog.stdout, d));
  samServer.stderr.on("data", (d) => pushLog(samLog.stderr, d));
  samServer.on("error", (e) => pushLog(samLog.stderr, "spawn error: " + e.message + "\n"));

  // studio/data/studio.db and studio/data/users/<id>/presets are somebody's
  // real accounts, grades and presets, not test fixtures. --data-dir (server.py,
  // also STUDIO_DATA_DIR) is exactly the escape hatch server.py documents for
  // this: a fresh temp folder per run, so every spec's Overwrite, Save as,
  // grade autosave and project commit lands there instead of the real
  // studio/data. grade/presets/ (the shipped library) and footage/ are not
  // affected by --data-dir and stay the real, shared, read-mostly ones, which
  // is what a spec needs to read real library presets.
  const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), "fixxr-studio-test-"));
  console.log("[run] isolated data dir " + dataDir);

  // footage/ was shared too, and that was a hole: the harness runs with
  // logins off, and with logins off the library's own root IS
  // content/footage, so a spec that uploads a clip or makes a folder wrote
  // into the founder's real footage. --footage (server.py, also
  // STUDIO_FOOTAGE) points the run at a temp folder instead. It holds
  // SYMLINKS to the real clips, never copies: the specs match on content
  // keys, which are a hash of the file's own bytes, so the links have to
  // lead to the very same files. Removing the folder afterwards removes the
  // links and never what they point at.
  const footageDir = fs.mkdtempSync(path.join(os.tmpdir(), "fixxr-studio-footage-"));
  const realFootage = path.join(CONTENT_DIR, "footage");
  for (const name of fs.readdirSync(realFootage)) {
    const src = path.join(realFootage, name);
    if (name.charAt(0) === "." || !fs.statSync(src).isFile()) continue;
    fs.symlinkSync(src, path.join(footageDir, name));
  }
  console.log("[run] isolated footage dir " + footageDir
    + " (" + fs.readdirSync(footageDir).length + " links to real clips)");

  // The cache is the one thing that must NOT be fresh per run. --data-dir
  // makes the server's default cache <data-dir>/cache, so a temp data dir
  // means a cold frame, proxy and segment cache and every spec pays a real
  // ffmpeg decode for frames it measured a minute ago; measured server side,
  // 800 to 1700 ms cold against 1 to 2 ms warm, which is what made three
  // specs read their answer before it arrived. This is a stable folder that
  // belongs to the harness alone (gitignored), so repeat runs are warm again
  // and the founder's studio/cache is still never touched.
  // Kept between runs, but not for ever: pruneHarnessCache() bounds it by age
  // and then by size before the server starts (lib/util.mjs documents both
  // numbers, studio/README.md says what the folder is and how to empty it by
  // hand). Nothing else on the disk is touched, and a pruned entry costs a
  // regeneration, never a wrong answer, because every name in there is a hash
  // of the clip plus the settings that made it.
  const cacheDir = path.join(HERE, ".cache");
  fs.mkdirSync(cacheDir, { recursive: true });
  console.log("[run] " + pruneHarnessCache(cacheDir).line);

  const serverLog = { stdout: [], stderr: [] };
  const server = spawn(PYTHON, ["studio/server.py", "--port", String(port),
    "--data-dir", dataDir, "--footage", footageDir, "--cache-dir", cacheDir,
    "--sam-url", samBase], {
    cwd: CONTENT_DIR,
    stdio: ["ignore", "pipe", "pipe"],
  });
  server.stdout.on("data", (d) => pushLog(serverLog.stdout, d));
  server.stderr.on("data", (d) => pushLog(serverLog.stderr, d));
  server.on("error", (e) => pushLog(serverLog.stderr, "spawn error: " + e.message + "\n"));

  let browser = null;
  const rows = [];
  let hardFailure = null;
  let savedGrades = [];
  let samStopped = false;

  // A spec proving the service-down badge (contract C4's 503, the start
  // command it carries) needs the real SAM stub to actually stop answering,
  // not a mock of "down". Exposed on ctx rather than reaching for the
  // process directly, so a spec kills it by the one handle this file
  // captured at spawn, same discipline as every other kill in this harness.
  // Idempotent and safe to call again from the finally block below: PID
  // only, never a name pattern, and only ever this one process.
  async function stopSam() {
    if (samStopped) return;
    samStopped = true;
    if (samServer && samServer.exitCode === null && !samServer.killed) {
      samServer.kill("SIGTERM");
      await sleep(500);
      try { samServer.kill("SIGKILL"); } catch (e) { /* already gone */ }
    }
  }

  try {
    console.log("[run] waiting for " + samBase + "/health ...");
    await waitForHttp200(samBase + "/health", 20000, 250);
    console.log("[run] SAM stub is up");

    console.log("[run] waiting for " + baseUrl + "/api/state ...");
    await waitForHttp200(baseUrl + "/api/state", 20000, 250);
    console.log("[run] server is up");

    savedGrades = await takeGradesAside(baseUrl);
    console.log("[run] set aside " + savedGrades.length + " saved grade(s) for the duration of this run");

    browser = await puppeteer.launch({
      executablePath: CHROME_PATH,
      headless: true, // "new" headless: this puppeteer-core removed the old 'shell' default
    });
    const page = await browser.newPage();
    await page.setViewport(DEFAULT_VIEWPORT);

    const consoleEvents = [];
    const pageErrors = [];
    page.on("console", (m) => consoleEvents.push({ type: m.type(), text: m.text() }));
    page.on("pageerror", (e) => pageErrors.push({ message: String((e && e.message) || e) }));

    await installInputProbe(page);

    console.log("[run] loading " + baseUrl + " ...");
    const loadFence = { console: consoleEvents.length, pageErrors: pageErrors.length };
    await page.goto(baseUrl + "/", { waitUntil: "domcontentloaded", timeout: 30000 });
    // Not networkidle0/2: /api/session/wait is a deliberate long poll and
    // /api/jobs repolls every few seconds, so the network is never idle for
    // the 500ms networkidle0 wants and that wait strategy simply times out.
    await waitForBootComplete(page, 20000);
    await sleep(500); // let the first async render settle before fencing "load errors"
    const afterLoadFence = { console: consoleEvents.length, pageErrors: pageErrors.length };

    const clipsRes = await fetch(baseUrl + "/api/clips").then((r) => r.json());
    const firstClip = (clipsRes.clips && clipsRes.clips[0] && clipsRes.clips[0].name) || null;

    const ctx = {
      browser,
      page,
      baseUrl,
      port,
      dataDir,
      consoleEvents,
      pageErrors,
      marks: { load: loadFence, afterLoad: afterLoadFence },
      defaultViewport: DEFAULT_VIEWPORT,
      firstClip,
      waitForBootComplete: (t) => waitForBootComplete(page, t),
      state: {},
      samBase,
      samPort,
      samDataDir,
      stopSam,
    };

    for (const file of SPECS_TO_RUN) {
      const mod = await import("./specs/" + file);
      const name = file.replace(/^\d+-/, "").replace(/\.mjs$/, "");
      await page.setViewport(DEFAULT_VIEWPORT); // each spec starts from the same known viewport
      await resetParamTab(page);                // ... and the same known sidebar tab
      let result;
      try {
        result = await mod.default(ctx);
        if (!result || !result.status) result = { status: "FAIL", evidence: "spec returned no result" };
      } catch (err) {
        // The table only has room for the message, and "Node is either not
        // clickable or not an Element" is the same message wherever it came
        // from, so the whole stack goes to the log as well: without it,
        // finding which of a spec's forty clicks threw means bisecting a run.
        console.error("[run] " + name + " threw:\n" + (err && err.stack ? err.stack : String(err)));
        result = { status: "FAIL", evidence: "threw: " + (err && err.stack ? err.stack.split("\n")[0] : String(err)) };
      }
      rows.push({ name, status: result.status, evidence: result.evidence || "" });
      console.log("[run] " + name + ": " + result.status + " - " + (result.evidence || ""));
    }
  } catch (err) {
    hardFailure = err;
  } finally {
    // Order matters: close the browser FIRST. grades.js flushes a pending
    // autosave from a beforeunload handler with fetch keepalive, so a tab
    // torn down after the restore could put a test's grade back on top of the
    // real one. Then restore, while the server is still alive to PUT to.
    if (browser) {
      try { await browser.close(); } catch (e) { /* best effort */ }
    }
    await putGradesBack(baseUrl, savedGrades);
    if (server && server.exitCode === null && !server.killed) {
      server.kill("SIGTERM");
      await sleep(500);
      try { server.kill("SIGKILL"); } catch (e) { /* already gone */ }
    }
    try { fs.rmSync(dataDir, { recursive: true, force: true }); } catch (e) { /* best effort */ }
    try { fs.rmSync(footageDir, { recursive: true, force: true }); } catch (e) { /* best effort */ }
    // stopSam() is idempotent: a spec that already killed it for the
    // service-down check (samStopped set) makes this a no-op, and any other
    // ending (a hard failure before that spec ran, an early throw) still
    // gets the SAM stub killed by the one PID captured at spawn.
    await stopSam();
    try { fs.rmSync(samDataDir, { recursive: true, force: true }); } catch (e) { /* best effort */ }
  }

  if (hardFailure) {
    console.error("[run] harness could not complete the run: " + (hardFailure.stack || hardFailure));
    console.error("[run] server stderr tail:\n" + serverLog.stderr.join("").slice(-4000));
    console.error("[run] SAM stub stderr tail:\n" + samLog.stderr.join("").slice(-4000));
    process.exit(1);
  }

  console.log("");
  console.log(renderTable(rows));
  const failed = rows.filter((r) => r.status === "FAIL");
  const skipped = rows.filter((r) => r.status === "SKIP");
  const passed = rows.filter((r) => r.status === "PASS");
  console.log("");
  console.log(rows.length + " specs: " + passed.length + " passed, " + failed.length + " failed, " + skipped.length + " skipped.");
  process.exit(failed.length ? 1 : 0);
}

main();
