#!/usr/bin/env python3
# tests/test_error_log_keys.py — W3 hardening.
#
# Three fixed defects, none of them previously covered:
#
#  1. The per-group error log was named after the group's LAST path component,
#     so `initech/core` and `globex/x/core` shared `.core_errors.json` — and a
#     clean run of one group os.remove()d the other group's pending failures.
#  2. cmd_clone installed a SIGINT handler with no try/finally. Any raise
#     (paths.UnsafePath, anything inside _process_group) left that handler
#     installed over dead locals, so a later unrelated Ctrl-C ran
#     _save_errors(dest, group, []) — the branch that DELETES the log.
#  3. cmd_retry_failed used the same os.path.exists(".git") test as the resume
#     path, so it hammered a broken directory forever.
#
# Everything is offline: the group/project fetchers and clone_repo are stubbed
# in the clone module for the duration of each test and restored in tearDown.

import json
import os
import signal
import unittest

from _bootstrap import TempTreeTestCase, fake_cfg, fake_projects

import clone as clonemod
import paths


class _CloneStubs(TempTreeTestCase):
    """Base: stub clone.py's network surface, restore it afterwards."""

    def setUp(self):
        super().setUp()
        self._saved = {name: getattr(clonemod, name)
                       for name in ("get_group", "get_projects",
                                    "get_subgroups", "clone_repo")}
        self.cloned = []
        self.projects_by_group = {}
        self.clone_result = True
        clonemod.get_group = lambda url, h, gp: {
            "id": 1, "path": gp.split("/")[-1], "full_path": gp}
        clonemod.get_projects = lambda url, h, gid, *a, **k: \
            self.projects_by_group.get(gid, [])
        clonemod.get_subgroups = lambda url, h, gid, *a, **k: []
        clonemod.clone_repo = self._clone_repo
        clonemod._session_stats.update(
            {"cloned": 0, "pulled": 0, "failed": 0, "invalid": 0})

    def tearDown(self):
        for name, fn in self._saved.items():
            setattr(clonemod, name, fn)
        super().tearDown()

    def _clone_repo(self, token, url, target):
        self.cloned.append(target)
        if not self.clone_result:
            return False
        os.makedirs(os.path.join(target, ".git"), exist_ok=True)
        return True

    def logs_in_dest(self):
        return sorted(f for f in os.listdir(self.dest)
                      if f.endswith("_errors.json"))


class ErrorLogNamingTests(_CloneStubs):

    def test_two_groups_sharing_a_last_component_get_different_files(self):
        a = clonemod._error_log_path(self.dest, "initech/core")
        b = clonemod._error_log_path(self.dest, "globex/x/core")
        self.assertNotEqual(a, b)

    def test_a_top_level_group_keeps_its_historic_filename(self):
        """Back-compat: existing .industry_errors.json files must keep working."""
        self.assertEqual(
            os.path.basename(clonemod._error_log_path(self.dest, "industry")),
            ".industry_errors.json")

    def test_a_clean_run_of_one_group_does_not_delete_the_others_errors(self):
        """The data-loss shape: pending failures of a different group vanished."""
        clonemod._save_errors(self.dest, "globex/x/core",
                              [{"project": "core", "path": "/x", "url": "u"}])
        clonemod._save_errors(self.dest, "initech/core", [])   # clean run
        survived = clonemod.load_errors(self.dest, "globex/x/core")
        self.assertEqual(len(survived), 1)
        self.assertEqual(survived[0]["project"], "core")

    def test_both_groups_can_hold_pending_errors_at_the_same_time(self):
        clonemod._save_errors(self.dest, "initech/core",
                              [{"project": "a", "path": "/a", "url": "u"}])
        clonemod._save_errors(self.dest, "globex/x/core",
                              [{"project": "b", "path": "/b", "url": "u"}])
        self.assertEqual(len(self.logs_in_dest()), 2)
        self.assertEqual(clonemod.load_errors(self.dest, "initech/core")[0]["project"], "a")
        self.assertEqual(
            clonemod.load_errors(self.dest, "globex/x/core")[0]["project"], "b")

    def test_a_group_path_that_would_escape_dest_cannot_name_a_log_file(self):
        """A traversal group path must still produce ONE filename component
        inside dest, never a path that climbs out of it."""
        path = clonemod._error_log_path(self.dest, "../../etc")
        self.assertEqual(os.path.dirname(os.path.normpath(path)),
                         os.path.normpath(self.dest))
        self.assertNotIn(os.sep, os.path.basename(path))
        self.assertEqual(os.path.normpath(os.path.dirname(os.path.realpath(path))),
                         os.path.normpath(os.path.realpath(self.dest)))

    def test_an_own_log_is_removed_after_a_clean_run(self):
        clonemod._save_errors(self.dest, "initech/core",
                              [{"project": "a", "path": "/a", "url": "u"}])
        self.assertEqual(len(self.logs_in_dest()), 1)
        clonemod._save_errors(self.dest, "initech/core", [])
        self.assertEqual(self.logs_in_dest(), [])


class LegacyLogMigrationTests(_CloneStubs):
    """An old collision-prone file is adopted only when it provably belongs to
    this group — never guessed at, never deleted on a guess."""

    def write_legacy(self, last_component, recorded_group, project="p"):
        path = os.path.join(self.dest, f".{last_component}_errors.json")
        with open(path, "w") as fh:
            json.dump({"date": "2026-01-01", "group": recorded_group,
                       "errors": [{"project": project, "path": "/p",
                                   "url": "u"}]}, fh)
        return path

    def test_an_attributable_legacy_file_is_read(self):
        self.write_legacy("core", "initech/core", project="legacy")
        got = clonemod.load_errors(self.dest, "initech/core")
        self.assertEqual([e["project"] for e in got], ["legacy"])

    def test_a_foreign_legacy_file_is_never_read_as_ours(self):
        self.write_legacy("core", "somebody/else/core", project="theirs")
        self.assertEqual(clonemod.load_errors(self.dest, "initech/core"), [])

    def test_a_foreign_legacy_file_is_never_deleted_by_our_clean_run(self):
        path = self.write_legacy("core", "somebody/else/core")
        clonemod._save_errors(self.dest, "initech/core", [])
        self.assertTrue(os.path.isfile(path),
                        "a clean run deleted a log it could not attribute")

    def test_an_attributable_legacy_file_is_superseded_not_duplicated(self):
        legacy = self.write_legacy("core", "initech/core")
        clonemod._save_errors(self.dest, "initech/core",
                              [{"project": "new", "path": "/n", "url": "u"}])
        self.assertFalse(os.path.isfile(legacy))
        self.assertEqual(
            [e["project"] for e in clonemod.load_errors(self.dest, "initech/core")],
            ["new"])

    def test_a_corrupt_legacy_file_does_not_crash_the_run(self):
        with open(os.path.join(self.dest, ".core_errors.json"), "w") as fh:
            fh.write("{not json")
        self.assertEqual(clonemod.load_errors(self.dest, "initech/core"), [])


class SigintHandlerTests(_CloneStubs):
    """The leaked handler closes over dead locals; a later unrelated Ctrl-C
    then runs the empty-list branch of _save_errors and destroys a valid log."""

    def setUp(self):
        super().setUp()
        self.baseline = signal.getsignal(signal.SIGINT)

    def assertHandlerRestored(self):
        self.assertIs(signal.getsignal(signal.SIGINT), self.baseline,
                      "SIGINT handler leaked out of cmd_clone")

    def test_handler_is_restored_after_an_unsafe_path_raise(self):
        with self.assertRaises(paths.UnsafePath):
            clonemod.cmd_clone(fake_cfg(), "../escape", self.dest)
        self.assertHandlerRestored()

    def test_handler_is_restored_when_process_group_raises(self):
        def boom(*a, **k):
            raise RuntimeError("boom inside _process_group")
        clonemod.get_projects = boom
        with self.assertRaises(RuntimeError):
            clonemod.cmd_clone(fake_cfg(), "initech", self.dest)
        self.assertHandlerRestored()

    def test_handler_is_restored_when_the_group_is_not_found(self):
        clonemod.get_group = lambda *a, **k: None
        clonemod.cmd_clone(fake_cfg(), "nope", self.dest)
        self.assertHandlerRestored()

    def test_handler_is_restored_after_a_normal_run(self):
        self.projects_by_group[1] = fake_projects("initech/feed-ms")
        clonemod.cmd_clone(fake_cfg(), "initech", self.dest)
        self.assertHandlerRestored()

    def test_an_api_failure_mid_clone_does_not_leave_the_handler_installed(self):
        """api.get_subgroups now raises instead of returning [] — a new raise
        path through cmd_clone that did not exist before Wave 2."""
        import api

        def raise_api(*a, **k):
            raise api.ApiError(401, "Invalid or expired token on page 1")
        clonemod.get_subgroups = raise_api
        self.projects_by_group[1] = fake_projects("initech/feed-ms")
        with self.assertRaises(api.ApiError):
            clonemod.cmd_clone(fake_cfg(), "initech", self.dest)
        self.assertHandlerRestored()


class RetryFailedTests(_CloneStubs):
    """cmd_retry_failed shared the resume path's os.path.exists('.git') test."""

    def _log(self, entries):
        clonemod._save_errors(self.dest, "initech", entries)

    def test_a_broken_directory_is_not_cloned_over_and_stays_in_the_log(self):
        broken = self.make_broken_clone("initech/feed-ms")
        self._log([{"project": "feed-ms", "path": broken,
                    "url": "http://git.example.net/initech/feed-ms.git"}])
        clonemod.cmd_retry_failed(fake_cfg(), "initech", self.dest)
        self.assertEqual(self.cloned, [], "a broken tree was cloned over")
        self.assertTrue(os.path.isdir(os.path.join(broken, ".git")),
                        "the broken tree was modified")
        remaining = clonemod.load_errors(self.dest, "initech")
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0].get("reason"), clonemod.REASON_INVALID)

    def test_a_genuinely_missing_project_is_retried_and_clears_the_log(self):
        target = os.path.join(self.dest, "initech", "feed-ms")
        self._log([{"project": "feed-ms", "path": target,
                    "url": "http://git.example.net/initech/feed-ms.git"}])
        clonemod.cmd_retry_failed(fake_cfg(), "initech", self.dest)
        self.assertEqual(self.cloned, [target])
        self.assertEqual(clonemod.load_errors(self.dest, "initech"), [])

    def test_a_manually_fixed_project_is_recognised_and_not_recloned(self):
        good = self.make_project_clone("initech/feed-ms")
        self._log([{"project": "feed-ms", "path": good,
                    "url": "http://git.example.net/initech/feed-ms.git"}])
        clonemod.cmd_retry_failed(fake_cfg(), "initech", self.dest)
        self.assertEqual(self.cloned, [])
        self.assertEqual(clonemod.load_errors(self.dest, "initech"), [])

    def test_a_still_failing_retry_keeps_the_entry_for_the_next_run(self):
        self.clone_result = False
        target = os.path.join(self.dest, "initech", "feed-ms")
        self._log([{"project": "feed-ms", "path": target,
                    "url": "http://git.example.net/initech/feed-ms.git"}])
        clonemod.cmd_retry_failed(fake_cfg(), "initech", self.dest)
        self.assertEqual(len(clonemod.load_errors(self.dest, "initech")), 1)

    def test_retry_uses_the_same_log_key_as_the_clone_that_wrote_it(self):
        """Write side and retry side must agree on the filename for a SUBGROUP,
        which is where the two used to diverge."""
        target = os.path.join(self.dest, "globex", "x", "core", "p")
        clonemod._save_errors(self.dest, "globex/x/core",
                              [{"project": "p", "path": target, "url": "u"}])
        self.assertEqual(
            len(clonemod.load_errors(self.dest, "globex/x/core")), 1)
        clonemod.cmd_retry_failed(fake_cfg(), "globex/x/core", self.dest)
        self.assertEqual(self.cloned, [target])


if __name__ == "__main__":
    unittest.main(verbosity=2)
