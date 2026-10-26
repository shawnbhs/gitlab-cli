# tests/test_w10_menu_disclosures.py — W10: the API's honesty reaches the screen.
#
# W3/W9 made api.py honest: get_paginated() raises instead of returning a short
# list, and every listing comes back as an api.PagedResult carrying
#
#     .complete  did the walk actually finish?
#     .error     the ApiError that stopped it
#     .total     the server's own X-Total for the query
#     .note      what this listing does and does NOT contain (archived filter,
#                subgroup inclusion, a refused scope)
#
# and api.record_failure() logs EVERY failure to a ledger whether or not
# `quiet` printed a line, precisely because the founding defect was a failure
# nobody was told about.
#
# gitlab_cli.py then threw all of it away. `group_coverage()` did
# `except Exception: projects = []`, so a group the server REFUSED to list
# rendered with the same ✅ as a fully mirrored one and contributed 0/0 to the
# roll-up. The disclosures existed; nothing displayed them.
#
# These tests pin the rendering, not the metadata. Each fails against the
# pre-W10 gitlab_cli.py.
#
# Offline: no network, no token, no GitLab. Every API call is a stub and the
# destination is a tempdir.

import builtins
import io
import os
import re
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout

import _bootstrap  # noqa: F401  (sys.path + live-error-log isolation)
from _bootstrap import PROJECT_ROOT

import api
import coverage as cov
import gitlab_cli as G
import setup as setup_mod


ANSI = re.compile(r"\x1b\[[0-9;]*m")
HOST = "git.example.net"
BASE = "http://" + HOST

GROUPS = [
    {"id": 1, "path": "alpha", "full_path": "alpha", "visibility": "private"},
    {"id": 2, "path": "beta", "full_path": "beta", "visibility": "private"},
]


def cfg():
    return {"url": BASE, "headers": {"PRIVATE-TOKEN": "offline"},
            "min_access": 0, "master_dir": ".", "name": "test"}


def _src(name="gitlab_cli.py"):
    with open(os.path.join(PROJECT_ROOT, name), encoding="utf-8") as fh:
        return fh.read()


def proj(pwn, kind="group"):
    return {"path_with_namespace": pwn,
            "namespace": {"kind": kind, "full_path": pwn.rsplit("/", 1)[0]}}


def paged(paths, note="", complete=True, error=None, total=None):
    """A listing shaped exactly like the one api.py hands back."""
    return api.PagedResult([proj(p) for p in paths], note=note,
                           complete=complete, error=error, total=total)


class MenuHarness(unittest.TestCase):
    """pick_group with every outside edge stubbed."""

    def setUp(self):
        self.dest = tempfile.mkdtemp(prefix="gitlabcli_w10_")
        self.addCleanup(shutil.rmtree, self.dest, True)
        api.reset_failures()
        self.addCleanup(api.reset_failures)

        self._saved = {n: getattr(G, n) for n in
                       ("get_top_level_groups", "scan_dest")}
        self.addCleanup(lambda: [setattr(G, n, v)
                                 for n, v in self._saved.items()])
        G.get_top_level_groups = lambda *a, **k: [dict(g) for g in GROUPS]
        G.scan_dest = lambda dest: dict(self.index)

        self._rp = cov.remote_projects
        self.addCleanup(setattr, cov, "remote_projects", self._rp)

        self._sr = setup_mod._remote_projects
        self.addCleanup(setattr, setup_mod, "_remote_projects", self._sr)

        self._io = cov.instance_orphans
        self.addCleanup(setattr, cov, "instance_orphans", self._io)
        cov.instance_orphans = lambda c, i, p, probe=True: []

        self.index = {}
        self.listings = {1: paged(["alpha/a"]), 2: paged(["beta/b"])}
        cov.remote_projects = lambda c, g: self._listing(g)
        setup_mod._remote_projects = lambda c: (
            [proj("alpha/a"), proj("beta/b")], "")

    def _listing(self, group):
        value = self.listings[group["id"]]
        if isinstance(value, Exception):
            raise value
        return value

    def run_menu(self, answer="q"):
        real_input, out = builtins.input, io.StringIO()
        builtins.input = lambda *a, **k: answer
        try:
            with redirect_stdout(out):
                try:
                    result = G.pick_group(cfg(), self.dest)
                except SystemExit:
                    result = "_exit"
        finally:
            builtins.input = real_input
        return result, ANSI.sub("", out.getvalue())


# ─── group_coverage carries the metadata instead of dropping it ───────────────

class CoverageKeepsWhatTheListingSaid(MenuHarness):

    def test_the_listing_note_survives_into_the_coverage_dict(self):
        self.listings[1] = paged(
            ["alpha/a"],
            note="includes archived projects; includes subgroup projects")
        c = G.group_coverage(cfg(), GROUPS[0], {})
        self.assertIn("archived", c["note"])
        self.assertIn("subgroup", c["note"],
                      "PagedResult.note is the only statement of what the "
                      "denominator contains; dropping it prints a fraction "
                      "with no population")

    def test_the_servers_own_total_survives(self):
        self.listings[1] = paged(["alpha/a"], total=97)
        self.assertEqual(97, G.group_coverage(cfg(), GROUPS[0], {})["server_total"])

    def test_a_refused_listing_is_UNKNOWN_not_complete(self):
        self.listings[1] = api.ApiError(403, "Access denied", BASE)
        c = G.group_coverage(cfg(), GROUPS[0], {})
        self.assertEqual(G.UNKNOWN_ICON, c["icon"],
                         "a group the server would not list rendered as ✅: "
                         "status_icon() calls a ZERO denominator complete")
        self.assertFalse(c["complete"])
        self.assertIn("403", c["error"])

    def test_an_incomplete_walk_is_UNKNOWN_even_though_it_returned_rows(self):
        # strict=False's shape: rows came back, but not all of them. A
        # fraction computed over a short denominator reads as better coverage
        # than reality — the exact silent-skip defect.
        self.listings[1] = paged(
            ["alpha/a"], complete=False,
            error=api.ApiError(500, "page 2 failed", BASE))
        c = G.group_coverage(cfg(), GROUPS[0], {})
        self.assertEqual(G.UNKNOWN_ICON, c["icon"])
        self.assertFalse(c["complete"])
        self.assertTrue(c["error"])

    def test_a_complete_listing_is_still_a_normal_icon(self):
        c = G.group_coverage(cfg(), GROUPS[0], {})
        self.assertNotEqual(G.UNKNOWN_ICON, c["icon"])
        self.assertTrue(c["complete"])
        self.assertEqual("", c["error"])

    def test_group_coverage_no_longer_probes_one_project_at_a_time(self):
        code = "\n".join(ln for ln in _src().splitlines()
                         if not ln.lstrip().startswith("#"))
        self.assertFalse(
            "get_project(" in code,
            "the per-orphan get_project() probe was an N+1 that also "
            "reported every RENAMED project as a 404 — coverage."
            "classify_orphan() replaces it")


# ─── the menu prints them ─────────────────────────────────────────────────────

class TheScreenStatesItsScope(MenuHarness):

    def test_the_listing_scope_is_printed(self):
        self.listings[1] = paged(
            ["alpha/a"], note="ACTIVE PROJECTS ONLY — archived projects are "
                              "EXCLUDED from this count")
        _r, out = self.run_menu()
        self.assertIn("listing scope:", out)
        self.assertIn("archived projects are EXCLUDED", out,
                      "the count on screen excludes archived projects and "
                      "never said so")

    def test_identical_scopes_are_stated_once_not_once_per_group(self):
        note = "includes archived projects; includes subgroup projects"
        self.listings = {1: paged(["alpha/a"], note=note),
                         2: paged(["beta/b"], note=note)}
        _r, out = self.run_menu()
        self.assertEqual(1, out.count("listing scope:"))


class TheScreenNamesWhatItCouldNotCount(MenuHarness):

    def test_a_failed_listing_is_named_with_its_reason(self):
        self.listings[1] = api.ApiError(403, "Access denied", BASE)
        _r, out = self.run_menu()
        self.assertIn("did not complete", out)
        self.assertIn("403", out)
        self.assertIn("NOT in the totals above", out,
                      "the roll-up silently omitted a whole group")

    def test_the_unknown_icon_is_in_the_legend(self):
        _r, out = self.run_menu()
        self.assertIn(G.UNKNOWN_ICON, out)
        self.assertIn("listing failed", out)

    def test_a_failed_listing_is_not_rendered_as_empty(self):
        # "empty" is an ANSWER. A failed listing has a zero denominator for
        # the same reason a genuinely empty group does, and one word for both
        # is exactly the confident-wrong report this wave removes.
        self.listings[1] = api.ApiError(403, "Access denied", BASE)
        _r, out = self.run_menu()
        line = [ln for ln in out.splitlines() if "alpha" in ln][0]
        self.assertNotIn("empty", line)
        self.assertIn("unknown", line)

    def test_clone_all_still_includes_a_group_whose_listing_failed(self):
        # Excluding it is how "clone everything" quietly became "clone the
        # groups that answered". Clone can only ADD.
        self.listings[1] = api.ApiError(500, "boom", BASE)
        result, _out = self.run_menu("ca")
        picked = {g["full_path"] for g in result["_groups"]}
        self.assertIn("alpha", picked)

    def test_no_failure_means_no_noise(self):
        _r, out = self.run_menu()
        self.assertNotIn("did not complete", out)


class QuietFailuresStillReachTheScreen(MenuHarness):

    def test_a_failure_recorded_with_quiet_true_is_disclosed(self):
        def noisy(c, g):
            if g["id"] == 2:
                api.record_failure(429, "Rate limited — wait a few seconds",
                                   BASE + "/api/v4/groups/2/projects",
                                   quiet=True)
            return paged(["%s/x" % g["full_path"]])
        cov.remote_projects = noisy

        _r, out = self.run_menu()
        self.assertIn("API failure(s) were recorded", out,
                      "`quiet` may suppress the console line, never the "
                      "knowledge of the failure — that pair IS the founding "
                      "defect")
        self.assertIn("Rate limited", out)

    def test_failures_from_before_this_screen_are_not_re_reported(self):
        api.record_failure(500, "an older, unrelated failure", BASE, quiet=True)
        _r, out = self.run_menu()
        self.assertNotIn("older, unrelated", out,
                         "the ledger is a snapshot diff, not a dump; the menu "
                         "must not re-blame a failure it did not cause")

    def test_a_clean_screen_says_nothing_about_failures(self):
        _r, out = self.run_menu()
        self.assertNotIn("API failure(s)", out)


class TheInstanceListingCanBeUnknownToo(MenuHarness):

    def test_an_unknown_project_list_is_disclosed_and_stops_the_sweep(self):
        setup_mod._remote_projects = lambda c: (None, "HTTP 502 from /projects")
        called = []
        cov.instance_orphans = lambda *a, **k: called.append(1) or []

        _r, out = self.run_menu()
        self.assertIn("instance project list UNKNOWN", out)
        self.assertIn("502", out)
        self.assertIn("orphan sweep was SKIPPED", out)
        self.assertEqual([], called,
                         "comparing clones against an UNKNOWN project list "
                         "labels every healthy clone an orphan")

    def test_an_exception_from_the_instance_listing_never_takes_the_menu_down(self):
        def boom(c):
            raise api.ApiError(500, "nope", BASE)
        setup_mod._remote_projects = boom
        _r, out = self.run_menu()
        self.assertIn("instance project list UNKNOWN", out)
        self.assertIn("ApiError", out)

    def test_the_flat_listing_is_fetched_exactly_once_per_screen(self):
        calls = []

        def counted(c):
            calls.append(1)
            return [proj("alpha/a"), proj("beta/b")], ""
        setup_mod._remote_projects = counted
        self.run_menu()
        self.assertEqual(1, len(calls),
                         "the coverage roll-up and the orphan sweep must read "
                         "ONE list, or the two blocks on screen describe two "
                         "different populations")


class SingleGroupScreenDisclosesToo(unittest.TestCase):
    """The per-group screen renders the same two facts."""

    def test_the_source_renders_note_and_error_for_the_chosen_group(self):
        tail = _src().split("# ─── Single group ", 1)[1]
        self.assertIn('gcov.get("note")', tail)
        self.assertIn('gcov.get("error")', tail)
        self.assertIn("coverage UNKNOWN", tail,
                      "a group whose listing failed must not be labelled "
                      "'not cloned' — that is a made-up answer with the same "
                      "shape as a real one")


if __name__ == "__main__":
    unittest.main()
