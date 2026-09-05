/* Viewer zoom, pan, and the two/four frame preview (contract E4,
 * plan/2026-09-05-studio-library/PLAN.md).
 *
 * Founder, 2026-09-05: "have more zoom controls for the frontend and for the
 * agent. for the preview window I should be able to put 4 frames. I should be
 * able to drag and drop from the preview timeline into the preview preview."
 *
 * Nothing here is simulated except the drag payload itself, and that one is
 * simulated the way the plan asks for (through the DataTransfer API): headless
 * Chrome will not start a native HTML5 drag from synthetic mouse input, so the
 * spec builds one DataTransfer, hands it to the filmstrip thumbnail's OWN
 * dragstart handler, and then hands the same object to the slot's own dragover
 * and drop handlers. Both halves of the app's code run for real; only the
 * browser's drag machinery in between is stood in for.
 *
 * The claims, in order:
 *   1. 200% writes 200% in the label and a scale(2) transform on #stage, and
 *      the picture on screen is twice the size it was.
 *   2. ctrl and the wheel over a point zooms around THAT point: the picture
 *      fraction under the pointer before the wheel is still under it after,
 *      inside a pixel.
 *   3. A drag pans: the picture moves with the pointer, both axes, at a zoom
 *      where it is bigger than the viewport on both.
 *   4. The window overlay follows the transform. Read through the picked
 *      rectangle (contract C7, window-editor.js setPick), which draws in the
 *      same overlay and writes nothing into anyone's config: its drawn box, in
 *      the overlay's own unscaled units, times the zoom, lands on the picture's
 *      real rect on screen.
 *   5. Frames 4 shows four slots, each with its own time, all four graded.
 *   6. Dropping two filmstrip thumbnails onto slots 3 and 4 sets those two
 *      times to the thumbnails' times and leaves the others alone.
 *   7. A full reload brings the count and the times back (they are stored on
 *      the project as an extra, not in the tab).
 *   8. POST /api/frame with a region answers with the region's dimensions, and
 *      with zoom 2 the same region comes back twice as big.
 */

const SLOT = "#framesGrid .fslot";

function fail(evidence) {
  return { status: "FAIL", evidence: evidence };
}

function near(a, b, tol) {
  return Math.abs(a - b) <= tol;
}

export default async function run(ctx) {
  const page = ctx.page;
  const notes = [];

  /* Whichever of the three possible picture elements is the one on screen,
   * measured the way window-editor.js and viewer-zoom.js both resolve it. */
  async function pictureRect() {
    return page.evaluate(() => {
      const ids = ["frameImg", "gpuCanvas", "playerVideo"];
      for (const id of ids) {
        const n = document.getElementById(id);
        if (!n) continue;
        if (n.tagName === "IMG" && !n.naturalWidth) continue;
        const r = n.getBoundingClientRect();
        if (r.width > 1 && r.height > 1) {
          return { left: r.left, top: r.top, width: r.width, height: r.height, id };
        }
      }
      return null;
    });
  }

  async function zoomState() {
    return page.evaluate(() => ({
      label: (document.getElementById("zoomLabel") || {}).textContent || "",
      transform: getComputedStyle(document.getElementById("stage")).transform,
      scale: window.ViewerZoom ? window.ViewerZoom.scale() : null,
      isFit: window.ViewerZoom ? window.ViewerZoom.isFit() : null,
    }));
  }

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  await page.click("#fitBtn");
  await sleep(400);
  const atFit = await pictureRect();
  if (!atFit) return fail("no picture in the viewer to zoom");
  const fitState = await zoomState();
  if (!/^fit /.test(fitState.label)) {
    return fail("the label does not say fit at fit: " + JSON.stringify(fitState.label));
  }

  /* ---- 1. 200% ----------------------------------------------------------- */

  await page.click("#zoom200Btn");
  await sleep(400);
  const at200 = await zoomState();
  const pic200 = await pictureRect();
  if (at200.label !== "200%") {
    return fail("the label after 200% is " + JSON.stringify(at200.label));
  }
  if (!/matrix\(2,\s*0,\s*0,\s*2/.test(at200.transform)) {
    return fail("#stage is not scaled by 2 at 200%: " + at200.transform);
  }
  if (!near(pic200.width / atFit.width, 2 / (fitState.scale || 1), 0.02)) {
    return fail("the picture did not grow by the right factor: fit "
      + atFit.width.toFixed(1) + "px at scale " + fitState.scale
      + ", 200% " + pic200.width.toFixed(1) + "px");
  }
  notes.push("fit " + Math.round(atFit.width) + "px (" + Math.round(fitState.scale * 100)
    + "%) -> 200% " + Math.round(pic200.width) + "px");

  /* ---- 2. wheel zoom around the pointer ---------------------------------- */

  /* Through CDP rather than page.mouse.wheel: the zoom is deliberately only
   * on ctrl or cmd (a plain wheel over the viewer has to keep scrolling the
   * column it sits in), and the modifier bitmask is what this event needs. */
  const cdp = await page.createCDPSession();
  const before = await pictureRect();
  /* A point inside the VIEWPORT, not just inside the picture: at 200% the
   * picture is wider than the viewport and hangs off both sides, and a wheel
   * dispatched over the clipped part lands on whatever is outside the viewer
   * instead of on the viewer. */
  const vpBox = await page.evaluate(() => {
    const r = document.getElementById("viewport").getBoundingClientRect();
    return { left: r.left, top: r.top, width: r.width, height: r.height };
  });
  const cx = Math.round(vpBox.left + vpBox.width * 0.35);
  const cy = Math.round(vpBox.top + vpBox.height * 0.35);
  const fx = (cx - before.left) / before.width;
  const fy = (cy - before.top) / before.height;
  await cdp.send("Input.dispatchMouseEvent", {
    type: "mouseWheel", x: cx, y: cy, deltaX: 0, deltaY: -120, modifiers: 2,
  });
  await sleep(350);
  const afterWheel = await pictureRect();
  const zoomedIn = afterWheel.width > before.width + 1;
  const heldX = afterWheel.left + fx * afterWheel.width;
  const heldY = afterWheel.top + fy * afterWheel.height;
  if (!zoomedIn) {
    return fail("ctrl and the wheel did not zoom in: " + before.width.toFixed(1)
      + "px -> " + afterWheel.width.toFixed(1) + "px");
  }
  if (!near(heldX, cx, 1.5) || !near(heldY, cy, 1.5)) {
    return fail("the point under the pointer moved: wanted (" + cx + ", " + cy
      + "), got (" + heldX.toFixed(1) + ", " + heldY.toFixed(1) + ")");
  }
  notes.push("wheel " + Math.round(before.width) + " -> " + Math.round(afterWheel.width)
    + "px, the point under the pointer held to "
    + Math.max(Math.abs(heldX - cx), Math.abs(heldY - cy)).toFixed(2) + "px");
  await cdp.detach().catch(() => { /* the page outlives this spec */ });

  /* ---- 3. pan ------------------------------------------------------------ */

  await page.click("#zoom400Btn");
  await sleep(400);
  const beforePan = await pictureRect();
  const vp = await page.evaluate(() => {
    const r = document.getElementById("viewport").getBoundingClientRect();
    return { left: r.left, top: r.top, width: r.width, height: r.height };
  });
  if (beforePan.width <= vp.width || beforePan.height <= vp.height) {
    return fail("400% did not make the picture bigger than the viewport, so a pan "
      + "would be clamped and prove nothing: picture " + Math.round(beforePan.width)
      + "x" + Math.round(beforePan.height) + ", viewport " + Math.round(vp.width)
      + "x" + Math.round(vp.height));
  }
  const startX = Math.round(vp.left + vp.width / 2);
  const startY = Math.round(vp.top + vp.height / 2);
  await page.mouse.move(startX, startY);
  await page.mouse.down();
  await page.mouse.move(startX - 30, startY - 20, { steps: 4 });
  await page.mouse.move(startX - 60, startY - 40, { steps: 4 });
  await page.mouse.up();
  await sleep(250);
  const afterPan = await pictureRect();
  const dx = afterPan.left - beforePan.left;
  const dy = afterPan.top - beforePan.top;
  if (!near(dx, -60, 3) || !near(dy, -40, 3)) {
    return fail("a drag did not pan the picture by the pointer's own movement: "
      + "wanted (-60, -40), got (" + dx.toFixed(1) + ", " + dy.toFixed(1) + ")");
  }
  notes.push("pan moved the picture by " + dx.toFixed(0) + ", " + dy.toFixed(0));

  /* ---- 4. the overlay follows the transform ------------------------------ */

  const overlay = await page.evaluate(() => {
    if (!window.WindowEditor || !window.WindowEditor.setPick) return null;
    window.WindowEditor.setPick({ crop: [0.25, 0.25, 0.75, 0.75], onChange: function () {} });
    const box = document.querySelector("#windowOverlay .pick-box");
    if (!box) { window.WindowEditor.setPick(null); return null; }
    const stage = document.getElementById("stage").getBoundingClientRect();
    const out = {
      x: parseFloat(box.getAttribute("x")),
      w: parseFloat(box.getAttribute("width")),
      y: parseFloat(box.getAttribute("y")),
      h: parseFloat(box.getAttribute("height")),
      stageLeft: stage.left, stageTop: stage.top,
      /* The transform on the box, not the on screen size of the picture:
         the SVG's user units are the untransformed ones, so this is the
         number that turns them back into screen pixels. */
      scale: window.ViewerZoom.stageScale(),
      shown: window.ViewerZoom.scale(),
    };
    window.WindowEditor.setPick(null);
    return out;
  });
  if (!overlay) return fail("the pick overlay could not be drawn to measure it");
  const picNow = await pictureRect();
  const drawnLeft = overlay.stageLeft + overlay.x * overlay.scale;
  const drawnWidth = overlay.w * overlay.scale;
  const wantLeft = picNow.left + 0.25 * picNow.width;
  const wantWidth = 0.5 * picNow.width;
  if (!near(drawnLeft, wantLeft, 2) || !near(drawnWidth, wantWidth, 2)) {
    return fail("the overlay does not follow the zoom: the 0.25..0.75 rectangle "
      + "draws at " + drawnLeft.toFixed(1) + "px wide " + drawnWidth.toFixed(1)
      + ", the picture says it should be at " + wantLeft.toFixed(1) + " wide "
      + wantWidth.toFixed(1));
  }
  notes.push("overlay rectangle lands within "
    + Math.max(Math.abs(drawnLeft - wantLeft), Math.abs(drawnWidth - wantWidth)).toFixed(2)
    + "px of the picture at " + Math.round(overlay.shown * 100) + "%");

  await page.click("#fitBtn");
  await sleep(300);

  /* ---- 5. four frames ---------------------------------------------------- */

  await page.select("#frameCount", "4");
  await page.waitForFunction(
    (sel) => document.querySelectorAll(sel + ":not(.off)").length === 4,
    { timeout: 10000 }, SLOT);
  await sleep(4000);            // four renders, one after another

  async function slotState() {
    return page.evaluate((sel) => {
      return Array.from(document.querySelectorAll(sel + ":not(.off)")).map((s) => {
        const r = s.getBoundingClientRect();
        const canvas = s.querySelector(".fcanvas");
        const img = s.querySelector(".fimg");
        return {
          tag: s.querySelector("b.ftime").textContent,
          w: Math.round(r.width), h: Math.round(r.height),
          painted: s.classList.contains("gpu")
            ? !!(canvas && canvas.width > 1)
            : !!(img && img.naturalWidth > 1),
          failed: s.classList.contains("failed"),
        };
      });
    }, SLOT);
  }

  const four = await slotState();
  if (four.length !== 4) return fail("Frames 4 shows " + four.length + " slots");
  const unpainted = four.filter((s) => !s.painted || s.failed);
  if (unpainted.length) {
    return fail("a slot has no graded picture in it: " + JSON.stringify(four));
  }
  const timesShown = four.map((s) => parseFloat(s.tag.split(":")[1]));
  if (new Set(timesShown.map((t) => t.toFixed(2))).size !== 4) {
    return fail("the four slots do not show four different times: "
      + JSON.stringify(four.map((s) => s.tag)));
  }
  notes.push("four slots at " + four.map((s) => s.tag).join(", "));

  /* ---- 6. drag a thumbnail onto a slot ----------------------------------- */

  const dropped = await page.evaluate((sel) => {
    const thumbs = Array.from(document.querySelectorAll("#thumbs img"));
    const slots = Array.from(document.querySelectorAll(sel + ":not(.off)"));
    if (thumbs.length < 12 || slots.length < 4) return null;
    function dragOnto(thumb, slot) {
      /* One DataTransfer, carried from the thumbnail's own dragstart handler
         to the slot's own drop handler, exactly as the browser would carry
         it. Headless Chrome will not begin a native drag from synthetic
         mouse input, so this is the honest stand-in the plan asks for. */
      const dt = new DataTransfer();
      thumb.dispatchEvent(new DragEvent("dragstart", { dataTransfer: dt, bubbles: true }));
      slot.dispatchEvent(new DragEvent("dragover", { dataTransfer: dt, bubbles: true, cancelable: true }));
      const over = slot.classList.contains("drop");
      slot.dispatchEvent(new DragEvent("drop", { dataTransfer: dt, bubbles: true, cancelable: true }));
      return { carried: dt.getData("application/x-studio-time"), over: over };
    }
    const a = dragOnto(thumbs[3], slots[2]);
    const b = dragOnto(thumbs[11], slots[3]);
    return {
      wanted: [Number(thumbs[3].dataset.t), Number(thumbs[11].dataset.t)],
      a: a, b: b,
    };
  }, SLOT);
  if (!dropped) return fail("no filmstrip thumbnails to drag from");
  if (!dropped.a.over || !dropped.b.over) {
    return fail("a slot did not accept the drag (no .drop highlight on dragover): "
      + JSON.stringify(dropped));
  }
  await sleep(3000);
  const afterDrop = await slotState();
  const gotThree = parseFloat(afterDrop[2].tag.split(":")[1]);
  const gotFour = parseFloat(afterDrop[3].tag.split(":")[1]);
  if (!near(gotThree, dropped.wanted[0], 0.02) || !near(gotFour, dropped.wanted[1], 0.02)) {
    return fail("the dropped thumbnails did not set the slot times: wanted "
      + JSON.stringify(dropped.wanted) + ", slots show " + gotThree + " and " + gotFour);
  }
  if (parseFloat(afterDrop[1].tag.split(":")[1]) !== timesShown[1]) {
    return fail("dropping on slots 3 and 4 moved slot 2 as well");
  }
  notes.push("dropped " + dropped.wanted.map((t) => t.toFixed(2)).join(" and ")
    + " onto slots 3 and 4");

  /* ---- 7. the slots survive a reload ------------------------------------- */

  await sleep(600);             // the project extra is written on the drop
  await page.reload({ waitUntil: "domcontentloaded", timeout: 30000 });
  await ctx.waitForBootComplete(20000);
  await page.waitForFunction(
    (sel) => document.querySelectorAll(sel + ":not(.off)").length === 4,
    { timeout: 15000 }, SLOT).catch(() => { /* asserted below with a message */ });
  await sleep(2500);
  const reloaded = await page.evaluate((sel) => ({
    count: document.getElementById("frameCount").value,
    tags: Array.from(document.querySelectorAll(sel + ":not(.off) b.ftime"))
      .map((b) => b.textContent),
  }), SLOT);
  if (reloaded.count !== "4" || reloaded.tags.length !== 4) {
    return fail("Frames did not come back as 4 after a reload: "
      + JSON.stringify(reloaded));
  }
  const backThree = parseFloat(reloaded.tags[2].split(":")[1]);
  const backFour = parseFloat(reloaded.tags[3].split(":")[1]);
  if (!near(backThree, dropped.wanted[0], 0.02) || !near(backFour, dropped.wanted[1], 0.02)) {
    return fail("the slot times did not survive the reload: wanted "
      + JSON.stringify(dropped.wanted) + ", got " + backThree + " and " + backFour);
  }
  notes.push("after a reload: " + reloaded.tags.join(", "));

  /* ---- 8. the region on POST /api/frame ---------------------------------- */

  const region = await page.evaluate(async () => {
    async function frame(body) {
      const r = await fetch("/api/frame", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!r.ok) {
        let msg = r.status;
        try { msg = (await r.json()).error; } catch (e) { /* body was not json */ }
        return { error: String(msg) };
      }
      await r.blob();
      return {
        size: r.headers.get("X-Frame-Size"),
        pixels: r.headers.get("X-Frame-Region-Pixels"),
        full: r.headers.get("X-Frame-Full-Size"),
      };
    }
    const proj = await fetch("/api/project").then((x) => x.json());
    const name = proj && proj.name;
    if (!name) return { error: "no open project to render a frame of" };
    const base = { clip: name, time: 0.5, width: 480, config: {} };
    return {
      clip: name,
      whole: await frame(base),
      half: await frame(Object.assign({ region: [0.25, 0.25, 0.75, 0.75] }, base)),
      zoomed: await frame(Object.assign({ region: [0.25, 0.25, 0.75, 0.75], zoom: 2 }, base)),
    };
  });
  if (!region || region.error) return fail("the region request failed: " + JSON.stringify(region));
  const [wholeW, wholeH] = String(region.whole.size).split("x").map(Number);
  const [halfW, halfH] = String(region.half.size).split("x").map(Number);
  const [zoomW] = String(region.zoomed.size).split("x").map(Number);
  if (!near(halfW, Math.round(wholeW / 2), 2) || !near(halfH, Math.round(wholeH / 2), 2)) {
    return fail("a half sized region did not come back half sized: whole "
      + region.whole.size + ", region " + region.half.size);
  }
  if (!near(zoomW, halfW * 2, 3)) {
    return fail("zoom 2 did not double the region: " + region.half.size
      + " -> " + region.zoomed.size);
  }
  /* A region too small to render is refused with a 400 naming the axis. That
   * is asserted in studio/tests/py/test_frame_region.py rather than here on
   * purpose: a deliberate 400 in the browser writes a console error into the
   * run's shared log, which is exactly what spec 02 fails a boot for. */
  notes.push("frame " + region.whole.size + ", region " + region.half.size
    + ", region at zoom 2 " + region.zoomed.size);

  /* Back to one frame and Fit, so the next run of this spec (and anything a
   * later spec measures in the viewer) starts where every other spec does. */
  await page.select("#frameCount", "1");
  await sleep(500);
  await page.click("#fitBtn");
  await sleep(300);

  return { status: "PASS", evidence: notes.join("; ") };
}
