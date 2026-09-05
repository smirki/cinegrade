#!/usr/bin/env node
/* The headless Chrome half of the GPU final render.
 *
 * Spawned by studio/render_gpu.py for one render and nothing else. It opens
 * static/render.html, waits for that page to finish, and prints what happened
 * as JSON lines on stdout so the server can put it in the job log.
 *
 * puppeteer-core comes from studio/tests/node_modules, the copy the UI test
 * harness already installed. Resolving it through createRequire with that
 * directory as the base is what lets this file live in studio/tools without a
 * second node_modules tree: there is exactly one puppeteer-core in this repo.
 *
 * The page is authorised by a header, not by a URL: the render token goes in
 * Authorization on every request the browser makes, so it never lands in a
 * server log, a referrer or the page source.
 */

import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const STUDIO = path.resolve(HERE, "..");
const require = createRequire(path.join(STUDIO, "tests", "package.json"));

function arg(name, fallback) {
  const i = process.argv.indexOf("--" + name);
  return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
}

function flag(name) {
  return process.argv.includes("--" + name);
}

const base = arg("base", "http://127.0.0.1:7431");
const jobId = arg("job", "");
const chrome = arg("chrome", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome");
const token = process.env.STUDIO_RENDER_TOKEN || "";
const headful = flag("headful");

function out(obj) {
  process.stdout.write(JSON.stringify(obj) + "\n");
}

/* Chrome's default headless GL on macOS is SwiftShader, which is a software
 * rasteriser: it works and it is slow. These are the flags that get the real
 * GPU, measured rather than copied from a forum, and the same set the parity
 * gate already uses. The renderer string the page reports back says which one
 * was actually handed over, so a silent fall back to software shows up in the
 * job log instead of being reported as a GPU render. */
const GPU_ARGS = [
  "--use-angle=metal",
  "--ignore-gpu-blocklist",
  "--enable-gpu-rasterization",
  "--enable-zero-copy",
  "--disable-dev-shm-usage",
];

async function main() {
  if (!token) throw new Error("STUDIO_RENDER_TOKEN is not set");
  let puppeteer;
  try {
    puppeteer = require("puppeteer-core");
  } catch (err) {
    throw new Error("puppeteer-core is not installed under studio/tests: " + err.message);
  }

  const browser = await puppeteer.launch({
    executablePath: chrome,
    headless: !headful,
    args: GPU_ARGS,
    protocolTimeout: 900000,
  });
  let code = 0;
  try {
    const page = await browser.newPage();
    await page.setExtraHTTPHeaders({ Authorization: "Bearer " + token });
    page.on("console", (m) => {
      const text = m.text();
      if (text.startsWith("{")) process.stdout.write(text + "\n");
      else out({ log: text.slice(0, 300) });
    });
    page.on("pageerror", (e) => out({ error: String((e && e.message) || e) }));

    await page.goto(base + "/render.html?job=" + encodeURIComponent(jobId),
                    { waitUntil: "domcontentloaded", timeout: 60000 });
    await page.waitForFunction(
      () => document.body.dataset.done === "1" || document.body.dataset.done === "error",
      { timeout: 6 * 3600 * 1000, polling: 500 });
    const info = await page.evaluate(() => window.__renderInfo || {});
    out({ result: info, renderer: info.renderer || "" });
    if (info && info.error) code = 1;
    const done = await page.evaluate(() => document.body.dataset.done);
    if (done !== "1") code = 1;
  } finally {
    try { await browser.close(); } catch (e) { /* already gone */ }
  }
  process.exit(code);
}

main().catch((err) => {
  out({ error: (err && err.message) || String(err) });
  process.exit(2);
});
