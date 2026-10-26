#!/usr/bin/env python3
# tests/test_w4_menu_input_validation.py
#
# Wave-4 regression tests for D7, D8 and D10 (reports/W3-8-interactive.md).
#
#   D7 (Medium)  gitlab_cli.py:282 — pressing Enter at "Which group?" falls
#                through every guard into the substring search, and
#                `"" in anything` is True, so a blank line "matches" all 23
#                groups and dumps the whole list under a heading claiming the
#                user searched for something. Enter is the single most common
#                accidental keystroke there is.
#
#   D8 (Medium)  gitlab_cli.py:272 — `"-1".isdigit()` is False, so a negative
#                number skips the range check and is reported as a missing
#                group NAME. `0` and `999` are handled correctly; only the
#                negative form is misrouted. (Verified in W3-8: a negative
#                index never reaches `groups[idx]`, so there is no
#                wrap-around selection bug — only a misleading message.)
#
#   D10 (Low)    gitlab_cli.py:608-617 — `_ask_next` tests only for "q" and
#                treats literally anything else as "back to menu", at the one
#                prompt that appears after every long operation, while
#                advertising exactly two valid answers.
#
# Offline: get_top_level_groups / scan_dest / group_coverage are stubbed in
# the gitlab_cli namespace, and input() is scripted. No network, no token, no
# filesystem writes outside the temp tree.

import builtins
import io
import re
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout

import _bootstrap  # noqa: F401  # puts the CLI's directory on sys.path
                   # so this module also runs standalone, not only via run_tests.py

import gitlab_cli

ANSI = re.compile(r"\x1b\[[0-9;]*m")

GROUPS = [
    {"id": 1, "path": "initech", "full_path": "initech", "visibility": "private"},
    {"id": 2, "path": "industry", "full_path": "industry", "visibility": "private"},
    {"id": 3, "path": "globex", "full_path": "globex", "visibility": "private"},
]


class _ScriptedInput:
    """Feeds input() a fixed script and records how many prompts were asked."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.asked = []
        self._real = None

    def __enter__(self):
        self._real = builtins.input

        def fake(prompt=""):
            self.asked.append(ANSI.sub("", str(prompt)))
            if not self.answers:
                raise AssertionError(
                    "the code asked for more input than the script provided; "
                    f"prompts so far: {self.asked}")
            return self.answers.pop(0)

        builtins.input = fake
        return self

    def __exit__(self, *exc):
        builtins.input = self._real
        return False

    @property
    def prompt_count(self):
        return len(self.asked)


class MenuTestCase(unittest.TestCase):
    """Stubs every network/disk call pick_group makes."""

    def setUp(self):
        self.dest = tempfile.mkdtemp(prefix="gitlabcli_test_")
        self.addCleanup(shutil.rmtree, self.dest, True)
        self._saved = {
            name: getattr(gitlab_cli, name)
            for name in ("get_top_level_groups", "scan_dest", "group_coverage",
                         "_print_orphans")
        }
        gitlab_cli.get_top_level_groups = lambda *a, **k: [dict(g) for g in GROUPS]
        gitlab_cli.scan_dest = lambda dest: {}
        gitlab_cli.group_coverage = lambda cfg, g, index: {
            "icon": "❌", "cloned": 0, "total": 3, "orphans": []}
        gitlab_cli._print_orphans = lambda covmap: None
        self.cfg = {"url": "https://hub.example", "headers": {}, "min_access": 0}

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(gitlab_cli, name, value)

    def pick(self, answers):
        """Run pick_group with a keystroke script; returns (result, stdout)."""
        buf = io.StringIO()
        with _ScriptedInput(answers) as scripted:
            with redirect_stdout(buf):
                try:
                    result = gitlab_cli.pick_group(self.cfg, self.dest)
                except SystemExit:
                    result = "_exit"
        self.last_script = scripted
        return result, ANSI.sub("", buf.getvalue())


class BlankInputTests(MenuTestCase):
    """D7 — Enter must not be read as 'search for the empty string'."""

    def test_enter_does_not_match_every_group(self):
        _result, out = self.pick(["", "q"])
        self.assertNotIn(
            "Several matches found", out,
            "a blank line was treated as a substring search; '' is in every "
            "string, so Enter dumps the entire group list as if the user had "
            "searched for it")

    def test_whitespace_only_input_is_not_a_search_either(self):
        _result, out = self.pick(["   ", "q"])
        self.assertNotIn("Several matches found", out)

    def test_enter_never_selects_a_group_by_accident(self):
        # This passes today and is the part that must never regress: whatever
        # a blank line does, it must not return a group.
        result, _out = self.pick(["", "q"])
        self.assertEqual(
            result, "_exit",
            "a blank line must never resolve to a group selection")


class NumericInputTests(MenuTestCase):
    """D8 — a number out of range must be reported as a number."""

    def test_zero_is_reported_as_out_of_range(self):
        _result, out = self.pick(["0", "q"])
        self.assertIn("Pick a number from 1 to 3", out)

    def test_too_large_is_reported_as_out_of_range(self):
        _result, out = self.pick(["999", "q"])
        self.assertIn("Pick a number from 1 to 3", out)

    def test_negative_is_reported_as_out_of_range_not_as_a_missing_name(self):
        _result, out = self.pick(["-1", "q"])
        self.assertIn(
            "Pick a number from 1 to 3", out,
            "'-1' fails .isdigit(), skips the range check and is reported as "
            "a group name that does not exist — sending the user to look for "
            "a group called '-1'")

    def test_negative_is_not_described_as_not_found(self):
        _result, out = self.pick(["-1", "q"])
        self.assertNotIn("'-1' not found", out)

    def test_a_valid_number_selects_that_group(self):
        result, _out = self.pick(["2"])
        self.assertEqual(result["full_path"], "industry")

    def test_a_negative_number_never_wraps_around_to_a_selection(self):
        # Explicitly checked in W3-8 and pinned here: the misleading message is
        # the whole defect; there is no index wrap-around behind it.
        result, _out = self.pick(["-1", "q"])
        self.assertEqual(result, "_exit")

    def test_an_unambiguous_name_still_selects(self):
        result, _out = self.pick(["industry"])
        self.assertEqual(result["full_path"], "industry")

    def test_name_search_is_case_insensitive(self):
        result, _out = self.pick(["GLOBEX"])
        self.assertEqual(result["full_path"], "globex")


class AskNextTests(unittest.TestCase):
    """D10 — the post-operation prompt advertises two answers; honour them."""

    def run_ask_next(self, answers):
        buf = io.StringIO()
        with _ScriptedInput(answers) as scripted:
            with redirect_stdout(buf):
                try:
                    gitlab_cli._ask_next()
                except SystemExit:
                    pass
        return scripted

    def test_q_exits(self):
        with self.assertRaises(SystemExit):
            with _ScriptedInput(["q"]):
                with redirect_stdout(io.StringIO()):
                    gitlab_cli._ask_next()

    def test_b_returns_to_the_menu(self):
        scripted = self.run_ask_next(["b"])
        self.assertEqual(scripted.prompt_count, 1)

    def test_garbage_is_not_silently_accepted_as_back(self):
        scripted = self.run_ask_next(["zz", "b"])
        self.assertGreater(
            scripted.prompt_count, 1,
            "'zz' was accepted as 'back to menu'; only 'q' is ever tested, so "
            "a fat-fingered key at the prompt that follows every long "
            "operation is silently treated as a valid answer")


if __name__ == "__main__":
    unittest.main(verbosity=2)
