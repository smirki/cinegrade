#!/usr/bin/env python3
"""resolve_rotation and frames.probe_rotation_tag: the "auto" rotation fix.

    uv run --project sam python sam/tests/test_rotation.py

Integration lane, item 1: sam/server.py's /track used to do
`int(body.get("rotation") or 0)`, which raises on the studio's own default
rotation, the literal string "auto". This suite pins the fix: "auto" (any
case) resolves to the clip's own display-matrix tag, the same field
CG.probe() reads on the studio side, while a concrete quarter turn (an int,
or one of the strings "0"/"90"/"180"/"270") still passes straight through.

No server is started here (this is the plain function, not the wire), so it
runs in under a second and needs no ffmpeg for most of the checks. Two
checks probe a real clip's own rotation tag to prove the resolution reads
the actual file rather than only ever landing on the fallback; both are
against footage already in this worktree (a symlink to content/footage) and
skip themselves, rather than fail, if that clip is not on this machine.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import check, report, sam_path                                # noqa: E402

sam_path()
from frames import probe_rotation_tag                                     # noqa: E402
from server import resolve_rotation                                       # noqa: E402

HERE = Path(__file__).resolve().parent
FOOTAGE = HERE.parent.parent / "footage"
# Rotation tags confirmed on this machine's footage with `ffprobe -show_entries
# stream_side_data` before this test was written: 90 on C002, -90 (== 270 once
# normalised into the studio's 0..350 vocabulary) on C003.
ROTATED_90 = FOOTAGE / "A001_09011336_C002.MOV"
ROTATED_NEG90 = FOOTAGE / "A001_09011832_C003.MOV"


def main() -> int:
    print("concrete rotations pass straight through (ints, strings, no clip needed)")
    check("a bare int 0", resolve_rotation(0, None) == 0)
    check("a bare int 90", resolve_rotation(90, None) == 90)
    check("the string \"0\"", resolve_rotation("0", None) == 0)
    check("the string \"90\"", resolve_rotation("90", None) == 90)
    check("the string \"180\"", resolve_rotation("180", None) == 180)
    check("the string \"270\"", resolve_rotation("270", None) == 270)
    check("a clip path present alongside a concrete rotation changes nothing",
          resolve_rotation("90", str(ROTATED_NEG90)) == 90)

    print("\nabsent or missing rotation, no clip to probe: falls back to 0")
    check("None with no clip", resolve_rotation(None, None) == 0)
    check("empty string with no clip", resolve_rotation("", None) == 0)
    check("\"auto\" with no clip", resolve_rotation("auto", None) == 0)
    check("a clip that does not exist does not raise",
          resolve_rotation("auto", "/no/such/file.mov") == 0)
    check("probe_rotation_tag itself does not raise on a missing file",
          probe_rotation_tag("/no/such/file.mov") == 0)

    print("\n\"auto\" is case insensitive")
    check("\"AUTO\" with no clip", resolve_rotation("AUTO", None) == 0)
    check("\"Auto\" with no clip", resolve_rotation("Auto", None) == 0)

    print("\nthis is the bug itself: the literal request the studio's own "
          "default sends used to raise ValueError")
    try:
        resolve_rotation("auto", None)
        ok = True
    except ValueError:
        ok = False
    check("resolve_rotation(\"auto\", ...) does not raise", ok)

    print("\n\"auto\" resolved from a real clip's own display-matrix tag "
          "(the studio's CG.probe() reads the same field)")
    if ROTATED_90.is_file():
        tag = probe_rotation_tag(str(ROTATED_90))
        check(f"probe_rotation_tag reads {ROTATED_90.name}'s own tag as 90",
              tag == 90, f"got {tag}")
        resolved = resolve_rotation("auto", str(ROTATED_90))
        check("resolve_rotation(\"auto\", clip) matches probe_rotation_tag(clip)",
              resolved == tag, f"got {resolved}")
        resolved_default = resolve_rotation(None, str(ROTATED_90))
        check("a missing rotation field resolves the same way \"auto\" does",
              resolved_default == tag, f"got {resolved_default}")
    else:
        print(f"  skip: {ROTATED_90} is not on this machine")

    if ROTATED_NEG90.is_file():
        tag = probe_rotation_tag(str(ROTATED_NEG90))
        # ffprobe reports this file's own tag as -90; CG.normalise_rotation's
        # numeric branch does `% 360` on a raw tag, so a negative quarter
        # turn lands on 270, never on a negative number a caller would then
        # have to special case.
        check(f"a clip tagged -90 (a real phone rotation) resolves to 270, "
              f"not -90", tag == 270, f"got {tag}")
    else:
        print(f"  skip: {ROTATED_NEG90} is not on this machine")

    return report("sam: rotation \"auto\" resolution")


if __name__ == "__main__":
    raise SystemExit(main())
