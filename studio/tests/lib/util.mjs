/* Small helpers shared by run.mjs and the specs. No dependencies beyond the
 * node runtime: this file is included in the harness's own footprint, so it
 * stays plain rather than pulling in anything else. */

import fs from "node:fs";
import net from "node:net";
import path from "node:path";

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

/* ---------------------------------------------------------------------------
 * The harness cache, and the cap on it
 *
 * run.mjs and parity-gate.mjs point the server they start at
 * studio/tests/.cache instead of a temp folder ON PURPOSE: a cold frame cache
 * costs a real ffmpeg decode per frame (800 to 1700 ms against 1 to 2 ms
 * warm), which is what once made three specs read an answer before it
 * arrived. Nothing ever removed anything from it, so it grew to 3.4 GB of
 * decoded frames for clips and settings no run asks for any more (round 1
 * minor: a test harness that quietly eats the disk).
 *
 * Both harnesses call pruneHarnessCache() before they start a server, so the
 * folder is bounded by two documented numbers and by nothing else:
 *
 *   CACHE_MAX_AGE_DAYS  anything not touched in this many days goes, on the
 *                       grounds that a run is warm from the last few days of
 *                       runs, not from a fortnight ago
 *   CACHE_MAX_BYTES     and if what is left is still bigger than this, the
 *                       oldest files go until it fits
 *
 * Every entry in there is content addressed (a hash of the clip plus the
 * settings), so removing one costs a regeneration and can never cost a wrong
 * answer. The prune is deliberately paranoid about WHERE it deletes: it
 * refuses any path that is not itself a `studio/tests/.cache`, and it never
 * follows a symlink, so it cannot reach studio/cache, studio/data, footage or
 * anything else the founder owns even if a link inside the folder points
 * there.
 * ------------------------------------------------------------------------ */

export const CACHE_MAX_AGE_DAYS = 7;
export const CACHE_MAX_BYTES = 2 * 1024 * 1024 * 1024;   // 2 GiB

function walkCache(dir, out) {
  let names;
  try { names = fs.readdirSync(dir); } catch { return; }
  for (const name of names) {
    const p = path.join(dir, name);
    let st;
    try { st = fs.lstatSync(p); } catch { continue; }
    if (st.isSymbolicLink()) continue;     // never step out of the folder
    if (st.isDirectory()) {
      walkCache(p, out);
      out.dirs.push(p);                    // children first, so rmdir works
    } else if (st.isFile()) {
      out.files.push({ path: p, size: st.size, mtime: st.mtimeMs });
    }
  }
}

function human(bytes) {
  if (bytes >= 1024 * 1024 * 1024) return (bytes / (1024 * 1024 * 1024)).toFixed(2) + " GB";
  if (bytes >= 1024 * 1024) return (bytes / (1024 * 1024)).toFixed(1) + " MB";
  if (bytes >= 1024) return (bytes / 1024).toFixed(0) + " kB";
  return bytes + " B";
}

/* Prune `cacheDir` to the two caps above and return what it did.
 *
 * Returns { bytesBefore, bytesAfter, removed, kept, byAge, bySize, line }.
 * `line` is the one sentence a harness prints, so both harnesses say the
 * same thing in the same words. Never throws for a folder that is not there
 * yet (the first run on a fresh clone).
 */
export function pruneHarnessCache(cacheDir, opts) {
  const o = opts || {};
  const maxAgeDays = o.maxAgeDays === undefined ? CACHE_MAX_AGE_DAYS : o.maxAgeDays;
  const maxBytes = o.maxBytes === undefined ? CACHE_MAX_BYTES : o.maxBytes;
  const dir = path.resolve(cacheDir);
  const want = path.join("studio", "tests", ".cache");
  if (!dir.endsWith(path.sep + want)) {
    throw new Error("refusing to prune " + dir + ": this only ever prunes a "
      + want + " folder, and only the one the harness itself writes");
  }
  const empty = { bytesBefore: 0, bytesAfter: 0, removed: 0, kept: 0, byAge: 0, bySize: 0 };
  if (!fs.existsSync(dir)) {
    return Object.assign(empty, { line: "harness cache " + dir + " does not exist yet, nothing to prune" });
  }
  const found = { files: [], dirs: [] };
  walkCache(dir, found);
  const bytesBefore = found.files.reduce((a, f) => a + f.size, 0);

  const cutoff = Date.now() - maxAgeDays * 24 * 60 * 60 * 1000;
  const gone = new Set();
  let byAge = 0;
  for (const f of found.files) {
    if (f.mtime >= cutoff) continue;
    try { fs.rmSync(f.path, { force: true }); gone.add(f.path); byAge += f.size; } catch { /* raced */ }
  }

  let left = found.files.filter((f) => !gone.has(f.path));
  let bytesAfter = left.reduce((a, f) => a + f.size, 0);
  let bySize = 0;
  if (bytesAfter > maxBytes) {
    left.sort((a, b) => a.mtime - b.mtime);            // oldest first
    for (const f of left) {
      if (bytesAfter <= maxBytes) break;
      try {
        fs.rmSync(f.path, { force: true });
        gone.add(f.path);
        bySize += f.size;
        bytesAfter -= f.size;
      } catch { /* raced */ }
    }
  }

  // Directories the prune emptied. `found.dirs` is deepest first, and rmdir
  // on a directory that still holds something simply fails, which is exactly
  // the test we want.
  for (const d of found.dirs) {
    try { fs.rmdirSync(d); } catch { /* not empty, or gone */ }
  }

  const removed = gone.size;
  const line = "harness cache " + dir + ": " + human(bytesBefore) + " -> "
    + human(bytesAfter) + ", removed " + removed + " file(s) ("
    + human(byAge) + " older than " + maxAgeDays + " days, " + human(bySize)
    + " to fit the " + human(maxBytes) + " cap), kept "
    + (found.files.length - removed) + " file(s)";
  return {
    bytesBefore, bytesAfter, removed, kept: found.files.length - removed,
    byAge, bySize, line,
  };
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
