# tests/test_bulk_honesty.py — a bulk run's closing line must not overclaim.
#
# Seen live on a real instance: "✖ Host name did not resolve (gitlab.example.com) ...
# (groups/112/subgroups)" mid-run, then a green "✔ Clone All finished —
# 1/1 groups." Three defects pinned here:
#   1. API failures recorded during the run must turn the green tick into a
#      yellow "finished with N API error(s)" warning that lists them;
#   2. counts of 1 say "group", not "groups";
#   3. with user namespaces off, Clone All prints ONE line with the real
#      count, consistent with the group list — not a paragraph.
# Offline: no server, no token.

import contextlib
import io
import os
import re
import unittest
from unittest import mock

import _bootstrap  # noqa: F401  (sys.path + live-error-log isolation)
from _bootstrap import PROJECT_ROOT

import api
import gitlab_cli as G

ANSI = re.compile(r"\x1b\[[0-9;]*m")
URL = "http://git.example.net/api/v4/groups/112/subgroups"


def _out(fn, *a, **k):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ret = fn(*a, **k)
    return ret, ANSI.sub("", buf.getvalue())


def _groups(*names):
    return [{"id": i, "full_path": n} for i, n in enumerate(names, 1)]


class BulkApiFailures(unittest.TestCase):

    def setUp(self):
        api.reset_failures()
        self.addCleanup(api.reset_failures)

    def test_api_failure_during_run_replaces_the_green_tick(self):
        def run_one(_g):
            api.record_failure(0, "Host name did not resolve (gitlab.example.com)",
                               URL, quiet=True)
            api.record_failure(503, "Service unavailable", URL, quiet=True)
        _r, out = _out(G._run_bulk, "Clone All", _groups("roboshan"), run_one)
        self.assertNotIn("\u2714", out)
        self.assertIn("Clone All finished with 2 API error(s)", out)
        self.assertIn("results may be incomplete", out)
        self.assertIn("reconnect the VPN and re-run", out)
        self.assertIn("Host name did not resolve (gitlab.example.com)", out)
        self.assertIn("groups/112/subgroups", out)

    def test_failures_from_before_the_run_are_not_blamed_on_it(self):
        api.record_failure(500, "old", URL, quiet=True)
        _r, out = _out(G._run_bulk, "Clone All", _groups("a"), lambda g: None)
        self.assertIn("\u2714 Clone All finished \u2014 1/1 group.", out)
        self.assertNotIn("API error", out)

    def test_api_failures_are_listed_alongside_group_failures(self):
        def run_one(g):
            api.record_failure(0, "Host name did not resolve (gitlab.example.com)",
                               URL, quiet=True)
            raise RuntimeError("boom")
        _r, out = _out(G._run_bulk, "Sync All", _groups("a"), run_one)
        self.assertIn("1 API error(s)", out)
        self.assertIn("Host name did not resolve", out)


class Pluralisation(unittest.TestCase):

    def setUp(self):
        api.reset_failures()
        self.addCleanup(api.reset_failures)

    def test_one_group_is_singular(self):
        _r, out = _out(G._run_bulk, "Clone All", _groups("a"), lambda g: None)
        self.assertIn("1/1 group.", out)
        self.assertNotIn("groups", out)

    def test_two_groups_stay_plural(self):
        _r, out = _out(G._run_bulk, "Clone All", _groups("a", "b"),
                       lambda g: None)
        self.assertIn("2/2 groups.", out)

    def test_menu_incomplete_count_is_singular(self):
        _r, out = _out(G._print_group_menu, 1, False)
        self.assertIn("(1 group incomplete)", out)

    def test_no_bare_count_plus_plural_noun_in_source(self):
        with open(os.path.join(PROJECT_ROOT, "gitlab_cli.py"),
                  encoding="utf-8") as fh:
            src = fh.read()
        bad = re.findall(r"\{[^{}]*\}(?:/\{[^{}]*\})? (?:groups|repos|errors)\b",
                         src)
        self.assertEqual([], bad)


class UserNamespacesOffLine(unittest.TestCase):

    def setUp(self):
        self.cfg = {"name": "x", "url": "http://git.example.net", "headers": {}}
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("GITLAB_CLI_USER_NAMESPACES", None)

    def _gate(self, counts):
        return _out(G._user_namespace_gate, self.cfg, counts)

    def test_off_is_one_short_line_with_the_real_count(self):
        got, out = self._gate({"total": 1, "present": 1})
        self.assertEqual([], got)
        self.assertIn("User namespaces: off (1 project, already on disk)", out)
        self.assertIn("GITLAB_CLI_USER_NAMESPACES=own or all", out)
        self.assertNotIn("no request about them was made", out)
        self.assertEqual(1, len([l for l in out.splitlines() if l.strip()]))

    def test_off_with_some_missing(self):
        _got, out = self._gate({"total": 3, "present": 1})
        self.assertIn("User namespaces: off (3 projects, 2 not on disk)", out)

    def test_off_with_none_on_instance(self):
        _got, out = self._gate({"total": 0, "present": 0})
        self.assertIn("User namespaces: off (none on this instance)", out)

    def test_off_with_unknown_count_says_so(self):
        _got, out = self._gate(None)
        self.assertIn("User namespaces: off (count unknown)", out)

    def test_clone_all_passes_the_group_list_counts(self):
        with open(os.path.join(PROJECT_ROOT, "gitlab_cli.py"),
                  encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn('_user_namespace_gate(cfg, group.get("_user_ns"))', src)
        self.assertIn('"_user_ns": user_ns', src)


if __name__ == "__main__":
    unittest.main()
