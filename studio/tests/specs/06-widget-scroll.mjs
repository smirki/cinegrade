/* widget-scroll: a real wheel event over #dock scrolls .gridcol >
 * .grid-stack (the single vertical scrollport, per the long comment on that
 * rule in style.css), and #dock itself has no scroll of its own to
 * intercept the gesture first (scrollHeight === clientHeight). This is the
 * other behaviour the plan calls out as unverified, answered with a real
 * page.mouse.wheel, not a scrollTop assignment.
 *
 * Uses a short viewport (1440x600) rather than the harness default: that
 * style.css comment documents .grid-stack's own overflow as a "short
 * window" behaviour (MIN_ROW_HEIGHT floors a row's height so a short
 * window's content genuinely exceeds the box). At 1440x900 whether there
 * is anything to scroll at all depends on how much unrelated content
 * #params currently has (other lanes are actively adding SCHEMA sections
 * to schema.js while this harness runs), which makes the amount of
 * overflow, and therefore whether this spec can even test anything,
 * incidental rather than a deliberate, repeatable check. */
export default async function run(ctx) {
  const page = ctx.page;

  if (!ctx.state.inputArrived || !ctx.state.inputArrived.wheel) {
    return { status: "SKIP", evidence: "input-probe found wheel events never reach the page in this Chrome" };
  }

  await page.setViewport({ width: 1440, height: 600 });
  await new Promise((r) => setTimeout(r, 250)); // let the resize handler recompute cellHeight

  const before = await page.evaluate(() => {
    var grid = document.querySelector(".gridcol > .grid-stack");
    var dock = document.getElementById("dock");
    if (!grid || !dock) return null;
    var dr = dock.getBoundingClientRect();
    return {
      scrollTop: grid.scrollTop,
      gridScrollHeight: grid.scrollHeight,
      gridClientHeight: grid.clientHeight,
      dockScrollHeight: dock.scrollHeight,
      dockClientHeight: dock.clientHeight,
      dockX: dr.left + dr.width / 2,
      dockY: dr.top + dr.height / 2,
    };
  });
  if (!before) return { status: "FAIL", evidence: "#dock or .gridcol > .grid-stack not found" };

  if (before.gridScrollHeight <= before.gridClientHeight) {
    return {
      status: "SKIP",
      evidence: ".gridcol > .grid-stack has nothing to scroll at this viewport/sidebar width (scrollHeight "
        + before.gridScrollHeight + " <= clientHeight " + before.gridClientHeight + ")",
    };
  }

  await page.mouse.move(before.dockX, before.dockY);
  await page.mouse.wheel({ deltaY: 400 });
  await new Promise((r) => setTimeout(r, 200));

  const afterScrollTop = await page.evaluate(() => document.querySelector(".gridcol > .grid-stack").scrollTop);

  const scrolled = afterScrollTop > before.scrollTop;
  const dockHasNoOwnScroll = before.dockScrollHeight === before.dockClientHeight;

  if (!scrolled) {
    return {
      status: "FAIL",
      evidence: "wheel over #dock did not move .gridcol > .grid-stack's scrollTop (" + before.scrollTop + " -> " + afterScrollTop + ")",
    };
  }
  if (!dockHasNoOwnScroll) {
    return {
      status: "FAIL",
      evidence: "#dock scrolls internally instead of leaving the gesture to bubble: scrollHeight=" + before.dockScrollHeight + " clientHeight=" + before.dockClientHeight,
    };
  }
  return {
    status: "PASS",
    evidence: ".grid-stack scrollTop " + before.scrollTop + " -> " + afterScrollTop + "px on a real wheel over #dock; "
      + "#dock scrollHeight===clientHeight (" + before.dockClientHeight + "px, no internal scroll of its own)",
  };
}
