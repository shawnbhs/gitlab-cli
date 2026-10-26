#!/usr/bin/env python3
# tests/test_lfs_clone.py — LFS-safe clone (D1, D2).
#
# D1  `group-a/platform/lfs-repo` could never be cloned: `git clone`
#     succeeded but checkout died in the LFS smudge filter
#     (`LFS: Client error ... gitlab-lfs/objects/<oid>` -> `smudge filter lfs
#     failed`). The tool said "Clone failed" and the project stayed "missing"
#     forever, although the repository itself was complete and valid.
# D2  stderr was truncated HEAD-first, so the user saw
#     "Downloading ... (36 MB) Error downloading obj" instead of the cause.
#
# Pins CONTRACT.md "git.py": the clone runs with GIT_LFS_SKIP_SMUDGE=1, LFS
# content is fetched separately, and a failure there yields status
# git.S_LFS ("lfs_incomplete") — the repo exists, only LFS file contents are
# missing — with res["detail"] = error_catalog.summarize(stderr).
#
# Everything is stubbed at git.py's subprocess seam (_run), the same seam
# test_w6_9_large_repo_fallback.py uses: no network, no real git-lfs, and
# credential/print/log side effects are neutralised so nothing touches
# ~/.git-credentials or the live error log.

import os
import shutil
import tempfile
import unittest

import _bootstrap  # noqa: F401  (path bootstrap + error-log guard)

import git as gitmod

TOKEN = "glpat-FAKEFAKEFAKEFAKEFAKE000"
URL = "http://gitlab.example.com/group-a/platform/lfs-repo.git"
OID = "9f2c4e1b7a3d5f60812c9e4b7d1a3f5e6c8b0a2d4f6e8a0c2b4d6f8e0a2c4b6d"

LFS_PULL_STDERR = (
    "Downloading assets/model.bin (36 MB)\n"
    "Error downloading object: assets/model.bin (9f2c4e1): Smudge error: "
    "Error downloading assets/model.bin (" + OID + "): LFS: Client error: "
    "http://gitlab.example.com/group-a/platform/lfs-repo.git/gitlab-lfs/objects/"
    + OID + "\n"
    "\n"
    "Errors logged to '/home/user/lfs-repo/.git/lfs/logs/20261022.log'.\n"
    "Use `git lfs logs last` to view the log.\n"
    "error: failed to fetch some objects from "
    "'http://gitlab.example.com/group-a/platform/lfs-repo.git/info/lfs'"
)
NOT_A_GIT_COMMAND = ("git: 'lfs' is not a git command. See 'git --help'.")
LS_FILES_OUT = OID[:10] + " * assets/model.bin"


class FakeGit:
    """Stands in for git._run. Records argv + env_extra of every call.

    lfs_installed — whether `git lfs ...` exists at all
    lfs_repo      — whether the cloned tree tracks files with LFS
    pull_fails    — whether fetching LFS content fails with LFS_PULL_STDERR
    """

    def __init__(self, lfs_installed=True, lfs_repo=True, pull_fails=True):
        self.lfs_installed = lfs_installed
        self.lfs_repo = lfs_repo
        self.pull_fails = pull_fails
        self.calls = []          # list of (argv, env_extra)

    def __call__(self, args, cwd=None, timeout=None, env_extra=None, **_kw):
        argv = [str(a) for a in args]
        self.calls.append((argv, dict(env_extra or {})))
        if "clone" in argv:
            self._materialise(argv[-1])
            return 0, "", "Cloning into 'lfs-repo'..."
        if "lfs" in argv:
            if not self.lfs_installed:
                return 1, "", NOT_A_GIT_COMMAND
            if "version" in argv:
                return 0, "git-lfs/3.4.1 (GitHub; linux amd64; go 1.22)", ""
            if "ls-files" in argv:
                return 0, (LS_FILES_OUT if self.lfs_repo else ""), ""
            if any(v in argv for v in ("pull", "fetch", "checkout")):
                if self.pull_fails:
                    return 2, "", LFS_PULL_STDERR
                return 0, "", ""
            return 0, "", ""
        if "rev-list" in argv:
            return 0, "3", ""
        if "--is-shallow-repository" in argv:
            return 0, "false", ""
        return 0, "", ""

    def _materialise(self, target):
        # What a real successful clone leaves: a work tree with .git/, and
        # for an LFS repository a .gitattributes routing files to the filter.
        os.makedirs(os.path.join(target, ".git"), exist_ok=True)
        with open(os.path.join(target, ".git", "HEAD"), "w") as fh:
            fh.write("ref: refs/heads/main\n")
        if self.lfs_repo:
            with open(os.path.join(target, ".gitattributes"), "w") as fh:
                fh.write("*.bin filter=lfs diff=lfs merge=lfs -text\n")

    def clone_calls(self):
        return [(a, e) for a, e in self.calls if "clone" in a]


class LfsCloneBase(unittest.TestCase):

    STUBBED = ("print_ok", "print_warn", "print_error", "log_error",
               "save_credential", "credential_known", "remote_has_userinfo",
               "_harden_repo_config", "_run", "lfs_available")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="t14_lfs_")
        self.target = os.path.join(self.tmp, "group-a", "platform", "lfs-repo")
        self._saved = {n: getattr(gitmod, n) for n in self.STUBBED
                       if hasattr(gitmod, n)}
        gitmod.print_ok = lambda *a, **k: None
        gitmod.print_warn = lambda *a, **k: None
        gitmod.print_error = lambda *a, **k: None
        self.logged = []
        gitmod.log_error = lambda *a, **k: self.logged.append(a)
        gitmod.save_credential = lambda *a, **k: True
        gitmod.credential_known = lambda *a, **k: True
        gitmod.remote_has_userinfo = lambda *a, **k: False
        gitmod._harden_repo_config = lambda *a, **k: None

    def tearDown(self):
        for name in self.STUBBED:
            if name in self._saved:
                setattr(gitmod, name, self._saved[name])
            elif hasattr(gitmod, name):
                delattr(gitmod, name)
        assert self.tmp.startswith(tempfile.gettempdir())
        shutil.rmtree(self.tmp, ignore_errors=True)

    def clone(self, **fake_kw):
        self.fake = FakeGit(**fake_kw)
        gitmod._run = self.fake
        gitmod.lfs_available = lambda: self.fake.lfs_installed
        return gitmod.clone_repo_detailed(TOKEN, URL, self.target)

    def assertTokenNeverInArgv(self):
        for argv, _env in self.fake.calls:
            for a in argv:
                self.assertNotIn(TOKEN, a, f"token leaked into argv: {argv}")


class ContractNamesTests(unittest.TestCase):

    def test_s_lfs_constant(self):
        self.assertEqual(gitmod.S_LFS, "lfs_incomplete")

    def test_lfs_available_is_callable_returning_bool(self):
        self.assertTrue(callable(gitmod.lfs_available))


class LfsPullFailureTests(LfsCloneBase):

    def test_lfs_pull_failure_is_s_lfs_not_error(self):
        res = self.clone(lfs_installed=True, lfs_repo=True, pull_fails=True)
        self.assertEqual(res["status"], gitmod.S_LFS, res)

    def test_detail_carries_the_lfs_cause_not_the_download_head(self):
        res = self.clone(lfs_installed=True, lfs_repo=True, pull_fails=True)
        detail = res.get("detail") or ""
        self.assertIn("lfs", detail.lower(), detail)
        self.assertNotIn("36 MB", detail)
        self.assertFalse(detail.startswith("Downloading"), detail)

    def test_clone_runs_with_smudge_skipped(self):
        self.clone(lfs_installed=True, lfs_repo=True, pull_fails=True)
        clones = self.fake.clone_calls()
        self.assertTrue(clones, "no git clone was issued")
        for argv, env in clones:
            self.assertEqual(env.get("GIT_LFS_SKIP_SMUDGE"), "1",
                             f"clone without GIT_LFS_SKIP_SMUDGE=1: {argv}")

    def test_lfs_content_is_fetched_after_the_clone(self):
        self.clone(lfs_installed=True, lfs_repo=True, pull_fails=True)
        lfs_fetch = [a for a, _ in self.fake.calls if "lfs" in a
                     and any(v in a for v in ("pull", "fetch", "checkout"))]
        self.assertTrue(lfs_fetch, "LFS content was never requested")

    def test_repository_is_kept(self):
        # S_LFS means the repo exists and is valid; it must not be deleted.
        self.clone(lfs_installed=True, lfs_repo=True, pull_fails=True)
        self.assertTrue(os.path.isdir(os.path.join(self.target, ".git")))

    def test_token_never_in_argv(self):
        self.clone(lfs_installed=True, lfs_repo=True, pull_fails=True)
        self.assertTokenNeverInArgv()


class LfsNotInstalledTests(LfsCloneBase):

    def test_lfs_repo_without_git_lfs_is_s_lfs_not_installed(self):
        res = self.clone(lfs_installed=False, lfs_repo=True)
        self.assertEqual(res["status"], gitmod.S_LFS, res)
        self.assertIn("not installed", (res.get("detail") or "").lower(), res)

    def test_token_never_in_argv(self):
        self.clone(lfs_installed=False, lfs_repo=True)
        self.assertTokenNeverInArgv()


class NonLfsRepoTests(LfsCloneBase):

    def test_plain_repo_with_lfs_installed_is_ok(self):
        res = self.clone(lfs_installed=True, lfs_repo=False, pull_fails=True)
        self.assertEqual(res["status"], "ok", res)

    def test_plain_repo_without_git_lfs_is_ok(self):
        res = self.clone(lfs_installed=False, lfs_repo=False)
        self.assertEqual(res["status"], "ok", res)

    def test_successful_lfs_pull_is_ok(self):
        res = self.clone(lfs_installed=True, lfs_repo=True, pull_fails=False)
        self.assertEqual(res["status"], "ok", res)

    def test_token_never_in_argv(self):
        self.clone(lfs_installed=True, lfs_repo=False)
        self.assertTokenNeverInArgv()


if __name__ == "__main__":
    unittest.main()
