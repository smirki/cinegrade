/* playback-render: Play loops until stopped (contract E3,
 * plan/2026-09-05-studio-library/PLAN.md), the render fields move into a
 * popup dialog, and the Renders list moves with them.
 *
 * Founder's words: "can u make the preview loop / keep looping. change the
 * ui so the 5 secs is optional, and default is all. the render settings
 * should be a popup. start 0 secs 3 width source engine ffmpeg Render
 * should go in the popup."
 *
 * The claims, in order:
 *   1. secs empty ("all"): Play keeps going past the old fixed 5s and, once
 *      it reaches the end of the clip, the playhead wraps back to the start
 *      and keeps playing rather than stopping (mode 3, the GPU proxy, which
 *      is the default whenever a GPU is available in this browser).
 *   2. a number in secs bounds the loop to that length from the playhead,
 *      still looping rather than stopping once (mode 3).
 *   3. the same bounded loop on the OTHER engine, the server stream
 *      (#gpuToggle off): it used to stop dead at the end of one segment,
 *      now it keeps playing past that point.
 *   4. secs persists on the project as an extra and comes back on reload.
 *   5. the timeline bar no longer contains any of the old inline render
 *      fields; #renderDialogBtn is the one Render control left in it.
 *   6. the render dialog: opens with start at the playhead, secs empty,
 *      width source, engine ffmpeg, a frame estimate; R opens it; Enter
 *      submits a render from a text field inside it; Esc closes it in one
 *      press even while a field has focus; a render started from it lands
 *      in the Renders list (moved out of the LUTs rail pane) with both an
 *      open and a reveal action.
 *   7. the dialog on the phone layout (contract C8): reachable, on screen,
 *      not clipped by the viewport.
 *
 * SKIPs rather than fails when there is no footage or no usable GPU in this
 * browser, matching 14-proxy-playback: mode 3 is what this spec spends most
 * of its assertions on, and there is nothing to say about it without one.
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const CONTENT = path.resolve(HERE, "..", "..", "..");
const OUT = path.join(CONTENT, "grade", "out");
const SHOTS = path.resolve(HERE, "..", "..", "shots");
const PHONE = { width: 390, height: 844, deviceScaleFactor: 3, isMobile: true, hasTouch: true };

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

function fail(evidence) {
  return { status: "FAIL", evidence: evidence };
}

export default async function run(ctx) {
  const page = ctx.page;
  const base = ctx.baseUrl;
  const notes = [];
  const made = []; // render output paths this spec created, cleaned up at the end

  if (!ctx.firstClip) {
    return { status: "SKIP", evidence: "no clips from /api/clips to play" };
  }
  const gpuOk = await page.evaluate(() => !!(window.StudioLive && StudioLive.available()));
  if (!gpuOk) {
    const reason = await page.evaluate(() =>
      (window.StudioLive && StudioLive.reason()) || "no reason reported");
    return { status: "SKIP", evidence: "no GPU renderer in this browser: " + reason };
  }

  const clipsRes = await fetch(base + "/api/clips").then((r) => r.json());
  const entry = (clipsRes.clips || []).find((c) => c.name === ctx.firstClip);
  const duration = (entry && entry.duration) || 0;
  const fps = (entry && entry.fps) || 24;
  if (!(duration > 0.5)) {
    return { status: "SKIP", evidence: "clip " + ctx.firstClip + " has no usable duration (" + duration + "s) to loop across" };
  }

  async function shot(name) {
    try {
      fs.mkdirSync(SHOTS, { recursive: true });
      await page.screenshot({ path: path.join(SHOTS, name) });
      return name;
    } catch (err) {
      return null; // evidence only, never pass/fail
    }
  }

  function readTime() {
    return page.evaluate(() => parseFloat(document.getElementById("timeLabel").textContent) || 0);
  }

  /* A real press on the ruler at that time. #scrub used to be a native
     <input type="range"> and this used to assign its .value and fire
     "input"; the rebuilt timeline (static/timeline.js) is a track you press,
     so this presses it, which is a truer user input than the assignment was.
     The y offset lands in the filmstrip lane, above the range lane at the
     bottom of the track, so this scrubs rather than dragging a loop range. */
  async function scrubTo(t) {
    const frac = Math.max(0, Math.min(1, t / duration));
    const box = await page.evaluate(() => {
      const card = document.querySelector('[gs-id="timeline"]');
      if (card) card.scrollIntoView({ block: "end" });
      const r = document.getElementById("scrub").getBoundingClientRect();
      return { x: r.left, y: r.top + 30, w: r.width };
    });
    await page.mouse.click(box.x + box.w * frac, box.y);
    await sleep(150);
  }

  async function setSecs(value) {
    await page.evaluate((v) => {
      const el = document.getElementById("playDur");
      el.value = v;
      el.dispatchEvent(new Event("change", { bubbles: true }));
    }, value);
  }

  async function isPlaying() {
    return page.evaluate(() => {
      const s = document.getElementById("stage");
      return {
        gpuLive: s.classList.contains("gpu-live"),
        video: s.classList.contains("playing"),
        proxy: !!(window.StudioLive && StudioLive.isProxyPlaying()),
        label: document.getElementById("playBtn").textContent,
      };
    });
  }

  async function press(id) {
    await page.click("#" + id);
  }

  // Sample #timeLabel every `every` ms for `total` ms.
  async function sampleTimes(total, every) {
    const out = [];
    const n = Math.ceil(total / every);
    for (let i = 0; i < n; i++) {
      out.push(await readTime());
      await sleep(every);
    }
    return out;
  }

  // A "wrap" is a sample that drops well below the previous one -- a real
  // loop restart, not just playback's own small backward jitter (mode 1's
  // timeupdate can repeat a value, never go meaningfully backward on its
  // own the way a fresh segment starting at 0 does).
  function wrapped(samples, dropAtLeast) {
    for (let i = 1; i < samples.length; i++) {
      if (samples[i - 1] - samples[i] > dropAtLeast) return true;
    }
    return false;
  }

  const problems = [];

  // --- 0. known starting state ---------------------------------------------
  await scrubTo(0);
  await sleep(300);

  // --- 1. secs empty ("all"): plays past the old 5s default and wraps ------
  await setSecs("");
  const nearEnd = Math.max(0, duration - Math.max(0.5, duration * 0.12));
  await scrubTo(nearEnd);
  await sleep(200);
  await press("playBtn");
  try {
    await page.waitForFunction(() => window.StudioLive && StudioLive.isProxyPlaying(),
      { timeout: 15000, polling: 100 });
  } catch (e) {
    problems.push("pressing Play with an empty secs field did not start proxy playback");
  }
  if (!problems.length) {
    const budgetMs = Math.min(6000, Math.max(1500, (duration - nearEnd) * 1000 * 2.5 + 1500));
    const samples = await sampleTimes(budgetMs, 150);
    const stillPlaying = await isPlaying();
    if (!wrapped(samples, duration * 0.3)) {
      problems.push("secs empty from " + nearEnd.toFixed(2) + "s never wrapped back toward the start in "
        + budgetMs + "ms of samples: " + samples.map((s) => s.toFixed(2)).join(","));
    } else if (!stillPlaying.proxy) {
      problems.push("the playhead wrapped but playback had stopped instead of continuing (label "
        + JSON.stringify(stillPlaying.label) + ")");
    } else {
      notes.push("secs empty: wrapped and kept playing, samples " + samples.map((s) => s.toFixed(2)).join(","));
    }
  }
  await press("playBtn");
  await sleep(300);
  if ((await isPlaying()).proxy) problems.push("pressing Play a second time did not stop mode 3 playback");

  // --- 2. a number in secs bounds the loop (mode 3, GPU) --------------------
  if (!problems.length) {
    await setSecs("1");
    await scrubTo(0);
    await sleep(200);
    await press("playBtn");
    try {
      await page.waitForFunction(() => window.StudioLive && StudioLive.isProxyPlaying(),
        { timeout: 15000, polling: 100 });
      const samples = await sampleTimes(2600, 130);
      const stillPlaying = await isPlaying();
      const max = Math.max.apply(null, samples);
      if (max > 1.6) {
        problems.push("secs=1 let the GPU loop reach " + max.toFixed(2) + "s, past its bound");
      } else if (!wrapped(samples, 0.4)) {
        problems.push("secs=1 never looped back in 2.6s of samples: " + samples.map((s) => s.toFixed(2)).join(","));
      } else if (!stillPlaying.proxy) {
        problems.push("secs=1 wrapped but stopped instead of looping again");
      } else {
        notes.push("secs=1 (GPU): bounded to " + max.toFixed(2) + "s max, looped, samples "
          + samples.map((s) => s.toFixed(2)).join(","));
      }
    } catch (e) {
      problems.push("secs=1 did not start proxy playback: " + e.message);
    }
    await press("playBtn");
    await sleep(300);
  }

  // --- 3. the same bound on the server-stream engine (#gpuToggle off) ------
  if (!problems.length) {
    await page.click("#gpuToggle");
    await sleep(200);
    const gpuOff = await page.evaluate(() => !document.getElementById("gpuToggle").classList.contains("active"));
    if (!gpuOff) {
      problems.push("#gpuToggle did not turn off (still .active)");
    } else {
      await scrubTo(0);
      await sleep(200);
      await press("playBtn");
      try {
        await page.waitForFunction(() => document.getElementById("stage").classList.contains("playing"),
          { timeout: 20000, polling: 150 });
        const samples = await sampleTimes(3200, 200);
        const stage = await page.evaluate(() => document.getElementById("stage").classList.contains("playing"));
        const max = Math.max.apply(null, samples);
        if (!stage) {
          problems.push("secs=1 on the server stream had stopped by the end of the sample window ("
            + samples.map((s) => s.toFixed(2)).join(",") + "), expected it still looping");
        } else if (max > 1.8) {
          problems.push("secs=1 on the server stream reached " + max.toFixed(2) + "s, past its bound");
        } else {
          notes.push("secs=1 (server stream): still playing after " + samples.length
            + " samples over 3.2s, max " + max.toFixed(2) + "s -- it used to stop dead at one segment");
        }
      } catch (e) {
        problems.push("secs=1 did not start server-stream playback: " + e.message);
      }
      await press("playBtn");
      await sleep(400);
    }
    await page.click("#gpuToggle"); // restore GPU preview for the rest of the run
    await sleep(200);
  }

  // --- 4. secs persists per project, survives a reload ----------------------
  if (!problems.length) {
    await setSecs("2.5");
    /* The change listener POSTs /api/project/extra, and on a project row that
       does not exist yet it opens the project and retries, so this is up to
       three round trips against a server that is still finishing the
       playback this spec just stopped. A fixed 200ms sleep read the PREVIOUS
       value (1) on a loaded machine and called the save broken. Bounded wait
       for the value instead: it still fails, with the same message, if the
       save never lands. */
    let seenSecs = null;
    const secsDeadline = Date.now() + 15000;
    while (Date.now() < secsDeadline) {
      const p = await fetch(base + "/api/project?clip=" + encodeURIComponent(ctx.firstClip))
        .then((r) => r.json()).catch(() => ({}));
      seenSecs = (p.extras || {}).play_secs;
      if (seenSecs === 2.5) break;
      await sleep(150);
    }
    if (seenSecs !== 2.5) {
      problems.push("after typing 2.5 into secs, GET /api/project extras.play_secs reads "
        + JSON.stringify(seenSecs) + ", expected 2.5");
    } else {
      await page.reload({ waitUntil: "domcontentloaded" });
      await ctx.waitForBootComplete(20000);
      await sleep(600);
      const restored = await page.$eval("#playDur", (el) => el.value);
      if (restored !== "2.5") {
        problems.push('after a reload #playDur reads ' + JSON.stringify(restored) + ', expected "2.5"');
      } else {
        notes.push("secs=2.5 round-tripped through extras.play_secs and survived a reload");
      }
      // Back to "all" so the rest of the run (and the next run of the suite,
      // since grades/extras on this clip are the isolated data dir's own)
      // starts from the documented default.
      await setSecs("");
    }
  }

  // --- 5. the timeline bar no longer carries the old inline render fields --
  const stray = await page.evaluate(() => {
    const ids = ["renderName", "renderStart", "renderDur", "renderScale", "renderEngine"];
    return ids.filter((id) => {
      const el = document.getElementById(id);
      return el && el.closest(".tlbar");
    });
  });
  if (stray.length) {
    problems.push("still inside .tlbar: " + stray.join(", ") + " (contract E3 moves these into the render dialog)");
  } else {
    notes.push("no render fields remain inside .tlbar");
  }

  // --- 6. the render dialog -------------------------------------------------
  if (!problems.length) {
    await scrubTo(duration * 0.4);
    await sleep(200);
    const playheadBefore = await readTime();

    // R opens it (contract E3), from a page that is not mid-typing anywhere.
    await page.click("#stage");
    await page.keyboard.down("Shift");
    await page.keyboard.press("KeyR");
    await page.keyboard.up("Shift");
    await sleep(200);
    let dlgOn = await page.evaluate(() => document.getElementById("renderOverlay").classList.contains("on"));
    if (!dlgOn) {
      problems.push("Shift+R did not open #renderOverlay");
    } else {
      const state = await page.evaluate(() => ({
        start: document.getElementById("renderStart").value,
        secs: document.getElementById("renderDur").value,
        scale: document.getElementById("renderScale").value,
        engine: document.getElementById("renderEngine").value,
        estimate: document.getElementById("renderEstimate").textContent,
      }));
      if (Math.abs(parseFloat(state.start) - playheadBefore) > 0.15) {
        problems.push("render dialog opened with start=" + state.start + ", expected the playhead " + playheadBefore.toFixed(2));
      }
      if (state.secs !== "") problems.push('render dialog opened with secs=' + JSON.stringify(state.secs) + ", expected empty (to the end)");
      if (state.scale !== "") problems.push("render dialog opened with width=" + JSON.stringify(state.scale) + ', expected "" (source)');
      if (state.engine !== "ffmpeg") problems.push("render dialog opened with engine=" + state.engine + ", expected ffmpeg");
      if (!/frame/.test(state.estimate)) problems.push("no frame estimate shown: " + JSON.stringify(state.estimate));
      else notes.push("render dialog defaults: start " + state.start + ", secs " + JSON.stringify(state.secs)
        + ", width " + JSON.stringify(state.scale) + ", engine " + state.engine + ", estimate " + JSON.stringify(state.estimate));

      await shot("24-render-dialog-desktop.png");

      // Esc closes in one press, even mid-focus in a field.
      await page.focus("#renderName");
      await page.keyboard.press("Escape");
      await sleep(150);
      dlgOn = await page.evaluate(() => document.getElementById("renderOverlay").classList.contains("on"));
      if (dlgOn) problems.push("Escape while #renderName had focus did not close the dialog in one press");
      else notes.push("Esc closed the dialog in one press from inside a text field");
    }
  }

  // --- 6b. a render started from the dialog lands in the Renders list ------
  if (!problems.length) {
    await page.click("#renderDialogBtn");
    await sleep(200);
    const stem = "w3b-e3-spec-" + ctx.port;
    await page.evaluate((n) => { document.getElementById("renderName").value = n; }, stem + "-a");
    await page.evaluate(() => { document.getElementById("renderStart").value = "0"; });
    await page.evaluate(() => { document.getElementById("renderDur").value = "0.4"; });
    await page.click("#renderBtn");

    async function waitForJob(label, timeoutMs) {
      const deadline = Date.now() + timeoutMs;
      while (Date.now() < deadline) {
        const jobs = await fetch(base + "/api/jobs").then((r) => r.json());
        const job = (jobs.jobs || []).find((j) => (j.label || "").indexOf(label) !== -1);
        if (job && job.status !== "running") return job;
        await sleep(400);
      }
      throw new Error("render " + label + " did not finish in " + timeoutMs + "ms");
    }

    try {
      const jobA = await waitForJob(stem + "-a", 60000);
      if (jobA.status !== "done") {
        problems.push("render " + stem + "-a failed: " + jobA.message);
      } else {
        made.push(jobA.output);
        notes.push("started from the dialog's Render button, finished: " + jobA.message);

        // Enter renders, from inside a text field in the dialog.
        await page.click("#renderDialogBtn");
        await sleep(200);
        await page.evaluate((n) => { document.getElementById("renderName").value = n; }, stem + "-b");
        await page.evaluate(() => { document.getElementById("renderStart").value = "0"; });
        await page.evaluate(() => { document.getElementById("renderDur").value = "0.4"; });
        await page.focus("#renderName");
        await page.keyboard.press("Enter");
        const jobB = await waitForJob(stem + "-b", 60000);
        if (jobB.status !== "done") {
          problems.push("render " + stem + "-b (Enter) failed: " + jobB.message);
        } else {
          made.push(jobB.output);
          const closedAfterEnter = await page.evaluate(() =>
            !document.getElementById("renderOverlay").classList.contains("on"));
          if (!closedAfterEnter) problems.push("Enter started the render but the dialog did not close afterward");
          else notes.push("Enter (focus in #renderName) started a render and closed the dialog: " + jobB.message);
        }

        await page.click("#renderDialogBtn");
        await sleep(500); // let the 900ms job poll refresh #renderList at least once
        const rows = await page.evaluate((n) => {
          const rows = Array.prototype.slice.call(document.querySelectorAll("#renderList .lutrow"));
          const row = rows.find((r) => r.textContent.indexOf(n) !== -1);
          if (!row) return null;
          return Array.prototype.map.call(row.querySelectorAll(".rowaction"), (a) => a.textContent.trim());
        }, stem + "-a");
        if (!rows) {
          problems.push("no #renderList row found for " + stem + "-a after it finished");
        } else if (rows.indexOf("open") === -1 || rows.indexOf("reveal") === -1) {
          problems.push("#renderList row for " + stem + "-a has actions " + JSON.stringify(rows) + ", expected open and reveal");
        } else {
          notes.push("#renderList row has both open and reveal actions");
        }
        await shot("24-render-dialog-list.png");
        await page.click("#renderClose");
        await sleep(150);
      }
    } catch (e) {
      problems.push(String(e.message || e));
    }
  }

  // --- 7. the dialog on the phone layout (contract C8) ----------------------
  if (!problems.length) {
    await page.setViewport(PHONE);
    await page.goto(base + "/", { waitUntil: "domcontentloaded", timeout: 30000 });
    await ctx.waitForBootComplete(20000);
    await sleep(700);
    const opened = await page.evaluate(() => {
      const btns = Array.prototype.slice.call(document.querySelectorAll("#mobilebar .mobiletab"));
      const b = btns.filter((x) => x.textContent.trim() === "Preview")[0];
      if (!b) return false;
      b.click();
      return true;
    });
    if (!opened) {
      problems.push('no #mobilebar "Preview" button at the phone viewport');
    } else {
      await sleep(400);
      const clicked = await page.evaluate(() => {
        const btn = document.getElementById("renderDialogBtn");
        if (!btn) return false;
        btn.scrollIntoView({ block: "center" });
        return true;
      });
      await sleep(300);
      if (clicked) await page.click("#renderDialogBtn");
      await sleep(300);
      const box = await page.evaluate(() => {
        const o = document.getElementById("renderOverlay");
        const sb = document.querySelector("#renderOverlay .sheetbox");
        if (!o || !o.classList.contains("on") || !sb) return null;
        const r = sb.getBoundingClientRect();
        return { left: r.left, right: r.right, width: r.width, onScreen: getComputedStyle(o).display !== "none" };
      });
      if (!box) {
        problems.push("the render dialog did not open at the phone viewport");
      } else if (box.left < -1 || box.right > PHONE.width + 1) {
        problems.push("the render dialog's box is " + JSON.stringify(box) + " at a " + PHONE.width + "px viewport: it overflows");
      } else {
        notes.push("render dialog fits the phone viewport: " + JSON.stringify(box));
        await shot("24-render-dialog-phone.png");
      }
    }
    // leave the page as every other spec expects to find it
    await page.setViewport(ctx.defaultViewport);
    await page.goto(base + "/", { waitUntil: "domcontentloaded", timeout: 30000 });
    await ctx.waitForBootComplete(20000);
    await sleep(500);
  }

  // --- cleanup ---------------------------------------------------------------
  try {
    await page.evaluate(() => {
      const o = document.getElementById("renderOverlay");
      if (o) o.classList.remove("on");
    });
  } catch (e) { /* best effort */ }
  for (const file of made) {
    try {
      if (file && path.dirname(file) === OUT && path.basename(file).indexOf("w3b-e3-spec-") === 0) {
        fs.unlinkSync(file);
      }
    } catch (e) { /* already gone */ }
  }

  if (problems.length) {
    return { status: "FAIL", evidence: problems.join("; ") + " [" + notes.join("; ") + "]" };
  }
  return { status: "PASS", evidence: notes.join("; ") };
}
