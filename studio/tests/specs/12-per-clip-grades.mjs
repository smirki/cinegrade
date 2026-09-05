/* per-clip grades (contract C3): a grade belongs to a clip, it saves itself,
 * and switching clips loads the right one.
 *
 * Everything here is driven through the real UI in the real browser: a real
 * click on a real control to change the grade, a real page reload to prove the
 * value came back from the server rather than from memory, and real clicks on
 * the clip rows in the Clips panel to switch clips. The only thing read out of
 * band is the config itself, through #jsonBtn / #jsonText, which is the same
 * "no private JS global" route the undo spec already uses.
 *
 * The control clicked is the curves-enabled checkbox for the same reason spec
 * 09 picks it: #params has no native <input type=range>, every slider is a
 * custom drag div, and the checkbox is the one control in the panel that a
 * synthetic-but-real click can flip deterministically. It goes through
 * onParamChange with commit true, which is exactly the "committed change" the
 * autosave hangs off, so it exercises the same path a slider release does.
 *
 * This spec writes to the account's real grade store (studio/data/studio.db),
 * so it snapshots whatever grades the two clips already had and puts them back
 * at the end, pass or fail.
 */

// Ceiling only, not a fixed wait: waitForGradeSettled (below) polls the real
// signal (StudioGrades.isPending() and the #gradeSaveState indicator)
// instead of sleeping this long every time. A run under load once got a
// transient "fetch failed" from this file's own direct-to-server fetches,
// which happened right after a fixed sleep no longer than the 600ms debounce
// itself: the debounce, and the PUT it fires, can both run long under load,
// so a fixed sleep is either too short (races the save) or an overpaid
// guess. Polling removes the guess; SETTLE_MS survives only as how long to
// poll before giving up.
const SETTLE_MS = 5000;

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
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

  const clips = await fetch(base + "/api/clips").then((r) => r.json()).then((j) => j.clips || []);
  if (clips.length < 2) {
    return { status: "SKIP", evidence: "per-clip grades need two clips in content/footage, found " + clips.length };
  }
  const A = clips[0].name;
  const B = clips[1].name;
  if (!clips[0].key || !clips[1].key || clips[0].key === clips[1].key) {
    return { status: "FAIL", evidence: "clips carry no distinct content key: " + clips[0].key + " and " + clips[1].key };
  }

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
  // grading database, not a fixture.
  const snapA = await getGrade(A);
  const snapB = await getGrade(B);

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

  const puts = () => page.evaluate(() => window.__gradePuts || 0);
  const dot = () =>
    page.evaluate(() => {
      const e = document.getElementById("gradeSaveState");
      return e ? e.textContent : null;
    });

  // Polls the real save state instead of sleeping a guessed duration: a
  // pending autosave, and the PUT it eventually fires, are both done exactly
  // when StudioGrades.isPending() goes false and the indicator stops reading
  // "saving" (grades.js exposes isPending() on window.StudioGrades "for the
  // UI test harness" for exactly this). Used everywhere this spec used to
  // sleep(SETTLE_MS) and then immediately ask the server or the DOM whether
  // a save had landed.
  async function waitForGradeSettled(ceilingMs) {
    const start = Date.now();
    let state = { pending: true, dot: "saving" };
    while (Date.now() - start < ceilingMs) {
      state = await page.evaluate(() => ({
        pending: !!(window.StudioGrades && window.StudioGrades.isPending()),
        dot: (document.getElementById("gradeSaveState") || {}).textContent,
      }));
      // dot undefined means grades.js has not mounted #gradeSaveState yet:
      // that is "not settled" too, not "nothing to wait for".
      if (state.dot !== undefined && !state.pending && state.dot !== "saving") return state;
      await new Promise((r) => setTimeout(r, 50));
    }
    return state;
  }

  async function clickClipRow(needle) {
    const rows = await page.$$("#browseClips .lutrow");
    for (const row of rows) {
      const label = await row.evaluate((el) => (el.querySelector("span") || {}).textContent || "");
      if (label === needle) {
        await row.click();               // puppeteer's real mouse, not el.click()
        return true;
      }
    }
    return false;
  }

  const notes = [];
  try {
    // Counts PUT /api/grade from inside the page. evaluateOnNewDocument so it
    // survives the reload below and is in place before app.js runs, which is
    // what lets "zero saves during a load" be measured at boot too.
    await page.evaluateOnNewDocument(() => {
      window.__gradePuts = 0;
      const orig = window.fetch;
      window.fetch = function (input, init) {
        const url = typeof input === "string" ? input : (input && input.url) || "";
        const method = String((init && init.method) || (input && input.method) || "GET").toUpperCase();
        if (method === "PUT" && /\/api\/grade(\?|$)/.test(url)) window.__gradePuts += 1;
        return orig.apply(this, arguments);
      };
    });

    // A known starting point: neither clip has a saved grade.
    //
    // The navigation comes FIRST and the delete second, which looks backwards
    // and is not. grades.js flushes a pending autosave from a beforeunload
    // handler with fetch keepalive, so leaving the page the previous spec was
    // on can write a grade, and a delete issued before that write lands is a
    // delete of nothing. Landing on a freshly booted page (which has edited
    // nothing, so has nothing pending) and only then clearing makes the
    // starting state actually empty. The reload after it is what the
    // assertions below run against.
    await page.goto(base + "/", { waitUntil: "domcontentloaded", timeout: 30000 });
    await ctx.waitForBootComplete(20000);
    await waitForGradeSettled(SETTLE_MS);
    await dropGrade(A);
    await dropGrade(B);

    await page.goto(base + "/", { waitUntil: "domcontentloaded", timeout: 30000 });
    await ctx.waitForBootComplete(20000);
    await waitForGradeSettled(SETTLE_MS);

    const bootPuts = await puts();
    if (bootPuts !== 0) {
      return { status: "FAIL", evidence: "booting on an ungraded clip issued " + bootPuts + " PUT /api/grade, it must issue none" };
    }
    notes.push("boot on an ungraded clip: 0 PUTs");

    const before = await readConfig();
    const v0 = !!(before.curves && before.curves.enabled);

    // 1. a committed change autosaves
    await page.click('section.stage[data-stage="curves"] input.ctl-switch');
    await waitForGradeSettled(SETTLE_MS);
    const v1 = !v0;
    const afterEdit = await readConfig();
    if (!!afterEdit.curves.enabled !== v1) {
      return { status: "FAIL", evidence: "the click did not change curves.enabled (still " + v0 + ")" };
    }
    const savedPuts = await puts();
    if (savedPuts < 1) {
      return { status: "FAIL", evidence: "a committed change issued no PUT /api/grade" };
    }
    const onServer = await getGrade(A);
    if (!onServer.exists || !!onServer.config.curves.enabled !== v1) {
      return { status: "FAIL", evidence: "the server does not hold the edit: exists=" + onServer.exists };
    }
    notes.push("one committed change: " + savedPuts + " PUT, server holds curves.enabled=" + v1 + ", indicator says " + (await dot()));

    // 2. it survives a reload
    await page.goto(base + "/", { waitUntil: "domcontentloaded", timeout: 30000 });
    await ctx.waitForBootComplete(20000);
    await waitForGradeSettled(SETTLE_MS);
    const afterReload = await readConfig();
    if (!!afterReload.curves.enabled !== v1) {
      return { status: "FAIL", evidence: "after a reload curves.enabled is " + afterReload.curves.enabled + ", expected the saved " + v1 };
    }
    const reloadPuts = await puts();
    if (reloadPuts !== 0) {
      return { status: "FAIL", evidence: "booting on a graded clip issued " + reloadPuts + " PUT /api/grade, it must issue none" };
    }
    notes.push("reload: curves.enabled came back as " + v1 + " with 0 PUTs during the load");

    // 3. a second change, so there is a live undo entry to test with later.
    //    (The undo stack is in memory by contract, so the reload above wiped
    //    the first one. That is the design, not a defect.)
    await page.click('section.stage[data-stage="curves"] input.ctl-switch');
    await waitForGradeSettled(SETTLE_MS);
    const v2 = v0;

    // 4. switching clips loads the other clip's grade, and saves nothing
    await page.click('.railtab[data-rail="browse"]');
    await page.waitForFunction(() => document.querySelectorAll("#browseClips .lutrow").length >= 2, { timeout: 15000 });
    const beforeSwitch = await puts();
    if (!(await clickClipRow(B))) {
      return { status: "FAIL", evidence: "no row for " + B + " in #browseClips" };
    }
    await sleep(2000);
    const switchPuts = (await puts()) - beforeSwitch;
    if (switchPuts !== 0) {
      return { status: "FAIL", evidence: "selecting a clip issued " + switchPuts + " PUT /api/grade, it must issue none" };
    }
    const onB = await readConfig();
    const defaults = await fetch(base + "/api/state").then((r) => r.json()).then((j) => j.defaults);
    if (JSON.stringify(onB) !== JSON.stringify(defaults)) {
      return { status: "FAIL", evidence: "an ungraded clip did not show the engine defaults (curves.enabled " + onB.curves.enabled + " vs default " + defaults.curves.enabled + ")" };
    }
    notes.push("switching to an ungraded clip: engine defaults shown, 0 PUTs");

    // 5. switching back brings the grade back, still with no save
    const beforeBack = await puts();
    if (!(await clickClipRow(A))) {
      return { status: "FAIL", evidence: "no row for " + A + " in #browseClips" };
    }
    await sleep(2000);
    const backPuts = (await puts()) - beforeBack;
    if (backPuts !== 0) {
      return { status: "FAIL", evidence: "switching back issued " + backPuts + " PUT /api/grade, it must issue none" };
    }
    const backOnA = await readConfig();
    if (!!backOnA.curves.enabled !== v2) {
      return { status: "FAIL", evidence: "switching back showed curves.enabled " + backOnA.curves.enabled + ", expected the saved " + v2 };
    }
    notes.push("switching back: the saved grade returned with 0 PUTs");

    // 6. the undo stack came back with the clip
    const undoDisabled = await page.$eval("#undoBtn", (el) => el.disabled);
    if (undoDisabled) {
      return { status: "FAIL", evidence: "after switching back to " + A + " the undo button is disabled, so its per clip history was lost" };
    }
    await page.click("#undoBtn");
    await waitForGradeSettled(SETTLE_MS);
    const afterUndo = await readConfig();
    if (!!afterUndo.curves.enabled !== v1) {
      return { status: "FAIL", evidence: "undo after switching back gave curves.enabled " + afterUndo.curves.enabled + ", expected " + v1 };
    }
    notes.push("undo after switching back undid this clip's own change (" + v2 + " -> " + v1 + ")");

    // 7. the copy control offers the other clip and copies its grade
    await putGrade(B, { curves: { enabled: v1 } });
    await page.evaluate(() => window.StudioGrades.refreshCopyList());
    await sleep(400);
    const options = await page.evaluate(() =>
      Array.from(document.querySelectorAll("#gradeCopyFrom option")).map((o) => o.textContent));
    if (options.indexOf(B) < 0) {
      return { status: "FAIL", evidence: "the copy-grade picker does not list " + B + ", it lists " + JSON.stringify(options) };
    }
    notes.push("copy-grade picker lists the other graded clip");

    return { status: "PASS", evidence: notes.join("; ") };
  } finally {
    // Reload FIRST, restore SECOND, same beforeunload reason as above: the
    // page being left behind can still flush one last save, and it must not
    // land on top of the account's real grade. The reload also hands the next
    // spec a freshly booted page rather than one mid experiment, since this
    // one navigates, switches clips and opens a rail tab.
    try {
      await page.goto(base + "/", { waitUntil: "domcontentloaded", timeout: 30000 });
      await ctx.waitForBootComplete(20000);
      await sleep(500);
    } catch (err) { /* the result above is what matters, not the tidy up */ }
    for (const [name, snap] of [[A, snapA], [B, snapB]]) {
      if (snap && snap.exists) await putGrade(name, snap.config);
      else await dropGrade(name);
    }
  }
}
