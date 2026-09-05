#!/usr/bin/env python3
"""The library: per account folders and clips, shared the way a drive is.

Contract E1 of the studio library arc. Everything a person can do to files
on this server that is not grading them lives here: list a folder, make one,
rename, move, trash and restore, and grant somebody else access to a folder
or a single clip.

The shape of the thing, in four sentences.

1. A LIBRARY is one account's own tree of files on this server. With logins
   off there is one account (id 0) and its library is `content/footage/`,
   which is where the founder's files already are, so nothing moves. With
   logins on an account's library is `studio/data/users/<id>/footage/`, which
   is where uploads already land, so again nothing moves.
2. An ORG is the tenant. Every account belongs to exactly one (`users.org_id`,
   default org 1 named `default`). A share, a team and the activity feed all
   stop at the org boundary, so two orgs on one server never see each other.
3. A TEAM is a named group inside an org. Sharing to a team is sharing to
   everyone in it, including whoever joins later.
4. A SHARE is a row saying "this path in this owner's library is readable
   (viewer) or writable (editor) by this user or this team". A share on a
   folder covers everything under it, which is the only rule needed to make
   the whole tree behave the way people expect.

Two things this module is deliberately strict about.

Paths. Every path from a client is a POSIX relative path inside one library
root: no leading slash, no `..`, no drive letters, no backslashes. It is
turned into a real path exactly once, by `confine()`, which resolves symlinks
first and then compares with `os.path.commonpath`, never with a string
prefix. A string prefix test says `/footage-other` is inside `/footage`;
commonpath does not. Nothing else in this file builds a path by hand.

Logins off. Every permission check in here returns "yes" when `auth.enabled()`
is false, because then there is exactly one user, one org and one library, and
the founder running this on their own Mac must not meet a permission system
that has nobody to check against. That is not a special case sprinkled through
the code: it is one early return in `best_role()` and one in `guard_edit()`.

Errors. A permission refusal is `auth.AuthError(403, sentence)`, which the
server already answers as a 403 with that sentence in `error`. A bad request
is a plain `ValueError`, which the server already answers as a 400 the same
way. So this module adds no new error plumbing to the request path.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

import auth as _auth
import db as _db
import grades as _grades
import projects as _projects

STUDIO = Path(__file__).resolve().parent
CONTENT = STUDIO.parent
# The shared footage folder. Same expression server.py uses, so the two agree
# by construction rather than by being kept in step by hand.
FOOTAGE = CONTENT / "footage"

# Trashed items go here inside the library they came from, so a trash never
# crosses an account boundary and never leaves the quota it was counted
# against. The leading dot keeps it out of every listing (this module and the
# older browse endpoint both skip dotted entries), so it does not look like a
# folder somebody made.
TRASH = ".trash"

# Extensions this tool can open. Bound from server.py at import so there is
# one list, not two that drift; the value here is only the fallback for a
# direct import of this module (the tests, an agent script).
VIDEO_EXT = {".mov", ".mp4", ".m4v", ".mxf", ".mkv"}

# Roles, worst to best. Ordering them in one place is what lets a user who
# holds a folder as viewer through a team and the same folder as editor
# directly end up with editor rather than with whichever row came back first.
ROLE_RANK = {"viewer": 1, "editor": 2, "owner": 3}
SHARE_ROLES = ("viewer", "editor")

VIEWER_MESSAGE = "viewer access: ask the owner for edit rights"

# A name typed into New folder or Rename. Anything that could climb out of the
# folder, hide the item, or confuse a shell is refused with a sentence rather
# than sanitised silently: a person who typed a slash meant something, and
# quietly saving it under a different name is worse than saying no.
NAME_MAX = 128
_BAD_NAME = re.compile(r"[\x00-\x1f/\\]")

SCHEMA = """
CREATE TABLE IF NOT EXISTS teams (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  org_id     INTEGER NOT NULL DEFAULT 1,
  name       TEXT NOT NULL,
  created_at REAL NOT NULL,
  UNIQUE (org_id, name)
);

CREATE TABLE IF NOT EXISTS team_members (
  team_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  role    TEXT NOT NULL DEFAULT 'member',
  PRIMARY KEY (team_id, user_id)
);
CREATE INDEX IF NOT EXISTS team_members_user ON team_members(user_id);

CREATE TABLE IF NOT EXISTS shares (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  org_id      INTEGER NOT NULL DEFAULT 1,
  owner_id    INTEGER NOT NULL,
  kind        TEXT NOT NULL DEFAULT 'folder',
  rel_path    TEXT NOT NULL,
  target_kind TEXT NOT NULL,
  target_id   INTEGER NOT NULL,
  role        TEXT NOT NULL DEFAULT 'viewer',
  created_by  INTEGER NOT NULL DEFAULT 0,
  created_at  REAL NOT NULL,
  UNIQUE (owner_id, rel_path, target_kind, target_id)
);
CREATE INDEX IF NOT EXISTS shares_target ON shares(target_kind, target_id);
CREATE INDEX IF NOT EXISTS shares_owner ON shares(owner_id);

CREATE TABLE IF NOT EXISTS activity (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  org_id      INTEGER NOT NULL DEFAULT 1,
  actor_id    INTEGER NOT NULL DEFAULT 0,
  ts          REAL NOT NULL,
  kind        TEXT NOT NULL,
  owner_id    INTEGER NOT NULL DEFAULT 0,
  rel_path    TEXT NOT NULL DEFAULT '',
  detail_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS activity_org ON activity(org_id, id);
"""

_inited = False
_init_lock = threading.Lock()

# Probe cache for the listing only, keyed by (realpath, size, mtime_ns) so a
# file edited in place is re-probed. A folder of thirty clips would otherwise
# pay thirty ffprobe runs on every single listing, which is the difference
# between a file pane that opens instantly and one that stalls.
_probe_fn = None
_probe_cache: dict[tuple, dict | None] = {}
_probe_lock = threading.Lock()


def bind(probe=None, video_ext=None) -> None:
    """Teach this module the two things server.py already knows.

    `probe` turns an absolute path into {duration, width, height} or None;
    server.py has the ffprobe wrapper and its own cache, so this module asks
    rather than growing a second copy of it. `video_ext` is the one set of
    extensions the studio opens.
    """
    global _probe_fn, VIDEO_EXT
    if probe is not None:
        _probe_fn = probe
    if video_ext:
        VIDEO_EXT = set(video_ext)


def init_schema() -> None:
    """Create the library tables. Additive and safe to call on every boot.

    db.init_schema() first, because that is what creates `orgs`, puts the
    default org in it and adds `users.org_id` to a database made before this
    arc existed.
    """
    global _inited
    with _init_lock:
        _db.init_schema()
        con = _db.connect()
        try:
            con.executescript(SCHEMA)
            con.commit()
        finally:
            con.close()
        _inited = True


def _ensure() -> None:
    if not _inited:
        init_schema()


# --------------------------------------------------------------------------
# orgs, teams and people
# --------------------------------------------------------------------------

def org_of(user_id) -> int:
    """The org a user belongs to, defaulting to org 1.

    User 0 (logins off) has no row in `users` at all, and a user created
    before this arc has `org_id` filled in by the migration, so the default
    covers both without a branch anywhere else.
    """
    uid = int(user_id or 0)
    if uid <= 0:
        return 1
    _ensure()
    con = _db.connect()
    try:
        row = con.execute("SELECT org_id FROM users WHERE id=?", (uid,)).fetchone()
    except Exception:                                          # noqa: BLE001
        return 1
    finally:
        con.close()
    return int(row["org_id"]) if row and row["org_id"] else 1


def create_org(name: str) -> dict:
    name = (name or "").strip()
    if not name:
        raise ValueError("an org needs a name")
    if len(name) > 64:
        raise ValueError("org names are up to 64 characters")
    _ensure()
    con = _db.connect()
    try:
        row = con.execute("SELECT id, name FROM orgs WHERE name=?",
                          (name,)).fetchone()
        if row:
            raise ValueError(f"an org named {name} already exists")
        cur = con.execute("INSERT INTO orgs (name, created_at) VALUES (?,?)",
                          (name, time.time()))
        con.commit()
        return {"id": int(cur.lastrowid), "name": name}
    finally:
        con.close()


def list_orgs() -> list[dict]:
    _ensure()
    con = _db.connect()
    try:
        rows = con.execute("SELECT id, name, created_at FROM orgs "
                           "ORDER BY id").fetchall()
        counts = {int(r["org_id"]): int(r["n"]) for r in con.execute(
            "SELECT org_id, COUNT(*) AS n FROM users GROUP BY org_id")}
    finally:
        con.close()
    return [{"id": int(r["id"]), "name": r["name"],
             "created_at": float(r["created_at"]),
             "users": counts.get(int(r["id"]), 0)} for r in rows]


def org_by_name(name) -> dict | None:
    """An org by name, or by id when the caller passed a number.

    Both are accepted because a person typing an admin command has the name
    in front of them and a script has the id.
    """
    raw = str(name or "").strip()
    if not raw:
        return None
    _ensure()
    con = _db.connect()
    try:
        row = con.execute("SELECT id, name FROM orgs WHERE name=?",
                          (raw,)).fetchone()
        if row is None and raw.isdigit():
            row = con.execute("SELECT id, name FROM orgs WHERE id=?",
                              (int(raw),)).fetchone()
    finally:
        con.close()
    return {"id": int(row["id"]), "name": row["name"]} if row else None


def set_user_org(user_id: int, org_id: int) -> None:
    _ensure()
    con = _db.connect()
    try:
        con.execute("UPDATE users SET org_id=? WHERE id=?",
                    (int(org_id), int(user_id)))
        con.commit()
    finally:
        con.close()


def create_team(name: str, org_id: int = 1) -> dict:
    name = (name or "").strip()
    if not name:
        raise ValueError("a team needs a name")
    if len(name) > 64:
        raise ValueError("team names are up to 64 characters")
    _ensure()
    con = _db.connect()
    try:
        row = con.execute("SELECT id FROM teams WHERE org_id=? AND name=?",
                          (int(org_id), name)).fetchone()
        if row:
            raise ValueError(f"a team named {name} already exists in that org")
        cur = con.execute("INSERT INTO teams (org_id, name, created_at) "
                          "VALUES (?,?,?)", (int(org_id), name, time.time()))
        con.commit()
        return {"id": int(cur.lastrowid), "name": name, "org_id": int(org_id)}
    finally:
        con.close()


def team_by_name(name: str, org_id: int = 1) -> dict | None:
    raw = str(name or "").strip()
    if not raw:
        return None
    _ensure()
    con = _db.connect()
    try:
        row = con.execute("SELECT id, name, org_id FROM teams "
                          "WHERE org_id=? AND name=?",
                          (int(org_id), raw)).fetchone()
        if row is None and raw.isdigit():
            row = con.execute("SELECT id, name, org_id FROM teams WHERE id=?",
                              (int(raw),)).fetchone()
    finally:
        con.close()
    return dict(row) if row else None


def get_team(team_id: int) -> dict | None:
    _ensure()
    con = _db.connect()
    try:
        row = con.execute("SELECT id, name, org_id FROM teams WHERE id=?",
                          (int(team_id),)).fetchone()
    finally:
        con.close()
    return {"id": int(row["id"]), "name": row["name"],
            "org_id": int(row["org_id"])} if row else None


def list_teams(org_id: int | None = None) -> list[dict]:
    _ensure()
    con = _db.connect()
    try:
        if org_id is None:
            rows = con.execute("SELECT t.id, t.name, t.org_id, o.name AS org "
                               "FROM teams t LEFT JOIN orgs o ON o.id=t.org_id "
                               "ORDER BY t.org_id, t.name").fetchall()
        else:
            rows = con.execute("SELECT t.id, t.name, t.org_id, o.name AS org "
                               "FROM teams t LEFT JOIN orgs o ON o.id=t.org_id "
                               "WHERE t.org_id=? ORDER BY t.name",
                               (int(org_id),)).fetchall()
        members: dict[int, list[str]] = {}
        for r in con.execute(
                "SELECT m.team_id AS tid, u.name AS name FROM team_members m "
                "JOIN users u ON u.id = m.user_id ORDER BY u.name"):
            members.setdefault(int(r["tid"]), []).append(r["name"])
    finally:
        con.close()
    return [{"id": int(r["id"]), "name": r["name"], "org_id": int(r["org_id"]),
             "org": r["org"] or "default",
             "members": members.get(int(r["id"]), [])} for r in rows]


def add_to_team(user_id: int, team_id: int, role: str = "member") -> None:
    if role not in ("member", "lead"):
        raise ValueError("a team role is member or lead")
    team = get_team(team_id)
    if team is None:
        raise ValueError("no such team")
    if org_of(user_id) != team["org_id"]:
        raise ValueError("that account is in a different org, and a team "
                         "cannot cross orgs")
    con = _db.connect()
    try:
        con.execute("INSERT INTO team_members (team_id, user_id, role) "
                    "VALUES (?,?,?) ON CONFLICT(team_id, user_id) DO UPDATE "
                    "SET role=excluded.role",
                    (int(team_id), int(user_id), role))
        con.commit()
    finally:
        con.close()


def remove_from_team(user_id: int, team_id: int) -> bool:
    _ensure()
    con = _db.connect()
    try:
        cur = con.execute("DELETE FROM team_members WHERE team_id=? AND "
                          "user_id=?", (int(team_id), int(user_id)))
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def teams_of(user_id) -> list[int]:
    uid = int(user_id or 0)
    if uid <= 0:
        return []
    _ensure()
    con = _db.connect()
    try:
        rows = con.execute("SELECT team_id FROM team_members WHERE user_id=?",
                           (uid,)).fetchall()
    finally:
        con.close()
    return [int(r["team_id"]) for r in rows]


def people(user_id) -> dict:
    """Who this account can share with: everybody in its own org.

    Names and ids only. There is nothing else in this database worth handing
    out (no emails exist), and whether somebody is an admin is the one extra
    bit a share picker legitimately wants, so that is the one extra bit it
    gets.
    """
    _ensure()
    org = org_of(user_id)
    me = int(user_id or 0)
    mine = set(teams_of(user_id))
    con = _db.connect()
    try:
        users = [{"id": int(r["id"]), "name": r["name"],
                  "admin": r["role"] == "admin", "me": int(r["id"]) == me}
                 for r in con.execute(
                     "SELECT id, name, role FROM users WHERE org_id=? "
                     "ORDER BY name", (org,)).fetchall()]
        teams = []
        for r in con.execute("SELECT id, name FROM teams WHERE org_id=? "
                             "ORDER BY name", (org,)).fetchall():
            n = con.execute("SELECT COUNT(*) AS n FROM team_members "
                            "WHERE team_id=?", (int(r["id"]),)).fetchone()["n"]
            teams.append({"id": int(r["id"]), "name": r["name"],
                          "members": int(n),
                          "mine": int(r["id"]) in mine})
    finally:
        con.close()
    return {"org": org, "me": me, "users": users, "teams": teams,
            "roles": list(SHARE_ROLES)}


# Names are looked up once per account and kept, because a listing, a feed
# and a share dialog all ask for the same handful of names over and over and
# each miss is a database round trip. Only a name that was actually found is
# cached: an id with no row yet (an account created later in the same
# process) has to stay askable.
_name_cache: dict[int, str] = {}


def user_name(user_id) -> str:
    uid = int(user_id or 0)
    if uid <= 0:
        return "local"
    hit = _name_cache.get(uid)
    if hit:
        return hit
    user = _auth.get_user(uid)
    if user:
        _name_cache[uid] = user["name"]
        return user["name"]
    return f"user {uid}"


# --------------------------------------------------------------------------
# paths
# --------------------------------------------------------------------------

def root_for(user_id) -> Path:
    """The library root for one account.

    Exactly the rule POST /api/upload already used before this arc: the
    account's own folder with logins on, the shared footage folder without.
    Written once here so every library operation and the upload agree.
    """
    uid = int(user_id or 0)
    if _auth.enabled() and uid > 0:
        return _auth.user_footage(uid)
    return FOOTAGE


def ensure_root(user_id) -> Path:
    root = root_for(user_id)
    root.mkdir(parents=True, exist_ok=True)
    return root


def norm_rel(raw) -> str:
    """A client path to a POSIX relative path, or a refusal.

    Empty means the root of the library. Backslashes become slashes so a
    Windows style path is understood rather than treated as a filename with
    backslashes in it. `..` is refused outright instead of being resolved,
    because a path that tries to climb is a bug or an attack and both are
    better answered than quietly repaired.
    """
    s = str(raw or "").strip().replace("\\", "/")
    if "\x00" in s:
        raise ValueError("that path is not a path")
    while s.startswith("/"):
        s = s[1:]
    parts = []
    for seg in s.split("/"):
        seg = seg.strip()
        if seg in ("", "."):
            continue
        if seg == "..":
            raise ValueError("a library path cannot contain ..")
        parts.append(seg)
    return "/".join(parts)


def confine(root: Path, rel: str) -> Path:
    """The one place a library path becomes a real path.

    realpath first, so a symlink pointing out of the library and a `..` that
    survived normalisation are both resolved before the comparison.
    commonpath rather than a string prefix, because `startswith` says
    `/footage-other` is inside `/footage`. Works for a path that does not
    exist yet (a new folder, an upload destination), which is why realpath is
    used and not resolve(strict=True).
    """
    rel = norm_rel(rel)
    root_real = os.path.realpath(str(root))
    if not rel:
        return Path(root_real)
    target = os.path.realpath(os.path.join(root_real, rel))
    try:
        if os.path.commonpath([target, root_real]) == root_real:
            return Path(target)
    except ValueError:
        pass
    # One deliberate exception, and only with logins off: the LAST element
    # may be a symlink pointing at a file elsewhere on this machine. There is
    # one library and one person when logins are off, so "outside your
    # library" means nothing for a link that person put there themselves, and
    # it is how the test harness runs against a temp footage folder holding
    # links to the three real clips instead of copies of them (copies would
    # change nothing, but the specs match on content keys and copies cost
    # gigabytes). The folder chain above it is still resolved and still has
    # to be inside the root, so a symlinked FOLDER cannot be walked through,
    # and with logins on this whole branch is skipped.
    if not _auth.enabled():
        parent = os.path.realpath(os.path.join(root_real,
                                               os.path.dirname(rel)))
        leaf = os.path.join(parent, os.path.basename(rel))
        try:
            chain_ok = os.path.commonpath([parent, root_real]) == root_real
        except ValueError:
            chain_ok = False
        if chain_ok and os.path.islink(leaf) and os.path.isfile(leaf):
            return Path(leaf)
    raise _auth.AuthError(403, "that path is outside your library, so this "
                               "server will not open it")


def rel_of(root: Path, path) -> str | None:
    """The POSIX path of `path` inside `root`, or None if it is not inside."""
    root_real = os.path.realpath(str(root))
    target = os.path.realpath(str(path))
    if target == root_real:
        return ""
    try:
        if os.path.commonpath([target, root_real]) != root_real:
            return None
    except ValueError:
        return None
    return Path(target).relative_to(root_real).as_posix()


def visible_rel(rel: str) -> str:
    """Refuse a WRITE aimed at a hidden path.

    Every library has a `.trash` inside it and every listing skips dotted
    names, so a write addressed at one would either park an item where nobody
    can see it or fill the trash by hand with entries the restore cannot read
    back. The trash routes reach that folder through their own door, which is
    the only door.
    """
    for seg in rel.split("/"):
        if seg.startswith("."):
            raise ValueError("that path is hidden, so nothing is written to it")
    return rel


def check_name(name: str) -> str:
    name = str(name or "").strip()
    if not name:
        raise ValueError("that needs a name")
    if len(name) > NAME_MAX:
        raise ValueError(f"names are up to {NAME_MAX} characters")
    if name in (".", ".."):
        raise ValueError("that is not a name")
    if name.startswith("."):
        raise ValueError("a name cannot start with a dot: this app hides "
                         "those, so the item would vanish")
    if _BAD_NAME.search(name):
        raise ValueError("a name cannot contain a slash or a control "
                         "character")
    return name


def owner_of_path(path) -> tuple[int, str] | None:
    """Which library an absolute path belongs to: (owner id, rel path).

    None for anything that is not in a library at all: a clip opened from
    somewhere else on the Mac, or the shared `content/footage/` while logins
    are on (which is a read only common area for every account, not anybody's
    library). The permission rule treats None as "not my business", which is
    what keeps the anywhere browser working exactly as it did.
    """
    target = os.path.realpath(str(path))
    users_dir = os.path.realpath(str(_auth.USERS_DIR))
    try:
        inside = os.path.commonpath([target, users_dir]) == users_dir
    except ValueError:
        inside = False
    if inside:
        parts = Path(target).relative_to(users_dir).parts
        if len(parts) >= 2 and parts[0].isdigit() and parts[1] == "footage":
            return int(parts[0]), "/".join(parts[2:])
        return None
    if not _auth.enabled():
        rel = rel_of(FOOTAGE, target)
        if rel is not None:
            return 0, rel
    return None


def tree_bytes(root: Path) -> int:
    """Every byte under a library root, trash included.

    Recursive, unlike the flat sum the upload quota used before folders
    existed: with folders, a flat sum would let anybody park a hundred
    gigabytes one level down and never hit the quota.
    """
    total = 0
    root = Path(root)
    if not root.exists():
        return 0
    for base, _dirs, files in os.walk(str(root)):
        for f in files:
            try:
                total += os.stat(os.path.join(base, f)).st_size
            except OSError:
                continue
    return total


# --------------------------------------------------------------------------
# shares and the permission rule
# --------------------------------------------------------------------------

def _covers(grant_path: str, rel: str, kind: str) -> bool:
    """Does a grant on `grant_path` cover the item at `rel`?

    A grant on a folder covers the folder and everything under it; the empty
    path is the whole library, so it covers everything. A grant on a file
    covers that file and nothing else.
    """
    if grant_path == rel:
        return True
    if kind != "folder":
        return False
    if grant_path == "":
        return True
    return rel.startswith(grant_path + "/")


def grants_held(user_id) -> list[dict]:
    """Every share this account holds, directly or through a team.

    One query per caller rather than one per item: a listing asks about every
    row in a folder, and going back to SQLite for each of them turns a folder
    listing into a hundred queries.
    """
    uid = int(user_id or 0)
    if uid <= 0:
        return []
    _ensure()
    org = org_of(uid)
    mine = teams_of(uid)
    con = _db.connect()
    try:
        rows = con.execute("SELECT * FROM shares WHERE org_id=?",
                           (org,)).fetchall()
    finally:
        con.close()
    out = []
    for r in rows:
        if r["target_kind"] == "user" and int(r["target_id"]) == uid:
            via = {"kind": "user", "id": uid, "name": user_name(uid)}
        elif r["target_kind"] == "team" and int(r["target_id"]) in mine:
            team = get_team(int(r["target_id"]))
            via = {"kind": "team", "id": int(r["target_id"]),
                   "name": (team or {}).get("name", "team")}
        else:
            continue
        out.append({"id": int(r["id"]), "owner_id": int(r["owner_id"]),
                    "kind": r["kind"], "rel_path": r["rel_path"],
                    "role": r["role"], "via": via,
                    "created_at": float(r["created_at"])})
    return out


def best_role(user_id, owner_id, rel: str, grants=None) -> str | None:
    """The strongest role this account has on one item, or None.

    With logins off this is always `owner`: there is one account, one org and
    one library, so there is nothing to check and nothing to refuse.
    """
    if not _auth.enabled():
        return "owner"
    uid = int(user_id or 0)
    if uid and int(owner_id) == uid:
        return "owner"
    rel = norm_rel(rel)
    role = None
    for g in (grants_held(uid) if grants is None else grants):
        if int(g["owner_id"]) != int(owner_id):
            continue
        if not _covers(g["rel_path"], rel, g["kind"]):
            continue
        if role is None or ROLE_RANK[g["role"]] > ROLE_RANK[role]:
            role = g["role"]
    return role


def require_read(user_id, owner_id, rel: str, grants=None) -> str:
    role = best_role(user_id, owner_id, rel, grants)
    if role is None:
        raise _auth.AuthError(403, "that item is in another account's library "
                                   "and has not been shared with you")
    return role


def require_edit(user_id, owner_id, rel: str, grants=None) -> str:
    role = require_read(user_id, owner_id, rel, grants)
    if role == "viewer":
        raise _auth.AuthError(403, VIEWER_MESSAGE)
    return role


def guard_edit(user_id, path) -> None:
    """The grading permission rule, contract E1.

    Called by every route that CHANGES a project (a session write, a grade
    save, checkout, undo, redo, fork, rotation, extras). It answers on the
    clip's file, not on the project, because a project is shared by content
    key across every account and the thing that differs between two accounts
    looking at the same history is how each of them reaches the file.

    Silent (allows) for a path that is not in anybody's library, which is
    every clip opened through the anywhere browser and everything in the
    shared footage folder, and silent for every caller when logins are off.
    """
    if not _auth.enabled():
        return
    if not path:
        return
    found = owner_of_path(path)
    if found is None:
        return
    owner_id, rel = found
    require_edit(user_id, owner_id, rel)


def guard_read(user_id, path) -> None:
    """The reading half of the same rule.

    Called by every route that HANDS BACK PIXELS or facts about a file: a
    frame, a scope, a thumbnail, a play segment, a proxy, a render, the
    source bytes, the match analysis, the stats. Without it an account could
    name somebody else's clip and get its frames back, because clips are
    keyed by bare file name in one table shared by every caller.

    Passes for your own library, for a viewer or an editor grant, for the
    shared `content/footage` common area, for anything opened from elsewhere
    on the Mac through the anywhere browser, and for every caller when logins
    are off. Refuses with a plain sentence otherwise.
    """
    if not _auth.enabled():
        return
    if not path:
        return
    found = owner_of_path(path)
    if found is None:
        return
    owner_id, rel = found
    require_read(user_id, owner_id, rel)


def share_item(user_id, rel: str, kind: str, target_kind: str,
               target_id: int, role: str) -> dict:
    """Grant somebody in the same org access to one item in MY library.

    Only the owner shares, and only their own path: there is no re-sharing of
    something you were given, which keeps the question "who can reach this
    file" answerable by looking at one owner's rows.
    """
    _ensure()
    uid = int(user_id or 0)
    rel = norm_rel(rel)
    kind = (kind or "").strip().lower() or "folder"
    if kind not in ("file", "folder"):
        raise ValueError("kind is file or folder")
    role = (role or "").strip().lower()
    if role not in SHARE_ROLES:
        raise ValueError("a role is viewer or editor")
    target_kind = (target_kind or "").strip().lower()
    if target_kind not in ("user", "team"):
        raise ValueError("share with a user or a team")
    target_id = int(target_id or 0)

    root = ensure_root(uid)
    target_path = confine(root, rel)
    if not target_path.exists():
        raise ValueError("there is nothing at that path to share")
    real_kind = "folder" if target_path.is_dir() else "file"
    if real_kind != kind:
        kind = real_kind

    org = org_of(uid)
    if target_kind == "user":
        if target_id == uid:
            raise ValueError("that is your own account: you already have it")
        who = _auth.get_user(target_id)
        if who is None or org_of(target_id) != org:
            raise ValueError("no such person in this org")
        target_name = who["name"]
    else:
        team = get_team(target_id)
        if team is None or team["org_id"] != org:
            raise ValueError("no such team in this org")
        target_name = team["name"]

    now = time.time()
    con = _db.connect()
    try:
        con.execute(
            "INSERT INTO shares (org_id, owner_id, kind, rel_path, "
            "target_kind, target_id, role, created_by, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(owner_id, rel_path, target_kind, target_id) "
            "DO UPDATE SET role=excluded.role, kind=excluded.kind",
            (org, uid, kind, rel, target_kind, target_id, role, uid, now))
        con.commit()
        row = con.execute(
            "SELECT * FROM shares WHERE owner_id=? AND rel_path=? AND "
            "target_kind=? AND target_id=?",
            (uid, rel, target_kind, target_id)).fetchone()
    finally:
        con.close()
    record(uid, "share", uid, rel,
           {"target_kind": target_kind, "target_id": target_id,
            "target": target_name, "role": role,
            "name": Path(rel).name or "the whole library"})
    return _share_row(row)


def _share_row(row) -> dict:
    if row is None:
        return {}
    target_id = int(row["target_id"])
    if row["target_kind"] == "user":
        name = user_name(target_id)
    else:
        team = get_team(target_id)
        name = (team or {}).get("name", f"team {target_id}")
    return {"id": int(row["id"]), "owner_id": int(row["owner_id"]),
            "kind": row["kind"], "path": row["rel_path"],
            "name": Path(row["rel_path"]).name or "the whole library",
            "target_kind": row["target_kind"], "target_id": target_id,
            "target": name, "role": row["role"],
            "created_at": float(row["created_at"]),
            "created_by": int(row["created_by"]),
            "by": user_name(row["created_by"])}


def item_shares(user_id, rel: str) -> dict:
    """The grants on one item in MY library, for the share dialog."""
    _ensure()
    uid = int(user_id or 0)
    rel = norm_rel(rel)
    root = ensure_root(uid)
    path = confine(root, rel)
    con = _db.connect()
    try:
        rows = con.execute("SELECT * FROM shares WHERE owner_id=? AND "
                           "rel_path=? ORDER BY id", (uid, rel)).fetchall()
        inherited = [r for r in con.execute(
            "SELECT * FROM shares WHERE owner_id=? AND kind='folder' "
            "ORDER BY id", (uid,)).fetchall()
            if r["rel_path"] != rel and _covers(r["rel_path"], rel, "folder")]
    finally:
        con.close()
    return {"path": rel, "name": Path(rel).name or "the whole library",
            "kind": "folder" if path.is_dir() else "file",
            "exists": path.exists(),
            "grants": [_share_row(r) for r in rows],
            # A file inside a shared folder is reachable through that folder's
            # grant. Saying so is the difference between a dialog that reads
            # "shared with nobody" and one that tells the truth.
            "inherited": [_share_row(r) for r in inherited]}


def unshare(user_id, share_id: int) -> bool:
    _ensure()
    uid = int(user_id or 0)
    con = _db.connect()
    try:
        row = con.execute("SELECT * FROM shares WHERE id=?",
                          (int(share_id),)).fetchone()
        if row is None:
            return False
        if int(row["owner_id"]) != uid and not _is_org_admin(uid, int(row["org_id"])):
            raise _auth.AuthError(403, "only the owner of a file can take a "
                                       "share off it")
        con.execute("DELETE FROM shares WHERE id=?", (int(share_id),))
        con.commit()
    finally:
        con.close()
    record(uid, "unshare", int(row["owner_id"]), row["rel_path"],
           {"target_kind": row["target_kind"],
            "target_id": int(row["target_id"]),
            "target": _share_row(row)["target"], "role": row["role"],
            "name": Path(row["rel_path"]).name or "the whole library"})
    return True


def _is_org_admin(user_id, org_id: int) -> bool:
    if not _auth.enabled():
        return True
    user = _auth.get_user(int(user_id or 0))
    return bool(user and user["role"] == "admin"
                and org_of(user_id) == int(org_id))


def _rewrite_shares(owner_id: int, old_rel: str, new_rel: str) -> int:
    """Follow a rename or a move with the grants that pointed at it.

    Prefix rewrite, so moving a folder carries every grant on anything inside
    it. Without this, renaming a folder through the app would silently break
    every share on it, which is the single most annoying thing a file app can
    do to a team.
    """
    con = _db.connect()
    try:
        rows = con.execute("SELECT id, rel_path FROM shares WHERE owner_id=?",
                           (int(owner_id),)).fetchall()
        moved = 0
        for r in rows:
            rel = r["rel_path"]
            if rel == old_rel:
                nxt = new_rel
            elif old_rel and rel.startswith(old_rel + "/"):
                nxt = new_rel + rel[len(old_rel):]
            else:
                continue
            try:
                con.execute("UPDATE shares SET rel_path=? WHERE id=?",
                            (nxt, int(r["id"])))
                moved += 1
            except Exception:                                  # noqa: BLE001
                # The destination already has a grant to the same target, so
                # the unique index refuses the update. Dropping the stale row
                # is right: the target already reaches the item.
                con.execute("DELETE FROM shares WHERE id=?", (int(r["id"]),))
        con.commit()
        return moved
    finally:
        con.close()


def _drop_shares(owner_id: int, rel: str) -> int:
    """Take the grants off an item that is going into the trash.

    Trashing something revokes access to it. The alternative (rewriting the
    grants to the trash path) would leave other people reading a file its
    owner believes they have thrown away.
    """
    con = _db.connect()
    try:
        rows = con.execute("SELECT id, rel_path FROM shares WHERE owner_id=?",
                           (int(owner_id),)).fetchall()
        gone = 0
        for r in rows:
            p = r["rel_path"]
            if p == rel or (rel and p.startswith(rel + "/")):
                con.execute("DELETE FROM shares WHERE id=?", (int(r["id"]),))
                gone += 1
        con.commit()
        return gone
    finally:
        con.close()


# --------------------------------------------------------------------------
# activity
# --------------------------------------------------------------------------

def record(actor_id, kind: str, owner_id, rel: str, detail=None) -> None:
    """One line in the org's feed. Never raises: a feed is not worth an error.

    Grade edits are not recorded here. They are already commits in the
    project history with an author on each one, and writing them twice would
    make the feed a worse copy of the History panel.
    """
    try:
        _ensure()
        con = _db.connect()
        try:
            con.execute(
                "INSERT INTO activity (org_id, actor_id, ts, kind, owner_id, "
                "rel_path, detail_json) VALUES (?,?,?,?,?,?,?)",
                (org_of(actor_id), int(actor_id or 0), time.time(), kind,
                 int(owner_id or 0), norm_rel(rel),
                 json.dumps(detail or {})))
            con.commit()
        finally:
            con.close()
    except Exception:                                          # noqa: BLE001
        pass


def activity_feed(user_id, limit: int = 50) -> dict:
    """The org's events this account may see, newest first.

    Visibility is the same rule as everywhere else: your own library, plus
    anything covered by a grant you hold. The scan is bounded rather than
    unbounded, because filtering in Python means reading rows the caller may
    not be allowed to see, and a feed is not a reason to read the whole table.
    """
    _ensure()
    uid = int(user_id or 0)
    limit = max(1, min(int(limit or 50), 200))
    org = org_of(uid)
    grants = grants_held(uid)
    con = _db.connect()
    try:
        rows = con.execute(
            "SELECT * FROM activity WHERE org_id=? ORDER BY id DESC LIMIT ?",
            (org, limit * 20)).fetchall()
    finally:
        con.close()
    out = []
    for r in rows:
        owner_id = int(r["owner_id"])
        if _auth.enabled() and owner_id != uid:
            if best_role(uid, owner_id, r["rel_path"], grants) is None:
                continue
        try:
            detail = json.loads(r["detail_json"] or "{}")
        except json.JSONDecodeError:
            detail = {}
        out.append({"id": int(r["id"]), "ts": float(r["ts"]),
                    "kind": r["kind"], "actor_id": int(r["actor_id"]),
                    "actor": user_name(r["actor_id"]),
                    "owner_id": owner_id, "owner": user_name(owner_id),
                    "path": r["rel_path"],
                    "name": detail.get("name") or Path(r["rel_path"]).name,
                    "detail": detail,
                    "mine": owner_id == uid})
        if len(out) >= limit:
            break
    return {"activity": out, "limit": limit}


# --------------------------------------------------------------------------
# listing
# --------------------------------------------------------------------------

def parse_root(user_id, root: str) -> dict:
    """Turn the `root` query value into what the listing needs.

    `mine` is this account's library. `shared` and `team:<id>` are index
    views: they have no folder of their own, they list grants. `user:<id>` is
    somebody else's library, browsable exactly as far as a grant reaches.
    `trash` is this account's own trash.
    """
    raw = str(root or "mine").strip() or "mine"
    uid = int(user_id or 0)
    if raw in ("mine", "me", "my"):
        return {"kind": "mine", "owner_id": uid}
    if raw == "shared":
        return {"kind": "shared", "owner_id": uid}
    if raw == "trash":
        return {"kind": "trash", "owner_id": uid}
    if raw.startswith("user:"):
        who = raw[5:].strip()
        if not who.isdigit():
            raise ValueError("a user root is user:<id>")
        owner = int(who)
        if owner == uid:
            return {"kind": "mine", "owner_id": uid}
        return {"kind": "user", "owner_id": owner}
    if raw.startswith("team:"):
        who = raw[5:].strip()
        if not who.isdigit():
            raise ValueError("a team root is team:<id>")
        return {"kind": "team", "owner_id": uid, "team_id": int(who)}
    raise ValueError("root is mine, shared, trash, team:<id> or user:<id>")


def _probe(path: Path) -> dict | None:
    if _probe_fn is None:
        return None
    try:
        st = path.stat()
    except OSError:
        return None
    ck = (os.path.realpath(str(path)), st.st_size, st.st_mtime_ns)
    with _probe_lock:
        if ck in _probe_cache:
            return _probe_cache[ck]
    try:
        info = _probe_fn(str(path))
    except Exception:                                          # noqa: BLE001
        info = None
    with _probe_lock:
        if len(_probe_cache) > 2000:
            _probe_cache.clear()
        _probe_cache[ck] = info
    return info


def _project_of(path: Path) -> dict | None:
    """The project head for one file, or None if nobody has graded it.

    The whole point of the arc's "when people make updates it shows up in
    history": the project is keyed by the file's CONTENT, so the same clip
    uploaded by two people is one history, and a listing can show its head
    without knowing who owns the file.
    """
    try:
        key = _grades.safe_key(path)
        if not key:
            return None
        row = _projects.get(key)
    except Exception:                                          # noqa: BLE001
        return None
    if not row:
        return None
    return {"key": key, "head": _projects.short(row["head"]),
            "head_full": row["head"], "branch": row["branch"],
            "updated": row["updated"]}


def _shared_index(owner_id: int) -> tuple[set, set]:
    """Which of an owner's paths carry a grant, for the small shared badge.

    Two sets: paths granted exactly, and the folder prefixes those paths sit
    under, so a row can say "something in here is shared" without a query per
    row.
    """
    con = _db.connect()
    try:
        rows = con.execute("SELECT rel_path FROM shares WHERE owner_id=?",
                           (int(owner_id),)).fetchall()
    except Exception:                                          # noqa: BLE001
        return set(), set()
    finally:
        con.close()
    exact = {r["rel_path"] for r in rows}
    under = set()
    for p in exact:
        parts = p.split("/") if p else []
        for i in range(len(parts)):
            under.add("/".join(parts[:i]))
    return exact, under


def _entry_folder(path: Path, rel: str, owner_id: int, root_label: str,
                  exact: set, under: set) -> dict:
    count = 0
    mtime = 0.0
    try:
        mtime = path.stat().st_mtime
        for e in os.scandir(str(path)):
            if e.name.startswith("."):
                continue
            if e.is_dir() or Path(e.name).suffix.lower() in VIDEO_EXT:
                count += 1
    except OSError:
        pass
    return {"name": path.name, "path": rel, "root": root_label,
            "owner_id": owner_id, "kind": "folder", "count": count,
            "mtime": mtime, "missing": False,
            "shared_by_me": rel in exact or rel in under,
            "shared_with_me": False}


def _entry_file(path: Path, rel: str, owner_id: int, root_label: str,
                exact: set, under: set) -> dict:
    try:
        st = path.stat()
        size, mtime = st.st_size, st.st_mtime
    except OSError:
        size, mtime = 0, 0.0
    info = _probe(path) or {}
    return {"name": path.name, "path": rel, "root": root_label,
            "owner_id": owner_id, "kind": "file", "bytes": size,
            "mtime": mtime, "missing": False,
            "key": _grades.safe_key(path),
            "duration": info.get("duration"),
            "width": info.get("width"), "height": info.get("height"),
            "project": _project_of(path),
            "shared_by_me": rel in exact,
            "shared_with_me": False}


def _crumbs(kind: str, owner_id: int, rel: str, root_label: str,
            grant_rel: str | None) -> list[dict]:
    if kind == "trash":
        return [{"name": "Trash", "root": "trash", "path": ""}]
    if kind in ("shared", "team"):
        return [{"name": "Shared with me" if kind == "shared" else "Team",
                 "root": root_label, "path": ""}]
    crumbs = []
    if kind == "mine":
        crumbs.append({"name": "My files", "root": "mine", "path": ""})
        start = 0
        parts = rel.split("/") if rel else []
    else:
        # Somebody else's library: the walk starts at the grant, because
        # there is nothing above it this account is allowed to see.
        base = grant_rel or ""
        crumbs.append({"name": "Shared with me", "root": "shared", "path": ""})
        crumbs.append({"name": Path(base).name or user_name(owner_id),
                       "root": root_label, "path": base})
        parts = rel.split("/") if rel else []
        start = len(base.split("/")) if base else 0
    walked = parts[:start]
    for seg in parts[start:]:
        walked = walked + [seg]
        crumbs.append({"name": seg, "root": root_label,
                       "path": "/".join(walked)})
    return crumbs


def listing(user_id, root: str = "mine", path: str = "") -> dict:
    """One folder, or one index view. The whole read side of the library."""
    _ensure()
    uid = int(user_id or 0)
    spec = parse_root(uid, root)
    kind = spec["kind"]
    rel = norm_rel(path)

    if kind == "shared":
        return _listing_grants(uid, None)
    if kind == "team":
        return _listing_grants(uid, spec["team_id"])
    if kind == "trash":
        return _listing_trash(uid)

    owner_id = int(spec["owner_id"])
    grants = grants_held(uid)
    role = require_read(uid, owner_id, rel, grants)
    grant_rel = None
    if role != "owner":
        # The shallowest grant that covers this path is the top of the tree
        # for this caller: everything above it is somebody else's business.
        for g in grants:
            if int(g["owner_id"]) == owner_id and _covers(g["rel_path"], rel,
                                                          g["kind"]):
                if grant_rel is None or len(g["rel_path"]) < len(grant_rel):
                    grant_rel = g["rel_path"]

    root_label = "mine" if owner_id == uid else f"user:{owner_id}"
    base = ensure_root(owner_id) if owner_id == uid else root_for(owner_id)
    here = confine(base, rel)
    out = {"root": root_label, "path": rel, "owner_id": owner_id,
           "owner": user_name(owner_id), "role": role,
           "can_edit": role in ("owner", "editor"),
           "parent": None, "folders": [], "files": [], "error": "",
           "crumbs": _crumbs("mine" if owner_id == uid else "user", owner_id,
                             rel, root_label, grant_rel)}
    if rel and (grant_rel is None or rel != grant_rel):
        out["parent"] = rel.rsplit("/", 1)[0] if "/" in rel else ""
    if not here.exists():
        out["error"] = "that folder is not there any more"
        out["missing"] = True
        return out
    if not here.is_dir():
        raise ValueError("that is a file, not a folder")

    exact, under = _shared_index(owner_id)
    try:
        entries = sorted(list(os.scandir(str(here))),
                         key=lambda e: e.name.lower())
    except PermissionError:
        out["error"] = f"no permission to read {rel or 'the library root'}"
        return out
    except OSError as exc:
        out["error"] = str(exc)
        return out
    for e in entries:
        if e.name.startswith("."):
            continue
        child = Path(e.path)
        child_rel = f"{rel}/{e.name}" if rel else e.name
        try:
            if e.is_dir():
                out["folders"].append(_entry_folder(child, child_rel, owner_id,
                                                    root_label, exact, under))
            elif child.suffix.lower() in VIDEO_EXT:
                out["files"].append(_entry_file(child, child_rel, owner_id,
                                                root_label, exact, under))
        except OSError:
            continue
    if owner_id != uid:
        for row in out["folders"] + out["files"]:
            row["shared_with_me"] = True
            row["shared_by_me"] = False
    return out


def _listing_grants(user_id, team_id: int | None) -> dict:
    """The `shared` and `team:<id>` index views.

    Neither is a folder on disk: they are the list of grants this account
    holds, shown as top level rows. Each row says which real root it opens
    into (`user:<owner>`), so the client navigates into a share exactly the
    way it navigates into a folder.
    """
    uid = int(user_id or 0)
    root_label = "shared" if team_id is None else f"team:{team_id}"
    out = {"root": root_label, "path": "", "owner_id": uid,
           "owner": user_name(uid), "role": "viewer", "can_edit": False,
           "parent": None, "folders": [], "files": [], "error": "",
           "crumbs": _crumbs("shared" if team_id is None else "team", uid, "",
                             root_label, None)}
    if team_id is not None:
        team = get_team(team_id)
        if team is None or team["org_id"] != org_of(uid):
            raise ValueError("no such team in this org")
        if _auth.enabled() and team_id not in teams_of(uid) \
                and not _is_org_admin(uid, team["org_id"]):
            raise _auth.AuthError(403, "you are not in that team")
        out["crumbs"][0]["name"] = team["name"]
    best: dict[tuple, dict] = {}
    for g in grants_held(uid):
        if team_id is not None and not (g["via"]["kind"] == "team"
                                        and g["via"]["id"] == team_id):
            continue
        ck = (int(g["owner_id"]), g["rel_path"])
        prev = best.get(ck)
        if prev and ROLE_RANK[prev["role"]] >= ROLE_RANK[g["role"]]:
            continue
        best[ck] = g
    for (owner_id, rel), g in sorted(
            best.items(), key=lambda kv: (user_name(kv[0][0]).lower(),
                                          kv[0][1].lower())):
        base = root_for(owner_id)
        try:
            path = confine(base, rel)
        except _auth.AuthError:
            continue
        label = f"user:{owner_id}"
        name = Path(rel).name or f"{user_name(owner_id)}'s library"
        if not path.exists():
            # Renamed or deleted outside the app. Reported, not hidden: a
            # share that silently vanishes is a share nobody can debug.
            row = {"name": name, "path": rel, "root": label,
                   "owner_id": owner_id, "owner": user_name(owner_id),
                   "kind": g["kind"], "missing": True, "role": g["role"],
                   "via": g["via"], "share_id": g["id"],
                   "shared_with_me": True, "shared_by_me": False,
                   "count": 0, "mtime": 0.0}
            (out["folders"] if g["kind"] == "folder" else out["files"]).append(row)
            continue
        if path.is_dir():
            row = _entry_folder(path, rel, owner_id, label, set(), set())
            out["folders"].append(row)
        else:
            row = _entry_file(path, rel, owner_id, label, set(), set())
            out["files"].append(row)
        row["name"] = name
        row["owner"] = user_name(owner_id)
        row["role"] = g["role"]
        row["via"] = g["via"]
        row["share_id"] = g["id"]
        row["shared_with_me"] = True
        row["shared_by_me"] = False
    return out


# --------------------------------------------------------------------------
# writes
# --------------------------------------------------------------------------

def _writable(user_id, root: str, rel: str) -> tuple[int, str, Path]:
    """Resolve a write target: (owner id, rel path, absolute path).

    Every write goes through this, so the confinement and the editor check
    are stated once rather than in each of the six operations below.
    """
    spec = parse_root(user_id, root)
    if spec["kind"] in ("shared", "team", "trash"):
        raise ValueError("pick a real folder first: that view is a list of "
                         "shares, not a place files live")
    owner_id = int(spec["owner_id"])
    rel = visible_rel(norm_rel(rel))
    require_edit(user_id, owner_id, rel)
    base = ensure_root(owner_id)
    return owner_id, rel, confine(base, rel)


def mkdir(user_id, root: str, path: str, name: str) -> dict:
    owner_id, rel, parent = _writable(user_id, root, path)
    name = check_name(name)
    if not parent.is_dir():
        raise ValueError("that folder is not there")
    target = confine(root_for(owner_id), f"{rel}/{name}" if rel else name)
    if target.exists():
        raise ValueError(f"there is already something called {name} here")
    target.mkdir(parents=False)
    new_rel = rel_of(root_for(owner_id), target) or name
    record(user_id, "mkdir", owner_id, new_rel, {"name": name})
    return {"ok": True, "path": new_rel, "name": name,
            "root": "mine" if owner_id == int(user_id or 0)
                    else f"user:{owner_id}"}


def rename(user_id, root: str, path: str, name: str) -> dict:
    owner_id, rel, src = _writable(user_id, root, path)
    if not rel:
        raise ValueError("the library root itself cannot be renamed")
    name = check_name(name)
    if not src.exists():
        raise ValueError("that item is not there any more")
    parent_rel = rel.rsplit("/", 1)[0] if "/" in rel else ""
    new_rel = f"{parent_rel}/{name}" if parent_rel else name
    dst = confine(root_for(owner_id), new_rel)
    if dst == src:
        return {"ok": True, "path": rel, "name": src.name, "shares": 0}
    if dst.exists():
        raise ValueError(f"there is already something called {name} here")
    src.rename(dst)
    moved = _rewrite_shares(owner_id, rel, new_rel)
    record(user_id, "rename", owner_id, new_rel,
           {"from": rel, "name": name, "was": Path(rel).name})
    return {"ok": True, "path": new_rel, "name": name, "shares": moved,
            "root": "mine" if owner_id == int(user_id or 0)
                    else f"user:{owner_id}"}


def move(user_id, root: str, path: str, to: str) -> dict:
    owner_id, rel, src = _writable(user_id, root, path)
    if not rel:
        raise ValueError("the library root itself cannot be moved")
    if not src.exists():
        raise ValueError("that item is not there any more")
    to_rel = visible_rel(norm_rel(to))
    require_edit(user_id, owner_id, to_rel)
    dest_dir = confine(root_for(owner_id), to_rel)
    if not dest_dir.is_dir():
        raise ValueError("the destination is not a folder")
    if src.is_dir():
        # Moving a folder into itself or into one of its own children would
        # make the tree unreachable, and shutil would happily do it.
        if to_rel == rel or to_rel.startswith(rel + "/"):
            raise ValueError("a folder cannot be moved inside itself")
    new_rel = f"{to_rel}/{src.name}" if to_rel else src.name
    if new_rel == rel:
        return {"ok": True, "path": rel, "shares": 0}
    dst = confine(root_for(owner_id), new_rel)
    if dst.exists():
        raise ValueError(f"there is already something called {src.name} there")
    src.rename(dst)
    moved = _rewrite_shares(owner_id, rel, new_rel)
    record(user_id, "move", owner_id, new_rel,
           {"from": rel, "to": to_rel, "name": src.name})
    return {"ok": True, "path": new_rel, "name": src.name, "shares": moved,
            "root": "mine" if owner_id == int(user_id or 0)
                    else f"user:{owner_id}"}


def _trash_encode(rel: str) -> str:
    """A trashed item's file name, holding where it came from.

    `<ts>_<rel with % and / escaped>`. The escaping is reversible and, for the
    common case of a file at the top of a library, changes nothing: the entry
    is literally `<ts>_<name>`, which is what the plan describes. Doing it
    this way means the trash needs no table of its own and no sidecar file
    sitting in somebody's footage folder.
    """
    safe = rel.replace("%", "%25").replace("/", "%2F")
    return f"{int(time.time())}_{safe}"


def _trash_decode(entry: str) -> tuple[float, str]:
    ts, _, rest = entry.partition("_")
    orig = rest.replace("%2F", "/").replace("%25", "%")
    return (float(ts) if ts.isdigit() else 0.0), orig


def trash(user_id, root: str, path: str) -> dict:
    """Move an item into the library's own trash. Nothing is ever deleted."""
    owner_id, rel, src = _writable(user_id, root, path)
    if not rel:
        raise ValueError("the library root itself cannot be trashed")
    if not src.exists():
        raise ValueError("that item is not there any more")
    base = root_for(owner_id)
    trash_dir = base / TRASH
    trash_dir.mkdir(parents=True, exist_ok=True)
    entry = _trash_encode(rel)
    dst = confine(base, f"{TRASH}/{entry}")
    n = 1
    while dst.exists():
        dst = confine(base, f"{TRASH}/{entry}~{n}")
        n += 1
    src.rename(dst)
    dropped = _drop_shares(owner_id, rel)
    record(user_id, "trash", owner_id, rel,
           {"name": Path(rel).name, "entry": dst.name, "shares": dropped})
    return {"ok": True, "path": rel, "entry": dst.name, "shares": dropped,
            "name": Path(rel).name}


def _listing_trash(user_id) -> dict:
    uid = int(user_id or 0)
    base = ensure_root(uid)
    out = {"root": "trash", "path": "", "owner_id": uid,
           "owner": user_name(uid), "role": "owner", "can_edit": True,
           "parent": None, "folders": [], "files": [], "error": "",
           "crumbs": _crumbs("trash", uid, "", "trash", None)}
    trash_dir = base / TRASH
    if not trash_dir.is_dir():
        return out
    for e in sorted(list(os.scandir(str(trash_dir))),
                    key=lambda x: x.name, reverse=True):
        ts, orig = _trash_decode(e.name)
        p = Path(e.path)
        row = {"name": Path(orig).name or e.name, "path": e.name,
               "root": "trash", "owner_id": uid, "orig": orig,
               "trashed_at": ts, "missing": False,
               "shared_by_me": False, "shared_with_me": False}
        try:
            row["bytes"] = p.stat().st_size if p.is_file() else 0
            row["mtime"] = p.stat().st_mtime
        except OSError:
            row["bytes"], row["mtime"] = 0, 0.0
        if p.is_dir():
            row["kind"] = "folder"
            out["folders"].append(row)
        else:
            row["kind"] = "file"
            row["key"] = _grades.safe_key(p)
            out["files"].append(row)
    return out


def restore(user_id, path: str, root: str = "trash") -> dict:
    """Put a trashed item back where it came from.

    `path` is the trash entry's own name, which is what the trash listing
    hands out. The original location is decoded from that name; if a new file
    has taken the spot in the meantime the restore lands beside it with a
    suffix rather than overwriting it.
    """
    uid = int(user_id or 0)
    base = ensure_root(uid)
    entry = norm_rel(path)
    if entry.startswith(TRASH + "/"):
        entry = entry[len(TRASH) + 1:]
    if "/" in entry:
        raise ValueError("a trash entry is one name, not a path")
    src = confine(base, f"{TRASH}/{entry}")
    if not src.exists():
        raise ValueError("that item is not in the trash")
    _ts, orig = _trash_decode(entry)
    orig = norm_rel(orig) or src.name
    dst = confine(base, orig)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        stem, suffix = Path(orig).stem, Path(orig).suffix
        parent = orig.rsplit("/", 1)[0] if "/" in orig else ""
        n = 1
        while dst.exists():
            name = f"{stem}_restored{'' if n == 1 else n}{suffix}"
            orig = f"{parent}/{name}" if parent else name
            dst = confine(base, orig)
            n += 1
    src.rename(dst)
    record(uid, "restore", uid, orig, {"name": Path(orig).name,
                                       "entry": entry})
    return {"ok": True, "path": orig, "name": Path(orig).name, "root": "mine"}


def upload_target(user_id, root=None, path=None) -> tuple[Path, int, str]:
    """Where POST /api/upload writes: (folder, owner id, rel folder).

    With no root and no path this is exactly what the upload did before this
    arc, which is what keeps the old client and the old tests working
    unchanged.
    """
    uid = int(user_id or 0)
    if not root and not path:
        return ensure_root(uid), uid, ""
    owner_id, rel, dest = _writable(uid, root or "mine", path or "")
    dest.mkdir(parents=True, exist_ok=True)
    return dest, owner_id, rel


def open_target(user_id, root: str, path: str) -> tuple[Path, int, str, str]:
    """Resolve {root, path} to a real file the caller may READ.

    Used by POST /api/project/open and GET /api/thumb, which both take either
    a clip name (the old way) or a library item (the new way). Returns
    (absolute path, owner id, rel path, role).
    """
    spec = parse_root(user_id, root)
    if spec["kind"] in ("shared", "team"):
        raise ValueError("that view is a list of shares: open the item "
                         "itself with root=user:<id>")
    owner_id = int(spec["owner_id"])
    rel = norm_rel(path)
    base = root_for(owner_id)
    if spec["kind"] == "trash":
        target = confine(base, f"{TRASH}/{rel}" if not rel.startswith(TRASH)
                         else rel)
        role = "owner"
    else:
        role = require_read(user_id, owner_id, rel)
        target = confine(base, rel)
    if not target.exists():
        raise ValueError("there is no file at that path")
    if target.is_dir():
        raise ValueError("that is a folder, not a clip")
    return target, owner_id, rel, role
