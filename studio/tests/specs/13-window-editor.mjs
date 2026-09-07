/* Power window shape editor (window-editor.js): the overlay is only worth
 * anything if a real drag on it moves the real config, so nothing here is
 * simulated. The pointer is driven with page.mouse (the same CDP input path
 * the input probe in spec 01 proved arrives), the config is read the way
 * spec 09 reads it (the JSON panel, which prints JSON.stringify(cfg())), and
 * the expected value is computed from the picture's own rendered width so
 * the assertion is "the shape followed the pointer across the picture", not
 * "some number changed".
 *
 * Contract C1 (layers, plan/2026-09-04-studio-layers/PLAN.md) moved the
 * window from config.window into config.layers[i].mask.window, and the
 * whole "secondary" plus "window" pair of stages became one "layers" stage
 * (studio/static/layers.js). This spec was written for the old config.window
 * shape; it is rewritten here against the new one now that
 * plan/2026-09-04-studio-layers/checkpoints/L1.md has confirmed the exact
 * path convention (see its "Config path convention for lane L1u" section).
 * The shipped "flat" preset carries no secondary or window block, so
 * loading it gives an EMPTY layers array (config.layers = []), same as
 * before layers existed at all: this spec still starts from that clean
 * slate and the Window button still has to create a layer before there is
 * anything to draw, exactly the "if no layer exists, the Window button
 * creates one and selects it" line in the contract.
 *
 * The claims, in order:
 *   1. #windowBtn shows the overlay with no layer selected (there is none
 *      yet), and creates and selects layer 0.
 *   2. Dragging the centre grip 120px right moves
 *      layers[0].mask.window.cx by exactly 120 / (rendered picture width),
 *      and auto-enables layers[0].mask.window.enabled.
 *   3. That whole drag is ONE undo step: one #undoBtn press restores cx and
 *      mask.window.enabled, leaving the layer itself (created by step 1, a
 *      separate commit) in place.
 *   4. A real drag on the rotation grip changes layers[0].mask.window.rotation.
 *   5. Arrow keys nudge the centre by one screen pixel, ten with shift.
 *   6. Unchecking the layer's own Window-enabled box and switching
 *      #windowBtn off hides the overlay again.
 */

const LAYERS_STAGE = 'section.stage[data-stage="layers"]';

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

  function layer0(cfg) {
    return (cfg && Array.isArray(cfg.layers) && cfg.layers[0]) || null;
  }
  function windowOf(cfg) {
    var l = layer0(cfg);
    return (l && l.mask && l.mask.window) || {};
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
   * be the starting config: a layer already there, a window already off
   * centre. Resetting through the real control is what makes the run
   * repeatable without reaching into any private state. */
  const hasFlat = await page.$eval("#presetSelect", (el) => {
    return Array.prototype.some.call(el.options, (o) => o.value === "flat");
  });
  if (!hasFlat) return fail('#presetSelect has no "flat" preset to reset the config from');
  await page.select("#presetSelect", "flat");
  await page.click("#loadPresetBtn");
  await new Promise((r) => setTimeout(r, 700));

  // --- 1. the toolbar button creates and selects a layer, and shows the
  //        overlay, when none existed --------------------------------------
  const before = await readConfig();
  if (!Array.isArray(before.layers) || before.layers.length !== 0) {
    return fail(
      'the "flat" preset is expected to carry no layers (config.layers === []); got '
      + JSON.stringify(before.layers) + ". The rest of this spec assumes a clean slate; "
      + "if flat now ships a layer this spec needs updating, not the app."
    );
  }
  if (await overlayOn()) {
    return fail("#windowOverlay was already showing with config.layers empty and #windowBtn off");
  }

  const hasBtn = await page.$("#windowBtn");
  if (!hasBtn) return fail("no #windowBtn in the viewer toolbar");
  await page.click("#windowBtn");
  await new Promise((r) => setTimeout(r, 200));
  if (!(await overlayOn())) {
    return fail("#windowBtn did not show #windowOverlay (config.layers was empty, which is the case it exists for)");
  }
  const afterBtn = await readConfig();
  if (!Array.isArray(afterBtn.layers) || afterBtn.layers.length !== 1) {
    return fail("#windowBtn was clicked with no layers, expected exactly one layer afterwards, got " + JSON.stringify(afterBtn.layers));
  }
  notes.push("#windowBtn created layer 0 and showed the overlay with no window enabled yet");

  // --- 2. drag the centre grip --------------------------------------------
  const pic = await pictureRect();
  if (!pic) return fail("no picture element with a non-zero rect, there is nothing to map fractions onto");

  const grip = await centreOf("#windowOverlay .win-centre");
  if (!grip) return fail("no #windowOverlay .win-centre grip on screen");

  const win0 = windowOf(afterBtn);
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
  const win1 = windowOf(afterDrag);
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
    return fail("the drag moved cx to " + cx1 + " but layers[0].mask.window.enabled is " + String(win1.enabled) + ", the auto-enable did not fire");
  }
  notes.push(
    "centre drag " + DX + "px right on a " + pic.w.toFixed(1) + "px picture: cx " + cx0
    + " -> " + cx1.toFixed(5) + " (expected " + expected.toFixed(5) + ", off by " + err.toFixed(5) + ")"
  );
  notes.push("layers[0].mask.window.enabled auto-enabled false -> true");

  // --- 3. one drag is one undo step ----------------------------------------
  /* The drag's OWN commit has to be on the server before Undo is pressed.
     #undoBtn is already enabled here by the commits earlier in this spec (the
     preset load, the window button), so its enabled state never proved that
     the drag landed, and an Undo pressed while the drag is still in flight
     pops the PREVIOUS commit and lets the drag's commit land on top of that
     undone head: the wrong commit is undone, cx stays where the drag put it,
     and no amount of waiting afterwards recovers it. Read the server from the
     node side so this poll does not queue behind the page's own six
     connections. */
  let clipForGrade = "";
  try {
    const cl = await fetch(ctx.baseUrl + "/api/clips").then((r) => r.json());
    clipForGrade = (((cl && cl.clips) || [])[0] || {}).name || "";
  } catch (e) {
    clipForGrade = "";
  }
  if (clipForGrade) {
    const gradeUrl = ctx.baseUrl + "/api/grade?clip=" + encodeURIComponent(clipForGrade);
    const landed = () =>
      fetch(gradeUrl)
        .then((r) => r.json())
        .then((g) => {
          const w = g && g.exists ? windowOf(g.config) : {};
          return w.enabled === true && Math.abs(Number(w.cx) - cx1) <= 1e-9;
        })
        .catch(() => false);
    const landDeadline = Date.now() + 20000;
    let onServer = await landed();
    while (!onServer && Date.now() < landDeadline) {
      await new Promise((r) => setTimeout(r, 120));
      onServer = await landed();
    }
    if (!onServer) {
      const g = await fetch(gradeUrl).then((r) => r.json()).catch(() => ({}));
      return fail("the drag's commit never reached the server: GET /api/grade holds "
        + JSON.stringify(g && g.exists ? windowOf(g.config) : null) + ", expected cx " + cx1);
    }
  }
  /* #undoBtn's enabled state is computed from the project response since
     contract C4 (updateUndoButtons reads head_commit.parent), so the drag's
     commit has to come back before the button can turn on: a fixed sleep is
     not a wait for that, which is the same point spec 09 makes in its own
     header. Poll, then assert exactly what this always asserted. */
  let undoDisabled = true;
  const enableDeadline = Date.now() + 20000;
  for (;;) {
    undoDisabled = await page.$eval("#undoBtn", (el) => el.disabled);
    if (!undoDisabled || Date.now() >= enableDeadline) break;
    await new Promise((r) => setTimeout(r, 100));
  }
  if (undoDisabled) return fail("cx changed but #undoBtn is disabled, so the drag committed no history step");
  await page.click("#undoBtn");
  /* Undo is a server round trip since contract C4 (#undoBtn posts to
     /api/project/undo and the answer is what lands on screen), so this waits
     for that answer instead of guessing at a sleep, exactly as spec 09 does
     and for the reason spec 09 gives: "It can no longer sleep a fixed 150 ms
     and read: every step is a round trip."
     Measured, on a server started with a fresh --data-dir and therefore a COLD
     frame cache (which is what the harness does since studio/server.py's
     _default_cache_dir put every --data-dir run's cache inside its own data
     folder): the four scope/stats renders that follow the drag's own commit
     take about a second each, Chrome's six-connections-per-host limit queues
     this undo behind them, and the answer arrives between 190 ms and 420 ms.
     Against a warm cache it arrives inside 190 ms. Either way the assertion
     below is unchanged: if the undo never lands, or lands on the wrong commit,
     this loop runs out and the same failure is reported. */
  const undoDeadline = Date.now() + 20000;
  let afterUndo = await readConfig();
  while (Math.abs(Number(windowOf(afterUndo).cx) - cx0) > 1e-9 && Date.now() < undoDeadline) {
    await new Promise((r) => setTimeout(r, 100));
    afterUndo = await readConfig();
  }
  const win2 = windowOf(afterUndo);
  if (Math.abs(Number(win2.cx) - cx0) > 1e-9) {
    return fail("one #undoBtn press left cx at " + win2.cx + ", expected it back at " + cx0 + " (the drag is meant to be one step, not many)");
  }
  if (win2.enabled !== false) {
    return fail("one #undoBtn press left layers[0].mask.window.enabled at " + String(win2.enabled) + ", expected the drag's own auto-enable to be undone too");
  }
  if (!Array.isArray(afterUndo.layers) || afterUndo.layers.length !== 1) {
    return fail("undoing the drag also removed layer 0 (got " + JSON.stringify(afterUndo.layers) + "), but the layer was created by a SEPARATE, earlier commit and should still be there");
  }
  notes.push("one undo restored cx to " + win2.cx + " and enabled to false, without undoing the layer's own creation");

  // --- 4. the rotation grip ------------------------------------------------
  // The undo above put mask.window.enabled back to false; #windowBtn is
  // still on, which is exactly why the overlay is still drawable here.
  if (!(await overlayOn())) {
    return fail("after undo, #windowOverlay went away even though #windowBtn is still on");
  }
  const rotGrip = await centreOf("#windowOverlay .win-rot");
  if (!rotGrip) return fail("no #windowOverlay .win-rot grip on screen");
  const rot0 = Number(win2.rotation === undefined ? 0 : win2.rotation);
  await drag(rotGrip, 90, 40);
  const afterRot = await readConfig();
  const win3 = windowOf(afterRot);
  const rot1 = Number(win3.rotation);
  if (!isFinite(rot1) || Math.abs(rot1 - rot0) < 1) {
    return fail("a real drag on the rotation grip left layers[0].mask.window.rotation at " + win3.rotation + " (was " + rot0 + ")");
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
  const afterKey1 = windowOf(await readConfig());
  const step1 = Number(afterKey1.cx) - cxBeforeKeys;
  if (Math.abs(step1 - 1 / pic.w) > 1e-6) {
    return fail("ArrowRight moved cx by " + step1 + ", expected 1 screen pixel = " + (1 / pic.w));
  }
  await focusOverlay();
  await page.keyboard.down("Shift");
  await page.keyboard.press("ArrowRight");
  await page.keyboard.up("Shift");
  await new Promise((r) => setTimeout(r, 150));
  const afterKey2 = windowOf(await readConfig());
  const step2 = Number(afterKey2.cx) - Number(afterKey1.cx);
  if (Math.abs(step2 - 10 / pic.w) > 1e-6) {
    return fail("shift+ArrowRight moved cx by " + step2 + ", expected 10 screen pixels = " + (10 / pic.w));
  }
  notes.push("arrow nudge: +" + step1.toFixed(6) + " (1px), shift +" + step2.toFixed(6) + " (10px) on a " + pic.w.toFixed(1) + "px picture");

  // --- 5. hiding it again --------------------------------------------------
  // layers.js tags every per-layer control's own root with the dotted field
  // path it edits (see makeItemControl's tagged() in layers.js), which is
  // what lets this select the WINDOW group's own enabled box specifically:
  // a layer body repeats the "ctl-switch" class six times over (mask.show,
  // mask.invert, mask.window.enabled, mask.window.invert, mask.key.enabled,
  // mask.key.invert), so ordinal position alone would be fragile.
  const winEnableSel = LAYERS_STAGE + ' .layer-item[data-layer-index="0"] [data-path="mask.window.enabled"] input.ctl-switch';
  const enableBox = await page.$(winEnableSel);
  if (!enableBox) return fail("no " + winEnableSel + " found (layer 0's own Window-enabled checkbox)");
  const checked = await page.$eval(winEnableSel, (el) => el.checked);
  if (!checked) return fail("layer 0's Window-enabled checkbox is unchecked after a drag auto-enabled it");

  /* Follow-up 1 (collapse by default, plan/2026-09-04-studio-layers) put
   * mask.window.enabled inside the layer's own Window group, a <details>
   * collapsed by default: a closed <details> lays its content out at zero
   * size, so a real click cannot land on the checkbox until the fold is
   * open, exactly as a person would have to open it first. layers.js tags
   * that <details> with data-fold="window" for exactly this. */
  const winFoldSel = LAYERS_STAGE + ' .layer-item[data-layer-index="0"] details[data-fold="window"]';
  const winFoldOpen = await page.$eval(winFoldSel, (el) => el.open).catch(() => null);
  if (winFoldOpen === null) return fail("no " + winFoldSel + " found (layer 0's own Window fold)");
  if (!winFoldOpen) {
    await page.click(winFoldSel + " > summary");
    await new Promise((r) => setTimeout(r, 150));
  }
  await enableBox.click();
  await new Promise((r) => setTimeout(r, 200));
  if (!(await overlayOn())) {
    return fail("unchecking Window-enabled hid the overlay while #windowBtn is still on, the button is supposed to hold it open");
  }
  await page.click("#windowBtn");
  await new Promise((r) => setTimeout(r, 200));
  if (await overlayOn()) {
    return fail("with mask.window.enabled unchecked and #windowBtn off, #windowOverlay is still showing");
  }
  const end = await readConfig();
  if (windowOf(end).enabled !== false) {
    return fail("the enabled checkbox click left layers[0].mask.window.enabled at " + String(windowOf(end).enabled));
  }
  notes.push("Window-enabled unchecked plus #windowBtn off hides the overlay");

  /* Leave the clip's autosaved grade back on the flat preset. This spec is
   * the only one that creates and edits a layer, and leaving one behind
   * (rotated, off centre) saved against the only clip in content/footage
   * would be the next run's starting state (and the next screenshot's). */
  await page.select("#presetSelect", "flat");
  await page.click("#loadPresetBtn");
  await new Promise((r) => setTimeout(r, 800));

  return { status: "PASS", evidence: notes.join("; ") };
}
