/* Small helpers shared by run.mjs and the specs. No dependencies beyond the
 * node runtime: this file is included in the harness's own footprint, so it
 * stays plain rather than pulling in anything else. */

import net from "node:net";

export function randomPort(min, max) {
  return min + Math.floor(Math.random() * (max - min));
}

/* Tries random ports in [min, max) until one binds, rather than scanning
 * sequentially from min: several Fixxr repos run dev servers at once on this
 * machine, and a sequential scan would keep colliding with whatever else
 * already grabbed the low end of a shared range. */
export async function findFreePort(min, max, attempts) {
  for (let i = 0; i < attempts; i++) {
    const port = randomPort(min, max);
    const free = await new Promise((resolve) => {
      const srv = net.createServer();
      srv.once("error", () => resolve(false));
      srv.listen(port, "127.0.0.1", () => {
        srv.close(() => resolve(true));
      });
    });
    if (free) return port;
  }
  throw new Error("could not find a free port in [" + min + ", " + max + ") after " + attempts + " attempts");
}

export function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export async function waitForHttp200(url, timeoutMs, intervalMs) {
  const start = Date.now();
  let last = null;
  while (Date.now() - start < timeoutMs) {
    try {
      const res = await fetch(url);
      if (res.status === 200) return true;
      last = "status " + res.status;
    } catch (err) {
      last = err && err.message;
    }
    await sleep(intervalMs);
  }
  throw new Error("timed out waiting for " + url + " to return 200 (last: " + last + ")");
}

export function hasRangeError(entries) {
  return entries.some((e) => /RangeError/.test(e.text || e.message || ""));
}

function padRight(s, n) {
  s = String(s);
  return s.length >= n ? s : s + " ".repeat(n - s.length);
}

export function renderTable(rows) {
  const headers = ["SPEC", "STATUS", "EVIDENCE"];
  const nameW = Math.max(headers[0].length, ...rows.map((r) => r.name.length));
  const statusW = Math.max(headers[1].length, ...rows.map((r) => r.status.length));
  const lines = [];
  lines.push(padRight(headers[0], nameW) + "  " + padRight(headers[1], statusW) + "  " + headers[2]);
  lines.push("-".repeat(nameW) + "  " + "-".repeat(statusW) + "  " + "-".repeat(headers[2].length));
  rows.forEach((r) => {
    lines.push(padRight(r.name, nameW) + "  " + padRight(r.status, statusW) + "  " + r.evidence);
  });
  return lines.join("\n");
}
