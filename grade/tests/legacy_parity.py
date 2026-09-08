"""The legacy-mask parity fixture: what the engine emitted BEFORE components.

Contract C1 says "components absent or empty: the old behaviour exactly.
Every existing preset renders identically; the parity gate proves it." This
file is that gate's fixture generator, and it is deliberately runnable
against ANY checkout of the engine:

    content/.venv/bin/python grade/tests/legacy_parity.py --engine <path to a grade dir>

prints the fingerprint of every case as JSON. It was run once against the
engine as it stood before the mask work (content/grade at the commit this
worktree branched from) and the answer is blessed in
tests/fixtures/legacy_parity.json; cases_mask.py re-runs it against the engine
in the tree and asserts every case still matches.

What a case fingerprints:

  graph    the whole filter_complex text, with the engine's own root replaced
           by <ROOT> so a checkout in a different directory compares equal
  inputs   the ffmpeg input list, same substitution
  files    a sha1 of every file the graph names (the baked layer cubes, the
           window mattes, the technical LUTs)

Graph text plus the bytes of every file it references IS the render: two
engines that agree on both cannot produce different pixels, and they agree in
milliseconds instead of in ffmpeg minutes. The suite renders one case anyway
as a sanity check on that argument.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from copy import deepcopy
from pathlib import Path


# The cases. Written as plain data so the generator can run against an engine
# that has never heard of components: nothing here mentions a component, a
# finesse block or a linear window.
STRONG_KEY = {"enabled": True, "hue_center": 30.0, "hue_width": 300.0,
              "hue_soft": 30.0, "sat_low": 0.0, "sat_soft": 0.3,
              "lum_soft": 0.3}
STRONG_CORRECT = {"sat_gain": 0.0, "lum_gain": 0.4, "exposure": 0.3,
                  "blur": 2.0}
WINDOW_ON = {"enabled": True, "shape": "ellipse", "cx": 0.45, "cy": 0.55,
             "w": 0.5, "h": 0.4, "rotation": 20.0, "softness": 0.2,
             "invert": False}
RECT_ON = dict(WINDOW_ON, shape="rect", rotation=45.0, softness=0.0)

INFO = {"width": 320, "height": 180, "rotation": 0, "autorotate": True,
        "pix_fmt": "yuv422p10le", "color_range": "tv", "color_space": "bt2020nc",
        "nb_frames": "240", "duration": "10", "codec": "prores", "profile": "4"}


def layer_cases(cg) -> dict:
    """Every legacy mask shape a layer can be in, as (name, config patch)."""
    def layer(mask=None, correct=None, **kw):
        out = deepcopy(cg.LAYER_DEFAULTS)
        out["mask"] = cg.deep_merge(out["mask"], mask or {})
        out["correct"] = cg.deep_merge(out["correct"], correct or STRONG_CORRECT)
        out.update(kw)
        return out

    cases = {
        "layer_none": [],
        "layer_key_only": [layer({"key": STRONG_KEY})],
        "layer_window_only": [layer({"window": WINDOW_ON})],
        "layer_window_and_key": [layer({"window": WINDOW_ON, "key": STRONG_KEY})],
        "layer_window_rect_hard": [layer({"window": RECT_ON, "key": STRONG_KEY})],
        "layer_window_inverted": [layer({"window": dict(WINDOW_ON, invert=True),
                                         "key": STRONG_KEY})],
        "layer_mask_invert_window_only": [layer({"window": WINDOW_ON,
                                                 "invert": True})],
        "layer_mask_invert_key_only": [layer({"key": STRONG_KEY,
                                              "invert": True})],
        "layer_mask_invert_both": [layer({"window": WINDOW_ON, "key": STRONG_KEY,
                                          "invert": True})],
        "layer_mask_invert_neither": [layer({"invert": True})],
        "layer_show_mask": [layer({"window": WINDOW_ON, "key": STRONG_KEY,
                                   "show": True})],
        "layer_after_look": [layer({"window": WINDOW_ON, "key": STRONG_KEY},
                                   placement="after_look")],
        "layer_disabled": [layer({"window": WINDOW_ON, "key": STRONG_KEY},
                                 enabled=False)],
        "layer_two_stacked": [
            layer({"window": WINDOW_ON, "key": STRONG_KEY}),
            layer({"window": RECT_ON}, placement="after_look"),
        ],
        "layer_global_then_windowed": [
            layer({}),
            layer({"window": WINDOW_ON, "key": STRONG_KEY}),
        ],
    }
    return cases


def configs(cg) -> dict:
    """Every case, as a full config the graph builders accept."""
    out = {}
    for name in sorted(p.stem for p in (cg.PRESETS).glob("*.json")):
        out[f"preset_{name}"] = cg.load_preset(name)
    out["defaults"] = deepcopy(cg.DEFAULTS)
    for name, layers in layer_cases(cg).items():
        out[name] = cg.deep_merge(cg.DEFAULTS, {"layers": layers})
    # The input INDEX map is the other half of the legacy contract: the radial
    # ramp comes before the window mattes and the grain plate after them, and
    # a change there reads the wrong input rather than raising.
    with_fx = cg.deep_merge(cg.DEFAULTS, {
        "fx": {"radial_blur": {"enabled": True}}, "grain": {"enabled": True},
        "layers": layer_cases(cg)["layer_two_stacked"]})
    out["layers_with_radial_and_grain"] = with_fx
    return out


def _norm(cg, text: str, root: Path) -> str:
    """Absolute paths out, placeholders in.

    The two cache directories are named separately from the root so the
    reference run can be pointed at a scratch copy of the engine: a
    fingerprint has to be comparable between a checkout in one directory
    whose caches are cold and a checkout in another whose caches are warm,
    and the only thing that carries meaning in a baked file's path is its
    hashed NAME.
    """
    return (text.replace(str(cg.LUT_LAYERS), "<LAYERS>")
                .replace(str(cg.LUT_MASKS), "<MASKS>")
                .replace(str(root), "<ROOT>"))


def fingerprint(cg, cfg: dict, info: dict, root: Path) -> dict:
    """Graph text, input list and the bytes of every file they name."""
    graph = cg.graph_with_mask(cfg, info)
    args = cg.ffmpeg_inputs("SOURCE.MOV", cfg, info, seek=1.5)
    text = f"{graph}\n{' '.join(args)}"
    files = {}
    for m in re.finditer(r"/[^\s'\":,\]\[]+\.(?:cube|png)", text.replace("\\", "")):
        p = Path(m.group(0))
        if not p.is_file():
            continue
        files[_norm(cg, str(p), root)] = hashlib.sha1(
            p.read_bytes()).hexdigest()[:16]
    return {
        "graph": _norm(cg, graph, root),
        "inputs": [_norm(cg, a, root) for a in args],
        "files": files,
    }


def collect(engine: Path) -> dict:
    sys.path.insert(0, str(engine))
    sys.path.insert(0, str(engine / "tools"))
    import cinegrade as cg
    root = Path(cg.ROOT)
    return {name: fingerprint(cg, cfg, INFO, root)
            for name, cfg in configs(cg).items()}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--engine", required=True,
                    help="the grade/ directory of the engine to fingerprint")
    ap.add_argument("-o", "--out", help="write here instead of stdout")
    a = ap.parse_args()
    data = collect(Path(a.engine).resolve())
    text = json.dumps(data, indent=1, sort_keys=True)
    if a.out:
        Path(a.out).write_text(text)
        print(f"{len(data)} cases -> {a.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
