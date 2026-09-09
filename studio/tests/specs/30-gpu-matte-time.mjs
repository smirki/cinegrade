/* Acceptance A1, the GPU half: a tracked matte MOVES with the picture.
 *
 * A1 is "with a tracked matte, playback shows the matte moving with the
 * subject in both the overlay and the graded picture". The overlay half is
 * masks.js's own canvas and specs 27 and 28 cover it. This spec is the GRADED
 * picture: the WebGL chain in gpu.js, driven through live.js the way the app
 * drives it, and then through the app's own controls and its own Play button.
 *
 * It fails on the code before this fix. gpu.js kept a playhead (`this.time`)
 * and keyed its matte cache on the frame index at that playhead, but no caller
 * ever set it: live.js passed `{ pixelScale }` and nothing else at all three
 * of its render call sites, so `this.time` stayed 0 for the whole session and
 * every matte component in every mode bound frame 0 of the store. The picture
 * moved and the mask sat still. Every claim below reads a frame NUMBER out of
 * the renderer's own report as well as looking at the pixels, so a regression
 * of that kind cannot pass by accident.
 *
 * The fixture is a matte built on disk with the studio's own Python against
 * grade/mattes.py's real write_gray_png (the same way spec 29 builds its
 * state fixtures, and the same on-disk contract C2 a real SAM service
 * writes): 96 frames at 24 fps of a white vertical bar that walks from the
 * left of the frame to the right. Nothing about the footage matters, because
 * the layer is written with `mask.show` on, which both engines render as the
 * MATTE ITSELF rather than as a graded picture. So the canvas is the matte,
 * and "did the matte move" is measured as the horizontal centre of brightness
 * on the canvas, which is a number, not an impression.
 *
 * The claims, in order:
 *   1. Through live.js at three explicit times, the renderer's matte report
 *      says it wanted AND got frame round(time * fps) each time, not frame 0,
 *      and nothing is reported lagging (ready() waits for the frame).
 *   2. The picture itself moves: the brightness centre on #gpuCanvas walks
 *      right across those three times, by roughly the distance the bar was
 *      built to walk.
 *   3. Through the APP: pressing the #scrub ruler at a second in and letting
 *      the app's own still render happen leaves the matte report on that
 *      second's frame (the app.js -> live.js -> gpu.js path, not a direct call).
 *   4. Through real PLAYBACK: pressing #playBtn and sampling for a second and
 *      a half sees the matte report take several different frame numbers and
 *      the brightness centre move on the canvas while it does.
 *
 * SKIPs (rather than fails) with no clips or no WebGL2, like every other GPU
 * spec here. Claim 4 needs the proxy, which the app encodes in the background
 * on clip select and spec 14 has already waited for by the time this runs; if
 * it somehow is not ready this spec says so in its evidence and still holds
 * claims 1 to 3, because those are the ones that pin the contract.
 */

import { execFileSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const CONTENT_DIR = path.resolve(HERE, "..", "..", "..");
const PYTHON = path.join(CONTENT_DIR, ".venv", "bin", "python");

const MATTE_FPS = 24.0;
const MATTE_FRAMES = 96;
const MATTE_W = 64;
const MATTE_H = 36;
// Where the bar's centre sits, as a fraction of the width, at the first and
// the last frame. The spec's own arithmetic below uses these two numbers, so
// the fixture and the assertions cannot drift apart.
const BAR_FROM = 0.15;
const BAR_TO = 0.85;

/* The fixture, written by the studio's own interpreter so the PNGs come out of
 * grade/mattes.py itself. index.json deliberately carries NO clip_key: a matte
 * with no clip key recorded cannot be checked against the clip in front of it
 * (server.py's _matte_clip_refusal says so in as many words), which is exactly
 * what a hand built fixture wants, since it was never tracked on anything. */
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

// Where the bar's centre should be at a frame, as a fraction of the width.
function expectedFrac(frame) {
  const k = Math.max(0, Math.min(MATTE_FRAMES - 1, frame));
  return BAR_FROM + (BAR_TO - BAR_FROM) * (k / (MATTE_FRAMES - 1));
}

export default async function run(ctx) {
  const page = ctx.page;
  const base = ctx.baseUrl;
  const notes = [];

  if (!ctx.firstClip) return { status: "SKIP", evidence: "no clips to grade" };
  const gpuOk = await page.evaluate(() => !!(window.StudioLive && StudioLive.available()));
  if (!gpuOk) {
    const why = await page.evaluate(() =>
      (window.StudioLive && StudioLive.reason()) || "no reason reported");
    return { status: "SKIP", evidence: "no GPU renderer in this browser: " + why };
  }
  const clipName = ctx.firstClip;

  /* The clip's own duration, so the times this spec probes are inside the clip
   * on whatever footage the machine has. The ruler spans the whole duration
   * (static/timeline.js), so claim 3 turns a time into a fraction of its
   * width with the same number. */
  const clips = await fetch(base + "/api/clips").then((r) => r.json())
    .then((j) => j.clips || []);
  const entry = clips.filter((c) => c.name === clipName)[0] || {};
  const duration = +entry.duration || 0;
  if (!(duration > 0.5)) {
    return { status: "SKIP", evidence: "clip " + clipName + " reports duration "
      + entry.duration + ", too short to move a matte through" };
  }
  // Three times inside the clip, the last as far in as the clip allows up to 2s.
  const TIMES = [0, Math.min(1.0, duration * 0.4), Math.min(2.0, duration * 0.8)];

  /* Put the app's own playhead at a clip time, with a real mouse press on the
   * ruler.
   *
   * Claims 3 and 4 used to assign #scrub.value and fire an "input" event,
   * which was the native <input type="range"> contract. #scrub is a track you
   * press now (static/timeline.js): the assignment was a no-op property write
   * on a div, nothing listened for "input", and the playhead stayed at 0, so
   * claim 3 would have measured the app path at the wrong time and claim 4
   * would have started playback from wherever the run happened to leave it.
   * A press is a truer user input than the assignment was and works on either
   * shape of the control, so there is no branch here for a range input.
   *
   * The y offset lands in the filmstrip lane (the ruler is 80px: the tick
   * scale, then the filmstrip, then the loop range lane at the bottom), so
   * this scrubs rather than dragging a loop range. Same helper shape as specs
   * 14, 24 and 28. Returns the time the app actually landed on, read off
   * #timeLabel, so a caller can tell "the playhead did not move" apart from
   * "the playhead moved and the matte did not follow". */
  async function scrubTo(t) {
    const frac = Math.max(0, Math.min(1, duration > 0 ? t / duration : 0));
    const box = await page.evaluate(() => {
      const card = document.querySelector('[gs-id="timeline"]');
      if (card) card.scrollIntoView({ block: "end" });
      const el = document.getElementById("scrub");
      if (!el) return null;
      const r = el.getBoundingClientRect();
      return { x: r.left, y: r.top + Math.min(30, r.height / 2), w: r.width };
    });
    if (!box || !(box.w > 10)) {
      throw new Error("#scrub has no usable width to press, so the playhead cannot be moved");
    }
    // Half a pixel in from the left edge for t=0: a press exactly on the
    // border can land on the card instead of the track.
    await page.mouse.click(box.x + Math.max(1, box.w * frac), box.y);
    await sleep(200);
    return page.evaluate(() =>
      parseFloat((document.getElementById("timeLabel") || {}).textContent) || 0);
  }

  const getGrade = () => fetch(base + "/api/grade?clip=" + encodeURIComponent(clipName))
    .then((r) => r.json());
  const putGrade = (config) => fetch(base + "/api/grade", {
    method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ clip: clipName, config }),
  });
  const dropGrade = () => fetch(base + "/api/grade?clip=" + encodeURIComponent(clipName),
    { method: "DELETE" });
  const snap = await getGrade();

  try {
    const rotation = await page.evaluate(() => {
      const b = document.querySelector('#rotSeg button[aria-pressed="true"]');
      return b ? b.getAttribute("data-rotation") : "auto";
    });
    const matteId = "a1move" + Date.now().toString(36);
    notes.push(buildFixture({
      dataDir: ctx.dataDir, clipKey: "a1-moving-" + Date.now().toString(36),
      id: matteId, clip: clipName, rotation: rotation,
      fps: MATTE_FPS, frames: MATTE_FRAMES,
      width: MATTE_W, height: MATTE_H, from: BAR_FROM, to: BAR_TO,
    }));

    /* The layer, written through the real #jsonApply button (the same public
     * door spec 29 uses to write a config). mask.show is what makes the canvas
     * the matte: no exposure to argue about, no dependence on the footage. */
    await page.click("#jsonBtn");
    await sleep(150);
    const cfg = JSON.parse(await page.$eval("#jsonText", (el) => el.value));
    cfg.layers = [{
      enabled: true, name: "A1 moving matte", placement: "before_look",
      mask: {
        show: true,
        components: [{
          id: "a1c0", type: "matte", op: "add", enabled: true,
          invert: false, feather: 0, name: "moving bar",
          matte: { id: matteId, recipe: {} },
        }],
      },
      correct: {},
    }];
    await page.evaluate((text) => { document.getElementById("jsonText").value = text; },
      JSON.stringify(cfg));
    const applyBtn = await page.$("#jsonApply");
    if (!applyBtn) return fail("no #jsonApply button");
    await applyBtn.click();
    await sleep(500);
    const applyError = await page.$eval("#jsonError", (el) => el.textContent).catch(() => "");
    if (applyError) return fail("#jsonApply rejected the config: " + applyError);

    /* -- claims 1 and 2: three explicit times through live.js ---------------
     *
     * One page.evaluate per time, each rendering and then sampling with no
     * yield in between, so the app's own debounced still render cannot land on
     * the canvas in the middle of a measurement (the same care spec 14 takes).
     * The brightness centre is the luminance weighted mean x over a small
     * copy of the canvas, in 0..1 across the width. */
    const probe = await page.evaluate(async (times, mid, clip, rot, config) => {
      function centre() {
        const c = document.getElementById("gpuCanvas");
        const copy = document.createElement("canvas");
        const w = Math.min(96, c.width), h = Math.min(54, c.height);
        copy.width = w; copy.height = h;
        const g = copy.getContext("2d");
        g.drawImage(c, 0, 0, w, h);
        const d = g.getImageData(0, 0, w, h).data;
        let sum = 0, wsum = 0, peak = 0;
        for (let y = 0; y < h; y++) {
          for (let x = 0; x < w; x++) {
            const i = (y * w + x) * 4;
            const v = (d[i] + d[i + 1] + d[i + 2]) / 3;
            if (v > peak) peak = v;
            sum += v;
            wsum += v * (x + 0.5);
          }
        }
        return { centre: sum > 0 ? +(wsum / sum / w).toFixed(4) : -1,
                 mean: +(sum / (w * h)).toFixed(2), peak: peak,
                 size: c.width + "x" + c.height };
      }
      const out = [];
      for (const t of times) {
        const r = await StudioLive.renderStill({
          clip: clip, time: t, width: 480, rotation: rot, config: config,
        });
        const s = centre();
        const m = (r.mattes || {})[mid] || null;
        out.push({ time: t, sample: s,
                   report: m ? { want: m.want, got: m.got, state: m.state,
                                 lagging: !!m.lagging, empty: !!m.empty } : null });
      }
      return out;
    }, TIMES, matteId, clipName, rotation, cfg);

    for (const row of probe) {
      const want = Math.round(row.time * MATTE_FPS);
      if (!row.report) {
        return fail("the renderer reported no matte row for " + matteId + " at t="
          + row.time + "s; the layer's matte component never reached gpu.js");
      }
      if (row.report.want !== want || row.report.got !== want) {
        return fail("at t=" + row.time + "s the renderer wanted frame "
          + row.report.want + " and got frame " + row.report.got
          + ", expected " + want + " for both (index = round(time * fps), C2). "
          + "want 0 at every time is the pre-fix behaviour: no caller passed "
          + "the playhead, so every render read matte frame 0");
      }
      if (row.report.lagging || row.report.empty) {
        return fail("at t=" + row.time + "s the matte reported lagging="
          + row.report.lagging + " empty=" + row.report.empty
          + "; ready() is supposed to await the frame before the render");
      }
      if (row.sample.centre < 0 || row.sample.peak < 200) {
        return fail("at t=" + row.time + "s the matte view canvas is dark (peak "
          + row.sample.peak + ", mean " + row.sample.mean + "), so the matte "
          + "never reached the picture at all");
      }
    }
    notes.push("frames wanted/got: " + probe.map((r) =>
      r.time + "s->" + r.report.want).join(", "));

    // Claim 2: the bar walked right, and by about the distance it was built to.
    const centres = probe.map((r) => r.sample.centre);
    for (let i = 1; i < centres.length; i++) {
      if (!(centres[i] > centres[i - 1] + 0.02)) {
        return fail("the matte did not move in the picture: brightness centre "
          + centres.join(" -> ") + " across t=" + probe.map((r) => r.time).join("/")
          + "s (fractions of the width). On the pre-fix code every frame binds "
          + "matte frame 0, so these three numbers are identical");
      }
    }
    const wantShift = expectedFrac(Math.round(TIMES[TIMES.length - 1] * MATTE_FPS))
      - expectedFrac(0);
    const gotShift = centres[centres.length - 1] - centres[0];
    if (Math.abs(gotShift - wantShift) > 0.12) {
      return fail("the matte moved, but not by the distance the fixture moves: "
        + "brightness centre shifted " + gotShift.toFixed(3)
        + " of the width over " + TIMES[TIMES.length - 1].toFixed(2)
        + "s, the bar was built to shift " + wantShift.toFixed(3));
    }
    notes.push("brightness centre " + centres.join(" -> ")
      + " (shift " + gotShift.toFixed(3) + ", fixture " + wantShift.toFixed(3) + ")");

    /* -- claim 3: the app's own still path -------------------------------- */
    await scrubTo(0);
    await sleep(900);
    const appTime = TIMES[1];
    // A real press on the ruler, which is exactly what a person does: app.js's
    // setTime runs, the still render goes through live.js, and gpu.js binds a
    // matte frame for that time or does not.
    const landed = await scrubTo(appTime);
    if (!(Math.abs(landed - appTime) < Math.max(0.25, duration * 0.05))) {
      return fail("pressing the ruler at " + appTime.toFixed(2) + "s of a "
        + duration.toFixed(2) + "s clip left the playhead at " + landed
        + "s, so the app path could not be measured at the time this claim is about");
    }
    let appReport = null;
    const appDeadline = Date.now() + 12000;
    while (Date.now() < appDeadline) {
      appReport = await page.evaluate((mid) => {
        const r = window.StudioLive.lastMattesReport();
        return r && r[mid] ? { want: r[mid].want, got: r[mid].got } : null;
      }, matteId);
      if (appReport && appReport.want > 0) break;
      await sleep(200);
    }
    const label = await page.evaluate(() =>
      (document.getElementById("timeLabel") || {}).textContent || "");
    if (!appReport || !(appReport.want > 0)) {
      return fail("after moving #scrub to about " + appTime + "s (time label "
        + JSON.stringify(label) + ") the app's own still render still reported "
        + "matte frame " + (appReport ? appReport.want : "none")
        + ": app.js's time is not reaching gpu.js");
    }
    notes.push("through the app at " + JSON.stringify(label) + ": frame "
      + appReport.want);

    /* -- claim 4: real playback ------------------------------------------- */
    const proxyReady = await page.evaluate(() => !!StudioLive.proxyReady());
    if (!proxyReady) {
      notes.push("proxy not ready, so playback was not sampled");
      return { status: "PASS", evidence: notes.join("; ") };
    }
    await scrubTo(0);
    await sleep(600);
    await page.click("#playBtn");
    try {
      await page.waitForFunction(() => StudioLive.isProxyPlaying(),
        { timeout: 20000, polling: 100 });
    } catch (e) {
      await page.click("#playBtn").catch(() => {});
      return fail("clicking Play did not start playback, so A1's playback half "
        + "could not be measured");
    }
    const seen = [];
    const play = [];
    for (let i = 0; i < 15; i++) {
      const s = await page.evaluate((mid) => {
        const r = window.StudioLive.lastMattesReport();
        const row = r && r[mid] ? r[mid] : null;
        const c = document.getElementById("gpuCanvas");
        const copy = document.createElement("canvas");
        const w = Math.min(96, c.width), h = Math.min(54, c.height);
        copy.width = w; copy.height = h;
        const g = copy.getContext("2d");
        g.drawImage(c, 0, 0, w, h);
        const d = g.getImageData(0, 0, w, h).data;
        let sum = 0, wsum = 0;
        for (let y = 0; y < h; y++) {
          for (let x = 0; x < w; x++) {
            const idx = (y * w + x) * 4;
            const v = (d[idx] + d[idx + 1] + d[idx + 2]) / 3;
            sum += v; wsum += v * (x + 0.5);
          }
        }
        return { want: row ? row.want : null,
                 centre: sum > 0 ? +(wsum / sum / w).toFixed(4) : -1 };
      }, matteId);
      if (s.want !== null && seen.indexOf(s.want) < 0) seen.push(s.want);
      if (s.centre >= 0) play.push(s.centre);
      await sleep(100);
    }
    await page.click("#playBtn");
    await sleep(400);

    if (seen.length < 3) {
      return fail("in 1.5s of playback the matte only ever reported frame "
        + JSON.stringify(seen) + ": the matte is not advancing with the "
        + "playhead during playback, which is acceptance A1");
    }
    const spread = Math.max.apply(null, play) - Math.min.apply(null, play);
    if (!(spread > 0.01)) {
      return fail("the matte's frame number advanced during playback ("
        + JSON.stringify(seen) + ") but the picture did not move: brightness "
        + "centre stayed within " + spread.toFixed(4) + " of the width");
    }
    notes.push("playback saw frames " + seen.join(",")
      + " and the centre moved " + spread.toFixed(3) + " of the width");

    return { status: "PASS", evidence: notes.join("; ") };
  } finally {
    await page.evaluate(() => {
      try { if (StudioLive.isProxyPlaying()) document.getElementById("playBtn").click(); }
      catch (e) { /* nothing playing */ }
    }).catch(() => {});
    if (snap && snap.exists) await putGrade(snap.config);
    else await dropGrade();
  }
}
