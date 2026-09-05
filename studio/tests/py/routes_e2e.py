#!/usr/bin/env python3
"""Contract C1 end to end: the real server, the real routes, over real HTTP.

    .venv/bin/python studio/tests/py/routes_e2e.py

Deliberately NOT named test_*.py, so `unittest discover` does not pick it up:
this one starts a server, needs ffprobe and a clip in content/footage, and
takes seconds rather than milliseconds. The unittest file beside it is the fast
gate; this is the proof that the routes themselves work when they are spoken to
over a socket.

The server it starts runs on a random high port with its own temporary data
directory, so it never touches port 7431 and never opens the real database. It
is stopped by the process id captured at spawn, never by a name pattern.
"""

from __future__ import annotations

import json
import os
import random
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONTENT = HERE.parent.parent.parent
PYTHON = CONTENT / ".venv" / "bin" / "python"

FAILED: list[str] = []
STEPS = 0


def free_port() -> int:
    for _ in range(60):
        port = random.randint(20000, 60000)
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise SystemExit("no free port")


def call(base: str, path: str, payload=None, method=None):
    url = base + path
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method=method or ("POST" if data else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()
        try:
            out = json.loads(body)
        except json.JSONDecodeError:
            out = {"error": body[:300]}
        out["status"] = exc.code
        return out


def check(label: str, ok: bool, detail: str = "") -> None:
    global STEPS
    STEPS += 1
    mark = "ok  " if ok else "FAIL"
    print(f"  {mark} {label}" + (f"   {detail}" if detail else ""))
    if not ok:
        FAILED.append(label + (f" ({detail})" if detail else ""))


def start(port: int, data_dir: Path):
    proc = subprocess.Popen(
        [str(PYTHON), "studio/server.py", "--port", str(port),
         "--data-dir", str(data_dir)],
        cwd=str(CONTENT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True)
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 40
    while time.time() < deadline:
        if proc.poll() is not None:
            print(proc.stdout.read())
            raise SystemExit("the server exited before it was ready")
        try:
            urllib.request.urlopen(base + "/api/state", timeout=2).read()
            return proc, base
        except Exception:                                     # noqa: BLE001
            time.sleep(0.25)
    proc.kill()
    raise SystemExit("the server never answered /api/state")


def stop(proc) -> None:
    """By the process id we captured at spawn, and nothing else."""
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="studio-routes-e2e-"))
    port = free_port()
    print(f"data dir {tmp}\nport     {port}")
    proc, base = start(port, tmp)
    print(f"server   pid {proc.pid}")
    try:
        clips = call(base, "/api/clips").get("clips") or []
        if not clips:
            print("no clips in content/footage, nothing to open")
            return 2
        clip = clips[0]["name"]
        print(f"clip     {clip}\n")

        print("whoami and an empty workspace")
        who = call(base, "/api/whoami")
        check("whoami answers with a default author", who.get("by") == "cli", who.get("by"))
        check("nothing is open yet", who.get("project") is None)
        empty = call(base, "/api/project")
        check("GET /api/project says so plainly", empty.get("open") is False,
              empty.get("note", ""))

        print("\nopen")
        proj = call(base, "/api/project/open", {"clip": clip, "by": "studio"})
        key = proj.get("key")
        check("open returns the project", bool(key), str(key))
        check("it has a root commit", bool(proj.get("head")), str(proj.get("head")))
        check("on branch main", proj.get("branch") == "main")
        check("rotation starts at auto", proj.get("rotation") == "auto")
        check("it reports the frame size", bool(proj.get("dims")),
              json.dumps(proj.get("dims")))
        check("the workspace followed it",
              call(base, "/api/whoami").get("project") == key)
        sess = call(base, "/api/session")
        check("the session carries the project", sess.get("project") == key)
        check("the session carries the head", sess.get("head") == proj.get("head"))
        check("the session carries the branch", sess.get("branch") == "main")
        check("the session carries the rotation", sess.get("rotation") == "auto")

        print("\nedits through POST /api/session, as the tab makes them")
        cfg = json.loads(json.dumps(proj["config"]))
        cfg["primaries"]["saturation"] = 1.2
        one = call(base, "/api/session",
                   {"config": cfg, "replace": True, "by": "studio", "clip": clip})
        check("a session write moves the head", one.get("head") != proj.get("head"))
        check("by is still what the caller sent", one.get("by") == "studio")
        again = call(base, "/api/session",
                     {"config": cfg, "replace": True, "by": "studio", "clip": clip})
        check("republishing the same config is not a commit",
              again.get("head") == one.get("head"))
        cfg2 = json.loads(json.dumps(cfg))
        cfg2["primaries"]["saturation"] = 1.4
        two = call(base, "/api/session",
                   {"config": cfg2, "replace": True, "by": "agent:lane",
                    "clip": clip, "time": 4.25})
        check("a second edit commits", two.get("head") != one.get("head"))
        check("the playhead moved with it", abs(float(two.get("time", 0)) - 4.25) < 1e-6)

        log = call(base, "/api/project/log")
        msgs = [c["message"] for c in log["commits"]]
        authors = [c["author"] for c in log["commits"]]
        check("the log has three commits", log.get("total") == 3, json.dumps(msgs))
        check("with readable messages",
              msgs[0] == "saturation 1.20 to 1.40" and msgs[1] == "saturation 1.00 to 1.20",
              json.dumps(msgs))
        check("and the authors that made them",
              authors[:2] == ["agent:lane", "studio"], json.dumps(authors))
        check("the head row is marked",
              [c["is_head"] for c in log["commits"]] == [True, False, False])

        print("\nundo and redo")
        back = call(base, "/api/project/undo", {"by": "studio"})
        check("undo moves the head back", back.get("head") == one.get("head"))
        check("and the live config with it",
              back["config"]["primaries"]["saturation"] == 1.2)
        check("the session sees it too",
              call(base, "/api/session").get("head") == one.get("head"))
        fwd = call(base, "/api/project/redo", {"by": "studio"})
        check("redo returns", fwd.get("head") == two.get("head"))
        check("redo at the tip is a no op and says so",
              call(base, "/api/project/redo", {}).get("moved") is False)
        check("nothing was deleted",
              call(base, "/api/project/log").get("total") == 3)

        print("\nthe long poll fires on a history move, carrying the new config")
        import threading
        seen = {}

        def waiter(since):
            seen.update(call(base, f"/api/session/wait?since={since}&timeout=20"))

        rev = call(base, "/api/session")["rev"]
        th = threading.Thread(target=waiter, args=(rev,))
        th.start()
        time.sleep(0.4)
        moved = call(base, "/api/project/undo", {"by": "agent:lane"})
        th.join(25)
        check("the wait returned", bool(seen), json.dumps(list(seen))[:80])
        check("past the revision it was waiting on", seen.get("rev", 0) > rev)
        check("with the new head", seen.get("head") == moved.get("head"))
        check("and the config that goes with it",
              seen["config"]["primaries"]["saturation"]
              == moved["config"]["primaries"]["saturation"])
        check("named the author who moved it", seen.get("by") == "agent:lane")
        call(base, "/api/project/redo", {})

        print("\na caller supplied message names the commit")
        cfgp = json.loads(json.dumps(proj["config"]))
        cfgp["look"]["mix"] = 0.6
        call(base, "/api/session", {"config": cfgp, "replace": True,
                                    "by": "studio", "clip": clip,
                                    "message": "loaded preset nature_cinema"})
        check("the message is used verbatim",
              call(base, "/api/project/log")["commits"][0]["message"]
              == "loaded preset nature_cinema")
        call(base, "/api/project/undo", {})
        call(base, "/api/project/checkout", {"commit": two["head"]})

        print("\ncheckout an older commit, then edit: it forks")
        root = log["commits"][-1]["short"]
        before_fork = call(base, "/api/project/log")["total"]
        at_root = call(base, "/api/project/checkout", {"commit": root})
        check("checkout takes a short id", at_root.get("head") == root)
        cfg3 = json.loads(json.dumps(proj["config"]))
        cfg3["grain"]["enabled"] = True
        forked = call(base, "/api/session",
                      {"config": cfg3, "replace": True, "by": "studio", "clip": clip})
        check("editing from an older point starts fork-1",
              forked.get("branch") == "fork-1", str(forked.get("branch")))
        tree = call(base, "/api/project/log")
        check("both branches are in the tree",
              [b["name"] for b in tree["branches"]] == ["main", "fork-1"],
              json.dumps([b["name"] for b in tree["branches"]]))
        check("the forward chain is still on main",
              tree["total"] == before_fork + 1
              and any(c["message"] == "saturation 1.20 to 1.40"
                      for c in tree["commits"]),
              f'{tree["total"]} commits')
        check("the fork's message reads", tree["commits"][0]["message"] == "grain on",
              tree["commits"][0]["message"])

        print("\nexplicit fork and branch listing")
        named = call(base, "/api/project/fork", {"name": "night"})
        check("a named fork is created", named.get("branch") == "night")
        check("it shows in the branch list",
              [b["name"] for b in named["branches"]] == ["main", "fork-1", "night"])
        check("a duplicate name is refused",
              call(base, "/api/project/fork", {"name": "night"}).get("status") == 400)

        print("\nrotation and time are project fields, not commits")
        before = call(base, "/api/project/log")["total"]
        rot = call(base, "/api/project/rotation", {"rotation": "90"})
        check("rotation is set", rot.get("rotation") == "90")
        # 90 is the RAW frame turned on its side, not the autorotated one: a
        # clip whose own tag already rotates it (this one) would otherwise be
        # measured twice.
        raw = clips[0].get("raw") or {}
        check("and the frame size follows it",
              rot["dims"]["width"] == raw.get("height")
              and rot["dims"]["height"] == raw.get("width"),
              f'{json.dumps(rot.get("dims"))} from raw {json.dumps(raw)}')
        check("the session reports it",
              call(base, "/api/session").get("rotation") == "90")
        check("a rotation nobody supports is refused",
              call(base, "/api/project/rotation", {"rotation": "45"}).get("status") == 400)
        check("the old autorotate word still works",
              call(base, "/api/project/rotation",
                   {"autorotate": False}).get("rotation") == "0")
        call(base, "/api/project/rotation", {"rotation": "auto"})
        call(base, "/api/project/time", {"time": 2.5})
        check("time is set",
              abs(call(base, "/api/project")["time"] - 2.5) < 1e-6)
        check("neither made a commit",
              call(base, "/api/project/log")["total"] == before)

        print("\nextras")
        crops = {"ref.png": {"ref": [0.1, 0.1, 0.6, 0.6]}}
        ex = call(base, "/api/project/extra",
                  {"name": "match_crops", "value": crops})
        check("an extra is stored", ex["extras"]["match_crops"] == crops)
        check("and read back on the project",
              call(base, "/api/project")["extras"]["match_crops"] == crops)

        print("\nthe old grades row follows HEAD")
        head_cfg = call(base, "/api/project")["config"]
        check("a clip graded through the session is in GET /api/grades",
              any(g["clip_key"] == key
                  for g in call(base, "/api/grades")["grades"]))
        copied = call(base, "/api/grade/copy",
                      {"from": key, "to": "f" * 32})
        check("and POST /api/grade/copy reads HEAD out of that row",
              json.dumps(copied.get("config")) == json.dumps(head_cfg),
              str(copied.get("error", ""))[:120])

        print("\nthe legacy grade routes still work, and now commit")
        head_before = call(base, "/api/project")["head"]
        cfg4 = json.loads(json.dumps(proj["config"]))
        cfg4["detail"]["sharpen"] = 0.4
        put = call(base, "/api/grade", {"clip": clip, "config": cfg4}, method="PUT")
        check("PUT /api/grade answers with its old fields",
              "key" in put and "updated_at" in put, json.dumps(put)[:120])
        check("and records a commit", put.get("head") not in (None, head_before))
        got = call(base, f"/api/grade?clip={clip}")
        check("GET /api/grade returns HEAD", got.get("exists") is True
              and got["config"]["detail"]["sharpen"] == 0.4)
        check("its message says what it was",
              call(base, "/api/project/log")["commits"][0]["message"] == "saved grade")
        check("the old grades list still lists the clip",
              any(g["clip_key"] == key for g in call(base, "/api/grades")["grades"]))

        print("\nDELETE /api/grade still means the clip reads as ungraded")
        tree_before = call(base, "/api/project/log")["total"]
        call(base, f"/api/grade?clip={clip}", method="DELETE")
        after_delete = call(base, f"/api/grade?clip={clip}")
        defaults = call(base, "/api/state")["defaults"]
        check("the config is back to the engine defaults",
              json.dumps(after_delete["config"]) == json.dumps(defaults))
        check("the history was not destroyed by it",
              call(base, "/api/project/log")["total"] == tree_before + 1)
        check("the reset is a commit, and says so",
              call(base, "/api/project/log")["commits"][0]["message"]
              == "reset to defaults")
        check("and the clip is gone from the saved grades list",
              not any(g["clip_key"] == key
                      for g in call(base, "/api/grades")["grades"]))
        # put the sharpen back so the restart check below has something to see
        call(base, "/api/grade", {"clip": clip, "config": cfg4}, method="PUT")

        print("\na restart restores the open project")
        state_before = call(base, "/api/project")
        tree_size = call(base, "/api/project/log")["total"]
        stop(proc)
        proc, base = start(port, tmp)
        print(f"server   pid {proc.pid} (restarted)")
        after = call(base, "/api/session")
        check("the session came back on the same project",
              after.get("project") == state_before["key"])
        check("on the same commit", after.get("head") == state_before["head"])
        check("with the same config",
              after["config"]["detail"]["sharpen"] == 0.4)
        check("the same clip and playhead",
              after.get("clip") == clip and abs(after["time"] - 2.5) < 1e-6,
              f'{after.get("clip")} {after.get("time")}')
        check("and revision 0, so nobody is woken by the restart",
              after.get("rev") == 0, str(after.get("rev")))
        check("the tree survived",
              call(base, "/api/project/log")["total"] == tree_size)
    finally:
        stop(proc)
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{STEPS - len(FAILED)} of {STEPS} checks passed")
    for f in FAILED:
        print("  FAILED " + f)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
