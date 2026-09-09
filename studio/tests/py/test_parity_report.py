#!/usr/bin/env python3
"""The parity report lands in the run's own data directory, not in the tree.

    content/.venv/bin/python -m unittest discover -s studio/tests/py

Round 3 finding 85. `PARITY_REPORT` was `studio/tools/parity-results.json`,
anchored on the SOURCE tree, while every harness that drives the parity page
runs a throwaway server with its own `--data-dir`. So the documented gate
command (`PARITY_MASKS=1 node parity-gate.mjs`), run from a clean checkout,
wrote timings over a tracked file while it ran: a reviewer or a CI job could
not run the gate and then trust `git status`, and every run dragged timing
churn into the next commit (the round 2 commit carries 733 lines of it).

A studio started with no data directory of its own is a person's real studio
and keeps writing where its own browser tab looks for the file, so the fix is
"follow the data dir when there is one" rather than "move the file".

Two claims, and the first is the one that needs a real server: a run with its
own data directory writes its report there and leaves the tracked file
untouched, and the report it wrote is the one it reads back. The tracked
file's bytes are captured before the request and put back afterwards, because
a test that proves this by dirtying the working tree has done the damage it is
testing for.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
STUDIO = HERE.parents[1]
CONTENT = STUDIO.parent
PYTHON = str(CONTENT / ".venv" / "bin" / "python")
SERVER = str(STUDIO / "server.py")
TRACKED = STUDIO / "tools" / "parity-results.json"

sys.path.insert(0, str(HERE))
from ports import free_port as _free_port                      # noqa: E402


def _get(url: str, timeout: float = 30.0):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


def _post(url: str, payload: dict, timeout: float = 30.0):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


class ParityReportPathTest(unittest.TestCase):
    proc = None
    data_dir = None
    base = ""

    @classmethod
    def setUpClass(cls):
        cls.data_dir = tempfile.mkdtemp(prefix="studio-parity-report-")
        port = _free_port()
        cls.base = f"http://127.0.0.1:{port}/api"
        cls.proc = subprocess.Popen(
            [PYTHON, SERVER, "--port", str(port), "--data-dir", cls.data_dir],
            cwd=str(CONTENT), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        deadline = time.time() + 40.0
        while time.time() < deadline:
            if cls.proc.poll() is not None:
                _out, err = cls.proc.communicate()
                raise RuntimeError("the studio server exited while starting:\n"
                                   + err.decode("utf-8", "replace")[-2000:])
            try:
                _get(cls.base + "/health", timeout=5.0)
                return
            except urllib.error.HTTPError:
                return
            except Exception:                                 # noqa: BLE001
                time.sleep(0.3)
        cls.tearDownClass()
        raise RuntimeError("the studio server never answered /api/health")

    @classmethod
    def tearDownClass(cls):
        if cls.proc is not None:
            try:
                cls.proc.terminate()
                cls.proc.wait(timeout=10)
            except Exception:                                 # noqa: BLE001
                try:
                    cls.proc.kill()
                except Exception:                             # noqa: BLE001
                    pass
            for pipe in (cls.proc.stdout, cls.proc.stderr):
                if pipe is not None:
                    pipe.close()
            cls.proc = None
        if cls.data_dir:
            shutil.rmtree(cls.data_dir, ignore_errors=True)
            cls.data_dir = None

    def test_a_run_with_its_own_data_dir_does_not_write_the_tracked_file(self):
        before = TRACKED.read_bytes() if TRACKED.exists() else None
        payload = {"rows": [{"id": "test-row", "verdict": "EXACT", "width": 640}],
                   "overall": "written by test_parity_report"}
        try:
            answer = _post(self.base + "/parity/report", payload)
            self.assertTrue(answer.get("ok"))

            after = TRACKED.read_bytes() if TRACKED.exists() else None
            self.assertEqual(
                before, after,
                "POST /api/parity/report rewrote studio/tools/parity-results.json, "
                "which is a tracked file: a server started with its own "
                "--data-dir must keep its report there")

            mine = Path(self.data_dir) / "parity-results.json"
            self.assertTrue(mine.is_file(),
                            f"{mine} was not written, so the report went "
                            f"somewhere else entirely")
            self.assertEqual(json.loads(mine.read_text()), payload)

            # And the route reads back what it wrote, so following the data
            # dir did not split the write from the read.
            self.assertEqual(_get(self.base + "/parity/report"), payload)
        finally:
            # Whatever happened above, this suite leaves the checkout as it
            # found it: with the fix removed the assertion fires and the file
            # is still put back.
            if before is not None:
                TRACKED.write_bytes(before)
            elif TRACKED.exists():
                TRACKED.unlink()

    def test_a_studio_with_no_data_dir_of_its_own_keeps_the_file_it_had(self):
        """The other half: a person's real studio is unchanged.

        `studio/tools/parity-results.json` is what parity.html's own tab
        reads, so the fix must not move the file for a normal local run. Asked
        of the resolver directly, with the data directory passed in, so the
        answer does not depend on which environment variable this test process
        happens to have been started with.
        """
        sys.path.insert(0, str(STUDIO))
        import server                                          # noqa: PLC0415

        self.assertEqual(server.parity_report_path(STUDIO / "data"),
                         server.PARITY_REPORT)
        self.assertEqual(server.parity_report_path(Path(self.data_dir)),
                         Path(self.data_dir) / "parity-results.json")


if __name__ == "__main__":
    unittest.main()
