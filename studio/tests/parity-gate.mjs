// Wave gate: run the parity harness headless and print the counts.
import puppeteer from "puppeteer-core";
import { spawn } from "node:child_process";
const CONTENT = "/Users/smirk/Programming/Fixxr-Agent-Workspace/content";
const port = 20000 + Math.floor(Math.random() * 40000);
const srv = spawn(`${CONTENT}/.venv/bin/python`, ["studio/server.py", "--port", String(port)], { cwd: CONTENT, stdio: "ignore" });
const base = `http://127.0.0.1:${port}`;
for (let i = 0; i < 60; i++) { try { const r = await fetch(base + "/api/state"); if (r.ok) break; } catch {} await new Promise(r => setTimeout(r, 500)); }
const browser = await puppeteer.launch({ executablePath: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", headless: true, args: ["--use-angle=metal", "--ignore-gpu-blocklist"] });
const page = await browser.newPage();
const errors = [];
page.on("pageerror", e => errors.push(String(e)));
await page.goto(base + "/parity.html?auto=1", { waitUntil: "domcontentloaded" });
const t0 = Date.now();
await page.waitForFunction(() => document.body.dataset.done === "1" || document.body.dataset.done === "error", { timeout: 600000, polling: 1000 });
const done = await page.evaluate(() => document.body.dataset.done);
const prog = await page.evaluate(() => document.getElementById("prog").textContent);
console.log("done:", done, "in", Math.round((Date.now() - t0) / 1000) + "s");
console.log("prog:", prog);
console.log("pageerrors:", errors.length, errors.slice(0, 3));
await browser.close(); srv.kill();
const rep = await (await fetch(base + "/api/parity/report").catch(() => null))?.json?.().catch(() => null);
process.exit(done === "1" ? 0 : 1);
