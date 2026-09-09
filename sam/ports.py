"""Ports on this machine that belong to somebody else.

One list for the whole tree, because four copies of it disagreed and none of
them covered a port that was live at the time (round 1 finding 20). A test that
binds one of these does not fail: it takes over a server a person or another
agent is using, and this workspace has already had a lane take down the
founder's live studio once.

The list itself is DATA, in `studio/tests/forbidden-ports.json`, because the
studio's node harness reads it too. This module reads that file and unions it
with the floor below, so the two can never drift apart in either direction:
a port added there protects everything under `sam/` with no edit here, and
`sam/` still works if the studio tree is not next to it (it is a separate uv
project and must not need one).

Who is on each one, as of round 1:

    7431   the founder's live studio (colour grading UI), always running
    7560   the SAM masks service the founder's studio expects to talk to
    7614   a studio test server another suite starts
    7615   a studio test server another suite starts
    7632   a Codex agent seat's studio, running out of this same worktree
    8756   the always-on launchd Granite ASR server on this machine
    22929  somebody's real server, found the hard way by the memory spike
    28958  the same

Read by `sam/tests/common.py` (`free_port`) and `sam/spike/track_memory.py`.
Print it from anywhere, including a shell:

    python sam/ports.py
"""

from __future__ import annotations

import json
from pathlib import Path

# The shared file, when the studio tree is next to this one.
SHARED = Path(__file__).resolve().parent.parent / "studio" / "tests" / \
    "forbidden-ports.json"

# What sam/ refuses even with no shared file to read: every port known to
# belong to something on this machine at the time of round 1.
FLOOR = frozenset({7431, 7560, 7614, 7615, 7632, 8756, 22929, 28958})


def _load() -> frozenset:
    try:
        shared = {int(p) for p in json.loads(SHARED.read_text())["ports"]}
    except (OSError, ValueError, KeyError, TypeError):
        return FLOOR
    # A union, never a replacement: a port this project knows about must not
    # disappear because somebody edited the other file.
    return frozenset(shared | set(FLOOR))


FORBIDDEN_PORTS = _load()

# The range a test may pick from. 20000 upwards keeps it clear of every
# service in this workspace, and the two spike-found ports above are inside
# it, which is exactly why the set is checked and not only the range.
PORT_RANGE = (20000, 60000)


def is_forbidden(port: int) -> bool:
    return int(port) in FORBIDDEN_PORTS


if __name__ == "__main__":
    print(" ".join(str(p) for p in sorted(FORBIDDEN_PORTS)))
