"""Generate the technical conversion LUTs: any supported input -> Rec.709.

This is the CST node. It does one job and no creative grading, so the look LUT
downstream stays interchangeable.

    input code -> scene linear -> BT.2020 -> gamut matrix -> tone map -> encode

"Any supported input" is convert.input in the engine: apple_log, hlg, pq,
rec709 and the five camera logs (slog3, logc3, vlog, clog3, dlog). Every one
of them decodes to the same scene linear BT.2020 point, so only the first step
of the chain above differs and everything after it is shared. That is
deliberate: one mechanism, one file, and no second place where a transfer
function could be written down differently.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

TOOLS = Path(__file__).resolve().parent
GRADE = TOOLS.parent
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(GRADE))
import colorlib as C
# The engine owns the cube naming, so the generator writes the exact file names
# the engine will later look for rather than a parallel convention that could
# drift out of step with it.
import cinegrade as cg


def input_to_scene_bt2020(code, source="apple_log", primaries=None):
    """One input's code values -> scene linear on BT.2020 primaries.

    The shared front of every cube below. `primaries` is the file's own tag,
    defaulting to whatever that input's standard puts it on, so a Rec.709 file
    is matrixed into BT.2020 and an HLG file (already BT.2020) is not touched.
    """
    if primaries is None:
        primaries = C.INPUT_NATIVE_PRIMARIES[source]
    scene = C.INPUT_DECODERS[source](code)
    return C.to_bt2020(scene, primaries)


def input_to_dwg(code, source="apple_log", primaries=None):
    """CST IN: any input -> DaVinci Intermediate / DaVinci Wide Gamut.

    Log to log, wide gamut to wider gamut. Nothing is tone mapped and nothing
    clips, so the primaries stage that runs after this has full headroom.
    """
    scene = input_to_scene_bt2020(code, source, primaries)
    dwg = C.apply_matrix(scene, C.BT2020_TO_DWG)
    return np.clip(C.dwg_encode(np.clip(dwg, 0.0, None)), 0.0, 1.0)


def apple_log_to_dwg(code):
    """The Apple Log CST IN, unchanged. Kept as its own name because the
    suite and the docs both call it that."""
    return input_to_dwg(code, "apple_log")


def dwg_to_rec709(code, tonemap="aces", encode="rec709a"):
    """CST OUT: DaVinci Wide Gamut -> Rec.709, applying the tone map."""
    scene = np.clip(C.dwg_decode(code), 0.0, None)
    if tonemap == "aces":
        ap1 = C.apply_matrix(scene, C.DWG_TO_AP1)
        tone = C.tonemap_aces(np.clip(ap1, 0.0, None))
        lin709 = C.apply_matrix(tone, C.AP1_TO_REC709)
    else:
        lin709 = C.apply_matrix(scene, C.DWG_TO_REC709)
        lin709 = C.TONEMAPS[tonemap](np.clip(lin709, 0.0, None))
    return C.ENCODERS[encode](np.clip(lin709, 0.0, 1.0))


def input_to_rec709(code, source="apple_log", primaries=None, tonemap="aces",
                    encode="gamma24", exposure=0.0):
    """CST DIRECT: any input straight to Rec.709, tone mapped and encoded."""
    scene = input_to_scene_bt2020(code, source, primaries)
    if exposure:
        scene = scene * (2.0 ** exposure)

    if tonemap == "aces":
        # The ACES fit is defined in AP1, so detour through that gamut.
        display = C.AP1_TO_REC709 @ np.eye(3)
        ap1 = C.apply_matrix(scene, C.BT2020_TO_AP1)
        tone = C.tonemap_aces(np.clip(ap1, 0.0, None))
        lin709 = C.apply_matrix(tone, C.AP1_TO_REC709)
    else:
        lin709 = C.apply_matrix(scene, C.BT2020_TO_REC709)
        lin709 = C.TONEMAPS[tonemap](np.clip(lin709, 0.0, None))

    return C.ENCODERS[encode](np.clip(lin709, 0.0, 1.0))


def apple_log_to_rec709(code, tonemap="aces", encode="gamma24", exposure=0.0):
    """The Apple Log direct CST, unchanged. Same reason as apple_log_to_dwg."""
    return input_to_rec709(code, "apple_log", None, tonemap, encode, exposure)


# The label each input carries in a .cube title and in its notes. The Apple Log
# spellings are the ones the cubes on disk already carry, so regenerating them
# rewrites the same bytes.
INPUT_TITLES = {"apple_log": "AppleLog", "hlg": "HLG",
                "pq": "PQ", "rec709": "Rec709", "slog3": "SLog3",
                "logc3": "LogC3", "vlog": "VLog", "clog3": "CLog3",
                "dlog": "DLog"}
INPUT_LABELS = {"apple_log": "Apple Log", "hlg": "HLG", "pq": "PQ",
                "rec709": "Rec.709", "slog3": "Sony S-Log3",
                "logc3": "ARRI LogC3 (EI 800)", "vlog": "Panasonic V-Log",
                "clog3": "Canon Log 3", "dlog": "DJI D-Log"}
PRIMARIES_TITLES = {"bt2020": "BT2020", "bt709": "BT709",
                    "sgamut3cine": "SGamut3Cine", "awg3": "AWG3",
                    "vgamut": "VGamut", "cinemagamut": "CinemaGamut",
                    "dgamut": "DGamut"}
PRIMARIES_LABELS = {"bt2020": "BT.2020", "bt709": "BT.709",
                    "sgamut3cine": "S-Gamut3.Cine",
                    "awg3": "ARRI Wide Gamut 3", "vgamut": "V-Gamut",
                    "cinemagamut": "Cinema Gamut", "dgamut": "D-Gamut"}

# The standard each non Apple Log input is read out of, written into the cube
# so the file itself says where its numbers came from.
INPUT_STANDARDS = {
    "hlg": "Transfer: ITU-R BT.2100 inverse OETF plus the OOTF at system "
           "gamma 1.2 on a 1000 cd/m2 display, anchored on ITU-R BT.2408 "
           "(26 cd/m2 is 0.18 scene linear, 203 cd/m2 reference white is "
           "1.405 scene linear).",
    "pq": "Transfer: SMPTE ST 2084 (BT.2100 PQ) EOTF to absolute cd/m2, "
          "anchored the same way as HLG (BT.2408: 26 cd/m2 is 0.18 scene "
          "linear).",
    "rec709": "Transfer: ITU-R BT.1886 inverse (2.4 gamma) to display "
              "linear, scaled by 1.538569 so a BT.709 OETF encoded 18% grey "
              "(code 0.40901) lands on 0.18 scene linear.",
}

# The five camera logs write their own line from the same constants the decode
# uses, so a cube's header names the document, the 18% grey code it is
# anchored on and the gamut, and cannot say something the maths does not.
for _name, _log in C.CAMERA_LOGS.items():
    INPUT_STANDARDS[_name] = (
        f"Transfer: {_log.doc}, decoded to scene linear reflectance "
        f"(18% grey at code {_log.grey_code():.6f}, which is 0.18 scene "
        f"linear, the same point Apple Log reaches).")


def cube_title(source: str, primaries: str) -> str:
    return f"{INPUT_TITLES[source]}_{PRIMARIES_TITLES[primaries]}"


def cube_in_note(source: str, primaries: str) -> str:
    return (f"In:  {INPUT_LABELS[source]}, {PRIMARIES_LABELS[primaries]} "
            f"primaries, FULL range")

# What --batch builds when it is given no list: every input the engine can
# name, on the primaries a file of that kind really carries.
#
# rec709:bt2020 is here because a Rec.709 transfer on BT.2020 primaries is a
# real combination (a 709 grade delivered inside a BT.2020 container) and the
# primaries are handled by their own matrix rather than assumed.
#
# The five camera logs are listed with no primaries suffix because there is no
# alternative to list: no container tag can name S-Gamut3.Cine, ARRI Wide
# Gamut 3, V-Gamut, Cinema Gamut or D-Gamut, so the gamut comes with the curve
# and each of them has exactly one cube set.
DEFAULT_BATCH = ("hlg", "pq", "rec709", "rec709:bt2020",
                 "slog3", "logc3", "vlog", "clog3", "dlog")

# The primaries a spec may name by hand. A camera gamut is not in here: it is
# reachable only as its own input's native gamut, because pairing (say) V-Log's
# curve with BT.709 primaries is not a file that exists.
SPEC_PRIMARIES = ("bt709", "bt2020")


def parse_spec(spec: str) -> tuple[str, str]:
    """"hlg" or "rec709:bt2020" -> (input, primaries)."""
    source, _, primaries = spec.partition(":")
    source = source.strip()
    if source not in C.INPUT_DECODERS:
        raise SystemExit(f"unknown input {source!r}; "
                         f"pick from {', '.join(C.INPUT_DECODERS)}")
    primaries = primaries.strip() or C.INPUT_NATIVE_PRIMARIES[source]
    if primaries not in SPEC_PRIMARIES and primaries != C.INPUT_NATIVE_PRIMARIES[source]:
        raise SystemExit(f"unknown primaries {primaries!r}; bt709 or bt2020, "
                         f"or leave it off for {source}'s own "
                         f"{C.INPUT_NATIVE_PRIMARIES[source]}")
    return source, primaries


def build_batch(specs, out_dir: Path, size: int) -> list[Path]:
    """Every cube one input needs: the DWG pair's IN, and the direct set.

    The direct set is the full product of tone map and encode, plus the
    unsuffixed name f_convert_out falls back to, exactly as the Apple Log set
    is built. Apple Log's own cubes are never written here, so they stay byte
    identical to what this repository already ships.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    grid = C.identity_grid(size)
    written = []
    for spec in specs:
        source, primaries = parse_spec(spec)
        base = cube_title(source, primaries)
        head = [cube_in_note(source, primaries)]
        if source in INPUT_STANDARDS:
            head.append(INPUT_STANDARDS[source])
        if primaries != "bt2020":
            head.append("Primaries: matrixed to BT.2020 in scene linear "
                        "before anything else runs.")

        path = out_dir / cg.technical_lut_name(source, primaries, "dwg")
        C.write_cube(path, input_to_dwg(grid, source, primaries), size,
                     f"{base}_to_DaVinciWideGamut",
                     comments=["Generated by content/grade/tools/make_cst.py"]
                     + head + ["Out: DaVinci Intermediate, DaVinci Wide Gamut",
                               "No tone map. This is the CST IN of the "
                               "working-space pair."])
        written.append(path)

        for tonemap in C.TONEMAPS:
            for encode in ("", "gamma24", "rec709a"):
                enc = encode or "gamma24"
                name = cg.technical_lut_name(source, primaries, "direct",
                                             tonemap, encode)
                C.write_cube(
                    out_dir / name,
                    input_to_rec709(grid, source, primaries, tonemap, enc),
                    size, f"{base}_to_Rec709_{tonemap}",
                    comments=["Generated by content/grade/tools/make_cst.py"]
                    + head + [f"Out: Rec.709 primaries, {enc} transfer",
                              f"Tone map: {tonemap}"])
                written.append(out_dir / name)
    return written


def print_anchors(size_note: str = "") -> None:
    """Where an 18% grey card sits in each input's own code values.

    These are the numbers cinegrade.MID_GREY_CODE_INPUT carries for the
    `direct` working space's contrast pivot, printed from the same colorlib
    functions the cubes are baked from so the two cannot drift.
    """
    from colour.models import log_encoding_AppleLogProfile as enc
    grey = C.MID_GREY_SCENE
    print(f"18% grey ({grey}) in each input's code values{size_note}:")
    print(f"    apple_log  {float(enc(grey)):.4f}")
    hlg_scene = (grey / C.SCENE_PER_NIT / C.HLG_PEAK_NITS) ** (
        1.0 / C.HLG_SYSTEM_GAMMA)
    print(f"    hlg        {float(C.hlg_oetf(np.array([hlg_scene]))[0]):.4f}")
    print(f"    pq         "
          f"{float(C.pq_inverse_eotf(np.array([grey / C.SCENE_PER_NIT]))[0]):.4f}")
    print(f"    rec709     "
          f"{float((grey / C.REC709_SCENE_SCALE) ** (1.0 / C.BT1886_GAMMA)):.4f}")
    # The camera logs print from their own published forward curve, which is
    # the only place any of these five numbers is computed.
    for name, log in C.CAMERA_LOGS.items():
        print(f"    {name:10s} {log.grey_code():.4f}   ({log.doc})")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--size", type=int, default=65)
    ap.add_argument("--tonemap", default="aces", choices=list(C.TONEMAPS))
    ap.add_argument("--encode", default="gamma24", choices=list(C.ENCODERS))
    ap.add_argument("--exposure", type=float, default=0.0)
    ap.add_argument("--stage", default="direct", choices=["direct", "in", "out"],
                    help="direct = one LUT to Rec.709; in/out = the DWG pair")
    ap.add_argument("--input", default="apple_log",
                    help="the source transfer: apple_log, hlg, pq, rec709, "
                         "slog3, logc3, vlog, clog3 or dlog, optionally with "
                         "its primaries as input:primaries")
    ap.add_argument("--batch", nargs="?", const=",".join(DEFAULT_BATCH),
                    help="write every cube for these inputs into --out-dir "
                         "instead of one cube to --out. Comma separated, "
                         f"default {','.join(DEFAULT_BATCH)}")
    ap.add_argument("--out-dir", default=str(GRADE / "luts" / "technical"))
    ap.add_argument("--anchors", action="store_true",
                    help="print each input's 18%% grey code value and exit")
    ap.add_argument("--out")
    args = ap.parse_args()

    if args.anchors:
        print_anchors()
        return

    if args.batch:
        specs = [s for s in args.batch.split(",") if s.strip()]
        written = build_batch(specs, Path(args.out_dir), args.size)
        print(f"wrote {len(written)} cubes ({args.size}^3) into {args.out_dir}")
        print_anchors()
        return

    if not args.out:
        ap.error("--out is required unless --batch or --anchors is given")

    source, primaries = parse_spec(args.input)
    grid = C.identity_grid(args.size)
    if args.stage == "in":
        out = input_to_dwg(grid, source, primaries)
        title = f"{cube_title(source, primaries)}_to_DaVinciWideGamut"
        notes = [cube_in_note(source, primaries),
                 "Out: DaVinci Intermediate, DaVinci Wide Gamut",
                 "No tone map. This is the CST IN of the working-space pair."]
    elif args.stage == "out":
        out = dwg_to_rec709(grid, args.tonemap, args.encode)
        title = f"DaVinciWideGamut_to_Rec709_{args.tonemap}"
        notes = ["In:  DaVinci Intermediate, DaVinci Wide Gamut",
                 f"Out: Rec.709 primaries, {args.encode} transfer",
                 f"Tone map: {args.tonemap}"]
    else:
        out = input_to_rec709(grid, source, primaries, args.tonemap,
                              args.encode, args.exposure)
        title = f"{cube_title(source, primaries)}_to_Rec709_{args.tonemap}"
        notes = [cube_in_note(source, primaries),
                 f"Out: Rec.709 primaries, {args.encode} transfer",
                 f"Tone map: {args.tonemap}   Exposure: {args.exposure:+.2f} stops"]

    C.write_cube(args.out, out, args.size, title,
                 comments=["Generated by content/grade/tools/make_cst.py"] + notes)

    # Sanity anchors. An 18% grey card must land near 0.39 or the CST is wrong.
    from colour.models import log_encoding_AppleLogProfile as enc
    print(f"wrote {args.out}  ({args.size}^3)")
    if args.stage != "direct" or source != "apple_log":
        return
    print("  anchor checks (Rec.709 output code):")
    for name, lin in [("18% grey", 0.18), ("90% white", 0.90),
                      ("2 stops over grey", 0.72), ("4 stops over", 2.88),
                      ("deep shadow 1%", 0.01)]:
        cv = np.array([[enc(lin)] * 3])
        res = apple_log_to_rec709(cv, args.tonemap, args.encode, args.exposure)
        print(f"    {name:20} log {cv[0][0]:.4f} -> {res[0][0]:.4f}")


if __name__ == "__main__":
    main()
