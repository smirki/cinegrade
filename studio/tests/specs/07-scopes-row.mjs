/* scopes-row: setting #dockbody.scrollLeft moves #stats and #scopes (the
 * two children of that horizontal strip) by the same delta, proving they
 * scroll together as one row rather than #dockbody clipping one of them or
 * one child having its own competing scroll. This is a direct DOM property
 * write, not a driven gesture, so it does not depend on input-probe. */
export default async function run(ctx) {
  const page = ctx.page;

  const before = await page.evaluate(() => {
    var db = document.getElementById("dockbody");
    var stats = document.getElementById("stats");
    var scopes = document.getElementById("scopes");
    if (!db || !stats || !scopes) return null;
    return {
      scrollLeft: db.scrollLeft,
      scrollWidth: db.scrollWidth,
      clientWidth: db.clientWidth,
      statsLeft: stats.getBoundingClientRect().left,
      scopesLeft: scopes.getBoundingClientRect().left,
    };
  });
  if (!before) return { status: "FAIL", evidence: "#dockbody, #stats or #scopes not found" };

  if (before.scrollWidth <= before.clientWidth) {
    return {
      status: "SKIP",
      evidence: "#dockbody has nothing to scroll at this viewport (scrollWidth " + before.scrollWidth + " <= clientWidth " + before.clientWidth + ")",
    };
  }

  const delta = Math.min(60, before.scrollWidth - before.clientWidth);
  const after = await page.evaluate((d) => {
    var db = document.getElementById("dockbody");
    db.scrollLeft += d;
    return {
      scrollLeft: db.scrollLeft,
      statsLeft: document.getElementById("stats").getBoundingClientRect().left,
      scopesLeft: document.getElementById("scopes").getBoundingClientRect().left,
    };
  }, delta);

  const actualDelta = after.scrollLeft - before.scrollLeft;
  const statsMoved = before.statsLeft - after.statsLeft;
  const scopesMoved = before.scopesLeft - after.scopesLeft;

  if (Math.abs(statsMoved - actualDelta) > 1 || Math.abs(scopesMoved - actualDelta) > 1) {
    return {
      status: "FAIL",
      evidence: "#dockbody.scrollLeft moved " + actualDelta + "px but #stats moved " + statsMoved + "px and #scopes moved " + scopesMoved + "px",
    };
  }

  return {
    status: "PASS",
    evidence: "#dockbody.scrollLeft +" + actualDelta + "px moved #stats " + statsMoved + "px and #scopes " + scopesMoved + "px (same delta)",
  };
}
