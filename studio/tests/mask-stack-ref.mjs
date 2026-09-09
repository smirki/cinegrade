/* The mask model v2 stack maths, tested on plain arrays.
 *
 * Why this exists: the component stack (C1) has to be right in three places
 * that cannot all be tested at once. The engine's ffmpeg graph is M2's, the
 * matte frames come from a service that does not exist yet, and the WebGL2
 * port needs a GPU, a server and real footage. What CAN be tested today is
 * the arithmetic itself, and gpu.js carries it as ordinary functions on plain
 * arrays (StudioGPU.mask.*) precisely so that it can be: those functions are
 * the DEFINITION and the shaders are a port of them, the same arrangement
 * cinegrade already uses for the power window (numpy reference, geq, shader).
 *
 * So this file is not a mock of anything. It loads the real gpu.js, in node,
 * with no browser and no server, and asserts on the real functions the
 * shaders mirror. What it cannot prove is that the SHADERS mirror them: that
 * is the mask fixture block of studio/tests/parity-gate.mjs, which renders 52
 * component stacks through both sides and measures them (it runs by default
 * and a failed row fails the gate). The two together are the contract: this
 * file says what the arithmetic IS, parity says the shaders do it.
 *
 * Run: node studio/tests/mask-stack-ref.mjs, or through the spec suite, which
 * runs it as spec 31.
 */
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const GPU = path.resolve(HERE, "..", "static", "gpu.js");

/* gpu.js is a plain script that hands itself to `window` or, without one, to
 * whatever `this` is at the top level. A vm context gives it exactly that and
 * nothing else, so a stray DOM or fetch reference at load time would fail
 * here rather than in a browser. */
const ctx = { console };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(GPU, "utf8"), ctx, { filename: "gpu.js" });
const SG = ctx.StudioGPU;
const M = SG && SG.mask;
if (!M) { console.error("gpu.js did not export StudioGPU.mask"); process.exit(2); }

let pass = 0, fail = 0;
const failures = [];

function check(name, cond, detail) {
  if (cond) { pass++; return; }
  fail++;
  failures.push(name + (detail ? "  " + detail : ""));
}
function eq(name, got, want) {
  check(name, got === want, "got " + JSON.stringify(got) + " want " + JSON.stringify(want));
}
function near(name, got, want, tol) {
  check(name, Math.abs(got - want) <= (tol === undefined ? 1e-9 : tol),
        "got " + got + " want " + want);
}

/* A stack made of constant valued matte components, which is the cleanest way
 * to test the ops: every pixel carries the same number, so one pixel is the
 * whole answer. */
function constStack(components, finesse, invert, values) {
  const mask = { components: components, invert: !!invert };
  if (finesse) mask.finesse = finesse;
  return M.stack(mask, 4, 4, {
    matte: (c) => values[c.id],
    key: (c) => values[c.id]
  });
}

// ---------------------------------------------------------------- basics

eq("q8 zero", M.q8(0), 0);
eq("q8 one", M.q8(1), 255);
eq("q8 half", M.q8(0.5), 128);
eq("q8 clamps low", M.q8(-3), 0);
eq("q8 clamps high", M.q8(4), 255);

/* The 16 bit vocabulary. The stack runs on gray16le on the engine side, so
 * everything below is a code over 65535 and the roundings differ per filter:
 * gblur uses lrintf (q16r), lut3d and geq truncate (q16f), and
 * blend=all_mode=multiply is a truncating integer divide, MEASURED against
 * ffmpeg on this machine rather than read off the source. */
eq("q16r rounds", M.q16r(0.5), 32768);
eq("q16f truncates", M.q16f(0.5), 32767);
eq("q16f of one is full swing", M.q16f(1), 65535);
eq("q16r clamps low", M.q16r(-1), 0);
eq("q16f clamps high", M.q16f(2), 65535);
eq("mul16 at full swing is exact", M.mul16(65535, 65535), 65535);
eq("mul16 truncates: 30000 x 30000 is 13733, measured", M.mul16(30000, 30000), 13733);
eq("mul16 truncates: 40000 x 50000 is 30518, measured", M.mul16(40000, 50000), 30518);
eq("mul16 by nothing is nothing", M.mul16(65535, 0), 0);
eq("an 8 bit window code lifts by 257", M.q16r(M.q8(0.6) / 255), M.q8(0.6) * 257);

// Python's round(), which is how a grow fraction becomes a pass count.
eq("pyRound breaks a half to even, down", M.pyRound(2.5), 2);
eq("pyRound breaks a half to even, up", M.pyRound(3.5), 4);
eq("pyRound is ordinary elsewhere", M.pyRound(2.6), 3);
eq("the grow cap is the engine's 32", M.GROW_MAX, 32);
/* ... and 32 is quoted AT a width, not at whatever width each side happens to
 * run (round 1 finding 31). The cap has to travel with the reference width or
 * the number means nothing, so they are asserted together. */
eq("the cap's reference width is the engine's 1920", M.GROW_REF_WIDTH, 1920);
eq("at the reference width the cap is the flat 32 it always was",
   M.growPasses(1.0, 1920), 32);
eq("a 640 preview clamps at 11 passes", M.growPasses(1.0, 640), 11);
eq("a 3840 render clamps at 64 passes", M.growPasses(1.0, 3840), 64);
/* The point of the whole change, as a number: the clamped grow is the same
 * FRACTION of the frame at every width, to within the one pass the integer
 * cap has to round to. Before it, 640 clamped at 1.72 percent of width and
 * 3840 at 0.83 percent, a factor of 2.1. */
check("the clamped grow is the same fraction of the frame at 640 and 3840",
      Math.abs(M.growPasses(1.0, 640) / 640
               - M.growPasses(1.0, 3840) / 3840) < 0.002,
      M.growPasses(1.0, 640) / 640 + " vs " + M.growPasses(1.0, 3840) / 3840);
eq("a grow under the cap is still just round(grow * width)",
   M.growPasses(0.01, 640), 6);
eq("the cap never falls below one pass, however small the frame",
   M.growPasses(1.0, 8), 1);

eq("usesComponents: absent", M.usesComponents({}), false);
eq("usesComponents: empty", M.usesComponents({ components: [] }), false);
eq("usesComponents: one disabled entry still counts",
   M.usesComponents({ components: [{ enabled: false }] }), true);

eq("components drops the disabled",
   M.components({ components: [{ id: "a" }, { id: "b", enabled: false }] }).length, 1);
eq("components fills the defaults in",
   M.components({ components: [{ id: "a" }] })[0].op, "add");

// ------------------------------------------------------------------- ops

/* Two constant components. A sampled matte frame is an 8 bit PNG that
 * gray16le lifts by 257, so 0.8 is 204 lifted to 52428 and 0.25 is 64 lifted
 * to 16448. Every expected number below is worked out by hand from the
 * measured ffmpeg formulas, not by calling the same helpers under test. */
const A = 0.8, B = 0.25;
const CA = 204 * 257, CB = 64 * 257;
eq("op input a lifts to 52428", CA, 52428);
eq("op input b lifts to 16448", CB, 16448);

const opStack = (op) => constStack(
  [{ id: "a", type: "matte", op: "add" }, { id: "b", type: "matte", op: op }],
  null, false, { a: A, b: B })[0];

// blend=all_mode=lighten
eq("add is max", opStack("add"), CA);
// blend=all_mode=multiply: floor(52428 * 16448 / 65535) = floor(862335744/65535)
eq("intersect is a times b", opStack("intersect"), 13158);
// negate then multiply: floor(52428 * 49087 / 65535) = floor(2573533236/65535)
eq("subtract is a times one minus b", opStack("subtract"), 39269);

eq("the accumulator starts at 0, so a leading intersect selects nothing",
   constStack([{ id: "a", type: "matte", op: "intersect" }], null, false, { a: A })[0], 0);
eq("a leading subtract selects nothing either",
   constStack([{ id: "a", type: "matte", op: "subtract" }], null, false, { a: A })[0], 0);

/* The engine reaches that answer by DROPPING the leading component instead of
 * folding it (stack_components), and this file mirrors that, because
 * layerActive has to agree with layer_active about whether the layer exists
 * and the cube slot names have to agree about which component is which. */
eq("a leading intersect is dropped, not folded",
   M.stackComponents({ components: [{ id: "a", op: "intersect" }, { id: "b", op: "add" }] })
     .map(e => e.comp.id).join(","), "b");
eq("and the surviving component keeps its RAW index, so a slot name is stable",
   M.stackComponents({ components: [{ id: "a", op: "intersect" }, { id: "b", op: "add" }] })[0].index, 1);
eq("a disabled component is dropped too",
   M.stackComponents({ components: [{ id: "a", enabled: false }, { id: "b" }] })
     .map(e => e.index).join(","), "1");
eq("everything after the first add survives, whatever its op",
   M.stackComponents({ components: [{ id: "a" }, { id: "b", op: "subtract" },
                                    { id: "c", op: "intersect" }] }).length, 3);
eq("a stack that is nothing but an intersect reaches nothing",
   M.stackComponents({ components: [{ id: "a", op: "intersect" }] }).length, 0);

// Dropping it changes nothing about the pixels, which is the whole point.
{
  const folded = constStack([{ id: "z", type: "matte", op: "subtract" },
                             { id: "a", type: "matte", op: "add" }],
                            null, false, { z: 1, a: A });
  const alone = constStack([{ id: "a", type: "matte", op: "add" }], null, false, { a: A });
  check("a leading subtract cannot change the answer",
        Array.from(folded).every((v, i) => v === alone[i]));
}

eq("component invert flips the component",
   constStack([{ id: "a", type: "matte", op: "add", invert: true }],
              null, false, { a: B })[0], 65535 - CB);

eq("mask.invert flips the result",
   constStack([{ id: "a", type: "matte", op: "add" }], null, true, { a: A })[0],
   65535 - CA);

eq("an absent matte reads 0",
   M.stack({ components: [{ id: "z", type: "matte", op: "add" }] }, 2, 2, {})[0], 0);
eq("an absent matte INVERTED reads 1, which is the documented trap",
   M.stack({ components: [{ id: "z", type: "matte", op: "add", invert: true }] },
           2, 2, {})[0], 65535);

// --------------------------------------------------------------- finesse

const plain = constStack([{ id: "a", type: "matte", op: "add" }], null, false, { a: A });
const withDefaults = constStack([{ id: "a", type: "matte", op: "add" }],
                                { blur: 0, grow: 0, clean_black: 0, clean_white: 0 },
                                false, { a: A });
check("a default finesse block is the exact identity",
      plain.every((v, i) => v === withDefaults[i]));

// The knee: identity at zero, both ends, and monotone in between.
let kneeIdentity = true, kneeMonotone = true, prev = -1;
for (let i = 0; i <= 255; i++) {
  const v = i / 255;
  if (Math.abs(M.softKnee(v, 0, 0) - v) > 1e-12) kneeIdentity = false;
  const k = M.softKnee(v, 0.2, 0.1);
  if (k < prev - 1e-12) kneeMonotone = false;
  prev = k;
}
check("softKnee at 0/0 is the identity on every 8 bit value", kneeIdentity);
check("softKnee is monotone", kneeMonotone);
near("clean_black pushes everything at the threshold to 0", M.softKnee(0.2, 0.2, 0), 0);
near("clean_white pushes everything at the threshold to 1", M.softKnee(0.9, 0, 0.1), 1);
near("below clean_black is 0", M.softKnee(0.1, 0.2, 0), 0);
near("above clean_white is 1", M.softKnee(0.95, 0, 0.1), 1);
// lo 0.2, hi 1, span 0.8, s 0.2: t = 0.5 at v = 0.6, and the knee leaves the
// midpoint of a smoothstep alone, so the answer is exactly t.
near("the knee does not move the midpoint", M.softKnee(0.6, 0.2, 0), 0.5, 1e-12);
check("the knee is a real curve, not a rescale",
      Math.abs(M.softKnee(0.4, 0.2, 0) - 0.25) > 1e-6,
      "got " + M.softKnee(0.4, 0.2, 0));

eq("clean_black in a stack pushes a low matte to 0",
   constStack([{ id: "a", type: "matte", op: "add" }],
              { blur: 0, grow: 0, clean_black: 0.5, clean_white: 0 },
              false, { a: 0.3 })[0], 0);
eq("clean_white in a stack pushes a high matte to full swing",
   constStack([{ id: "a", type: "matte", op: "add" }],
              { blur: 0, grow: 0, clean_black: 0, clean_white: 0.5 },
              false, { a: 0.7 })[0], 65535);

/* cleanParams is cinegrade's clean_geq, clamps and 6 decimal round trip and
 * all. The clamps are not decoration: without the lo cap a clean_black of 1
 * is a divide by zero, and the knee is the SUM of the two controls, which is
 * the one place this file used to disagree with the engine. */
{
  const p0 = M.cleanParams(0, 0);
  eq("no cleaning is lo 0, den 1, knee 0", [p0.lo, p0.den, p0.knee].join(","), "0,1,0");
  const p1 = M.cleanParams(0.2, 0.1);
  eq("lo is clean_black", p1.lo, 0.2);
  eq("den is 1 - clean_white - clean_black", p1.den, 0.7);
  eq("the knee is the SUM of the two, not the larger", p1.knee, 0.3);
  const p2 = M.cleanParams(1, 1);
  eq("clean_black is capped so the span cannot collapse", p2.lo, 0.999);
  eq("the span keeps a floor of one thousandth", p2.den, 0.001);
  eq("the knee saturates at 1", p2.knee, 1);
}

/* Finesse ORDER: clean, then grow, then blur.
 *
 * Tested as an order, not as "cleaning does something". The composed answer is
 * rebuilt here out of the very primitives maskStackCPU is made of (softKnee,
 * morph, gblur and the two roundings, all exported for exactly this), once in
 * the documented order and once with one step moved, and then:
 *
 *   - the documented order has to match M.stack CODE FOR CODE, which pins the
 *     order completely rather than bounding it, and
 *   - each wrong order has to differ, in a stated direction.
 *
 * This block used to compare "clean plus blur" against "blur alone", which
 * only proves that cleaning changes something: it would have passed just as
 * happily with the order reversed, which is the one thing it claimed to check.
 * The order matters because it decides whether the softness survives: clean
 * last re-crushes the ramp the blur just made. */
{
  const W = 32, H = 32;
  const win = { shape: "rect", cx: 0.5, cy: 0.5, w: 0.5, h: 0.5,
                rotation: 0, softness: 0.4, invert: false };
  const comps = [{ id: "w", type: "window", op: "add", window: win }];
  const fin = { blur: 0.06, grow: 0.09, clean_black: 0.3, clean_white: 0.2 };
  const got = M.stack({ components: comps, finesse: fin }, W, H, {});

  /* The combined matte BEFORE finesse. One add component over an accumulator
   * that starts at 0 folds to max(0, b), which is the component itself on the
   * 16 bit lattice, so this is the same array the finesse steps below see. */
  function base() {
    const w = M.window(win, W, H);
    const a = new Float64Array(W * H);
    for (let i = 0; i < a.length; i++) a[i] = M.q16r(w[i]) / M.MAX;
    return a;
  }
  // geq truncates its own output, which is why this is q16f and not q16r.
  function clean(a) {
    const o = Float64Array.from(a);
    for (let i = 0; i < o.length; i++) {
      o[i] = M.q16f(M.softKnee(o[i], fin.clean_black, fin.clean_white)) / M.MAX;
    }
    return o;
  }
  function grow(a) {
    // The cap is a fraction of the width now (finding 31), so the hand
    // composition asks for the pass count the same way the stack does.
    return M.morph(a, W, H, M.growPasses(fin.grow, W), fin.grow > 0);
  }
  function blur(a) { return M.gblur(a, W, H, fin.blur * W); }
  function codes(a) {
    const o = new Uint16Array(a.length);
    for (let i = 0; i < a.length; i++) o[i] = M.q16r(a[i]);
    return o;
  }
  const documented = codes(blur(grow(clean(base()))));
  const cleanLast = codes(clean(blur(grow(base()))));
  const growLast = codes(grow(blur(clean(base()))));

  const soft = (u) => Array.from(u).filter(v => v > 0 && v < M.MAX).length;
  const total = (u) => Array.from(u).reduce((s, v) => s + v, 0);

  check("the stack's finesse IS clean, then grow, then blur, code for code",
        Array.from(got).every((v, i) => v === documented[i]),
        "the stack and a hand composed clean/grow/blur disagree");
  check("cleaning LAST is a different picture (the order is not free)",
        Array.from(got).some((v, i) => v !== cleanLast[i]));
  check("and cleaning last crushes the ramp the blur made: fewer soft codes",
        soft(cleanLast) < soft(documented),
        "clean last " + soft(cleanLast) + " soft codes, documented order "
        + soft(documented));
  check("growing LAST is a different picture too",
        Array.from(got).some((v, i) => v !== growLast[i]));
  check("and growing after the blur selects less than growing before it",
        total(growLast) < total(documented),
        "grow last totals " + total(growLast) + ", documented order "
        + total(documented));
  check("the softness survives the documented order, so the edge is a ramp",
        soft(documented) > 0);
}

/* The grow step in COMPOSITION: the pass count is Python's round() of
 * grow * width, capped at 32 passes AT 1920 WIDE (so 17 at the 1000 this
 * block composes at).
 *
 * pyRound is unit tested above, but the number that reaches morph is the one
 * that matters, and nothing tested THAT: the old block only checked the cap,
 * so a stack using JavaScript's Math.round (which rounds a half away from
 * zero, where Python rounds it to even) would have passed. A rect window with
 * no softness makes the pass count directly measurable: a dilate of n passes
 * widens the run of fully selected pixels by exactly n on each side.
 *
 * 1000 wide rather than the 200 this used to compose at: with the cap now a
 * fraction of the width, 200 wide clamps at 3 passes and the rounding checks
 * below (2.5 -> 2, 3.5 -> 4) would be measuring the cap instead of the
 * rounding. The widths are the only thing that changed here. */
{
  const W = 1000, H = 8;
  const comps = [{ id: "w", type: "window", op: "add",
                   window: { shape: "rect", cx: 0.5, cy: 0.5, w: 0.2, h: 0.9,
                             rotation: 0, softness: 0, invert: false } }];
  function widthOf(u16) {
    const y = Math.floor(H / 2);
    let n = 0;
    for (let x = 0; x < W; x++) if (u16[y * W + x] === M.MAX) n++;
    return n;
  }
  function stackWith(grow) {
    return M.stack({ components: comps,
                     finesse: { blur: 0, grow: grow, clean_black: 0, clean_white: 0 } },
                   W, H, {});
  }
  const flat = widthOf(stackWith(0));
  check("the un-grown rect is a measurable run of selected pixels", flat > 8, "run " + flat);
  // 0.004 * 1000 = 4 exactly: four passes, four pixels each side.
  eq("a grow of 4 passes widens the selection by 4 pixels each side",
     widthOf(stackWith(0.004)), flat + 8);
  /* 0.0025 * 1000 = 2.5, which Python rounds DOWN to 2 (round half to even)
   * and JavaScript rounds UP to 3. Two passes, so four pixels, not six: this
   * is the assertion that fails if the pass count ever stops going through
   * pyRound. */
  eq("a grow of exactly two and a half passes rounds to two, Python's way",
     widthOf(stackWith(0.0025)), flat + 4);
  // 0.0035 * 1000 = 3.5, which rounds to 4 both ways: the half-to-even rule
  // only shows up on an odd half, and this pins the other side of it.
  eq("a grow of three and a half passes rounds to four",
     widthOf(stackWith(0.0035)), flat + 8);

  // The cap at THIS width: 32 passes at 1920 is 17 passes at 1000.
  const cap = M.growPasses(1.0, W);
  eq("the cap at 1000 wide is 32 scaled to it", cap, 17);
  const big = stackWith(0.5);
  const capped = stackWith(cap / W);
  // 0.5 * 1000 = 500 passes and 0.017 * 1000 = 17: both clamp to 17, so the
  // two mattes are the same picture and a grow past the cap does nothing more.
  check("grow is capped, so a bigger grow does not grow further",
        Array.from(big).every((v, i) => v === capped[i]));
  eq("the capped grow really is the cap's worth of passes, measured",
     widthOf(big), flat + 2 * cap);
  eq("and the cap is 32 at the reference width", M.GROW_MAX, 32);
}

// ------------------------------------------------- the key and luma cubes

/* A luma component is a KEY with the hue and saturation halves opened all the
 * way, which is how the engine spells it (component_key). Getting this wrong
 * is not subtle: lutRequests asks the server for a cube keyed on this block,
 * so a luma whose hue was left at the default would be baked as an orange key
 * 40 degrees wide and select almost nothing. */
{
  const asKey = M.componentKey({ type: "key", key: { hue_center: 200, lum_low: 0.3 } });
  eq("a key component keeps its own hue", asKey.hue_center, 200);
  eq("a key component keeps its own luma range", asKey.lum_low, 0.3);
  eq("the key block is always live inside a component", asKey.enabled, true);

  const asLuma = M.componentKey({ type: "luma", key: { hue_center: 200, lum_low: 0.3 } });
  eq("luma opens the hue window all the way", asLuma.hue_width, 360);
  eq("luma centres the hue window at zero", asLuma.hue_center, 0);
  eq("luma opens the saturation window all the way",
     [asLuma.sat_low, asLuma.sat_high].join(","), "0,1");
  eq("luma keeps the luminance range, which is the whole point", asLuma.lum_low, 0.3);

  // The cube the GPU asks the server for is cinegrade.key_matte_lut's own
  // layer: matte view on, this key, nothing else.
  const L = M.keyMatteLayer(asLuma);
  eq("the key cube layer is a matte view", L.mask.show, true);
  eq("the key cube layer carries no window stack", L.mask.components.length, 0);
  eq("the key cube layer is not inverted", L.mask.invert, false);
  eq("the key cube layer carries the widened key", L.mask.key.hue_width, 360);
}

/* One answer to "what type is this component" (round 1 finding 30).
 *
 * The three places that asked it used to disagree, and the disagreement only
 * showed on a type that was not spelled exactly right: `type: "Matte"` was a
 * matte to the engine, a window to the shader and a key to this reference. So
 * the checks below are about the AWKWARD spellings, not the ordinary ones. */
{
  eq("a type is lowercased, the way the engine lowercases it",
     M.componentType({ type: "Matte" }), "matte");
  eq("luma folds into key, so nothing downstream carries a fourth branch",
     M.componentType({ type: "LUMA" }), "key");
  eq("no type at all is a window, the engine's own default",
     M.componentType({}), "window");
  eq("the four spellings the engine knows are the four this file knows",
     M.TYPES.join(","), "matte,key,luma,window");
  let threw = "";
  try { M.componentType({ type: "gradient" }); } catch (e) { threw = e.message; }
  check("a type outside those four is an error, not a silent window",
        threw.indexOf("gradient") >= 0 && threw.indexOf("is not one of") >= 0,
        threw || "nothing was thrown");
  /* And the stack really uses it: a capitalised matte type used to compose as
   * a KEY here (the fall-through) while the engine composed a matte. With no
   * sample function a matte composes as black, so the two spellings have to
   * give the same picture. */
  const comps = (t) => ({ components: [{ id: "c", type: t, op: "add",
                                         matte: { id: "m_x" } }] });
  const lower = M.stack(comps("matte"), 8, 4, {});
  const upper = M.stack(comps("Matte"), 8, 4, {});
  check("a capitalised matte composes as a matte, not as whatever came last",
        Array.from(lower).every((v, i) => v === upper[i]),
        Array.from(lower).join(",") + " vs " + Array.from(upper).join(","));
}

// ------------------------------------------------------------ morphology

function grid(W, H, fn) {
  const a = new Float64Array(W * H);
  for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) a[y * W + x] = fn(x, y);
  return a;
}

{
  const W = 7, H = 7;
  const impulse = grid(W, H, (x, y) => (x === 3 && y === 3) ? 1 : 0);
  const d1 = M.morph(impulse, W, H, 1, true);
  let on = 0;
  for (let i = 0; i < W * H; i++) if (d1[i] === 1) on++;
  eq("dilate by 1 is a 3x3 square", on, 9);
  const d2 = M.morph(impulse, W, H, 2, true);
  on = 0;
  for (let i = 0; i < W * H; i++) if (d2[i] === 1) on++;
  eq("dilate by 2 is a 5x5 square", on, 25);

  const block = grid(W, H, (x, y) => (x >= 2 && x <= 4 && y >= 2 && y <= 4) ? 1 : 0);
  const e1 = M.morph(block, W, H, 1, false);
  on = 0;
  for (let i = 0; i < W * H; i++) if (e1[i] === 1) on++;
  eq("erode by 1 leaves only the centre of a 3x3 block", on, 1);
  eq("erode keeps the centre where it was", e1[3 * W + 3], 1);

  const flat = grid(W, H, () => 0.5);
  const df = M.morph(flat, W, H, 3, true);
  check("dilating a flat field changes nothing, borders included",
        Array.from(df).every(v => v === 0.5));
  eq("radius 0 is the identity", M.morph(impulse, W, H, 0, true)[3 * W + 3], 1);
}

// ------------------------------------------------------------------ blur

{
  const W = 32, H = 8;
  const flat = grid(W, H, () => 0.5);
  const b = M.gblur(flat, W, H, 3.0);
  const code = M.q16r(0.5) / M.MAX;
  check("gblur is normalised: a flat field survives it, borders included",
        Array.from(b).every(v => Math.abs(v - code) <= 1 / 255),
        "worst " + Math.max(...Array.from(b).map(v => Math.abs(v - code) * 255)));

  const step = grid(W, H, (x) => x < W / 2 ? 1 : 0);
  const sb = M.gblur(step, W, H, 3.0);
  check("gblur softens a hard edge", sb[4 * W + 15] < 1 && sb[4 * W + 16] > 0);
  check("gblur output is on the 16 bit lattice",
        Array.from(sb).every(v => Math.abs(v * M.MAX - Math.round(v * M.MAX)) < 1e-6));
  let mono = true;
  for (let x = 1; x < W; x++) if (sb[4 * W + x] > sb[4 * W + x - 1] + 1e-9) mono = false;
  check("gblur of a step is monotone", mono);
}

// ---------------------------------------------------------------- windows

{
  const W = 64, H = 64;
  const rect = M.window({ shape: "rect", cx: 0.5, cy: 0.5, w: 0.5, h: 0.5,
                          rotation: 0, softness: 0, invert: false }, W, H);
  eq("a hard rect is solid at its centre", rect[32 * W + 32], 1);
  eq("a hard rect is empty outside", rect[2 * W + 2], 0);
  const inv = M.window({ shape: "rect", cx: 0.5, cy: 0.5, w: 0.5, h: 0.5,
                         rotation: 0, softness: 0, invert: true }, W, H);
  eq("window invert flips it", inv[32 * W + 32], 0);
  eq("window invert flips the outside too", inv[2 * W + 2], 1);

  const soft = M.window({ shape: "ellipse", cx: 0.5, cy: 0.5, w: 0.5, h: 0.5,
                          rotation: 0, softness: 0.5, invert: false }, W, H);
  check("a feathered ellipse has intermediate values",
        Array.from(soft).some(v => v > 0 && v < 1));
  check("every window value is on the 8 bit lattice",
        Array.from(soft).every(v => Math.abs(v * 255 - Math.round(v * 255)) < 1e-9));
}

/* The linear gradient (new in v2), on the engine's formula.
 *
 * Three things about it are easy to get wrong and this file got all three
 * wrong before M2's engine settled them: the gradient runs along the ROTATED
 * VERTICAL (so angle 0 selects the TOP of the frame, not the left), the
 * transition width is `w` as a fraction of frame WIDTH on both axes, and
 * `softness` is an EASE EXPONENT across that transition rather than a
 * smoothstep blend. Softness 0 is a hard step at the centre line, softness 1
 * is a straight ramp, and above 1 eases both ends. */
{
  const W = 100, H = 100;
  const lin = (softness, extra) => M.window(Object.assign({
    shape: "linear", cx: 0.5, cy: 0.5, w: 0.5, h: 0.5,
    angle: 0, softness: softness, invert: false
  }, extra || {}), W, H);
  const code = (a, x, y) => Math.round(a[y * W + x] * 255);

  const hard = lin(0);
  eq("softness 0 selects the top of the frame", code(hard, 50, 0), 255);
  eq("softness 0 drops the bottom of the frame", code(hard, 50, 99), 0);
  eq("softness 0 is a step, on at the centre line", code(hard, 50, 50), 255);
  eq("softness 0 is a step, off one row below", code(hard, 50, 51), 0);

  // w 0.5 of a 100 wide frame is a 50 pixel transition centred on cy = 50,
  // so t = clip(0.5 - (y - 50)/50, 0, 1) and softness 1 gives m = t exactly.
  const ramp = lin(1);
  const t = (y) => Math.max(0, Math.min(1, 0.5 - (y - 50) / 50));
  let straight = true;
  for (let y = 0; y < H; y++) {
    if (code(ramp, 50, y) !== M.q8(t(y))) straight = false;
  }
  check("softness 1 is a straight ramp across the transition", straight);
  eq("the ramp is full at the top of the transition", code(ramp, 50, 25), 255);
  eq("the ramp is empty at the bottom of the transition", code(ramp, 50, 75), 0);
  eq("the ramp is half at the centre line", code(ramp, 50, 50), 128);

  // ease 2: below the midpoint m = 2t^2, so at t = 0.3 (y = 60) it is 0.18.
  const eased = lin(2);
  eq("softness 2 eases the lower half in", code(eased, 50, 60), M.q8(2 * 0.3 * 0.3));
  eq("softness 2 still crosses the centre line at half", code(eased, 50, 50), 128);
  // ease 0.5 is snappier, so the same point sits higher than the straight ramp.
  const snappy = lin(0.5);
  check("softness below 1 is snappier than the straight ramp",
        code(snappy, 50, 60) > code(ramp, 50, 60),
        "snappy " + code(snappy, 50, 60) + " straight " + code(ramp, 50, 60));

  let mono = true;
  for (let y = 1; y < H; y++) {
    if (code(ramp, 50, y) > code(ramp, 50, y - 1)) mono = false;
  }
  check("the gradient is monotone down the frame", mono);

  // Turned 90 degrees the gradient selects the RIGHT of the frame, because
  // uy = -dx there and the selected half is uy <= 0.
  const turned = lin(0, { angle: 90 });
  eq("at angle 90 the right of the frame is selected", code(turned, 99, 50), 255);
  eq("at angle 90 the left of the frame is not", code(turned, 0, 50), 0);

  /* `angle` absent falls back to `rotation`, which is what
   * win.get("angle", win.get("rotation", 0)) does in the engine and the one
   * reason WINDOW_DEFAULTS must not carry an `angle` key. */
  const byRotation = M.window({ shape: "linear", cx: 0.5, cy: 0.5, w: 0.5, h: 0.5,
                                rotation: 90, softness: 0, invert: false }, W, H);
  let sameAim = true;
  for (let i = 0; i < W * H; i++) if (byRotation[i] !== turned[i]) sameAim = false;
  check("a gradient with only a rotation is aimed by it", sameAim);

  const flipped = lin(0, { invert: true });
  eq("invert flips the gradient", code(flipped, 50, 0), 0);
  eq("invert flips the other side too", code(flipped, 50, 99), 255);
}

// A window component inside a stack agrees with the window on its own.
{
  const W = 32, H = 32;
  const win = { shape: "rect", cx: 0.5, cy: 0.5, w: 0.5, h: 0.5,
                rotation: 0, softness: 0.2, invert: false };
  const alone = M.window(win, W, H);
  const inStack = M.stack({ components: [{ id: "w", type: "window", op: "add", window: win }] },
                          W, H, {});
  // The engine bakes a window component as an 8 bit PNG and gray16le lifts
  // it by 257, so the stack's 16 bit code is the window's 8 bit code times 257
  // with nothing lost on the way.
  let same = true;
  for (let i = 0; i < W * H; i++) {
    if (Math.round(alone[i] * 255) * 257 !== inStack[i]) same = false;
  }
  check("one window component alone is exactly that window's matte", same);
}

// feather blurs the component, and it happens AFTER the invert.
{
  const W = 32, H = 32;
  const win = { shape: "rect", cx: 0.5, cy: 0.5, w: 0.5, h: 0.5,
                rotation: 0, softness: 0, invert: false };
  const hard = M.stack({ components: [{ id: "w", type: "window", op: "add", window: win }] },
                       W, H, {});
  const soft = M.stack({ components: [{ id: "w", type: "window", op: "add",
                                        window: win, feather: 0.02 }] }, W, H, {});
  check("feather softens the component", Array.from(soft).some(v => v > 0 && v < 65535));
  // gblur's IIR leaks a little everywhere, so "alone" is within a code and
  // not bit identical; at 8 bits that difference used to round away.
  check("feather leaves the middle alone",
        Math.abs(soft[16 * W + 16] - hard[16 * W + 16]) <= 1,
        "soft " + soft[16 * W + 16] + " hard " + hard[16 * W + 16]);

  const invFeather = M.stack({ components: [{ id: "w", type: "window", op: "add",
                                              window: win, feather: 0.02, invert: true }] },
                             W, H, {});
  // invert then feather: the inverted matte blurred is full swing minus the
  // blurred one everywhere the blur's own boundary handling is symmetric,
  // which the interior is. The frame's corners are where the two orders
  // differ, which is exactly why the order is pinned.
  check("invert before feather gives the complement in the interior",
        Math.abs((65535 - soft[16 * W + 16]) - invFeather[16 * W + 16]) <= 2,
        "soft " + soft[16 * W + 16] + " inv " + invFeather[16 * W + 16]);
}

// --------------------------------------------------- matte frames (rule 10)

eq("frameIndex is round(time * fps)", M.frameIndex({ fps: 24, frames: 100 }, 1.0), 24);
eq("frameIndex rounds", M.frameIndex({ fps: 24, frames: 100 }, 1.01), 24);
eq("frameIndex clamps to the last written frame",
   M.frameIndex({ fps: 24, frames: 10 }, 100), 9);
eq("frameIndex clamps at zero", M.frameIndex({ fps: 24, frames: 10 }, -5), 0);
eq("frameIndex is null without an index", M.frameIndex(null, 1.0), null);
eq("frameIndex is null with no fps", M.frameIndex({ fps: 0, frames: 10 }, 1.0), null);

/* The cache key has no render width in it any more. A matte frame is fetched
 * and cached at the matte's OWN size and scaled per pass with the ported
 * swscale filter, so one decoded frame serves every render size: keying on the
 * width would hold the same frame twice at 640 and at 1280, and the second
 * copy would be a different picture from the engine's anyway, since the engine
 * scales the store's frame itself. */
eq("frameKey uses the frame number when there is an index",
   M.frameKey("m1", { fps: 24, frames: 100 }, 1.0), "m1:24");
eq("frameKey falls back to the time when there is not",
   M.frameKey("m1", null, 1.0), "m1:t1.000");
eq("two times in one frame share a cache entry",
   M.frameKey("m1", { fps: 24, frames: 100 }, 1.00),
   M.frameKey("m1", { fps: 24, frames: 100 }, 1.01));
eq("the render width is NOT part of the key any more",
   M.frameKey("m1", { fps: 24, frames: 100 }, 1.0), M.frameKeyAt("m1", 24));
check("and the prefetch's own key formatter is the same one",
      M.frameKeyAt("m1", 7) === "m1:7");

/* A failed index fetch is retried with an exponential backoff rather than
 * remembered as broken for the session (which used to switch the prefetch off
 * permanently and degrade every key to the time form). */
eq("the first retry waits two seconds", M.retryDelay(1), 2000);
eq("the delay doubles", M.retryDelay(2), 4000);
eq("and stops doubling at thirty seconds", M.retryDelay(20), 30000);
check("a zero or negative try count is still a real delay", M.retryDelay(0) >= 2000);

/* The cache cap grows with the number of mattes on the config and shrinks with
 * the size of a frame. A flat 48 with a per matte read ahead of 8 is a cache
 * that thrashes as soon as a grade has six mattes on it: each matte's prefetch
 * evicts the frames another matte is about to need, and the state line sits on
 * "matte lagging" for the rest of the session. */
{
  const floor = M.PREFETCH + 2;
  eq("one matte gets the flat floor of 48", M.cacheCap(1, 0), M.CACHE_MAX);
  eq("so do four, whose windows still fit inside 48",
     M.cacheCap(4, 0), M.CACHE_MAX);
  eq("six mattes get a read ahead window each instead",
     M.cacheCap(6, 0), 6 * floor);
  eq("a small frame does not lower the cap",
     M.cacheCap(6, 64 * 36 * 4), 6 * floor);
  // An HD matte frame is 8.3 MB on the card, so the byte ceiling bites first.
  eq("an HD frame lowers the cap to what the byte ceiling allows",
     M.cacheCap(6, 1920 * 1080 * 4),
     Math.floor(M.CACHE_BYTES / (1920 * 1080 * 4)));
  // A 4K frame is 33 MB: the ceiling would allow seven, which is less than one
  // read ahead window, and a cache smaller than the read ahead cannot work at
  // all, so the floor wins and the ceiling is deliberately overshot.
  eq("but the cap never drops below one read ahead window plus two",
     M.cacheCap(6, 3840 * 2160 * 4), floor);
  eq("even for an absurd frame size", M.cacheCap(6, M.CACHE_BYTES), floor);
}

{
  const order = [];
  M.lruTouch(order, "a"); M.lruTouch(order, "b"); M.lruTouch(order, "a");
  eq("lruTouch moves a repeat to the end", order.join(","), "b,a");

  const frames = {}, last = {}, dropped = [];
  for (const k of ["a", "b", "c", "d", "e"]) {
    frames[k] = { id: "m", tex: k };
    M.lruTouch(order.length ? order : order, k);
  }
  // rebuild a clean order for the eviction test
  const ord2 = ["a", "b", "c", "d", "e"];
  last.m = frames.a;
  M.lruEvict(ord2, frames, last, 3, (e) => dropped.push(e.tex));
  eq("lruEvict drops the least recently used first", dropped.join(","), "a,b");
  eq("lruEvict leaves the cache at the cap", ord2.join(","), "c,d,e");
  eq("lruEvict frees the entries it dropped", Object.keys(frames).sort().join(","), "c,d,e");
  eq("lruEvict clears a fallback it evicted", last.m, undefined);
}

// ------------------------------------------------------- legacy is legacy

{
  const cfg = SG.fullConfig({});
  eq("the default config still has an empty stack", cfg.layers.length, 0);
  const legacy = SG.fullConfig({
    layers: [{ mask: { window: { enabled: true } }, correct: { exposure: 0.5 } }]
  });
  eq("a legacy layer does not take the component path",
     M.usesComponents(SG.configLayers(legacy)[0].mask), false);
  eq("a legacy layer's defaults now carry an empty stack",
     SG.configLayers(legacy)[0].mask.components.length, 0);
  eq("and a default finesse block",
     M.finesseActive(M.finesse(SG.configLayers(legacy)[0].mask)), false);

  const v2 = SG.fullConfig({
    layers: [{ mask: { components: [{ id: "c1", type: "window", op: "add" }] } }]
  });
  eq("a v2 layer takes the component path",
     M.usesComponents(SG.configLayers(v2)[0].mask), true);
  eq("a v2 layer is active", SG.layerActive(SG.configLayers(v2)[0]), true);

  const empty = SG.fullConfig({ layers: [{ mask: { components: [] } }] });
  eq("an absent stack is the legacy path, so the layer is still active",
     SG.layerActive(SG.configLayers(empty)[0]), true);
  const allOff = SG.fullConfig({
    layers: [{ mask: { components: [{ id: "c1", enabled: false }] } }]
  });
  eq("a stack with nothing switched on selects nothing, so the layer is dropped",
     SG.layerActive(SG.configLayers(allOff)[0]), false);
  const onlyIntersect = SG.fullConfig({
    layers: [{ mask: { components: [{ id: "c1", op: "intersect" }] } }]
  });
  eq("a stack that is nothing but an intersect is dropped, as in layer_active",
     SG.layerActive(SG.configLayers(onlyIntersect)[0]), false);
  const allOffInverted = SG.fullConfig({
    layers: [{ mask: { invert: true, components: [{ id: "c1", enabled: false }] } }]
  });
  eq("inverted, the same stack is a global correction",
     SG.layerActive(SG.configLayers(allOffInverted)[0]), true);
}

// ------------------------------------------------------------ the report

{
  const rep = SG.stageReport({
    layers: [{ mask: { components: [
      { id: "c1", type: "matte", op: "add", matte: { id: "m_abc" } }
    ] } }]
  });
  const row = rep.stages.filter(s => s.id === "layer0")[0];
  eq("a v2 layer gets its own row", !!row, true);
  eq("and claims close until the fixtures measure it", row.status, "close");
  check("the note names the component", row.note.indexOf("add matte m_abc") >= 0, row.note);

  const lagging = SG.stageReport({
    layers: [{ mask: { components: [
      { id: "c1", type: "matte", op: "add", matte: { id: "m_abc" } }
    ] } }]
  }, { m_abc: { state: "partial", want: 90, got: 42, lagging: true } });
  const row2 = lagging.stages.filter(s => s.id === "layer0")[0];
  check("a lagging matte is reported, not hidden",
        row2.note.indexOf("matte lagging") >= 0, row2.note);
  check("and names the frame it wanted and the frame it served",
        row2.note.indexOf("wanted frame 90") >= 0
        && row2.note.indexOf("serving frame 42") >= 0, row2.note);
  eq("the row carries the lagging list too", row2.lagging.length, 1);

  // The stage report is still callable with one argument, which is how every
  // existing caller calls it.
  eq("stageReport with no live state still works",
     SG.stageReport({}).overall, "exact");
}

// ------------------------------------------------------------------ done

console.log((fail ? "FAIL" : "PASS") + "  " + pass + " checks passed, " + fail + " failed");
for (const f of failures) console.log("  FAIL  " + f);
process.exit(fail ? 1 : 0);
