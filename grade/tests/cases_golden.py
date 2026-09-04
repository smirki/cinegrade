"""Group 2: golden frames.

A grade is a picture, and a picture is what a unit test cannot hold. What is
stored instead is a compact fingerprint of each rendered frame: per channel
mean, median and percentiles, mean saturation, hue family shares, and a 12x12
block-mean thumbnail plus its hash.

The thumbnail is kept as numbers, not just as a hash, so a failure can say
which part of the frame moved and by how much. A hash alone tells you something
changed and nothing else, which is the difference between a report you can act
on and one you re-bless out of frustration.

Deliberate changes are re-blessed with --update. Nothing else rewrites these
files, so a golden moving on its own is always a regression to explain.
"""

from __future__ import annotations

import json

import harness as H

UPDATE = False

# Chosen to cover the tree, not to cover the preset list: a CST-only baseline,
# a look at full strength, and the two presets that switch on the FX and grain
# branches, across both clips.
GOLDENS = [
    ("clipA_flat", H.CLIP_A, H.TIME_A, "flat"),
    ("clipA_blockbuster", H.CLIP_A, H.TIME_A, "blockbuster"),
    ("clipA_cinekit", H.CLIP_A, H.TIME_A, "cinekit"),
    ("clipB_flat", H.CLIP_B, H.TIME_B, "flat"),
    ("clipB_natural", H.CLIP_B, H.TIME_B, "natural"),
    ("clipB_reels", H.CLIP_B, H.TIME_B, "reels"),
]


def _path(name):
    return H.GOLDENS / f"{name}.json"


def _make_test(name, clip, t, preset):
    def run(ctx):
        img = H.render(clip, H.preset(preset), t)
        got = H.fingerprint(img)
        path = _path(name)

        if UPDATE:
            # What is being blessed away gets printed first. A silent --update
            # is how a regression becomes the new reference: the run says
            # nothing, the file changes, and the next person has no record of
            # what moved. This happened during this suite's own build, so the
            # drift is now part of the update output.
            if path.exists():
                prev = json.loads(path.read_text()).get("fingerprint")
                drifts = H.compare_fingerprint(got, prev) if prev else []
                if drifts:
                    ctx.note(f"{name}: blessing {len(drifts)} changes, "
                             f"confirm each one is intended")
                    for d in drifts:
                        ctx.note(f"  {d}")
                else:
                    ctx.note(f"{name}: unchanged, rewritten identically")
            H.GOLDENS.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(
                {"clip": clip.name, "time": t, "preset": preset,
                 "width": H.WIDTH, "fingerprint": got}, indent=1))
            ctx.note(f"blessed {path.name}: luma mean "
                     f"{got['stats']['luma_mean']:.5f}, sat "
                     f"{got['stats']['sat_mean']:.5f}, "
                     f"hash {got['thumb_hash'][:12]}")
            return

        if not path.exists():
            ctx.check(False, f"no golden at {path}. Render it deliberately with "
                             f"--update, then read the numbers before trusting them")
            return

        want = json.loads(path.read_text())
        s = got["stats"]
        # Recorded before the assertions so it is the line the one-line report
        # shows: the frame's own numbers say more than "it matched".
        ctx.note(f"{preset} on {clip.name} at {t}s: luma mean "
                 f"{s['luma_mean']:.5f} std {s['luma_std']:.5f} "
                 f"sat {s['sat_mean']:.5f} hf {s['hf']:.5f} "
                 f"hash {got['thumb_hash'][:12]}")
        ctx.expect_eq(f"{name}: golden was blessed at the same width",
                      want.get("width"), H.WIDTH)
        drifts = H.compare_fingerprint(got, want["fingerprint"])
        if drifts:
            for d in drifts:
                ctx.check(False, f"{name}: {d}")
        else:
            ctx.check(True, f"{name}: fingerprint matches the blessed golden")

    return run


def test_golden_set_is_complete(ctx):
    """Every declared golden has a blessed file, and nothing stale is left."""
    if UPDATE:
        ctx.skip("update mode writes the golden files, so completeness is moot")
        return
    declared = {n for n, _, _, _ in GOLDENS}
    on_disk = {p.stem for p in H.GOLDENS.glob("*.json")}
    ctx.note(f"{len(declared)} goldens declared, {len(on_disk)} on disk")
    missing = sorted(declared - on_disk)
    orphan = sorted(on_disk - declared)
    ctx.expect_true("every declared golden is blessed", not missing,
                    f"missing: {missing}" if missing else "none missing")
    ctx.expect_true("no orphan golden files left behind", not orphan,
                    f"orphans: {orphan}" if orphan else "none orphaned")


def register(suite):
    g = "golden"
    suite.add(g, "set_is_complete", test_golden_set_is_complete,
              doc="the blessed files on disk match the declared golden set")
    for name, clip, t, preset in GOLDENS:
        suite.add(g, name, _make_test(name, clip, t, preset),
                  doc=f"{preset} on {clip.name} at {t}s")
