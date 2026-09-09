/* Acceptance A1, the written FILE: a GPU final render binds each frame's own
 * matte frame.
 *
 * Spec 30 proves the preview half (live.js -> gpu.js reads the playhead).
 * This is the other half, and the one that ships: studio/render_gpu.py builds
 * a plan, a headless Chrome runs studio/static/render.js against it, and the
 * frames it posts back are encoded into a file somebody delivers. If the
 * worker cannot work out WHEN a frame is, every frame of that file is graded
 * against matte frame 0 and the mask stands still on a moving subject.
 *
 * Round 1 finding 2 left this half deliberately refusing rather than wrong:
 * render.js works each frame's clip time out as `plan.start + i / plan.fps`,
 * and the plan carried neither key, so a GPU render of a config with a
 * tracked matte threw a sentence naming the matte ids instead of writing a
 * file. The plan carries `fps` and `start` now (render_gpu.py), so this spec
 * pins both halves of that:
 *
 *   1. A real GPU render, through POST /api/render, of a layer whose only
 *      mask component is a matte that MOVES. The layer renders in matte view
 *      (`mask.show`), so the encoded picture IS the matte and the footage
 *      underneath it does not matter. Two frames of the output file, the
 *      first and the last, are decoded and the horizontal centre of
 *      brightness is measured on each: it has to have walked right by about
 *      the distance the fixture's own bar walks in that many frames. On the
 *      pre-fix code this render REFUSED (no fps in the plan); with the plan
 *      keys but without finding 2's own fix in render.js the two centres are
 *      identical, because every frame binds matte frame 0.
 *   2. The refusal is still there for a plan that has no timebase. The
 *      worker page (render.html, the real one, with the real render.js) is
 *      opened in its own tab with the plan route stubbed: a plan with a
 *      matte config and no `fps` must end in an error naming the matte, and
 *      the SAME plan with `fps` and `start` must get past that guard. This
 *      matters because the guard is the only thing standing between an older
 *      server and a silently wrong file.
 *
 * SKIPs (never falsely passes) with no clips, no WebGL2 in this browser, or
 * no Chrome/node for the server's own render worker: those are facts about
 * the machine. Saves no grade at all (the config goes straight to the render
 * route), and deletes only the file it made, by full path, in grade/out.
 */

import { execFileSync, spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const CONTENT_DIR = path.resolve(HERE, "..", "..", "..");
const PYTHON = path.join(CONTENT_DIR, ".venv", "bin", "python");
const OUT = path.join(CONTENT_DIR, "grade", "out");

const MATTE_FPS = 24.0;
const MATTE_FRAMES = 48;          // 2s of matte at 24 fps
const MATTE_W = 64;
const MATTE_H = 36;
const BAR_FROM = 0.15;            // the bar's centre at frame 0, as a fraction
const BAR_TO = 0.85;              // ... and at the last frame
const RENDER_WIDTH = 640;
const RENDER_SECONDS = 1.0;

/* The same fixture writer spec 30 uses, on the studio's own interpreter so the
 * PNGs come out of grade/mattes.py itself (contract C2's on-disk shape). No
 * `clip_key` on purpose: a matte with none recorded cannot be checked against
 * the clip in front of it, which is what a hand built fixture wants. */
const FIXTURE_SCRIPT = `
import sys, os, json
sys.path.insert(0, "grade")
import numpy as np
import mattes as MT

spec = json.load(sys.stdin)
d = os.path.join(spec["dataDir"], "mattes", spec["clipKey"], spec["id"])
os.makedirs(d, exist_ok=True)
w, h, n = int(spec["width"]), int(spec["height"]), int(spec["frames"])
half = max(1, int(round(w / 8.0)))
for k in range(n):
    frac = spec["from"] + (spec["to"] - spec["from"]) * (k / float(n - 1))
    cx = int(round(frac * w))
    arr = np.zeros((h, w), dtype=np.uint8)
    lo, hi = max(0, cx - half), min(w, cx + half)
    arr[:, lo:hi] = 255
    MT.write_gray_png(os.path.join(d, MT.frame_name(k)), arr)
index = {
    "matte_id": spec["id"], "clip": spec["clip"], "rotation": spec["rotation"],
    "fps": spec["fps"], "frames": n, "width": w, "height": h,
    "state": "done", "done_frames": n,
    "areas": [0.25] * n, "scores": [1.0] * n,
    "created": "2026-09-09T00:00:00Z", "model": "a1-fixture", "backend": "a1-fixture",
}
with open(os.path.join(d, MT.INDEX_NAME), "w") as f:
    json.dump(index, f)
print("ok " + str(n) + " frames " + str(w) + "x" + str(h))
`;

function buildFixture(spec) {
  return execFileSync(PYTHON, ["-c", FIXTURE_SCRIPT], {
    cwd: CONTENT_DIR,
    input: JSON.stringify(spec),
    encoding: "utf8",
  }).trim();
}

function fail(evidence) {
  return { status: "FAIL", evidence: evidence };
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

function probe(file) {
  const r = spawnSync("ffprobe", ["-v", "error", "-select_streams", "v:0",
    "-show_entries", "stream=codec_name,pix_fmt,width,height,nb_read_packets",
    "-count_packets", "-of", "json", file], { encoding: "utf8" });
  try {
    return JSON.parse(r.stdout).streams[0] || null;
  } catch (err) {
    return null;
  }
}

/* Every frame of a render as rgb48le, in one decode. A ProRes 4:2:2 10 bit
 * file of a second at 640 wide is a few tens of megabytes, which is cheaper
 * than one ffmpeg process per frame and, more to the point, cannot mix two
 * different seeks up. */
function framesOf(file) {
  const r = spawnSync("ffmpeg", ["-v", "error", "-i", file, "-f", "rawvideo",
    "-pix_fmt", "rgb48le", "-"], { maxBuffer: 1 << 30 });
  return r.stdout;
}

/* The luminance weighted centre of one frame, in 0..1 across the width, plus
 * the peak so a black frame is told apart from a centred one. */
function centreOf(buf, offset, w, h) {
  let sum = 0, wsum = 0, peak = 0;
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      const i = offset + ((y * w + x) * 3) * 2;
      const v = (buf.readUInt16LE(i) + buf.readUInt16LE(i + 2)
                 + buf.readUInt16LE(i + 4)) / 3;
      if (v > peak) peak = v;
      sum += v;
      wsum += v * (x + 0.5);
    }
  }
  return { centre: sum > 0 ? +(wsum / sum / w).toFixed(4) : -1,
           peak: Math.round(peak) };
}

// Where the bar's centre should be at a matte frame, as a fraction of width.
function expectedFrac(frame) {
  const k = Math.max(0, Math.min(MATTE_FRAMES - 1, frame));
  return BAR_FROM + (BAR_TO - BAR_FROM) * (k / (MATTE_FRAMES - 1));
}

async function runJob(baseUrl, body) {
  const res = await fetch(baseUrl + "/api/render", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const started = await res.json();
  if (!res.ok || !started.job) {
    throw new Error("render refused: " + JSON.stringify(started));
  }
  const id = started.job.id;
  const deadline = Date.now() + 300000;
  while (Date.now() < deadline) {
    await sleep(500);
    const jobs = await fetch(baseUrl + "/api/jobs").then((r) => r.json());
    const job = (jobs.jobs || []).find((j) => j.id === id);
    if (!job) throw new Error("the job vanished from /api/jobs");
    if (job.status !== "running") return job;
  }
  throw new Error("the GPU render did not finish in 300s");
}

/* Claim 2: the worker page against a stubbed plan.
 *
 * Its own tab, its own request interception, and only two routes answered
 * here: the plan (the thing under test) and the frame queue (204, "no
 * frames", so a plan that gets PAST the guard finishes at once instead of
 * needing a whole render). Everything else, gpu.js and render.js themselves
 * included, is served by the real server. */
async function runWorkerWithPlan(ctx, plan) {
  const tab = await ctx.browser.newPage();
  try {
    await tab.setRequestInterception(true);
    tab.on("request", (req) => {
      const url = req.url();
      if (url.indexOf("/api/render/gpu/plan") >= 0) {
        req.respond({ status: 200, contentType: "application/json",
                      body: JSON.stringify(plan) }).catch(() => {});
        return;
      }
      if (url.indexOf("/api/render/gpu/next") >= 0) {
        req.respond({ status: 204, body: "" }).catch(() => {});
        return;
      }
      req.continue().catch(() => {});
    });
    await tab.goto(ctx.baseUrl + "/render.html", { waitUntil: "domcontentloaded" });
    await tab.waitForFunction(() => !!document.body.dataset.done,
                              { timeout: 30000, polling: 100 });
    return tab.evaluate(() => ({
      done: document.body.dataset.done,
      error: (window.__renderInfo || {}).error || "",
      frames: (window.__renderInfo || {}).frames || 0,
    }));
  } finally {
    await tab.close().catch(() => {});
  }
}

export default async function run(ctx) {
  const page = ctx.page;
  const base = ctx.baseUrl;
  const notes = [];

  if (!ctx.firstClip) return { status: "SKIP", evidence: "no clips to render" };
  const gpuOk = await page.evaluate(() =>
    !!(window.StudioLive && StudioLive.available()));
  if (!gpuOk) {
    const why = await page.evaluate(() =>
      (window.StudioLive && StudioLive.reason()) || "no reason reported");
    return { status: "SKIP", evidence: "no GPU renderer in this browser: " + why };
  }
  const clipName = ctx.firstClip;

  const state = await fetch(base + "/api/state").then((r) => r.json());
  const clips = await fetch(base + "/api/clips").then((r) => r.json())
    .then((j) => j.clips || []);
  const entry = clips.filter((c) => c.name === clipName)[0] || {};
  const duration = +entry.duration || 0;
  if (!(duration > RENDER_SECONDS + 0.1)) {
    return { status: "SKIP", evidence: "clip " + clipName + " is "
      + entry.duration + "s, too short for a " + RENDER_SECONDS + "s render" };
  }

  const rotation = await page.evaluate(() => {
    const b = document.querySelector('#rotSeg button[aria-pressed="true"]');
    return b ? b.getAttribute("data-rotation") : "auto";
  });
  const matteId = "a1file" + Date.now().toString(36);
  notes.push(buildFixture({
    dataDir: ctx.dataDir, clipKey: "a1-render-" + Date.now().toString(36),
    id: matteId, clip: clipName, rotation: rotation,
    fps: MATTE_FPS, frames: MATTE_FRAMES,
    width: MATTE_W, height: MATTE_H, from: BAR_FROM, to: BAR_TO,
  }));

  const config = {
    layers: [{
      enabled: true, name: "A1 rendered matte", placement: "before_look",
      mask: {
        show: true,
        components: [{
          id: "a1r0", type: "matte", op: "add", enabled: true,
          invert: false, feather: 0, name: "moving bar",
          matte: { id: matteId, recipe: {} },
        }],
      },
      correct: {},
    }],
  };

  const stem = "w9-gpu-matte-" + ctx.port;
  const made = [];
  try {
    let job;
    try {
      job = await runJob(base, {
        clip: clipName, config: config, rotation: rotation,
        start: 0, duration: RENDER_SECONDS, scale: RENDER_WIDTH,
        no_audio: true, engine: "gpu", name: stem,
      });
    } catch (err) {
      return fail("the GPU render could not be started: " + err.message);
    }
    if (job.status !== "done") {
      const why = String(job.message || "");
      if (/needs Chrome and node/.test(why)) {
        return { status: "SKIP", evidence: "the GPU engine is not available "
          + "on this machine: " + why };
      }
      if (/carries no frame rate/.test(why)) {
        return fail("the GPU render refused for want of a timebase, which is "
          + "the pre-fix plan: render_gpu.py is not sending fps/start. " + why);
      }
      return fail("the GPU render failed: " + why);
    }
    made.push(job.output);

    if (!job.output || !fs.existsSync(job.output)) {
      return fail("the render reported done with no file at " + job.output);
    }
    const meta = probe(job.output);
    if (!meta) return fail("ffprobe could not read the render as a video");
    const w = +meta.width, h = +meta.height;
    const count = Number(meta.nb_read_packets) || 0;
    if (!(w > 0 && h > 0) || count < 4) {
      return fail("the render is " + meta.width + "x" + meta.height + " with "
        + meta.nb_read_packets + " frames, too little to measure a matte moving");
    }

    const buf = framesOf(job.output);
    const frameBytes = w * h * 3 * 2;
    const have = Math.floor(buf.length / frameBytes);
    if (have < 2) {
      return fail("decoded " + have + " frames out of the render ("
        + buf.length + " bytes at " + frameBytes + " per frame)");
    }
    const last = have - 1;
    const a = centreOf(buf, 0, w, h);
    const b = centreOf(buf, last * frameBytes, w, h);
    if (a.centre < 0 || b.centre < 0 || a.peak < 8000 || b.peak < 8000) {
      return fail("the rendered matte view is dark (frame 0 peak " + a.peak
        + ", frame " + last + " peak " + b.peak + " of 65535), so the matte "
        + "never reached the encoded picture at all");
    }

    /* The render's own frame rate decides which matte frame the last output
     * frame should hold: the file's frames are start + i/fps on the clip's
     * timeline, and the matte is indexed at its own fps. */
    const outFps = have / RENDER_SECONDS;
    const lastMatteFrame = Math.round((last / outFps) * MATTE_FPS);
    const wantShift = expectedFrac(lastMatteFrame) - expectedFrac(0);
    const gotShift = b.centre - a.centre;
    if (!(gotShift > 0.02)) {
      return fail("the matte did not move in the rendered file: brightness "
        + "centre " + a.centre + " at frame 0 and " + b.centre + " at frame "
        + last + " of " + have + ". Identical centres are exactly what "
        + "grading every frame against matte frame 0 looks like");
    }
    if (Math.abs(gotShift - wantShift) > 0.15) {
      return fail("the matte moved in the file, but not by the distance the "
        + "fixture moves: centre shifted " + gotShift.toFixed(3)
        + " over " + have + " frames, the bar was built to shift "
        + wantShift.toFixed(3) + " by matte frame " + lastMatteFrame);
    }
    notes.push("rendered " + have + " frames " + w + "x" + h + ", brightness "
      + "centre " + a.centre + " -> " + b.centre + " (shift "
      + gotShift.toFixed(3) + ", fixture " + wantShift.toFixed(3) + ")");

    /* -- claim 2: the refusal, both directions -------------------------- */
    const basePlan = {
      job: "stub", width: 64, height: 36,
      frameBytes: 64 * 36 * 3 * 2, outBytes: 64 * 36 * 3 * 2,
      outFormat: "rgb48le", expected: 1, pixelScale: 0.1,
      config: config, defaults: state.defaults, window: 1,
    };
    const noTimebase = await runWorkerWithPlan(ctx, basePlan);
    if (noTimebase.done === "1") {
      return fail("the render worker accepted a plan with no frame rate and a "
        + "tracked matte in the config, so an older server would get a file "
        + "with the matte frozen at frame 0 instead of a refusal");
    }
    if (!/carries no frame rate/.test(noTimebase.error)
        || noTimebase.error.indexOf(matteId) < 0) {
      return fail("a plan with no frame rate ended in " + noTimebase.done
        + " with " + JSON.stringify(noTimebase.error) + ", which is not the "
        + "refusal naming the matte that finding 2 asks for");
    }
    const withTimebase = await runWorkerWithPlan(ctx,
      { ...basePlan, fps: MATTE_FPS, start: 0 });
    if (/carries no frame rate/.test(withTimebase.error)) {
      return fail("the same plan WITH fps and start was still refused: "
        + withTimebase.error);
    }
    if (withTimebase.done !== "1") {
      return fail("the worker did not get past the timebase guard on a plan "
        + "carrying fps and start: " + withTimebase.done + " "
        + JSON.stringify(withTimebase.error));
    }
    notes.push("plan with no fps refused (" + noTimebase.error.slice(0, 60)
      + "...), the same plan with fps and start ran");

    return { status: "PASS", evidence: notes.join("; ") };
  } finally {
    // No grade is saved by this spec (the config goes straight to the render
    // route), so there is nothing to put back: only the file it made.
    for (const file of made) {
      try {
        if (file && path.dirname(file) === OUT
            && path.basename(file).startsWith(stem)) {
          fs.unlinkSync(file);
        }
      } catch (err) { /* it was already gone */ }
    }
  }
}
