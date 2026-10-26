# tests/test_w10_menu_orphans.py — W10: orphan CLASSIFICATION reaches the menu.
#
# coverage.py grew a complete orphan vocabulary in W9-4:
#
#     local_orphans()          detection, purely LOCAL, everything UNKNOWN
#     classify_orphan()        one GET per orphan -> renamed / deleted / unknown
#     instance_orphans()       the instance-wide sweep the menu wants
#     orphan_summary()         counts, split by which SIDE answered
#     orphan_display_lines()   the single rendering, colour-free
#
# Not one of them had a caller. gitlab_cli.py kept its own `_orphans_of()`,
# which was wrong in two ways that matter:
#
#   1. It asked ONCE PER TOP-LEVEL GROUP. A clone whose entire top-level group
#      was dissolved server-side has no group left to iterate, so it was never
#      visited. That is why an audit found dozens of orphans on the Hub tree and
#      the menu could only ever reach a few.
#   2. It printed "404 on the server" for every one of them. GitLab follows its
#      own rename redirect, so a RENAMED project answers 200 at its old path —
#      the sentence was wrong for most of them, and it is the sentence a user
#      acts on with rm -rf.
#
# Offline: the server is a stub, the disk is a tempdir, no token.

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

# alpha is alive. beta was DISSOLVED server-side: it is not in the group list
# at all, which is exactly the shape the old per-group sweep could not see.
GROUPS = [{"id": 1, "path": "alpha", "full_path": "alpha",
           "visibility": "private"}]


def cfg():
    return {"url": BASE, "headers": {}, "min_access": 0,
            "master_dir": ".", "name": "test"}


def proj(pwn, kind="group"):
    return {"path_with_namespace": pwn,
            "namespace": {"kind": kind, "full_path": pwn.rsplit("/", 1)[0]}}


def _src(name="gitlab_cli.py"):
    with open(os.path.join(PROJECT_ROOT, name), encoding="utf-8") as fh:
        return fh.read()


class FakeResponse:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


class MenuWithOrphans(unittest.TestCase):
    """pick_group over a disk that holds orphans, with a stubbed server."""

    # on disk: one live clone, one renamed, one deleted, one whose whole
    # top-level group is gone.
    INDEX = {
        (HOST, "alpha/live"):    "/d/alpha/live",
        (HOST, "alpha/renamed"): "/d/alpha/renamed",
        (HOST, "alpha/deleted"): "/d/alpha/deleted",
        (HOST, "beta/orphan"):   "/d/beta/orphan",
        ("other.host", "alpha/live"): "/d/other/live",   # another instance
    }
    FLAT = [proj("alpha/live")]

    # what the server says about each stale path
    TABLE = {
        "alpha/renamed": FakeResponse(200, {"path_with_namespace":
                                            "alpha/renamed-new"}),
        "alpha/deleted": FakeResponse(404),
        "beta/orphan":   FakeResponse(404),
    }

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
        G.scan_dest = lambda dest: dict(self.INDEX)

        self._rp = cov.remote_projects
        self.addCleanup(setattr, cov, "remote_projects", self._rp)
        cov.remote_projects = lambda c, g: api.PagedResult(
            [proj("alpha/live")], note="includes archived projects")

        self._sr = setup_mod._remote_projects
        self.addCleanup(setattr, setup_mod, "_remote_projects", self._sr)
        setup_mod._remote_projects = lambda c: (list(self.FLAT), "")

        self.asked = []
        self._req = api.request
        self.addCleanup(setattr, api, "request", self._req)
        api.request = self._stub

    def _stub(self, url, headers, params=None, quiet=False, record=True):
        import urllib.parse
        path = urllib.parse.unquote(url.rsplit("/api/v4/projects/", 1)[-1])
        self.asked.append(path)
        return self.TABLE.get(path, FakeResponse(404))

    def run_menu(self, answer="q"):
        real_input, out = builtins.input, io.StringIO()
        if isinstance(answer, (list, tuple)):
            # scripted answers, then "q" forever so the menu always exits
            feed = iter(answer)
            builtins.input = lambda *a, **k: next(feed, "q")
        else:
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


class TheSweepIsInstanceWide(MenuWithOrphans):

    def test_a_clone_whose_whole_group_is_gone_is_still_found(self):
        _r, out = self.run_menu(["o", "", "q"])  # paths live behind the o Details key
        self.assertIn("beta/orphan", out,
                      "a clone whose top-level group was dissolved is "
                      "unreachable by a per-group sweep — this is the 29 vs 6 "
                      "gap")

    def test_every_orphan_is_found_and_no_live_clone_is_accused(self):
        _r, out = self.run_menu(["o", "", "q"])  # paths live behind the o Details key
        for path in ("alpha/renamed", "alpha/deleted", "beta/orphan"):
            self.assertIn(path, out)
        self.assertNotIn("alpha/live", out.split("orphaned clone")[-1])

    def test_another_instances_clones_are_not_orphans_here(self):
        _r, out = self.run_menu()
        self.assertNotIn("/d/other/live", out,
                         "a shared master_dir holds another instance's "
                         "clones; they match no project HERE and must not be "
                         "reported as orphans of this instance")

    def test_the_headline_counts_them(self):
        _r, out = self.run_menu()
        self.assertIn("3 orphaned clones", out)


class ClassificationIsRendered(MenuWithOrphans):

    def test_renamed_is_not_called_deleted(self):
        _r, out = self.run_menu(["o", "", "q"])  # paths live behind the o Details key
        self.assertIn("renamed upstream", out)
        self.assertIn("alpha/renamed-new", out,
                      "the server said this project is alive at a NEW path; "
                      "the old screen said '404 on the server' for it")

    def test_deleted_says_deleted_and_carries_the_archive_warning(self):
        _r, out = self.run_menu()
        self.assertIn("deleted upstream", out)
        self.assertIn("only remaining copy", out,
                      "a deleted orphan may be the last copy — the advice "
                      "must precede any rm -rf")

    def test_the_two_classes_are_counted_separately(self):
        _r, out = self.run_menu()
        self.assertIn("1 renamed upstream", out)
        self.assertIn("2 deleted upstream", out)

    def test_an_inconclusive_probe_is_UNKNOWN_never_deleted(self):
        # 403: a token that briefly lost access looks exactly like a deletion
        # from here. Calling it deleted is the unrecoverable mistake.
        self.TABLE = dict(self.TABLE, **{"alpha/deleted": FakeResponse(403)})
        _r, out = self.run_menu(["o", "", "q"])  # paths live behind the o Details key
        self.assertIn("unknown", out)
        self.assertIn("HTTP 403", out)
        self.assertIn("do not act on this", out)

    def test_a_transport_failure_is_UNKNOWN_too(self):
        def dead(url, headers, params=None, quiet=False):
            return None
        api.request = dead
        _r, out = self.run_menu()
        self.assertIn("unknown", out)
        self.assertNotIn("deleted upstream", out)

    def test_the_old_hardcoded_404_sentence_is_gone(self):
        # Comments are stripped: the history of the defect is documented in
        # the source on purpose; what must be gone is the printed string.
        code = "\n".join(ln for ln in _src().splitlines()
                         if not ln.lstrip().startswith("#"))
        self.assertFalse("404 on the server" in code,
                         "that sentence was wrong for every RENAMED orphan")

    def test_the_menu_renders_through_the_shared_renderer(self):
        # Three screens describing the same three classifications in three
        # different words is how the vocabulary drifts.
        self.assertTrue("orphan_display_lines" in _src())


class ZeroIsStatedNotSuppressed(MenuWithOrphans):

    INDEX = {(HOST, "alpha/live"): "/d/alpha/live"}

    def test_a_clean_tree_says_zero(self):
        _r, out = self.run_menu()
        self.assertIn("0 orphaned clones", out,
                      "a block that silently starts appearing is a block "
                      "nobody notices")
        self.assertIn("every work tree matches a live project", out)


class ProbeCostIsBounded(MenuWithOrphans):

    def test_only_the_stale_paths_are_probed(self):
        self.run_menu()
        self.assertEqual({"alpha/renamed", "alpha/deleted", "beta/orphan"},
                         set(self.asked))
        self.assertNotIn("alpha/live", self.asked,
                         "a live clone must cost no round-trip at all")

    def test_each_orphan_is_probed_once(self):
        self.run_menu()
        self.assertEqual(len(self.asked), len(set(self.asked)))


class TheSelectedGroupCarriesItsOwnOrphans(MenuWithOrphans):

    def test_selecting_a_group_narrows_the_instance_list(self):
        result, _out = self.run_menu("alpha")
        paths = {o["path"] for o in result["_orphans"]}
        self.assertEqual({"alpha/renamed", "alpha/deleted"}, paths,
                         "the group screen shows its own namespace; the "
                         "INSTANCE list is what it was narrowed from, so the "
                         "dissolved-group record still exists")

    def test_numeric_selection_carries_them_too(self):
        result, _out = self.run_menu("1")
        self.assertEqual(2, len(result["_orphans"]))

    def test_narrowing_matches_on_a_path_boundary_not_a_prefix(self):
        # 'alpha' must not swallow 'alphabet/x'.
        orphans = [{"path": "alpha/x"}, {"path": "alphabet/y"},
                   {"path": "alpha"}]
        got = {o["path"] for o in G._orphans_under(orphans, "alpha")}
        self.assertEqual({"alpha/x", "alpha"}, got)


class NeverActs(MenuWithOrphans):

    def test_the_menu_only_reports(self):
        before = dict(self.INDEX)
        _r, out = self.run_menu()
        self.assertEqual(before, self.INDEX)
        for verb in ("Deleting", "Removing", "rm -rf ", "shutil.rmtree"):
            self.assertNotIn(verb, out)

    def test_no_orphan_path_is_ever_handed_to_a_destructive_call(self):
        tail = _src().split("Orphans: on disk", 1)[1].split("\ndef _cap_info",
                                                            1)[0]
        for forbidden in ("rmtree", "os.remove", "os.rename", "shutil.move"):
            self.assertNotIn(forbidden, tail)


class TheSweepNeverTakesTheMenuDown(MenuWithOrphans):

    def test_a_crashing_classifier_is_absorbed(self):
        def boom(*a, **k):
            raise RuntimeError("classifier exploded")
        self._io = cov.instance_orphans
        cov.instance_orphans = boom
        self.addCleanup(setattr, cov, "instance_orphans", self._io)

        _r, out = self.run_menu()
        self.assertIn("Groups:", out, "the group list must still draw")


if __name__ == "__main__":
    unittest.main()
