/* undo: a real click on a real control in #params changes the live config,
 * and #undoBtn reverts it. #params has no native <input type=range> or
 * <input type=number>: every slider and number field in controls.js is a
 * custom drag/contenteditable div with no "value" property and no input or
 * change event wired to it (only the "check" kind uses a real <input
 * type=checkbox>), so this spec drives that checkbox for real rather than
 * dispatching input/change events a slider never listens for. The cleanest
 * read of the live config is the JSON panel (#jsonBtn opens it and fills
 * #jsonText with JSON.stringify(cfg())), which is exactly what the plan
 * asks for: no private JS global to reach into.
 *
 * Since contract C4 undo is the PROJECT's undo: #undoBtn posts to
 * /api/project/undo and applies the config that comes back, so the same undo
 * stack is shared with the CLI and with any other tab. Two consequences for
 * this spec. It can no longer sleep a fixed 150 ms and read: every step is a
 * round trip, so each one waits for its own observable effect (the button
 * enabling, the value changing) with a ceiling. And redo is now real, so it
 * is checked here too, both from #redoBtn and from the keyboard.
 *
 * The keyboard leg presses Control+z rather than Meta+z: app.js accepts
 * either (`var meta = ev.metaKey || ev.ctrlKey`), and Chrome on macOS
 * swallows some synthetic key events while Meta is held, which would make
 * this a flaky test of the harness rather than of the app. */
export default async function run(ctx) {
  const page = ctx.page;

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  // Waits for a round trip's effect instead of guessing how long it takes.
  async function until(what, read, want, ceilingMs) {
    const start = Date.now();
    let seen = await read();
    while (seen !== want && Date.now() - start < (ceilingMs || 8000)) {
      await sleep(120);
      seen = await read();
    }
    if (seen !== want) {
      return "waited " + (Date.now() - start) + "ms for " + what + " to be " + want + ", it is " + seen;
    }
    return null;
  }

  const need = ["mousedown", "mouseup"];
  const missing = need.filter((t) => !ctx.state.inputArrived || !ctx.state.inputArrived[t]);
  if (missing.length) {
    return {
      status: "SKIP",
      evidence: "input-probe found " + missing.join(" and ") + " never reach the page, the checkbox and buttons cannot be clicked for real",
    };
  }

  const checkboxSel = 'section.stage[data-stage="curves"] input.ctl-switch';
  const hasCheckbox = await page.$(checkboxSel);
  if (!hasCheckbox) return { status: "FAIL", evidence: "no " + checkboxSel + " found in #params" };

  async function readConfig() {
    await page.click("#jsonBtn");
    await new Promise((r) => setTimeout(r, 100));
    const text = await page.$eval("#jsonText", (el) => el.value);
    await page.evaluate(() => {
      var o = document.getElementById("jsonOverlay");
      if (o) o.classList.remove("on");
    });
    return JSON.parse(text);
  }

  const before = await readConfig();
  const beforeVal = !!(before.curves && before.curves.enabled);

  await page.click(checkboxSel);
  await sleep(200);

  const afterClick = await readConfig();
  const afterVal = !!(afterClick.curves && afterClick.curves.enabled);

  if (afterVal === beforeVal) {
    return { status: "FAIL", evidence: "clicking the curves-enabled checkbox left config.curves.enabled at " + beforeVal };
  }

  const curves = async () => !!((await readConfig()).curves || {}).enabled;
  const undoEnabled = () => page.$eval("#undoBtn", (el) => !el.disabled);

  // The commit is a round trip, and #undoBtn only enables once the tab has
  // heard the new HEAD back, so wait for that rather than assuming.
  let bad = await until("#undoBtn enabled after a committed change", undoEnabled, true, 8000);
  if (bad) return { status: "FAIL", evidence: "config changed (" + beforeVal + " -> " + afterVal + ") but " + bad };

  await page.click("#undoBtn");
  bad = await until("config.curves.enabled after #undoBtn", curves, beforeVal, 8000);
  if (bad) return { status: "FAIL", evidence: bad };

  // Redo is the same history read the other way. It is enabled only when HEAD
  // has somewhere forward to go, which is exactly what the undo just created.
  const redoEnabled = () => page.$eval("#redoBtn", (el) => !el.disabled);
  bad = await until("#redoBtn enabled after an undo", redoEnabled, true, 8000);
  if (bad) return { status: "FAIL", evidence: bad };

  await page.click("#redoBtn");
  bad = await until("config.curves.enabled after #redoBtn", curves, afterVal, 8000);
  if (bad) return { status: "FAIL", evidence: bad };

  // And the same two from the keyboard, which is how anybody grading actually
  // reaches for them. Drop focus first: key events go to the focused element,
  // the clicks above left it on a button or the checkbox, and app.js ignores
  // these keys whenever the target is an input, a select or a contenteditable.
  // Blurring rather than clicking somewhere neutral, because a click on the
  // stage means something (window editor, match picker) and would be a side
  // effect this spec has no business causing.
  await page.evaluate(() => {
    const el = document.activeElement;
    if (el && el.blur) el.blur();
  });
  await page.keyboard.down("Control");
  await page.keyboard.press("KeyZ");
  await page.keyboard.up("Control");
  bad = await until("config.curves.enabled after ctrl+z", curves, beforeVal, 8000);
  if (bad) return { status: "FAIL", evidence: bad };

  await page.keyboard.down("Control");
  await page.keyboard.down("Shift");
  await page.keyboard.press("KeyZ");
  await page.keyboard.up("Shift");
  await page.keyboard.up("Control");
  bad = await until("config.curves.enabled after shift+ctrl+z", curves, afterVal, 8000);
  if (bad) return { status: "FAIL", evidence: bad };

  // Leave the clip as this spec found it, so a later spec reading the same
  // config is not looking at a stray flipped checkbox.
  await page.click("#undoBtn");
  await until("config.curves.enabled restored for the next spec", curves, beforeVal, 8000);

  return {
    status: "PASS",
    evidence: "config.curves.enabled " + beforeVal + " -> " + afterVal + " on a real click, back to "
      + beforeVal + " after #undoBtn and forward again after #redoBtn, and the same pair from ctrl+z"
      + " and shift+ctrl+z (read via #jsonBtn/#jsonText)",
  };
}
