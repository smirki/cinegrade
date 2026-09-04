/* sidebar-toggles: clicking each toggle flips its data attribute, and at
 * 1280x720 (the narrowest width this tool targets, per sidebars.js) the
 * right toggle is still hit-testable at its own centre point, not covered
 * by something else. Both toggles are clicked twice (flip, then flip back)
 * so this spec leaves the sidebars exactly as it found them for the specs
 * that come after it. */
export default async function run(ctx) {
  const page = ctx.page;

  const need = ["mousedown", "mouseup"];
  const missing = need.filter((t) => !ctx.state.inputArrived || !ctx.state.inputArrived[t]);
  if (missing.length) {
    return {
      status: "SKIP",
      evidence: "input-probe found " + missing.join(" and ") + " never reach the page, a real click cannot be driven",
    };
  }

  await page.setViewport({ width: 1280, height: 720 });

  async function attr(side) {
    return page.evaluate((s) => document.documentElement.getAttribute("data-sidebar-" + s), side);
  }

  // #sidebarRight/#sidebarLeft animate width over 160ms (style.css), so the
  // wait after each click has to clear that transition: a later spec
  // measuring rendered width right after this one runs would otherwise
  // read a mid-transition value, not the settled one.
  const results = [];
  for (const [btnId, side] of [["sidebarLeftToggle", "left"], ["sidebarRightToggle", "right"]]) {
    const before = await attr(side);
    await page.click("#" + btnId);
    await new Promise((r) => setTimeout(r, 250));
    const afterFirst = await attr(side);
    // click again to restore the original state before the next spec runs
    await page.click("#" + btnId);
    await new Promise((r) => setTimeout(r, 250));
    const restored = await attr(side);
    results.push({ side, before, afterFirst, flipped: before !== afterFirst, restored: restored === before });
  }

  const notFlipped = results.filter((r) => !r.flipped);
  if (notFlipped.length) {
    return {
      status: "FAIL",
      evidence: "toggle click did not flip data-sidebar-* for: " + notFlipped.map((r) => r.side).join(", ") + " " + JSON.stringify(results),
    };
  }
  const notRestored = results.filter((r) => !r.restored);
  if (notRestored.length) {
    return {
      status: "FAIL",
      evidence: "toggle flips but a second click did not restore the original state for: " + notRestored.map((r) => r.side).join(", "),
    };
  }

  const hit = await page.evaluate(() => {
    var btn = document.getElementById("sidebarRightToggle");
    if (!btn) return null;
    var r = btn.getBoundingClientRect();
    var el = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    return { isBtn: el === btn, isDescendant: !!(el && btn.contains(el)), hitTag: el ? el.tagName + (el.id ? "#" + el.id : "") : null };
  });
  if (!hit) return { status: "FAIL", evidence: "#sidebarRightToggle not found" };
  if (!hit.isBtn && !hit.isDescendant) {
    return { status: "FAIL", evidence: "elementFromPoint at #sidebarRightToggle's centre at 1280x720 hit " + hit.hitTag + " instead" };
  }

  return {
    status: "PASS",
    evidence: "left: " + results[0].before + " -> " + results[0].afterFirst
      + ", right: " + results[1].before + " -> " + results[1].afterFirst
      + "; #sidebarRightToggle hit-testable at 1280x720 (" + hit.hitTag + ")",
  };
}
