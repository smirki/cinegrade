#!/usr/bin/env python3
"""Contract C1: the project store, its history, and the messages it writes.

Run from content/:

    .venv/bin/python -m unittest discover -s studio/tests/py -v

Everything here runs against a TEMPORARY database created in setUpModule and
deleted in tearDownModule. That is not politeness, it is the point: the real
studio/data/studio.db holds somebody's accounts, their saved grades and, after
this arc, their project history. A test suite that opened it would be editing
real work. STUDIO_DATA_DIR is set before db.py is imported so the path is
temporary from the first connection, and _guard() below refuses to run at all
if the database under the test is anywhere near studio/data.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
STUDIO = HERE.parent.parent
CONTENT = STUDIO.parent

TMP = Path(tempfile.mkdtemp(prefix="studio-projects-test-"))
os.environ["STUDIO_DATA_DIR"] = str(TMP)

sys.path.insert(0, str(STUDIO))
sys.path.insert(0, str(CONTENT / "grade"))

import db                 # noqa: E402  (the env var has to be set first)
import grades             # noqa: E402
import projects as P      # noqa: E402

db.set_data_dir(TMP)
grades.USERS_DIR = db.DATA / "users"


def _guard() -> None:
    real = (STUDIO / "data").resolve()
    used = Path(db.DB_PATH).resolve()
    if str(used).startswith(str(real)):
        raise SystemExit(f"refusing to run: the tests would write {used}, "
                         f"which is the real studio database")


def setUpModule() -> None:
    _guard()
    db.init_schema()
    grades.init_schema()
    P.init_schema()
    # Clip resolution without any real footage: the store only ever sees a
    # content key, a display name and a path, so a fake resolver is a complete
    # substitute and the tests do not depend on what is in content/footage.
    P.bind_resolver(lambda name: (
        "0" * (32 - len(str(name))) + "".join(
            c if c in "0123456789abcdef" else "a" for c in str(name).lower()),
        str(name), f"/tmp/footage/{name}"))


def tearDownModule() -> None:
    shutil.rmtree(TMP, ignore_errors=True)


def cfg(**over):
    """A full config with a few leaves moved, by dotted path."""
    out = P.defaults()
    for path, value in over.items():
        node = out
        parts = path.split("__")
        for p in parts[:-1]:
            node = node[int(p)] if p.isdigit() else node[p]
        last = parts[-1]
        if last.isdigit():
            node[int(last)] = value
        else:
            node[last] = value
    return out


def layer(name="Sky", **over):
    lay = {
        "enabled": True, "name": name, "placement": "before_look",
        "mask": {"show": False, "invert": False,
                 "window": {"enabled": False, "shape": "ellipse", "cx": 0.5,
                            "cy": 0.5, "w": 0.6, "h": 0.6, "rotation": 0.0,
                            "softness": 0.15, "invert": False},
                 "key": {"enabled": False, "invert": False, "hue_center": 30.0,
                         "hue_width": 40.0, "hue_soft": 15.0, "sat_low": 0.1,
                         "sat_high": 1.0, "sat_soft": 0.1, "lum_low": 0.0,
                         "lum_high": 1.0, "lum_soft": 0.1}},
        "correct": {"exposure": 0.0, "contrast": 1.0, "pivot": None,
                    "saturation": 1.0, "temperature": 0.0, "tint": 0.0,
                    "hue_shift": 0.0, "sat_gain": 1.0, "lum_gain": 1.0,
                    "offset": [0.0, 0.0, 0.0], "blur": 0.0, "strength": 1.0},
    }
    for path, value in over.items():
        node = lay
        parts = path.split("__")
        for p in parts[:-1]:
            node = node[p]
        node[parts[-1]] = value
    return lay


class OpenAndMigrate(unittest.TestCase):
    def test_open_creates_a_root_commit_from_the_defaults(self):
        st = P.open(1, "clipaaa1")
        self.assertIsNotNone(st["head"])
        self.assertEqual(st["branch"], "main")
        self.assertEqual(st["rotation"], "auto")
        self.assertEqual(st["config"], P.defaults())
        tree = P.log(1, st["key"])
        self.assertEqual(len(tree["commits"]), 1)
        self.assertEqual(tree["commits"][0]["message"], "root")
        self.assertEqual(tree["commits"][0]["author"], "server")
        self.assertTrue(tree["commits"][0]["is_head"])
        self.assertTrue(tree["commits"][0]["is_tip"])

    def test_the_config_comes_back_in_the_engine_s_own_key_order(self):
        """Not a detail: the JSON panel and the UI harness compare the config
        as a STRING against /api/state's defaults, so a config that came back
        with its keys sorted would read as a different config."""
        st = P.open(1, "clipaaa5")
        self.assertEqual(json.dumps(st["config"]), json.dumps(P.defaults()))
        P.commit(1, st["key"], cfg(primaries__saturation=1.1), "a")
        after = P.state(st["key"])["config"]
        self.assertEqual(list(after.keys()), list(P.defaults().keys()))

    def test_open_parks_this_account_on_the_project(self):
        st = P.open(7, "clipaaa2")
        self.assertEqual(P.workspace_key(7), st["key"])
        self.assertIn(7, P.workspaces())

    def test_open_migrates_the_old_saved_grade_into_the_root(self):
        key, name, path = P.resolve("clipaaa3")
        grades.put_grade(0, key, name, P.full_config({"grain": {"enabled": True}}))
        st = P.open(1, "clipaaa3")
        self.assertTrue(st["config"]["grain"]["enabled"])
        tree = P.log(1, key)
        self.assertEqual(tree["commits"][0]["message"], "root: migrated saved grade")
        # and the grades row it came from is still there, untouched
        self.assertIsNotNone(grades.get_grade(0, key))

    def test_open_is_shared_between_accounts(self):
        a = P.open(1, "clipaaa4")
        b = P.open(2, "clipaaa4")
        self.assertEqual(a["key"], b["key"])
        self.assertEqual(a["head"], b["head"])
        P.commit(2, b["key"], cfg(primaries__saturation=1.4), "manav")
        self.assertEqual(P.state(a["key"])["config"]["primaries"]["saturation"], 1.4)


class Commits(unittest.TestCase):
    def test_an_identical_config_is_not_a_commit(self):
        st = P.open(1, "clipbbb1")
        first = st["head"]
        again = P.commit(1, st["key"], P.defaults(), "studio")
        self.assertEqual(again["head"], first)
        self.assertEqual(P.log(1, st["key"])["total"], 1)

    def test_a_real_change_commits_with_an_author_and_a_message(self):
        st = P.open(1, "clipbbb2")
        after = P.commit(1, st["key"], cfg(primaries__saturation=1.2), "agent:lane")
        self.assertNotEqual(after["head"], st["head"])
        top = P.log(1, st["key"])["commits"][0]
        self.assertEqual(top["author"], "agent:lane")
        self.assertEqual(top["message"], "saturation 1.00 to 1.20")
        self.assertEqual(top["parent"], st["head"])
        self.assertEqual(len(top["short"]), 7)
        self.assertEqual(top["changes"][0]["path"], "primaries.saturation")
        self.assertEqual(top["changes"][0]["old"], 1.0)
        self.assertEqual(top["changes"][0]["new"], 1.2)

    def test_a_caller_supplied_message_wins(self):
        st = P.open(1, "clipbbb3")
        P.commit(1, st["key"], cfg(look__mix=0.5), "studio",
                 message="loaded preset nature_cinema")
        self.assertEqual(P.log(1, st["key"])["commits"][0]["message"],
                         "loaded preset nature_cinema")

    def test_committing_from_an_older_point_forks_instead_of_losing_work(self):
        st = P.open(1, "clipbbb4")
        root = st["head"]
        one = P.commit(1, st["key"], cfg(primaries__saturation=1.2), "a")
        two = P.commit(1, st["key"], cfg(primaries__saturation=1.4), "a")
        self.assertEqual(P.log(1, st["key"])["total"], 3)
        back = P.checkout(1, st["key"], root)
        self.assertEqual(back["head"], root)
        forked = P.commit(1, st["key"], cfg(primaries__contrast=1.3), "a")
        self.assertEqual(forked["branch"], "fork-1")
        tree = P.log(1, st["key"])
        self.assertEqual(tree["total"], 4)
        names = [b["name"] for b in tree["branches"]]
        self.assertEqual(names, ["main", "fork-1"])
        # the forward chain is still there, still on main, still its tip
        tips = {b["name"]: b["tip"] for b in tree["branches"]}
        self.assertEqual(tips["main"], two["head"])
        self.assertEqual(tips["fork-1"], forked["head"])
        self.assertEqual([c["id"] for c in tree["commits"] if c["is_head"]],
                         [forked["head"]])
        self.assertIn(one["head"], [c["id"] for c in tree["commits"]])


class GradeRowMirror(unittest.TestCase):
    """The old per clip `grades` row follows HEAD, so the picker stays right.

    The tab no longer autosaves through PUT /api/grade (it commits through the
    session), so a commit is the only thing that can keep that row current.
    GET /api/grades, GET /api/grade for a clip with no project and
    POST /api/grade/copy all read it.
    """

    def test_a_session_commit_shows_up_in_the_saved_grades_list(self):
        st = P.open(1, "graderow1")
        key = st["key"]
        P.commit(1, key, cfg(primaries__saturation=1.2), "studio")
        listed = {r["clip_key"]: r for r in grades.list_grades(1)}
        self.assertIn(key, listed)
        self.assertEqual(listed[key]["clip_name"], "graderow1")
        row = grades.get_grade(1, key)
        self.assertEqual(json.dumps(row["config"]),
                         json.dumps(P.state(key)["config"]))

    def test_undo_puts_the_parent_s_config_in_the_row(self):
        st = P.open(1, "graderow2")
        key = st["key"]
        P.commit(1, key, cfg(primaries__saturation=1.2), "studio")
        P.commit(1, key, cfg(primaries__saturation=1.6), "studio")
        self.assertEqual(
            grades.get_grade(1, key)["config"]["primaries"]["saturation"], 1.6)
        back = P.undo(1, key)
        row = grades.get_grade(1, key)
        self.assertEqual(row["config"]["primaries"]["saturation"], 1.2)
        self.assertEqual(json.dumps(row["config"]), json.dumps(back["config"]))

    def test_a_project_that_is_only_defaults_has_no_row_at_all(self):
        st = P.open(1, "graderow3")
        self.assertIsNone(grades.get_grade(1, st["key"]))
        self.assertNotIn(st["key"],
                         [r["clip_key"] for r in grades.list_grades(1)])

    def test_going_back_to_the_defaults_takes_the_row_away_again(self):
        st = P.open(1, "graderow4")
        key = st["key"]
        P.commit(1, key, cfg(look__mix=0.5), "studio")
        self.assertIsNotNone(grades.get_grade(1, key))
        P.checkout(1, key, st["head"])
        self.assertIsNone(grades.get_grade(1, key))

    def test_a_fork_writes_the_row_it_lands_on(self):
        st = P.open(1, "graderow5")
        key = st["key"]
        one = P.commit(1, key, cfg(primaries__saturation=1.2), "studio")
        P.commit(1, key, cfg(primaries__saturation=1.9), "studio")
        P.fork(1, key, from_commit=one["head"], name="night")
        self.assertEqual(
            grades.get_grade(1, key)["config"]["primaries"]["saturation"], 1.2)

    def test_the_mirror_is_quiet_about_a_clip_with_no_project(self):
        self.assertIsNone(P.mirror_grade(1, "f" * 32))


class Navigation(unittest.TestCase):
    def setUp(self):
        self.st = P.open(1, "clipccc" + self.id().split("_")[-1][:1])
        self.key = self.st["key"]
        self.root = self.st["head"]
        self.one = P.commit(1, self.key, cfg(primaries__saturation=1.2), "a")["head"]
        self.two = P.commit(1, self.key, cfg(primaries__saturation=1.4), "a")["head"]

    def test_undo_and_redo_walk_the_branch(self):
        back = P.undo(1, self.key)
        self.assertTrue(back["moved"])
        self.assertEqual(back["head"], self.one)
        self.assertEqual(back["config"]["primaries"]["saturation"], 1.2)
        again = P.undo(1, self.key)
        self.assertEqual(again["head"], self.root)
        stuck = P.undo(1, self.key)
        self.assertFalse(stuck["moved"])
        self.assertIn("first commit", stuck["note"])
        fwd = P.redo(1, self.key)
        self.assertTrue(fwd["moved"])
        self.assertEqual(fwd["head"], self.one)
        self.assertEqual(P.redo(1, self.key)["head"], self.two)
        end = P.redo(1, self.key)
        self.assertFalse(end["moved"])
        self.assertIn("newest commit", end["note"])
        # nothing was deleted by any of that
        self.assertEqual(P.log(1, self.key)["total"], 3)

    def test_checkout_takes_a_short_id(self):
        st = P.checkout(1, self.key, self.one[:7])
        self.assertEqual(st["head"], self.one)
        self.assertEqual(st["config"]["primaries"]["saturation"], 1.2)
        with self.assertRaises(ValueError):
            P.checkout(1, self.key, "ffffff0")

    def test_fork_names_itself_and_moves_head_without_a_commit(self):
        before = P.log(1, self.key)["total"]
        st = P.fork(1, self.key)
        self.assertEqual(st["branch"], "fork-1")
        self.assertEqual(st["head"], self.two)
        self.assertEqual(P.log(1, self.key)["total"], before)
        named = P.fork(1, self.key, from_commit=self.root, name="night")
        self.assertEqual(named["branch"], "night")
        self.assertEqual(named["head"], self.root)
        with self.assertRaises(ValueError):
            P.fork(1, self.key, name="night")
        with self.assertRaises(ValueError):
            P.fork(1, self.key, name="not a branch name")


class Fields(unittest.TestCase):
    def test_rotation_and_time_and_preset_are_not_commits(self):
        st = P.open(1, "clipddd1")
        key = st["key"]
        before = P.log(1, key)["total"]
        P.set_rotation(1, key, "90")
        P.set_time(1, key, 4.25)
        P.set_preset(1, key, "nature_cinema")
        after = P.state(key)
        self.assertEqual(after["rotation"], "90")
        self.assertEqual(after["time"], 4.25)
        self.assertEqual(after["preset"], "nature_cinema")
        self.assertEqual(P.log(1, key)["total"], before)
        self.assertEqual(after["head"], st["head"])

    def test_rotation_rejects_anything_else_and_accepts_the_old_boolean(self):
        st = P.open(1, "clipddd2")
        with self.assertRaises(ValueError):
            P.set_rotation(1, st["key"], "45")
        self.assertEqual(P.set_rotation(1, st["key"], "true")["rotation"], "auto")
        self.assertEqual(P.set_rotation(1, st["key"], "false")["rotation"], "0")

    def test_open_rotation_answers_for_the_open_project_and_never_raises(self):
        st = P.open(5, "clipddd3")
        self.assertEqual(P.open_rotation(5), "auto")
        P.set_rotation(5, st["key"], "270")
        self.assertEqual(P.open_rotation(5), "270")
        self.assertIsNone(P.open_rotation(999))

    def test_extras_are_a_bag_the_project_carries(self):
        st = P.open(1, "clipddd4")
        key = st["key"]
        crops = {"ref.png": {"ref": [0.1, 0.1, 0.5, 0.5]}}
        out = P.set_extra(1, key, "match_crops", crops)
        self.assertEqual(out["extras"]["match_crops"], crops)
        self.assertEqual(P.state(key)["extras"]["match_crops"], crops)
        self.assertEqual(P.set_extra(1, key, "match_crops", None)["extras"], {})
        with self.assertRaises(ValueError):
            P.set_extra(1, key, "bad name", 1)


class Describe(unittest.TestCase):
    """Twelve and more readable messages, in the words the panel uses."""

    def one(self, **over):
        return P.describe_change(P.defaults(), cfg(**over))

    def test_a_single_slider(self):
        self.assertEqual(self.one(primaries__saturation=1.2),
                         "saturation 1.00 to 1.20")

    def test_a_bipolar_slider_shows_where_it_landed_with_its_unit(self):
        self.assertEqual(self.one(convert__exposure=0.7), "exposure +0.70 stops")

    def test_two_changes_are_listed_compactly(self):
        self.assertEqual(
            self.one(look__lut="grade/luts/looks/kodak2383.cube", look__mix=0.25),
            "look LUT kodak2383, mix 0.25")

    def test_an_enable_reads_as_the_section_it_turns_on(self):
        self.assertEqual(self.one(curves__enabled=True), "curves on")
        self.assertEqual(self.one(grain__enabled=True, grain__stock="35mm"),
                         "grain on, 35mm")

    def test_three_or_more_changes_are_counted_and_named(self):
        self.assertEqual(
            self.one(primaries__contrast=1.2, primaries__pivot=0.4,
                     primaries__highlight_rolloff=0.3),
            "3 changes: contrast, pivot, highlight rolloff")

    def test_more_than_four_changes_say_how_many_are_not_listed(self):
        msg = self.one(primaries__contrast=1.2, primaries__pivot=0.4,
                       primaries__highlight_rolloff=0.3,
                       primaries__saturation=1.1, primaries__vibrance=0.2,
                       primaries__black_lift=0.05)
        self.assertEqual(msg, "6 changes: contrast, pivot, saturation, "
                              "vibrance and 2 more")

    def test_a_layer_added_and_removed(self):
        one = cfg()
        one["layers"] = [layer("Sky")]
        self.assertEqual(P.describe_change(P.defaults(), one), 'layer "Sky" added')
        self.assertEqual(P.describe_change(one, P.defaults()), 'layer "Sky" removed')

    def test_inside_a_layer_the_layer_is_named(self):
        one = cfg()
        one["layers"] = [layer("Sky")]
        two = json.loads(json.dumps(one))
        two["layers"][0]["mask"]["key"]["hue_center"] = 45.0
        self.assertEqual(P.describe_change(one, two),
                         'layer "Sky" key hue centre 30 to 45')

    def test_renaming_a_layer_says_both_names(self):
        one = cfg()
        one["layers"] = [layer("Sky")]
        two = json.loads(json.dumps(one))
        two["layers"][0]["name"] = "Ground"
        self.assertEqual(P.describe_change(one, two),
                         'layer "Sky" renamed to "Ground"')

    def test_a_slice_vector(self):
        self.assertEqual(self.one(slice__vectors__green__hue=-12.0),
                         "HSL slice green hue -12")

    def test_a_curve_says_it_was_edited(self):
        self.assertEqual(self.one(curves__master=[[0.0, 0.0], [0.5, 0.6], [1.0, 1.0]]),
                         "master curve edited")

    def test_clearing_a_lut(self):
        one = cfg(look__lut="grade/luts/looks/kodak2383.cube")
        self.assertEqual(P.describe_change(one, P.defaults()), "reset to defaults")
        two = cfg(look__lut="grade/luts/looks/kodak2383.cube", grain__enabled=True)
        three = cfg(grain__enabled=True)
        self.assertEqual(P.describe_change(two, three), "look LUT cleared")

    def test_back_to_the_defaults(self):
        self.assertEqual(P.describe_change(cfg(primaries__saturation=1.4),
                                           P.defaults()),
                         "reset to defaults")

    def test_nothing_moved(self):
        self.assertEqual(P.describe_change(P.defaults(), P.defaults()), "no change")

    def test_a_select_names_its_field(self):
        self.assertEqual(self.one(convert__tonemap="filmic"), "tone map filmic")

    def test_no_arrows_and_no_dashes_as_separators(self):
        for msg in (self.one(primaries__saturation=1.2),
                    self.one(convert__exposure=0.7),
                    self.one(grain__enabled=True, grain__stock="35mm")):
            for bad in ("->", "\u2192", "\u2014", "\u2013"):
                self.assertNotIn(bad, msg)

    def test_a_comment_riding_in_the_config_is_not_a_change(self):
        one = cfg()
        two = cfg()
        two["_comment"] = "from a preset"
        self.assertEqual(P.changed_paths(one, two), [])


class Log(unittest.TestCase):
    def test_the_log_is_the_whole_tree_newest_first(self):
        st = P.open(1, "clipeee1")
        key = st["key"]
        P.commit(1, key, cfg(primaries__saturation=1.2), "manav")
        P.fork(1, key, from_commit=st["head"], name="night")
        P.commit(1, key, cfg(primaries__contrast=1.3), "agent:lane")
        tree = P.log(1, key)
        self.assertEqual(tree["total"], 3)
        self.assertEqual(len(tree["commits"]), 3)
        self.assertEqual([b["name"] for b in tree["branches"]], ["main", "night"])
        self.assertEqual({c["author"] for c in tree["commits"]},
                         {"server", "manav", "agent:lane"})
        self.assertEqual(tree["commits"][0]["branch"], "night")
        self.assertGreaterEqual(tree["commits"][0]["ts"], tree["commits"][-1]["ts"])
        self.assertEqual(sum(1 for c in tree["commits"] if c["is_head"]), 1)


class Reopen(unittest.TestCase):
    """A restart is a fresh import against the same file, and loses nothing."""

    def test_a_fresh_module_on_the_same_database_sees_the_same_state(self):
        st = P.open(3, "clipfff1")
        key = st["key"]
        P.commit(3, key, cfg(primaries__saturation=1.35), "manav")
        P.set_rotation(3, key, "180")
        P.set_time(3, key, 4.0)

        import importlib
        fresh = importlib.reload(P)
        fresh.bind_resolver(lambda name: (key, str(name), "/tmp/footage/x"))
        self.assertEqual(fresh.workspace_key(3), key)
        again = fresh.state(key)
        self.assertEqual(again["config"]["primaries"]["saturation"], 1.35)
        self.assertEqual(again["rotation"], "180")
        self.assertEqual(again["time"], 4.0)
        self.assertEqual(again["head"], P.state(key)["head"])
        self.assertEqual(fresh.open_rotation(3), "180")
        # put the module the other tests hold back the way it was
        importlib.reload(P)
        setUpModule()


if __name__ == "__main__":
    unittest.main()
