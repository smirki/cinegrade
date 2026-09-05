#!/usr/bin/env python3
"""Per clip, per user grades: the identity of a clip and the storage of its look.

Until this file existed the studio held ONE grade. Switching clips kept
whatever was on screen, so a look built for a night interior followed you onto
a beach shot and nobody was told. This module gives every clip its own saved
grade, and every account its own copy of it.

Two ideas do the work.

1. A clip is identified by its CONTENT, not by its name or its path. The key is
   the first 32 hex characters of

       sha256(size as 8 byte big endian || first 1 MiB || last 1 MiB)

   so renaming a file, moving it to another folder, or opening it from a
   different mount all land on the same grade. It is not a full file hash on
   purpose: hashing 4 GB of ProRes to decide which row to read would make
   listing a folder take minutes. Reading 2 MiB from each end plus the exact
   byte length is enough to separate real footage: two different takes differ
   in the container header inside the first megabyte, and a truncated or
   re-wrapped copy differs in length. What it does NOT survive is a re-encode:
   a transcoded copy is a different file and gets a different grade. That is
   listed in the honesty list rather than hidden.

2. Everything is scoped by user_id, and user_id 0 is the local no-login user.
   With logins off the server hands every route the same id 0, so per clip
   grades work exactly the same on a laptop with no accounts as they do on a
   network deployment with ten.

Standard library only, same as the rest of the studio. The tables live in the
same SQLite file db.py owns (studio/data/studio.db, gitignored) and are created
here with CREATE TABLE IF NOT EXISTS, per contract C1: db.py owns the auth
tables, every other module owns its own.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
from pathlib import Path

STUDIO = Path(__file__).resolve().parent
# Same reason server.py does it: so `import db` resolves however this file was
# reached, whether as part of the server or from a test that imported it alone.
if str(STUDIO) not in sys.path:
    sys.path.insert(0, str(STUDIO))
import db as _db  # noqa: E402  (path has to be set first)

# Derived from db.DATA rather than STUDIO so a test data directory
# (STUDIO_DATA_DIR) moves the per user preset folders with it.
USERS_DIR = _db.DATA / "users"

# 1 MiB from the head and 1 MiB from the tail. On a file smaller than 2 MiB the
# two windows overlap and some bytes are hashed twice, which changes nothing
# about correctness: the same file always produces the same digest.
CHUNK = 1024 * 1024
KEY_LEN = 32

# No FOREIGN KEY on user_id, deliberately. The local no-login user is id 0 and
# has no row in `users`, so a foreign key would reject every grade made on a
# laptop without accounts, which is the common case. The cost of leaving it out
# is that deleting an account leaves its grades behind as orphan rows; they are
# a few kB of JSON and invisible to every other account, and delete_user in
# auth.py can clean them up later if that ever matters.
SCHEMA = """
CREATE TABLE IF NOT EXISTS grades (
  user_id     INTEGER NOT NULL,
  clip_key    TEXT NOT NULL,
  clip_name   TEXT NOT NULL DEFAULT '',
  config_json TEXT NOT NULL,
  updated_at  REAL NOT NULL,
  PRIMARY KEY (user_id, clip_key)
);

CREATE TABLE IF NOT EXISTS clip_keys (
  realpath  TEXT NOT NULL,
  size      INTEGER NOT NULL,
  mtime_ns  INTEGER NOT NULL,
  clip_key  TEXT NOT NULL,
  created_at REAL NOT NULL,
  PRIMARY KEY (realpath, size, mtime_ns)
);
"""

_mem: dict[tuple[str, int, int], str] = {}
_mem_lock = threading.Lock()

_inited = False
_init_lock = threading.Lock()


def init_schema() -> None:
    """Create the grade tables if they are not there yet. Safe to call often."""
    global _inited
    with _init_lock:
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
# clip identity
# --------------------------------------------------------------------------

def _digest(path: str, size: int) -> str:
    h = hashlib.sha256()
    # The length goes in first and as a fixed width field, so a file whose
    # first megabyte happens to match another's cannot collide just because
    # the rest of it is a different length.
    h.update(size.to_bytes(8, "big"))
    with open(path, "rb") as fh:
        h.update(fh.read(min(CHUNK, size)))
        if size > CHUNK:
            fh.seek(max(0, size - CHUNK))
            h.update(fh.read(CHUNK))
    return h.hexdigest()[:KEY_LEN]


def clip_key(path) -> str:
    """The content key for one file, cached in memory and on disk.

    Cached by (realpath, size, mtime) rather than by path alone: editing a file
    in place keeps its path and would otherwise keep serving the old key and
    therefore the old clip's grade. Two caches on purpose. The in memory one
    makes a repeat listing free inside one server run; the SQLite one makes the
    FIRST listing after a restart free too, which is the case that actually
    hurt (a folder of thirty 4K clips is thirty 2 MiB reads).
    """
    rp = os.path.realpath(str(path))
    st = os.stat(rp)
    ck = (rp, st.st_size, st.st_mtime_ns)
    with _mem_lock:
        hit = _mem.get(ck)
    if hit:
        return hit
    _ensure()
    con = _db.connect()
    try:
        row = con.execute(
            "SELECT clip_key FROM clip_keys WHERE realpath=? AND size=? "
            "AND mtime_ns=?", (rp, st.st_size, st.st_mtime_ns)).fetchone()
        if row:
            key = row["clip_key"]
        else:
            key = _digest(rp, st.st_size)
            con.execute(
                "INSERT OR REPLACE INTO clip_keys "
                "(realpath, size, mtime_ns, clip_key, created_at) "
                "VALUES (?,?,?,?,?)",
                (rp, st.st_size, st.st_mtime_ns, key, time.time()))
            con.commit()
    finally:
        con.close()
    with _mem_lock:
        _mem[ck] = key
    return key


def safe_key(path) -> str:
    """clip_key, but a file that cannot be read gives "" instead of raising.

    Used by the listing endpoints: one unreadable file in a browsed folder
    should cost that file its key, not the whole folder its listing.
    """
    try:
        return clip_key(path)
    except OSError:
        return ""


def is_key(value: str) -> bool:
    v = (value or "").strip().lower()
    return len(v) == KEY_LEN and all(c in "0123456789abcdef" for c in v)


# --------------------------------------------------------------------------
# grades
# --------------------------------------------------------------------------

def get_grade(user_id: int, key: str) -> dict | None:
    _ensure()
    con = _db.connect()
    try:
        row = con.execute(
            "SELECT clip_name, config_json, updated_at FROM grades "
            "WHERE user_id=? AND clip_key=?", (int(user_id), key)).fetchone()
    finally:
        con.close()
    if not row:
        return None
    try:
        config = json.loads(row["config_json"])
    except json.JSONDecodeError:
        return None
    return {"clip_key": key, "clip_name": row["clip_name"], "config": config,
            "updated_at": row["updated_at"]}


def put_grade(user_id: int, key: str, clip_name: str, config: dict) -> float:
    """Store one clip's whole config. Returns the timestamp written."""
    _ensure()
    now = time.time()
    con = _db.connect()
    try:
        con.execute(
            "INSERT INTO grades (user_id, clip_key, clip_name, config_json, "
            "updated_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(user_id, clip_key) DO UPDATE SET "
            "clip_name=excluded.clip_name, config_json=excluded.config_json, "
            "updated_at=excluded.updated_at",
            (int(user_id), key, clip_name or "",
             json.dumps(config, allow_nan=False), now))
        con.commit()
    finally:
        con.close()
    return now


def list_grades(user_id: int) -> list[dict]:
    """Every clip this account has a saved grade for, newest first.

    The config is deliberately not included: this feeds a picker, and sending
    thirty full configs to fill a dropdown is thirty times the payload for
    information the dropdown does not show.
    """
    _ensure()
    con = _db.connect()
    try:
        rows = con.execute(
            "SELECT clip_key, clip_name, updated_at FROM grades "
            "WHERE user_id=? ORDER BY updated_at DESC",
            (int(user_id),)).fetchall()
    finally:
        con.close()
    return [{"clip_key": r["clip_key"], "clip_name": r["clip_name"],
             "updated_at": r["updated_at"]} for r in rows]


def delete_grade(user_id: int, key: str) -> bool:
    _ensure()
    con = _db.connect()
    try:
        cur = con.execute("DELETE FROM grades WHERE user_id=? AND clip_key=?",
                          (int(user_id), key))
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


# --------------------------------------------------------------------------
# per user preset folder
# --------------------------------------------------------------------------

def user_presets_dir(user_id: int) -> Path:
    """Where this account's own presets live.

    grade/presets/ is a shipped library that is checked into the repository
    and shared by every account, so a save there would be a shared edit to a
    tracked file. A user's own saves go here instead, under the same gitignored
    studio/data/users/<id>/ tree the account's footage uses. User 0 (logins
    off) gets a folder like everybody else, which is what stops the founder's
    local "save as" from writing into the shipped library.
    """
    d = USERS_DIR / str(int(user_id)) / "presets"
    d.mkdir(parents=True, exist_ok=True)
    return d
