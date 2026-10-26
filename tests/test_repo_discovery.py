#!/usr/bin/env python3
# tests/test_repo_discovery.py — W3 hardening.
#
# pull._find_git_repos used to do `dirs[:] = []` the moment it saw a .git, so
# the walk stopped at the FIRST repository on each branch of the tree. On the
# real machine that hid dozens of repositories nested inside another repo's
# work tree: `pull` never synced them and, worse, `remotes`
# never audited them for credentials. An invisible repo cannot be scrubbed.
#
# The rewrite keeps descending while preserving the three properties that made
# the prune tempting: never walk into .git, never follow symlinks, never loop.
# All four are pinned here, on a real directory tree.

import os
import unittest

from _bootstrap import TempTreeTestCase

from pull import _find_git_repos


class RepoDiscoveryTests(TempTreeTestCase):

    def found(self, root=None):
        return set(os.path.realpath(p) for p in _find_git_repos(root or self.dest))

    def test_a_plain_repository_is_found(self):
        r = self.make_project_clone("initech/feed-ms")
        self.assertIn(os.path.realpath(r), self.found())

    def test_a_repository_nested_inside_another_repository_is_found(self):
        """The 36-repo blind spot: a whole second MasterGroups checked out
        inside the meta-repo work tree."""
        outer = self.make_project_clone("globex/meta-repo")
        inner_rel = "globex/meta-repo/MasterGroups/globex/hooli"
        inner = self.make_project_clone(inner_rel)
        got = self.found()
        self.assertIn(os.path.realpath(outer), got)
        self.assertIn(os.path.realpath(inner), got,
                      "a nested repository is still invisible to pull/remotes")

    def test_several_repositories_nested_at_different_depths_are_all_found(self):
        paths = ["a/one", "a/one/deep/two", "a/one/deep/two/deeper/three"]
        for p in paths:
            self.make_project_clone(p)
        got = self.found()
        for p in paths:
            self.assertIn(os.path.realpath(os.path.join(self.dest, *p.split("/"))),
                          got, p)

    def test_a_dot_git_file_counts_as_a_repository(self):
        """Submodule work trees and `git worktree` checkouts record .git as a
        FILE; they carry remotes — and therefore possible credentials — exactly
        like a normal clone."""
        path = os.path.join(self.dest, "sub", "module")
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, ".git"), "w") as fh:
            fh.write("gitdir: ../../.git/modules/module\n")
        self.assertIn(os.path.realpath(path), self.found())

    def test_a_plain_directory_is_not_reported_as_a_repository(self):
        os.makedirs(os.path.join(self.dest, "initech", "not-a-repo"), exist_ok=True)
        self.assertEqual(self.found(), set())

    def test_the_walk_never_descends_into_dot_git(self):
        """.git/objects is most of the file count; walking it is what makes a
        full-descent walk unaffordable. Also, no path inside .git may ever be
        reported as a work tree."""
        self.make_project_clone("initech/feed-ms")
        for p in _find_git_repos(self.dest):
            self.assertNotIn(os.sep + ".git" + os.sep, p + os.sep)

    def test_a_symlink_cycle_terminates(self):
        repo = self.make_project_clone("a/one")
        try:
            os.symlink(self.dest, os.path.join(repo, "loop"),
                       target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks not available on this platform/filesystem")
        got = self.found()                       # must return, not hang
        self.assertIn(os.path.realpath(repo), got)

    def test_a_symlinked_repository_is_not_double_counted(self):
        repo = self.make_project_clone("a/one")
        try:
            os.symlink(repo, os.path.join(self.dest, "alias"),
                       target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks not available on this platform/filesystem")
        self.assertEqual(len(_find_git_repos(self.dest)), 1)

    def test_the_result_is_sorted_and_free_of_duplicates(self):
        for p in ("a/one", "b/two", "a/one/deep/three"):
            self.make_project_clone(p)
        got = _find_git_repos(self.dest)
        self.assertEqual(got, sorted(got))
        self.assertEqual(len(got), len(set(got)))

    def test_a_missing_root_returns_an_empty_list_rather_than_raising(self):
        self.assertEqual(
            _find_git_repos(os.path.join(self.tmp, "does-not-exist")), [])

    def test_discovery_agrees_with_an_independent_walk(self):
        """Referee: count .git entries with a dead-simple walk of our own and
        require the tool to match. This is the assertion that actually caught
        the 564-vs-600 gap."""
        for p in ("a/one", "a/one/nested/two", "b/three",
                  "b/three/deep/four/five"):
            self.make_project_clone(p)
        independent = set()
        for root, dirs, files in os.walk(self.dest):
            if ".git" in dirs or ".git" in files:
                independent.add(os.path.realpath(root))
        self.assertEqual(self.found(), independent)


if __name__ == "__main__":
    unittest.main(verbosity=2)
