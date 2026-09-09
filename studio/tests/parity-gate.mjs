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
 *   PARITY_MASKS        "0" turns the mask fixture block off (it is ON by
 *                       default, and a FAILED mask row fails the gate)
 *   PARITY_MASK_OUT     write the mask block's own rows as JSON here
 *   PARITY_MATTE_ID     also measure this REAL tracked matte, on top of the
 *                       ones the block builds for itself
 */
import puppeteer from "puppeteer-core";
import { spawn, execFileSync } from "node:child_process";
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
 * the sweep is exactly the checked in grade/presets/ library.
 *
 * PARITY_DATA_DIR overrides this with a caller supplied, PERSISTENT
 * directory instead of a fresh temp one, and that directory is never
 * deleted by this script's own cleanup() below (same treatment as
 * cacheDir). This is the only way a PARITY_MATTE_ID row can ever measure
 * anything: the matte store is a plain directory the SAM service writes to
 * (C2), so a real (non stub) matte has to already be sitting under
 * `<data-dir>/mattes/` before this server starts, which an ephemeral
 * mkdtemp can never provide. Without this override the 3 PARITY_MATTE_ID
 * rows can only ever be exercised by hand, never through this gate. */
const dataDirIsOwn = !process.env.PARITY_DATA_DIR;
const dataDir = process.env.PARITY_DATA_DIR
  || fs.mkdtempSync(path.join(os.tmpdir(), "fixxr-parity-data-"));
if (!dataDirIsOwn) fs.mkdirSync(dataDir, { recursive: true });

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
  if (dataDirIsOwn) {
    try { fs.rmSync(dataDir, { recursive: true, force: true }); } catch { /* best effort */ }
  }
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
 * The block runs BY DEFAULT and a FAILED row fails the gate (see the exit at
 * the bottom of this file). It also refuses to pass on the wrong number of
 * rows, so deleting a fixture cannot quietly shrink the result.
 *
 * PARITY_MASKS=0 turns the block off, which is for bisecting the sweep above
 * it and nothing else: a run without these rows is not a result.
 *
 * A note on this comment's history, because it is the kind of thing that
 * wastes an afternoon: it used to say "Measured 2026-09-08: 3 EXACT, 0 CLOSE,
 * 23 FAILED" and blame studio/server.py for building its `-i` list with the
 * old window-only loop. server.py calls CG.mask_extra_inputs on both paths and
 * has since the same commit that comment landed in, so the sentence was never
 * true. Every row measures, and every row measures EXACT.
 *
 * The matte rows below build their own matte on disk (C2 is a directory of
 * PNGs plus an index.json, which is a thing a test can write) rather than
 * waiting for a PARITY_MATTE_ID from a hand run. Two of them are deliberately
 * at a size the render is NOT, because that is the branch where the browser
 * has to scale the store's frame with swscale's own bilinear filter the way
 * the engine's `scale=W:H:flags=bilinear` does, and the frames differ from each
 * other, so a preview that read frame 0 instead of round(time * fps) fails
 * here as well as in the spec suite.
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

/* The matte fixture on disk.
 *
 * C2 says a matte is `<data-dir>/mattes/<clip-key>/<matte-id>/000000.png...`
 * plus an index.json, so a test can write one, and both sides read the same
 * bytes: the engine feeds the PNG sequence to ffmpeg as an extra input at
 * frame round(seek * fps) (cinegrade.matte_input_args), the browser fetches
 * the same frame over GET /api/matte/<id>/frame. Nothing is mocked here; this
 * is the store's own on-disk format, written by the studio's own Python
 * against grade/mattes.py's real write_gray_png.
 *
 * index.json deliberately records NO clip_key: a matte with none cannot be
 * checked against the clip in front of it (server.py's _matte_clip_refusal
 * says so in as many words), which is right for a fixture that was never
 * tracked on anything.
 *
 * Each frame is a cone: a smooth radial ramp with a hard boundary, centred at
 * an x that walks across the frame with the frame number. Smooth because a
 * resampler difference only shows up on a gradient, hard-edged because an edge
 * is where a filter's tap count shows up, and moving because a preview that
 * ignores time then measures FAILED instead of passing quietly.
 */
const MATTE_FIXTURE_SCRIPT = `
import sys, os, json
sys.path.insert(0, "grade")
import numpy as np
import mattes as MT

spec = json.load(sys.stdin)
d = os.path.join(spec["dataDir"], "mattes", spec["clipKey"], spec["id"])
os.makedirs(d, exist_ok=True)
w, h, n = int(spec["width"]), int(spec["height"]), int(spec["frames"])
ys, xs = np.mgrid[0:h, 0:w]
r = 0.45 * w
for k in range(n):
    cx = (0.2 + 0.6 * (k / float(max(1, n - 1)))) * w
    cy = h * 0.5
    d2 = np.sqrt((xs - cx) ** 2 + ((ys - cy) * (w / float(h))) ** 2)
    v = np.clip(1.0 - d2 / r, 0.0, 1.0)
    MT.write_gray_png(os.path.join(d, MT.frame_name(k)),
                      np.clip(np.round(v * 255.0), 0, 255).astype(np.uint8))
index = {
    "matte_id": spec["id"], "clip": spec["clip"], "rotation": "auto",
    "fps": spec["fps"], "frames": n, "width": w, "height": h,
    "state": "done", "done_frames": n,
    "areas": [0.2] * n, "scores": [1.0] * n,
    "created": "2026-09-09T00:00:00Z", "model": "parity-fixture",
    "backend": "parity-fixture",
}
with open(os.path.join(d, MT.INDEX_NAME), "w") as f:
    json.dump(index, f)
print("ok " + spec["id"] + " " + str(n) + " frames at " + str(w) + "x" + str(h))
`;

function buildMatteFixture(spec) {
  return execFileSync(PYTHON, ["-c", MATTE_FIXTURE_SCRIPT], {
    cwd: ROOT, input: JSON.stringify(spec), encoding: "utf8",
  }).trim();
}

const MASKS_ON = process.env.PARITY_MASKS !== "0";
let maskBad = false;
if (!MASKS_ON) {
  console.log("mask fixtures: SKIPPED by PARITY_MASKS=0, so this run is not a result");
}
if (MASKS_ON) {
  const widths = (process.env.PARITY_WIDTHS || "640,1280").split(",").map(Number);

  /* What the sweep above actually measured, read out of the page rather than
   * guessed, so the matte fixture is built for THIS clip at THIS time. */
  const pstate = await page.evaluate(() => ({
    clip: window.Parity.state.clip, time: window.Parity.state.time,
  }));
  /* The render's exact pixel size at the first width, from the server's own
   * header, so one fixture can be built at exactly the size the render runs at
   * (the texelFetch branch) and read at another width as well (the resample
   * branch). Guessing this from the clip's aspect would only reproduce
   * scale_for_preview's rounding by accident. */
  const srcHead = await fetch(base + "/api/source", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ clip: pstate.clip, time: pstate.time,
                           width: widths[0], autorotate: true }),
  });
  const nativeSize = (srcHead.headers.get("X-Frame-Size") || "0x0").split("x").map(Number);
  await srcHead.arrayBuffer();          // drain it, the bytes are not wanted
  if (!(nativeSize[0] > 0 && nativeSize[1] > 0)) {
    console.error("could not read the render size for the matte fixtures");
    cleanup();
    process.exit(2);
  }

  const stamp = Date.now().toString(36);
  const clipKey = "parity-mask-" + stamp;
  const MFPS = 24;
  // Ten seconds of matte, so any PARITY_TIME inside a test clip has a frame.
  const MFRAMES = 240;
  const nativeId = "pmnative" + stamp;
  const smallId = "pmsmall" + stamp;
  console.log(buildMatteFixture({
    dataDir, clipKey, id: nativeId, clip: pstate.clip, fps: MFPS,
    frames: MFRAMES, width: nativeSize[0], height: nativeSize[1],
  }));
  console.log(buildMatteFixture({
    dataDir, clipKey, id: smallId, clip: pstate.clip, fps: MFPS,
    frames: MFRAMES, width: 320, height: 180,
  }));
  console.log("matte fixture frame at " + pstate.time + "s: "
    + Math.round(pstate.time * MFPS));

  /* PARITY_MATTE_ID still works and now means "measure this REAL tracked matte
   * as well as the built ones", which is the useful thing to do with the id
   * `cinegrade mask track` prints. */
  const realId = process.env.PARITY_MATTE_ID || "";
  MASK_FIXTURES.push(
    /* The matte at exactly the render's size at the first width: no scaling at
     * all on either side there, which isolates the 8 bit lattice hop
     * (gray16le lifts a matte code by 257) from the resampling. At the second
     * width the same fixture is a 2x upscale on both sides. */
    { id: "maskv2_matte", stage: "mask",
      config: { layers: [{ mask: { components: [
                  comp({ id: "c1", type: "matte", matte: { id: nativeId } })] },
                           correct: LCORR }] } },
    { id: "maskv2_matte_in_window", stage: "mask",
      config: { layers: [{ mask: { components: [
                  comp({ id: "c1", type: "matte", matte: { id: nativeId } }),
                  comp({ id: "c2", window: WIN_A, op: "intersect" })] },
                           correct: LCORR }] } },
    { id: "maskv2_matte_feather_clean", stage: "mask",
      config: { layers: [{ mask: { components: [
                  comp({ id: "c1", type: "matte", matte: { id: nativeId },
                         feather: 0.01 })],
                           finesse: { grow: 0.004, clean_black: 0.15 } },
                           correct: LCORR }] } },
    /* A matte the render's size is NOT, at both widths: the row that measures
     * the resampler. The engine scales the store's frame with
     * scale=W:H:flags=bilinear and the browser runs the ported swscale filter,
     * so this row is EXACT; with the card's own LINEAR filter (a two tap tent,
     * which swscale's bilinear is not on an upscale of this ratio) it is not. */
    { id: "maskv2_matte_scaled", stage: "mask",
      config: { layers: [{ mask: { components: [
                  comp({ id: "c1", type: "matte", matte: { id: smallId } })] },
                           correct: LCORR }] } },
    /* The same scaled matte in MATTE VIEW, where the matte is the picture
     * itself rather than the weight on a correction: a resampler difference
     * lands on the output undiluted here, which makes this the sharpest row in
     * the block. */
    { id: "maskv2_matte_scaled_show", stage: "mask",
      config: { layers: [{ mask: { show: true, components: [
                  comp({ id: "c1", type: "matte", matte: { id: smallId } })] },
                           correct: LCORR }] } });
  if (realId) {
    MASK_FIXTURES.push(
      { id: "maskv2_matte_tracked", stage: "mask",
        config: { layers: [{ mask: { components: [
                    comp({ id: "c1", type: "matte", matte: { id: realId } })] },
                             correct: LCORR }] } });
  }
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
  /* The exit status, which is the whole point of a gate.
   *
   * These verdicts used to be printed and then thrown away: the only thing
   * that reached process.exit was the sweep page's own `done` flag, so all 52
   * mask rows could read FAILED and the gate still exited 0. Now a FAILED row
   * is fatal, and so is the WRONG NUMBER of rows, because a fixture that
   * quietly stops being measured (deleted, renamed, or lost to a thrown
   * fixture builder) is the same hole in a different shape. */
  const expected = MASK_FIXTURES.length * widths.length;
  if (maskRows.length !== expected) {
    console.error("mask fixtures: measured " + maskRows.length
      + " rows, expected " + expected + " (" + MASK_FIXTURES.length
      + " fixtures at " + widths.length + " widths)");
    maskBad = true;
  }
  if (counts.FAILED) {
    console.error("mask fixtures: " + counts.FAILED + " FAILED, which is fatal");
    maskBad = true;
  }
}

await browser.close();
cleanup();
process.exit(done === "1" && !maskBad ? 0 : 1);
