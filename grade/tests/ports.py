"""The forbidden port list, read from the one file that holds it.

Same list, same reasons and the same file as `studio/tests/py/ports.py`
(round 1 finding 20): `studio/tests/forbidden-ports.json`. This engine-side
copy of the READER exists only so grade/tests does not have to put
studio/tests/py on sys.path (that folder holds modules whose names would
shadow this suite's own `harness` and `suite`); the LIST itself is not
duplicated, which is the thing the finding was about.

`free_port()` replaces the two hand rolled pickers this suite had, one of
which excluded nothing at all while picking from 20000-60000, a range that
contains two ports a real server was found on.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONTENT = HERE.parents[1]
LIST_PATH = CONTENT / "studio" / "tests" / "forbidden-ports.json"

FORBIDDEN_PORTS: frozenset = frozenset(
    int(p) for p in json.loads(LIST_PATH.read_text())["ports"])


def free_port(attempts: int = 40) -> int:
    """A free loopback port that is not on the forbidden list."""
    for _ in range(max(1, attempts)):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = int(s.getsockname()[1])
        if port not in FORBIDDEN_PORTS:
            return port
    raise RuntimeError(
        f"could not find a free port outside {sorted(FORBIDDEN_PORTS)} "
        f"in {attempts} attempts")
