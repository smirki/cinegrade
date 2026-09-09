"""Group 9: contract G9's friction fixes, port safety and the new agent verbs.

Two kinds of test live here on purpose:

The port-refusal and env-precedence checks never open a socket: they call
cinegrade.resolve_server / cinegrade.require_server in process, against
constructed argparse.Namespace objects, so this file can prove "no --port, no
--url, no env at all still means 7431" without ever risking a stray request
landing on the founder's real server at 7431. A manual smoke test earlier in
this lane's own work did make that mistake once (a bare `whoami` with no
flags, against a server that happened to be up); this file is written so an
automated run can never repeat it, not just so this run does not.

Everything that DOES talk over HTTP does so against a server this file starts
itself: a random port, its own temporary --data-dir, and a --footage
directory of SYMLINKS to the real clips in content/footage (never copies, a
clip's identity is a hash of its own bytes), killed by the process id
captured at spawn, the same pattern studio/tests/py/test_grade_client.py and
cases_cli_project.py both use.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

import harness as H
from ports import free_port as _shared_free_port

cg = H.cg

PY = str(H.CONTENT / ".venv" / "bin" / "python")
ENGINE = str(H.GRADE / "cinegrade.py")
SERVER = str(H.CONTENT / "studio" / "server.py")
FOOTAGE_SRC = H.CONTENT / "footage"
REFS = H.CONTENT / "refs"
CLIP = H.CLIP_A.name

_ENV_KEYS = ("STUDIO_PORT", "STUDIO_URL", "STUDIO_AGENT")


# --------------------------------------------------------------------------
# resolve_server / require_server: in process, no socket, ever
# --------------------------------------------------------------------------

def _ns(**kw):
    base = {"port": None, "url": None, "agent": None, "attach": None}
    base.update(kw)
    return argparse.Namespace(**base)


def _clear_env() -> dict:
    return {k: os.environ.pop(k, None) for k in _ENV_KEYS}


def _restore_env(saved: dict) -> None:
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def test_resolve_server_defaults_to_7431_unlabelled(ctx):
    """No --port, no --url, no env at all: the human default, proved without
    ever opening a socket. This only reads the constant cinegrade.py itself
    defines; it never dials it."""
    saved = _clear_env()
    try:
        base, given = cg.resolve_server(_ns())
    finally:
        _restore_env(saved)
    ctx.expect_eq("defaults to 7431", base, f"http://127.0.0.1:{cg.STUDIO_PORT}")
    ctx.expect_true("given is False with nothing set", given is False, str(given))


def test_require_server_allows_a_human_with_no_agent(ctx):
    saved = _clear_env()
    try:
        base = cg.require_server(_ns())
    finally:
        _restore_env(saved)
    ctx.expect_eq("human keeps the 7431 default", base,
                  f"http://127.0.0.1:{cg.STUDIO_PORT}")


def test_require_server_refuses_an_unlabelled_agent(ctx):
    """Contract G9 friction 8: naming yourself an agent without naming a
    server used to silently inherit the founder's live studio at 7431."""
    saved = _clear_env()
    try:
        try:
            cg.require_server(_ns(agent="colorbot"))
            ctx.check(False, "an unlabelled agent was not refused")
        except cg.GradeError as exc:
            ctx.expect_true("refusal names the requirement",
                            "must name its server" in str(exc), str(exc))
    finally:
        _restore_env(saved)


def test_require_server_refuses_via_studio_agent_env_too(ctx):
    saved = _clear_env()
    try:
        os.environ["STUDIO_AGENT"] = "agent:colorbot"
        try:
            cg.require_server(_ns())
            ctx.check(False, "STUDIO_AGENT alone was not refused")
        except cg.GradeError as exc:
            ctx.expect_true("refusal names the requirement",
                            "must name its server" in str(exc), str(exc))
    finally:
        _restore_env(saved)


def test_require_server_allows_an_agent_with_a_port(ctx):
    saved = _clear_env()
    try:
        base = cg.require_server(_ns(agent="colorbot", port=55001))
    finally:
        _restore_env(saved)
    ctx.expect_eq("agent with --port is allowed", base, "http://127.0.0.1:55001")


def test_require_server_allows_an_agent_with_studio_port_env(ctx):
    saved = _clear_env()
    try:
        os.environ["STUDIO_AGENT"] = "agent:colorbot"
        os.environ["STUDIO_PORT"] = "55002"
        base = cg.require_server(_ns(agent="colorbot"))
    finally:
        _restore_env(saved)
    ctx.expect_eq("agent with STUDIO_PORT is allowed", base,
                  "http://127.0.0.1:55002")


def test_port_flag_beats_studio_port_env(ctx):
    saved = _clear_env()
    try:
        os.environ["STUDIO_PORT"] = "9999"
        base, given = cg.resolve_server(_ns(port=55003))
    finally:
        _restore_env(saved)
    ctx.expect_eq("--port wins over STUDIO_PORT", base, "http://127.0.0.1:55003")
    ctx.expect_true("given is True", given, str(given))


def test_studio_port_env_used_without_a_flag(ctx):
    saved = _clear_env()
    try:
        os.environ["STUDIO_PORT"] = "55004"
        base, given = cg.resolve_server(_ns())
    finally:
        _restore_env(saved)
    ctx.expect_eq("STUDIO_PORT alone is honoured", base, "http://127.0.0.1:55004")
    ctx.expect_true("given is True", given, str(given))


def test_url_beats_port_from_either_source(ctx):
    saved = _clear_env()
    try:
        os.environ["STUDIO_PORT"] = "9999"
        base, given = cg.resolve_server(
            _ns(port=55005, url="http://127.0.0.1:55006"))
    finally:
        _restore_env(saved)
    ctx.expect_eq("--url wins over --port and STUDIO_PORT",
                  base, "http://127.0.0.1:55006")
    ctx.expect_true("given is True", given, str(given))


def test_studio_url_env_beats_port(ctx):
    saved = _clear_env()
    try:
        os.environ["STUDIO_URL"] = "http://127.0.0.1:55007"
        base, given = cg.resolve_server(_ns(port=55008))
    finally:
        _restore_env(saved)
    ctx.expect_eq("STUDIO_URL wins over --port", base, "http://127.0.0.1:55007")
    ctx.expect_true("given is True", given, str(given))


# --------------------------------------------------------------------------
# match, preset, grade, and whoami's project_clip: a real server
# --------------------------------------------------------------------------

# Round 1 finding 20: this file's own two-port exclusion list disagreed with
# the three others in the tree and covered neither 7560, 7615 nor 7632. The
# list lives in studio/tests/forbidden-ports.json now and `ports.free_port`
# is the one picker every suite here uses.
_free_port = _shared_free_port


def _make_footage_dir() -> Path:
    """A temp folder of SYMLINKS to content/footage: --footage isolates this
    run's registrations from the founder's real footage folder, but the
    files themselves have to be the real ones, by content hash, or nothing
    in them matches a real clip."""
    tmp = Path(tempfile.mkdtemp(prefix="cinegrade-cli-agent-footage-"))
    if FOOTAGE_SRC.is_dir():
        for p in FOOTAGE_SRC.iterdir():
            if p.is_file() and not p.name.startswith("."):
                (tmp / p.name).symlink_to(p)
    return tmp


def _start_server(data_dir: Path, footage_dir: Path):
    port = _free_port()
    proc = subprocess.Popen(
        [PY, SERVER, "--port", str(port), "--data-dir", str(data_dir),
         "--footage", str(footage_dir)],
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
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


def _cli(args, env=None):
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    return subprocess.run([PY, ENGINE] + args, capture_output=True, text=True,
                          env=full_env)


def _looks_snapshot() -> list[str]:
    d = H.GRADE / "luts" / "looks"
    return sorted(p.name for p in d.iterdir()) if d.is_dir() else []


def test_agent_verbs_and_env_precedence_round_trip(ctx):
    """The new match/preset/grade verbs, the agent-with-no-server refusal at
    the real CLI (not just in process), and STUDIO_PORT / STUDIO_AGENT read
    end to end through a real server. One server, one scenario, in order,
    the same shape as cases_cli_project.py's own round trip test.
    """
    data_dir = Path(tempfile.mkdtemp(prefix="cinegrade-cli-agent-data-"))
    footage_dir = _make_footage_dir()
    out_dir = Path(tempfile.mkdtemp(prefix="cinegrade-cli-agent-match-out-"))
    proc = None
    looks_before = _looks_snapshot()
    try:
        proc, port = _start_server(data_dir, footage_dir)
        ctx.note(f"server pid {proc.pid} on port {port}, data dir {data_dir}")

        # An agent with no --port, no --url and no env at all is refused
        # before any request goes out, at the real CLI, not just in process.
        r = _cli(["whoami", "--agent", "colorbot"])
        ctx.expect_true("unlabelled agent refused at the real CLI",
                        r.returncode != 0, f"exit {r.returncode}")
        ctx.expect_true("refusal names the requirement",
                        "must name its server" in r.stderr, r.stderr[:300])
        ctx.expect_true("no traceback reaches the user",
                        "Traceback" not in r.stderr, r.stderr[:200])

        # STUDIO_AGENT alone (no --agent flag) hits the same refusal.
        r = _cli(["whoami"], env={"STUDIO_AGENT": "agent:colorbot"})
        ctx.expect_true(
            "STUDIO_AGENT alone is refused the same way",
            r.returncode != 0 and "must name its server" in r.stderr,
            r.stderr[:300])

        # STUDIO_PORT (no --port flag) is honoured, for a human and an agent.
        r = _cli(["whoami", "--json"], env={"STUDIO_PORT": str(port)})
        ctx.expect_eq("STUDIO_PORT alone reaches the real server",
                      r.returncode, 0)

        r = _cli(["whoami", "--agent", "colorbot", "--json"],
                 env={"STUDIO_PORT": str(port)})
        ctx.expect_eq("an agent with STUDIO_PORT is allowed through",
                      r.returncode, 0)
        if r.returncode == 0:
            who = json.loads(r.stdout)
            ctx.expect_true(
                "whoami --json has no project_clip before any project is open",
                who.get("project_clip") is None, json.dumps(who))

        # Open a project as the agent, then whoami --json names its clip.
        r = _cli(["project", "open", CLIP, "--agent", "colorbot"],
                 env={"STUDIO_PORT": str(port)})
        ctx.expect_eq("project open (agent): exit code", r.returncode, 0)

        r = _cli(["whoami", "--agent", "colorbot", "--json"],
                 env={"STUDIO_PORT": str(port)})
        ctx.expect_eq("whoami --json (post open): exit code", r.returncode, 0)
        if r.returncode == 0:
            who = json.loads(r.stdout)
            ctx.expect_eq("whoami --json now names the open clip",
                          who.get("project_clip"), CLIP)
        r_text = _cli(["whoami", "--agent", "colorbot"],
                      env={"STUDIO_PORT": str(port)})
        ctx.expect_true(
            "plain whoami shows the clip alongside the project hash",
            f"({CLIP})" in r_text.stdout, r_text.stdout)

        # preset save / load
        preset_file = out_dir / "preset-in.json"
        preset_file.write_text(json.dumps({"primaries": {"saturation": 1.1}}))
        r = _cli(["preset", "save", "g10-test-preset", "-p", str(preset_file),
                  "--comment", "lane G10 test", "--agent", "colorbot",
                  "--port", str(port)])
        ctx.expect_eq("preset save: exit code", r.returncode, 0)
        ctx.expect_true("preset save reports the name",
                        "g10-test-preset" in r.stdout, r.stdout)

        r = _cli(["preset", "load", "g10-test-preset", "--json",
                  "--port", str(port)])
        ctx.expect_eq("preset load --json: exit code", r.returncode, 0)
        if r.returncode == 0:
            loaded = json.loads(r.stdout)
            ctx.expect_eq("preset load: comment round trips",
                          loaded.get("comment"), "lane G10 test")
            ctx.expect_eq(
                "preset load: config round trips",
                loaded.get("config", {}).get("primaries", {}).get("saturation"),
                1.1)

        # grade save / load
        r = _cli(["grade", "save", CLIP, "-p", str(preset_file),
                  "--message", "g10 test grade", "--agent", "colorbot",
                  "--port", str(port)])
        ctx.expect_eq("grade save: exit code", r.returncode, 0)
        ctx.expect_true("grade save reports the clip", CLIP in r.stdout, r.stdout)

        r = _cli(["grade", "load", CLIP, "--json", "--port", str(port)])
        ctx.expect_eq("grade load --json: exit code", r.returncode, 0)
        if r.returncode == 0:
            gload = json.loads(r.stdout)
            ctx.expect_true("grade load reports it exists",
                            gload.get("exists") is True, json.dumps(gload))

        r = _cli(["grade", "load", "no-such-clip.mov", "--port", str(port)])
        ctx.expect_true("loading a grade for an unsaved clip fails cleanly",
                        r.returncode != 0, f"exit {r.returncode}")
        ctx.expect_true("no traceback for an unsaved clip",
                        "Traceback" not in r.stderr, r.stderr[:200])

        # match: always through a temp out_dir, grade/luts/looks untouched.
        ref = REFS / "IMG_2562.PNG"
        r = _cli(["match", str(ref), CLIP, "--time", "0.1",
                  "--name", "g10-test-match", "--out-dir", str(out_dir),
                  "--json", "--agent", "colorbot", "--port", str(port)])
        ctx.expect_eq("match --json: exit code", r.returncode, 0)
        if r.returncode == 0:
            mout = json.loads(r.stdout)
            lut = mout.get("lut") or ""
            ctx.expect_eq("match writes into the given out_dir",
                          str(Path(lut).parent) if lut else "", str(out_dir))
            ctx.expect_true("the cube was actually written",
                            bool(lut) and Path(lut).exists(), lut)

        # a second match, non-JSON: a one line summary naming the look.
        r = _cli(["match", str(ref), CLIP, "--time", "0.1",
                  "--name", "g10-test-match-2", "--out-dir", str(out_dir),
                  "--port", str(port)])
        ctx.expect_eq("match (text): exit code", r.returncode, 0)
        ctx.expect_true("match text summary names the look",
                        "match g10-test-match-2" in r.stdout, r.stdout)
    finally:
        _stop_server(proc)
        shutil.rmtree(data_dir, ignore_errors=True)
        shutil.rmtree(footage_dir, ignore_errors=True)
        shutil.rmtree(out_dir, ignore_errors=True)
        looks_after = _looks_snapshot()
        ctx.expect_eq(
            "grade/luts/looks is unchanged by every match call in this test",
            looks_after, looks_before)


# --------------------------------------------------------------------------
# match --rotate (round 2 tooling note 6): the payload cinegrade.cmd_match
# builds, checked in process by swapping out _studio_call for one that
# records what it was handed rather than opening a socket, the same "no
# server needed for pure logic" style the resolve_server checks above use.
# --------------------------------------------------------------------------

def _match_ns(**kw):
    base = dict(ref="r.png", clip="c.mov", time=0.0, preset=None,
               method="reinhard", rotate=None, strength=1.0,
               luma_preserve=True, ref_crop=None, frame_crop=None,
               name=None, out_dir=None, json=True,
               port=55999, url=None, agent=None, attach=None)
    base.update(kw)
    return argparse.Namespace(**base)


def _captured_match_payload(**ns_kw):
    captured = {}
    real = cg._studio_call
    try:
        cg._studio_call = lambda base, path, method="GET", payload=None, \
            headers=None: (captured.__setitem__("payload", payload)
                           or {"ok": True, "name": "x", "lut": "/tmp/x.cube",
                              "warnings": []})
        cg.cmd_match(_match_ns(**ns_kw))
    finally:
        cg._studio_call = real
    return captured.get("payload") or {}


def test_match_rotate_flag_is_sent_as_the_rotation_field(ctx):
    payload = _captured_match_payload(rotate="180")
    ctx.expect_eq("--rotate 180 is sent as the rotation field",
                  payload.get("rotation"), "180")


def test_match_rotation_defaults_to_auto_with_no_flag_and_no_preset(ctx):
    payload = _captured_match_payload()
    ctx.expect_eq("no --rotate, no preset: rotation is auto",
                  payload.get("rotation"), "auto")


def test_match_rotation_falls_back_to_the_configs_own_rotation(ctx):
    preset_file = Path(tempfile.mkdtemp(prefix="cinegrade-match-rot-")) / "p.json"
    preset_file.write_text(json.dumps({"rotation": "90"}))
    payload = _captured_match_payload(preset=str(preset_file))
    ctx.expect_eq("no --rotate, preset carries rotation 90: honoured",
                  payload.get("rotation"), "90")


def register(suite):
    g = "cli_agent"
    suite.add(g, "resolve_server_defaults_to_7431_unlabelled",
              test_resolve_server_defaults_to_7431_unlabelled,
              doc="nothing set at all still resolves to the human 7431 "
                  "default, checked without opening a socket")
    suite.add(g, "require_server_allows_a_human_with_no_agent",
              test_require_server_allows_a_human_with_no_agent,
              doc="a human with no --agent keeps today's silent 7431 default")
    suite.add(g, "require_server_refuses_an_unlabelled_agent",
              test_require_server_refuses_an_unlabelled_agent,
              doc="--agent with no --port/--url/env is refused before any "
                  "request goes out")
    suite.add(g, "require_server_refuses_via_studio_agent_env_too",
              test_require_server_refuses_via_studio_agent_env_too,
              doc="STUDIO_AGENT alone hits the same refusal as --agent")
    suite.add(g, "require_server_allows_an_agent_with_a_port",
              test_require_server_allows_an_agent_with_a_port,
              doc="an agent that names --port is let through")
    suite.add(g, "require_server_allows_an_agent_with_studio_port_env",
              test_require_server_allows_an_agent_with_studio_port_env,
              doc="an agent that names STUDIO_PORT is let through")
    suite.add(g, "port_flag_beats_studio_port_env",
              test_port_flag_beats_studio_port_env,
              doc="--port wins over STUDIO_PORT when both are given")
    suite.add(g, "studio_port_env_used_without_a_flag",
              test_studio_port_env_used_without_a_flag,
              doc="STUDIO_PORT alone is honoured with no --port")
    suite.add(g, "url_beats_port_from_either_source",
              test_url_beats_port_from_either_source,
              doc="--url wins over --port and STUDIO_PORT")
    suite.add(g, "studio_url_env_beats_port",
              test_studio_url_env_beats_port,
              doc="STUDIO_URL wins over --port with no --url flag")
    suite.add(g, "agent_verbs_and_env_precedence_round_trip",
              test_agent_verbs_and_env_precedence_round_trip,
              doc="match/preset/grade verbs, the real-CLI agent refusal, "
                  "and whoami's project_clip, against a real server")
    suite.add(g, "match_rotate_flag_is_sent_as_the_rotation_field",
              test_match_rotate_flag_is_sent_as_the_rotation_field,
              doc="--rotate 180 lands on the payload's rotation field")
    suite.add(g, "match_rotation_defaults_to_auto_with_no_flag_and_no_preset",
              test_match_rotation_defaults_to_auto_with_no_flag_and_no_preset,
              doc="no --rotate, no preset rotation: auto")
    suite.add(g, "match_rotation_falls_back_to_the_configs_own_rotation",
              test_match_rotation_falls_back_to_the_configs_own_rotation,
              doc="no --rotate: the --preset config's own rotation wins "
                  "over auto")
