"""The forbidden port list, read from the one file that holds it.

Round 1 finding 20: four hand written lists in four files disagreed with each
other and none of them covered 7632, which is where a live agent seat's studio
was running at the time. The list now lives in
`studio/tests/forbidden-ports.json` and every suite reads it from there, so
adding a port is one edit in one place.

`free_port()` is the helper every server-starting test in studio/tests/py
should use: it asks the kernel for an unused port and then refuses any port on
the list, however it was offered.

Not named `test_*`, so `unittest discover` does not try to collect it.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

HERE = Path(__file__).resolve().parent
LIST_PATH = HERE.parent / "forbidden-ports.json"

FORBIDDEN_PORTS: frozenset = frozenset(
    int(p) for p in json.loads(LIST_PATH.read_text())["ports"])


def free_port(attempts: int = 40) -> int:
    """A free loopback port that is not on the forbidden list.

    Bind-and-release on port 0 rather than a random pick: the kernel only
    hands out something nothing else holds. The forbidden check is still
    needed, because an ephemeral range can include a listed port and a
    listener that is down right now (a studio somebody is about to restart)
    would happily be handed over.
    """
    for _ in range(max(1, attempts)):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = int(s.getsockname()[1])
        if port not in FORBIDDEN_PORTS:
            return port
    raise RuntimeError(
        f"could not find a free port outside {sorted(FORBIDDEN_PORTS)} "
        f"in {attempts} attempts")
