#!/usr/bin/env node
/* Lane W2d's own verification for the upload feature. Not part of run.mjs's
 * SPEC_FILES (that suite is L7's), kept as its own file the same way L8c's
 * verify-icons-l8c.mjs is, so it never needs an edit to run.mjs. Spawns the
 * real studio/server.py on a random port, drives real headless Chrome via
 * puppeteer-core (same vendored copy studio/tests uses), and checks:
 *   - #uploadClipBtn renders inside the Clips panel.
 *   - driving the real hidden file input with elementHandle.uploadFile()
 *     (puppeteer's actual browser upload, not a mocked fetch) sends the
 *     file through POST /api/upload and the app selects the new clip:
 *     the Source panel's "file" row shows the uploaded name, the button's
 *     own label returns to "Upload", and the file is really on disk in
 *     this run's isolated footage folder (never the real content/footage).
 *   - zero console errors and zero pageerror events across the run.
 *
 * Usage: node verify-upload-w2d.mjs   (run from studio/tests, or anywhere:
 * paths below are all resolved from this file's own location).
 */

import puppeteer from "puppeteer-core";
import { spawn } from "node:child_process";
import path from "node:path";
import os from "node:os";
import { fileURLToPath } from "node:url";
import {
  existsSync, unlinkSync, copyFileSync, mkdirSync,
  mkdtempSync, readdirSync, statSync, symlinkSync, rmSync,
} from "node:fs";
import { findFreePort, waitForHttp200, sleep } from "./lib/util.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const CONTENT_DIR = path.resolve(HERE, "..", ".."); // studio/tests -> studio -> content
const PYTHON = path.join(CONTENT_DIR, ".venv", "bin", "python");
const CHROME_PATH = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const PORT_MIN = 20000;
const PORT_MAX = 60000;

// A small, real clip this lane trims from the project's own footage, so the
// upload is a genuine short video, not a synthetic file, and small enough
// that the whole run stays fast.
const SOURCE_CLIP = path.join(CONTENT_DIR, "footage", "A001_09011336_C002.MOV");
const TEST_UPLOAD_PATH = "/tmp/w2d_verify_upload.mov";
const UPLOADED_NAME = "w2d_verify_upload.mov";

function pushLog(buf, chunk) {
  buf.push(chunk.toString());
  if (buf.length > 1000) buf.shift();
}

async function waitForBootComplete(page, timeoutMs) {
  await page.waitForFunction(() => {
    var host = document.getElementById("params");
    return !!(host && host.querySelector("section.stage"));
  }, { timeout: timeoutMs || 20000 });
}

const checks = [];
function record(name, ok, evidence) {
  checks.push({ name, ok, evidence });
  console.log("[verify] " + (ok ? "PASS" : "FAIL") + " " + name + " - " + evidence);
}

async function trimTestClip() {
  await new Promise((resolve, reject) => {
    const ff = spawn("ffmpeg", ["-v", "error", "-y", "-i", SOURCE_CLIP,
      "-t", "1", "-c", "copy", TEST_UPLOAD_PATH]);
    ff.on("exit", (code) => code === 0 ? resolve() : reject(new Error("ffmpeg trim failed: " + code)));
    ff.on("error", reject);
  });
}

async function main() {
  await trimTestClip();
  const port = await findFreePort(PORT_MIN, PORT_MAX, 40);
  const baseUrl = "http://127.0.0.1:" + port;
  console.log("[verify] port " + port);

  // studio/data/studio.db and content/footage are somebody's real accounts,
  // grades and shared clip library, not test fixtures. --data-dir and
  // --footage (server.py, also STUDIO_DATA_DIR / STUDIO_FOOTAGE) are the
  // escape hatch server.py documents for this, the same one run.mjs uses: a
  // fresh temp folder for the database, and a temp folder of symlinks to the
  // real clips (never copies, so content keys still match) for the footage
  // library. This is the fix for the exact hazard this script used to carry:
  // uploading a clip with logins off used to land it straight in the real
  // content/footage, which this script then deleted again, so a re-run right
  // after a crash (or anyone reading the DB mid-run) would see a phantom
  // project. With --footage, the upload lands in this run's own temp folder.
  const dataDir = mkdtempSync(path.join(os.tmpdir(), "fixxr-studio-test-"));
  const footageDir = mkdtempSync(path.join(os.tmpdir(), "fixxr-studio-footage-"));
  const realFootage = path.join(CONTENT_DIR, "footage");
  for (const name of readdirSync(realFootage)) {
    const src = path.join(realFootage, name);
    if (name.charAt(0) === "." || !statSync(src).isFile()) continue;
    symlinkSync(src, path.join(footageDir, name));
  }
  console.log("[verify] isolated data dir " + dataDir);
  console.log("[verify] isolated footage dir " + footageDir);

  const serverLog = { stdout: [], stderr: [] };
  const server = spawn(PYTHON, ["studio/server.py", "--port", String(port),
    "--data-dir", dataDir, "--footage", footageDir], {
    cwd: CONTENT_DIR,
    stdio: ["ignore", "pipe", "pipe"],
  });
  server.stdout.on("data", (d) => pushLog(serverLog.stdout, d));
  server.stderr.on("data", (d) => pushLog(serverLog.stderr, d));

  let browser = null;
  let hardFailure = null;
  const footageDest = path.join(footageDir, UPLOADED_NAME);

  try {
    await waitForHttp200(baseUrl + "/api/state", 20000, 250);
    console.log("[verify] server is up");

    browser = await puppeteer.launch({ executablePath: CHROME_PATH, headless: true });
    const page = await browser.newPage();
    await page.setViewport({ width: 1440, height: 900 });

    const consoleEvents = [];
    const pageErrors = [];
    page.on("console", (m) => consoleEvents.push({ type: m.type(), text: m.text() }));
    page.on("pageerror", (e) => pageErrors.push(String((e && e.message) || e)));

    await page.goto(baseUrl + "/", { waitUntil: "domcontentloaded", timeout: 30000 });
    await waitForBootComplete(page, 20000);
    await sleep(500);

    // The Clips/Browse rail tab is not the default active one.
    await page.click('.railtab[data-rail="browse"]');
    await page.waitForFunction(() => {
      var host = document.getElementById("browseQuick");
      return !!(host && host.children.length);
    }, { timeout: 10000 });
    await sleep(200);

    // 1. The Upload button is really there, in the Clips panel.
    const uploadBtnInfo = await page.evaluate(() => {
      var btn = document.getElementById("uploadClipBtn");
      var input = document.getElementById("uploadClipFile");
      var pane = document.querySelector('.railpane[data-rail="browse"]');
      return {
        btnExists: !!btn,
        btnVisible: !!btn && btn.offsetParent !== null,
        btnText: btn ? btn.textContent : null,
        inputExists: !!input,
        inputAccept: input ? input.accept : null,
        inputMultiple: input ? input.multiple : null,
        inPane: !!(pane && btn && pane.contains(btn)),
      };
    });
    record(
      "Upload button renders in the Clips panel",
      uploadBtnInfo.btnExists && uploadBtnInfo.btnVisible && uploadBtnInfo.inPane
        && uploadBtnInfo.inputExists && uploadBtnInfo.inputMultiple === true,
      JSON.stringify(uploadBtnInfo)
    );

    // 2. Drive the real hidden file input with puppeteer's actual browser
    // upload (elementHandle.uploadFile), not a mocked fetch or a manually
    // dispatched change event: this exercises the same DataTransfer path a
    // real user's file picker would.
    const fileInput = await page.$("#uploadClipFile");
    await fileInput.uploadFile(TEST_UPLOAD_PATH);
    // uploadFile() on this puppeteer-core version dispatches input/change
    // on the element itself, which app.js's change listener consumes.

    // Wait for the button to show it is done (back to "Upload") rather than
    // a fixed sleep, since an upload's duration is a real network + ffprobe
    // round trip, not a constant.
    await page.waitForFunction(() => {
      var btn = document.getElementById("uploadClipBtn");
      return btn && btn.textContent === "Upload" && !btn.disabled;
    }, { timeout: 20000 });

    const afterUpload = await page.evaluate((name) => {
      var clipInfo = document.getElementById("clipInfo");
      var fileRow = clipInfo ? Array.prototype.find.call(
        clipInfo.querySelectorAll("div"),
        function (d) {
          var em = d.querySelector("em");
          return em && em.textContent === "file";
        }
      ) : null;
      var fileValue = fileRow ? fileRow.querySelector("span").textContent : null;
      return { fileValue: fileValue };
    }, UPLOADED_NAME);

    record(
      "the uploaded clip is selected after upload (Source panel shows its name)",
      afterUpload.fileValue === UPLOADED_NAME,
      "Source panel file row: " + JSON.stringify(afterUpload)
    );

    record(
      "the uploaded file actually landed on disk in the isolated footage folder",
      existsSync(footageDest),
      footageDest + (existsSync(footageDest) ? " exists" : " is missing")
    );

    // 3. Zero console errors, zero pageerrors, across the whole run.
    const consoleErrors = consoleEvents.filter((e) => e.type === "error");
    record(
      "zero console errors",
      consoleErrors.length === 0,
      consoleErrors.length + " console.error message(s)" + (consoleErrors.length ? ": " + JSON.stringify(consoleErrors.slice(0, 3)) : "")
    );
    record(
      "zero pageerrors",
      pageErrors.length === 0,
      pageErrors.length + " pageerror event(s)" + (pageErrors.length ? ": " + JSON.stringify(pageErrors.slice(0, 3)) : "")
    );
  } catch (err) {
    hardFailure = err;
  } finally {
    if (browser) {
      try { await browser.close(); } catch (e) { /* best effort */ }
    }
    if (server && server.exitCode === null && !server.killed) {
      server.kill("SIGTERM");
      await sleep(500);
      try { server.kill("SIGKILL"); } catch (e) { /* already gone */ }
    }
    // Clean up: the point of this script is proving the feature works, not
    // leaving a test artefact behind in the real footage folder.
    try { if (existsSync(footageDest)) unlinkSync(footageDest); } catch (e) { /* best effort */ }
    try { if (existsSync(TEST_UPLOAD_PATH)) unlinkSync(TEST_UPLOAD_PATH); } catch (e) { /* best effort */ }
    try { rmSync(dataDir, { recursive: true, force: true }); } catch (e) { /* best effort */ }
    try { rmSync(footageDir, { recursive: true, force: true }); } catch (e) { /* best effort */ }
  }

  if (hardFailure) {
    console.error("[verify] could not complete: " + (hardFailure.stack || hardFailure));
    console.error("[verify] server stderr tail:\n" + serverLog.stderr.join("").slice(-4000));
    process.exit(1);
  }

  console.log("");
  const failed = checks.filter((c) => !c.ok);
  console.log(checks.length + " checks: " + (checks.length - failed.length) + " passed, " + failed.length + " failed.");
  process.exit(failed.length ? 1 : 0);
}

main();
