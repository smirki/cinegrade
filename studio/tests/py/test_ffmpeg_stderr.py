#!/usr/bin/env python3
"""A progress-reporting ffmpeg's stderr is a FILE, so it cannot fill up.

    content/.venv/bin/python -m unittest discover -s studio/tests/py

Round 2 finding 70. The studio's three long ffmpeg workers (render, proxy,
mask proxy) all run the same shape:

    proc = Popen(cmd, stdout=PIPE, stderr=???)
    for line in proc.stdout:      # -progress pipe:1, parsed for a percentage
        ...
    proc.wait()
    errors = proc.stderr.read()   # only AFTER wait()

With `stderr=PIPE` that shape deadlocks. A pipe holds about 64 KB; nothing
reads this one until after wait(), so once ffmpeg has written more than that,
ffmpeg blocks inside its own write, stops emitting progress lines on stdout,
the `for` loop blocks waiting for a line that will never come, and wait() is
never reached. The visible symptom is a render job frozen at some percentage
with no error and no timeout, forever.

sam/frames.py hit this in round 1 (finding 45) and fixed it with a temp file.
This arc copied the unfixed shape into the new mask proxy worker, so the fix
is `_ffmpeg_stderr()` in studio/server.py and these are its tests.

Two halves, because they are two different claims:

  the mechanism   the worker loop, driven against a child that writes far
                  more than a pipe can hold BEFORE it writes its progress
                  lines, finishes and reports what the child said. A
                  watchdog kills the child if it does not, so a regression
                  is a red test rather than a hung suite.
  the users       every ffmpeg in studio/server.py that reads a pipe and
                  does not read stderr while it does uses it, and none of
                  them still asks for `stderr=subprocess.PIPE`. That is the
                  three progress workers plus the play segment streamer,
                  which has the same shape, no stderr reader at all, and the
                  worst version of the consequence: it blocks holding an
                  ffmpeg slot and a live response.

No ffmpeg and no server are started here: the child is a python one liner, so
the test is fast and the failure it describes is reproduced exactly rather
than approximated.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
STUDIO = HERE.parents[1]
CONTENT = STUDIO.parent
PYTHON = str(CONTENT / ".venv" / "bin" / "python")

# Comfortably more than a 64 KB pipe buffer, and written in one go, which is
# what a per frame ffmpeg warning on a long clip adds up to.
NOISE_BYTES = 300 * 1024
WATCHDOG_S = 20.0


class FfmpegStderrTest(unittest.TestCase):
    server_mod = None
    tmp = None

    @classmethod
    def setUpClass(cls):
        # server.py opens a database at import time; STUDIO_DATA_DIR points it
        # at a throwaway folder so importing the module never touches
        # studio/data.
        cls.tmp = tempfile.mkdtemp(prefix="studio-ffmpeg-stderr-")
        os.environ["STUDIO_DATA_DIR"] = cls.tmp
        sys.path.insert(0, str(STUDIO))
        import server                                       # noqa: PLC0415
        cls.server_mod = server

    @classmethod
    def tearDownClass(cls):
        if cls.tmp:
            shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_a_noisy_child_does_not_wedge_the_progress_loop(self):
        """The worker shape, run for real against a deliberately noisy child.

        The child writes 300 KB to stderr FIRST and only then its progress
        lines, so with a pipe it can never reach them: this is the deadlock,
        not a model of it. The killer thread exists so that a regression
        reports a failure instead of hanging the suite; if it ever fires, the
        test says so in its own message.
        """
        srv = self.server_mod
        child = (
            "import sys\n"
            f"sys.stderr.write('x' * {NOISE_BYTES})\n"
            "sys.stderr.flush()\n"
            "for i in range(1, 4):\n"
            "    sys.stdout.write('out_time_ms=%d\\n' % (i * 1000))\n"
            "    sys.stdout.flush()\n"
            "sys.stderr.write('\\nthe last thing it said')\n"
        )
        errors = srv._ffmpeg_stderr()
        proc = subprocess.Popen([PYTHON, "-c", child],
                                stdout=subprocess.PIPE, stderr=errors)
        killed = []

        def watchdog():
            try:
                proc.wait(timeout=WATCHDOG_S)
            except subprocess.TimeoutExpired:
                killed.append(True)
                proc.kill()

        t = threading.Thread(target=watchdog, daemon=True)
        t.start()

        seen = []
        for line in proc.stdout:                    # the workers' own loop
            line = line.decode("utf-8", "replace").strip()
            if line.startswith("out_time_ms="):
                seen.append(line)
        proc.wait()
        t.join(timeout=WATCHDOG_S + 5)

        self.assertFalse(
            killed,
            "the progress loop never finished: a child that writes more than "
            "a pipe holds to stderr wedged it, which is exactly the render "
            "job frozen at a percentage that finding 70 is about")
        self.assertEqual(len(seen), 3, seen)
        self.assertEqual(proc.returncode, 0)

        tail = srv._ffmpeg_stderr_text(errors)
        self.assertIn("the last thing it said", tail)
        self.assertLessEqual(len(tail), 600,
                             "the tail is trimmed for a job message")

    def test_the_tail_never_raises_on_a_handle_it_cannot_read(self):
        """A broken stderr handle must not replace the real failure.

        A job that died for a real reason has to report THAT reason; the
        stderr tail is a nicety on top of it.
        """
        srv = self.server_mod

        class Broken:
            def seek(self, *_a):
                raise OSError("gone")

            def read(self, *_a):
                raise OSError("gone")

        self.assertEqual(srv._ffmpeg_stderr_text(Broken()), "")

    def test_all_three_workers_use_a_file_and_none_of_them_a_pipe(self):
        """The source, because the mechanism above only proves the helper.

        Several readers in one file share this shape and the arc's own
        history is that a fixed one and an unfixed one lived side by side.
        So: at least one `_ffmpeg_stderr()` for every `-progress` worker, and
        no `stderr=subprocess.PIPE` anywhere in the file. The fourth reader
        the count allows for is the play segment streamer, which is not a
        progress worker but blocks on stdout the same way and was found while
        fixing these three.
        """
        text = (STUDIO / "server.py").read_text()
        progress = text.count('"-progress"')
        uses = len(re.findall(r"_ffmpeg_stderr\(\)", text))
        self.assertGreaterEqual(
            progress, 3, "expected the three progress-reporting workers")
        self.assertGreaterEqual(
            uses, progress,
            f"{progress} workers read -progress line by line but only {uses} "
            f"of them give stderr a file")
        self.assertNotIn(
            "stderr=subprocess.PIPE", text,
            "a worker still hands stderr a pipe nothing reads until after "
            "wait(), which is the deadlock finding 70 describes")
        self.assertIn("stderr=errors", text)


if __name__ == "__main__":
    unittest.main()
