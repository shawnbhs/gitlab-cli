#!/usr/bin/env python3
# tests/test_lfs_retry_summary.py — Clone All LFS-retry reporting gap.
#
# A previous run left a REASON_LFS row (repo cloned, LFS contents missing).
# The next cmd_clone run announced "1 cloned with LFS files missing in the
# previous run" but then (a) never re-ran `git lfs pull` for that repo and
# (b) the final summary never named the repo nor said whether its LFS
# content was now complete. Contract pinned here:
#   * the run RETRIES LFS for each such repo (git lfs pull),
#   * the summary prints "LFS repaired: <group/repo>" or
#     "LFS still missing: <group/repo>" followed by the error_catalog fix.
#
# Offline: git.py's subprocess seam (_run) is replaced by a fake; GitLab API
# calls are stubbed on the clone module.

import contextlib
import io
import os
import unittest

from _bootstrap import TempTreeTestCase, fake_cfg, fake_projects

import clone as clone_mod
import git as gitmod
import paths

GROUP = "initech"
PWN = "initech/lfs-repo"


class FakeLfsGit:
    """git._run stand-in: a valid repo whose LFS files are pointers until a
    `git lfs pull` succeeds (pull_fails=False)."""

    def __init__(self, pull_fails):
        self.pull_fails = pull_fails
        self.complete = False
        self.calls = []

    def __call__(self, args, cwd=None, timeout=None, env_extra=None, **_kw):
        argv = [str(a) for a in args]
        self.calls.append(argv)
        if "rev-parse" in argv:
            return 0, ".git", ""
        if "lfs" in argv:
            if "ls-files" in argv:
                return 0, ("abc123 %s assets/model.bin"
                           % ("*" if self.complete else "-")), ""
            if "pull" in argv:
                if self.pull_fails:
                    return 2, "", ("Error downloading object: assets/model.bin: "
                                   "Smudge error: LFS: Client error: "
                                   "http://git.example.net/initech/lfs-repo.git"
                                   "/gitlab-lfs/objects/abc123")
                self.complete = True
                return 0, "", ""
            return 0, "git-lfs/3.4.1", ""
        if "status" in argv:
            return 0, "", ""
        return 0, "", ""

    def pulls(self):
        return [a for a in self.calls if "lfs" in a and "pull" in a]


class LfsRetrySummaryTests(TempTreeTestCase):

    MOD_STUBS = ("get_group", "get_projects", "get_subgroups",
                 "presence_index", "_lazy_known", "prune_leftovers",
                 "load_errors")
    GIT_STUBS = ("_run", "lfs_available", "_harden_repo_config", "log_error")

    def setUp(self):
        super().setUp()
        self._mod = {n: getattr(clone_mod, n) for n in self.MOD_STUBS}
        self._git = {n: getattr(gitmod, n) for n in self.GIT_STUBS}
        self.local = paths.target_for_clone(GROUP, self.dest, None)
        self.target = os.path.join(self.local, "lfs-repo")
        os.makedirs(os.path.join(self.target, ".git"), exist_ok=True)
        with open(os.path.join(self.target, ".gitattributes"), "w") as fh:
            fh.write("*.bin filter=lfs diff=lfs merge=lfs -text\n")
        prior = [{"project": "lfs-repo", "path": self.target,
                  "url": "http://git.example.net/initech/lfs-repo.git",
                  "reason": clone_mod.REASON_LFS,
                  "code": "lfs_missing_object",
                  "detail": "LFS: Client error"}]
        clone_mod.get_group = lambda u, h, p: {"id": 1, "path": GROUP,
                                               "full_path": GROUP}
        clone_mod.get_projects = lambda u, h, gid: fake_projects(PWN)
        clone_mod.get_subgroups = lambda u, h, gid: []
        clone_mod.presence_index = lambda dest, refresh=False: {}
        clone_mod._lazy_known = lambda *a, **k: None
        clone_mod.prune_leftovers = lambda *a, **k: None
        clone_mod.load_errors = lambda dest, gp: [dict(r) for r in prior]
        gitmod.lfs_available = lambda: True
        gitmod._harden_repo_config = lambda *a, **k: None
        gitmod.log_error = lambda *a, **k: None

    def tearDown(self):
        for n, v in self._mod.items():
            setattr(clone_mod, n, v)
        for n, v in self._git.items():
            setattr(gitmod, n, v)
        super().tearDown()

    def run_clone(self, pull_fails):
        self.fake = FakeLfsGit(pull_fails)
        gitmod._run = self.fake
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            clone_mod.cmd_clone(fake_cfg(), GROUP, self.dest)
        return buf.getvalue()

    def test_previous_lfs_repo_is_retried_with_lfs_pull(self):
        self.run_clone(pull_fails=False)
        self.assertTrue(self.fake.pulls(),
                        "repo flagged LFS-missing last run was never retried")

    def test_repaired_repo_is_named_in_summary(self):
        out = self.run_clone(pull_fails=False)
        self.assertIn(f"LFS repaired: {PWN}", out)
        self.assertNotIn(f"LFS still missing: {PWN}", out)

    def test_still_missing_repo_is_named_with_fix_hint(self):
        out = self.run_clone(pull_fails=True)
        self.assertTrue(self.fake.pulls())
        self.assertIn(f"LFS still missing: {PWN}", out)
        fix = clone_mod._fix_for("lfs_missing_object")
        if fix:
            tail = out.split(f"LFS still missing: {PWN}", 1)[1]
            self.assertIn(fix, tail)
        self.assertNotIn(f"LFS repaired: {PWN}", out)


if __name__ == "__main__":
    unittest.main()
