#!/usr/bin/env python3
# tests/test_w6_1_coverage_walker.py — W6-1.
#
# THE DEFECT. Three tree walkers existed and disagreed:
#
#   pull._find_git_repos      exhaustive, .git-as-file, cycle-guarded  -> 783
#   coverage.find_repos       pruned at the first .git, dir-only       -> 747
#   log._find_git_repos       exhaustive, no cycle guard              -> 783
#
# coverage.find_repos feeds coverage.scan(), and scan() is what the
# interactive menu's status icons, the "Clone All" selection and the action
# gates are computed from. So the menu was STRUCTURALLY BLIND to the 36 real
# clones nested inside another checkout: a project genuinely on disk inside a
# nested tree could never read as present. Status computed one way, action
# taken another — the recurring defect class in this tool.
#
# These tests FAIL against the pre-fix coverage.py and PASS after.

import os
import unittest

from _bootstrap import TempTreeTestCase, BASE, fake_cfg, fake_projects

import coverage as cov
from pull import _find_git_repos as exhaustive_walk


def real(paths):
    return set(os.path.realpath(p) for p in paths)


class NestedRepoDiscovery(TempTreeTestCase):
    """coverage must see every work tree, not just the topmost one."""

    def test_a_repository_nested_inside_another_is_found(self):
        """The 36-repo blind spot, in miniature: a second MasterGroups
        checked out inside another clone's work tree."""
        outer = self.make_project_clone("globex/meta-repo")
        inner = self.make_project_clone(
            "globex/meta-repo/MasterGroups/globex/hooli")
        got = real(cov.find_repos(self.dest))
        self.assertIn(os.path.realpath(outer), got)
        self.assertIn(os.path.realpath(inner), got,
                      "coverage is still blind to a nested clone — the menu "
                      "can never show it as present")

    def test_repositories_nested_at_several_depths_are_all_found(self):
        rels = ["a/one", "a/one/deep/two", "a/one/deep/two/deeper/three"]
        for r in rels:
            self.make_project_clone(r)
        got = real(cov.find_repos(self.dest))
        for r in rels:
            self.assertIn(
                os.path.realpath(os.path.join(self.dest, *r.split("/"))),
                got, f"{r} invisible to coverage")

    def test_dot_git_as_a_FILE_counts_as_a_work_tree(self):
        """A submodule or `git worktree` checkout records .git as a file
        holding a `gitdir:` pointer. It has an origin like any clone."""
        path = os.path.join(self.dest, "sub", "module")
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, ".git"), "w") as fh:
            fh.write("gitdir: ../../.git/modules/module\n")
        self.assertIn(os.path.realpath(path),
                      real(cov.find_repos(self.dest)),
                      "coverage ignores a .git FILE, so a submodule or "
                      "linked worktree can never read as present")

    def test_dot_git_file_inside_an_existing_clone_is_found(self):
        """Both defects at once: a .git-file work tree nested in a clone."""
        self.make_project_clone("grp/outer")
        path = os.path.join(self.dest, "grp", "outer", "vendor", "libx")
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, ".git"), "w") as fh:
            fh.write("gitdir: ../../.git/modules/libx\n")
        self.assertIn(os.path.realpath(path),
                      real(cov.find_repos(self.dest)))


class WalkerParity(TempTreeTestCase):
    """coverage.find_repos and pull._find_git_repos must not disagree.

    They are the two halves of the status/action seam: coverage decides what
    the icon says, pull decides what the action touches. Any divergence is a
    reintroduction of the same bug class.
    """

    def build(self):
        self.make_project_clone("initech/feed-ms")
        self.make_project_clone("initech/order-ms")
        self.make_project_clone("globex/meta-repo")
        self.make_project_clone(
            "globex/meta-repo/MasterGroups/globex/hooli/hooli-front")
        self.make_project_clone(
            "globex/meta-repo/MasterGroups/globex/initech/cart-ms")
        wt = os.path.join(self.dest, "globex", "meta-repo", "wt")
        os.makedirs(wt, exist_ok=True)
        with open(os.path.join(wt, ".git"), "w") as fh:
            fh.write("gitdir: ../.git/worktrees/wt\n")

    def test_coverage_and_pull_find_the_same_set(self):
        self.build()
        self.assertEqual(real(cov.find_repos(self.dest)),
                         real(exhaustive_walk(self.dest)),
                         "the status walker and the action walker disagree")

    def test_coverage_finds_every_work_tree(self):
        self.build()
        self.assertEqual(len(cov.find_repos(self.dest)), 6)


class SymlinkCycleGuard(TempTreeTestCase):
    """An unguarded walk on this machine can loop forever."""

    def test_a_symlink_cycle_terminates(self):
        base = self.make_project_clone("grp/proj")
        loop = os.path.join(base, "loop")
        try:
            os.symlink(base, loop)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        got = cov.find_repos(self.dest)          # must not hang
        self.assertIn(os.path.realpath(base), real(got))
        self.assertEqual(len(got), 1,
                         "the symlinked copy was counted as a second repo")

    def test_a_symlink_to_an_ancestor_directory_terminates(self):
        self.make_project_clone("a/one")
        self.make_project_clone("a/two")
        link = os.path.join(self.dest, "a", "one", "up")
        try:
            os.symlink(os.path.join(self.dest, "a"), link)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        got = real(cov.find_repos(self.dest))    # must not hang
        self.assertEqual(len(got), 2)


class PresenceThroughScan(TempTreeTestCase):
    """The seam that actually mattered: the menu's index."""

    def test_a_nested_clone_is_present_in_the_scan_index(self):
        """scan() is what the icons, Clone All and the action gates read.
        A project only present inside a nested tree used to be invisible to
        every one of them."""
        self.make_project_clone("globex/meta-repo")
        self.make_repo(
            "globex/meta-repo/MasterGroups/globex/only-here",
            origin=f"{BASE}/globex/only-here.git")
        index = cov.scan(self.dest)
        self.assertIn(("git.example.net", "globex/only-here"), index,
                      "a clone that exists only inside a nested tree still "
                      "reads as missing to the menu")

    def test_group_coverage_counts_a_nested_only_project(self):
        self.make_project_clone("globex/meta-repo")
        self.make_repo(
            "globex/meta-repo/MasterGroups/globex/only-here",
            origin=f"{BASE}/globex/only-here.git")
        projects = fake_projects("globex/meta-repo",
                                 "globex/only-here")
        cloned, total = cov.group_coverage(
            fake_cfg(), {"id": 1}, cov.scan(self.dest), projects)
        self.assertEqual((cloned, total), (2, 2),
                         "a fully-cloned group reads as partial because one "
                         "of its projects lives inside a nested tree")

    def test_status_icon_is_complete_for_a_nested_only_project(self):
        self.make_project_clone("globex/meta-repo")
        self.make_repo(
            "globex/meta-repo/MasterGroups/globex/only-here",
            origin=f"{BASE}/globex/only-here.git")
        c, t = cov.group_coverage(
            fake_cfg(), {"id": 1}, cov.scan(self.dest),
            fake_projects("globex/meta-repo", "globex/only-here"))
        self.assertEqual(cov.status_icon(c, t), "✅")

    def test_a_nested_only_project_is_not_reported_missing(self):
        self.make_project_clone("globex/meta-repo")
        self.make_repo(
            "globex/meta-repo/MasterGroups/globex/only-here",
            origin=f"{BASE}/globex/only-here.git")
        missing = cov.missing_projects(
            fake_cfg(), {"id": 1}, cov.scan(self.dest),
            fake_projects("globex/meta-repo", "globex/only-here"))
        self.assertEqual([p["path_with_namespace"] for p in missing], [],
                         "Clone All would clone a second copy of a project "
                         "that is already on disk")


class PrunedModeStillAvailable(TempTreeTestCase):
    """The old semantics stay reachable, explicitly, for the one caller whose
    question really is 'which are the canonical clones?' (duplicate/layout
    reporting in doctor)."""

    def test_nested_false_prunes_like_the_old_walker(self):
        self.make_project_clone("grp/outer")
        self.make_project_clone("grp/outer/vendor/inner")
        self.assertEqual(len(cov.find_repos(self.dest, nested=False)), 1)
        self.assertEqual(len(cov.find_repos(self.dest)), 2)

    def test_find_repos_pruned_is_the_named_entry_point(self):
        self.make_project_clone("grp/outer")
        self.make_project_clone("grp/outer/vendor/inner")
        self.assertEqual(cov.find_repos_pruned(self.dest),
                         cov.find_repos(self.dest, nested=False))

    def test_default_is_exhaustive(self):
        """Regression guard: the DEFAULT must never go back to pruning, or
        the menu goes blind again."""
        self.make_project_clone("grp/outer")
        self.make_project_clone("grp/outer/vendor/inner")
        self.assertEqual(len(cov.find_repos(self.dest)),
                         len(exhaustive_walk(self.dest)))


class WalkerInvariants(TempTreeTestCase):
    """Properties the prune was protecting, which must survive de-pruning."""

    def test_the_dot_git_directory_is_never_descended_into(self):
        base = self.make_project_clone("grp/proj")
        decoy = os.path.join(base, ".git", "modules", "x")
        os.makedirs(os.path.join(decoy, ".git"), exist_ok=True)
        got = real(cov.find_repos(self.dest))
        self.assertNotIn(os.path.realpath(decoy), got,
                         "the walk descended into .git")
        self.assertEqual(len(got), 1)

    def test_a_missing_root_returns_empty(self):
        self.assertEqual(cov.find_repos(os.path.join(self.dest, "nope")), [])
        self.assertEqual(
            cov.find_repos(os.path.join(self.dest, "nope"), nested=False), [])

    def test_result_is_sorted_and_stable(self):
        for r in ["b/two", "a/one", "c/three"]:
            self.make_project_clone(r)
        got = cov.find_repos(self.dest)
        self.assertEqual(got, sorted(got))
        self.assertEqual(got, cov.find_repos(self.dest))


if __name__ == "__main__":
    unittest.main(verbosity=2)
