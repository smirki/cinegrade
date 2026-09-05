/* The hue curve editor: Hue vs Hue, Hue vs Sat, Hue vs Lum, Lum vs Sat, Sat vs Sat.
 *
 * A separate widget from Ctl.curveEditor rather than a flag on it, because
 * three things differ and every one of them is structural:
 *
 *   1. The neutral is a FLAT line, not the diagonal. y is an offset (Hue vs
 *      Hue, in turns) or a multiplier (the other four, around 1), so the
 *      identity is "no points at all" and not "two points on y = x".
 *   2. The three hue axes are PERIODIC. There are no endpoints to pin, a point
 *      can sit anywhere including on the seam, and the spline wraps.
 *   3. The y range is not [0, 1]. A multiplier runs 0 to 2 and an offset runs
 *      -0.25 to +0.25 of a turn, so the canvas needs a per curve scale.
 *
 * The spline is the same one grade/slice.py bakes: Fritsch and Carlson
 * monotone cubic, scipy's _edge_case at true ends, and a wrapped node on each
 * side for the periodic axes so there are no true ends at all. The line drawn
 * here is therefore the line that renders, which is the whole point of drawing
 * it. The cube itself is baked by the server, never here.
 */
(function (global) {
  "use strict";

  /* Mirrors grade/slice.py CURVE_AXES. `lo` and `hi` are the canvas range, not
   * a clamp on what a config may hold: a preset written by hand can go past
   * them and will draw off the top rather than being silently rewritten. */
  var AXES = {
    hue_hue: {
      tab: "H/H", name: "Hue vs Hue", periodic: true, neutral: 0,
      lo: -0.25, hi: 0.25, xLabel: "hue", yLabel: "hue offset, turns",
      hueX: true
    },
    hue_sat: {
      tab: "H/S", name: "Hue vs Sat", periodic: true, neutral: 1,
      lo: 0, hi: 2, xLabel: "hue", yLabel: "saturation x", hueX: true
    },
    hue_lum: {
      tab: "H/L", name: "Hue vs Lum", periodic: true, neutral: 1,
      lo: 0, hi: 2, xLabel: "hue", yLabel: "brightness x", hueX: true
    },
    lum_sat: {
      tab: "L/S", name: "Lum vs Sat", periodic: false, neutral: 1,
      lo: 0, hi: 2, xLabel: "luma (Rec.709)", yLabel: "saturation x"
    },
    sat_sat: {
      tab: "S/S", name: "Sat vs Sat", periodic: false, neutral: 1,
      lo: 0, hi: 2, xLabel: "saturation", yLabel: "saturation x"
    }
  };
  var KEYS = ["hue_hue", "hue_sat", "hue_lum", "lum_sat", "sat_sat"];

  function clamp(v, a, b) { return v < a ? a : (v > b ? b : v); }
  function sgn(x) { return x > 0 ? 1 : (x < 0 ? -1 : 0); }

  /* scipy's _edge_case, the end slope ffmpeg's pchip uses. Only reached on the
   * two non periodic axes; a loop has no ends. */
  function edgeSlope(h0, h1, m0, m1) {
    var d = ((2 * h0 + h1) * m0 - h0 * m1) / (h0 + h1);
    if (sgn(d) !== sgn(m0)) return 0;
    if (sgn(m0) !== sgn(m1) && Math.abs(d) > 3 * Math.abs(m0)) return 3 * m0;
    return d;
  }

  function interiorSlopes(h, d, n) {
    var m = new Array(n), k;
    for (k = 0; k < n; k++) m[k] = 0;
    for (k = 1; k < n - 1; k++) {
      if (d[k - 1] * d[k] <= 0) continue;
      var w1 = 2 * h[k] + h[k - 1], w2 = h[k] + 2 * h[k - 1];
      m[k] = (w1 + w2) / (w1 / d[k - 1] + w2 / d[k]);
    }
    return m;
  }

  function cleanPoints(pts, period) {
    var out = [], i;
    for (i = 0; i < (pts || []).length; i++) {
      var x = +pts[i][0];
      if (period) x = ((x % period) + period) % period;
      out.push([x, +pts[i][1]]);
    }
    out.sort(function (a, b) { return a[0] - b[0]; });
    var kept = [];
    for (i = 0; i < out.length; i++) {
      if (kept.length && Math.abs(out[i][0] - kept[kept.length - 1][0]) < 1e-9) {
        kept[kept.length - 1] = out[i];
      } else {
        kept.push(out[i]);
      }
    }
    return kept;
  }

  function hermite(x, x0, x1, y0, y1, m0, m1) {
    var h = x1 - x0, t = (x - x0) / h, t2 = t * t, t3 = t2 * t;
    return (2 * t3 - 3 * t2 + 1) * y0 + (t3 - 2 * t2 + t) * h * m0
      + (-2 * t3 + 3 * t2) * y1 + (t3 - t2) * h * m1;
  }

  function findSpan(xs, x) {
    var i = 0;
    while (i < xs.length - 2 && x > xs[i + 1]) i++;
    return i;
  }

  function evalOpen(pts, xs, neutral) {
    var p = cleanPoints(pts, 0), n = p.length, i;
    if (n === 0) return xs.map(function () { return neutral; });
    if (n === 1) return xs.map(function () { return p[0][1]; });
    var px = p.map(function (q) { return q[0]; });
    var py = p.map(function (q) { return q[1]; });
    var h = [], d = [];
    for (i = 0; i < n - 1; i++) {
      h.push(Math.max(1e-9, px[i + 1] - px[i]));
      d.push((py[i + 1] - py[i]) / h[i]);
    }
    if (n === 2) {
      return xs.map(function (x) { return py[0] + (x - px[0]) * d[0]; });
    }
    var m = interiorSlopes(h, d, n);
    m[0] = edgeSlope(h[0], h[1], d[0], d[1]);
    m[n - 1] = edgeSlope(h[n - 2], h[n - 3], d[n - 2], d[n - 3]);
    return xs.map(function (x) {
      if (x <= px[0]) return py[0];
      if (x >= px[n - 1]) return py[n - 1];
      var i2 = findSpan(px, x);
      return hermite(x, px[i2], px[i2 + 1], py[i2], py[i2 + 1], m[i2], m[i2 + 1]);
    });
  }

  function evalPeriodic(pts, xs, neutral) {
    var p = cleanPoints(pts, 1), n = p.length, i;
    if (n === 0) return xs.map(function () { return neutral; });
    if (n === 1) return xs.map(function () { return p[0][1]; });
    var ex = [p[n - 1][0] - 1], ey = [p[n - 1][1]];
    for (i = 0; i < n; i++) { ex.push(p[i][0]); ey.push(p[i][1]); }
    ex.push(p[0][0] + 1); ey.push(p[0][1]);
    var en = ex.length, h = [], d = [];
    for (i = 0; i < en - 1; i++) {
      h.push(Math.max(1e-9, ex[i + 1] - ex[i]));
      d.push((ey[i + 1] - ey[i]) / h[i]);
    }
    var m = interiorSlopes(h, d, en);
    return xs.map(function (x) {
      var w = ((x - ex[0]) % 1 + 1) % 1 + ex[0];
      var i2 = findSpan(ex, w);
      return hermite(w, ex[i2], ex[i2 + 1], ey[i2], ey[i2 + 1], m[i2], m[i2 + 1]);
    });
  }

  function evalCurve(key, pts, xs) {
    var a = AXES[key];
    return a.periodic ? evalPeriodic(pts, xs, a.neutral)
      : evalOpen(pts, xs.map(function (x) { return clamp(x, 0, 1); }), a.neutral);
  }

  /* HSV to a CSS colour, for the hue strip under the three hue axes. Only
   * decoration: nothing downstream reads it. */
  function hueCss(t) {
    var h = t * 6, c = 1, x = 1 - Math.abs((h % 2) - 1), r, g, b;
    var s = Math.floor(h) % 6;
    if (s === 0) { r = c; g = x; b = 0; } else if (s === 1) { r = x; g = c; b = 0; }
    else if (s === 2) { r = 0; g = c; b = x; } else if (s === 3) { r = 0; g = x; b = c; }
    else if (s === 4) { r = x; g = 0; b = c; } else { r = c; g = 0; b = x; }
    return "rgb(" + Math.round(r * 215) + "," + Math.round(g * 215) + ","
      + Math.round(b * 215) + ")";
  }

  function editor(opts) {
    var canvas = opts.canvas;
    var ctx = canvas.getContext("2d");
    var key = KEYS[0];
    var curves = opts.value;
    var dragIndex = -1;
    var W = 0, H = 0;

    function axis() { return AXES[key]; }

    function size() {
      var r = canvas.getBoundingClientRect();
      var dpr = global.devicePixelRatio || 1;
      W = Math.max(120, Math.round(r.width));
      H = Math.max(110, Math.round(r.width * 0.62));
      canvas.style.height = H + "px";
      canvas.width = W * dpr;
      canvas.height = H * dpr;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }

    function toY(v) {
      var a = axis();
      return (1 - (v - a.lo) / (a.hi - a.lo)) * H;
    }
    function fromY(py) {
      var a = axis();
      return clamp(a.lo + (1 - py / H) * (a.hi - a.lo), a.lo, a.hi);
    }
    function toPx(p) { return [p[0] * W, toY(p[1])]; }

    function draw() {
      var a = axis(), i;
      ctx.clearRect(0, 0, W, H);
      ctx.fillStyle = "#141414";
      ctx.fillRect(0, 0, W, H);

      if (a.hueX) {
        // A hue axis is unreadable without saying which hue is where.
        for (i = 0; i < W; i += 2) {
          ctx.fillStyle = hueCss(i / W);
          ctx.fillRect(i, H - 6, 2, 6);
        }
      }

      ctx.strokeStyle = "#2a2a2a";
      ctx.lineWidth = 1;
      for (i = 1; i < 4; i++) {
        ctx.beginPath();
        ctx.moveTo(Math.round(W * i / 4) + 0.5, 0);
        ctx.lineTo(Math.round(W * i / 4) + 0.5, H);
        ctx.stroke();
      }
      // The neutral: where the curve does nothing at all.
      ctx.strokeStyle = "#4a4a2a";
      ctx.beginPath();
      ctx.moveTo(0, Math.round(toY(a.neutral)) + 0.5);
      ctx.lineTo(W, Math.round(toY(a.neutral)) + 0.5);
      ctx.stroke();

      KEYS.forEach(function (k) {
        var pts = curves[k] || [];
        if (k !== key && !pts.length) return;
        var xs = [], x;
        for (x = 0; x <= W; x += 2) xs.push(x / W);
        var ys = evalCurve(k, pts, xs);
        // Another curve's y is on its own scale, so it is only drawn when it
        // shares this one's range. Otherwise it would be a lie about height.
        if (k !== key && AXES[k].hi !== a.hi) return;
        ctx.strokeStyle = k === key ? "#e0e0e0" : "#6a6a6a";
        ctx.globalAlpha = k === key ? 1 : 0.3;
        ctx.lineWidth = k === key ? 1.6 : 1;
        ctx.beginPath();
        for (var j = 0; j < xs.length; j++) {
          var py = clamp(toY(ys[j]), -20, H + 20);
          if (j === 0) ctx.moveTo(xs[j] * W, py); else ctx.lineTo(xs[j] * W, py);
        }
        ctx.stroke();
        ctx.globalAlpha = 1;
      });

      (curves[key] || []).forEach(function (p, i2) {
        var q = toPx(p);
        ctx.fillStyle = i2 === dragIndex ? "#ffffff" : "#e0e0e0";
        ctx.strokeStyle = "#000";
        ctx.lineWidth = 1;
        ctx.beginPath(); ctx.arc(q[0], q[1], 3.5, 0, 6.2832);
        ctx.fill(); ctx.stroke();
      });
    }

    function nearest(px, py) {
      var best = -1, bd = 1e9;
      (curves[key] || []).forEach(function (p, i) {
        var q = toPx(p);
        var d = Math.hypot(q[0] - px, q[1] - py);
        if (d < bd) { bd = d; best = i; }
      });
      return bd < 10 ? best : -1;
    }

    function localPos(ev) {
      var r = canvas.getBoundingClientRect();
      return [ev.clientX - r.left, ev.clientY - r.top];
    }

    function commit(c) { opts.onChange(key, curves[key], c); }

    canvas.addEventListener("contextmenu", function (ev) { ev.preventDefault(); });

    // Pointer events, not mouse events (contract C8, mobile mode): a touch
    // drag produces no stream of compatibility mousemove events, so a curve
    // bound to mousedown plus document mousemove would see the tap and never
    // the drag. See the header comment in controls.js for the full reasoning;
    // the CSS half is `touch-action: none` on this canvas in style.css.
    canvas.addEventListener("pointerdown", function (ev) {
      ev.preventDefault();
      var p = localPos(ev);
      var idx = nearest(p[0], p[1]);
      var pts = curves[key] || (curves[key] = []);

      // Every point is removable here, unlike the RGB curve editor: there are
      // no pinned black and white ends on a correction curve, and removing the
      // last point is how you get back to the identity.
      if ((ev.altKey || ev.button === 2) && idx >= 0) {
        pts.splice(idx, 1);
        draw(); commit(true);
        return;
      }
      if (idx < 0) {
        if (ev.button !== 0) return;
        var v = [clamp(p[0] / W, 0, 1), fromY(p[1])];
        var at = 0;
        while (at < pts.length && pts[at][0] < v[0]) at++;
        pts.splice(at, 0, v);
        idx = at;
      }
      dragIndex = idx;
      draw();

      function move(e) {
        var q = localPos(e);
        var arr = curves[key];
        var nx = clamp(q[0] / W, 0, 1);
        // Points keep their order in x so the spline stays single valued. On a
        // periodic axis the two outermost points are still free to reach the
        // seam, they just cannot pass their neighbours.
        if (dragIndex > 0) nx = Math.max(nx, arr[dragIndex - 1][0] + 0.004);
        if (dragIndex < arr.length - 1) nx = Math.min(nx, arr[dragIndex + 1][0] - 0.004);
        arr[dragIndex] = [nx, fromY(q[1])];
        draw();
        commit(false);
      }
      function up() {
        document.removeEventListener("pointermove", move);
        document.removeEventListener("pointerup", up);
        document.removeEventListener("pointercancel", up);
        dragIndex = -1;
        draw();
        commit(true);
      }
      document.addEventListener("pointermove", move);
      document.addEventListener("pointerup", up);
      document.addEventListener("pointercancel", up);
    });

    canvas.addEventListener("dblclick", function (ev) {
      ev.preventDefault();
      curves[key] = [];
      draw();
      commit(true);
    });

    size(); draw();

    return {
      resize: function () { size(); draw(); },
      setChannel: function (k) { if (AXES[k]) { key = k; draw(); } },
      channel: function () { return key; },
      set: function (next) { curves = next; draw(); },
      redraw: draw
    };
  }

  global.HueCurve = {
    AXES: AXES, KEYS: KEYS, editor: editor, evalCurve: evalCurve
  };
})(window);
