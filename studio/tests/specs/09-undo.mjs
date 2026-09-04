/* undo: a real click on a real control in #params changes the live config,
 * and #undoBtn reverts it. #params has no native <input type=range> or
 * <input type=number>: every slider and number field in controls.js is a
 * custom drag/contenteditable div with no "value" property and no input or
 * change event wired to it (only the "check" kind uses a real <input
 * type=checkbox>), so this spec drives that checkbox for real rather than
 * dispatching input/change events a slider never listens for. The cleanest
 * read of the live config is the JSON panel (#jsonBtn opens it and fills
 * #jsonText with JSON.stringify(cfg())), which is exactly what the plan
 * asks for: no private JS global to reach into. */
export default async function run(ctx) {
  const page = ctx.page;

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
  await new Promise((r) => setTimeout(r, 150));

  const afterClick = await readConfig();
  const afterVal = !!(afterClick.curves && afterClick.curves.enabled);

  if (afterVal === beforeVal) {
    return { status: "FAIL", evidence: "clicking the curves-enabled checkbox left config.curves.enabled at " + beforeVal };
  }

  const undoDisabled = await page.$eval("#undoBtn", (el) => el.disabled);
  if (undoDisabled) {
    return { status: "FAIL", evidence: "config changed (" + beforeVal + " -> " + afterVal + ") but #undoBtn is disabled" };
  }

  await page.click("#undoBtn");
  await new Promise((r) => setTimeout(r, 150));

  const afterUndo = await readConfig();
  const undoneVal = !!(afterUndo.curves && afterUndo.curves.enabled);

  if (undoneVal !== beforeVal) {
    return { status: "FAIL", evidence: "after #undoBtn, config.curves.enabled is " + undoneVal + ", expected it back to " + beforeVal };
  }

  return {
    status: "PASS",
    evidence: "config.curves.enabled " + beforeVal + " -> " + afterVal + " on a real click, back to " + undoneVal + " after #undoBtn (read via #jsonBtn/#jsonText)",
  };
}
