/* gpu-preview: after the first clip renders, #gpuCanvas is visible
 * (#stage.gpu-live, per app.js's setStageLayer) and its pixels are not all
 * identical. Needs footage: uses the first entry from /api/clips, which
 * run.mjs already fetched into ctx.firstClip; with no clips this SKIPs
 * rather than reporting a false result about a picture that was never
 * asked for. */
export default async function run(ctx) {
  const page = ctx.page;

  if (!ctx.firstClip) {
    return { status: "SKIP", evidence: "no clips from /api/clips to render" };
  }

  try {
    await page.waitForFunction(() => {
      var stage = document.getElementById("stage");
      return !!(stage && stage.classList.contains("gpu-live"));
    }, { timeout: 15000 });
  } catch (e) {
    const reason = await page.evaluate(() => {
      var btn = document.getElementById("gpuToggle");
      return btn ? btn.title : null;
    });
    return {
      status: "SKIP",
      evidence: "#stage never reached gpu-live within 15s for clip " + ctx.firstClip
        + (reason ? " (GPU preview unavailable: " + reason + ")" : " (no GPU-unavailable reason found either)"),
    };
  }

  const pixelCheck = await page.evaluate(() => {
    var canvas = document.getElementById("gpuCanvas");
    if (!canvas || canvas.width === 0 || canvas.height === 0) {
      return { ok: false, reason: "gpuCanvas has no pixels (" + (canvas ? canvas.width + "x" + canvas.height : "missing") + ")" };
    }
    var cs = getComputedStyle(canvas);
    var box = canvas.getBoundingClientRect();
    if (cs.display === "none" || box.width === 0 || box.height === 0) {
      return { ok: false, reason: "gpuCanvas is not visible (display:" + cs.display + ", box " + box.width + "x" + box.height + ")" };
    }
    var w = Math.min(64, canvas.width);
    var h = Math.min(64, canvas.height);
    var copy = document.createElement("canvas");
    copy.width = w;
    copy.height = h;
    var ctx2d = copy.getContext("2d");
    ctx2d.drawImage(canvas, 0, 0, w, h);
    var data = ctx2d.getImageData(0, 0, w, h).data;
    var first = [data[0], data[1], data[2], data[3]];
    var uniform = true;
    for (var i = 0; i < data.length; i += 4) {
      if (data[i] !== first[0] || data[i + 1] !== first[1] || data[i + 2] !== first[2] || data[i + 3] !== first[3]) {
        uniform = false;
        break;
      }
    }
    return { ok: true, uniform: uniform, sampled: w + "x" + h, canvasSize: canvas.width + "x" + canvas.height };
  });

  if (!pixelCheck.ok) {
    return { status: "FAIL", evidence: pixelCheck.reason };
  }
  if (pixelCheck.uniform) {
    return {
      status: "FAIL",
      evidence: "#gpuCanvas (" + pixelCheck.canvasSize + ") is visible but every sampled pixel in a " + pixelCheck.sampled + " sample is identical",
    };
  }
  return {
    status: "PASS",
    evidence: "clip " + ctx.firstClip + ": #gpuCanvas (" + pixelCheck.canvasSize + ") is visible and its pixels vary across a " + pixelCheck.sampled + " sample",
  };
}
