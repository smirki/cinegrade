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
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zlib
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
from ports import free_port as _shared_free_port              # noqa: E402
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
    """A port nothing is on, and nothing on the shared forbidden list.

    This file used to draw from 20000-60000 with a bind test and no list at
    all, which is round 1 finding 20's exact shape: the two ports that list
    names because a real server was found squatting on them (22929, 28958)
    are both inside that range, and a bind test only proves a port is free
    at this instant. One list, one picker
    (studio/tests/forbidden-ports.json, studio/tests/py/ports.py), the same
    one test_mask_routes.py and the node harnesses use.
    """
    for _ in range(60):
        try:
            return _shared_free_port()
        except OSError:
            continue
    raise unittest.SkipTest("no free port")


def cli(*args, stdin: str = "") -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(PYTHON), SERVER, "--data-dir", str(STATE["tmp"]), *args],
        cwd=str(CONTENT), input=stdin, capture_output=True, text=True)


class _FakeSam(BaseHTTPRequestHandler):
    """The smallest SAM service that lets a mask track job EXIST.

    Round 2 findings 54 and 72 are about who may read and cancel a mask track
    job, and a job only exists once POST /api/mask/track has been answered by
    a service. This one accepts a track, keeps it running for ever and never
    writes a matte frame: nothing here is about tracking, only about there
    being a job with a clip name attached to it. The real service's own
    behaviour is covered by test_mask_routes.py against a much fuller fake.
    """

    cancels: list = []

    def log_message(self, fmt, *args):                 # noqa: D401  (quiet)
        pass

    def _json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):                                    # noqa: N802
        if self.path == "/health":
            self._json(200, {"ok": True, "backend": "fake", "model": "none",
                             "loaded": True, "busy": False, "queue": 0})
            return
        if self.path.startswith("/jobs/"):
            self._json(200, {"state": "running", "done_frames": 0,
                             "total_frames": 4, "fps": 10.0,
                             "elapsed_s": 0.1, "matte_ids": ["m_identity"],
                             "mattes": [{"matte_id": "m_identity",
                                         "state": "running"}]})
            return
        self._json(404, {"error": self.path})

    def do_POST(self):                                   # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            self.rfile.read(n)
        if self.path == "/track":
            self._json(200, {
                "job_id": "sam_identity_job", "state": "queued",
                "total_frames": 4, "fps": 10.0,
                "mattes": [{"matte_id": "m_identity", "object_id": "0",
                            "label": "a private subject", "kind": "text"}],
                "matte_ids": ["m_identity"]})
            return
        if self.path.endswith("/cancel"):
            _FakeSam.cancels.append(self.path)
            self._json(200, {"ok": True, "state": "cancelled"})
            return
        self._json(404, {"error": self.path})


def start_fake_sam() -> str:
    port = free_port()
    srv = ThreadingHTTPServer(("127.0.0.1", port), _FakeSam)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    STATE["sam"] = {"server": srv, "thread": thread, "port": port}
    return f"http://127.0.0.1:{port}"


def stop_fake_sam() -> None:
    entry = STATE.get("sam")
    if not entry:
        return
    entry["server"].shutdown()
    entry["server"].server_close()


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

    # Two accounts, before either server starts, so the gated server below has
    # somebody to sign in as. `ida` is an admin because two of the rules this
    # file pins are about the difference: the matte list shows an account only
    # the mattes it may read, and deleting a matte needs an admin.
    for who, role in (("ada", "user"), ("ida", "admin")):
        made = cli("--create-user", who, "--role", role, "--password-stdin",
                   stdin=PASSWORD + "\n")
        if made.returncode != 0:
            raise unittest.SkipTest(f"could not create the {who} test "
                                    f"account: {made.stderr}")

    sam_url = start_fake_sam()
    start_server("primary")
    # The gated server is the one every permission case runs against, so it is
    # the one that needs a SAM service to have mask track jobs at all
    # (findings 54 and 72). Nothing else in this file calls a mask route that
    # reaches the service, so pointing it at the fake changes no other test.
    start_server("gated", "--auth", "--sam-url", sam_url)
    STATE["ada"] = login("ada")
    STATE["ida"] = login("ida")


def tearDownModule() -> None:
    stop_server("primary")
    stop_server("gated")
    stop_fake_sam()
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
# fixtures for the two matte route tests at the end of this file: an account's
# own cookie, a clip that really is private to one account, and a matte on
# disk. Written by hand rather than tracked, because this file never starts a
# SAM service and what those tests need is a matte whose CLIP is known.
# --------------------------------------------------------------------------

def as_user(who: str) -> dict:
    """The headers a signed in account's own browser tab would send.

    `Sec-Fetch-Site: same-origin` is not decoration: a cookie authenticated
    POST, PUT or DELETE with neither that header nor a matching Origin is
    refused by the CSRF rule (contract C2, auth.csrf_ok), on purpose, because a
    script should carry an agent token rather than somebody's session cookie.
    Every browser sends it, so this is what the studio page's own writes look
    like, and without it these tests would be measuring the CSRF rule instead
    of the permission rules they are named for.
    """
    return {"Cookie": STATE[who]["cookie"], "Sec-Fetch-Site": "same-origin"}


def uid_of(who: str) -> int:
    out = call("/api/auth/me", headers=as_user(who), server="gated")
    return int((out.get("user") or {}).get("id") or 0)


def tiny_clip(path: Path, colour: str = "blue", seconds: float = 0.4) -> bool:
    """A real, tiny video file, because /api/open probes what it opens.

    Bounded by an explicit -t like every other ffmpeg call in this repo.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    done = subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
         "-i", f"color=c={colour}:s=64x64:r=10", "-t", str(seconds),
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
        capture_output=True)
    return done.returncode == 0 and path.is_file()


def private_library_clip(who: str) -> str | None:
    """A clip in `who`'s OWN library, opened so the server knows its name.

    Read permission is decided on the path a clip name resolves to
    (library.owner_of_path), and only a file inside an account's library root
    is private to it: the shared footage folder is a read only common area
    every account may see, and a path from anywhere else on the Mac belongs to
    nobody. So this writes a real file into that account's library and opens it
    as that account, which is how a library clip gets a name the matte routes
    can carry. None when anything about that did not work, so the caller can
    skip rather than assert on a setup failure.
    """
    uid = uid_of(who)
    if not uid:
        return None
    target = (Path(STATE["tmp"]) / "users" / str(uid) / "footage"
              / f"private-{who}.mp4")
    if not tiny_clip(target):
        return None
    out = call("/api/open", {"path": str(target)}, headers=as_user(who),
               server="gated")
    if out.get("status") != 200:
        return None
    return out.get("name")


def grey_png(path: Path, value: int = 200, size: int = 8) -> Path:
    """One 8 bit greyscale PNG, stdlib only (this file imports no numpy)."""
    raw = b"".join(b"\x00" + bytes([value]) * size for _ in range(size))

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return (struct.pack(">I", len(data)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xffffffff))

    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 0, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b""))
    return path


def write_matte(matte_id: str, clip: str, frames: int = 2) -> Path:
    """One matte directory under the two servers' shared data dir (C2)."""
    d = Path(STATE["tmp"]) / "mattes" / "fixtures" / matte_id
    d.mkdir(parents=True, exist_ok=True)
    for i in range(frames):
        grey_png(d / f"{i:06d}.png")
    (d / "index.json").write_text(json.dumps({
        "matte_id": matte_id, "clip": clip, "clip_key": "fixtures",
        "rotation": 0, "fps": 10.0, "frames": frames,
        "start_frame": 0, "end_frame": frames, "width": 8, "height": 8,
        "recipe": {"prompts": {"text": ["a private subject"]}},
        "state": "done", "done_frames": frames,
        "areas": [0.5] * frames, "scores": [0.9] * frames,
        "created": time.time(), "model": "fixture", "backend": "fixture",
    }))
    return d


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
        # footage_dir and matte_root joined this route with checkpoint gap 4:
        # a CLI pointed at a server with STUDIO_URL had its own idea of where
        # footage and mattes live, so a bare clip name that worked in the
        # panel raised an ffprobe traceback in the shell. One cheap call now
        # says where both are.
        self.assertEqual(set(out) - {"status", "_set_cookie"},
                         {"ok", "version", "clips", "uptime_s",
                          "ffmpeg_slots_free", "cache_dir", "data_dir",
                          "footage_dir", "matte_root", "logins"})
        self.assertFalse(out["logins"])
        self.assertEqual(out["clips"], len(STATE["clips"]))
        self.assertIsInstance(out["version"], str)
        self.assertGreater(out["uptime_s"], 0)
        self.assertGreaterEqual(out["ffmpeg_slots_free"], 0)
        self.assertEqual(Path(out["data_dir"]), Path(STATE["tmp"]).resolve())
        # Both new paths are this server's own, not the CLI's defaults:
        # answering with content/footage here would send a caller looking in
        # the founder's real folders for a clip only this temp server has.
        self.assertEqual(Path(out["footage_dir"]).resolve(),
                         Path(STATE["footage"]).resolve())
        self.assertEqual(Path(out["matte_root"]).parent,
                         Path(STATE["tmp"]).resolve())

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
        # An agent caller gets the trimmed save shape (round 2 tooling item
        # 5): "name" (no .json suffix, unlike the bare caller's "saved"),
        # "comment", "path", "ok", no library listing. See test_16 for the
        # two shapes checked directly against each other.
        saved = call("/api/preset",
                     {"name": name, "config": {"convert": {"exposure": 0.25}},
                      "comment": "saved by an agent"},
                     headers=agent("sonnet"))
        self.assertEqual(saved["name"], name)
        self.assertEqual(saved["comment"], "saved by an agent")
        self.assertTrue(saved["ok"])
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

    def test_16_preset_save_shape_differs_by_caller(self):
        # Round 2 tooling item 5: a bare (browser-shaped) caller keeps the
        # response savePreset() and fillPresets() in static/app.js actually
        # read, byte for byte; an agent caller gets confirmation of its own
        # call instead of the whole preset library re-sent on every save.
        name = "l2-preset-shape-probe"
        bare = call("/api/preset",
                    {"name": name, "config": {"convert": {"exposure": 0.1}},
                     "comment": "from the browser"})
        self.assertEqual(bare["saved"], f"{name}.json")
        self.assertIn("path", bare)
        self.assertIn("presets", bare)
        self.assertIn(name, [p["name"] for p in bare["presets"]])
        for missing in ("name", "comment", "ok"):
            self.assertNotIn(missing, bare,
                             f"the browser shape grew a {missing!r} key")

        # A second save, as an agent, with no comment: write_preset() keeps
        # the existing comment on disk rather than blanking it, and the
        # agent shape's own "comment" must read back what is actually
        # there, not just echo the (empty) one this call sent.
        trimmed = call("/api/preset",
                       {"name": name, "config": {"convert": {"exposure": 0.2}}},
                       headers=agent("presetshape"))
        self.assertEqual(trimmed["name"], name)
        self.assertEqual(trimmed["comment"], "from the browser",
                         "the agent shape must report the comment the file "
                         "actually carries, not the blank one this call sent")
        self.assertIn("path", trimmed)
        self.assertTrue(trimmed["ok"])
        for missing in ("saved", "presets"):
            self.assertNotIn(missing, trimmed,
                             f"an agent's response still carries {missing!r}")

    def test_17_footage_is_one_shared_root_for_every_caller(self):
        # Logins are off, so an agent reads the same footage folder user 0
        # does: the library read guards treat it exactly like user 0.
        for headers in (None, agent("sonnet"), agent("opus")):
            out = call("/api/library?root=mine&path=", headers=headers)
            self.assertEqual(out.get("status", 200), 200)
            names = [f["name"] for f in out.get("files") or []]
            for clip in STATE["clips"]:
                self.assertIn(clip, names,
                              "an agent could not see the shared footage")

    def test_18_a_commit_by_an_agent_is_signed_by_the_agent(self):
        clip = STATE["clips"][0]
        session_post({"clip": clip,
                      "config": {"convert": {"exposure": 0.61}},
                      "message": "identity test commit"}, agent("sonnet"))
        out = call(f"/api/project/log?clip={urllib.parse.quote(clip)}",
                   headers=agent("sonnet"))
        authors = [c.get("author") for c in out.get("commits") or []]
        self.assertIn("agent:sonnet", authors)

    # --- the log ----------------------------------------------------------

    def test_19_every_request_is_logged_with_its_caller(self):
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

    def test_20_the_log_names_the_clip_when_there_is_one(self):
        clip = STATE["clips"][0]
        session_post({"clip": clip, "time": 4.0}, agent("clipprobe"))
        lines = [ln for ln in log_text().splitlines()
                 if "agent:clipprobe POST /api/session" in ln]
        self.assertTrue(lines)
        self.assertIn(f"clip={clip}", lines[-1])

    # --- the browser is unaffected ---------------------------------------

    def test_21_state_still_has_everything_the_page_reads(self):
        out = call("/api/state")
        for key in ("defaults", "clips", "presets", "looks", "refs",
                    "renders", "paths", "stat_definitions", "scope_aspect",
                    "caller"):
            self.assertIn(key, out)
        self.assertEqual(len(out["clips"]), len(STATE["clips"]))

    # --- the matte routes, with logins ON --------------------------------

    def test_22_the_matte_list_shows_only_the_mattes_an_account_may_read(self):
        """`GET /api/matte?clip=` guarded and `GET /api/matte` did not.

        A matte summary carries the clip's own file name and the matte's
        recipe, which for a text prompt is the prompt words, so the no-clip
        branch handed any signed in account the name of every clip anybody on
        this server had ever tracked a matte on. That is the exact thing
        _guard_read exists to stop ("without this a signed in account could
        name a file it has never been shown").

        The private clip is a real file in ida's own library, opened as ida so
        the server knows the name, which is what makes it a clip ada may not
        read rather than one this server cannot resolve.
        """
        private = private_library_clip("ida")
        if private is None:
            self.skipTest("could not put a clip in ida's library")
        write_matte("m_privateida", private)
        write_matte("m_sharedclip", STATE["clips"][0])

        as_ada = call("/api/matte", headers=as_user("ada"), server="gated")
        self.assertEqual(as_ada.get("status"), 200, as_ada)
        ada_ids = [m["matte_id"] for m in as_ada.get("mattes", [])]
        self.assertNotIn("m_privateida", ada_ids,
                         "the list handed out a matte of a clip in somebody "
                         "else's library")
        self.assertIn("m_sharedclip", ada_ids,
                      "the filter dropped a matte of the shared footage "
                      "folder, which every account may read")

        # The owner still sees it, so this is a permission filter and not a
        # route that stopped answering.
        as_ida = call("/api/matte", headers=as_user("ida"), server="gated")
        self.assertIn("m_privateida",
                      [m["matte_id"] for m in as_ida.get("mattes", [])])

        # The single-matte route was already guarded; asserted here because it
        # is what the list route now agrees with.
        one = call("/api/matte/m_privateida", headers=as_user("ada"),
                   server="gated")
        self.assertEqual(one.get("status"), 403, one)

        # And with logins off nothing is filtered: the local and the agent
        # case answer exactly what they answered before.
        local = call("/api/matte")
        self.assertEqual(
            {"m_privateida", "m_sharedclip"} -
            {m["matte_id"] for m in local.get("mattes", [])}, set(),
            "the primary server (logins off) should still list every matte")

    def test_23_deleting_a_matte_needs_an_admin(self):
        """The one route in the arc that destroys data had no test at all: its
        `_delete` helper in test_mask_routes.py was defined and never called,
        which is how the path-shaped-id blocker shipped. Both sides here: a
        signed in non-admin is refused and the directory survives, an admin
        succeeds and it is gone."""
        matte = write_matte("m_deleteme", STATE["clips"][0])
        refused = call("/api/matte/m_deleteme", method="DELETE",
                       headers=as_user("ada"), server="gated")
        self.assertEqual(refused.get("status"), 403, refused)
        self.assertIn("admin", refused.get("error", ""))
        self.assertTrue(matte.is_dir(), "a non-admin DELETE removed it anyway")

        done = call("/api/matte/m_deleteme", method="DELETE",
                    headers=as_user("ida"), server="gated")
        self.assertEqual(done.get("status"), 200, done)
        self.assertEqual(done.get("deleted"), "m_deleteme")
        self.assertFalse(matte.exists(), "an admin DELETE left it on disk")


    def test_24_the_mask_job_routes_show_only_what_an_account_may_read(self):
        """Round 2 findings 54 and 72: `GET /api/mask/status`, `GET
        /api/mask/jobs`, `GET /api/mask/jobs/<id>` and `POST
        /api/mask/jobs/<id>/cancel`.

        Round 1 gave `GET /api/matte` this guard and wrote out why: a matte
        summary carries the clip's own file name and, for a text prompt, the
        prompt words. A mask JOB carries the same two things (`clip`,
        `clip_key`, and a label built as "mask track <clip> <matte ids>") and
        the three list routes answer with every caller's jobs at once, so on
        a studio with logins on any signed in account could read back the
        file name of every clip anybody had tracked, including clips in
        other people's private library folders. The cancel route had no gate
        at all, and job ids are enumerable through the list routes, so any
        account could stop anybody's track.

        With logins off, which is the default and how every agent runs this,
        all four are no-ops: `test_the_matte_list_with_no_clip_still_answers_
        with_logins_off` in test_mask_routes.py is the other half of that.
        """
        private = private_library_clip("ida")
        if private is None:
            self.skipTest("could not put a clip in ida's library")
        started = call("/api/mask/track",
                       {"clip": private, "prompts": {"text": ["a private "
                                                              "subject"]}},
                       headers=as_user("ida"), server="gated")
        if started.get("status") != 200:
            self.skipTest(f"the track could not be queued: {started}")
        job_id = started.get("job_id")
        self.assertTrue(job_id, started)

        # ada may not read ida's library clip, so she sees no such job at all.
        listed = call("/api/mask/jobs", headers=as_user("ada"), server="gated")
        self.assertEqual(listed.get("status"), 200, listed)
        self.assertNotIn(job_id, [j.get("id") for j in listed.get("jobs", [])],
                         "the jobs list handed out a job on somebody else's "
                         "private clip")
        status = call("/api/mask/status", headers=as_user("ada"),
                      server="gated")
        self.assertEqual(status.get("status"), 200, status)
        blob = json.dumps(status.get("jobs", []))
        self.assertNotIn(job_id, blob)
        self.assertNotIn(private, blob,
                         "mask/status named a clip this account may not read")

        one = call(f"/api/mask/jobs/{job_id}", headers=as_user("ada"),
                   server="gated")
        self.assertEqual(one.get("status"), 403, one)

        cancelled = call(f"/api/mask/jobs/{job_id}/cancel", method="POST",
                         headers=as_user("ada"), server="gated")
        self.assertEqual(cancelled.get("status"), 403, cancelled)

        # The owner still sees it and can still stop it, so this is a
        # permission filter and not a route that stopped working.
        mine = call("/api/mask/jobs", headers=as_user("ida"), server="gated")
        self.assertIn(job_id, [j.get("id") for j in mine.get("jobs", [])])
        mine_one = call(f"/api/mask/jobs/{job_id}", headers=as_user("ida"),
                        server="gated")
        self.assertEqual(mine_one.get("status"), 200, mine_one)
        self.assertEqual(mine_one.get("clip"), private)
        stop = call(f"/api/mask/jobs/{job_id}/cancel", method="POST",
                    headers=as_user("ida"), server="gated")
        self.assertEqual(stop.get("status"), 200, stop)
        self.assertEqual(stop.get("state"), "cancelled", stop)

        # And the generic cancel route, which has the identical hole and the
        # identical one line fix.
        again = call("/api/mask/track",
                     {"clip": private, "prompts": {"text": ["a second "
                                                            "private subject"]}},
                     headers=as_user("ida"), server="gated")
        if again.get("status") == 200 and again.get("job_id"):
            generic = call("/api/job/cancel", {"id": again["job_id"]},
                           headers=as_user("ada"), server="gated")
            self.assertEqual(generic.get("status"), 403, generic)


    def test_26_the_read_rule_has_exactly_one_implementation(self):
        """Round 3 finding 86: two copies of the read rule, under a comment
        saying there was one.

        `_may_read`'s docstring says "Same rule, same function, so the two
        cannot drift apart". That was true while it called `_guard_read`.
        Round 2 needed the same question outside a request handler (for
        `_config_matte_refusals`, which is a plain function carrying a user
        id), added the module level `_may_read_clip`, and pointed `_may_read`
        at it: the METHOD became a thin wrapper, and the five lines it used to
        share with `_guard_read` were copied instead. Two bodies resolving a
        clip name and asking the library the same question, in one file, is
        exactly the drift the docstring promised could not happen.

        Pinned by source, because that is what the claim is about: there is
        one function that turns a clip name into a path and asks
        `LIB.guard_read`, and both the raising half and the asking half go
        through it. The behaviour half of this rule is test_22, test_24 and
        test_25 above, which is why nothing here calls a route.

        Counted as the OPERATION, not as one spelling of it (round 4 finding
        96): this used to count the literal `clip_path(str(name or ""))`, so a
        second copy written `clip_path(str(name))`, or with the argument split
        over two lines, passed a test whose whole purpose was to forbid it.
        What must appear once is the call to `LIB.guard_read` that a clip NAME
        reaches. The file's other call is `POST /api/open`, which guards an
        absolute path the caller handed in and never sees a clip name; that is
        why the count is two and why this test says which is which, so a THIRD
        call site, however it is spelled, is red.
        """
        text = (CONTENT / "studio" / "server.py").read_text()
        rule_at = text.index("def _guard_read_clip(")
        rule_end = text.index("\ndef ", rule_at)
        rule_body = text[rule_at:rule_end]
        self.assertIn("LIB.guard_read(", rule_body,
                      "_guard_read_clip does not ask the library at all")
        asks = [m.start() for m in re.finditer(r"LIB\.guard_read\(", text)]
        outside = [i for i in asks if not rule_at <= i < rule_end]
        self.assertEqual(
            len(asks), 2,
            f"{len(asks)} places in studio/server.py ask LIB.guard_read; "
            f"there has to be exactly one for a clip NAME (_guard_read_clip) "
            f"plus POST /api/open's path guard, or the raising half and the "
            f"asking half of the clip rule can answer differently")
        self.assertEqual(len(outside), 1, outside)
        self.assertIn(
            "raw_open", text[max(0, outside[0] - 800):outside[0]],
            "the second LIB.guard_read is not POST /api/open's path guard, so "
            "it is a second implementation of the clip read rule")
        self.assertIn("def _guard_read_clip(", text)
        for caller in ("def _may_read_clip(", "    def _guard_read(self"):
            i = text.index(caller)
            body = text[i:i + 2000]
            self.assertIn("_guard_read_clip(", body,
                          f"{caller.strip()} does not go through the shared "
                          f"read rule")

    def test_25_a_refusal_does_not_name_a_clip_this_account_cannot_read(self):
        """Round 2 finding 60: the arc's own safety feature was a disclosure
        channel.

        The clip ownership refusal begins "matte <id> was tracked on <the
        other clip's file name>", and it runs on a matte id the CALLER named,
        ahead of any read guard. So naming a matte id you may not read told
        you which file it came from, and finding 54 made the ids enumerable.

        Both halves are pinned: the refusal still happens for ada (it is a
        real mismatch, and silently measuring it is the bug the whole guard
        exists to stop), and it still names the matte she asked for, but it
        does not name ida's file. For ida, who may read that clip, the full
        sentence comes back, so this is a redaction and not a route that
        stopped explaining itself.
        """
        private = private_library_clip("ida")
        if private is None:
            self.skipTest("could not put a clip in ida's library")
        write_matte("m_secretclip", private)
        shared = STATE["clips"][0]
        body = {"clip": shared, "time": 0.1, "width": 64, "config": {},
                "mask": {"components": [{"id": "c1", "type": "matte",
                                         "op": "add",
                                         "matte": {"id": "m_secretclip"}}]}}

        as_ada = call("/api/stats", body, headers=as_user("ada"),
                      server="gated")
        self.assertEqual(as_ada.get("status"), 400, as_ada)
        said = as_ada.get("error", "")
        self.assertIn("m_secretclip", said)
        self.assertNotIn(private, said,
                         "the refusal named a clip this account may not read")
        self.assertIn("cannot read", said)

        as_ida = call("/api/stats", body, headers=as_user("ida"),
                      server="gated")
        self.assertEqual(as_ida.get("status"), 400, as_ida)
        self.assertIn(private, as_ida.get("error", ""),
                      "the owner should still be told which clip it was "
                      "tracked on")


if __name__ == "__main__":
    unittest.main()
