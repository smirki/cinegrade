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
    look: { lut: null, mix: 1.0 },
    detail: { soften: 0.0, sharpen: 0.0 },
    fx: {
      halation: { enabled: false, threshold: 0.62, sigma: 26, strength: 0.55, tint: [1.0, 0.34, 0.16] },
      bloom: { enabled: false, threshold: 0.72, sigma: 70, strength: 0.28, tint: [1.0, 0.97, 0.92] },
      rgb_split: { enabled: false, amount: 1.6 },
      radial_blur: { enabled: false, sigma: 9, start: 0.55, end: 1.0 },
      vignette: { enabled: false, amount: 0.45, radius: 0.85 }
    },
    grain: { enabled: false, strength: 40, size: 3, opacity: 0.5 },
    letterbox: { enabled: false, aspect: 2.39 },
    curves: {
      enabled: false, interp: "pchip",
      master: [[0.0, 0.0], [1.0, 1.0]], r: [[0.0, 0.0], [1.0, 1.0]],
      g: [[0.0, 0.0], [1.0, 1.0]], b: [[0.0, 0.0], [1.0, 1.0]]
    },
    secondary: {
      enabled: false, show_mask: false, invert: false,
      hue_center: 30.0, hue_width: 40.0, hue_soft: 15.0,
      sat_low: 0.10, sat_high: 1.0, sat_soft: 0.10,
      lum_low: 0.0, lum_high: 1.0, lum_soft: 0.10,
      hue_shift: 0.0, sat_gain: 1.0, lum_gain: 1.0,
      tint: [0.0, 0.0, 0.0], strength: 1.0
    },
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
  function fullConfig(cfg) { return deepMerge(DEFAULTS, cfg || {}); }

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
   * This is the one place Ctl.pchipEval in controls.js does NOT agree with the
   * filter: the editor uses the plain end slope m0, ffmpeg uses this. On a
   * four point S curve the gap reaches about 1.3 code values at 8 bit inside
   * the first and last segments, so the render path has to use this version or
   * the preview lies about the curve the user just drew. The editor is still
   * the right thing to draw with; it is just slightly wrong at the ends. */
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
    var detail = soften || sharpen;
    var plan = {
      vignette: vignette, soften: soften, sharpen: sharpen, detail: detail,
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

  var STAGE_NOTES = {
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
    secondary: "The server bakes the qualifier to a 33 cube and the GPU applies "
             + "it tetrahedrally, so this is the same table ffmpeg reads.",
    look: "3D LUT, tetrahedral, then blend=normal, which is dst = lut*mix + "
        + "pre_lut*(1-mix).",
    halation: "Highlight pass, swscale bilinear downscale to a quarter, gblur, "
            + "swscale bicubic upscale, tint, then screen at strength. The "
            + "gblur is ffmpeg's IIR recursion, not a convolution.",
    bloom: "Same structure as halation at an eighth resolution.",
    radial_blur: "gblur plus maskedmerge against a radial ramp. The ramp is "
               + "generated by ffmpeg as an 8 bit PNG, so it is an 8 bit matte "
               + "in the real path too.",
    rgb_split: "Whole pixel channel shift with edge smear. The engine rounds "
             + "the amount to an integer, so this is exact by construction.",
    vignette: "cos^4 falloff with ffmpeg's LCG dither. This is where the chain "
            + "drops to 8 bit, because vignette has no 16 bit pixel format. On "
            + "its own it measures exact: no channel of any pixel off by more "
            + "than 1.",
    grain: "NOT PORTED. ffmpeg's noise filter seeds itself from the clock, so "
         + "two ffmpeg runs of the same config do not match each other either. "
         + "Pixel parity with it is impossible in principle, not just here.",
    detail: "gblur then unsharp. unsharp is YUV only, so the picture takes an "
          + "RGB to YUV round trip and only Y is sharpened. Sharpen on its own "
          + "runs at 16 bit, where ffmpeg's own scaling makes it 256 times "
          + "weaker, so that lane barely moves the picture and measures exact.",
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
          ? "Note: controls.js Ctl.pchipEval uses a different end slope from "
          + "ffmpeg, so the drawn curve and the rendered curve differ by up to "
          + "about 1.3 code values inside the first and last segment. The GPU "
          + "path follows ffmpeg, not the editor."
          : "");
    add("secondary", "Secondary", !!(cfg.secondary && cfg.secondary.enabled), "exact");
    add("look", "Look", !!cfg.look.lut, "exact");

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
    add("grain", "Grain", plan.grain, "unsupported");
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
   * curves, secondary and the look mix.
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
    "uniform sampler3D uSec;",
    "uniform sampler3D uLook;",
    "uniform sampler2D uCurve;",
    "uniform sampler2D uBlc;",
    "uniform ivec4 uSizes;",          // cstIn, cstOut, secondary, look; 0 = off
    "uniform int uHasCurve;",
    "uniform int uHasBlc;",
    "uniform float uLookMix;",
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
    "  if (uSizes.z > 0) c = q16(tetra(uSec, uSizes.z, c));",
    "  if (uSizes.w > 0) {",
    "    vec3 lk = q16(tetra(uLook, uSizes.w, c));",
    // blend=normal is dst = top*opacity + bottom*(1-opacity) in float,
    // truncated back to an integer code. The LUT branch is input 0.
    "    vec3 lc = floor(lk * CODE + 0.5), pc = floor(c * CODE + 0.5);",
    "    c = floor(lc * uLookMix + pc * (1.0 - uLookMix)) / CODE;",
    "  }",
    "  oCol = vec4(c, 1.0);",
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
   * The ramp is not a clean 0 to 1 gradient and modelling it as one is wrong
   * by up to 19 code values. cinegrade bakes it with geq on a limited range
   * YUV surface and then converts to gray, so the real chain is: truncate
   * 255*ramp to a code, expand limited to full with round((v-16)*255/219),
   * then widen 8 bit to 16 bit by a shift of 8 (NOT by 257, so the matte tops
   * out at 65280 and the blur never fully replaces the sharp layer). All
   * three steps were measured against a mask ffmpeg generated; with them the
   * matte matches exactly, without them it does not. */
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
    "  float m8 = clamp(floor((v8 - 16.0) * (255.0 / 219.0) + 0.5), 0.0, 255.0);",
    "  uint m = uint(m8) * 256u;",
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
    if (cfg.secondary && cfg.secondary.enabled) {
      reqs.push({ slot: "secondary", key: "sec:" + stableJson(cfg.secondary),
                  body: { kind: "secondary", config: cfg.secondary } });
    }
    if (cfg.look.lut) {
      reqs.push({ slot: "look", key: "look:" + cfg.look.lut,
                  body: { kind: "look", name: cfg.look.lut } });
    }
    return reqs;
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
    return Promise.all(reqs.map(function (r) { return self.lut(r.key, r.body); }))
      .then(function () { return true; });
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
   * is what ffmpeg's parser actually sees, not the full precision float. */
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
      pts.push([0, 0]);
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
      pts.push([1, 1]);
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

  Instance.prototype.colourPass = function (cfg, W, H) {
    var gl = this.gl, G = this.G, self = this;
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
    G.bindTex(prog, "uSrc", 0, this.src.tex);
    var sizes = [bindLut("uCstIn", 1, "cstIn"), bindLut("uCstOut", 2, "cstOut"),
                 bindLut("uSec", 3, "secondary"), bindLut("uLook", 4, "look")];
    gl.uniform4i(G.loc(prog, "uSizes"), sizes[0], sizes[1], sizes[2], sizes[3]);
    gl.uniform1f(G.loc(prog, "uLookMix"), fmt(+cfg.look.mix, 4));

    var offR = +cv.exposure + +p.temperature;
    var offG = +cv.exposure + +p.tint;
    var offB = +cv.exposure - +p.temperature;
    var hasLog = Math.abs(offR) > 1e-6 || Math.abs(offG) > 1e-6 || Math.abs(offB) > 1e-6;
    gl.uniform1i(G.loc(prog, "uHasLog"), hasLog ? 1 : 0);
    gl.uniform3f(G.loc(prog, "uLogOff"),
                 fmt(offR * APPLE_LOG_STOP, 6) * LM,
                 fmt(offG * APPLE_LOG_STOP, 6) * LM,
                 fmt(offB * APPLE_LOG_STOP, 6) * LM);

    var lift = triplet(p.lift, 0), gain = triplet(p.gain, 1);
    var hasLift = lift.some(nz) || gain.some(function (v) { return Math.abs(v - 1) > 1e-6; });
    gl.uniform1i(G.loc(prog, "uHasLift"), hasLift ? 1 : 0);
    gl.uniform3f(G.loc(prog, "uLiftA"), fmt(lift[0], 4) * LM, fmt(lift[1], 4) * LM,
                 fmt(lift[2], 4) * LM);
    gl.uniform3f(G.loc(prog, "uLiftB"), fmt(gain[0] - lift[0], 4),
                 fmt(gain[1] - lift[1], 4), fmt(gain[2] - lift[2], 4));

    var pivot = (p.pivot === null || p.pivot === undefined)
      ? MID_GREY_CODE[cv.working_space] : +p.pivot;
    var contrast = +p.contrast;
    gl.uniform1i(G.loc(prog, "uHasContrast"), Math.abs(contrast - 1) > 1e-6 ? 1 : 0);
    gl.uniform2f(G.loc(prog, "uContrast"), fmt(contrast, 4), fmt(pivot, 4) * LM);

    var bright = triplet(p.brightness, 0);
    gl.uniform1i(G.loc(prog, "uHasBright"), bright.some(nz) ? 1 : 0);
    gl.uniform3f(G.loc(prog, "uBright"), fmt(bright[0], 4) * LM,
                 fmt(bright[1], 4) * LM, fmt(bright[2], 4) * LM);

    var gamma = triplet(p.gamma, 1);
    var hasGamma = gamma.some(function (v) { return Math.abs(v - 1) > 1e-6; });
    gl.uniform1i(G.loc(prog, "uHasGamma"), hasGamma ? 1 : 0);
    gl.uniform3f(G.loc(prog, "uInvGamma"), fmt(1 / gamma[0], 4), fmt(1 / gamma[1], 4),
                 fmt(1 / gamma[2], 4));

    var sat = +p.saturation, lr = 0.2126, lg = 0.7152, lb = 0.0722, iv = 1 - sat;
    gl.uniform1i(G.loc(prog, "uHasSat"), Math.abs(sat - 1) > 1e-6 ? 1 : 0);
    gl.uniform3f(G.loc(prog, "uSatR"), fmt(iv * lr + sat, 5), fmt(iv * lg, 5), fmt(iv * lb, 5));
    gl.uniform3f(G.loc(prog, "uSatG"), fmt(iv * lr, 5), fmt(iv * lg + sat, 5), fmt(iv * lb, 5));
    gl.uniform3f(G.loc(prog, "uSatB"), fmt(iv * lr, 5), fmt(iv * lg, 5), fmt(iv * lb + sat, 5));

    gl.uniform1i(G.loc(prog, "uHasVib"), Math.abs(+p.vibrance) > 1e-6 ? 1 : 0);
    gl.uniform1f(G.loc(prog, "uVib"), fmt(+p.vibrance, 4));

    var bl = +p.black_lift, hr = +p.highlight_rolloff;
    var hasBlc = Math.abs(bl) > 1e-6 || Math.abs(hr) > 1e-6;
    // Built first, bound second, because building one binds it somewhere.
    var blcTex = hasBlc ? this.blcTexture(bl, hr, pivot) : this.blcTexture(0, 0, 0.5);
    var curve = this.curveState(cfg);
    gl.uniform1i(G.loc(prog, "uHasBlc"), hasBlc ? 1 : 0);
    G.bindTex(prog, "uBlc", 5, blcTex);
    gl.uniform1i(G.loc(prog, "uHasCurve"), curve.has ? 1 : 0);
    G.bindTex(prog, "uCurve", 6, curve.tex);

    var out = G.acquire(W, H);
    G.draw(out);
    this.passCount++;
    return out;
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
    return cur;
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

    var cur = this.colourPass(cfg, W, H);

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

    // Grain is deliberately absent. See STAGE_NOTES.grain.
    if (plan.detail) cur = this.detailPass(cur, cfg, plan, W, H, codeMax);
    if (cfg.letterbox.enabled) cur = this.letterboxPass(cur, cfg, W, H, codeMax, yuv);

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

    // Exposed for the parity harness, which reports what it compared.
    notes: STAGE_NOTES,
    version: "1"
  };

  global.StudioGPU = StudioGPU;
})(typeof window !== "undefined" ? window : this);
