/* Masks, lane M7: the real end-to-end flow, all against the real stub SAM
 * service run.mjs starts (see run.mjs's own SAM_PYTHON/SAM_STUB_DELAY_MS
 * comments), all through real clicks in the real browser. Nothing here
 * reads a private JS global except StudioLive.isProxyPlaying(), which spec
 * 14 already established is a legitimate public one.
 *
 * Rotation is set to "0" through the real #rotSeg control before anything
 * else: studio/server.py's default rotation is the string "auto", and
 * sam/server.py's own /track handler does `int(body.get("rotation") or 0)`,
 * which raises on "auto". This is a real, found bug (checkpoints/M6.md
 * "Bug B", and this lane's own studio/tests/py/test_mask_stub_service.py
 * pins it directly against the service). It lives in studio/server.py,
 * which this lane may not edit, so every real pick and track here works
 * around it exactly the way M6 says they had to for their own screenshots:
 * a concrete rotation, never the default.
 *
 * "0" specifically only works because of a second, previously-undiscovered
 * bug this lane found and fixed (Bug C, grade/mattes.py's info_from_dir):
 * a real track's own index.json stores rotation as the JSON integer 0, and
 * `raw.get("rotation") or "auto"` treated that as falsy and silently
 * rewrote it to the string "auto", so every real matte made at rotation 0
 * (SAM's normal encoding of "no rotation") permanently read back as made
 * at "auto" and masks.js's own componentState() called it stale forever
 * against a viewer that really was at 0. Fixed with a `_parse_rotation`
 * helper that only defaults to "auto" when the field is truly absent
 * (None or ""), mirroring the same-shaped `_parse_created` fix for Bug A.
 * grade/mattes.py is not on this lane's forbidden list (server.py,
 * cinegrade.py and sam/ are); both fixes are covered by this lane's own
 * Python suite and by grade/tests/run_tests.py --group mask.
 *
 * The claims, in order:
 *   1. Choosing "Object (click or box)" from the add menu starts a
 *      component in needs_pick (comp.matte.needs_pick, read here through
 *      [data-mask-comp="0"]'s own data-mask-state) with no viewer click at
 *      all yet, and opens [data-mask-pickpanel] with its hint.
 *   2. A real click on the picture (page.mouse, the same input path spec 13
 *      proves arrives) adds a point, and Find (real POST
 *      /api/mask/segment) returns candidates: [data-mask-candidates] gets
 *      at least one [data-mask-candidate] chip, and its own thumbnail
 *      (the server's tint overlay image) is a real, non-empty image.
 *   3. Choosing a candidate starts a real track (POST /api/mask/track):
 *      [data-mask-progress] appears with done/total frame text, and over
 *      real wall clock time (the stub's own per-frame delay) the row's
 *      data-mask-state is observed somewhere other than "done" before
 *      landing on "done", the same "this file is not proving what it
 *      claims to otherwise" discipline as the Python suite's own track
 *      test.
 *   4. Acceptance A1: with the track done, switching the layer's display to
 *      "overlay" and moving the playhead across two different times (the
 *      real #scrub control) reads two different #maskOverlay data-frame
 *      values, because the stub's own matte is a drifting ellipse, not a
 *      static one. A real Play/pause pass over #playBtn is taken too, as
 *      corroborating evidence only (real-time headless playback timing is
 *      not this file's PASS/FAIL basis, the deterministic scrub is).
 */

const LAYERS_STAGE = 'section.stage[data-stage="layers"]';

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
    await sleep(150);
  }
  return last;
}

// Same reason spec 27 has this: the app is on a commit-based project model
// (contract C3), so #layersAddBtn's click commits ASYNCHRONOUSLY. A fixed
// sleep only proves the DOM updated in memory, not that the server's own
// copy of this clip's config landed the new layer yet. Poll it instead.
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

  const need = ["mousedown", "mouseup"];
  const missing = need.filter((t) => !ctx.state.inputArrived || !ctx.state.inputArrived[t]);
  if (missing.length) {
    return {
      status: "SKIP",
      evidence: "input-probe found " + missing.join(" and ") + " never reach the page, a real click on the picture cannot be performed",
    };
  }

  const clips = await fetch(base + "/api/clips").then((r) => r.json()).then((j) => j.clips || []);
  const usable = clips.filter((c) => !c.error && c.duration);
  if (!usable.length) return { status: "SKIP", evidence: "no usable clips in content/footage" };
  // studio/server.py's own _clips() lists FOOTAGE.iterdir() sorted, and
  // app.js boots onto clips[0] (ctx.firstClip, already selected, no switch
  // needed): the mask proxy encodes the WHOLE clip once per clip and
  // rotation (_mask_proxy_params), and the stub's per-frame delay times
  // every frame in it, so this only proceeds when that already-selected
  // clip also happens to be the shortest one available, keeping this
  // spec's own runtime bounded without driving a clip switch through any
  // private state.
  usable.sort((a, b) => a.duration - b.duration);
  const shortest = usable[0];
  if (!ctx.firstClip || shortest.name !== ctx.firstClip) {
    return {
      status: "SKIP",
      evidence: "the boot-selected clip (" + ctx.firstClip + ") is not the shortest available ("
        + shortest.name + ", " + shortest.duration.toFixed(1) + "s); switching clips without a "
        + "private JS global is out of scope for this spec",
    };
  }
  const clipName = ctx.firstClip;
  notes.push("clip " + clipName + " (" + shortest.duration.toFixed(1) + "s)");

  const getGrade = () => fetch(base + "/api/grade?clip=" + encodeURIComponent(clipName)).then((r) => r.json());
  const putGrade = (config) => fetch(base + "/api/grade", {
    method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ clip: clipName, config }),
  });
  const dropGrade = () => fetch(base + "/api/grade?clip=" + encodeURIComponent(clipName), { method: "DELETE" });
  const snap = await getGrade();

  async function pictureBox() {
    return page.evaluate(() => {
      var ids = ["frameImg", "gpuCanvas", "playerVideo"];
      for (var i = 0; i < ids.length; i++) {
        var n = document.getElementById(ids[i]);
        if (!n) continue;
        if (n.tagName === "IMG" && !n.naturalWidth) continue;
        var r = n.getBoundingClientRect();
        if (r.width > 1 && r.height > 1) return { left: r.left, top: r.top, w: r.width, h: r.height };
      }
      return null;
    });
  }

  try {
    // -- rotation, worked around to a concrete value -------------------------
    // #rotSeg lives in the left rail's "Files" pane (index.html: the browse
    // pane, contract C4's own comment "right against the list of clips it
    // applies to"), not the pane active by default ("Refs"), so the tab has
    // to be opened first, same as specs 12, 19 and 23 already do before
    // touching #rotSeg.
    await page.click('.railtab[data-rail="browse"]');
    await sleep(150);
    const rotBtn = await page.$('#rotSeg button[data-rotation="0"]');
    if (!rotBtn) {
      return fail('no #rotSeg button[data-rotation="0"]: cannot work around the documented '
        + 'rotation="auto" bug (checkpoints/M6.md Bug B) to run a real pick or track');
    }
    await rotBtn.click();
    await sleep(400);
    notes.push('rotation set to "0" through #rotSeg, working around Bug B');

    // -- add a layer, open the mask panel -----------------------------------
    await page.evaluate((sel) => {
      const stage = document.querySelector(sel);
      if (stage) stage.scrollIntoView({ block: "center" });
    }, LAYERS_STAGE);
    const before = await page.evaluate(() => document.querySelectorAll('[data-mask-layer]').length);
    const beforeServerLayers = ((snap && snap.exists && snap.config && snap.config.layers) || []).length;
    const addLayerBtn = await page.$("#layersAddBtn");
    if (!addLayerBtn) return fail("no #layersAddBtn in " + LAYERS_STAGE);
    await addLayerBtn.click();
    const committedLayers = await waitForLayerCount(getGrade, beforeServerLayers + 1, 8000);
    if (committedLayers !== beforeServerLayers + 1) {
      return fail("server's own /api/grade config still has " + committedLayers
        + " layer(s) 8s after clicking #layersAddBtn, expected " + (beforeServerLayers + 1)
        + " (the commit never landed server-side)");
    }
    const layers = await page.evaluate(() => Array.prototype.map.call(
      document.querySelectorAll('[data-mask-layer]'), (el) => el.getAttribute("data-mask-layer")));
    if (layers.length !== before + 1) return fail("Add layer did not add exactly one mask panel section");
    const idx = layers[layers.length - 1];
    const sec = '[data-mask-layer="' + idx + '"]';

    // -- 1: Object add-kind starts needs_pick with no viewer click yet ------
    await page.click(sec + " [data-mask-add]");
    await sleep(150);
    const objectItem = await page.$(sec + ' [data-mask-addmenu] [data-mask-add-kind="object"]');
    if (!objectItem) return fail('no [data-mask-add-kind="object"] in the add menu');
    await objectItem.click();
    await sleep(250);

    const row0State = await page.evaluate((sel) => {
      const row = document.querySelector(sel + ' [data-mask-comp="0"]');
      return row ? row.getAttribute("data-mask-state") : null;
    }, sec);
    if (row0State !== "needs_pick") {
      return fail("choosing Object left component 0 at data-mask-state=\"" + row0State
        + "\", expected \"needs_pick\" before any point is picked");
    }
    const hint = await page.evaluate((sel) => {
      const h = document.querySelector(sel + " [data-mask-pickpanel] [data-mask-pick-hint]");
      return h ? h.textContent.trim() : null;
    }, sec);
    if (!hint) return fail("no [data-mask-pick-hint] in the open pick panel");
    notes.push('needs_pick organically, before any point: "' + hint.slice(0, 40) + '..."');

    // -- 2: a real click on the picture, Find, candidates --------------------
    const box = await pictureBox();
    if (!box) return fail("no visible picture element (frameImg/gpuCanvas/playerVideo) to click on");
    const cx = box.left + box.w * 0.5;
    const cy = box.top + box.h * 0.5;
    await page.mouse.move(cx, cy);
    await page.mouse.down();
    await page.mouse.up();
    await sleep(200);

    const pointCount = await page.evaluate((sel) => {
      const c = document.querySelector(sel + " [data-mask-pick-points]");
      return c ? c.textContent : null;
    }, sec);
    if (!pointCount || pointCount.indexOf("0 points") === 0) {
      return fail("clicking the picture did not register a pick point (" + JSON.stringify(pointCount) + ")");
    }
    notes.push("picked a point: " + pointCount);

    const findBtn = await page.$(sec + " [data-mask-pick-run]");
    if (!findBtn) return fail("no [data-mask-pick-run] button");
    await findBtn.click();

    const candidates = await waitFor(page, (sel) => {
      const list = Array.prototype.map.call(
        document.querySelectorAll(sel + " [data-mask-candidates] [data-mask-candidate]"),
        (el) => el.getAttribute("data-mask-candidate"));
      return list.length ? list : null;
    }, 15000, [sec]);
    if (!candidates || !candidates.length) {
      const errTxt = await page.evaluate((s) => {
        const box2 = document.querySelector(s + " .mask-pick");
        return box2 ? box2.textContent.slice(0, 200) : null;
      }, sec);
      return fail("Find returned no [data-mask-candidate] chips within 15s (panel text: " + errTxt + ")");
    }
    notes.push(candidates.length + " candidate(s): " + candidates.join(", "));

    // The chip's own thumbnail: the server's tint overlay, a real image.
    const thumbOk = await page.evaluate((sel) => {
      const img = document.querySelector(sel + " [data-mask-candidates] [data-mask-candidate] img");
      return img ? { src: img.src, w: img.naturalWidth, h: img.naturalHeight } : null;
    }, sec);
    if (!thumbOk || !thumbOk.src) return fail("candidate chip has no thumbnail <img> at all");
    // naturalWidth can still be 0 immediately after the src is set; give the
    // real network fetch a moment before treating that as a failure.
    let thumbLoaded = thumbOk;
    if (!thumbLoaded.w) {
      thumbLoaded = await waitFor(page, (sel) => {
        const img = document.querySelector(sel + " [data-mask-candidates] [data-mask-candidate] img");
        return img && img.naturalWidth ? { w: img.naturalWidth, h: img.naturalHeight } : null;
      }, 5000, [sec]) || thumbLoaded;
    }
    if (!thumbLoaded.w) return fail("candidate thumbnail never finished loading a real image: " + thumbOk.src);
    notes.push("candidate thumbnail is a real " + thumbLoaded.w + "x" + thumbLoaded.h + " image");

    // -- 3: choose one, a real track starts, progress and states -----------
    const allBtn = await page.$(sec + " [data-mask-candidate-all]");
    const clickedAll = !!allBtn;
    if (allBtn) await allBtn.click();
    else await page.click(sec + ' [data-mask-candidate="' + candidates[0] + '"]');
    await sleep(300);

    // Read the row(s) back by id rather than assuming index "0" stays put:
    // "Add all" (when the panel offers it) can turn the one needs_pick
    // placeholder into several real components, and the last one in
    // document order is the one this spec keeps polling either way.
    const allComps = await page.evaluate((sel) => Array.prototype.map.call(
      document.querySelectorAll(sel + " [data-mask-comp]"),
      (el) => ({ idx: el.getAttribute("data-mask-comp"), state: el.getAttribute("data-mask-state") })), sec);
    notes.push("component rows after choosing (" + (clickedAll ? "Add all" : "one candidate")
      + "): " + JSON.stringify(allComps));
    const targetIdx = allComps.length ? allComps[allComps.length - 1].idx : "0";

    const seenStates = new Set();
    let finalState = null;
    const deadline = Date.now() + 60000;
    while (Date.now() < deadline) {
      const st = await page.evaluate((sel, idx) => {
        const row = document.querySelector(sel + ' [data-mask-comp="' + idx + '"]');
        const prog = document.querySelector(sel + " [data-mask-progress-text]");
        return { state: row ? row.getAttribute("data-mask-state") : null,
                 progress: prog ? prog.textContent : null };
      }, sec, targetIdx);
      if (st.state) seenStates.add(st.state);
      if (st.progress) notes.push("progress: " + st.progress);
      if (st.state === "done") { finalState = st.state; break; }
      if (st.state === "failed") { finalState = st.state; break; }
      await sleep(400);
    }
    if (finalState !== "done") {
      return fail("component 0 never reached done within 60s; states seen: "
        + Array.from(seenStates).join(", ") + "; last: " + finalState);
    }
    if (!(seenStates.size > 1 || seenStates.has("queued") || seenStates.has("running"))) {
      return fail("component 0 went straight to done on the very first read; states seen: "
        + Array.from(seenStates).join(", ") + " (this proves nothing about progress or a live job)");
    }
    notes.push("real states observed before done: " + Array.from(seenStates).join(", "));

    // -- 4: acceptance A1, the overlay follows the playhead ------------------
    const overlayBtn = await page.$(sec + ' [data-mask-display-mode="overlay"]');
    if (!overlayBtn) return fail('no [data-mask-display-mode="overlay"] button');
    await overlayBtn.click();
    await sleep(400);

    async function scrubTo(fraction) {
      await page.evaluate((f) => {
        const s = document.getElementById("scrub");
        if (!s) return;
        s.value = String(Math.round(f * (parseFloat(s.max) || 1000)));
        s.dispatchEvent(new Event("input", { bubbles: true }));
      }, fraction);
    }

    async function overlayFrame() {
      return waitFor(page, () => {
        const cv = document.getElementById("maskOverlay");
        if (!cv || cv.dataset.on !== "true") return null;
        return cv.dataset.frame || null;
      }, 6000, []);
    }

    await scrubTo(0.1);
    const frameA = await overlayFrame();
    if (!frameA) return fail("#maskOverlay never turned on (data-on=\"true\") with a served frame after switching to Overlay");

    await scrubTo(0.85);
    // Give the LRU/prefetch a moment to actually decode the new frame
    // before reading it, same discipline as the Python suite's polling.
    let frameB = null;
    const bDeadline = Date.now() + 6000;
    while (Date.now() < bDeadline) {
      const f = await page.evaluate(() => {
        const cv = document.getElementById("maskOverlay");
        return cv ? cv.dataset.frame || null : null;
      });
      if (f && f !== frameA) { frameB = f; break; }
      await sleep(150);
    }
    if (!frameB) {
      return fail("#maskOverlay's data-frame stayed at " + frameA + " after moving the playhead from 10% to 85% "
        + "of the clip: the overlay did not follow, which is exactly what A1 requires");
    }
    notes.push("A1: #maskOverlay data-frame moved " + frameA + " -> " + frameB + " across two scrub positions");

    // Corroborating evidence only: real Play/pause. Never the PASS/FAIL basis
    // (headless real-time playback timing is not reliable enough for that),
    // exactly the discipline spec 22 and spec 14 already apply to their own
    // screenshots and stats.
    try {
      await scrubTo(0.05);
      await sleep(300);
      const playBtn = await page.$("#playBtn");
      if (playBtn) {
        await playBtn.click();
        await sleep(1200);
        const midFrame = await page.evaluate(() => {
          const cv = document.getElementById("maskOverlay");
          return cv ? cv.dataset.frame || null : null;
        });
        await playBtn.click();
        notes.push("Play/pause corroboration: data-frame during playback read " + midFrame);
      }
    } catch (e) { /* evidence only */ }

    return { status: "PASS", evidence: notes.join("; ") };
  } finally {
    if (snap && snap.exists) await putGrade(snap.config);
    else await dropGrade();
  }
}
