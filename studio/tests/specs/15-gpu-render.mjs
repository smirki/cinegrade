/* gpu-render: the GPU final render path, end to end, against the ffmpeg one.
 *
 * Renders the same half second of the same clip twice, once through each
 * engine, and checks the four things that separate "a file appeared" from "a
 * render worked":
 *
 *   1. the job reaches done rather than failed,
 *   2. the file exists and ffprobe reads it as a video,
 *   3. the pixel format is the 10 bit one the ffmpeg path produces
 *      (yuv422p10le), so the GPU path is not quietly 8 bit,
 *   4. one decoded frame is within the parity harness's own thresholds of
 *      the same frame out of the ffmpeg render.
 *
 * SKIPs rather than fails when the machine has no Chrome or no node for the
 * server to spawn, because that is a property of the machine and not of the
 * code under test. The server says so in the job message, which is the only
 * signal the spec has and the same one a user would see.
 */
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const CONTENT = path.resolve(HERE, "..", "..", "..");
const OUT = path.join(CONTENT, "grade", "out");

// studio/static/parity.js, verdict(): the same numbers the GPU preview is
// held to, in 8 bit code values.
const TH = { exactMax: 1, closeMax: 16, closePctOver1: 0.5, closePctOver4: 0.02 };

const WIDTH = 640;
const DURATION = 0.5;

/* A real grade rather than the defaults, so the comparison exercises the
 * colour chain, and deliberately without vignette or sharpen: those two are
 * the stages where the GPU port and ffmpeg's own filter differ below the 8
 * bit level, which a 10 bit output preserves and the 8 bit parity harness
 * cannot see. The spec measures the render path, not those two stages. */
const CONFIG = {
  convert: { exposure: 0.4 },
  primaries: { contrast: 1.08, saturation: 1.06, temperature: 0.05,
               black_lift: 0.02, highlight_rolloff: 0.08 },
  fx: { halation: { enabled: true, threshold: 0.55, sigma: 40, strength: 0.5 } },
  grain: { enabled: false }
};

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

function frame16(file, index) {
  const r = spawnSync("ffmpeg", ["-v", "error", "-i", file, "-frames:v",
    String(index + 1), "-f", "rawvideo", "-pix_fmt", "rgb48le", "-"],
    { maxBuffer: 1 << 30 });
  return r.stdout;
}

async function runJob(baseUrl, body) {
  const res = await fetch(baseUrl + "/api/render", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  });
  const started = await res.json();
  if (!res.ok || !started.job) throw new Error("render refused: " + JSON.stringify(started));
  const id = started.job.id;
  const deadline = Date.now() + 300000;
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 500));
    const jobs = await fetch(baseUrl + "/api/jobs").then((r) => r.json());
    const job = (jobs.jobs || []).find((j) => j.id === id);
    if (!job) throw new Error("the job vanished from /api/jobs");
    if (job.status !== "running") return job;
  }
  throw new Error("the render did not finish in 300s");
}

export default async function run(ctx) {
  if (!ctx.firstClip) {
    return { status: "SKIP", evidence: "no clips from /api/clips to render" };
  }
  const stem = "w3b-spec-" + ctx.port;
  const made = [];
  try {
    const common = {
      clip: ctx.firstClip, config: CONFIG, start: 0, duration: DURATION,
      scale: WIDTH, no_audio: true
    };
    const gpu = await runJob(ctx.baseUrl, { ...common, engine: "gpu",
                                            name: stem + "-gpu" });
    if (gpu.status !== "done") {
      const why = String(gpu.message || "");
      if (/needs Chrome and node/.test(why)) {
        return { status: "SKIP", evidence: "the GPU engine is not available on "
                 + "this machine: " + why };
      }
      return { status: "FAIL", evidence: "the GPU render failed: " + why };
    }
    made.push(gpu.output);
    const ff = await runJob(ctx.baseUrl, { ...common, engine: "ffmpeg",
                                           name: stem + "-ffmpeg" });
    if (ff.status !== "done") {
      return { status: "FAIL", evidence: "the ffmpeg render failed: " + ff.message };
    }
    made.push(ff.output);

    const problems = [];
    if (!fs.existsSync(gpu.output)) problems.push("no file at " + gpu.output);
    const g = probe(gpu.output);
    const f = probe(ff.output);
    if (!g) problems.push("ffprobe could not read the GPU output as a video");
    if (g && g.pix_fmt !== "yuv422p10le") {
      problems.push("the GPU output is " + g.pix_fmt + ", not yuv422p10le");
    }
    if (g && f && (g.width !== f.width || g.height !== f.height)) {
      problems.push("size " + g.width + "x" + g.height + " against the ffmpeg "
                    + "render's " + f.width + "x" + f.height);
    }
    if (g && Number(g.nb_read_packets) < 2) {
      problems.push("only " + g.nb_read_packets + " frames in the GPU output");
    }

    let stat = null;
    if (g && f && g.width === f.width && g.height === f.height) {
      const a = frame16(ff.output, 0);
      const b = frame16(gpu.output, 0);
      const n = Math.min(a.length, b.length) / 2;
      if (n < 1000) {
        problems.push("could not decode a frame out of both renders");
      } else {
        let max = 0, over1 = 0, over4 = 0, sum = 0;
        for (let i = 0; i < n; i++) {
          const d = Math.abs(a.readUInt16LE(i * 2) - b.readUInt16LE(i * 2)) / 257;
          sum += d;
          if (d > max) max = d;
          if (d > 1) over1++;
          if (d > 4) over4++;
        }
        stat = { max: max, mean: sum / n, pct1: (over1 / n) * 100,
                 pct4: (over4 / n) * 100 };
        const ok = stat.max <= TH.exactMax
          || (stat.max <= TH.closeMax && stat.pct1 <= TH.closePctOver1
              && stat.pct4 <= TH.closePctOver4);
        if (!ok) {
          problems.push("frame 0 is outside the parity thresholds: max "
            + stat.max.toFixed(3) + ", " + stat.pct1.toFixed(3) + "% over 1, "
            + stat.pct4.toFixed(4) + "% over 4");
        }
      }
    }

    const evidence = "clip " + ctx.firstClip + " at " + WIDTH + " wide for "
      + DURATION + "s: gpu " + (g ? g.codec_name + " " + g.pix_fmt + " "
      + g.width + "x" + g.height + " " + g.nb_read_packets + " frames" : "?")
      + ", " + gpu.message + (stat ? ("; frame 0 max " + stat.max.toFixed(3)
      + ", mean " + stat.mean.toFixed(4) + ", " + stat.pct1.toFixed(3)
      + "% over 1, " + stat.pct4.toFixed(4) + "% over 4") : "");

    if (problems.length) {
      return { status: "FAIL", evidence: problems.join("; ") + " [" + evidence + "]" };
    }
    return { status: "PASS", evidence: evidence };
  } finally {
    // Only the two files this spec made, by their full paths, and only in
    // grade/out. Everything else in that folder belongs to somebody.
    for (const file of made) {
      try {
        if (file && path.dirname(file) === OUT && path.basename(file).startsWith(stem)) {
          fs.unlinkSync(file);
        }
      } catch (err) { /* it was already gone */ }
    }
  }
}
