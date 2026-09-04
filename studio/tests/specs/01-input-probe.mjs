/* input-probe: settles whether Chrome, driven only through puppeteer's own
 * page.mouse API (never a hand rolled raw CDP call), actually delivers
 * mousedown, mouseup, wheel and pointerdown to page level JavaScript in this
 * headless setup. run.mjs installs a window level, capture phase counter
 * for these before the very first navigation (installInputProbe), which
 * this spec reads after driving one of each. Every later spec that needs
 * one of these event types reads ctx.state.inputArrived and reports SKIP
 * with the missing type named instead of a false PASS.
 *
 * The probe fires at #maintoolbar .spacer on purpose: it is flex filler
 * with no click, drag or scroll handler of its own, so driving real input
 * there cannot start a sidebar drag, a scope drag or anything else that
 * would leave the page in a different state than it found it in. */
export default async function run(ctx) {
  const page = ctx.page;

  const target = await page.evaluate(() => {
    var el = document.querySelector("#maintoolbar .spacer");
    if (!el) return null;
    var r = el.getBoundingClientRect();
    return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
  });
  if (!target) {
    return { status: "FAIL", evidence: "no #maintoolbar .spacer element to probe input against" };
  }

  await page.evaluate(() => {
    window.__inputProbe = { mousedown: 0, mouseup: 0, mousemove: 0, wheel: 0, pointerdown: 0, pointerup: 0 };
  });

  await page.mouse.move(target.x, target.y);
  await page.mouse.down();
  await page.mouse.up();
  await page.mouse.wheel({ deltaY: 10 });
  await new Promise((r) => setTimeout(r, 150));

  const counts = await page.evaluate(() => window.__inputProbe);

  const arrived = {
    mousedown: counts.mousedown > 0,
    mouseup: counts.mouseup > 0,
    wheel: counts.wheel > 0,
    pointerdown: counts.pointerdown > 0,
    mousemove: counts.mousemove > 0,
  };
  ctx.state.inputArrived = arrived;
  ctx.state.inputCounts = counts;

  const evidence = "mousedown=" + counts.mousedown + " mouseup=" + counts.mouseup
    + " wheel=" + counts.wheel + " pointerdown=" + counts.pointerdown
    + " (mousemove=" + counts.mousemove + ", pointerup=" + counts.pointerup + " also counted)"
    + " via page.mouse.move/down/up/wheel at #maintoolbar .spacer";

  return { status: "PASS", evidence };
}
