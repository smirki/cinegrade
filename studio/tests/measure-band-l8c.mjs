#!/usr/bin/env node
/* Lane L8c's re-measurement of the "band" the stalled L8b lane also owned:
 * the gap between the bottom of the last grid card (#timeline) and the
 * bottom of its scrollport (.gridcol > .grid-stack, see the widget-scroll
 * spec and the long computeCellHeight comment in app.js), at three window
 * sizes. Not part of run.mjs's SPEC_FILES, kept as its own file for the
 * same reason as verify-icons-l8c.mjs.
 *
 * Usage: node measure-band-l8c.mjs
 */

import puppeteer from "puppeteer-core";
import { spawn } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { findFreePort, waitForHttp200, sleep } from "./lib/util.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const CONTENT_DIR = path.resolve(HERE, "..", "..");
const PYTHON = path.join(CONTENT_DIR, ".venv", "bin", "python");
const CHROME_PATH = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const PORT_MIN = 20000;
const PORT_MAX = 60000;

const SIZES = [
  { width: 1280, height: 720 },
  { width: 1440, height: 900 },
  { width: 2560, height: 1200 },
];

async function waitForBootComplete(page, timeoutMs) {
  await page.waitForFunction(() => {
    var host = document.getElementById("params");
    return !!(host && host.querySelector("section.stage"));
  }, { timeout: timeoutMs || 20000 });
}

function pushLog(buf, chunk) {
  buf.push(chunk.toString());
  if (buf.length > 1000) buf.shift();
}

async function measureAt(page, size) {
  await page.setViewport(size);
  // Let the resize handler (initGrid's window "resize" listener, app.js)
  // recompute cellHeight and re-lay-out the grid before reading anything.
  await sleep(400);
  return page.evaluate(() => {
    var grid = document.querySelector(".gridcol > .grid-stack");
    var last = document.querySelector('#gridMid > .grid-stack-item[gs-id="timeline"]');
    if (!grid || !last) return { error: "grid or last card (#timeline) not found" };
    var gridRect = grid.getBoundingClientRect();
    var lastRect = last.getBoundingClientRect();
    return {
      scrollHeight: grid.scrollHeight,
      clientHeight: grid.clientHeight,
      needsScroll: grid.scrollHeight > grid.clientHeight,
      scrollportBottom: gridRect.bottom,
      lastCardBottom: lastRect.bottom,
      band: gridRect.bottom - lastRect.bottom,
    };
  });
}

async function main() {
  const port = await findFreePort(PORT_MIN, PORT_MAX, 40);
  const baseUrl = "http://127.0.0.1:" + port;
  console.log("[measure] port " + port);

  const serverLog = { stdout: [], stderr: [] };
  const server = spawn(PYTHON, ["studio/server.py", "--port", String(port)], {
    cwd: CONTENT_DIR,
    stdio: ["ignore", "pipe", "pipe"],
  });
  server.stdout.on("data", (d) => pushLog(serverLog.stdout, d));
  server.stderr.on("data", (d) => pushLog(serverLog.stderr, d));

  let browser = null;
  let hardFailure = null;
  const results = [];

  try {
    await waitForHttp200(baseUrl + "/api/state", 20000, 250);
    browser = await puppeteer.launch({ executablePath: CHROME_PATH, headless: true });
    const page = await browser.newPage();
    await page.setViewport(SIZES[0]);

    await page.goto(baseUrl + "/", { waitUntil: "domcontentloaded", timeout: 30000 });
    await waitForBootComplete(page, 20000);
    await sleep(500);

    for (const size of SIZES) {
      const r = await measureAt(page, size);
      results.push({ size, r });
      console.log("[measure] " + size.width + "x" + size.height + ": " + JSON.stringify(r));
    }
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
    console.error("[measure] could not complete: " + (hardFailure.stack || hardFailure));
    console.error("[measure] server stderr tail:\n" + serverLog.stderr.join("").slice(-4000));
    process.exit(1);
  }

  console.log("");
  console.log("SIZE        BAND(px)  NEEDS_SCROLL  SCROLLPORT_BOTTOM  LAST_CARD_BOTTOM");
  results.forEach(({ size, r }) => {
    if (r.error) {
      console.log(size.width + "x" + size.height + "  ERROR: " + r.error);
      return;
    }
    console.log(
      (size.width + "x" + size.height).padEnd(11) + " " +
      String(Math.round(r.band)).padStart(8) + "  " +
      String(r.needsScroll).padEnd(12) + "  " +
      String(Math.round(r.scrollportBottom)).padEnd(17) + "  " +
      String(Math.round(r.lastCardBottom))
    );
  });
}

main();
