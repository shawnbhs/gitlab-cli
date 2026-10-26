# tests/test_w10_remote_name_injection.py — W10: argument injection is a CLASS,
# not a call site.
#
# An earlier wave closed it for one string: check_url() stops a clone URL that
# begins with `-` or uses a non-http transport. The URL is not the only value
# of ours that becomes a git argument, and it is not the only one an attacker
# controls.
#
# A remote NAME is attacker-controlled. Git validates a remote name when it is
# created (`git remote add`) and never when it is read: `git remote` prints the
# `[remote "..."]` section headers of .git/config verbatim. This tool walks
# repositories it did not create, so a hostile .git/config is the ordinary
# case. From the name the sync path builds `f"{remote}/{branch}"` and hands it
# to rev-parse, rev-list, log, merge and `branch --set-upstream-to=`.
#
# The proof below is a real one, not an assertion about strings: a repository
# whose remote is literally named `--output=<file>` makes real git create
# <file> on the pre-fix code, with exit status 0. Every exploit test asserts on
# the SIDE EFFECT (a marker file), never on a return code — git returns 0 after
# doing the attacker's work.
#
# Offline: real git, real temp dirs, no network, no token, no live error log
# (via _bootstrap -> _error_log_guard).

import os
import shutil
import subprocess
import tempfile
import unittest

import _bootstrap  # noqa: F401  (sys.path + live-error-log isolation)
from _bootstrap import TempTreeTestCase, git

import git as gitmod


MARKER_DIR = None          # per-test temp dir the payload would write into


def _marker(tmp, name="PWNED"):
    return os.path.join(tmp, name)


def _add_hostile_remote(repo, name, url="http://example.invalid/x.git"):
    """Append a `[remote "<name>"]` stanza that `git remote add` would refuse.

    This is exactly how a hostile repository reaches this tool: someone else
    wrote the config file. Nothing validates it on the way in.
    """
    cfg = os.path.join(repo, ".git", "config")
    with open(cfg, "a", encoding="utf-8") as fh:
        fh.write(
            '[remote "%s"]\n\turl = %s\n'
            '\tfetch = +refs/heads/*:refs/remotes/evil/*\n' % (name, url))


class GitItselfExecutesTheseArguments(unittest.TestCase):
    """Ground truth: the payloads used below really do something on this git.

    If this test fails the rest prove nothing — the exploit would be blocked
    by the git binary rather than by our code.
    """

    def test_log_writes_a_file_when_a_ref_is_an_option(self):
        tmp = tempfile.mkdtemp(prefix="w10_truth_")
        self.addCleanup(shutil.rmtree, tmp, True)
        repo = os.path.join(tmp, "r")
        os.makedirs(repo)
        git("init", "-q", repo)
        git("commit", "-q", "--allow-empty", "-m", "x", cwd=repo)

        target = _marker(tmp, "GIT_REALLY_WRITES_THIS")
        r = subprocess.run(
            [gitmod.GIT_BIN, "-C", repo, "log", "-1", "--format=%ct",
             "--output=" + target],
            capture_output=True, text=True, timeout=30)
        self.assertTrue(
            os.path.exists(target),
            "this git (%s) does not honour --output; the exploit tests below "
            "would be vacuous" % r.stderr.strip())


class RemoteNameIsValidated(unittest.TestCase):
    """The validator itself — an allow-list, not a list of known-bad options."""

    def test_an_option_shaped_remote_name_is_rejected(self):
        for bad in ("--upload-pack=touch /tmp/x",
                    "--output=/tmp/x",
                    "-u/tmp/x",
                    "--exec=sh",
                    "-c"):
            with self.subTest(bad=bad):
                self.assertFalse(gitmod.is_safe_remote_name(bad))
                with self.assertRaises(gitmod.UnsafeRemote):
                    gitmod.check_remote_name(bad)

    def test_a_newline_in_a_remote_name_is_rejected(self):
        for bad in ("origin\nupstream", "origin\r", "orig\x00in"):
            with self.subTest(bad=bad):
                self.assertFalse(gitmod.is_safe_remote_name(bad))

    def test_empty_is_rejected(self):
        self.assertFalse(gitmod.is_safe_remote_name(""))
        self.assertFalse(gitmod.is_safe_remote_name(None))

    def test_real_remote_names_still_pass(self):
        for good in ("origin", "upstream", "gitlab-2", "fork.old",
                     "team/mirror", "o"):
            with self.subTest(good=good):
                self.assertTrue(gitmod.is_safe_remote_name(good))
                self.assertEqual(gitmod.check_remote_name(good), good)

    def test_the_refused_name_is_not_echoed_raw_into_the_message(self):
        """The name can carry a token (it is attacker-supplied text)."""
        try:
            gitmod.check_remote_name("--upload-pack=curl h?t=glpat-" + "A" * 20)
        except gitmod.UnsafeRemote as e:
            self.assertNotIn("glpat-" + "A" * 20, str(e))
        else:
            self.fail("not refused")


class RefIsValidated(unittest.TestCase):

    def test_option_shaped_refs_are_rejected(self):
        for bad in ("--output=/tmp/x", " --upload-pack=sh", "-c", "--all"):
            with self.subTest(bad=bad):
                with self.assertRaises(gitmod.UnsafeRef):
                    gitmod.check_ref(bad)

    def test_control_characters_are_rejected(self):
        for bad in ("main\nupstream", "main\r", "ma\x00in"):
            with self.subTest(bad=bad):
                with self.assertRaises(gitmod.UnsafeRef):
                    gitmod.check_ref(bad)

    def test_the_refs_this_tool_really_builds_still_pass(self):
        for good in ("main", "origin/main", "main^{commit}", "@{u}",
                     "refs/heads/feature/x", "HEAD~3"):
            with self.subTest(good=good):
                self.assertEqual(gitmod.check_ref(good), good)


class DangerousArgvIsRefusedByTheRunner(unittest.TestCase):
    """The backstop: _run() refuses a command-executing option in ANY git argv.

    This is what covers the call sites this task does not own (remotes.py's
    `remote set-url <name>`, clone.py's fetch/update-ref), without two
    subagents writing the same file.
    """

    def test_run_refuses_and_reports_a_failure_rather_than_raising(self):
        tmp = tempfile.mkdtemp(prefix="w10_backstop_")
        self.addCleanup(shutil.rmtree, tmp, True)
        target = _marker(tmp)
        rc, out, err = gitmod._run(
            [gitmod.GIT_BIN, "log", "-1", "--output=" + target])
        self.assertNotEqual(rc, 0)
        self.assertFalse(os.path.exists(target),
                         "git ran despite the refusal")
        self.assertIn("unsafe", err.lower())

    def test_every_documented_vector_is_refused(self):
        for bad in ("--upload-pack=touch /tmp/x", "--receive-pack=sh",
                    "--exec=sh", "--output=/tmp/x", "--config=core.x=y",
                    "-c", "-ccore.sshCommand=sh", "-u/tmp/x", "-o/tmp/x"):
            with self.subTest(bad=bad):
                self.assertEqual(
                    gitmod.unsafe_git_arg([gitmod.GIT_BIN, "fetch", bad]), bad)

    def test_the_argv_this_tool_really_builds_is_not_refused(self):
        clean = [
            [gitmod.GIT_BIN, "clone", "--quiet", "--", "http://h/x.git", "/d"],
            [gitmod.GIT_BIN, "-C", "/d", "fetch", "--all", "--prune",
             "--tags", "--quiet"],
            [gitmod.GIT_BIN, "-C", "/d", "log", "-1", "--format=%ct", "main"],
            [gitmod.GIT_BIN, "-C", "/d", "rev-list", "--left-right",
             "--count", "main...origin/main"],
            [gitmod.GIT_BIN, "-C", "/d", "remote", "set-url", "origin",
             "http://h/x.git"],
            [gitmod.GIT_BIN, "-C", "/d", "branch",
             "--set-upstream-to=origin/main", "main"],
            [gitmod.GIT_BIN, "credential", "approve"],
        ]
        for argv in clean:
            with self.subTest(argv=argv[2:5]):
                self.assertIsNone(gitmod.unsafe_git_arg(argv))

    def test_a_positional_after_a_double_dash_is_left_alone(self):
        """`--` is the whole point: after it, a value is a value."""
        self.assertIsNone(gitmod.unsafe_git_arg(
            [gitmod.GIT_BIN, "clone", "--", "--output=not-an-option", "/d"]))

    def test_a_non_git_argv_is_not_judged_by_gits_option_grammar(self):
        """The umask wrapper is `/bin/sh -c ...` — that `-c` is not injection."""
        self.assertIsNone(gitmod.unsafe_git_arg(
            ["/bin/sh", "-c", 'umask "$1"; shift; exec "$@"', "sh", "0077",
             gitmod.GIT_BIN, "clone"]))

    def test_the_umask_wrapper_still_runs(self):
        rc, out, _ = gitmod._run(["/bin/sh", "-c", "echo hi"], umask=0o077)
        self.assertEqual(rc, 0)
        self.assertEqual(out, "hi")


class HostileRemoteNameDoesNotReachGit(TempTreeTestCase):
    """End-to-end, against real git, on a repository this tool did not create."""

    def setUp(self):
        super().setUp()
        # The payload's drop zone. It EXISTS, because `git --output=<path>`
        # needs its parent directory to exist — the realistic payload is a
        # remote named after a directory that is already there.
        self.drop = os.path.join(self.tmp, "drop")
        os.makedirs(self.drop, exist_ok=True)

    def assertNothingDropped(self, why):
        self.assertEqual(sorted(os.listdir(self.drop)), [], why)

    def _hostile_repo(self, name, rel="grp/proj"):
        repo = self.make_repo(rel, origin=None)
        _add_hostile_remote(repo, name)
        return repo

    def test_the_payload_is_dropped_from_the_remote_list(self):
        repo = self._hostile_repo("--output=" + self.drop)
        # Sanity: git itself really does report the hostile name.
        r = git("remote", cwd=repo)
        self.assertIn("--output=", r.stdout)
        self.assertEqual(gitmod._remotes(repo), [],
                         "an option-shaped remote name survived _remotes()")

    def test_ref_time_does_not_write_the_attackers_file(self):
        """The exact ref the sync path builds: f"{remote}/{branch}"."""
        repo = self._hostile_repo("--output=" + self.drop)
        branch = git("symbolic-ref", "--short", "HEAD",
                     cwd=repo).stdout.strip() or "master"
        gitmod._ref_time(repo, "--output=%s/%s" % (self.drop, branch))
        self.assertNothingDropped("arbitrary file write via a ref argument")

    def test_ref_exists_refuses_an_option_shaped_ref(self):
        repo = self.make_repo("grp/proj2", origin=None)
        self.assertFalse(gitmod._ref_exists(repo, "--output=" + self.drop))
        self.assertTrue(gitmod._ref_exists(repo, "HEAD"))
        self.assertNothingDropped("rev-parse wrote the attacker's file")

    def test_counts_refuses_an_option_shaped_ref(self):
        repo = self.make_repo("grp/proj3", origin=None)
        self.assertEqual(
            gitmod._counts(repo, "master", "--output=%s/m" % self.drop),
            (0, 0))
        self.assertNothingDropped("rev-list wrote the attacker's file")

    def test_sync_repo_on_a_hostile_repo_writes_nothing(self):
        """The whole sync path, end to end, on a repository built to attack it."""
        rce = os.path.join(self.drop, "RCE")
        repo = self._hostile_repo("--upload-pack=touch " + rce)
        _add_hostile_remote(repo, "--output=" + self.drop,
                            url="http://example.invalid/y.git")
        res = gitmod.sync_repo(repo)
        self.assertNothingDropped("a payload ran during sync_repo()")
        # And it degrades honestly rather than pretending the repo is fine.
        self.assertEqual(res["status"], gitmod.S_NO_REMOTE)

    def test_a_real_remote_alongside_a_hostile_one_still_works(self):
        """Refusing the bad name must not disable the good one."""
        repo = self.make_repo("grp/proj4",
                              origin="http://git.example.net/grp/proj4.git")
        _add_hostile_remote(repo, "--output=" + self.drop)
        self.assertEqual(gitmod._remotes(repo), ["origin"])

    def test_other_branches_behind_refuses_a_hostile_remote(self):
        repo = self.make_repo("grp/proj5", origin=None)
        self.assertEqual(
            gitmod._other_branches_behind(repo, "--output=" + self.drop,
                                          "master"),
            [])
        self.assertNothingDropped("for-each-ref path wrote the attacker's file")


class HostileHeadDoesNotReachGit(TempTreeTestCase):
    """HEAD is a file in a repository we did not write, like any other."""

    def test_sync_repo_refuses_an_option_shaped_branch(self):
        drop = os.path.join(self.tmp, "drop_head")
        os.makedirs(drop)
        repo = self.make_repo("grp/head", origin=None)
        git("remote", "add", "origin", "http://example.invalid/z.git", cwd=repo)
        with open(os.path.join(repo, ".git", "HEAD"), "w") as fh:
            fh.write("ref: refs/heads/--output=%s\n" % drop)
        res = gitmod.sync_repo(repo)
        self.assertEqual(sorted(os.listdir(drop)), [],
                         "a payload in HEAD reached git")
        self.assertIn(res["status"], (gitmod.S_ERROR, gitmod.S_GONE,
                                      gitmod.S_AUTH, gitmod.S_DETACHED))


class LogFiltersAreValidated(TempTreeTestCase):
    """`--since=`/`--author=` are built from values that cross a boundary."""

    def test_an_option_shaped_since_is_refused(self):
        repo = self.make_repo("grp/log", origin=None)
        drop = os.path.join(self.tmp, "drop_log")
        os.makedirs(drop)
        self.assertEqual(
            gitmod.get_commits_since(repo, "--output=%s/x" % drop), [])
        self.assertEqual(sorted(os.listdir(drop)), [])

    def test_an_option_shaped_author_is_refused(self):
        repo = self.make_repo("grp/log2", origin=None)
        self.assertEqual(
            gitmod.get_commits_since(repo, "1 year ago", author="--output=/tmp/n"),
            [])

    def test_a_normal_query_still_returns_commits(self):
        repo = self.make_repo("grp/log3", origin=None)
        rows = gitmod.get_commits_since(repo, "1 year ago")
        self.assertTrue(rows, "validation broke the ordinary log path")
        self.assertEqual(rows[0]["message"], "init")


class TheClassIsClosedEverywhereItIsCalled(unittest.TestCase):
    """Every module that shells out to git goes through the guarded runner.

    A source-level check on purpose: it is the thing that regresses. A new
    call site added in remotes.py or clone.py with its own subprocess.run
    would reopen the class silently, and no behavioural test would notice
    until someone wrote one.
    """

    MODULES = ("remotes.py", "clone.py", "pull.py", "credentials.py",
               "caps.py", "members.py", "instances.py")

    # remotes.py's `git credential approve` is a stdin-protocol call with a
    # fully literal argv — no attacker value reaches it — and it is validated
    # field-by-field by remotes._credential_approve(). It is named here so the
    # test stays a guard against NEW bypasses rather than a snapshot.
    ALLOWED = {("remotes.py", "credential")}

    def test_no_module_builds_its_own_unguarded_git_subprocess_call(self):
        from _bootstrap import PROJECT_ROOT
        offenders = []
        for mod in self.MODULES:
            p = os.path.join(PROJECT_ROOT, mod)
            if not os.path.exists(p):
                continue
            with open(p, encoding="utf-8") as fh:
                lines = fh.readlines()
            for n, line in enumerate(lines, 1):
                s = line.strip()
                if s.startswith("#") or (
                        "subprocess.run" not in s and "subprocess.Popen" not in s):
                    continue
                window = "".join(lines[n - 1:n + 4])
                if any(mod == m and tok in window for m, tok in self.ALLOWED):
                    continue
                offenders.append("%s:%d %s" % (mod, n, s[:60]))
        self.assertEqual(
            offenders, [],
            "a git subprocess call bypasses git._run()'s argument guard")

    def test_the_argv_remotes_builds_is_understood_by_the_guard(self):
        """remotes.scrub() rewrites URLs using a name read from a config."""
        argv = [gitmod.GIT_BIN, "-C", "/nowhere", "remote", "set-url",
                "--output=/tmp/w10_nope", "http://h/x.git"]
        self.assertIsNotNone(
            gitmod.unsafe_git_arg(argv),
            "the guard does not recognise the argv remotes.py builds")
        # …and the runner actually refuses it rather than just reporting it.
        rc, _, err = gitmod._run(argv)
        self.assertNotEqual(rc, 0)
        self.assertIn("unsafe", err.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
