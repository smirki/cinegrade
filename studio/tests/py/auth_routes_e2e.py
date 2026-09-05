#!/usr/bin/env python3
"""Contract C1 with logins ON: who a commit says made it, and who is refused.

    .venv/bin/python studio/tests/py/auth_routes_e2e.py

Same shape as routes_e2e.py beside it: a real server on a random high port
with its own temporary data directory, stopped by the process id captured at
spawn. It creates one account inside that temporary directory and nowhere
else, so nothing here touches the real accounts database.

What it proves, and why each one matters:

* An unauthenticated request to a project route is refused, so the history is
  not a hole in the login wall.
* The author on a commit is the ACCOUNT name, not the `by` label in the body:
  a signed in browser cannot sign somebody else's name to an edit.
* A cookie authenticated write with a cross site fetch header is refused by
  the same CSRF rule /api/session already had.
* An agent token still works and signs with the account it belongs to.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib import error, request

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from routes_e2e import FAILED, check, free_port, start, stop  # noqa: E402

CONTENT = HERE.parent.parent.parent
PYTHON = CONTENT / ".venv" / "bin" / "python"
NAME = "p1lane"
PASSWORD = "a-long-enough-test-password"


def call(base, path, payload=None, method=None, headers=None):
    data = json.dumps(payload).encode() if payload is not None else None
    hdrs = dict(headers or {})
    if data:
        hdrs["Content-Type"] = "application/json"
    req = request.Request(base + path, data=data, headers=hdrs,
                          method=method or ("POST" if data else "GET"))
    try:
        with request.urlopen(req, timeout=60) as r:
            body = json.loads(r.read().decode())
            body["_cookie"] = r.headers.get("Set-Cookie", "")
            return body
    except error.HTTPError as exc:
        try:
            out = json.loads(exc.read().decode())
        except Exception:                                     # noqa: BLE001
            out = {}
        out["status"] = exc.code
        return out


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="studio-auth-e2e-"))
    port = free_port()
    made = subprocess.run(
        [str(PYTHON), "studio/server.py", "--data-dir", str(tmp),
         "--create-user", NAME, "--role", "admin", "--password-stdin"],
        cwd=str(CONTENT), input=PASSWORD + "\n", capture_output=True, text=True)
    print(made.stdout.strip() or made.stderr.strip())
    proc, base = start_auth(port, tmp)
    print(f"server   pid {proc.pid} (logins on)\n")
    try:
        clips = request.urlopen(base + "/api/state", timeout=10)
        clips.read()
    except error.HTTPError as exc:
        check("the app itself is behind the login", exc.code == 401, str(exc.code))
    try:
        print("refused without a session")
        check("GET /api/project is refused",
              call(base, "/api/project").get("status") == 401)
        check("POST /api/project/open is refused",
              call(base, "/api/project/open", {"clip": "x"}).get("status") == 401)
        check("GET /api/whoami is refused",
              call(base, "/api/whoami").get("status") == 401)

        print("\nsigned in")
        login = call(base, "/api/auth/login",
                     {"username": NAME, "password": PASSWORD})
        cookie = (login.get("_cookie") or "").split(";")[0]
        check("the account signed in", bool(cookie), cookie[:24])
        session = {"Cookie": cookie, "Sec-Fetch-Site": "same-origin"}
        who = call(base, "/api/whoami", headers=session)
        check("whoami names the account", who.get("user") == NAME, str(who.get("user")))
        check("and signs writes as that account", who.get("by") == NAME)

        clip = (call(base, "/api/clips", headers=session).get("clips") or [{}])[0].get("name")
        if not clip:
            print("no clips in content/footage, stopping after the auth checks")
            return 1 if FAILED else 0
        proj = call(base, "/api/project/open", {"clip": clip}, headers=session)
        check("the account can open a project", bool(proj.get("key")))

        cfg = json.loads(json.dumps(proj["config"]))
        cfg["primaries"]["saturation"] = 1.25
        call(base, "/api/session",
             {"config": cfg, "replace": True, "by": "somebody-else",
              "clip": clip}, headers=session)
        top = call(base, "/api/project/log", headers=session)["commits"][0]
        check("the commit is signed with the ACCOUNT, not the by label",
              top["author"] == NAME, top["author"])
        check("and still reads well", top["message"] == "saturation 1.00 to 1.25",
              top["message"])

        print("\nthe CSRF rule covers the new routes too")
        cross = {"Cookie": cookie, "Sec-Fetch-Site": "cross-site",
                 "Origin": "http://evil.example"}
        check("a cross site undo is refused",
              call(base, "/api/project/undo", {}, headers=cross).get("status") == 403)
        check("a cross site session write is refused the same way",
              call(base, "/api/session", {"config": cfg},
                   headers=cross).get("status") == 403)

        print("\nan agent token is an account, and signs like one")
        token = call(base, "/api/auth/token", {"label": "p1"}, headers=session)
        bearer = {"Authorization": "Bearer " + token.get("token", "")}
        cfg["primaries"]["saturation"] = 1.35
        call(base, "/api/session",
             {"config": cfg, "replace": True, "by": "agent:lane", "clip": clip},
             headers=bearer)
        top = call(base, "/api/project/log", headers=bearer)["commits"][0]
        check("a token write is signed with its account", top["author"] == NAME,
              top["author"])
        check("the tree is shared, not per account",
              call(base, "/api/project/log", headers=session)["total"]
              == call(base, "/api/project/log", headers=bearer)["total"])
    finally:
        stop(proc)
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    print("")
    for f in FAILED:
        print("  FAILED " + f)
    print("no failures" if not FAILED else f"{len(FAILED)} failed")
    return 1 if FAILED else 0


def start_auth(port: int, tmp: Path):
    """start() from routes_e2e, but the readiness probe expects a 401."""
    import time
    proc = subprocess.Popen(
        [str(PYTHON), "studio/server.py", "--port", str(port),
         "--data-dir", str(tmp), "--auth"],
        cwd=str(CONTENT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True)
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 40
    while time.time() < deadline:
        if proc.poll() is not None:
            print(proc.stdout.read())
            raise SystemExit("the server exited before it was ready")
        try:
            request.urlopen(base + "/api/state", timeout=2).read()
            return proc, base
        except error.HTTPError:
            return proc, base            # 401 means it is up and gated
        except Exception:                                     # noqa: BLE001
            time.sleep(0.25)
    proc.kill()
    raise SystemExit("the server never came up")


if __name__ == "__main__":
    sys.exit(main())
