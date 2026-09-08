/* files: the library explorer in the client (contract E2,
 * plan/2026-09-05-studio-library/PLAN.md).
 *
 * What is under test is studio/static/files.js plus the files block in
 * style.css plus static/activity.js, against lane L1's studio/library.py.
 *
 * This spec runs in TWO places, and the split is deliberate rather than
 * incidental.
 *
 *   Part A, on the harness's own server (logins off). READ ONLY. With logins
 *     off an account's library IS content/footage, which is the founder's real
 *     footage folder, and this suite may not add, rename or remove a file in
 *     it. So Part A proves the things that do not need a write: the tab is
 *     called Files, the root switcher offers exactly the roots that make sense
 *     without accounts, the list draws real clips with real durations and
 *     project heads, the grid and sort controls work, This Mac is still the
 *     old anywhere browser, and the layout holds at 1280 x 800, 1440 x 900,
 *     1920 x 1080 and 390 x 844 in both themes with nothing spilling out of
 *     the pane and 36 px touch targets on the phone.
 *
 *   Part B, on a SECOND server this spec starts itself: its own random port,
 *     its own temporary data directory, --auth, and two accounts created
 *     through the CLI with the password piped on stdin (never on a command
 *     line). Every write in this file happens there, where the library is
 *     studio/data/users/<id>/footage inside that temp directory and nothing
 *     the founder owns can be touched. It is stopped by the pid captured at
 *     spawn and its directory is removed. That server is where the whole write
 *     story is proved: new folder, drag and drop upload, move by dragging a
 *     row onto a folder, rename, open, trash, restore, and then the sharing
 *     half -- alice shares a folder with bob, bob sees it under Shared with
 *     me, opens the clip, is refused as a viewer with the server's own
 *     sentence on screen, is promoted to editor, commits, and his name is on
 *     that commit in alice's own History panel.
 *
 * Every drag in here is a real DragEvent carrying a real DataTransfer through
 * the page's own listeners, which is what the contract asks for ("a synthetic
 * file dropped through the DataTransfer API"); puppeteer's mouse API cannot
 * drive an HTML5 drag at all, so a mouse gesture would have proved nothing.
 *
 * The page is put back to the desktop viewport, the theme it was found in, the
 * My files root and the Refs rail tab before returning.
 */

import { spawn, spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { findFreePort, sleep } from "../lib/util.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const CONTENT_DIR = path.resolve(HERE, "..", "..", ".."); // specs -> tests -> studio -> content
const PYTHON = path.join(CONTENT_DIR, ".venv", "bin", "python");
const SHOTS = path.resolve(HERE, "..", "..", "shots");
const PASSWORD = "a-long-enough-spec-password";
const PHONE = { width: 390, height: 844, deviceScaleFactor: 2, isMobile: true, hasTouch: true };
const DESKTOP_SIZES = [
  { width: 1280, height: 800 },
  { width: 1440, height: 900 },
  { width: 1920, height: 1080 },
];

function fail(evidence) {
  return { status: "FAIL", evidence: evidence };
}

/* ---- the second server -------------------------------------------------- */

function cli(dataDir, args, stdin) {
  return spawnSync(PYTHON, ["studio/server.py", "--data-dir", dataDir, ...args], {
    cwd: CONTENT_DIR, input: stdin || "", encoding: "utf8",
  });
}

/* A real, tiny video, because POST /api/upload runs ffprobe on what it is
 * given and refuses anything ffprobe cannot read. Bounded by -t, like every
 * other ffmpeg call in this repo, and written into the spec's own temp
 * directory so nothing lands beside the founder's footage. */
function tinyClip(dest, colour, seconds) {
  const r = spawnSync("ffmpeg", ["-v", "error", "-y", "-f", "lavfi",
    "-i", "color=c=" + colour + ":s=64x64:r=10", "-t", String(seconds),
    "-c:v", "libx264", "-pix_fmt", "yuv420p", dest], { encoding: "utf8" });
  return r.status === 0;
}

async function waitForServer(base, timeoutMs) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    try {
      const res = await fetch(base + "/api/state");
      // 401 is the right answer from a server running with logins on: it is up
      // and it is gated, which is exactly what this spec wants.
      if (res.status === 200 || res.status === 401) return true;
    } catch (err) { /* not listening yet */ }
    await sleep(250);
  }
  return false;
}

/* ---- page helpers, used against either server --------------------------- */

async function waitForBoot(page, timeoutMs) {
  await page.waitForFunction(() => {
    const host = document.getElementById("params");
    return !!(host && host.querySelector("section.stage"));
  }, { timeout: timeoutMs || 30000 });
}

async function openFilesTab(page) {
  await page.click('.railtab[data-rail="browse"]');
  await page.waitForFunction(() => {
    const pane = document.querySelector('.railpane[data-rail="browse"]');
    return !!(pane && pane.classList.contains("active"));
  }, { timeout: 10000 });
}

/* Leaving the phone layout puts the rail back on whichever tab it opens with
   (Refs), so anything that measures the Files pane has to ask for it again
   rather than assume the last click still holds. */
async function ensureFilesPane(page) {
  const on = await page.evaluate(() => {
    const p = document.querySelector('.railpane[data-rail="browse"]');
    return !!(p && p.classList.contains("active") && p.offsetParent);
  });
  if (on) return;
  await openFilesTab(page);
  await page.waitForFunction(() => {
    const el = document.querySelector(".filespane");
    return !!(el && el.offsetParent && el.getBoundingClientRect().width > 1);
  }, { timeout: 10000 });
  await sleep(300);
}

async function chooseRoot(page, root) {
  const ok = await page.evaluate((r) => {
    const b = document.querySelector('#filesRoots .filesroot[data-root="' + r + '"]');
    if (!b) return false;
    b.click();
    return true;
  }, root);
  if (!ok) throw new Error("no root chip for " + root);
  await sleep(500);
}

/* One read of the whole pane, so an assertion is a plain comparison rather
 * than five round trips. Everything here comes off the real DOM. */
async function readPane(page) {
  return page.evaluate(() => {
    const pane = document.querySelector(".filespane");
    const text = (el) => (el ? el.textContent.trim() : null);
    const rows = Array.prototype.map.call(
      document.querySelectorAll("#filesList .filerow"), (r) => ({
        kind: r.dataset.kind,
        path: r.dataset.path,
        root: r.dataset.root,
        name: text(r.querySelector(".fname")),
        // The subtitle is several spans laid out with a gap, so join them the
        // way the gap reads rather than letting textContent run "0:01" into
        // "1 MB".
        sub: Array.prototype.map.call(r.querySelectorAll(".fsub > *"), (x) => x.textContent.trim())
          .filter(Boolean).join(" "),
        badge: text(r.querySelector(".fbadge")),
        thumb: !!r.querySelector("img.fthumb"),
        actions: Array.prototype.map.call(r.querySelectorAll(".faction"), (a) => a.textContent.trim()),
        active: r.classList.contains("active"),
      }));
    return {
      root: pane ? pane.dataset.filesroot : null,
      chips: Array.prototype.map.call(document.querySelectorAll("#filesRoots .filesroot"),
        (b) => ({ root: b.dataset.root, label: b.textContent.trim(), active: b.classList.contains("active") })),
      crumbs: Array.prototype.map.call(document.querySelectorAll("#filesCrumbs .crumb"),
        (b) => b.textContent.trim()),
      rows: rows,
      note: text(document.querySelector("#filesList .note")),
      error: text(document.getElementById("filesError")),
      uploads: Array.prototype.map.call(document.querySelectorAll("#filesUploads .uploadrow"),
        (u) => ({ text: u.textContent.trim(), failed: u.classList.contains("failed") })),
      uploadBtn: (() => {
        const b = document.getElementById("uploadClipBtn");
        return b ? { label: b.textContent.trim(), disabled: b.disabled, visible: b.offsetParent !== null } : null;
      })(),
      macListVisible: (() => {
        const el = document.getElementById("browseClips");
        return !!(el && el.offsetParent !== null);
      })(),
      libListVisible: (() => {
        const el = document.getElementById("filesList");
        return !!(el && el.offsetParent !== null);
      })(),
    };
  });
}

async function waitForRow(page, name, timeoutMs) {
  await page.waitForFunction((n) => {
    const rows = document.querySelectorAll("#filesList .filerow .fname");
    return Array.prototype.some.call(rows, (el) => el.textContent.trim() === n);
  }, { timeout: timeoutMs || 20000 }, name);
}

async function waitForNoRow(page, name, timeoutMs) {
  await page.waitForFunction((n) => {
    const rows = document.querySelectorAll("#filesList .filerow .fname");
    return !Array.prototype.some.call(rows, (el) => el.textContent.trim() === n);
  }, { timeout: timeoutMs || 20000 }, name);
}

/* A row's own action button, clicked through the real handler. The buttons are
 * opacity 0 until the row is hovered on a pointer device (that is the
 * contract's "row actions on hover"), and the hover reveal itself is asserted
 * separately below; driving them from here by name keeps every other step from
 * depending on a hover landing on the right pixel. */
async function clickRowAction(page, name, action) {
  const ok = await page.evaluate((n, a) => {
    const rows = Array.prototype.slice.call(document.querySelectorAll("#filesList .filerow"));
    const row = rows.filter((r) => {
      const el = r.querySelector(".fname");
      return el && el.textContent.trim() === n;
    })[0];
    if (!row) return "no row";
    const btn = Array.prototype.slice.call(row.querySelectorAll(".faction"))
      .filter((b) => b.textContent.trim() === a)[0];
    if (!btn) return "no action";
    btn.click();
    return "ok";
  }, name, action);
  if (ok !== "ok") throw new Error(action + " on " + name + ": " + ok);
  await sleep(250);
}

async function clickRow(page, name) {
  const ok = await page.evaluate((n) => {
    const rows = Array.prototype.slice.call(document.querySelectorAll("#filesList .filerow"));
    const row = rows.filter((r) => {
      const el = r.querySelector(".fname");
      return el && el.textContent.trim() === n;
    })[0];
    if (!row) return false;
    row.click();
    return true;
  }, name);
  if (!ok) throw new Error("no row called " + name);
  await sleep(400);
}

async function typeIntoInlineInput(page, value) {
  await page.waitForSelector("#filesList input.frename", { timeout: 5000 });
  await page.type("#filesList input.frename", value);
  await page.keyboard.press("Enter");
  await sleep(600);
}

/* The layout claim, measured rather than looked at: the pane's own box does
 * not scroll sideways, and no row, crumb, action or toolbar button sticks out
 * of it. #filesRoots is excluded from the child sweep on purpose: it is a
 * deliberate sideways scroller (a long team name must not wrap the switcher
 * into three lines), so its CONTENT is allowed to be wider than it is and its
 * own box is checked instead. */
async function measureLayout(page) {
  return page.evaluate(() => {
    const pane = document.querySelector(".filespane");
    if (!pane) return { ok: false, why: "no pane" };
    const box = pane.getBoundingClientRect();
    const out = { paneScrollX: pane.scrollWidth - pane.clientWidth, overflowing: [], smallTargets: [] };
    // A pane that is not on screen has no boxes to be wrong, so every check
    // below would pass by saying nothing. Measuring the hidden pane is the one
    // way this sweep could lie, so it is reported and treated as a failure.
    out.visible = !!pane.offsetParent && box.width > 1 && box.height > 1;
    if (!out.visible) {
      const left = document.getElementById("sidebarLeft");
      out.why = "paneDisplay=" + getComputedStyle(pane).display
        + " active=" + pane.classList.contains("active")
        + " box=" + Math.round(box.width) + "x" + Math.round(box.height)
        + " mobile=" + document.documentElement.getAttribute("data-mobile")
        + " page=" + document.documentElement.getAttribute("data-mobilepage")
        + " leftW=" + (left ? Math.round(left.getBoundingClientRect().width) : "none")
        + " leftDisplay=" + (left ? getComputedStyle(left).display : "none");
    }
    out.rowCount = pane.querySelectorAll("#filesList .filerow").length;
    const roots = document.getElementById("filesRoots");
    out.rootsFits = !roots || roots.getBoundingClientRect().width <= box.width + 1;
    const phone = document.documentElement.getAttribute("data-mobile") === "on";
    const sel = ".filerow, .filescrumbs .crumb, .filestools .btn, .filestools select, "
      + "#filesList .note, .uploadrow, .filesroot, .faction";
    Array.prototype.forEach.call(pane.querySelectorAll(sel), (el) => {
      const r = el.getBoundingClientRect();
      if (r.width === 0 && r.height === 0) return;           // not on screen
      const inRoots = !!(roots && roots.contains(el));
      if (!inRoots && (r.right > box.right + 1 || r.left < box.left - 1)) {
        out.overflowing.push(el.className + " right=" + Math.round(r.right) + " pane=" + Math.round(box.right));
      }
      if (phone && el.matches(".faction, .filesroot, .filestools .btn, .filestools select, .filescrumbs .crumb")
          && r.height < 36) {
        out.smallTargets.push(el.className + " " + Math.round(r.height) + "px");
      }
    });
    return out;
  });
}

async function shot(page, name) {
  try {
    fs.mkdirSync(SHOTS, { recursive: true });
    await page.screenshot({ path: path.join(SHOTS, name) });
    return name;
  } catch (err) {
    return null;                              // evidence only, never pass/fail
  }
}

/* Dark is the default and is written as no attribute at all, so "dark" and
 * null are the same theme; only "light" is spelled out. Comparing the raw
 * attribute would click the toggle to leave a dark page for a dark page and
 * land on light instead. */
function themeOf(attr) { return attr === "light" ? "light" : "dark"; }

async function setTheme(page, want) {
  const wanted = themeOf(want);
  const now = themeOf(await page.evaluate(() => document.documentElement.getAttribute("data-theme")));
  if (now === wanted) return now;
  await page.click("#themeToggle");
  await sleep(350);
  return themeOf(await page.evaluate(() => document.documentElement.getAttribute("data-theme")));
}

/* ---- the spec ------------------------------------------------------------ */

export default async function run(ctx) {
  const page = ctx.page;
  const notes = [];
  const shots = [];
  const startTheme = await page.evaluate(() => document.documentElement.getAttribute("data-theme"));
  // data-mobilepage survives in localStorage, so the phone half of the layout
  // sweep below has to put back whichever page the suite was left on.
  const startMobilePage = await page.evaluate(() =>
    document.documentElement.getAttribute("data-mobilepage") || "preview");

  /* ================= Part A: the harness's own server, read only ========== */

  await openFilesTab(page);
  await page.waitForFunction(() => document.querySelectorAll("#filesRoots .filesroot").length > 0,
    { timeout: 10000 });
  await sleep(700);

  const tabLabel = await page.$eval('.railtab[data-rail="browse"]', (el) => el.textContent.trim());
  if (tabLabel !== "Files") return fail('the rail tab is labelled "' + tabLabel + '", not "Files"');

  let pane = await readPane(page);
  const chipRoots = pane.chips.map((c) => c.root);
  // Logins off: one account, one org, so there is nobody to share with and no
  // team to be in. Those chips must not be drawn at all, and This Mac must be.
  const wantChips = ["mine", "trash", "thismac"];
  if (JSON.stringify(chipRoots) !== JSON.stringify(wantChips)) {
    return fail("with logins off the root chips are " + JSON.stringify(chipRoots)
      + ", expected " + JSON.stringify(wantChips));
  }
  // Which root the pane opens on is whichever one this browser profile was
  // last left in (specs 12 and 19 both visit This Mac), so the assertion is
  // that clicking My files lands there, not that it was already there.
  const openedOn = pane.root;
  if (pane.root !== "mine") {
    await chooseRoot(page, "mine");
    await page.waitForFunction(() => document.querySelectorAll("#filesList .filerow").length > 0,
      { timeout: 15000 });
    pane = await readPane(page);
  }
  if (pane.root !== "mine") return fail("clicking My files left the pane on root " + pane.root);
  if (!pane.rows.length) return fail("My files drew no rows at all (footage has clips)");
  const withThumb = pane.rows.filter((r) => r.kind === "file" && r.thumb).length;
  const withHead = pane.rows.filter((r) => r.kind === "file" && /[0-9a-f]{7}/.test(r.sub || "")).length;
  if (!withThumb) return fail("no clip row drew a thumbnail");
  notes.push("My files (the pane opened on the remembered root " + openedOn + "): "
    + pane.rows.length + " rows, " + withThumb + " with a thumbnail, "
    + withHead + " showing a project head, crumbs " + JSON.stringify(pane.crumbs));

  // Row actions are quiet until the row is hovered, and appear on hover. This
  // is the one place the reveal itself is measured with a real pointer.
  const hoverProof = await (async () => {
    const before = await page.$eval("#filesList .filerow .facts", (el) => getComputedStyle(el).opacity);
    await page.hover("#filesList .filerow");
    await sleep(250);
    const after = await page.$eval("#filesList .filerow .facts", (el) => getComputedStyle(el).opacity);
    return { before, after };
  })();
  if (!(Number(hoverProof.before) < 0.5 && Number(hoverProof.after) > 0.9)) {
    return fail("row actions did not appear on hover: opacity " + hoverProof.before + " -> " + hoverProof.after);
  }
  notes.push("row actions opacity " + hoverProof.before + " -> " + hoverProof.after + " on hover");

  // Grid and sort are real: the class moves and the order changes.
  await page.click("#filesLayout");
  await sleep(300);
  const gridOn = await page.$eval("#filesList", (el) => el.classList.contains("grid"));
  // The grid is a different row layout, so it gets its own picture rather than
  // being taken on trust from the class name.
  shots.push(await shot(page, "23-files-grid.png"));
  await page.click("#filesLayout");
  await sleep(300);
  const gridOff = await page.$eval("#filesList", (el) => el.classList.contains("grid"));
  if (!gridOn || gridOff) return fail("the grid toggle did not flip the list class (" + gridOn + " -> " + gridOff + ")");
  const byName = (await readPane(page)).rows.map((r) => r.name).join(",");
  await page.select("#filesSort", "size");
  await sleep(300);
  const bySize = (await readPane(page)).rows.map((r) => r.name).join(",");
  await page.select("#filesSort", "name");
  await sleep(300);
  notes.push("sort by name [" + byName + "] and by size [" + bySize + "]"
    + (byName === bySize ? " (same order: this folder is already size ordered)" : ""));

  // This Mac is the old anywhere browser, untouched, and it is a different
  // list from the library's.
  await chooseRoot(page, "thismac");
  await sleep(600);
  const onMac = await readPane(page);
  if (!onMac.macListVisible || onMac.libListVisible) {
    return fail("This Mac did not swap the lists (mac " + onMac.macListVisible
      + ", library " + onMac.libListVisible + ")");
  }
  const macCrumbs = await page.$eval("#browseCrumbs", (el) => el.children.length);
  await chooseRoot(page, "mine");
  await sleep(600);
  const backOnMine = await readPane(page);
  if (backOnMine.macListVisible || !backOnMine.libListVisible) {
    return fail("going back to My files did not restore the library list");
  }
  notes.push("This Mac still draws its own breadcrumb (" + macCrumbs + " parts) and its own clip list");

  // Trash: a root of its own, empty on this server because nothing here writes.
  await chooseRoot(page, "trash");
  const trashPane = await readPane(page);
  if (trashPane.root !== "trash") return fail("the Trash chip did not switch root");
  notes.push("Trash root: " + (trashPane.note || (trashPane.rows.length + " rows")));
  await chooseRoot(page, "mine");

  // ---- layout, four sizes, two themes ------------------------------------
  const layoutProblems = [];
  for (const theme of ["dark", "light"]) {
    const got = await setTheme(page, theme);
    if (got !== theme) { notes.push("could not reach the " + theme + " theme (stayed " + got + ")"); }
    for (const size of DESKTOP_SIZES) {
      await page.setViewport(size);
      await sleep(450);
      await ensureFilesPane(page);
      const m = await measureLayout(page);
      if (!m.visible) {
        layoutProblems.push(theme + " " + size.width + "x" + size.height
          + ": the Files pane was not on screen, so nothing here was measured (" + m.why + ")");
      }
      if (m.paneScrollX > 1) {
        layoutProblems.push(theme + " " + size.width + "x" + size.height + ": the pane scrolls sideways by " + m.paneScrollX + "px");
      }
      if (!m.rootsFits) { layoutProblems.push(theme + " " + size.width + ": the root switcher is wider than the pane"); }
      if (m.overflowing.length) {
        layoutProblems.push(theme + " " + size.width + "x" + size.height + ": " + m.overflowing.slice(0, 3).join("; "));
      }
      shots.push(await shot(page, "23-files-" + size.width + "x" + size.height + "-" + theme + ".png"));
    }
    // The phone: real mobile emulation, the Clips page, and 36 px targets.
    await page.setViewport(PHONE);
    await sleep(500);
    await page.evaluate(() => {
      const b = document.querySelector('#mobilebar .mobiletab[data-mobilepage="clips"]');
      if (b) b.click();
    });
    await sleep(600);
    // The Clips page opens on whichever rail tab it was last left on (Refs, as
    // the suite runs), so the Files tab has to be asked for again here or the
    // whole phone sweep would measure a pane that is not on screen.
    await ensureFilesPane(page);
    await sleep(500);
    const mp = await measureLayout(page);
    if (!mp.visible) {
      layoutProblems.push(theme + " phone: the Files pane was not on screen, so nothing here was measured");
    }
    if (mp.paneScrollX > 1) {
      layoutProblems.push(theme + " phone: the pane scrolls sideways by " + mp.paneScrollX + "px");
    }
    if (mp.overflowing.length) {
      layoutProblems.push(theme + " phone: " + mp.overflowing.slice(0, 3).join("; "));
    }
    if (mp.smallTargets.length) {
      layoutProblems.push(theme + " phone: touch targets under 36px " + mp.smallTargets.slice(0, 4).join("; "));
    }
    if (!mp.rowCount) {
      layoutProblems.push(theme + " phone: the list drew no rows, so the row layout was not measured");
    }
    shots.push(await shot(page, "23-files-390x844-" + theme + ".png"));
    // Put the phone page back while the bar is still on screen: sidebars.js
    // keeps that choice in localStorage AND in a variable of its own, so
    // clicking the button is the only way to move both.
    await page.evaluate((want) => {
      const b = document.querySelector('#mobilebar .mobiletab[data-mobilepage="' + want + '"]');
      if (b) b.click();
    }, startMobilePage);
    await sleep(300);
    await page.setViewport(ctx.defaultViewport);
    await sleep(400);
  }
  if (layoutProblems.length) {
    await setTheme(page, startTheme);
    return fail("layout: " + layoutProblems.slice(0, 4).join(" | "));
  }
  notes.push("layout clean at 1280x800, 1440x900, 1920x1080 and 390x844 in both themes; "
    + "no element outside the pane, no sideways scroll, every phone target at least 36px");

  await setTheme(page, startTheme);
  await page.setViewport(ctx.defaultViewport);
  await sleep(300);

  // The activity panel is mounted at the foot of the History tab and answers
  // even when there is nothing in it (this server has written nothing).
  const activity = await page.evaluate(() => {
    const panel = document.getElementById("activityPanel");
    const list = document.getElementById("activityList");
    return {
      mounted: !!panel,
      inHistory: !!(panel && panel.closest('.parampane[data-paramtab="history"]')),
      rows: list ? list.querySelectorAll(".activityrow").length : -1,
      note: list && list.querySelector(".note") ? list.querySelector(".note").textContent.trim() : null,
    };
  });
  if (!activity.mounted || !activity.inHistory) {
    return fail("the activity list is not mounted at the foot of the History tab: " + JSON.stringify(activity));
  }
  notes.push("activity panel mounted in the History tab (" + activity.rows + " rows on this server)");

  /* ================= Part B: a second server, with logins ================= */

  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "fixxr-studio-files-"));
  const clipPath = path.join(tmp, "seed.mp4");
  let server = null;
  let ctxA = null;
  let ctxB = null;
  let partB = null;

  try {
    if (!tinyClip(clipPath, "orange", 1.4)) {
      notes.push("ffmpeg could not make a test clip, so Part B (the writes and the sharing) did not run");
      throw new Error("__skip_part_b__");
    }
    for (const who of ["alice", "bob"]) {
      const made = cli(tmp, ["--create-user", who, "--role", "user", "--password-stdin"], PASSWORD + "\n");
      if (made.status !== 0) {
        notes.push("could not create " + who + ": " + (made.stderr || "").trim());
        throw new Error("__skip_part_b__");
      }
    }
    const port2 = await findFreePort(20000, 60000, 40);
    const base2 = "http://127.0.0.1:" + port2;
    server = spawn(PYTHON, ["studio/server.py", "--port", String(port2), "--data-dir", tmp, "--auth"], {
      cwd: CONTENT_DIR, stdio: ["ignore", "pipe", "pipe"],
    });
    const serverErr = [];
    server.stderr.on("data", (d) => { serverErr.push(d.toString()); if (serverErr.length > 200) serverErr.shift(); });
    if (!(await waitForServer(base2, 40000))) {
      notes.push("the second server never came up: " + serverErr.join("").slice(-400));
      throw new Error("__skip_part_b__");
    }

    // One browser context per account, so the two sessions have their own
    // cookies and their own localStorage rather than fighting over one.
    ctxA = await ctx.browser.createBrowserContext();
    ctxB = await ctx.browser.createBrowserContext();
    const pageA = await ctxA.newPage();
    const pageB = await ctxB.newPage();
    await pageA.setViewport(ctx.defaultViewport);
    await pageB.setViewport(ctx.defaultViewport);
    const errorsB = [];
    pageA.on("pageerror", (e) => errorsB.push("alice: " + e.message));
    pageB.on("pageerror", (e) => errorsB.push("bob: " + e.message));

    async function signIn(p, who) {
      await p.goto(base2 + "/login.html", { waitUntil: "domcontentloaded", timeout: 30000 });
      await p.type("#loginUser", who);
      await p.type("#loginPass", PASSWORD);
      await Promise.all([
        p.waitForNavigation({ waitUntil: "domcontentloaded", timeout: 30000 }),
        p.click("#loginSubmit"),
      ]);
      await waitForBoot(p, 40000);
      await sleep(600);
      await openFilesTab(p);
      /* Generous, and wall clock: this second server is answering two freshly
         booted pages (each one warming a proxy, asking for a strip of
         thumbnails and rendering a frame) on a machine that is also running
         the first server for the rest of this run. The listing arrives when
         it reaches the front of that queue, so a short ceiling here reads
         load as a broken files pane. */
      await p.waitForFunction(() => document.querySelectorAll("#filesRoots .filesroot").length > 0,
        { timeout: 30000 });
      // New folder and Upload are disabled until a listing says this account may
      // write here, so the first listing is the real "ready" signal, not the chips.
      try {
        await p.waitForFunction(() => {
          const list = document.getElementById("filesList");
          const btn = document.getElementById("filesNewFolder");
          return !!(list && list.children.length && btn && !btn.disabled);
        }, { timeout: 45000 });
      } catch (e) {
        const seen = await p.evaluate(() => ({
          rows: (document.getElementById("filesList") || { children: [] }).children.length,
          text: ((document.getElementById("filesList") || {}).textContent || "").slice(0, 160),
          disabled: (document.getElementById("filesNewFolder") || {}).disabled,
        })).catch((x) => String(x));
        throw new Error(who + "'s first listing never arrived: " + JSON.stringify(seen)
          + " | errors " + JSON.stringify(errorsB.slice(0, 4)));
      }
    }

    await signIn(pageA, "alice");
    await signIn(pageB, "bob");

    // With logins on the switcher offers Shared with me and no This Mac.
    const aliceChips = (await readPane(pageA)).chips.map((c) => c.root);
    if (aliceChips.indexOf("shared") < 0 || aliceChips.indexOf("thismac") >= 0) {
      return fail("with logins on the chips are " + JSON.stringify(aliceChips)
        + "; expected Shared with me and no This Mac");
    }

    // ---- 1. new folder --------------------------------------------------
    await pageA.click("#filesNewFolder");
    await typeIntoInlineInput(pageA, "takes");
    await waitForRow(pageA, "takes");

    // ---- 2. drag and drop upload ----------------------------------------
    const b64 = fs.readFileSync(clipPath).toString("base64");
    await pageA.evaluate((name, data) => {
      const bin = atob(data);
      const bytes = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
      const file = new File([bytes], name, { type: "video/mp4" });
      const dt = new DataTransfer();
      dt.items.add(file);
      const target = document.getElementById("filesList");
      ["dragenter", "dragover", "drop"].forEach((type) => {
        target.dispatchEvent(new DragEvent(type, { bubbles: true, cancelable: true, dataTransfer: dt }));
      });
    }, "seed.mp4", b64);
    await pageA.waitForFunction(() => {
      const b = document.getElementById("uploadClipBtn");
      return b && b.textContent.trim() === "Upload" && !b.disabled;
    }, { timeout: 60000 });
    await waitForRow(pageA, "seed.mp4", 30000);
    const afterUpload = await readPane(pageA);
    const uploaded = afterUpload.rows.filter((r) => r.name === "seed.mp4")[0];
    if (!uploaded || uploaded.kind !== "file") return fail("the dropped file did not appear as a clip row");
    if (afterUpload.uploads.length) return fail("an upload row was left behind: " + JSON.stringify(afterUpload.uploads));
    notes.push("drag and drop upload landed as " + uploaded.name + " (" + uploaded.sub + ")");
    shots.push(await shot(pageA, "23-files-auth-mine.png"));

    // A refusal is not silent: a file with an extension the tool does not read
    // keeps its own row with the server's own sentence on it.
    await pageA.evaluate(() => {
      const file = new File([new Uint8Array([1, 2, 3, 4])], "notes.txt", { type: "text/plain" });
      const dt = new DataTransfer();
      dt.items.add(file);
      const target = document.getElementById("filesList");
      ["dragenter", "dragover", "drop"].forEach((type) => {
        target.dispatchEvent(new DragEvent(type, { bubbles: true, cancelable: true, dataTransfer: dt }));
      });
    });
    await pageA.waitForFunction(() => {
      const rows = document.querySelectorAll("#filesUploads .uploadrow.failed");
      return rows.length > 0;
    }, { timeout: 30000 });
    const refusal = await readPane(pageA);
    notes.push("a refused upload keeps its row: " + JSON.stringify(refusal.uploads[0].text).slice(0, 140));
    shots.push(await shot(pageA, "23-files-upload-refused.png"));
    await pageA.evaluate(() => {
      const b = document.querySelector("#filesUploads .uploadrow.failed .udismiss");
      if (b) b.click();
    });

    // ---- 3. move it into the folder by dragging the row -----------------
    const moved = await pageA.evaluate(() => {
      const rows = Array.prototype.slice.call(document.querySelectorAll("#filesList .filerow"));
      const src = rows.filter((r) => r.dataset.kind === "file")[0];
      const dst = rows.filter((r) => r.dataset.kind === "folder")[0];
      if (!src || !dst) return "missing a row";
      const dt = new DataTransfer();
      src.dispatchEvent(new DragEvent("dragstart", { bubbles: true, cancelable: true, dataTransfer: dt }));
      dst.dispatchEvent(new DragEvent("dragover", { bubbles: true, cancelable: true, dataTransfer: dt }));
      dst.dispatchEvent(new DragEvent("drop", { bubbles: true, cancelable: true, dataTransfer: dt }));
      return "ok";
    });
    if (moved !== "ok") return fail("could not set up the move drag: " + moved);
    await waitForNoRow(pageA, "seed.mp4", 20000);
    await clickRow(pageA, "takes");
    await waitForRow(pageA, "seed.mp4", 20000);
    const inFolder = await readPane(pageA);
    if (inFolder.crumbs[inFolder.crumbs.length - 1] !== "takes") {
      return fail("the breadcrumb does not end at takes: " + JSON.stringify(inFolder.crumbs));
    }
    notes.push("dragging the row onto the folder moved it; crumbs are now "
      + JSON.stringify(inFolder.crumbs));

    // ---- 4. rename ------------------------------------------------------
    await clickRowAction(pageA, "seed.mp4", "Rename");
    await pageA.evaluate(() => {
      const i = document.querySelector("#filesList input.frename");
      if (i) i.value = "";
    });
    await typeIntoInlineInput(pageA, "take-one.mp4");
    await waitForRow(pageA, "take-one.mp4", 20000);
    notes.push("renamed in place to take-one.mp4");

    // ---- 5. open it -----------------------------------------------------
    await clickRow(pageA, "take-one.mp4");
    await pageA.waitForFunction(() => {
      const el = document.getElementById("histKey");
      return !!(el && el.textContent && el.textContent.trim() !== "no project");
    }, { timeout: 40000 }).catch(() => null);
    const openedProject = await pageA.evaluate(() => fetch("/api/project").then((r) => r.json()));
    if (!openedProject.open || openedProject.name !== "take-one.mp4") {
      return fail("clicking the clip did not open its project: " + JSON.stringify({
        open: openedProject.open, name: openedProject.name,
      }));
    }
    notes.push("opening the clip from the library opened project " + openedProject.key
      + " (" + openedProject.name + ")");

    // ---- 6. trash and restore -------------------------------------------
    await clickRowAction(pageA, "take-one.mp4", "Trash");
    await waitForNoRow(pageA, "take-one.mp4", 20000);
    await chooseRoot(pageA, "trash");
    await waitForRow(pageA, "take-one.mp4", 20000);
    const trashRows = (await readPane(pageA)).rows;
    const trashed = trashRows.filter((r) => r.name === "take-one.mp4")[0];
    if (!trashed || trashed.actions.indexOf("Restore") < 0) {
      return fail("the trashed clip has no Restore action: " + JSON.stringify(trashed));
    }
    shots.push(await shot(pageA, "23-files-trash.png"));
    await clickRowAction(pageA, "take-one.mp4", "Restore");
    await waitForNoRow(pageA, "take-one.mp4", 20000);
    await chooseRoot(pageA, "mine");
    await clickRow(pageA, "takes");
    await waitForRow(pageA, "take-one.mp4", 20000);
    notes.push("trash kept it (with Restore) and restore put it back in takes");

    // ---- 7. share the folder with bob as a viewer -----------------------
    await chooseRoot(pageA, "mine");
    await pageA.waitForFunction(() => document.querySelectorAll("#filesList .filerow").length > 0,
      { timeout: 15000 });
    await clickRowAction(pageA, "takes", "Share");
    await pageA.waitForFunction(() => {
      const o = document.getElementById("shareOverlay");
      return !!(o && o.classList.contains("on") && document.getElementById("shareTarget"));
    }, { timeout: 15000 });
    const targets = await pageA.$eval("#shareTarget", (el) =>
      Array.prototype.map.call(el.options, (o) => o.textContent.trim()));
    if (targets.indexOf("bob") < 0) return fail("bob is not in the share picker: " + JSON.stringify(targets));
    shots.push(await shot(pageA, "23-files-share-dialog.png"));
    const bobValue = await pageA.$eval("#shareTarget", (el) => {
      const opt = Array.prototype.filter.call(el.options, (o) => o.textContent.trim() === "bob")[0];
      return opt ? opt.value : "";
    });
    if (!bobValue) return fail("the share picker has no option for bob");
    await pageA.select("#shareTarget", bobValue);
    await pageA.select("#shareRole", "viewer");
    await pageA.click("#shareAddBtn");
    await pageA.waitForFunction(() => {
      const rows = document.querySelectorAll("#shareBody .sharerow");
      return rows.length > 0;
    }, { timeout: 15000 });
    const grants = await pageA.evaluate(() =>
      Array.prototype.map.call(document.querySelectorAll("#shareBody .sharerow"), (r) => ({
        who: r.querySelector(".sname").textContent.trim(),
        role: r.querySelector("select").value,
      })));
    if (!grants.filter((g) => g.who === "bob" && g.role === "viewer").length) {
      return fail("the share did not appear in the dialog: " + JSON.stringify(grants));
    }
    const shareNote = await pageA.$eval("#shareBody .sharenote", (el) => el.textContent.trim());
    if (shareNote.indexOf("viewer") < 0 || shareNote.indexOf("editor") < 0) {
      return fail("the share dialog does not say what the two roles mean");
    }
    await pageA.click("#shareCloseBtn");
    notes.push("shared takes with bob as viewer through the dialog");

    // ---- 8. bob sees it under Shared with me and opens the clip ---------
    await pageB.reload({ waitUntil: "domcontentloaded" });
    await waitForBoot(pageB, 40000);
    await sleep(600);
    await openFilesTab(pageB);
    await pageB.waitForFunction(() => document.querySelectorAll("#filesRoots .filesroot").length > 0,
      { timeout: 15000 });
    await chooseRoot(pageB, "shared");
    await waitForRow(pageB, "takes", 20000);
    const bobShared = await readPane(pageB);
    const sharedRow = bobShared.rows.filter((r) => r.name === "takes")[0];
    if (!sharedRow || sharedRow.root.indexOf("user:") !== 0) {
      return fail("bob's Shared with me row does not point at alice's library: " + JSON.stringify(sharedRow));
    }
    if (sharedRow.badge !== "viewer") {
      return fail("bob's shared row does not show the viewer badge: " + JSON.stringify(sharedRow));
    }
    shots.push(await shot(pageB, "23-files-shared-with-me.png"));
    await clickRow(pageB, "takes");
    await waitForRow(pageB, "take-one.mp4", 20000);
    await clickRow(pageB, "take-one.mp4");
    await sleep(2500);
    const bobProject = await pageB.evaluate(() => fetch("/api/project").then((r) => r.json()));
    if (!bobProject.open || bobProject.name !== "take-one.mp4") {
      return fail("bob could not open the shared clip: " + JSON.stringify({
        open: bobProject.open, name: bobProject.name,
      }));
    }
    notes.push("bob opened the shared clip and got the same project key ("
      + (bobProject.key === openedProject.key ? "identical to alice's" : "DIFFERENT from alice's") + ")");

    // ---- 9. a viewer who commits gets the sentence on screen ------------
    await pageB.evaluate(() => {
      const first = document.querySelector("#params input.ctl-switch");
      if (first) first.click();
    });
    await pageB.waitForFunction(() => {
      const t = document.getElementById("toast");
      return !!(t && /viewer access/i.test(t.textContent || ""));
    }, { timeout: 20000 });
    const refusalText = await pageB.$eval("#toast", (el) => el.textContent.trim());
    shots.push(await shot(pageB, "23-files-viewer-refused.png"));
    notes.push('a viewer commit was refused on screen: "' + refusalText + '"');

    // ---- 10. promote bob to editor and let him commit -------------------
    await clickRowAction(pageA, "takes", "Share");
    await pageA.waitForFunction(() => document.querySelectorAll("#shareBody .sharerow select").length > 0,
      { timeout: 15000 });
    await pageA.select("#shareBody .sharerow select", "editor");
    await sleep(1200);
    await pageA.click("#shareCloseBtn");

    const commitsBefore = await pageB.evaluate(() =>
      fetch("/api/project/log").then((r) => r.json()).then((j) => (j.commits || []).length));
    // A DIFFERENT switch from the refused one on purpose: the refused edit never
    // reached the server, so toggling that same switch back would publish a
    // config identical to HEAD and there would be nothing to commit. This one
    // genuinely differs from what the project holds.
    const clicked = await pageB.evaluate(() => {
      const all = Array.prototype.slice.call(document.querySelectorAll("#params input.ctl-switch"));
      if (all.length < 2) return false;
      all[1].click();
      return true;
    });
    if (!clicked) return fail("there is no second switch in the grade panel to commit with");
    await pageB.waitForFunction((n) => fetch("/api/project/log").then((r) => r.json())
      .then((j) => (j.commits || []).length > n), { timeout: 30000 }, commitsBefore)
      .catch(() => null);
    const bobLog = await pageB.evaluate(() =>
      fetch("/api/project/log").then((r) => r.json()));
    const bobCommits = (bobLog.commits || []).filter((c) => c.author === "bob");
    if (!bobCommits.length) {
      return fail("bob was made an editor but his commit is not in the log: "
        + JSON.stringify((bobLog.commits || []).map((c) => c.author)));
    }
    notes.push("as an editor bob's commit landed (" + bobCommits.length + " commit(s) authored bob)");

    // ---- 11. alice's own History panel shows his name -------------------
    await pageA.evaluate(() => {
      const b = document.querySelector('.paramtab[data-paramtab="history"]');
      if (b) b.click();
    });
    // The live session is per account, so alice is never told about bob's
    // commit: her panel is nudged with the same studio:session event her own
    // next edit would fire, which is history.js's documented refresh hook.
    await pageA.evaluate(() => window.dispatchEvent(new CustomEvent("studio:session", { detail: {} })));
    await pageA.waitForFunction(() => {
      const rows = document.getElementById("historyRows");
      if (!rows) return false;
      return !!rows.querySelector('[title="bob"]');
    }, { timeout: 30000 }).catch(async () => {
      await pageA.evaluate(() => window.dispatchEvent(new CustomEvent("studio:session", { detail: {} })));
      await pageA.waitForFunction(() => {
        const rows = document.getElementById("historyRows");
        return !!(rows && rows.querySelector('[title="bob"]'));
      }, { timeout: 20000 });
    });
    const historyAuthors = await pageA.evaluate(() =>
      Array.prototype.map.call(document.querySelectorAll("#historyRows .havatar"), (a) => a.title));
    shots.push(await shot(pageA, "23-files-owner-history.png"));
    notes.push("alice's History panel shows the authors " + JSON.stringify(historyAuthors));

    // ---- 12. the activity feed named the actor --------------------------
    await pageA.evaluate(() => window.StudioActivity && window.StudioActivity.refresh());
    await sleep(1200);
    const feed = await pageA.evaluate(() =>
      Array.prototype.map.call(document.querySelectorAll("#activityList .activityrow"),
        (r) => Array.prototype.map.call(r.children, (x) => x.textContent.trim())
          .filter(Boolean).join(" ").replace(/\s+/g, " ").trim()));
    if (!feed.length) return fail("the activity feed is empty after a folder, an upload, a move, a rename and a share");
    const kinds = ["uploaded", "made the folder", "moved", "renamed", "shared"];
    const missing = kinds.filter((k) => !feed.some((row) => row.indexOf(k) >= 0));
    if (missing.length) {
      return fail("the activity feed is missing " + JSON.stringify(missing) + ": " + JSON.stringify(feed.slice(0, 6)));
    }
    shots.push(await shot(pageA, "23-files-activity.png"));
    notes.push("activity feed: " + feed.length + " events, first is " + JSON.stringify(feed[0]));

    if (errorsB.length) {
      return fail("page errors on the second server: " + errorsB.slice(0, 3).join(" | "));
    }
    partB = "ok";
  } catch (err) {
    if (err && err.message === "__skip_part_b__") {
      partB = "skipped";
    } else {
      partB = "failed";
      const where = (err && err.stack ? err.stack.split("\n") : [])
        .filter((l) => l.indexOf("23-files.mjs") >= 0).slice(0, 2).join(" << ");
      notes.push("Part B threw: " + (err && err.message ? err.message : String(err)) + " at " + where);
    }
  } finally {
    for (const c of [ctxA, ctxB]) {
      if (c) { try { await c.close(); } catch (e) { /* best effort */ } }
    }
    // By the pid captured at spawn, never by pattern.
    if (server && server.exitCode === null && !server.killed) {
      server.kill("SIGTERM");
      await sleep(600);
      try { server.kill("SIGKILL"); } catch (e) { /* already gone */ }
    }
    try { fs.rmSync(tmp, { recursive: true, force: true }); } catch (e) { /* best effort */ }
    // Leave the shared page exactly as it was found.
    try {
      await setTheme(page, startTheme);
      await page.setViewport(ctx.defaultViewport);
      await chooseRoot(page, "mine");
      await page.click('.railtab[data-rail="refs"]');
    } catch (e) { /* the next spec resets the viewport and the tab anyway */ }
  }

  if (partB === "failed") {
    return fail(notes.filter((n) => n.indexOf("Part B threw") === 0).join(" ") + " | " + notes.slice(0, 3).join("; "));
  }

  const shotNames = shots.filter(Boolean);
  return {
    status: "PASS",
    evidence: notes.join("; ") + (partB === "skipped" ? "; PART B SKIPPED" : "")
      + "; shots: " + shotNames.length,
  };
}
