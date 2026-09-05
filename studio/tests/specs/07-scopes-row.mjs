/* scopes-row: #stats and #scopes are the two children of #dockbody, the one
 * horizontally scrolling strip the "Scopes & stats" card is built from.
 * #scopes scrolls with the strip like a plain child; #stats is pinned with
 * position: sticky (style.css) so the luma/rgb percentile numbers stay on
 * screen while the scope images scroll past underneath it. This is a direct
 * DOM property write, not a driven gesture, so it does not depend on
 * input-probe.
 *
 * The claims, in order:
 *   1. #stats and #scopes start at the same top: pinning #stats
 *      horizontally must not knock the "one row" layout out of alignment.
 *   2. A scroll moves #scopes by the scroll delta but leaves #stats in
 *      place (the pin doing its job).
 *   3. Scrolled all the way to the end of #dockbody's range, #stats and
 *      #scopes still share the same top, and #stats' whole bounding box is
 *      still inside #dockbody's own visible box, i.e. the numbers are still
 *      actually readable, not clipped or carried off screen with the
 *      images they describe.
 */
export default async function run(ctx) {
  const page = ctx.page;

  const rects = () =>
    page.evaluate(() => {
      var db = document.getElementById("dockbody");
      var stats = document.getElementById("stats");
      var scopes = document.getElementById("scopes");
      if (!db || !stats || !scopes) return null;
      var d = db.getBoundingClientRect();
      var s = stats.getBoundingClientRect();
      var c = scopes.getBoundingClientRect();
      return {
        scrollLeft: db.scrollLeft,
        scrollWidth: db.scrollWidth,
        clientWidth: db.clientWidth,
        dock: { left: d.left, right: d.right },
        stats: { left: s.left, right: s.right, top: s.top },
        scopes: { left: c.left, top: c.top },
      };
    });

  const before = await rects();
  if (!before) return { status: "FAIL", evidence: "#dockbody, #stats or #scopes not found" };

  if (Math.abs(before.stats.top - before.scopes.top) > 2) {
    return {
      status: "FAIL",
      evidence: "before any scroll, #stats top " + before.stats.top.toFixed(1) + " and #scopes top " + before.scopes.top.toFixed(1) + " differ by more than 2px, they are not sharing one row",
    };
  }

  if (before.scrollWidth <= before.clientWidth) {
    return {
      status: "SKIP",
      evidence: "#dockbody has nothing to scroll at this viewport (scrollWidth " + before.scrollWidth + " <= clientWidth " + before.clientWidth + ")",
    };
  }

  const delta = Math.min(60, before.scrollWidth - before.clientWidth);
  await page.evaluate((d) => { document.getElementById("dockbody").scrollLeft += d; }, delta);
  const partial = await rects();

  const actualDelta = partial.scrollLeft - before.scrollLeft;
  const scopesMoved = before.scopes.left - partial.scopes.left;
  const statsMoved = before.stats.left - partial.stats.left;

  if (Math.abs(scopesMoved - actualDelta) > 1) {
    return {
      status: "FAIL",
      evidence: "#dockbody.scrollLeft moved " + actualDelta + "px but #scopes moved " + scopesMoved + "px, they are meant to track together",
    };
  }
  if (Math.abs(statsMoved) > 1) {
    return {
      status: "FAIL",
      evidence: "#dockbody.scrollLeft moved " + actualDelta + "px and #stats moved " + statsMoved + "px with it; #stats is pinned (position: sticky) and should not move",
    };
  }

  // The end of the range is the case that actually exercises the pin: a
  // small scroll leaves #stats inside the visible box either way.
  await page.evaluate(() => {
    var db = document.getElementById("dockbody");
    db.scrollLeft = db.scrollWidth;
  });
  const atEnd = await rects();

  if (Math.abs(atEnd.stats.top - atEnd.scopes.top) > 2) {
    return {
      status: "FAIL",
      evidence: "scrolled fully right, #stats top " + atEnd.stats.top.toFixed(1) + " and #scopes top " + atEnd.scopes.top.toFixed(1) + " differ by more than 2px, they no longer share one row",
    };
  }

  const inside = atEnd.stats.left >= atEnd.dock.left - 0.5 && atEnd.stats.right <= atEnd.dock.right + 0.5;
  if (!inside) {
    return {
      status: "FAIL",
      evidence: "scrolled fully right, #stats (left " + atEnd.stats.left.toFixed(1) + ", right " + atEnd.stats.right.toFixed(1) + ") is outside #dockbody's visible box (left " + atEnd.dock.left.toFixed(1) + ", right " + atEnd.dock.right.toFixed(1) + "), the pin let it scroll away",
    };
  }

  try {
    const path = (await import("node:path")).default;
    const { fileURLToPath } = await import("node:url");
    const { mkdirSync } = await import("node:fs");
    const here = path.dirname(fileURLToPath(import.meta.url));
    const shotsDir = path.resolve(here, "..", "..", "shots"); // specs -> tests -> studio -> shots
    mkdirSync(shotsDir, { recursive: true });
    await page.screenshot({ path: path.join(shotsDir, "w2f-stats-pinned.png") });
  } catch (err) {
    // The screenshot is evidence for a human, not part of the pass/fail
    // contract: a write failure here must not turn a real PASS into a FAIL.
  }

  return {
    status: "PASS",
    evidence: "one row: #stats/#scopes top within 2px before and after scroll; #dockbody.scrollLeft +" + actualDelta
      + "px moved #scopes " + scopesMoved.toFixed(1) + "px and #stats " + statsMoved.toFixed(1) + "px (pinned); "
      + "scrolled fully right (scrollLeft " + atEnd.scrollLeft + " of scrollWidth " + atEnd.scrollWidth + "), #stats stayed inside #dockbody's visible box",
  };
}
