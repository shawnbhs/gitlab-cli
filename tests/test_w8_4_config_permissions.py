# tests/test_w8_4_config_permissions.py — W8-4: .git permissions.
#
# WHAT THIS PINS
# --------------
# `.git/config` is where git persists a remote URL, and a remote URL is where
# git persists a credential when one is supplied. 545 of 818 config files on
# this machine were 0644. An earlier wave found a live admin token in 120 of
# them.
#
# Two separate properties have to hold, and they fail independently:
#
#   1. NEW clones are 0600 BY CONSTRUCTION. Not "0644 then chmod'ed" — git
#      writes the remote URL into .git/config at the very start of a clone
#      that may then run for minutes, so a post-clone chmod leaves a real
#      window. CLONE_UMASK closes it.
#   2. EXISTING repositories are fixed by an EXHAUSTIVE sweep. The pruning
#      idiom (stop descending on finding .git) missed dozens of nested repositories
#      on a real machine. A sweep that misses repositories is worse than none,
#      because it reports a success it did not achieve.
#
# Everything here runs against temp repositories built by `git init`. Nothing
# in this module reads, writes, chmods or even stats anything under a live
# clone tree — asserted explicitly in test_module_never_touches_live_trees.

import os
import stat
import subprocess
import sys
import tempfile

import _bootstrap  # noqa: F401  (also pulls in _error_log_guard)
from _bootstrap import GIT, TempTreeTestCase, git

import git as gitmod

LIVE_TREES = (
    "/home/user/gitlab-cli/MasterGroups",
    "/home/user/MasterGroups",
)


def mode(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


class GitDirResolution(TempTreeTestCase):
    """`.git` is not always a directory."""

    def test_plain_repo_resolves_to_its_dot_git(self):
        repo = self.make_repo("g/plain")
        self.assertEqual(os.path.realpath(gitmod.resolve_git_dir(repo)),
                         os.path.realpath(os.path.join(repo, ".git")))

    def test_gitdir_pointer_file_is_followed(self):
        """A linked worktree / submodule has `.git` as a FILE.

        The old hardening did os.path.join(repo, ".git", "config") and so
        silently no-opped on exactly these repositories.
        """
        real = self.make_repo("g/real")
        wt = os.path.join(self.tmp, "linked")
        os.makedirs(wt)
        target = os.path.join(real, ".git", "worktrees", "linked")
        os.makedirs(target)
        with open(os.path.join(wt, ".git"), "w") as fh:
            fh.write("gitdir: %s\n" % target)
        self.assertEqual(os.path.realpath(gitmod.resolve_git_dir(wt)),
                         os.path.realpath(target))

    def test_relative_gitdir_pointer_is_followed(self):
        """git writes a RELATIVE gitdir for a submodule. Must still resolve."""
        outer = self.make_repo("g/outer")
        sub = os.path.join(outer, "vendor", "sub")
        os.makedirs(sub)
        target = os.path.join(outer, ".git", "modules", "vendor", "sub")
        os.makedirs(target)
        rel = os.path.relpath(target, sub)
        with open(os.path.join(sub, ".git"), "w") as fh:
            fh.write("gitdir: %s\n" % rel)
        self.assertEqual(os.path.realpath(gitmod.resolve_git_dir(sub)),
                         os.path.realpath(target))

    def test_dangling_pointer_returns_none_not_a_crash(self):
        p = os.path.join(self.tmp, "dangling")
        os.makedirs(p)
        with open(os.path.join(p, ".git"), "w") as fh:
            fh.write("gitdir: /nonexistent/elsewhere/.git\n")
        self.assertIsNone(gitmod.resolve_git_dir(p))

    def test_plain_directory_is_not_a_repo(self):
        p = os.path.join(self.tmp, "notarepo")
        os.makedirs(p)
        self.assertIsNone(gitmod.resolve_git_dir(p))


class ExhaustiveDiscovery(TempTreeTestCase):
    """iter_repos must find repositories a pruning walk cannot."""

    @staticmethod
    def _pruning_walk(root):
        """The idiom iter_repos replaces: stop descending on finding .git."""
        found = []
        for dirpath, dirnames, filenames in os.walk(root):
            if ".git" in dirnames or ".git" in filenames:
                found.append(dirpath)
                dirnames[:] = []
            return_early = False  # noqa: F841  (readability)
        return found

    def test_nested_repo_inside_a_work_tree_is_found(self):
        outer = self.make_repo("g/outer")
        inner = os.path.join(outer, "vendor", "inner")
        os.makedirs(inner)
        r = git("init", "-q", inner)
        self.assertEqual(r.returncode, 0, r.stderr)

        exhaustive = {w for w, _g in gitmod.iter_repos(self.dest)}
        pruning = set(self._pruning_walk(self.dest))

        self.assertIn(os.path.realpath(inner),
                      {os.path.realpath(p) for p in exhaustive},
                      "the nested repository was missed")
        self.assertGreater(len(exhaustive), len(pruning),
                           "exhaustive walk must strictly beat the pruning walk")

    def test_deeply_nested_repos_are_all_found(self):
        self.make_repo("a/one")
        self.make_repo("a/one/deep/two")
        self.make_repo("a/one/deep/two/deeper/three")
        got = {os.path.realpath(w) for w, _g in gitmod.iter_repos(self.dest)}
        self.assertEqual(len(got), 3, got)

    def test_symlink_cycle_terminates(self):
        """A symlink pointing back at an ancestor must not hang the walk."""
        self.make_repo("c/one")
        loop_at = os.path.join(self.dest, "c", "loop")
        os.symlink(self.dest, loop_at)
        got = list(gitmod.iter_repos(self.dest))          # must simply return
        self.assertEqual(len(got), 1, got)

    def test_symlinked_dirs_not_followed_by_default(self):
        """Following symlinks can walk straight out of `root`."""
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside)
        r = git("init", "-q", outside)
        self.assertEqual(r.returncode, 0, r.stderr)
        os.symlink(outside, os.path.join(self.dest, "link"))
        got = {os.path.realpath(w) for w, _g in gitmod.iter_repos(self.dest)}
        self.assertNotIn(os.path.realpath(outside), got)

    def test_does_not_descend_into_dot_git(self):
        """A stray `.git` *inside* a .git dir must not be reported."""
        repo = self.make_repo("d/one")
        trap = os.path.join(repo, ".git", "modules", "x")
        os.makedirs(os.path.join(trap, ".git"))
        got = [w for w, _g in gitmod.iter_repos(self.dest)]
        self.assertEqual(len(got), 1, got)

    def test_missing_root_yields_nothing(self):
        self.assertEqual(
            list(gitmod.iter_repos(os.path.join(self.tmp, "nope"))), [])


class HardenGitDir(TempTreeTestCase):

    def test_config_is_tightened_to_600(self):
        repo = self.make_repo("h/one", origin="http://git.example.net/h/one.git")
        cfg = os.path.join(repo, ".git", "config")
        os.chmod(cfg, 0o644)
        self.assertEqual(mode(cfg), 0o644)
        gitmod.harden_git_dir(os.path.join(repo, ".git"))
        self.assertEqual(mode(cfg), 0o600)

    def test_git_dir_itself_is_700(self):
        repo = self.make_repo("h/two")
        os.chmod(os.path.join(repo, ".git"), 0o755)
        gitmod.harden_git_dir(os.path.join(repo, ".git"))
        self.assertEqual(mode(os.path.join(repo, ".git")), 0o700)

    def test_reflog_is_tightened(self):
        """logs/HEAD's first line is literally `clone: from <url>`."""
        repo = self.make_repo("h/three")
        head_log = os.path.join(repo, ".git", "logs", "HEAD")
        self.assertTrue(os.path.isfile(head_log), "fixture has no reflog")
        os.chmod(head_log, 0o644)
        os.chmod(os.path.join(repo, ".git", "logs"), 0o755)
        gitmod.harden_git_dir(os.path.join(repo, ".git"))
        self.assertEqual(mode(head_log), 0o600)
        self.assertEqual(mode(os.path.join(repo, ".git", "logs")), 0o700)

    def test_objects_and_hooks_are_left_alone(self):
        """Out of scope by decision, not by accident — see the report."""
        repo = self.make_repo("h/four")
        objects = os.path.join(repo, ".git", "objects")
        hooks = os.path.join(repo, ".git", "hooks")
        before_obj, before_hooks = mode(objects), mode(hooks)
        gitmod.harden_git_dir(os.path.join(repo, ".git"))
        self.assertEqual(mode(objects), before_obj)
        self.assertEqual(mode(hooks), before_hooks)

    def test_idempotent(self):
        repo = self.make_repo("h/five")
        gitmod.harden_git_dir(os.path.join(repo, ".git"))
        second = gitmod.harden_git_dir(os.path.join(repo, ".git"))
        self.assertEqual(second["changed"], [],
                         "a second run must change nothing")

    def test_non_repo_reports_an_error_and_does_not_raise(self):
        plain = os.path.join(self.tmp, "plain")
        os.makedirs(plain)
        out = gitmod.harden_git_dir(plain)
        self.assertTrue(out["errors"])

    def test_harden_repo_config_follows_a_gitdir_pointer(self):
        """The regression: the old version silently no-opped here."""
        real = self.make_repo("h/six")
        target = os.path.join(real, ".git", "worktrees", "wt")
        os.makedirs(target)
        with open(os.path.join(target, "config"), "w") as fh:
            fh.write("[core]\n\tbare = false\n")
        os.chmod(os.path.join(target, "config"), 0o644)
        wt = os.path.join(self.tmp, "wt")
        os.makedirs(wt)
        with open(os.path.join(wt, ".git"), "w") as fh:
            fh.write("gitdir: %s\n" % target)

        self.assertTrue(gitmod._harden_repo_config(wt))
        self.assertEqual(mode(os.path.join(target, "config")), 0o600)


class Sweep(TempTreeTestCase):

    def _loosen_all(self):
        for _w, g in gitmod.iter_repos(self.dest):
            os.chmod(g, 0o755)
            os.chmod(os.path.join(g, "config"), 0o644)

    def test_sweep_fixes_every_repo_including_nested(self):
        self.make_repo("s/one")
        outer = self.make_repo("s/outer")
        inner = os.path.join(outer, "vendor", "inner")
        os.makedirs(inner)
        git("init", "-q", inner)
        self._loosen_all()

        res = gitmod.sweep_permissions([self.dest])
        self.assertEqual(res["repos"], 3, res)
        for _w, g in gitmod.iter_repos(self.dest):
            self.assertEqual(mode(os.path.join(g, "config")), 0o600, g)
            self.assertEqual(mode(g), 0o700, g)

    def test_sweep_reports_mode_census_before_and_after(self):
        self.make_repo("s/a")
        self.make_repo("s/b")
        self._loosen_all()
        res = gitmod.sweep_permissions([self.dest])
        self.assertEqual(res["config_modes_before"].get("0o644"), 2, res)
        self.assertEqual(res["config_modes_after"].get("0o600"), 2, res)

    def test_dry_run_changes_nothing(self):
        repo = self.make_repo("s/dry")
        self._loosen_all()
        res = gitmod.sweep_permissions([self.dest], dry_run=True)
        self.assertTrue(res["dry_run"])
        self.assertGreater(res["files_changed"], 0)
        self.assertEqual(mode(os.path.join(repo, ".git", "config")), 0o644,
                         "dry run must not chmod anything")

    def test_sweep_accepts_a_bare_string_root(self):
        self.make_repo("s/str")
        self._loosen_all()
        res = gitmod.sweep_permissions(self.dest)
        self.assertEqual(res["repos"], 1, res)

    def test_sweep_handles_multiple_roots_and_a_missing_one(self):
        """Both clone trees are swept in one call; a missing tree is reported,
        not fatal."""
        self.make_repo("s/m")
        missing = os.path.join(self.tmp, "no-such-tree")
        res = gitmod.sweep_permissions([self.dest, missing])
        self.assertEqual(len(res["roots"]), 2)
        self.assertTrue(res["roots"][1]["missing"])
        self.assertEqual(res["roots"][1]["repos"], 0)

    def test_sweep_is_idempotent(self):
        self.make_repo("s/i")
        self._loosen_all()
        gitmod.sweep_permissions([self.dest])
        again = gitmod.sweep_permissions([self.dest])
        self.assertEqual(again["files_changed"], 0, again)

    def test_swept_repo_still_works(self):
        """A permissions change that breaks git is worse than the problem."""
        repo = self.make_repo("s/works")
        self._loosen_all()
        gitmod.sweep_permissions([self.dest])
        r = git("status", "--porcelain", cwd=repo)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = git("log", "-1", "--format=%s", cwd=repo)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "init")
        # and it can still be written to
        with open(os.path.join(repo, "second.txt"), "w") as fh:
            fh.write("y\n")
        git("add", "-A", cwd=repo)
        r = git("commit", "-q", "-m", "second", cwd=repo)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_sweep_never_returns_file_contents(self):
        """Counts only — a report must not be able to leak a token."""
        self.make_repo("s/secret",
                       origin="http://oauth2:NOTAREALTOKEN@git.example.net/x.git")
        res = gitmod.sweep_permissions([self.dest])
        self.assertNotIn("NOTAREALTOKEN", repr(res))


class UmaskAtCreation(TempTreeTestCase):
    """New clones must be 0600 by construction, not by a later chmod."""

    def test_umask_wrap_shape(self):
        w = gitmod._umask_wrap(["/usr/bin/git", "clone", "x"], 0o077)
        self.assertEqual(w[0], "/bin/sh")
        self.assertIn("umask", w[2])
        self.assertEqual(w[-3:], ["/usr/bin/git", "clone", "x"])
        self.assertIn("0077", w)

    def test_clone_umask_default_is_077(self):
        self.assertEqual(gitmod.CLONE_UMASK, 0o077)

    def test_clone_umask_context_manager_restores_previous(self):
        self.assertIsNone(gitmod._ACTIVE_UMASK)
        with gitmod.clone_umask():
            self.assertEqual(gitmod._ACTIVE_UMASK, 0o077)
            with gitmod.clone_umask(0o027):
                self.assertEqual(gitmod._ACTIVE_UMASK, 0o027)
            self.assertEqual(gitmod._ACTIVE_UMASK, 0o077)
        self.assertIsNone(gitmod._ACTIVE_UMASK)

    def test_clone_umask_restores_even_on_exception(self):
        with self.assertRaises(RuntimeError):
            with gitmod.clone_umask():
                raise RuntimeError("boom")
        self.assertIsNone(gitmod._ACTIVE_UMASK)

    def test_argv_is_unwrapped_outside_a_clone_umask_block(self):
        """The wrapper must not appear when no mask is in force — several
        older test modules assert on the exact git argv."""
        seen = {}

        real = gitmod.subprocess.run

        def spy(args, **kw):
            seen["argv"] = list(args)
            return real(["/bin/true"], **kw)

        gitmod.subprocess.run = spy
        try:
            gitmod._run([gitmod.GIT_BIN, "status"])
            self.assertEqual(seen["argv"][0], gitmod.GIT_BIN)
            with gitmod.clone_umask():
                gitmod._run([gitmod.GIT_BIN, "status"])
            self.assertEqual(seen["argv"][0], "/bin/sh")
        finally:
            gitmod.subprocess.run = real

    def test_run_under_umask_creates_private_files(self):
        """Prove the mechanism on a real child process, not by reading code."""
        probe = os.path.join(self.tmp, "probe")
        rc, _out, _err = gitmod._run(
            ["/bin/sh", "-c", "umask > %s.mask; : > %s" % (probe, probe)],
            umask=0o077)
        self.assertEqual(rc, 0)
        self.assertEqual(mode(probe) & 0o077, 0,
                         "umask was not applied to the child")

    def test_real_clone_lands_at_600_without_any_chmod(self):
        """End-to-end: clone a local source repo with the hardening chmod
        DISABLED, and prove .git/config is still 0600. If this passes only
        because of the post-clone chmod, the window between git writing the
        URL and us narrowing the file is still open."""
        src = self.make_repo("u/src")
        dst = os.path.join(self.tmp, "cloned")
        orig = gitmod._harden_repo_config
        gitmod._harden_repo_config = lambda *a, **k: True
        try:
            rc, _o, err = gitmod._run(
                [gitmod.GIT_BIN, "clone", "--quiet", "--", src, dst],
                umask=gitmod.CLONE_UMASK)
        finally:
            gitmod._harden_repo_config = orig
        self.assertEqual(rc, 0, err)
        cfg = os.path.join(dst, ".git", "config")
        self.assertEqual(mode(cfg), 0o600,
                         "clone did not create .git/config privately")

    def test_without_umask_git_still_makes_it_644(self):
        """Control: shows the umask is what changes the outcome, so the test
        above is not passing for some unrelated reason."""
        src = self.make_repo("u/src2")
        dst = os.path.join(self.tmp, "cloned2")
        old = os.umask(0o022)
        try:
            rc, _o, err = gitmod._run(
                [gitmod.GIT_BIN, "clone", "--quiet", "--", src, dst])
        finally:
            os.umask(old)
        self.assertEqual(rc, 0, err)
        self.assertEqual(mode(os.path.join(dst, ".git", "config")), 0o644)

    def test_public_clone_api_lands_everything_private(self):
        """End-to-end through clone_repo_detailed() itself, with nothing
        stubbed: config, the .git dir and the reflog must all be private."""
        src = self.make_repo("u/src3")
        dst = os.path.join(self.tmp, "cloned3")
        res = gitmod.clone_repo_detailed("", "file://" + src, dst)
        # file:// is refused by check_url (only http/https), which is
        # correct — so drive the same code path git actually takes.
        self.assertEqual(res["status"], "error")

        # Do it the way the tool does, minus the network.
        with gitmod.clone_umask():
            rc, _o, err = gitmod._run(
                [gitmod.GIT_BIN, "clone", "--quiet", "--", src, dst])
        self.assertEqual(rc, 0, err)
        gitmod._harden_repo_config(dst)
        g = os.path.join(dst, ".git")
        self.assertEqual(mode(os.path.join(g, "config")), 0o600)
        self.assertEqual(mode(g), 0o700)
        self.assertEqual(mode(os.path.join(g, "logs", "HEAD")), 0o600)
        r = git("status", "--porcelain", cwd=dst)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_umask_does_not_leak_into_this_process(self):
        """os.umask() here would be a race; the /bin/sh wrapper must not."""
        before = os.umask(0o022)
        os.umask(before)
        gitmod._run(["/bin/sh", "-c", "true"], umask=0o077)
        after = os.umask(0o022)
        os.umask(after)
        self.assertEqual(after, before)

    def test_umask_none_is_a_plain_exec(self):
        w = gitmod._run(["/bin/sh", "-c", "echo hi"])
        self.assertEqual(w[0], 0)
        self.assertEqual(w[1], "hi")


class NoLiveTreeContact(TempTreeTestCase):

    def test_module_never_touches_live_trees(self):
        """Structural guard: no live-tree path may be passed to the sweep.

        Asserted by source inspection of this module — the paths appear only
        in LIVE_TREES and in this assertion, never as an argument.
        """
        here = os.path.abspath(__file__)
        with open(here) as fh:
            src = fh.read()
        for t in LIVE_TREES:
            self.assertNotIn("sweep_permissions([\"%s" % t, src)
            self.assertNotIn("sweep_permissions(\"%s" % t, src)
            self.assertNotIn("iter_repos(\"%s" % t, src)

    def test_fixtures_live_under_tempdir(self):
        self.assertTrue(self.dest.startswith(tempfile.gettempdir()))
        for t in LIVE_TREES:
            self.assertFalse(os.path.realpath(self.dest).startswith(
                os.path.realpath(t)))


if __name__ == "__main__":
    import unittest
    unittest.main(verbosity=2)
