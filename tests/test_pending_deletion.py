#!/usr/bin/env python3
# tests/test_pending_deletion.py — GitLab "delayed deletion" projects (D5-D7).
#
# GitLab's delayed deletion renames a project to
# `<name>-deletion_scheduled-<id>` (and can do the same to a whole group:
# `app-x-deletion_scheduled-98/app-x-chat`). Measured on a real group: about
# half "cloned", and ALL the "missing" projects were pending deletion. The tool:
#
#   D5  counted them as MISSING, listed the old clone as "renamed upstream —
#       re-point the remote", and Clone All would clone or move a clone INTO
#       the deletion-scheduled path;
#   D7  printed ~45 lines of orphan paths before the prompt.
#
# Pins CONTRACT.md "coverage.py": PENDING_DELETION_RE, is_pending_deletion(),
# the "pending_deletion" count with pending projects excluded from totals and
# from missing, ORPHAN_PENDING, and orphan_display_lines(detail=).
#
# Offline: probe_path is replaced by a stub; no network, no token.

import unittest

import _bootstrap  # noqa: F401  (path bootstrap + error-log guard)

import coverage as cov

HOST = "git.example.net"
CFG = {"url": "http://" + HOST, "headers": {}}


def _project(pwn, kind="group", **extra):
    p = {"id": abs(hash(pwn)) % 100000,
         "path": pwn.split("/")[-1],
         "path_with_namespace": pwn,
         "namespace": {"kind": kind},
         "http_url_to_repo": f"http://{HOST}/{pwn}.git"}
    p.update(extra)
    return p


class IsPendingDeletionTests(unittest.TestCase):

    def test_project_suffix_is_pending(self):
        self.assertTrue(
            cov.is_pending_deletion("team-y/infra-deletion_scheduled-149"))

    def test_group_suffix_is_pending(self):
        self.assertTrue(
            cov.is_pending_deletion("app-x-deletion_scheduled-98/app-x-chat"))

    def test_dict_with_marked_for_deletion_at_is_pending(self):
        self.assertTrue(cov.is_pending_deletion(
            {"path_with_namespace": "team-y/infra",
             "marked_for_deletion_at": "2026-10-20"}))

    def test_dict_with_marked_for_deletion_on_is_pending(self):
        self.assertTrue(cov.is_pending_deletion(
            {"path_with_namespace": "team-y/infra",
             "marked_for_deletion_on": "2026-10-20"}))

    def test_dict_with_pending_path_is_pending(self):
        self.assertTrue(cov.is_pending_deletion(
            {"path_with_namespace": "team-y/infra-deletion_scheduled-149"}))

    def test_live_path_is_not_pending(self):
        self.assertFalse(cov.is_pending_deletion("team-y/infra"))

    def test_word_without_hyphen_suffix_is_not_pending(self):
        self.assertFalse(cov.is_pending_deletion("x/deletion_scheduled-notes"))

    def test_suffix_without_digits_is_not_pending(self):
        self.assertFalse(
            cov.is_pending_deletion("x/infra-deletion_scheduled-later"))

    def test_live_dict_is_not_pending(self):
        self.assertFalse(cov.is_pending_deletion(
            {"path_with_namespace": "team-y/infra",
             "marked_for_deletion_at": None}))

    def test_empty_inputs_are_not_pending(self):
        self.assertFalse(cov.is_pending_deletion(""))
        self.assertFalse(cov.is_pending_deletion({}))

    def test_regex_matches_contract(self):
        self.assertTrue(cov.PENDING_DELETION_RE.search(
            "a-deletion_scheduled-1"))
        self.assertTrue(cov.PENDING_DELETION_RE.search(
            "a-deletion_scheduled-1/b"))
        self.assertFalse(cov.PENDING_DELETION_RE.search(
            "a-deletion_scheduled-1x"))


class CoverageExcludesPendingTests(unittest.TestCase):
    """team-y shape: live projects plus pending ones that are not on disk."""

    def setUp(self):
        self.projects = [
            _project("team-y/infra"),
            _project("team-y/api"),
            _project("team-y/old-deletion_scheduled-149"),
            _project("app-x-deletion_scheduled-98/app-x-chat"),
            _project("team-y/flagged", marked_for_deletion_at="2026-10-20"),
        ]
        # only team-y/infra is cloned
        self.index = {(HOST, "team-y/infra"): "/home/u/MasterGroups/team-y/infra"}

    def test_counts_report_pending_deletion(self):
        c = cov.coverage_counts(self.projects, self.index.keys(), HOST)
        self.assertIn("pending_deletion", c)
        self.assertIsInstance(c["pending_deletion"], int)
        self.assertEqual(c["pending_deletion"], 3)

    def test_counts_exclude_pending_from_totals(self):
        c = cov.coverage_counts(self.projects, self.index.keys(), HOST)
        self.assertEqual(c["group_total"], 2)
        self.assertEqual(c["all_total"], 2)
        self.assertEqual(c["group_present"], 1)

    def test_counts_exclude_pending_from_missing(self):
        c = cov.coverage_counts(self.projects, self.index.keys(), HOST)
        self.assertEqual(c["missing"], 1)

    def test_counts_still_reconcile(self):
        c = cov.coverage_counts(self.projects, self.index.keys(), HOST)
        self.assertTrue(cov.coverage_consistent(c))

    def test_no_pending_projects_reports_zero(self):
        c = cov.coverage_counts(self.projects[:2], self.index.keys(), HOST)
        self.assertEqual(c["pending_deletion"], 0)

    def test_group_coverage_total_excludes_pending(self):
        cloned, total = cov.group_coverage(CFG, {"id": 1}, self.index,
                                           projects=self.projects)
        self.assertEqual((cloned, total), (1, 2))

    def test_missing_projects_excludes_pending(self):
        missing = cov.missing_projects(CFG, {"id": 1}, self.index,
                                       projects=self.projects)
        self.assertEqual([p["path_with_namespace"] for p in missing],
                         ["team-y/api"])


class OrphanPendingTests(unittest.TestCase):

    def setUp(self):
        self._orig_probe = cov.probe_path

    def tearDown(self):
        cov.probe_path = self._orig_probe

    def _orphan(self, path):
        return {"host": HOST, "path": path,
                "dir": "/home/user/MasterGroups/" + path,
                "classification": cov.ORPHAN_UNKNOWN,
                "source": cov.SOURCE_LOCAL, "probe_status": None,
                "current_path": None, "reason": "not classified"}

    def test_constant_value(self):
        self.assertEqual(cov.ORPHAN_PENDING, "pending_deletion")

    def test_constant_is_a_known_classification_with_label_and_advice(self):
        self.assertIn(cov.ORPHAN_PENDING, cov.ORPHAN_CLASSIFICATIONS)
        self.assertTrue(cov.ORPHAN_LABELS.get(cov.ORPHAN_PENDING))
        self.assertTrue(cov.ORPHAN_ADVICE.get(cov.ORPHAN_PENDING))

    def test_rename_to_pending_path_is_orphan_pending(self):
        cov.probe_path = lambda cfg, path: {
            "status": 200, "verdict": "renamed",
            "current_path": "team-y/infra-deletion_scheduled-149"}
        o = cov.classify_orphan(CFG, self._orphan("team-y/infra"))
        self.assertEqual(o["classification"], cov.ORPHAN_PENDING)

    def test_rename_to_pending_group_is_orphan_pending(self):
        cov.probe_path = lambda cfg, path: {
            "status": 200, "verdict": "renamed",
            "current_path": "app-x-deletion_scheduled-98/app-x-chat"}
        o = cov.classify_orphan(CFG, self._orphan("app-x/app-x-chat"))
        self.assertEqual(o["classification"], cov.ORPHAN_PENDING)

    def test_ordinary_rename_stays_renamed(self):
        cov.probe_path = lambda cfg, path: {
            "status": 200, "verdict": "renamed",
            "current_path": "team-y/infrastructure"}
        o = cov.classify_orphan(CFG, self._orphan("team-y/infra"))
        self.assertEqual(o["classification"], cov.ORPHAN_RENAMED)

    def test_pending_advice_never_says_re_point_the_remote(self):
        # D5: re-pointing the remote at a deletion-scheduled path is wrong.
        # ("nothing to re-point" is fine; the instruction is what is wrong.)
        self.assertNotIn("re-point the remote",
                         cov.ORPHAN_ADVICE[cov.ORPHAN_PENDING].lower())


class OrphanDisplayTests(unittest.TestCase):
    """D7: the group screen was ~45 lines of paths before the prompt."""

    def setUp(self):
        self.orphans = []
        plan = ((cov.ORPHAN_PENDING, 12), (cov.ORPHAN_RENAMED, 10),
                (cov.ORPHAN_DELETED, 8))
        for cls, n in plan:
            for i in range(n):
                path = f"team-y/{cls}-old-{i}"
                self.orphans.append({
                    "host": HOST, "path": path,
                    "dir": "/home/user/code/MasterGroups/" + path,
                    "classification": cls, "source": cov.SOURCE_SERVER,
                    "probe_status": 200 if cls != cov.ORPHAN_DELETED else 404,
                    "current_path": (f"team-y/x-{i}-deletion_scheduled-{i}"
                                     if cls == cov.ORPHAN_PENDING else
                                     f"team-y/new-{i}"
                                     if cls == cov.ORPHAN_RENAMED else None),
                    "reason": "stub"})
        self.n_classes = 3

    def test_compact_has_no_home_paths(self):
        lines = cov.orphan_display_lines(self.orphans, detail=False)
        joined = "\n".join(lines)
        self.assertNotIn("/home/", joined)

    def test_compact_lists_no_orphan_paths(self):
        joined = "\n".join(cov.orphan_display_lines(self.orphans, detail=False))
        for o in self.orphans:
            self.assertNotIn(o["path"], joined)

    def test_compact_is_short(self):
        lines = cov.orphan_display_lines(self.orphans, detail=False)
        # headline + per class (label line, plus advice) + small constant
        self.assertLessEqual(len(lines), 1 + 2 * self.n_classes + 2,
                             "\n".join(lines))

    def test_compact_is_the_default(self):
        self.assertEqual(cov.orphan_display_lines(self.orphans),
                         cov.orphan_display_lines(self.orphans, detail=False))

    def test_compact_mentions_pending_class(self):
        joined = "\n".join(
            cov.orphan_display_lines(self.orphans, detail=False)).lower()
        self.assertIn("pending", joined)

    def test_detail_contains_paths(self):
        joined = "\n".join(cov.orphan_display_lines(self.orphans, detail=True))
        for o in self.orphans:
            self.assertIn(o["path"], joined)
        self.assertIn("/home/", joined)


if __name__ == "__main__":
    unittest.main()
