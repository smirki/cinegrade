/* The rectangle you pick before you match (contract C7).
 *
 * Founder, 2026-09-05: "i need to be able to use the rectangle mask tool to
 * pick what i want before I use match reference." So this drives the real
 * tools with the real pointer (page.mouse, the same CDP input path spec 01
 * proved arrives), reads the real readout, and checks the rectangle against
 * the real server through GET /api/project rather than any private state.
 *
 * The claims, in order:
 *   1. Picking is off until asked for: with Pick off, a drag on the reference
 *      draws nothing.
 *   2. Pick on, a real drag on the reference draws a rectangle whose four
 *      fractions match where the pointer went, and the readout prints them.
 *   3. That rectangle is on the PROJECT, not in the page:
 *      GET /api/project?clip=... has it under extras.match_crops[REF].ref.
 *   4. A corner handle resizes it, and the new rectangle is saved too.
 *   5. After a full page reload, selecting the same reference brings the
 *      rectangle back.
 *   6. Pick on frame draws into the SAME overlay the power window uses, and
 *      switching it off leaves that overlay exactly as it was (the layer's
 *      window is not disturbed by picking).
 *   7. A real match reports which rectangles it measured.
 *
 * The cube a match writes lands in grade/luts/looks, which is shared and not
 * under the harness's temp data dir, so this spec deletes any look that was
 * not there before it ran and leaves the rest alone.
 */

function fail(evidence) {
  return { status: "FAIL", evidence: evidence };
}

const REF_BOX = { x0: 0.20, y0: 0.25, x1: 0.70, y1: 0.65 };
const TOL = 0.02;   // two decimals of readout, plus a pixel of rounding

export default async function run(ctx) {
  const page = ctx.page;
  const notes = [];

  /* Spec 01 measures whether synthetic input reaches the page at all. When
   * it has run and says no, there is no point pretending to drag; when it
   * has not run (this file being driven on its own), just try. */
  const need = ["mousedown", "mousemove", "mouseup"];
  const probe = ctx.state.inputArrived;
  const missing = probe ? need.filter((t) => !probe[t]) : [];
  if (missing.length) {
    return {
      status: "SKIP",
      evidence: "input-probe found " + missing.join(" and ") + " never reach the page, a real drag cannot be performed",
    };
  }

  async function rectOf(sel) {
    return page.evaluate((s) => {
      const n = document.querySelector(s);
      if (!n) return null;
      const r = n.getBoundingClientRect();
      if (!(r.width > 1 && r.height > 1)) return null;
      return { left: r.left, top: r.top, width: r.width, height: r.height };
    }, sel);
  }

  async function drag(from, to, steps) {
    await page.mouse.move(from.x, from.y);
    await page.mouse.down();
    await page.mouse.move(to.x, to.y, { steps: steps || 12 });
    await page.mouse.up();
    await new Promise((r) => setTimeout(r, 250));
  }

  function same(a, b) {
    return a && b && Math.abs(a.left - b.left) < 1 && Math.abs(a.top - b.top) < 1
      && Math.abs(a.width - b.width) < 1 && Math.abs(a.height - b.height) < 1;
  }

  /* A drag on the reference, in fractions of the reference, repeated if the
   * picture moved under it.
   *
   * It can: the readout under the image grows from "whole image" to a
   * wrapped two line rectangle the moment the first rectangle exists, which
   * makes #matchRef taller, #refImgWrap shorter and the centred image
   * smaller. A drag measured against the rect from before that reflow lands
   * somewhere else on the picture, which is a measuring mistake in this
   * file, not a bug in the tool: the app itself reads the rect at mousedown
   * and again on every resize. The second attempt runs after the readout has
   * already reached its final size, so it is stable. */
  async function refDrag(f0, f1) {
    let rect = null;
    for (let attempt = 0; attempt < 3; attempt++) {
      const before = await rectOf("#refImg");
      if (!before) return null;
      const p = (f) => ({ x: before.left + f[0] * before.width,
                          y: before.top + f[1] * before.height });
      await drag(p(f0), p(f1));
      rect = await rectOf("#refImg");
      if (same(before, rect)) return rect;
    }
    return rect;
  }

  async function readout() {
    return page.$eval("#matchCropReadout", (el) => el.textContent || "");
  }

  function parseLine(text, label) {
    const line = String(text).split("\n").find((l) => l.trim().startsWith(label));
    if (!line) return null;
    if (/whole image/.test(line)) return "whole";
    const nums = line.match(/-?\d+\.\d\d/g);
    return nums && nums.length >= 4 ? nums.slice(0, 4).map(Number) : null;
  }

  /* Which clip the app has open, read off the app's own source readout
   * (#clipInfo's first row is kv("file", entry.name) in drawClipInfo), so
   * this asks the server about the same project the page is writing to even
   * if an earlier spec left a different clip selected. */
  async function openClip() {
    const shown = await page.evaluate(() => {
      const n = document.querySelector("#clipInfo div span");
      return n ? n.textContent.trim() : "";
    });
    return shown || ctx.firstClip;
  }

  async function storedCrops() {
    const name = await openClip();
    const res = await fetch(ctx.baseUrl + "/api/project?clip=" + encodeURIComponent(name));
    if (!res.ok) return {};
    const body = await res.json();
    return (body && body.extras && body.extras.match_crops) || {};
  }

  const looksBefore = await fetch(ctx.baseUrl + "/api/looks")
    .then((r) => r.json())
    .then((j) => new Set((j.looks || []).map((l) => l.name)))
    .catch(() => new Set());

  // --- select a reference -------------------------------------------------
  const refCount = await page.$$eval("#refList img", (els) => els.length);
  if (!refCount) return { status: "SKIP", evidence: "no images in refs/, there is nothing to pick on" };
  const refName = await page.$eval("#refList img", (el) => el.title);
  await page.click("#refList img");
  try {
    await page.waitForFunction(() => {
      const im = document.getElementById("refImg");
      return !!(im && im.naturalWidth > 0 && im.getBoundingClientRect().width > 20);
    }, { timeout: 20000 });
  } catch (e) {
    return fail("the reference image never appeared in #refPane, there is nothing to draw on");
  }
  notes.push("reference " + refName + " shown beside the frame");

  let img = await rectOf("#refImg");
  if (!img) return fail("#refImg has no rendered rect");
  const at = (fx, fy) => ({ x: img.left + fx * img.width, y: img.top + fy * img.height });

  // --- 1. with Pick off, a drag draws nothing ------------------------------
  await drag(at(0.3, 0.3), at(0.6, 0.6));
  let on = await page.$eval("#refCropBox", (el) => el.classList.contains("on"));
  if (on) return fail("a drag on the reference drew a rectangle with the Pick tool switched OFF");
  notes.push("with Pick off, a drag on the reference draws nothing");

  // --- 2. pick, then drag --------------------------------------------------
  await page.click("#pickRefBtn");
  await new Promise((r) => setTimeout(r, 120));
  const armed = await page.evaluate(() => ({
    btn: document.getElementById("pickRefBtn").classList.contains("active"),
    wrap: document.getElementById("refImgWrap").classList.contains("picking"),
  }));
  if (!armed.btn || !armed.wrap) {
    return fail("#pickRefBtn did not arm the tool: " + JSON.stringify(armed));
  }

  img = await refDrag([REF_BOX.x0, REF_BOX.y0], [REF_BOX.x1, REF_BOX.y1]);
  if (!img) return fail("#refImg lost its rendered rect during the drag");
  on = await page.$eval("#refCropBox", (el) => el.classList.contains("on"));
  if (!on) return fail("a real drag with Pick on did not draw a rectangle on the reference");

  const handles = await page.$$eval("#refCropBox .crophandle", (els) => els.length);
  if (handles !== 8) return fail("expected 8 resize handles on the reference rectangle, got " + handles);

  let text = await readout();
  let shown = parseLine(text, "reference");
  if (!Array.isArray(shown)) return fail("the readout does not print the reference rectangle: " + JSON.stringify(text));
  const want = [REF_BOX.x0, REF_BOX.y0, REF_BOX.x1, REF_BOX.y1];
  for (let i = 0; i < 4; i++) {
    if (Math.abs(shown[i] - want[i]) > TOL) {
      return fail("the drawn rectangle is not where the pointer went: readout "
        + JSON.stringify(shown) + " against " + JSON.stringify(want));
    }
  }
  notes.push("drag " + want.map((v) => v.toFixed(2)).join(" ") + " reads back as " + shown.join(" "));

  // --- 3. it is on the project, not in the page ---------------------------
  let crops = await storedCrops();
  let saved = crops[refName] && crops[refName].ref;
  if (!Array.isArray(saved)) {
    return fail("GET /api/project has no extras.match_crops[" + refName + "].ref, so an agent cannot see the pick: "
      + JSON.stringify(crops));
  }
  for (let i = 0; i < 4; i++) {
    if (Math.abs(saved[i] - want[i]) > TOL) {
      return fail("the saved rectangle is not the drawn one: " + JSON.stringify(saved));
    }
  }
  notes.push("saved on the project as " + saved.map((v) => v.toFixed(3)).join(" "));

  // --- 4. a handle resizes it ---------------------------------------------
  const eastBefore = shown[2];
  img = (await rectOf("#refImg")) || img;   // at() below must use today's rect
  const east = await page.evaluate(() => {
    const h = document.querySelector('#refCropBox .crophandle[data-role="e"]');
    if (!h) return null;
    const r = h.getBoundingClientRect();
    return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
  });
  if (!east) return fail("no east handle on the reference rectangle");
  await drag(east, at(0.85, (REF_BOX.y0 + REF_BOX.y1) / 2));
  text = await readout();
  shown = parseLine(text, "reference");
  if (!Array.isArray(shown) || Math.abs(shown[2] - 0.85) > TOL) {
    return fail("dragging the east handle to 0.85 left x1 at " + JSON.stringify(shown)
      + " (was " + eastBefore + ")");
  }
  crops = await storedCrops();
  saved = crops[refName].ref;
  if (Math.abs(saved[2] - 0.85) > TOL) {
    return fail("the resize was not saved: " + JSON.stringify(saved));
  }
  notes.push("east handle moved x1 " + eastBefore + " to " + shown[2] + ", saved");
  const afterResize = shown.slice();

  // --- 5. it survives a reload --------------------------------------------
  await page.reload({ waitUntil: "domcontentloaded", timeout: 30000 });
  await ctx.waitForBootComplete(20000);
  await new Promise((r) => setTimeout(r, 400));
  await page.click("#refList img");
  await page.waitForFunction(() => {
    const im = document.getElementById("refImg");
    return !!(im && im.naturalWidth > 0);
  }, { timeout: 20000 });
  await new Promise((r) => setTimeout(r, 300));
  text = await readout();
  shown = parseLine(text, "reference");
  if (!Array.isArray(shown)) {
    return fail("after a reload the reference rectangle is gone: " + JSON.stringify(text));
  }
  for (let i = 0; i < 4; i++) {
    if (Math.abs(shown[i] - afterResize[i]) > TOL) {
      return fail("after a reload the rectangle came back different: "
        + JSON.stringify(shown) + " against " + JSON.stringify(afterResize));
    }
  }
  const drawn = await page.$eval("#refCropBox", (el) => el.classList.contains("on"));
  if (!drawn) return fail("after a reload the rectangle is in the readout but not drawn on the picture");
  notes.push("after a reload the rectangle came back as " + shown.join(" "));

  // --- 6. the frame pick shares the window overlay -------------------------
  const overlayBefore = await page.evaluate(() => {
    const n = document.getElementById("windowOverlay");
    return n ? { on: n.classList.contains("on"), picking: n.classList.contains("picking") } : null;
  });
  if (!overlayBefore) return fail("no #windowOverlay in the viewer");
  await page.waitForFunction(() => {
    const ids = ["frameImg", "gpuCanvas", "playerVideo"];
    return ids.some((id) => {
      const n = document.getElementById(id);
      if (!n || (n.tagName === "IMG" && !n.naturalWidth)) return false;
      const r = n.getBoundingClientRect();
      return r.width > 1 && r.height > 1;
    });
  }, { timeout: 30000 });

  await page.click("#pickFrameBtn");
  await new Promise((r) => setTimeout(r, 200));
  const picking = await page.evaluate(() => {
    const n = document.getElementById("windowOverlay");
    return { on: n.classList.contains("on"), picking: n.classList.contains("picking") };
  });
  if (!picking.picking || !picking.on) {
    return fail("Pick on frame did not put the window overlay into pick mode: " + JSON.stringify(picking));
  }

  const pic = await page.evaluate(() => {
    const ids = ["frameImg", "gpuCanvas", "playerVideo"];
    for (const id of ids) {
      const n = document.getElementById(id);
      if (!n || (n.tagName === "IMG" && !n.naturalWidth)) continue;
      const r = n.getBoundingClientRect();
      if (r.width > 1 && r.height > 1) return { left: r.left, top: r.top, width: r.width, height: r.height };
    }
    return null;
  });
  if (!pic) return fail("no picture element with a rect to pick on");
  const atFrame = (fx, fy) => ({ x: pic.left + fx * pic.width, y: pic.top + fy * pic.height });
  await drag(atFrame(0.15, 0.20), atFrame(0.75, 0.60));

  text = await readout();
  const frameShown = parseLine(text, "frame");
  if (!Array.isArray(frameShown)) {
    return fail("the readout does not print the frame rectangle: " + JSON.stringify(text));
  }
  const wantFrame = [0.15, 0.20, 0.75, 0.60];
  for (let i = 0; i < 4; i++) {
    if (Math.abs(frameShown[i] - wantFrame[i]) > TOL) {
      return fail("the frame rectangle is not where the pointer went: "
        + JSON.stringify(frameShown) + " against " + JSON.stringify(wantFrame));
    }
  }
  crops = await storedCrops();
  const savedFrame = crops[refName] && crops[refName].frame;
  if (!Array.isArray(savedFrame) || Math.abs(savedFrame[0] - 0.15) > TOL) {
    return fail("the frame rectangle was not saved on the project: " + JSON.stringify(crops[refName]));
  }
  notes.push("frame rectangle " + frameShown.join(" ") + ", saved on the project");

  // The layer's own window must be untouched by all of that, and switching
  // the pick off must put the overlay back where it was.
  await page.click("#pickFrameBtn");
  await new Promise((r) => setTimeout(r, 200));
  const overlayAfter = await page.evaluate(() => {
    const n = document.getElementById("windowOverlay");
    return { on: n.classList.contains("on"), picking: n.classList.contains("picking") };
  });
  if (overlayAfter.picking) return fail("the overlay is still in pick mode after switching Pick on frame off");
  if (overlayAfter.on !== overlayBefore.on) {
    return fail("picking changed what the window overlay was showing: before "
      + JSON.stringify(overlayBefore) + ", after " + JSON.stringify(overlayAfter));
  }
  notes.push("the window overlay went back to on=" + overlayAfter.on + " after picking");

  // --- 7. a real match names the rectangles --------------------------------
  await page.click("#matchRefBtn");
  try {
    await page.waitForFunction(() => {
      const el = document.getElementById("matchRefResult");
      return !!(el && /measured:/.test(el.textContent || ""));
    }, { timeout: 120000 });
  } catch (e) {
    const status = await page.$eval("#matchRefResult", (el) => el.textContent || "").catch(() => "");
    return fail("the match never reported which rectangles it measured: " + JSON.stringify(status.slice(0, 300)));
  }
  const measured = await page.$eval("#matchRefResult", (el) => {
    const line = Array.prototype.map.call(el.children, (c) => c.textContent || "")
      .find((t) => t.indexOf("measured:") === 0);
    return line || "";
  });
  const wantRefText = afterResize.map((v) => v.toFixed(2)).join(" ");
  const wantFrameText = frameShown.map((v) => v.toFixed(2)).join(" ");
  if (measured.indexOf(wantRefText) < 0 || measured.indexOf(wantFrameText) < 0) {
    return fail("the match readout does not name both rectangles. wanted \"" + wantRefText
      + "\" and \"" + wantFrameText + "\", got: " + JSON.stringify(measured));
  }
  notes.push(measured);

  /* Back to the shipped "flat" preset before the cube is deleted below, so
   * no later spec (or a later run of this one) inherits a config pointing at
   * a look LUT that is no longer on disk. */
  try {
    const hasFlat = await page.$eval("#presetSelect", (el) =>
      Array.prototype.some.call(el.options, (o) => o.value === "flat"));
    if (hasFlat) {
      await page.select("#presetSelect", "flat");
      await page.click("#loadPresetBtn");
      await new Promise((r) => setTimeout(r, 700));
    }
  } catch (e) { /* the cleanup below is what matters */ }

  // Leave grade/luts/looks as it was found, minus nothing and plus nothing.
  try {
    const after = await fetch(ctx.baseUrl + "/api/looks").then((r) => r.json());
    for (const l of after.looks || []) {
      if (!looksBefore.has(l.name)) {
        await fetch(ctx.baseUrl + "/api/look?name=" + encodeURIComponent(l.name), { method: "DELETE" });
      }
    }
  } catch (e) { /* best effort: a leftover cube is not a test failure */ }

  return { status: "PASS", evidence: notes.join("; ") };
}
