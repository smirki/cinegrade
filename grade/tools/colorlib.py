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


# --- the other input transfers: HLG, PQ and Rec.709 -----------------------
#
# Every one of these ends where apple_log_to_scene_linear ends: scene linear on
# BT.2020 primaries, 0.18 for an 18% grey card. That is the whole point. Once a
# source is there, the working space chain, the tone map and the encode that
# follow do not know or care what the file was shot in.
#
# The shared anchor is ITU-R BT.2408 (Operational Practices in HDR Television).
# It publishes, for both HLG and PQ, a Reference Level of 26 cd/m2 (the HDR
# stand-in for an 18% grey card) and a Reference White / graphics white of
# 203 cd/m2. Pinning 26 cd/m2 to 0.18 scene linear is what makes an HLG mid
# grey land where an Apple Log mid grey lands, and it puts 203 cd/m2 reference
# white at 1.405 scene linear, well inside the tone map's shoulder.
#
# Anchoring on reference white instead was considered and rejected: the HLG
# OOTF's 1.2 system gamma stretches the grey to white ratio from the scene
# referred 5.0 to 7.7, so pinning white would leave mid grey 0.63 stops dark.
# The mid tone is what the eye judges, so the mid tone is the anchor.
HDR_REF_GREY_NITS = 26.0            # BT.2408 Reference Level
HDR_REF_WHITE_NITS = 203.0          # BT.2408 Reference White (graphics white)
SCENE_PER_NIT = MID_GREY_SCENE / HDR_REF_GREY_NITS

# ITU-R BT.2100 Table 5, the HLG reference OETF constants.
HLG_A = 0.17883277
HLG_B = 0.28466892                  # 1 - 4a
HLG_C = 0.55991073                  # 0.5 - a * ln(4a)
# BT.2100 Note 5f: the OOTF's system gamma for a nominal 1000 cd/m2 display.
HLG_SYSTEM_GAMMA = 1.2
HLG_PEAK_NITS = 1000.0
# BT.2100 Table 5, the OOTF's luminance coefficients (BT.2020 primaries).
HLG_LUMA = (0.2627, 0.6780, 0.0593)

# SMPTE ST 2084 / BT.2100 Table 4, the PQ constants.
PQ_M1 = 2610.0 / 16384.0
PQ_M2 = 2523.0 / 4096.0 * 128.0
PQ_C1 = 3424.0 / 4096.0
PQ_C2 = 2413.0 / 4096.0 * 32.0
PQ_C3 = 2392.0 / 4096.0 * 32.0
PQ_PEAK_NITS = 10000.0

# BT.1886 is a pure 2.4 power law for a display with no black lift, which is
# the transfer a Rec.709 delivery file is mastered against.
BT1886_GAMMA = 2.4


def hlg_inverse_oetf(code: np.ndarray) -> np.ndarray:
    """HLG signal -> normalised HLG scene light, ITU-R BT.2100 Table 5.

    The forward OETF is E' = sqrt(3E) below 1/12 and a*ln(12E - b) + c above
    it, so this is that piecewise function read backwards. The output is the
    HLG scene light E in [0, 1], NOT display light and not yet scene linear in
    this pipeline's sense: hlg_to_scene_linear does the rest.
    """
    e = np.clip(np.asarray(code, dtype=np.float64), 0.0, 1.0)
    low = e * e / 3.0
    # 12L - b is only evaluated where the high branch is taken, but numpy
    # computes both arms of a where(), so the log's argument is floored.
    high = (np.exp((e - HLG_C) / HLG_A) + HLG_B) / 12.0
    return np.where(e <= 0.5, low, high)


def hlg_oetf(scene: np.ndarray) -> np.ndarray:
    """The inverse of hlg_inverse_oetf, same standard, same constants."""
    l = np.clip(np.asarray(scene, dtype=np.float64), 0.0, 1.0)
    low = np.sqrt(3.0 * l)
    high = HLG_A * np.log(np.maximum(12.0 * l - HLG_B, 1e-12)) + HLG_C
    return np.where(l <= 1.0 / 12.0, low, high)


def hlg_ootf(scene: np.ndarray, peak: float = HLG_PEAK_NITS) -> np.ndarray:
    """HLG scene light -> display light in cd/m2, ITU-R BT.2100 Table 5.

    Ld = alpha * Ys ** (gamma - 1) * Es per channel, with Ys the BT.2020
    luminance of the three scene channels and alpha the display peak. On a
    neutral this is simply peak * E ** 1.2, which is the form the exposure
    LUT in cinegrade.f_log_stage inverts.

    Cross check against BT.2408 rather than against itself: 75% signal
    (reference white) comes out at 203.2 cd/m2 and 38% signal (reference
    level) at 26.2 cd/m2, the two numbers that table publishes.
    """
    s = np.asarray(scene, dtype=np.float64)
    y = (HLG_LUMA[0] * s[..., 0] + HLG_LUMA[1] * s[..., 1]
         + HLG_LUMA[2] * s[..., 2])[..., None]
    return peak * np.clip(y, 0.0, None) ** (HLG_SYSTEM_GAMMA - 1.0) * s


def hlg_to_scene_linear(code: np.ndarray) -> np.ndarray:
    """HLG signal -> this pipeline's scene linear, on the file's own primaries.

    Inverse OETF, then the OOTF for a nominal 1000 cd/m2 display, then the one
    BT.2408 anchor (26 cd/m2 is 0.18). Primaries are handled separately by the
    caller, because an HLG file is normally already on BT.2020 primaries and
    then there is nothing to do.
    """
    return np.clip(hlg_ootf(hlg_inverse_oetf(code)) * SCENE_PER_NIT, 0.0, None)


def pq_eotf(code: np.ndarray) -> np.ndarray:
    """PQ signal -> absolute display light in cd/m2, SMPTE ST 2084."""
    e = np.clip(np.asarray(code, dtype=np.float64), 0.0, 1.0)
    p = e ** (1.0 / PQ_M2)
    num = np.maximum(p - PQ_C1, 0.0)
    den = np.maximum(PQ_C2 - PQ_C3 * p, 1e-12)
    return PQ_PEAK_NITS * (num / den) ** (1.0 / PQ_M1)


def pq_inverse_eotf(nits: np.ndarray) -> np.ndarray:
    """Absolute cd/m2 -> PQ signal, the inverse of pq_eotf."""
    y = np.clip(np.asarray(nits, dtype=np.float64) / PQ_PEAK_NITS, 0.0, 1.0)
    p = y ** PQ_M1
    return ((PQ_C1 + PQ_C2 * p) / (1.0 + PQ_C3 * p)) ** PQ_M2


def pq_to_scene_linear(code: np.ndarray) -> np.ndarray:
    """PQ signal -> this pipeline's scene linear, on the file's own primaries.

    ST 2084 gives absolute cd/m2 directly, so the only choice here is the same
    BT.2408 anchor HLG uses: 26 cd/m2 is 0.18 scene linear, which puts 203
    cd/m2 reference white at 1.405.
    """
    return np.clip(pq_eotf(code) * SCENE_PER_NIT, 0.0, None)


# A BT.709 OETF encoded 18% grey card sits at code 0.40901, and BT.1886 takes
# that back to 0.11699 display linear, 0.62 stops below the 0.18 the Apple Log
# path reaches. Treating display linear as scene linear with no scaling would
# therefore break the one rule this stage exists for, so one constant lines the
# two up. It is derived, not tuned: 0.18 divided by BT.1886 of BT.709's own
# encoding of 0.18.
REC709_SCENE_SCALE = MID_GREY_SCENE / (
    (1.099 * MID_GREY_SCENE ** 0.45 - 0.099) ** BT1886_GAMMA)


def rec709_to_scene_linear(code: np.ndarray) -> np.ndarray:
    """Rec.709 delivery code -> this pipeline's scene linear.

    BT.1886 inverse (a 2.4 power law) to display linear, then the one scale
    above. Primaries are the caller's job: a normal Rec.709 file is on BT.709
    primaries and needs a matrix into BT.2020, and a file that carries a
    bt709 transfer on BT.2020 primaries needs none.

    A file that really is display referred is still better graded with
    working_space "rec709", which skips both conversions entirely. This path
    exists so a Rec.709 source can join everything else in the log working
    space, and the tone map downstream will render it a second time.
    """
    lin = np.clip(np.asarray(code, dtype=np.float64), 0.0, 1.0) ** BT1886_GAMMA
    return lin * REC709_SCENE_SCALE


# --- the camera logs: S-Log3, LogC3, V-Log, Canon Log 3, D-Log -------------
#
# Five vendor curves, one shape. Every one of them is published as a log
# segment with a linear toe under it:
#
#     y = C * log10(A * x + B) + D      for x >= CUT_X
#     y = E * x + F                     for x <  CUT_X
#
# x is scene linear REFLECTANCE (0.18 is an 18% grey card, which is exactly
# the domain apple_log_to_scene_linear reaches) and y is that curve's own
# normalised code value. So the decode below is one function for all five, and
# the only thing that distinguishes them is six constants and a cut.
#
# Writing them in one shape is not a simplification of the standards: it is
# what the standards already are, rearranged. Where a document states the log
# segment in a different but equivalent algebra the rearrangement is shown in
# the comment next to the constants, so the numbers can be checked against the
# page they came from.
#
# CUT_Y is the same cut expressed in code values. It is given explicitly, not
# derived, because two of these curves are very slightly discontinuous at the
# join in the published constants (D-Log's toe reaches 0.139995 where the
# document's decode threshold is 0.14) and the document's own decode threshold
# is what a decoder is supposed to use.
#
# What these numbers are NOT: an opinion about the container. y is the signal
# after the engine has normalised the file to full scale using the file's own
# color_range tag, exactly as HLG, PQ and Rec.709 are handled above. A camera
# log file whose range tag disagrees with its data decodes wrong, and that is
# a container problem to fix on the file, not a curve to bend here.


class CameraLog:
    """One vendor log curve, its gamut and the document both came from."""

    def __init__(self, name, doc, gamut, A, B, C, D, E, F, cut_x, cut_y):
        self.name = name
        self.doc = doc
        self.gamut = gamut
        self.A, self.B, self.C, self.D = A, B, C, D
        self.E, self.F = E, F
        self.cut_x, self.cut_y = cut_x, cut_y

    def to_scene_linear(self, code: np.ndarray) -> np.ndarray:
        """Code values -> scene linear reflectance, clamped at zero.

        Every one of these curves encodes some negative reflectance below its
        toe (S-Log3 puts zero at code 95, LogC3 at 0.0928, V-Log at 0.125),
        which is real headroom for noise in the camera and meaningless light
        downstream, so it is clamped exactly the way Apple Log's is.
        """
        y = np.asarray(code, dtype=np.float64)
        low = (y - self.F) / self.E
        # Both arms of a where() are evaluated, so the log's argument is
        # floored even though the high arm is only taken above cut_y.
        high = (10.0 ** ((y - self.D) / self.C) - self.B) / self.A
        return np.clip(np.where(y < self.cut_y, low, high), 0.0, None)

    def from_scene_linear(self, scene: np.ndarray) -> np.ndarray:
        """The published forward curve: scene linear -> code values.

        Here rather than only in the suite because the anchor printer, the
        tests and any caller that wants to write a synthetic patch in a
        camera's own code values all need the same forward curve, and two
        copies of a vendor formula is one copy too many.
        """
        x = np.clip(np.asarray(scene, dtype=np.float64), 0.0, None)
        low = self.E * x + self.F
        high = self.C * np.log10(
            np.maximum(self.A * x + self.B, 1e-12)) + self.D
        return np.where(x < self.cut_x, low, high)

    def grey_code(self) -> float:
        """Where this curve puts an 18% grey card, in its own code values."""
        return float(self.from_scene_linear(np.array([MID_GREY_SCENE]))[0])


CAMERA_LOGS = {
    # Sony, "Technical Summary for S-Gamut3.Cine/S-Log3 and S-Gamut3/S-Log3".
    # The document writes the log segment as
    #     y = (420 + log10((x + 0.01) / (0.18 + 0.01)) * 261.5) / 1023
    # in 10 bit code values, and the toe as
    #     y = (x * (171.2102946929 - 95) / 0.01125 + 95) / 1023.
    # Dividing through by 1023 and folding the 1/0.19 inside the log gives
    # A = 1/0.19, B = 0.01/0.19, C = 261.5/1023, D = 420/1023, which is why
    # 18% grey is code 420 exactly: log10(1) is 0 and D is left standing.
    "slog3": CameraLog(
        "slog3", "Sony S-Log3 technical summary", "sgamut3cine",
        A=1.0 / 0.19, B=0.01 / 0.19, C=261.5 / 1023.0, D=420.0 / 1023.0,
        E=(171.2102946929 - 95.0) / 0.01125 / 1023.0, F=95.0 / 1023.0,
        cut_x=0.01125, cut_y=171.2102946929 / 1023.0),

    # ARRI, "ALEXA Log C Curve: Usage in VFX", the EI 800 row of its table of
    # per exposure index constants. EI 800 is the sensor's native sensitivity
    # and the one every ARRI LogC3 delivery LUT is built for; the other rows
    # are a different curve and would need their own input name, so this is
    # logc3 at EI 800 and says so.
    #     cut 0.010591, a 5.555556, b 0.052272, c 0.247190,
    #     d 0.385537,   e 5.367655, f 0.092809
    # y = c * log10(a * x + b) + d above the cut, e * x + f below it, which is
    # already this shape. 18% grey: a * 0.18 is 1.0, so y = c * log10(1.052272)
    # + d = 0.391007, the 10 bit code 400 ARRI's documentation quotes.
    "logc3": CameraLog(
        "logc3", "ARRI Log C curve usage document (EI 800)", "awg3",
        A=5.555556, B=0.052272, C=0.247190, D=0.385537,
        E=5.367655, F=0.092809, cut_x=0.010591,
        cut_y=5.367655 * 0.010591 + 0.092809),

    # Panasonic, "V-Log/V-Gamut Reference Manual".
    #     cut1 0.01, cut2 0.181, b 0.00873, c 0.241514, d 0.598206
    # y = c * log10(x + b) + d above cut1, 5.6 * x + 0.125 below it. A is 1
    # because the document adds b to x directly rather than scaling it.
    "vlog": CameraLog(
        "vlog", "Panasonic V-Log/V-Gamut reference manual", "vgamut",
        A=1.0, B=0.00873, C=0.241514, D=0.598206,
        E=5.6, F=0.125, cut_x=0.01, cut_y=0.181),

    # Canon, "White Paper on Canon Log Gamma Curves", the Canon Log 3 section.
    # The document states the curve against IRE, where its input is scene
    # reflectance divided by 0.9 (90% white is the reference), so
    #     y = 0.42889912 * log10(14.98325 * (x / 0.9) + 1) + 0.069886632
    # and the 0.9 is folded into A: 14.98325 / 0.9 = 16.648056. The middle
    # segment 2.3069815 * (x / 0.9) + 0.073059361 becomes E the same way, and
    # its cut moves from 0.014 IRE-referred to 0.0126 reflectance.
    #
    # Canon Log 3 has a THIRD segment below x / 0.9 = -0.014, a mirrored log
    # for sub-black. It is not carried here: every code it covers decodes to
    # negative reflectance, which this pipeline clamps to zero, and the linear
    # segment extended down through the same codes clamps to zero too. The
    # suite measures that the two agree wherever the answer is not zero.
    "clog3": CameraLog(
        "clog3", "Canon Log gamma curves white paper (Canon Log 3)",
        "cinemagamut",
        A=14.98325 / 0.9, B=1.0, C=0.42889912, D=0.069886632,
        E=2.3069815 / 0.9, F=0.073059361,
        cut_x=0.014 * 0.9, cut_y=0.105357102),

    # DJI, "D-Log and D-Gamut white paper".
    #     y = log10(x * 0.9892 + 0.0108) * 0.256663 + 0.584555   for x > 0.0078
    #     y = 6.025 * x + 0.0929                                 otherwise
    # with the published decode threshold at y = 0.14. The toe actually
    # reaches 0.139995 at the cut, so cut_y is 0.14 as documented rather than
    # the derived value: a 5e-6 seam either way, and the document's number is
    # the one a decoder is told to use.
    "dlog": CameraLog(
        "dlog", "DJI D-Log white paper", "dgamut",
        A=0.9892, B=0.0108, C=0.256663, D=0.584555,
        E=6.025, F=0.0929, cut_x=0.0078, cut_y=0.14),
}


# The gamut each camera log is defined on, as the xy chromaticities its own
# document publishes, with D65 white throughout. They are built into real
# colourspaces here rather than looked up by name so this file states the
# numbers it uses; the suite asserts each one equals colour-science's own
# independently sourced definition of the same gamut, which is the check that
# a digit was not transposed.
#
#   S-Gamut3.Cine   Sony S-Log3 technical summary
#   ARRI Wide Gamut 3 (AWG3)   ARRI Log C curve usage document
#   V-Gamut         Panasonic V-Log/V-Gamut reference manual
#   Cinema Gamut    Canon Log gamma curves white paper
#   D-Gamut         DJI D-Log white paper
D65 = np.array([0.3127, 0.3290])

CAMERA_GAMUT_PRIMARIES = {
    "sgamut3cine": np.array([[0.766, 0.275], [0.225, 0.800], [0.089, -0.087]]),
    "awg3": np.array([[0.6840, 0.3130], [0.2210, 0.8480], [0.0861, -0.1020]]),
    "vgamut": np.array([[0.730, 0.280], [0.165, 0.840], [0.100, -0.030]]),
    "cinemagamut": np.array([[0.7400, 0.2700], [0.1700, 1.1400],
                             [0.0800, -0.1000]]),
    "dgamut": np.array([[0.7100, 0.3100], [0.2100, 0.8800], [0.0900, -0.0800]]),
}

# The name colour-science registers each of these under, kept next to the
# numbers so the cross check in the suite has one place to read.
CAMERA_GAMUT_REFERENCE = {
    "sgamut3cine": "S-Gamut3.Cine", "awg3": "ARRI Wide Gamut 3",
    "vgamut": "V-Gamut", "cinemagamut": "Cinema Gamut",
    "dgamut": "DJI D-Gamut",
}

CAMERA_GAMUTS = {
    name: colour.RGB_Colourspace(
        CAMERA_GAMUT_REFERENCE[name], primaries, D65, "D65")
    for name, primaries in CAMERA_GAMUT_PRIMARIES.items()
}


# The input transfers, by the name convert.input uses. apple_log is here so
# every caller can look one up by name instead of special casing it.
INPUT_DECODERS = {
    "apple_log": apple_log_to_scene_linear,
    "hlg": hlg_to_scene_linear,
    "pq": pq_to_scene_linear,
    "rec709": rec709_to_scene_linear,
    **{name: log.to_scene_linear for name, log in CAMERA_LOGS.items()},
}

# The primaries an input's own standard puts it on, so a file that carries
# something else can be spotted and matrixed. A camera log's entry is its
# camera gamut, and no container tag can name one of those (there is no code
# point for S-Gamut3.Cine or ARRI Wide Gamut 3), which is why the engine reads
# the gamut off the curve rather than off the file for those five.
INPUT_NATIVE_PRIMARIES = {
    "apple_log": "bt2020",
    "hlg": "bt2020",
    "pq": "bt2020",
    "rec709": "bt709",
    **{name: log.gamut for name, log in CAMERA_LOGS.items()},
}

REC709_TO_BT2020 = colour.matrix_RGB_to_RGB(REC709, APPLE_LOG_GAMUT, "CAT02")

# One matrix per camera gamut, into the same BT.2020 scene linear point every
# other input lands on. Every one of these gamuts is D65, as is BT.2020, so
# CAT02 has nothing to adapt and this is a pure primaries change.
CAMERA_GAMUT_TO_BT2020 = {
    name: colour.matrix_RGB_to_RGB(space, APPLE_LOG_GAMUT, "CAT02")
    for name, space in CAMERA_GAMUTS.items()
}

PRIMARIES_TO_BT2020 = {"bt709": REC709_TO_BT2020, **CAMERA_GAMUT_TO_BT2020}


def to_bt2020(scene: np.ndarray, primaries: str) -> np.ndarray:
    """Scene linear on `primaries` -> scene linear on BT.2020.

    bt2020 is a pass through (the matrix would be an identity plus rounding),
    which is what keeps an HLG or PQ file, and Apple Log itself, on exactly the
    numbers their decode produced.

    Every camera gamut is wider than BT.2020, so a saturated colour comes out
    of this with a negative channel. That is correct and is deliberately not
    clamped here: the next step is the matrix into DaVinci Wide Gamut, which is
    wider still, and clamping in the intermediate would throw away colour the
    working space can hold.
    """
    if primaries == "bt2020":
        return scene
    matrix = PRIMARIES_TO_BT2020.get(primaries)
    if matrix is None:
        raise ValueError(f"unknown primaries {primaries!r}")
    return apply_matrix(scene, matrix)


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
