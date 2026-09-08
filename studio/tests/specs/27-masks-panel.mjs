/* Masks (SAM 3.1, contracts C1-C7, plan/2026-09-08-studio-masks/PLAN.md),
 * lane M7 (tests): the panel itself, against the REAL stub SAM service
 * run.mjs now starts next to studio/server.py (see its own comment on
 * SAM_PYTHON/SAM_SERVER). Nothing here is mocked: the service line's
 * "ok" reads a real GET /api/mask/status answer from a real subprocess,
 * and the add menu is the real studio/static/masks.js, built from its own
 * ADD_KINDS table, in the real browser.
 *
 * DOM ids and data attributes are M6's own contract (checkpoints/M6.md
 * section 1), written early "so M7 can write specs against names that are
 * already decided", so nothing here reaches for a private JS global.
 *
 * The claims, in order:
 *   1. Adding a layer (contract C1, the existing #layersAddBtn) gives it a
 *      mask panel: [data-mask-layer="0"] renders inside that layer's own
 *      body, with an empty component list and a working Add button.
 *   2. The service line reports the real stub healthy: data-mask-service-
 *      state flips from "checking" to "ok" once the panel's own boot poll
 *      (masks.js's init(), pollNow() on load) lands.
 *   3. The add menu opens on a real click and lists every kind the plan
 *      asks for, spot checked by kind name rather than by counting rows
 *      (a group heading is not a kind and must not be miscounted as one).
 *   4. At a phone viewport (spec 22's own PHONE preset) the panel is still
 *      genuinely laid out: visible, non-zero, and never wider than the
 *      viewport itself, on the Grade mobile page reached the same way
 *      spec 22 reaches it.
 */

const LAYERS_STAGE = 'section.stage[data-stage="layers"]';
const PHONE = { width: 390, height: 844, deviceScaleFactor: 3, isMobile: true, hasTouch: true };

function fail(evidence) {
  return { status: "FAIL", evidence: evidence };
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

async function waitFor(page, fn, timeoutMs, args) {
  const deadline = Date.now() + timeoutMs;
  let last;
  while (Date.now() < deadline) {
    last = await page.evaluate(fn, ...(args || []));
    if (last) return last;
    await sleep(100);
  }
  return last;
}

// Same reason spec 16 has waitForServerConfig/undoStillDisabled: the app is
// on a commit-based project model (contract C3, grades.js's own comment:
// "the debounced PUT /api/grade autosave is gone"), so a click that adds a
// layer commits ASYNCHRONOUSLY. A fixed sleep only proves the DOM updated
// in memory; it does not prove the server's own copy of this clip's config
// landed the new layer before some later action (here, the mobile Grade
// tab's real paramtab click) triggers a re-render that can revert to
// whatever the server still has on file. Poll the server's own config
// instead of guessing a sleep long enough.
async function waitForLayerCount(getGrade, count, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  let last = null;
  while (Date.now() < deadline) {
    const g = await getGrade();
    const layers = (g && g.config && g.config.layers) || [];
    last = layers.length;
    if (last === count) return last;
    await sleep(150);
  }
  return last;
}

export default async function run(ctx) {
  const page = ctx.page;
  const base = ctx.baseUrl;
  const notes = [];

  const clips = await fetch(base + "/api/clips").then((r) => r.json()).then((j) => j.clips || []);
  if (!clips.length) {
    return { status: "SKIP", evidence: "no clips in content/footage for a layer to hold a mask panel" };
  }

  // Same snapshot/restore discipline as spec 16: this writes into the
  // account's real grade for the first clip (through the real Add layer
  // button, the only way a mask panel exists at all), so the clip's own
  // grade is set aside first and put back in a finally block.
  const clipName = clips[0].name;
  const getGrade = () => fetch(base + "/api/grade?clip=" + encodeURIComponent(clipName)).then((r) => r.json());
  const putGrade = (config) => fetch(base + "/api/grade", {
    method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ clip: clipName, config }),
  });
  const dropGrade = () => fetch(base + "/api/grade?clip=" + encodeURIComponent(clipName), { method: "DELETE" });
  const snap = await getGrade();

  try {
    // -- 1: add a layer, the panel renders inside its body -----------------
    const addOk = await page.evaluate((sel) => {
      const stage = document.querySelector(sel);
      if (!stage) return "no " + sel;
      stage.scrollIntoView({ block: "center" });
      return null;
    }, LAYERS_STAGE);
    if (addOk) return fail(addOk);

    const before = await page.evaluate(() => (document.querySelectorAll('[data-mask-layer]').length));
    const beforeServerLayers = ((snap && snap.exists && snap.config && snap.config.layers) || []).length;
    const addBtn = await page.$("#layersAddBtn");
    if (!addBtn) return fail("no #layersAddBtn in " + LAYERS_STAGE);
    await addBtn.click();
    const committedLayers = await waitForLayerCount(getGrade, beforeServerLayers + 1, 8000);
    if (committedLayers !== beforeServerLayers + 1) {
      return fail("server's own /api/grade config still has " + committedLayers
        + " layer(s) 8s after clicking #layersAddBtn, expected " + (beforeServerLayers + 1)
        + " (the commit never landed server-side)");
    }

    const afterLayers = await page.evaluate(() => Array.prototype.map.call(
      document.querySelectorAll('[data-mask-layer]'), (el) => el.getAttribute("data-mask-layer")));
    if (afterLayers.length !== before + 1) {
      return fail("clicking #layersAddBtn once left " + afterLayers.length
        + " [data-mask-layer] section(s), expected " + (before + 1));
    }
    const layerIdx = afterLayers[afterLayers.length - 1];
    const sectionSel = '[data-mask-layer="' + layerIdx + '"]';
    notes.push("layer " + layerIdx + " has a mask panel section");

    const shape = await page.evaluate((sel) => {
      const root = document.querySelector(sel);
      if (!root) return null;
      return {
        hasAdd: !!root.querySelector("[data-mask-add]"),
        components: root.querySelectorAll("[data-mask-comp]").length,
        hasComponentsList: !!root.querySelector("[data-mask-components]"),
        hasService: !!root.querySelector("[data-mask-service]"),
      };
    }, sectionSel);
    if (!shape) return fail(sectionSel + " did not render");
    if (!shape.hasAdd) return fail(sectionSel + " has no [data-mask-add] button");
    if (!shape.hasComponentsList) return fail(sectionSel + " has no [data-mask-components] list");
    if (shape.components !== 0) {
      return fail(sectionSel + " started with " + shape.components + " component row(s), expected 0 on a fresh layer");
    }
    if (!shape.hasService) return fail(sectionSel + " has no [data-mask-service] line");
    notes.push("a fresh layer's mask stack is empty, with the Add button and the service line present");

    // -- 2: the service line reports the real stub healthy -----------------
    const serviceState = await waitFor(page, (sel) => {
      const line = document.querySelector(sel + " [data-mask-service]");
      return line ? line.getAttribute("data-mask-service-state") : null;
    }, 8000, [sectionSel]);
    if (serviceState !== "ok") {
      return fail(sectionSel + " [data-mask-service]'s data-mask-service-state read \""
        + serviceState + "\", expected \"ok\" against the real stub run.mjs starts");
    }
    notes.push("[data-mask-service-state]=\"ok\" against the real SAM stub run.mjs starts on " + ctx.samBase);

    // -- 3: the add menu opens and lists every kind -------------------------
    await page.click(sectionSel + " [data-mask-add]");
    await sleep(150);
    const menuKinds = await page.evaluate((sel) => Array.prototype.map.call(
      document.querySelectorAll(sel + " [data-mask-addmenu] [data-mask-add-kind]"),
      (el) => el.getAttribute("data-mask-add-kind")), sectionSel);
    const expectKinds = ["subject", "sky", "background", "person", "face", "hair", "lips",
      "eyes", "teeth", "clothes", "object", "text", "colour", "luma", "linear", "radial", "rect"];
    const missingKinds = expectKinds.filter((k) => menuKinds.indexOf(k) < 0);
    if (missingKinds.length) {
      return fail("[data-mask-addmenu] is missing kind(s): " + missingKinds.join(", ")
        + " (found: " + menuKinds.join(", ") + ")");
    }
    notes.push("add menu lists all " + expectKinds.length + " kinds: " + menuKinds.join(", "));
    // Close it the same way a person would: click elsewhere on the page.
    await page.click("body");
    await sleep(100);

    // -- 4: mobile layout ----------------------------------------------------
    await page.setViewport(PHONE);
    await sleep(200);
    const openedGrade = await page.evaluate(() => {
      const btns = Array.prototype.slice.call(document.querySelectorAll("#mobilebar .mobiletab"));
      const b = btns.filter((x) => x.textContent.trim() === "Grade")[0];
      if (!b) return false;
      b.click();
      return true;
    });
    if (!openedGrade) return fail("no Grade button in #mobilebar at the phone viewport");
    await sleep(300);
    await page.evaluate((sel) => {
      const el = document.querySelector(sel);
      if (el) el.scrollIntoView({ block: "center" });
    }, sectionSel);
    await sleep(200);

    const mobileShape = await page.evaluate((sel) => {
      const root = document.querySelector(sel);
      if (!root) return null;
      const r = root.getBoundingClientRect();
      const add = root.querySelector("[data-mask-add]");
      const ar = add ? add.getBoundingClientRect() : null;
      return { w: r.width, h: r.height, addW: ar ? ar.width : 0, addH: ar ? ar.height : 0 };
    }, sectionSel);
    if (!mobileShape) {
      const dbg = await page.evaluate(() => ({
        mobilepage: document.documentElement.getAttribute("data-mobilepage"),
        paramtab: document.documentElement.getAttribute("data-paramtab"),
        maskLayers: document.querySelectorAll("[data-mask-layer]").length,
        layerItems: document.querySelectorAll(".layer-item").length,
        stages: document.querySelectorAll("#params > section.stage").length,
        layersStageDisplay: (function () {
          var s = document.querySelector('section.stage[data-stage="layers"]');
          return s ? getComputedStyle(s).display : "no-stage";
        })(),
      }));
      return fail(sectionSel + " is not present on the mobile Grade page; debug=" + JSON.stringify(dbg));
    }
    if (mobileShape.w <= 0 || mobileShape.h <= 0) {
      return fail(sectionSel + " has zero size at the phone viewport (" + JSON.stringify(mobileShape) + ")");
    }
    if (mobileShape.w > PHONE.width) {
      return fail(sectionSel + " is " + mobileShape.w + "px wide, wider than the " + PHONE.width
        + "px phone viewport: it does not lay out, it overflows");
    }
    if (mobileShape.addW <= 0 || mobileShape.addH <= 0) {
      return fail("[data-mask-add] has zero size at the phone viewport, so it could not actually be pressed");
    }
    notes.push("at " + PHONE.width + "x" + PHONE.height + " the panel is " + Math.round(mobileShape.w)
      + "px wide (fits) with a pressable Add button");

    return { status: "PASS", evidence: notes.join("; ") };
  } finally {
    // Same discipline as spec 22: this spec is the only one before it in
    // numeric order that switches to the phone viewport, and every spec
    // after it in the same page session (28, 29, and anything a future
    // lane numbers above this) expects the desktop layout back.
    await page.setViewport(ctx.defaultViewport);
    if (snap && snap.exists) await putGrade(snap.config);
    else await dropGrade();
  }
}
