/* presets (contract C5): Load, Overwrite and Save as must move the WHOLE
 * config, and a comment belongs to the preset FILE, never to the live
 * config.
 *
 * The founder's complaint, verbatim: "the preset overwriting and loading is
 * wrong because it doesnt overwrite or LOAD the full settings i think you
 * need to check it." What they actually saw: a preset saved as Manav_1_2
 * without typing a comment still carried the comment of the preset they had
 * loaded earlier (nature_cinema), so the file described a different grade.
 *
 * Everything here is driven through the real UI: real selects on
 * #presetSelect, real clicks on #loadPresetBtn / #savePresetBtn /
 * #saveAsBtn / #layersAddBtn, a real native <select> pick for the second
 * look slot, real checkbox clicks for committed field changes, and a real
 * page reload to prove a value came from the server. Save as opens two
 * native prompt() dialogs (name, then comment); armDialogs answers them for
 * real through Puppeteer's dialog protocol rather than stubbing
 * window.prompt. The live config is read the same no-private-JS-global way
 * spec 09 and spec 12 do: #jsonBtn / #jsonText.
 *
 * This spec creates and deletes ONLY presets named p5_*: p5_a, p5_c,
 * p5_full and p5_shadow_test (the last one also gets a same-named LIBRARY
 * fixture written directly under grade/presets/, to prove the "overwrite a
 * library preset shadows it" behaviour, and removed again in a finally).
 * run.mjs points the server at a fresh --data-dir per run, so every user
 * preset this spec saves or overwrites lands in that temp folder, never in
 * the founder's real studio/data/users/0/presets (Manav_1, Manav_1_2,
 * nature_cinema, green_banger, verci_warehouse, wes_anderson): those are
 * never loaded, saved over or deleted by this spec. grade/presets/ (the
 * shipped library) is not covered by --data-dir and stays the real, shared
 * one, which is why the library-shadow fixture below still has to clean up
 * after itself.
 */

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const CONTENT = path.resolve(HERE, "..", "..", "..");
const LIB_PRESETS_DIR = path.join(CONTENT, "grade", "presets");

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

function deepEqual(a, b) {
  if (a === b) return true;
  if (typeof a !== typeof b) return false;
  if (a === null || b === null) return a === b;
  if (typeof a !== "object") return false;
  if (Array.isArray(a) !== Array.isArray(b)) return false;
  if (Array.isArray(a)) {
    if (a.length !== b.length) return false;
    for (let i = 0; i < a.length; i++) if (!deepEqual(a[i], b[i])) return false;
    return true;
  }
  const ak = Object.keys(a), bk = Object.keys(b);
  if (ak.length !== bk.length) return false;
  for (const k of ak) {
    if (!Object.prototype.hasOwnProperty.call(b, k) || !deepEqual(a[k], b[k])) return false;
  }
  return true;
}

function stripComment(o) {
  if (!o || typeof o !== "object") return o;
  const c = Object.assign({}, o);
  delete c._comment;
  return c;
}

export default async function run(ctx) {
  const page = ctx.page;
  const base = ctx.baseUrl;

  const need = ["mousedown", "mouseup"];
  const missing = need.filter((t) => !ctx.state.inputArrived || !ctx.state.inputArrived[t]);
  if (missing.length) {
    return {
      status: "SKIP",
      evidence: "input-probe found " + missing.join(" and ") + " never reach the page, nothing here can be clicked for real",
    };
  }

  if (!ctx.firstClip) {
    return { status: "SKIP", evidence: "no clips from /api/clips to grade a preset onto" };
  }
  if (!ctx.dataDir) {
    return { status: "FAIL", evidence: "ctx.dataDir is not set: run.mjs must pass the server an isolated --data-dir, or this spec would write into the real studio/data" };
  }

  const P5_NAMES = ["p5_a", "p5_c", "p5_full", "p5_shadow_test"];
  const shadowLibPath = path.join(LIB_PRESETS_DIR, "p5_shadow_test.json");
  const userPresetFile = (name) => path.join(ctx.dataDir, "users", "0", "presets", name + ".json");

  const getPresets = () => fetch(base + "/api/presets").then((r) => r.json()).then((j) => j.presets || []);
  const getPreset = (name) => fetch(base + "/api/preset?name=" + encodeURIComponent(name)).then((r) => r.json());
  const deletePreset = (name) => fetch(base + "/api/preset?name=" + encodeURIComponent(name), { method: "DELETE" }).catch(() => {});

  async function cleanupPresets() {
    for (const n of P5_NAMES) { try { await deletePreset(n); } catch (e) { /* best effort */ } }
    try { if (fs.existsSync(shadowLibPath)) fs.unlinkSync(shadowLibPath); } catch (e) { /* best effort */ }
  }

  // Same helper as specs 12 and 16: a fixed sleep after a reload is a guess,
  // and the real race is boot()'s own async restore of this clip's SAVED
  // GRADE (selectClip -> grades.js fetches /api/grade and writes it into
  // S.slots). Measured: that fetch can still be in flight after
  // waitForBootComplete resolves (which only proves Panels.build got far
  // enough to render section.stage, well before fillPresets/bind/selectClip
  // run), and if a case's own loadPresetUI click lands first, the grade
  // fetch's own late resolution overwrites the freshly loaded preset a
  // moment later with the clip's pre-reload grade, a false "Load did
  // nothing" reading that is actually a reload-settle race, not a preset
  // bug. window.StudioGrades.isPending() and #gradeSaveState are exposed
  // "for the UI test harness" for exactly this wait.
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

  async function readConfig() {
    await page.click("#jsonBtn");
    await sleep(150);
    const text = await page.$eval("#jsonText", (el) => el.value);
    await page.evaluate(() => {
      const o = document.getElementById("jsonOverlay");
      if (o) o.classList.remove("on");
    });
    return JSON.parse(text);
  }

  // Guarded: page.select() on a value with no matching <option> does not
  // throw, it leaves the <select> on whatever a plain HTML select falls
  // back to (its first option), which in this dropdown is a REAL library
  // preset (alphabetically first). Loading, then Overwrite, would silently
  // land on that real preset instead of failing loudly. Measured: this bug,
  // before this guard existed, actually did it (see the FIXED entry in
  // limits.js), which is why this checks the option landed before clicking
  // Load at all.
  async function loadPresetUI(name) {
    await page.select("#presetSelect", name);
    const got = await page.$eval("#presetSelect", (el) => el.value);
    if (got !== name) {
      throw new Error("refusing to Load: #presetSelect could not be set to " + JSON.stringify(name)
        + " (it reads " + JSON.stringify(got) + ", so that option does not exist in the dropdown yet)");
    }
    const before = await presetCalls("__presetLoads", name);
    await page.click("#loadPresetBtn");
    await waitForPresetCall("__presetLoads", name, before,
      'Load of "' + name + '": GET /api/preset never came back');
  }

  /* How many times this page has finished a preset request for NAME. See the
     evaluateOnNewDocument block below for why the count exists; before that
     block has been installed (this spec's first navigation) the arrays are
     undefined and this reads 0, which is exactly the "nothing to wait for"
     answer a caller wants then. */
  async function presetCalls(bucket, name) {
    return page.evaluate((b, n) => {
      const rows = window[b] || [];
      return rows.filter((x) => x === n).length;
    }, bucket, name);
  }

  /* Wait for one more completed round trip for NAME, then let the page's own
     handler for it run. The ceiling is generous on purpose: this is waiting on
     a request queued behind up to a second of cold-cache frame rendering, and
     a 20 s ceiling that never fires costs nothing while a 450 ms sleep that
     is 50 ms short costs a false failure. The settle sleep after it is because
     the count is incremented when the RESPONSE arrives, and app.js reads the
     JSON body and applies it a moment later. */
  async function waitForPresetCall(bucket, name, wasCount, whatFailed) {
    const deadline = Date.now() + 20000;
    while (Date.now() < deadline) {
      if ((await presetCalls(bucket, name)) > wasCount) {
        await sleep(150);
        return;
      }
      await sleep(50);
    }
    throw new Error(whatFailed + " within 20s");
  }

  function armDialogs(answers) {
    const queue = answers.slice();
    const seen = [];
    const handler = async (dialog) => {
      seen.push(dialog.message());
      const val = queue.length ? queue.shift() : "";
      try { await dialog.accept(val); } catch (e) { try { await dialog.dismiss(); } catch (e2) { /* gone already */ } }
    };
    page.on("dialog", handler);
    return { off: () => page.off("dialog", handler), seen };
  }

  // Save as opens two prompt() dialogs (name, then comment). Confirms
  // against the server's own preset list rather than a toast, so a slow
  // save is a wait, not a false failure.
  async function saveAsUI(name, comment) {
    const armed = armDialogs([name, comment]);
    const before = await presetCalls("__presetSaves", name);
    await page.click("#saveAsBtn");
    let ok = false;
    for (let i = 0; i < 30 && !ok; i++) {
      await sleep(200);
      const list = await getPresets();
      ok = list.some((p) => p.name === name);
    }
    armed.off();
    // The loop above proves the FILE is there, which is what the case asserts;
    // this waits for the page's own half of the same round trip (savePreset's
    // handler is what sets S.presetName and #presetSelect, and the very next
    // helper a caller reaches for, overwriteUI, refuses to click if that has
    // not happened yet). Nothing to wait for if the save never landed.
    if (ok) {
      await waitForPresetCall("__presetSaves", name, before,
        '"Save as" of "' + name + '": POST /api/preset never came back');
    }
    return { ok, seen: armed.seen };
  }

  // Same guard, the other direction: Overwrite writes to S.presetName (or,
  // failing that, whatever #presetSelect currently shows), so a caller here
  // MUST say which preset it believes it is overwriting and this refuses to
  // click the button at all if the dropdown disagrees, rather than writing
  // over an unintended preset.
  async function overwriteUI(expectedName) {
    const got = await page.$eval("#presetSelect", (el) => el.value);
    if (got !== expectedName) {
      throw new Error("refusing to Overwrite: #presetSelect shows " + JSON.stringify(got)
        + ", expected " + JSON.stringify(expectedName) + " (a stray Overwrite could hit a real preset)");
    }
    const before = await presetCalls("__presetSaves", expectedName);
    await page.click("#savePresetBtn");
    await waitForPresetCall("__presetSaves", expectedName, before,
      'Overwrite of "' + expectedName + '": POST /api/preset never came back');
  }

  async function toggleCheckbox(sel) {
    const has = await page.$(sel);
    if (!has) throw new Error("no element for " + sel);
    await page.click(sel);
    await sleep(200);
  }

  async function toggleFxHalation() {
    const boxes = await page.$$('section.stage[data-stage="fx"] input.ctl-switch');
    if (!boxes.length) throw new Error("no checkboxes found in the fx stage");
    // Schema order (schema.js, fx stage): halation.enabled is the first CHK.
    await boxes[0].click();
    await sleep(200);
  }

  async function selectLut2(name) {
    const sels = await page.$$('section.stage[data-stage="look"] select');
    if (sels.length < 2) throw new Error("expected 2 <select> in the look stage (lut, lut2), found " + sels.length);
    await sels[1].select(name);
    await sleep(300);
  }

  const CURVES_CHK = 'section.stage[data-stage="curves"] input.ctl-switch';
  const GRAIN_CHK = 'section.stage[data-stage="grain"] input.ctl-switch';

  // Installed via evaluateOnNewDocument (only takes effect on the NEXT
  // navigation, same reason spec 12's __gradePuts probe does the same
  // thing) so this spec does one deliberate reload right after arming it,
  // to get a fresh boot with the patch live from the first script tag.
  //
  // It also counts the preset round trips this spec's own helpers have to wait
  // for. Load and Overwrite are both a request to the server whose ANSWER is
  // what lands (Load replaces the live config from the response; Overwrite is
  // only on disk once the POST comes back), and a fixed sleep is not a wait for
  // an answer. Measured on a server started with a fresh --data-dir, which is
  // what run.mjs does and which since studio/server.py's _default_cache_dir
  // means a COLD frame cache: the four scope/stats renders that follow every
  // committed change take about a second each, Chrome's six-connections-per-host
  // limit queues the next click's request behind them, and an Overwrite POST
  // that the server handled in 4 ms did not reach it for 1049 ms. Counting
  // completions is a NEUTRAL signal ("the request came back"), not a check on
  // the value any case then asserts.
  await page.evaluateOnNewDocument(() => {
    window.__lutLookPosts = [];
    window.__sessionPosts = [];
    window.__presetLoads = [];
    window.__presetSaves = [];
    const orig = window.fetch;
    window.fetch = function (input, init) {
      const url = typeof input === "string" ? input : (input && input.url) || "";
      const method = String((init && init.method) || (input && input.method) || "GET").toUpperCase();
      if (method === "GET" && /\/api\/preset\?/.test(url)) {
        const m = /[?&]name=([^&]*)/.exec(url);
        const who = m ? decodeURIComponent(m[1]) : "";
        return orig.apply(this, arguments).then(function (r) {
          window.__presetLoads.push(who);
          return r;
        });
      }
      if (method === "POST" && /\/api\/preset(\?|$)/.test(url)) {
        let who = "";
        try {
          const bt = (init && init.body) || "";
          const body = typeof bt === "string" ? JSON.parse(bt) : null;
          who = (body && body.name) || "";
        } catch (e) { /* not JSON, not ours */ }
        return orig.apply(this, arguments).then(function (r) {
          window.__presetSaves.push(who);
          return r;
        });
      }
      if (method === "POST" && /\/api\/lut(\?|$)/.test(url)) {
        try {
          const bt = (init && init.body) || "";
          const body = typeof bt === "string" ? JSON.parse(bt) : null;
          if (body && body.kind === "look") window.__lutLookPosts.push(body.name);
        } catch (e) { /* not JSON, not ours */ }
      }
      if (method === "POST" && /\/api\/session(\?|$)/.test(url)) {
        try {
          const bt = (init && init.body) || "";
          const body = typeof bt === "string" ? JSON.parse(bt) : null;
          window.__sessionPosts.push(body);
        } catch (e) { /* not JSON, not ours */ }
      }
      return orig.apply(this, arguments);
    };
  });

  const cases = [];
  function record(name, ok, note) { cases.push({ name, ok, note }); }

  try {
    await cleanupPresets(); // in case a previous, interrupted run left p5_* behind

    await page.goto(base + "/", { waitUntil: "domcontentloaded", timeout: 30000 });
    await ctx.waitForBootComplete(20000);
    await waitForGradeSettled(4000);

    // ---- case: Load replaces the WHOLE config, including layers/lut2 ----
    try {
      await loadPresetUI("flat");
      const flatCfg = await readConfig();
      record("load strips _comment (flat has one on disk)", !("_comment" in flatCfg),
        ("_comment" in flatCfg) ? "live config after loading flat still carries _comment=" + JSON.stringify(flatCfg._comment)
                                : "live config after loading flat has no _comment key");

      await loadPresetUI("cinematic");
      const cineCfg = await readConfig();
      record("load strips _comment (cinematic has one on disk)", !("_comment" in cineCfg),
        ("_comment" in cineCfg) ? "live config after loading cinematic still carries _comment=" + JSON.stringify(cineCfg._comment)
                                : "live config after loading cinematic has no _comment key");

      const cineServer = await getPreset("cinematic");
      const cineEq = deepEqual(stripComment(cineCfg), stripComment(cineServer.config));
      record("load matches full_config(cinematic) exactly", cineEq,
        cineEq ? "live config equals GET /api/preset?name=cinematic (comment aside)"
               : "mismatch: live=" + JSON.stringify(cineCfg) + " server=" + JSON.stringify(cineServer.config));

      // Load B (flat) after A (cinematic): every field A turned on must go
      // back to B's own value (its default, since flat sets almost nothing).
      await loadPresetUI("flat");
      const flatCfg2 = await readConfig();
      const resetChecks = {
        "fx.halation.enabled": [flatCfg2.fx.halation.enabled, false],
        "fx.bloom.enabled": [flatCfg2.fx.bloom.enabled, false],
        "grain.enabled": [flatCfg2.grain.enabled, false],
        "look.lut": [flatCfg2.look.lut, null],
        "primaries.contrast": [flatCfg2.primaries.contrast, 1.0],
        "detail.sharpen": [flatCfg2.detail.sharpen, 0.0],
      };
      const resetFails = Object.entries(resetChecks).filter(([, [got, want]]) => got !== want);
      record("loading B after A resets every field A changed", resetFails.length === 0,
        resetFails.length === 0
          ? "all " + Object.keys(resetChecks).length + " checked fields reset to flat/default"
          : "left over from cinematic: " + JSON.stringify(Object.fromEntries(resetFails)));

      // Layers and the second look slot: build a config that has both, save
      // it as a fixture, blow it away with a plain preset, then load the
      // fixture back and check every field including layers/lut2.
      await loadPresetUI("cinematic");
      await page.click("#layersAddBtn");
      await sleep(250);
      await selectLut2("kodak2383");
      const beforeSave = await readConfig();
      const layerLanded = Array.isArray(beforeSave.layers) && beforeSave.layers.length === 1 && beforeSave.look.lut2 === "kodak2383";
      record("layer + look LUT 2 landed in the live config before saving the fixture", layerLanded,
        layerLanded ? "1 layer present, look.lut2=kodak2383"
                    : "layers=" + JSON.stringify(beforeSave.layers) + " look.lut2=" + beforeSave.look.lut2);

      const saved = await saveAsUI("p5_full", "p5 full config fixture (layers + look LUT 2)");
      record("save-as p5_full for the layers/lut2 fixture", saved.ok,
        saved.ok ? "p5_full appeared in /api/presets" : "p5_full never appeared, dialogs seen: " + JSON.stringify(saved.seen));

      await loadPresetUI("flat");
      const flatCfg3 = await readConfig();
      const flatReset = (flatCfg3.layers || []).length === 0 && flatCfg3.look.lut2 === null;
      record("flat resets layers/lut2 to empty/null before the round trip", flatReset,
        flatReset ? "layers=[] and look.lut2=null on flat"
                  : "flat still shows layers=" + JSON.stringify(flatCfg3.layers) + " lut2=" + flatCfg3.look.lut2);

      await loadPresetUI("p5_full");
      const p5FullCfg = await readConfig();
      const p5FullServer = await getPreset("p5_full");
      const fullEq = deepEqual(stripComment(p5FullCfg), stripComment(p5FullServer.config));
      const layersEq = Array.isArray(p5FullCfg.layers) && p5FullCfg.layers.length === 1
        && deepEqual(p5FullCfg.layers, p5FullServer.config.layers);
      const lut2Eq = p5FullCfg.look.lut2 === "kodak2383";
      const allOk = fullEq && layersEq && lut2Eq;
      record("load p5_full restores layers and the second look slot exactly", allOk,
        allOk ? "layers (1 entry) and look.lut2=kodak2383 both came back, full config equals full_config(p5_full)"
              : "fullEq=" + fullEq + " layersEq=" + layersEq + " lut2Eq=" + lut2Eq
                + " live.layers=" + JSON.stringify(p5FullCfg.layers) + " live.lut2=" + p5FullCfg.look.lut2);
    } catch (err) {
      record("case: load replaces the whole config", false, "threw: " + (err && err.message || err));
    }

    // ---- case: Overwrite writes every changed field, and it survives a reload
    try {
      await loadPresetUI("cinematic");
      const savedA = await saveAsUI("p5_a", "p5 case2 base (a copy of cinematic)");
      if (!savedA.ok) throw new Error("p5_a never appeared: " + JSON.stringify(savedA.seen));

      const baseCfg = await readConfig();
      const base3 = { curves: baseCfg.curves.enabled, grain: baseCfg.grain.enabled, halation: baseCfg.fx.halation.enabled };

      await toggleCheckbox(CURVES_CHK);
      await toggleCheckbox(GRAIN_CHK);
      await toggleFxHalation();
      const changedCfg = await readConfig();
      const changed3 = { curves: changedCfg.curves.enabled, grain: changedCfg.grain.enabled, halation: changedCfg.fx.halation.enabled };
      const reallyChanged = changed3.curves !== base3.curves && changed3.grain !== base3.grain && changed3.halation !== base3.halation;
      if (!reallyChanged) {
        throw new Error("the three toggles did not all flip: before=" + JSON.stringify(base3) + " after=" + JSON.stringify(changed3));
      }

      await overwriteUI("p5_a");
      const afterOverwrite = await getPreset("p5_a");
      const wroteAll = afterOverwrite.config.curves.enabled === changed3.curves
        && afterOverwrite.config.grain.enabled === changed3.grain
        && afterOverwrite.config.fx.halation.enabled === changed3.halation;
      record("overwrite writes every changed field", wroteAll,
        wroteAll ? "p5_a.json now holds all three toggled values"
                 : "p5_a.json on disk is " + JSON.stringify({
                     curves: afterOverwrite.config.curves.enabled,
                     grain: afterOverwrite.config.grain.enabled,
                     halation: afterOverwrite.config.fx.halation.enabled,
                   }) + " expected " + JSON.stringify(changed3));

      await page.goto(base + "/", { waitUntil: "domcontentloaded", timeout: 30000 });
      await ctx.waitForBootComplete(20000);
      await waitForGradeSettled(4000);
      await loadPresetUI("p5_a");
      const afterReloadLoad = await readConfig();
      const cameBack = afterReloadLoad.curves.enabled === changed3.curves
        && afterReloadLoad.grain.enabled === changed3.grain
        && afterReloadLoad.fx.halation.enabled === changed3.halation;
      record("overwrite survives a reload, then Load brings all three back", cameBack,
        cameBack ? "after a reload, loading p5_a again shows all three overwritten values"
                 : "after reload, p5_a loaded as " + JSON.stringify({
                     curves: afterReloadLoad.curves.enabled,
                     grain: afterReloadLoad.grain.enabled,
                     halation: afterReloadLoad.fx.halation.enabled,
                   }));
    } catch (err) {
      record("case: overwrite round trip", false, "threw: " + (err && err.message || err));
    }

    // ---- case: Save as with a blank comment must not inherit the loaded preset's comment
    try {
      await loadPresetUI("cinematic"); // cinematic has a real _comment on disk
      await toggleCheckbox(GRAIN_CHK);
      const savedC = await saveAsUI("p5_c", "");
      if (!savedC.ok) throw new Error("p5_c never appeared: " + JSON.stringify(savedC.seen));

      const filePath = userPresetFile("p5_c");
      const rawFile = fs.existsSync(filePath) ? JSON.parse(fs.readFileSync(filePath, "utf8")) : null;
      const fileHasComment = !!(rawFile && Object.prototype.hasOwnProperty.call(rawFile, "_comment"));
      const serverC = await getPreset("p5_c");
      const configHasComment = Object.prototype.hasOwnProperty.call(serverC.config, "_comment");
      const ok = !fileHasComment && !configHasComment;
      record("save-as with a blank comment does not inherit the loaded preset's comment", ok,
        ok ? "p5_c.json has no _comment (cinematic's comment is not present)"
           : "p5_c leaked a comment: file._comment=" + JSON.stringify(rawFile && rawFile._comment)
             + " config._comment=" + JSON.stringify(serverC.config._comment));
    } catch (err) {
      record("case: save-as comment does not leak", false, "threw: " + (err && err.message || err));
    }

    // ---- case: Overwrite on a library preset writes a user copy that shadows it
    try {
      fs.writeFileSync(shadowLibPath, JSON.stringify({
        _comment: "p5 fixture (library side), deleted by studio/tests/specs/20-presets.mjs",
        convert: { exposure: 0.11 },
        grain: { enabled: true },
      }, null, 2) + "\n");

      // Writing the file does not tell the open tab: #presetSelect only
      // regains its options through fillPresets(), which only runs on boot
      // or after a save/delete round trip through the UI. Without this
      // reload, the next loadPresetUI("p5_shadow_test") would find no such
      // <option>, and its own guard would (correctly) refuse to proceed;
      // this reload is what makes the fixture actually loadable for real,
      // the way it would be if a second account had saved it moments ago.
      await page.goto(base + "/", { waitUntil: "domcontentloaded", timeout: 30000 });
      await ctx.waitForBootComplete(20000);
      await waitForGradeSettled(4000);

      let list = await getPresets();
      let entries = list.filter((p) => p.name === "p5_shadow_test");
      record("library fixture appears once, marked library", entries.length === 1 && entries[0] && entries[0].library === true,
        "entries: " + JSON.stringify(entries));

      await loadPresetUI("p5_shadow_test");
      const loaded = await readConfig();
      const loadedOk = loaded.convert.exposure === 0.11 && loaded.grain.enabled === true;
      record("loading the library preset reads the library file", loadedOk,
        loadedOk ? "convert.exposure=0.11 grain.enabled=true, as in the library fixture"
                 : "got convert.exposure=" + loaded.convert.exposure + " grain.enabled=" + loaded.grain.enabled);

      await toggleCheckbox(GRAIN_CHK); // true -> false
      await overwriteUI("p5_shadow_test");

      list = await getPresets();
      entries = list.filter((p) => p.name === "p5_shadow_test");
      record("overwrite on a library preset leaves exactly one dropdown entry, now mine", entries.length === 1 && entries[0] && entries[0].library === false,
        "entries after overwrite: " + JSON.stringify(entries));

      const afterOverwrite = await getPreset("p5_shadow_test");
      const shadowServed = afterOverwrite.config.grain.enabled === false;
      record("GET /api/preset now serves the user's shadow, not the library file", shadowServed,
        shadowServed ? "grain.enabled=false (the overwritten value); the library file on disk still has true"
                     : "GET /api/preset returned grain.enabled=" + afterOverwrite.config.grain.enabled);
    } catch (err) {
      record("case: overwrite shadows a library preset", false, "threw: " + (err && err.message || err));
    } finally {
      await deletePreset("p5_shadow_test");
      try { if (fs.existsSync(shadowLibPath)) fs.unlinkSync(shadowLibPath); } catch (e) { /* best effort */ }
    }

    // ---- case: the modified dot is off right after Load, on after one change
    try {
      await loadPresetUI("flat");
      const dotAfterLoad = await page.$eval("#modifiedDot", (el) => el.classList.contains("on"));
      await toggleCheckbox(CURVES_CHK);
      const dotAfterChange = await page.$eval("#modifiedDot", (el) => el.classList.contains("on"));
      const ok = dotAfterLoad === false && dotAfterChange === true;
      record("modified dot: off right after load, on after one change", ok,
        "dot.on after load=" + dotAfterLoad + ", dot.on after one checkbox flip=" + dotAfterChange);
    } catch (err) {
      record("case: modified dot", false, "threw: " + (err && err.message || err));
    }

    // ---- case: after a preset load, the GPU preview picks up the new look
    try {
      // A fresh reload, not just a fresh __lutLookPosts array: gpu.js caches
      // an uploaded LUT texture per instance keyed by name (Instance.luts),
      // and "cinematic" (look.lut = blockbuster) was already loaded earlier
      // in this same spec (case 1), so without a new page its texture would
      // already be resident and no POST /api/lut would fire on the second
      // load below, that being the correct, intentional no-refetch behaviour
      // rather than a bug. Reloading gives this case its own GPU instance
      // with an empty cache, so "did it reupload" is a real signal again.
      await page.goto(base + "/", { waitUntil: "domcontentloaded", timeout: 30000 });
      await ctx.waitForBootComplete(20000);
      await waitForGradeSettled(4000);

      let liveNow = await page.evaluate(() => {
        const s = document.getElementById("stage");
        return !!(s && s.classList.contains("gpu-live"));
      }).catch(() => false);
      if (!liveNow) {
        try {
          await page.waitForFunction(() => {
            const s = document.getElementById("stage");
            return !!(s && s.classList.contains("gpu-live"));
          }, { timeout: 12000 });
          liveNow = true;
        } catch (e) { liveNow = false; }
      }
      if (!liveNow) {
        record("gpu preview agrees after a preset load", null,
          "SKIP: #stage never reached gpu-live within 12s, GPU preview unavailable on this machine");
      } else {
        const samplePixels = () => page.evaluate(() => {
          const c = document.getElementById("gpuCanvas");
          const w = Math.min(32, c.width), h = Math.min(32, c.height);
          const t = document.createElement("canvas");
          t.width = w; t.height = h;
          t.getContext("2d").drawImage(c, 0, 0, w, h);
          return Array.from(t.getContext("2d").getImageData(0, 0, w, h).data);
        });

        await loadPresetUI("flat"); // look.lut null: no "look"-kind /api/lut POST expected
        await sleep(300);
        const before = await page.evaluate(() => (window.__lutLookPosts || []).length);
        const beforePixels = await samplePixels();

        await loadPresetUI("cinematic"); // look.lut = blockbuster
        let after = before;
        for (let i = 0; i < 25 && after <= before; i++) {
          await sleep(200);
          after = await page.evaluate(() => (window.__lutLookPosts || []).length);
        }
        await sleep(300);
        const afterPixels = await samplePixels();

        const reuploaded = after > before;
        let pixelsChanged = false;
        for (let i = 0; i < beforePixels.length; i++) {
          if (Math.abs(beforePixels[i] - afterPixels[i]) > 2) { pixelsChanged = true; break; }
        }
        const ok = reuploaded && pixelsChanged;
        record("gpu preview agrees after a preset load", ok,
          "used the lighter substitute the plan allows (full numeric server/GPU parity is the parity-gate harness, "
          + "minutes long, not run here): look-kind /api/lut POSTs went " + before + " -> " + after
          + " (reuploaded=" + reuploaded + "), a 32x32 sample of #gpuCanvas changed=" + pixelsChanged);
      }
    } catch (err) {
      record("case: gpu preview after preset load", false, "threw: " + (err && err.message || err));
    }

    // ---- case: loading a preset publishes message: "loaded preset NAME"
    try {
      await page.evaluate(() => { window.__sessionPosts = []; });
      const logRes = await fetch(base + "/api/project/log").catch(() => null);
      const logAvailable = !!(logRes && logRes.status !== 404);
      /* This case counts commits, so it starts from a log that has stopped
         moving: a commit still in flight from the case before it (the preset
         load and LUT upload the GPU check just did) would otherwise land
         inside this window and read as this one preset load making two.
         Bounded, and it waits only for quiet, never for a number. */
      let beforeLen = null;
      if (logAvailable) {
        const readLen = () => fetch(base + "/api/project/log")
          .then((r) => r.json())
          .then((j) => (j && Array.isArray(j.commits) ? j.commits.length : null))
          .catch(() => null);
        let previous = await logRes.json().catch(() => null);
        previous = previous && Array.isArray(previous.commits) ? previous.commits.length : null;
        beforeLen = previous;
        for (let i = 0; i < 25; i++) {
          await sleep(200);
          const now = await readLen();
          beforeLen = now === null ? beforeLen : now;
          if (now !== null && now === previous) break;
          previous = now;
        }
      }

      await loadPresetUI("flat");
      await sleep(250);
      const posts = await page.evaluate(() => window.__sessionPosts || []);
      const last = posts.length ? posts[posts.length - 1] : null;
      const messageOk = !!(last && last.message === "loaded preset flat");
      record("loadPreset sends message: \"loaded preset NAME\" on its publish", messageOk,
        messageOk ? "POST /api/session carried message=" + JSON.stringify(last.message)
                  : "no matching POST /api/session message field, last post body=" + JSON.stringify(last));

      if (!logAvailable) {
        record("loading a preset records one commit \"loaded preset NAME\"", null,
          "SKIP: GET /api/project/log is not available on this server yet (P1's project store had not landed when this ran)");
      } else {
        let afterLen = beforeLen, afterMsg = null;
        for (let i = 0; i < 20; i++) {
          const after = await fetch(base + "/api/project/log").then((r) => r.json()).catch(() => null);
          if (after && Array.isArray(after.commits)) {
            afterLen = after.commits.length;
            afterMsg = after.commits[0] && after.commits[0].message;
            if (afterLen !== beforeLen) break;
          }
          await sleep(200);
        }
        const ok = afterLen === beforeLen + 1 && afterMsg === "loaded preset flat";
        record("loading a preset records one commit \"loaded preset NAME\"", ok,
          ok ? "the project log grew by exactly one commit, message=" + JSON.stringify(afterMsg)
             : "log length " + beforeLen + " -> " + afterLen + ", newest message=" + JSON.stringify(afterMsg));
      }
    } catch (err) {
      record("case: loaded-preset commit", false, "threw: " + (err && err.message || err));
    }

    const fails = cases.filter((c) => c.ok === false);
    const skips = cases.filter((c) => c.ok === null);
    const evidence = cases
      .map((c) => (c.ok === true ? "PASS" : c.ok === false ? "FAIL" : "SKIP") + " [" + c.name + "] " + c.note)
      .join(" || ");

    if (fails.length) {
      return { status: "FAIL", evidence: fails.length + " of " + cases.length + " cases failed. " + evidence };
    }
    return { status: "PASS", evidence: cases.length + " cases, " + skips.length + " skipped. " + evidence };
  } finally {
    try { await cleanupPresets(); } catch (e) { /* best effort */ }
    try {
      await page.goto(base + "/", { waitUntil: "domcontentloaded", timeout: 30000 });
      await ctx.waitForBootComplete(20000);
    } catch (e) { /* the result above is what matters, not the tidy up */ }
  }
}
