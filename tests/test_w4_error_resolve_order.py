#!/usr/bin/env python3
# tests/test_w4_error_resolve_order.py
#
# Wave-4 regression test for D4 (reports/W3-8-interactive.md, HIGH).
#
# The Error Log printed entries newest-first:
#
#     for i, e in enumerate(sorted(filtered, key=..., reverse=True), 1):
#
# but looked the typed numbers up in `filtered`, which is still in FILE order
# (append order, i.e. oldest-first). The two orderings are exact reverses, so
# typing "1" to resolve the error shown as 1 resolved the LAST one on screen.
#
# Why HIGH and not cosmetic: `c  Clear all resolved` then deletes every entry
# flagged that way. A user triaging their log silently deletes the errors they
# have NOT dealt with and keeps the ones they have — and the app prints
# "✔ 1 error(s) marked resolved." either way.
#
# The fix is to sort once into a single canonical list and index THAT list.
# These tests pin the invariant, not the implementation: whatever ordering the
# screen uses, the number the user types must address the entry printed under
# that number.
#
# Offline, and the real ~/.gitlab-cli/errors.log is never touched — ERROR_DIR
# and ERROR_FILE are redirected into this test's own temp dir.

import builtins
import io
import json
import os
import re
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta

from _bootstrap import TempTreeTestCase

import errors as errors_mod

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _entry(title, days_ago, etype="Git", resolved=False):
    return {
        "date": (datetime.now() - timedelta(days=days_ago)).isoformat(timespec="seconds"),
        "type": etype,
        "title": title,
        "context": f"ctx-{title}",
        "fix": "do the thing",
        "resolved": resolved,
    }


class ErrorLogTestCase(TempTreeTestCase):
    """Redirects the error log into the temp tree for the whole test."""

    def setUp(self):
        super().setUp()
        self._real_dir = errors_mod.ERROR_DIR
        self._real_file = errors_mod.ERROR_FILE
        errors_mod.ERROR_DIR = os.path.join(self.tmp, "dot-gitlab-cli")
        errors_mod.ERROR_FILE = os.path.join(errors_mod.ERROR_DIR, "errors.log")
        os.makedirs(errors_mod.ERROR_DIR, exist_ok=True)

    def tearDown(self):
        errors_mod.ERROR_DIR = self._real_dir
        errors_mod.ERROR_FILE = self._real_file
        super().tearDown()

    # ── helpers ──────────────────────────────────────────────────────────────

    def write_log(self, entries):
        """Write entries in the given order — that IS the file/append order."""
        with open(errors_mod.ERROR_FILE, "w", encoding="utf-8") as fh:
            for e in entries:
                fh.write(json.dumps(e, ensure_ascii=False) + "\n")

    def read_log(self):
        out = []
        with open(errors_mod.ERROR_FILE, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out

    def run_menu(self, answers):
        """Drive cmd_errors with a scripted list of keystrokes."""
        script = list(answers)

        def fake_input(_prompt=""):
            if not script:
                raise AssertionError(
                    "cmd_errors asked for more input than the script provided")
            return script.pop(0)

        real_input = builtins.input
        builtins.input = fake_input
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                errors_mod.cmd_errors()
        finally:
            builtins.input = real_input
        return ANSI.sub("", buf.getvalue())

    def resolved_titles(self):
        return {e["title"] for e in self.read_log() if e.get("resolved")}

    def screen_order(self, text):
        """The titles in the order the screen numbered them."""
        order = []
        for line in text.splitlines():
            m = re.match(r"\s*(\d+)\.\s+\[", line)
            if m:
                order.append(int(m.group(1)))
        return order


class ResolveTargetsThePrintedNumberTests(ErrorLogTestCase):

    def test_resolving_1_resolves_the_entry_printed_as_1(self):
        # File order is oldest-first, which is what append order produces.
        self.write_log([_entry("OLDEST", 20), _entry("MIDDLE", 10), _entry("NEWEST", 1)])

        text = self.run_menu(["1", "r", "1"])

        # Establish what the screen actually showed under number 1, so the
        # assertion below does not assume a particular sort direction.
        titles_in_print_order = re.findall(r"^\s*\d+\.\s+\[[^\]]+\]\s+([A-Z_]+)\b", text, re.MULTILINE)
        self.assertTrue(titles_in_print_order,
                        f"could not parse the rendered list:\n{text}")
        shown_first = titles_in_print_order[0]

        self.assertEqual(
            self.resolved_titles(), {shown_first},
            "the number the user typed did not address the entry printed "
            "under that number — the screen sorts one way and the resolver "
            "indexes another")

    def test_screen_is_newest_first_and_that_is_what_gets_resolved(self):
        self.write_log([_entry("OLDEST", 20), _entry("MIDDLE", 10), _entry("NEWEST", 1)])
        text = self.run_menu(["1", "r", "1"])
        self.assertLess(text.index("NEWEST"), text.index("OLDEST"),
                        "the list is documented as newest-first")
        self.assertEqual(self.resolved_titles(), {"NEWEST"})

    def test_resolving_the_last_number_resolves_the_oldest(self):
        self.write_log([_entry("OLDEST", 20), _entry("MIDDLE", 10), _entry("NEWEST", 1)])
        self.run_menu(["1", "r", "3"])
        self.assertEqual(self.resolved_titles(), {"OLDEST"})

    def test_middle_number_resolves_the_middle_entry(self):
        self.write_log([_entry("OLDEST", 20), _entry("MIDDLE", 10), _entry("NEWEST", 1)])
        self.run_menu(["1", "r", "2"])
        self.assertEqual(self.resolved_titles(), {"MIDDLE"})

    def test_several_numbers_resolve_exactly_those_entries(self):
        self.write_log([_entry("A", 20), _entry("B", 5), _entry("C", 2), _entry("D", 1)])
        # newest-first on screen: D(1) C(2) B(3) A(4)
        self.run_menu(["1", "r", "1,4"])
        self.assertEqual(self.resolved_titles(), {"D", "A"})

    def test_file_order_already_newest_first_still_resolves_correctly(self):
        # The old bug was invisible on a log that happened to be sorted the
        # same way. Pinning both orders stops a fix that only works by luck.
        self.write_log([_entry("NEWEST", 1), _entry("MIDDLE", 10), _entry("OLDEST", 20)])
        self.run_menu(["1", "r", "1"])
        self.assertEqual(self.resolved_titles(), {"NEWEST"})

    def test_duplicate_number_resolves_one_entry_not_two(self):
        self.write_log([_entry("OLD", 10), _entry("NEW", 1)])
        text = self.run_menu(["1", "r", "1,1"])
        self.assertEqual(self.resolved_titles(), {"NEW"})
        self.assertIn("1 error(s) marked resolved", text)

    def test_out_of_range_number_is_rejected_and_resolves_nothing(self):
        self.write_log([_entry("OLD", 10), _entry("NEW", 1)])
        self.run_menu(["1", "r", "9"])
        self.assertEqual(self.resolved_titles(), set())

    def test_filtered_view_numbers_index_the_filtered_list(self):
        # Filter 2 = unresolved only. The numbers on screen must address the
        # FILTERED entries, never positions in the full log.
        self.write_log([
            _entry("RESOLVED_OLD", 20, resolved=True),
            _entry("OPEN_MID", 10),
            _entry("OPEN_NEW", 1),
        ])
        self.run_menu(["2", "r", "1"])
        self.assertEqual(
            self.resolved_titles(), {"RESOLVED_OLD", "OPEN_NEW"},
            "resolving 1 in a filtered view must hit the first FILTERED entry")


class ClearResolvedTests(ErrorLogTestCase):
    """`c` deletes what `r` flagged — so `r` hitting the wrong row loses data."""

    def test_clear_removes_only_the_entries_actually_resolved(self):
        self.write_log([_entry("OLDEST", 20), _entry("MIDDLE", 10), _entry("NEWEST", 1)])
        self.run_menu(["1", "r", "1"])          # resolve the one shown as 1
        self.run_menu(["1", "c"])               # then clear resolved
        remaining = {e["title"] for e in self.read_log()}
        self.assertEqual(
            remaining, {"OLDEST", "MIDDLE"},
            "clear must delete exactly the entry the user resolved")

    def test_clear_on_a_log_with_nothing_resolved_deletes_nothing(self):
        self.write_log([_entry("A", 5), _entry("B", 1)])
        self.run_menu(["1", "c"])
        self.assertEqual({e["title"] for e in self.read_log()}, {"A", "B"})


class LogFilePermissionTests(ErrorLogTestCase):
    """Every write-back path must re-assert 0600 — the log can quote git stderr."""

    @unittest.skipUnless(os.name == "posix", "POSIX modes only")
    def test_resolve_rewrite_keeps_the_log_private(self):
        self.write_log([_entry("A", 5), _entry("B", 1)])
        os.chmod(errors_mod.ERROR_FILE, 0o644)
        self.run_menu(["1", "r", "1"])
        self.assertEqual(os.stat(errors_mod.ERROR_FILE).st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main(verbosity=2)
