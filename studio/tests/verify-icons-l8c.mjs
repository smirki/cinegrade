#!/usr/bin/env node
/* Lane L8c's own verification for the HugeIcons-from-the-library switch.
 * Not part of run.mjs's SPEC_FILES (that suite is L7's, this is one lane's
 * targeted check, kept as its own file so it never needs an edit to
 * run.mjs). Spawns the real studio/server.py on a random port, drives real
 * headless Chrome via puppeteer-core (same vendored copy studio/tests uses),
 * and checks:
 *   - every [data-icon] element resolves to an svg (itself, for the
 *     dynamic panels.js/app.js render() sites, or a child, for the static
 *     index.html mount() placeholders) containing at least one real shape
 *     element (path/circle/rect/...), i.e. actual data from the package,
 *     never an empty placeholder.
 *   - zero console errors and zero pageerror events across the whole run.
 *   - the parameter accordion's section headers show an icon each.
 *   - the theme toggle still flips data-theme and shows the right one of
 *     its two icons per theme.
 * Also takes the two required screenshots.
 *
 * Usage: node verify-icons-l8c.mjs   (run from studio/tests, or anywhere:
 * paths below are all resolved from this file's own location).
 */

import puppeteer from "puppeteer-core";
import { spawn } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { mkdirSync } from "node:fs";
import { findFreePort, waitForHttp200, sleep } from "./lib/util.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const CONTENT_DIR = path.resolve(HERE, "..", ".."); // studio/tests -> studio -> content
const PYTHON = path.join(CONTENT_DIR, ".venv", "bin", "python");
const CHROME_PATH = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const SHOTS_DIR = path.join(CONTENT_DIR, "studio", "shots");
const PORT_MIN = 20000;
const PORT_MAX = 60000;

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

async function main() {
  mkdirSync(SHOTS_DIR, { recursive: true });
  const port = await findFreePort(PORT_MIN, PORT_MAX, 40);
  const baseUrl = "http://127.0.0.1:" + port;
  console.log("[verify] port " + port);

  const serverLog = { stdout: [], stderr: [] };
  const server = spawn(PYTHON, ["studio/server.py", "--port", String(port)], {
    cwd: CONTENT_DIR,
    stdio: ["ignore", "pipe", "pipe"],
  });
  server.stdout.on("data", (d) => pushLog(serverLog.stdout, d));
  server.stderr.on("data", (d) => pushLog(serverLog.stderr, d));

  let browser = null;
  let hardFailure = null;

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

    // The Clips/Browse rail tab is not the default active one (Refs is), and
    // its folder/video/quick-jump icons only render on first open
    // (openInitialBrowseDir in app.js). Click it now so the icon audit below
    // covers those dynamic sites too, not just what boot() renders eagerly.
    await page.click('.railtab[data-rail="browse"]');
    await page.waitForFunction(() => {
      var host = document.getElementById("browseQuick");
      return !!(host && host.children.length);
    }, { timeout: 10000 });
    await sleep(200);

    // 1. Every [data-icon] element resolved to a real, non-empty icon.
    const iconAudit = await page.evaluate(() => {
      var shapeTags = ["path", "circle", "rect", "line", "polygon", "polyline", "ellipse"];
      var els = Array.prototype.slice.call(document.querySelectorAll("[data-icon]"));
      var report = els.map(function (el) {
        var svg = el.tagName.toLowerCase() === "svg" ? el : el.querySelector("svg");
        var shapeCount = 0;
        if (svg) {
          shapeTags.forEach(function (tag) { shapeCount += svg.getElementsByTagName(tag).length; });
        }
        return { name: el.getAttribute("data-icon"), tag: el.tagName.toLowerCase(), hasSvg: !!svg, shapeCount: shapeCount };
      });
      return report;
    });
    const missing = iconAudit.filter((r) => !r.hasSvg || r.shapeCount < 1);
    record(
      "every data-icon element has real shape data",
      iconAudit.length > 0 && missing.length === 0,
      iconAudit.length + " data-icon element(s) found, " + missing.length + " missing/empty ("
        + (missing.length ? JSON.stringify(missing) : "none") + "); names: "
        + Array.from(new Set(iconAudit.map((r) => r.name))).sort().join(", ")
    );

    // 2. Panel section headers each show an icon (.stagehead-icon).
    const stageheadAudit = await page.evaluate(() => {
      var heads = Array.prototype.slice.call(document.querySelectorAll(".stagehead-icon"));
      return heads.map(function (svg) { return svg.querySelectorAll("path,circle,rect").length; });
    });
    record(
      "stage header icons rendered",
      stageheadAudit.length > 0 && stageheadAudit.every((n) => n > 0),
      stageheadAudit.length + " .stagehead-icon element(s), shape counts: " + JSON.stringify(stageheadAudit)
    );

    // 3. Theme toggle: click, confirm data-theme flips and the right icon shows.
    const themeBefore = await page.evaluate(() => ({
      theme: document.documentElement.getAttribute("data-theme") || "dark",
      sunVisible: getComputedStyle(document.querySelector("#themeToggle .sun")).display !== "none",
      moonVisible: getComputedStyle(document.querySelector("#themeToggle .moon")).display !== "none",
    }));
    await page.screenshot({ path: path.join(SHOTS_DIR, "icons-library-dark.png") });
    await page.click("#themeToggle");
    await sleep(150);
    const themeAfter = await page.evaluate(() => ({
      theme: document.documentElement.getAttribute("data-theme") || "dark",
      sunVisible: getComputedStyle(document.querySelector("#themeToggle .sun")).display !== "none",
      moonVisible: getComputedStyle(document.querySelector("#themeToggle .moon")).display !== "none",
    }));
    await page.screenshot({ path: path.join(SHOTS_DIR, "icons-library-light.png") });
    // Restore original theme so a re-run of the official suite right after this
    // one finds the default state.
    await page.click("#themeToggle");
    await sleep(150);

    const themeFlipped = themeBefore.theme !== themeAfter.theme;
    const rightIconPerTheme =
      (themeBefore.theme === "dark" && themeBefore.sunVisible && !themeBefore.moonVisible) &&
      (themeAfter.theme === "light" && !themeAfter.sunVisible && themeAfter.moonVisible);
    record(
      "theme toggle flips and shows the right icon",
      themeFlipped && rightIconPerTheme,
      "before " + JSON.stringify(themeBefore) + ", after " + JSON.stringify(themeAfter)
    );

    // 4. Zero console errors, zero pageerrors, across the whole run.
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
