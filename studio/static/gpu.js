/* StudioGPU: a WebGL2 re-implementation of the cinegrade filter chain.
 *
 * The contract this file signs up to is narrow and strict: for a given config
 * it either produces the same picture ffmpeg produces, to within a code value
 * at 8 bit, or it says out loud that it cannot. `StudioGPU.stageReport(config)`
 * is that statement, and a caller is expected to fall back to the ffmpeg path
 * for any stage it marks unsupported. A preview that quietly drifts from the
 * render is worse than no preview at all, because a grade gets made against it.
 *
 * Everything here was written against the ffmpeg 8.1 sources for the filters
 * cinegrade actually calls (lut, lut3d, curves, colorchannelmixer, vibrance,
 * gblur, unsharp, vignette, blend, maskedmerge, rgbashift) and against the
 * format negotiation ffmpeg actually performs on those graphs, which is not
 * the same thing as what the graph string looks like. See STAGE_NOTES.
 *
 * No build step, no modules, no network at run time beyond the two local
 * endpoints it uses to fetch LUT data.
 */

(function (global) {
  "use strict";

  // ------------------------------------------------------------------
  // constants mirrored from grade/cinegrade.py
  // ------------------------------------------------------------------

  var APPLE_LOG_STOP = 0.08492;
  /* Mirrors cinegrade.py. The API does not expose these, so a change to
 * either constant in the engine has to be copied here or the preview
 * quietly disagrees with the render. */
  var MID_GREY_CODE = { dwg: 0.3360, direct: 0.4883 };
  var VIGNETTE_RADIUS_NEUTRAL = 0.85;

  /* mid_detail: local contrast at a wide gaussian radius. Mirrors
   * MID_DETAIL_SIGMA_FRAC / MID_DETAIL_SIGMA_MIN / MID_DETAIL_K in
   * cinegrade.py. Sigma is a FRACTION of the actual rendered width, so it is
   * computed from the source texture's own width at render time (see
   * detailPass) rather than needing a scaleForPreview entry: a preview
   * texture is genuinely fewer real pixels, so 2% of ITS width is already the
   * proportionally correct radius. */
  var MID_DETAIL_SIGMA_FRAC = 0.02;
  var MID_DETAIL_SIGMA_MIN = 1.0;
  var MID_DETAIL_K = 1.0;

  /* The power window block, mirrored from cinegrade.DEFAULTS["window"].
   *
   * It is a named constant as well as a member of DEFAULTS below because
   * setDefaults() can replace DEFAULTS with a copy that came from an older
   * server, and a window block with missing fields would resolve to NaN in
   * the matte rather than to the engine's own numbers. This constant is the
   * merge base, so every field always has a value.
   *
   * Every geometric value is a FRACTION of the frame, which is what lets a
   * 640 wide preview and a 3840 wide render describe the same shape without
   * scaleForPreview having a case for any of it. cx/cy are the centre, w/h
   * the FULL extent (not the half axis), rotation is degrees clockwise on
   * screen, softness is the feather width as a fraction of the shape radius. */
  var WINDOW_DEFAULTS = {
    enabled: false, shape: "ellipse",
    cx: 0.5, cy: 0.5, w: 0.6, h: 0.6,
    rotation: 0.0, softness: 0.15, invert: false
  };

  /* One layer, mirrored from cinegrade.LAYER_DEFAULTS.
   *
   * A named constant for the same reason WINDOW_DEFAULTS is one: a layer
   * arriving from an older server, or straight out of a preset file, carries
   * only the fields somebody touched, and a missing one has to resolve to the
   * engine's own number rather than to NaN. Every layer read out of a config
   * goes through configLayers, which merges onto this.
   *
   * A layer is a mask (a power window and a colour key, either or both, with
   * one invert over the pair) and a correction. The correction is baked to a
   * 33 cube BY THE SERVER, from the same cinegrade.layer_lut the render
   * calls, so none of the colour maths is reimplemented here. */
  var LAYER_DEFAULTS = {
    enabled: true, name: "Layer 1", placement: "before_look",
    mask: {
      show: false, invert: false,
      window: WINDOW_DEFAULTS,
      key: {
        enabled: false, invert: false,
        hue_center: 30.0, hue_width: 40.0, hue_soft: 15.0,
        sat_low: 0.10, sat_high: 1.0, sat_soft: 0.10,
        lum_low: 0.0, lum_high: 1.0, lum_soft: 0.10
      }
    },
    correct: {
      exposure: 0.0, contrast: 1.0, pivot: null, saturation: 1.0,
      temperature: 0.0, tint: 0.0,
      hue_shift: 0.0, sat_gain: 1.0, lum_gain: 1.0,
      offset: [0.0, 0.0, 0.0], blur: 0.0, strength: 1.0
    }
  };

  /* correct.blur is quoted in pixels at 1920 wide and resolved against the
   * width the render is actually running at, exactly the way every window
   * parameter is a fraction of the frame. That is what keeps a 640 wide
   * preview showing the blur the full render will have without
   * scaleForPreview needing a case for it. Mirrors layer_blur_sigma. */
  var LAYER_BLUR_REF_WIDTH = 1920.0;

  // A local copy of cinegrade.DEFAULTS. It is a mirror, not the source of
  // truth: setDefaults() lets a caller hand over the copy the server sent so
  // an engine change cannot silently leave this file behind.
  var DEFAULTS = {
    convert: { tonemap: "aces", exposure: 0.0, working_space: "dwg", encode: "rec709a" },
    primaries: {
      contrast: 1.0, pivot: null, saturation: 1.0, vibrance: 0.0,
      temperature: 0.0, tint: 0.0, brightness: 0.0,
      lift: 0.0, gamma: 1.0, gain: 1.0,
      black_lift: 0.0, highlight_rolloff: 0.0
    },
    look: { lut: null, mix: 1.0, lut2: null, mix2: 1.0, balance: 0.0 },
    detail: { soften: 0.0, sharpen: 0.0, mid_detail: 0.0 },
    // prep.denoise (hqdn3d) cannot run in this shader: reported unsupported
    // in stageReport whenever active, so the caller falls back to the ffmpeg
    // still. Mirrored here only so fullConfig never resolves it to NaN.
    prep: { denoise: { enabled: false, spatial: 0.0, temporal: 0.0 } },
    fx: {
      halation: { enabled: false, threshold: 0.62, sigma: 26, strength: 0.55, tint: [1.0, 0.34, 0.16] },
      bloom: { enabled: false, threshold: 0.72, sigma: 70, strength: 0.28, tint: [1.0, 0.97, 0.92] },
      rgb_split: { enabled: false, amount: 1.6 },
      radial_blur: { enabled: false, sigma: 9, start: 0.55, end: 1.0 },
      vignette: { enabled: false, amount: 0.45, radius: 0.85 }
    },
    grain: { enabled: false, strength: 40, size: 3, opacity: 0.5,
             stock: "custom", softness: 0.0, response: "flat",
             color: 0.0, seed: 0 },
    letterbox: { enabled: false, aspect: 2.39 },
    curves: {
      enabled: false, interp: "pchip",
      master: [[0.0, 0.0], [1.0, 1.0]], r: [[0.0, 0.0], [1.0, 1.0]],
      g: [[0.0, 0.0], [1.0, 1.0]], b: [[0.0, 0.0], [1.0, 1.0]]
    },
    hue_curves: {
      enabled: false,
      hue_hue: [], hue_sat: [], hue_lum: [], lum_sat: [], sat_sat: []
    },
    slice: {
      enabled: false, density: 0.0,
      vectors: {
        red: { hue: 0.0, sat: 1.0, density: 0.0 },
        yellow: { hue: 0.0, sat: 1.0, density: 0.0 },
        green: { hue: 0.0, sat: 1.0, density: 0.0 },
        cyan: { hue: 0.0, sat: 1.0, density: 0.0 },
        blue: { hue: 0.0, sat: 1.0, density: 0.0 },
        magenta: { hue: 0.0, sat: 1.0, density: 0.0 },
        skin: { hue: 0.0, sat: 1.0, density: 0.0 }
      },
      tetra: {
        enabled: false,
        r: [0.0, 0.0, 0.0], g: [0.0, 0.0, 0.0], b: [0.0, 0.0, 0.0],
        c: [0.0, 0.0, 0.0], m: [0.0, 0.0, 0.0], y: [0.0, 0.0, 0.0]
      }
    },
    /* A list of masked correction layers, applied in order. This replaces
     * the single `secondary` block and the single `window` block. Empty is
     * the default and builds exactly the chain that shipped before layers
     * existed, so an old config is not merely equivalent, it is identical.
     * See LAYER_DEFAULTS and migrateLayers. */
    layers: [],
    output: { codec: "prores_ks", profile: 3, crf: 16, preset: "slow" }
  };

  function deepMerge(base, over) {
    var out = {}, k;
    for (k in base) {
      if (Object.prototype.hasOwnProperty.call(base, k)) out[k] = clone(base[k]);
    }
    for (k in (over || {})) {
      if (!Object.prototype.hasOwnProperty.call(over, k)) continue;
      var v = over[k];
      if (isPlain(v) && isPlain(out[k])) out[k] = deepMerge(out[k], v);
      else out[k] = clone(v);
    }
    return out;
  }
  function isPlain(v) {
    return v && typeof v === "object" && !Array.isArray(v);
  }
  function clone(v) {
    if (Array.isArray(v)) return v.map(clone);
    if (isPlain(v)) return deepMerge(v, {});
    return v;
  }
  /* The engine's own read path: migrate FIRST, then merge the defaults.
   *
   * The order is not cosmetic. DEFAULTS always supplies an empty `layers`, so
   * merging first would make every pre-layers config look like a config that
   * already has a (empty) stack, and its secondary and window would be
   * dropped without a word. Mirrors cinegrade.load_preset and server.py's
   * full_config, which had to learn the same lesson. */
  function fullConfig(cfg) { return deepMerge(DEFAULTS, migrateLayers(cfg || {})); }

  /* Round the way cinegrade's format strings round.
   *
   * The engine writes its numbers into an ffmpeg filter string with a fixed
   * number of decimals, so ffmpeg never sees the full double. Matching that
   * rounding is free and removes a whole class of small drift. */
  function fmt(v, digits) {
    return parseFloat(Number(v).toFixed(digits));
  }

  function triplet(value, def) {
    if (value === null || value === undefined) value = def;
    if (Array.isArray(value)) return [+value[0], +value[1], +value[2]];
    return [+value, +value, +value];
  }

  function curveIsIdentity(pts) {
    if (!pts || pts.length < 2) return true;
    for (var i = 0; i < pts.length; i++) {
      if (Math.abs(+pts[i][0] - +pts[i][1]) >= 1e-6) return false;
    }
    return true;
  }

  /* The hue curves + Color Slice + Tetra stage: is it doing anything?
   *
   * A port of grade/slice.py is_identity, and the ONLY thing this file ports:
   * the cube itself is baked by the engine and fetched over /api/lut, so the
   * preview cannot drift from the render by re-deriving the colour maths in
   * JavaScript. This predicate exists so a neutral stage costs no request and
   * no lut3d, which is exactly what build_graph does with f_slice returning [].
   *
   * The neutral of a hue curve is a FLAT line, not the diagonal: y is an
   * offset (hue_hue, 0) or a multiplier (the other four, 1). An empty list is
   * the identity.
   */
  var SLICE_CURVE_NEUTRAL = {
    hue_hue: 0, hue_sat: 1, hue_lum: 1, lum_sat: 1, sat_sat: 1
  };
  var SLICE_VECTORS = ["red", "yellow", "green", "cyan", "blue", "magenta", "skin"];
  var TETRA_CORNERS = ["r", "g", "b", "c", "m", "y"];

  function hueCurvesActive(hc) {
    if (!hc || !hc.enabled) return false;
    for (var key in SLICE_CURVE_NEUTRAL) {
      if (!Object.prototype.hasOwnProperty.call(SLICE_CURVE_NEUTRAL, key)) continue;
      var pts = hc[key] || [];
      for (var i = 0; i < pts.length; i++) {
        if (Math.abs(+pts[i][1] - SLICE_CURVE_NEUTRAL[key]) > 1e-9) return true;
      }
    }
    return false;
  }

  function sliceBlockActive(sl) {
    if (!sl || !sl.enabled) return false;
    if (Math.abs(+sl.density || 0) > 1e-9) return true;
    var vecs = sl.vectors || {}, i, j;
    for (i = 0; i < SLICE_VECTORS.length; i++) {
      var v = vecs[SLICE_VECTORS[i]] || {};
      if (Math.abs(+(v.hue || 0)) > 1e-9) return true;
      if (Math.abs((v.sat === undefined ? 1 : +v.sat) - 1) > 1e-9) return true;
      if (Math.abs(+(v.density || 0)) > 1e-9) return true;
    }
    var te = sl.tetra || {};
    if (te.enabled) {
      for (i = 0; i < TETRA_CORNERS.length; i++) {
        var d = te[TETRA_CORNERS[i]] || [0, 0, 0];
        for (j = 0; j < 3; j++) if (Math.abs(+d[j] || 0) > 1e-9) return true;
      }
    }
    return false;
  }

  function sliceActive(cfg) {
    return hueCurvesActive(cfg.hue_curves) || sliceBlockActive(cfg.slice);
  }

  // ------------------------------------------------------------------
  // ffmpeg curves, ported exactly (libavfilter/vf_curves.c)
  // ------------------------------------------------------------------

  var CURVE_LUT_SIZE = 65536;   // the pipeline is 16 bit, so this is ffmpeg's
  var CURVE_SCALE = 65535;

  /* Natural cubic spline, ffmpeg's interpolate().
   *
   * Not the same spline as the pchip below, and the difference is visible: the
   * black lift / highlight rolloff control uses `curves` with no interp
   * argument, and ffmpeg's default is natural, not pchip. */
  function naturalLut(points, out) {
    var n = points.length, i;
    if (n === 0) { for (i = 0; i < CURVE_LUT_SIZE; i++) out[i] = i / CURVE_SCALE; return out; }
    if (n === 1) { for (i = 0; i < CURVE_LUT_SIZE; i++) out[i] = clamp01(points[0][1]); return out; }

    var h = new Float64Array(n - 1), r = new Float64Array(n);
    for (i = 0; i < n - 1; i++) h[i] = points[i + 1][0] - points[i][0];
    for (i = 1; i < n - 1; i++) {
      r[i] = 6 * ((points[i + 1][1] - points[i][1]) / h[i]
                - (points[i][1] - points[i - 1][1]) / h[i - 1]);
    }
    // Tridiagonal solve with the natural end condition (second derivative 0).
    var md = new Float64Array(n), ad = new Float64Array(n), bd = new Float64Array(n);
    md[0] = md[n - 1] = 1;
    for (i = 1; i < n - 1; i++) { bd[i] = h[i - 1]; md[i] = 2 * (h[i - 1] + h[i]); ad[i] = h[i]; }
    for (i = 1; i < n; i++) {
      var den = md[i] - bd[i] * ad[i - 1];
      var k = den ? 1 / den : 1;
      ad[i] *= k;
      r[i] = (r[i] - bd[i] * r[i - 1]) * k;
    }
    for (i = n - 2; i >= 0; i--) r[i] = r[i] - ad[i] * r[i + 1];

    var xStart0 = (points[0][0] * CURVE_SCALE) | 0;
    for (i = 0; i < xStart0; i++) out[i] = clamp01(points[0][1]);
    for (var seg = 0; seg < n - 1; seg++) {
      var yc = points[seg][1], yn = points[seg + 1][1];
      var a = yc;
      var b = (yn - yc) / h[seg] - h[seg] * r[seg] / 2 - h[seg] * (r[seg + 1] - r[seg]) / 6;
      var c = r[seg] / 2;
      var d = (r[seg + 1] - r[seg]) / (6 * h[seg]);
      var xs = (points[seg][0] * CURVE_SCALE) | 0;
      var xe = (points[seg + 1][0] * CURVE_SCALE) | 0;
      for (var x = xs; x <= xe; x++) {
        var xx = (x - xs) / CURVE_SCALE;
        out[x] = clamp01(a + b * xx + c * xx * xx + d * xx * xx * xx);
      }
    }
    var xEnd = (points[n - 1][0] * CURVE_SCALE) | 0;
    for (i = xEnd; i < CURVE_LUT_SIZE; i++) out[i] = clamp01(points[n - 1][1]);
    return out;
  }

  function sgn(x) { return x > 0 ? 1 : (x < 0 ? -1 : 0); }

  /* scipy's _edge_case, which is what ffmpeg's pchip uses at the two ends.
   *
   * Ctl.pchipEval in controls.js used to skip this and take the plain end
   * secant instead, so the curve the editor drew was not the curve that
   * rendered: on the four point S curve 0/0 0.25/0.18 0.75/0.82 1/1, measured
   * against ffmpeg 8.1.1 on a 65536 entry ramp, the drawn line was 454/65535
   * out at x = 0.0803, which is 1.77 code values at 8 bit. The editor now uses
   * the same edge case, so the drawn line and this table agree. */
  function pchipEdgeCase(h0, h1, m0, m1) {
    var d = ((2 * h0 + h1) * m0 - h0 * m1) / (h0 + h1);
    if (sgn(d) !== sgn(m0)) return 0;
    if (sgn(m0) !== sgn(m1) && Math.abs(d) > 3 * Math.abs(m0)) return 3 * m0;
    return d;
  }

  function pchipLut(points, out) {
    var n = points.length, i;
    if (n === 0) { for (i = 0; i < CURVE_LUT_SIZE; i++) out[i] = i / CURVE_SCALE; return out; }
    if (n === 1) { for (i = 0; i < CURVE_LUT_SIZE; i++) out[i] = clamp01(points[0][1]); return out; }

    // ffmpeg works in LUT index units here, not in [0,1]; keeping that means
    // the interval widths and slopes are bit for bit the same quantities.
    var xi = new Float64Array(n), fi = new Float64Array(n);
    for (i = 0; i < n; i++) { xi[i] = points[i][0] * CURVE_SCALE; fi[i] = points[i][1] * CURVE_SCALE; }
    var hi = new Float64Array(n - 1), mi = new Float64Array(n - 1);
    for (i = 0; i < n - 1; i++) { hi[i] = xi[i + 1] - xi[i]; mi[i] = (fi[i + 1] - fi[i]) / hi[i]; }

    if (n === 2) {
      var m = mi[0], b = fi[0] - xi[0] * m;
      for (i = 0; i < CURVE_LUT_SIZE; i++) out[i] = clamp01((i * m + b) / CURVE_SCALE);
      return out;
    }

    var di = new Float64Array(n);
    for (i = 0; i < n - 2; i++) {
      if (sgn(mi[i + 1]) !== sgn(mi[i]) || mi[i + 1] === 0 || mi[i] === 0) {
        di[i + 1] = 0;
      } else {
        var w1 = 2 * hi[i + 1] + hi[i], w2 = hi[i + 1] + 2 * hi[i];
        di[i + 1] = (w1 + w2) / (w1 / mi[i] + w2 / mi[i + 1]);
      }
    }
    di[0] = pchipEdgeCase(hi[0], hi[1], mi[0], mi[1]);
    di[n - 1] = pchipEdgeCase(hi[n - 2], hi[n - 3], mi[n - 2], mi[n - 3]);

    var x = 0;
    for (; x < xi[0] && x < CURVE_LUT_SIZE; x++) out[x] = clamp01(fi[0] / CURVE_SCALE);
    for (i = 0; i < n - 1; i++) {
      var h = hi[i], f0 = fi[i], f1 = fi[i + 1], d0 = di[i], d1 = di[i + 1], x0 = xi[i];
      for (; x < xi[i + 1] && x < CURVE_LUT_SIZE; x++) {
        var t = (x - x0) / h;
        out[x] = clamp01((hermiteHalf(1 - t, f0, -h * d0) + hermiteHalf(t, f1, h * d1)) / CURVE_SCALE);
      }
    }
    for (; x < CURVE_LUT_SIZE; x++) out[x] = clamp01(fi[n - 1] / CURVE_SCALE);
    return out;
  }

  function hermiteHalf(t, f, d) {
    var t2 = t * t, t3 = t2 * t;
    return f * (3 * t2 - 2 * t3) + d * (t3 - t2);
  }

  function clamp01(v) { return v < 0 ? 0 : (v > 1 ? 1 : v); }

  /* Build the RGB curve tables the way `curves` builds its graph[].
   *
   * Note the composition order: ffmpeg does graph[i][j] = master[channel[j]],
   * so the per channel curve runs FIRST and the master runs on its output.
   * Getting that backwards is invisible on symmetric curves and obvious on
   * asymmetric ones. */
  /* Cheap answer to "will the curves node do anything", used by stageReport so
   * asking for a report does not build a 65536 entry table four times. */
  function curvesActive(cfg) {
    var c = cfg.curves || {};
    if (!c.enabled) return false;
    return ["master", "r", "g", "b"].some(function (k) { return !curveIsIdentity(c[k]); });
  }

  function buildCurveTable(cfg) {
    var c = cfg.curves || {};
    var interp = c.interp === "natural" ? naturalLut : pchipLut;
    var chans = ["r", "g", "b"], out = {}, any = false;

    function pts(key) {
      var p = c[key];
      if (curveIsIdentity(p)) return null;
      // The engine writes the points with 4 decimals into the filter string.
      return p.map(function (q) { return [fmt(q[0], 4), fmt(q[1], 4)]; });
    }
    var master = pts("master");
    var masterLut = master ? interp(master, new Float64Array(CURVE_LUT_SIZE)) : null;
    if (masterLut) any = true;

    chans.forEach(function (ch) {
      var p = pts(ch);
      var lut = null;
      if (p) { lut = interp(p, new Float64Array(CURVE_LUT_SIZE)); any = true; }
      if (masterLut) {
        var composed = new Float64Array(CURVE_LUT_SIZE);
        for (var i = 0; i < CURVE_LUT_SIZE; i++) {
          // ffmpeg indexes the master table with the channel table's quantised
          // 16 bit output, so the intermediate really is rounded here.
          var mid = lut ? Math.round(lut[i] * CURVE_SCALE) : i;
          composed[i] = masterLut[mid];
        }
        lut = composed;
      }
      out[ch] = lut;
    });
    return any ? out : null;
  }

  // ------------------------------------------------------------------
  // ffmpeg gblur, ported exactly (libavfilter/vf_gblur.c, set_params)
  // ------------------------------------------------------------------

  function gblurParams(sigma, steps) {
    steps = steps || 1;
    var lambda = (sigma * sigma) / (2.0 * steps);
    var dnu = (1.0 + 2.0 * lambda - Math.sqrt(1.0 + 4.0 * lambda)) / (2.0 * lambda);
    var postscale = Math.pow(dnu / lambda, steps);
    var boundaryscale = 1.0 / (1.0 - dnu);
    if (!isFinite(postscale) || postscale === 0) postscale = 1;
    if (!isFinite(boundaryscale) || boundaryscale === 0) boundaryscale = 1;
    if (!isFinite(dnu)) dnu = 0;
    return { nu: dnu, postscale: postscale, boundaryscale: boundaryscale, steps: steps };
  }

  // ------------------------------------------------------------------
  // swscale resampling filters, ported from libswscale/utils.c initFilter
  // ------------------------------------------------------------------

  /* Build the separable resampling filter swscale builds for one axis.
   *
   * The halation and bloom branches scale down with `flags=bilinear` and back
   * up with `flags=bicubic`. swscale's "bilinear" is not a two tap tent: on a
   * downscale it widens the tent by the scale factor, so a 4x reduction is a
   * nine tap filter. Guessing a mipmap-style box average here would be wrong
   * by a visible amount on a hard highlight edge, which is exactly where
   * halation lives.
   *
   * Returns { pos: Int32Array(dstW), taps: Float32Array(dstW * size), size }.
   * Coefficients are normalised to sum to one in float; ffmpeg quantises them
   * to int16 with error diffusion, which is a relative error near 1/16384 and
   * far under a code at 8 bit. */
  function swsFilter(srcW, dstW, kind) {
    var one = 1 << 30;
    var xInc = Math.floor(((srcW * 65536) + (dstW >> 1)) / dstW);
    var srcPos = 128, dstPos = 128;    // get_local_pos(c, 0, 0, dir) for luma
    var i, j;

    if (Math.abs(xInc - 0x10000) < 10 && srcPos === dstPos) {
      // unscaled: swscale short circuits to a single unit tap
      var pos1 = new Int32Array(dstW), taps1 = new Float32Array(dstW);
      for (i = 0; i < dstW; i++) { pos1[i] = i; taps1[i] = 1; }
      return { pos: pos1, taps: taps1, size: 1 };
    }

    var sizeFactor = kind === "bicubic" ? 4 : 2;
    var filterSize;
    if (xInc <= (1 << 16)) filterSize = 1 + sizeFactor;
    else filterSize = 1 + Math.floor((sizeFactor * srcW + dstW - 1) / dstW);
    filterSize = Math.min(filterSize, srcW - 2);
    filterSize = Math.max(filterSize, 1);

    var filter = new Float64Array(dstW * filterSize);
    var pos = new Int32Array(dstW);
    // xDstInSrc counts in 1/2^17 of a source pixel; the centre of output i is
    // xDstInSrc / 2^17.
    var xDstInSrc = Math.floor((dstPos * xInc) / 128) - Math.floor((srcPos * 65536) / 128);

    for (i = 0; i < dstW; i++) {
      var xx = trunc((xDstInSrc - (filterSize - 2) * 65536) / 131072);
      pos[i] = xx;
      for (j = 0; j < filterSize; j++) {
        var d = Math.abs(xx * 131072 - xDstInSrc) * 8192;
        if (xInc > (1 << 16)) d = trunc(d * dstW / srcW);
        var coeff;
        if (kind === "bicubic") {
          // B = 0, C = 0.6, ffmpeg's default cubic parameters
          var B = 0, C = 0.6 * (1 << 24);
          if (d >= 2147483648) {
            coeff = 0;
          } else {
            var dd = Math.floor((d * d) / one);
            var ddd = Math.floor((dd * d) / one);
            if (d < one) {
              coeff = (12 * (1 << 24) - 9 * B - 6 * C) * ddd
                    + (-18 * (1 << 24) + 12 * B + 6 * C) * dd
                    + (6 * (1 << 24) - 2 * B) * one;
            } else {
              coeff = (-B - 6 * C) * ddd + (6 * B + 30 * C) * dd
                    + (-12 * B - 48 * C) * d + (8 * B + 24 * C) * one;
            }
            coeff = coeff / (Math.pow(2, 54) / one);
          }
        } else {
          coeff = one - d;
          if (coeff < 0) coeff = 0;
        }
        filter[i * filterSize + j] = coeff;
        xx++;
      }
      xDstInSrc += 2 * xInc;
    }

    // Border handling: taps that fall outside the source are folded into the
    // nearest edge tap, which is why a swscale edge is a clamp and not black.
    for (i = 0; i < dstW; i++) {
      if (pos[i] < 0) {
        for (j = 1; j < filterSize; j++) {
          var left = Math.max(j + pos[i], 0);
          filter[i * filterSize + left] += filter[i * filterSize + j];
          filter[i * filterSize + j] = 0;
        }
        pos[i] = 0;
      }
      if (pos[i] + filterSize > srcW) {
        var shift = pos[i] + Math.min(filterSize - srcW, 0);
        var acc = 0;
        for (j = filterSize - 1; j >= 0; j--) {
          if (pos[i] + j >= srcW) { acc += filter[i * filterSize + j]; filter[i * filterSize + j] = 0; }
        }
        for (j = filterSize - 1; j >= 0; j--) {
          filter[i * filterSize + j] = (j < shift) ? 0 : filter[i * filterSize + j - shift];
        }
        pos[i] -= shift;
        filter[i * filterSize + srcW - 1 - pos[i]] += acc;
      }
    }

    var taps = new Float32Array(dstW * filterSize);
    for (i = 0; i < dstW; i++) {
      var sum = 0;
      for (j = 0; j < filterSize; j++) sum += filter[i * filterSize + j];
      if (!sum) sum = 1;
      for (j = 0; j < filterSize; j++) taps[i * filterSize + j] = filter[i * filterSize + j] / sum;
    }
    return { pos: pos, taps: taps, size: filterSize };
  }

  function trunc(v) { return v < 0 ? Math.ceil(v) : Math.floor(v); }

  // ------------------------------------------------------------------
  // what the chain actually does, stage by stage, for a given config
  // ------------------------------------------------------------------

  /* ffmpeg picks the pixel format for a filter chain by negotiation, not by
   * what the graph string says, and two filters cinegrade uses cannot run in
   * the gbrp16le the rest of the chain is in:
   *
   *   vignette  supports 8 bit only (yuv444p, rgb24, gray8), so the WHOLE tail
   *             from the vignette onwards is quantised to 8 bit.
   *   unsharp   supports YUV only, so a sharpen forces an RGB to YUV round
   *             trip and sharpens Y alone, leaving U and V untouched.
   *
   * Verified by reading the auto_scale insertions ffmpeg logs at -v verbose on
   * the real studio preview graph, not by reading the pix_fmts lists. The
   * table below is that observation. */
  function chainPlan(cfg) {
    var vignette = !!cfg.fx.vignette.enabled;
    var soften = +cfg.detail.soften > 1e-6;
    var sharpen = +cfg.detail.sharpen > 1e-6;
    var midDetail = Math.abs(+cfg.detail.mid_detail) > 1e-6;
    var detail = soften || sharpen || midDetail;
    var dn = (cfg.prep && cfg.prep.denoise) || {};
    var denoiseActive = !!dn.enabled
      && (+dn.spatial > 1e-6 || +dn.temporal > 1e-6);
    var plan = {
      vignette: vignette, soften: soften, sharpen: sharpen,
      midDetail: midDetail, detail: detail, denoise: denoiseActive,
      grain: !!cfg.grain.enabled,
      // where the picture stops being 16 bit RGB
      quantise: "none",   // none | rgb8 | yuv8 | yuv16
      quantiseAt: null    // "vignette" | "detail"
    };
    if (vignette) {
      plan.quantise = detail ? "yuv8" : "rgb8";
      plan.quantiseAt = "vignette";
    } else if (sharpen) {
      plan.quantise = "yuv16";
      plan.quantiseAt = "detail";
    }
    return plan;
  }

  /* The layer stack, ported from cinegrade.py's config_layers, layer_active,
   * layer_window, layer_branches and layer_blur_sigma.
   *
   * Every one of these decides what the ENGINE would put in the graph, not
   * what looks reasonable here. A layer the engine drops (disabled, or a mask
   * whose matte is zero everywhere) has to be dropped here too, or the
   * preview shows a correction the render does not have. */
  function windowBlock(win) {
    return deepMerge(WINDOW_DEFAULTS, win || {});
  }

  function configLayers(cfg) {
    var list = (cfg && cfg.layers) || [];
    var out = [];
    for (var i = 0; i < list.length; i++) {
      out.push(deepMerge(LAYER_DEFAULTS, list[i] || {}));
    }
    return out;
  }

  /* True when the layer has anything to contribute.
   *
   * A disabled layer is absent, and so is a layer whose combined matte is
   * zero everywhere: the matte of a layer with no mask is 1, mask.invert
   * makes that 0, and a correction merged under a matte of 0 is the picture. */
  function layerActive(layer) {
    if (!layer.enabled) return false;
    var m = layer.mask;
    if (m.invert && !m.window.enabled && !m.key.enabled) return false;
    return true;
  }

  /* The window matte this layer needs, with mask.invert folded in, or null.
   *
   * null means no spatial matte at all, which is what keeps a key only or a
   * global layer down to a single cube pass. The combined matte is
   * window * key, inverted as a whole by mask.invert. A product cannot be
   * inverted inside either half, but it does factor:
   * 1 - w*k = (1 - w) * 1 + w * (1 - k). With the key OFF that collapses to
   * 1 - w, which is the window with its own invert flipped, so one branch is
   * enough; with the key ON as well, layerBranches grades two branches and
   * the window matte itself is left alone. */
  function layerWindow(layer) {
    var m = layer.mask, win = m.window;
    if (!win.enabled) return null;
    if (m.invert && !m.key.enabled) {
      return deepMerge(win, { invert: !win.invert });
    }
    return windowBlock(win);
  }

  /* Which baked cubes this layer needs, in merge input order.
   *
   * One entry is one graded branch, merged against the untouched picture
   * under the window matte. Two entries is mask.invert with BOTH a window and
   * a key: branch A carries the full correction and wins where the window is
   * closed, branch B carries the correction under the inverted key and wins
   * where it is open, which is the factorisation above, exactly. */
  function layerBranches(layer) {
    var m = layer.mask;
    if (!m.invert) return [m.key.enabled ? "key" : "one"];
    if (m.window.enabled && m.key.enabled) return ["one", "inv"];
    return m.window.enabled ? ["one"] : ["inv"];
  }

  /* The layer's blur sigma in pixels at THIS render's width. */
  function layerBlurSigma(layer, width) {
    var sigma = +layer.correct.blur;
    if (!(sigma > 0)) return 0;
    return sigma * width / LAYER_BLUR_REF_WIDTH;
  }

  /* The active layers at one placement point, each with its array index, so a
   * LUT slot name can be built from the index the config really uses rather
   * than from a running counter. */
  function activeLayers(cfg, placement) {
    var all = configLayers(cfg), out = [];
    for (var i = 0; i < all.length; i++) {
      if (layerActive(all[i]) && all[i].placement === placement) {
        out.push({ index: i, layer: all[i] });
      }
    }
    return out;
  }

  function lookActive(cfg) {
    return !!cfg.look.lut
      || (!!cfg.look.lut2 && +(cfg.look.balance || 0) > 1e-6);
  }

  /* The two blocks a config written before layers existed carried. Kept only
   * as the merge base migrateLayers fills a partial block in from; nothing
   * else reads them. */
  var LEGACY_SECONDARY = {
    enabled: false, show_mask: false, invert: false,
    hue_center: 30.0, hue_width: 40.0, hue_soft: 15.0,
    sat_low: 0.10, sat_high: 1.0, sat_soft: 0.10,
    lum_low: 0.0, lum_high: 1.0, lum_soft: 0.10,
    hue_shift: 0.0, sat_gain: 1.0, lum_gain: 1.0,
    tint: [0.0, 0.0, 0.0], strength: 1.0
  };
  var LEGACY_WINDOW = {
    enabled: false, shape: "ellipse",
    cx: 0.5, cy: 0.5, w: 0.6, h: 0.6,
    rotation: 0.0, softness: 0.15, invert: false
  };

  /* Rewrite a pre-layers config into the layers shape, on read.
   *
   * A config carrying `secondary` and/or `window` and no `layers` becomes one
   * layer: the qualifier moves to mask.key, the shape to mask.window,
   * show_mask to mask.show, the HSV controls and the tint push and the
   * strength to correct, and the layer is enabled exactly when the SECONDARY
   * was. That last detail is the whole of the old window_active rule: a
   * window switched on over a secondary switched off rendered nothing at all,
   * because a shape with nothing to gate was dropped from the graph.
   *
   * Byte for byte the same as cinegrade.migrate_layers, which is what lets a
   * preset load into the studio and the CLI and produce one picture. */
  function migrateLayers(cfg) {
    if (!cfg || (!cfg.secondary && !cfg.window)) return cfg || {};
    var out = {}, k;
    for (k in cfg) {
      if (!Object.prototype.hasOwnProperty.call(cfg, k)) continue;
      if (k === "secondary" || k === "window") continue;
      out[k] = cfg[k];
    }
    // A config that already has a stack keeps it: the two old blocks are
    // dropped rather than turned into a second, duplicate layer.
    if (cfg.layers) return out;
    var sec = deepMerge(LEGACY_SECONDARY, cfg.secondary || {});
    var win = deepMerge(LEGACY_WINDOW, cfg.window || {});
    var layer = clone(LAYER_DEFAULTS);
    layer.enabled = !!sec.enabled;
    layer.placement = "before_look";
    layer.mask.show = !!sec.show_mask;
    layer.mask.invert = false;
    layer.mask.window = win;
    layer.mask.key = {
      enabled: !!sec.enabled, invert: !!sec.invert,
      hue_center: sec.hue_center, hue_width: sec.hue_width,
      hue_soft: sec.hue_soft,
      sat_low: sec.sat_low, sat_high: sec.sat_high, sat_soft: sec.sat_soft,
      lum_low: sec.lum_low, lum_high: sec.lum_high, lum_soft: sec.lum_soft
    };
    layer.correct.hue_shift = sec.hue_shift;
    layer.correct.sat_gain = sec.sat_gain;
    layer.correct.lum_gain = sec.lum_gain;
    layer.correct.offset = sec.tint;
    layer.correct.strength = sec.strength;
    out.layers = [layer];
    return out;
  }

  /* Python's float(f"{x:.Nf}"), which is how cinegrade rounds the geometry.
   *
   * The engine passes every window constant through a decimal round trip so
   * that the double numpy evaluates is bit for bit the double ffmpeg parses
   * back out of the geq string. Doing the same round trip here means the
   * shader starts from the same constants both references start from instead
   * of from full precision numbers that could round a boundary pixel the
   * other way. toFixed and Python's format differ only on an exact decimal
   * tie, which needs a dyadic input landing exactly on a half at the tenth
   * decimal place; none of the shipped shapes can produce one. */
  function qdec(x, places) { return parseFloat((+x).toFixed(places)); }

  /* Fractions in, pixels out, rounded once. Mirrors _window_geometry. */
  function windowGeometry(win, width, height) {
    var soft = Math.max(0, +win.softness);
    var r = (+win.rotation) * Math.PI / 180;
    var g = {
      rect: String(win.shape) === "rect",
      invert: !!win.invert,
      soft: soft,
      cr: qdec(Math.cos(r), 12),
      sr: qdec(Math.sin(r), 12),
      cxp: qdec(+win.cx * width, 10),
      cyp: qdec(+win.cy * height, 10),
      // A half axis is clamped to one pixel, so a zero width window is a one
      // pixel line rather than a divide by zero.
      ax: qdec(Math.max(+win.w * width / 2, 1), 10),
      ay: qdec(Math.max(+win.h * height / 2, 1), 10)
    };
    // The feather edges, precomputed for the same reason: one rounding, shared.
    g.hi = qdec(1 + soft, 10);
    g.den = soft > 0 ? qdec(2 * soft, 10) : 0;
    return g;
  }

  var STAGE_NOTES = {
    prep_denoise: "NOT PORTED. hqdn3d has no WebGL2 equivalent here, so an "
                + "enabled denoise always falls back to the ffmpeg-rendered "
                + "still, the same way grain falls back today, except this "
                + "one is NOT exempted from blocking the GPU preview: it "
                + "changes the picture materially (see limits.js), so a "
                + "silent GPU preview that ignored it would be worse than no "
                + "preview at all.",
    log: "Exposure and white balance as a log domain offset, one clamped add "
       + "per channel. Pure arithmetic, no resampling, no quantisation.",
    cst_in: "3D LUT, real tetrahedral interpolation on texelFetch of the eight "
          + "surrounding grid points. Hardware trilinear filtering is NOT used: "
          + "it gives a different answer on a 65 cube.",
    primaries: "Lift/gain, contrast, brightness, gamma, the luma preserving "
             + "saturation matrix, vibrance (ported from vf_vibrance.c) and the "
             + "black lift / highlight rolloff toe and shoulder, which are the "
             + "engine's own explicit points read back through ffmpeg's pchip "
             + "spline, rounded to 4 decimals the way the engine writes them.",
    cst_out: "3D LUT, tetrahedral, same as CST IN.",
    curves: "ffmpeg's own 65536 entry table, built here with the same natural "
          + "and pchip code paths, composed in ffmpeg's order (channel first, "
          + "then master).",
    slice: "Hue curves, Color Slice and Tetra bake to one 33 cube on the "
         + "server, from the same grade/slice.py the render calls, and the GPU "
         + "applies it tetrahedrally. Nothing about the colour maths is "
         + "reimplemented here, so the preview cannot drift from the render.",
    layers: "One masked correction. The server bakes the colour half (the "
          + "key and every colour control) to a 33 cube with the same "
          + "cinegrade.layer_lut the render calls, and the GPU applies it "
          + "tetrahedrally, so this is the same table ffmpeg reads. The power "
          + "window is the engine's own matte formula evaluated per pixel in "
          + "the shader rather than baked to a PNG, quantised the same way "
          + "with floor(m*255+0.5) and scaled into the 16 bit merge by 257, "
          + "then merged against the un-graded branch exactly as maskedmerge "
          + "does. The matte is 8 bit in both paths. Layers are applied one "
          + "after another in array order, each as its own pass, so a stack "
          + "of any length costs passes but no accuracy: every pass lands on "
          + "the same 16 bit lattice the fused chain lands on.",
    look: "3D LUT, tetrahedral, then blend=normal, which is dst = lut*mix + "
        + "pre_lut*(1-mix). A second slot blends in PARALLEL: both slots read "
        + "the same pre-look signal, then balance crossfades slot 1's result "
        + "against slot 2's the same truncated way. Slot 2 off (no lut2, or "
        + "balance 0) is byte identical to the single-slot path above.",
    halation: "Highlight pass, swscale bilinear downscale to a quarter, gblur, "
            + "swscale bicubic upscale, tint, then screen at strength. The "
            + "gblur is ffmpeg's IIR recursion, not a convolution.",
    bloom: "Same structure as halation at an eighth resolution.",
    radial_blur: "gblur plus maskedmerge against a radial ramp. The ramp is "
               + "generated by ffmpeg as an 8 bit PNG, so it is an 8 bit matte "
               + "in the real path too, quantised here with floor(255*ramp) to "
               + "match. It scales into the 16 bit merge by 257, the same hop "
               + "the window matte takes, so a fully open ramp applies the "
               + "whole blur.",
    rgb_split: "Whole pixel channel shift with edge smear. The engine rounds "
             + "the amount to an integer, so this is exact by construction.",
    vignette: "cos^4 falloff with ffmpeg's LCG dither. This is where the chain "
            + "drops to 8 bit, because vignette has no 16 bit pixel format. On "
            + "its own it measures exact: no channel of any pixel off by more "
            + "than 1.",
    grain: "Overlay blend against a plate the server renders (the "
         + "/api/grain/plate route, a slice of the same build_graph text "
         + "ffmpeg's own grain block runs, so the two can never drift "
         + "apart), plus an optional response=film weight computed the "
         + "same colorchannelmixer plus smoothstep formula cinegrade.py's "
         + "blend all_expr does. A stock preset is three numbers (size, "
         + "strength, softness measured on a real frame), not a scan of "
         + "real film.",
    detail: "gblur then unsharp then mid_detail. unsharp is YUV only, so the "
          + "picture takes an RGB to YUV round trip and only Y is sharpened. "
          + "Sharpen on its own runs at 16 bit, where ffmpeg's own scaling "
          + "makes it 256 times weaker, so that lane barely moves the "
          + "picture and measures exact. mid_detail is a plain gblur plus a "
          + "blend=all_expr, both format-agnostic in ffmpeg, so it runs "
          + "wherever the chain already is (16 bit RGB, or YUV if sharpen or "
          + "vignette already forced that) with no format change of its own, "
          + "and applies the SAME formula to every plane, which is why it "
          + "also touches chroma when it happens to land in YUV. Pushed "
          + "positive on a real hard edge it does overshoot the valid range, "
          + "so both engines clamp the finished expression to that range at "
          + "the same point: this shader clamps to 0..uMax and the engine "
          + "wraps its blend in clip(...,0,65535). ffmpeg's blend does not "
          + "clamp by itself, it wraps modulo 65536, which is what the "
          + "mid_detail parity rows were catching until 2026-09-05. All "
          + "three mid_detail rows now measure exact at both widths.",
    letterbox: "Crop to an even height and pad back with black."
  };

  /* The honesty mechanism.
   *
   * Returns one row per pipeline stage with a status a caller can act on:
   *   exact       indistinguishable from ffmpeg at 8 bit
   *   close       ported, with a known and measured approximation
   *   unsupported cannot be reproduced, fall back to the ffmpeg path
   *   off         not enabled by this config
   * `overall` is the worst status among the ACTIVE stages, so a caller can
   * show one indicator without walking the list. */
  function stageReport(config) {
    var cfg = fullConfig(config);
    var plan = chainPlan(cfg);
    var p = cfg.primaries, cv = cfg.convert;
    var rows = [];

    function add(id, name, active, status, extra) {
      rows.push({
        id: id, name: name, active: !!active,
        status: active ? status : "off",
        note: STAGE_NOTES[id] + (extra ? " " + extra : "")
      });
    }

    add("prep_denoise", "Prep denoise", plan.denoise, "unsupported");

    var offR = fmt(+cv.exposure + +p.temperature, 12);
    var offG = fmt(+cv.exposure + +p.tint, 12);
    var offB = fmt(+cv.exposure - +p.temperature, 12);
    add("log", "Log (exposure, white balance)",
        Math.abs(offR) > 1e-6 || Math.abs(offG) > 1e-6 || Math.abs(offB) > 1e-6, "exact");

    add("cst_in", "CST in", cv.working_space === "dwg", "exact");

    var lift = triplet(p.lift, 0), gain = triplet(p.gain, 1), bright = triplet(p.brightness, 0);
    var gamma = triplet(p.gamma, 1);
    var primActive = lift.some(nz) || gain.some(function (v) { return Math.abs(v - 1) > 1e-6; })
      || Math.abs(+p.contrast - 1) > 1e-6 || bright.some(nz)
      || gamma.some(function (v) { return Math.abs(v - 1) > 1e-6; })
      || Math.abs(+p.saturation - 1) > 1e-6 || Math.abs(+p.vibrance) > 1e-6
      || Math.abs(+p.black_lift) > 1e-6 || Math.abs(+p.highlight_rolloff) > 1e-6;
    add("primaries", "Primaries", primActive, "exact");

    add("cst_out", "CST out", true, "exact");
    add("curves", "Curves", curvesActive(cfg), "exact",
        cfg.curves && cfg.curves.enabled
          ? "The line drawn in the curve editor is the same spline: "
          + "controls.js Ctl.pchipEval now uses ffmpeg's end slope, measured "
          + "within 1/65535 of the filter on a 65536 entry ramp."
          : "");
    add("slice", "Hue curves, Slice, Tetra", sliceActive(cfg), "exact");
    /* One row per layer, not one row for the stage. A stack is a list of
     * independent corrections and "layers: exact" would say nothing about
     * which one the user is looking at, so each row names its placement and
     * what its mask is made of. A config with no layers still gets a single
     * inactive row, so the report has a stable shape. */
    var lys = configLayers(cfg);
    if (!lys.length) {
      add("layers", "Layers", false, "exact");
    } else {
      lys.forEach(function (L, li) {
        var bits = [];
        if (L.mask.window.enabled) bits.push("power window");
        if (L.mask.key.enabled) bits.push("colour key");
        if (!bits.length) bits.push("no mask, so the whole frame");
        if (L.mask.invert) bits.push("mask inverted");
        if (+L.correct.blur > 0) bits.push("blur " + fmt(+L.correct.blur, 3));
        if (L.mask.show) bits.push("matte view");
        var on = layerActive(L);
        rows.push({
          id: "layer" + li,
          name: "Layer " + (li + 1) + (L.name ? " (" + L.name + ")" : ""),
          active: on, status: on ? "exact" : "off",
          note: STAGE_NOTES.layers + " This one runs " + L.placement
              + ": " + bits.join(", ") + "."
        });
      });
    }
    add("look", "Look",
        !!cfg.look.lut || (!!cfg.look.lut2 && +(cfg.look.balance || 0) > 1e-6),
        "exact");

    /* These three were expected to be approximations because they involve
     * swscale resampling and ffmpeg's IIR blur. They measure exact (no channel
     * of any pixel off by more than 1), so that is what they claim. */
    add("halation", "FX halation", !!cfg.fx.halation.enabled, "exact");
    add("bloom", "FX bloom", !!cfg.fx.bloom.enabled, "exact");
    add("radial_blur", "FX radial blur", !!cfg.fx.radial_blur.enabled, "exact");
    add("rgb_split", "FX rgb split",
        !!cfg.fx.rgb_split.enabled && Math.round(+cfg.fx.rgb_split.amount) !== 0, "exact");
    add("vignette", "FX vignette", plan.vignette,
        plan.quantise === "yuv8" ? "close" : "exact",
        plan.quantise === "yuv8"
          ? "This config also has detail on, so ffmpeg runs the whole tail in 8 "
          + "bit yuv444p (the chroma centre it uses there is 127, not 128). That "
          + "lane is the only one that does not measure exact. swscale adds an "
          + "8x8 ordered dither on the way in, which turns this preview's last "
          + "bit float difference into a whole 8 bit code on roughly half a "
          + "percent of pixels, and the sharpen then multiplies it. Measured on "
          + "the shipped presets: at most 4 code values out, 0.2 to 0.4 percent "
          + "of channels off by more than 1, none off by more than 4."
          : "");
    add("grain", "Grain", plan.grain, "exact",
        "Measured exact on a rendered still across grain defaults, the "
      + "35mm stock, response=film and color=1 (parity.js, 8 rows at 640 "
      + "and 1280 wide): mean channel difference 0.006 of 255, max 1 code "
      + "value, 0 percent of channels over 1. That is for a STILL: the "
      + "plate this preview fetches is one frame's worth (ffmpeg's noise "
      + "generator's own frame 0, cached per config and size), not one "
      + "plate per output frame the way a real multi frame export "
      + "advances it, so a looping or playing preview reuses the same "
      + "plate on every frame while a real export's grain keeps changing. "
      + "See the Limits panel.");
    add("detail", "Detail", plan.detail,
        plan.quantise === "yuv8" ? "close" : "exact",
        plan.quantise === "yuv8"
          ? "Running next to a vignette puts this in the 8 bit yuv lane, which "
          + "is the one lane that does not measure exact. The sharpen then "
          + "multiplies the difference: measured at most 4 code values out."
          : plan.sharpen && plan.quantise === "yuv16"
          ? "ffmpeg's unsharp shifts by 8+depth bits but scales amount by 65536 "
          + "regardless, so at 16 bit the sharpen is 256 times weaker than the "
          + "same number at 8 bit. That is the engine's behaviour and it is what "
          + "the GPU reproduces."
          : plan.midDetail
          ? "mid_detail does overshoot the valid range at a real hard edge, "
          + "and both engines now clamp the finished expression the same way: "
          + "this shader to 0..uMax, the engine with clip(...,0,65535) inside "
          + "its blend. Measured on the parity clip at mid_detail 0.5, 1.0 "
          + "and -0.5, at 640 and 1280 wide: max 1 code value, 0 percent of "
          + "channel samples off by more than 1, all six rows exact."
          : "");
    add("letterbox", "Letterbox", !!cfg.letterbox.enabled, "exact");

    var order = { exact: 0, close: 1, unsupported: 2, off: -1 };
    var overall = "exact", unsupported = [], close = [];
    rows.forEach(function (r) {
      if (!r.active) return;
      if (r.status === "unsupported") unsupported.push(r.id);
      if (r.status === "close") close.push(r.id);
      if (order[r.status] > order[overall]) overall = r.status;
    });

    return {
      stages: rows, overall: overall,
      unsupported: unsupported, approximate: close,
      canRender: unsupported.length === 0,
      quantise: plan.quantise,
      summary: unsupported.length
        ? "Falls back to ffmpeg: " + unsupported.join(", ")
        : (close.length ? "Approximate in: " + close.join(", ") : "Exact")
    };
  }

  function nz(v) { return Math.abs(v) > 1e-6; }

  /* The server scales pixel denominated parameters when it renders a preview
   * smaller than the source. A GPU render of the same preview has to use the
   * same scaled numbers or the glow it shows is the wrong size. Mirrors
   * studio/server.py scale_for_preview. */
  function scaleForPreview(cfg, factor) {
    if (factor >= 0.999) return fullConfig(cfg);
    var out = fullConfig(cfg);
    out.fx.halation.sigma = +out.fx.halation.sigma * factor;
    out.fx.bloom.sigma = +out.fx.bloom.sigma * factor;
    out.fx.radial_blur.sigma = +out.fx.radial_blur.sigma * factor;
    /* The engine refuses to let a real channel split scale away to nothing in
     * a small preview, so anything the user can see (0.5 of a pixel or more)
     * is floored at one whole pixel instead of rounding to zero. */
    var split = +out.fx.rgb_split.amount;
    out.fx.rgb_split.amount =
      split >= 0.5 ? Math.max(1.0, split * factor) : split * factor;
    out.detail.soften = +out.detail.soften * factor;
    out.grain.size = Math.max(1, Math.round(+out.grain.size * factor));
    // detail.mid_detail is deliberately NOT scaled here: its sigma is defined
    // as a FRACTION of the actual rendered width (2%), not a fixed pixel
    // count like soften/halation/bloom/radial_blur above, so detailPass
    // already computes the right radius from the real source width at
    // render time. Scaling it again here would double-correct it.
    return out;
  }

  // ------------------------------------------------------------------
  // GLSL
  // ------------------------------------------------------------------

  function src(lines) { return lines.join("\n") + "\n"; }

  // One triangle bigger than the viewport. Cheaper than a quad and it has no
  // diagonal seam, which matters when a pass reads its own neighbours.
  var VERT = src([
    "#version 300 es",
    "void main() {",
    "  vec2 p = vec2((gl_VertexID << 1) & 2, gl_VertexID & 2);",
    "  gl_Position = vec4(p * 2.0 - 1.0, 0.0, 1.0);",
    "}"
  ]);

  var LIB_LUT3D = [
    "vec3 lutAt(sampler3D t, int ri, int gi, int bi) {",
    // ffmpeg stores the cube as lut[r*N*N + g*N + b], so the 3D texture is
    // uploaded with blue on x and red on z. Fetching in the wrong order is a
    // silent red/blue swap that only shows on asymmetric LUTs.
    "  return texelFetch(t, ivec3(bi, gi, ri), 0).rgb;",
    "}",
    "vec3 tetra(sampler3D t, int n, vec3 c) {",
    "  float lm = float(n - 1);",
    "  vec3 s = clamp(c * lm, vec3(0.0), vec3(lm));",
    "  ivec3 pv = ivec3(s);",
    "  ivec3 nx = min(pv + ivec3(1), ivec3(n - 1));",
    "  vec3 d = s - vec3(pv);",
    "  vec3 c000 = lutAt(t, pv.r, pv.g, pv.b);",
    "  vec3 c111 = lutAt(t, nx.r, nx.g, nx.b);",
    "  if (d.r > d.g) {",
    "    if (d.g > d.b) {",
    "      vec3 c100 = lutAt(t, nx.r, pv.g, pv.b);",
    "      vec3 c110 = lutAt(t, nx.r, nx.g, pv.b);",
    "      return (1.0-d.r)*c000 + (d.r-d.g)*c100 + (d.g-d.b)*c110 + d.b*c111;",
    "    } else if (d.r > d.b) {",
    "      vec3 c100 = lutAt(t, nx.r, pv.g, pv.b);",
    "      vec3 c101 = lutAt(t, nx.r, pv.g, nx.b);",
    "      return (1.0-d.r)*c000 + (d.r-d.b)*c100 + (d.b-d.g)*c101 + d.g*c111;",
    "    }",
    "    vec3 c001 = lutAt(t, pv.r, pv.g, nx.b);",
    "    vec3 c101 = lutAt(t, nx.r, pv.g, nx.b);",
    "    return (1.0-d.b)*c000 + (d.b-d.r)*c001 + (d.r-d.g)*c101 + d.g*c111;",
    "  }",
    "  if (d.b > d.g) {",
    "    vec3 c001 = lutAt(t, pv.r, pv.g, nx.b);",
    "    vec3 c011 = lutAt(t, pv.r, nx.g, nx.b);",
    "    return (1.0-d.b)*c000 + (d.b-d.g)*c001 + (d.g-d.r)*c011 + d.r*c111;",
    "  } else if (d.b > d.r) {",
    "    vec3 c010 = lutAt(t, pv.r, nx.g, pv.b);",
    "    vec3 c011 = lutAt(t, pv.r, nx.g, nx.b);",
    "    return (1.0-d.g)*c000 + (d.g-d.b)*c010 + (d.b-d.r)*c011 + d.r*c111;",
    "  }",
    "  vec3 c010 = lutAt(t, pv.r, nx.g, pv.b);",
    "  vec3 c110 = lutAt(t, nx.r, nx.g, pv.b);",
    "  return (1.0-d.g)*c000 + (d.g-d.r)*c010 + (d.r-d.b)*c110 + d.b*c111;",
    "}"
  ];

  /* The whole colour head in one pass: log, CST in, primaries, CST out,
   * curves, the slice cube and the look mix.
   *
   * The layer stack is NOT here. A stack can be any length and each layer can
   * need a window matte and a blur, neither of which fits a fused per-pixel
   * pass, so layers run as their own passes and this shader is invoked twice
   * when there are any: once with the look held back (opts.lookOff) for the
   * head, and once with everything but the look held back (opts.lookOnly)
   * after the before_look layers have run. Every stage is already guarded by
   * its own uniform, so the second invocation executes only the look block
   * and the split is arithmetically identical to the fused pass: the render
   * targets are RGBA32F, so a value handed from one pass to the next comes
   * back bit for bit.
   *
   * Every `lut` stage here is written the way vf_lut BEHAVES on gbrp16le,
   * which is not what the filter string reads like. vf_lut's `maxval` for
   * that pixel format is 65280, not 65535, and it stores av_clip((int)res, 0,
   * 65280). So any lut anywhere in the chain crushes the top 255 codes and
   * pure white comes back as 254 at eight bit, not 255. Measured, not
   * assumed: a full 16 bit ramp through format=gbrp16le,lut=r='val' comes
   * back clipped at 65280.
   *
   * The per stage quantisation is deliberate too. Each of these filters
   * writes integer codes, and pinning the float chain to the same lattice is
   * what stops a fifteen stage chain from drifting. lut, lut3d and vibrance
   * all truncate; colorchannelmixer sums pre-rounded integer products. */
  var FS_COLOR = src([
    "#version 300 es",
    "precision highp float;",
    "precision highp int;",
    "precision highp sampler3D;",
    "uniform sampler2D uSrc;",
    "uniform sampler3D uCstIn;",
    "uniform sampler3D uCstOut;",
    "uniform sampler3D uSlice;",
    "uniform sampler3D uLook;",
    "uniform sampler3D uLook2;",
    "uniform sampler2D uCurve;",
    "uniform sampler2D uBlc;",
    "uniform ivec4 uSizes;",          // cstIn, cstOut, spare, look; 0 = off
    "uniform int uSliceSize;",       // hue curves + slice + tetra cube; 0 = off
    "uniform int uLook2Size;",        // second look slot; 0 = off, ivec4 is full
    "uniform int uHasCurve;",
    "uniform int uHasBlc;",
    "uniform float uLookMix;",
    "uniform float uLookMix2;",
    "uniform float uLookBalance;",
    "uniform vec3 uLogOff;",
    "uniform int uHasLog;",
    "uniform vec3 uLiftA;",
    "uniform vec3 uLiftB;",
    "uniform int uHasLift;",
    "uniform vec2 uContrast;",        // (contrast, pivot in normalised units)
    "uniform int uHasContrast;",
    "uniform vec3 uBright;",
    "uniform int uHasBright;",
    "uniform vec3 uInvGamma;",
    "uniform int uHasGamma;",
    "uniform vec3 uSatR;",
    "uniform vec3 uSatG;",
    "uniform vec3 uSatB;",
    "uniform int uHasSat;",
    "uniform float uVib;",
    "uniform int uHasVib;",
    "out vec4 oCol;"
  ].concat(LIB_LUT3D).concat([
    "const float CODE = 65535.0;",
    "const float LUTMAX = 65280.0;",
    "const float LM = LUTMAX / CODE;",
    "vec3 lutq(vec3 v) { return clamp(floor(v * CODE), 0.0, LUTMAX) / CODE; }",
    "vec3 q16(vec3 v) { return clamp(floor(v * CODE), 0.0, CODE) / CODE; }",
    "float curve1(vec3 v, int ch) {",
    "  int idx = clamp(int(v[ch] * CODE + 0.5), 0, 65535);",
    "  return texelFetch(uCurve, ivec2(idx & 255, idx >> 8), 0)[ch];",
    "}",
    "float blcAt(float v) {",
    "  int idx = clamp(int(v * CODE + 0.5), 0, 65535);",
    "  return texelFetch(uBlc, ivec2(idx & 255, idx >> 8), 0).r;",
    "}",
    // colorchannelmixer rounds each coefficient times each input code to an
    // integer FIRST and then sums, so a float matrix multiply is not the same
    // arithmetic. roundEven matches lrint under the default rounding mode.
    "float mixrow(vec3 code, vec3 k) {",
    "  vec3 t = roundEven(code * k);",
    "  return t.r + t.g + t.b;",
    "}",
    "void main() {",
    "  ivec2 p = ivec2(gl_FragCoord.xy);",
    "  vec3 c = texelFetch(uSrc, p, 0).rgb;",
    "  if (uHasLog == 1) c = lutq(clamp(c + uLogOff, 0.0, LM));",
    "  if (uSizes.x > 0) c = q16(tetra(uCstIn, uSizes.x, c));",
    "  if (uHasLift == 1) c = lutq(clamp(uLiftA + c * uLiftB, 0.0, LM));",
    "  if (uHasContrast == 1)",
    "    c = lutq(clamp((c - uContrast.y) * uContrast.x + uContrast.y, 0.0, LM));",
    "  if (uHasBright == 1) c = lutq(clamp(c + uBright, 0.0, LM));",
    "  if (uHasGamma == 1)",
    "    c = lutq(clamp(pow(max(c / LM, vec3(0.0)), uInvGamma) * LM, 0.0, LM));",
    "  if (uHasSat == 1) {",
    "    vec3 code = floor(c * CODE + 0.5);",
    "    c = clamp(vec3(mixrow(code, uSatR), mixrow(code, uSatG),",
    "                   mixrow(code, uSatB)), 0.0, CODE) / CODE;",
    "  }",
    "  if (uHasVib == 1) {",
    "    float mx = max(c.r, max(c.g, c.b));",
    "    float mn = min(c.r, min(c.g, c.b));",
    "    float csat = mx - mn;",
    "    float luma = c.g * 0.715158 + c.r * 0.212656 + c.b * 0.072186;",
    // FFSIGN(a) is (a > 0 ? 1 : -1) and `alternate` is off, so the sign term
    // is -1 for a positive intensity and +1 for a negative one.
    "    float sgnv = uVib > 0.0 ? -1.0 : 1.0;",
    "    float k = 1.0 + uVib * (1.0 - sgnv * csat);",
    "    c = q16(clamp(vec3(luma) + (c - vec3(luma)) * k, 0.0, 1.0));",
    "  }",
    "  if (uHasBlc == 1) c = vec3(blcAt(c.r), blcAt(c.g), blcAt(c.b));",
    "  if (uSizes.y > 0) c = q16(tetra(uCstOut, uSizes.y, c));",
    "  if (uHasCurve == 1) c = vec3(curve1(c, 0), curve1(c, 1), curve1(c, 2));",
    /* Hue curves, Color Slice and Tetra: one cube, baked by the server
     * from grade/slice.py and applied here exactly the way a layer's cube
     * is, so this is the same table lut3d reads. It sits after the curves
     * and before the layer stack, matching build_graph. */
    "  if (uSliceSize > 0) c = q16(tetra(uSlice, uSliceSize, c));",
    "  vec3 preLook = c;",
    "  if (uSizes.w > 0) {",
    "    vec3 lk = q16(tetra(uLook, uSizes.w, c));",
    // blend=normal is dst = top*opacity + bottom*(1-opacity) in float,
    // truncated back to an integer code. The LUT branch is input 0.
    "    vec3 lc = floor(lk * CODE + 0.5), pc = floor(c * CODE + 0.5);",
    "    c = floor(lc * uLookMix + pc * (1.0 - uLookMix)) / CODE;",
    "  }",
    /* Slot 2 blends in PARALLEL with slot 1, mirroring build_graph: it grades
     * preLook (the same pre-look signal slot 1 saw), never slot 1's output.
     * A is c above (slot 1's result, or preLook itself with no slot 1 LUT).
     * B is slot 2's own mix blend against preLook. balance then crossfades
     * A and B the same truncated way blend=normal does. balance <= 0 or no
     * slot 2 LUT skips this whole block, so the slot-1-only path above is
     * untouched and the output is byte identical to before slot 2 existed. */
    "  if (uLook2Size > 0 && uLookBalance > 0.0) {",
    "    vec3 aC = floor(c * CODE + 0.5);",
    "    vec3 lk2 = q16(tetra(uLook2, uLook2Size, preLook));",
    "    vec3 l2c = floor(lk2 * CODE + 0.5), p2c = floor(preLook * CODE + 0.5);",
    "    vec3 bC = floor(l2c * uLookMix2 + p2c * (1.0 - uLookMix2));",
    "    c = floor(bC * uLookBalance + aC * (1.0 - uLookBalance)) / CODE;",
    "  }",
    "  oCol = vec4(c, 1.0);",
    "}"
  ]));

  /* The power window matte and the 16 bit masked merge, shared by every layer
   * pass. Both were inside the colour shader when there was one secondary
   * with one window; they moved out unchanged when the stack arrived, so
   * there is still exactly one copy of the formula on this side.
   *
   * The matte is C4's formula on integer pixel indices with no half pixel
   * offset, which is what geq's X and Y are. The rounding is floor(x + 0.5)
   * and not roundEven: numpy's rint and ffmpeg's expression language disagree
   * on halves, so the engine pinned both of its references to floor(x + 0.5)
   * and this is the third one.
   *
   * The merge is maskedmerge on gbrp16le. The 8 bit matte is scaled by 257,
   * not by a left shift of 8: the engine routes it through format=gray16le
   * for exactly this reason, because a shift tops the matte out at 65280 and
   * a fully open window would then apply 99.61% of the correction. */
  var LIB_WINDOW = [
    "uniform int uWinRect;",
    "uniform int uWinInvert;",
    "uniform vec2 uWinCentre;",     // cx*W, cy*H in pixels
    "uniform vec2 uWinAxis;",       // the two half axes in pixels
    "uniform vec2 uWinRot;",        // (cos, sin) of the rotation
    "uniform vec2 uWinFeather;",    // (1+softness, 2*softness); y 0 is a hard edge
    "const float WCODE = 65535.0;",
    "float winMatte(ivec2 q) {",
    "  float dx = float(q.x) - uWinCentre.x;",
    "  float dy = float(q.y) - uWinCentre.y;",
    "  float ux = dx * uWinRot.x + dy * uWinRot.y;",
    "  float uy = dy * uWinRot.x - dx * uWinRot.y;",
    "  float a = ux / uWinAxis.x;",
    "  float b = uy / uWinAxis.y;",
    "  float d = uWinRect == 1 ? max(abs(a), abs(b)) : sqrt(a * a + b * b);",
    "  float m = uWinFeather.y <= 0.0 ? (d <= 1.0 ? 1.0 : 0.0)",
    "          : clamp((uWinFeather.x - d) / uWinFeather.y, 0.0, 1.0);",
    "  if (uWinInvert == 1) m = 1.0 - m;",
    "  return floor(m * 255.0 + 0.5);",
    "}",
    "vec3 winMerge(vec3 base, vec3 over, float m8) {",
    "  uint m = uint(m8) * 257u;",
    "  uvec3 bs = uvec3(floor(base * WCODE + 0.5));",
    "  uvec3 os = uvec3(floor(over * WCODE + 0.5));",
    "  uvec3 r = (bs * (65535u - m) + (os * m + 32767u)) / 65535u;",
    "  return vec3(r) / WCODE;",
    "}"
  ];

  /* One layer's baked cube, applied on its own.
   *
   * A stack can be any length, so the cube cannot live in the fused colour
   * shader the way the single secondary's did: there is no fixed number of
   * texture units to give it. lut3d's own quantisation is kept (q16), so a
   * layer pass lands on the same 16 bit lattice every other stage lands on
   * and a stack of four costs four passes and no accuracy. */
  var FS_LAYER_LUT = src([
    "#version 300 es",
    "precision highp float;",
    "precision highp int;",
    "precision highp sampler3D;",
    "uniform sampler2D uSrc;",
    "uniform sampler3D uLut;",
    "uniform int uSize;",
    "out vec4 oCol;"
  ].concat(LIB_LUT3D).concat([
    "vec3 q16(vec3 v) { return clamp(floor(v * 65535.0), 0.0, 65535.0) / 65535.0; }",
    "void main() {",
    "  vec3 c = texelFetch(uSrc, ivec2(gl_FragCoord.xy), 0).rgb;",
    "  if (uSize > 0) c = q16(tetra(uLut, uSize, c));",
    "  oCol = vec4(c, 1.0);",
    "}"
  ]));

  /* The merge under a layer's window matte.
   *
   * uBase is the branch that wins where the matte is 0 and uOver the one that
   * wins where it is 255, because maskedmerge returns its FIRST input where
   * the mask is 0. In matte view the graded branch IS the colour matte, so
   * the base goes black (the engine puts a colorchannelmixer rr=0:gg=0:bb=0
   * there) and the merge reads as colour matte times window matte rather than
   * as picture outside the shape. */
  var FS_LAYER_MERGE = src([
    "#version 300 es",
    "precision highp float;",
    "precision highp int;",
    "uniform sampler2D uBase;",
    "uniform sampler2D uOver;",
    "uniform int uWinBlack;",
    "out vec4 oCol;"
  ].concat(LIB_WINDOW).concat([
    "void main() {",
    "  ivec2 p = ivec2(gl_FragCoord.xy);",
    "  vec3 base = uWinBlack == 1 ? vec3(0.0) : texelFetch(uBase, p, 0).rgb;",
    "  vec3 over = texelFetch(uOver, p, 0).rgb;",
    "  oCol = vec4(winMerge(base, over, winMatte(p)), 1.0);",
    "}"
  ]));

  // highlight_pass, which is a `lut`, so it carries vf_lut's 65280 ceiling and
  // its truncation the same way the primaries stages do.
  var FS_HIGHLIGHT = src([
    "#version 300 es",
    "precision highp float;",
    "precision highp int;",
    "uniform sampler2D uTex;",
    "uniform float uT;",
    "uniform float uGain;",
    "uniform float uMax;",
    "out vec4 oCol;",
    "void main() {",
    "  vec3 c = texelFetch(uTex, ivec2(gl_FragCoord.xy), 0).rgb;",
    "  c = clamp((c - uT) * uGain, 0.0, uMax);",
    "  oCol = vec4(clamp(floor(c * 65535.0), 0.0, 65280.0) / 65535.0, 1.0);",
    "}"
  ]);

  // Separable swscale resample. uTaps is (filterSize x dstN) and uPos is
  // (dstN x 1), both R32F, generated on the CPU by swsFilter.
  var FS_RESAMPLE = src([
    "#version 300 es",
    "precision highp float;",
    "precision highp int;",
    "uniform sampler2D uTex;",
    "uniform sampler2D uTaps;",
    "uniform sampler2D uPos;",
    "uniform int uSize;",
    "uniform ivec2 uSrcSize;",
    "uniform int uVertical;",
    "out vec4 oCol;",
    "void main() {",
    "  ivec2 p = ivec2(gl_FragCoord.xy);",
    "  int o = uVertical == 1 ? p.y : p.x;",
    "  int base = int(texelFetch(uPos, ivec2(o, 0), 0).r);",
    "  vec3 acc = vec3(0.0);",
    "  for (int j = 0; j < uSize; j++) {",
    "    float w = texelFetch(uTaps, ivec2(j, o), 0).r;",
    "    int s = clamp(base + j, 0, (uVertical == 1 ? uSrcSize.y : uSrcSize.x) - 1);",
    "    ivec2 q = uVertical == 1 ? ivec2(p.x, s) : ivec2(s, p.y);",
    "    acc += w * texelFetch(uTex, q, 0).rgb;",
    "  }",
    "  oCol = vec4(acc, 1.0);",
    "}"
  ]);

  /* One doubling step of the geometric prefix scan that reproduces gblur.
   *
   * gblur is an IIR recursion (y[x] = u[x] + nu*y[x-1]) which is sequential by
   * nature, but it is also exactly a prefix sum with geometric weights, and a
   * prefix sum parallelises with the classic doubling trick in log2(width)
   * passes. That is why this matches ffmpeg exactly rather than approximately:
   * it is the same recursion, evaluated in a different order. */
  var FS_SCAN = src([
    "#version 300 es",
    "precision highp float;",
    "precision highp int;",
    "uniform sampler2D uTex;",
    "uniform ivec2 uSize;",
    "uniform ivec2 uDir;",
    "uniform int uOff;",
    "uniform float uW;",
    "out vec4 oCol;",
    "void main() {",
    "  ivec2 p = ivec2(gl_FragCoord.xy);",
    "  vec3 v = texelFetch(uTex, p, 0).rgb;",
    "  ivec2 q = p - uDir * uOff;",
    "  if (q.x >= 0 && q.y >= 0 && q.x < uSize.x && q.y < uSize.y) {",
    "    v += uW * texelFetch(uTex, q, 0).rgb;",
    "  }",
    "  oCol = vec4(v, 1.0);",
    "}"
  ]);

  // gblur multiplies the first and last sample of every row (then column) by
  // boundaryscale. It is a one line pass but leaving it out shifts the whole
  // blurred image, so it is not optional.
  var FS_EDGESCALE = src([
    "#version 300 es",
    "precision highp float;",
    "precision highp int;",
    "uniform sampler2D uTex;",
    "uniform ivec2 uDir;",
    "uniform int uIndex;",
    "uniform float uFactor;",
    "out vec4 oCol;",
    "void main() {",
    "  ivec2 p = ivec2(gl_FragCoord.xy);",
    "  vec3 v = texelFetch(uTex, p, 0).rgb;",
    "  int c = uDir.x == 1 ? p.x : p.y;",
    "  if (c == uIndex) v *= uFactor;",
    "  oCol = vec4(v, 1.0);",
    "}"
  ]);

  // postscale plus the write back to integer codes ffmpeg does with lrintf.
  var FS_POSTSCALE = src([
    "#version 300 es",
    "precision highp float;",
    "precision highp int;",
    "uniform sampler2D uTex;",
    "uniform float uScale;",
    "uniform float uMax;",
    "uniform float uQuant;",   // code scale to round to, 0 to skip
    "out vec4 oCol;",
    "void main() {",
    "  vec3 v = clamp(texelFetch(uTex, ivec2(gl_FragCoord.xy), 0).rgb * uScale, 0.0, uMax);",
    "  if (uQuant > 0.0) v = floor(v * uQuant + 0.5) / uQuant;",
    "  oCol = vec4(v, 1.0);",
    "}"
  ]);

  // The halation and bloom tint is a diagonal colorchannelmixer, so each
  // output is one rounded product of one input code and one coefficient.
  var FS_TINT = src([
    "#version 300 es",
    "precision highp float;",
    "precision highp int;",
    "uniform sampler2D uTex;",
    "uniform vec3 uTint;",
    "out vec4 oCol;",
    "void main() {",
    "  vec3 v = texelFetch(uTex, ivec2(gl_FragCoord.xy), 0).rgb;",
    "  vec3 code = floor(v * 65535.0 + 0.5);",
    "  oCol = vec4(clamp(roundEven(code * uTint), 0.0, 65535.0) / 65535.0, 1.0);",
    "}"
  ]);

  /* blend=all_mode=screen:all_opacity=s with the base as input 0.
   *
   * Transcribed as the integer arithmetic ffmpeg actually runs rather than as
   * the algebraic shorthand: screen is MAX - (MAX-a)*(MAX-b)/MAX with a
   * TRUNCATING integer divide, and the opacity lerp is
   * top + (expr - top)*opacity truncated back to a code. Doing it in floats
   * instead drifts by a code either way on dark pixels, where (MAX-a)*(MAX-b)
   * is close to 2^32 and float32 can no longer represent it exactly. uint is
   * the right type here: the product just fits, which is presumably why the
   * filter gets away with it too. */
  var FS_SCREEN = src([
    "#version 300 es",
    "precision highp float;",
    "precision highp int;",
    "uniform sampler2D uBase;",
    "uniform sampler2D uGlow;",
    "uniform float uOpacity;",
    "out vec4 oCol;",
    "void main() {",
    "  ivec2 p = ivec2(gl_FragCoord.xy);",
    "  vec3 a = texelFetch(uBase, p, 0).rgb;",
    "  vec3 b = texelFetch(uGlow, p, 0).rgb;",
    "  uvec3 ai = uvec3(floor(a * 65535.0 + 0.5));",
    "  uvec3 bi = uvec3(floor(b * 65535.0 + 0.5));",
    "  uvec3 scr = uvec3(65535u) - (uvec3(65535u) - ai) * (uvec3(65535u) - bi)",
    "              / uvec3(65535u);",
    "  vec3 top = vec3(ai);",
    "  vec3 o = floor(top + (vec3(scr) - top) * uOpacity);",
    "  oCol = vec4(clamp(o, 0.0, 65535.0) / 65535.0, 1.0);",
    "}"
  ]);

  /* maskedmerge against the radial ramp.
   *
   * The ramp is still quantised, because cinegrade bakes it to an 8 bit PNG:
   * geq truncates 255*ramp to a code, so the shader does floor(255*ramp) too.
   * That is the only emulation left here.
   *
   * Until 2026-09-04 two more steps were needed, and both were engine defects
   * rather than facts about the format. radial_mask ran its geq on a limited
   * range YUV surface and converted to gray afterwards, which expanded every
   * code by round((v-16)*255/219) (248 of 256 codes moved, up to 20 code
   * values, only 220 distinct codes surviving), and graph_with_mask widened
   * the 8 bit matte to 16 bit with a shift of 8, so the matte topped out at
   * 65280 and a fully open ramp applied 99.61% of the blur instead of all of
   * it. The engine now bakes through format=gray first and merges through
   * WINDOW_MASK_FORMAT (format=gray16le,format=gbrp16le), which is a multiply
   * by exactly 257, verified at max |v - 257n| = 0 across all 256 codes. So
   * the shader multiplies by 257 and emulates nothing else. */
  var FS_MASKEDMERGE = src([
    "#version 300 es",
    "precision highp float;",
    "precision highp int;",
    "uniform sampler2D uBase;",
    "uniform sampler2D uOver;",
    "uniform vec2 uCentre;",
    "uniform vec2 uHalf;",
    "uniform float uStart;",
    "uniform float uSpan;",
    "out vec4 oCol;",
    "void main() {",
    "  ivec2 p = ivec2(gl_FragCoord.xy);",
    "  float dx = (float(p.x) - uCentre.x) / uHalf.x;",
    "  float dy = (float(p.y) - uCentre.y) / uHalf.y;",
    "  float ramp = clamp((length(vec2(dx, dy)) - uStart) / uSpan, 0.0, 1.0);",
    "  float v8 = clamp(floor(255.0 * ramp), 0.0, 255.0);",
    "  uint m = uint(v8) * 257u;",
    "  uvec3 bs = uvec3(floor(texelFetch(uBase, p, 0).rgb * 65535.0 + 0.5));",
    "  uvec3 os = uvec3(floor(texelFetch(uOver, p, 0).rgb * 65535.0 + 0.5));",
    "  uvec3 r = (bs * (65535u - m) + (os * m + 32767u)) / 65535u;",
    "  oCol = vec4(vec3(r) / 65535.0, 1.0);",
    "}"
  ]);

  var FS_RGBASHIFT = src([
    "#version 300 es",
    "precision highp float;",
    "precision highp int;",
    "uniform sampler2D uTex;",
    "uniform ivec2 uSize;",
    "uniform int uAmt;",
    "out vec4 oCol;",
    "void main() {",
    "  ivec2 p = ivec2(gl_FragCoord.xy);",
    "  int xr = clamp(p.x - uAmt, 0, uSize.x - 1);",
    "  int xb = clamp(p.x + uAmt, 0, uSize.x - 1);",
    "  float r = texelFetch(uTex, ivec2(xr, p.y), 0).r;",
    "  float g = texelFetch(uTex, p, 0).g;",
    "  float b = texelFetch(uTex, ivec2(xb, p.y), 0).b;",
    "  oCol = vec4(r, g, b, 1.0);",
    "}"
  ]);

  /* RGB to and from the code values ffmpeg's auto-inserted converter makes.
   *
   * BT.601 at full range, NOT BT.2020 and not BT.709. The studio preview
   * pipes a raw rgb48le frame in over stdin, so the buffer carries no
   * colorspace tag at all and swscale falls back to its own default, which is
   * BT.601. Measured by pushing four thousand random colours through
   * setparams=range=full,format=gbrp16le,format=yuv444p16le and fitting: an
   * affine model reproduces it to within about one code at 16 bit, and the
   * coefficients below are that fit rather than the textbook numbers, because
   * swscale carries a 256/255 factor through its 16 bit path that the
   * textbook numbers do not have. */
  var YUV_LIB = [
    "vec3 rgbToYuv(vec3 c) {",
    "  return vec3(dot(c, vec3(0.3001522, 0.58928286, 0.11443896)),",
    "              dot(c, vec3(-0.16967558, -0.33230586, 0.50194555)),",
    "              dot(c, vec3(0.50194653, -0.42064875, -0.08133264)));",
    "}",
    "vec3 yuvToRgb(vec3 y) {",
    "  return vec3(y.x + 1.402 * y.z,",
    "              y.x - 0.344136 * y.y - 0.714136 * y.z,",
    "              y.x + 1.772 * y.y);",
    "}"
  ];

  /* swscale's 8x8 ordered dither, used whenever the source format is 16 bit
   * per component, which ours always is. Without it the chroma planes are
   * wrong on half of all pixels. */
  var DITHER8 = [
    "const int DITH[64] = int[64](",
    "   36, 68,  60, 92,  34, 66,  58, 90,",
    "  100,  4, 124, 28,  98,  2, 122, 26,",
    "   52, 84,  44, 76,  50, 82,  42, 74,",
    "  116, 20, 108, 12, 114, 18, 106, 10,",
    "   32, 64,  56, 88,  38, 70,  62, 94,",
    "   96,  0, 120, 24, 102,  6, 126, 30,",
    "   48, 80,  40, 72,  54, 86,  46, 78,",
    "  112, 16, 104,  8, 118, 22, 110, 14);",
    "int dith8(ivec2 p, int xoff) {",
    "  return DITH[(p.y & 7) * 8 + ((p.x + xoff) & 7)];",
    "}"
  ];

  /* gbrp16le to yuv444p, reproduced as the integer arithmetic swscale really
   * runs rather than as the matrix the documentation implies.
   *
   * Three stages, all measured against ffmpeg 8.1.1 on a 65536 sample ramp and
   * all exact on every sample:
   *   1. the input reader, limited range BT.601 coefficients at 1/32768, with
   *      swscale's own bias terms
   *   2. the limited to full range expansion, whose coefficients come out of
   *      solve_range_convert as 19078 for luma and 18652 for chroma
   *   3. the 8 bit writer, which adds the ordered dither and shifts by 7
   * The float matrix form of this is right on only 49 percent of chroma
   * samples, which is why it is not used. */
  var FS_TOCODE = src([
    "#version 300 es",
    "precision highp float;",
    "precision highp int;",
    "uniform sampler2D uTex;",
    "uniform sampler2D uTail;",
    "uniform float uMax;",     // 255 or 65535
    "uniform int uYuv;",
    "out vec4 oCol;"
  ].concat(YUV_LIB).concat(DITHER8).concat([
    "void main() {",
    "  ivec2 p = ivec2(gl_FragCoord.xy);",
    "  vec3 c = clamp(texelFetch(uTex, p, 0).rgb, 0.0, 1.0);",
    "  ivec3 q = ivec3(floor(c * 65535.0 + 0.5));",
    "  vec3 v;",
    "  if (uYuv == 1 && uMax < 256.0) {",
    "    int r = q.r, g = q.g, b = q.b;",
    "    int y16 = (8414 * r + 16519 * g + 3208 * b + 134217728 + 16384) >> 15;",
    "    int u16 = (-4865 * r - 9528 * g + 14392 * b + 1073741824 + 16384) >> 15;",
    "    int v16 = (14392 * r - 12061 * g - 2332 * b + 1073741824 + 16384) >> 15;",
    "    int y15 = min(y16 >> 1, 32767);",
    "    int u15 = min(u16 >> 1, 32767);",
    "    int v15 = min(v16 >> 1, 32767);",
    "    y15 = min((y15 * 19078 - 39084288) >> 14, 32767);",
    "    u15 = min((u15 * 18652 - 38207488) >> 14, 32767);",
    "    v15 = min((v15 * 18652 - 38207488) >> 14, 32767);",
    "    v = vec3(clamp((y15 + dith8(p, 0)) >> 7, 0, 255),",
    "             clamp((u15 + dith8(p, 0)) >> 7, 0, 255),",
    "             clamp((v15 + dith8(p, 3)) >> 7, 0, 255));",
    "  } else if (uYuv == 1) {",
    "    vec3 y = rgbToYuv(c);",
    "    float mid = floor(uMax / 2.0) + 1.0;",
    "    v = vec3(floor(y.x * uMax + 0.5),",
    "             floor(y.y * uMax + mid + 0.5),",
    "             floor(y.z * uMax + mid + 0.5));",
    "  } else {",
    // Dropping to rgb24 mid graph is the same swscale conversion as the one at
    // the end, so it uses the same measured table rather than a divide by 257.
    "    v = 255.0 * vec3(texelFetch(uTail, ivec2(q.r & 255, q.r >> 8), 0).r,",
    "                     texelFetch(uTail, ivec2(q.g & 255, q.g >> 8), 0).g,",
    "                     texelFetch(uTail, ivec2(q.b & 255, q.b >> 8), 0).b);",
    "    v = floor(v + 0.5);",
    "  }",
    "  oCol = vec4(clamp(v, 0.0, uMax), 1.0);",
    "}"
  ]));

  /* yuv444p back to rgb24. Fitted against ffmpeg on a 65536 sample ramp and
   * exact on every sample, which the float matrix was not (it missed 0.13
   * percent of green and 0.09 percent of blue by one code). */
  var FS_FROMCODE = src([
    "#version 300 es",
    "precision highp float;",
    "precision highp int;",
    "uniform sampler2D uTex;",
    "uniform float uMax;",
    "uniform int uYuv;",
    "out vec4 oCol;"
  ].concat(YUV_LIB).concat([
    "void main() {",
    "  vec3 v = texelFetch(uTex, ivec2(gl_FragCoord.xy), 0).rgb;",
    "  vec3 c;",
    "  if (uYuv == 1 && uMax < 256.0) {",
    "    int y = int(v.x), u = int(v.y) - 128, w = int(v.z) - 128;",
    "    int r = clamp((65536 * y + 91879 * w + 32768) >> 16, 0, 255);",
    "    int g = clamp((65536 * y - 22552 * u - 46800 * w + 32768) >> 16, 0, 255);",
    "    int b = clamp((65536 * y + 116120 * u + 32768) >> 16, 0, 255);",
    "    c = vec3(r, g, b) / 255.0;",
    "  } else if (uYuv == 1) {",
    "    float mid = floor(uMax / 2.0) + 1.0;",
    "    c = yuvToRgb(vec3(v.x / uMax, (v.y - mid) / uMax, (v.z - mid) / uMax));",
    "  } else {",
    "    c = v / uMax;",
    "  }",
    "  oCol = vec4(clamp(c, 0.0, 1.0), 1.0);",
    "}"
  ]));

  /* The last step of every graph is format=rgb24, and it is not the rounding
   * anyone would write down.
   *
   * (v + 128) >> 8 is wrong for 832 of the 65536 input codes, round(v/257) is
   * wrong for 31808 of them, and the three output channels do not even share
   * one table: R, G and B round differently on the same input because
   * swscale's gbrp16le reader treats plane 0 and planes 1 and 2 on different
   * code paths. It IS a pure function of the code (verified by running the
   * ramp forwards and backwards and getting identical tables), so the honest
   * thing is to carry the measured tables.
   *
   * Stored as the 255 input codes where each channel steps up, encoded as one
   * digit each: the step is within a few codes of 256*k - 128, so only the
   * small signed offset needs storing. Measured against ffmpeg 8.1.1; if the
   * numbers here ever stop matching, the parity harness will say so loudly
   * rather than quietly. */
  var TAIL8_DELTA = {
    r:
      "33344444455553333444444555533344444455553333444444555333344444457778" +
      "88886667777788888666777777888866667777788888666777777888866667777788" +
      "88866677777788886667777778888666677777788886667777778888666677777788" +
      "866667777778888666677777788866667777778886666677777",
    g:
      "33344444455553333444444555533344444455553333444444555333344444452301" +
      "11111222223311111122222230111111222223011111122222230111111222223011" +
      "11112222220011111222222301111112222220011111222222301111112222220011" +
      "111222222001111112222220011111222222001111112222200",
    b:
      "33344444455553333444444555533344444455553333444444555333344444457778" +
      "88888999977788888899997777888888999777788888899997777888888999777788" +
      "88889997777788888899977778888889997777788888899777778888889997777788" +
      "888999777778888889997777788888999777778888889977777"
  };

  function tail8Table() {
    // 65536 entries per channel, packed into one RGB32F 256 by 256 texture so
    // the final pass is a single texelFetch instead of a search.
    var data = new Float32Array(256 * 256 * 3);
    ["r", "g", "b"].forEach(function (ch, ci) {
      var s = TAIL8_DELTA[ch], v = 0, k = 0;
      for (var code = 0; code < 65536; code++) {
        while (k < 255 && code >= 256 * (k + 1) - 128 + (s.charCodeAt(k) - 51)) {
          k++; v++;
        }
        data[code * 3 + ci] = v / 255;
      }
    });
    return data;
  }

  var FS_TAIL8 = src([
    "#version 300 es",
    "precision highp float;",
    "precision highp int;",
    "uniform sampler2D uTex;",
    "uniform sampler2D uTail;",
    "uniform int uFlip;",
    "uniform int uDirect;",
    "uniform ivec2 uSize;",
    "out vec4 oCol;",
    "void main() {",
    "  ivec2 p = ivec2(gl_FragCoord.xy);",
    "  if (uFlip == 1) p.y = uSize.y - 1 - p.y;",
    "  vec3 c = texelFetch(uTex, p, 0).rgb;",
    /* When the chain already dropped to 8 bit, ffmpeg's closing format=rgb24
     * is a no-op, so the table must not run again. Feeding an 8 bit code back
     * through it as code/255*65535 lands past the table's own step for every
     * value above about 128 and adds one, which is a real bug this harness
     * caught: the picture came out a code brighter over most of its range. */
    "  if (uDirect == 1) { oCol = vec4(c, 1.0); return; }",
    "  ivec3 idx = clamp(ivec3(floor(c * 65535.0 + 0.5)), 0, 65535);",
    "  oCol = vec4(texelFetch(uTail, ivec2(idx.r & 255, idx.r >> 8), 0).r,",
    "              texelFetch(uTail, ivec2(idx.g & 255, idx.g >> 8), 0).g,",
    "              texelFetch(uTail, ivec2(idx.b & 255, idx.b >> 8), 0).b, 1.0);",
    "}"
  ]);

  /* vignette, working on integer code values.
   *
   * ffmpeg dithers this filter by default with a linear congruential
   * generator advanced once per component in raster order, starting from zero
   * on the first frame. It is deterministic, so it can be reproduced exactly
   * by jumping the generator ahead to this pixel's index. Without it the
   * result is off by up to a whole code everywhere, because the dither is
   * added before a truncation, not after a rounding. */
  var FS_VIGNETTE = src([
    "#version 300 es",
    "precision highp float;",
    "precision highp int;",
    "uniform sampler2D uTex;",
    "uniform ivec2 uSize;",
    "uniform vec2 uCentre;",
    "uniform float uAngle;",
    "uniform float uDmax2;",
    "uniform float uMax;",
    "uniform int uYuv;",
    "out vec4 oCol;",
    "uint lcgAt(uint n) {",
    "  uint A = 1u, C = 0u, ba = 1664525u, bc = 1013904223u;",
    "  for (int i = 0; i < 32; i++) {",
    "    if (n == 0u) break;",
    "    if ((n & 1u) == 1u) { C = A * bc + C; A = A * ba; }",
    "    bc = ba * bc + bc;",
    "    ba = ba * ba;",
    "    n >>= 1u;",
    "  }",
    "  return C;",
    "}",
    "float dither(uint n) { return float(lcgAt(n)) * (1.0 / 4294967296.0); }",
    /* GLSL ES only promises cos to about 2^-11 absolute, and the vignette
     * factor is cos to the fourth, so a sloppy cos moves the factor by enough
     * to flip the floor on a quarter of all channels. ffmpeg computes this in
     * double and stores a float, so the series below (accurate to about 1e-8
     * over the only range that occurs, 0 to pi/2) is what agrees with it. */
    "float vcos(float x) {",
    "  float t = x * x;",
    "  return 1.0 + t * (-0.5 + t * (4.1666667e-2 + t * (-1.3888889e-3 +",
    "         t * (2.4801587e-5 + t * (-2.7557319e-7 + t * 2.0876757e-9)))));",
    "}",
    "void main() {",
    "  ivec2 p = ivec2(gl_FragCoord.xy);",
    "  float xx = trunc(float(p.x) - uCentre.x);",
    "  float yy = trunc(float(p.y) - uCentre.y);",
    /* Squared, not hypot over dmax, so the exact corner lands on dnorm 1.0
     * instead of 1.0000001. Both terms are exact integers in a float there,
     * and ffmpeg does this in double where the ratio is exactly 1, so the
     * naive form dropped the corner pixel to black. */
    "  float dnorm = sqrt((xx * xx + yy * yy) / uDmax2);",
    "  float f = 0.0;",
    "  if (dnorm <= 1.0) { float c4 = vcos(uAngle * dnorm); c4 = c4 * c4; f = c4 * c4; }",
    "  vec3 v = texelFetch(uTex, p, 0).rgb;",
    "  uint px = uint(p.y * uSize.x + p.x);",
    "  uint plane = uint(uSize.x * uSize.y);",
    "  vec3 o;",
    "  if (uYuv == 1) {",
    "    float mid = floor(uMax / 2.0);",   // 127, which is what vf_vignette uses
    "    o = vec3(floor(v.x * f + dither(px)),",
    "             floor(f * (v.y - mid) + mid + dither(px + plane)),",
    "             floor(f * (v.z - mid) + mid + dither(px + plane * 2u)));",
    "  } else {",
    "    uint n = px * 3u;",
    "    o = vec3(floor(v.r * f + dither(n)),",
    "             floor(v.g * f + dither(n + 1u)),",
    "             floor(v.b * f + dither(n + 2u)));",
    "  }",
    "  oCol = vec4(clamp(o, 0.0, uMax), 1.0);",
    "}"
  ]);

  /* unsharp, working on integer code values.
   *
   * The 5x5 matrix is not a box: unsharp cascades four two tap running sums
   * per axis, which is a binomial kernel [1,4,6,4,1] over 256, and the divide
   * is an integer shift with a half added first. Edges replicate.
   *
   * Only the first plane is touched, because chroma_amount defaults to zero
   * and the plane the filter calls luma is Y here. */
  var FS_UNSHARP = src([
    "#version 300 es",
    "precision highp float;",
    "precision highp int;",
    "uniform sampler2D uTex;",
    "uniform ivec2 uSize;",
    "uniform float uAmount;",   // already scaled to ffmpeg's integer amount
    "uniform float uShift;",    // 2^(8+depth)
    "uniform float uMax;",
    "out vec4 oCol;",
    "void main() {",
    "  ivec2 p = ivec2(gl_FragCoord.xy);",
    "  vec3 v = texelFetch(uTex, p, 0).rgb;",
    "  float w[5];",
    "  w[0] = 1.0; w[1] = 4.0; w[2] = 6.0; w[3] = 4.0; w[4] = 1.0;",
    "  float sum = 0.0;",
    "  for (int dy = 0; dy < 5; dy++) {",
    "    int sy = clamp(p.y + dy - 2, 0, uSize.y - 1);",
    "    for (int dx = 0; dx < 5; dx++) {",
    "      int sx = clamp(p.x + dx - 2, 0, uSize.x - 1);",
    "      sum += w[dy] * w[dx] * texelFetch(uTex, ivec2(sx, sy), 0).r;",
    "    }",
    "  }",
    "  float blur = floor((sum + 128.0) / 256.0);",
    "  float d = floor((v.r - blur) * uAmount / uShift);",
    "  oCol = vec4(clamp(v.r + d, 0.0, uMax), v.g, v.b, 1.0);",
    "}"
  ]);

  /* mid_detail: out = A*(1+coeff) - B*coeff, A the sharp branch, B the
   * blurred one. Mirrors cinegrade.py's f_mid_detail_segment blend, which is
   * written the same way (not "A + coeff*(A-B)") specifically so no maxval
   * constant is needed: ffmpeg's blend filter has none in its expr language,
   * confirmed against `ffmpeg -h filter=blend`. This shader runs on whatever
   * is in uA/uB (normalised float RGB when uMax is 1, raw YUV or RGB code
   * values when uMax is 255 or 65535, exactly like FS_UNSHARP above), and
   * clamps the finished expression to 0..uMax. ffmpeg's blend does NOT clamp
   * on its own: an out-of-range all_expr result wraps modulo 65536 at 16 bit
   * (measured, ffmpeg 8.1.1), which is why f_mid_detail_segment now writes
   * clip(...,0,65535) around the same expression. The clamp therefore sits at
   * the same point in both engines: after the whole expression, in the
   * working range. All three channels get the same expression, matching the
   * engine's all_expr, which is why mid_detail also touches chroma when the
   * chain happens to be in YUV at this point: that is what ffmpeg really
   * does too. */
  var FS_MIDDETAIL = src([
    "#version 300 es",
    "precision highp float;",
    "precision highp int;",
    "uniform sampler2D uA;",
    "uniform sampler2D uB;",
    "uniform float uCoeff;",
    "uniform float uMax;",
    "out vec4 oCol;",
    "void main() {",
    "  ivec2 p = ivec2(gl_FragCoord.xy);",
    "  vec3 a = texelFetch(uA, p, 0).rgb;",
    "  vec3 b = texelFetch(uB, p, 0).rgb;",
    "  vec3 o = a * (1.0 + uCoeff) - b * uCoeff;",
    "  oCol = vec4(clamp(o, 0.0, uMax), 1.0);",
    "}"
  ]);

  /* Film grain (C3). Overlay blend against the plate the server rendered
   * (grain/plate route, itself a slice of the real build_graph text), plus
   * an optional response=film weight computed the same way cinegrade.py's
   * blend all_expr does: colorchannelmixer's rr/rg/rb=0.2126/0.7152/0.0722
   * mix applied to whichever three components uTex currently holds, exactly
   * once, with no special case for a YUV stage. That is deliberate, not an
   * oversight: cinegrade.py's own grain block runs that same colorchannelmixer
   * and blend on `cur` without ever asking what pixel format it is in, so
   * matching it verbatim (rather than branching on uYuv the way FS_VIGNETTE
   * does) is what FS_MIDDETAIL's own comment already established is the
   * right call for this codebase: "All three channels get the same
   * expression... that is what ffmpeg really does too."
   *
   * uMax is codeMax||1, the same convention detailPass uses, so this runs
   * in whatever range uTex currently is (normalised float when nothing has
   * quantised yet, 255 or 65535 code values when vignette or sharpen has).
   * uPlate is always uploaded normalised 0..1 (see uploadGrainPlate) and is
   * rescaled up to uMax here so the response weight's 0.5*uMax midpoint
   * lines up with uTex's own range.
   *
   * The final overlay+opacity step normalises to 0..1 first (ffmpeg's
   * standard blend modes, unlike all_expr, work in normalised float
   * regardless of bit depth), matching the formula measured against
   * ffmpeg's own integer blend in cinegrade.py (max |predicted-actual| 1.49
   * of 65535, mean 0.57, at opacity 0.5 over 2000 random 16-bit pairs). */
  var FS_GRAIN = src([
    "#version 300 es",
    "precision highp float;",
    "precision highp int;",
    "uniform sampler2D uTex;",
    "uniform sampler2D uPlate;",
    "uniform float uOpacity;",
    "uniform float uMax;",
    "uniform int uFilm;",
    "out vec4 oCol;",
    "void main() {",
    "  ivec2 p = ivec2(gl_FragCoord.xy);",
    "  vec3 cur = texelFetch(uTex, p, 0).rgb;",
    "  vec3 plate = texelFetch(uPlate, p, 0).rgb * uMax;",
    "  if (uFilm == 1) {",
    "    float L = 0.2126 * cur.r + 0.7152 * cur.g + 0.0722 * cur.b;",
    "    float t = clamp((L / uMax - 0.5) * 2.0, 0.0, 1.0);",
    "    float w = 1.0 - 0.75 * (3.0 * t * t - 2.0 * t * t * t);",
    "    plate = 0.5 * uMax + w * (plate - 0.5 * uMax);",
    "  }",
    "  vec3 a = cur / uMax;",
    "  vec3 b = plate / uMax;",
    "  vec3 lo = 2.0 * a * b;",
    "  vec3 hi = 1.0 - 2.0 * (1.0 - a) * (1.0 - b);",
    "  vec3 ov = mix(lo, hi, step(0.5, a));",
    "  vec3 dst = a * (1.0 - uOpacity) + ov * uOpacity;",
    "  oCol = vec4(clamp(dst, 0.0, 1.0) * uMax, 1.0);",
    "}"
  ]);

  var FS_LETTERBOX = src([
    "#version 300 es",
    "precision highp float;",
    "precision highp int;",
    "uniform sampler2D uTex;",
    "uniform int uTop;",
    "uniform int uBottom;",
    "uniform vec3 uBlack;",
    "out vec4 oCol;",
    "void main() {",
    "  ivec2 p = ivec2(gl_FragCoord.xy);",
    "  vec3 v = texelFetch(uTex, p, 0).rgb;",
    "  if (p.y < uTop || p.y >= uBottom) v = uBlack;",
    "  oCol = vec4(v, 1.0);",
    "}"
  ]);

  var FS_COPY = src([
    "#version 300 es",
    "precision highp float;",
    "precision highp int;",
    "uniform sampler2D uTex;",
    "uniform int uFlip;",
    "uniform ivec2 uSize;",
    "out vec4 oCol;",
    "void main() {",
    "  ivec2 p = ivec2(gl_FragCoord.xy);",
    "  if (uFlip == 1) p.y = uSize.y - 1 - p.y;",
    "  oCol = vec4(texelFetch(uTex, p, 0).rgb, 1.0);",
    "}"
  ]);

  // ------------------------------------------------------------------
  // GL plumbing
  // ------------------------------------------------------------------

  function Gl(gl) {
    this.gl = gl;
    this.programs = {};
    this.pool = {};
    this.vao = gl.createVertexArray();
  }

  Gl.prototype.program = function (key, fs) {
    if (this.programs[key]) return this.programs[key];
    var gl = this.gl;
    var p = gl.createProgram();
    gl.attachShader(p, this.shader(gl.VERTEX_SHADER, VERT));
    gl.attachShader(p, this.shader(gl.FRAGMENT_SHADER, fs));
    gl.linkProgram(p);
    if (!gl.getProgramParameter(p, gl.LINK_STATUS)) {
      throw new Error("link failed for " + key + ": " + gl.getProgramInfoLog(p));
    }
    p._loc = {};
    this.programs[key] = p;
    return p;
  };

  Gl.prototype.shader = function (type, source) {
    var gl = this.gl;
    var s = gl.createShader(type);
    gl.shaderSource(s, source);
    gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) {
      throw new Error("compile failed: " + gl.getShaderInfoLog(s) + "\n" + numbered(source));
    }
    return s;
  };

  function numbered(s) {
    return s.split("\n").map(function (l, i) { return (i + 1) + ": " + l; }).join("\n");
  }

  Gl.prototype.loc = function (p, name) {
    if (!(name in p._loc)) p._loc[name] = this.gl.getUniformLocation(p, name);
    return p._loc[name];
  };

  /* Render targets come from a pool keyed by size.
   *
   * The gblur scan alone runs forty odd passes, each ping ponging between two
   * targets, so allocating per pass would spend more time in the driver than
   * in the shader. */
  Gl.prototype.acquire = function (w, h, fmt) {
    fmt = fmt || "f32";
    var key = w + "x" + h + ":" + fmt;
    var free = this.pool[key] || (this.pool[key] = []);
    if (free.length) return free.pop();
    var gl = this.gl;
    this.scratch();
    var tex = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, tex);
    var internal = fmt === "f32" ? gl.RGBA32F : gl.RGBA8;
    var type = fmt === "f32" ? gl.FLOAT : gl.UNSIGNED_BYTE;
    gl.texImage2D(gl.TEXTURE_2D, 0, internal, w, h, 0, gl.RGBA, type, null);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    var fbo = gl.createFramebuffer();
    gl.bindFramebuffer(gl.FRAMEBUFFER, fbo);
    gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, tex, 0);
    var st = gl.checkFramebufferStatus(gl.FRAMEBUFFER);
    if (st !== gl.FRAMEBUFFER_COMPLETE) throw new Error("framebuffer incomplete: " + st);
    return { tex: tex, fbo: fbo, w: w, h: h, key: key };
  };

  Gl.prototype.release = function (t) {
    if (t) this.pool[t.key].push(t);
  };

  Gl.prototype.draw = function (target) {
    var gl = this.gl;
    gl.bindFramebuffer(gl.FRAMEBUFFER, target ? target.fbo : null);
    gl.viewport(0, 0, target ? target.w : gl.canvas.width, target ? target.h : gl.canvas.height);
    gl.bindVertexArray(this.vao);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
  };

  /* Every texture upload happens on this unit and nothing samples it.
   *
   * gl.texImage2D works on whatever unit happens to be active, so creating a
   * texture in the middle of binding others silently rebinds one of them.
   * That produced a first frame that was wrong and every later frame right,
   * which is exactly the kind of bug a preview must not have. */
  var SCRATCH_UNIT = 15;

  Gl.prototype.scratch = function () {
    this.gl.activeTexture(this.gl.TEXTURE0 + SCRATCH_UNIT);
  };

  Gl.prototype.bindTex = function (p, name, unit, tex, kind) {
    var gl = this.gl;
    gl.activeTexture(gl.TEXTURE0 + unit);
    gl.bindTexture(kind === "3d" ? gl.TEXTURE_3D : gl.TEXTURE_2D, tex);
    gl.uniform1i(this.loc(p, name), unit);
  };

  // ------------------------------------------------------------------
  // instance
  // ------------------------------------------------------------------

  function Instance(canvas, gl) {
    this.canvas = canvas;
    this.gl = gl;
    this.G = new Gl(gl);
    this.src = null;             // { tex, w, h }
    this.luts = {};              // content key -> { size, tex } or a pending promise
    this.grainPlates = {};       // plate key -> { tex, w, h } or a pending promise
    this.curveTex = null;
    this.tapCache = {};
    this.out = null;             // RGBA8 target the final picture lands in
    this.lastTiming = null;
    this.apiBase = "";
    this.identityLut = null;
  }

  Instance.prototype.dispose = function () {
    var gl = this.gl;
    var self = this;
    Object.keys(this.luts).forEach(function (k) {
      if (self.luts[k] && self.luts[k].tex) gl.deleteTexture(self.luts[k].tex);
    });
    this.luts = {};
    Object.keys(this.grainPlates).forEach(function (k) {
      var p = self.grainPlates[k];
      if (p && p.tex) gl.deleteTexture(p.tex);
    });
    this.grainPlates = {};
  };

  Instance.prototype.setSource = function (u16, w, h) {
    var gl = this.gl;
    this.G.scratch();
    // rgb48le arrives interleaved without alpha; the GPU wants four channels,
    // and normalising here means every shader downstream works in [0,1].
    var f = new Float32Array(w * h * 4);
    for (var i = 0, j = 0; i < w * h; i++, j += 3) {
      f[i * 4] = u16[j] / 65535;
      f[i * 4 + 1] = u16[j + 1] / 65535;
      f[i * 4 + 2] = u16[j + 2] / 65535;
      f[i * 4 + 3] = 1;
    }
    if (!this.src || this.src.w !== w || this.src.h !== h) {
      if (this.src) gl.deleteTexture(this.src.tex);
      var tex = gl.createTexture();
      gl.bindTexture(gl.TEXTURE_2D, tex);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA32F, w, h, 0, gl.RGBA, gl.FLOAT, f);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
      this.src = { tex: tex, w: w, h: h };
    } else {
      gl.bindTexture(gl.TEXTURE_2D, this.src.tex);
      gl.texSubImage2D(gl.TEXTURE_2D, 0, 0, 0, w, h, gl.RGBA, gl.FLOAT, f);
    }
    return this.src;
  };

  // --- LUTs ---------------------------------------------------------

  function lutRequests(cfg) {
    var reqs = [];
    var cv = cfg.convert;
    if (cv.working_space === "dwg") {
      reqs.push({ slot: "cstIn", key: "tech:AppleLog_to_DWG.cube",
                  body: { kind: "technical", name: "AppleLog_to_DWG.cube" } });
      var n = "DWG_to_Rec709_" + cv.tonemap + "_" + cv.encode + ".cube";
      reqs.push({ slot: "cstOut", key: "tech:" + n, body: { kind: "technical", name: n } });
    } else {
      // f_convert_out prefers the per encode file and only falls back to the
      // unsuffixed one on an old tree, so ask for the same one it would pick.
      var d = "AppleLog_to_Rec709_" + cv.tonemap + "_" + cv.encode + ".cube";
      reqs.push({ slot: "cstOut", key: "tech:" + d, body: { kind: "technical", name: d } });
    }
    /* One cube per layer, or two for the one case that needs two branches.
     * The slot name carries the layer's ARRAY index so a pass can find its
     * own cube without replaying the stack's control flow, and the cache key
     * carries only what the cube depends on. */
    configLayers(cfg).forEach(function (L, i) {
      if (!layerActive(L)) return;
      layerBranches(L).forEach(function (v) {
        reqs.push({ slot: "layer" + i + ":" + v,
                    key: "layer:" + stableJson(layerCubeKey(L, v)),
                    body: { kind: "layer", config: L, variant: v } });
      });
    });
    /* The hue curves + slice + tetra cube. Asked for only when something is
     * actually set, so a neutral stage costs no round trip, and keyed on both
     * blocks together because they bake into one table. */
    if (sliceActive(cfg)) {
      var sliceCfg = { hue_curves: cfg.hue_curves, slice: cfg.slice };
      reqs.push({ slot: "slice", key: "slice:" + stableJson(sliceCfg),
                  body: { kind: "slice", config: sliceCfg } });
    }
    if (cfg.look.lut) {
      reqs.push({ slot: "look", key: "look:" + cfg.look.lut,
                  body: { kind: "look", name: cfg.look.lut } });
    }
    // Slot 2 only costs a fetch when it can actually show: no LUT, or
    // balance at 0, mirrors the engine's own build_graph gate exactly.
    if (cfg.look.lut2 && +(cfg.look.balance || 0) > 1e-6) {
      reqs.push({ slot: "look2", key: "look:" + cfg.look.lut2,
                  body: { kind: "look", name: cfg.look.lut2 } });
    }
    return reqs;
  }

  /* Everything the baked cube depends on, and nothing else. Mirrors
   * cinegrade._layer_cube_key: the name, the enabled flag, the placement, the
   * window and the blur cannot change a single cube entry, so folding them
   * into the cache key would refetch an identical table every time a layer is
   * renamed or dragged up the stack. */
  var LAYER_CUBE_FIELDS = ["exposure", "contrast", "pivot", "saturation",
    "temperature", "tint", "hue_shift", "sat_gain", "lum_gain", "offset",
    "strength"];

  function layerCubeKey(layer, variant) {
    var cor = {};
    LAYER_CUBE_FIELDS.forEach(function (k) { cor[k] = layer.correct[k]; });
    return { variant: variant, show: !!layer.mask.show,
             key: layer.mask.key, correct: cor };
  }

  function stableJson(o) {
    if (Array.isArray(o)) return "[" + o.map(stableJson).join(",") + "]";
    if (o && typeof o === "object") {
      return "{" + Object.keys(o).sort().map(function (k) {
        return JSON.stringify(k) + ":" + stableJson(o[k]);
      }).join(",") + "}";
    }
    return JSON.stringify(o);
  }

  Instance.prototype.ready = function (config, opts) {
    var self = this;
    var cfg = this.prepareConfig(config, opts);
    var reqs = lutRequests(cfg);
    var work = reqs.map(function (r) { return self.lut(r.key, r.body); });
    // Grain plate prefetch (C3). width/height come from opts when the
    // caller has not called setSource yet (render.js's worker loop calls
    // ready() before setSource16, so this.src is still null there) and fall
    // back to the already-uploaded source size otherwise (live.js and
    // parity.js both call setSource before ready, so this.src is already
    // valid for them). If neither is known yet, grain is silently skipped
    // here and render() throws its own clear error instead of guessing a
    // size to prefetch at.
    if (cfg.grain.enabled) {
      var gw = (opts && opts.width) || (this.src && this.src.w);
      var gh = (opts && opts.height) || (this.src && this.src.h);
      if (gw && gh) {
        var gkey = grainPlateKey(cfg.grain, gw, gh);
        var gqs = grainPlateQuery(cfg.grain, gw, gh);
        work.push(this.grainPlate(gkey, gqs, gw, gh));
      }
    }
    return Promise.all(work).then(function () { return true; });
  };

  Instance.prototype.lut = function (key, body) {
    var self = this;
    var hit = this.luts[key];
    if (hit && hit.tex) return Promise.resolve(hit);
    if (hit && hit.pending) return hit.pending;
    var pending = fetch(this.apiBase + "/api/lut", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    }).then(function (r) {
      if (!r.ok) return r.text().then(function (t) { throw new Error("lut " + key + ": " + t); });
      return r.arrayBuffer();
    }).then(function (buf) {
      var n = new DataView(buf).getUint32(0, true);
      var raw = new Float32Array(buf, 4, n * n * n * 3);
      var entry = { size: n, tex: self.uploadLut(raw, n) };
      self.luts[key] = entry;
      return entry;
    });
    this.luts[key] = { pending: pending };
    return pending;
  };

  /* Upload a cube as a 3D texture in ffmpeg's memory order.
   *
   * The file has red varying fastest; ffmpeg's own array has blue varying
   * fastest (lut[r*N*N + g*N + b]). A 3D texture indexes x fastest, so blue
   * goes on x and red on z, and the upload transposes to match. */
  Instance.prototype.uploadLut = function (raw, n) {
    var gl = this.gl;
    this.G.scratch();
    var data = new Float32Array(n * n * n * 4);
    for (var b = 0; b < n; b++) {
      for (var g = 0; g < n; g++) {
        for (var r = 0; r < n; r++) {
          var srcIdx = (r + n * g + n * n * b) * 3;
          var dstIdx = (b + n * g + n * n * r) * 4;
          data[dstIdx] = raw[srcIdx];
          data[dstIdx + 1] = raw[srcIdx + 1];
          data[dstIdx + 2] = raw[srcIdx + 2];
          data[dstIdx + 3] = 1;
        }
      }
    }
    var tex = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_3D, tex);
    gl.texImage3D(gl.TEXTURE_3D, 0, gl.RGBA32F, n, n, n, 0, gl.RGBA, gl.FLOAT, data);
    gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_WRAP_R, gl.CLAMP_TO_EDGE);
    return tex;
  };

  // --- grain plate (C3) ----------------------------------------------

  /* Every field the server's /api/grain/plate route reads, always sent in
   * full (never a partial query), so the same config always produces the
   * same query string and the same cache key: the server keys its own disk
   * cache on the raw request dict it receives, not on the config it resolves
   * that dict into, so a partial request here would be a second, wasted
   * cache entry rather than a correctness problem, but sending everything
   * every time avoids that waste. response is deliberately never included:
   * the plate itself never depends on it (see grainPass and the FS_GRAIN
   * comment), only the picture's own live luminance does. */
  function grainPlateFields(g) {
    return {
      stock: g.stock || "custom",
      size: +g.size,
      strength: +g.strength,
      softness: +(g.softness || 0),
      color: +(g.color || 0),
      seed: +(g.seed || 0)
    };
  }

  function grainPlateKey(g, w, h) {
    var f = grainPlateFields(g);
    f.w = w; f.h = h;
    return "grain:" + stableJson(f);
  }

  function grainPlateQuery(g, w, h) {
    var f = grainPlateFields(g);
    return "width=" + w + "&height=" + h +
      "&stock=" + encodeURIComponent(f.stock) +
      "&size=" + encodeURIComponent(f.size) +
      "&strength=" + encodeURIComponent(f.strength) +
      "&softness=" + encodeURIComponent(f.softness) +
      "&color=" + encodeURIComponent(f.color) +
      "&seed=" + encodeURIComponent(f.seed);
  }

  Instance.prototype.grainPlate = function (key, qs, w, h) {
    var self = this;
    var hit = this.grainPlates[key];
    if (hit && hit.tex) return Promise.resolve(hit);
    if (hit && hit.pending) return hit.pending;
    var pending = fetch(this.apiBase + "/api/grain/plate?" + qs)
      .then(function (r) {
        if (!r.ok) return r.text().then(function (t) {
          throw new Error("grain plate " + key + ": " + t);
        });
        return r.arrayBuffer();
      }).then(function (buf) {
        var entry = { w: w, h: h, tex: self.uploadGrainPlate(new Uint16Array(buf), w, h) };
        self.grainPlates[key] = entry;
        return entry;
      });
    this.grainPlates[key] = { pending: pending };
    return pending;
  };

  /* Same normalise-by-65535 shape as setSource, minus the alpha channel and
   * minus its update-in-place branch: a grain plate texture is never
   * resized after upload, it is cached forever under its own content key
   * and a new size just gets a new key. */
  Instance.prototype.uploadGrainPlate = function (u16, w, h) {
    var gl = this.gl;
    this.G.scratch();
    var f = new Float32Array(w * h * 4);
    for (var i = 0, j = 0; i < w * h; i++, j += 3) {
      f[i * 4] = u16[j] / 65535;
      f[i * 4 + 1] = u16[j + 1] / 65535;
      f[i * 4 + 2] = u16[j + 2] / 65535;
      f[i * 4 + 3] = 1;
    }
    var tex = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, tex);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA32F, w, h, 0, gl.RGBA, gl.FLOAT, f);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    return tex;
  };

  Instance.prototype.identity = function () {
    if (!this.identityLut) {
      // A 2 cube identity, bound into the sampler slots a config does not use.
      // GLSL requires every sampler to be bound to something valid even when
      // the branch that reads it never runs.
      var d = new Float32Array(2 * 2 * 2 * 3);
      for (var b = 0; b < 2; b++) for (var g = 0; g < 2; g++) for (var r = 0; r < 2; r++) {
        var i = (r + 2 * g + 4 * b) * 3;
        d[i] = r; d[i + 1] = g; d[i + 2] = b;
      }
      this.identityLut = this.uploadLut(d, 2);
    }
    return this.identityLut;
  };

  // Python's round(), which is half to even. The engine calls it on the RGB
  // split amount, so 2.5 becomes 2 and 1.5 becomes 2.
  function pyRound(v) {
    var f = Math.floor(v), d = v - f;
    if (d > 0.5) return f + 1;
    if (d < 0.5) return f;
    return (f % 2 === 0) ? f : f + 1;
  }

  Instance.prototype.tapTex = function (srcN, dstN, kind) {
    var key = srcN + ">" + dstN + ":" + kind;
    if (this.tapCache[key]) return this.tapCache[key];
    var f = swsFilter(srcN, dstN, kind);
    var gl = this.gl;
    this.G.scratch();
    var taps = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, taps);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.R32F, f.size, dstN, 0, gl.RED, gl.FLOAT, f.taps);
    nearest(gl, gl.TEXTURE_2D);
    var pos = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, pos);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.R32F, dstN, 1, 0, gl.RED, gl.FLOAT,
                  Float32Array.from(f.pos));
    nearest(gl, gl.TEXTURE_2D);
    this.tapCache[key] = { taps: taps, pos: pos, size: f.size };
    return this.tapCache[key];
  };

  function nearest(gl, target) {
    gl.texParameteri(target, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
    gl.texParameteri(target, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
    gl.texParameteri(target, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(target, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
  }

  /* Building the 65536 entry curve table and re-uploading it costs one to two
   * milliseconds, which is several times the cost of the render itself, so it
   * is cached on the curves config. Dragging a primaries slider must not pay
   * for a curve that did not move. */
  Instance.prototype.curveState = function (cfg) {
    var key = (cfg.curves && cfg.curves.enabled) ? stableJson(cfg.curves) : "off";
    if (this.curveKey === key && this.curveTex) {
      return { tex: this.curveTex, has: this.curveHas };
    }
    var table = key === "off" ? null : buildCurveTable(cfg);
    var tex = this.curveTexture(table || {});
    this.curveKey = key;
    this.curveHas = !!table;
    return { tex: tex, has: !!table };
  };

  Instance.prototype.curveTexture = function (table) {
    var gl = this.gl;
    this.G.scratch();
    var data = new Float32Array(256 * 256 * 3);
    for (var i = 0; i < CURVE_LUT_SIZE; i++) {
      data[i * 3] = table.r ? table.r[i] : i / CURVE_SCALE;
      data[i * 3 + 1] = table.g ? table.g[i] : i / CURVE_SCALE;
      data[i * 3 + 2] = table.b ? table.b[i] : i / CURVE_SCALE;
    }
    if (!this.curveTex) {
      this.curveTex = gl.createTexture();
      gl.bindTexture(gl.TEXTURE_2D, this.curveTex);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGB32F, 256, 256, 0, gl.RGB, gl.FLOAT, data);
      nearest(gl, gl.TEXTURE_2D);
    } else {
      gl.bindTexture(gl.TEXTURE_2D, this.curveTex);
      gl.texSubImage2D(gl.TEXTURE_2D, 0, 0, 0, 256, 256, gl.RGB, gl.FLOAT, data);
    }
    return this.curveTex;
  };

  Instance.prototype.prepareConfig = function (config, opts) {
    var cfg = fullConfig(config);
    var f = opts && opts.pixelScale;
    if (f && f < 0.999) cfg = scaleForPreview(cfg, f);
    return cfg;
  };

  // --- the passes ---------------------------------------------------

  Instance.prototype.simple = function (key, fs, w, h, setup, fmt) {
    var G = this.G, gl = this.gl;
    var p = G.program(key, fs);
    gl.useProgram(p);
    var out = G.acquire(w, h, fmt);
    setup(p, G);
    G.draw(out);
    return out;
  };

  Instance.prototype.copyInto = function (input, w, h) {
    var self = this;
    return this.simple("copy", FS_COPY, w, h, function (p, G) {
      G.bindTex(p, "uTex", 0, input);
      self.gl.uniform1i(G.loc(p, "uFlip"), 0);
      self.gl.uniform2i(G.loc(p, "uSize"), w, h);
    });
  };

  /* gblur, as the exact IIR recursion ffmpeg runs, evaluated as a geometric
   * prefix scan so it parallelises. Returns a fresh target and does not
   * consume the input. */
  Instance.prototype.gblur = function (input, w, h, sigma, maxVal, quant) {
    var G = this.G, gl = this.gl, self = this;
    var pr = gblurParams(sigma, 1);
    var cur = this.copyInto(input, w, h);
    var passes = 1;

    function scanAxis(dx, dy, n) {
      // forward: the first sample is scaled, then the recursion runs up
      var e = self.simple("edge", FS_EDGESCALE, w, h, function (p, G2) {
        G2.bindTex(p, "uTex", 0, cur.tex);
        gl.uniform2i(G2.loc(p, "uDir"), dx, dy);
        gl.uniform1i(G2.loc(p, "uIndex"), 0);
        gl.uniform1f(G2.loc(p, "uFactor"), pr.boundaryscale);
      });
      G.release(cur); cur = e; passes++;
      cur = self.scanRun(cur, w, h, dx, dy, n, pr.nu, maxVal);

      var e2 = self.simple("edge", FS_EDGESCALE, w, h, function (p, G2) {
        G2.bindTex(p, "uTex", 0, cur.tex);
        gl.uniform2i(G2.loc(p, "uDir"), dx, dy);
        gl.uniform1i(G2.loc(p, "uIndex"), n - 1);
        gl.uniform1f(G2.loc(p, "uFactor"), pr.boundaryscale);
      });
      G.release(cur); cur = e2; passes++;
      cur = self.scanRun(cur, w, h, -dx, -dy, n, pr.nu, maxVal);
    }

    scanAxis(1, 0, w);
    scanAxis(0, 1, h);

    var out = this.simple("postscale", FS_POSTSCALE, w, h, function (p, G2) {
      G2.bindTex(p, "uTex", 0, cur.tex);
      gl.uniform1f(G2.loc(p, "uScale"), pr.postscale * pr.postscale);
      gl.uniform1f(G2.loc(p, "uMax"), maxVal);
      gl.uniform1f(G2.loc(p, "uQuant"), quant || 0);
    });
    G.release(cur);
    this.passCount += passes + 1;
    return out;
  };

  /* One direction of the scan.
   *
   * Each pass doubles the reach, so the whole recursion costs ceil(log2(n))
   * passes rather than n sequential steps. It stops early once nu^offset is
   * small enough that the terms it drops are far below a sixteen bit code,
   * which for a small sigma is after one or two passes. */
  Instance.prototype.scanRun = function (cur, w, h, dx, dy, n, nu, maxVal) {
    var G = this.G, gl = this.gl, self = this;
    var tail = maxVal / Math.max(1e-9, 1 - nu);
    for (var d = 1; d < n; d *= 2) {
      var weight = Math.pow(nu, d);
      if (weight * tail < 1e-5) break;
      /* eslint-disable no-loop-func */
      var next = this.simple("scan", FS_SCAN, w, h, (function (dd, ww) {
        return function (p, G2) {
          G2.bindTex(p, "uTex", 0, cur.tex);
          gl.uniform2i(G2.loc(p, "uSize"), w, h);
          gl.uniform2i(G2.loc(p, "uDir"), dx, dy);
          gl.uniform1i(G2.loc(p, "uOff"), dd);
          gl.uniform1f(G2.loc(p, "uW"), ww);
        };
      })(d, weight));
      G.release(cur);
      cur = next;
      self.passCount++;
    }
    return cur;
  };

  Instance.prototype.resample = function (input, sw, sh, dw, dh, kind) {
    var G = this.G, gl = this.gl, self = this;
    var hf = this.tapTex(sw, dw, kind);
    var mid = this.simple("resample", FS_RESAMPLE, dw, sh, function (p, G2) {
      G2.bindTex(p, "uTex", 0, input);
      G2.bindTex(p, "uTaps", 1, hf.taps);
      G2.bindTex(p, "uPos", 2, hf.pos);
      gl.uniform1i(G2.loc(p, "uSize"), hf.size);
      gl.uniform2i(G2.loc(p, "uSrcSize"), sw, sh);
      gl.uniform1i(G2.loc(p, "uVertical"), 0);
    });
    var vf = this.tapTex(sh, dh, kind);
    var out = this.simple("resample", FS_RESAMPLE, dw, dh, function (p, G2) {
      G2.bindTex(p, "uTex", 0, mid.tex);
      G2.bindTex(p, "uTaps", 1, vf.taps);
      G2.bindTex(p, "uPos", 2, vf.pos);
      gl.uniform1i(G2.loc(p, "uSize"), vf.size);
      gl.uniform2i(G2.loc(p, "uSrcSize"), dw, sh);
      gl.uniform1i(G2.loc(p, "uVertical"), 1);
    });
    G.release(mid);
    // swscale writes gbrp16le, so the branch really is quantised here.
    var q = this.simple("postscale", FS_POSTSCALE, dw, dh, function (p, G2) {
      G2.bindTex(p, "uTex", 0, out.tex);
      gl.uniform1f(G2.loc(p, "uScale"), 1.0);
      gl.uniform1f(G2.loc(p, "uMax"), 1.0);
      gl.uniform1f(G2.loc(p, "uQuant"), 65535);
    });
    G.release(out);
    this.passCount += 3;
    return q;
  };

  // --- the colour head ----------------------------------------------

  Instance.prototype.tail8 = function () {
    if (!this.tailTex) {
      var gl = this.gl;
      this.G.scratch();
      this.tailTex = gl.createTexture();
      gl.bindTexture(gl.TEXTURE_2D, this.tailTex);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGB32F, 256, 256, 0, gl.RGB, gl.FLOAT,
                    tail8Table());
      nearest(gl, gl.TEXTURE_2D);
    }
    return this.tailTex;
  };

  /* Black lift and highlight roll-off, ported point for point from
   * _tone_ends_points in cinegrade.py. It is a toe and a shoulder that each
   * reach mid grey with slope 1, written as explicit points, so the mid tones
   * do not move. The 4 decimal rounding is reproduced because the rounded text
   * is what ffmpeg's parser actually sees, not the full precision float.
   *
   * The untouched half has to be a SPREAD of points on y=x, not one endpoint.
   * ffmpeg's pchip takes every node's slope from its neighbouring secants and
   * weights those secants by the neighbouring interval widths, so one lone
   * endpoint makes this half's interval enormous and lets the graded half
   * dictate the slope at both of its ends. This port used to emit the lone
   * endpoint while the engine emitted the spread, which is what made
   * prim_black_lift and the four presets that inherit it fail parity.
   *
   * Measured, not reasoned: a 65536 entry 16 bit ramp pushed through
   * `ffmpeg -vf curves=all='<engine points>':interp=pchip` (ffmpeg 8.1.1) and
   * diffed against this file's pchipLut at the same indices, for black_lift
   * 0.08 / highlight_rolloff 0 / pivot 0.336. With the lone endpoint the port
   * was 278/65535 away from ffmpeg at index 44430 (x = 0.678, the middle of
   * the one long segment above the pivot), which is 1.08 code values at 8 bit.
   * With the spread the largest gap over all 65536 entries is 1/65535, which
   * is the float versus integer rounding tie and nothing else. */
  function toneEndsPoints(bl, hr, pivot, n) {
    n = n || 6;
    // Past these the segment has no room to land on and the exponent blows up.
    bl = Math.min(bl, pivot - 1e-3);
    hr = Math.min(hr, 1.0 - pivot - 1e-3);
    var pts = [], i, v, u, k;
    if (Math.abs(bl) > 1e-6) {
      k = pivot / (pivot - bl);
      for (i = 0; i < n; i++) {
        v = i / (n - 1);
        pts.push([v * pivot, bl + (pivot - bl) * Math.pow(v, k)]);
      }
    } else {
      // Untouched toe: n points on y=x, every local secant exactly 1.
      for (i = 0; i < n; i++) {
        v = i / (n - 1);
        pts.push([v * pivot, v * pivot]);
      }
    }
    pts.push([pivot, pivot]);
    if (Math.abs(hr) > 1e-6) {
      k = (1.0 - pivot) / (1.0 - hr - pivot);
      for (i = 1; i < n; i++) {
        u = i / (n - 1);
        pts.push([pivot + u * (1.0 - pivot),
                  pivot + (1.0 - hr - pivot) * (1.0 - Math.pow(1.0 - u, k))]);
      }
    } else {
      // Untouched shoulder: the same spread, running up from the pivot.
      for (i = 1; i < n; i++) {
        u = i / (n - 1);
        pts.push([pivot + u * (1.0 - pivot), pivot + u * (1.0 - pivot)]);
      }
    }
    pts.sort(function (a, b) { return (a[0] - b[0]) || (a[1] - b[1]); });
    var seen = {}, out = [];
    for (i = 0; i < pts.length; i++) {
      var key = fmt(pts[i][0], 4);
      if (seen[key] === 1) continue;   // ffmpeg rejects a repeated x coordinate
      seen[key] = 1;
      out.push([key, fmt(Math.min(1, Math.max(0, pts[i][1])), 4)]);
    }
    return out;
  }

  Instance.prototype.blcTexture = function (bl, hr, pivot) {
    var gl = this.gl;
    this.G.scratch();
    var key = bl + "/" + hr + "/" + pivot;
    if (this.blcKey === key && this.blcTex) return this.blcTex;
    // f_primaries writes this call with interp=pchip, so it is the monotone
    // spline here, not the natural one the curves node defaults away from.
    var table = pchipLut(toneEndsPoints(bl, hr, pivot),
                         new Float64Array(CURVE_LUT_SIZE));
    var data = new Float32Array(256 * 256);
    for (var i = 0; i < CURVE_LUT_SIZE; i++) data[i] = table[i];
    if (!this.blcTex) {
      this.blcTex = gl.createTexture();
      gl.bindTexture(gl.TEXTURE_2D, this.blcTex);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.R32F, 256, 256, 0, gl.RED, gl.FLOAT, data);
      nearest(gl, gl.TEXTURE_2D);
    } else {
      gl.bindTexture(gl.TEXTURE_2D, this.blcTex);
      gl.texSubImage2D(gl.TEXTURE_2D, 0, 0, 0, 256, 256, gl.RED, gl.FLOAT, data);
    }
    this.blcKey = key;
    return this.blcTex;
  };

  /* The fused colour head.
   *
   * opts.srcTex   read this texture instead of the loaded source.
   * opts.lookOff  build the head but hold the look back, so the before_look
   *               layers can run between them.
   * opts.lookOnly build ONLY the look: every head stage is guarded by its own
   *               uniform, so turning all of them off leaves exactly the look
   *               block and nothing else. That is what lets a layer stack run
   *               in the middle of this chain without a second copy of the
   *               look's arithmetic living somewhere else.
   *
   * With no layers at all, render() calls this once with no options and the
   * pass is byte for byte the one that shipped before layers existed. */
  Instance.prototype.colourPass = function (cfg, W, H, opts) {
    var gl = this.gl, G = this.G, self = this;
    opts = opts || {};
    var head = !opts.lookOnly;      // run log, CST, primaries, curves, slice
    var wantLook = !opts.lookOff;   // run the look block
    var p = cfg.primaries, cv = cfg.convert;
    var LM = 65280 / 65535;
    var prog = G.program("color", FS_COLOR);
    gl.useProgram(prog);

    var reqs = lutRequests(cfg), slot = {};
    reqs.forEach(function (r) {
      var e = self.luts[r.key];
      if (!e || !e.tex) throw new Error("LUT not loaded: " + r.key);
      slot[r.slot] = e;
    });
    var ident = this.identity();
    function bindLut(name, unit, s) {
      var e = slot[s];
      G.bindTex(prog, name, unit, e ? e.tex : ident, "3d");
      return e ? e.size : 0;
    }
    G.bindTex(prog, "uSrc", 0, opts.srcTex || this.src.tex);
    // uSizes.z is spare: the layer cubes left this shader when the single
    // secondary became a stack. Every sampler is still bound (to the identity
    // cube when its slot is empty) because an unbound sampler is undefined
    // behaviour; the size is what switches a stage off.
    var sizes = [bindLut("uCstIn", 1, "cstIn"), bindLut("uCstOut", 2, "cstOut"),
                 0, bindLut("uLook", 4, "look")];
    if (!head) { sizes[0] = 0; sizes[1] = 0; }
    if (!wantLook) sizes[3] = 0;
    gl.uniform4i(G.loc(prog, "uSizes"), sizes[0], sizes[1], sizes[2], sizes[3]);
    var sliceSize = bindLut("uSlice", 7, "slice");
    gl.uniform1i(G.loc(prog, "uSliceSize"), head ? sliceSize : 0);
    gl.uniform1f(G.loc(prog, "uLookMix"), fmt(+cfg.look.mix, 4));
    // Second look slot: a separate int uniform because uSizes (ivec4) is
    // full. Unit 8, the next free texture unit in this pass (0..7 taken).
    var look2Size = bindLut("uLook2", 8, "look2");
    gl.uniform1i(G.loc(prog, "uLook2Size"), wantLook ? look2Size : 0);
    gl.uniform1f(G.loc(prog, "uLookMix2"), fmt(+cfg.look.mix2, 4));
    gl.uniform1f(G.loc(prog, "uLookBalance"), fmt(+(cfg.look.balance || 0), 4));

    var offR = +cv.exposure + +p.temperature;
    var offG = +cv.exposure + +p.tint;
    var offB = +cv.exposure - +p.temperature;
    var hasLog = Math.abs(offR) > 1e-6 || Math.abs(offG) > 1e-6 || Math.abs(offB) > 1e-6;
    gl.uniform1i(G.loc(prog, "uHasLog"), (head && hasLog) ? 1 : 0);
    gl.uniform3f(G.loc(prog, "uLogOff"),
                 fmt(offR * APPLE_LOG_STOP, 6) * LM,
                 fmt(offG * APPLE_LOG_STOP, 6) * LM,
                 fmt(offB * APPLE_LOG_STOP, 6) * LM);

    var lift = triplet(p.lift, 0), gain = triplet(p.gain, 1);
    var hasLift = lift.some(nz) || gain.some(function (v) { return Math.abs(v - 1) > 1e-6; });
    gl.uniform1i(G.loc(prog, "uHasLift"), (head && hasLift) ? 1 : 0);
    gl.uniform3f(G.loc(prog, "uLiftA"), fmt(lift[0], 4) * LM, fmt(lift[1], 4) * LM,
                 fmt(lift[2], 4) * LM);
    gl.uniform3f(G.loc(prog, "uLiftB"), fmt(gain[0] - lift[0], 4),
                 fmt(gain[1] - lift[1], 4), fmt(gain[2] - lift[2], 4));

    var pivot = (p.pivot === null || p.pivot === undefined)
      ? MID_GREY_CODE[cv.working_space] : +p.pivot;
    var contrast = +p.contrast;
    gl.uniform1i(G.loc(prog, "uHasContrast"),
                 (head && Math.abs(contrast - 1) > 1e-6) ? 1 : 0);
    gl.uniform2f(G.loc(prog, "uContrast"), fmt(contrast, 4), fmt(pivot, 4) * LM);

    var bright = triplet(p.brightness, 0);
    gl.uniform1i(G.loc(prog, "uHasBright"), (head && bright.some(nz)) ? 1 : 0);
    gl.uniform3f(G.loc(prog, "uBright"), fmt(bright[0], 4) * LM,
                 fmt(bright[1], 4) * LM, fmt(bright[2], 4) * LM);

    var gamma = triplet(p.gamma, 1);
    var hasGamma = gamma.some(function (v) { return Math.abs(v - 1) > 1e-6; });
    gl.uniform1i(G.loc(prog, "uHasGamma"), (head && hasGamma) ? 1 : 0);
    gl.uniform3f(G.loc(prog, "uInvGamma"), fmt(1 / gamma[0], 4), fmt(1 / gamma[1], 4),
                 fmt(1 / gamma[2], 4));

    var sat = +p.saturation, lr = 0.2126, lg = 0.7152, lb = 0.0722, iv = 1 - sat;
    gl.uniform1i(G.loc(prog, "uHasSat"), (head && Math.abs(sat - 1) > 1e-6) ? 1 : 0);
    gl.uniform3f(G.loc(prog, "uSatR"), fmt(iv * lr + sat, 5), fmt(iv * lg, 5), fmt(iv * lb, 5));
    gl.uniform3f(G.loc(prog, "uSatG"), fmt(iv * lr, 5), fmt(iv * lg + sat, 5), fmt(iv * lb, 5));
    gl.uniform3f(G.loc(prog, "uSatB"), fmt(iv * lr, 5), fmt(iv * lg, 5), fmt(iv * lb + sat, 5));

    gl.uniform1i(G.loc(prog, "uHasVib"),
                 (head && Math.abs(+p.vibrance) > 1e-6) ? 1 : 0);
    gl.uniform1f(G.loc(prog, "uVib"), fmt(+p.vibrance, 4));

    var bl = +p.black_lift, hr = +p.highlight_rolloff;
    var hasBlc = Math.abs(bl) > 1e-6 || Math.abs(hr) > 1e-6;
    // Built first, bound second, because building one binds it somewhere.
    var blcTex = hasBlc ? this.blcTexture(bl, hr, pivot) : this.blcTexture(0, 0, 0.5);
    var curve = this.curveState(cfg);
    gl.uniform1i(G.loc(prog, "uHasBlc"), (head && hasBlc) ? 1 : 0);
    G.bindTex(prog, "uBlc", 5, blcTex);
    gl.uniform1i(G.loc(prog, "uHasCurve"), (head && curve.has) ? 1 : 0);
    G.bindTex(prog, "uCurve", 6, curve.tex);

    var out = G.acquire(W, H);
    G.draw(out);
    this.passCount++;
    return out;
  };

  // --- layers -------------------------------------------------------

  /* Which baked cube belongs to which layer branch, keyed "index:variant".
   *
   * Read back out of lutRequests rather than rebuilt, so the pass and the
   * fetch cannot disagree about what a layer's cube is keyed on. */
  Instance.prototype.layerSlots = function (cfg) {
    var self = this, out = {};
    lutRequests(cfg).forEach(function (r) {
      if (r.slot.indexOf("layer") !== 0) return;
      var e = self.luts[r.key];
      if (!e || !e.tex) throw new Error("LUT not loaded: " + r.key);
      out[r.slot.slice(5)] = e;
    });
    return out;
  };

  /* One layer's baked cube, applied to a texture. */
  Instance.prototype.layerLutPass = function (tex, entry, W, H) {
    var gl = this.gl, ident = this.identity();
    var out = this.simple("layerlut", FS_LAYER_LUT, W, H, function (pr, G2) {
      G2.bindTex(pr, "uSrc", 0, tex);
      G2.bindTex(pr, "uLut", 1, entry ? entry.tex : ident, "3d");
      gl.uniform1i(G2.loc(pr, "uSize"), entry ? entry.size : 0);
    });
    this.passCount++;
    return out;
  };

  /* The masked merge of a graded branch back over the picture.
   *
   * Every window parameter is a fraction of the frame, so the geometry is
   * resolved against the size this render is actually running at and no
   * preview scaling is needed. */
  Instance.prototype.layerMerge = function (base, over, win, black, W, H) {
    var gl = this.gl;
    var wg = windowGeometry(win, W, H);
    var out = this.simple("layermerge", FS_LAYER_MERGE, W, H, function (pr, G2) {
      G2.bindTex(pr, "uBase", 0, base);
      G2.bindTex(pr, "uOver", 1, over);
      gl.uniform1i(G2.loc(pr, "uWinBlack"), black ? 1 : 0);
      gl.uniform1i(G2.loc(pr, "uWinRect"), wg.rect ? 1 : 0);
      gl.uniform1i(G2.loc(pr, "uWinInvert"), wg.invert ? 1 : 0);
      gl.uniform2f(G2.loc(pr, "uWinCentre"), wg.cxp, wg.cyp);
      gl.uniform2f(G2.loc(pr, "uWinAxis"), wg.ax, wg.ay);
      gl.uniform2f(G2.loc(pr, "uWinRot"), wg.cr, wg.sr);
      gl.uniform2f(G2.loc(pr, "uWinFeather"), wg.hi, wg.den);
    });
    this.passCount++;
    return out;
  };

  /* One layer: cube, optional blur, optional merge under the window matte.
   *
   * Mirrors build_layers filter for filter. A layer with no window is a
   * single cube on the running picture, which is what keeps a migrated
   * secondary the same one-chain graph it always was. A layer WITH a window
   * grades a branch and merges it back, un-graded branch first, because
   * maskedmerge returns its first input where the matte is 0. */
  Instance.prototype.layerPass = function (cur, entry, slots, W, H) {
    var G = this.G, self = this;
    var layer = entry.layer, idx = entry.index;
    var win = layerWindow(layer);
    var show = !!layer.mask.show;
    /* The blur is a picture operation. In matte view there is no picture,
     * only the selection, and blurring that would misreport how soft the mask
     * really is, so the engine leaves it out of that branch and so does this. */
    var sigma = show ? 0 : layerBlurSigma(layer, W);
    var branches = layerBranches(layer);

    function graded(variant) {
      var t = self.layerLutPass(cur.tex, slots[idx + ":" + variant], W, H);
      if (sigma > 0) {
        var b = self.gblur(t.tex, W, H, fmt(sigma, 3), 1, 65535);
        G.release(t);
        t = b;
      }
      return t;
    }

    var over = graded(branches[branches.length - 1]);
    if (!win) { G.release(cur); return over; }

    // Two branches is mask.invert over a window AND a key: the first branch
    // is the full correction and wins where the window is closed. One branch
    // in matte view puts black there instead, so the merge reads as colour
    // matte times window matte rather than as picture outside the shape.
    var base = branches.length > 1 ? graded(branches[0]) : null;
    var out = this.layerMerge(base ? base.tex : cur.tex, over.tex,
                              win, show, W, H);
    if (base) G.release(base);
    G.release(over);
    G.release(cur);
    return out;
  };

  Instance.prototype.layerStack = function (cur, cfg, list, W, H) {
    if (!list.length) return cur;
    var slots = this.layerSlots(cfg);
    for (var i = 0; i < list.length; i++) {
      cur = this.layerPass(cur, list[i], slots, W, H);
    }
    return cur;
  };

  // --- FX -----------------------------------------------------------

  /* halation and bloom: identical shape, different divisor.
   *
   * highlight pass, swscale bilinear down, gblur, swscale bicubic back up,
   * diagonal colorchannelmixer tint, then screen at `strength`. */
  Instance.prototype.glowPass = function (cur, h, div, W, H) {
    var gl = this.gl, G = this.G, self = this;
    var t = Math.max(0, Math.min(0.99, +h.threshold));
    var LM = 65280 / 65535;
    var hp = this.simple("highlight", FS_HIGHLIGHT, W, H, function (pr, G2) {
      G2.bindTex(pr, "uTex", 0, cur.tex);
      gl.uniform1f(G2.loc(pr, "uT"), fmt(t, 4) * LM);
      gl.uniform1f(G2.loc(pr, "uGain"), fmt(1 / Math.max(1e-3, 1 - t), 4));
      gl.uniform1f(G2.loc(pr, "uMax"), LM);
    });
    this.passCount++;
    var dw = Math.max(2, Math.floor(W / div)), dh = Math.max(2, Math.floor(H / div));
    var small = this.resample(hp.tex, W, H, dw, dh, "bilinear");
    G.release(hp);
    var blur = this.gblur(small.tex, dw, dh, fmt(Math.max(0.3, +h.sigma / div), 3), 1, 65535);
    G.release(small);
    var big = this.resample(blur.tex, dw, dh, W, H, "bicubic");
    G.release(blur);
    var tint = this.simple("tint", FS_TINT, W, H, function (pr, G2) {
      G2.bindTex(pr, "uTex", 0, big.tex);
      gl.uniform3f(G2.loc(pr, "uTint"), fmt(+h.tint[0], 3), fmt(+h.tint[1], 3),
                   fmt(+h.tint[2], 3));
    });
    G.release(big);
    this.passCount++;
    var out = this.simple("screen", FS_SCREEN, W, H, function (pr, G2) {
      G2.bindTex(pr, "uBase", 0, cur.tex);
      G2.bindTex(pr, "uGlow", 1, tint.tex);
      gl.uniform1f(G2.loc(pr, "uOpacity"), fmt(+h.strength, 3));
    });
    G.release(tint);
    G.release(cur);
    this.passCount++;
    return out;
  };

  Instance.prototype.radialPass = function (cur, rb, W, H) {
    var gl = this.gl, G = this.G;
    var soft = this.gblur(cur.tex, W, H, fmt(+rb.sigma, 3), 1, 65535);
    var out = this.simple("mask", FS_MASKEDMERGE, W, H, function (pr, G2) {
      G2.bindTex(pr, "uBase", 0, cur.tex);
      G2.bindTex(pr, "uOver", 1, soft.tex);
      gl.uniform2f(G2.loc(pr, "uCentre"), W / 2, H / 2);
      gl.uniform2f(G2.loc(pr, "uHalf"), W / 2, H / 2);
      gl.uniform1f(G2.loc(pr, "uStart"), +rb.start);
      gl.uniform1f(G2.loc(pr, "uSpan"), Math.max(1e-3, +rb.end - +rb.start));
    });
    G.release(soft); G.release(cur);
    this.passCount++;
    return out;
  };

  Instance.prototype.shiftPass = function (cur, amt, W, H) {
    var gl = this.gl, G = this.G;
    var out = this.simple("shift", FS_RGBASHIFT, W, H, function (pr, G2) {
      G2.bindTex(pr, "uTex", 0, cur.tex);
      gl.uniform2i(G2.loc(pr, "uSize"), W, H);
      gl.uniform1i(G2.loc(pr, "uAmt"), amt);
    });
    G.release(cur);
    this.passCount++;
    return out;
  };

  Instance.prototype.toCode = function (cur, W, H, codeMax, yuv) {
    var gl = this.gl, G = this.G;
    var tail = this.tail8();
    var out = this.simple("tocode", FS_TOCODE, W, H, function (pr, G2) {
      G2.bindTex(pr, "uTex", 0, cur.tex);
      G2.bindTex(pr, "uTail", 1, tail);
      gl.uniform1f(G2.loc(pr, "uMax"), codeMax);
      gl.uniform1i(G2.loc(pr, "uYuv"), yuv);
    });
    G.release(cur);
    this.passCount++;
    return out;
  };

  Instance.prototype.fromCode = function (cur, W, H, codeMax, yuv) {
    var gl = this.gl, G = this.G;
    var out = this.simple("fromcode", FS_FROMCODE, W, H, function (pr, G2) {
      G2.bindTex(pr, "uTex", 0, cur.tex);
      gl.uniform1f(G2.loc(pr, "uMax"), codeMax);
      gl.uniform1i(G2.loc(pr, "uYuv"), yuv);
    });
    G.release(cur);
    this.passCount++;
    return out;
  };

  Instance.prototype.vignettePass = function (cur, v, W, H, codeMax, yuv) {
    var gl = this.gl, G = this.G;
    /* radius scales the falloff distance, and the factor is cos(angle*d)^4,
     * so the engine folds radius into the angle instead of adding a filter.
     * The clamp is ffmpeg's limit (an angle past pi/2 is refused) and it is
     * applied before the 4 decimal formatting, same order as the engine. */
    var angle = fmt(Math.min(1.5707,
      (0.2 + +v.amount * 1.1)
        * (VIGNETTE_RADIUS_NEUTRAL / Math.max(0.05, +v.radius))), 4);
    var out = this.simple("vignette", FS_VIGNETTE, W, H, function (pr, G2) {
      G2.bindTex(pr, "uTex", 0, cur.tex);
      gl.uniform2i(G2.loc(pr, "uSize"), W, H);
      gl.uniform2f(G2.loc(pr, "uCentre"), W / 2, H / 2);
      gl.uniform1f(G2.loc(pr, "uAngle"), angle);
      gl.uniform1f(G2.loc(pr, "uDmax2"), (W / 2) * (W / 2) + (H / 2) * (H / 2));
      gl.uniform1f(G2.loc(pr, "uMax"), codeMax);
      gl.uniform1i(G2.loc(pr, "uYuv"), yuv);
    });
    G.release(cur);
    this.passCount++;
    return out;
  };

  Instance.prototype.letterboxPass = function (cur, cfg, W, H, codeMax, yuv) {
    var lb = cfg.letterbox;
    var target = Math.round(W / +lb.aspect);
    if (target >= H) return cur;
    target -= target % 2;
    var off = Math.floor((H - target) / 2);
    var gl = this.gl, G = this.G;
    // The pad colour is black in whatever space the tail is in, which for the
    // yuv branch means the chroma planes sit at their midpoint, not at zero.
    var black = codeMax
      ? (yuv ? [0, Math.floor(codeMax / 2) + 1, Math.floor(codeMax / 2) + 1] : [0, 0, 0])
      : [0, 0, 0];
    var out = this.simple("letterbox", FS_LETTERBOX, W, H, function (pr, G2) {
      G2.bindTex(pr, "uTex", 0, cur.tex);
      gl.uniform1i(G2.loc(pr, "uTop"), off);
      gl.uniform1i(G2.loc(pr, "uBottom"), off + target);
      gl.uniform3f(G2.loc(pr, "uBlack"), black[0], black[1], black[2]);
    });
    G.release(cur);
    this.passCount++;
    return out;
  };

  // --- detail -------------------------------------------------------

  Instance.prototype.detailPass = function (cur, cfg, plan, W, H, codeMax) {
    var gl = this.gl, G = this.G;
    var maxVal = codeMax || 1;
    var quant = codeMax ? 1 : 65535;
    if (plan.soften) {
      var soft = this.gblur(cur.tex, W, H, fmt(+cfg.detail.soften, 3), maxVal, quant);
      G.release(cur);
      cur = soft;
    }
    if (plan.sharpen) {
      // 8 + bit depth, and the amount is scaled by 65536 no matter the depth,
      // which is why the same number is 256 times weaker at 16 bit.
      var bits = codeMax === 255 ? 8 : 16;
      var amount = trunc(fmt(+cfg.detail.sharpen, 3) * 65536);
      var out = this.simple("unsharp", FS_UNSHARP, W, H, function (pr, G2) {
        G2.bindTex(pr, "uTex", 0, cur.tex);
        gl.uniform2i(G2.loc(pr, "uSize"), W, H);
        gl.uniform1f(G2.loc(pr, "uAmount"), amount);
        gl.uniform1f(G2.loc(pr, "uShift"), Math.pow(2, 8 + bits));
        gl.uniform1f(G2.loc(pr, "uMax"), maxVal);
      });
      G.release(cur);
      this.passCount++;
      cur = out;
    }
    if (plan.midDetail) {
      // sigma is a FRACTION of the actual width being rendered (2%), so using
      // W here (the real source texture width, already at preview size when
      // this is a preview) already agrees with the engine's info["width"] at
      // any size: no scaleForPreview entry needed, see MID_DETAIL_SIGMA_FRAC.
      var sigma = Math.max(MID_DETAIL_SIGMA_FRAC * W, MID_DETAIL_SIGMA_MIN);
      var coeff = fmt(+cfg.detail.mid_detail, 3) * MID_DETAIL_K;
      var blurred = this.gblur(cur.tex, W, H, fmt(sigma, 3), maxVal, quant);
      var mixed = this.simple("middetail", FS_MIDDETAIL, W, H, function (pr, G2) {
        G2.bindTex(pr, "uA", 0, cur.tex);
        G2.bindTex(pr, "uB", 1, blurred.tex);
        gl.uniform1f(G2.loc(pr, "uCoeff"), coeff);
        gl.uniform1f(G2.loc(pr, "uMax"), maxVal);
      });
      G.release(blurred);
      G.release(cur);
      this.passCount++;
      cur = mixed;
    }
    return cur;
  };

  // --- grain (C3) -----------------------------------------------------

  /* The plate itself was already fetched and uploaded by ready(); this pass
   * only ever looks it up by key. Throwing when it is missing (rather than
   * skipping the effect) matches colourPass's own "LUT not loaded" contract,
   * so a config that turns grain on always either grades with grain or fails
   * loudly, never silently grades without it. */
  Instance.prototype.grainPass = function (cur, cfg, W, H, codeMax) {
    var gl = this.gl, G = this.G;
    var g = cfg.grain;
    var key = grainPlateKey(g, W, H);
    var plate = this.grainPlates[key];
    if (!plate || !plate.tex) {
      throw new Error("grain plate not loaded (call ready() first): " + key);
    }
    var maxVal = codeMax || 1;
    var out = this.simple("grain", FS_GRAIN, W, H, function (pr, G2) {
      G2.bindTex(pr, "uTex", 0, cur.tex);
      G2.bindTex(pr, "uPlate", 1, plate.tex);
      gl.uniform1f(G2.loc(pr, "uOpacity"), fmt(+g.opacity, 4));
      gl.uniform1f(G2.loc(pr, "uMax"), maxVal);
      gl.uniform1i(G2.loc(pr, "uFilm"), g.response === "film" ? 1 : 0);
    });
    G.release(cur);
    this.passCount++;
    return out;
  };

  // --- the whole render ---------------------------------------------

  /* One graded frame on the GPU.
   *
   * opts.pixelScale mirrors the server's preview downscale, so pass
   * previewWidth / sourceWidth and the pixel denominated parameters get
   * scaled the same way the ffmpeg path scales them.
   *
   * Returns the timing breakdown and the stage report, so a caller never has
   * to ask separately whether what it just drew is trustworthy. */
  Instance.prototype.render = function (config, opts) {
    opts = opts || {};
    if (!this.src) throw new Error("StudioGPU: setSource has not been called");
    var gl = this.gl, G = this.G;
    var cfg = this.prepareConfig(config, opts);
    var plan = chainPlan(cfg);
    var report = stageReport(cfg);
    var W = this.src.w, H = this.src.h;
    this.passCount = 0;
    var t0 = now();

    /* The colour head, the layer stack and the look.
     *
     * With no layers this is ONE fused pass, exactly the pass that shipped
     * before layers existed, so every config that has no stack renders the
     * bytes it always did. With a stack the same shader is invoked twice, the
     * layers running in between, which is arithmetically identical: every
     * head stage is guarded by its own uniform and an RGBA32F render target
     * hands a value to the next pass bit for bit. */
    var before = activeLayers(cfg, "before_look");
    var after = activeLayers(cfg, "after_look");
    var cur;
    if (!before.length && !after.length) {
      cur = this.colourPass(cfg, W, H);
    } else {
      cur = this.colourPass(cfg, W, H, { lookOff: true });
      cur = this.layerStack(cur, cfg, before, W, H);
      if (lookActive(cfg)) {
        var looked = this.colourPass(cfg, W, H,
                                     { lookOnly: true, srcTex: cur.tex });
        G.release(cur);
        cur = looked;
      }
      cur = this.layerStack(cur, cfg, after, W, H);
    }

    if (cfg.fx.halation.enabled) cur = this.glowPass(cur, cfg.fx.halation, 4, W, H);
    if (cfg.fx.bloom.enabled) cur = this.glowPass(cur, cfg.fx.bloom, 8, W, H);
    if (cfg.fx.radial_blur.enabled) cur = this.radialPass(cur, cfg.fx.radial_blur, W, H);
    var amt = pyRound(+cfg.fx.rgb_split.amount);
    if (cfg.fx.rgb_split.enabled && amt) cur = this.shiftPass(cur, amt, W, H);

    var codeMax = 0, yuv = 0;
    if (plan.quantise === "rgb8") codeMax = 255;
    else if (plan.quantise === "yuv8") { codeMax = 255; yuv = 1; }
    else if (plan.quantise === "yuv16") { codeMax = 65535; yuv = 1; }

    if (plan.quantiseAt === "vignette") cur = this.toCode(cur, W, H, codeMax, yuv);
    if (plan.vignette) cur = this.vignettePass(cur, cfg.fx.vignette, W, H, codeMax, yuv);
    if (plan.quantiseAt === "detail") cur = this.toCode(cur, W, H, codeMax, yuv);

    // Grain (C3): the plate is whatever /api/grain/plate served for this
    // exact config and size, prefetched by ready(). If it is missing this
    // throws, the same contract colourPass already uses for a LUT slot.
    if (plan.grain) cur = this.grainPass(cur, cfg, W, H, codeMax);
    if (plan.detail) cur = this.detailPass(cur, cfg, plan, W, H, codeMax);
    if (cfg.letterbox.enabled) cur = this.letterboxPass(cur, cfg, W, H, codeMax, yuv);

    // An 8 bit chain is captured HERE, in code space, before the conversion
    // back to normalised RGB. ffmpeg's own graph never makes that conversion:
    // when a config drops to 8 bit its link stays rgb24 (vignette alone) or
    // yuv444p (vignette with detail) all the way into the encoder, and going
    // RGB -> YUV from a 16 bit buffer instead lands about 3.5 of 1023 brighter
    // in luma. Measured with ffmpeg -v debug on this engine's own graph.
    if (opts.want16 && codeMax === 255) {
      this.keepOutput(cur, W, H, yuv ? "yuv444p" : "rgb24");
    }

    if (codeMax) cur = this.fromCode(cur, W, H, codeMax, yuv);

    // The finished 8 bit picture. When the tail never left 16 bit this is the
    // measured format=rgb24 table; when it dropped to 8 bit earlier the value
    // is already on the 8 bit lattice and the table is an identity on it.
    if (!this.out || this.out.w !== W || this.out.h !== H) {
      if (this.out) { gl.deleteTexture(this.out.tex); gl.deleteFramebuffer(this.out.fbo); }
      this.out = G.acquire(W, H, "u8");
      this.out.pooled = false;
    }
    var self = this;
    var prog = G.program("tail8", FS_TAIL8);
    gl.useProgram(prog);
    G.bindTex(prog, "uTex", 0, cur.tex);
    G.bindTex(prog, "uTail", 1, this.tail8());
    gl.uniform1i(G.loc(prog, "uFlip"), 0);
    gl.uniform1i(G.loc(prog, "uDirect"), codeMax === 255 ? 1 : 0);
    gl.uniform2i(G.loc(prog, "uSize"), W, H);
    G.draw(this.out);
    // opts.want16 keeps the render's own picture as well as the 8 bit preview
    // one. `cur` here is what ffmpeg's graph holds BEFORE the closing
    // format=rgb24, which is the only place a final render can read without
    // banding. When the chain already dropped to 8 bit the capture happened
    // higher up, in code space, for the reason in keepOutput's comment.
    if (opts.want16 && codeMax !== 255) this.keepOutput(cur, W, H, "gbrp16le");
    G.release(cur);
    this.passCount++;

    // Present. The canvas is bottom row first, the picture is top row first,
    // so this pass is the only place a flip belongs.
    if (this.canvas) {
      if (this.canvas.width !== W || this.canvas.height !== H) {
        this.canvas.width = W; this.canvas.height = H;
      }
      var copy = G.program("copy", FS_COPY);
      gl.useProgram(copy);
      G.bindTex(copy, "uTex", 0, this.out.tex);
      gl.uniform1i(G.loc(copy, "uFlip"), 1);
      gl.uniform2i(G.loc(copy, "uSize"), W, H);
      G.draw(null);
    }

    gl.finish();
    var ms = now() - t0;
    this.lastTiming = { ms: ms, passes: this.passCount, width: W, height: H };
    return { ms: ms, passes: this.passCount, width: W, height: H,
             config: cfg, plan: plan, report: report };
  };

  /* rgb24, top row first, byte for byte comparable with the raw body of
   * POST /api/frame {"format":"raw"}. */
  Instance.prototype.readPixels = function () {
    if (!this.out) throw new Error("StudioGPU: nothing rendered yet");
    var gl = this.gl, W = this.out.w, H = this.out.h;
    var rgba = new Uint8Array(W * H * 4);
    gl.bindFramebuffer(gl.FRAMEBUFFER, this.out.fbo);
    gl.readPixels(0, 0, W, H, gl.RGBA, gl.UNSIGNED_BYTE, rgba);
    var rgb = new Uint8Array(W * H * 3);
    for (var i = 0, j = 0; i < W * H; i++, j += 3) {
      rgb[j] = rgba[i * 4];
      rgb[j + 1] = rgba[i * 4 + 1];
      rgb[j + 2] = rgba[i * 4 + 2];
    }
    return rgb;
  };

  // --- the final render's own readback ------------------------------

  /* Pack the finished picture into one integer target laid out exactly like
   * the raw frame ffmpeg wants, so the CPU never touches a pixel.
   *
   * Three layouts, and which one is right is not a preference: it is the
   * format ffmpeg's own graph is carrying at the point this port cuts it,
   * read out of `ffmpeg -v debug` on the engine's real graph.
   *   gbrp16le  16 bit planar G, B, R, stacked as a W by 3H target. The
   *             chain never left 16 bit (no vignette).
   *   yuv444p   8 bit planar Y, U, V, same stacking. vignette with detail:
   *             ffmpeg runs both in yuv444p full range.
   *   rgb24     8 bit packed R, G, B as a 3W by H target. vignette alone.
   * Handing the encoder a packed 16 bit RGB frame instead of the planar one
   * differs from the single process render by up to 17.4 of 255; handing it a
   * 16 bit frame where ffmpeg had an 8 bit one costs about 3.5 of 1023 of
   * luma. Both were measured on this project's own footage.
   *
   * The 16 bit quantisation is floor(v * 65535 + 0.5), the same rounding
   * FS_TAIL8 applies before its 8 bit table, so the render and the preview
   * agree about what code a value is. */
  var FS_PACK = src([
    "#version 300 es",
    "precision highp float;",
    "precision highp int;",
    "uniform sampler2D uTex;",
    "uniform int uHeight;",       // frame height, so plane = y / uHeight
    "uniform int uPacked;",       // 1: R,G,B triples along x. 0: stacked planes
    "uniform float uScale;",      // 65535 for normalised input, 1 for codes
    "uniform float uMax;",        // 65535 or 255
    "uniform ivec3 uOrder;",      // which channel each plane comes from
    "out uvec4 oCol;",
    "void main() {",
    "  ivec2 p = ivec2(gl_FragCoord.xy);",
    "  int plane, x, y;",
    "  if (uPacked == 1) { plane = p.x % 3; x = p.x / 3; y = p.y; }",
    "  else { plane = p.y / uHeight; x = p.x; y = p.y - plane * uHeight; }",
    "  vec3 c = texelFetch(uTex, ivec2(x, y), 0).rgb;",
    "  int ch = plane == 0 ? uOrder.x : (plane == 1 ? uOrder.y : uOrder.z);",
    "  float v = ch == 0 ? c.r : (ch == 1 ? c.g : c.b);",
    "  oCol = uvec4(uint(clamp(floor(v * uScale + 0.5), 0.0, uMax)), 0u, 0u, 0u);",
    "}"
  ]);

  var PACK_LAYOUTS = {
    gbrp16le: { bytes: 2, packed: 0, scale: 65535, max: 65535, order: [1, 2, 0] },
    yuv444p:  { bytes: 1, packed: 0, scale: 1, max: 255, order: [0, 1, 2] },
    rgb24:    { bytes: 1, packed: 1, scale: 1, max: 255, order: [0, 1, 2] }
  };

  Instance.prototype.keepOutput = function (cur, W, H, format) {
    var gl = this.gl, G = this.G;
    var lay = PACK_LAYOUTS[format];
    if (!lay) throw new Error("StudioGPU: no readback layout for " + format);
    var tw = lay.packed ? W * 3 : W;
    var th = lay.packed ? H : H * 3;
    var max = gl.getParameter(gl.MAX_TEXTURE_SIZE);
    if (tw > max || th > max) {
      throw new Error("StudioGPU: a " + W + "x" + H + " frame needs a "
        + tw + "x" + th + " readback target and this GPU stops at " + max);
    }
    if (!this.outRaw || this.outRaw.w !== tw || this.outRaw.h !== th
        || this.outRaw.format !== format) {
      if (this.outRaw) {
        gl.deleteTexture(this.outRaw.tex);
        gl.deleteFramebuffer(this.outRaw.fbo);
      }
      G.scratch();
      var tex = gl.createTexture();
      gl.bindTexture(gl.TEXTURE_2D, tex);
      gl.texImage2D(gl.TEXTURE_2D, 0, lay.bytes === 2 ? gl.R16UI : gl.R8UI,
                    tw, th, 0, gl.RED_INTEGER,
                    lay.bytes === 2 ? gl.UNSIGNED_SHORT : gl.UNSIGNED_BYTE, null);
      nearest(gl, gl.TEXTURE_2D);
      var fbo = gl.createFramebuffer();
      gl.bindFramebuffer(gl.FRAMEBUFFER, fbo);
      gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0,
                              gl.TEXTURE_2D, tex, 0);
      var st = gl.checkFramebufferStatus(gl.FRAMEBUFFER);
      if (st !== gl.FRAMEBUFFER_COMPLETE) {
        throw new Error("StudioGPU: no integer render target for " + format
                        + " (status " + st + ")");
      }
      this.outRaw = { tex: tex, fbo: fbo, w: tw, h: th, format: format,
                      bytes: lay.bytes };
    }
    var prog = G.program("pack", FS_PACK);
    gl.useProgram(prog);
    G.bindTex(prog, "uTex", 0, cur.tex);
    gl.uniform1i(G.loc(prog, "uHeight"), H);
    gl.uniform1i(G.loc(prog, "uPacked"), lay.packed);
    gl.uniform1f(G.loc(prog, "uScale"), lay.scale);
    gl.uniform1f(G.loc(prog, "uMax"), lay.max);
    gl.uniform3i(G.loc(prog, "uOrder"), lay.order[0], lay.order[1], lay.order[2]);
    G.draw(this.outRaw);
    this.passCount++;
    return this.outRaw;
  };

  /* The last render's picture as the exact bytes ffmpeg's rawvideo demuxer
   * wants, with the name of the format it is in.
   *
   * Only valid after render(config, {want16: true}); readPixels() above stays
   * exactly as it was, 8 bit RGB, for the preview and the parity harness. */
  Instance.prototype.readOutput = function () {
    var t = this.outRaw;
    if (!t) {
      throw new Error("StudioGPU: render with {want16: true} before readOutput");
    }
    var gl = this.gl;
    var buf = t.bytes === 2 ? new Uint16Array(t.w * t.h)
                            : new Uint8Array(t.w * t.h);
    gl.bindFramebuffer(gl.FRAMEBUFFER, t.fbo);
    // The default 4 byte row alignment is right for some widths and silently
    // wrong for others, and this is the one place the byte layout has to be
    // exact.
    gl.pixelStorei(gl.PACK_ALIGNMENT, t.bytes);
    gl.readPixels(0, 0, t.w, t.h, gl.RED_INTEGER,
                  t.bytes === 2 ? gl.UNSIGNED_SHORT : gl.UNSIGNED_BYTE, buf);
    gl.pixelStorei(gl.PACK_ALIGNMENT, 4);
    return { data: buf, format: t.format };
  };

  /* Upload an rgb48le source frame without touching it on the CPU.
   *
   * setSource above expands the frame into a Float32Array in JavaScript,
   * which is fine for one still and is 33 million writes per frame at 4K.
   * EXT_texture_norm16 lets the same bytes go straight to the GPU as a
   * normalised RGB16 texture, sampled as value/65535: the identical numbers
   * the float path produces. Falls back to setSource when the extension is
   * missing, so this is a speed path and never a fidelity one. */
  Instance.prototype.setSource16 = function (u16, w, h) {
    var gl = this.gl;
    if (this.norm16 === undefined) {
      this.norm16 = gl.getExtension("EXT_texture_norm16");
    }
    if (!this.norm16) return this.setSource(u16, w, h);
    this.G.scratch();
    if (!this.src || this.src.w !== w || this.src.h !== h || !this.src.norm16) {
      if (this.src) gl.deleteTexture(this.src.tex);
      var tex = gl.createTexture();
      gl.bindTexture(gl.TEXTURE_2D, tex);
      gl.pixelStorei(gl.UNPACK_ALIGNMENT, 2);
      gl.texImage2D(gl.TEXTURE_2D, 0, this.norm16.RGB16_EXT, w, h, 0,
                    gl.RGB, gl.UNSIGNED_SHORT, u16);
      gl.pixelStorei(gl.UNPACK_ALIGNMENT, 4);
      nearest(gl, gl.TEXTURE_2D);
      this.src = { tex: tex, w: w, h: h, norm16: true };
      return this.src;
    }
    gl.bindTexture(gl.TEXTURE_2D, this.src.tex);
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 2);
    gl.texSubImage2D(gl.TEXTURE_2D, 0, 0, 0, w, h, gl.RGB, gl.UNSIGNED_SHORT, u16);
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 4);
    return this.src;
  };

  function now() {
    return (typeof performance !== "undefined" && performance.now)
      ? performance.now() : Date.now();
  }

  // ------------------------------------------------------------------
  // public API
  // ------------------------------------------------------------------

  var StudioGPU = {
    /* Returns an instance, or null with a reason on `StudioGPU.lastError`.
     * Never throws for a missing feature: a caller that cannot get a context
     * is supposed to keep using the ffmpeg path, not to break. */
    create: function (canvas, options) {
      options = options || {};
      var gl = null;
      try {
        gl = canvas.getContext("webgl2", {
          alpha: false, antialias: false, depth: false, stencil: false,
          premultipliedAlpha: false, preserveDrawingBuffer: true
        });
      } catch (e) {
        StudioGPU.lastError = "webgl2 context failed: " + e.message;
        return null;
      }
      if (!gl) {
        StudioGPU.lastError = "this browser has no WebGL2";
        return null;
      }
      // RGBA32F render targets are not optional here. Half float has an 11 bit
      // mantissa, which is an eighth of an 8 bit code, and the chain is up to
      // fifteen stages deep with a blur inside it, so the error would be
      // visible rather than theoretical.
      if (!gl.getExtension("EXT_color_buffer_float")) {
        StudioGPU.lastError = "no EXT_color_buffer_float, so no float render targets";
        return null;
      }
      var inst;
      try {
        inst = new Instance(canvas, gl);
        inst.apiBase = options.apiBase || "";
      } catch (e) {
        StudioGPU.lastError = "init failed: " + e.message;
        return null;
      }
      StudioGPU.lastError = null;
      return inst;
    },

    // Which stages this config can run EXACTLY, which only APPROXIMATELY, and
    // which not at all. Static: no GPU needed to ask.
    stageReport: stageReport,

    // The server's preview parameter scaling, mirrored so a caller can see
    // the numbers the GPU will actually use.
    scaleForPreview: scaleForPreview,

    // The engine defaults, which every partial config is merged over. The
    // caller passes what GET /api/state returns under "defaults" so gpu.js
    // never has to keep its own copy of the engine's defaults in sync.
    setDefaults: function (d) { DEFAULTS = clone(d); },
    defaults: function () { return clone(DEFAULTS); },
    fullConfig: fullConfig,
    chainPlan: chainPlan,

    /* The layer stack, exposed for the UI and for anything that has to read a
     * config the same way the engine reads it. migrateLayers is the pre-layers
     * rewrite (run it BEFORE merging the defaults, or an empty `layers` from
     * DEFAULTS makes an old config look new and its secondary is dropped);
     * LAYER_DEFAULTS is the merge base for one layer, which the window editor
     * needs so a half written layer never resolves to NaN. */
    migrateLayers: migrateLayers,
    LAYER_DEFAULTS: LAYER_DEFAULTS,
    configLayers: configLayers,
    layerActive: layerActive,

    // Exposed for the parity harness, which reports what it compared.
    notes: STAGE_NOTES,
    version: "1"
  };

  global.StudioGPU = StudioGPU;
})(typeof window !== "undefined" ? window : this);
