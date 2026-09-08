/* Masks, lane M7: the states that a live race is either impossible or
 * needlessly slow to reach, proven the same way M4's own CLI fixtures and
 * this lane's own studio/tests/py/test_mask_stub_service.py prove them:
 * hand written matte fixtures on real disk, read back through the real
 * studio/server.py matte routes, exactly like a real SAM service's own
 * index.json would be. This is not a mock of the mask store: grade/
 * mattes.py's info_from_dir() reads a hand built index.json and a real
 * service's index.json through the identical code path, which is the
 * whole point of C2's on-disk contract.
 *
 * The fixtures are built by shelling out to the studio's own Python
 * interpreter running a few lines against grade/mattes.py's own
 * write_gray_png (real PNG bytes, not invented ones), the same module
 * this lane's Python suite imports directly. No new dependency: node:child_
 * process is the runtime's own module.
 *
 * The layer that references them is written through #jsonApply, a real
 * button in the real app (app.js, "config applied"), the same "no private
 * JS global" rule every other spec in this suite already follows for
 * reading config; this is that same panel used to WRITE one, which is
 * exactly what it is for.
 *
 * Last in the mask specs on purpose: its final claim kills the real SAM
 * stub run.mjs started (ctx.stopSam(), the one PID captured at spawn) to
 * prove the service down badge and its start command against a service
 * that has actually stopped answering, not a simulation of down. Nothing
 * numbered after this spec needs the SAM stub alive.
 *
 * The claims, in order:
 *   1. A matte fixture on disk with state "failed" and an error renders
 *      [data-mask-comp]'s data-mask-state="failed" and a non-empty
 *      [data-mask-reason].
 *   2. A matte fixture with state "partial" and an error renders
 *      data-mask-state="partial" with its own reason.
 *   3. A matte fixture that is "done" but made for a different clip name
 *      renders data-mask-state="stale" (componentState's own clip-mismatch
 *      rule, checkpoints/M6.md's own documented behaviour), because the
 *      panel does not just trust a done state, it also checks the matte
 *      was made for what is on screen.
 *   4. Stopping the real SAM stub service flips [data-mask-service]'s
 *      data-mask-service-state to "down" and [data-mask-start-cmd] carries
 *      the real command from sam_client.py's own START_COMMAND (the same
 *      503 studio/server.py answers with).
 */

import { execFileSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const CONTENT_DIR = path.resolve(HERE, "..", "..", "..");
const PYTHON = path.join(CONTENT_DIR, ".venv", "bin", "python");

const FIXTURE_SCRIPT = `
import sys, os, json
sys.path.insert(0, "grade")
import numpy as np
import mattes as MT

spec = json.load(sys.stdin)
for m in spec["mattes"]:
    d = os.path.join(spec["dataDir"], "mattes", spec["clipKey"], m["id"])
    os.makedirs(d, exist_ok=True)
    w, h = int(m.get("width", 64)), int(m.get("height", 36))
    arr = np.full((h, w), 128, dtype=np.uint8)
    MT.write_gray_png(os.path.join(d, MT.frame_name(0)), arr)
    index = {
        "matte_id": m["id"], "clip": m["clip"], "clip_key": spec["clipKey"],
        "rotation": m["rotation"], "fps": 24.0, "frames": m.get("frames", 1),
        "width": w, "height": h, "state": m["state"],
        "done_frames": m.get("done_frames", 1), "areas": [0.5], "scores": [1.0],
        "created": "2026-09-08T00:00:00Z", "model": "m7-fixture", "backend": "m7-fixture",
    }
    if m.get("error"):
        index["error"] = m["error"]
    with open(os.path.join(d, MT.INDEX_NAME), "w") as f:
        json.dump(index, f)
print("ok " + str(len(spec["mattes"])))
`;

function buildFixtures(dataDir, clipKey, mattes) {
  return execFileSync(PYTHON, ["-c", FIXTURE_SCRIPT], {
    cwd: CONTENT_DIR,
    input: JSON.stringify({ dataDir, clipKey, mattes }),
    encoding: "utf8",
  }).trim();
}

function fail(evidence) {
  return { status: "FAIL", evidence: evidence };
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

async function waitForValue(page, fn, target, timeoutMs, args) {
  const deadline = Date.now() + timeoutMs;
  let last = null;
  while (Date.now() < deadline) {
    last = await page.evaluate(fn, ...(args || []));
    if (last === target) return last;
    await sleep(150);
  }
  return last;
}

export default async function run(ctx) {
  const page = ctx.page;
  const base = ctx.baseUrl;
  const notes = [];

  const clips = await fetch(base + "/api/clips").then((r) => r.json()).then((j) => j.clips || []);
  if (!clips.length) return { status: "SKIP", evidence: "no clips in content/footage" };
  const clipName = ctx.firstClip || clips[0].name;

  const getGrade = () => fetch(base + "/api/grade?clip=" + encodeURIComponent(clipName)).then((r) => r.json());
  const putGrade = (config) => fetch(base + "/api/grade", {
    method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ clip: clipName, config }),
  });
  const dropGrade = () => fetch(base + "/api/grade?clip=" + encodeURIComponent(clipName), { method: "DELETE" });
  const snap = await getGrade();

  try {
    // The rotation control is public DOM, read rather than guessed: spec 28
    // (which runs before this one) may have already moved it away from the
    // server default "auto" through its own real click, and this spec's
    // fixtures must match whatever is actually showing or the rotation-
    // mismatch half of componentState's stale rule would fire by accident.
    const rotation = await page.evaluate(() => {
      const b = document.querySelector('#rotSeg button[aria-pressed="true"]');
      return b ? b.getAttribute("data-rotation") : "auto";
    });
    notes.push("viewer rotation read as \"" + rotation + "\"");

    const clipKey = "m7fixtures-" + Date.now().toString(36);
    const mattes = [
      { id: "m7fail" + Date.now().toString(36), state: "failed", clip: clipName, rotation,
        error: "the model found nothing for m7 fixture subject" },
      { id: "m7partial" + Date.now().toString(36), state: "partial", clip: clipName, rotation,
        error: "RuntimeError: the stub was asked to fail on purpose" },
      { id: "m7stale" + Date.now().toString(36), state: "done", clip: "some-other-clip-m7.mov", rotation },
    ];
    const built = buildFixtures(ctx.dataDir, clipKey, mattes);
    notes.push("fixtures: " + built);

    // -- write the layer through the real #jsonApply button -----------------
    await page.click("#jsonBtn");
    await sleep(150);
    const currentText = await page.$eval("#jsonText", (el) => el.value);
    const cfg = JSON.parse(currentText);
    cfg.layers = [{
      enabled: true, name: "M7 fixture states", placement: "before_look",
      mask: { components: mattes.map((m, i) => ({
        id: "c" + i, type: "matte", op: "add", enabled: true, invert: false, feather: 0,
        name: ["failed fixture", "partial fixture", "stale fixture"][i],
        matte: { id: m.id, recipe: {} },
      })) },
      correct: {},
    }];
    await page.evaluate((text) => { document.getElementById("jsonText").value = text; },
      JSON.stringify(cfg));
    const applyBtn = await page.$("#jsonApply");
    if (!applyBtn) return fail("no #jsonApply button");
    await applyBtn.click();
    await sleep(400);

    const applyError = await page.$eval("#jsonError", (el) => el.textContent).catch(() => "");
    if (applyError) return fail("#jsonApply rejected the config: " + applyError);

    const sections = await page.evaluate(() => Array.prototype.map.call(
      document.querySelectorAll("[data-mask-layer]"), (el) => el.getAttribute("data-mask-layer")));
    if (!sections.length) return fail("applying the config did not render any [data-mask-layer] section");
    const sec = '[data-mask-layer="' + sections[sections.length - 1] + '"]';

    async function stateAndReason(j) {
      return page.evaluate((s, jj) => {
        const row = document.querySelector(s + ' [data-mask-comp="' + jj + '"]');
        if (!row) return null;
        const reason = row.querySelector("[data-mask-reason]");
        return { state: row.getAttribute("data-mask-state"), reason: reason ? reason.textContent : "" };
      }, sec, j);
    }

    async function waitForState(j, target, timeoutMs) {
      const deadline = Date.now() + (timeoutMs || 8000);
      let last = null;
      while (Date.now() < deadline) {
        last = await stateAndReason(j);
        if (last && last.state === target) return last;
        await sleep(150);
      }
      return last;
    }

    // -- 1: failed -----------------------------------------------------------
    const r0 = await waitForState(0, "failed");
    if (!r0 || r0.state !== "failed") {
      return fail("component 0 (failed fixture) read data-mask-state=\""
        + (r0 && r0.state) + "\", expected \"failed\"");
    }
    if (!r0.reason) return fail("component 0 is failed but [data-mask-reason] is empty");
    notes.push("failed: reason \"" + r0.reason + "\"");

    // -- 2: partial ------------------------------------------------------------
    const r1 = await waitForState(1, "partial");
    if (!r1 || r1.state !== "partial") {
      return fail("component 1 (partial fixture) read data-mask-state=\""
        + (r1 && r1.state) + "\", expected \"partial\"");
    }
    if (!r1.reason) return fail("component 1 is partial but [data-mask-reason] is empty");
    notes.push("partial: reason \"" + r1.reason + "\"");

    // -- 3: stale --------------------------------------------------------------
    const r2 = await waitForState(2, "stale");
    if (!r2 || r2.state !== "stale") {
      return fail("component 2 (done, but made for a different clip) read data-mask-state=\""
        + (r2 && r2.state) + "\", expected \"stale\" (componentState's own clip-mismatch check)");
    }
    if (!r2.reason) return fail("component 2 is stale but [data-mask-reason] is empty");
    notes.push("stale: reason \"" + r2.reason + "\"");

    // -- 4: the real service stops answering -----------------------------------
    const beforeState = await page.evaluate((s) => {
      const line = document.querySelector(s + " [data-mask-service]");
      return line ? line.getAttribute("data-mask-service-state") : null;
    }, sec);
    if (beforeState !== "ok") {
      return fail("expected [data-mask-service-state]=\"ok\" before stopping the SAM stub, read \""
        + beforeState + "\"");
    }
    await ctx.stopSam();
    notes.push("stopped the real SAM stub at " + ctx.samBase);

    const downState = await waitForValue(page, (s) => {
      const line = document.querySelector(s + " [data-mask-service]");
      return line ? line.getAttribute("data-mask-service-state") : null;
    }, "down", 12000, [sec]);
    if (downState !== "down") {
      return fail("[data-mask-service-state] read \"" + downState
        + "\" within 12s of stopping the SAM stub, expected \"down\"");
    }
    const startCmd = await page.evaluate((s) => {
      const c = document.querySelector(s + " [data-mask-start-cmd]");
      return c ? c.textContent : null;
    }, sec);
    if (!startCmd || startCmd.indexOf("server.py") < 0) {
      return fail("[data-mask-start-cmd] read " + JSON.stringify(startCmd)
        + ", expected it to name sam/server.py the way sam_client.py's START_COMMAND does");
    }
    notes.push("service down, start command: \"" + startCmd + "\"");

    return { status: "PASS", evidence: notes.join("; ") };
  } finally {
    if (snap && snap.exists) await putGrade(snap.config);
    else await dropGrade();
  }
}
