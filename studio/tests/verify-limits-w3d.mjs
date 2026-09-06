#!/usr/bin/env node
/* Lane W3d one-off verification: boots a private studio server, opens the
 * Limits panel in real headless Chrome through puppeteer-core, and checks
 * the honesty list end to end: zero page/console errors, every entry in
 * every array actually rendered, the parity sentence filled from the real
 * report, no em or en dash in any entry text, plus a length census (entries
 * per array, total words) and one full page screenshot.
 *
 * Not part of npm test: this is a one-shot check for this lane's own
 * report, run directly with `node verify-limits-w3d.mjs` from studio/tests.
 * Uses the same launch pattern as run.mjs (puppeteer-core, real Chrome,
 * random port) but does not touch the shared spec files or the shared grade
 * data protection dance, since it never changes a grade.
 */
import puppeteer from "puppeteer-core";
import { spawn } from "node:child_process";
import path from "node:path";
import fs from "node:fs";
import os from "node:os";
import { fileURLToPath } from "node:url";
import { findFreePort, waitForHttp200, sleep } from "./lib/util.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const CONTENT_DIR = path.resolve(HERE, "..", "..");
const PYTHON = path.join(CONTENT_DIR, ".venv", "bin", "python");
const CHROME_PATH = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const SHOT_PATH = path.join(CONTENT_DIR, "studio", "shots", "w3d-limits.png");
const PARITY_JSON = path.join(CONTENT_DIR, "studio", "tools", "parity-results.json");

async function main() {
  const port = await findFreePort(20000, 60000, 40);
  const baseUrl = "http://127.0.0.1:" + port;
  console.log("[verify] port " + port);

  // studio/data/studio.db and content/footage are somebody's real accounts,
  // grades and shared clip library, not test fixtures. --data-dir and
  // --footage (server.py, also STUDIO_DATA_DIR / STUDIO_FOOTAGE) are the
  // escape hatch server.py documents for this, the same one run.mjs uses: a
  // fresh temp folder for the database, and a temp folder of symlinks to the
  // real clips (never copies, so content keys still match) for the footage
  // library, so this run never reads or writes the real ones.
  const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), "fixxr-studio-test-"));
  const footageDir = fs.mkdtempSync(path.join(os.tmpdir(), "fixxr-studio-footage-"));
  const realFootage = path.join(CONTENT_DIR, "footage");
  for (const name of fs.readdirSync(realFootage)) {
    const src = path.join(realFootage, name);
    if (name.charAt(0) === "." || !fs.statSync(src).isFile()) continue;
    fs.symlinkSync(src, path.join(footageDir, name));
  }
  console.log("[verify] isolated data dir " + dataDir);
  console.log("[verify] isolated footage dir " + footageDir);

  const server = spawn(PYTHON, ["studio/server.py", "--port", String(port),
    "--data-dir", dataDir, "--footage", footageDir], {
    cwd: CONTENT_DIR,
    stdio: ["ignore", "pipe", "pipe"],
  });
  const serverErr = [];
  server.stderr.on("data", (d) => serverErr.push(d.toString()));
  console.log("[verify] spawned server pid " + server.pid);

  let browser = null;
  let exitCode = 0;

  try {
    await waitForHttp200(baseUrl + "/api/state", 20000, 250);
    console.log("[verify] server is up");

    browser = await puppeteer.launch({ executablePath: CHROME_PATH, headless: true });
    const page = await browser.newPage();
    await page.setViewport({ width: 1440, height: 1200 });

    const consoleErrors = [];
    const pageErrors = [];
    page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(m.text()); });
    page.on("pageerror", (e) => pageErrors.push(String((e && e.message) || e)));

    await page.goto(baseUrl + "/", { waitUntil: "domcontentloaded", timeout: 30000 });
    await page.waitForFunction(() => {
      var host = document.getElementById("params");
      return !!(host && host.querySelector("section.stage"));
    }, { timeout: 20000 });
    await sleep(400);

    const preClickErrors = pageErrors.length + consoleErrors.length;

    await page.click("#limitsBtn");
    // The panel HTML is set synchronously in the click handler; the parity
    // span is filled a tick later by refreshParityClaim's own fetch.
    await page.waitForFunction(() => {
      var el = document.getElementById("parityClaim");
      return !!(el && el.textContent && el.textContent.length > 0);
    }, { timeout: 10000 });
    await sleep(200);

    const result = await page.evaluate(() => {
      var body = document.getElementById("limitsBody");
      var lis = Array.prototype.slice.call(body.querySelectorAll("li"));
      var text = body.textContent || "";
      var words = text.trim().split(/\s+/).filter(Boolean).length;
      var parityClaim = document.getElementById("parityClaim");
      // Section counts follow document order: h4 headings partition the <ul>s.
      var uls = Array.prototype.slice.call(body.querySelectorAll("ul"));
      var counts = uls.map(function (ul) { return ul.querySelectorAll("li").length; });
      return {
        liCount: lis.length,
        totalWords: words,
        parityText: parityClaim ? parityClaim.textContent : null,
        sectionCounts: counts,
        bodyLength: text.length,
      };
    });

    const entryTexts = await page.evaluate(() => {
      var body = document.getElementById("limitsBody");
      return Array.prototype.slice.call(body.querySelectorAll("li")).map(function (li) { return li.textContent; });
    });

    await page.screenshot({ path: SHOT_PATH, fullPage: true });
    console.log("[verify] screenshot written to " + SHOT_PATH);

    const postClickErrors = pageErrors.length + consoleErrors.length;

    // ---- checks ----
    console.log("");
    console.log("[verify] page errors: " + pageErrors.length + ", console.error: " + consoleErrors.length);
    if (pageErrors.length) console.log("  first pageerror: " + pageErrors[0]);
    if (consoleErrors.length) console.log("  first console.error: " + consoleErrors[0]);

    console.log("[verify] li count in #limitsBody: " + result.liCount);
    console.log("[verify] ul section counts (doc order): " + JSON.stringify(result.sectionCounts));
    console.log("[verify] total words in #limitsBody: " + result.totalWords + " (chars: " + result.bodyLength + ")");
    console.log("[verify] #parityClaim text: " + result.parityText);

    let parityTruth = null;
    try {
      parityTruth = JSON.parse(fs.readFileSync(PARITY_JSON, "utf8"));
    } catch (err) {
      console.log("[verify] could not read parity-results.json: " + err.message);
    }
    if (parityTruth) {
      const c = parityTruth.counts || {};
      const expectSubstr = c.EXACT + " EXACT, " + c.CLOSE + " CLOSE, " + c.FAILED + " FAILED";
      const matchesCounts = (result.parityText || "").indexOf(expectSubstr) !== -1;
      const matchesTotal = (result.parityText || "").indexOf(String(parityTruth.runCount) + " comparisons") !== -1;
      console.log("[verify] parity-results.json counts: " + JSON.stringify(c) + " of " + parityTruth.runCount);
      console.log("[verify] parityClaim contains counts substring: " + matchesCounts);
      console.log("[verify] parityClaim contains runCount substring: " + matchesTotal);
      if (!matchesCounts || !matchesTotal) exitCode = 1;
    }

    const dashRe = /[–—]/;
    const dashHits = entryTexts.filter((t) => dashRe.test(t));
    console.log("[verify] entries containing an em or en dash: " + dashHits.length);
    if (dashHits.length) {
      exitCode = 1;
      console.log("  first offending text: " + dashHits[0].slice(0, 200));
    }

    if (pageErrors.length || consoleErrors.length) exitCode = 1;
    console.log("");
    console.log("[verify] pre-click errors: " + preClickErrors + ", post-click errors: " + postClickErrors);
  } catch (err) {
    console.error("[verify] failed: " + (err && err.stack ? err.stack : err));
    exitCode = 1;
  } finally {
    if (browser) {
      try { await browser.close(); } catch (e) { /* best effort */ }
    }
    if (server && server.exitCode === null && !server.killed) {
      console.log("[verify] killing server pid " + server.pid);
      process.kill(server.pid, "SIGTERM");
      await sleep(400);
      try { process.kill(server.pid, "SIGKILL"); } catch (e) { /* already gone */ }
    }
    if (serverErr.length && exitCode) {
      console.log("[verify] server stderr tail:\n" + serverErr.join("").slice(-2000));
    }
    try { fs.rmSync(dataDir, { recursive: true, force: true }); } catch (e) { /* best effort */ }
    try { fs.rmSync(footageDir, { recursive: true, force: true }); } catch (e) { /* best effort */ }
  }
  process.exit(exitCode);
}

main();
