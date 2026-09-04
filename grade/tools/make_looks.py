"""Generate creative LOOK LUTs, applied in Rec.709 after the CST.

These are the interchangeable part. A purchased .cube (Jay's v2 pack, a Kodak
print emulation, anything) drops into the same slot and the rest of the chain
does not change. Everything here is built from scratch so nothing is copied.

Ops are composed in display code space, [0, 1] Rec.709.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import colorlib as C


# --- grading primitives ---------------------------------------------------

def contrast(rgb, amount, pivot=0.435):
    """S-curve around a pivot. 0.435 is Rec.709 mid grey in gamma 2.4."""
    return np.clip((rgb - pivot) * amount + pivot, 0.0, 1.0)


def soft_contrast(rgb, amount, pivot=0.435):
    """Contrast with a rolled shoulder and toe, so nothing clips to flat."""
    x = np.clip(rgb, 1e-6, 1.0)
    lift = (x - pivot) * amount + pivot
    # blend back toward the original at the extremes to protect the ends
    w = 1.0 - np.abs(np.clip(x, 0, 1) * 2.0 - 1.0) ** 3
    return np.clip(x + (lift - x) * w, 0.0, 1.0)


def saturation(rgb, amount):
    return np.clip(C.luma709(rgb) + (rgb - C.luma709(rgb)) * amount, 0.0, 1.0)


def sat_by_luma(rgb, shadow_sat=1.0, mid_sat=1.0, high_sat=1.0):
    """Desaturate shadows and highlights independently. Very film-like."""
    y = np.clip(C.luma709(rgb), 0.0, 1.0)
    w_sh = np.clip(1.0 - y * 2.5, 0.0, 1.0)
    w_hi = np.clip((y - 0.6) * 2.5, 0.0, 1.0)
    w_mid = np.clip(1.0 - w_sh - w_hi, 0.0, 1.0)
    amt = w_sh * shadow_sat + w_mid * mid_sat + w_hi * high_sat
    grey = C.luma709(rgb)
    return np.clip(grey + (rgb - grey) * amt, 0.0, 1.0)


def split_tone(rgb, shadow_rgb=(0, 0, 0), highlight_rgb=(0, 0, 0), balance=0.5,
               protect_saturated=0.0, preserve_order=True, floor_frac=0.45):
    """Push shadows one way and highlights the other.

    Two different guards, and it matters which one does the work.

    `preserve_order` stops the push from reordering a pixel's channels, but
    only in proportion to how saturated the pixel already is. A dark maroon car
    at R.25 G.08 B.10 is a real object colour, so a -0.075 red and +0.080 blue
    push turning it blue-dominant is a bug and gets clamped. A warm near-neutral
    shadow on a sidewalk has no object colour to protect, and flipping it from
    warm to cool is the entire point of a teal/orange grade, so it is left
    alone. Applying the clamp to both is what makes a grade look like nothing
    happened: under warm light almost every shadow is red-dominant, so a blanket
    clamp cancels the shadow push across the whole frame.

    `floor_frac` stops the push from crushing a channel to zero. A -0.075 red
    shadow push meets a near-black pixel at R.06 and takes it to 0, which reads
    as a fully saturated teal blob instead of a cool shadow. Holding every
    channel at or above `floor_frac` of where it started keeps dark areas
    tinted rather than clipped.

    `protect_saturated` is the blunt version: it scales the push down on
    anything that already has a hue. It works, but at the strength needed to
    save the car it also erases the tint from grass, asphalt and sky, which is
    the entire visible half of a teal/orange grade. Keep it low and let
    `preserve_order` handle the inversion.
    """
    y = np.clip(C.luma709(rgb), 0.0, 1.0)
    w_hi = np.clip((y - balance) / max(1e-6, 1.0 - balance), 0.0, 1.0)
    w_sh = np.clip((balance - y) / max(1e-6, balance), 0.0, 1.0)
    if protect_saturated > 0.0:
        mx = rgb.max(-1, keepdims=True)
        mn = rgb.min(-1, keepdims=True)
        sat = (mx - mn) / np.maximum(mx, 1e-6)
        keep = 1.0 - protect_saturated * np.clip(sat * 1.6, 0.0, 1.0)
        w_hi = w_hi * keep
        w_sh = w_sh * keep

    push = np.array(shadow_rgb) * w_sh + np.array(highlight_rgb) * w_hi
    out = rgb + push

    if floor_frac > 0.0:
        # one scalar per pixel again, so the push direction survives
        kf = np.ones(rgb.shape[:-1] + (1,))
        for c in range(3):
            v = rgb[..., c:c + 1]
            p = push[..., c:c + 1]
            room = v * (1.0 - floor_frac)
            with np.errstate(divide='ignore', invalid='ignore'):
                lim = np.where(p < -room, room / np.maximum(-p, 1e-9), 1.0)
            kf = np.minimum(kf, np.clip(lim, 0.0, 1.0))
        push = push * kf
        out = rgb + push

    if preserve_order:
        # Scale the whole push back per pixel until the channel ranking of the
        # result matches the ranking of the input. One scalar per pixel, so the
        # push keeps its direction and only loses magnitude.
        k = np.ones(rgb.shape[:-1] + (1,))
        for a in range(3):
            for b in range(3):
                if a == b:
                    continue
                gap = rgb[..., a:a + 1] - rgb[..., b:b + 1]
                dp = push[..., a:a + 1] - push[..., b:b + 1]
                # only care where input had a ranking and the push undoes it
                bad = (gap > 0.0) & (gap + dp < 0.0)
                with np.errstate(divide='ignore', invalid='ignore'):
                    lim = np.where(bad, gap / np.maximum(-dp, 1e-9), 1.0)
                k = np.minimum(k, np.clip(lim, 0.0, 1.0))
        mx = rgb.max(-1, keepdims=True)
        mn = rgb.min(-1, keepdims=True)
        sat = (mx - mn) / np.maximum(mx, 1e-6)
        hold = np.clip((sat - 0.22) / 0.30, 0.0, 1.0)   # neutral shadows go free
        out = rgb + push * (1.0 - hold * (1.0 - k))

    return np.clip(out, 0.0, 1.0)


def lift_gamma_gain(rgb, lift=(0, 0, 0), gamma=(1, 1, 1), gain=(1, 1, 1)):
    x = np.clip(rgb, 0.0, 1.0)
    x = x + np.array(lift) * (1.0 - x)
    x = np.clip(x, 1e-6, 1.0) ** (1.0 / np.array(gamma))
    return np.clip(x * np.array(gain), 0.0, 1.0)


def film_toe(rgb, strength=0.12):
    """Milk the blacks the way a film print never reaches true zero."""
    return rgb * (1.0 - strength) + strength * (rgb ** 0.55) * strength


def channel_crosstalk(rgb, amount=0.06):
    """Bleed each channel into the others. Kills digital over-purity."""
    m = np.eye(3) * (1.0 - 2 * amount) + amount
    return np.clip(C.apply_matrix(rgb, m), 0.0, 1.0)


def hue_warm_skin(rgb, amount=0.05):
    """Nudge orange-family hues toward warm without moving the whole frame."""
    r, g, b = rgb[..., 0:1], rgb[..., 1:2], rgb[..., 2:3]
    mx, mn = rgb.max(-1, keepdims=True), rgb.min(-1, keepdims=True)
    sat = (mx - mn) / np.maximum(mx, 1e-6)
    is_skin = np.clip((r - b) * 3.0, 0.0, 1.0) * np.clip(sat * 3.0, 0.0, 1.0)
    warm = np.concatenate([r * (1 + amount), g * (1 + amount * 0.35), b], -1)
    return np.clip(rgb + (warm - rgb) * is_skin, 0.0, 1.0)


# --- the looks ------------------------------------------------------------

def look_neutral(x):
    return x


def look_kodak2383(x):
    """Print film emulation. Cool dense shadows, warm mids, creamy roll-off."""
    x = soft_contrast(x, 1.14)
    x = split_tone(x, shadow_rgb=(-0.012, -0.004, 0.020),
                   highlight_rgb=(0.020, 0.010, -0.014), balance=0.48)
    x = sat_by_luma(x, shadow_sat=0.82, mid_sat=1.10, high_sat=0.72)
    x = channel_crosstalk(x, 0.045)
    x = hue_warm_skin(x, 0.045)
    x = lift_gamma_gain(x, lift=(0.008, 0.007, 0.012), gamma=(1.0, 1.0, 0.99))
    return x


def look_teal_orange(x):
    """The blockbuster separation. Heavier handed, reads instantly."""
    x = soft_contrast(x, 1.20)
    x = split_tone(x, shadow_rgb=(-0.030, 0.004, 0.038),
                   highlight_rgb=(0.036, 0.012, -0.028), balance=0.46)
    x = sat_by_luma(x, shadow_sat=0.90, mid_sat=1.18, high_sat=0.70)
    x = hue_warm_skin(x, 0.07)
    return x


def look_warm_film(x):
    """Soft, warm, low contrast. The travel-vlog look most iPhone grades want."""
    x = soft_contrast(x, 1.06)
    x = lift_gamma_gain(x, lift=(0.020, 0.014, 0.010),
                        gamma=(1.02, 1.0, 0.98), gain=(1.02, 1.0, 0.985))
    x = split_tone(x, shadow_rgb=(0.004, 0.002, 0.014),
                   highlight_rgb=(0.026, 0.014, -0.010), balance=0.5)
    x = sat_by_luma(x, shadow_sat=0.88, mid_sat=1.08, high_sat=0.80)
    x = channel_crosstalk(x, 0.05)
    x = hue_warm_skin(x, 0.05)
    return x


def look_bleach(x):
    """Bleach bypass. Silver retained, so contrast up and saturation down."""
    y = C.luma709(x)
    x = np.clip(x * 0.55 + y * 0.45, 0, 1)
    x = soft_contrast(x, 1.35)
    x = sat_by_luma(x, shadow_sat=0.55, mid_sat=0.72, high_sat=0.45)
    x = split_tone(x, shadow_rgb=(-0.008, 0.0, 0.012),
                   highlight_rgb=(0.012, 0.010, 0.004))
    return x


def look_nordic(x):
    """Cold, desaturated, blue-green shadows. Overcast and moody."""
    x = soft_contrast(x, 1.10)
    x = split_tone(x, shadow_rgb=(-0.020, 0.000, 0.026),
                   highlight_rgb=(-0.008, 0.004, 0.016), balance=0.5)
    x = sat_by_luma(x, shadow_sat=0.70, mid_sat=0.88, high_sat=0.70)
    return x



def hue_push_greens(rgb, toward_yellow=0.10, desat=0.30):
    """Pull foliage green toward olive and drop its saturation.

    Digital green is the single biggest tell that footage was not shot on film.
    Detects green-dominant pixels and moves them only.
    """
    r, g, b = rgb[..., 0:1], rgb[..., 1:2], rgb[..., 2:3]
    mx, mn = rgb.max(-1, keepdims=True), rgb.min(-1, keepdims=True)
    sat = (mx - mn) / np.maximum(mx, 1e-6)
    is_green = np.clip((g - np.maximum(r, b)) * 4.0, 0.0, 1.0) * np.clip(sat * 2.5, 0.0, 1.0)
    olive = np.concatenate([r + (g - r) * toward_yellow, g, b * (1.0 - toward_yellow * 0.5)], -1)
    grey = C.luma709(olive)
    olive = grey + (olive - grey) * (1.0 - desat)
    return np.clip(rgb + (olive - rgb) * is_green, 0.0, 1.0)


def haze(rgb, amount=0.06, tint=(1.0, 0.92, 0.80)):
    """Lift the whole frame toward a tinted fog. Milky blacks, low contrast."""
    return np.clip(rgb * (1.0 - amount) + amount * np.array(tint), 0.0, 1.0)


def look_forest(x):
    """Moody forest: dense cool shadows, olive greens, creamy highlights."""
    x = soft_contrast(x, 1.22)
    x = hue_push_greens(x, toward_yellow=0.16, desat=0.38)
    x = split_tone(x, shadow_rgb=(-0.026, -0.006, 0.012),
                   highlight_rgb=(0.014, 0.010, -0.004), balance=0.44)
    x = sat_by_luma(x, shadow_sat=0.68, mid_sat=0.92, high_sat=0.62)
    x = lift_gamma_gain(x, lift=(0.0, 0.004, 0.008), gamma=(0.98, 1.0, 1.0))
    x = channel_crosstalk(x, 0.05)
    return x


def look_golden_haze(x):
    """Warm hazy film: milky lifted blacks, golden mids, very soft contrast."""
    x = soft_contrast(x, 0.94)
    x = haze(x, amount=0.075, tint=(1.0, 0.90, 0.76))
    x = lift_gamma_gain(x, lift=(0.036, 0.026, 0.014),
                        gamma=(1.03, 1.0, 0.96), gain=(1.0, 0.99, 0.965))
    x = split_tone(x, shadow_rgb=(0.010, 0.004, 0.006),
                   highlight_rgb=(0.030, 0.016, -0.012), balance=0.52)
    x = sat_by_luma(x, shadow_sat=0.78, mid_sat=1.02, high_sat=0.72)
    x = hue_warm_skin(x, 0.06)
    x = channel_crosstalk(x, 0.07)
    return x


def look_premium(x):
    """cinekit's balance + golden_haze's atmosphere + forest's olive greens.

    The palette is deliberately collapsed toward three families: warm skin,
    olive foliage, cool shadow. Restraint plus separation is what reads as
    expensive; a full-spectrum frame reads as a phone.

    Note the haze here is small. Most of the atmosphere comes from the bloom
    stage in FX, which is highlight driven, so it glows where there is light
    instead of uniformly greying the frame.
    """
    x = soft_contrast(x, 1.13)
    # the single biggest tell: digital green. Collapse it to olive first.
    x = hue_push_greens(x, toward_yellow=0.20, desat=0.42)
    # a whisper of global fog, just enough to break pure black
    x = haze(x, amount=0.030, tint=(1.0, 0.93, 0.82))
    # cool dense shadows against warm highlights: the separation that sells it
    x = split_tone(x, shadow_rgb=(-0.024, -0.005, 0.020),
                   highlight_rgb=(0.030, 0.015, -0.012), balance=0.46)
    # hold the mids, kill saturation at both ends
    x = sat_by_luma(x, shadow_sat=0.70, mid_sat=1.05, high_sat=0.60)
    # then buy the skin back out of that desaturation
    x = hue_warm_skin(x, 0.075)
    x = channel_crosstalk(x, 0.06)
    x = lift_gamma_gain(x, lift=(0.010, 0.009, 0.014), gamma=(1.0, 1.0, 0.99))
    return x


def look_natural(x):
    """Matched to the seven Reels references: warm, restrained, honest.

    The common thread across all of them is what they do NOT do. No heavy
    teal-orange, no milky lift, no crushed blacks. Saturation sits just under
    neutral, highlights are warm and softly rolled, shadows stay dense but
    keep detail, and greens read olive rather than the neon yellow-green a
    phone produces straight out of camera.
    """
    x = soft_contrast(x, 1.10)
    # Grass and foliage are the whole frame here, and untreated they are the
    # single loudest tell that this came off a phone.
    x = hue_push_greens(x, toward_yellow=0.13, desat=0.30)
    # warm highlights, barely cool shadows: separation without a colour cast
    x = split_tone(x, shadow_rgb=(-0.010, -0.002, 0.012),
                   highlight_rgb=(0.024, 0.012, -0.008), balance=0.50)
    x = sat_by_luma(x, shadow_sat=0.84, mid_sat=0.98, high_sat=0.74)
    x = hue_warm_skin(x, 0.05)
    x = channel_crosstalk(x, 0.045)
    # blacks land just off zero, the way a print never reaches true black
    x = lift_gamma_gain(x, lift=(0.006, 0.005, 0.008), gamma=(1.0, 1.0, 0.995))
    return x


def steer(h, s, weights_targets, original_h):
    """Rotate selected hue families toward target angles.

    weights_targets: list of (center, width, target, strength).
    Weights are always taken from the ORIGINAL hue, so families do not chase
    each other as earlier entries move them.
    """
    total_w = np.zeros_like(h)
    for center, width, target, strength in weights_targets:
        w = C.hue_weight(original_h, center, width)
        d = ((target - h + 180.0) % 360.0) - 180.0
        h = h + d * w * strength
        total_w = np.maximum(total_w, w)
    return h, total_w


def vibrance_curve(s, amount):
    """Boost mid-saturation hardest and leave already-saturated pixels alone.

    Straight multiplication is what sends skin radioactive in a bad teal/orange
    grade; this rolls off as saturation approaches 1.
    """
    return np.clip(s + amount * s * (1.0 - s), 0.0, 1.0)


def look_blockbuster(x):
    """Loud, deliberate teal and orange. Meant to be obvious.

    Built the way a real one is: contrast first so the split has range to work
    with, then hue families steered onto two complementary targets, then a
    luminance split so darks go teal and lights go warm. The saturation lift
    is a vibrance curve rather than a multiply, which is what keeps skin and
    the warm highlights believable instead of neon.
    """
    x = soft_contrast(x, 1.30)

    h, s, v = C.rgb_to_hsv(x)
    h0 = h.copy()

    # cool families onto one teal, warm families onto one orange
    h, w_cool = steer(h, s, [
        (215.0, 90.0, 196.0, 0.60),   # sky, shade, water
        (180.0, 55.0, 192.0, 0.55),   # existing cyans
        (100.0, 65.0, 162.0, 0.26),   # foliage: a nudge toward teal, it clips fast
    ], h0)
    h, w_warm = steer(h, s, [
        (32.0, 60.0, 28.0, 0.50),     # skin, wood, golden light
        (55.0, 45.0, 36.0, 0.45),     # yellows pulled back to orange
    ], h0)

    # push both families, leave everything between them alone
    s = vibrance_curve(s, 0.42 * w_cool + 0.46 * w_warm)
    x = C.hsv_to_rgb(h, s, v)

    # the luminance split: this is what reads as "graded" at a glance
    # balance sits high on purpose. At 0.50 only the darkest pixels count as
    # shadow, so once the frame is exposed to a normal brightness the cool half
    # of the split falls off the bottom of the image and the grade reads as
    # warm-only. At 0.62 the midtones cool as well, which is what a teal/orange
    # actually looks like: cool mids, warm highlights.
    x = split_tone(x, shadow_rgb=(-0.078, 0.012, 0.086),
                   highlight_rgb=(0.078, 0.024, -0.064), balance=0.62,
                   protect_saturated=0.20, preserve_order=True)

    x = sat_by_luma(x, shadow_sat=1.00, mid_sat=1.12, high_sat=0.76)
    # blacks stay rich and openly teal, never muddy grey
    x = lift_gamma_gain(x, lift=(0.0, 0.010, 0.026), gamma=(1.0, 1.0, 0.97))
    return x


def look_blockbuster_max(x):
    """Same engine, pushed further. Use when the grade should be the point."""
    x = soft_contrast(x, 1.38)
    h, s, v = C.rgb_to_hsv(x)
    h0 = h.copy()
    h, w_cool = steer(h, s, [
        (215.0, 95.0, 198.0, 0.78),
        (180.0, 60.0, 194.0, 0.70),
        (100.0, 70.0, 176.0, 0.62),
    ], h0)
    h, w_warm = steer(h, s, [
        (32.0, 65.0, 26.0, 0.65),
        (55.0, 50.0, 33.0, 0.60),
    ], h0)
    s = vibrance_curve(s, 0.80 * w_cool + 0.70 * w_warm)
    x = C.hsv_to_rgb(h, s, v)
    x = split_tone(x, shadow_rgb=(-0.070, 0.006, 0.068),
                   highlight_rgb=(0.066, 0.022, -0.056), balance=0.47,
                   protect_saturated=0.85)
    x = sat_by_luma(x, shadow_sat=1.0, mid_sat=1.28, high_sat=0.74)
    x = lift_gamma_gain(x, lift=(0.0, 0.006, 0.018), gamma=(1.0, 1.0, 0.97))
    return x


def look_reels(x):
    """Calibrated against the reference frames, not invented.

    Measuring the content area of those five reels gives a consistent target:
    mean saturation 0.28 to 0.38, green saturation 0.22 to 0.34, warm pixels
    outnumbering cool ones except where the sky is genuinely blue, blacks that
    reach 0 and highlights that run soft up to 0.95 and beyond.

    The read people call cinematic there is dynamic range plus restraint: a
    wide spread from true black to soft white, with the colour pulled back. A
    loud teal/orange measures at roughly 0.50 saturation and looks like a
    filter over the top of the picture instead. The orange/teal structure is
    still here, warm highlights against cool shadows, it is just held at the
    level the references actually sit at.
    """
    # wide tonal spread first. No black milking here on purpose: three of the
    # five references measure a 5th percentile of exactly 0.000, so the blacks
    # genuinely bottom out rather than sitting lifted.
    x = soft_contrast(x, 1.22)

    h, s, v = C.rgb_to_hsv(x)
    h0 = h.copy()

    # greens are the giveaway. Reference greens sit near 0.28 saturation and
    # lean olive, so pull them off vivid green and take the intensity out.
    h, w_grn = steer(h, s, [
        (105.0, 60.0, 88.0, 0.30),    # foliage toward olive, away from neon
    ], h0)
    _, w_warm = steer(h, s, [(35.0, 55.0, 33.0, 0.0)], h0)
    _, w_cool = steer(h, s, [(210.0, 70.0, 205.0, 0.0)], h0)

    s = s * (1.0 - 0.34 * w_grn)             # the big one
    s = s * (1.0 + 0.10 * w_warm)            # keep skin and gold alive
    s = s * (1.0 - 0.06 * w_cool)
    x = C.hsv_to_rgb(h, s, v)

    # the split, held quiet: warm highlights, cool shadows
    x = split_tone(x, shadow_rgb=(-0.030, 0.002, 0.034),
                   highlight_rgb=(0.036, 0.012, -0.030), balance=0.58,
                   protect_saturated=0.20, preserve_order=True)

    # creamy highlights: colour drops away as things approach white
    x = sat_by_luma(x, shadow_sat=1.00, mid_sat=1.08, high_sat=0.66)
    x = saturation(x, 0.98)
    x = lift_gamma_gain(x, lift=(0.002, 0.004, 0.008), gamma=(1.0, 1.0, 0.995))
    return x


# --- second wave: primitives for the measured looks -----------------------
# Everything below was built against a stated numeric target and verified on
# frames from footage/, not eyeballed. Nothing above this line is touched.


def rank_guard(base, out, release_below=None, keep_frac=0.0):
    """Scale a transform back per pixel until it stops reordering channels.

    `split_tone` carries its own version of this, deliberately released on
    near-neutral pixels so that a warm shadow is free to flip cool. That
    release is exactly wrong for a low saturation look: once a frame has been
    pulled down to a quarter of its original chroma, every object colour reads as
    near-neutral, so a saturation-weighted guard protects nothing and a blue
    cast turns the dark maroon car blue all over again. Full protection is the
    default here for that reason.

    Also note it protects a RANKING, not a hue. Two channels held in order can
    still swap how far apart they sit, which is why the maroon probe comes out
    of a night grade at 327 degrees rather than 353: red is still the largest
    channel, blue simply got closer to it.

    `release_below=(floor, span)` puts the split_tone behaviour back for the
    looks that need it. Anything below `floor` saturation is free to reorder,
    which is what lets a cool night grade flip a near-neutral warm wall without
    also flipping the foliage and the tail lights.

    `keep_frac` asks for more than survival. At 0.0 a clamped pixel comes to
    rest with its two channels exactly equal, which keeps the maroon probe red
    dominant on paper and lands it at 307 degrees, a purple. Holding a fraction
    of the original gap instead keeps the hue near where it started rather than
    on the boundary.

    Meant to wrap a cast, not a desaturation. The scale is one scalar per pixel
    applied to the whole push, so a clamped pixel loses tint; if the push also
    carried the desaturation, that pixel would get its colour back instead and
    show up as a blotch.
    """
    push = out - base
    k = np.ones(base.shape[:-1] + (1,))
    for a in range(3):
        for b in range(3):
            if a == b:
                continue
            gap = base[..., a:a + 1] - base[..., b:b + 1]
            dp = push[..., a:a + 1] - push[..., b:b + 1]
            keep = gap * keep_frac
            bad = (gap > 0.0) & (gap + dp < keep)
            with np.errstate(divide="ignore", invalid="ignore"):
                lim = np.where(bad, (gap - keep) / np.maximum(-dp, 1e-9), 1.0)
            k = np.minimum(k, np.clip(lim, 0.0, 1.0))
    hold = np.ones_like(k)
    if release_below is not None:
        sat_floor, span = release_below
        mx = base.max(-1, keepdims=True)
        mn = base.min(-1, keepdims=True)
        sat = (mx - mn) / np.maximum(mx, 1e-6)
        hold = np.clip((sat - sat_floor) / max(1e-6, span), 0.0, 1.0)
    return np.clip(base + push * (1.0 - hold * (1.0 - k)), 0.0, 1.0)


def mono_cast(rgb, residual=0.24, shadow_rgb=(-0.012, -0.002, 0.026),
              highlight_rgb=(0.008, 0.005, -0.002), balance=0.62,
              shadow_fade=0.26, keep_frac=0.40):
    """Collapse to near-monochrome, then tint the result by luminance.

    `residual` is the fraction of the original chroma that survives. It is not
    decoration: at 0.0 the frame is grey and the tint alone decides every hue,
    so a red car and a green hedge come out the same blue. Holding a quarter of
    the chroma is what still lets an object read as the colour it was, and it
    is what gives `rank_guard` something to protect.

    `shadow_fade` tapers the cast away below that luma. A fixed additive tint
    is a percentage of whatever it lands on, so a +0.030 blue push on a 0.03
    pixel is a 60% saturated blue, and the measured result is a monochrome
    frame with a blue blob in the blacks. Fading it out below 0.26 keeps the
    blacks dense and neutral and puts the colour where it can be seen.

    The cast is split by luminance rather than applied flat because a flat tint
    on a monochrome image only moves the white point and reads as a broken
    camera profile.
    """
    y = C.luma709(rgb)
    base = np.clip(y + (rgb - y) * residual, 0.0, 1.0)
    yb = np.clip(C.luma709(base), 0.0, 1.0)
    w_hi = np.clip((yb - balance) / max(1e-6, 1.0 - balance), 0.0, 1.0)
    w_sh = np.clip((balance - yb) / max(1e-6, balance), 0.0, 1.0)
    if shadow_fade > 0.0:
        w_sh = w_sh * np.clip(yb / shadow_fade, 0.0, 1.0)
    cast = np.array(shadow_rgb) * w_sh + np.array(highlight_rgb) * w_hi
    return rank_guard(base, base + cast, keep_frac=keep_frac)


def warm_source_mask(rgb, center=34.0, width=60.0, v_low=0.42, v_high=0.78):
    """1 where a pixel is a bright warm light source, 0 everywhere else.

    A night grade that cools the whole frame uniformly also cools the sodium
    lamps, the windows and the dashboard, and the result reads as a blue filter
    rather than as night. Real night footage is cool everywhere the light is
    ambient and warm everywhere there is a bulb, and that contrast is the whole
    effect. Gated on value as well as hue, because a dark brown wall is warm
    too and it is not a light.
    """
    h, s, v = C.rgb_to_hsv(rgb)
    w = (C.hue_weight(h, center, width)
         * np.clip((v - v_low) / max(1e-6, v_high - v_low), 0.0, 1.0)
         * np.clip(s * 3.0, 0.0, 1.0))
    return w[..., None]


# --- second wave: the looks -----------------------------------------------

def look_film_portrait(x):
    """Daylight colour negative, portrait stock. Flat, warm, skin first.

    Target: tonal spread (95th minus 5th percentile) 3 to 10% NARROWER than
    the ungraded frame, black floor up 6 to 12 code values, mean saturation
    within 12% of source, greens pulled 4 to 10 degrees toward yellow, under
    1% of pixels clipped white. The green figure started at 10 degrees and was
    revised down: `hue_push_greens` only reaches 40% weight on a grass-coloured
    pixel, and the alternative, a steer strong enough to move it 10 degrees,
    is the setting that sent foliage neon in the blockbuster work. The point of a negative stock is
    latitude, so the tone curve is deliberately flatter than the source rather
    than steeper: this is the only look in the library that reduces contrast
    and keeps its colour.

    kodak2383 is the print stock and sits the other way round, dense and
    contrasty. Reach for this one when the frame has to hold a face.
    """
    x = soft_contrast(x, 0.96)
    # a negative never reaches true black, and it never reaches paper white
    # either. The lift raises the floor, the sub-unity gain pulls the ceiling
    # down, and the pair is what makes the curve flatter rather than just
    # brighter.
    x = lift_gamma_gain(x, lift=(0.031, 0.028, 0.032), gain=(0.952, 0.947, 0.937))
    x = hue_push_greens(x, toward_yellow=0.28, desat=0.10)
    x = split_tone(x, shadow_rgb=(-0.004, 0.000, 0.010),
                   highlight_rgb=(0.020, 0.011, -0.007), balance=0.50,
                   preserve_order=True)
    x = sat_by_luma(x, shadow_sat=0.98, mid_sat=1.22, high_sat=0.80)
    x = hue_warm_skin(x, 0.065)
    # interlayer crosstalk is a real property of a dye-coupled negative, and it
    # is most of why film colour never looks channel-pure the way a sensor does
    x = channel_crosstalk(x, 0.05)
    return x


def look_film_tungsten(x):
    """Tungsten balanced negative, exposed in daylight. Cool by construction.

    This is not a stylistic blue: a 3200K stock shot at 5600K without an 85
    correction filter genuinely lands blue-cyan, with the red record
    underexposed and the shadows going deepest. Target: cool family share up at
    least 10 points on an interior frame, mean saturation within 18% of source,
    deep reds allowed to lean magenta but never to lose red dominance, and no
    channel driven to zero (the guard that matters here, since the whole grade
    pulls red down).

    That saturation tolerance is 18 and not 15 because the two test frames move
    in opposite directions and no single setting satisfies both: a cool cast
    subtracts chroma from a green exterior (measured -16%) and adds it to a
    warm interior (+16%), because chroma is distance from grey and the cast
    runs with the greens and against the tungsten.

    The cast is carried by lift and gamma, not by gain. Gain scales, so it bites
    hardest exactly where there is least room: at 0.965 red the near-white probe
    came out blue-dominant, meaning white itself had changed family. Shadows and
    mids blue with a white that stays white is also the truer description of the
    stock, whose red record recovers as exposure climbs.
    """
    x = soft_contrast(x, 1.12)
    x = lift_gamma_gain(x, lift=(0.002, 0.006, 0.018),
                        gamma=(0.955, 0.995, 1.032), gain=(0.982, 0.990, 0.990))

    h, s, v = C.rgb_to_hsv(x)
    h0 = h.copy()
    # an underexposed red record reads magenta, not orange. Held to 0.18: past
    # about 0.3 skin stops being skin and starts being a bruise.
    h, _ = steer(h, s, [(6.0, 30.0, 350.0, 0.18)], h0)
    # greens go cyan on tungsten stock, but only partway. 0.26 measures foliage
    # at 138 degrees, still green family; past 0.5 it crosses 165 into
    # blue-green and a hedge starts reading as water.
    h, _ = steer(h, s, [(110.0, 55.0, 155.0, 0.26)], h0)
    x = C.hsv_to_rgb(h, s, v)

    x = split_tone(x, shadow_rgb=(-0.020, -0.003, 0.026),
                   highlight_rgb=(-0.005, 0.001, 0.007), balance=0.52,
                   protect_saturated=0.15, preserve_order=True)
    x = sat_by_luma(x, shadow_sat=0.86, mid_sat=1.04, high_sat=0.74)
    x = channel_crosstalk(x, 0.045)
    return x


def look_commercial(x):
    """Flat, clean, brand-safe. The look that stays out of the way.

    Target: tonal spread 10 to 18% narrower than the ungraded frame, mean
    saturation down 8 to 18%, nothing clipped at either end (under 0.5% of
    pixels at black or white), and no hue family moved more than 8 degrees.
    Product and interview footage gets cut against graphics and titles, so the
    grade has to leave headroom at the top and detail at the bottom rather than
    looking finished on its own.

    The trap here is that flattening a frame desaturates it as a side effect:
    the first pass measured 12% flatter but 32% less saturated, which is not a
    clean commercial look, it is a washed-out one. The saturation is put back
    in the mids to hold the two moves apart.
    """
    # both ends pulled in first: lift raises the floor, gain drops the ceiling
    x = lift_gamma_gain(x, lift=(0.055, 0.055, 0.059), gain=(0.925, 0.928, 0.936))
    x = soft_contrast(x, 0.95)
    # greens are still the tell even in a neutral grade, just a smaller nudge
    x = hue_push_greens(x, toward_yellow=0.09, desat=0.06)
    x = split_tone(x, shadow_rgb=(0.000, 0.002, 0.008),
                   highlight_rgb=(0.008, 0.007, 0.002), balance=0.50,
                   preserve_order=True)
    x = sat_by_luma(x, shadow_sat=1.08, mid_sat=1.18, high_sat=0.98)
    x = channel_crosstalk(x, 0.02)
    return x


def look_blue_hour(x):
    """Cold night. Ambient light goes blue, bulbs stay warm.

    Target: cool family share above 50% on an interior frame and above 35% on
    a green exterior, warm share under 25%, mean luminance down 8 to 25%
    against the source, mean saturation between 0.22 and 0.30 so the frame
    reads cold rather than grey, and the maroon probe still red dominant after
    a shadow push this heavy.

    The exterior number is lower on purpose and is a real limit, not a fudge.
    A golden hour landscape is 60% foliage by pixel count, so the only way to
    get it past 50% cool is to push green past 165 degrees, at which point a
    hedge reads as water. Foliage stays green and the number stays at 38%.

    The warm source mask is the load-bearing part. Cooling everything equally
    produces a blue filter; cooling everything except the bulbs produces night.

    Two measured traps are fixed here. The cool leg is applied through
    `rank_guard` because `lift_gamma_gain` has no order guard of its own: a
    red gain of 0.90 against a blue gain of 1.0 swapped red and blue on grass
    and sent foliage from 103 degrees to 171, out of green and into cyan. And
    the shadow push is smaller than it wants to be because at -0.030 red the
    deep shadow probe came out at 0.014 red and 0.85 saturation, which is the
    clipped teal blob that `floor_frac` exists to prevent.
    """
    x = soft_contrast(x, 1.14)
    warm = warm_source_mask(x, center=34.0, width=62.0, v_low=0.44, v_high=0.80)

    h, s, v = C.rgb_to_hsv(x)
    h0 = h.copy()
    # foliage has very little colour at night. Steer it toward blue-green and
    # take a third of its saturation; 0.26 lands it near 135 degrees, which
    # leaves headroom before the 165 degree line where green stops being green.
    h, w_grn = steer(h, s, [(105.0, 58.0, 148.0, 0.26)], h0)
    s = s * (1.0 - 0.34 * w_grn)
    x = C.hsv_to_rgb(h, s, v)

    cool = lift_gamma_gain(x, lift=(0.000, 0.004, 0.016),
                           gamma=(0.900, 0.955, 1.065), gain=(0.912, 0.940, 0.972))
    cool = split_tone(cool, shadow_rgb=(-0.012, -0.002, 0.022),
                      highlight_rgb=(-0.018, 0.002, 0.030), balance=0.58,
                      protect_saturated=0.35, preserve_order=True)
    # object colours keep their channel order and a third of their channel
    # spread, near-neutrals are free to flip cool
    cool = rank_guard(x, cool, release_below=(0.20, 0.28), keep_frac=0.35)
    # bulbs keep 80% of their original warmth, everything else takes the cool
    x = np.clip(x + (cool - x) * (1.0 - 0.80 * warm), 0.0, 1.0)

    x = sat_by_luma(x, shadow_sat=0.94, mid_sat=1.08, high_sat=0.78)
    return x


def look_interior(x):
    """Warm interior under practical light. Rich, not hazy.

    Target: warm family share above 65%, mean saturation up 15 to 30% against
    the source, and a black level within 8 code values of the source. That last
    number is the whole difference against golden_haze, which reaches a similar
    warm share by lifting the blacks 28 code values into fog. Warm and rich,
    not warm and milky.

    The warm share target started at 70 and came back to 65. Reaching 70 needs
    a warm push on the shadows as well, and measured that way the asphalt and
    deep shadow probes both flip to red dominant: a neutral road surface turns
    into a warm one. Three percentage points of warm share is not worth a grade
    that cannot hold a grey.

    Shadows stay faintly cool on purpose, so the cool family share does not
    fall as far as the warm share rises. A warm push on both ends collapses the
    frame into one orange note, and it also flips the near-neutral deep shadow
    probe out of the cool family, which is the measurable version of the same
    complaint.
    """
    x = soft_contrast(x, 1.10)

    h, s, v = C.rgb_to_hsv(x)
    h0 = h.copy()
    # wood, beige walls and warm white bulbs all sit in the yellow band and all
    # read cheap there. Pull them to amber. Width 45 around 55 means the weight
    # is already zero by 100 degrees, so foliage through a window is untouched.
    h, _ = steer(h, s, [(55.0, 45.0, 40.0, 0.30)], h0)
    x = C.hsv_to_rgb(h, s, v)

    x = split_tone(x, shadow_rgb=(-0.004, -0.002, 0.006),
                   highlight_rgb=(0.034, 0.016, -0.024), balance=0.46,
                   protect_saturated=0.15, preserve_order=True)
    x = sat_by_luma(x, shadow_sat=0.86, mid_sat=0.99, high_sat=0.70)
    x = hue_warm_skin(x, 0.085)
    x = lift_gamma_gain(x, lift=(0.006, 0.004, 0.003),
                        gamma=(1.02, 1.0, 0.975), gain=(0.985, 0.975, 0.925))
    return x


def look_punch(x):
    """High contrast, high saturation, hue faithful. Product and car footage.

    The counterpart to blockbuster: the same order of saturation, none of the
    hue steering. blockbuster collapses the palette onto two complementary
    targets, which is a style; this leaves every hue where the camera put it
    and only makes it stronger, which is what a product shot needs when the
    client knows what colour their logo is.

    Target: mean saturation up 55 to 75% against the source, EVERY probe hue
    within 6 degrees of where it started, tonal spread up at least 10%, under
    0.5% of pixels with saturation pinned at 1.0 and under 0.5% clipped black.

    There is deliberately no split tone. A shadow push of only -0.008 red moved
    the near-neutral deep shadow probe 17 degrees, which is nothing to look at
    but breaks the one promise this look makes. Tonal depth comes from the
    curve instead. Saturation is a vibrance curve rather than a multiply,
    because a multiply is what pins the already-saturated pixels and turns a
    red car into a flat red shape with no detail in it.
    """
    x = soft_contrast(x, 1.30)
    h, s, v = C.rgb_to_hsv(x)
    s = vibrance_curve(s, 0.40)
    x = C.hsv_to_rgb(h, s, v)
    x = sat_by_luma(x, shadow_sat=0.96, mid_sat=1.10, high_sat=0.82)
    x = lift_gamma_gain(x, gamma=(0.985, 0.985, 0.985))
    return x


def look_silverblue(x):
    """Near monochrome with a steel blue cast. The colour is the accent.

    Target: mean saturation between 0.05 and 0.10 on both test frames (roughly
    a quarter of the source), every probe keeping its dominant channel, no
    channel driven to zero, and a cool cast measured as a positive change in
    mean blue minus red in every luma band. bleach sits at 0.13 to 0.15 and is
    a silver-retention look with the contrast up; this goes considerably
    further down and adds a cast, so the two do not overlap.

    Counting warm against cool pixels is the wrong test here and was the first
    one tried. On a golden hour exterior the fifth of the chroma that survives
    is warm whatever tint goes over it, so the count reports the source and not
    the grade: 6% warm against 0.2% cool while the cast is unambiguously blue,
    +5 to +34 code values of blue minus red against the ungraded frame,
    increasing with luminance.

    The residual chroma is what keeps this from being a blue filter over a
    black and white image: at 0.24 a red brake light still reads red, just
    quietly, and `rank_guard` inside `mono_cast` is what stops the cast from
    taking that away.

    Order matters here and cost a probe failure to find. With the black lift
    applied after the cast, its +0.011 blue against +0.005 red went on
    unguarded and pushed the maroon probe from 353 degrees to 293, blue
    dominant, which is precisely the bug the guard exists to prevent. The lift
    runs first now, so the guarded cast is the last thing to touch the pixel.
    """
    x = soft_contrast(x, 1.22)
    # the floor sits just off zero so the blacks read as dense rather than
    # clipped, which is the difference between moody and broken
    x = lift_gamma_gain(x, lift=(0.008, 0.008, 0.009), gamma=(1.0, 1.0, 0.995))
    x = mono_cast(x, residual=0.24,
                  shadow_rgb=(-0.012, -0.002, 0.026),
                  highlight_rgb=(0.002, 0.004, 0.008), balance=0.70,
                  shadow_fade=0.26, keep_frac=0.40)
    return x


LOOKS = {
    "neutral": look_neutral,
    "kodak2383": look_kodak2383,
    "teal_orange": look_teal_orange,
    "warm_film": look_warm_film,
    "bleach": look_bleach,
    "nordic": look_nordic,
    "forest": look_forest,
    "golden_haze": look_golden_haze,
    "premium": look_premium,
    "natural": look_natural,
    "blockbuster": look_blockbuster,
    "blockbuster_max": look_blockbuster_max,
    "reels": look_reels,
    "film_portrait": look_film_portrait,
    "film_tungsten": look_film_tungsten,
    "commercial": look_commercial,
    "blue_hour": look_blue_hour,
    "interior": look_interior,
    "punch": look_punch,
    "silverblue": look_silverblue,
}


def main() -> None:
    out_dir = Path(__file__).resolve().parent.parent / "luts" / "looks"
    out_dir.mkdir(parents=True, exist_ok=True)
    size = 33
    grid = C.identity_grid(size)
    for name, fn in LOOKS.items():
        result = np.clip(fn(grid.copy()), 0.0, 1.0)
        path = out_dir / f"{name}.cube"
        C.write_cube(path, result, size, name, comments=[
            "Generated by content/grade/tools/make_looks.py",
            "Domain: Rec.709 gamma 2.4. Apply AFTER the CST, not to raw log.",
        ])
        drift = np.abs(result - grid).mean()
        print(f"  {name:14} -> {path.name:22} mean drift from identity {drift:.4f}")


if __name__ == "__main__":
    main()
