/* Fixxr Studio parity harness.
 *
 * Renders the same configuration twice, once through gpu.js on the card and
 * once through ffmpeg on the server, then measures how far apart the two
 * pictures are in 8 bit code values. The point of the whole exercise is that a
 * preview which lies is worse than a slow preview, so every claim gpu.js makes
 * in StudioGPU.stageReport has to be backed by a number measured here.
 *
 * Grain is disabled in every configuration below, deliberately. ffmpeg's noise
 * filter seeds its generator from the wall clock, so two ffmpeg runs of the
 * same grain config do not match each other. There is no ground truth to
 * compare against, so any grain parity number would be theatre. gpu.js reports
 * grain as "unsupported" and callers are expected to fall back to ffmpeg.
 *
 * Loads with no build step, no modules and no network beyond this machine.
 */
(function (global) {
  "use strict";

  /* Verdict thresholds, applied per configuration over every channel of every
   * pixel. These are stated on the page so nobody has to read the source to
   * know what "EXACT" was allowed to mean.
   *
   * EXACT allows a single code value because the GPU works in float and ffmpeg
   * works in integers, so the last bit can round either way at a tie. Anything
   * that needs more slack than that is not exact and is not described as such. */
  var TH = {
    exactMax: 1,
    closeMax: 16,
    closePctOver1: 0.5,
    closePctOver4: 0.02
  };

  var S = {
    clip: null,
    time: 1.0,
    widths: [640, 1280],
    gpu: null,
    state: null,
    srcCache: {},
    presetCfg: {},
    rows: [],
    timings: { gpuCold: [], gpuWarm: [], ffmpeg: [], gpuPasses: {} },
    log: [],
    running: false
  };

  function el(id) { return document.getElementById(id); }

  function say(msg) {
    S.log.push(msg);
    var n = el("log");
    if (n) { n.textContent = S.log.slice(-14).join("\n"); }
  }

  function post(url, body) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    }).then(function (r) {
      if (!r.ok) return r.text().then(function (t) { throw new Error(url + ": " + t); });
      return r;
    });
  }

  /* ---------------------------------------------------------------- configs */

  /* Every entry is a partial config. StudioGPU.fullConfig deep merges it into
   * the engine defaults the server reported, so a config here names only what
   * it changes and cannot drift when a default moves. `stage` is the pipeline
   * stage the entry is meant to exercise, which is what makes per stage
   * attribution possible later. */
  function isolatedConfigs() {
    var c = [];
    function add(id, stage, cfg) { c.push({ id: id, group: "stage", stage: stage, config: cfg }); }

    add("log_exposure_up", "log", { convert: { exposure: 1.0 } });
    add("log_exposure_down", "log", { convert: { exposure: -1.0 } });
    add("log_temp_warm", "log", { primaries: { temperature: 0.30 } });
    add("log_temp_cool", "log", { primaries: { temperature: -0.30 } });
    add("log_tint", "log", { primaries: { tint: -0.25 } });

    add("prim_lift", "primaries", { primaries: { lift: 0.05 } });
    add("prim_lift_rgb", "primaries", { primaries: { lift: [0.03, -0.02, 0.05] } });
    add("prim_gain", "primaries", { primaries: { gain: 1.25 } });
    add("prim_gain_rgb", "primaries", { primaries: { gain: [1.10, 1.0, 0.88] } });
    add("prim_contrast_up", "primaries", { primaries: { contrast: 1.40 } });
    add("prim_contrast_down", "primaries", { primaries: { contrast: 0.70 } });
    add("prim_contrast_pivot", "primaries", { primaries: { contrast: 1.30, pivot: 0.42 } });
    add("prim_brightness", "primaries", { primaries: { brightness: 0.06 } });
    add("prim_gamma", "primaries", { primaries: { gamma: 1.20 } });
    add("prim_gamma_rgb", "primaries", { primaries: { gamma: [1.05, 1.0, 0.92] } });
    add("prim_sat_up", "primaries", { primaries: { saturation: 1.50 } });
    add("prim_sat_zero", "primaries", { primaries: { saturation: 0.0 } });
    add("prim_vibrance_up", "primaries", { primaries: { vibrance: 0.50 } });
    add("prim_vibrance_down", "primaries", { primaries: { vibrance: -0.40 } });
    add("prim_black_lift", "primaries", { primaries: { black_lift: 0.08 } });
    add("prim_high_rolloff", "primaries", { primaries: { highlight_rolloff: 0.10 } });
    add("prim_blc_both", "primaries", { primaries: { black_lift: 0.06, highlight_rolloff: 0.12 } });

    add("curves_master_pchip", "curves", {
      curves: { enabled: true, interp: "pchip",
                master: [[0, 0], [0.25, 0.18], [0.75, 0.82], [1, 1]] } });
    add("curves_master_natural", "curves", {
      curves: { enabled: true, interp: "natural",
                master: [[0, 0], [0.25, 0.18], [0.75, 0.82], [1, 1]] } });
    add("curves_rgb", "curves", {
      curves: { enabled: true, interp: "pchip",
                r: [[0, 0.02], [0.5, 0.54], [1, 1]],
                g: [[0, 0], [0.5, 0.48], [1, 0.98]],
                b: [[0, 0.04], [0.5, 0.46], [1, 0.95]] } });
    add("curves_master_and_rgb", "curves", {
      curves: { enabled: true, interp: "pchip",
                master: [[0, 0], [0.4, 0.34], [1, 1]],
                b: [[0, 0.03], [1, 0.96]] } });

    add("sec_sat_gain", "secondary", {
      secondary: { enabled: true, hue_center: 30, hue_width: 40, sat_gain: 1.6 } });
    add("sec_hue_shift_tint", "secondary", {
      secondary: { enabled: true, hue_center: 120, hue_width: 60, hue_shift: -18,
                   tint: [0.02, 0.0, -0.03], strength: 0.8 } });
    add("sec_inverted", "secondary", {
      secondary: { enabled: true, invert: true, hue_center: 30, hue_width: 50,
                   lum_gain: 0.85 } });

    add("look_natural_full", "look", { look: { lut: "natural", mix: 1.0 } });
    add("look_natural_half", "look", { look: { lut: "natural", mix: 0.5 } });
    add("look_silverblue", "look", { look: { lut: "silverblue", mix: 1.0 } });
    add("look_blockbuster_mix", "look", { look: { lut: "blockbuster_max", mix: 0.35 } });

    add("fx_halation", "halation", { fx: { halation: { enabled: true } } });
    add("fx_halation_strong", "halation", {
      fx: { halation: { enabled: true, threshold: 0.45, sigma: 40, strength: 0.85 } } });
    add("fx_bloom", "bloom", { fx: { bloom: { enabled: true } } });
    add("fx_bloom_strong", "bloom", {
      fx: { bloom: { enabled: true, threshold: 0.55, sigma: 110, strength: 0.60 } } });
    add("fx_rgb_split", "rgb_split", { fx: { rgb_split: { enabled: true } } });
    add("fx_rgb_split_wide", "rgb_split", { fx: { rgb_split: { enabled: true, amount: 4.0 } } });
    add("fx_radial", "radial_blur", { fx: { radial_blur: { enabled: true } } });
    add("fx_radial_strong", "radial_blur", {
      fx: { radial_blur: { enabled: true, sigma: 20, start: 0.35, end: 0.95 } } });
    add("fx_vignette", "vignette", { fx: { vignette: { enabled: true } } });
    add("fx_vignette_heavy", "vignette", {
      fx: { vignette: { enabled: true, amount: 0.80, radius: 0.60 } } });

    add("detail_soften", "detail", { detail: { soften: 1.4 } });
    add("detail_sharpen", "detail", { detail: { sharpen: 0.9 } });
    add("detail_both", "detail", { detail: { soften: 0.8, sharpen: 0.6 } });

    add("letterbox_239", "letterbox", { letterbox: { enabled: true, aspect: 2.39 } });
    add("letterbox_185", "letterbox", { letterbox: { enabled: true, aspect: 1.85 } });
    return c;
  }

  /* ffmpeg picks the pixel format for the tail of the graph by negotiation,
   * so which lane the picture ends up in depends on which optical stages are
   * on, not on anything the preset says. These four configs pin one lane each
   * so the report can attribute an error to a lane rather than to a stage. */
  function laneConfigs() {
    return [
      { id: "lane_none_16bit", group: "lane", stage: "lane:none", config: {} },
      { id: "lane_rgb8", group: "lane", stage: "lane:rgb8", config: {
        fx: { vignette: { enabled: true } } } },
      { id: "lane_yuv16", group: "lane", stage: "lane:yuv16", config: {
        detail: { sharpen: 0.9 } } },
      { id: "lane_yuv8", group: "lane", stage: "lane:yuv8", config: {
        fx: { vignette: { enabled: true } }, detail: { sharpen: 0.9 } } },
      { id: "lane_yuv8_soft", group: "lane", stage: "lane:yuv8", config: {
        fx: { vignette: { enabled: true } }, detail: { soften: 0.8, sharpen: 0.9 } } }
    ];
  }

  function spaceAndTonemapConfigs() {
    var c = [];
    ["dwg", "direct"].forEach(function (ws) {
      ["aces", "filmic", "none"].forEach(function (tm) {
        ["rec709a", "gamma24"].forEach(function (en) {
          c.push({
            id: "cst_" + ws + "_" + tm + "_" + en,
            group: "convert", stage: "cst_out",
            config: { convert: { working_space: ws, tonemap: tm, encode: en } }
          });
        });
      });
    });
    return c;
  }

  function combinedConfigs() {
    return [
      { id: "combo_grade_only", group: "combined", stage: null, config: {
        primaries: { contrast: 1.18, saturation: 1.12, lift: 0.02, gain: 1.05,
                     temperature: 0.08, black_lift: 0.03, highlight_rolloff: 0.06 } } },
      { id: "combo_grade_and_look", group: "combined", stage: null, config: {
        primaries: { contrast: 1.15, vibrance: 0.25 },
        look: { lut: "warm_film", mix: 0.7 } } },
      { id: "combo_optics", group: "combined", stage: null, config: {
        fx: { halation: { enabled: true }, bloom: { enabled: true },
              vignette: { enabled: true } } } },
      { id: "combo_optics_and_detail", group: "combined", stage: null, config: {
        fx: { halation: { enabled: true }, vignette: { enabled: true } },
        detail: { soften: 0.6, sharpen: 0.5 } } },
      { id: "combo_everything_but_grain", group: "combined", stage: null, config: {
        convert: { exposure: 0.15 },
        primaries: { contrast: 1.22, saturation: 1.10, vibrance: 0.2,
                     temperature: 0.06, black_lift: 0.04, highlight_rolloff: 0.08 },
        curves: { enabled: true, interp: "pchip", master: [[0, 0], [0.35, 0.30], [1, 1]] },
        secondary: { enabled: true, hue_center: 30, hue_width: 45, sat_gain: 1.25 },
        look: { lut: "premium", mix: 0.85 },
        fx: { halation: { enabled: true }, bloom: { enabled: true },
              rgb_split: { enabled: true }, radial_blur: { enabled: true },
              vignette: { enabled: true } },
        detail: { soften: 0.5, sharpen: 0.4 },
        letterbox: { enabled: true, aspect: 2.39 } } },
      { id: "combo_direct_space_optics", group: "combined", stage: null, config: {
        convert: { working_space: "direct", tonemap: "filmic", encode: "gamma24" },
        primaries: { contrast: 1.25, saturation: 0.9 },
        fx: { vignette: { enabled: true }, halation: { enabled: true } } } },
      { id: "combo_letterbox_over_optics", group: "combined", stage: null, config: {
        fx: { bloom: { enabled: true }, vignette: { enabled: true } },
        letterbox: { enabled: true, aspect: 2.0 } } },
      { id: "combo_sharpen_over_look", group: "combined", stage: null, config: {
        look: { lut: "kodak2383", mix: 1.0 },
        detail: { sharpen: 1.2 } } }
    ];
  }

  /* The shipped presets are pulled from the server rather than copied here, so
   * this harness tests what the app actually ships and cannot go stale. */
  function presetConfigs() {
    var names = (S.state.presets || []).map(function (p) { return p.name; });
    return Promise.all(names.map(function (n) {
      return fetch("/api/preset?name=" + encodeURIComponent(n))
        .then(function (r) { return r.json(); })
        .then(function (j) { return { name: n, config: j.config || j }; });
    })).then(function (list) {
      return list.map(function (p) {
        return { id: "preset_" + p.name, group: "preset", stage: null, config: p.config };
      });
    });
  }

  function buildConfigs() {
    return presetConfigs().then(function (presets) {
      var all = [{ id: "engine_defaults", group: "baseline", stage: null, config: {} }]
        .concat(presets)
        .concat(isolatedConfigs())
        .concat(laneConfigs())
        .concat(spaceAndTonemapConfigs())
        .concat(combinedConfigs());
      /* Grain off everywhere. See the file header: ffmpeg's noise filter is
       * clock seeded, so there is no stable ground truth to measure against. */
      all.forEach(function (e) {
        e.config = StudioGPU.fullConfig(e.config);
        e.config.grain.enabled = false;
      });
      return all;
    });
  }

  /* ------------------------------------------------------------ measurement */

  function sourceWidth() {
    var c = (S.state.clips || []).filter(function (x) { return x.name === S.clip; })[0];
    if (!c) return 0;
    return (c.autorotate && c.autorotate.width) || (c.raw && c.raw.width) || c.width || 0;
  }

  function fetchSource(width) {
    if (S.srcCache[width]) return Promise.resolve(S.srcCache[width]);
    return post("/api/source", {
      clip: S.clip, time: S.time, width: width, autorotate: true
    }).then(function (r) {
      var size = (r.headers.get("X-Frame-Size") || "0x0").split("x").map(Number);
      return r.arrayBuffer().then(function (buf) {
        S.srcCache[width] = { data: new Uint16Array(buf), w: size[0], h: size[1] };
        return S.srcCache[width];
      });
    });
  }

  function fetchFfmpeg(cfg, width) {
    var t0 = performance.now();
    return post("/api/frame", {
      clip: S.clip, time: S.time, width: width, autorotate: true,
      format: "raw", config: cfg
    }).then(function (r) {
      var size = (r.headers.get("X-Frame-Size") || "0x0").split("x").map(Number);
      return r.arrayBuffer().then(function (buf) {
        return { pix: new Uint8Array(buf), ms: performance.now() - t0,
                 w: size[0], h: size[1] };
      });
    });
  }

  function compare(gpu, ff, n) {
    var hist = new Float64Array(256);
    var max = [0, 0, 0], sum = [0, 0, 0], cmax = [0, 0, 0];
    for (var i = 0; i < n; i++) {
      var b = i * 3;
      for (var k = 0; k < 3; k++) {
        var d = gpu[b + k] - ff[b + k];
        if (d < 0) d = -d;
        if (d > cmax[k]) cmax[k] = d;
        sum[k] += d;
        hist[d] += 1;
      }
    }
    max = cmax;
    var total = n * 3, seen = 0, p99 = -1, over1 = 0, over4 = 0;
    for (var d2 = 0; d2 < 256; d2++) {
      seen += hist[d2];
      if (p99 < 0 && seen >= total * 0.99) p99 = d2;
      if (d2 > 1) over1 += hist[d2];
      if (d2 > 4) over4 += hist[d2];
    }
    return {
      maxR: max[0], maxG: max[1], maxB: max[2],
      meanR: sum[0] / n, meanG: sum[1] / n, meanB: sum[2] / n,
      max: Math.max(max[0], max[1], max[2]),
      mean: (sum[0] + sum[1] + sum[2]) / total,
      p99: p99,
      pctOver1: 100 * over1 / total,
      pctOver4: 100 * over4 / total,
      pixels: n
    };
  }

  function verdict(m) {
    if (m.max <= TH.exactMax) return "EXACT";
    if (m.max <= TH.closeMax && m.pctOver1 <= TH.closePctOver1
        && m.pctOver4 <= TH.closePctOver4) return "CLOSE";
    return "FAILED";
  }

  /* The server caches a render by config, so re-running the sweep would time
   * cache hits and flatter the GPU. These configs carry a nonce that has never
   * been rendered, so they are guaranteed misses and give the real cost of the
   * ffmpeg path. */
  function coldFfmpeg(width, n) {
    var out = [], chain = Promise.resolve();
    for (var i = 0; i < n; i++) {
      (function (k) {
        chain = chain.then(function () {
          var cfg = StudioGPU.fullConfig({
            primaries: { contrast: 1 + (Date.now() % 9973) * 1e-7 + k * 1e-7 }
          });
          cfg.grain.enabled = false;
          return fetchFfmpeg(cfg, width).then(function (r) { out.push(r.ms); });
        });
      })(i);
    }
    return chain.then(function () { return out; });
  }

  function median(a) {
    if (!a.length) return 0;
    var b = a.slice().sort(function (x, y) { return x - y; });
    return b[Math.floor(b.length / 2)];
  }

  function runOne(entry, width) {
    var factor = 1;
    return fetchSource(width).then(function (src) {
      factor = src.w / (sourceWidth() || src.w);
      S.gpu.setSource(src.data, src.w, src.h);
      return S.gpu.ready(entry.config, { pixelScale: factor });
    }).then(function () {
      /* Cold means the source was just uploaded and every look up table for
       * this config is already on the card. Warm is the number that matters
       * when a slider moves, because nothing is re-uploaded then. */
      var cold = S.gpu.render(entry.config, { pixelScale: factor });
      var gpuPix = S.gpu.readPixels();
      var t0 = performance.now();
      S.gpu.render(entry.config, { pixelScale: factor });
      var warm = performance.now() - t0;

      return fetchFfmpeg(entry.config, width).then(function (ff) {
        var row = {
          id: entry.id, group: entry.group, stage: entry.stage, width: width,
          size: [cold.width, cold.height],
          gpuColdMs: cold.ms, gpuWarmMs: warm, gpuPasses: cold.passes,
          ffmpegMs: ff.ms
        };
        if (ff.w !== cold.width || ff.h !== cold.height) {
          row.error = "size mismatch: gpu " + cold.width + "x" + cold.height
            + " vs ffmpeg " + ff.w + "x" + ff.h;
          row.verdict = "FAILED";
          return row;
        }
        var m = compare(gpuPix, ff.pix, cold.width * cold.height);
        for (var k in m) { if (Object.prototype.hasOwnProperty.call(m, k)) row[k] = m[k]; }
        row.verdict = verdict(m);
        var rep = StudioGPU.stageReport(entry.config);
        row.claimed = rep.overall;
        row.approximate = rep.approximate;
        row.unsupported = rep.unsupported;
        row.quantise = rep.quantise;
        row.activeStages = rep.stages.filter(function (s) { return s.active; })
          .map(function (s) { return s.id; });
        S.timings.gpuCold.push(cold.ms);
        S.timings.gpuWarm.push(warm);
        S.timings.ffmpeg.push(ff.ms);
        return row;
      });
    }).catch(function (e) {
      return { id: entry.id, group: entry.group, stage: entry.stage, width: width,
               verdict: "FAILED", error: String(e && e.message || e) };
    });
  }

  /* ------------------------------------------------------------- reporting */

  var ORDER = { EXACT: 0, CLOSE: 1, FAILED: 2 };

  function worst(a, b) { return ORDER[b] > ORDER[a] ? b : a; }

  function stageRollup(rows) {
    var by = {};
    rows.forEach(function (r) {
      if (r.group !== "stage" && r.group !== "convert" && r.group !== "lane") return;
      var s = r.stage || "unknown";
      if (!by[s]) by[s] = { stage: s, verdict: "EXACT", max: 0, mean: 0,
                            pctOver1: 0, pctOver4: 0, p99: 0, configs: 0, worstId: "" };
      var e = by[s];
      e.configs++;
      if (ORDER[r.verdict] > ORDER[e.verdict] || (r.max || 0) > e.max) {
        if (ORDER[r.verdict] >= ORDER[e.verdict]) e.worstId = r.id + " @" + r.width;
      }
      e.verdict = worst(e.verdict, r.verdict);
      e.max = Math.max(e.max, r.max || 0);
      e.mean = Math.max(e.mean, r.mean || 0);
      e.p99 = Math.max(e.p99, r.p99 || 0);
      e.pctOver1 = Math.max(e.pctOver1, r.pctOver1 || 0);
      e.pctOver4 = Math.max(e.pctOver4, r.pctOver4 || 0);
    });
    return Object.keys(by).map(function (k) { return by[k]; });
  }

  /* Attribution: for a config that is not exact, name the stages it has on
   * that were not exact when tested on their own. That turns "this preset is
   * off by 6" into "this preset is off by 6 and it has halation on, which is
   * off by 6 on its own", which is the difference between a number and a
   * cause. */
  function attribute(rows) {
    var byStage = {};
    stageRollup(rows).forEach(function (s) { byStage[s.stage] = s; });
    rows.forEach(function (r) {
      if (!r.activeStages || r.verdict === "EXACT") { r.blame = []; return; }
      r.blame = r.activeStages.filter(function (id) {
        var s = byStage[id];
        return s && s.verdict !== "EXACT";
      });
      /* Nothing on its own is off, so the disagreement belongs to the pixel
       * format lane the combination lands in, not to any one stage. */
      if (!r.blame.length) r.blame = ["lane:" + (r.quantise || "none")];
    });
  }

  function num(v, d) {
    return (v === undefined || v === null) ? "" : (+v).toFixed(d === undefined ? 3 : d);
  }

  function cls(v) {
    return v === "EXACT" ? "ok" : (v === "CLOSE" ? "warn" : "bad");
  }

  function renderTables(report) {
    var h = [];

    h.push("<h2>Verdict thresholds</h2><p class='muted'>EXACT: no channel of no pixel "
      + "differs by more than " + TH.exactMax + " code value. CLOSE: worst channel within "
      + TH.closeMax + ", at most " + TH.closePctOver1 + " percent of channels off by more "
      + "than 1 and at most " + TH.closePctOver4 + " percent off by more than 4. Anything "
      + "else is FAILED.</p>");

    h.push("<h2>Per stage</h2><table><tr><th>Stage</th><th>Verdict</th><th>configs</th>"
      + "<th>worst max</th><th>worst mean</th><th>p99</th><th>% &gt;1</th><th>% &gt;4</th>"
      + "<th>worst case</th></tr>");
    report.stages.forEach(function (s) {
      h.push("<tr><td>" + s.stage + "</td><td class='" + cls(s.verdict) + "'>" + s.verdict
        + "</td><td>" + s.configs + "</td><td>" + s.max + "</td><td>" + num(s.mean, 4)
        + "</td><td>" + s.p99 + "</td><td>" + num(s.pctOver1, 4) + "</td><td>"
        + num(s.pctOver4, 4) + "</td><td class='muted'>" + s.worstId + "</td></tr>");
    });
    h.push("</table>");

    ["preset", "baseline", "lane", "convert", "stage", "combined"].forEach(function (g) {
      var rows = report.rows.filter(function (r) { return r.group === g; });
      if (!rows.length) return;
      h.push("<h2>" + g + " (" + rows.length + " runs)</h2>");
      h.push("<table><tr><th>Config</th><th>W</th><th>Verdict</th><th>claimed</th>"
        + "<th>max</th><th>mean</th><th>p99</th><th>% &gt;1</th><th>% &gt;4</th>"
        + "<th>maxR/G/B</th><th>GPU warm ms</th><th>ffmpeg ms</th><th>blame</th></tr>");
      rows.forEach(function (r) {
        h.push("<tr><td>" + r.id + "</td><td>" + r.width + "</td><td class='" + cls(r.verdict)
          + "'>" + r.verdict + "</td><td class='muted'>" + (r.claimed || "") + "</td><td>"
          + (r.error ? "" : r.max) + "</td><td>" + num(r.mean, 4) + "</td><td>"
          + (r.error ? "" : r.p99) + "</td><td>" + num(r.pctOver1, 4) + "</td><td>"
          + num(r.pctOver4, 4) + "</td><td>"
          + (r.error ? "" : r.maxR + "/" + r.maxG + "/" + r.maxB) + "</td><td>"
          + num(r.gpuWarmMs, 2) + "</td><td>" + num(r.ffmpegMs, 0) + "</td><td class='muted'>"
          + ((r.blame || []).join(", ") || (r.error || "")) + "</td></tr>");
      });
      h.push("</table>");
    });

    h.push("<h2>Timing</h2><table><tr><th>What</th><th>median ms</th><th>samples</th></tr>"
      + "<tr><td>GPU first render after a source upload</td><td>"
      + num(report.timing.gpuColdMedianMs, 1) + "</td><td>" + report.timing.samples
      + "</td></tr>"
      + "<tr><td>GPU re-render, source already on the card</td><td>"
      + num(report.timing.gpuWarmMedianMs, 2) + "</td><td>" + report.timing.samples
      + "</td></tr>"
      + "<tr><td>ffmpeg round trip for the same frame</td><td>"
      + num(report.timing.ffmpegMedianMs, 0) + "</td><td>" + report.timing.samples
      + "</td></tr></table>");

    h.push("<table><tr><th>Width</th><th>pixels</th><th>GPU cold ms</th>"
      + "<th>GPU warm ms</th><th>passes</th><th>ffmpeg ms (may be cached)</th>"
      + "<th>ffmpeg ms, guaranteed cache miss</th></tr>");
    (report.timing.byWidth || []).forEach(function (b2) {
      h.push("<tr><td>" + b2.width + "</td><td>"
        + (b2.size ? b2.size[0] + "x" + b2.size[1] : "") + "</td><td>"
        + num(b2.gpuColdMedianMs, 1) + "</td><td>" + num(b2.gpuWarmMedianMs, 2)
        + "</td><td>" + b2.gpuPassesMedian + "</td><td>" + num(b2.ffmpegMedianMs, 0)
        + "</td><td>" + (b2.ffmpegColdMs || []).map(function (v) {
            return v.toFixed(0); }).join(", ") + "</td></tr>");
    });
    h.push("</table>");

    h.push("<h2>Notes</h2><ul>");
    report.notes.forEach(function (n) { h.push("<li>" + n + "</li>"); });
    h.push("</ul>");

    el("out").innerHTML = h.join("");
  }

  function summarise(rows) {
    var n = { EXACT: 0, CLOSE: 0, FAILED: 0 };
    rows.forEach(function (r) { n[r.verdict] = (n[r.verdict] || 0) + 1; });
    return n;
  }

  function glInfo() {
    try {
      var gl = S.gpu.gl;
      var d = gl.getExtension("WEBGL_debug_renderer_info");
      return {
        vendor: d ? gl.getParameter(d.UNMASKED_VENDOR_WEBGL) : gl.getParameter(gl.VENDOR),
        renderer: d ? gl.getParameter(d.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER),
        version: gl.getParameter(gl.VERSION)
      };
    } catch (e) { return { error: String(e) }; }
  }

  /* `only` is a substring or a group name, used to re-run one part of the
   * sweep while debugging without paying for the whole thing. */
  function run(opts) {
    if (S.running) return Promise.resolve();
    S.running = true;
    document.body.dataset.done = "";
    var only = (opts && opts.only) || S.only || "";
    var configs = null, cold = { };
    return coldFfmpeg(S.widths[0], 3).then(function (a) {
      cold[S.widths[0]] = a;
      if (S.widths.length < 2) return null;
      return coldFfmpeg(S.widths[S.widths.length - 1], 3).then(function (b) {
        cold[S.widths[S.widths.length - 1]] = b;
      });
    }).then(buildConfigs).then(function (list) {
      if (only) {
        list = list.filter(function (e) {
          return e.id.indexOf(only) >= 0 || e.group === only || e.stage === only;
        });
      }
      configs = list;
      var total = list.length * S.widths.length, done = 0;
      say(list.length + " configs x " + S.widths.length + " widths = " + total + " runs");
      var chain = Promise.resolve();
      S.widths.forEach(function (w) {
        list.forEach(function (entry) {
          chain = chain.then(function () {
            return runOne(entry, w).then(function (row) {
              S.rows.push(row);
              done++;
              el("prog").textContent = done + " / " + total + "  " + row.id + " @" + w
                + "  " + row.verdict;
              if (row.verdict !== "EXACT") {
                say(row.verdict + "  " + row.id + " @" + w + "  max "
                  + (row.max === undefined ? row.error : row.max));
              }
            });
          });
        });
      });
      return chain;
    }).then(function () {
      attribute(S.rows);
      var report = {
        generated: new Date().toISOString(),
        clip: S.clip, time: S.time, widths: S.widths, only: only || null,
        gpuVersion: StudioGPU.version,
        gl: glInfo(),
        thresholds: TH,
        counts: summarise(S.rows),
        configCount: configs.length,
        runCount: S.rows.length,
        timing: {
          gpuColdMedianMs: median(S.timings.gpuCold),
          gpuWarmMedianMs: median(S.timings.gpuWarm),
          ffmpegMedianMs: median(S.timings.ffmpeg),
          samples: S.timings.gpuWarm.length,
          byWidth: S.widths.map(function (w) {
            var rows = S.rows.filter(function (r) { return r.width === w && !r.error; });
            return {
              width: w,
              size: rows.length ? rows[0].size : null,
              gpuColdMedianMs: median(rows.map(function (r) { return r.gpuColdMs; })),
              gpuWarmMedianMs: median(rows.map(function (r) { return r.gpuWarmMs; })),
              gpuPassesMedian: median(rows.map(function (r) { return r.gpuPasses; })),
              ffmpegMedianMs: median(rows.map(function (r) { return r.ffmpegMs; })),
              ffmpegColdMs: cold[w] || null
            };
          })
        },
        stages: stageRollup(S.rows),
        rows: S.rows,
        notes: [
          "Grain is disabled in every configuration. ffmpeg's noise filter seeds "
          + "from the wall clock, so two ffmpeg runs of the same grain config do "
          + "not agree with each other and there is no ground truth to measure.",
          "ffmpeg times include the HTTP round trip and the server's own decode, "
          + "and the server caches by config, so a repeated config is much faster "
          + "than a fresh one. The median below is over first-time renders in this "
          + "sweep only.",
          "The GPU warm number is a re-render with the source texture and every "
          + "look up table already resident, which is the case while a slider moves."
        ]
      };
      renderTables(report);
      el("prog").textContent = "done: " + JSON.stringify(report.counts);
      global.parityReport = report;
      /* A filtered run is a debugging aid, so it must not overwrite the saved
       * report with a partial sweep. */
      if (only) {
        say("filtered run, not posted");
        S.running = false;
        document.body.dataset.done = "1";
        return report;
      }
      return post("/api/parity/report", report).then(function () {
        say("posted to /api/parity/report");
        S.running = false;
        document.body.dataset.done = "1";
        return report;
      });
    }).catch(function (e) {
      say("ABORTED: " + String(e && e.message || e));
      S.running = false;
      document.body.dataset.done = "error";
      throw e;
    });
  }

  function boot() {
    var q = new URLSearchParams(location.search);
    return fetch("/api/state").then(function (r) { return r.json(); }).then(function (st) {
      S.state = st;
      StudioGPU.setDefaults(st.defaults);
      S.clip = q.get("clip") || (st.clips[0] && st.clips[0].name);
      S.time = q.get("time") ? parseFloat(q.get("time")) : 1.0;
      S.only = q.get("only") || "";
      if (q.get("widths")) {
        S.widths = q.get("widths").split(",").map(function (x) { return parseInt(x, 10); });
      }
      S.gpu = StudioGPU.create(el("cv"));
      if (!S.gpu) {
        el("prog").textContent = "no WebGL2: " + StudioGPU.lastError;
        document.body.dataset.done = "error";
        return;
      }
      el("head").textContent = "clip " + S.clip + " at t=" + S.time
        + "s, widths " + S.widths.join(" and ");
      say("ready, renderer: " + glInfo().renderer);
      if (q.get("auto") === "1") return run();
    });
  }

  global.Parity = { run: run, boot: boot, state: S, thresholds: TH };
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})(typeof window !== "undefined" ? window : this);
