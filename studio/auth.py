#!/usr/bin/env python3
"""Accounts, sessions and agent tokens for Fixxr Studio.

Standard library only. Everything here is off by default: with auth disabled
the studio behaves exactly as it did before this file existed, because the
server asks `enabled()` first and takes the old path when the answer is no.
That is the point. The founder runs this on 127.0.0.1 with no login, and
nothing about that changes; auth exists so the same program can also be put
on a network without handing every device on it a shell-adjacent file
browser.

What this module is honest about:

- The built in HTTP server speaks plain HTTP. A password posted to it crosses
  the network in the clear unless a TLS reverse proxy sits in front. There is
  no way to fix that inside this file; it is recorded in the Limits panel.
- The rate limiter is a dict in this process. It resets when the server
  restarts and it is not shared between processes. It stops password
  guessing at browser speed, not a determined distributed attacker.
- Agent tokens do not expire. Contract C2 says so deliberately: an agent
  running for days should not have its token die mid run. Deleting the token
  is the revocation story.

Password hashing is scrypt with the parameters contract C2 fixes: n=2**15,
r=8, p=1, a 16 byte salt, stored as `scrypt$n$r$p$salt_b64$hash_b64`. Those
parameters cost about 32 MB of memory per hash, which is the whole idea: it
makes a GPU cracking rig expensive per guess. It also means the default
OpenSSL maxmem (32 MB) is a hair too small for it, so maxmem is passed
explicitly below. Without that you get "Invalid parameter combination for
n, r, p" out of hashlib, which reads like a parameter bug and is not one.

Session cookies and agent tokens are 32 random bytes from `secrets`, stored
only as a SHA-256 digest (see studio/db.py for why SHA-256 and not scrypt for
those two).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import os
import secrets
import sqlite3
import threading
import time
import urllib.parse
from collections import defaultdict, deque
from http.cookies import SimpleCookie
from pathlib import Path

import db

STUDIO = Path(__file__).resolve().parent
USERS_DIR = STUDIO / "data" / "users"

SESSION_COOKIE = "studio_session"
SESSION_IDLE_SECONDS = 30 * 24 * 3600          # contract C2: 30 days idle

SCRYPT_N = 2 ** 15
SCRYPT_R = 8
SCRYPT_P = 1
# 128 * r * n is scrypt's working set: 128 * 8 * 32768 = 33554432 bytes, which
# is exactly OpenSSL's default cap, so the default rejects these parameters by
# one allocation. Doubling the cap is the documented way through.
SCRYPT_MAXMEM = 128 * 1024 * 1024
SCRYPT_DKLEN = 32

ROLES = ("admin", "user")

# Rate limit window and ceilings, contract C2.
RATE_WINDOW = 300.0                            # 5 minutes
RATE_MAX_PER_USER = 5
RATE_MAX_PER_ADDR = 20

_ENABLED = False
_BEHIND_HTTPS_PROXY = False


class AuthError(Exception):
    """An HTTP status plus a sentence a human can act on.

    Separate from StudioError (which the server turns into a 400) because
    401, 403 and 429 all mean something different to the client: retry with
    credentials, do not retry, and wait respectively.
    """

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------
# module state
# --------------------------------------------------------------------------

def set_enabled(flag: bool) -> None:
    global _ENABLED
    _ENABLED = bool(flag)
    if _ENABLED:
        db.init_schema()


def enabled() -> bool:
    return _ENABLED


def set_behind_https_proxy(flag: bool) -> None:
    global _BEHIND_HTTPS_PROXY
    _BEHIND_HTTPS_PROXY = bool(flag)


def behind_https_proxy() -> bool:
    return _BEHIND_HTTPS_PROXY


def is_loopback(host: str) -> bool:
    """True only for addresses that cannot be reached from another machine.

    An empty host, 0.0.0.0 and :: are all wildcard binds: they listen on
    every interface the box has, so they are the opposite of loopback even
    though they contain no routable address themselves.
    """
    h = (host or "").strip()
    if not h:
        return False
    if h.lower() in ("localhost", "localhost.", "ip6-localhost"):
        return True
    try:
        return ipaddress.ip_address(h.strip("[]")).is_loopback
    except ValueError:
        return False


def trusted_loopback(addr: str) -> bool:
    """is_loopback, but also False whenever a proxy could be lying about it.

    A route gated as "loopback only" (reveal-in-Finder today) is trusting the
    socket's peer address to mean "this is the same computer". That trust is
    correct with no proxy in front, because only a process on this machine
    can open a TCP connection whose source is 127.0.0.1. It stops being
    correct the moment --behind-https-proxy is set: a reverse proxy on this
    same machine, forwarding to this loopback bind, makes every request from
    every device on the network arrive with THAT peer address too, since the
    proxy is what actually opened the socket. At that point the server
    cannot tell a caller on this machine from one on the internet, so a
    loopback-gated route has to refuse everyone rather than trust an address
    it can no longer interpret.
    """
    if behind_https_proxy():
        return False
    return is_loopback(addr)


# --------------------------------------------------------------------------
# passwords
# --------------------------------------------------------------------------

def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_N,
                        r=SCRYPT_R, p=SCRYPT_P, maxmem=SCRYPT_MAXMEM,
                        dklen=SCRYPT_DKLEN)
    return "scrypt$%d$%d$%d$%s$%s" % (SCRYPT_N, SCRYPT_R, SCRYPT_P,
                                      _b64(salt), _b64(dk))


def verify_password(password: str, stored: str) -> bool:
    """Constant time compare against a stored scrypt record.

    Any malformed record returns False rather than raising: a corrupted row
    should lock that one account out, not 500 the login endpoint for
    everybody.
    """
    parts = (stored or "").split("$")
    if len(parts) != 6 or parts[0] != "scrypt":
        return False
    try:
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt = base64.b64decode(parts[4])
        want = base64.b64decode(parts[5])
    except (ValueError, TypeError):
        return False
    try:
        got = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r,
                             p=p, maxmem=SCRYPT_MAXMEM, dklen=len(want))
    except ValueError:
        return False
    return hmac.compare_digest(got, want)


def _digest(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# users
# --------------------------------------------------------------------------

def _row_user(row) -> dict:
    return {"id": row["id"], "name": row["name"], "role": row["role"]}


def create_user(name: str, password: str, role: str = "user") -> dict:
    name = (name or "").strip()
    if not name:
        raise AuthError(400, "a user needs a name")
    if len(name) > 64 or "/" in name or "\\" in name:
        raise AuthError(400, "user names are up to 64 characters and cannot "
                             "contain a slash")
    if role not in ROLES:
        raise AuthError(400, "role must be admin or user")
    if not password:
        raise AuthError(400, "a user needs a password")
    if len(password) < 8:
        raise AuthError(400, "the password must be at least 8 characters")
    db.init_schema()
    con = db.connect()
    try:
        try:
            cur = con.execute(
                "INSERT INTO users (name, role, password, created_at) "
                "VALUES (?, ?, ?, ?)",
                (name, role, hash_password(password), time.time()))
        except sqlite3.IntegrityError:
            raise AuthError(409, f"a user named {name} already exists") from None
        con.commit()
        uid = cur.lastrowid
    finally:
        con.close()
    # Per user folders exist from the moment the account does, so the
    # confinement check below has a real directory to compare against rather
    # than a path that does not resolve.
    (USERS_DIR / str(uid) / "footage").mkdir(parents=True, exist_ok=True)
    return {"id": uid, "name": name, "role": role}


def list_users() -> list[dict]:
    db.init_schema()
    con = db.connect()
    try:
        rows = con.execute("SELECT id, name, role, created_at FROM users "
                           "ORDER BY id").fetchall()
        return [{"id": r["id"], "name": r["name"], "role": r["role"],
                 "created_at": r["created_at"]} for r in rows]
    finally:
        con.close()


def delete_user(name: str) -> bool:
    db.init_schema()
    con = db.connect()
    try:
        cur = con.execute("DELETE FROM users WHERE name = ?", ((name or "").strip(),))
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def user_count() -> int:
    db.init_schema()
    con = db.connect()
    try:
        return int(con.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"])
    finally:
        con.close()


def get_user(user_id: int) -> dict | None:
    con = db.connect()
    try:
        row = con.execute("SELECT id, name, role FROM users WHERE id = ?",
                          (user_id,)).fetchone()
        return _row_user(row) if row else None
    finally:
        con.close()


def check_login(name: str, password: str) -> dict | None:
    """Name plus password to a user record, or None.

    The `name` column is COLLATE NOCASE, so this matches regardless of case,
    which is what a person typing their own name into a login box expects.

    On an unknown user this still pays for one scrypt hash before answering.
    Without that, a missing account answers in microseconds and a real
    account answers in tens of milliseconds, and that difference alone tells
    an attacker which names exist.
    """
    con = db.connect()
    try:
        row = con.execute("SELECT id, name, role, password FROM users "
                          "WHERE name = ?", ((name or "").strip(),)).fetchone()
    finally:
        con.close()
    if row is None:
        hash_password(password or "no such user")
        return None
    if not verify_password(password or "", row["password"]):
        return None
    return _row_user(row)


# --------------------------------------------------------------------------
# sessions
# --------------------------------------------------------------------------

def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    con = db.connect()
    try:
        con.execute("INSERT INTO sessions (token_hash, user_id, created_at, "
                    "last_seen) VALUES (?, ?, ?, ?)",
                    (_digest(token), user_id, now, now))
        # Idle sessions are swept here rather than on a timer thread: the only
        # moment this table grows is a login, so that is the only moment it
        # needs pruning.
        con.execute("DELETE FROM sessions WHERE last_seen < ?",
                    (now - SESSION_IDLE_SECONDS,))
        con.commit()
    finally:
        con.close()
    return token


def session_user(token: str) -> dict | None:
    if not token:
        return None
    now = time.time()
    con = db.connect()
    try:
        row = con.execute(
            "SELECT u.id AS id, u.name AS name, u.role AS role, "
            "       s.last_seen AS last_seen "
            "FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token_hash = ?", (_digest(token),)).fetchone()
        if not row:
            return None
        if now - row["last_seen"] > SESSION_IDLE_SECONDS:
            con.execute("DELETE FROM sessions WHERE token_hash = ?",
                        (_digest(token),))
            con.commit()
            return None
        # Only write the touch once a minute. Every request would otherwise
        # take the write lock, which on a page that fires a dozen requests
        # per scrub turns a read path into a write path for no benefit.
        if now - row["last_seen"] > 60:
            con.execute("UPDATE sessions SET last_seen = ? WHERE token_hash = ?",
                        (now, _digest(token)))
            con.commit()
        return {"id": row["id"], "name": row["name"], "role": row["role"]}
    finally:
        con.close()


def delete_session(token: str) -> None:
    if not token:
        return
    con = db.connect()
    try:
        con.execute("DELETE FROM sessions WHERE token_hash = ?", (_digest(token),))
        con.commit()
    finally:
        con.close()


def session_token_from_cookie(header: str) -> str:
    if not header:
        return ""
    try:
        jar = SimpleCookie()
        jar.load(header)
    except Exception:                                          # noqa: BLE001
        return ""
    morsel = jar.get(SESSION_COOKIE)
    return morsel.value if morsel else ""


def cookie_header(token: str, secure: bool) -> str:
    bits = [f"{SESSION_COOKIE}={token}", "Path=/", "HttpOnly",
            "SameSite=Strict", f"Max-Age={SESSION_IDLE_SECONDS}"]
    if secure:
        bits.append("Secure")
    return "; ".join(bits)


def clear_cookie_header(secure: bool) -> str:
    bits = [f"{SESSION_COOKIE}=", "Path=/", "HttpOnly", "SameSite=Strict",
            "Max-Age=0"]
    if secure:
        bits.append("Secure")
    return "; ".join(bits)


# --------------------------------------------------------------------------
# agent tokens
# --------------------------------------------------------------------------

def create_token(user_id: int, label: str = "") -> dict:
    token = secrets.token_urlsafe(32)
    con = db.connect()
    try:
        cur = con.execute("INSERT INTO tokens (token_hash, user_id, label, "
                          "created_at) VALUES (?, ?, ?, ?)",
                          (_digest(token), user_id, (label or "")[:64],
                           time.time()))
        con.commit()
        tid = cur.lastrowid
    finally:
        con.close()
    return {"id": tid, "token": token, "label": (label or "")[:64]}


def token_user(token: str) -> dict | None:
    if not token:
        return None
    con = db.connect()
    try:
        row = con.execute(
            "SELECT u.id AS id, u.name AS name, u.role AS role, t.id AS tid "
            "FROM tokens t JOIN users u ON u.id = t.user_id "
            "WHERE t.token_hash = ?", (_digest(token),)).fetchone()
        if not row:
            return None
        con.execute("UPDATE tokens SET last_used = ? WHERE id = ?",
                    (time.time(), row["tid"]))
        con.commit()
        return {"id": row["id"], "name": row["name"], "role": row["role"]}
    finally:
        con.close()


def list_tokens(user_id: int) -> list[dict]:
    con = db.connect()
    try:
        rows = con.execute("SELECT id, label, created_at, last_used FROM tokens "
                           "WHERE user_id = ? ORDER BY id", (user_id,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def delete_token(user_id: int, token_id: int | None = None,
                 token: str = "") -> bool:
    """Delete one agent token, by id or by the secret itself.

    By the secret is what makes `DELETE /api/auth/token` with only an
    Authorization header work: an agent can retire its own credential
    without having been told its row id.
    """
    con = db.connect()
    try:
        if token_id is not None:
            cur = con.execute("DELETE FROM tokens WHERE id = ? AND user_id = ?",
                              (token_id, user_id))
        elif token:
            cur = con.execute("DELETE FROM tokens WHERE token_hash = ? AND "
                              "user_id = ?", (_digest(token), user_id))
        else:
            return False
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


# --------------------------------------------------------------------------
# rate limiting (in memory, resets on restart)
# --------------------------------------------------------------------------

_fail_user: dict[str, deque] = defaultdict(deque)
_fail_addr: dict[str, deque] = defaultdict(deque)
_rate_lock = threading.Lock()


def _prune(dq: deque, now: float) -> None:
    while dq and now - dq[0] > RATE_WINDOW:
        dq.popleft()


def rate_blocked(username: str, addr: str) -> bool:
    now = time.time()
    key = (username or "").strip().lower()
    with _rate_lock:
        _prune(_fail_user[key], now)
        _prune(_fail_addr[addr], now)
        return (len(_fail_user[key]) >= RATE_MAX_PER_USER
                or len(_fail_addr[addr]) >= RATE_MAX_PER_ADDR)


def rate_record_failure(username: str, addr: str) -> None:
    now = time.time()
    key = (username or "").strip().lower()
    with _rate_lock:
        _fail_user[key].append(now)
        _fail_addr[addr].append(now)


def rate_clear(username: str) -> None:
    key = (username or "").strip().lower()
    with _rate_lock:
        _fail_user.pop(key, None)


# --------------------------------------------------------------------------
# request helpers the Handler calls
# --------------------------------------------------------------------------

def user_from_request(cookie: str, authorization: str) -> tuple[dict | None, str]:
    """Resolve the caller.

    Returns (user or None, how) where how is "bearer", "cookie" or "".
    A present but invalid Bearer header does NOT fall through to the cookie:
    an agent that sent a dead token should be told its token is dead, not
    silently served as whoever happens to be logged in in that browser.
    """
    if authorization and authorization.strip().lower().startswith("bearer "):
        return token_user(authorization.strip()[7:].strip()), "bearer"
    token = session_token_from_cookie(cookie)
    if token:
        user = session_user(token)
        if user:
            return user, "cookie"
    return None, ""


def csrf_ok(sec_fetch_site: str, origin: str, host: str) -> bool:
    """Contract C2's CSRF rule, applied only to cookie authenticated writes.

    Fetch metadata first (`Sec-Fetch-Site`), because every browser this tool
    targets sends it and a cross site form post is labelled `cross-site`
    there whether or not it also sends an Origin. `none` means the user typed
    the URL or used a bookmark, which cannot be an attacker's page. Origin
    matching Host is the fallback for a client that sends no fetch metadata.

    A request with neither header (curl, an agent script) fails this check,
    which is deliberate: those should authenticate with a Bearer token, and
    Bearer requests never reach here.
    """
    site = (sec_fetch_site or "").strip().lower()
    if site in ("same-origin", "none"):
        return True
    if origin:
        try:
            netloc = urllib.parse.urlsplit(origin.strip()).netloc
        except ValueError:
            return False
        if netloc and netloc == (host or "").strip():
            return True
    return False


def user_footage(user_id: int) -> Path:
    return USERS_DIR / str(user_id) / "footage"


def allowed_roots(user: dict | None, footage: Path) -> list[Path]:
    roots = [Path(os.path.realpath(footage))]
    if user and user.get("id"):
        d = user_footage(user["id"])
        d.mkdir(parents=True, exist_ok=True)
        roots.append(Path(os.path.realpath(d)))
    return roots


def confine(raw, roots: list[Path]) -> Path:
    """Resolve a path and refuse it unless it sits inside one of the roots.

    realpath first, so a symlink pointing out of the tree and a `..` in the
    middle of the string are both resolved away before the comparison.
    commonpath rather than a string prefix test, because a prefix test says
    /footage-other is inside /footage.
    """
    target = Path(os.path.realpath(os.path.expanduser(str(raw))))
    for root in roots:
        try:
            if os.path.commonpath([str(target), str(root)]) == str(root):
                return target
        except ValueError:
            continue                    # different drive or mount, not a match
    where = " or ".join(str(r) for r in roots)
    raise AuthError(403, "with logins on, this browser only opens files under "
                         + where)
