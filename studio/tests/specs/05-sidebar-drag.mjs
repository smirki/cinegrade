/* sidebar-drag: a real mousedown, a real mousemove, then a real mouseup on
 * the right sidebar's own resize handle (.sidebar-resize[data-resize-side
 * ="right"], see initSidebarResizers in app.js) changes #sidebarRight's
 * rendered width by at least 100px, and the new width survives a reload
 * (sidebars.js paints it from localStorage before first paint). This is
 * one of the two behaviours the plan calls out as unverified on this
 * machine, answered here with a real drag, not a value assignment. */

/* Polls an element's rendered width every 50ms until two consecutive
 * readings agree (or the ceiling passes), rather than sleeping a fixed
 * duration: #sidebarRight's 160ms width transition (style.css) means any
 * fixed sleep at or near that duration reads a mid-transition width and
 * flakes under load. */
async function waitForStableWidth(page, elementId, ceilingMs) {
  const read = (id) => {
    var el = document.getElementById(id);
    return el ? el.getBoundingClientRect().width : null;
  };
  const start = Date.now();
  let prev = await page.evaluate(read, elementId);
  while (Date.now() - start < ceilingMs) {
    await new Promise((r) => setTimeout(r, 50));
    const cur = await page.evaluate(read, elementId);
    if (cur === prev) return cur;
    prev = cur;
  }
  return prev;
}

export default async function run(ctx) {
  const page = ctx.page;

  const need = ["mousedown", "mouseup", "mousemove"];
  const missing = need.filter((t) => !ctx.state.inputArrived || !ctx.state.inputArrived[t]);
  if (missing.length) {
    return {
      status: "SKIP",
      evidence: "input-probe found " + missing.join(", ") + " never reach the page, a real drag cannot be driven",
    };
  }

  const collapsed = await page.evaluate(() => document.documentElement.hasAttribute("data-sidebar-right"));
  if (collapsed) {
    await page.click("#sidebarRightToggle");
    await waitForStableWidth(page, "sidebarRight", 2000);
  }
  // #sidebarRight animates width over 160ms (style.css): settle here too, in
  // case an earlier spec left a collapse/expand transition still finishing,
  // so "before" is the resting width, not a mid-transition one. Polled, not
  // slept, so this holds under load instead of racing the transition.
  await waitForStableWidth(page, "sidebarRight", 2000);

  const before = await page.evaluate(() => {
    var el = document.getElementById("sidebarRight");
    var handle = document.querySelector('.sidebar-resize[data-resize-side="right"]');
    if (!el || !handle) return null;
    var hr = handle.getBoundingClientRect();
    return {
      width: el.getBoundingClientRect().width,
      stored: window.localStorage.getItem("fixxr-studio-sidebar-right-w"),
      handleX: hr.left + hr.width / 2,
      handleY: hr.top + hr.height / 2,
    };
  });
  if (!before) return { status: "FAIL", evidence: "#sidebarRight or its .sidebar-resize[data-resize-side=right] handle not found" };

  // The right sidebar's handle sits on its own left edge (app.js:
  // initSidebarResizers), so dragging it left widens the sidebar. -150px
  // stays inside its [240, 480] clamp from a 300px default.
  const dragBy = -150;
  await page.mouse.move(before.handleX, before.handleY);
  await page.mouse.down();
  await page.mouse.move(before.handleX + dragBy / 2, before.handleY, { steps: 5 });
  await page.mouse.move(before.handleX + dragBy, before.handleY, { steps: 5 });
  await page.mouse.up();
  // Poll instead of a fixed sleep: a 150ms sleep raced #sidebarRight's own
  // 160ms width transition and flaked under load.
  await waitForStableWidth(page, "sidebarRight", 2000);

  const after = await page.evaluate(() => ({
    width: document.getElementById("sidebarRight").getBoundingClientRect().width,
    stored: window.localStorage.getItem("fixxr-studio-sidebar-right-w"),
  }));

  const delta = after.width - before.width;
  if (Math.abs(delta) < 100) {
    return {
      status: "FAIL",
      evidence: "width changed by only " + delta.toFixed(1) + "px (" + before.width.toFixed(1) + " -> " + after.width.toFixed(1) + "px), wanted at least 100px",
    };
  }

  await page.reload({ waitUntil: "domcontentloaded", timeout: 30000 });
  await ctx.waitForBootComplete(20000);
  const afterReload = await page.evaluate(() => document.getElementById("sidebarRight").getBoundingClientRect().width);

  if (Math.abs(afterReload - after.width) > 1) {
    return {
      status: "FAIL",
      evidence: "width after drag was " + after.width.toFixed(1) + "px but " + afterReload.toFixed(1) + "px after reload, the drag did not persist",
    };
  }

  // Restore the pre-drag width: this widened the sidebar by a lot on
  // purpose (comfortably clears the 100px bar), and left uncorrected it
  // starves the mid column of enough overflow for widget-scroll and
  // scopes-row (specs that come right after this one) to have anything
  // real to scroll.
  await page.evaluate((w) => {
    if (window.fixxrSidebars) window.fixxrSidebars.setWidth("right", w);
  }, before.width);
  await waitForStableWidth(page, "sidebarRight", 2000);

  return {
    status: "PASS",
    evidence: "#sidebarRight " + before.width.toFixed(0) + "px -> " + after.width.toFixed(0) + "px (delta " + delta.toFixed(0)
      + "px) via real mousedown/move/up on its resize handle; survives reload at " + afterReload.toFixed(0) + "px"
      + " (localStorage fixxr-studio-sidebar-right-w=" + after.stored + "), restored to " + before.width.toFixed(0) + "px afterwards",
  };
}
