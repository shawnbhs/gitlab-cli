# tests/test_coverage_status.py
#
# THE core regression. RC-2: gitlab_cli.group_status called a group "cloned"
# because its FOLDER existed. globex had 1 of 22 projects on disk, read as
# ✅, was therefore excluded from "Clone All", and pick_action offered only
# Sync — so 51 projects could never be fetched and the tool reported success.
#
# Everything here is offline: the remote project list is passed in explicitly,
# the local side is real git repositories in a temp dir.

import unittest

from _bootstrap import TempTreeTestCase, fake_cfg, fake_projects
import coverage as cov


class StatusIconTests(unittest.TestCase):
    """The pure decision table. If this table drifts, RC-2 comes back."""

    def test_one_of_twentytwo_is_partial_never_complete(self):
        self.assertEqual(cov.status_icon(1, 22), "◐")

    def test_twentyone_of_twentytwo_is_partial(self):
        self.assertEqual(cov.status_icon(21, 22), "◐")

    def test_zero_of_n_is_nothing(self):
        self.assertEqual(cov.status_icon(0, 22), "❌")

    def test_n_of_n_is_complete(self):
        self.assertEqual(cov.status_icon(22, 22), "✅")

    def test_empty_remote_group_is_complete_not_missing(self):
        # agent-knowledge-deletion_scheduled-114 has 0 projects on the remote.
        # Reporting ❌ would put it in Clone All forever and never clear.
        self.assertEqual(cov.status_icon(0, 0), "✅")

    def test_more_on_disk_than_remote_is_still_complete(self):
        # stale/renamed leftovers must not make a complete group read partial
        self.assertEqual(cov.status_icon(23, 22), "✅")


class GroupCoverageTests(TempTreeTestCase):
    """coverage.group_coverage over a real on-disk tree."""

    def _projects_22(self):
        return fake_projects(*[f"globex/sub{i}/p{i}" for i in range(1, 23)])

    def test_one_of_twentytwo_on_disk_reports_partial(self):
        projects = self._projects_22()
        self.make_project_clone("globex/sub1/p1")
        index = cov.scan(self.dest)
        cloned, total = cov.group_coverage(
            fake_cfg(), {"id": 35, "full_path": "globex"}, index, projects)
        self.assertEqual((cloned, total), (1, 22))
        self.assertEqual(cov.status_icon(cloned, total), "◐")

    def test_zero_on_disk_reports_nothing(self):
        projects = fake_projects(*[f"legacy_transfer/p{i}"
                                   for i in range(1, 30)])
        index = cov.scan(self.dest)
        cloned, total = cov.group_coverage(
            fake_cfg(), {"id": 1, "full_path": "legacy_transfer"},
            index, projects)
        self.assertEqual((cloned, total), (0, 29))
        self.assertEqual(cov.status_icon(cloned, total), "❌")

    def test_all_on_disk_reports_complete(self):
        projects = fake_projects("industry/a", "industry/b")
        for p in projects:
            self.make_project_clone(p["path_with_namespace"])
        index = cov.scan(self.dest)
        cloned, total = cov.group_coverage(
            fake_cfg(), {"id": 2, "full_path": "industry"}, index, projects)
        self.assertEqual((cloned, total), (2, 2))
        self.assertEqual(cov.status_icon(cloned, total), "✅")

    def test_empty_remote_group_reports_complete(self):
        index = cov.scan(self.dest)
        cloned, total = cov.group_coverage(
            fake_cfg(), {"id": 114, "full_path": "agent-knowledge"}, index, [])
        self.assertEqual((cloned, total), (0, 0))
        self.assertEqual(cov.status_icon(cloned, total), "✅")

    def test_coverage_is_layout_agnostic_drifted_clone_still_counts(self):
        # RC-3: old clones sit under MasterGroups/globex/... while config
        # points at MasterGroups/. Identity is the origin URL, not the path.
        projects = fake_projects("globex/mcps/x")
        self.make_repo("some/unrelated/place",
                       "http://git.example.net/globex/mcps/x.git")
        index = cov.scan(self.dest)
        cloned, total = cov.group_coverage(
            fake_cfg(), {"id": 35, "full_path": "globex"}, index, projects)
        self.assertEqual((cloned, total), (1, 1))

    def test_missing_projects_lists_exactly_the_uncloned_ones(self):
        projects = fake_projects("initech/a", "initech/b", "initech/c")
        self.make_project_clone("initech/b")
        index = cov.scan(self.dest)
        missing = cov.missing_projects(
            fake_cfg(), {"id": 3, "full_path": "initech"}, index, projects)
        self.assertEqual(sorted(p["path_with_namespace"] for p in missing),
                         ["initech/a", "initech/c"])

    def test_a_directory_that_is_not_a_repo_does_not_count_as_cloned(self):
        # the RC-2 shape: clone.py creates empty subgroup dirs eagerly
        import os
        projects = fake_projects("initech/a")
        os.makedirs(os.path.join(self.dest, "initech", "a"), exist_ok=True)
        index = cov.scan(self.dest)
        cloned, total = cov.group_coverage(
            fake_cfg(), {"id": 3, "full_path": "initech"}, index, projects)
        self.assertEqual((cloned, total), (0, 1))
        self.assertEqual(cov.status_icon(cloned, total), "❌")

    def test_repo_with_no_origin_does_not_count_as_any_project(self):
        projects = fake_projects("globex/agent")
        self.make_repo("globex/agent")          # orphan, no remote
        index = cov.scan(self.dest)
        cloned, _ = cov.group_coverage(
            fake_cfg(), {"id": 35, "full_path": "globex"}, index, projects)
        self.assertEqual(cloned, 0)

    def test_host_of_ignores_port_scheme_and_case(self):
        self.assertEqual(cov.host_of("http://Git.example.net"), "git.example.net")
        self.assertEqual(cov.host_of("https://git.example.net:8443/"), "git.example.net")

    def test_clone_of_a_different_host_does_not_satisfy_this_instance(self):
        projects = fake_projects("initech/a")
        self.make_repo("initech/a", "http://other.example.com/initech/a.git")
        index = cov.scan(self.dest)
        cloned, total = cov.group_coverage(
            fake_cfg(), {"id": 3, "full_path": "initech"}, index, projects)
        self.assertEqual((cloned, total), (0, 1))


if __name__ == "__main__":
    unittest.main()
