/* Layers (contract C1, plan/2026-09-04-studio-layers/PLAN.md): the panel
 * side, studio/static/layers.js plus the "layers" stage in schema.js, and
 * the retargeted power window in window-editor.js.
 *
 * Everything here is driven through the real UI in the real browser: real
 * clicks on the Add layer / Move up / Undo / Remove buttons, a real select
 * on the placement dropdown, a real keyboard rename, and a real drag on the
 * window overlay, the same page.mouse path spec 13 proves arrives. The
 * config is read the same "no private JS global" way every other spec
 * reads it, through #jsonBtn / #jsonText.
 *
 * The claims, in order:
 *   1. Add layer (twice) appends default layers named "Layer N" and
 *      history-commits each one; the second becomes the selected layer.
 *   2. Renaming a layer's name field commits on Enter, not on every
 *      keystroke.
 *   3. The placement select writes layers[i].placement.
 *   4. Clicking a layer selects it (a visible .selected marker moves), and
 *      the Window button plus a real drag on the overlay enables and moves
 *      THAT layer's mask.window, going through the same auto-enable and one
 *      undo step spec 13 already proves for a single layer.
 *   5. Move up swaps two layers (and the selection follows the layer, not
 *      the row); one undo restores the original order, AND the selection
 *      marker follows the same layer by identity to its restored index
 *      (layers.js tracks the selected layer by a stable client-side id, not
 *      by array index, exactly so an undo cannot leave the marker on the
 *      wrong layer).
 *   6. Remove drops a layer.
 *   7. A reload proves the whole array round-tripped through the real
 *      server (PUT/GET /api/grade, contract C3), not just in memory.
 *
 * This spec writes to the account's real grade store (studio/data/
 * studio.db) for one clip, so it snapshots that clip's grade first and
 * puts it back in a finally block, pass or fail, the same discipline spec
 * 12 uses for its two clips.
 */

const LAYERS_STAGE = 'section.stage[data-stage="layers"]';
const SETTLE_MS = 5000;

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

function fail(evidence) {
  return { status: "FAIL", evidence: evidence };
}

export default async function run(ctx) {
  const page = ctx.page;
  const base = ctx.baseUrl;

  const need = ["mousedown", "mousemove", "mouseup"];
  const missing = need.filter((t) => !ctx.state.inputArrived || !ctx.state.inputArrived[t]);
  if (missing.length) {
    return {
      status: "SKIP",
      evidence: "input-probe found " + missing.join(" and ") + " never reach the page, this spec needs a real drag on the window overlay",
    };
  }

  const clips = await fetch(base + "/api/clips").then((r) => r.json()).then((j) => j.clips || []);
  if (!clips.length) {
    return { status: "SKIP", evidence: "no clips in content/footage to hold a grade for this spec" };
  }
  const clipName = clips[0].name;

  const getGrade = (name) =>
    fetch(base + "/api/grade?clip=" + encodeURIComponent(name)).then((r) => r.json());
  const putGrade = (name, config) =>
    fetch(base + "/api/grade", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ clip: name, config }),
    });
  const dropGrade = (name) =>
    fetch(base + "/api/grade?clip=" + encodeURIComponent(name), { method: "DELETE" });

  // Snapshot first, restore in the finally below: this is somebody's real
  // grading database, not a fixture (same reasoning as spec 12).
  const snap = await getGrade(clipName);

  async function readConfig() {
    await page.click("#jsonBtn");
    await sleep(120);
    const text = await page.$eval("#jsonText", (el) => el.value);
    await page.evaluate(() => {
      const o = document.getElementById("jsonOverlay");
      if (o) o.classList.remove("on");
    });
    return JSON.parse(text);
  }

  // Polls the real save state (grades.js exposes StudioGrades.isPending()
  // and #gradeSaveState "for the UI test harness" for exactly this) rather
  // than sleeping a guessed duration; see spec 12's own copy of this helper
  // for the fuller reasoning.
  async function waitForGradeSettled(ceilingMs) {
    const start = Date.now();
    let state = { pending: true, dot: "saving" };
    while (Date.now() - start < ceilingMs) {
      state = await page.evaluate(() => ({
        pending: !!(window.StudioGrades && window.StudioGrades.isPending()),
        dot: (document.getElementById("gradeSaveState") || {}).textContent,
      }));
      if (state.dot !== undefined && !state.pending && state.dot !== "saving") return state;
      await new Promise((r) => setTimeout(r, 50));
    }
    return state;
  }

  /* Is #undoBtn STILL disabled after giving the commit time to come back?
     Since contract C4 the button's enabled state is computed from the project
     response (updateUndoButtons reads head_commit.parent), so a click plus a
     fixed sleep is not a wait for it: spec 09 says the same thing in its own
     header and polls. Returns true only when the ceiling ran out with the
     button still disabled, so every assertion built on it is unchanged. */
  async function undoStillDisabled(ceilingMs) {
    const deadline = Date.now() + (ceilingMs || 20000);
    for (;;) {
      if (!(await page.$eval("#undoBtn", (el) => el.disabled))) return false;
      if (Date.now() >= deadline) return true;
      await sleep(100);
    }
  }

  /* Wait until the SERVER's copy of this clip's config satisfies pred, i.e.
     until the commit a click just fired has actually landed. This is what has
     to be true before ANY Undo: #undoBtn is enabled by every earlier commit
     too, so "enabled" never proved that the newest edit was on the server,
     and clicking Undo while that edit is still in flight pops the PREVIOUS
     commit and lets the in-flight one land on top of the undone head, which
     reads exactly like "undo did nothing". Returns the config that satisfied
     pred, or null when the ceiling ran out. */
  async function waitForServerConfig(pred, ceilingMs) {
    const deadline = Date.now() + (ceilingMs || 20000);
    const ok = (g) => !!(g && g.exists && g.config && pred(g.config));
    let g = await getGrade(clipName);
    while (!ok(g) && Date.now() < deadline) {
      await sleep(120);
      g = await getGrade(clipName);
    }
    return ok(g) ? g.config : null;
  }

  function layerAt(cfg, idx) {
    return (cfg && Array.isArray(cfg.layers) && cfg.layers[idx]) || null;
  }

  async function selectedIndexClass() {
    return page.evaluate((stageSel) => {
      var stage = document.querySelector(stageSel);
      if (!stage) return null;
      var el = stage.querySelector(".layer-item.selected");
      return el ? Number(el.dataset.layerIndex) : -1;
    }, LAYERS_STAGE);
  }

  function layerItemSel(idx) {
    return LAYERS_STAGE + ' .layer-item[data-layer-index="' + idx + '"]';
  }

  /* Clicking anywhere in a layer item selects it (root's own mousedown
   * listener in layers.js has no exclusions, contract C1's "clicking a
   * layer's header or any control in it selects that layer"). The header
   * ALSO toggles collapse on any click that is not on one of its own
   * interactive children (layers.js's own header click handler), so
   * selecting through the header would collapse the very body this spec
   * still needs visible for the controls after it. The first ".stagesub"
   * in a layer's body ("Mask") is a plain heading div with no click handler
   * of its own and sits above every other control, so a click there selects
   * the layer without collapsing it or touching any real control. */
  async function selectLayerSafely(idx) {
    const sel = layerItemSel(idx) + " .stagesub";
    const el = await page.$(sel);
    if (!el) return false;
    await el.click();
    await sleep(150);
    return true;
  }

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
  async function drag(from, dx, dy) {
    await page.mouse.move(from.x, from.y);
    await page.mouse.down();
    await page.mouse.move(from.x + dx, from.y + dy, { steps: 12 });
    await page.mouse.up();
    await sleep(200);
  }

  const notes = [];

  try {
    await page.setViewport({ width: 1440, height: 1500 });
    await sleep(300);

    await page.goto(base + "/", { waitUntil: "domcontentloaded", timeout: 30000 });
    await ctx.waitForBootComplete(20000);
    await waitForGradeSettled(SETTLE_MS);

    // A known starting point, reached through the app's own preset control
    // (not by reaching into private state): the shipped "flat" preset
    // carries no secondary, window or layers block, so loading it gives an
    // empty config.layers.
    const hasFlat = await page.$eval("#presetSelect", (el) =>
      Array.prototype.some.call(el.options, (o) => o.value === "flat"));
    if (!hasFlat) return fail('#presetSelect has no "flat" preset to reset the config from');
    await page.select("#presetSelect", "flat");
    await page.click("#loadPresetBtn");
    await sleep(700);
    await waitForGradeSettled(SETTLE_MS);

    const start = await readConfig();
    if (!Array.isArray(start.layers) || start.layers.length !== 0) {
      return fail('the "flat" preset is expected to carry no layers; got ' + JSON.stringify(start.layers));
    }

    const addBtn = await page.$("#layersAddBtn");
    if (!addBtn) return fail("no #layersAddBtn in the " + LAYERS_STAGE + " toolbar");

    // Wait for the picture to actually be on screen before the window drag
    // later needs it (same wait spec 13 uses; done here, once, up front).
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
      return fail("no graded frame appeared in the viewer within 30s, the window drag later has nothing to map onto");
    }

    // --- 1. add two layers, history-committed, second one selected --------
    await addBtn.click();
    await sleep(200);
    let cfg = await readConfig();
    if (!Array.isArray(cfg.layers) || cfg.layers.length !== 1) {
      return fail("clicking #layersAddBtn once left config.layers at " + JSON.stringify(cfg.layers));
    }
    if ((layerAt(cfg, 0) || {}).name !== "Layer 1") {
      return fail('the first added layer is named "' + (layerAt(cfg, 0) || {}).name + '", expected "Layer 1"');
    }
    let undoDisabled = await undoStillDisabled();
    if (undoDisabled) return fail("adding a layer left #undoBtn disabled, so it committed no history step");

    await addBtn.click();
    await sleep(200);
    cfg = await readConfig();
    if (!Array.isArray(cfg.layers) || cfg.layers.length !== 2) {
      return fail("clicking #layersAddBtn twice left config.layers at " + JSON.stringify(cfg.layers));
    }
    if ((layerAt(cfg, 1) || {}).name !== "Layer 2") {
      return fail('the second added layer is named "' + (layerAt(cfg, 1) || {}).name + '", expected "Layer 2"');
    }
    const selAfterAdd = await selectedIndexClass();
    if (selAfterAdd !== 1) {
      return fail("after adding a second layer the .selected marker is on index " + selAfterAdd + ", expected 1 (the one just added)");
    }
    notes.push("Add layer twice: 2 layers named Layer 1 / Layer 2, each its own undo step, layer 1 (index 1) selected");

    // --- 2. rename layer 1 (index 1), commits on Enter only ----------------
    const nameSel = layerItemSel(1) + " .layer-name-input";
    const nameInput = await page.$(nameSel);
    if (!nameInput) return fail("no " + nameSel + " found");

    // A marker property on the layer item's own DOM node: layers.js's
    // rebuild() (see its file header) tears down and rebuilds every layer
    // item from scratch, but only on a COMMITTED change. If the marker is
    // still there right after typing, no rebuild happened yet (the "input"
    // handler wrote with commit=false, exactly so a rebuild mid keystroke
    // cannot fight the very field being typed into); if it is gone after
    // Enter, a real commit and rebuild did happen.
    await page.evaluate((sel) => {
      var n = document.querySelector(sel);
      if (n) n.dataset.testMarker = "1";
    }, layerItemSel(1));

    // Focus plus the real HTMLInputElement.select() method, not a triple
    // click: a synthetic clickCount:3 landed inconsistently in practice
    // (observed inserting mid string instead of replacing), where .select()
    // is the standard, deterministic way to pre select an input's whole
    // value before typing. The typing and the Enter right after it are
    // still real keyboard events; this only removes the ambiguity of WHERE
    // a synthetic multi click lands.
    await page.$eval(nameSel, (el) => { el.focus(); el.select(); });
    await page.keyboard.type("Sky Fix");
    await sleep(150);
    const markerAfterType = await page.evaluate((sel) => {
      var n = document.querySelector(sel);
      return n ? n.dataset.testMarker : null;
    }, layerItemSel(1));
    if (markerAfterType !== "1") {
      return fail("typing into the rename field rebuilt the layer list before Enter/blur (the marker did not survive), which would fight the very field being typed into");
    }

    await page.keyboard.press("Enter");
    await sleep(200);
    const markerAfterCommit = await page.evaluate((sel) => {
      var n = document.querySelector(sel);
      return n ? n.dataset.testMarker : null;
    }, layerItemSel(1));
    if (markerAfterCommit === "1") {
      return fail("pressing Enter did not rebuild the layer list (the marker survived), so the rename may not have actually committed");
    }
    cfg = await readConfig();
    if ((layerAt(cfg, 1) || {}).name !== "Sky Fix") {
      return fail('renaming layer 1 to "Sky Fix" left its name at "' + (layerAt(cfg, 1) || {}).name + '"');
    }
    notes.push('renamed layer 1 (index 1) to "Sky Fix": no rebuild while typing, a real rebuild on Enter, name committed');

    // --- 3. placement select on layer 0 ------------------------------------
    const placeSel = layerItemSel(0) + ' [data-path="placement"] select';
    const hasPlace = await page.$(placeSel);
    if (!hasPlace) return fail("no " + placeSel + " found");
    await page.select(placeSel, "after_look");
    await sleep(150);
    cfg = await readConfig();
    if ((layerAt(cfg, 0) || {}).placement !== "after_look") {
      return fail('setting layer 0\'s placement select left placement at "' + (layerAt(cfg, 0) || {}).placement + '", expected "after_look"');
    }
    notes.push('layer 0 placement set to "after_look" via its select');

    // --- 4. select layer 1, enable its window through the overlay ---------
    if (!(await selectLayerSafely(1))) return fail("could not click " + layerItemSel(1) + " .stagesub to select it");
    let selNow = await selectedIndexClass();
    if (selNow !== 1) return fail("clicking layer 1's body left the .selected marker on index " + selNow + ", expected 1");
    notes.push("clicking layer 1's body moved the .selected marker to index 1");

    const hasWinBtn = await page.$("#windowBtn");
    if (!hasWinBtn) return fail("no #windowBtn in the viewer toolbar");
    await page.click("#windowBtn");
    await sleep(200);
    if (!(await overlayOn())) return fail("#windowBtn did not show #windowOverlay for the selected layer");
    cfg = await readConfig();
    if (!Array.isArray(cfg.layers) || cfg.layers.length !== 2) {
      return fail("#windowBtn changed the layer count to " + JSON.stringify(cfg.layers) + ", it should only select/show, not create, when a layer already exists");
    }

    const pic = await pictureRect();
    if (!pic) return fail("no picture element with a non-zero rect to map the drag onto");
    const grip = await centreOf("#windowOverlay .win-centre");
    if (!grip) return fail("no #windowOverlay .win-centre grip on screen");
    const win0 = (layerAt(cfg, 1).mask || {}).window || {};
    const cx0 = Number(win0.cx === undefined ? 0.5 : win0.cx);
    const DX = 100;
    if (cx0 + DX / pic.w > 0.98) {
      return fail("the fitted picture is only " + pic.w.toFixed(1) + "px wide, a " + DX + "px drag would hit the 0..1 clamp instead of testing the mapping");
    }
    await drag(grip, DX, 0);
    await sleep(200);
    cfg = await readConfig();
    const win1 = (layerAt(cfg, 1).mask || {}).window || {};
    const cx1 = Number(win1.cx);
    const expected = cx0 + DX / pic.w;
    if (Math.abs(cx1 - expected) > 0.01) {
      return fail("dragging the overlay's centre grip left layers[1].mask.window.cx at " + cx1 + ", expected close to " + expected.toFixed(5));
    }
    if (win1.enabled !== true) {
      return fail("the drag moved layers[1].mask.window.cx but mask.window.enabled is " + String(win1.enabled) + ", auto-enable did not fire");
    }
    if ((layerAt(cfg, 0).mask.window || {}).enabled) {
      return fail("the drag on the OVERLAY, which is meant to edit only the SELECTED layer (index 1), also enabled layer 0's window");
    }
    notes.push("Window button plus a real drag enabled and moved layers[1] (Sky Fix)'s mask.window (cx " + cx0 + " -> " + cx1.toFixed(5) + "), layer 0's window untouched");

    // --- 5. move layer 1 up, undo restores the order -----------------------
    const upSel = layerItemSel(1) + ' button[title="Move up"]';
    const upBtn = await page.$(upSel);
    if (!upBtn) return fail("no " + upSel + " found");
    await upBtn.click();
    await sleep(200);
    cfg = await readConfig();
    if ((layerAt(cfg, 0) || {}).name !== "Sky Fix") {
      return fail('Move up on layer 1 left index 0 named "' + (layerAt(cfg, 0) || {}).name + '", expected "Sky Fix" to have moved there');
    }
    if ((layerAt(cfg, 1) || {}).placement !== "after_look") {
      return fail("Move up on layer 1 left index 1 as " + JSON.stringify(layerAt(cfg, 1)) + ", expected the other layer (placement after_look) pushed down there");
    }
    const selAfterMove = await selectedIndexClass();
    if (selAfterMove !== 0) {
      return fail("Move up moved the layer but the .selected marker is on index " + selAfterMove + ", expected 0 (selection follows the layer, not the row)");
    }
    notes.push("Move up swapped the two layers and the .selected marker followed Sky Fix to index 0");

    /* The move's own commit has to be ON THE SERVER before Undo is pressed,
       or Undo pops whatever was committed before it (the window drag in step
       4) and the move's in-flight commit then lands on top of that undone
       head, leaving the moved order on screen for good. That is not a slow
       server, it is the wrong commit being undone, and no amount of waiting
       afterwards recovers it. */
    const movedOnServer = await waitForServerConfig((c) =>
      Array.isArray(c.layers) && c.layers.length === 2
      && (c.layers[0] || {}).name === "Sky Fix");
    if (!movedOnServer) {
      const g = await getGrade(clipName);
      return fail("Move up never reached the server: GET /api/grade still holds "
        + JSON.stringify(g && g.config && g.config.layers && g.config.layers.map((l) => l.name)));
    }

    undoDisabled = await undoStillDisabled();
    if (undoDisabled) return fail("the move left #undoBtn disabled, so it committed no history step");
    await page.click("#undoBtn");
    /* Undo is a server round trip since contract C4 (#undoBtn posts to
       /api/project/undo and the answer is what lands on screen), so this waits
       for that answer rather than guessing a sleep, the same way spec 09 does
       and for the reason spec 09 states: "It can no longer sleep a fixed 150 ms
       and read: every step is a round trip." Measured on a cold frame cache
       (which is what a fresh --data-dir run has since studio/server.py's
       _default_cache_dir gave every --data-dir server its own cache folder):
       the scope and stats renders that follow the move's own commit take about
       a second each, Chrome queues this undo behind them at its six connection
       per host limit, and the answer can arrive after 400 ms. The assertion
       below is unchanged: an undo that never lands, or lands on the wrong
       commit, runs this loop out and reports the same failure. */
    const undoDeadline = Date.now() + 20000;
    cfg = await readConfig();
    while ((layerAt(cfg, 0) || {}).placement !== "after_look" && Date.now() < undoDeadline) {
      await sleep(100);
      cfg = await readConfig();
    }
    if ((layerAt(cfg, 0) || {}).placement !== "after_look" || (layerAt(cfg, 1) || {}).name !== "Sky Fix") {
      return fail("one undo after Move up left the order at " + JSON.stringify(cfg.layers.map((l) => l.name)) + ", expected the original order restored");
    }
    if ((layerAt(cfg, 1).mask.window || {}).enabled !== true) {
      return fail("undoing the move also lost Sky Fix's own window, which the move itself never touched");
    }
    notes.push("one undo restored the original order (Layer 1 [after_look] then Sky Fix [window on])");

    // A real pass/fail assertion (follow-up 2, fixed): the marker is now
    // tracked by the layer's own stable client-side id (layers.js's idMap /
    // resolveIds), never by array index, so it has to still read "Sky Fix"
    // here even though undo just restored Sky Fix to a DIFFERENT index (1,
    // not the 0 the move had put it at) and did so through brand new
    // objects (app.js's restore() replaces the whole config by JSON.parsing
    // a snapshot string, so this is not even the same object reference
    // moveLayer() swapped). This used to only be measured, not asserted:
    // see limits.js, this entry moved from BROKEN to FIXED.
    const selAfterUndo = await selectedIndexClass();
    const nameAtSelAfterUndo = (layerAt(cfg, selAfterUndo) || {}).name || "(none)";
    if (nameAtSelAfterUndo !== "Sky Fix") {
      return fail("after undoing the move, .selected is on index " + selAfterUndo + " (" + nameAtSelAfterUndo
        + "), expected the marker to have followed Sky Fix back to its restored index");
    }
    notes.push(
      "after that undo, .selected followed Sky Fix to its restored index " + selAfterUndo
      + " (identity tracked, not array position)"
    );

    // --- 6. remove a layer ---------------------------------------------------
    const rmSel = layerItemSel(0) + ' button[title="Remove layer"]';
    const rmBtn = await page.$(rmSel);
    if (!rmBtn) return fail("no " + rmSel + " found");
    await rmBtn.click();
    await sleep(200);
    cfg = await readConfig();
    if (!Array.isArray(cfg.layers) || cfg.layers.length !== 1) {
      return fail("removing layer 0 left config.layers at " + JSON.stringify(cfg.layers) + ", expected exactly one layer left");
    }
    if ((layerAt(cfg, 0) || {}).name !== "Sky Fix") {
      return fail('after removing the other layer, the one left is named "' + (layerAt(cfg, 0) || {}).name + '", expected "Sky Fix"');
    }
    if ((layerAt(cfg, 0).mask.window || {}).enabled !== true) {
      return fail("the surviving layer's own window (enabled by step 4) did not survive the remove");
    }
    notes.push('Remove dropped "Layer 1", leaving Sky Fix (with its window still enabled) as the only layer');

    // --- 7. reload proves the round trip through the real server -----------
    await waitForGradeSettled(SETTLE_MS);
    /* waitForGradeSettled above is not enough on its own any more: since
       contract C4 the commit IS the save, grades.js no longer runs a debounced
       PUT, and #gradeSaveState now reads "committed <short id>" rather than
       cycling through saving/saved, so that helper returns the moment it is
       asked. The remove's own commit is still a POST /api/session in flight,
       and on a cold frame cache (every --data-dir run has one since
       studio/server.py's _default_cache_dir gave each its own cache folder) it
       queues behind about a second of scope and stats rendering at Chrome's
       six-connections-per-host limit. So poll the server for the answer, with
       a ceiling, instead of reading once and hoping. The assertion underneath
       is unchanged: a remove that never reaches the server runs the loop out
       and reports exactly the same failure. */
    const serverDeadline = Date.now() + 20000;
    let onServer = await getGrade(clipName);
    while (Date.now() < serverDeadline
           && !(onServer.exists && Array.isArray(onServer.config.layers)
                && onServer.config.layers.length === 1)) {
      await sleep(150);
      onServer = await getGrade(clipName);
    }
    if (!onServer.exists || !Array.isArray(onServer.config.layers) || onServer.config.layers.length !== 1) {
      return fail("GET /api/grade for " + clipName + " does not hold the one-layer config the UI just showed: " + JSON.stringify(onServer.config && onServer.config.layers));
    }

    await page.goto(base + "/", { waitUntil: "domcontentloaded", timeout: 30000 });
    await ctx.waitForBootComplete(20000);
    await waitForGradeSettled(SETTLE_MS);
    // Same reason as the poll above: waitForBootComplete only proves the panels
    // rendered, and the clip's own config arrives later, on the answer to
    // selectClip's POST /api/project/open. The line the server holds was
    // already checked above, so this waits for the page to catch up with it
    // rather than reading while the boot fetch is still in the air.
    const reloadDeadline = Date.now() + 20000;
    let afterReload = await readConfig();
    while (Date.now() < reloadDeadline
           && !(Array.isArray(afterReload.layers) && afterReload.layers.length === 1)) {
      await sleep(150);
      afterReload = await readConfig();
    }
    if (!Array.isArray(afterReload.layers) || afterReload.layers.length !== 1) {
      return fail("after a reload config.layers is " + JSON.stringify(afterReload.layers) + ", expected the one saved layer");
    }
    const reloaded = layerAt(afterReload, 0);
    if (reloaded.name !== "Sky Fix" || !reloaded.mask || !reloaded.mask.window || reloaded.mask.window.enabled !== true) {
      return fail("after a reload the surviving layer is " + JSON.stringify(reloaded) + ", expected Sky Fix with its window still enabled");
    }
    notes.push("reload: the one remaining layer (Sky Fix, window enabled, cx " + reloaded.mask.window.cx + ") round-tripped through PUT/GET /api/grade");

    return { status: "PASS", evidence: notes.join("; ") };
  } finally {
    try {
      await page.goto(base + "/", { waitUntil: "domcontentloaded", timeout: 30000 });
      await ctx.waitForBootComplete(20000);
      await sleep(500);
    } catch (err) { /* the result above is what matters, not the tidy up */ }
    if (snap && snap.exists) await putGrade(clipName, snap.config);
    else await dropGrade(clipName);
  }
}
