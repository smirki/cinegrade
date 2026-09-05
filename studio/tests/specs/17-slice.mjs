/* slice (contract C2): the two new sections render, their tooltips say what
 * the units are, the Tetra subgroup starts collapsed, and a real click on a
 * real control reaches the server.
 *
 * "Reaches the server" is asserted twice on purpose, because the two halves
 * fail differently. The live config read out of #jsonText proves the click
 * landed on the right path in the browser. POST /api/lut with kind "slice"
 * proves the server will actually bake that config into the 33 cube both
 * ffmpeg and gpu.js read; a section that renders beautifully and 500s on the
 * bake is worse than one that never drew.
 */
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

  const shape = await page.evaluate(() => {
    function sec(id) { return document.querySelector('section.stage[data-stage="' + id + '"]'); }
    const hc = sec("hue_curves");
    const sl = sec("slice");
    const fold = sl ? sl.querySelector("details") : null;
    const titles = sl
      ? Array.from(sl.querySelectorAll("[title]")).map((el) => el.getAttribute("title")).join(" | ")
      : "";
    return {
      hueSection: !!hc,
      sliceSection: !!sl,
      canvas: !!(hc && hc.querySelector("#hueCurveCanvas")),
      tabs: hc ? Array.from(hc.querySelectorAll(".curvetabs .btn")).map((b) => b.textContent) : [],
      foldPresent: !!fold,
      foldOpen: fold ? fold.open : null,
      foldLabel: fold ? fold.querySelector("summary").textContent : "",
      trios: sl ? sl.querySelectorAll("details .ctl").length : 0,
      unitsDeg: /Degrees of hue rotation/.test(titles),
      unitsMul: /Saturation multiplier/.test(titles),
      unitsDensity: /L' = L \* \(1 - density \* S\)/.test(titles),
    };
  });

  if (!shape.hueSection || !shape.sliceSection) {
    return { status: "FAIL", evidence: "sections rendered: hue_curves=" + shape.hueSection + " slice=" + shape.sliceSection };
  }
  if (!shape.canvas || shape.tabs.length < 6) {
    return { status: "FAIL", evidence: "#hueCurveCanvas=" + shape.canvas + ", tabs=" + JSON.stringify(shape.tabs) + " (want five curve tabs plus reset)" };
  }
  if (!shape.foldPresent || shape.foldOpen !== false) {
    return { status: "FAIL", evidence: "Tetra subgroup present=" + shape.foldPresent + " open=" + shape.foldOpen + " (must start collapsed)" };
  }
  if (!shape.unitsDeg || !shape.unitsMul || !shape.unitsDensity) {
    return { status: "FAIL", evidence: "tooltips missing units: deg=" + shape.unitsDeg + " multiplier=" + shape.unitsMul + " density=" + shape.unitsDensity };
  }

  async function readConfig() {
    await page.click("#jsonBtn");
    await new Promise((r) => setTimeout(r, 100));
    const text = await page.$eval("#jsonText", (el) => el.value);
    await page.evaluate(() => {
      const o = document.getElementById("jsonOverlay");
      if (o) o.classList.remove("on");
    });
    return JSON.parse(text);
  }

  const before = await readConfig();

  // A real click on the real canvas adds a real point. The widget listens for
  // mousedown, so this goes through Chrome's input pipeline, not a synthetic
  // JS call into the editor. ElementHandle.click (not page.mouse.click at a
  // rect read out of the page) because the params sidebar scrolls: the canvas
  // sits far below the fold on a 1280x720 viewport, and absolute coordinates
  // from getBoundingClientRect would land on whatever happens to be there.
  const handle = await page.$("#hueCurveCanvas");
  const canvasBox = await handle.boundingBox();
  await handle.click({ offset: { x: canvasBox.width * 0.3, y: canvasBox.height * 0.3 } });
  await new Promise((r) => setTimeout(r, 200));

  const after = await readConfig();
  const pts = (after.hue_curves && after.hue_curves.hue_hue) || [];
  if (pts.length <= ((before.hue_curves && before.hue_curves.hue_hue) || []).length) {
    return {
      status: "FAIL",
      evidence: "clicking #hueCurveCanvas at 30%/30% of its "
        + Math.round(canvasBox.width) + "x" + Math.round(canvasBox.height)
        + " box left hue_curves.hue_hue at " + JSON.stringify(pts),
    };
  }
  // autoEnableParent should have ticked the section on by itself, the same way
  // moving a curves control does.
  if (!(after.hue_curves && after.hue_curves.enabled)) {
    return { status: "FAIL", evidence: "a point was added but hue_curves.enabled stayed false" };
  }

  // The server side of the same config: it has to bake.
  const bake = await page.evaluate(async (b, cfg) => {
    const r = await fetch(b + "/api/lut", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind: "slice", config: cfg }),
    });
    if (!r.ok) return { ok: false, status: r.status, body: (await r.text()).slice(0, 200) };
    const buf = await r.arrayBuffer();
    return { ok: true, size: new DataView(buf).getUint32(0, true), bytes: buf.byteLength };
  }, base, { hue_curves: after.hue_curves, slice: after.slice });

  if (!bake.ok || bake.size !== 33 || bake.bytes !== 4 + 33 * 33 * 33 * 3 * 4) {
    return { status: "FAIL", evidence: "POST /api/lut kind=slice returned " + JSON.stringify(bake) };
  }

  // Put it back so later specs and the founder's saved state are untouched.
  await page.click("#undoBtn");
  await new Promise((r) => setTimeout(r, 150));

  return {
    status: "PASS",
    evidence: "hue_curves and slice sections rendered, tabs " + JSON.stringify(shape.tabs)
      + ", Tetra subgroup collapsed with " + shape.trios + " rows, a real canvas click wrote "
      + JSON.stringify(pts) + " to hue_curves.hue_hue and auto-enabled the section, and "
      + "POST /api/lut kind=slice baked a " + bake.size + " cube (" + bake.bytes + " bytes)",
  };
}
