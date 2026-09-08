/* Wave gate: run the parity harness headless and print the counts.
 *
 * Everything it touches is resolved from THIS FILE's own location, so the
 * gate measures the tree it lives in. It used to carry an absolute path to
 * the founder's live studio, which meant a worktree running the gate was
 * measuring somebody else's gpu.js and writing into somebody else's cache.
 *
 * Overrides, all optional:
 *   STUDIO_PYTHON       interpreter to run studio/server.py with
 *   STUDIO_FOOTAGE_SRC  folder of clips to symlink into the temp library
 *   PARITY_ONLY         substring / group / stage filter (a partial sweep is
 *                       NOT posted back to studio/tools/parity-results.json)
 *   PARITY_CLIP, PARITY_TIME, PARITY_WIDTHS
 *   PARITY_OUT          write the whole report as JSON here as well
 */
import puppeteer from "puppeteer-core";
import { spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));   // studio/tests
const ROOT = path.resolve(HERE, "..", "..");                  // the studio repo root

function firstExisting(list) {
  for (const p of list) { if (p && fs.existsSync(p)) return p; }
  return null;
}

// The studio's own venv if this tree has one, else the sibling checkout's
// (read only: an interpreter is executed, never written to), else the PATH.
const PYTHON = firstExisting([
  process.env.STUDIO_PYTHON,
  path.join(ROOT, ".venv", "bin", "python"),
  path.resolve(ROOT, "..", "..", "content", ".venv", "bin", "python")
]) || "python3";

// Footage is data, not code: a worktree has none of its own (the folder is
// gitignored), so fall back to the sibling checkout's clips. They are only
// ever read, and only ever through symlinks in a temp folder.
const FOOTAGE_SRC = firstExisting([
  process.env.STUDIO_FOOTAGE_SRC,
  fs.existsSync(path.join(ROOT, "footage"))
    && fs.readdirSync(path.join(ROOT, "footage")).some(n => n.charAt(0) !== ".")
    ? path.join(ROOT, "footage") : null,
  path.resolve(ROOT, "..", "..", "content", "footage")
]);
if (!FOOTAGE_SRC) { console.error("no footage folder found"); process.exit(2); }

const port = 20000 + Math.floor(Math.random() * 40000);

// Same isolation run.mjs uses: with logins off the library's own root is
// <root>/footage, so the gate gets a temp folder of SYMLINKS to the real
// clips (never copies: parity is measured on the same bytes) and cannot
// write into real footage. Removing it removes the links, not the clips.
const footageDir = fs.mkdtempSync(path.join(os.tmpdir(), "fixxr-parity-footage-"));
for (const name of fs.readdirSync(FOOTAGE_SRC)) {
  const src = path.join(FOOTAGE_SRC, name);
  if (name.charAt(0) === "." || !fs.statSync(src).isFile()) continue;
  fs.symlinkSync(src, path.join(footageDir, name));
}

/* A fresh data dir per run, for the same reason run.mjs uses one: the
 * server's default is <root>/studio/data, which holds real accounts and real
 * saved presets. The preset sweep reads GET /api/state's preset list, so
 * without this the row count is whoever-ran-it's own saved presets plus the
 * shipped library, and two runs on two machines are not comparable. With it
 * the sweep is exactly the checked in grade/presets/ library. */
const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), "fixxr-parity-data-"));

// The cache is deliberately NOT temporary: --data-dir makes the server's
// default cache <data dir>/cache, so every run would re-decode frames it
// already measured. This is the harness's own stable cache folder, shared
// with run.mjs, gitignored, and never the founder's studio/cache.
const cacheDir = path.join(ROOT, "studio", "tests", ".cache");
fs.mkdirSync(cacheDir, { recursive: true });

console.log("root:", ROOT);
console.log("python:", PYTHON);
console.log("footage:", FOOTAGE_SRC);
console.log("port:", port);

const srv = spawn(PYTHON, ["studio/server.py", "--port", String(port),
  "--data-dir", dataDir, "--footage", footageDir, "--cache-dir", cacheDir],
  { cwd: ROOT, stdio: "ignore" });
const base = `http://127.0.0.1:${port}`;

/* Kill the server and the temp folders on EVERY exit path. Without this a run
 * that throws (a puppeteer timeout, a bad fixture) leaves a studio server
 * listening and a temp library behind, and the next run picks a different
 * port and never notices. Only ever this run's own PID. */
let cleaned = false;
function cleanup() {
  if (cleaned) return;
  cleaned = true;
  try { srv.kill(); } catch { /* already gone */ }
  try { fs.rmSync(footageDir, { recursive: true, force: true }); } catch { /* best effort */ }
  try { fs.rmSync(dataDir, { recursive: true, force: true }); } catch { /* best effort */ }
}
process.on("exit", cleanup);
process.on("SIGINT", () => { cleanup(); process.exit(130); });
process.on("uncaughtException", (e) => { console.error(e); cleanup(); process.exit(2); });

let up = false;
for (let i = 0; i < 60; i++) {
  try { const r = await fetch(base + "/api/state"); if (r.ok) { up = true; break; } } catch {}
  await new Promise(r => setTimeout(r, 500));
}
if (!up) { srv.kill(); console.error("server never came up"); process.exit(2); }

const qs = new URLSearchParams({ auto: "1" });
if (process.env.PARITY_ONLY) qs.set("only", process.env.PARITY_ONLY);
if (process.env.PARITY_CLIP) qs.set("clip", process.env.PARITY_CLIP);
if (process.env.PARITY_TIME) qs.set("time", process.env.PARITY_TIME);
if (process.env.PARITY_WIDTHS) qs.set("widths", process.env.PARITY_WIDTHS);

/* protocolTimeout 0 disables puppeteer's 3 minute cap on a single CDP call.
 * The sweep runs on the page's main thread, so every poll of waitForFunction
 * queues behind whatever render is in flight; one cold ffmpeg decode of a
 * large source on a loaded machine is enough to blow past three minutes and
 * kill the run with "Runtime.callFunctionOn timed out" rather than a result. */
const browser = await puppeteer.launch({
  executablePath: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  headless: true, protocolTimeout: 0,
  args: ["--use-angle=metal", "--ignore-gpu-blocklist"]
});
const page = await browser.newPage();
const errors = [];
page.on("pageerror", e => errors.push(String(e)));
await page.goto(base + "/parity.html?" + qs.toString(), { waitUntil: "domcontentloaded" });
const t0 = Date.now();
await page.waitForFunction(
  () => document.body.dataset.done === "1" || document.body.dataset.done === "error",
  { timeout: 1800000, polling: 1000 });
const done = await page.evaluate(() => document.body.dataset.done);
const prog = await page.evaluate(() => document.getElementById("prog").textContent);
// Read the report out of the PAGE, not out of the server: a filtered run is
// never posted, and the old gate fetched the route after killing the server.
const report = await page.evaluate(() => window.parityReport || null);
console.log("done:", done, "in", Math.round((Date.now() - t0) / 1000) + "s");
console.log("prog:", prog);
console.log("pageerrors:", errors.length, errors.slice(0, 3));
if (report && report.rows) {
  const bad = report.rows.filter(r => r.verdict !== "EXACT");
  for (const r of bad) {
    console.log("  " + r.verdict + "  " + r.id + " @" + r.width
      + "  max " + (r.max === undefined ? r.error : r.max)
      + (r.blame && r.blame.length ? "  [" + r.blame.join(", ") + "]" : ""));
  }
  if (process.env.PARITY_OUT) {
    fs.writeFileSync(process.env.PARITY_OUT, JSON.stringify(report, null, 2));
    console.log("report written to", process.env.PARITY_OUT);
  }
}
/* ------------------------------------------------------------ mask fixtures
 *
 * Mask model v2 (C1). These live HERE rather than in parity.js because the
 * component stack is one lane's work and parity.js is another's; the sweep
 * above is untouched, so its counts stay comparable run to run, and these
 * rows are measured the same way with the same thresholds.
 *
 * Every fixture carries a LOUD correction for the same reason the existing
 * layer rows do: a matte that is right under a correction that barely moves
 * the picture would measure exact while proving nothing.
 *
 * Measured 2026-09-08 against the branch: 3 EXACT, 0 CLOSE, 23 FAILED. The
 * three that pass are the three needing no new ffmpeg input (a key component,
 * a luma component, and an all-off inverted stack, which is a global
 * correction). Every one of the 23 fails on the same integration item, an
 * error from ffmpeg and not a difference in pixels:
 *
 *     Invalid file index 1 in filtergraph description
 *     [1:v]format=gray16le,setrange=full,setsar=1[cm0_0];...
 *
 * studio/server.py still builds its `-i` list with the old window_layers
 * loop, which cannot see a component stack's window and matte inputs, so the
 * graph references an input nobody passed. The fix is one line in each of the
 * two places that inline that loop (`args += CG.mask_extra_inputs(cfg, info,
 * seek=...)`, see M2's checkpoint) and it is not this lane's file. The rows
 * are written to run unchanged once it lands, which is the honest state: the
 * gate says "awaiting integration" rather than pretending.
 */
const LCORR = { hue_shift: -25, sat_gain: 1.45, lum_gain: 1.12,
                offset: [0.05, -0.02, -0.04] };
const LKEY = { enabled: true, hue_center: 30, hue_width: 50, hue_soft: 20 };
const WIN_A = { enabled: true, shape: "ellipse", cx: 0.42, cy: 0.48,
                w: 0.5, h: 0.55, rotation: 14.0, softness: 0.2 };
const WIN_B = { enabled: true, shape: "rect", cx: 0.60, cy: 0.55,
                w: 0.45, h: 0.4, rotation: -22.0, softness: 0.1 };
const comp = (o) => Object.assign({ enabled: true, op: "add", type: "window" }, o);

const MASK_FIXTURES = [
  /* The control row. A stack of ONE window with everything else at its
   * default has to land exactly where the same shape as a legacy window
   * lands: same geq formula, same 8 bit PNG lifted by 257, same merge. If
   * this row disagrees, the v2 path is doing something the legacy path is
   * not, and every row under it is measuring the wrong thing. */
  { id: "maskv2_one_window", stage: "mask",
    config: { layers: [{ mask: { components: [comp({ id: "c1", window: WIN_A })] },
                         correct: LCORR }] } },
  { id: "maskv2_rect_hard", stage: "mask",
    config: { layers: [{ mask: { components: [comp({ id: "c1",
                window: { enabled: true, shape: "rect", cx: 0.5, cy: 0.5,
                          w: 0.5, h: 0.6, rotation: 33.0, softness: 0.0 } })] },
                         correct: LCORR }] } },
  { id: "maskv2_component_feather", stage: "mask",
    config: { layers: [{ mask: { components: [
                comp({ id: "c1", window: WIN_A, feather: 0.02 })] },
                         correct: LCORR }] } },
  { id: "maskv2_component_invert", stage: "mask",
    config: { layers: [{ mask: { components: [
                comp({ id: "c1", window: WIN_A, invert: true })] },
                         correct: LCORR }] } },
  { id: "maskv2_mask_invert", stage: "mask",
    config: { layers: [{ mask: { invert: true,
                components: [comp({ id: "c1", window: WIN_A })] },
                         correct: LCORR }] } },

  // the three ops, each against the same pair of shapes
  { id: "maskv2_op_add", stage: "mask",
    config: { layers: [{ mask: { components: [
                comp({ id: "c1", window: WIN_A }),
                comp({ id: "c2", window: WIN_B, op: "add" })] },
                         correct: LCORR }] } },
  { id: "maskv2_op_intersect", stage: "mask",
    config: { layers: [{ mask: { components: [
                comp({ id: "c1", window: WIN_A }),
                comp({ id: "c2", window: WIN_B, op: "intersect" })] },
                         correct: LCORR }] } },
  { id: "maskv2_op_subtract", stage: "mask",
    config: { layers: [{ mask: { components: [
                comp({ id: "c1", window: WIN_A }),
                comp({ id: "c2", window: WIN_B, op: "subtract" })] },
                         correct: LCORR }] } },

  /* The linear gradient. `softness` is an EASE EXPONENT here, not a blend, so
   * the three rows are the three shapes it can take: 0 is a hard step at the
   * centre line, 1 is a straight ramp, and above 1 eases both ends. The
   * fourth row leaves `angle` out entirely and aims the gradient with
   * `rotation`, which is the fallback _window_geometry does and the one thing
   * a defaulted `angle` key would silently break. */
  { id: "maskv2_linear_hard", stage: "mask",
    config: { layers: [{ mask: { components: [comp({ id: "c1",
                window: { enabled: true, shape: "linear", cx: 0.4, cy: 0.5,
                          angle: 34, w: 0.6, softness: 0.0 } })] },
                         correct: LCORR }] } },
  { id: "maskv2_linear_straight", stage: "mask",
    config: { layers: [{ mask: { components: [comp({ id: "c1",
                window: { enabled: true, shape: "linear", cx: 0.5, cy: 0.5,
                          angle: 90, w: 0.3, softness: 1.0 } })] },
                         correct: LCORR }] } },
  { id: "maskv2_linear_eased", stage: "mask",
    config: { layers: [{ mask: { components: [comp({ id: "c1",
                window: { enabled: true, shape: "linear", cx: 0.5, cy: 0.45,
                          angle: 12, w: 0.45, softness: 2.5 } })] },
                         correct: LCORR }] } },
  { id: "maskv2_linear_by_rotation", stage: "mask",
    config: { layers: [{ mask: { components: [comp({ id: "c1",
                window: { enabled: true, shape: "linear", cx: 0.5, cy: 0.5,
                          rotation: 120, w: 0.4, softness: 1.5 } })] },
                         correct: LCORR }] } },

  /* A stack whose first component is not an `add`. The engine DROPS it in
   * stack_components() and the GPU folds it against a zero accumulator, and
   * the two have to land on the same pixels or one of them is wrong about
   * what "folds from zero" means. */
  { id: "maskv2_leading_subtract", stage: "mask",
    config: { layers: [{ mask: { components: [
                comp({ id: "c0", window: WIN_B, op: "subtract" }),
                comp({ id: "c1", window: WIN_A, op: "add" })] },
                         correct: LCORR }] } },

  /* A stack that reaches nothing, inverted, which C1 says is a global
   * correction (the matte is 0, inverting it is 1 everywhere). Its own row
   * because it is a corner both sides have to read the same way, and because
   * the engine is one guard away from getting it wrong: mask_stack_segments
   * formats its accumulator label in without checking the loop set one, so
   * called directly it returns "[None]negate,..." for this config, an ffmpeg
   * input pad named None. build_layers guards it (`has_components(layer) and
   * bool(stack_components(layer))`), so today this row measures EXACT. It is
   * here so that stays true. */
  { id: "maskv2_all_off_inverted", stage: "mask",
    config: { layers: [{ mask: { invert: true, components: [
                comp({ id: "c1", window: WIN_A, enabled: false })] },
                         correct: LCORR }] } },

  /* Grow past the cap. 0.5 of the frame width is 100+ passes at any working
   * width and both sides clamp to 32, so this row fails loudly if one of them
   * forgot the cap rather than quietly taking minutes to render. */
  { id: "maskv2_finesse_grow_capped", stage: "mask",
    config: { layers: [{ mask: { components: [comp({ id: "c1", window: WIN_A })],
                                 finesse: { grow: 0.5 } }, correct: LCORR }] } },

  // a key as a component, alone and intersected with a shape
  { id: "maskv2_key", stage: "mask",
    config: { layers: [{ mask: { components: [
                comp({ id: "c1", type: "key", key: LKEY })] },
                         correct: LCORR }] } },
  { id: "maskv2_key_in_window", stage: "mask",
    config: { layers: [{ mask: { components: [
                comp({ id: "c1", window: WIN_A }),
                comp({ id: "c2", type: "key", key: LKEY, op: "intersect" })] },
                         correct: LCORR }] } },
  /* A luma component: the engine folds it into a key by opening the hue and
   * saturation halves all the way (component_key), and so does the preview.
   * Worth its own row because getting the widening wrong bakes an orange key
   * 40 degrees wide instead of a luminance range, which selects almost
   * nothing and would read as "the matte is missing" rather than as a bug. */
  { id: "maskv2_luma", stage: "mask",
    config: { layers: [{ mask: { components: [
                comp({ id: "c1", type: "luma",
                       key: { enabled: true, lum_low: 0.45, lum_high: 1.0,
                              lum_soft: 0.15 } })] },
                         correct: LCORR }] } },

  // finesse, one control at a time and then all of them together
  { id: "maskv2_finesse_blur", stage: "mask",
    config: { layers: [{ mask: { components: [comp({ id: "c1", window: WIN_A })],
                                 finesse: { blur: 0.02 } }, correct: LCORR }] } },
  { id: "maskv2_finesse_grow", stage: "mask",
    config: { layers: [{ mask: { components: [comp({ id: "c1", window: WIN_A })],
                                 finesse: { grow: 0.01 } }, correct: LCORR }] } },
  { id: "maskv2_finesse_shrink", stage: "mask",
    config: { layers: [{ mask: { components: [comp({ id: "c1", window: WIN_A })],
                                 finesse: { grow: -0.01 } }, correct: LCORR }] } },
  { id: "maskv2_finesse_clean", stage: "mask",
    config: { layers: [{ mask: { components: [comp({ id: "c1", window: WIN_A })],
                                 finesse: { clean_black: 0.2, clean_white: 0.15 } },
                         correct: LCORR }] } },
  { id: "maskv2_finesse_all", stage: "mask",
    config: { layers: [{ mask: { components: [
                comp({ id: "c1", window: WIN_A }),
                comp({ id: "c2", window: WIN_B, op: "subtract" })],
                                 finesse: { blur: 0.01, grow: 0.006,
                                            clean_black: 0.1, clean_white: 0.1 } },
                         correct: LCORR }] } },

  // the matte view of a stack, which is what the UI shows while it is built
  { id: "maskv2_show", stage: "mask",
    config: { layers: [{ mask: { show: true, components: [
                comp({ id: "c1", window: WIN_A }),
                comp({ id: "c2", window: WIN_B, op: "intersect" })] },
                         correct: LCORR }] } },

  // a layer blur under a v2 matte: the blur must stay inside the graded
  // branch, exactly as it does on the legacy path
  { id: "maskv2_layer_blur", stage: "mask",
    config: { layers: [{ mask: { components: [comp({ id: "c1", window: WIN_A })] },
                         correct: { blur: 24.0, lum_gain: 1.05 } }] } },

  // two v2 layers in a row, which do not commute
  { id: "maskv2_two_layers", stage: "mask",
    config: { layers: [
      { name: "desaturate", mask: { components: [comp({ id: "c1", window: WIN_B })] },
        correct: { saturation: 0.0 } },
      { name: "push red", mask: { components: [comp({ id: "c2", window: WIN_A })] },
        correct: { offset: [0.22, 0.0, -0.05] } }] } }
];

/* A matte component needs a real matte in the registry, so it is only added
 * when one is named. `cinegrade mask track` prints the id; pass it in as
 * PARITY_MATTE_ID once the service and the routes are on the branch. */
if (process.env.PARITY_MATTE_ID) {
  const mid = process.env.PARITY_MATTE_ID;
  MASK_FIXTURES.push(
    { id: "maskv2_matte", stage: "mask",
      config: { layers: [{ mask: { components: [
                  comp({ id: "c1", type: "matte", matte: { id: mid } })] },
                           correct: LCORR }] } },
    { id: "maskv2_matte_in_window", stage: "mask",
      config: { layers: [{ mask: { components: [
                  comp({ id: "c1", type: "matte", matte: { id: mid } }),
                  comp({ id: "c2", window: WIN_A, op: "intersect" })] },
                           correct: LCORR }] } },
    { id: "maskv2_matte_feather_clean", stage: "mask",
      config: { layers: [{ mask: { components: [
                  comp({ id: "c1", type: "matte", matte: { id: mid },
                         feather: 0.01 })],
                           finesse: { grow: 0.004, clean_black: 0.15 } },
                           correct: LCORR }] } });
}

if (process.env.PARITY_MASKS === "1") {
  const widths = (process.env.PARITY_WIDTHS || "640,1280").split(",").map(Number);
  const maskRows = await page.evaluate(async (fixtures, ws) => {
    const P = window.Parity, S = P.state, TH = P.thresholds;
    const gpu = S.gpu, clip = S.clip, time = S.time;
    function sourceWidth() {
      const c = (S.state.clips || []).filter(x => x.name === clip)[0];
      if (!c) return 0;
      return (c.autorotate && c.autorotate.width) || (c.raw && c.raw.width) || c.width || 0;
    }
    async function post(url, body) {
      const r = await fetch(url, { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body) });
      if (!r.ok) throw new Error(url + ": " + (await r.text()).slice(0, 300));
      return r;
    }
    async function source(width) {
      if (S.srcCache[width]) return S.srcCache[width];
      const r = await post("/api/source", { clip, time, width, autorotate: true });
      const size = (r.headers.get("X-Frame-Size") || "0x0").split("x").map(Number);
      const buf = await r.arrayBuffer();
      S.srcCache[width] = { data: new Uint16Array(buf), w: size[0], h: size[1] };
      return S.srcCache[width];
    }
    function compare(a, b, n) {
      const hist = new Float64Array(256);
      let max = 0, sum = 0;
      for (let i = 0; i < n * 3; i++) {
        let d = a[i] - b[i];
        if (d < 0) d = -d;
        if (d > max) max = d;
        sum += d;
        hist[d] += 1;
      }
      const total = n * 3;
      let over1 = 0, over4 = 0;
      for (let d = 2; d < 256; d++) over1 += hist[d];
      for (let d = 5; d < 256; d++) over4 += hist[d];
      return { max, mean: sum / total,
               pctOver1: 100 * over1 / total, pctOver4: 100 * over4 / total };
    }
    function verdict(m) {
      if (m.max <= TH.exactMax) return "EXACT";
      if (m.max <= TH.closeMax && m.pctOver1 <= TH.closePctOver1
          && m.pctOver4 <= TH.closePctOver4) return "CLOSE";
      return "FAILED";
    }
    const rows = [];
    for (const w of ws) {
      for (const f of fixtures) {
        const row = { id: f.id, group: "mask", stage: f.stage, width: w };
        try {
          const src = await source(w);
          const factor = src.w / (sourceWidth() || src.w);
          gpu.setSource(src.data, src.w, src.h);
          const cfg = window.StudioGPU.fullConfig(f.config);
          cfg.grain.enabled = false;
          await gpu.ready(cfg, { pixelScale: factor, time: time });
          const out = gpu.render(cfg, { pixelScale: factor, time: time });
          const pix = gpu.readPixels();
          row.claimed = out.report.overall;
          row.mattes = out.mattes;
          const r = await post("/api/frame", { clip, time, width: w,
            autorotate: true, format: "raw", config: cfg });
          const size = (r.headers.get("X-Frame-Size") || "0x0").split("x").map(Number);
          const ff = new Uint8Array(await r.arrayBuffer());
          if (size[0] !== out.width || size[1] !== out.height) {
            row.verdict = "FAILED";
            row.error = "size mismatch gpu " + out.width + "x" + out.height
              + " vs ffmpeg " + size[0] + "x" + size[1];
          } else {
            const m = compare(pix, ff, out.width * out.height);
            Object.assign(row, m);
            row.verdict = verdict(m);
          }
        } catch (e) {
          row.verdict = "FAILED";
          row.error = String((e && e.message) || e);
        }
        rows.push(row);
      }
    }
    return rows;
  }, MASK_FIXTURES, widths);

  const counts = { EXACT: 0, CLOSE: 0, FAILED: 0 };
  for (const r of maskRows) counts[r.verdict] = (counts[r.verdict] || 0) + 1;
  console.log("mask fixtures:", JSON.stringify(counts));
  for (const r of maskRows) {
    console.log("  " + r.verdict + "  " + r.id + " @" + r.width
      + (r.error ? "  " + r.error
                 : "  max " + r.max + "  mean " + (r.mean || 0).toFixed(4)
                   + "  %>1 " + (r.pctOver1 || 0).toFixed(4)));
  }
  if (process.env.PARITY_MASK_OUT) {
    fs.writeFileSync(process.env.PARITY_MASK_OUT,
                     JSON.stringify({ rows: maskRows, counts }, null, 2));
  }
}

await browser.close();
cleanup();
process.exit(done === "1" ? 0 : 1);
