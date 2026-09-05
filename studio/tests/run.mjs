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
import path from "node:path";
import { fileURLToPath } from "node:url";
import { findFreePort, waitForHttp200, sleep, renderTable } from "./lib/util.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const CONTENT_DIR = path.resolve(HERE, "..", ".."); // studio/tests -> studio -> content
const PYTHON = path.join(CONTENT_DIR, ".venv", "bin", "python");
const CHROME_PATH = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const DEFAULT_VIEWPORT = { width: 1440, height: 900 };
const PORT_MIN = 20000;
const PORT_MAX = 60000;

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
];

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
  const port = await findFreePort(PORT_MIN, PORT_MAX, 40);
  const baseUrl = "http://127.0.0.1:" + port;
  console.log("[run] port " + port);

  const serverLog = { stdout: [], stderr: [] };
  const server = spawn(PYTHON, ["studio/server.py", "--port", String(port)], {
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

  try {
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
      consoleEvents,
      pageErrors,
      marks: { load: loadFence, afterLoad: afterLoadFence },
      defaultViewport: DEFAULT_VIEWPORT,
      firstClip,
      waitForBootComplete: (t) => waitForBootComplete(page, t),
      state: {},
    };

    for (const file of SPEC_FILES) {
      const mod = await import("./specs/" + file);
      const name = file.replace(/^\d+-/, "").replace(/\.mjs$/, "");
      await page.setViewport(DEFAULT_VIEWPORT); // each spec starts from the same known viewport
      let result;
      try {
        result = await mod.default(ctx);
        if (!result || !result.status) result = { status: "FAIL", evidence: "spec returned no result" };
      } catch (err) {
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
  }

  if (hardFailure) {
    console.error("[run] harness could not complete the run: " + (hardFailure.stack || hardFailure));
    console.error("[run] server stderr tail:\n" + serverLog.stderr.join("").slice(-4000));
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
