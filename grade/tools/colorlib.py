"""Shared color math for the headless grading kit.

Everything here operates on float RGB arrays of shape (..., 3).

Two distinct domains are used and must not be confused:
  scene linear  - radiometric, 0.18 is an 18% grey card, highlights go well above 1.0
  display code  - what a Rec.709 display eats, nominally [0, 1]
"""

from __future__ import annotations

import numpy as np
import colour
from colour.models import (
    log_decoding_AppleLogProfile,
    oetf_DaVinciIntermediate,
    oetf_inverse_DaVinciIntermediate,
)

# Apple Log is defined on BT.2020 primaries with a D65 white point.
APPLE_LOG_GAMUT = colour.RGB_COLOURSPACES["ITU-R BT.2020"]
REC709 = colour.RGB_COLOURSPACES["ITU-R BT.709"]
ACESCG = colour.RGB_COLOURSPACES["ACEScg"]
# DaVinci Wide Gamut: the intermediate working space. Its log encoding holds
# 100.0 scene linear at code 1.0, against Apple Log's 12.0, so primaries applied
# here have roughly three more stops of highlight headroom before they clip.
DWG = colour.RGB_COLOURSPACES["DaVinci Wide Gamut"]

BT2020_TO_REC709 = colour.matrix_RGB_to_RGB(APPLE_LOG_GAMUT, REC709, "CAT02")
BT2020_TO_AP1 = colour.matrix_RGB_to_RGB(APPLE_LOG_GAMUT, ACESCG, "CAT02")
BT2020_TO_DWG = colour.matrix_RGB_to_RGB(APPLE_LOG_GAMUT, DWG, "CAT02")
DWG_TO_REC709 = colour.matrix_RGB_to_RGB(DWG, REC709, "CAT02")
DWG_TO_AP1 = colour.matrix_RGB_to_RGB(DWG, ACESCG, "CAT02")
AP1_TO_REC709 = colour.matrix_RGB_to_RGB(ACESCG, REC709, "CAT02")

MID_GREY_SCENE = 0.18


def apply_matrix(rgb: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    return np.einsum("ij,...j->...i", matrix, rgb)


def apple_log_to_scene_linear(code: np.ndarray) -> np.ndarray:
    """Apple Log code values -> scene linear, still on BT.2020 primaries.

    Apple Log maps code 0.0 to -0.056 linear, so the decode is clamped at zero.
    Leaving the negatives in would poison the gamut matrix that follows.
    """
    return np.clip(log_decoding_AppleLogProfile(code), 0.0, None)


# --- tone mapping: scene linear -> display linear -------------------------

def tonemap_aces(x: np.ndarray) -> np.ndarray:
    """Stephen Hill's fit of the ACES RRT + sRGB ODT. Input/output in AP1.

    Lands an 18% grey card near 0.10 display linear, which is ~0.39 after a
    2.4 gamma encode. That is the Rec.709 mid grey broadcast expects.
    """
    a = x * (x + 0.0245786) - 0.000090537
    b = x * (0.983729 * x + 0.432951) + 0.238081
    return a / b


def tonemap_filmic(x: np.ndarray, white: float = 12.0) -> np.ndarray:
    """Gentler shoulder than ACES: less contrast, keeps more midtone latitude.

    A Reinhard-with-white-point core plus a mild toe. Closer to what DaVinci's
    own 'Apple Log -> Rec.709 with tone mapping' CST produces.
    """
    x = np.clip(x, 0.0, None)
    mapped = x * (1.0 + x / (white * white)) / (1.0 + x)
    toe = 0.002
    return np.clip((mapped - toe) / (1.0 - toe), 0.0, None)


def tonemap_none(x: np.ndarray) -> np.ndarray:
    """No shoulder at all. Anything over diffuse white clips hard."""
    return np.clip(x, 0.0, 1.0)


# --- display encoding -----------------------------------------------------

def encode_gamma24(x: np.ndarray) -> np.ndarray:
    return np.clip(x, 0.0, 1.0) ** (1.0 / 2.4)


def decode_gamma24(x: np.ndarray) -> np.ndarray:
    return np.clip(x, 0.0, 1.0) ** 2.4


def encode_rec709_oetf(x: np.ndarray) -> np.ndarray:
    """The actual BT.709 OETF (camera-side), not the 2.4 display gamma."""
    x = np.clip(x, 0.0, 1.0)
    return np.where(x < 0.018, 4.5 * x, 1.099 * x ** 0.45 - 0.099)


def encode_gamma22(x: np.ndarray) -> np.ndarray:
    """The practical stand-in for DaVinci's Rec.709-A.

    Resolve offers Rec.709-A for Mac display pipelines and Rec.709 Gamma 2.4 for
    calibrated external monitors. Grading against the wrong one is why a grade
    can look correct in the app and washed out in QuickTime.
    """
    return np.clip(x, 0.0, 1.0) ** (1.0 / 2.2)


def dwg_encode(x: np.ndarray) -> np.ndarray:
    return oetf_DaVinciIntermediate(x)


def dwg_decode(x: np.ndarray) -> np.ndarray:
    return oetf_inverse_DaVinciIntermediate(x)


def encode_srgb(x: np.ndarray) -> np.ndarray:
    x = np.clip(x, 0.0, 1.0)
    return np.where(x <= 0.0031308, x * 12.92, 1.055 * x ** (1 / 2.4) - 0.055)


TONEMAPS = {"aces": tonemap_aces, "filmic": tonemap_filmic, "none": tonemap_none}
ENCODERS = {
    "gamma24": encode_gamma24,
    "rec709a": encode_gamma22,
    "rec709": encode_rec709_oetf,
    "srgb": encode_srgb,
}


def luma709(rgb: np.ndarray) -> np.ndarray:
    return (
        0.2126 * rgb[..., 0] + 0.7152 * rgb[..., 1] + 0.0722 * rgb[..., 2]
    )[..., None]


# --- .cube writer ---------------------------------------------------------

def identity_grid(size: int) -> np.ndarray:
    """(size**3, 3) grid in .cube ordering: red index varies fastest."""
    axis = np.linspace(0.0, 1.0, size)
    b, g, r = np.meshgrid(axis, axis, axis, indexing="ij")
    return np.stack([r, g, b], axis=-1).reshape(-1, 3)


def write_cube(path, rgb: np.ndarray, size: int, title: str, comments=()) -> None:
    rgb = np.asarray(rgb).reshape(-1, 3)
    if rgb.shape[0] != size ** 3:
        raise ValueError(f"expected {size ** 3} entries, got {rgb.shape[0]}")
    with open(path, "w") as fh:
        for line in comments:
            fh.write(f"# {line}\n")
        fh.write(f'TITLE "{title}"\n')
        fh.write(f"LUT_3D_SIZE {size}\n")
        fh.write("DOMAIN_MIN 0.0 0.0 0.0\nDOMAIN_MAX 1.0 1.0 1.0\n")
        for r, g, b in rgb:
            fh.write(f"{r:.6f} {g:.6f} {b:.6f}\n")


# --- HSV, for hue-selective work ------------------------------------------
# A real teal/orange grade steers specific hue families toward complementary
# targets. That needs actual hue angles, not the channel arithmetic that a
# simple tint uses.

def rgb_to_hsv(rgb: np.ndarray):
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    mx = rgb.max(-1)
    mn = rgb.min(-1)
    d = mx - mn
    h = np.zeros_like(mx)
    nz = d > 1e-9
    i = nz & (mx == r)
    h[i] = (((g - b)[i] / d[i]) % 6.0)
    i = nz & (mx == g) & (mx != r)
    h[i] = ((b - r)[i] / d[i]) + 2.0
    i = nz & (mx == b) & (mx != r) & (mx != g)
    h[i] = ((r - g)[i] / d[i]) + 4.0
    h = (h * 60.0) % 360.0
    s = np.where(mx > 1e-9, d / np.maximum(mx, 1e-9), 0.0)
    return h, s, mx


def hsv_to_rgb(h: np.ndarray, s: np.ndarray, v: np.ndarray) -> np.ndarray:
    h = np.mod(h, 360.0)
    c = v * s
    x = c * (1.0 - np.abs(np.mod(h / 60.0, 2.0) - 1.0))
    m = v - c
    z = np.zeros_like(h)
    seg = (h / 60.0).astype(int) % 6
    r = np.select([seg == 0, seg == 1, seg == 2, seg == 3, seg == 4, seg == 5],
                  [c, x, z, z, x, c])
    g = np.select([seg == 0, seg == 1, seg == 2, seg == 3, seg == 4, seg == 5],
                  [x, c, c, x, z, z])
    b = np.select([seg == 0, seg == 1, seg == 2, seg == 3, seg == 4, seg == 5],
                  [z, z, x, c, c, x])
    return np.clip(np.stack([r + m, g + m, b + m], axis=-1), 0.0, 1.0)


def hue_weight(h: np.ndarray, center: float, width: float) -> np.ndarray:
    """Triangular falloff around a hue angle, wrapping correctly at 0/360."""
    d = np.abs(((h - center + 180.0) % 360.0) - 180.0)
    return np.clip(1.0 - d / max(1e-6, width), 0.0, 1.0) ** 0.75
