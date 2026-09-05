/* Measures the playback proxy path (live.js Mode 3) against the still path.
 *
 * Every number the honesty entry for proxy playback claims comes from this
 * file, so it is a tool and not a one-off script: run it again after any
 * change to the encode (server.py's proxy section) or to the upload
 * (uploadVideoFrame in live.js) and the entry can be re-checked rather than
 * trusted.
 *
 * It answers three questions:
 *
 *   1. Fidelity. The same frame, the same config, graded twice: once from
 *      /api/source (16-bit, the reference path) and once from the 8-bit
 *      4:2:0 proxy. Reported as mean and max absolute difference per channel
 *      and the share of pixels off by more than 2 of 255.
 *   2. Performance. Five seconds of playback: frames graded per second, what
 *      the renderer skipped, what the decoder dropped, and whether a config
 *      change mid-playback lands on the next frame.
 *   3. Memory. The JS heap before and after, when Chrome exposes it.
 *
 * Frame alignment is the trap here, and it was measured rather than assumed.
 * ffmpeg's -ss and a <video>'s currentTime agree: both land on the frame
 * whose display interval CONTAINS the time given, so both paths are asked
 * for the same number. They are asked for the MIDDLE of a frame,
 * (n + 0.5)/fps, so that no rounding at either end can tip one of them into
 * the neighbouring frame. Getting this wrong is not subtle and not silent:
 * an early version of this file offset the two by a quarter frame in
 * opposite directions, compared frame 11 against frame 12 on a moving shot,
 * and reported a mean difference of 3.9 of 255 that was motion, not codec.
 * The video's own currentTime after the seek is printed for the same reason.
 *
 * Usage: node proxy-fidelity.mjs [--clip NAME] [--width 960] [--seconds 5]
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

function arg(name, dflt) {
  const i = process.argv.indexOf("--" + name);
  return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : dflt;
}

const WIDTH = parseInt(arg("width", "960"), 10);
const SECONDS = parseFloat(arg("seconds", "5"));

async function main() {
  const port = await findFreePort(20000, 60000, 40);
  const baseUrl = "http://127.0.0.1:" + port;
  const server = spawn(PYTHON, ["studio/server.py", "--port", String(port)], {
    cwd: CONTENT_DIR, stdio: ["ignore", "pipe", "pipe"],
  });
  let serverErr = "";
  server.stderr.on("data", (d) => { serverErr = (serverErr + d).slice(-4000); });

  let browser = null;
  try {
    await waitForHttp200(baseUrl + "/api/state", 30000, 250);
    const clips = await fetch(baseUrl + "/api/clips").then((r) => r.json());
    const wanted = arg("clip", null);
    const entry = wanted
      ? clips.clips.find((c) => c.name === wanted)
      : clips.clips[0];
    if (!entry) throw new Error("no clip to measure (footage/ is empty?)");
    console.log("[proxy-fidelity] clip " + entry.name + " " + entry.autorotate.width
      + "x" + entry.autorotate.height + " " + entry.fps.toFixed(3) + " fps "
      + entry.duration.toFixed(2) + "s, proxy width " + WIDTH);

    // The first run on a clip encodes two proxies (one per range tag) before
    // it can measure anything, and on a long 4K clip that is minutes: well
    // past puppeteer's 180s default for a single evaluate.
    browser = await puppeteer.launch({
      executablePath: CHROME_PATH, headless: true, protocolTimeout: 900000
    });
    const page = await browser.newPage();
    await page.setViewport({ width: 1440, height: 900 });
    const pageErrors = [];
    page.on("pageerror", (e) => pageErrors.push(String((e && e.message) || e)));
    await page.goto(baseUrl + "/", { waitUntil: "domcontentloaded", timeout: 30000 });
    await page.waitForFunction(() => {
      const host = document.getElementById("params");
      return !!(host && host.querySelector("section.stage"));
    }, { timeout: 20000 });
    // The app's own first render has to be finished and settled, or it lands
    // on the shared canvas in the middle of a measurement.
    await sleep(2500);

    const result = await page.evaluate(async (opts) => {
      const out = { variants: {}, errors: [] };
      const canvas = document.getElementById("gpuCanvas");
      const gl = canvas.getContext("webgl2");
      if (!gl) { out.errors.push("no webgl2 on #gpuCanvas"); return out; }

      function readback() {
        const w = canvas.width, h = canvas.height;
        const buf = new Uint8Array(w * h * 4);
        gl.bindFramebuffer(gl.FRAMEBUFFER, null);
        gl.readPixels(0, 0, w, h, gl.RGBA, gl.UNSIGNED_BYTE, buf);
        return { w: w, h: h, buf: buf };
      }
      function meanLuma(px) {
        let s = 0;
        for (let i = 0; i < px.buf.length; i += 4) {
          s += 0.2126 * px.buf[i] + 0.7152 * px.buf[i + 1] + 0.0722 * px.buf[i + 2];
        }
        return s / (px.buf.length / 4);
      }
      function compare(a, b) {
        if (a.w !== b.w || a.h !== b.h) {
          return { error: "size mismatch " + a.w + "x" + a.h + " vs " + b.w + "x" + b.h };
        }
        const n = a.buf.length / 4;
        const sum = [0, 0, 0], max = [0, 0, 0];
        let over2 = 0, over1 = 0;
        for (let i = 0; i < a.buf.length; i += 4) {
          let worst = 0;
          for (let c = 0; c < 3; c++) {
            const d = Math.abs(a.buf[i + c] - b.buf[i + c]);
            sum[c] += d;
            if (d > max[c]) max[c] = d;
            if (d > worst) worst = d;
          }
          if (worst > 2) over2++;
          if (worst > 1) over1++;
        }
        return {
          mean: sum.map((s) => +(s / n).toFixed(3)),
          max: max,
          pctOver2: +((over2 / n) * 100).toFixed(3),
          pctOver1: +((over1 / n) * 100).toFixed(3),
          pixels: n
        };
      }

      const state = await fetch("/api/state").then((r) => r.json());
      const base = JSON.parse(JSON.stringify(state.defaults));
      const graded = JSON.parse(JSON.stringify(state.defaults));
      graded.convert.exposure = 0.4;
      graded.primaries.contrast = 1.25;
      graded.primaries.saturation = 1.2;
      graded.primaries.temperature = 0.15;
      graded.primaries.gamma = 1.1;
      const configs = { defaults: base, graded: graded };

      // The frame rate the proxy reports, not the one /api/clips reports:
      // those differ on a clip whose container runs longer than its frames
      // (see _proxy_fps in server.py), and frame arithmetic needs the real one.
      const frames = opts.frameIndices;

      for (const range of ["full", "limited"]) {
        const v = { range: range, times: [] };
        try {
          const info = await StudioLive.prepareProxy({
            clip: opts.clip, width: opts.width, autorotate: true, range: range
          });
          v.info = {
            width: info.width, height: info.height, bytes: info.bytes,
            duration: info.duration, gop: info.gop, crf: info.crf, fps: info.fps
          };
          const fps = info.fps || opts.fps;
          await StudioLive.attachProxy(info, {});
          for (const n of frames) {
            // The middle of frame n, asked of both paths (see the header).
            const tStill = (n + 0.5) / fps;
            const tProxy = tStill;
            const row = { frame: n, tStill: +tStill.toFixed(5), tProxy: +tProxy.toFixed(5), configs: {} };
            for (const cname of Object.keys(configs)) {
              const cfg = configs[cname];
              await StudioLive.renderStill({
                clip: opts.clip, time: tStill, width: opts.width, autorotate: true,
                config: cfg, sourceWidth: opts.sourceWidth
              });
              const still = readback();
              const r = await StudioLive.seekProxy(tProxy, () => cfg,
                { sourceWidth: opts.sourceWidth });
              const proxy = readback();
              row.videoTime = +StudioLive.proxyCurrentTime().toFixed(5);
              row.configs[cname] = compare(still, proxy);
              row.configs[cname].stillLuma = +meanLuma(still).toFixed(2);
              row.configs[cname].proxyLuma = +meanLuma(proxy).toFixed(2);
              row.renderMs = r ? +r.ms.toFixed(2) : null;
            }
            v.times.push(row);
          }
        } catch (e) {
          v.error = String((e && e.message) || e);
        }
        out.variants[range] = v;
      }
      return out;
    }, {
      clip: entry.name, width: WIDTH, fps: entry.fps,
      sourceWidth: entry.autorotate.width,
      frameIndices: [Math.round(entry.fps * 0.5), Math.round(entry.fps * 1.5),
                     Math.round(entry.fps * 2.5)],
    });

    console.log("\n== fidelity: still (16-bit) vs proxy (8-bit 4:2:0) ==");
    for (const range of Object.keys(result.variants)) {
      const v = result.variants[range];
      if (v.error) { console.log(range + ": FAILED " + v.error); continue; }
      console.log("\n-- range tag: " + range + "  (" + v.info.width + "x" + v.info.height
        + ", " + (v.info.bytes / 1e6).toFixed(2) + " MB, crf " + v.info.crf
        + ", gop " + v.info.gop + ")");
      for (const row of v.times) {
        for (const cname of Object.keys(row.configs)) {
          const c = row.configs[cname];
          if (c.error) { console.log("  frame " + row.frame + " " + cname + ": " + c.error); continue; }
          console.log("  frame " + row.frame + " (video t=" + row.videoTime + ") " + cname
            + ": mean R/G/B " + c.mean.join("/")
            + "  max " + c.max.join("/")
            + "  >2: " + c.pctOver2 + "%  >1: " + c.pctOver1 + "%"
            + "  luma " + c.stillLuma + " vs " + c.proxyLuma);
        }
      }
      const all = [];
      v.times.forEach((row) => Object.keys(row.configs).forEach((k) => {
        if (!row.configs[k].error) all.push(row.configs[k]);
      }));
      if (all.length) {
        const meanOfMeans = [0, 1, 2].map((c) =>
          +(all.reduce((s, x) => s + x.mean[c], 0) / all.length).toFixed(3));
        const maxOfMax = [0, 1, 2].map((c) => Math.max(...all.map((x) => x.max[c])));
        const worstPct = Math.max(...all.map((x) => x.pctOver2));
        console.log("  SUMMARY " + range + ": mean " + meanOfMeans.join("/")
          + ", max " + maxOfMax.join("/") + ", worst pct>2 " + worstPct + "%");
      }
    }

    // ---- performance ----------------------------------------------------
    const perf = await page.evaluate(async (opts) => {
      const canvas = document.getElementById("gpuCanvas");
      const gl = canvas.getContext("webgl2");
      function meanLuma() {
        const w = canvas.width, h = canvas.height;
        const buf = new Uint8Array(w * h * 4);
        gl.bindFramebuffer(gl.FRAMEBUFFER, null);
        gl.readPixels(0, 0, w, h, gl.RGBA, gl.UNSIGNED_BYTE, buf);
        let s = 0;
        for (let i = 0; i < buf.length; i += 4) {
          s += 0.2126 * buf[i] + 0.7152 * buf[i + 1] + 0.0722 * buf[i + 2];
        }
        return s / (buf.length / 4);
      }
      const state = await fetch("/api/state").then((r) => r.json());
      const cfg = JSON.parse(JSON.stringify(state.defaults));
      const info = await StudioLive.prepareProxy({
        clip: opts.clip, width: opts.width, autorotate: true, range: opts.range
      });
      await StudioLive.attachProxy(info, {});
      await StudioLive.seekProxy(0, () => cfg, { sourceWidth: opts.sourceWidth });

      const mem0 = performance.memory ? performance.memory.usedJSHeapSize : null;
      const stamps = [];        // wall clock of every graded frame
      const renderMs = [];      // what the GPU chain itself cost per frame
      let lumaBefore = null, lumaAfterChange = null, framesAtChange = -1;
      let changed = false, frameCount = 0, endedAt = null;
      const video = document.getElementById("proxyVideo");
      const t0 = performance.now();

      await new Promise((resolve) => {
        // The clip can be shorter than the requested window, in which case
        // playback ends on its own. Measuring the fps over the requested
        // window instead of the window frames actually arrived in was the
        // first version of this file's mistake: it divided a whole clip's
        // frames by a timeout and reported 10.8 fps for real time playback.
        function stop() { StudioLive.pauseProxy(); resolve(); }
        if (video) video.addEventListener("ended", function () {
          endedAt = performance.now();
          stop();
        }, { once: true });
        StudioLive.playProxy(() => cfg, {
          sourceWidth: opts.sourceWidth,
          onFrame: function (f) {
            frameCount = f.frames;
            stamps.push(performance.now());
            if (typeof f.ms === "number") renderMs.push(f.ms);
            if (!changed && performance.now() - t0 > opts.seconds * 500) {
              lumaBefore = meanLuma();
              cfg.convert.exposure = 1.5;   // the "slider moved mid playback"
              changed = true;
              framesAtChange = f.frames;
            } else if (changed && lumaAfterChange === null && f.frames === framesAtChange + 1) {
              lumaAfterChange = meanLuma();
            }
            if (performance.now() - t0 > opts.seconds * 1000) stop();
          },
          onError: function () { stop(); }
        });
        setTimeout(stop, opts.seconds * 1000 + 6000);
      });
      const stats = StudioLive.proxyPlaybackStats();
      const mem1 = performance.memory ? performance.memory.usedJSHeapSize : null;
      const span = stamps.length > 1 ? stamps[stamps.length - 1] - stamps[0] : 0;
      return {
        elapsedMs: Math.round(span), frames: frameCount,
        achievedFps: span > 0 ? +((stamps.length - 1) / (span / 1000)).toFixed(2) : 0,
        endedEarly: endedAt !== null,
        meanRenderMs: renderMs.length
          ? +(renderMs.reduce((a, b) => a + b, 0) / renderMs.length).toFixed(2) : null,
        maxRenderMs: renderMs.length ? +Math.max(...renderMs).toFixed(2) : null,
        stats: stats,
        lumaBefore: lumaBefore === null ? null : +lumaBefore.toFixed(2),
        lumaAfterChange: lumaAfterChange === null ? null : +lumaAfterChange.toFixed(2),
        framesAtChange: framesAtChange,
        heapBefore: mem0, heapAfter: mem1,
        videoTime: +StudioLive.proxyCurrentTime().toFixed(3)
      };
    }, {
      clip: entry.name, width: WIDTH, seconds: SECONDS, range: "full",
      sourceWidth: entry.autorotate.width
    });

    console.log("\n== performance: " + SECONDS + "s of playback at " + WIDTH + " wide ==");
    console.log("  graded " + perf.frames + " frames over " + perf.elapsedMs + " ms = "
      + perf.achievedFps + " fps (the clip's own rate is " + entry.fps.toFixed(2)
      + " fps, which is the ceiling: a <video> plays at 1x)"
      + (perf.endedEarly ? " [clip ended before the window]" : ""));
    console.log("  GPU chain per frame: mean " + perf.meanRenderMs + " ms, max "
      + perf.maxRenderMs + " ms");
    console.log("  renderer skipped " + (perf.stats ? perf.stats.skipped : "?")
      + " presented frames, decoder dropped " + (perf.stats ? perf.stats.dropped : "?")
      + ", callback mode " + (perf.stats ? perf.stats.mode : "?"));
    console.log("  video reached t=" + perf.videoTime + "s");
    console.log("  config change mid playback: mean luma " + perf.lumaBefore
      + " -> " + perf.lumaAfterChange + " on the next frame"
      + " (delta " + (perf.lumaAfterChange !== null && perf.lumaBefore !== null
        ? (perf.lumaAfterChange - perf.lumaBefore).toFixed(2) : "not measured") + ")");
    console.log("\n== memory ==");
    if (perf.heapBefore === null || perf.heapAfter === null) {
      console.log("  performance.memory is not exposed in this Chrome: not measured");
    } else {
      console.log("  JS heap " + (perf.heapBefore / 1e6).toFixed(1) + " MB -> "
        + (perf.heapAfter / 1e6).toFixed(1) + " MB over " + perf.frames + " frames"
        + " (delta " + ((perf.heapAfter - perf.heapBefore) / 1e6).toFixed(1) + " MB)");
    }
    if (pageErrors.length) {
      console.log("\npage errors: " + JSON.stringify(pageErrors.slice(0, 5)));
    }
  } catch (err) {
    console.error("[proxy-fidelity] failed: " + (err && err.stack ? err.stack : err));
    if (serverErr) console.error("server stderr tail:\n" + serverErr);
    process.exitCode = 1;
  } finally {
    if (browser) { try { await browser.close(); } catch (e) { /* best effort */ } }
    if (server.exitCode === null && !server.killed) {
      server.kill("SIGTERM");
      await sleep(500);
      try { server.kill("SIGKILL"); } catch (e) { /* already gone */ }
    }
  }
}

main();
