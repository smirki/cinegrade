#!/usr/bin/env python3
"""Contract G2: one shared server, one identity per caller, logins off.

Run from content/:

    .venv/bin/python -m unittest discover -s studio/tests/py -v

The problem this covers. With logins off every request used to be user 0, so
six agents grading at once shared one live session, one open project, one set
of stored match crops and one rotation fallback: one agent's scrub moved
everybody else's picture. An agent now names itself with an X-Studio-Agent
header and gets a row of its own, and can borrow somebody else's state on
purpose with X-Studio-Attach rather than by accident.

Everything here runs against REAL servers on random high ports, each with its
own temporary data folder and its own temporary footage folder of symlinks,
both deleted at the end and both terminated by the process id captured at
spawn. That is not politeness: studio/data holds somebody's accounts, grades
and project history, content/footage holds their footage, and studio/cache
holds the frames they are scrubbing through right now. A test that opened any
of the three would be editing real work.

Two servers, because the last case needs the opposite setting:

  primary   logins OFF, its own data and footage. Almost every test.
  gated     logins ON, the SAME data folder, so the agent rows the primary
            created are visible to it and it can prove they cannot sign in.
"""

from __future__ import annotations

import json
import os
import random
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONTENT = HERE.parent.parent.parent
PYTHON = CONTENT / ".venv" / "bin" / "python"
SERVER = "studio/server.py"
REAL_FOOTAGE = CONTENT / "footage"
REAL_DATA = (CONTENT / "studio" / "data").resolve()
REAL_CACHE = (CONTENT / "studio" / "cache").resolve()

PASSWORD = "a-long-enough-test-password"
STATE: dict = {}


# --------------------------------------------------------------------------
# servers of our own
# --------------------------------------------------------------------------

def free_port() -> int:
    for _ in range(60):
        port = random.randint(20000, 60000)
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise unittest.SkipTest("no free port")


def cli(*args, stdin: str = "") -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(PYTHON), SERVER, "--data-dir", str(STATE["tmp"]), *args],
        cwd=str(CONTENT), input=stdin, capture_output=True, text=True)


def start_server(name: str, *extra) -> None:
    """Spawn one server, wait for it to answer, remember its pid and log."""
    port = free_port()
    log = open(STATE["tmp"] / f"{name}.log", "w+b")          # noqa: SIM115
    proc = subprocess.Popen(
        [str(PYTHON), SERVER, "--port", str(port),
         "--data-dir", str(STATE["tmp"]), "--footage", str(STATE["footage"]),
         *extra],
        cwd=str(CONTENT), stdout=log, stderr=subprocess.STDOUT)
    STATE[name] = {"port": port, "base": f"http://127.0.0.1:{port}",
                   "proc": proc, "log": log,
                   "log_path": STATE["tmp"] / f"{name}.log"}
    deadline = time.time() + 40
    while time.time() < deadline:
        if proc.poll() is not None:
            raise unittest.SkipTest(f"the {name} server exited before it "
                                    f"was ready")
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health",
                                   timeout=2).read()
            return
        except urllib.error.HTTPError:
            return                       # 401 means it is up and gated
        except Exception:                                     # noqa: BLE001
            time.sleep(0.25)
    raise unittest.SkipTest(f"the {name} server never came up")


def stop_server(name: str) -> None:
    entry = STATE.get(name)
    if not entry:
        return
    proc = entry["proc"]
    if proc.poll() is None:
        proc.terminate()                 # by the pid captured at spawn
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
    try:
        entry["log"].close()
    except Exception:                                         # noqa: BLE001
        pass


def setUpModule() -> None:
    if not PYTHON.exists():
        raise unittest.SkipTest("content/.venv is not there")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise unittest.SkipTest("ffmpeg and ffprobe are needed by the server")
    clips = sorted(p for p in REAL_FOOTAGE.glob("*")
                   if p.suffix.lower() in (".mov", ".mp4"))[:3]
    if not clips:
        raise unittest.SkipTest("no clips in content/footage to point at")

    STATE["tmp"] = Path(tempfile.mkdtemp(prefix="studio-identity-test-"))
    STATE["footage"] = STATE["tmp"] / "footage"
    STATE["footage"].mkdir(parents=True, exist_ok=True)
    # Symlinks, never copies: these are multi hundred megabyte camera files
    # and the point is only that the server has real clips to resolve. The
    # test never writes through them.
    for p in clips:
        (STATE["footage"] / p.name).symlink_to(p)
    STATE["clips"] = [p.name for p in clips]

    guard = Path(STATE["tmp"]).resolve()
    if str(guard).startswith(str(REAL_DATA)):
        raise SystemExit("refusing to run: the temp data dir is inside the "
                         "real studio/data")

    # One account, before either server starts, so the gated server below has
    # somebody to sign in as.
    made = cli("--create-user", "ada", "--role", "user", "--password-stdin",
               stdin=PASSWORD + "\n")
    if made.returncode != 0:
        raise unittest.SkipTest(f"could not create the test account: "
                                f"{made.stderr}")

    start_server("primary")
    start_server("gated", "--auth")
    STATE["ada"] = login("ada")


def tearDownModule() -> None:
    stop_server("primary")
    stop_server("gated")
    if STATE.get("tmp"):
        shutil.rmtree(STATE["tmp"], ignore_errors=True)


# --------------------------------------------------------------------------
# talking to them
# --------------------------------------------------------------------------

def call(path, payload=None, method=None, headers=None, server="primary"):
    """One request. Returns the decoded JSON with `status` on a refusal."""
    hdrs = dict(headers or {})
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(STATE[server]["base"] + path, data=data,
                                 headers=hdrs,
                                 method=method or ("POST" if data else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            body = r.read()
            if (r.headers.get("Content-Type") or "").startswith(
                    "application/json"):
                out = json.loads(body.decode())
                if isinstance(out, dict):
                    out["_set_cookie"] = r.headers.get("Set-Cookie", "")
                    out["status"] = r.status
                return out
            return {"bytes": len(body), "status": r.status}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        try:
            out = json.loads(raw)
        except json.JSONDecodeError:
            out = {"error": raw[:300]}
        out["status"] = exc.code
        return out


def login(name: str) -> dict:
    out = call("/api/auth/login", {"username": name, "password": PASSWORD},
               server="gated")
    cookie = (out.get("_set_cookie") or "").split(";")[0]
    return {"name": name, "cookie": cookie}


def agent(name: str) -> dict:
    return {"X-Studio-Agent": name}


def session_get(headers=None) -> dict:
    return call("/api/session", headers=headers)


def session_post(payload, headers=None) -> dict:
    return call("/api/session", payload, headers=headers)


def log_text() -> str:
    """Everything the primary server has written to its log so far."""
    path = STATE["primary"]["log_path"]
    try:
        return path.read_text(errors="replace")
    except OSError:
        return ""


# --------------------------------------------------------------------------
# the tests. Numbered because they tell one story in order.
# --------------------------------------------------------------------------

class Identity(unittest.TestCase):
    maxDiff = None

    # --- the cheap first call ------------------------------------------

    def test_01_health_says_what_this_server_is(self):
        out = call("/api/health")
        self.assertEqual(out["status"], 200)
        self.assertTrue(out["ok"])
        self.assertEqual(set(out) - {"status", "_set_cookie"},
                         {"ok", "version", "clips", "uptime_s",
                          "ffmpeg_slots_free", "cache_dir", "data_dir",
                          "logins"})
        self.assertFalse(out["logins"])
        self.assertEqual(out["clips"], len(STATE["clips"]))
        self.assertIsInstance(out["version"], str)
        self.assertGreater(out["uptime_s"], 0)
        self.assertGreaterEqual(out["ffmpeg_slots_free"], 0)
        self.assertEqual(Path(out["data_dir"]), Path(STATE["tmp"]).resolve())

    def test_02_the_cache_lands_under_the_data_dir(self):
        # The whole reason this exists: a temp server must not evict the
        # frames the real one is scrubbing through.
        out = call("/api/health")
        cache = Path(out["cache_dir"])
        self.assertEqual(cache.parent, Path(STATE["tmp"]).resolve())
        self.assertNotEqual(cache, REAL_CACHE)
        self.assertTrue(cache.is_dir(), "the cache folder was never made")

    # --- who is calling -------------------------------------------------

    def test_03_a_bare_caller_is_still_user_zero(self):
        for path in ("/api/state", "/api/whoami"):
            out = call(path)
            self.assertEqual(out["caller"],
                             {"id": 0, "name": "local", "attached_to": None},
                             f"{path} changed for a caller that sent nothing")

    def test_04_an_agent_gets_a_row_of_its_own(self):
        out = call("/api/whoami", headers=agent("sonnet"))
        self.assertEqual(out["caller"]["name"], "agent:sonnet")
        self.assertGreater(out["caller"]["id"], 0)
        self.assertIsNone(out["caller"]["attached_to"])
        # A write from this caller is signed with the same name, whatever the
        # body says it is from.
        self.assertEqual(out["by"], "agent:sonnet")
        again = call("/api/whoami", headers=agent("sonnet"))
        self.assertEqual(again["caller"]["id"], out["caller"]["id"],
                         "a second call made a second row")

    def test_05_the_query_and_body_forms_name_the_same_caller(self):
        by_header = call("/api/whoami", headers=agent("terra"))["caller"]
        by_query = call("/api/whoami?agent=terra")["caller"]
        self.assertEqual(by_query, by_header)
        # And in a body, for a caller that cannot set headers.
        by_body = call("/api/session", {"agent": "terra", "time": 0.5})
        self.assertEqual(by_body["by"], "agent:terra")

    def test_06_a_bad_agent_name_is_refused(self):
        for bad in ("../evil", "a/b", "", "x" * 65):
            out = call("/api/whoami", headers={"X-Studio-Agent": bad})
            if bad == "":
                # An empty header is the same as no header at all.
                self.assertEqual(out["caller"]["id"], 0)
                continue
            self.assertEqual(out.get("status"), 400,
                             f"{bad!r} was not refused")

    # --- one caller, one session ----------------------------------------

    def test_07_two_agents_hold_independent_sessions(self):
        clip = STATE["clips"][0]
        one = session_post({"clip": clip, "time": 1.0}, agent("luna"))
        self.assertEqual(one["clip"], clip)
        self.assertEqual(one["time"], 1.0)
        self.assertEqual(one["by"], "agent:luna")

        two = session_get(agent("sol"))
        self.assertIsNone(two["clip"], "the second agent saw the first's clip")
        self.assertEqual(two["rev"], 0)

        session_post({"clip": clip, "time": 7.0}, agent("sol"))
        self.assertEqual(session_get(agent("luna"))["time"], 1.0,
                         "the second agent moved the first one's playhead")
        self.assertEqual(session_get(agent("sol"))["time"], 7.0)

    def test_08_two_agents_hold_independent_workspaces(self):
        # Each opened the same clip above, so the project key is shared (a
        # project is per clip, by design) but the workspace pointer is per
        # caller, and a caller that opened nothing still has none.
        luna = call("/api/whoami", headers=agent("luna"))["project"]
        sol = call("/api/whoami", headers=agent("sol"))["project"]
        fresh = call("/api/whoami", headers=agent("vega"))["project"]
        self.assertTrue(luna)
        self.assertEqual(luna, sol)
        self.assertIsNone(fresh, "a brand new agent inherited a workspace")

    def test_09_the_browser_tab_is_untouched_by_all_of_it(self):
        out = session_get()
        self.assertIsNone(out["clip"])
        self.assertEqual(out["rev"], 0)
        self.assertIsNone(call("/api/whoami")["project"])

    # --- attach ----------------------------------------------------------

    def test_10_attach_sees_and_changes_user_zero(self):
        clip = STATE["clips"][0]
        both = {"X-Studio-Agent": "sonnet", "X-Studio-Attach": "0"}
        out = call("/api/whoami", headers=both)
        self.assertEqual(out["caller"]["name"], "agent:sonnet")
        self.assertEqual(out["caller"]["attached_to"], 0)

        # Writing while attached moves USER 0's session, not the agent's.
        session_post({"clip": clip, "time": 3.5}, both)
        local = session_get()
        self.assertEqual(local["clip"], clip)
        self.assertEqual(local["time"], 3.5)
        # ... and it is signed by the agent, not by the user it borrowed.
        self.assertEqual(local["by"], "agent:sonnet")
        self.assertNotEqual(session_get(agent("sonnet"))["time"], 3.5,
                            "the attached write also moved the agent's own "
                            "session")

    def test_11_attach_to_an_unknown_name_is_refused(self):
        out = call("/api/whoami", headers={"X-Studio-Agent": "sonnet",
                                           "X-Studio-Attach": "nobody-here"})
        self.assertEqual(out["status"], 400)
        self.assertIn("no account or agent", out["error"])

    def test_12_attach_is_refused_with_logins_on(self):
        hdrs = {"Cookie": STATE["ada"]["cookie"], "X-Studio-Attach": "0"}
        out = call("/api/whoami", headers=hdrs, server="gated")
        self.assertEqual(out["status"], 403)
        self.assertIn("logins", out["error"])
        # And the same refusal without a session, so it is the header that is
        # refused rather than the account that is missing a permission.
        out = call("/api/whoami", headers={"X-Studio-Attach": "0"},
                   server="gated")
        self.assertEqual(out["status"], 403)

    def test_13_an_agent_row_can_never_sign_in(self):
        # Make the row on the server with logins off ...
        made = call("/api/whoami", headers=agent("loginprobe"))
        self.assertEqual(made["caller"]["name"], "agent:loginprobe")
        # ... then try it as an account on the gated server, which shares the
        # same database. Both an empty password and a guess are refused.
        for password in ("", PASSWORD, "agent:loginprobe"):
            out = call("/api/auth/login",
                       {"username": "agent:loginprobe", "password": password},
                       server="gated")
            self.assertEqual(out["status"], 401,
                             f"an agent signed in with {password!r}")

    # --- concurrency ------------------------------------------------------

    def test_14_a_stale_if_rev_is_409_and_changes_nothing(self):
        clip = STATE["clips"][0]
        first = session_post({"clip": clip, "time": 2.0}, agent("rev"))
        rev = first["rev"]
        ok = session_post({"time": 2.5, "if_rev": rev}, agent("rev"))
        self.assertEqual(ok["time"], 2.5)
        self.assertEqual(ok["rev"], rev + 1)

        stale = session_post({"time": 9.9, "if_rev": rev}, agent("rev"))
        self.assertEqual(stale["status"], 409)
        self.assertEqual(stale["rev"], rev + 1)
        self.assertEqual(stale["session"]["time"], 2.5)
        self.assertEqual(session_get(agent("rev"))["time"], 2.5,
                         "the refused write landed anyway")

    # --- what stays shared ------------------------------------------------

    def test_15_a_preset_an_agent_saves_is_everybody_s(self):
        name = "l2-identity-probe"
        saved = call("/api/preset",
                     {"name": name, "config": {"convert": {"exposure": 0.25}},
                      "comment": "saved by an agent"},
                     headers=agent("sonnet"))
        self.assertEqual(saved["saved"], f"{name}.json")
        # users/0/presets, whoever saved it, so the browser tab sees it.
        self.assertEqual(Path(saved["path"]).parent,
                         Path(STATE["tmp"]).resolve() / "users" / "0"
                         / "presets")
        for headers in (None, agent("opus")):
            listed = call("/api/presets", headers=headers)
            self.assertIn(name, [p["name"] for p in listed["presets"]],
                          "a preset an agent saved was invisible")
        back = call(f"/api/preset?name={urllib.parse.quote(name)}")
        self.assertAlmostEqual(back["config"]["convert"]["exposure"], 0.25)

    def test_16_footage_is_one_shared_root_for_every_caller(self):
        # Logins are off, so an agent reads the same footage folder user 0
        # does: the library read guards treat it exactly like user 0.
        for headers in (None, agent("sonnet"), agent("opus")):
            out = call("/api/library?root=mine&path=", headers=headers)
            self.assertEqual(out.get("status", 200), 200)
            names = [f["name"] for f in out.get("files") or []]
            for clip in STATE["clips"]:
                self.assertIn(clip, names,
                              "an agent could not see the shared footage")

    def test_17_a_commit_by_an_agent_is_signed_by_the_agent(self):
        clip = STATE["clips"][0]
        session_post({"clip": clip,
                      "config": {"convert": {"exposure": 0.61}},
                      "message": "identity test commit"}, agent("sonnet"))
        out = call(f"/api/project/log?clip={urllib.parse.quote(clip)}",
                   headers=agent("sonnet"))
        authors = [c.get("author") for c in out.get("commits") or []]
        self.assertIn("agent:sonnet", authors)

    # --- the log ----------------------------------------------------------

    def test_18_every_request_is_logged_with_its_caller(self):
        call("/api/whoami", headers=agent("logprobe"))
        call("/api/whoami", headers={"X-Studio-Agent": "logprobe",
                                     "X-Studio-Attach": "0"})
        text = log_text()
        plain = [ln for ln in text.splitlines()
                 if "agent:logprobe GET /api/whoami" in ln]
        attached = [ln for ln in text.splitlines()
                    if "agent:logprobe>0 GET /api/whoami" in ln]
        self.assertTrue(plain, "no request log line for the agent")
        self.assertTrue(attached, "no request log line for the attached call")
        # time, caller, method, route, status, milliseconds
        self.assertRegex(plain[0],
                         r"^\d\d:\d\d:\d\d agent:logprobe GET /api/whoami "
                         r"\d{3} \d+ms$")

    def test_19_the_log_names_the_clip_when_there_is_one(self):
        clip = STATE["clips"][0]
        session_post({"clip": clip, "time": 4.0}, agent("clipprobe"))
        lines = [ln for ln in log_text().splitlines()
                 if "agent:clipprobe POST /api/session" in ln]
        self.assertTrue(lines)
        self.assertIn(f"clip={clip}", lines[-1])

    # --- the browser is unaffected ---------------------------------------

    def test_20_state_still_has_everything_the_page_reads(self):
        out = call("/api/state")
        for key in ("defaults", "clips", "presets", "looks", "refs",
                    "renders", "paths", "stat_definitions", "scope_aspect",
                    "caller"):
            self.assertIn(key, out)
        self.assertEqual(len(out["clips"]), len(STATE["clips"]))


if __name__ == "__main__":
    unittest.main()
