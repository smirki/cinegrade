/* proxy-playback: the whole of mode 3 through the real UI.
 *
 * Prepare the proxy (the app starts that encode itself on clip select, so
 * this waits for it rather than triggering it), press Play with a real
 * click, and check the four things that separate "playback works" from "a
 * button changed colour":
 *
 *   1. the canvas content changes between two samples a second apart,
 *   2. the timeline time advances,
 *   3. #stage is on the GPU layer while playing, never the video layer
 *      (mode 3 presents to #gpuCanvas, not to #playerVideo),
 *   4. stopping returns to the still path with the layer classes correct.
 *
 * SKIPs rather than fails when there is no footage or no usable GPU, since
 * neither is something this spec can assert anything about.
 */
export default async function run(ctx) {
  const page = ctx.page;

  if (!ctx.firstClip) {
    return { status: "SKIP", evidence: "no clips from /api/clips to play" };
  }
  const gpuOk = await page.evaluate(() => !!(window.StudioLive && StudioLive.available()));
  if (!gpuOk) {
    const reason = await page.evaluate(() =>
      (window.StudioLive && StudioLive.reason()) || "no reason reported");
    return { status: "SKIP", evidence: "no GPU renderer in this browser: " + reason };
  }

  // The proxy is one ffmpeg pass over the whole clip, started in the
  // background by selectClip. Two minutes is generous for the short test
  // clips and still bounded.
  const t0 = Date.now();
  try {
    await page.waitForFunction(() => window.StudioLive && StudioLive.proxyReady(),
      { timeout: 120000, polling: 500 });
  } catch (e) {
    const status = await page.evaluate(() => document.getElementById("playStatus").textContent);
    return {
      status: "FAIL",
      evidence: "the proxy never became ready within 120s (playStatus: " + JSON.stringify(status) + ")",
    };
  }
  const prepMs = Date.now() - t0;
  const info = await page.evaluate(() => StudioLive.proxyInfo());

  function sampleCanvas() {
    return page.evaluate(() => {
      const c = document.getElementById("gpuCanvas");
      const copy = document.createElement("canvas");
      const w = Math.min(48, c.width), h = Math.min(48, c.height);
      copy.width = w; copy.height = h;
      const g = copy.getContext("2d");
      g.drawImage(c, 0, 0, w, h);
      const d = g.getImageData(0, 0, w, h).data;
      let sum = 0;
      const bytes = [];
      for (let i = 0; i < d.length; i += 4) sum += d[i] + d[i + 1] + d[i + 2];
      for (let i = 0; i < 24; i += 4) bytes.push(d[i], d[i + 1], d[i + 2]);
      return { mean: +(sum / (d.length / 4) / 3).toFixed(3), head: bytes.join(","),
               size: c.width + "x" + c.height };
    });
  }
  const readTime = () => page.evaluate(() =>
    parseFloat(document.getElementById("timeLabel").textContent) || 0);
  const stageClasses = () => page.evaluate(() => {
    const s = document.getElementById("stage");
    return { playing: s.classList.contains("playing"), gpu: s.classList.contains("gpu-live") };
  });

  // Start from a known place so "the time advanced" is not measured from the
  // end of the clip.
  await page.evaluate(() => {
    const scrub = document.getElementById("scrub");
    scrub.value = "0";
    scrub.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await new Promise((r) => setTimeout(r, 900));

  const before = await sampleCanvas();
  const timeBefore = await readTime();

  await page.click("#playBtn");
  try {
    await page.waitForFunction(() => StudioLive.isProxyPlaying(),
      { timeout: 30000, polling: 100 });
  } catch (e) {
    const label = await page.evaluate(() => document.getElementById("playBtn").textContent);
    const status = await page.evaluate(() => document.getElementById("playStatus").textContent);
    return {
      status: "FAIL",
      evidence: "clicking Play did not start proxy playback (button " + JSON.stringify(label)
        + ", status " + JSON.stringify(status) + ")",
    };
  }
  const duringLayer = await stageClasses();
  await new Promise((r) => setTimeout(r, 1000));
  const during = await sampleCanvas();
  const timeDuring = await readTime();
  const label = await page.evaluate(() => document.getElementById("playBtn").textContent);
  const stats = await page.evaluate(() => StudioLive.proxyPlaybackStats());

  await page.click("#playBtn");
  await new Promise((r) => setTimeout(r, 1200));
  const stopped = await page.evaluate(() => StudioLive.isProxyPlaying());
  const afterLayer = await stageClasses();
  const afterLabel = await page.evaluate(() => document.getElementById("playBtn").textContent);
  const badge = await page.evaluate(() => document.getElementById("rendererBadge").textContent);

  const problems = [];
  if (during.head === before.head) {
    problems.push("the canvas did not change during playback (same first pixels, mean "
      + before.mean + " -> " + during.mean + ")");
  }
  if (!(timeDuring > timeBefore)) {
    problems.push("the timeline did not advance (" + timeBefore + "s -> " + timeDuring + "s)");
  }
  if (!duringLayer.gpu || duringLayer.playing) {
    problems.push("wrong layer while playing: gpu-live=" + duringLayer.gpu
      + ", playing=" + duringLayer.playing + " (mode 3 draws on #gpuCanvas)");
  }
  if (label !== "Pause") problems.push("the button said " + JSON.stringify(label) + " while playing");
  if (stopped) problems.push("playback did not stop on the second click");
  if (afterLayer.playing) problems.push("#stage kept the video layer after stopping");
  if (!afterLayer.gpu) problems.push("#stage did not return to the GPU still layer after stopping");
  if (afterLabel !== "Play") problems.push("the button said " + JSON.stringify(afterLabel) + " after stopping");
  if (stats && stats.frames < 5) {
    problems.push("only " + stats.frames + " frames were graded in a second of playback");
  }

  const evidence = "clip " + ctx.firstClip + ": proxy " + (info ? info.width + "x" + info.height : "?")
    + " ready in " + prepMs + " ms, played " + (stats ? stats.frames : "?") + " frames at "
    + (stats ? stats.measuredFps : "?") + " fps (" + (stats ? stats.skipped : "?")
    + " skipped, " + (stats ? stats.dropped : "?") + " dropped, mode "
    + (stats ? stats.mode : "?") + "), canvas " + before.mean + " -> " + during.mean
    + ", time " + timeBefore + "s -> " + timeDuring + "s, badge after stop "
    + JSON.stringify(badge);

  if (problems.length) {
    return { status: "FAIL", evidence: problems.join("; ") + " [" + evidence + "]" };
  }
  return { status: "PASS", evidence: evidence };
}
