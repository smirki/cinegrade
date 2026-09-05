/* Power window shape editor (window-editor.js): the overlay is only worth
 * anything if a real drag on it moves the real config, so nothing here is
 * simulated. The pointer is driven with page.mouse (the same CDP input path
 * the input probe in spec 01 proved arrives), the config is read the way
 * spec 09 reads it (the JSON panel, which prints JSON.stringify(cfg())), and
 * the expected value is computed from the picture's own rendered width so
 * the assertion is "the shape followed the pointer across the picture", not
 * "some number changed".
 *
 * The claims, in order:
 *   1. #windowBtn shows the overlay before the window stage is enabled.
 *   2. Dragging the centre grip 120px right moves window.cx by exactly
 *      120 / (rendered picture width), and auto-enables the stage.
 *   3. That whole drag is ONE undo step: one #undoBtn press restores cx.
 *   4. A real drag on the rotation grip changes window.rotation.
 *   5. Arrow keys nudge the centre by one screen pixel, ten with shift.
 *   6. Unchecking the panel's enabled box and switching #windowBtn off
 *      hides the overlay again.
 */

const WINDOW_STAGE = 'section.stage[data-stage="window"]';

function fail(evidence) {
  return { status: "FAIL", evidence: evidence };
}

export default async function run(ctx) {
  const page = ctx.page;

  const need = ["mousedown", "mousemove", "mouseup"];
  const missing = need.filter((t) => !ctx.state.inputArrived || !ctx.state.inputArrived[t]);
  if (missing.length) {
    return {
      status: "SKIP",
      evidence: "input-probe found " + missing.join(" and ") + " never reach the page, a real drag cannot be performed",
    };
  }

  /* The JSON panel is the honest read of the live config: it is filled from
   * JSON.stringify(cfg()) by app.js itself, so it cannot drift from what the
   * renderer is using, and it needs no private global to be exposed. */
  async function readConfig() {
    await page.click("#jsonBtn");
    await new Promise((r) => setTimeout(r, 120));
    const text = await page.$eval("#jsonText", (el) => el.value);
    await page.evaluate(() => {
      var o = document.getElementById("jsonOverlay");
      if (o) o.classList.remove("on");
    });
    return JSON.parse(text);
  }

  /* Mirrors pictureRect() in window-editor.js: whichever of the three
   * elements inside #afterLayer is actually showing the frame right now. */
  async function pictureRect() {
    return page.evaluate(() => {
      var ids = ["frameImg", "gpuCanvas", "playerVideo"];
      for (var i = 0; i < ids.length; i++) {
        var n = document.getElementById(ids[i]);
        if (!n) continue;
        if (n.tagName === "IMG" && !n.naturalWidth) continue;
        var r = n.getBoundingClientRect();
        if (r.width > 1 && r.height > 1) return { w: r.width, h: r.height };
      }
      return null;
    });
  }

  async function centreOf(selector) {
    return page.evaluate((sel) => {
      var n = document.querySelector(sel);
      if (!n) return null;
      var r = n.getBoundingClientRect();
      if (!r.width && !r.height) return null;
      return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
    }, selector);
  }

  async function overlayOn() {
    return page.evaluate(() => {
      var n = document.getElementById("windowOverlay");
      return !!(n && n.classList.contains("on"));
    });
  }

  /* Waits for #windowOverlay to exist, then focuses it only if it is a real,
   * focusable element. #windowOverlay is an <svg tabindex="0"> (see build()
   * in window-editor.js), an SVGElement, not an HTMLElement, so an
   * `instanceof HTMLElement` check would reject the very element this is
   * meant to focus; checking for a callable .focus() accepts both HTML and
   * SVG elements and still refuses null. Puppeteer's own page.focus() does
   * require an HTMLElement and throws "Cannot focus non-HTMLElement" on
   * this SVG overlay outright (why this file calls the native DOM .focus()
   * through page.evaluate instead); under CPU load a read landing between a
   * re-render's remove and re-add of the overlay could also find nothing
   * there, which is what the wait below removes. */
  async function focusOverlay() {
    await page.waitForFunction(() => !!document.getElementById("windowOverlay"), { timeout: 5000 });
    await page.evaluate(() => {
      var el = document.getElementById("windowOverlay");
      if (el && typeof el.focus === "function") { el.focus(); }
    });
  }

  async function drag(from, dx, dy) {
    await page.mouse.move(from.x, from.y);
    await page.mouse.down();
    // Steps matter: a single jump would deliver one pointermove, and the
    // point of driving this for real is that the intermediate moves (the
    // uncommitted live edits) happen too.
    await page.mouse.move(from.x + dx, from.y + dy, { steps: 12 });
    await page.mouse.up();
    await new Promise((r) => setTimeout(r, 200));
  }

  const notes = [];

  /* A taller viewport than the harness default, for one reason: the footage
   * in content/footage is portrait (960x1706), so at 1440x900 the fitted
   * picture is only about 157px wide and a 120px drag would push the centre
   * clean off the right edge and into the 0..1 clamp, which would test the
   * clamp instead of the mapping. run.mjs resets the viewport before every
   * spec, so this affects nothing after this file. */
  await page.setViewport({ width: 1440, height: 1500 });
  await new Promise((r) => setTimeout(r, 400));

  /* This spec runs after reload-clean, which waits only for the panels to
   * exist. The overlay refuses to draw until a frame has actually decoded
   * (there is no rect to map fractions onto before that), so wait for the
   * picture rather than race it. */
  try {
    await page.waitForFunction(() => {
      var ids = ["frameImg", "gpuCanvas", "playerVideo"];
      for (var i = 0; i < ids.length; i++) {
        var n = document.getElementById(ids[i]);
        if (!n) continue;
        if (n.tagName === "IMG" && !n.naturalWidth) continue;
        var r = n.getBoundingClientRect();
        if (r.width > 1 && r.height > 1) return true;
      }
      return false;
    }, { timeout: 30000 });
  } catch (e) {
    return fail("no graded frame appeared in the viewer within 30s, the overlay has nothing to map onto");
  }

  /* Start from the shipped "flat" preset, loaded through the app's own
   * preset control. Per-clip grades (spec 12's feature) autosave, so a
   * previous run of THIS spec is saved against this clip and would otherwise
   * be the starting config: a window already enabled, already off centre.
   * Resetting through the real control is what makes the run repeatable
   * without reaching into any private state. */
  const hasFlat = await page.$eval("#presetSelect", (el) => {
    return Array.prototype.some.call(el.options, (o) => o.value === "flat");
  });
  if (!hasFlat) return fail('#presetSelect has no "flat" preset to reset the config from');
  await page.select("#presetSelect", "flat");
  await page.click("#loadPresetBtn");
  await new Promise((r) => setTimeout(r, 700));

  // --- 1. the toolbar button draws before the stage is enabled -------------
  const before = await readConfig();
  const win0 = (before && before.window) || {};
  if (win0.enabled) {
    return fail("window.enabled is true straight after loading the flat preset, so auto-enable cannot be observed");
  }
  if (await overlayOn()) {
    return fail("#windowOverlay was already showing with window.enabled false and #windowBtn off");
  }

  const hasBtn = await page.$("#windowBtn");
  if (!hasBtn) return fail("no #windowBtn in the viewer toolbar");
  await page.click("#windowBtn");
  await new Promise((r) => setTimeout(r, 200));
  if (!(await overlayOn())) {
    return fail("#windowBtn did not show #windowOverlay (window.enabled is still false, which is the case it exists for)");
  }
  notes.push("#windowBtn showed the overlay with window.enabled false");

  // --- 2. drag the centre grip --------------------------------------------
  const pic = await pictureRect();
  if (!pic) return fail("no picture element with a non-zero rect, there is nothing to map fractions onto");

  const grip = await centreOf("#windowOverlay .win-centre");
  if (!grip) return fail("no #windowOverlay .win-centre grip on screen");

  const cx0 = Number(win0.cx === undefined ? 0.5 : win0.cx);
  const DX = 120;
  /* cx is clamped to 0..1 by design, so a drag that would leave the frame
   * would be measuring the clamp, not the mapping. Say so rather than fail
   * on a number that is actually correct. */
  if (cx0 + DX / pic.w > 0.98) {
    return fail(
      "the fitted picture is only " + pic.w.toFixed(1) + "px wide, so a " + DX
      + "px drag from cx " + cx0 + " lands past the frame edge and hits the 0..1 clamp"
    );
  }
  await drag(grip, DX, 0);

  const afterDrag = await readConfig();
  const win1 = (afterDrag && afterDrag.window) || {};
  const cx1 = Number(win1.cx);
  const expected = cx0 + DX / pic.w;
  const err = Math.abs(cx1 - expected);
  if (!(err <= 0.005)) {
    return fail(
      "centre drag of " + DX + "px right on a " + pic.w.toFixed(1) + "px wide picture: cx "
      + cx0 + " -> " + cx1 + ", expected " + expected.toFixed(5) + " (off by " + err.toFixed(5) + ", tolerance 0.005)"
    );
  }
  if (win1.enabled !== true) {
    return fail("the drag moved cx to " + cx1 + " but window.enabled is " + String(win1.enabled) + ", the auto-enable did not fire");
  }
  notes.push(
    "centre drag " + DX + "px right on a " + pic.w.toFixed(1) + "px picture: cx " + cx0
    + " -> " + cx1.toFixed(5) + " (expected " + expected.toFixed(5) + ", off by " + err.toFixed(5) + ")"
  );
  notes.push("window.enabled auto-enabled false -> true");

  // --- 3. one drag is one undo step ----------------------------------------
  const undoDisabled = await page.$eval("#undoBtn", (el) => el.disabled);
  if (undoDisabled) return fail("cx changed but #undoBtn is disabled, so the drag committed no history step");
  await page.click("#undoBtn");
  await new Promise((r) => setTimeout(r, 200));
  const afterUndo = await readConfig();
  const win2 = (afterUndo && afterUndo.window) || {};
  if (Math.abs(Number(win2.cx) - cx0) > 1e-9) {
    return fail("one #undoBtn press left cx at " + win2.cx + ", expected it back at " + cx0 + " (the drag is meant to be one step, not many)");
  }
  notes.push("one undo restored cx to " + win2.cx);

  // --- 4. the rotation grip ------------------------------------------------
  // The undo above put window.enabled back to false; #windowBtn is still on,
  // which is exactly why the overlay is still drawable here.
  if (!(await overlayOn())) {
    return fail("after undo, #windowOverlay went away even though #windowBtn is still on");
  }
  const rotGrip = await centreOf("#windowOverlay .win-rot");
  if (!rotGrip) return fail("no #windowOverlay .win-rot grip on screen");
  const rot0 = Number(win2.rotation === undefined ? 0 : win2.rotation);
  await drag(rotGrip, 90, 40);
  const afterRot = await readConfig();
  const win3 = (afterRot && afterRot.window) || {};
  const rot1 = Number(win3.rotation);
  if (!isFinite(rot1) || Math.abs(rot1 - rot0) < 1) {
    return fail("a real drag on the rotation grip left window.rotation at " + win3.rotation + " (was " + rot0 + ")");
  }
  notes.push("rotation grip drag: rotation " + rot0 + " -> " + rot1);

  // --- 4b. keyboard nudge --------------------------------------------------
  // One screen pixel per press, ten with shift, and only while the overlay
  // has focus. The key events are real (CDP), the focus is set by script
  // because reading the config in between moves it to the JSON panel (and
  // because puppeteer's page.focus refuses a non-HTML element outright).
  // focusOverlay() waits for the overlay to exist and only calls .focus()
  // on a real, focusable element (see its own comment): this step once
  // failed under CPU load with "Cannot focus non-HTMLElement".
  const cxBeforeKeys = Number(win3.cx);
  await focusOverlay();
  await page.keyboard.press("ArrowRight");
  await new Promise((r) => setTimeout(r, 150));
  const afterKey1 = (await readConfig()).window || {};
  const step1 = Number(afterKey1.cx) - cxBeforeKeys;
  if (Math.abs(step1 - 1 / pic.w) > 1e-6) {
    return fail("ArrowRight moved cx by " + step1 + ", expected 1 screen pixel = " + (1 / pic.w));
  }
  await focusOverlay();
  await page.keyboard.down("Shift");
  await page.keyboard.press("ArrowRight");
  await page.keyboard.up("Shift");
  await new Promise((r) => setTimeout(r, 150));
  const afterKey2 = (await readConfig()).window || {};
  const step2 = Number(afterKey2.cx) - Number(afterKey1.cx);
  if (Math.abs(step2 - 10 / pic.w) > 1e-6) {
    return fail("shift+ArrowRight moved cx by " + step2 + ", expected 10 screen pixels = " + (10 / pic.w));
  }
  notes.push("arrow nudge: +" + step1.toFixed(6) + " (1px), shift +" + step2.toFixed(6) + " (10px) on a " + pic.w.toFixed(1) + "px picture");

  // --- 5. hiding it again --------------------------------------------------
  // The window section's first .ctl-switch is its "enabled" checkbox (the
  // schema puts it first; the second one is "invert").
  const enableBox = await page.$(WINDOW_STAGE + " input.ctl-switch");
  if (!enableBox) return fail("no enabled checkbox found in " + WINDOW_STAGE);
  const checked = await page.$eval(WINDOW_STAGE + " input.ctl-switch", (el) => el.checked);
  if (!checked) return fail("the window stage's enabled checkbox is unchecked after a drag auto-enabled it");
  await enableBox.click();
  await new Promise((r) => setTimeout(r, 200));
  if (!(await overlayOn())) {
    return fail("unchecking enabled hid the overlay while #windowBtn is still on, the button is supposed to hold it open");
  }
  await page.click("#windowBtn");
  await new Promise((r) => setTimeout(r, 200));
  if (await overlayOn()) {
    return fail("with window.enabled unchecked and #windowBtn off, #windowOverlay is still showing");
  }
  const end = await readConfig();
  if ((end.window || {}).enabled !== false) {
    return fail("the enabled checkbox click left window.enabled at " + String((end.window || {}).enabled));
  }
  notes.push("enabled unchecked plus #windowBtn off hides the overlay");

  /* Leave the clip's autosaved grade back on the flat preset. This spec is
   * the only one that edits the window, and leaving a rotated, off centre
   * window saved against the only clip in content/footage would be the next
   * run's starting state (and the next screenshot's). */
  await page.select("#presetSelect", "flat");
  await page.click("#loadPresetBtn");
  await new Promise((r) => setTimeout(r, 800));

  return { status: "PASS", evidence: notes.join("; ") };
}
