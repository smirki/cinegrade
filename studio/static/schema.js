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
      id: "prep", node: "0", name: "Prep",
      note: "Runs first, right after decode and before the colour space "
          + "conversion below, on the same normalised RGB buffer the rest "
          + "of the graph is built from rather than the camera's native "
          + "YUV planes (documented as a deviation in the Limits panel). "
          + "Not part of the GPU preview: turning denoise on always falls "
          + "back to a server render, which costs real time (see Limits).",
      enable: ["prep", "denoise", "enabled"],
      controls: [
        CHK(["prep", "denoise", "enabled"], "enabled"),
        S(["prep", "denoise", "spatial"], "spatial", 0, 1, 0.005, 0.0,
          { title: "hqdn3d spatial strength, mapped 0..1 onto hqdn3d's own "
                 + "0..8 luma and chroma spatial argument (both set equal: "
                 + "there is no separate luma/chroma split on this RGB "
                 + "buffer). 0.5 lands near ffmpeg's own no-argument "
                 + "default of 4.0. Measured on real footage at 1920 wide: "
                 + "0.5 lowers a flat 64x64 patch's std dev from 106.7 to "
                 + "90.3 of 65535 and moves the whole frame by a mean of "
                 + "0.96 of 255." }),
        S(["prep", "denoise", "temporal"], "temporal", 0, 1, 0.005, 0.0,
          { title: "hqdn3d temporal strength, same 0..8 mapping as spatial. "
                 + "Needs real motion to do anything: on a single still it "
                 + "is very nearly a no-op (measured max difference 4.15 of "
                 + "255, mean 0.95, which is hqdn3d's own rounding on its "
                 + "first frame, not a bug in this engine), but on an "
                 + "actual render it moves a mid frame by a mean of 2.52 "
                 + "of 255 and a max of 24.45, several times the still's "
                 + "residual." })
      ]
    },

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
      id: "hue_curves", node: "3b", name: "Hue curves",
      note: "Hue vs Hue, Hue vs Sat, Hue vs Lum, Lum vs Sat and Sat vs Sat, "
          + "baked with Color Slice below into one 33 point 3D LUT. The "
          + "neutral is the flat olive line, not the diagonal: y is an offset "
          + "(Hue vs Hue, in turns of the wheel) or a multiplier (the other "
          + "four, around 1.0), and no points at all is the identity. The "
          + "three hue axes wrap, so a point near the left edge and one near "
          + "the right edge are neighbours.",
      enable: ["hue_curves", "enabled"],
      controls: [
        CHK(["hue_curves", "enabled"], "enabled"),
        { kind: "huecurves",
          paths: [["hue_curves", "hue_hue"], ["hue_curves", "hue_sat"],
                  ["hue_curves", "hue_lum"], ["hue_curves", "lum_sat"],
                  ["hue_curves", "sat_sat"]] }
      ]
    },

    {
      id: "slice", node: "3c", name: "Color Slice",
      note: "Six hue vectors plus skin, each with a hue rotation, a saturation "
          + "multiplier and a density, plus a global density and the six Tetra "
          + "cube corners. A pixel's weight for a vector is a raised cosine of "
          + "its hue distance from that vector's centre, zero at 60 degrees, "
          + "scaled by its own saturation, so neutrals are never touched. The "
          + "six chromatic weights sum to exactly 1 at every hue.",
      enable: ["slice", "enabled"],
      controls: [
        CHK(["slice", "enabled"], "enabled"),
        S(["slice", "density"], "global density", -1, 1, 0.005, 0.0,
          { bipolar: true,
            title: "L' = L * (1 - density * S) on every pixel: colour gets "
                 + "denser and darker in proportion to how saturated it "
                 + "already is, and grey is left exactly where it was. "
                 + "Negative brightens." }),
        { kind: "sub", label: "Red (0 deg)" },
        S(["slice", "vectors", "red", "hue"], "hue", -60, 60, 0.5, 0.0,
          { bipolar: true, precision: 1, unit: "deg",
            title: "Degrees of hue rotation at the centre of this vector, "
                 + "falling to zero 60 degrees away and scaled by the pixel's "
                 + "own saturation, so a grey pixel never moves. "}),
        S(["slice", "vectors", "red", "sat"], "sat", 0, 2, 0.005, 1.0,
          { bipolar: true, unit: "x",
            title: "Saturation multiplier at the centre of this vector. The "
                 + "gain is weight scaled, so on a pixel of saturation 0.6 a "
                 + "setting of 1.8 lands at 1.48, not 1.8." }),
        S(["slice", "vectors", "red", "density"], "density", -1, 1, 0.005, 0.0,
          { bipolar: true,
            title: "Darkens this vector's colours: L' = L * (1 - density * S "
                 + "* weight), where weight already carries the pixel's "
                 + "saturation. Negative brightens. Grey is never touched. "
                 + "Per vector density therefore carries saturation twice and "
                 + "the global density above carries it once, so at the same "
                 + "number the global control bites 4.68x harder: on clip A at "
                 + "640 wide, all six vector densities at 1 darken mean luma by "
                 + "0.01369 while the global at 1 darkens it by 0.06414. See "
                 + "the Limits panel." }),
        { kind: "sub", label: "Yellow (60 deg)" },
        S(["slice", "vectors", "yellow", "hue"], "hue", -60, 60, 0.5, 0.0,
          { bipolar: true, precision: 1, unit: "deg",
            title: "Degrees of hue rotation at the centre of this vector, "
                 + "falling to zero 60 degrees away and scaled by the pixel's "
                 + "own saturation, so a grey pixel never moves. "}),
        S(["slice", "vectors", "yellow", "sat"], "sat", 0, 2, 0.005, 1.0,
          { bipolar: true, unit: "x",
            title: "Saturation multiplier at the centre of this vector. The "
                 + "gain is weight scaled, so on a pixel of saturation 0.6 a "
                 + "setting of 1.8 lands at 1.48, not 1.8." }),
        S(["slice", "vectors", "yellow", "density"], "density", -1, 1, 0.005, 0.0,
          { bipolar: true,
            title: "Darkens this vector's colours: L' = L * (1 - density * S "
                 + "* weight), where weight already carries the pixel's "
                 + "saturation. Negative brightens. Grey is never touched. "
                 + "Per vector density therefore carries saturation twice and "
                 + "the global density above carries it once, so at the same "
                 + "number the global control bites 4.68x harder: on clip A at "
                 + "640 wide, all six vector densities at 1 darken mean luma by "
                 + "0.01369 while the global at 1 darkens it by 0.06414. See "
                 + "the Limits panel." }),
        { kind: "sub", label: "Green (120 deg)" },
        S(["slice", "vectors", "green", "hue"], "hue", -60, 60, 0.5, 0.0,
          { bipolar: true, precision: 1, unit: "deg",
            title: "Degrees of hue rotation at the centre of this vector, "
                 + "falling to zero 60 degrees away and scaled by the pixel's "
                 + "own saturation, so a grey pixel never moves. "}),
        S(["slice", "vectors", "green", "sat"], "sat", 0, 2, 0.005, 1.0,
          { bipolar: true, unit: "x",
            title: "Saturation multiplier at the centre of this vector. The "
                 + "gain is weight scaled, so on a pixel of saturation 0.6 a "
                 + "setting of 1.8 lands at 1.48, not 1.8." }),
        S(["slice", "vectors", "green", "density"], "density", -1, 1, 0.005, 0.0,
          { bipolar: true,
            title: "Darkens this vector's colours: L' = L * (1 - density * S "
                 + "* weight), where weight already carries the pixel's "
                 + "saturation. Negative brightens. Grey is never touched. "
                 + "Per vector density therefore carries saturation twice and "
                 + "the global density above carries it once, so at the same "
                 + "number the global control bites 4.68x harder: on clip A at "
                 + "640 wide, all six vector densities at 1 darken mean luma by "
                 + "0.01369 while the global at 1 darkens it by 0.06414. See "
                 + "the Limits panel." }),
        { kind: "sub", label: "Cyan (180 deg)" },
        S(["slice", "vectors", "cyan", "hue"], "hue", -60, 60, 0.5, 0.0,
          { bipolar: true, precision: 1, unit: "deg",
            title: "Degrees of hue rotation at the centre of this vector, "
                 + "falling to zero 60 degrees away and scaled by the pixel's "
                 + "own saturation, so a grey pixel never moves. "}),
        S(["slice", "vectors", "cyan", "sat"], "sat", 0, 2, 0.005, 1.0,
          { bipolar: true, unit: "x",
            title: "Saturation multiplier at the centre of this vector. The "
                 + "gain is weight scaled, so on a pixel of saturation 0.6 a "
                 + "setting of 1.8 lands at 1.48, not 1.8." }),
        S(["slice", "vectors", "cyan", "density"], "density", -1, 1, 0.005, 0.0,
          { bipolar: true,
            title: "Darkens this vector's colours: L' = L * (1 - density * S "
                 + "* weight), where weight already carries the pixel's "
                 + "saturation. Negative brightens. Grey is never touched. "
                 + "Per vector density therefore carries saturation twice and "
                 + "the global density above carries it once, so at the same "
                 + "number the global control bites 4.68x harder: on clip A at "
                 + "640 wide, all six vector densities at 1 darken mean luma by "
                 + "0.01369 while the global at 1 darkens it by 0.06414. See "
                 + "the Limits panel." }),
        { kind: "sub", label: "Blue (240 deg)" },
        S(["slice", "vectors", "blue", "hue"], "hue", -60, 60, 0.5, 0.0,
          { bipolar: true, precision: 1, unit: "deg",
            title: "Degrees of hue rotation at the centre of this vector, "
                 + "falling to zero 60 degrees away and scaled by the pixel's "
                 + "own saturation, so a grey pixel never moves. "}),
        S(["slice", "vectors", "blue", "sat"], "sat", 0, 2, 0.005, 1.0,
          { bipolar: true, unit: "x",
            title: "Saturation multiplier at the centre of this vector. The "
                 + "gain is weight scaled, so on a pixel of saturation 0.6 a "
                 + "setting of 1.8 lands at 1.48, not 1.8." }),
        S(["slice", "vectors", "blue", "density"], "density", -1, 1, 0.005, 0.0,
          { bipolar: true,
            title: "Darkens this vector's colours: L' = L * (1 - density * S "
                 + "* weight), where weight already carries the pixel's "
                 + "saturation. Negative brightens. Grey is never touched. "
                 + "Per vector density therefore carries saturation twice and "
                 + "the global density above carries it once, so at the same "
                 + "number the global control bites 4.68x harder: on clip A at "
                 + "640 wide, all six vector densities at 1 darken mean luma by "
                 + "0.01369 while the global at 1 darkens it by 0.06414. See "
                 + "the Limits panel." }),
        { kind: "sub", label: "Magenta (300 deg)" },
        S(["slice", "vectors", "magenta", "hue"], "hue", -60, 60, 0.5, 0.0,
          { bipolar: true, precision: 1, unit: "deg",
            title: "Degrees of hue rotation at the centre of this vector, "
                 + "falling to zero 60 degrees away and scaled by the pixel's "
                 + "own saturation, so a grey pixel never moves. "}),
        S(["slice", "vectors", "magenta", "sat"], "sat", 0, 2, 0.005, 1.0,
          { bipolar: true, unit: "x",
            title: "Saturation multiplier at the centre of this vector. The "
                 + "gain is weight scaled, so on a pixel of saturation 0.6 a "
                 + "setting of 1.8 lands at 1.48, not 1.8." }),
        S(["slice", "vectors", "magenta", "density"], "density", -1, 1, 0.005, 0.0,
          { bipolar: true,
            title: "Darkens this vector's colours: L' = L * (1 - density * S "
                 + "* weight), where weight already carries the pixel's "
                 + "saturation. Negative brightens. Grey is never touched. "
                 + "Per vector density therefore carries saturation twice and "
                 + "the global density above carries it once, so at the same "
                 + "number the global control bites 4.68x harder: on clip A at "
                 + "640 wide, all six vector densities at 1 darken mean luma by "
                 + "0.01369 while the global at 1 darkens it by 0.06414. See "
                 + "the Limits panel." }),
        { kind: "sub", label: "Skin (20.87 deg)" },
        S(["slice", "vectors", "skin", "hue"], "hue", -60, 60, 0.5, 0.0,
          { bipolar: true, precision: 1, unit: "deg",
            title: "Degrees of hue rotation at the centre of this vector, "
                 + "falling to zero 60 degrees away and scaled by the pixel's "
                 + "own saturation, so a grey pixel never moves. " + "The centre is measured, not chosen: it is the hue of the "
                     + "engine's own skin probe (0.55, 0.40, 0.32) in "
                     + "grade/tools/match_ref.py. It overlaps red and yellow "
                     + "on purpose. "}),
        S(["slice", "vectors", "skin", "sat"], "sat", 0, 2, 0.005, 1.0,
          { bipolar: true, unit: "x",
            title: "Saturation multiplier at the centre of this vector. The "
                 + "gain is weight scaled, so on a pixel of saturation 0.6 a "
                 + "setting of 1.8 lands at 1.48, not 1.8." }),
        S(["slice", "vectors", "skin", "density"], "density", -1, 1, 0.005, 0.0,
          { bipolar: true,
            title: "Darkens this vector's colours: L' = L * (1 - density * S "
                 + "* weight), where weight already carries the pixel's "
                 + "saturation. Negative brightens. Grey is never touched. "
                 + "Per vector density therefore carries saturation twice and "
                 + "the global density above carries it once, so at the same "
                 + "number the global control bites 4.68x harder: on clip A at "
                 + "640 wide, all six vector densities at 1 darken mean luma by "
                 + "0.01369 while the global at 1 darkens it by 0.06414. See "
                 + "the Limits panel." }),
        { kind: "fold", label: "Tetra (cube corners)",
          title: "Six corner moves with black and white pinned, interpolated "
               + "tetrahedrally over the cube. Reach for this instead of six "
               + "vector pushes when a neutral must not move.",
          controls: [
            CHK(["slice", "tetra", "enabled"], "enabled"),
        { kind: "trio", path: ["slice", "tetra", "r"], label: "red",
          def: [0, 0, 0], min: -1, max: 1, step: 0.005, precision: 3,
          swatchBias: 0.5,
          title: "How far the red corner of the RGB cube moves, in R, G and B. "
               + "Black and white stay pinned, so the whole neutral axis is "
               + "fixed no matter what these say." },
        { kind: "trio", path: ["slice", "tetra", "g"], label: "green",
          def: [0, 0, 0], min: -1, max: 1, step: 0.005, precision: 3,
          swatchBias: 0.5,
          title: "How far the green corner of the RGB cube moves, in R, G and B. "
               + "Black and white stay pinned, so the whole neutral axis is "
               + "fixed no matter what these say." },
        { kind: "trio", path: ["slice", "tetra", "b"], label: "blue",
          def: [0, 0, 0], min: -1, max: 1, step: 0.005, precision: 3,
          swatchBias: 0.5,
          title: "How far the blue corner of the RGB cube moves, in R, G and B. "
               + "Black and white stay pinned, so the whole neutral axis is "
               + "fixed no matter what these say." },
        { kind: "trio", path: ["slice", "tetra", "c"], label: "cyan",
          def: [0, 0, 0], min: -1, max: 1, step: 0.005, precision: 3,
          swatchBias: 0.5,
          title: "How far the cyan corner of the RGB cube moves, in R, G and B. "
               + "Black and white stay pinned, so the whole neutral axis is "
               + "fixed no matter what these say." },
        { kind: "trio", path: ["slice", "tetra", "m"], label: "magenta",
          def: [0, 0, 0], min: -1, max: 1, step: 0.005, precision: 3,
          swatchBias: 0.5,
          title: "How far the magenta corner of the RGB cube moves, in R, G and B. "
               + "Black and white stay pinned, so the whole neutral axis is "
               + "fixed no matter what these say." },
        { kind: "trio", path: ["slice", "tetra", "y"], label: "yellow",
          def: [0, 0, 0], min: -1, max: 1, step: 0.005, precision: 3,
          swatchBias: 0.5,
          title: "How far the yellow corner of the RGB cube moves, in R, G and B. "
               + "Black and white stay pinned, so the whole neutral axis is "
               + "fixed no matter what these say." },
          ] }
      ]
    },

    {
      // Replaces the old "secondary" (node 4) and "window" (node 4b)
      // stages: any number of masked correction layers instead of one fixed
      // qualifier-plus-shape pair. kind "array" tells panels.js to hand this
      // stage's body to layers.js (studio/static/layers.js) rather than
      // walking `controls` itself; `controls` below carries only a dummy
      // entry so app.js's coverage audit (which walks S.defaults and never
      // recurses into an array) has something to mark "layers" covered by.
      // itemControls is layers.js's real template: one entry per field in a
      // layer's `mask` and `correct` groups, with paths RELATIVE to that one
      // layer (layers.js prefixes ["layers", i] itself). A layer's header
      // (name, enabled toggle, add/remove/duplicate/move, the selected
      // marker) is drawn directly by layers.js, not declared here, the same
      // way this stage's own icon/name/chevron header is drawn by panels.js
      // and not declared in any stage's `controls` either.
      id: "layers", node: "4", name: "Layers", kind: "array",
      note: "Any number of masked correction layers, replacing the single "
          + "secondary qualifier and window pair. Each layer keys a matte "
          + "(a window shape, a colour qualifier, or both multiplied "
          + "together) and applies its own correction only where that matte "
          + "is open, before or after the look LUT. Layers apply serially in "
          + "the order shown, top to bottom.",
      controls: [
        { path: ["layers"] }
      ],
      itemControls: [
        SEL(["placement"], "placement", [
          { value: "before_look", label: "before the look" },
          { value: "after_look", label: "after the look" }
        ], { title: "Where in the pipeline this layer's correction lands: "
                  + "before the creative look LUT (grading the source) or "
                  + "after it (grading the finished look)." }),

        { kind: "sub", label: "Mask" },
        CHK(["mask", "show"], "matte in preset",
          { title: "Stores the matte view in the preset. For a quick look use "
                 + "the Matte button over the viewer instead, which does not "
                 + "touch the config." }),
        CHK(["mask", "invert"], "invert mask",
          { title: "Inverts the combined matte (window times key) after both "
                 + "are computed, so the correction lands everywhere the "
                 + "shape and the qualifier together did NOT pick." }),

        // Window, Key and Correct are "fold" groups (kind "fold", the same
        // collapsible <details>/<summary> kind lane L2 added to the Slice
        // stage for Tetra): collapsed by default, one per layer per group,
        // remembered for the session by layers.js (see its foldState). Each
        // fold's own summary text is computed live by layers.js from the
        // CURRENT layer (id carries which group so layers.js can key its
        // remembered open/closed state and its own auto-enable lookups by
        // it without depending on the label string).
        { kind: "fold", id: "window", label: "Window",
          title: "Collapsed by default. The summary line shows on or off; "
               + "open it to reach the shape controls.",
          controls: [
            CHK(["mask", "window", "enabled"], "enabled",
              { title: "Does nothing on its own: it multiplies whatever the key "
                     + "below picks. With neither Window nor Key on, the matte is "
                     + "open everywhere and the correction is a global grade." }),
            SEL(["mask", "window", "shape"], "shape", [
              { value: "ellipse", label: "ellipse" },
              { value: "rect", label: "rectangle" }
            ]),
            S(["mask", "window", "cx"], "centre x", 0, 1, 0.002, 0.5),
            S(["mask", "window", "cy"], "centre y", 0, 1, 0.002, 0.5),
            S(["mask", "window", "w"], "width", 0, 2, 0.002, 0.6,
              { title: "Full extent, not the half axis, as a fraction of the frame "
                     + "width. Past 1.0 the shape is wider than the frame, which is "
                     + "how you feather in from one edge only." }),
            S(["mask", "window", "h"], "height", 0, 2, 0.002, 0.6),
            S(["mask", "window", "rotation"], "rotation", -180, 180, 0.5, 0,
              { bipolar: true, precision: 1, unit: "deg",
                title: "Degrees clockwise on screen. A rotation does not resize "
                     + "the shape, so the covered area stays put." }),
            S(["mask", "window", "softness"], "softness", 0, 1, 0.005, 0.15,
              { title: "Feather width as a fraction of the shape's own radius, so "
                     + "it scales with the shape. Exactly 0 is a hard edge with no "
                     + "partial pixels at all." }),
            CHK(["mask", "window", "invert"], "invert window",
              { title: "Grades everything outside the shape instead of inside." })
          ] },

        { kind: "fold", id: "key", label: "Key",
          title: "Collapsed by default. The summary line shows on or off; "
               + "open it to reach the hue, saturation and luma controls.",
          controls: [
            CHK(["mask", "key", "enabled"], "enabled",
              { title: "A colour key, not a shape: hue, saturation and luma "
                     + "windows pick pixels, multiplied against the window "
                     + "above if that is also on." }),
            CHK(["mask", "key", "invert"], "invert key"),
            S(["mask", "key", "hue_center"], "hue centre", 0, 360, 0.5, 30,
              { precision: 1, unit: "deg" }),
            S(["mask", "key", "hue_width"], "hue width", 0, 360, 0.5, 40, { precision: 1 }),
            S(["mask", "key", "hue_soft"], "hue softness", 0.5, 120, 0.5, 15, { precision: 1 }),
            S(["mask", "key", "sat_low"], "sat low", 0, 1, 0.005, 0.10),
            S(["mask", "key", "sat_high"], "sat high", 0, 1, 0.005, 1.0),
            S(["mask", "key", "sat_soft"], "sat softness", 0.005, 0.6, 0.005, 0.10),
            S(["mask", "key", "lum_low"], "luma low", 0, 1, 0.005, 0.0),
            S(["mask", "key", "lum_high"], "luma high", 0, 1, 0.005, 1.0),
            S(["mask", "key", "lum_soft"], "luma softness", 0.005, 0.6, 0.005, 0.10)
          ] },

        { kind: "fold", id: "correct", label: "Correct",
          title: "Collapsed by default. The summary line lists which fields "
               + "are away from their default; \"default\" means none are.",
          controls: [
            S(["correct", "exposure"], "exposure", -4, 4, 0.01, 0,
              { bipolar: true, precision: 2, unit: "stops" }),
            S(["correct", "contrast"], "contrast", 0.3, 2.5, 0.005, 1.0, { bipolar: true }),
            { kind: "pivot", path: ["correct", "pivot"], label: "pivot" },
            S(["correct", "saturation"], "saturation", 0, 2.5, 0.005, 1.0, { bipolar: true }),
            S(["correct", "temperature"], "temperature", -0.5, 0.5, 0.002, 0,
              { bipolar: true, unit: "stops", title: "positive is warmer" }),
            S(["correct", "tint"], "tint", -0.5, 0.5, 0.002, 0,
              { bipolar: true, unit: "stops", title: "positive is greener" }),
            S(["correct", "hue_shift"], "hue shift", -180, 180, 0.5, 0,
              { bipolar: true, precision: 1, unit: "deg" }),
            S(["correct", "sat_gain"], "sat gain", 0, 3, 0.005, 1.0, { bipolar: true }),
            S(["correct", "lum_gain"], "luma gain", 0, 3, 0.005, 1.0, { bipolar: true }),
            { kind: "trio", path: ["correct", "offset"], label: "offset push",
              min: -0.4, max: 0.4, step: 0.002, def: [0, 0, 0], precision: 3,
              swatchBias: 0.5 },
            S(["correct", "blur"], "blur", 0, 40, 0.1, 0,
              { precision: 2, unit: "px @1920w",
                title: "Gaussian sigma in pixels at a 1920-wide frame, scaled "
                     + "with the real width. A separate spatial pass on the "
                     + "corrected branch, before the matte merge. Upper bound is "
                     + "a UI convenience, not an engine limit." }),
            S(["correct", "strength"], "strength", 0, 1, 0.005, 1.0)
          ] }
      ]
    },

    {
      id: "look", node: "5", name: "Look",
      note: "A creative .cube in Rec.709, applied after the conversion and "
          + "never to raw log. Full strength is usually too strong; 0.6 to 0.8 "
          + "is the normal working state. A second slot blends in PARALLEL "
          + "with the first, not stacked on top of it: both slots grade the "
          + "same picture, then balance mixes between the two results. Slot 2 "
          + "off (no LUT, or balance at 0) leaves slot 1 exactly as before.",
      controls: [
        { kind: "lut", path: ["look", "lut"], label: "look LUT" },
        S(["look", "mix"], "mix", 0, 1, 0.005, 1.0),
        { kind: "sub", label: "Slot 2" },
        { kind: "lut", path: ["look", "lut2"], label: "look LUT 2" },
        S(["look", "mix2"], "mix2", 0, 1, 0.005, 1.0),
        S(["look", "balance"], "balance", 0, 1, 0.005, 0.0,
          { title: "0 shows slot 1 alone (today's look), 1 shows slot 2 alone. "
                 + "In between is a straight crossfade between the two graded "
                 + "results, not a second LUT applied on top of the first." })
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
        SEL(["grain", "stock"], "stock", [
          { value: "custom", label: "custom (use the sliders below)" },
          { value: "16mm", label: "16mm (coarsest, heaviest)" },
          { value: "35mm", label: "35mm (this panel's old default look)" },
          { value: "65mm", label: "65mm (finest, lightest)" }
        ], { title: "Each stock is three numbers (size, strength and "
                  + "softness measured on a real frame from footage/ at "
                  + "1920 wide), not a scan of real film. 16mm is the "
                  + "coarsest and heaviest grain, 35mm is the exact size, "
                  + "strength and softness this panel shipped with before "
                  + "this control existed, and 65mm is the finest and "
                  + "lightest. Picking anything but custom overrides the "
                  + "size, strength and softness sliders below with that "
                  + "stock's own numbers; opacity, response, color and "
                  + "seed are unaffected." }),
        S(["grain", "strength"], "strength", 0, 100, 0.5, 40, { precision: 1 }),
        S(["grain", "size"], "size", 1, 10, 1, 3, { precision: 0, integer: true }),
        S(["grain", "softness"], "softness", 0, 2, 0.01, 0.0,
          { precision: 2, title: "Blurs the grain plate before it is "
                  + "blended onto the picture, so the plate's own high "
                  + "frequency content falls even though grain is still "
                  + "visibly on the picture. Ignored when a stock is set: "
                  + "the stock's own softness number is used instead." }),
        SEL(["grain", "response"], "response", [
          { value: "flat", label: "flat (same amount everywhere)" },
          { value: "film", label: "film (weaker in bright areas)" }
        ], { title: "flat applies the same grain amplitude everywhere. "
                  + "film scales it down toward the highlights: full "
                  + "strength at and below mid grey, easing on a "
                  + "smoothstep to a quarter strength at white, measured "
                  + "on the picture's own luminance after every earlier "
                  + "stage (look, fx, everything) has already run." }),
        S(["grain", "color"], "color", 0, 1, 0.005, 0.0,
          { precision: 3, title: "0 is one grey plate applied identically "
                  + "to red, green and blue. 1 is three independent "
                  + "plates, one per channel, so the grain itself carries "
                  + "a little color instead of only affecting brightness. "
                  + "Values between mix the two." }),
        S(["grain", "seed"], "seed", 0, 999999, 1, 0,
          { precision: 0, integer: true,
            title: "0 reproduces the exact plate this panel has always "
                  + "rendered, which is what keeps every existing preset "
                  + "byte identical. Any other number reseeds the noise "
                  + "generator to a different plate with the same "
                  + "statistics: it still looks like grain, just not the "
                  + "same grain." }),
        S(["grain", "opacity"], "opacity", 0, 1, 0.005, 0.5)
      ]
    },

    {
      id: "detail", node: "8", name: "Detail",
      note: "Soften first and add sharpness back: it reads cleaner than the "
          + "in-camera sharpening it replaces.",
      controls: [
        S(["detail", "soften"], "soften", 0, 12, 0.05, 0, { precision: 2, unit: "sigma" }),
        S(["detail", "sharpen"], "sharpen", 0, 3, 0.01, 0, { precision: 2 }),
        S(["detail", "mid_detail"], "mid detail", -1, 1, 0.005, 0.0,
          { bipolar: true, precision: 2,
            title: "Local contrast: the same split, blur and blend idea as "
                 + "sharpen, but on a wide gaussian (sigma is 2% of the "
                 + "frame width, not a fixed radius) so it moves midtone "
                 + "texture rather than edges. One radius only, not "
                 + "Resolve's multi-scale midtone detail tool. Measured on "
                 + "real footage at 1920 wide: +1.0 moves the whole frame "
                 + "by a mean of 6.7 of 255, -1.0 by 6.8, so about the same "
                 + "amount either way; a hard edge already in the shot can "
                 + "still swing far more than that at that one edge." })
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
