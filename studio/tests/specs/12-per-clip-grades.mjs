/* per-clip grades, as contract C4 leaves them: a clip's grade is its
 * project's HEAD commit.
 *
 * This spec used to pin contract C3's model: a debounced PUT /api/grade after
 * every committed change, and "a clip with no saved grade shows the engine
 * defaults" as something the CLIENT arranged by publishing them. Both are
 * gone. Opening a clip is POST /api/project/open, which creates the project
 * on first sight with a root commit (the old saved grade migrated, or the
 * engine defaults) and answers with HEAD's config; a committed edit is POST
 * /api/session, which records the next commit; and the server writes the old
 * grades row itself, which is what still fills the copy-from picker.
 *
 * What is unchanged, and is the whole point of the feature, is the behaviour
 * a person sees: every clip owns its grade, switching clips loads the right
 * one, and neither a boot nor a clip switch may write anything.
 *
 * Everything is driven through the real UI: real clicks on a real control to
 * change the grade, a real page load to prove the value came back from the
 * server rather than from memory, and real clicks on the clip rows. The only
 * things read out of band are the config (through #jsonBtn / #jsonText, the
 * same "no private JS global" route spec 09 uses) and the project log.
 *
 * The control clicked is the curves-enabled checkbox for the same reason spec
 * 09 picks it: #params has no native <input type=range>, every slider is a
 * custom drag div, and the checkbox is the one control a synthetic-but-real
 * click can flip deterministically. It goes through onParamChange with commit
 * true, which is exactly the "committed change" a commit hangs off.
 */

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

  const project = (clip) =>
    fetch(base + "/api/project?clip=" + encodeURIComponent(clip)).then((r) => r.json());
  const logOf = (clip) =>
    fetch(base + "/api/project/log?clip=" + encodeURIComponent(clip)).then((r) => r.json());

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

  // Every write the page could make, counted from inside the page. PUT
  // /api/grade must now be zero everywhere (the autosave is retired), and
  // POST /api/session is the one that would silently republish a config over
  // somebody else's edit if a boot or a clip switch ever did it again.
  const counts = () =>
    page.evaluate(() => ({
      gradePuts: window.__p4GradePuts || 0,
      sessionPosts: window.__p4SessionPosts || 0,
    }));

  async function waitForClip(name, ceilingMs) {
    const start = Date.now();
    let seen = null;
    while (Date.now() - start < (ceilingMs || 20000)) {
      seen = await page.evaluate(() => {
        const first = document.querySelector("#clipInfo div span");
        return first ? first.textContent : null;
      });
      if (seen === name) return true;
      await sleep(100);
    }
    return false;
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
  // Counts writes from inside the page. evaluateOnNewDocument so it survives
  // the reloads below and is in place before app.js runs, which is what lets
  // "zero writes during a load" be measured at boot too.
  //
  // The two globals are prefixed because evaluateOnNewDocument scripts are
  // never removed: this one keeps running on every later navigation, in every
  // later spec, for the rest of the run. Spec 20 arms a probe of its own on
  // the same page and used a plainly named window.__sessionPosts (an ARRAY of
  // bodies); this counter incrementing the same name turned that array into
  // the string "1" and cost spec 20 a false failure. One prefix, no collision.
  await page.evaluateOnNewDocument(() => {
    window.__p4GradePuts = 0;
    window.__p4SessionPosts = 0;
    const orig = window.fetch;
    window.fetch = function (input, init) {
      const url = typeof input === "string" ? input : (input && input.url) || "";
      const method = String((init && init.method) || (input && input.method) || "GET").toUpperCase();
      if (method === "PUT" && /\/api\/grade(\?|$)/.test(url)) window.__p4GradePuts += 1;
      if (method === "POST" && /\/api\/session(\?|$)/.test(url)) window.__p4SessionPosts += 1;
      return orig.apply(this, arguments);
    };
  });

  // A freshly booted page with the probe live from the first script tag.
  await page.goto(base + "/", { waitUntil: "domcontentloaded", timeout: 30000 });
  await ctx.waitForBootComplete(20000);
  if (!(await waitForClip(A, 20000))) {
    return { status: "FAIL", evidence: "the page did not settle on " + A + " after a boot" };
  }
  await sleep(900);

  // ---- 1. booting on an existing project writes nothing -------------------
  const bootCounts = await counts();
  if (bootCounts.gradePuts !== 0) {
    return { status: "FAIL", evidence: "booting issued " + bootCounts.gradePuts + " PUT /api/grade, the autosave is retired and it must issue none" };
  }
  if (bootCounts.sessionPosts !== 0) {
    return { status: "FAIL", evidence: "booting issued " + bootCounts.sessionPosts + " POST /api/session, a boot must publish nothing" };
  }
  const openedA = await project(A);
  if (!openedA.open) {
    return { status: "FAIL", evidence: "no project exists for " + A + " after booting on it" };
  }
  notes.push("boot: 0 PUT /api/grade, 0 POST /api/session, project " + openedA.head + " open on " + A);

  // ---- 2. a committed change is a commit ---------------------------------
  const before = await readConfig();
  const v0 = !!(before.curves && before.curves.enabled);
  const logBefore = await logOf(A);

  await page.click('section.stage[data-stage="curves"] input.ctl-switch');
  await sleep(900);
  const v1 = !v0;
  const afterEdit = await readConfig();
  if (!!afterEdit.curves.enabled !== v1) {
    return { status: "FAIL", evidence: "the click did not change curves.enabled (still " + v0 + ")" };
  }
  const logAfter = await logOf(A);
  if (logAfter.commits.length !== logBefore.commits.length + 1) {
    return {
      status: "FAIL",
      evidence: "one committed change should be exactly one commit, the log went from "
        + logBefore.commits.length + " to " + logAfter.commits.length,
    };
  }
  const headA = await project(A);
  if (!!headA.config.curves.enabled !== v1) {
    return { status: "FAIL", evidence: "the project's HEAD does not hold the edit: curves.enabled=" + headA.config.curves.enabled };
  }
  notes.push("one committed change: exactly 1 new commit (\"" + logAfter.commits[0].message
    + "\", author " + logAfter.commits[0].author + "), HEAD holds curves.enabled=" + v1);

  // ---- 3. it survives a reload, and the reload writes nothing -------------
  await page.goto(base + "/", { waitUntil: "domcontentloaded", timeout: 30000 });
  await ctx.waitForBootComplete(20000);
  await waitForClip(A, 20000);
  await sleep(900);
  const afterReload = await readConfig();
  if (!!afterReload.curves.enabled !== v1) {
    return { status: "FAIL", evidence: "after a reload curves.enabled is " + afterReload.curves.enabled + ", expected the committed " + v1 };
  }
  const reloadCounts = await counts();
  if (reloadCounts.gradePuts !== 0 || reloadCounts.sessionPosts !== 0) {
    return {
      status: "FAIL",
      evidence: "booting on a graded clip issued " + reloadCounts.gradePuts + " PUT /api/grade and "
        + reloadCounts.sessionPosts + " POST /api/session, it must issue none",
    };
  }
  const logReload = await logOf(A);
  if (logReload.commits.length !== logAfter.commits.length) {
    return { status: "FAIL", evidence: "the reload added " + (logReload.commits.length - logAfter.commits.length) + " commit(s)" };
  }
  notes.push("reload: curves.enabled came back as " + v1 + " with 0 writes and 0 new commits");

  // ---- 4. a second change, so there is something to undo later ------------
  await page.click('section.stage[data-stage="curves"] input.ctl-switch');
  await sleep(900);
  const v2 = v0;

  // ---- 5. switching clips opens the other project and writes nothing ------
  await page.click('.railtab[data-rail="browse"]');
  // Contract E2 gave the Files pane a root switcher, and #browseClips is the
  // This Mac view's own clip list: it is on screen only while that root is the
  // one showing. Naming the view this spec uses is the whole change; what it
  // then does with the list is untouched.
  await page.click('#filesRoots .filesroot[data-root="thismac"]');
  await page.waitForFunction(() => document.querySelectorAll("#browseClips .lutrow").length >= 2, { timeout: 15000 });
  const beforeSwitch = await counts();
  if (!(await clickClipRow(B))) {
    return { status: "FAIL", evidence: "no row for " + B + " in #browseClips" };
  }
  if (!(await waitForClip(B, 20000))) {
    return { status: "FAIL", evidence: "clicking " + B + " never put it on screen" };
  }
  await sleep(900);
  const switchCounts = await counts();
  if (switchCounts.gradePuts !== beforeSwitch.gradePuts || switchCounts.sessionPosts !== beforeSwitch.sessionPosts) {
    return {
      status: "FAIL",
      evidence: "selecting a clip issued " + (switchCounts.gradePuts - beforeSwitch.gradePuts)
        + " PUT /api/grade and " + (switchCounts.sessionPosts - beforeSwitch.sessionPosts)
        + " POST /api/session, it must issue neither",
    };
  }
  const onB = await readConfig();
  const projB = await project(B);
  if (!projB.open) return { status: "FAIL", evidence: "clicking " + B + " did not open a project for it" };
  if (JSON.stringify(onB) !== JSON.stringify(projB.config)) {
    return {
      status: "FAIL",
      evidence: "the page does not show " + B + "'s HEAD config: curves.enabled on screen "
        + onB.curves.enabled + " vs HEAD " + projB.config.curves.enabled,
    };
  }
  if (!!onB.curves.enabled === v1 && v1 !== v0) {
    return { status: "FAIL", evidence: "switching to " + B + " carried " + A + "'s grade across (curves.enabled " + onB.curves.enabled + ")" };
  }
  notes.push("switching to " + B + ": its own project opened at " + projB.head
    + " and the page shows that HEAD, with 0 writes");

  // ---- 6. switching back restores this clip's own grade -------------------
  const beforeBack = await counts();
  if (!(await clickClipRow(A))) {
    return { status: "FAIL", evidence: "no row for " + A + " in #browseClips" };
  }
  if (!(await waitForClip(A, 20000))) {
    return { status: "FAIL", evidence: "clicking " + A + " never put it back on screen" };
  }
  await sleep(900);
  const backCounts = await counts();
  if (backCounts.gradePuts !== beforeBack.gradePuts || backCounts.sessionPosts !== beforeBack.sessionPosts) {
    return {
      status: "FAIL",
      evidence: "switching back issued " + (backCounts.gradePuts - beforeBack.gradePuts) + " PUT /api/grade and "
        + (backCounts.sessionPosts - beforeBack.sessionPosts) + " POST /api/session, it must issue neither",
    };
  }
  const backOnA = await readConfig();
  if (!!backOnA.curves.enabled !== v2) {
    return { status: "FAIL", evidence: "switching back showed curves.enabled " + backOnA.curves.enabled + ", expected the committed " + v2 };
  }
  notes.push("switching back: " + A + "'s own grade returned with 0 writes");

  // ---- 7. the history came back with the clip, because it is the project's
  const undoDisabled = await page.$eval("#undoBtn", (el) => el.disabled);
  if (undoDisabled) {
    return { status: "FAIL", evidence: "after switching back to " + A + " the undo button is disabled, so its history did not come with it" };
  }
  await page.click("#undoBtn");
  const undoStart = Date.now();
  let afterUndo = await readConfig();
  while (!!afterUndo.curves.enabled !== v1 && Date.now() - undoStart < 8000) {
    await sleep(150);
    afterUndo = await readConfig();
  }
  if (!!afterUndo.curves.enabled !== v1) {
    return { status: "FAIL", evidence: "undo after switching back gave curves.enabled " + afterUndo.curves.enabled + ", expected " + v1 };
  }
  notes.push("undo after switching back undid this clip's own change (" + v2 + " to " + v1 + ")");

  // ---- 8. an outside edit lands once and is not sent straight back --------
  // The failure this catches is a loop: the long poll hands the tab a config,
  // the tab applies it, applying it looks like a local change, the tab
  // publishes it, and the two sides pass the same config back and forth. The
  // server would dedupe the commits, so the history would look innocent while
  // every keystroke raced a round trip. Posting without a `by` field is what
  // the CLI does, and it is what makes the server sign this "cli" and the tab
  // treat it as somebody else's edit rather than its own echo.
  const beforeOutside = await counts();
  const outsideCfg = JSON.parse(JSON.stringify(afterUndo));
  outsideCfg.convert.exposure = 0.37;
  await fetch(base + "/api/session", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ config: outsideCfg, clip: A, replace: true }),
  });
  const outsideStart = Date.now();
  let landed = await readConfig();
  while (landed.convert.exposure !== 0.37 && Date.now() - outsideStart < 10000) {
    await sleep(200);
    landed = await readConfig();
  }
  if (landed.convert.exposure !== 0.37) {
    return { status: "FAIL", evidence: "an outside POST /api/session never reached the page (exposure reads " + landed.convert.exposure + ")" };
  }
  await sleep(1500);                       // long enough for a bounce to show
  const afterOutside = await counts();
  if (afterOutside.sessionPosts !== beforeOutside.sessionPosts) {
    return {
      status: "FAIL",
      evidence: "applying an outside edit made the page publish " + (afterOutside.sessionPosts - beforeOutside.sessionPosts)
        + " POST /api/session of its own, which is the echo loop this guards",
    };
  }
  notes.push("an outside edit (exposure 0.37, author cli) reached the page and was not republished");

  // Put the clip back the way step 7 left it, again from the outside, so the
  // specs after this one see the picture they expect.
  await fetch(base + "/api/session", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ config: afterUndo, clip: A, replace: true }),
  });
  const backStart = Date.now();
  let restored = await readConfig();
  while (restored.convert.exposure !== afterUndo.convert.exposure && Date.now() - backStart < 10000) {
    await sleep(200);
    restored = await readConfig();
  }

  // ---- 9. the copy picker still lists the other graded clip ---------------
  // B has no row in the old `grades` table (a commit does not write one), so
  // this is also the check that the picker learned to look at the projects.
  await page.evaluate(() => window.StudioGrades.refreshCopyList());
  await sleep(500);
  const options = await page.evaluate(() =>
    Array.from(document.querySelectorAll("#gradeCopyFrom option")).map((o) => o.textContent));
  if (options.indexOf(B) < 0) {
    return { status: "FAIL", evidence: "the copy-grade picker does not list " + B + ", it lists " + JSON.stringify(options) };
  }
  notes.push("copy-grade picker lists the other graded clip");

  return { status: "PASS", evidence: notes.join("; ") };
}
