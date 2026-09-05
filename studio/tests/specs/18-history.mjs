/* History panel (contract C3, plan/2026-09-05-studio-projects/PLAN.md): the
 * git style tree in the right sidebar's History tab, studio/static/history.js
 * plus the tab strip in sidebars.js/style.css/index.html.
 *
 * Everything here drives the real UI in the real browser against the real
 * project store (studio/projects.py through studio/server.py's C1 routes):
 * a real click on the tab strip, a real drag on a slider (the same
 * mousedown/mousemove/mouseup path spec 09 and 16 already prove arrives),
 * real clicks on Undo/Redo/Go here/Fork from here, and a real prompt()
 * dialog for the explicit fork. The live config is read the same "no
 * private JS global" way every other spec reads it, through
 * #jsonBtn/#jsonText.
 *
 * The claims, in order:
 *   1. Opening the History tab flips <html data-paramtab> and shows at
 *      least the project's root row (oldest, at the bottom of the list).
 *   2. Moving a slider on the Grade tab, then switching back to History,
 *      shows one more row on top: author "studio", a readable message (not
 *      "root", not empty).
 *   3. Undo moves HEAD back to the pre edit commit and the slider's live
 *      value reverts.
 *   4. Redo moves HEAD forward again.
 *   5. "Go here" on the root row checks out the root commit.
 *   6. Editing again from that non tip HEAD auto forks (projects.py commit()
 *      rule 2): a new branch appears in the branch select and the graph
 *      grows a fork edge.
 *   7. "Fork from here" on the new HEAD, answering its prompt() with a name,
 *      adds that named branch to the select.
 *   8. A reload keeps the History tab open (same pre paint attribute
 *      sidebars.js already proves for the sidebars) and shows the same
 *      number of rows, since nothing changed on the way.
 *
 * Screenshots of the panel in both themes are evidence for a human, not
 * part of the pass/fail contract: a screenshot write failure must not turn
 * a real PASS into a FAIL, the same discipline spec 07 already uses for its
 * own w2f-stats-pinned.png.
 */

const EXPOSURE_SLIDER = 'section.stage[data-stage="convert"] .ctl-slider .ctl-value';

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
      evidence: "input-probe found " + missing.join(" and ") + " never reach the page, this spec needs a real drag on a slider",
    };
  }

  async function switchTab(tab) {
    const clicked = await page.evaluate((t) => {
      var btns = document.querySelectorAll(".paramtab");
      for (var i = 0; i < btns.length; i++) {
        if (btns[i].getAttribute("data-paramtab") === t) { btns[i].click(); return true; }
      }
      return false;
    }, tab);
    if (!clicked) throw new Error('no .paramtab[data-paramtab="' + tab + '"] found');
    await sleep(250);
  }

  async function readConfig() {
    await page.click("#jsonBtn");
    await sleep(120);
    const text = await page.$eval("#jsonText", (el) => el.value);
    await page.evaluate(() => {
      var o = document.getElementById("jsonOverlay");
      if (o) o.classList.remove("on");
    });
    return JSON.parse(text);
  }

  async function historyRows() {
    return page.evaluate(() => {
      var rows = document.querySelectorAll("#historyRows .historyrow");
      return Array.prototype.map.call(rows, function (r) {
        // The GitKraken style redesign swapped the full name chip for a
        // compact avatar circle (initials, or a bot glyph for an agent);
        // the full author string moved to its title attribute, still one
        // read away, same as it was one hover away for a human before.
        return {
          id: r.querySelector(".hid") ? r.querySelector(".hid").textContent : "",
          author: r.querySelector(".havatar") ? r.querySelector(".havatar").title : "",
          msg: r.querySelector(".hmsg") ? r.querySelector(".hmsg").textContent : "",
          head: r.classList.contains("head"),
        };
      });
    });
  }

  // How many characters of a row's message are actually ON SCREEN, not just
  // present in the DOM (.hmsg's textContent is always the full string; only
  // CSS clips it). Walks a Range over the message's own text node one
  // character at a time and stops at the first offset whose bounding box
  // crosses the visible box's own right edge, so this is the real rendered
  // width doing the measuring, not a guess from font metrics.
  async function visibleMsgChars(rowIndex) {
    return page.evaluate((idx) => {
      var rows = document.querySelectorAll("#historyRows .historyrow");
      var row = rows[idx];
      var msgEl = row && row.querySelector(".hmsg");
      var textNode = msgEl && msgEl.firstChild;
      if (!textNode || textNode.nodeType !== 3) return { visible: 0, boxWidth: msgEl ? msgEl.getBoundingClientRect().width : 0 };
      var boxRight = msgEl.getBoundingClientRect().right;
      var range = document.createRange();
      var visible = 0;
      for (var i = 1; i <= textNode.length; i++) {
        range.setStart(textNode, 0);
        range.setEnd(textNode, i);
        if (range.getBoundingClientRect().right > boxRight + 0.5) break;
        visible = i;
      }
      return { visible: visible, boxWidth: msgEl.getBoundingClientRect().width };
    }, rowIndex);
  }

  async function branchOptions() {
    return page.evaluate(() => {
      var sel = document.getElementById("histBranch");
      return sel ? Array.prototype.map.call(sel.options, function (o) { return o.textContent; }) : [];
    });
  }

  async function head() {
    return page.evaluate(() => (document.getElementById("histHead") || {}).textContent || "");
  }

  async function dragSlider(dx) {
    const handle = await page.$(EXPOSURE_SLIDER);
    if (!handle) throw new Error("no " + EXPOSURE_SLIDER + " found in #params");
    const box = await handle.boundingBox();
    if (!box) throw new Error(EXPOSURE_SLIDER + " has no bounding box (the Grade pane must be the active tab to drag it)");
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.mouse.down();
    await page.mouse.move(box.x + box.width / 2 + dx, box.y + box.height / 2, { steps: 8 });
    await page.mouse.up();
    await sleep(400);
  }

  function armDialog(answer) {
    const seen = [];
    const handler = async (dialog) => {
      seen.push(dialog.message());
      try { await dialog.accept(answer); } catch (e) { try { await dialog.dismiss(); } catch (e2) { /* gone */ } }
    };
    page.on("dialog", handler);
    return { off: () => page.off("dialog", handler), seen };
  }

  async function saveThemeShot(name, shotSrc) {
    try {
      const path = (await import("node:path")).default;
      const { fileURLToPath } = await import("node:url");
      const { mkdirSync } = await import("node:fs");
      const here = path.dirname(fileURLToPath(import.meta.url));
      const shotsDir = path.resolve(here, "..", "..", "shots"); // specs -> tests -> studio -> shots
      mkdirSync(shotsDir, { recursive: true });
      await (shotSrc || page).screenshot({ path: path.join(shotsDir, name) });
    } catch (err) {
      // Evidence for a human, not part of the pass/fail contract.
    }
  }

  const notes = [];

  await page.goto(base + "/", { waitUntil: "domcontentloaded", timeout: 30000 });
  await ctx.waitForBootComplete(20000);
  await sleep(500);

  // --- 1. open History, assert the root row -------------------------------
  await switchTab("history");
  const tabAttr = await page.evaluate(() => document.documentElement.getAttribute("data-paramtab"));
  if (tabAttr !== "history") return fail('clicking the History tab left data-paramtab at "' + tabAttr + '"');

  let rows = await historyRows();
  if (!rows.length) return fail("no rows in #historyRows after opening the History tab");
  const rootRow = rows[rows.length - 1];
  if (!/root/i.test(rootRow.msg)) {
    return fail('the oldest row (bottom of the list) reads "' + rootRow.msg + '", expected a root commit ("root" or "root: migrated saved grade")');
  }
  notes.push(rows.length + " row(s) on open, oldest is the root commit (" + rootRow.id + " \"" + rootRow.msg + "\")");
  const rowsBeforeEdit = rows.length;

  // --- screenshots, both themes, evidence only --------------------------------
  // Measured directly, not assumed: this headless Chrome can hand back a
  // screenshot PNG that still shows the OLD theme's colours even though
  // getComputedStyle, read at the exact moment of the screenshot call, already
  // reports the new theme (confirmed with data-theme, #sidebarRight's computed
  // background and a fresh page.evaluate() read all agreeing the DOM is
  // correct while the saved PNG was not). Reproduces on the plain Grade tab
  // with no History code involved, on a brand new tab, on a brand new browser
  // process, and even on the very first navigation of a fresh page pre-seeded
  // (via localStorage, before any script ran) straight into the target theme,
  // so this is a real capture-side quirk of this machine's headless Chrome
  // (GPU preview here runs on ANGLE's Metal backend), not this panel's CSS or
  // this spec's sequencing. An extra throwaway page.screenshot() plus a short
  // wait sometimes recovers it and never hurts, so it stays below, but it is
  // not a proven fix: treat these two PNGs as best-effort evidence for a
  // human, same as spec 07's own screenshots, and trust spec 10's PASS
  // (getComputedStyle-based, no screenshot involved) for the actual claim
  // that the theme toggle itself works.
  const startingTheme = (await page.evaluate(() => document.documentElement.getAttribute("data-theme"))) || "dark";
  await saveThemeShot("18-history-" + startingTheme + ".png");
  await page.click("#themeToggle");
  await sleep(500);
  await page.screenshot().catch(() => {}); // throwaway, see above: sometimes helps, never hurts
  await sleep(300);
  const otherTheme = startingTheme === "light" ? "dark" : "light";
  await saveThemeShot("18-history-" + otherTheme + ".png");
  await page.click("#themeToggle"); // leave the page in the theme it was found in
  await sleep(200);

  // --- 2. move a slider, assert a new "studio" row appears ----------------
  await switchTab("grade");
  const beforeEdit = await readConfig();
  const exposureBefore = beforeEdit.convert.exposure;
  await dragSlider(50);
  const afterEdit = await readConfig();
  if (afterEdit.convert.exposure === exposureBefore) {
    return fail("dragging " + EXPOSURE_SLIDER + " left convert.exposure at " + exposureBefore + ", the drag did not register");
  }

  await switchTab("history");
  rows = await historyRows();
  if (rows.length !== rowsBeforeEdit + 1) {
    return fail("after the slider edit, #historyRows has " + rows.length + " row(s), expected " + (rowsBeforeEdit + 1));
  }
  const editRow = rows[0];
  if (!editRow.head) return fail("the new top row is not marked .head after a live edit");
  if (editRow.author !== "studio") return fail('the new row\'s author chip reads "' + editRow.author + '", expected "studio" (this tab\'s own signature)');
  if (!editRow.msg || /^root/i.test(editRow.msg) || editRow.msg === "no change") {
    return fail('the new row has an unreadable message: "' + editRow.msg + '"');
  }
  if (!/exposure/i.test(editRow.msg)) {
    return fail('dragging the exposure slider produced the message "' + editRow.msg + '", expected it to name "exposure" (the founder\'s own ask: a row should read like "hsl slider moved", not a generic label)');
  }
  // Not just the DOM: the log route itself, so this checks the actual data
  // the row is drawn from, not only what ended up on screen.
  const logAfterEdit = await page.evaluate(() => fetch("/api/project/log?limit=2000").then((r) => r.json()));
  const topCommitMsg = (logAfterEdit.commits && logAfterEdit.commits[0] && logAfterEdit.commits[0].message) || "";
  if (!/exposure/i.test(topCommitMsg)) {
    return fail('GET /api/project/log\'s own top commit message reads "' + topCommitMsg + '", expected it to name "exposure" too, not just the row shown on screen');
  }
  notes.push('slider edit added a "studio" row on top: "' + editRow.msg + '" (GET /api/project/log agrees: "' + topCommitMsg + '")');

  // At the sidebar's default width, the message must not be crushed down to
  // a couple of letters ("lay...") the way the id/time/branch pill used to
  // starve it: GitKraken drops its OWN secondary columns first, so the
  // message keeps a real, readable amount of text on screen even at 300px.
  const editRowVisible = await visibleMsgChars(0);
  if (editRowVisible.visible < 12) {
    return fail("at the sidebar's default width, the exposure row's message shows only " + editRowVisible.visible + " visible character(s) (box " + editRowVisible.boxWidth.toFixed(1) + "px wide), expected at least 12");
  }
  notes.push("at the default sidebar width, the exposure row's message shows " + editRowVisible.visible + " visible character(s) (box " + editRowVisible.boxWidth.toFixed(1) + "px wide)");
  const headAfterEdit = await head();

  // --- 3. Undo: HEAD moves back, slider value reverts ---------------------
  const undoDisabled = await page.$eval("#histUndoBtn", (el) => el.disabled);
  if (undoDisabled) return fail("#histUndoBtn is disabled right after a real edit, expected enabled");
  await page.click("#histUndoBtn");
  await sleep(500);
  const headAfterUndo = await head();
  if (headAfterUndo === headAfterEdit || !headAfterUndo) {
    return fail("Undo left HEAD at " + JSON.stringify(headAfterUndo) + ", expected it to move back from " + headAfterEdit);
  }
  await switchTab("grade");
  const afterUndoCfg = await readConfig();
  if (afterUndoCfg.convert.exposure !== exposureBefore) {
    return fail("after Undo, convert.exposure reads " + afterUndoCfg.convert.exposure + ", expected it back to " + exposureBefore);
  }
  notes.push("Undo moved HEAD " + headAfterEdit + " to " + headAfterUndo + " and reverted convert.exposure to " + exposureBefore);

  // --- 4. Redo: HEAD moves forward again -----------------------------------
  await switchTab("history");
  const redoDisabled = await page.$eval("#histRedoBtn", (el) => el.disabled);
  if (redoDisabled) return fail("#histRedoBtn is disabled right after an Undo, expected enabled");
  await page.click("#histRedoBtn");
  await sleep(500);
  const headAfterRedo = await head();
  if (headAfterRedo !== headAfterEdit) {
    return fail("Redo left HEAD at " + headAfterRedo + ", expected it back to " + headAfterEdit + " (the edit commit)");
  }
  notes.push("Redo moved HEAD back to " + headAfterRedo);

  // --- 5. Go here on the root row -------------------------------------------
  rows = await historyRows();
  const rootId = rows[rows.length - 1].id;
  const rowHandles = await page.$$("#historyRows .historyrow");
  const rootHandle = rowHandles[rowHandles.length - 1];
  await rootHandle.hover();
  const rootButtons = await rootHandle.$$("button");
  if (rootButtons.length < 2) return fail("the root row has only " + rootButtons.length + " action button(s), expected Go here and Fork from here");
  const rootGotoLabel = await page.evaluate((el) => el.textContent, rootButtons[0]);
  if (!/go here/i.test(rootGotoLabel)) return fail('the root row\'s first action button reads "' + rootGotoLabel + '", expected "Go here"');
  await rootButtons[0].click();
  await sleep(500);
  const headAtRoot = await head();
  if (headAtRoot !== rootId) {
    return fail('"Go here" on the root row left HEAD at "' + headAtRoot + '", expected the root id "' + rootId + '"');
  }
  notes.push('"Go here" on the root row moved HEAD to ' + headAtRoot);

  // --- 6. editing from a non tip HEAD auto forks ----------------------------
  const branchesBeforeFork = await branchOptions();
  await switchTab("grade");
  await dragSlider(-60);
  await switchTab("history");
  const branchesAfterFork = await branchOptions();
  if (branchesAfterFork.length <= branchesBeforeFork.length) {
    return fail("editing from a non tip commit did not grow the branch select: before=" + JSON.stringify(branchesBeforeFork) + " after=" + JSON.stringify(branchesAfterFork));
  }
  rows = await historyRows();
  const forkedRow = rows[0];
  if (!forkedRow.head) return fail("after the auto fork edit, the new top row is not marked .head");
  const graphChildren = await page.evaluate(() => document.getElementById("historyGraph").children.length);
  if (graphChildren < 3) {
    return fail("the graph only has " + graphChildren + " SVG child element(s) after a fork, expected at least a dot for each visible commit plus a fork edge");
  }
  notes.push("editing from root auto forked: branch select grew from " + branchesBeforeFork.length + " to " + branchesAfterFork.length + " option(s), graph has " + graphChildren + " element(s)");

  // --- 6b. GitKraken style graph: lane colours, curved fork edges, HEAD ring -
  const graphInfo = await page.evaluate(() => {
    var svg = document.getElementById("historyGraph");
    var forkEdges = Array.prototype.slice.call(svg.querySelectorAll(".hedge.fork"));
    var straightEdges = Array.prototype.slice.call(svg.querySelectorAll("line.hedge"));
    function stroke(el) { return getComputedStyle(el).stroke; }
    return {
      forkTags: forkEdges.map(function (e) { return e.tagName.toLowerCase(); }),
      forkStrokes: forkEdges.map(stroke),
      straightStrokes: straightEdges.map(stroke),
      ringCount: svg.querySelectorAll(".hnode-ring").length,
      headCenterCount: svg.querySelectorAll(".hnode-head-center").length,
    };
  });
  if (!graphInfo.forkTags.length) {
    return fail("no .hedge.fork element in the graph after an auto fork, expected at least one (the parent's lane leaving on a curve into the child's lane)");
  }
  const notPath = graphInfo.forkTags.find((t) => t !== "path");
  if (notPath) {
    return fail("a .hedge.fork element is a <" + notPath + ">, expected every fork edge to be a <path> (a bezier curve), not a straight <line>");
  }
  // Two branches, two lane colours: the fork edge's own stroke (the new
  // branch's lane) has to actually differ from an existing straight edge's
  // stroke (an older lane, e.g. main) -- read as real computed SVG paint,
  // not by trusting a class name alone.
  const distinctStrokes = new Set(graphInfo.forkStrokes.concat(graphInfo.straightStrokes));
  if (distinctStrokes.size < 2) {
    return fail("fork edge stroke(s) " + JSON.stringify(graphInfo.forkStrokes) + " and straight edge stroke(s) " + JSON.stringify(graphInfo.straightStrokes) + " are not distinguishable, expected two different branches to render in two different lane colours");
  }
  if (graphInfo.ringCount < 1 || graphInfo.headCenterCount < 1) {
    return fail("expected HEAD to render as a ring (.hnode-ring) with a filled centre (.hnode-head-center), found ring=" + graphInfo.ringCount + " centre=" + graphInfo.headCenterCount);
  }
  notes.push("graph has " + distinctStrokes.size + " distinct lane stroke colour(s) across a straight and a fork edge, the fork edge is a <path> curve, HEAD renders as a ring plus a filled centre");

  // --- 6c. hovering a row highlights its ancestry, dims the rest -------------
  const hoverRowHandle = (await page.$$("#historyRows .historyrow"))[0];
  await hoverRowHandle.hover();
  await sleep(150);
  const hoverInfo = await page.evaluate(() => {
    var svg = document.getElementById("historyGraph");
    return { lit: svg.querySelectorAll(".hgraph-lit").length, dim: svg.querySelectorAll(".hgraph-dim").length };
  });
  if (!hoverInfo.lit) return fail("hovering the top history row applied no .hgraph-lit element in the graph, expected its ancestry path to light up (GitKraken's own lineage highlight)");
  if (!hoverInfo.dim) return fail("hovering the top history row applied no .hgraph-dim element in the graph, expected the rest of the tree to dim");
  await page.mouse.move(0, 0);
  await sleep(150);
  const afterLeave = await page.evaluate(() => document.getElementById("historyGraph").querySelectorAll(".hgraph-lit, .hgraph-dim").length);
  if (afterLeave !== 0) {
    return fail("moving the mouse off the row left " + afterLeave + " .hgraph-lit/.hgraph-dim element(s) behind, expected the highlight to clear on mouseleave");
  }
  notes.push("hovering the top row lit " + hoverInfo.lit + " ancestry element(s) and dimmed " + hoverInfo.dim + ", both cleared on mouseleave");

  const forkHeadId = await head();

  // --- 7. explicit Fork from here, answering the prompt() -------------------
  rowHandles.length = 0;
  const rowHandles2 = await page.$$("#historyRows .historyrow");
  const headHandle = rowHandles2[0];
  await headHandle.hover();
  const headButtons = await headHandle.$$("button");
  if (headButtons.length < 2) return fail("the HEAD row has only " + headButtons.length + " action button(s), expected Go here and Fork from here");
  const forkLabel = await page.evaluate((el) => el.textContent, headButtons[1]);
  if (!/fork from here/i.test(forkLabel)) return fail('the HEAD row\'s second action button reads "' + forkLabel + '", expected "Fork from here"');

  const branchName = "spec18-fork";
  const armed = armDialog(branchName);
  await headButtons[1].click();
  await sleep(600);
  armed.off();
  if (!armed.seen.length) return fail('clicking "Fork from here" opened no prompt() dialog');

  const branchesAfterExplicitFork = await branchOptions();
  const named = branchesAfterExplicitFork.some((b) => b.indexOf(branchName + " (") === 0);
  if (!named) {
    return fail('after "Fork from here" answered with "' + branchName + '", the branch select does not list it: ' + JSON.stringify(branchesAfterExplicitFork));
  }
  notes.push('"Fork from here" on HEAD (' + forkHeadId + '), named via its prompt(), added "' + branchName + '" to the branch select: ' + JSON.stringify(branchesAfterExplicitFork));

  const rowsBeforeReload = (await historyRows()).length;

  // --- 8. reload: tab and rows survive ---------------------------------------
  await page.goto(base + "/", { waitUntil: "domcontentloaded", timeout: 30000 });
  await ctx.waitForBootComplete(20000);
  await sleep(600);
  const tabAfterReload = await page.evaluate(() => document.documentElement.getAttribute("data-paramtab"));
  if (tabAfterReload !== "history") {
    return fail("after a reload, data-paramtab reads " + JSON.stringify(tabAfterReload) + ", expected it to stay on history like the sidebars do");
  }
  const rowsAfterReload = await historyRows();
  if (rowsAfterReload.length !== rowsBeforeReload) {
    return fail("after a reload, #historyRows has " + rowsAfterReload.length + " row(s), expected the same " + rowsBeforeReload + " (nothing should have changed on the way)");
  }
  notes.push("reload kept data-paramtab=history and the same " + rowsAfterReload.length + " row(s)");

  return { status: "PASS", evidence: notes.join("; ") };
}
