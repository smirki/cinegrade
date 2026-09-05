#!/usr/bin/env python3
"""Contract E1: the library, sharing, tenancy and the grading permission rule.

Run from content/:

    .venv/bin/python -m unittest discover -s studio/tests/py

Everything here runs against a REAL server on a random high port with its own
temporary data directory, started once for the module and stopped by the
process id captured at spawn. Nothing in this file imports `db`, `auth` or
`library` directly, and that is on purpose: the other test modules in this
folder point the shared `db` module at their own temporary directory at import
time, and `unittest discover` imports every module before it runs any of them,
so a module level data directory here would be a coin toss over which one won.
Talking to a separate process over HTTP has no such problem, and it also means
these tests exercise the routes lane L2 will actually call rather than the
functions underneath them.

The accounts, the org, the team and the clips all live inside that temporary
directory. The real studio/data is never opened, and no account is ever
created outside the temp directory.

What each group proves, in plain terms:

* isolation: two accounts on one server cannot see each other's files.
* sharing: a folder handed to a person, and a folder handed to a team, show up
  for the other side and can be browsed into.
* the money rule: a viewer can look and read the history but cannot commit; an
  editor can, and the owner sees the editor's name on the commit.
* renames: renaming through the app carries the share with it; renaming behind
  the app's back is reported as a broken share instead of quietly vanishing.
* trash: nothing is deleted, and what went in comes back out where it was.
* tenancy: an account in another org is not a person you can share with, and
  cannot see anything.
* the feed: every one of those events is in the activity list with who did it.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import random
import socket
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

PASSWORD = "a-long-enough-test-password"
STATE: dict = {}


# --------------------------------------------------------------------------
# a server of our own
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


def tiny_clip(path: Path, colour: str, seconds: float) -> Path:
    """A real, tiny video file, because the upload route runs ffprobe on it.

    Bounded by -t, like every other ffmpeg call in this repo. Different
    colours and lengths for different files on purpose: a clip's project is
    keyed by its CONTENT, so two byte identical uploads would share one
    history and one test would then be looking at another test's commits.
    """
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
         "-i", f"color=c={colour}:s=64x64:r=10", "-t", str(seconds),
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
        check=True, capture_output=True)
    return path


def setUpModule() -> None:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise unittest.SkipTest("ffmpeg and ffprobe are needed for uploads")
    STATE["tmp"] = Path(tempfile.mkdtemp(prefix="studio-library-test-"))
    STATE["clips"] = STATE["tmp"] / "sources"
    STATE["clips"].mkdir(parents=True, exist_ok=True)
    for name, colour, secs in (("red.mp4", "red", 1.0),
                               ("green.mp4", "green", 1.2),
                               ("blue.mp4", "blue", 1.4),
                               ("grey.mp4", "gray", 1.6)):
        tiny_clip(STATE["clips"] / name, colour, secs)

    # One org named `other` so tenancy has something to be isolated from, one
    # team in the default org, three accounts.
    cli("--create-org", "other")
    for who, org in (("alice", None), ("bob", None), ("carol", "other")):
        args = ["--create-user", who, "--role", "user", "--password-stdin"]
        if org:
            args += ["--org", org]
        out = cli(*args, stdin=PASSWORD + "\n")
        if out.returncode != 0:
            raise unittest.SkipTest(f"could not create {who}: {out.stderr}")
    cli("--create-team", "crew", "--org", "default")
    cli("--add-to-team", "bob", "crew")

    port = free_port()
    STATE["port"] = port
    STATE["base"] = f"http://127.0.0.1:{port}"
    STATE["proc"] = subprocess.Popen(
        [str(PYTHON), SERVER, "--port", str(port), "--data-dir",
         str(STATE["tmp"]), "--auth"],
        cwd=str(CONTENT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True)
    deadline = time.time() + 40
    ready = False
    while time.time() < deadline:
        if STATE["proc"].poll() is not None:
            raise unittest.SkipTest("the server exited before it was ready")
        try:
            urllib.request.urlopen(STATE["base"] + "/api/state", timeout=2).read()
            ready = True
            break
        except urllib.error.HTTPError:
            ready = True                     # 401 means it is up and gated
            break
        except Exception:                                      # noqa: BLE001
            time.sleep(0.25)
    if not ready:
        raise unittest.SkipTest("the server never came up")
    for who in ("alice", "bob", "carol"):
        STATE[who] = login(who)


def tearDownModule() -> None:
    proc = STATE.get("proc")
    if proc is not None:
        if proc.poll() is None:
            proc.terminate()                 # by the pid captured at spawn
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
        if proc.stdout is not None:
            proc.stdout.close()
    if STATE.get("tmp"):
        shutil.rmtree(STATE["tmp"], ignore_errors=True)


# --------------------------------------------------------------------------
# talking to it
# --------------------------------------------------------------------------

def call(session, path, payload=None, method=None, raw=None, headers=None):
    """One request. Returns the decoded JSON with `status` on a refusal."""
    hdrs = dict(headers or {})
    if session:
        hdrs["Cookie"] = session["cookie"]
        hdrs["Sec-Fetch-Site"] = "same-origin"      # the CSRF rule
    data = raw
    if payload is not None:
        data = json.dumps(payload).encode()
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(STATE["base"] + path, data=data,
                                 headers=hdrs,
                                 method=method or ("POST" if data else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            body = r.read()
            if (r.headers.get("Content-Type") or "").startswith("application/json"):
                out = json.loads(body.decode())
                if isinstance(out, dict):
                    out["_set_cookie"] = r.headers.get("Set-Cookie", "")
                return out
            return {"bytes": len(body),
                    "content_type": r.headers.get("Content-Type", "")}
    except urllib.error.HTTPError as exc:
        raw_body = exc.read().decode(errors="replace")
        try:
            out = json.loads(raw_body)
        except json.JSONDecodeError:
            out = {"error": raw_body[:300]}
        out["status"] = exc.code
        return out


def login(name: str) -> dict:
    out = call(None, "/api/auth/login",
               {"username": name, "password": PASSWORD})
    cookie = (out.get("_set_cookie") or "").split(";")[0]
    who = call({"cookie": cookie}, "/api/auth/me")
    return {"name": name, "cookie": cookie,
            "id": int((who.get("user") or {}).get("id", 0))}


def upload(session, source: Path, root: str = "mine", path: str = "",
           as_name: str | None = None, query: bool = True) -> dict:
    """POST /api/upload with the raw body, the way the client does it.

    `query=False` is the shape every client used before this arc: no root and
    no path at all. It has to keep landing at the top of the caller's own
    library, or the old page and the old scripts break.
    """
    body = source.read_bytes()
    url = "/api/upload"
    if query:
        url += "?" + urllib.parse.urlencode({"root": root, "path": path})
    return call(session, url, raw=body, method="POST",
                headers={"X-File-Name": urllib.parse.quote(as_name
                                                           or source.name),
                         "Content-Type": "application/octet-stream"})


def library(session, root="mine", path=""):
    q = urllib.parse.urlencode({"root": root, "path": path})
    return call(session, f"/api/library?{q}")


def names(listing) -> list[str]:
    return ([f["name"] for f in listing.get("folders") or []]
            + [f["name"] for f in listing.get("files") or []])


class Library(unittest.TestCase):
    """One server, one story, in order. The numbers fix the order."""

    maxDiff = None

    # --- each account has its own library ------------------------------

    def test_01_a_new_account_starts_empty(self):
        for who in ("alice", "bob"):
            out = library(STATE[who])
            self.assertEqual(out["root"], "mine")
            self.assertEqual(out["role"], "owner")
            self.assertTrue(out["can_edit"])
            self.assertEqual(names(out), [], f"{who} started with files")
            self.assertEqual(out["owner"], who)

    def test_02_upload_lands_in_my_library_only(self):
        # No root, no path: exactly the request every client made before this
        # arc, which still has to land at the top of this account's library.
        out = upload(STATE["alice"], STATE["clips"] / "red.mp4", query=False)
        self.assertNotIn("status", out, out.get("error"))
        self.assertEqual(out["path"], "red.mp4")
        self.assertEqual(out["root"], "mine")
        self.assertEqual(names(library(STATE["alice"])), ["red.mp4"])
        # And nobody else's.
        self.assertEqual(names(library(STATE["bob"])), [])

    def test_03_a_listed_clip_carries_what_the_pane_needs(self):
        entry = library(STATE["alice"])["files"][0]
        self.assertEqual(entry["kind"], "file")
        self.assertEqual(entry["owner_id"], STATE["alice"]["id"])
        self.assertEqual(len(entry["key"]), 32)
        self.assertGreater(entry["bytes"], 0)
        self.assertAlmostEqual(entry["duration"], 1.0, delta=0.3)
        self.assertEqual([entry["width"], entry["height"]], [64, 64])
        self.assertIsNone(entry["project"])          # nobody has graded it

    def test_04_folders_and_upload_into_one(self):
        made = call(STATE["alice"], "/api/library/mkdir",
                    {"root": "mine", "path": "", "name": "Shoot"})
        self.assertEqual(made["path"], "Shoot")
        out = upload(STATE["alice"], STATE["clips"] / "green.mp4",
                     path="Shoot")
        self.assertEqual(out["path"], "Shoot/green.mp4")
        inside = library(STATE["alice"], path="Shoot")
        self.assertEqual(names(inside), ["green.mp4"])
        self.assertEqual(inside["parent"], "")
        self.assertEqual([c["path"] for c in inside["crumbs"]], ["", "Shoot"])
        top = library(STATE["alice"])
        self.assertEqual(top["folders"][0]["name"], "Shoot")
        self.assertEqual(top["folders"][0]["count"], 1)

    def test_05_a_path_that_climbs_out_is_refused(self):
        for bad in ("../../etc", "..", "Shoot/../../..", "a/../../b"):
            out = library(STATE["alice"], path=bad)
            self.assertIn(out.get("status"), (400, 403),
                          f"{bad} was not refused: {out}")
            self.assertIn("..", out.get("error", ""))
        # An absolute path is read as relative to the library, never as a
        # path on the machine: /etc is the folder called etc in this account's
        # own library, which does not exist.
        out = library(STATE["alice"], path="/etc")
        self.assertEqual(out["path"], "etc")
        self.assertTrue(out.get("missing"))
        self.assertEqual(names(out), [])

    def test_06_another_library_is_not_readable_without_a_grant(self):
        out = library(STATE["bob"], root=f"user:{STATE['alice']['id']}")
        self.assertEqual(out.get("status"), 403)
        self.assertIn("shared", out["error"])

    # --- the read guard -------------------------------------------------

    def test_07_a_clip_only_i_opened_is_not_readable_by_anybody_else(self):
        """Naming somebody else's clip must not hand back its pixels.

        Clips are addressed by bare file name, and the name table is one
        table for the whole server, so the moment alice opens `red.mp4` that
        name resolves for every caller on it. What stops bob is the library
        read rule, not the name being secret.
        """
        opened = call(STATE["alice"], "/api/project/open",
                      {"root": "mine", "path": "red.mp4"})
        self.assertNotIn("status", opened, opened.get("error"))
        STATE["red"] = opened["name"]
        self.assertEqual(STATE["red"], "red.mp4")
        # The owner still reads her own frame, so this is a rule and not a
        # blanket refusal.
        mine = call(STATE["alice"], "/api/frame",
                    {"clip": STATE["red"], "time": 0, "width": 64})
        self.assertGreater(mine.get("bytes", 0), 0)

        message = ("that item is in another account's library and has not "
                   "been shared with you")
        for path, payload in (
                ("/api/frame", {"clip": STATE["red"], "time": 0, "width": 64}),
                ("/api/stats", {"clip": STATE["red"], "time": 0, "width": 64}),
                ("/api/scope", {"clip": STATE["red"], "time": 0, "width": 64}),
                ("/api/source", {"clip": STATE["red"], "time": 0, "width": 64}),
                ("/api/range", {"clip": STATE["red"], "time": 0,
                                "duration": 0.2, "width": 64}),
                ("/api/play/prepare", {"clip": STATE["red"], "time": 0,
                                       "duration": 0.2, "width": 64}),
                ("/api/proxy/prepare", {"clip": STATE["red"], "width": 64}),
                ("/api/render", {"clip": STATE["red"], "duration": 0.2}),
                ("/api/match", {"clip": STATE["red"], "ref": "none.png"}),
                ("/api/project/open", {"clip": STATE["red"]})):
            out = call(STATE["bob"], path, payload, method="POST")
            self.assertEqual(out.get("status"), 403, f"{path}: {out}")
            self.assertEqual(out["error"], message, path)
        for path in ("/api/thumb?clip=red.mp4&t=0&w=32",
                     "/api/range/limit?clip=red.mp4&width=64"):
            out = call(STATE["bob"], path)
            self.assertEqual(out.get("status"), 403, f"{path}: {out}")
            self.assertEqual(out["error"], message, path)

    def test_08_a_playback_key_is_not_a_way_in(self):
        """The segment and proxy GETs carry a cache key and no clip name.

        They are plain GETs so a <video src> can point straight at them,
        which means the key is the whole request. The key alice gets back
        from prepare must not work for bob, and a key this server never
        minted must not work for anybody: the segment cache is one folder for
        the whole machine, so an unknown key may name a file some other
        account built.
        """
        prepared = call(STATE["alice"], "/api/play/prepare",
                        {"clip": STATE["red"], "time": 0, "duration": 0.2,
                         "width": 64})
        self.assertNotIn("status", prepared, prepared.get("error"))
        stolen = call(STATE["bob"], "/api/play/stream?key=" + prepared["key"])
        self.assertEqual(stolen.get("status"), 403, stolen)
        self.assertIn("has not been shared with you", stolen.get("error", ""))
        unknown = call(STATE["bob"], "/api/play/stream?key=" + "0" * 40)
        self.assertEqual(unknown.get("status"), 400, unknown)
        self.assertIn("prepare", unknown.get("error", ""))

    # --- sharing --------------------------------------------------------

    def test_10_share_a_folder_with_a_person(self):
        out = call(STATE["alice"], "/api/library/share",
                   {"path": "Shoot", "kind": "folder", "target_kind": "user",
                    "target_id": STATE["bob"]["id"], "role": "viewer"})
        self.assertNotIn("status", out, out.get("error"))
        self.assertEqual(out["role"], "viewer")
        self.assertEqual(out["target"], "bob")
        STATE["share_id"] = out["id"]

        shared = library(STATE["bob"], root="shared")
        self.assertEqual(names(shared), ["Shoot"])
        row = shared["folders"][0]
        self.assertEqual(row["root"], f"user:{STATE['alice']['id']}")
        self.assertEqual(row["owner"], "alice")
        self.assertEqual(row["role"], "viewer")
        self.assertTrue(row["shared_with_me"])
        self.assertFalse(row["missing"])

    def test_11_the_grant_can_be_browsed_into(self):
        inside = library(STATE["bob"], root=f"user:{STATE['alice']['id']}",
                         path="Shoot")
        self.assertEqual(names(inside), ["green.mp4"])
        self.assertEqual(inside["role"], "viewer")
        self.assertFalse(inside["can_edit"])
        self.assertIsNone(inside["parent"], "a viewer must not walk upwards")
        # And no further than the grant reaches.
        up = library(STATE["bob"], root=f"user:{STATE['alice']['id']}")
        self.assertEqual(up.get("status"), 403)

    def test_12_the_owner_sees_the_badge_on_what_they_shared(self):
        top = library(STATE["alice"])
        folder = [f for f in top["folders"] if f["name"] == "Shoot"][0]
        self.assertTrue(folder["shared_by_me"])
        grants = call(STATE["alice"], "/api/library/share?path=Shoot")
        self.assertEqual(len(grants["grants"]), 1)
        self.assertEqual(grants["grants"][0]["target"], "bob")

    def test_13_a_viewer_can_open_the_clip_and_read_the_history(self):
        opened = call(STATE["bob"], "/api/project/open",
                      {"root": f"user:{STATE['alice']['id']}",
                       "path": "Shoot/green.mp4"})
        self.assertNotIn("status", opened, opened.get("error"))
        self.assertTrue(opened["open"])
        STATE["green"] = opened["name"]
        STATE["green_key"] = opened["key"]
        log = call(STATE["bob"], "/api/project/log")
        self.assertGreaterEqual(log["total"], 1)
        thumb = call(STATE["bob"],
                     "/api/thumb?root=user:%d&path=%s&t=0&w=32"
                     % (STATE["alice"]["id"],
                        urllib.parse.quote("Shoot/green.mp4")))
        self.assertGreater(thumb.get("bytes", 0), 0)
        self.assertEqual(thumb.get("content_type"), "image/jpeg")

    def test_13b_a_viewer_reads_pixels_of_the_shared_clip_and_nothing_else(self):
        frame = call(STATE["bob"], "/api/frame",
                     {"clip": STATE["green"], "time": 0, "width": 64})
        self.assertGreater(frame.get("bytes", 0), 0)
        self.assertEqual(frame.get("content_type"), "image/jpeg")
        stats = call(STATE["bob"], "/api/stats",
                     {"clip": STATE["green"], "time": 0, "width": 64})
        self.assertIn("stats", stats)
        # The grant is on `Shoot`, so the clip alice left at the top of her
        # library is still refused to the same account in the same session.
        out = call(STATE["bob"], "/api/frame",
                   {"clip": STATE["red"], "time": 0, "width": 64})
        self.assertEqual(out.get("status"), 403, out)

    def test_14_a_viewer_cannot_commit(self):
        cfg = call(STATE["bob"], "/api/project")["config"]
        cfg["primaries"]["saturation"] = 1.2
        for path, payload in (
                ("/api/session", {"config": cfg, "replace": True,
                                  "clip": STATE["green"]}),
                ("/api/grade", {"clip": STATE["green"], "config": cfg}),
                ("/api/project/rotation", {"clip": STATE["green"],
                                           "rotation": "90"}),
                ("/api/project/extra", {"clip": STATE["green"],
                                        "name": "x", "value": 1}),
                ("/api/project/fork", {"clip": STATE["green"]}),
                ("/api/project/undo", {"clip": STATE["green"]})):
            method = "PUT" if path == "/api/grade" else "POST"
            out = call(STATE["bob"], path, payload, method=method)
            self.assertEqual(out.get("status"), 403, f"{path}: {out}")
            self.assertEqual(out["error"],
                             "viewer access: ask the owner for edit rights")

    def test_15_an_editor_can_commit_and_the_owner_sees_who(self):
        call(STATE["alice"], "/api/library/share",
             {"path": "Shoot", "kind": "folder", "target_kind": "user",
              "target_id": STATE["bob"]["id"], "role": "editor"})
        self.assertEqual(
            library(STATE["bob"], root=f"user:{STATE['alice']['id']}",
                    path="Shoot")["role"], "editor")
        cfg = call(STATE["bob"], "/api/project")["config"]
        cfg["primaries"]["saturation"] = 1.25
        out = call(STATE["bob"], "/api/session",
                   {"config": cfg, "replace": True, "by": "not-bob",
                    "clip": STATE["green"]})
        self.assertNotIn("status", out, out.get("error"))
        # Alice opens the same clip through her own library and sees it.
        alice_open = call(STATE["alice"], "/api/project/open",
                          {"root": "mine", "path": "Shoot/green.mp4"})
        self.assertEqual(alice_open["key"], STATE["green_key"],
                         "the same file must be the same project")
        log = call(STATE["alice"], "/api/project/log")
        top = log["commits"][0]
        self.assertEqual(top["author"], "bob")
        self.assertIn("saturation", top["message"])
        # And the head shows up in the owner's file listing.
        entry = library(STATE["alice"], path="Shoot")["files"][0]
        self.assertEqual(entry["project"]["head"], top["short"])
        self.assertEqual(entry["project"]["branch"], "main")

    def test_16_an_editor_can_upload_into_the_shared_folder(self):
        out = upload(STATE["bob"], STATE["clips"] / "blue.mp4",
                     root=f"user:{STATE['alice']['id']}", path="Shoot")
        self.assertNotIn("status", out, out.get("error"))
        self.assertEqual(out["path"], "Shoot/blue.mp4")
        self.assertEqual(out["root"], f"user:{STATE['alice']['id']}")
        self.assertIn("blue.mp4", names(library(STATE["alice"], path="Shoot")))
        # It landed in alice's library, not in bob's.
        self.assertEqual(names(library(STATE["bob"])), [])

    def test_17_share_a_folder_with_a_team(self):
        call(STATE["alice"], "/api/library/mkdir",
             {"root": "mine", "path": "", "name": "Crew"})
        upload(STATE["alice"], STATE["clips"] / "grey.mp4", path="Crew")
        team = [t for t in call(STATE["alice"], "/api/people")["teams"]
                if t["name"] == "crew"][0]
        out = call(STATE["alice"], "/api/library/share",
                   {"path": "Crew", "kind": "folder", "target_kind": "team",
                    "target_id": team["id"], "role": "viewer"})
        self.assertEqual(out["target"], "crew")
        shared = library(STATE["bob"], root="shared")
        self.assertIn("Crew", names(shared))
        row = [f for f in shared["folders"] if f["name"] == "Crew"][0]
        self.assertEqual(row["via"]["kind"], "team")
        team_view = library(STATE["bob"], root=f"team:{team['id']}")
        self.assertEqual(names(team_view), ["Crew"])
        inside = library(STATE["bob"], root=f"user:{STATE['alice']['id']}",
                         path="Crew")
        self.assertEqual(names(inside), ["grey.mp4"])

    def test_18_unshare_takes_it_away(self):
        grants = call(STATE["alice"], "/api/library/share?path=Crew")
        sid = grants["grants"][0]["id"]
        self.assertTrue(call(STATE["alice"], "/api/library/unshare",
                             {"share_id": sid})["removed"])
        self.assertNotIn("Crew", names(library(STATE["bob"], root="shared")))
        self.assertEqual(
            library(STATE["bob"], root=f"user:{STATE['alice']['id']}",
                    path="Crew").get("status"), 403)

    # --- renames, moves, trash -----------------------------------------

    def test_20_rename_through_the_app_keeps_the_share(self):
        out = call(STATE["alice"], "/api/library/rename",
                   {"root": "mine", "path": "Shoot", "name": "Shoot day 2"})
        self.assertEqual(out["path"], "Shoot day 2")
        self.assertEqual(out["shares"], 1)
        shared = library(STATE["bob"], root="shared")
        self.assertEqual(names(shared), ["Shoot day 2"])
        inside = library(STATE["bob"], root=f"user:{STATE['alice']['id']}",
                         path="Shoot day 2")
        self.assertIn("green.mp4", names(inside))

    def test_21_move_a_clip_between_folders(self):
        call(STATE["alice"], "/api/library/mkdir",
             {"root": "mine", "path": "", "name": "Selects"})
        out = call(STATE["alice"], "/api/library/move",
                   {"root": "mine", "path": "red.mp4", "to": "Selects"})
        self.assertEqual(out["path"], "Selects/red.mp4")
        self.assertEqual(names(library(STATE["alice"], path="Selects")),
                         ["red.mp4"])
        self.assertNotIn("red.mp4", names(library(STATE["alice"])))
        # A folder cannot swallow itself.
        bad = call(STATE["alice"], "/api/library/move",
                   {"root": "mine", "path": "Selects", "to": "Selects"})
        self.assertEqual(bad.get("status"), 400)

    def test_22_a_rename_on_disk_shows_the_share_as_missing(self):
        alice_root = (STATE["tmp"] / "users" / str(STATE["alice"]["id"])
                      / "footage")
        (alice_root / "Shoot day 2").rename(alice_root / "Shoot day 3")
        shared = library(STATE["bob"], root="shared")
        row = [f for f in shared["folders"] if f["path"] == "Shoot day 2"][0]
        self.assertTrue(row["missing"], "a broken share must say so")
        # Put it back so the rest of the story still works.
        (alice_root / "Shoot day 3").rename(alice_root / "Shoot day 2")
        self.assertFalse(
            [f for f in library(STATE["bob"], root="shared")["folders"]
             if f["path"] == "Shoot day 2"][0]["missing"])

    def test_23_trash_keeps_the_file_and_restore_puts_it_back(self):
        out = call(STATE["alice"], "/api/library/trash",
                   {"root": "mine", "path": "Selects/red.mp4"})
        self.assertNotIn("status", out, out.get("error"))
        self.assertEqual(names(library(STATE["alice"], path="Selects")), [])
        trash = library(STATE["alice"], root="trash")
        row = [f for f in trash["files"] if f["orig"] == "Selects/red.mp4"][0]
        self.assertEqual(row["name"], "red.mp4")
        self.assertGreater(row["bytes"], 0, "the bytes are still there")
        back = call(STATE["alice"], "/api/library/restore",
                    {"path": row["path"]})
        self.assertEqual(back["path"], "Selects/red.mp4")
        self.assertEqual(names(library(STATE["alice"], path="Selects")),
                         ["red.mp4"])
        self.assertEqual(library(STATE["alice"], root="trash")["files"], [])

    def test_24_trashing_a_shared_folder_takes_the_grant_off_it(self):
        call(STATE["alice"], "/api/library/mkdir",
             {"root": "mine", "path": "", "name": "Temp"})
        call(STATE["alice"], "/api/library/share",
             {"path": "Temp", "kind": "folder", "target_kind": "user",
              "target_id": STATE["bob"]["id"], "role": "viewer"})
        self.assertIn("Temp", names(library(STATE["bob"], root="shared")))
        out = call(STATE["alice"], "/api/library/trash",
                   {"root": "mine", "path": "Temp"})
        self.assertEqual(out["shares"], 1)
        self.assertNotIn("Temp", names(library(STATE["bob"], root="shared")))

    def test_25_names_that_would_break_things_are_refused(self):
        for name in ("", "a/b", "..", ".hidden", "x" * 200):
            out = call(STATE["alice"], "/api/library/mkdir",
                       {"root": "mine", "path": "", "name": name})
            self.assertEqual(out.get("status"), 400, f"{name!r}: {out}")
            self.assertTrue(out.get("error"))

    def test_26_hidden_paths_are_not_writable(self):
        """The trash is reached through the trash routes and no other way.

        Without this, `move` with `to=".trash"` would park an item in the
        trash with none of the bookkeeping the restore reads back, and
        `mkdir` could make folders no listing shows.
        """
        for call_path, payload in (
                ("/api/library/mkdir", {"root": "mine", "path": ".trash",
                                        "name": "sneaky"}),
                ("/api/library/move", {"root": "mine", "path": "Selects",
                                       "to": ".trash"}),
                ("/api/library/rename", {"root": "mine", "path": ".trash",
                                         "name": "bin"})):
            out = call(STATE["alice"], call_path, payload)
            self.assertEqual(out.get("status"), 400, f"{call_path}: {out}")
            self.assertIn("hidden", out["error"])
        # And the trash listing is still readable through its own route.
        self.assertEqual(library(STATE["alice"], root="trash")["root"], "trash")

    # --- tenancy --------------------------------------------------------

    def test_30_another_org_is_not_a_person_you_can_share_with(self):
        picker = call(STATE["alice"], "/api/people")
        listed = [u["name"] for u in picker["users"]]
        self.assertIn("bob", listed)
        self.assertNotIn("carol", listed)
        self.assertEqual([t["name"] for t in picker["teams"]], ["crew"])
        for u in picker["users"]:
            self.assertEqual(set(u), {"id", "name", "admin", "me"})
        out = call(STATE["alice"], "/api/library/share",
                   {"path": "Selects", "kind": "folder",
                    "target_kind": "user", "target_id": STATE["carol"]["id"],
                    "role": "viewer"})
        self.assertEqual(out.get("status"), 400)
        self.assertIn("org", out["error"])

    def test_31_another_org_sees_nothing(self):
        self.assertEqual(names(library(STATE["carol"])), [])
        self.assertEqual(names(library(STATE["carol"], root="shared")), [])
        self.assertEqual(
            library(STATE["carol"],
                    root=f"user:{STATE['alice']['id']}").get("status"), 403)
        self.assertEqual(call(STATE["carol"], "/api/activity")["activity"], [])
        self.assertEqual([u["name"] for u in
                          call(STATE["carol"], "/api/people")["users"]],
                         ["carol"])

    # --- the feed -------------------------------------------------------

    def test_40_the_activity_feed_has_the_events_and_the_actors(self):
        feed = call(STATE["alice"], "/api/activity?limit=100")["activity"]
        kinds = {e["kind"] for e in feed}
        for want in ("upload", "mkdir", "rename", "move", "trash", "restore",
                     "share", "unshare"):
            self.assertIn(want, kinds, f"{want} is missing from the feed")
        actors = {e["actor"] for e in feed}
        self.assertIn("alice", actors)
        self.assertIn("bob", actors, "the editor's upload should be in here")
        newest = feed[0]
        self.assertEqual(set(newest) >= {"id", "ts", "kind", "actor",
                                         "owner", "path", "detail"}, True)
        self.assertGreaterEqual(feed[0]["ts"], feed[-1]["ts"])

    def test_41_the_feed_only_shows_what_you_can_reach(self):
        feed = call(STATE["bob"], "/api/activity?limit=100")["activity"]
        self.assertTrue(feed, "bob holds a grant, so he sees those events")
        for e in feed:
            self.assertTrue(
                e["owner_id"] == STATE["bob"]["id"]
                or e["path"] == "Shoot day 2"
                or e["path"].startswith("Shoot day 2/")
                or e["path"] == "Shoot"
                or e["path"].startswith("Shoot/"),
                f"bob should not see {e['kind']} on {e['path']}")

    # --- logins off ------------------------------------------------------

    def test_50_with_logins_off_every_check_passes(self):
        """A second server, no --auth, on the founder's own footage folder.

        This is the case that matters most: the founder runs this locally
        with no accounts, and the whole permission system has to be invisible
        there. Read only on purpose (the library root with logins off is the
        real content/footage), so this makes no folders and moves nothing.
        """
        port = free_port()
        tmp = Path(tempfile.mkdtemp(prefix="studio-library-noauth-"))
        proc = subprocess.Popen(
            [str(PYTHON), SERVER, "--port", str(port), "--data-dir", str(tmp)],
            cwd=str(CONTENT), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True)
        base = f"http://127.0.0.1:{port}"
        try:
            deadline = time.time() + 40
            while time.time() < deadline:
                try:
                    urllib.request.urlopen(base + "/api/state", timeout=2).read()
                    break
                except Exception:                              # noqa: BLE001
                    time.sleep(0.25)
            else:
                self.skipTest("the second server never came up")
            out = json.loads(urllib.request.urlopen(
                base + "/api/library?root=mine", timeout=30).read().decode())
            self.assertEqual(out["role"], "owner")
            self.assertTrue(out["can_edit"])
            self.assertEqual(out["owner"], "local")
            footage = CONTENT / "footage"
            on_disk = sorted(p.name for p in footage.iterdir()
                             if p.suffix.lower() in (".mov", ".mp4", ".m4v",
                                                     ".mxf", ".mkv"))
            self.assertEqual(sorted(f["name"] for f in out["files"]), on_disk)
            people = json.loads(urllib.request.urlopen(
                base + "/api/people", timeout=10).read().decode())
            self.assertEqual(people["me"], 0)
            self.assertEqual(people["org"], 1)
        finally:
            if proc.poll() is None:
                proc.terminate()             # by the pid captured at spawn
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=10)
            if proc.stdout is not None:
                proc.stdout.close()
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
