#!/usr/bin/env python3
"""Projects: one git style history per clip, shared by every account.

Why this exists. Until now the studio held a per account, per clip GRADE: one
config row, overwritten on every save. That row answers "what does this clip
look like" and nothing else. It cannot answer "who changed it", "what did it
look like before", "an agent and I are both editing, whose value am I
looking at", or "I reloaded the page, put it back exactly as it was". Those
are the four things the founder asked for, and all four are the same missing
thing: a history with names on it.

So a PROJECT is everything the studio knows about one clip:

    rotation, playhead, the loaded preset name, a bag of small extras, and a
    history of commits on branches, each commit carrying the full config
    after the change, its author, and a readable message.

Three decisions worth stating plainly, because they are not obvious.

1. A project is keyed by the clip's CONTENT (grades.clip_key, the same 32 hex
   digest per clip grades already use), not by its name or its path. Rename
   the file, move it to another drive, and the project follows it.

2. A project is SHARED by every account (founder's call, 2026-09-05). Two
   people and an agent working on the same clip see ONE tree, and the author
   field on each commit is what tells their work apart. What is per account is
   only which project that account currently has open: the `workspace` table.

3. The old `grades` table is the MIGRATION SOURCE and stays a live mirror.
   The first time a clip's project is created, the most recently updated
   saved grade for that key becomes the root commit, so nobody's existing
   work turns into an empty history. After that the row is kept equal to
   HEAD: every commit and every move of HEAD writes it through
   (mirror_grade below), because the grade picker, GET /api/grades, GET
   /api/grade for a clip with no project and POST /api/grade/copy all still
   read that table, and the tab no longer autosaves into it. A HEAD that is
   the plain defaults removes the row instead of writing one, so a clip
   nobody has graded stays out of the list of graded clips.

Standard library only, like the rest of the studio, and the tables live in the
same SQLite file db.py owns, created here with CREATE TABLE IF NOT EXISTS, per
the studio's rule: db.py owns the auth tables, every other module owns its own.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import threading
import time
from pathlib import Path

STUDIO = Path(__file__).resolve().parent
CONTENT = STUDIO.parent
GRADE = CONTENT / "grade"
FOOTAGE = CONTENT / "footage"

# Same reason server.py and grades.py do it: so these imports resolve however
# this file was reached, whether as part of the server, from the CLI, or from
# a test that imported it alone.
if str(STUDIO) not in sys.path:
    sys.path.insert(0, str(STUDIO))
if str(GRADE) not in sys.path:
    sys.path.insert(0, str(GRADE))
import db as _db        # noqa: E402  (path has to be set first)
import grades as _grades  # noqa: E402  (same reason)
import cinegrade as _cg   # noqa: E402  (same reason)

# The five rotations the whole arc speaks, as strings in JSON so "0" cannot be
# confused with "unset". "auto" means honour the file's own rotation tag, which
# is what ffmpeg does by default and what the studio did before this arc.
ROTATIONS = ("auto", "0", "90", "180", "270")

# Branch names are validated exactly like preset names (server.write_preset),
# because they end up in a UI dropdown next to them and in commit messages.
NAME_RE = re.compile(r"[A-Za-z0-9_.-]+")
NAME_MAX = 64

# A commit's change list is stored whole so the History panel can expand a row
# without a second request. A root commit, a preset load or a "reset all"
# moves most of the config at once, and storing three hundred entries for one
# row is a lot of JSON for something nobody reads to the end. Past this many
# the list is truncated and a marker entry says by how much.
CHANGES_MAX = 120

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
  key         TEXT PRIMARY KEY,
  name        TEXT NOT NULL DEFAULT '',
  path        TEXT NOT NULL DEFAULT '',
  rotation    TEXT NOT NULL DEFAULT 'auto',
  head        TEXT,
  branch      TEXT NOT NULL DEFAULT 'main',
  time        REAL NOT NULL DEFAULT 0,
  preset      TEXT,
  extras_json TEXT NOT NULL DEFAULT '{}',
  created     REAL NOT NULL,
  updated     REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS commits (
  id           TEXT PRIMARY KEY,
  project_key  TEXT NOT NULL,
  parent       TEXT,
  branch       TEXT NOT NULL,
  author       TEXT NOT NULL,
  ts           REAL NOT NULL,
  message      TEXT NOT NULL,
  changes_json TEXT NOT NULL,
  config_json  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS commits_project ON commits(project_key, ts);

CREATE TABLE IF NOT EXISTS branches (
  project_key TEXT NOT NULL,
  name        TEXT NOT NULL,
  tip         TEXT,
  created     REAL NOT NULL,
  PRIMARY KEY (project_key, name)
);

CREATE TABLE IF NOT EXISTS workspace (
  user_id     INTEGER PRIMARY KEY,
  project_key TEXT,
  updated     REAL NOT NULL
);
"""

_inited = False
_init_lock = threading.Lock()

# Cache for open_rotation() only. The render paths (contract C2) ask for the
# open project's rotation on every frame, stats, thumbnail and proxy request,
# and that must not be a database read each time. Every write in this module
# that can move a rotation or a workspace clears it.
_rot_cache: dict[int, str | None] = {}
_rot_lock = threading.Lock()


def init_schema() -> None:
    """Create the project tables if they are not there yet. Safe to call often."""
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


def _invalidate_rotation(user_id: int | None = None) -> None:
    with _rot_lock:
        if user_id is None:
            _rot_cache.clear()
        else:
            _rot_cache.pop(int(user_id), None)


# --------------------------------------------------------------------------
# how a clip name becomes a key, a name and a path
# --------------------------------------------------------------------------
#
# server.py owns clip resolution (it knows about clips opened from outside
# content/footage, which live in memory only), so it hands this module its own
# resolver at import. Unbound, the fallback below resolves a name inside
# content/footage, which is enough for the CLI and for the tests.

_resolver = None


def bind_resolver(fn) -> None:
    """Teach this module how the caller turns a clip name into (key, name, path)."""
    global _resolver
    _resolver = fn


def _default_resolve(name: str) -> tuple[str, str, str]:
    p = FOOTAGE / str(name or "").strip()
    if not p.exists():
        raise ValueError(f"clip not found: {name}")
    return _grades.clip_key(p), p.name, str(p)


def resolve(clip: str) -> tuple[str, str, str]:
    """(content key, display name, path) for a clip name, or a bare key.

    A bare 32 hex key is accepted for the same reason server._grade_key accepts
    one: a project can outlive the clip's presence in the current footage
    folder, and a caller holding only the key (the History panel, an agent
    reading GET /api/project) must still be able to address it.
    """
    clip = str(clip or "").strip()
    if not clip:
        raise ValueError("no clip given")
    if _grades.is_key(clip):
        row = get(clip.lower())
        if row:
            return clip.lower(), row["name"], row["path"]
        return clip.lower(), "", ""
    fn = _resolver or _default_resolve
    return fn(clip)


# --------------------------------------------------------------------------
# config helpers
# --------------------------------------------------------------------------

def full_config(cfg: dict | None) -> dict:
    """The same fill in server.full_config does, available without the server.

    migrate_layers runs first, on the config as it arrived, because that is the
    shape a pre-layers preset or saved grade still has.
    """
    return _cg.deep_merge(_cg.DEFAULTS, _cg.migrate_layers(cfg or {}))


_defaults_cache: dict | None = None
_defaults_canon: str | None = None


def defaults() -> dict:
    """The engine defaults, filled out. Cached: this is on the commit path."""
    global _defaults_cache
    if _defaults_cache is None:
        _defaults_cache = full_config({})
    return json.loads(json.dumps(_defaults_cache))


def _canon(cfg: dict) -> str:
    """One config, one string. Sorted keys so two equal configs cannot differ."""
    return json.dumps(cfg, sort_keys=True, allow_nan=False,
                      separators=(",", ":"))


def _is_defaults(cfg: dict | None) -> bool:
    """Is this config exactly the engine defaults, ie. no grade at all?"""
    global _defaults_canon
    if _defaults_canon is None:
        _defaults_canon = _canon(defaults())
    return _canon(full_config(cfg)) == _defaults_canon


def _commit_id(parent: str | None, author: str, ts: float, body: str) -> str:
    return hashlib.sha1(
        f"{parent or ''}|{author}|{ts!r}|{body}".encode()).hexdigest()


def short(commit_id: str | None) -> str:
    return (commit_id or "")[:7]


# --------------------------------------------------------------------------
# readable messages
# --------------------------------------------------------------------------
#
# The vocabulary is the one on screen: studio/static/schema.js is where the
# labels come from, so "primaries.saturation" reads as "saturation" and
# "layers.0.mask.key.hue_center" reads as `layer "Sky" key hue centre`,
# exactly as the panel spells them. Kept as a table here rather than parsed
# out of the JavaScript at runtime: the studio is a Python server serving
# static files, and making the server depend on parsing its own front end
# would break the moment somebody reformats that file.

# Sections whose "enabled" leaf reads as "<section> on" rather than
# "enabled on".
_SECTION_LABEL = {
    "prep.denoise": "denoise", "curves": "curves", "hue_curves": "hue curves",
    "slice": "HSL slice", "slice.tetra": "tetra", "grain": "grain",
    "letterbox": "letterbox", "fx.halation": "halation", "fx.bloom": "bloom",
    "fx.rgb_split": "RGB split", "fx.radial_blur": "radial blur",
    "fx.vignette": "vignette",
}

# One entry per leaf that the panel shows, keyed by its dotted path with any
# array index removed. Paths not listed fall back to the last path segment
# with underscores turned into spaces, so a parameter added to the engine
# still produces a readable line before anyone updates this table.
_LABELS = {
    "prep.denoise.enabled": "denoise",
    "prep.denoise.spatial": "denoise spatial",
    "prep.denoise.temporal": "denoise temporal",
    "convert.working_space": "working space",
    "convert.tonemap": "tone map",
    "convert.encode": "output gamma",
    "convert.exposure": "exposure",
    "primaries.contrast": "contrast",
    "primaries.pivot": "pivot",
    "primaries.saturation": "saturation",
    "primaries.vibrance": "vibrance",
    "primaries.temperature": "temperature",
    "primaries.tint": "tint",
    "primaries.brightness": "brightness",
    "primaries.lift": "lift",
    "primaries.gamma": "gamma",
    "primaries.gain": "gain",
    "primaries.black_lift": "black lift",
    "primaries.highlight_rolloff": "highlight rolloff",
    "curves.enabled": "curves",
    "curves.interp": "curve interpolation",
    "curves.master": "master curve",
    "curves.r": "red curve",
    "curves.g": "green curve",
    "curves.b": "blue curve",
    "hue_curves.enabled": "hue curves",
    "hue_curves.hue_hue": "hue vs hue curve",
    "hue_curves.hue_sat": "hue vs sat curve",
    "hue_curves.hue_lum": "hue vs lum curve",
    "hue_curves.lum_sat": "lum vs sat curve",
    "hue_curves.sat_sat": "sat vs sat curve",
    "slice.enabled": "HSL slice",
    "slice.density": "HSL slice global density",
    "slice.tetra.enabled": "tetra",
    "look.lut": "look LUT",
    "look.mix": "mix",
    "look.lut2": "look LUT 2",
    "look.mix2": "mix2",
    "look.balance": "balance",
    "fx.halation.enabled": "halation",
    "fx.halation.threshold": "halation threshold",
    "fx.halation.sigma": "halation sigma",
    "fx.halation.strength": "halation strength",
    "fx.halation.tint": "halation tint",
    "fx.bloom.enabled": "bloom",
    "fx.bloom.threshold": "bloom threshold",
    "fx.bloom.sigma": "bloom sigma",
    "fx.bloom.strength": "bloom strength",
    "fx.bloom.tint": "bloom tint",
    "fx.rgb_split.enabled": "RGB split",
    "fx.rgb_split.amount": "RGB split amount",
    "fx.radial_blur.enabled": "radial blur",
    "fx.radial_blur.sigma": "radial blur sigma",
    "fx.radial_blur.start": "radial blur start",
    "fx.radial_blur.end": "radial blur end",
    "fx.vignette.enabled": "vignette",
    "fx.vignette.amount": "vignette amount",
    "fx.vignette.radius": "vignette radius",
    "grain.enabled": "grain",
    "grain.stock": "grain stock",
    "grain.strength": "grain strength",
    "grain.size": "grain size",
    "grain.softness": "grain softness",
    "grain.response": "grain response",
    "grain.color": "grain colour",
    "grain.seed": "grain seed",
    "grain.opacity": "grain opacity",
    "detail.soften": "soften",
    "detail.sharpen": "sharpen",
    "detail.mid_detail": "mid detail",
    "letterbox.enabled": "letterbox",
    "letterbox.aspect": "letterbox aspect",
    "output.codec": "codec",
    "output.profile": "prores profile",
    "output.crf": "crf",
    "output.preset": "x26x preset",
    # layers, after the index has been stripped
    "layers.enabled": "on",
    "layers.name": "name",
    "layers.placement": "placement",
    "layers.mask.show": "matte in preset",
    "layers.mask.invert": "invert mask",
    "layers.mask.window.enabled": "window",
    "layers.mask.window.shape": "window shape",
    "layers.mask.window.cx": "window centre x",
    "layers.mask.window.cy": "window centre y",
    "layers.mask.window.w": "window width",
    "layers.mask.window.h": "window height",
    "layers.mask.window.rotation": "window rotation",
    "layers.mask.window.softness": "window softness",
    "layers.mask.window.invert": "invert window",
    "layers.mask.key.enabled": "key",
    "layers.mask.key.invert": "invert key",
    "layers.mask.key.hue_center": "key hue centre",
    "layers.mask.key.hue_width": "key hue width",
    "layers.mask.key.hue_soft": "key hue softness",
    "layers.mask.key.sat_low": "key sat low",
    "layers.mask.key.sat_high": "key sat high",
    "layers.mask.key.sat_soft": "key sat softness",
    "layers.mask.key.lum_low": "key luma low",
    "layers.mask.key.lum_high": "key luma high",
    "layers.mask.key.lum_soft": "key luma softness",
    "layers.correct.exposure": "exposure",
    "layers.correct.contrast": "contrast",
    "layers.correct.pivot": "pivot",
    "layers.correct.saturation": "saturation",
    "layers.correct.temperature": "temperature",
    "layers.correct.tint": "tint",
    "layers.correct.hue_shift": "hue shift",
    "layers.correct.sat_gain": "sat gain",
    "layers.correct.lum_gain": "luma gain",
    "layers.correct.offset": "offset push",
    "layers.correct.blur": "blur",
    "layers.correct.strength": "strength",
}

# Leaves whose value says everything on its own, so the label would only be in
# the way: "grain on, 35mm" rather than "grain on, grain stock 35mm".
_VALUE_ONLY = {"grain.stock"}

# Leaves measured in stops or degrees, where the number alone would be
# ambiguous.
_UNITS = {
    "convert.exposure": "stops",
    "layers.correct.exposure": "stops",
    "primaries.temperature": "stops",
    "primaries.tint": "stops",
    "layers.correct.temperature": "stops",
    "layers.correct.tint": "stops",
    "layers.mask.window.rotation": "deg",
    "layers.correct.hue_shift": "deg",
}

_INDEX_RE = re.compile(r"\.\d+")

# Changed parameters are listed in the order the panel shows them, not
# alphabetically: "contrast, pivot, highlight rolloff" is the order somebody
# just moved them in, and "contrast, highlight rolloff, pivot" is the order a
# computer would sort them in. _LABELS is written in panel order and Python
# dicts keep insertion order, so the table is also the ordering.
_ORDER = {path: i for i, path in enumerate(_LABELS)}
_SECTION_ORDER = ["prep", "convert", "primaries", "curves", "hue_curves",
                  "slice", "layers", "look", "fx", "grain", "detail",
                  "letterbox", "output"]


def _generic(path: str) -> str:
    return _INDEX_RE.sub("", path)


def _sort_key(path: str):
    gen = _generic(path)
    top = gen.split(".")[0]
    section = (_SECTION_ORDER.index(top) if top in _SECTION_ORDER
               else len(_SECTION_ORDER))
    parts = path.split(".")
    # Layer changes group by layer, so one layer's edits read together rather
    # than every layer's exposure followed by every layer's blur.
    item = int(parts[1]) if top == "layers" and len(parts) > 1 and parts[1].isdigit() else 0
    return (section, item, _ORDER.get(gen, 9999), path)


def label_for(path: str) -> str:
    """The words a person sees for one dotted config path."""
    gen = _generic(path)
    if gen in _LABELS:
        return _LABELS[gen]
    if gen.endswith(".enabled"):
        parent = gen[: -len(".enabled")]
        if parent in _SECTION_LABEL:
            return _SECTION_LABEL[parent]
    if gen.startswith("slice.vectors."):
        parts = gen.split(".")
        if len(parts) == 4:
            return f"HSL slice {parts[2]} {parts[3]}"
    if gen.startswith("slice.tetra."):
        corner = {"r": "red", "g": "green", "b": "blue", "c": "cyan",
                  "m": "magenta", "y": "yellow"}.get(gen.split(".")[-1], "")
        if corner:
            return f"tetra {corner}"
    return gen.split(".")[-1].replace("_", " ")


def _num(value: float, signed: bool = False, whole: bool | None = None) -> str:
    """A number the way the panel shows it: whole when it is whole, else 2 dp.

    `whole` overrides that test, so a pair of values that a message shows side
    by side can be printed at one precision instead of "1 to 1.20".
    """
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    if f != f or f in (float("inf"), float("-inf")):
        return str(value)
    is_whole = abs(f - round(f)) < 1e-9 if whole is None else whole
    if is_whole:
        text = "%d" % int(round(f))
    else:
        text = "%.2f" % f
    if signed and f > 0:
        text = "+" + text
    return text


def _fmt(path: str, value) -> str:
    """One config value as words. Lists and curves say what they are."""
    gen = _generic(path)
    if value is None:
        return "auto" if gen.endswith("pivot") else "none"
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, (int, float)):
        signed = gen in _UNITS and _UNITS[gen] in ("stops", "deg")
        text = _num(value, signed=signed)
        unit = _UNITS.get(gen)
        return f"{text} {unit}" if unit else text
    if isinstance(value, list):
        if value and isinstance(value[0], (int, float)):
            return "[" + ", ".join(_num(v) for v in value) + "]"
        return "edited"
    if isinstance(value, str):
        # A LUT arrives as a path or a file name; the panel shows its stem.
        if gen in ("look.lut", "look.lut2"):
            return Path(value).stem or value
        return value
    return str(value)


def _flatten(cfg, prefix: str = "") -> dict:
    """Every leaf of a config as dotted path to value.

    Lists of numbers and curve point lists are leaves: a curve is edited as a
    whole and "the master curve changed" is what a person wants to read, not
    six lines about individual control points. Only `layers` recurses, because
    each layer is a named thing in its own right.
    """
    out = {}
    if isinstance(cfg, dict):
        for k, v in cfg.items():
            if str(k).startswith("_"):
                continue          # _comment and friends are not parameters
            out.update(_flatten(v, f"{prefix}.{k}" if prefix else str(k)))
        return out
    if isinstance(cfg, list) and cfg and isinstance(cfg[0], dict):
        for i, v in enumerate(cfg):
            out.update(_flatten(v, f"{prefix}.{i}"))
        return out
    out[prefix] = cfg
    return out


def _layer_names(cfg: dict) -> list[str]:
    names = []
    for i, layer in enumerate(cfg.get("layers") or []):
        if isinstance(layer, dict):
            names.append(str(layer.get("name") or f"Layer {i + 1}"))
        else:
            names.append(f"Layer {i + 1}")
    return names


def changed_paths(old: dict | None, new: dict) -> list[dict]:
    """The dotted paths that moved, with old and new values and their labels."""
    a = _flatten(full_config(old or {}))
    b = _flatten(full_config(new or {}))
    names = _layer_names(full_config(new or {})) or _layer_names(full_config(old or {}))
    out = []
    for path in sorted(set(a) | set(b), key=_sort_key):
        if a.get(path, "\0missing") == b.get(path, "\0missing"):
            continue
        label = label_for(path)
        if path.startswith("layers."):
            idx = path.split(".")[1]
            if idx.isdigit():
                who = names[int(idx)] if int(idx) < len(names) else f"Layer {int(idx) + 1}"
                label = f'layer "{who}" {label}'
        out.append({"path": path, "label": label,
                    "old": a.get(path), "new": b.get(path)})
    return out


def _one_line(entry: dict) -> str:
    """One change, spelled out: label, then old to new (or just the new value).

    A parameter whose neutral value is zero (exposure, a hue rotation, an
    offset) reads better as the number it moved TO with its sign, because
    "0.00 to +0.70 stops" spends four words saying nothing. A parameter whose
    neutral is something else (saturation 1.0, a key's hue centre at 30) reads
    as "old to new", because the number alone would not say how far it went.
    """
    path, label = entry["path"], entry["label"]
    old, new = entry["old"], entry["new"]
    if _generic(path) == "layers.name":
        return f'layer "{old}" renamed to "{new}"'
    if isinstance(new, bool) or isinstance(old, bool):
        return f"{label} {_fmt(path, new)}"
    if isinstance(new, (int, float)) and isinstance(old, (int, float)):
        if abs(float(old)) < 1e-12:
            return f"{label} {_fmt(path, new)}"
        # Both sides at the same precision, or "1 to 1.20" reads as a typo.
        whole = (abs(float(old) - round(float(old))) < 1e-9
                 and abs(float(new) - round(float(new))) < 1e-9)
        text = f"{_num(old, whole=whole)} to {_num(new, whole=whole)}"
        unit = _UNITS.get(_generic(path))
        return f"{label} {text} {unit}" if unit else f"{label} {text}"
    if new is None:
        return f"{label} cleared"
    if isinstance(old, str) and isinstance(new, str) and _generic(path).endswith(".name"):
        return f'{label} "{old}" to "{new}"'
    return f"{label} {_fmt(path, new)}"


def _compact(entry: dict) -> str:
    """One change in as few words as still make sense, for a two change line."""
    path = entry["path"]
    if _generic(path) == "layers.name":
        return f'layer renamed "{entry["old"]}" to "{entry["new"]}"'
    if _generic(path) in _VALUE_ONLY:
        return _fmt(path, entry["new"])
    if isinstance(entry["new"], bool):
        return f"{entry['label']} {_fmt(path, entry['new'])}"
    if entry["new"] is None:
        return f"{entry['label']} cleared"
    return f"{entry['label']} {_fmt(path, entry['new'])}"


def describe_change(old: dict | None, new: dict) -> str:
    """A readable one line message for the move from `old` to `new`.

    Reads like the panel: "saturation 1.00 to 1.20", "grain on, 35mm",
    'layer "Sky" key hue centre 30 to 45', "3 changes: contrast, pivot,
    highlight rolloff". No arrows and no dashes as separators, on purpose:
    this text goes in a commit message, a tooltip and a toast, and the studio
    writes "to" between two values everywhere else.
    """
    old_full = full_config(old or {})
    new_full = full_config(new or {})
    if old is None:
        return "root"
    if _canon(old_full) == _canon(new_full):
        return "no change"

    old_layers, new_layers = _layer_names(old_full), _layer_names(new_full)
    if len(new_layers) != len(old_layers):
        added = len(new_layers) - len(old_layers)
        if added > 0:
            fresh = [n for n in new_layers if n not in old_layers] or new_layers[-added:]
            if added == 1:
                return f'layer "{fresh[0]}" added'
            return f"{added} layers added"
        gone = [n for n in old_layers if n not in new_layers] or old_layers[added:]
        if added == -1:
            return f'layer "{gone[0]}" removed'
        return f"{-added} layers removed"

    if _is_defaults(new_full):
        return "reset to defaults"

    entries = changed_paths(old_full, new_full)
    if not entries:
        return "grade updated"
    if len(entries) == 1:
        return _one_line(entries[0])
    if len(entries) == 2:
        return ", ".join(_compact(e) for e in entries)
    labels = [e["label"] for e in entries]
    shown = labels[:4]
    tail = "" if len(labels) <= 4 else f" and {len(labels) - 4} more"
    return f"{len(entries)} changes: " + ", ".join(shown) + tail


# --------------------------------------------------------------------------
# rows
# --------------------------------------------------------------------------

def _project_row(row) -> dict:
    try:
        extras = json.loads(row["extras_json"] or "{}")
    except json.JSONDecodeError:
        extras = {}
    if not isinstance(extras, dict):
        extras = {}
    return {"key": row["key"], "name": row["name"], "path": row["path"],
            "rotation": row["rotation"] or "auto", "head": row["head"],
            "branch": row["branch"] or "main", "time": float(row["time"] or 0.0),
            "preset": row["preset"], "extras": extras,
            "created": float(row["created"]), "updated": float(row["updated"])}


def _commit_row(row, head: str | None = None,
                tips: set | None = None) -> dict:
    try:
        changes = json.loads(row["changes_json"] or "[]")
    except json.JSONDecodeError:
        changes = []
    return {"id": row["id"], "short": short(row["id"]), "parent": row["parent"],
            "branch": row["branch"], "author": row["author"],
            "ts": float(row["ts"]), "message": row["message"],
            "changes": changes,
            "is_head": row["id"] == head,
            "is_tip": bool(tips and row["id"] in tips)}


def get(key: str) -> dict | None:
    """One project record, or None. No config: use head_config for that."""
    _ensure()
    con = _db.connect()
    try:
        row = con.execute("SELECT * FROM projects WHERE key=?", (key,)).fetchone()
    finally:
        con.close()
    return _project_row(row) if row else None


def head_config(key: str) -> dict | None:
    _ensure()
    con = _db.connect()
    try:
        row = con.execute(
            "SELECT c.config_json AS cj FROM projects p "
            "JOIN commits c ON c.id = p.head WHERE p.key=?", (key,)).fetchone()
    finally:
        con.close()
    if not row:
        return None
    try:
        return full_config(json.loads(row["cj"]))
    except json.JSONDecodeError:
        return None


def head_commit(key: str) -> dict | None:
    _ensure()
    con = _db.connect()
    try:
        row = con.execute(
            "SELECT c.* FROM projects p JOIN commits c ON c.id = p.head "
            "WHERE p.key=?", (key,)).fetchone()
        if not row:
            return None
        head = row["id"]
        tips = {r["tip"] for r in con.execute(
            "SELECT tip FROM branches WHERE project_key=?", (key,)).fetchall()}
    finally:
        con.close()
    out = _commit_row(row, head, tips)
    out.pop("changes", None)
    return out


def _migration_source(con, key: str) -> dict | None:
    """The newest saved grade for this clip, across every account.

    Read only. The grades table is never rewritten or deleted from here: it is
    where a project that did not exist yet gets its first commit from, and it
    stays exactly as it was afterwards.
    """
    try:
        row = con.execute(
            "SELECT config_json FROM grades WHERE clip_key=? "
            "ORDER BY updated_at DESC LIMIT 1", (key,)).fetchone()
    except Exception:                                        # noqa: BLE001
        return None                     # no grades table on a fresh database
    if not row:
        return None
    try:
        cfg = json.loads(row["config_json"])
    except json.JSONDecodeError:
        return None
    return cfg if isinstance(cfg, dict) else None


def ensure(key: str, name: str = "", path: str = "") -> dict:
    """The project for this content key, created with a root commit if new.

    Does NOT touch anybody's workspace: this is the "make sure the history
    exists" call that PUT /api/grade and a bare session write use. Opening a
    project for a person is open_project below.
    """
    _ensure()
    _grades.init_schema()               # the migration source has to exist
    con = _db.connect()
    try:
        row = con.execute("SELECT * FROM projects WHERE key=?", (key,)).fetchone()
        if row:
            # A clip that moved or was opened under a different display name
            # keeps its project; the name and path are refreshed so the panel
            # shows where it is now.
            if name and (row["name"] != name or (path and row["path"] != path)):
                con.execute("UPDATE projects SET name=?, path=? WHERE key=?",
                            (name, path or row["path"], key))
                con.commit()
                row = con.execute("SELECT * FROM projects WHERE key=?",
                                  (key,)).fetchone()
            return _project_row(row)

        now = time.time()
        saved = _migration_source(con, key)
        cfg = full_config(saved)
        body = _canon(cfg)
        message = "root: migrated saved grade" if saved is not None else "root"
        cid = _commit_id(None, "server", now, body)
        changes = [] if saved is None else changed_paths(defaults(), cfg)
        con.execute(
            "INSERT INTO commits (id, project_key, parent, branch, author, ts,"
            " message, changes_json, config_json) VALUES (?,?,?,?,?,?,?,?,?)",
            (cid, key, None, "main", "server", now, message,
             json.dumps(changes[:CHANGES_MAX]), body))
        con.execute(
            "INSERT INTO branches (project_key, name, tip, created) "
            "VALUES (?,?,?,?)", (key, "main", cid, now))
        con.execute(
            "INSERT INTO projects (key, name, path, rotation, head, branch,"
            " time, preset, extras_json, created, updated) "
            "VALUES (?,?,?,'auto',?,'main',0,NULL,'{}',?,?)",
            (key, name, path, cid, now, now))
        con.commit()
        row = con.execute("SELECT * FROM projects WHERE key=?", (key,)).fetchone()
        return _project_row(row)
    finally:
        con.close()


def open_project(user_id: int, clip: str) -> dict:
    """Open a clip for one account: create or find its project, then park there.

    Returns the full state GET /api/project answers with, config included.
    """
    key, name, path = resolve(clip)
    ensure(key, name, path)
    set_workspace(user_id, key)
    return state(key)


# `projects.open(user, clip)` is the name the plan and the routes use. Defined
# as open_project above so nothing inside this module can shadow the builtin
# by accident.
open = open_project                                          # noqa: A001


def set_workspace(user_id: int, key: str | None) -> None:
    _ensure()
    con = _db.connect()
    try:
        con.execute(
            "INSERT INTO workspace (user_id, project_key, updated) "
            "VALUES (?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
            "project_key=excluded.project_key, updated=excluded.updated",
            (int(user_id), key, time.time()))
        con.commit()
    finally:
        con.close()
    _invalidate_rotation(user_id)


def workspace_key(user_id: int) -> str | None:
    _ensure()
    con = _db.connect()
    try:
        row = con.execute("SELECT project_key FROM workspace WHERE user_id=?",
                          (int(user_id),)).fetchone()
    finally:
        con.close()
    return row["project_key"] if row and row["project_key"] else None


def workspaces() -> dict[int, str]:
    """Every account's open project, for rebuilding the live state on boot."""
    _ensure()
    con = _db.connect()
    try:
        rows = con.execute(
            "SELECT user_id, project_key FROM workspace "
            "WHERE project_key IS NOT NULL").fetchall()
    finally:
        con.close()
    return {int(r["user_id"]): r["project_key"] for r in rows}


def open_rotation(user_id: int) -> str | None:
    """The open project's rotation for this account, or None.

    Called from the render paths on every frame, so it is cached in memory and
    it NEVER raises: a rotation nobody can read is not a reason to fail a
    render, it is a reason to fall back to `auto`.
    """
    uid = int(user_id or 0)
    with _rot_lock:
        if uid in _rot_cache:
            return _rot_cache[uid]
    value = None
    try:
        _ensure()
        con = _db.connect()
        try:
            row = con.execute(
                "SELECT p.rotation AS rot FROM workspace w "
                "JOIN projects p ON p.key = w.project_key "
                "WHERE w.user_id=?", (uid,)).fetchone()
        finally:
            con.close()
        if row and row["rot"] in ROTATIONS:
            value = row["rot"]
    except Exception:                                        # noqa: BLE001
        value = None
    with _rot_lock:
        _rot_cache[uid] = value
    return value


# --------------------------------------------------------------------------
# commits
# --------------------------------------------------------------------------

def _branch_tip(con, key: str, branch: str) -> str | None:
    row = con.execute("SELECT tip FROM branches WHERE project_key=? AND name=?",
                      (key, branch)).fetchone()
    return row["tip"] if row else None


def _next_fork_name(con, key: str) -> str:
    have = {r["name"] for r in con.execute(
        "SELECT name FROM branches WHERE project_key=?", (key,)).fetchall()}
    n = 1
    while f"fork-{n}" in have:
        n += 1
    return f"fork-{n}"


def mirror_grade(user_id: int, key: str, proj: dict | None = None) -> None:
    """Write HEAD's config back into the old per clip `grades` row.

    The `grades` table came first and things still read it: GET /api/grades
    fills the copy picker, GET /api/grade answers for a clip with no project,
    POST /api/grade/copy reads the source clip out of it, and a brand new
    project migrates its root commit from it. Since the tab stopped autosaving
    through PUT /api/grade and commits through the session instead, nothing
    else keeps that row current, so every move of HEAD writes it through here
    and the two answers cannot drift apart.

    A HEAD that is exactly the engine defaults means "this clip has no grade",
    which is how every project starts, so that case REMOVES the row instead of
    writing one. Without that rule, opening a clip would list it as graded and
    resetting a clip would leave it listed forever.

    Never raises: the row is a convenience copy, and failing to write it must
    not fail the edit that was actually asked for.
    """
    try:
        st = proj if proj is not None else state(key)
        if not st:
            return
        cfg = st.get("config") or {}
        if _is_defaults(cfg):
            _grades.delete_grade(int(user_id), key)
            return
        name = st.get("name") or ""
        if not name:
            row = _grades.get_grade(int(user_id), key)
            name = (row or {}).get("clip_name") or ""
        _grades.put_grade(int(user_id), key, name, cfg)
    except Exception as exc:                                 # noqa: BLE001
        print(f"projects: grade row not mirrored for {key[:7]} ({exc})",
              file=sys.stderr)


def commit(user_id: int, key: str, config: dict, author: str,
           message: str | None = None) -> dict:
    """Record one committed change. Returns the resulting project state.

    Three rules do all the work here:

    1. An identical config is not a commit. The tab republishes its whole
       config on a clip switch and on a preset load, and a history full of
       "no change" rows would bury the edits that mean something.
    2. Committing from a point that is not the tip of its branch FORKS first.
       Going back to an older commit and then grading must never throw away
       the work that came after it; it starts a new line instead, exactly the
       way git does.
    3. The message is generated from the change itself unless the caller has
       something better to say ("loaded preset nature_cinema", "saved grade").
    """
    _ensure()
    cfg = full_config(config)
    body = _canon(cfg)
    proj = ensure(key)
    con = _db.connect()
    try:
        head = proj["head"]
        branch = proj["branch"]
        old_cfg = None
        if head:
            row = con.execute("SELECT config_json FROM commits WHERE id=?",
                              (head,)).fetchone()
            if row:
                try:
                    old_cfg = json.loads(row["config_json"])
                except json.JSONDecodeError:
                    old_cfg = None
        if old_cfg is not None and _canon(full_config(old_cfg)) == body:
            return state(key)           # rule 1: nothing moved

        tip = _branch_tip(con, key, branch)
        now = time.time()
        if head and tip and head != tip:
            branch = _next_fork_name(con, key)     # rule 2: fork, do not lose
            con.execute(
                "INSERT INTO branches (project_key, name, tip, created) "
                "VALUES (?,?,?,?)", (key, branch, head, now))

        text = message or describe_change(old_cfg, cfg)
        cid = _commit_id(head, author, now, body)
        changes = changed_paths(old_cfg, cfg)
        if len(changes) > CHANGES_MAX:
            extra = len(changes) - CHANGES_MAX
            changes = changes[:CHANGES_MAX] + [
                {"path": "", "label": f"and {extra} more", "old": None,
                 "new": None}]
        con.execute(
            "INSERT INTO commits (id, project_key, parent, branch, author, ts,"
            " message, changes_json, config_json) VALUES (?,?,?,?,?,?,?,?,?)",
            (cid, key, head, branch, str(author or "cli"), now, text,
             json.dumps(changes), body))
        con.execute(
            "INSERT INTO branches (project_key, name, tip, created) "
            "VALUES (?,?,?,?) ON CONFLICT(project_key, name) DO UPDATE SET "
            "tip=excluded.tip", (key, branch, cid, now))
        con.execute("UPDATE projects SET head=?, branch=?, updated=? WHERE key=?",
                    (cid, branch, now, key))
        con.commit()
    finally:
        con.close()
    out = state(key)
    mirror_grade(user_id, key, out)
    return out


def _resolve_commit(con, key: str, ref: str) -> str:
    ref = str(ref or "").strip().lower()
    if not ref:
        raise ValueError("no commit given")
    row = con.execute("SELECT id FROM commits WHERE project_key=? AND id=?",
                      (key, ref)).fetchone()
    if row:
        return row["id"]
    rows = con.execute(
        "SELECT id FROM commits WHERE project_key=? AND id LIKE ?",
        (key, ref + "%")).fetchall()
    if not rows:
        raise ValueError(f"no commit {ref} in this project")
    if len(rows) > 1:
        raise ValueError(f"{ref} matches {len(rows)} commits, use more digits")
    return rows[0]["id"]


def checkout(user_id: int, key: str, ref: str) -> dict:
    """Move HEAD to a commit. Non destructive: nothing is deleted or rewritten."""
    _ensure()
    con = _db.connect()
    try:
        cid = _resolve_commit(con, key, ref)
        row = con.execute("SELECT branch FROM commits WHERE id=?", (cid,)).fetchone()
        con.execute("UPDATE projects SET head=?, branch=?, updated=? WHERE key=?",
                    (cid, row["branch"], time.time(), key))
        con.commit()
    finally:
        con.close()
    out = state(key)
    mirror_grade(user_id, key, out)
    return out


def fork(user_id: int, key: str, from_commit: str | None = None,
         name: str | None = None) -> dict:
    """Start a new branch here (or at a given commit) and move HEAD to it."""
    _ensure()
    proj = get(key)
    if proj is None:
        raise ValueError("no project for that clip")
    con = _db.connect()
    try:
        cid = _resolve_commit(con, key, from_commit) if from_commit else proj["head"]
        if not cid:
            raise ValueError("this project has no commit to fork from")
        if name:
            name = str(name).strip()
            if not NAME_RE.fullmatch(name) or len(name) > NAME_MAX:
                raise ValueError("branch names may use letters, digits, dot, "
                                 "dash and underscore only")
            exists = con.execute(
                "SELECT 1 FROM branches WHERE project_key=? AND name=?",
                (key, name)).fetchone()
            if exists:
                raise ValueError(f"this project already has a branch {name}")
        else:
            name = _next_fork_name(con, key)
        now = time.time()
        con.execute("INSERT INTO branches (project_key, name, tip, created) "
                    "VALUES (?,?,?,?)", (key, name, cid, now))
        con.execute("UPDATE projects SET head=?, branch=?, updated=? WHERE key=?",
                    (cid, name, now, key))
        con.commit()
    finally:
        con.close()
    out = state(key)
    mirror_grade(user_id, key, out)
    return out


def undo(user_id: int, key: str) -> dict:
    """HEAD steps back to its parent. The branch tip is kept, so redo works."""
    _ensure()
    proj = get(key)
    if proj is None:
        raise ValueError("no project for that clip")
    con = _db.connect()
    try:
        row = con.execute("SELECT parent FROM commits WHERE id=?",
                          (proj["head"],)).fetchone()
        parent = row["parent"] if row else None
        if not parent:
            out = state(key)
            out["moved"] = False
            out["note"] = "already at the first commit of this project"
            return out
        # The branch does not move: undo walks back along the line you are on,
        # and its tip is what makes redo possible. Only checkout and fork
        # change which branch you are standing on.
        con.execute("UPDATE projects SET head=?, updated=? WHERE key=?",
                    (parent, time.time(), key))
        con.commit()
    finally:
        con.close()
    out = state(key)
    mirror_grade(user_id, key, out)
    out["moved"] = True
    return out


def redo(user_id: int, key: str) -> dict:
    """HEAD steps forward, along the path from HEAD to the current branch tip."""
    _ensure()
    proj = get(key)
    if proj is None:
        raise ValueError("no project for that clip")
    con = _db.connect()
    try:
        tip = _branch_tip(con, key, proj["branch"])
        head = proj["head"]
        child = None
        node = tip
        seen = 0
        while node and node != head and seen < 100000:
            row = con.execute("SELECT parent FROM commits WHERE id=?",
                              (node,)).fetchone()
            parent = row["parent"] if row else None
            if parent == head:
                child = node
                break
            node = parent
            seen += 1
        if not child:
            out = state(key)
            out["moved"] = False
            out["note"] = "already at the newest commit on this branch"
            return out
        con.execute("UPDATE projects SET head=?, updated=? WHERE key=?",
                    (child, time.time(), key))
        con.commit()
    finally:
        con.close()
    out = state(key)
    mirror_grade(user_id, key, out)
    out["moved"] = True
    return out


# --------------------------------------------------------------------------
# project fields (not commits)
# --------------------------------------------------------------------------
#
# Rotation, playhead, loaded preset name and the extras bag describe how the
# clip is being VIEWED, not what the grade is. Committing them would fill the
# history with "scrubbed to 4.2 s" and make undo mean two different things.

def _set_field(key: str, column: str, value) -> dict:
    _ensure()
    con = _db.connect()
    try:
        cur = con.execute(f"UPDATE projects SET {column}=?, updated=? WHERE key=?",
                          (value, time.time(), key))
        con.commit()
        if cur.rowcount == 0:
            raise ValueError("no project for that clip")
    finally:
        con.close()
    return state(key)


def set_rotation(user_id: int, key: str, rotation: str) -> dict:
    rotation = str(rotation or "").strip().lower()
    if rotation in ("true", "yes", "on"):
        rotation = "auto"               # `autorotate: true` in old wire shapes
    if rotation in ("false", "no", "off"):
        rotation = "0"
    if rotation not in ROTATIONS:
        raise ValueError("rotation must be one of " + ", ".join(ROTATIONS))
    out = _set_field(key, "rotation", rotation)
    _invalidate_rotation()
    return out


def set_time(user_id: int, key: str, seconds: float) -> dict:
    try:
        value = max(0.0, float(seconds))
    except (TypeError, ValueError) as exc:
        raise ValueError("time must be a number of seconds") from exc
    return _set_field(key, "time", value)


def set_preset(user_id: int, key: str, name: str | None) -> dict:
    return _set_field(key, "preset", (str(name).strip() if name else None))


def set_extra(user_id: int, key: str, name: str, value) -> dict:
    """One entry in the project's extras bag, for small per project settings.

    Contract C7 keeps its match rectangles here. Anything that is not a colour
    parameter and does not belong in the history goes here rather than growing
    a column per feature.
    """
    name = str(name or "").strip()
    if not name or not NAME_RE.fullmatch(name):
        raise ValueError("an extra's name may use letters, digits, dot, dash "
                         "and underscore only")
    _ensure()
    con = _db.connect()
    try:
        row = con.execute("SELECT extras_json FROM projects WHERE key=?",
                          (key,)).fetchone()
        if not row:
            raise ValueError("no project for that clip")
        try:
            bag = json.loads(row["extras_json"] or "{}")
        except json.JSONDecodeError:
            bag = {}
        if not isinstance(bag, dict):
            bag = {}
        if value is None:
            bag.pop(name, None)
        else:
            bag[name] = value
        con.execute("UPDATE projects SET extras_json=?, updated=? WHERE key=?",
                    (json.dumps(bag, allow_nan=False), time.time(), key))
        con.commit()
    finally:
        con.close()
    return state(key)


# --------------------------------------------------------------------------
# reading the tree
# --------------------------------------------------------------------------

def log(user_id: int, key: str, limit: int = 200) -> dict:
    """The WHOLE tree, newest first: every branch, every commit, and HEAD.

    Not just the current branch. The History panel draws the graph, so it needs
    the branches nobody is standing on as much as the one they are.
    """
    _ensure()
    proj = get(key)
    if proj is None:
        raise ValueError("no project for that clip")
    limit = max(1, min(int(limit or 200), 2000))
    con = _db.connect()
    try:
        brows = con.execute(
            "SELECT name, tip, created FROM branches WHERE project_key=? "
            "ORDER BY created ASC", (key,)).fetchall()
        counts = {r["branch"]: r["n"] for r in con.execute(
            "SELECT branch, COUNT(*) AS n FROM commits WHERE project_key=? "
            "GROUP BY branch", (key,)).fetchall()}
        rows = con.execute(
            "SELECT * FROM commits WHERE project_key=? ORDER BY ts DESC, "
            "rowid DESC LIMIT ?", (key, limit)).fetchall()
        total = con.execute("SELECT COUNT(*) AS n FROM commits WHERE project_key=?",
                            (key,)).fetchone()["n"]
    finally:
        con.close()
    tips = {r["tip"] for r in brows if r["tip"]}
    head = proj["head"]
    return {
        "key": key,
        "name": proj["name"],
        "head": head,
        "head_short": short(head),
        "branch": proj["branch"],
        "branches": [{"name": r["name"], "tip": r["tip"],
                      "short": short(r["tip"]),
                      "commits": int(counts.get(r["name"], 0)),
                      "is_current": r["name"] == proj["branch"]}
                     for r in brows],
        "commits": [_commit_row(r, head, tips) for r in rows],
        "total": int(total),
        "limit": limit,
    }


def state(key: str) -> dict | None:
    """Everything GET /api/project answers with, config included.

    One connection for the whole record on purpose: this runs after every
    committed edit the tab publishes, and four separate opens for four small
    reads is four times the work on the hottest path in the module.
    """
    _ensure()
    con = _db.connect()
    try:
        prow = con.execute("SELECT * FROM projects WHERE key=?", (key,)).fetchone()
        if prow is None:
            return None
        proj = _project_row(prow)
        crow = None
        if proj["head"]:
            crow = con.execute("SELECT * FROM commits WHERE id=?",
                               (proj["head"],)).fetchone()
        brows = con.execute(
            "SELECT name, tip, created FROM branches WHERE project_key=? "
            "ORDER BY created ASC", (key,)).fetchall()
    finally:
        con.close()
    cfg = None
    head_row = None
    if crow is not None:
        try:
            # Back through full_config on the way out, not just json.loads: the
            # stored form is sorted for byte comparison, and a config handed to
            # the tab should come back in the engine's own key order, which is
            # what every other route serves and what the JSON panel shows.
            cfg = full_config(json.loads(crow["config_json"]))
        except json.JSONDecodeError:
            cfg = None
        tips = {r["tip"] for r in brows if r["tip"]}
        head_row = _commit_row(crow, proj["head"], tips)
        head_row.pop("changes", None)
    out = dict(proj)
    out["config"] = cfg if cfg is not None else defaults()
    out["head_short"] = short(proj["head"])
    out["head_commit"] = head_row
    out["branches"] = [{"name": r["name"], "tip": r["tip"],
                        "short": short(r["tip"]),
                        "is_current": r["name"] == proj["branch"]}
                       for r in brows]
    return out
