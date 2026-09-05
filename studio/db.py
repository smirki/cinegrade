#!/usr/bin/env python3
"""One SQLite file for everything the studio owns about its users.

Standard library only, like the rest of the studio: sqlite3 ships with
Python, so adding accounts costs no install step and no service to run.

The file lives at studio/data/studio.db, which is gitignored. That folder
holds user owned data (accounts now, per user footage and presets later), so
it is deliberately outside the tracked tree: a password hash has no business
in a git history, and neither does somebody's footage.

Threading note. The studio's HTTP server is a ThreadingHTTPServer, so several
requests touch this database at once. sqlite3 connections are not safe to
share across threads, so `connect()` hands out a FRESH connection every call
and the caller closes it. That sounds wasteful and is not: opening a SQLite
file is a couple of syscalls, and the alternative (one shared connection with
a lock around it) serialises every request behind the slowest query.

WAL mode is what makes concurrent readers work at all here. In the default
rollback journal a writer blocks every reader; in WAL a writer blocks only
other writers. journal_mode is a persistent property of the database file, so
setting it on each connection is a no-op after the first, but it costs
nothing and means a database created by any code path is in the right mode.

Other modules (per clip grades in wave 2, uploads after that) create their
own tables in their own init function with CREATE TABLE IF NOT EXISTS. This
module owns the auth tables and nothing else: accounts, sessions, agent
tokens, and the `orgs` an account belongs to, which is here rather than in
library.py because it adds a column to `users` and this file owns that table.
The library's own tables (teams, shares, activity) are library.py's.
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

STUDIO = Path(__file__).resolve().parent
# studio/data unless somebody says otherwise. The override exists for tests:
# this database is somebody's real accounts, grades and project history, and a
# test suite must never open it. STUDIO_DATA_DIR (or server.py --data-dir,
# which sets it) points the whole data folder somewhere temporary; with the
# variable unset the path is exactly what it always was.
DATA = Path(os.environ.get("STUDIO_DATA_DIR") or (STUDIO / "data"))
DB_PATH = DATA / "studio.db"


def set_data_dir(path) -> Path:
    """Point the data folder somewhere else. Call before anything connects.

    Rebinds the module globals rather than handing every caller a new path,
    because connect() reads DB_PATH on each call and the other modules read
    db.DATA, so one assignment moves all of them.
    """
    global DATA, DB_PATH
    DATA = Path(path).expanduser().resolve()
    DB_PATH = DATA / "studio.db"
    os.environ["STUDIO_DATA_DIR"] = str(DATA)
    return DATA

# Auth tables only, per contract C1. Every statement is IF NOT EXISTS so
# init_schema() is safe to call on every boot.
#
# Tokens (session cookies and agent tokens alike) are stored as a SHA-256 of
# the secret, never the secret itself. A stolen database file then does not
# hand the thief live sessions. SHA-256 rather than scrypt for these two,
# on purpose: a session token is 32 bytes from os.urandom, so there is no
# dictionary to attack and no reason to pay 32 MB of scrypt on every single
# request. Passwords are different (a human chose them) and do get scrypt.
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  name       TEXT NOT NULL COLLATE NOCASE UNIQUE,
  role       TEXT NOT NULL DEFAULT 'user',
  password   TEXT NOT NULL,
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY,
  user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at REAL NOT NULL,
  last_seen  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS tokens (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  token_hash TEXT NOT NULL UNIQUE,
  user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  label      TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  last_used  REAL
);
CREATE INDEX IF NOT EXISTS tokens_user ON tokens(user_id);

CREATE TABLE IF NOT EXISTS orgs (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  name       TEXT NOT NULL COLLATE NOCASE UNIQUE,
  created_at REAL NOT NULL
);
"""


def connect() -> sqlite3.Connection:
    """A fresh connection, WAL, foreign keys on, rows as sqlite3.Row.

    The caller closes it. `with db.connect() as con` does NOT close in
    sqlite3 (the context manager is a transaction, not the connection), so
    the callers here use try/finally.
    """
    DATA.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_PATH), timeout=10.0)
    con.row_factory = sqlite3.Row
    # busy_timeout matters more than it looks: two threads writing at the
    # same moment would otherwise raise "database is locked" immediately
    # instead of waiting the fraction of a millisecond the other write takes.
    con.execute("PRAGMA busy_timeout=10000")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def init_schema() -> None:
    """Create the auth tables if they are not there yet, then migrate.

    Both halves are safe to run on every boot and on a database that has
    already been through them, which is what makes this callable from four
    places without any of them coordinating.
    """
    con = connect()
    try:
        con.executescript(SCHEMA)
        _migrate(con)
        con.commit()
    finally:
        con.close()


def _migrate(con) -> None:
    """The additive steps a CREATE TABLE IF NOT EXISTS cannot express.

    Two of them, both from the library arc, both idempotent:

    1. Org 1 named `default` exists. Every account belongs to exactly one org
       and this is the one they all start in, so a server that has never
       heard of orgs behaves afterwards exactly as it did before: one tenant,
       everybody in it.
    2. `users.org_id` exists, defaulting to 1. ALTER TABLE ADD COLUMN is the
       only way to add a column to a table that already holds somebody's
       accounts, and SQLite allows NOT NULL there as long as the default is a
       constant, which 1 is. The column is checked for first rather than
       added inside a try, because "duplicate column name" is an error string
       to parse and PRAGMA table_info is an answer.

    Nothing here rewrites, drops or reorders an existing row or column, so
    running it against a real database cannot lose anything.
    """
    if con.execute("SELECT COUNT(*) AS n FROM orgs").fetchone()["n"] == 0:
        con.execute("INSERT INTO orgs (id, name, created_at) VALUES (1, ?, ?)",
                    ("default", time.time()))
    cols = {r["name"] for r in con.execute("PRAGMA table_info(users)")}
    if "org_id" not in cols:
        con.execute("ALTER TABLE users ADD COLUMN org_id INTEGER NOT NULL "
                    "DEFAULT 1")
