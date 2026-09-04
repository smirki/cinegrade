/* The parameter schema.
 *
 * One entry per leaf in cinegrade's DEFAULTS dict, grouped by pipeline stage
 * in node order. app.js audits this list against the DEFAULTS the server sends
 * and complains in the console if a parameter exists in the engine but has no
 * control here, so the panel cannot quietly fall behind the engine. */

(function (global) {
  "use strict";

  var S = function (path, label, min, max, step, def, extra) {
    var o = {
      kind: "slider", path: path, label: label, min: min, max: max,
      step: step, def: def, precision: 3, bipolar: false
    };
    for (var k in (extra || {})) o[k] = extra[k];
    return o;
  };
  var SEL = function (path, label, options, extra) {
    var o = { kind: "select", path: path, label: label, options: options };
    for (var k in (extra || {})) o[k] = extra[k];
    return o;
  };
  var CHK = function (path, label, extra) {
    var o = { kind: "check", path: path, label: label };
    for (var k in (extra || {})) o[k] = extra[k];
    return o;
  };

  global.SCHEMA = [
    {
      id: "convert", node: "1", name: "Convert",
      note: "Apple Log in, Rec.709 out. Exposure sits here on purpose: on a "
          + "log signal a constant offset is exactly a stop, so this is where "
          + "highlights are actually recovered rather than stretched. Set the "
          + "working space to rec709 for footage that is already display "
          + "referred, such as an mp4 or a Rec.709 export coming back for FX.",
      controls: [
        SEL(["convert", "working_space"], "working space", [
          { value: "dwg", label: "dwg (CST in, grade, CST out)" },
          { value: "direct", label: "direct (one LUT to 709)" },
          { value: "rec709", label: "rec709 (source is already 709)" }
        ], { title: "dwg grades before the tone map, so a hard push rolls off "
                  + "instead of clipping. direct is one LUT straight to Rec.709. "
                  + "rec709 is for a source that is NOT camera log: it skips both "
                  + "conversions and grades in place, which is what an mp4 needs "
                  + "and is also the FX-only round trip. Picking the wrong one is "
                  + "refused with a message rather than rendered wrong." }),
        SEL(["convert", "tonemap"], "tone map", [
          { value: "aces", label: "aces (filmic roll-off)" },
          { value: "filmic", label: "filmic (gentler shoulder)" },
          { value: "none", label: "none (clips over white)" }
        ], { title: "Ignored in rec709 working space: there is no tone map on "
                  + "a source that is already display referred." }),
        SEL(["convert", "encode"], "output gamma", [
          { value: "rec709a", label: "rec709a (Mac display)" },
          { value: "gamma24", label: "gamma24 (calibrated monitor)" }
        ], { title: "Read on both the dwg and direct paths, which now load a "
                  + "per encode LUT each. Ignored in rec709 working space, "
                  + "where the source is already in its delivery encoding and "
                  + "nothing re-encodes it." }),
        S(["convert", "exposure"], "exposure", -4, 4, 0.01, 0,
          { bipolar: true, precision: 2, unit: "stops" })
      ]
    },

    {
      id: "primaries", node: "2", name: "Primaries",
      note: "Runs inside the working space, before the output transform. "
          + "Temperature and tint are missing on purpose: they are applied in "
          + "the log stage above, where an offset is a linear gain.",
      controls: [
        S(["primaries", "contrast"], "contrast", 0.3, 2.5, 0.005, 1.0, { bipolar: true }),
        { kind: "pivot", path: ["primaries", "pivot"], label: "pivot" },
        S(["primaries", "saturation"], "saturation", 0, 2.5, 0.005, 1.0,
          { bipolar: true,
            title: "The whole range renders. Above about 2.08 one matrix would "
                 + "exceed ffmpeg's coefficient limit and used to fail the "
                 + "render outright, so the move is now split across several "
                 + "passes. The split is exact, not an approximation: the "
                 + "saturation matrix composes, so N passes of the Nth root "
                 + "give the same result as one impossible pass." }),
        S(["primaries", "vibrance"], "vibrance", -1, 1, 0.005, 0,
          { bipolar: true, title: "Another way to add saturation. Measured on this "
                                + "footage it spreads its gain much like a plain "
                                + "saturation multiply (11.5% of the gain landing on "
                                + "flat colour against 11.7% for a matched multiply), "
                                + "so despite the name it does not protect colour "
                                + "that is already saturated. Seven presets use it, "
                                + "so the behaviour is left as it is." }),
        S(["primaries", "temperature"], "temperature", -0.5, 0.5, 0.002, 0,
          { bipolar: true, unit: "stops", title: "positive is warmer" }),
        S(["primaries", "tint"], "tint", -0.5, 0.5, 0.002, 0,
          { bipolar: true, unit: "stops", title: "positive is greener" }),
        { kind: "wheels", label: "wheels", wheels: [
          { path: ["primaries", "lift"], name: "Lift", def: 0, scale: 0.12,
            min: -0.6, max: 0.6, masterMin: -0.3, masterMax: 0.3,
            step: 0.002, precision: 3 },
          { path: ["primaries", "gamma"], name: "Gamma", def: 1, scale: 0.25,
            min: 0.2, max: 4, masterMin: 0.3, masterMax: 3,
            step: 0.005, precision: 3 },
          { path: ["primaries", "gain"], name: "Gain", def: 1, scale: 0.25,
            min: 0, max: 4, masterMin: 0, masterMax: 2.5,
            step: 0.005, precision: 3 },
          { path: ["primaries", "brightness"], name: "Offset", def: 0, scale: 0.08,
            min: -0.5, max: 0.5, masterMin: -0.25, masterMax: 0.25,
            step: 0.002, precision: 3 }
        ]},
        S(["primaries", "black_lift"], "black lift", -0.1, 0.3, 0.002, 0, { bipolar: true }),
        S(["primaries", "highlight_rolloff"], "highlight rolloff", 0, 0.5, 0.002, 0)
      ]
    },

    {
      id: "curves", node: "3", name: "Curves",
      note: "Applied after the output transform, in Rec.709 display code, "
          + "because that is the space the curve you draw is a picture of. "
          + "The olive vertical line is where 18% grey lands (0.392).",
      enable: ["curves", "enabled"],
      controls: [
        CHK(["curves", "enabled"], "enabled"),
        SEL(["curves", "interp"], "interpolation", [
          { value: "pchip", label: "pchip (monotone, no overshoot)" },
          { value: "natural", label: "natural cubic" }
        ]),
        { kind: "curves", paths: [["curves", "master"], ["curves", "r"],
                                  ["curves", "g"], ["curves", "b"]] }
      ]
    },

    {
      id: "secondary", node: "4", name: "Secondary (HSL qualifier)",
      note: "A colour key, not a shape. Hue, saturation and luma windows pick "
          + "pixels; the correction moves only those. Baked into a 33-cube on "
          + "each change, so it costs a lookup at render time.",
      enable: ["secondary", "enabled"],
      controls: [
        CHK(["secondary", "enabled"], "enabled"),
        CHK(["secondary", "invert"], "invert key"),
        CHK(["secondary", "show_mask"], "matte in preset",
          { title: "Stores the matte view in the preset. For a quick look use "
                 + "the Matte button over the viewer instead, which does not "
                 + "touch the config." }),
        S(["secondary", "hue_center"], "hue centre", 0, 360, 0.5, 30,
          { precision: 1, unit: "deg" }),
        S(["secondary", "hue_width"], "hue width", 0, 360, 0.5, 40, { precision: 1 }),
        S(["secondary", "hue_soft"], "hue softness", 0.5, 120, 0.5, 15, { precision: 1 }),
        S(["secondary", "sat_low"], "sat low", 0, 1, 0.005, 0.10),
        S(["secondary", "sat_high"], "sat high", 0, 1, 0.005, 1.0),
        S(["secondary", "sat_soft"], "sat softness", 0.005, 0.6, 0.005, 0.10),
        S(["secondary", "lum_low"], "luma low", 0, 1, 0.005, 0.0),
        S(["secondary", "lum_high"], "luma high", 0, 1, 0.005, 1.0),
        S(["secondary", "lum_soft"], "luma softness", 0.005, 0.6, 0.005, 0.10),
        S(["secondary", "hue_shift"], "hue shift", -180, 180, 0.5, 0,
          { bipolar: true, precision: 1, unit: "deg" }),
        S(["secondary", "sat_gain"], "sat gain", 0, 3, 0.005, 1.0, { bipolar: true }),
        S(["secondary", "lum_gain"], "luma gain", 0, 3, 0.005, 1.0, { bipolar: true }),
        { kind: "trio", path: ["secondary", "tint"], label: "tint push",
          min: -0.4, max: 0.4, step: 0.002, def: [0, 0, 0], precision: 3,
          swatchBias: 0.5 },
        S(["secondary", "strength"], "strength", 0, 1, 0.005, 1.0)
      ]
    },

    {
      id: "look", node: "5", name: "Look",
      note: "A creative .cube in Rec.709, applied after the conversion and "
          + "never to raw log. Full strength is usually too strong; 0.6 to 0.8 "
          + "is the normal working state.",
      controls: [
        { kind: "lut", path: ["look", "lut"], label: "look LUT" },
        S(["look", "mix"], "mix", 0, 1, 0.005, 1.0)
      ]
    },

    {
      id: "fx", node: "6", name: "FX",
      note: "Optical effects, in the order halation, bloom, radial blur, RGB "
          + "split, vignette. Each trades sharpness for character, so they "
          + "only pay off on a source that had sharpness to spend.",
      controls: [
        { kind: "sub", label: "Halation" },
        CHK(["fx", "halation", "enabled"], "enabled"),
        S(["fx", "halation", "threshold"], "threshold", 0, 0.99, 0.005, 0.62),
        S(["fx", "halation", "sigma"], "sigma", 0.5, 160, 0.5, 26, { precision: 1, unit: "px" }),
        S(["fx", "halation", "strength"], "strength", 0, 6, 0.005, 0.55,
          { title: "Was capped at 1.0 because it was only a blend opacity, "
                 + "which ffmpeg rejects above 1. Past 1.0 the extra now drives "
                 + "the glow layer's amplitude instead, so it keeps going. At or "
                 + "below 1.0 it is byte identical to before. Measured on blown "
                 + "highlights: strength 1 moves 10.9% of pixels, 4 moves 36.5%, "
                 + "and clipping to white starts around 4. Needs real highlights "
                 + "in the shot: on flat footage even 8 barely registers." }),
        { kind: "trio", path: ["fx", "halation", "tint"], label: "tint",
          min: 0, max: 2, step: 0.005, def: [1.0, 0.34, 0.16], precision: 3 },

        { kind: "sub", label: "Bloom" },
        CHK(["fx", "bloom", "enabled"], "enabled"),
        S(["fx", "bloom", "threshold"], "threshold", 0, 0.99, 0.005, 0.72),
        S(["fx", "bloom", "sigma"], "sigma", 0.5, 300, 1, 70, { precision: 1, unit: "px" }),
        S(["fx", "bloom", "strength"], "strength", 0, 4, 0.005, 0.28,
          { title: "Same fix as halation strength: past 1.0 this drives the glow "
                 + "layer's amplitude rather than a blend opacity ffmpeg caps at "
                 + "1. Stops at 4 rather than 6 because bloom blows out about "
                 + "twice as fast: it already flattens 3.2% of the frame to white "
                 + "at strength 2 and 12.3% at 4. Needs real highlights to show." }),
        { kind: "trio", path: ["fx", "bloom", "tint"], label: "tint",
          min: 0, max: 2, step: 0.005, def: [1.0, 0.97, 0.92], precision: 3 },

        { kind: "sub", label: "RGB split" },
        CHK(["fx", "rgb_split", "enabled"], "enabled"),
        S(["fx", "rgb_split", "amount"], "amount", 0, 12, 0.1, 1.6,
          { precision: 1, unit: "px",
            title: "Rounded to whole pixels by the engine, so anything under "
                 + "0.5 has no effect at all and the values in between land in "
                 + "steps. In the preview the shift is forced to at least one "
                 + "pixel: the honestly scaled amount would round to zero at a "
                 + "960 preview of a 4K source and the effect would vanish from "
                 + "the preview while still being in the render, so a small "
                 + "amount reads STRONGER here than it will finish." }),

        { kind: "sub", label: "Radial blur" },
        CHK(["fx", "radial_blur", "enabled"], "enabled"),
        S(["fx", "radial_blur", "sigma"], "sigma", 0.3, 40, 0.1, 9, { precision: 2, unit: "px" }),
        S(["fx", "radial_blur", "start"], "start", 0, 1.5, 0.005, 0.55),
        S(["fx", "radial_blur", "end"], "end", 0, 1.6, 0.005, 1.0),

        { kind: "sub", label: "Vignette" },
        CHK(["fx", "vignette", "enabled"], "enabled"),
        S(["fx", "vignette", "amount"], "amount", 0, 1, 0.005, 0.45),
        S(["fx", "vignette", "radius"], "radius", 0.05, 2, 0.005, 0.85,
          { title: "How far in from the corners the darkening reaches. Bigger "
                 + "is a tighter, more distant vignette. This was genuinely "
                 + "inert until now. ffmpeg's vignette factor is cos(angle * d) "
                 + "to the fourth, so scaling d is the same as scaling the "
                 + "angle and radius rides in on the angle with no extra filter. "
                 + "0.85 reproduces the old fixed falloff exactly, which is why "
                 + "no existing preset moved." })
      ]
    },

    {
      id: "grain", node: "7", name: "Grain",
      enable: ["grain", "enabled"],
      note: "size is the plate downscale factor, not a pixel radius. Per-pixel "
          + "noise at 4K averages away to nothing at 1080p, which is why grain "
          + "with size 1 reads as no grain at all.",
      controls: [
        CHK(["grain", "enabled"], "enabled"),
        S(["grain", "strength"], "strength", 0, 100, 0.5, 40, { precision: 1 }),
        S(["grain", "size"], "size", 1, 10, 1, 3, { precision: 0, integer: true }),
        S(["grain", "opacity"], "opacity", 0, 1, 0.005, 0.5)
      ]
    },

    {
      id: "detail", node: "8", name: "Detail",
      note: "Soften first and add sharpness back: it reads cleaner than the "
          + "in-camera sharpening it replaces.",
      controls: [
        S(["detail", "soften"], "soften", 0, 12, 0.05, 0, { precision: 2, unit: "sigma" }),
        S(["detail", "sharpen"], "sharpen", 0, 3, 0.01, 0, { precision: 2 })
      ]
    },

    {
      id: "letterbox", node: "9", name: "Letterbox",
      enable: ["letterbox", "enabled"],
      note: "Last geometric step, so the effects above still see the whole frame.",
      controls: [
        CHK(["letterbox", "enabled"], "enabled"),
        S(["letterbox", "aspect"], "aspect", 1.0, 3.0, 0.005, 2.39, { precision: 3 })
      ]
    },

    {
      id: "output", node: "10", name: "Output",
      note: "Only used by a full render. Previews are always JPEG at 4:4:4.",
      controls: [
        SEL(["output", "codec"], "codec", [
          { value: "prores_ks", label: "ProRes (prores_ks)" },
          { value: "libx264", label: "H.264 (libx264)" },
          { value: "libx265", label: "H.265 (libx265)" }
        ]),
        SEL(["output", "profile"], "prores profile", [
          { value: 0, label: "0 proxy" }, { value: 1, label: "1 LT" },
          { value: 2, label: "2 422" }, { value: 3, label: "3 422 HQ" },
          { value: 4, label: "4 4444" }, { value: 5, label: "5 4444 XQ" }
        ], { numeric: true }),
        S(["output", "crf"], "crf", 0, 51, 1, 16, { precision: 0, integer: true }),
        SEL(["output", "preset"], "x26x preset", [
          "ultrafast", "superfast", "veryfast", "faster", "fast",
          "medium", "slow", "slower", "veryslow"
        ])
      ]
    }
  ];
})(window);
