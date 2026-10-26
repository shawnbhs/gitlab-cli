# tests/test_resume_validity.py
#
# The BLOCKING Wave-1 finding. clone.py resumes with
# `os.path.exists(os.path.join(target, ".git"))`. `git clone` creates `.git/`
# FIRST and fills it during transfer, so a Ctrl-C, a VPN drop or the 120 s
# GIT_TIMEOUT leaves a directory that trips that check — and the project is
# skipped FOREVER on every later run while nothing reports it as missing.
# `.git` as a dangling worktree/submodule FILE passes the same check.
#
# Specified post-fix behaviour, which is what these tests assert:
#   * a directory whose `.git` is not a usable repository is NOT "already
#     cloned" — it must be re-attempted or recorded, never silently skipped;
#   * a `.git` file is not a successful clone either;
#   * a healthy repository IS skipped (resume must still work, or a rerun
#     re-clones every project).
#
# Fully offline: local git repos in a temp dir, clone_repo stubbed out.

import os
import unittest

from _bootstrap import TempTreeTestCase, fake_cfg, fake_projects, git
import clone as clone_mod


def _resolve_health_check():
    """The post-fix predicate, whatever the owner of clone.py named it."""
    for name in ("_is_healthy_repo", "is_healthy_repo", "_is_valid_repo",
                 "is_valid_repo", "_repo_ok", "_is_cloned"):
        fn = getattr(clone_mod, name, None)
        if callable(fn):
            return name, fn
    return None, None


class HealthPredicateTests(TempTreeTestCase):
    """clone.py must expose a validity check, not a bare os.path.exists."""

    def setUp(self):
        super().setUp()
        self.name, self.fn = _resolve_health_check()
        if self.fn is None:
            self.skipTest(
                "clone.py exposes no repo-health predicate yet "
                "(expected one of _is_healthy_repo/is_healthy_repo/_is_valid_repo); "
                "behaviour is covered by ProcessGroupResumeTests instead")

    def test_healthy_repo_is_healthy(self):
        p = self.make_project_clone("initech/a")
        self.assertTrue(self.fn(p))

    def test_interrupted_clone_is_not_healthy(self):
        p = self.make_broken_clone("initech/b")
        self.assertTrue(os.path.exists(os.path.join(p, ".git")),
                        "fixture must reproduce the trap: .git exists")
        self.assertFalse(self.fn(p),
                         "a partial clone was accepted as complete — it would "
                         "be skipped forever")

    def test_git_as_a_dangling_file_is_not_healthy(self):
        p = self.make_broken_clone("initech/c", git_is_file=True)
        self.assertTrue(os.path.exists(os.path.join(p, ".git")))
        self.assertFalse(self.fn(p))

    def test_plain_directory_is_not_healthy(self):
        p = os.path.join(self.dest, "initech", "d")
        os.makedirs(p, exist_ok=True)
        self.assertFalse(self.fn(p))

    def test_missing_directory_is_not_healthy(self):
        self.assertFalse(self.fn(os.path.join(self.dest, "nope")))

    def test_empty_repo_with_no_commits_is_still_healthy(self):
        p = self.make_repo("initech/e", origin="http://git.example.net/initech/e.git",
                           commit=False)
        self.assertTrue(self.fn(p), "a freshly-initialised repo is a valid repo")


class ProcessGroupResumeTests(TempTreeTestCase):
    """Behaviour, independent of how the predicate is named."""

    def setUp(self):
        super().setUp()
        self.attempted = []
        self._orig_projects = clone_mod.get_projects
        self._orig_subgroups = clone_mod.get_subgroups
        self._orig_clone = clone_mod.clone_repo
        clone_mod.get_subgroups = lambda url, headers, gid: []
        clone_mod.clone_repo = self._fake_clone

    def tearDown(self):
        clone_mod.get_projects = self._orig_projects
        clone_mod.get_subgroups = self._orig_subgroups
        clone_mod.clone_repo = self._orig_clone
        super().tearDown()

    def _fake_clone(self, token, url, target):
        self.attempted.append(target)
        os.makedirs(target, exist_ok=True)
        git("init", "-q", target)
        git("remote", "add", "origin", url, cwd=target)
        return True

    def _run_group(self, projects, local_path):
        clone_mod.get_projects = lambda url, headers, gid: projects
        clone_mod._session_stats.update({"cloned": 0, "failed": 0, "skipped": 0})
        return clone_mod._process_group(fake_cfg(), {"id": 1, "path": "initech"},
                                        local_path)

    def test_a_broken_clone_is_not_silently_skipped(self):
        projects = fake_projects("initech/broken")
        local = os.path.join(self.dest, "initech")
        self.make_broken_clone("initech/broken")
        errors = self._run_group(projects, local)
        target = os.path.join(local, "broken")
        self.assertTrue(
            self.attempted or errors,
            "a partial clone was skipped as 'done' and recorded as no error — "
            "that project is now invisible forever (clone.py resume bug)")
        if self.attempted:
            self.assertIn(target, self.attempted)

    def test_a_git_file_is_not_silently_skipped(self):
        projects = fake_projects("initech/wt")
        local = os.path.join(self.dest, "initech")
        self.make_broken_clone("initech/wt", git_is_file=True)
        errors = self._run_group(projects, local)
        self.assertTrue(self.attempted or errors,
                        "a .git FILE was accepted as a successful clone")

    def test_a_healthy_clone_is_skipped_so_resume_still_works(self):
        projects = fake_projects("initech/ok")
        local = os.path.join(self.dest, "initech")
        os.makedirs(local, exist_ok=True)
        p = os.path.join(local, "ok")
        os.makedirs(p, exist_ok=True)
        git("init", "-q", p)
        git("remote", "add", "origin", "http://git.example.net/initech/ok.git", cwd=p)
        errors = self._run_group(projects, local)
        self.assertEqual(self.attempted, [],
                         "a healthy repo was re-cloned — resume is broken")
        self.assertEqual(errors, [])

    def test_a_missing_project_is_cloned(self):
        projects = fake_projects("initech/new")
        local = os.path.join(self.dest, "initech")
        errors = self._run_group(projects, local)
        self.assertEqual(self.attempted, [os.path.join(local, "new")])
        self.assertEqual(errors, [])

    def test_a_broken_clone_is_never_counted_as_present_by_coverage(self):
        import coverage as cov
        self.make_broken_clone("initech/broken")
        index = cov.scan(self.dest)
        self.assertNotIn(("git.example.net", "initech/broken"), index)


if __name__ == "__main__":
    unittest.main()
