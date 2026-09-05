/* reload-state (contract C4): a reload brings the whole application back,
 * and costs nothing.
 *
 * The founder's words the contract came from: "if i reload it shouldnt break
 * or reset the whole application" and "the whole 'project' is based on the
 * video sha u open ... when u open a video in it a proj automatically appears
 * the state u had it in".
 *
 * So this spec sets up a real, distinctive state through the real UI (a
 * committed grade change, the playhead moved off zero with a real click on
 * the scrub bar, and rotation 90 chosen in the rotation control), reloads,
 * and asserts every one of those came back. Then the two things that make it
 * a project rather than a lucky cache:
 *
 *   - the reload must create NO commit and NO session revision. Both are
 *     read straight off the server (GET /api/project/log, GET /api/session),
 *     not inferred. This is the assertion that would catch a boot that
 *     republishes the config, which is exactly what the old boot did and
 *     what could land on top of an agent's edit.
 *   - switching to another clip and back must restore the same state, again
 *     without a commit: the project belongs to the clip's content, not to
 *     this page's memory.
 *
 * Finally a second tab is opened on the same server and has to show the same
 * project, because the project lives on the server and not in a tab.
 */

const CURVES_CHK = 'section.stage[data-stage="curves"] input.ctl-switch';

function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

function fail(evidence) { return { status: "FAIL", evidence: evidence }; }

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

  const getProject = () => fetch(base + "/api/project").then((r) => r.json());
  const getLog = () => fetch(base + "/api/project/log").then((r) => r.json());
  const getSession = () => fetch(base + "/api/session").then((r) => r.json());

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

  // What the PAGE believes, read only from what is on screen: the clip name
  // in the Source block, the time label under the viewer, and which button of
  // the rotation control is the chosen one.
  const readScreen = () =>
    page.evaluate(() => {
      const first = document.querySelector("#clipInfo div span");
      const on = document.querySelector("#rotSeg button.active");
      return {
        clip: first ? first.textContent : null,
        time: (document.getElementById("timeLabel") || {}).textContent || null,
        rotation: on ? on.getAttribute("data-rotation") : null,
      };
    });

  // The rotation control and the clip list live in the left rail's Clips
  // pane, which is only one of four panes and is not always the one showing.
  // Puppeteer refuses to click an element with no box, so every real click in
  // here goes through this first, exactly as a person would have to.
  async function showClipsPane() {
    await page.click('.railtab[data-rail="browse"]');
    // Contract E2 gave that pane a root switcher; #browseClips below is the
    // This Mac view's own clip list, so this spec says which view it is using
    // rather than relying on whichever one happens to be remembered.
    await page.click('#filesRoots .filesroot[data-root="thismac"]');
    await page.waitForFunction(() => {
      const el = document.querySelector('#rotSeg button[data-rotation="90"]');
      if (!el) return false;
      const r = el.getBoundingClientRect();
      return r.width > 1 && r.height > 1;
    }, { timeout: 15000 });
  }

  async function setRotationUI(value) {
    await showClipsPane();
    const sel = '#rotSeg button[data-rotation="' + value + '"]';
    const btn = await page.$(sel);
    if (!btn) throw new Error("no rotation button for " + value + " (" + sel + ")");
    await btn.click();
    // The control is the only writer of the project's rotation, so waiting on
    // the server's own answer is the honest wait rather than a sleep.
    for (let i = 0; i < 60; i++) {
      const p = await getProject();
      if (p.rotation === value) return p;
      await sleep(100);
    }
    throw new Error("the project's rotation never became " + value);
  }

  // A real click on the scrub bar's track, which is a native <input
  // type="range">, so this is a genuine user input event and not a value
  // assignment. The exact landing time does not matter, only that it is not
  // zero and that it comes back.
  async function scrubTo(fraction) {
    const box = await page.$eval("#scrub", (el) => {
      const r = el.getBoundingClientRect();
      return { x: r.left, y: r.top + r.height / 2, w: r.width };
    });
    await page.mouse.click(box.x + box.w * fraction, box.y);
    await sleep(900);            // past the 400 ms playhead debounce
  }

  async function waitForClip(name, ceilingMs) {
    const start = Date.now();
    while (Date.now() - start < (ceilingMs || 15000)) {
      const s = await readScreen();
      if (s.clip === name) return s;
      await sleep(100);
    }
    return await readScreen();
  }

  async function clickClipRow(needle) {
    const rows = await page.$$("#browseClips .lutrow");
    for (const row of rows) {
      const label = await row.evaluate((el) => (el.querySelector("span") || {}).textContent || "");
      if (label === needle) { await row.click(); return true; }
    }
    return false;
  }

  const notes = [];
  let second = null;
  try {
    // ---- a known, distinctive state, all of it through the real UI --------
    await page.goto(base + "/", { waitUntil: "domcontentloaded", timeout: 30000 });
    await ctx.waitForBootComplete(20000);
    await sleep(800);

    const opened = await getProject();
    if (!opened.open) return fail("no project is open after a plain boot, so there is nothing to restore");
    const clipA = opened.name;

    // 1. grade it: one committed change through a real click.
    const before = await readConfig();
    const v0 = !!(before.curves && before.curves.enabled);
    await page.click(CURVES_CHK);
    await sleep(700);
    const graded = await readConfig();
    if (!!graded.curves.enabled === v0) {
      return fail("the click did not change curves.enabled (still " + v0 + "), so there is no grade to restore");
    }

    // 2. move the playhead, 3. choose a rotation.
    await scrubTo(0.4);
    await setRotationUI("90");
    await sleep(600);

    const wantScreen = await readScreen();
    const wantCfg = await readConfig();
    const wantProj = await getProject();
    const wantLog = await getLog();
    const wantRev = (await getSession()).rev;
    if (!(wantProj.time > 0.01)) {
      return fail("the scrub click left the project's playhead at " + wantProj.time + "s, so a restore of it proves nothing");
    }
    if (wantProj.rotation !== "90") return fail("rotation did not reach 90 on the project, it reads " + wantProj.rotation);
    notes.push("set up: clip " + clipA + ", playhead " + wantProj.time.toFixed(2) + "s, rotation 90, HEAD "
      + wantProj.head + ", " + wantLog.commits.length + " commit(s), session rev " + wantRev);

    // The rotation control is the only writer, and the History panel's header
    // is a second reader of the same value. Two places showing one number is
    // exactly where they drift, so this checks the header caught up rather
    // than trusting that it did.
    await page.click('.paramtab[data-paramtab="history"]');
    let histRot = "";
    for (let i = 0; i < 60; i++) {
      histRot = await page.evaluate(() =>
        (document.getElementById("histRotation") || {}).textContent || "");
      if (/90/.test(histRot)) break;
      await sleep(100);
    }
    if (!/90/.test(histRot)) {
      return fail("the History header reads " + JSON.stringify(histRot) + " after the rotation went to 90");
    }
    await page.click('.paramtab[data-paramtab="grade"]');
    notes.push("the History header followed the rotation control: " + JSON.stringify(histRot));

    // ---- reload once ------------------------------------------------------
    await page.reload({ waitUntil: "domcontentloaded", timeout: 30000 });
    await ctx.waitForBootComplete(20000);
    await waitForClip(clipA, 15000);
    await sleep(900);

    const gotScreen = await readScreen();
    const gotCfg = await readConfig();
    const gotProj = await getProject();
    const gotLog = await getLog();
    const gotRev = (await getSession()).rev;

    if (gotScreen.clip !== wantScreen.clip) {
      return fail("after a reload the viewer shows " + gotScreen.clip + ", expected " + wantScreen.clip);
    }
    if (gotScreen.rotation !== "90") {
      return fail("after a reload the rotation control shows " + gotScreen.rotation + ", expected 90");
    }
    if (gotScreen.time !== wantScreen.time) {
      return fail("after a reload the playhead reads " + gotScreen.time + ", expected " + wantScreen.time);
    }
    if (JSON.stringify(gotCfg) !== JSON.stringify(wantCfg)) {
      return fail("after a reload the live config is not the one that was on screen: curves.enabled "
        + gotCfg.curves.enabled + " vs " + wantCfg.curves.enabled);
    }
    if (gotProj.head !== wantProj.head) {
      return fail("after a reload HEAD is " + gotProj.head + ", expected " + wantProj.head);
    }
    if (gotLog.commits.length !== wantLog.commits.length) {
      return fail("the reload created " + (gotLog.commits.length - wantLog.commits.length)
        + " commit(s): the log went from " + wantLog.commits.length + " to " + gotLog.commits.length);
    }
    if (gotRev !== wantRev) {
      return fail("the reload bumped the session revision from " + wantRev + " to " + gotRev
        + ", so it published something");
    }
    notes.push("reload 1: clip, playhead " + gotScreen.time + ", rotation 90, config and HEAD " + gotProj.head
      + " all came back; log still " + gotLog.commits.length + " commit(s), session rev still " + gotRev);

    // ---- reload a second time, same assertions ---------------------------
    await page.reload({ waitUntil: "domcontentloaded", timeout: 30000 });
    await ctx.waitForBootComplete(20000);
    await waitForClip(clipA, 15000);
    await sleep(900);
    const twice = await readScreen();
    const twiceLog = await getLog();
    const twiceRev = (await getSession()).rev;
    if (twice.clip !== wantScreen.clip || twice.rotation !== "90" || twice.time !== wantScreen.time) {
      return fail("a second reload landed on " + JSON.stringify(twice) + ", expected " + JSON.stringify(wantScreen));
    }
    if (twiceLog.commits.length !== wantLog.commits.length || twiceRev !== wantRev) {
      return fail("the second reload cost " + (twiceLog.commits.length - wantLog.commits.length)
        + " commit(s) and " + (twiceRev - wantRev) + " revision(s)");
    }
    notes.push("reload 2: identical, still " + twiceLog.commits.length + " commit(s) and rev " + twiceRev);

    // ---- switch clips and come back --------------------------------------
    const clips = await fetch(base + "/api/clips").then((r) => r.json()).then((j) => j.clips || []);
    const other = clips.map((c) => c.name).filter((n) => n !== clipA)[0];
    if (!other) {
      notes.push("only one clip in footage, the clip switch half of this spec had nothing to switch to");
    } else {
      await page.click('.railtab[data-rail="browse"]');
      await page.click('#filesRoots .filesroot[data-root="thismac"]');
      await page.waitForFunction(() => document.querySelectorAll("#browseClips .lutrow").length >= 2,
        { timeout: 15000 });
      if (!(await clickClipRow(other))) return fail("no row for " + other + " in #browseClips");
      await waitForClip(other, 20000);
      await sleep(900);
      const onOther = await getProject();
      if (onOther.name !== other) return fail("clicking " + other + " left the open project on " + onOther.name);

      if (!(await clickClipRow(clipA))) return fail("no row for " + clipA + " in #browseClips");
      await waitForClip(clipA, 20000);
      await sleep(900);
      const backScreen = await readScreen();
      const backCfg = await readConfig();
      const backProj = await getProject();
      const backLog = await getLog();
      if (backScreen.rotation !== "90") {
        return fail("coming back to " + clipA + " showed rotation " + backScreen.rotation + ", expected 90");
      }
      if (backScreen.time !== wantScreen.time) {
        return fail("coming back to " + clipA + " showed the playhead at " + backScreen.time
          + ", expected " + wantScreen.time);
      }
      if (JSON.stringify(backCfg) !== JSON.stringify(wantCfg)) {
        return fail("coming back to " + clipA + " did not restore its config (curves.enabled "
          + backCfg.curves.enabled + " vs " + wantCfg.curves.enabled + ")");
      }
      if (backProj.head !== wantProj.head) {
        return fail("coming back to " + clipA + " left HEAD at " + backProj.head + ", expected " + wantProj.head);
      }
      if (backLog.commits.length !== wantLog.commits.length) {
        return fail("leaving " + clipA + " and coming back cost "
          + (backLog.commits.length - wantLog.commits.length) + " commit(s)");
      }
      notes.push("clip switch to " + other + " and back: playhead, rotation, config and HEAD "
        + backProj.head + " all restored, still " + backLog.commits.length + " commit(s)");
    }

    // ---- a second tab sees the same project ------------------------------
    second = await ctx.browser.newPage();
    await second.setViewport(ctx.defaultViewport);
    await second.goto(base + "/", { waitUntil: "domcontentloaded", timeout: 30000 });
    await second.waitForFunction(() => {
      const h = document.getElementById("params");
      return !!(h && h.querySelector("section.stage"));
    }, { timeout: 20000 });
    await sleep(1200);
    const twoScreen = await second.evaluate(() => {
      const first = document.querySelector("#clipInfo div span");
      const on = document.querySelector("#rotSeg button.active");
      return {
        clip: first ? first.textContent : null,
        time: (document.getElementById("timeLabel") || {}).textContent || null,
        rotation: on ? on.getAttribute("data-rotation") : null,
      };
    });
    if (twoScreen.clip !== clipA || twoScreen.rotation !== "90" || twoScreen.time !== wantScreen.time) {
      return fail("a second tab on the same server shows " + JSON.stringify(twoScreen)
        + ", expected the same project as the first: " + JSON.stringify(wantScreen));
    }
    const afterTwo = await getLog();
    if (afterTwo.commits.length !== wantLog.commits.length) {
      return fail("opening a second tab cost " + (afterTwo.commits.length - wantLog.commits.length) + " commit(s)");
    }
    notes.push("a second tab opened on the same project: same clip, same playhead, same rotation, 0 new commits");

    return { status: "PASS", evidence: notes.join("; ") };
  } finally {
    if (second) { try { await second.close(); } catch (e) { /* best effort */ } }
    // Put the rotation back so the specs after this one measure the clip the
    // way every other spec expects it, and leave a freshly booted page.
    try {
      await page.bringToFront();
      await setRotationUI("auto");
      await page.goto(base + "/", { waitUntil: "domcontentloaded", timeout: 30000 });
      await ctx.waitForBootComplete(20000);
      await sleep(400);
    } catch (err) { /* the result above is what matters, not the tidy up */ }
  }
}
