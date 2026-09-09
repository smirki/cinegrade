"""Group 8: the CLI's identity and project commands, against a real server.

Unlike the rest of cases_cli.py, this group starts an actual studio server
(a random port, a temporary --data-dir, never port 7431 and never
studio/data) and drives it with subprocess calls to cinegrade.py, the same
way an outside agent would. One test owns the server for its whole scenario
and stops it by the process id it captured, in a finally block, so a failure
part way through never leaves an orphan listening on a port.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

import harness as H
# Round 1 finding 20: this file's own picker excluded NOTHING while drawing
# from 20000-60000, a range that contains two ports a real server was found
# squatting. One shared list now (studio/tests/forbidden-ports.json).
from ports import free_port as _free_port

PY = str(H.CONTENT / ".venv" / "bin" / "python")
ENGINE = str(H.GRADE / "cinegrade.py")
SERVER = str(H.CONTENT / "studio" / "server.py")
CLIP = H.CLIP_A.name


def _start_server(data_dir: Path):
    port = _free_port()
    proc = subprocess.Popen(
        [PY, SERVER, "--port", str(port), "--data-dir", str(data_dir)],
        cwd=str(H.CONTENT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True)
    deadline = time.time() + 40
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("studio server exited before it was ready:\n"
                               + (proc.stdout.read() or ""))
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state",
                                   timeout=2).read()
            return proc, port
        except Exception:                                     # noqa: BLE001
            time.sleep(0.25)
    proc.kill()
    raise RuntimeError("studio server never answered /api/state")


def _stop_server(proc) -> None:
    """By the process id captured at spawn, and nothing else."""
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


def _post_json(port, path, payload):
    """One raw POST, for the one call this test needs that has no CLI verb
    of its own (`project/extra`, contract C7's match_crops)."""
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/{path}", data=data,
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


def _cli(port, args, env=None):
    full_env = None
    if env is not None:
        full_env = dict(os.environ)
        full_env.update(env)
    return subprocess.run([PY, ENGINE] + args + ["--port", str(port)],
                          capture_output=True, text=True, env=full_env)


def test_identity_and_project_round_trip(ctx):
    """whoami, session patch and every project command, end to end.

    One server, one scenario, checked in order: this is a workflow test, not
    a table of independent facts, because the round trip (open, edit,
    checkout, fork, undo, redo) is exactly what an agent does and exactly
    what would break quietly if one route's response shape drifted under it.
    """
    tmp = Path(tempfile.mkdtemp(prefix="cinegrade-cli-project-"))
    proc = None
    try:
        proc, port = _start_server(tmp)
        ctx.note(f"server pid {proc.pid} on port {port}, data dir {tmp}")

        # whoami: the three step precedence, nothing else touched.
        r = _cli(port, ["whoami"])
        ctx.expect_eq("whoami: exit code", r.returncode, 0)
        ctx.expect_true("whoami with nothing set reads cli",
                        "by       cli" in r.stdout, r.stdout)

        r = _cli(port, ["whoami"], env={"CINEGRADE_AGENT": "agent:colorbot-3"})
        ctx.expect_true("whoami honours CINEGRADE_AGENT",
                        "by       agent:colorbot-3" in r.stdout, r.stdout)

        r = _cli(port, ["whoami", "--by", "agent:override"],
                 env={"CINEGRADE_AGENT": "agent:colorbot-3"})
        ctx.expect_true("--by beats CINEGRADE_AGENT",
                        "by       agent:override" in r.stdout, r.stdout)

        who = json.loads(_cli(port, ["whoami", "--json"]).stdout)
        ctx.expect_true("whoami --json parses and says logins are off",
                        who.get("auth") is False, json.dumps(who))

        # open
        r = _cli(port, ["project", "open", CLIP, "--by", "studio"])
        ctx.expect_eq("project open: exit code", r.returncode, 0)
        ctx.expect_true("project open reports the clip",
                        CLIP in r.stdout, r.stdout[:300])
        ctx.expect_true("project open starts on branch main",
                        "branch    main" in r.stdout, r.stdout)

        state = json.loads(_cli(port, ["project", "show", "--json"]).stdout)
        ctx.expect_true("project show --json parses and is open",
                        state.get("open") is True, json.dumps(state)[:200])
        root_head = state["head"]

        # session patch with --by and --message
        r = _cli(port, ["session", "patch",
                        '{"primaries": {"saturation": 1.2}}',
                        "--by", "lane", "--message", "warm it"])
        ctx.expect_eq("session patch: exit code", r.returncode, 0)
        out = json.loads(r.stdout)
        ctx.expect_eq("session patch: by lands as sent", out.get("by"), "lane")

        logtxt = _cli(port, ["project", "log"]).stdout
        ctx.expect_true("log shows the patch author", "lane" in logtxt, logtxt)
        ctx.expect_true("log shows the patch message", "warm it" in logtxt, logtxt)
        ctx.expect_true("log has no arrows in it", "->" not in logtxt, logtxt)

        log_json = json.loads(_cli(port, ["project", "log", "--json"]).stdout)
        ctx.expect_eq("log --json parses to two commits so far",
                      log_json.get("total"), 2)
        ctx.expect_eq("log --json head commit carries the message",
                      log_json["commits"][0]["message"], "warm it")

        # rotate and time
        r = _cli(port, ["project", "rotate", "90", "--by", "lane"])
        ctx.expect_eq("rotate: exit code", r.returncode, 0)
        ctx.expect_true("rotate reports the new rotation",
                        "rotation  90" in r.stdout, r.stdout)
        r = _cli(port, ["project", "time", "2.5", "--by", "lane"])
        ctx.expect_eq("time: exit code", r.returncode, 0)
        ctx.expect_true("time reports the new playhead",
                        "time      2.50s" in r.stdout, r.stdout)

        # undo, redo
        r = _cli(port, ["project", "undo", "--by", "lane"])
        ctx.expect_true("undo moved HEAD back to the root",
                        f"head      {root_head}" in r.stdout, r.stdout)
        r = _cli(port, ["project", "redo", "--by", "lane"])
        ctx.expect_true("redo moved HEAD forward again",
                        "moved     yes" in r.stdout, r.stdout)

        # checkout the root by its short id, then edit, which must fork
        r = _cli(port, ["project", "checkout", root_head, "--by", "lane"])
        ctx.expect_eq("checkout: exit code", r.returncode, 0)
        ctx.expect_true("checkout landed on the root",
                        f"head      {root_head}" in r.stdout, r.stdout)
        r2 = _cli(port, ["session", "patch",
                         '{"primaries": {"contrast": 1.3}}',
                         "--by", "lane", "--message", "from the root"])
        ctx.expect_eq("edit from an old HEAD: exit code", r2.returncode, 0)
        state2 = json.loads(_cli(port, ["project", "show", "--json"]).stdout)
        ctx.expect_true("editing from a non tip commit forks",
                        str(state2.get("branch", "")).startswith("fork-"),
                        state2.get("branch"))

        fork_log = _cli(port, ["project", "log", "--all"]).stdout
        ctx.expect_true("the log names the fork point",
                        "forked into" in fork_log, fork_log)

        # an explicit, named fork
        r = _cli(port, ["project", "fork", "my-look", "--by", "lane"])
        ctx.expect_eq("fork: exit code", r.returncode, 0)
        state3 = json.loads(_cli(port, ["project", "show", "--json"]).stdout)
        ctx.expect_eq("named fork switches to that branch",
                      state3.get("branch"), "my-look")

        # contract C7: a saved match_crops pick shows up as "saved picks"
        _post_json(port, "project/extra",
                  {"name": "match_crops",
                   "value": {"IMG_2562.PNG": {"ref": [0.2, 0.25, 0.7, 0.65],
                                               "frame": [0.1, 0.1, 0.9, 0.9]}}})
        r = _cli(port, ["project", "show"])
        ctx.expect_true(
            "project show prints a saved pick after project/extra sets one",
            "saved picks" in r.stdout and "IMG_2562.PNG" in r.stdout
            and "0.20" in r.stdout and "0.70" in r.stdout, r.stdout)
        show_json = json.loads(_cli(port, ["project", "show", "--json"]).stdout)
        ctx.expect_eq(
            "project show --json carries the same extras",
            show_json.get("extras", {}).get("match_crops", {})
            .get("IMG_2562.PNG", {}).get("ref"), [0.2, 0.25, 0.7, 0.65])

        # a bad clip and a bad commit id both fail cleanly, no traceback
        r = _cli(port, ["project", "open", "no-such-clip.mov"])
        ctx.expect_true("opening a clip that does not exist fails",
                        r.returncode != 0)
        ctx.expect_true("no traceback reaches the user for a bad clip",
                        "Traceback" not in r.stderr, r.stderr[:200])

        r = _cli(port, ["project", "checkout", "deadbeef"])
        ctx.expect_true("checking out a commit that does not exist fails",
                        r.returncode != 0)
        ctx.expect_true("no traceback reaches the user for a bad commit",
                        "Traceback" not in r.stderr, r.stderr[:200])
    finally:
        if proc is not None:
            _stop_server(proc)
        shutil.rmtree(tmp, ignore_errors=True)


def register(suite):
    g = "cli_project"
    suite.add(g, "identity_and_project_round_trip",
              test_identity_and_project_round_trip,
              doc="whoami, session patch --by/--message and every project "
                  "command round trip against a real server, killed by pid")
